"""
futures_position_monitor.py — Position monitoring for Binance Futures.

Extracted from core/futures_trade_executor.py (god file) — Tahap 5 refactor.
Mirrors the pattern of core/executors/spot_position_monitor.py.

Wraps check_futures_positions() and its two inner helpers as private methods
of FuturesPositionMonitor.  Business logic is identical to the god file;
only structural change: closure vars become explicit method parameters.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services import chart_analyzer as ca
from core.utils.telegram import send_telegram as _send_telegram


class FuturesPositionMonitor:
    """
    Monitors open Futures positions and drives their state machine.
    Mirrors SpotPositionMonitor.

    Usage:
        monitor = FuturesPositionMonitor(client)
        monitor.check_positions(verbose=False)
    """

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def check_positions(self, verbose: bool = False) -> None:
        """Drive the state machine for every OPEN futures trade."""
        from core.futures_trade_executor import (
            load_futures_log, accrue_funding,
            compute_mae_mfe_from_candles, LEVERAGE, MARGIN_MODE,
        )
        from core.executors.futures_order_executor import FuturesOrderExecutor
        from services.supabase_client import update_futures_by_order_id

        client      = self.client
        trades      = load_futures_log()
        open_trades = [t for t in trades if t.get("exit_status") == "OPEN"]
        log_dirty   = False

        if not open_trades:
            print("\n  No open futures positions.")
            for t in ([t for t in trades if t.get("exit_status") != "OPEN"][-5:]):
                pnl  = f"${t['realized_pnl_usd']:+.2f}" if t.get("realized_pnl_usd") else "n/a"
                print(f"    {t['symbol']:10} {t.get('position_side','?'):5} "
                      f"{t['exit_status']:10}  PnL: {pnl}")
            return

        print(f"\n  ── OPEN FUTURES POSITIONS: {len(open_trades)} ──")
        print(f"  {'Symbol':<12} {'Side':<6} {'Status':<22} {'Unreal PnL':>12}  Exit Orders")
        print(f"  {'─'*65}")

        resolved_this_run: list[tuple] = []
        try:
            all_tickers = {t["symbol"]: float(t["price"])
                           for t in client.futures_symbol_ticker()}
        except Exception:
            all_tickers = {}

        for trade in open_trades:
            sym  = trade["symbol"]
            side = trade.get("position_side", "LONG")
            eid  = trade.get("entry_order_id")

            # Step 1: entry order status
            try:
                entry_order  = client.futures_get_order(symbol=sym, orderId=eid)
                entry_status = entry_order.get("status", "UNKNOWN")
            except Exception as e:
                print(f"  {sym:<12} ⚠ Could not query: {e}")
                continue

            filled_qty = float(entry_order.get("executedQty", 0))
            cum_quote  = float(entry_order.get("cumQuote", 0))
            if filled_qty > 0 and cum_quote > 0:
                fill_price = cum_quote / filled_qty
            else:
                _avg = float(entry_order.get("avgPrice", 0))
                if _avg > 0:
                    fill_price = _avg
                else:
                    _resolved = False
                    try:
                        my_trades = client.futures_account_trades(
                            symbol=sym, orderId=eid, limit=5)
                        if my_trades:
                            tq  = sum(float(t["qty"])      for t in my_trades)
                            tqq = sum(float(t["quoteQty"]) for t in my_trades)
                            if tq > 0 and tqq > 0:
                                fill_price = tqq / tq
                                _resolved  = True
                    except Exception:
                        pass
                    if not _resolved:
                        fill_price = float(
                            entry_order.get("price", trade["entry_price"]))

            if trade.get("entry_status") != entry_status:
                trade["entry_status"] = entry_status
                log_dirty = True

            if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
                planned = trade.get("entry_price", fill_price)
                trade["entry_fill_price"] = fill_price
                trade["entry_fill_time"]  = int(entry_order.get("updateTime", 0))
                trade["entry_qty"]        = filled_qty
                trade["slippage_pct"]     = (
                    round((fill_price - planned) / planned * 100, 4)
                    if planned else None
                )
                trade["last_funding_check_time"] = trade["entry_fill_time"]
                log_dirty = True
                _send_telegram(
                    f"✅ [FUTURES] Filled: {sym} {side}"
                    f" @ {ca._fmt_price(fill_price).strip()}"
                    f" | SL: {ca._fmt_price(trade.get('sl')).strip()}"
                    f" | TP: {ca._fmt_price(trade.get('tp1')).strip()}"
                    f" | Liq: {ca._fmt_price(trade.get('liquidation_price')).strip()}"
                )

            # Step 2: place exit orders if filled and not yet placed
            if entry_status == "FILLED" and not trade.get("exit_orders_placed"):
                print(f"  {sym:<12} ✅ FILLED — placing TP + SL orders...")
                exit_result = FuturesOrderExecutor(client).place_exit_orders(trade)
                trade["tp_order_id"]        = exit_result.get("tp_order_id")
                trade["sl_order_id"]        = exit_result.get("sl_order_id")
                trade["tp_algo_id"]         = exit_result.get("tp_algo_id")
                trade["sl_algo_id"]         = exit_result.get("sl_algo_id")
                trade["exit_orders_placed"] = exit_result["success"]
                log_dirty = True

                if exit_result.get("emergency_exit"):
                    current = all_tickers.get(sym, fill_price)
                    pnl_usd = (current - fill_price) * filled_qty * (
                        1 if side == "LONG" else -1)
                    pnl_pct = pnl_usd / trade.get("entry_notional", 1) * 100
                    trade["exit_status"]        = "SL_HIT"
                    trade["exit_price"]         = round(current, 6)
                    trade["realized_pnl_usd"]   = round(pnl_usd, 4)
                    trade["realized_pnl_pct"]   = round(pnl_pct, 2)
                    trade["exit_orders_placed"] = False
                    log_dirty = True
                    resolved_this_run.append((sym, "SL_HIT", pnl_usd, side))
                    continue

                if not exit_result["success"]:
                    print(f"\n  {'!'*60}")
                    print(f"  !! CRITICAL: Exit orders FAILED for {sym} {side} !!")
                    print(f"  !! Position UNPROTECTED !!")
                    print(f"  {'!'*60}\n")

            # Step 2.5: accrue funding
            if entry_status == "FILLED" and trade.get("exit_status") == "OPEN":
                if accrue_funding(client, trade):
                    log_dirty = True

            # Step 3: check exit order status
            exit_str   = "n/a"
            tp_id      = trade.get("tp_order_id")
            sl_id      = trade.get("sl_order_id")
            tp_algo_id = trade.get("tp_algo_id")
            sl_algo_id = trade.get("sl_algo_id")

            if trade.get("exit_orders_placed") and (tp_id or sl_id or tp_algo_id or sl_algo_id):
                exit_status_found = exit_price_found = exit_time_from_exchange = None

                for oid, aid, otype in [
                    (tp_id, tp_algo_id, "TP"),
                    (sl_id, sl_algo_id, "SL"),
                ]:
                    if not oid and not aid:
                        continue
                    status, ep, upd = self._query_exit_order(client, oid, aid, otype)
                    if status in ("FILLED", "EXECUTED", "COMPLETED", "FINISHED"):
                        exit_status_found       = "TP_HIT" if otype == "TP" else "SL_HIT"
                        exit_price_found        = ep or float(
                            trade.get("tp1" if otype == "TP" else "sl", 0))
                        exit_time_from_exchange = upd
                        break
                    if status:
                        exit_str = status

                if exit_status_found:
                    ef       = trade.get("entry_fill_price") or trade["entry_price"]
                    qty      = trade.get("entry_qty", 0)
                    mult     = 1 if side == "LONG" else -1
                    pnl_usd  = (exit_price_found - ef) * qty * mult
                    pnl_pct  = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                    exit_ms  = (int(exit_time_from_exchange) if exit_time_from_exchange
                                else int(datetime.now(timezone.utc).timestamp() * 1000))

                    trade.update({
                        "exit_status":      exit_status_found,
                        "exit_price":       round(exit_price_found, 6),
                        "exit_time":        exit_ms,
                        "realized_pnl_usd": round(pnl_usd, 4),
                        "realized_pnl_pct": round(pnl_pct, 2),
                    })
                    if trade.get("entry_fill_time"):
                        trade["time_in_position_sec"] = (
                            exit_ms - int(trade["entry_fill_time"])) // 1000

                    self._cancel_algo_orders(client, sym, tp_algo_id, sl_algo_id)

                    if trade.get("entry_fill_time"):
                        trade.update(compute_mae_mfe_from_candles(
                            symbol            = sym,
                            position_side     = side,
                            entry_price       = ef,
                            entry_time_ms     = int(trade["entry_fill_time"]),
                            exit_time_ms      = exit_ms,
                            liquidation_price = trade.get("liquidation_price", 0),
                        ))

                    log_dirty = True
                    resolved_this_run.append((sym, exit_status_found, pnl_usd, side))
                    exit_str = (f"{'🟢' if exit_status_found == 'TP_HIT' else '🔴'} "
                                f"{exit_status_found}")
                    continue

            # Step 4: current price + display
            current = all_tickers.get(sym)
            if current is None:
                try:
                    current = float(client.futures_symbol_ticker(symbol=sym)["price"])
                except Exception:
                    current = None

            # Step 3.5: price-guard
            if (entry_status == "FILLED" and trade.get("exit_status") == "OPEN"
                    and current is not None and trade.get("exit_orders_placed")):
                sl_lv = trade.get("sl")
                breached = (
                    (side == "LONG"  and sl_lv and current <= sl_lv) or
                    (side == "SHORT" and sl_lv and current >= sl_lv)
                )
                if breached:
                    print(f"  ⚠  [{sym}] Price {current:.4f} breached SL {sl_lv:.4f} "
                          f"— closing position with reduce-only MARKET order.")
                    ef      = trade.get("entry_fill_price") or trade["entry_price"]
                    qty     = trade.get("entry_qty", 0)
                    mult    = 1 if side == "LONG" else -1
                    close_side = "SELL" if side == "LONG" else "BUY"
                    try:
                        close_resp = client.futures_create_order(
                            symbol=sym,
                            side=close_side,
                            type="MARKET",
                            quantity=str(qty),
                            positionSide="BOTH",
                            reduceOnly=True,
                        )
                    except Exception as exc:
                        print(f"  🚨 [{sym}] MARKET close failed; DB remains OPEN: {exc}")
                        _send_telegram(
                            f"🚨 [FUTURES] SL BREACH CLOSE FAILED: {sym} {side}\n"
                            f"Position remains OPEN on exchange.\n{exc}"
                        )
                        continue

                    executed_qty = float(close_resp.get("executedQty", 0) or 0)
                    cum_quote = float(close_resp.get("cumQuote", 0)
                                      or close_resp.get("cumQuoteQty", 0)
                                      or close_resp.get("cummulativeQuoteQty", 0) or 0)
                    fill_px = float(close_resp.get("avgPrice", 0) or 0)
                    if not fill_px and executed_qty > 0 and cum_quote > 0:
                        fill_px = cum_quote / executed_qty
                    fill_px = fill_px or current
                    pnl_usd = (fill_px - ef) * qty * mult
                    pnl_pct = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                    exit_ms = int(close_resp.get("updateTime", 0)
                                  or close_resp.get("time", 0)
                                  or datetime.now(timezone.utc).timestamp() * 1000)
                    trade.update({
                        "exit_status":      "SL_HIT",
                        "exit_price":       round(fill_px, 6),
                        "exit_time":        exit_ms,
                        "realized_pnl_usd": round(pnl_usd, 4),
                        "realized_pnl_pct": round(pnl_pct, 2),
                    })
                    if trade.get("entry_fill_time"):
                        trade["time_in_position_sec"] = (
                            exit_ms - int(trade["entry_fill_time"])) // 1000
                    self._cancel_algo_orders(client, sym, tp_algo_id, sl_algo_id)
                    log_dirty = True
                    resolved_this_run.append((sym, "SL_HIT", pnl_usd, side))
                    _send_telegram(
                        f"🛑 [FUTURES] SL_HIT (price-guard): {sym} {side}"
                        f" @ {ca._fmt_price(fill_px).strip()}  |  PnL: ${pnl_usd:+.2f}"
                    )
                    continue

            pnl_display = "n/a"
            if entry_status == "FILLED" and current and trade.get("entry_qty", 0) > 0:
                ref = trade.get("entry_fill_price") or trade["entry_price"]
                pnl = (current - ref) * trade.get("entry_qty", 0) * (
                    1 if side == "LONG" else -1)
                pnl_display = f"${pnl:+.3f}"
            elif entry_status in ("NEW", "PARTIALLY_FILLED") and current:
                ep   = trade.get("entry_price", current)
                dist = (ep - current) / current * 100
                pnl_display = f"{dist:+.2f}% fill"

            status_icon = {
                "NEW": "🕐 NEW (pending)", "FILLED": "✅ FILLED",
                "PARTIALLY_FILLED": "🔄 PARTIAL", "CANCELED": "❌ CANCELED",
            }.get(entry_status, entry_status)[:20]
            print(f"  {sym:<12} {side:<6} {status_icon:<22} {pnl_display:>12}  {exit_str}")

            if verbose:
                self._print_verbose_card(
                    trade, sym, side, entry_status, current, LEVERAGE, MARGIN_MODE)

        # Summary
        if resolved_this_run:
            print(f"\n  ── Resolved this run: {len(resolved_this_run)} trade(s) ──")
            for sym, status, pnl, ps in resolved_this_run:
                icon = "🟢" if status == "TP_HIT" else "🔴"
                print(f"    {icon} {sym} {ps} {status}  PnL: ${pnl:+.4f}")
                _send_telegram(
                    f"{icon} [FUTURES] {'TP HIT' if status == 'TP_HIT' else 'SL HIT'}: "
                    f"{sym} {ps} {'+' if pnl >= 0 else ''}{pnl:.4f} USD"
                )

        # Persist
        if log_dirty:
            for ot in open_trades:
                eid = ot.get("entry_order_id")
                if not eid:
                    continue
                from services.supabase_client import update_futures_by_order_id
                update_futures_by_order_id(eid, {
                    k: ot.get(k) for k in [
                        "entry_status", "entry_fill_price", "entry_fill_time",
                        "entry_qty", "slippage_pct", "last_funding_check_time",
                        "tp_order_id", "sl_order_id", "tp_algo_id", "sl_algo_id",
                        "exit_orders_placed", "funding_rate_paid", "funding_rate_history",
                        "exit_status", "exit_price", "exit_time",
                        "realized_pnl_usd", "realized_pnl_pct", "time_in_position_sec",
                        "max_adverse_excursion_pct", "max_favorable_excursion_pct",
                        "distance_to_liquidation_pct_min",
                    ]
                })

        print("\n  Run --check-positions again to refresh.")

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _query_exit_order(client, order_id, algo_id, label) -> tuple:
        """
        Query exit order via algo endpoint — NO symbol filter (testnet bug).
        Fallback: scan open-orders list if primary returns UNKNOWN.
        Returns (status, fill_price, update_time) or (None, None, None).
        """
        effective_id = algo_id or order_id
        if not effective_id:
            return None, None, None
        try:
            resp = client.futures_get_algo_order(algoId=effective_id)
            if isinstance(resp, list):
                matches = [o for o in resp
                           if str(o.get("algoId")) == str(effective_id)]
                o = matches[0] if matches else {}
            else:
                o = resp or {}

            status = (o.get("algoStatus") or o.get("orderStatus")
                      or o.get("status") or "UNKNOWN")

            if status.upper() == "UNKNOWN":
                try:
                    all_open = client.futures_get_open_algo_orders()
                    if isinstance(all_open, dict):
                        all_open = all_open.get("orders", [])
                    match = next(
                        (x for x in (all_open or [])
                         if str(x.get("algoId")) == str(effective_id)), None)
                    if match:
                        status = match.get("algoStatus") or match.get("status") or "NEW"
                        o = match
                except Exception:
                    pass

            raw_qty   = float(o.get("executedQty") or o.get("qty") or 0)
            raw_quote = float(o.get("cumQuote") or o.get("cummulativeQuoteQty") or 0)
            fill_price = (raw_quote / raw_qty if raw_qty > 0 and raw_quote > 0
                          else float(o.get("triggerPrice") or o.get("stopPrice")
                                     or o.get("price") or 0))
            return status, fill_price, o.get("updateTime") or o.get("bookTime")
        except Exception as e:
            print(f"  [WARN] _query_exit_order failed ({label} algoId={effective_id}): {e}")
            return None, None, None

    @staticmethod
    def _cancel_algo_orders(client, symbol: str,
                            tp_algo_id=None, sl_algo_id=None) -> list:
        """
        Cancel ALL open exit-related orders for a symbol.
        Path 1: /openAlgoOrders without symbol filter (testnet bug workaround).
        Path 2: /openOrders for reduceOnly regular orders.
        """
        canceled: list[str] = []

        try:
            all_algos = client.futures_get_open_algo_orders()
            if isinstance(all_algos, dict):
                all_algos = all_algos.get("orders", [])
            sym_algos = [o for o in (all_algos or []) if o.get("symbol") == symbol]
            for ao in sym_algos:
                aid = ao.get("algoId") or ao.get("orderId")
                if not aid:
                    continue
                try:
                    client.futures_cancel_algo_order(symbol=symbol, algoId=aid)
                    canceled.append(f"algo:{aid}")
                except Exception as ce:
                    print(f"  ⚠  [{symbol}] Could not cancel algo #{aid}: {ce}")
            if sym_algos:
                print(f"  🧹 [{symbol}] Canceled "
                      f"{len([c for c in canceled if c.startswith('algo:')])} "
                      f"algo order(s)")
        except Exception as qe:
            print(f"  ⚠  [{symbol}] /openAlgoOrders query failed: {qe}")
            for aid in [tp_algo_id, sl_algo_id]:
                if aid:
                    try:
                        client.futures_cancel_algo_order(symbol=symbol, algoId=aid)
                        canceled.append(f"algo:{aid}")
                    except Exception:
                        pass

        try:
            EXIT_TYPES = {"TAKE_PROFIT_MARKET", "STOP_MARKET", "TAKE_PROFIT", "STOP"}
            opens = client.futures_get_open_orders(symbol=symbol)
            if isinstance(opens, dict):
                opens = opens.get("orders", [])
            for ro in [o for o in (opens or [])
                       if o.get("type") in EXIT_TYPES and o.get("reduceOnly")]:
                oid = ro.get("orderId")
                if not oid:
                    continue
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=oid)
                    canceled.append(f"order:{oid}")
                except Exception as ce:
                    print(f"  ⚠  [{symbol}] Could not cancel order #{oid}: {ce}")
        except Exception as qe2:
            print(f"  ⚠  [{symbol}] /openOrders query failed: {qe2}")

        if not canceled:
            print(f"  ℹ  [{symbol}] No open exit orders found to cancel.")
        return canceled

    @staticmethod
    def _print_verbose_card(trade: dict, sym: str, side: str,
                            entry_status: str, current,
                            leverage: int, margin_mode: str) -> None:
        """Print the detailed position card used in --verbose mode."""
        entry_price = trade.get("entry_price")
        entry_fill  = trade.get("entry_fill_price")
        sl  = trade.get("sl")
        tp  = trade.get("tp1")
        liq = trade.get("liquidation_price")
        qty = trade.get("entry_qty") or 0

        def _fp(p):
            return ca._fmt_price(p, width=14).strip() if p is not None else "n/a"

        def _pct(a, b):
            try:   return (a - b) / b * 100
            except: return None

        cur_str   = _fp(current)
        entry_str = _fp(entry_fill or entry_price)
        sl_str    = _fp(sl);  tp_str  = _fp(tp);  liq_str = _fp(liq)
        pct_to_entry = _pct(entry_price, current) if current else None
        pct_sl   = _pct(sl,  current)  if (current and sl)  else None
        pct_tp   = _pct(tp,  current)  if (current and tp)  else None
        pct_liq  = _pct(liq, current)  if (current and liq) else None
        rr       = trade.get("planned_rr")

        W = 78
        print("\n  " + "╔" + "═" * (W - 2) + "╗")
        print(f"  ║ {f'{sym}  {side}':<{W-4}} ║")
        print(f"  ║{'':{W-2}}║")

        if entry_status in ("NEW", "PARTIALLY_FILLED"):
            pl = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
            if pct_to_entry is not None:
                pl += f"   ({pct_to_entry:+.2f}% to fill)"
            print(f"  ║ {pl:<{W-4}} ║")
            print(f"  ║{'':{W-2}}║")
            tl = f"TP1: {tp_str:>12}   |   SL: {sl_str:>12}"
            if rr:
                tl += f"   |   R:R  {rr:.2f}:1"
            print(f"  ║ {tl:<{W-4}} ║")
        elif entry_status == "FILLED":
            ef_ref  = entry_fill or entry_price
            el = f"Entry: {entry_str:>12}   |  Current: {cur_str:>12}"
            if current is not None and ef_ref is not None and abs(current - ef_ref) < 1e-9:
                el += "  ⚠ stale?"
            print(f"  ║ {el:<{W-4}} ║")
            print(f"  ║{'':{W-2}}║")
            tl = f"TP1: {tp_str:>12}"
            if pct_tp is not None:
                tl += f" ({pct_tp:+.2f}%)"
            tl = tl.ljust(36) + f"  |  SL: {sl_str:>12}"
            if pct_sl is not None:
                tl += f" ({pct_sl:+.2f}%)"
            if rr:
                tl += f"   R:R {rr:.2f}:1"
            print(f"  ║ {tl:<{W-4}} ║")
        print(f"  ║{'':{W-2}}║")

        liq_line = f"Liq: {liq_str:>12}"
        if pct_liq is not None:
            liq_line += f" ({pct_liq:+.2f}% from current)"
        liq_line = liq_line.ljust(38) + f"  |  {leverage}x {margin_mode}"
        print(f"  ║ {liq_line:<{W-4}} ║")
        print(f"  ║{'':{W-2}}║")

        sl_line = f"Status: {entry_status}"
        if entry_status == "FILLED" and qty and current:
            ef_ref = entry_fill or entry_price
            unreal = qty * (current - ef_ref) * (1 if side == "LONG" else -1)
            sl_line += f"  |  Unreal: ${unreal:+.3f}"
        funding = trade.get("funding_rate_paid") or 0.0
        if funding != 0.0:
            sl_line += f"  |  Funding: ${funding:+.4f}"
        sl_line += f"  |  Regime: {trade.get('volatility_regime_at_entry', '?')}"
        if trade.get("tp_order_id"):
            sl_line += "  |  Exit orders: ✅ placed"
        elif entry_status == "FILLED" and not trade.get("exit_orders_placed"):
            sl_line += "  |  Exit orders: ⚠ NOT placed"
        print(f"  ║ {sl_line:<{W-4}} ║")
        print("  " + "╚" + "═" * (W - 2) + "╝\n")
