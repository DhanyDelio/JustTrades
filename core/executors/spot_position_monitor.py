
import sys
import atexit
from datetime import datetime, timezone
from collections import Counter, defaultdict

import services.chart_analyzer as ca
from core.paper_trade_executor import (
    _fmt_order_status,
    _send_telegram,
    LAB_STARTING_CAPITAL,
    BUDGET_USD,
    PER_TRADE_BUDGET
)
from core.managers.portfolio_manager import PortfolioManager

class SpotPositionMonitor:
    def __init__(self, client, repo, order_executor):
        self.client = client
        self.repo = repo
        self.order_executor = order_executor

    def check_positions(self, verbose: bool = False, mode: str = "all") -> None:
        client = self.client
        repo = self.repo
        SpotOrderExecutor = lambda c: self.order_executor  # wrapper to mock SpotOrderExecutor(client) calls inside

        """
        For each trade with exit_status == OPEN:
        1. Query entry order status from exchange.
        2. If entry FILLED and no OCO yet → place OCO, update log.
        3. If OCO placed → check OCO legs for TP_HIT / SL_HIT, update log.
        4. Print grouped summary (compact by default, detailed with --verbose).
        """
        trades     = repo.load_trade_log()
        # Filter trades according to mode (single/lab/all) for display and operations
        filtered_trades = [t for t in trades if repo.match_mode(t, mode)]
        open_trades = [t for t in filtered_trades if t.get("exit_status") == "OPEN"]
        log_dirty  = False
    
        if not open_trades:
            print("\n  No open positions in trade_log.json")
            closed = [t for t in filtered_trades if t.get("exit_status") != "OPEN"][-5:]
            if closed:
                print(f"\n  Last {len(closed)} closed trade(s):")
                for t in closed:
                    pnl = f"${t['realized_pnl_usd']:+.2f}" if t.get("realized_pnl_usd") is not None else "n/a"
                    hrs = f"{t['time_to_resolution_sec']//3600}h" if t.get("time_to_resolution_sec") else "n/a"
                    print(f"    {t['symbol']:10} {t['direction'].upper():5} "
                          f"{t['exit_status']:15}  PnL: {pnl:>8}  held: {hrs}")
            return
    
        # Show lab pool status (if any clustered trades exist)
        pm = PortfolioManager(repo, BUDGET_USD, LAB_STARTING_CAPITAL, PER_TRADE_BUDGET)
        pool = pm.compute_lab_pool(trades)
        lab_cap = pool["lab_capital"]
        net_pnl = pool["closed_cluster_pnl"]
        deployed = pool["deployed_capital"]
        available = pool["available_capital"]
        max_new = pool["max_new_positions"]
        print(f"\n  Lab capital: ${lab_cap:.2f} (started ${LAB_STARTING_CAPITAL:.0f}, net P&L ${net_pnl:+.2f})  |  Deployed: ${deployed:.2f}  |  Available: ${available:.2f}  |  Max new positions: {max_new}")
    
        # ── Group by correlation_cluster_id ───────────────────────────────
        from collections import Counter, defaultdict
        clusters: dict[str, list] = defaultdict(list)
        for t in open_trades:
            cid = t.get("correlation_cluster_id") or "single"
            clusters[cid].append(t)
    
        dup_syms = [sym for sym, count in Counter(t["symbol"] for t in open_trades).items() if count > 1]
        if dup_syms:
            print("\n  ⚠ DUPLICATE OPEN SYMBOL(S) DETECTED — review before adding more positions:")
            for sym in dup_syms:
                entries = sorted(
                    [t for t in open_trades if t["symbol"] == sym],
                    key=lambda t: t.get("open_time", "")
                )
                print(f"\n    {sym}:")
                for t in entries:
                    cluster_label = t.get("correlation_cluster_id") or "single --propose"
                    print(f"      Order #{t['entry_order_id']:<10}  cluster={cluster_label}")
                    print(f"        status={t.get('entry_status','?')}  opened={t.get('open_time','?')[:19]}")
                # Identify the older single-propose entry to suggest cancellation
                single_entries = [t for t in entries if not t.get("correlation_cluster_id")]
                if single_entries:
                    stale = single_entries[0]
                    print(f"\n      ⚠  Order #{stale['entry_order_id']} is a stale single --propose entry.")
                    print(f"         If you no longer want it, cancel it at testnet.binance.vision,")
                    print(f"         then update trade_log.json: set that entry's exit_status to 'CANCELED'.")
                    print(f"         Until canceled, BOTH orders may fill — doubling your {sym} exposure.")
    
        n_filled   = sum(1 for t in open_trades if t.get("entry_status") == "FILLED")
        n_oco      = sum(1 for t in open_trades if t.get("oco_placed"))
        n_pending  = len(open_trades) - n_filled
    
        print(f"\n  ── OPEN POSITIONS: {len(open_trades)} total  "
              f"({n_pending} pending fill, {n_filled} filled, {n_oco} OCO active) ──")
    
        resolved_this_run = []
    
        for cid, group in clusters.items():
            cluster_label = f"Cluster {cid}" if cid != "single" else "Single trade"
            print(f"\n  [{cluster_label}  —  {len(group)} position(s)]")
    
            # Keep the compact table header for navigation, but only print the
            # per-symbol summary row in non-verbose mode.
            print(f"  {'Symbol':<10} {'Status':<22} {'PnL/Info':>12}  OCO")
            print(f"  {'─'*55}")
    
            # Batch-fetch current prices for this group's symbols to avoid per-symbol rate hits
            try:
                all_tickers = client.get_all_tickers()
                price_map = {t.get('symbol'): float(t.get('price')) for t in all_tickers}
            except Exception:
                price_map = {}
    
            for trade in group:
                sym  = trade["symbol"]
                dirn = trade["direction"].upper()
                eid  = trade.get("entry_order_id")
    
                # ── Step 1: Query entry order ──────────────────────────────
                # If entry order returns -2013 (purged after testnet reset):
                #   - local entry_status=FILLED  → use persisted state, continue to OCO
                #   - local entry_status=PENDING  → ambiguous, mark RECONCILIATION_REQUIRED
                #   - other errors               → skip this trade this cycle
                _entry_order_missing = False
                try:
                    entry_order  = client.get_order(symbol=sym, orderId=eid)
                    entry_status = entry_order.get("status", "UNKNOWN")
                except Exception as e:
                    err_str      = str(e)
                    _purged      = "-2013" in err_str or "Order does not exist" in err_str
                    local_status = trade.get("entry_status", "")

                    if _purged and local_status == "FILLED":
                        # Entry order history gone but position was already FILLED.
                        # Persisted entry_status / entry_fill_price / entry_qty are
                        # sufficient — proceed directly to OCO reconciliation.
                        entry_status         = "FILLED"
                        _entry_order_missing = True
                        print(
                            f"  {sym:<10} ℹ Entry order {eid} not found (purged). "
                            f"Using persisted FILLED state — continuing to OCO check."
                        )
                    elif _purged and local_status in ("NEW", "PARTIALLY_FILLED", ""):
                        # Pending entry order purged — cannot confirm fill.
                        entry_status = local_status or "UNKNOWN"
                        trade["oco_reconciliation_status"] = "RECONCILIATION_REQUIRED"
                        log_dirty = True
                        print(
                            f"  {sym:<10} ⚠ Entry order {eid} not found, "
                            f"local_status={local_status!r}. RECONCILIATION_REQUIRED."
                        )
                        continue
                    else:
                        # Non-purge error (network, auth, etc.) — skip this cycle.
                        print(f"  {sym:<10} ⚠ Could not query entry order: {e}")
                        continue
    
                # If entry order was purged, skip fill-price derivation and
                # use values already persisted in the trade dict.
                if _entry_order_missing:
                    filled_qty  = float(trade.get("entry_qty") or 0)
                    fill_price  = float(trade.get("entry_fill_price") or trade.get("entry_price") or 0)
                    # entry_status is already set above; nothing more to derive here.
                    # Jump past all entry-order-derived logic below.
                    # The goto-equivalent: set entry_order to an empty dict so
                    # downstream reads like entry_order.get(...) return None safely.
                    entry_order = {}
                else:
                    filled_qty  = float(entry_order.get("executedQty", 0))
                    actual_fill = float(entry_order.get("cummulativeQuoteQty", 0))
                # fill_price derivation — only when entry order was actually fetched
                if not _entry_order_missing:
                    if filled_qty > 0 and actual_fill > 0:
                        fill_price = actual_fill / filled_qty
                    else:
                        # Fallback 1: /api/v3/myTrades — most reliable actual fill price
                        _fill_resolved = False
                        try:
                            my_trades = client.get_my_trades(symbol=sym, orderId=eid, limit=5)
                            if my_trades:
                                total_qty   = sum(float(t["qty"])   for t in my_trades)
                                total_quote = sum(float(t["quoteQty"]) for t in my_trades)
                                if total_qty > 0 and total_quote > 0:
                                    fill_price = total_quote / total_qty
                                    _fill_resolved = True
                        except Exception:
                            pass
                        if not _fill_resolved:
                            # Fallback 2: limit price from the order
                            fill_price = float(entry_order.get("price", trade["entry_price"]))
    
                if trade.get("entry_status") != entry_status:
                    trade["entry_status"] = entry_status
                    log_dirty = True
                # Only update fill details from exchange if entry order was fetched.
                # For purged orders (_entry_order_missing), fill details are already
                # persisted in the trade dict — do not overwrite with None/defaults.
                if not _entry_order_missing:
                    if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
                        trade["entry_fill_price"] = fill_price
                        trade["entry_fill_time"]  = entry_order.get("updateTime")
                        trade["entry_qty"]        = filled_qty
                        planned = trade.get("entry_price", fill_price)
                        trade["slippage_pct"] = round(
                            (fill_price - planned) / planned * 100, 4
                        ) if planned else None
                        log_dirty = True
                        # Notify on NEW → FILLED transition
                        _send_telegram(
                            f"✅ Filled: {sym} {trade.get('direction','').lower()} @ {ca._fmt_price(fill_price).strip()}"
                            f" | SL: {ca._fmt_price(trade.get('sl')).strip()}"
                            f" | TP: {ca._fmt_price(trade.get('tp1')).strip()}"
                        )
    
                # ── Step 2: Place OCO if filled and no OCO yet ─────────────
                if entry_status == "FILLED" and not trade.get("oco_placed"):
                    print(f"  {sym:<10} ✅ FILLED — placing OCO...")
                    oco_resp, last_err = None, None
                    for attempt in range(1, 3):
                        try:
                            oco_resp = self.order_executor.place_oco_order(trade)
                            break
                        except RuntimeError as e:
                            last_err = e
                            if attempt < 2:
                                import time; time.sleep(3)
    
                    # ── Market-sell path: place_oco_order already sold the position ──
                    # trade["_market_sold"] is set by the emergency market-sell branch.
                    # Update the log as SL_HIT and skip ALL further OCO logic for this trade.
                    if trade.get("_market_sold"):
                        entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                        exit_px    = float(oco_resp.get("fills", [{}])[0].get("price", 0) or 0) \
                                     if oco_resp and oco_resp.get("fills") else None
                        # Fallback: use cummulativeQuoteQty / executedQty
                        if not exit_px and oco_resp:
                            exec_qty  = float(oco_resp.get("executedQty", 0) or 0)
                            cum_quote = float(oco_resp.get("cummulativeQuoteQty", 0) or 0)
                            exit_px   = cum_quote / exec_qty if exec_qty > 0 else None
                        if not exit_px:
                            exit_px = trade["sl"]   # conservative fallback
                        pnl_usd = (exit_px - entry_fill) * trade["entry_qty"]
                        pnl_pct = pnl_usd / trade.get("entry_notional", 1) * 100
                        # exit_time: use transactTime from market sell response (accurate)
                        # fallback to updateTime, then entry_fill_time + small offset
                        exit_ts = (
                            oco_resp.get("transactTime")
                            or oco_resp.get("updateTime")
                            if oco_resp else None
                        )
                        if not exit_ts:
                            exit_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                        trade["exit_status"]      = "SL_HIT"
                        trade["exit_price"]       = round(exit_px, 6)
                        trade["exit_time"]        = int(exit_ts)
                        trade["realized_pnl_usd"] = round(pnl_usd, 4)
                        trade["realized_pnl_pct"] = round(pnl_pct, 2)
                        # time_to_resolution_sec from fill to market sell
                        fill_t = trade.get("entry_fill_time")
                        if fill_t:
                            trade["time_to_resolution_sec"] = (int(exit_ts) - int(fill_t)) // 1000
                        trade["oco_placed"]       = False
                        trade["oco_list_id"]      = None
                        trade.pop("_market_sold", None)
                        log_dirty = True
                        resolved_this_run.append((sym, "SL_HIT", pnl_usd))
                        print(f"  {sym:<10} 🔴 Emergency market sell — SL_HIT logged  PnL: ${pnl_usd:+.4f}")
                        continue   # ← skip OCO placement and Step 3 entirely
    
                    if oco_resp:
                        oco_orders = oco_resp.get("orderReports", [])
                        trade["oco_placed"]    = True
                        trade["oco_order_ids"] = [o["orderId"] for o in oco_orders]
                        trade["oco_list_id"]   = oco_resp.get("orderListId")
                        log_dirty = True
                        
                        from services.supabase_client import update_spot_by_order_id
                        eid = trade.get("entry_order_id")
                        if eid:
                            update_spot_by_order_id(eid, {
                                "entry_status":          trade.get("entry_status"),
                                "entry_fill_price":      trade.get("entry_fill_price"),
                                "entry_fill_time":       trade.get("entry_fill_time"),
                                "entry_qty":             trade.get("entry_qty"),
                                "slippage_pct":          trade.get("slippage_pct"),
                                "oco_placed":            True,
                                "oco_order_ids":         trade.get("oco_order_ids"),
                                "oco_list_id":           trade.get("oco_list_id"),
                            })
                            
                        print(f"  {sym:<10} ✅ OCO placed  List#{trade['oco_list_id']}")
                    else:
                        print(
                            f"\n  {'!'*60}\n"
                            f"  !! CRITICAL: OCO FAILED for {sym} — POSITION UNPROTECTED !!\n"
                            f"  !! Error: {str(last_err)[:48]:<50}!!\n"
                            f"  !! SL: {ca._fmt_price(trade['sl']).strip():<30} "
                            f"TP: {ca._fmt_price(trade['tp1']).strip():<20}!!\n"
                            f"  !! Fix manually at testnet.binance.vision              !!\n"
                            f"  {'!'*60}\n"
                        )
                        import atexit
                        atexit.register(lambda: sys.exit(2))
    
                # ── Step 3: Check OCO status ────────────────────────────────
                oco_str = "n/a"
                if trade.get("oco_placed") and trade.get("oco_list_id"):
                    try:
                        oco_status  = client.v3_get_order_list(orderListId=trade["oco_list_id"])
                        list_status = oco_status.get("listOrderStatus", "UNKNOWN")
                        oco_str     = list_status
    
                        if list_status == "ALL_DONE":
                            for leg_ref in oco_status.get("orders", []):
                                leg = client.get_order(symbol=sym, orderId=leg_ref["orderId"])
                                if leg.get("status") == "FILLED":
                                    exec_qty   = float(leg.get("executedQty", 0) or 1)
                                    cum_quote  = float(leg.get("cummulativeQuoteQty", 0))
                                    exit_price = cum_quote / exec_qty if exec_qty > 0 \
                                                 else float(leg.get("price", 0))
                                    exit_status = "SL_HIT" if "STOP" in leg.get("type","") else "TP_HIT"
                                    entry_fill  = trade.get("entry_fill_price") or trade["entry_price"]
                                    pnl_usd     = (exit_price - entry_fill) * trade["entry_qty"]
                                    pnl_pct     = pnl_usd / trade["entry_notional"] * 100
                                    trade["exit_status"]     = exit_status
                                    trade["exit_price"]      = round(exit_price, 6)
                                    trade["exit_time"]       = leg.get("updateTime")
                                    trade["realized_pnl_usd"] = round(pnl_usd, 4)
                                    trade["realized_pnl_pct"] = round(pnl_pct, 2)
                                    fill_t = trade.get("entry_fill_time")
                                    exit_t = leg.get("updateTime")
                                    if fill_t and exit_t:
                                        trade["time_to_resolution_sec"] = (int(exit_t)-int(fill_t))//1000
                                    elif exit_t and trade.get("open_time"):
                                        # Fallback: use open_time (order placed time) if fill_time missing
                                        try:
                                            open_ms = int(datetime.fromisoformat(
                                                trade["open_time"]
                                            ).timestamp() * 1000)
                                            trade["time_to_resolution_sec"] = (int(exit_t) - open_ms) // 1000
                                        except Exception:
                                            pass
                                    log_dirty = True
                                    resolved_this_run.append((sym, exit_status, pnl_usd))
                                    oco_str = f"{'🟢' if exit_status=='TP_HIT' else '🔴'} {exit_status}"
                                    break
    
                            if trade.get("exit_status") != "OPEN":
                                continue
                    except Exception as e:
                        err_str = str(e)
                        # ── Step 3a: OCO Protection Reconciliation ─────────
                        # -2018: order list does not exist (purged / testnet reset)
                        # -2013: child order does not exist
                        # Either means oco_placed=True in DB but exchange has no record.
                        # SAFE RULE: order-not-found = MISSING PROTECTION.
                        # Do NOT infer SL executed — no fill, no sold balance confirmed.
                        _is_missing = "-2018" in err_str or "-2013" in err_str
                        if _is_missing:
                            prev_recon  = trade.get("oco_reconciliation_status", "")
                            new_recon   = "UNPROTECTED"
                            trade["oco_reconciliation_status"] = new_recon
                            log_dirty = True
                            oco_str = "⚠ UNPROTECTED"
                            print(
                                f"  ⚠  [{sym}] OCO missing on exchange (order-not-found). "
                                f"Position UNPROTECTED. "
                                f"oco_list_id={trade.get('oco_list_id')} error: {err_str[:60]}"
                            )
                            # Alert only when state actually transitions into this status.
                            # prev_recon != new_recon catches the first detection correctly.
                            # If prev was UNPROTECTED_SL_BREACH and we re-detect UNPROTECTED
                            # (price moved back above SL), that is also a new transition.
                            if prev_recon != new_recon:
                                cur_disp = ca._fmt_price(price_map.get(sym)).strip() if price_map.get(sym) else "?"
                                _send_telegram(
                                    f"🚨 [SPOT] UNPROTECTED POSITION: {sym}\n"
                                    f"OCO {trade.get('oco_list_id')} not found on exchange.\n"
                                    f"Cause: testnet reset / OCO purged.\n"
                                    f"SL: {ca._fmt_price(trade.get('sl')).strip()}  Current: {cur_disp}\n"
                                    f"Asset still in wallet — NOT auto-sold.\n"
                                    f"Manual intervention required."
                                )
                        else:
                            # Transient API error — do not change protection status
                            trade["oco_reconciliation_status"] = "RECONCILIATION_REQUIRED"
                            log_dirty = True
                            oco_str = f"⚠ {err_str[:30]}"
    
                if not repo.should_show_live_position(trade, entry_status):
                    continue
    
                # ── Step 4: Compact line + optional verbose card ────────────
                status_str = _fmt_order_status(entry_status)[:20]
                try:
                    current = price_map.get(sym)
                    if current is None:
                        current = float(client.get_symbol_ticker(symbol=sym)["price"])
                except Exception:
                    current = None
    
                # ── Step 3.5: SL breach handling — two distinct cases ──────────
                # Case A: OCO confirmed MISSING (UNPROTECTED) + price below SL
                #   → UNPROTECTED_SL_BREACH: asset still held, NO execution occurred.
                #   → Do NOT set exit_status=SL_HIT. Log state, alert, require manual action.
                # Case B: OCO was placed + price below SL (testnet OCO didn't trigger)
                #   → Price-guard: safe to infer SL execution (OCO existence was confirmed
                #     in Step 3 because no -2018/-2013 error was returned).
                recon_status = trade.get("oco_reconciliation_status", "")
                if (entry_status == "FILLED"
                        and trade.get("exit_status") == "OPEN"
                        and current is not None):
                    sl_level   = trade.get("sl")
                    sl_breached = sl_level and current <= float(sl_level)

                    if sl_breached and recon_status in ("UNPROTECTED", "UNPROTECTED_SL_BREACH"):
                        entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                        est_pnl    = trade.get("entry_qty", 0) * (current - entry_fill)
                        prev_recon = recon_status
                        new_recon  = "UNPROTECTED_SL_BREACH"
                        print(
                            f"  🚨 [{sym}] UNPROTECTED_SL_BREACH: "
                            f"price {current:.6f} below SL {sl_level:.6f}, "
                            f"OCO missing, asset NOT sold. "
                            f"Est. unrealized loss: ${est_pnl:+.4f} USDT. "
                            f"Manual intervention required."
                        )
                        if prev_recon != new_recon:
                            trade["oco_reconciliation_status"] = new_recon
                            log_dirty = True
                            # Alert only on transition into this state (not every cycle).
                            _send_telegram(
                                f"🚨 [SPOT] UNPROTECTED SL BREACH: {sym}\n"
                                f"Price {ca._fmt_price(current).strip()} is below SL "
                                f"{ca._fmt_price(trade.get('sl')).strip()}.\n"
                                f"OCO is MISSING — asset NOT sold, position still open.\n"
                                f"Est. unrealized loss: ${est_pnl:+.4f} USDT\n"
                                f"Manual intervention required."
                            )
                        oco_str = "🚨 UNPROTECTED_SL_BREACH"

                    elif sl_breached and trade.get("oco_placed"):
                        # OCO exists on exchange (confirmed in Step 3) but didn't fire.
                        # Price-guard: safe to resolve as SL_HIT.
                        print(
                            f"  ⚠  [{sym}] Price {current:.4f} breached SL {sl_level:.4f} "
                            f"— OCO confirmed placed but not triggered. Resolving as SL_HIT."
                        )
                        entry_fill   = trade.get("entry_fill_price") or trade["entry_price"]
                        qty          = trade.get("entry_qty", 0)
                        pnl_usd      = qty * (current - entry_fill)
                        pnl_pct      = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                        exit_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                        trade["exit_status"]      = "SL_HIT"
                        trade["exit_price"]       = round(current, 6)
                        trade["exit_time"]        = exit_time_ms
                        trade["realized_pnl_usd"] = round(pnl_usd, 4)
                        trade["realized_pnl_pct"] = round(pnl_pct, 2)
                        if trade.get("entry_fill_time") and exit_time_ms:
                            trade["time_to_resolution_sec"] = (
                                exit_time_ms - int(trade["entry_fill_time"])
                            ) // 1000
                        log_dirty = True
                        resolved_this_run.append((sym, "SL_HIT", pnl_usd))
                        _send_telegram(
                            f"🛑 [SPOT] SL_HIT (price-guard, OCO confirmed): "
                            f"{sym} @ {ca._fmt_price(current).strip()}"
                            f"  |  PnL: ${pnl_usd:+.2f}"
                        )
                        continue
    
                # Compact view: show PnL only for filled positions;
                # for pending orders show distance to entry instead (more useful than "n/a")
                pnl_display = "n/a"
                if entry_status == "FILLED" and trade.get("entry_qty", 0) > 0 and current is not None:
                    ref_price = trade.get("entry_fill_price") or trade["entry_price"]
                    qty = trade.get("entry_qty", 0)
                    pnl_usd = qty * (current - ref_price)
                    pnl_display = f"${pnl_usd:+.3f}"
                elif entry_status in ("NEW", "PARTIALLY_FILLED") and current is not None:
                    entry_limit = trade.get("entry_price")
                    if entry_limit and current:
                        dist_pct = (entry_limit - current) / current * 100
                        # Positive dist_pct = entry is above current (limit BUY waiting for pullback)
                        pnl_display = f"{dist_pct:+.2f}% fill"
    
                if not verbose:
                    print(f"  {sym:<10} {status_str:<22} {pnl_display:>12}  {oco_str}")
    
                if verbose:
                    # Verbose: spacious, aligned info card
                    sym_hdr = f"{sym}  {dirn}"
                    entry_price = trade.get("entry_price")
                    entry_fill = trade.get("entry_fill_price")
                    sl = trade.get("sl")
                    tp = trade.get("tp1")
                    qty = trade.get("entry_qty") or 0
    
                    cur_str = ca._fmt_price(current, width=14).strip() if current is not None else "n/a"
                    entry_str = ca._fmt_price(entry_fill or entry_price, width=14).strip() if (entry_fill or entry_price) is not None else "n/a"
                    sl_str = ca._fmt_price(sl, width=14).strip() if sl is not None else "n/a"
                    tp_str = ca._fmt_price(tp, width=14).strip() if tp is not None else "n/a"
    
                    def pct(a, b):
                        try:
                            return (a - b) / b * 100
                        except Exception:
                            return None
    
                    if current is not None:
                        pct_to_entry = pct(entry_price, current)
                        pct_sl = pct(sl, current) if sl else None
                        pct_tp = pct(tp, current) if tp else None
                    else:
                        pct_to_entry = pct_sl = pct_tp = None
    
                    W = 78
                    print("\n  " + "╔" + "═" * (W - 2) + "╗")
                    print(f"  ║ {sym_hdr:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                    # Conditional content based on order status
                    if entry_status in ("NEW", "PARTIALLY_FILLED"):
                        # Show only the 'to fill' line
                        price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                        if pct_to_entry is not None:
                            price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                        print(f"  ║ {price_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                    elif entry_status == "FILLED":
                        # Show Entry + Current, then SL / TP distances
                        entrycur_line = f"Entry: {entry_str:>12}   |  Current: {cur_str:>12}"
                        # Fix 3: warn if current price equals entry_fill_price after FILLED —
                        # this usually means the testnet price is stale or fill_price fallback fired.
                        _entry_ref = entry_fill or entry_price
                        _price_unchanged = (
                            current is not None
                            and _entry_ref is not None
                            and abs(current - _entry_ref) < 1e-9
                        )
                        if _price_unchanged:
                            entrycur_line += "  ⚠ stale?"
                        print(f"  ║ {entrycur_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                        sltp_line = f"SL: {sl_str:>12}"
                        if pct_sl is not None:
                            sltp_line += f" ({pct_sl:+.2f}%)"
                        sltp_line = sltp_line.ljust(38)
                        sltp_line += f"  |  TP: {tp_str:>12}"
                        if pct_tp is not None:
                            sltp_line += f" ({pct_tp:+.2f}%)"
                        print(f"  ║ {sltp_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                    else:
                        # Fallback: show both lines
                        price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                        if pct_to_entry is not None:
                            price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                        print(f"  ║ {price_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                        sltp_line = f"SL: {sl_str:>12}"
                        if pct_sl is not None:
                            sltp_line += f" ({pct_sl:+.2f}%)"
                        sltp_line = sltp_line.ljust(38)
                        sltp_line += f"  |  TP: {tp_str:>12}"
                        if pct_tp is not None:
                            sltp_line += f" ({pct_tp:+.2f}%)"
                        print(f"  ║ {sltp_line:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                    status_line = f"Status: {_fmt_order_status(entry_status)}"
                    if entry_status == 'FILLED' and qty:
                        r_pnl = trade.get('realized_pnl_usd')
                        if r_pnl is not None:
                            status_line += f"  |  Realized: ${r_pnl:+.4f}"
                        if current is not None and qty:
                            ref = trade.get('entry_fill_price') or trade.get('entry_price')
                            unreal = qty * (current - ref)
                            status_line += f"  |  Unreal: ${unreal:+.3f}"
                    if trade.get('oco_list_id'):
                        status_line += f"  |  OCO List: {trade['oco_list_id']}"
                    print(f"  ║ {status_line:<{W-4}} ║")
                    print("  " + "╚" + "═" * (W - 2) + "╝\n")
    
        # ── Summary of what changed this run ───────────────────────────────
        if resolved_this_run:
            print(f"\n  ── Resolved this run: {len(resolved_this_run)} trade(s) ──")
            for sym, status, pnl in resolved_this_run:
                icon = "🟢" if status == "TP_HIT" else "🔴"
                print(f"    {icon} {sym}  {status}  PnL: ${pnl:+.4f}")
    
            # ── Telegram notif ──────────────────────────────────────────────
            for sym, status, pnl in resolved_this_run:
                icon = "🟢" if status == "TP_HIT" else "🔴"
                emoji_label = "TP HIT" if status == "TP_HIT" else "SL HIT"
                _send_telegram(
                    f"{icon} {emoji_label}: {sym} "
                    f"{'+'if pnl>=0 else ''}{pnl:.4f} USD\n"
                    f"(detected via --check-positions)"
                )
    
        # ── Save updates ───────────────────────────────────────────────────
        if log_dirty:
            from services.supabase_client import update_spot_by_order_id
            for ot in open_trades:
                eid = ot.get("entry_order_id")
                if not eid:
                    continue
                update_spot_by_order_id(eid, {
                    "entry_status":               ot.get("entry_status"),
                    "entry_fill_price":           ot.get("entry_fill_price"),
                    "entry_fill_time":            ot.get("entry_fill_time"),
                    "entry_qty":                  ot.get("entry_qty"),
                    "slippage_pct":               ot.get("slippage_pct"),
                    "oco_placed":                 ot.get("oco_placed"),
                    "oco_order_ids":              ot.get("oco_order_ids"),
                    "oco_list_id":                ot.get("oco_list_id"),
                    "tp1":                        ot.get("tp1"),
                    "exit_status":                ot.get("exit_status"),
                    "exit_price":                 ot.get("exit_price"),
                    "exit_time":                  ot.get("exit_time"),
                    "realized_pnl_usd":           ot.get("realized_pnl_usd"),
                    "realized_pnl_pct":           ot.get("realized_pnl_pct"),
                    "time_to_resolution_sec":     ot.get("time_to_resolution_sec"),
                })
                # Persist oco_reconciliation_status separately — requires
                # the column to exist in trades_spot (see docs/migrations/).
                # Safe to skip if column not yet present; in-memory state is
                # still correct for this cycle.
                recon = ot.get("oco_reconciliation_status")
                if recon:
                    try:
                        update_spot_by_order_id(eid, {"oco_reconciliation_status": recon})
                    except Exception:
                        pass  # column not yet migrated — silently skip
    
        if not verbose and len(open_trades) > 1:
            print(f"\n  ℹ️  Use --verbose for detailed per-position cards.")
        print("\n  Run --check-positions again to refresh.")
        print("  To manually close: testnet.binance.vision → spot trading → cancel OCO")
    
    
    # ---------------------------------------------------------------------------
    # 9. MAIN — --propose and --check-positions
