from abc import ABC, abstractmethod
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import time

class OrderExecutor(ABC):
    """
    KONTRAK UTAMA (Abstract Base Class) untuk semua tipe eksekusi order (Spot maupun Futures).
    Setiap turunan (child class) WAJIB mengimplementasikan method yang ditandai @abstractmethod.
    Ini memastikan API bot konsisten (selalu punya place_entry_order, check_positions, dll).
    """

    def __init__(self, client):
        self.client = client
        self._constraints_cache: dict[str, dict] = {}
        self.CACHE_TTL_SECONDS = 24 * 3600  # 24 jam

    # =========================================================================
    # ABSTRACT METHODS (Harus di-implementasi ulang oleh child class)
    # Alasan: Perbedaan fundamental pada API Binance Spot vs Futures.
    # =========================================================================

    @abstractmethod
    def place_entry_order(self, candidate: dict) -> dict:
        """
        Mengeksekusi Limit/Market order untuk masuk posisi (Entry).
        - Spot: Menggunakan endpoint standar.
        - Futures: Memerlukan parameter tambahan seperti positionSide (LONG/SHORT) 
                   dan wajib memanggil fungsi set leverage terlebih dahulu.
        """
        pass

    @abstractmethod
    def place_exit_orders(self, trade: dict) -> dict:
        """
        Mengeksekusi order penutup posisi pelindung (Take Profit & Stop Loss).
        - Spot: Menggunakan endpoint OCO (One-Cancels-the-Other) bawaan Binance.
        - Futures: Mengirim 2 order kondisional terpisah (STOP_MARKET & TAKE_PROFIT_MARKET)
                   karena Futures tidak memiliki OCO native.
        """
        pass

    @abstractmethod
    def check_positions(self, verbose: bool = False, mode: str = "all") -> None:
        """
        Memeriksa status order yang sedang berjalan dan mengupdate log database.
        - Spot: Mengecek status orderId dan OCO orderListId.
        - Futures: Mengecek status algoId, memantau markPrice, dan memeriksa 
                   risiko likuidasi (Liquidation Price).
        """
        pass

    @abstractmethod
    def log_trade(self, order: dict, cand: dict, correlation_cluster_id: str | None = None) -> None:
        """
        Mencatat trade baru ke database (Supabase).
        - Spot: Memanggil upsert_spot (schema tabel spot).
        - Futures: Memanggil upsert_futures (schema tabel futures punya kolom tambahan seperti leverage).
        """
        pass

    # =========================================================================
    # CONCRETE METHODS (100% Identik untuk Spot dan Futures)
    # Alasan: Logika matematika/pembulatan tidak bergantung pada tipe market.
    # =========================================================================

    def get_symbol_constraints(self, symbol: str) -> dict:
        """
        Mengambil filter harga dan kuantitas dari Binance dengan sistem Caching TTL 24 jam.
        Mencegah pemanggilan API berulang-ulang untuk koin yang sama jika berjalan berhari-hari.
        """
        now = time.time()
        cached = self._constraints_cache.get(symbol)

        # Jika belum ada di cache ATAU umurnya sudah lebih dari TTL (24 jam)
        if not cached or (now - cached["timestamp"]) > self.CACHE_TTL_SECONDS:
            
            # Fetch data baru dari API (akan di-override logic fetch-nya jika endpoint beda)
            info = self.client.get_symbol_info(symbol)
            if not info:
                return {}

            price_filter = next((f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            lot_filter = next((f for f in info['filters'] if f['filterType'] == 'LOT_SIZE'), None)

            constraints = {
                "tick_size": float(price_filter['tickSize']) if price_filter else 0.0,
                "step_size": float(lot_filter['stepSize']) if lot_filter else 0.0
            }

            self._constraints_cache[symbol] = {
                "data": constraints,
                "timestamp": now
            }

        return self._constraints_cache[symbol]["data"]

    def round_step(self, value: float, step: float) -> float:
        """
        Membulatkan nilai (quantity) ke step_size terdekat sesuai rules LOT_SIZE Binance.
        """
        val = Decimal(str(value))
        st = Decimal(str(step))
        rounded = (val / st).quantize(Decimal('1'), rounding=ROUND_DOWN) * st
        return float(rounded)

    def round_tick(self, value: float, tick: float) -> float:
        """
        Membulatkan harga (price) ke tick_size terdekat sesuai rules PRICE_FILTER Binance.
        """
        val = Decimal(str(value))
        tk = Decimal(str(tick))
        rounded = (val / tk).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * tk
        return float(rounded)


# =============================================================================
# STUB CLASSES (Skeleton / Draf Implementasi)
# =============================================================================

class SpotOrderExecutor(OrderExecutor):
    """
    Implementasi Order Executor khusus untuk market Binance Spot.
    Mengadaptasi fungsi-fungsi asli dari paper_trade_executor.py.
    """

    def place_entry_order(self, candidate: dict) -> dict:
        """Place entry limit BUY on Binance Spot."""
        from binance.enums import SIDE_BUY, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC
        from binance.exceptions import BinanceAPIException

        sym   = candidate["symbol"]
        qty   = candidate["sizing"]["qty"]
        entry = candidate["entry_price"]

        # Mengambil dari TTL Cache di Base Class
        constraints = self.get_symbol_constraints(sym)
        step = constraints.get("step_size", 0.0)
        tick = constraints.get("tick_size", 0.0)

        qty_str   = f"{self.round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        price_str = f"{self.round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

        try:
            return self.client.create_order(
                symbol      = sym,
                side        = SIDE_BUY,
                type        = ORDER_TYPE_LIMIT,
                timeInForce = TIME_IN_FORCE_GTC,
                quantity    = qty_str,
                price       = price_str,
            )
        except BinanceAPIException as e:
            raise RuntimeError(f"Binance API error: {e}") from e

    def place_exit_orders(self, trade: dict) -> dict:
        """
        Place OCO SELL after LONG entry is filled.

        Binance OCO structure (python-binance >=1.0.37):
            above leg (price > current) = LIMIT_MAKER  → TP
            below leg (price < current) = STOP_LOSS_LIMIT → SL

        Race condition handling:
            - Price already >= TP1 → adjust TP1 to current * 1.003
            - Price already <= SL  → emergency MARKET SELL immediately
            - Price-constraint errors (-1013, -1021) → retry up to 3x

        Raises RuntimeError if all retries exhausted or fatal API error.
        """
        from binance.exceptions import BinanceAPIException
        import time as _time

        MAX_OCO_RETRIES = 3
        TP_ADJUST_BUFFER = 0.003  # 0.3% buffer above current price

        sym = trade["symbol"]
        qty = trade["entry_qty"]
        sl  = trade["sl"]

        # Fetch symbol precision from TTL cache (base class)
        constraints = self.get_symbol_constraints(sym)
        tick = constraints.get("tick_size", 0.01)
        step = constraints.get("step_size", 0.001)

        qty_str = f"{self.round_step(qty, step):.8f}".rstrip("0").rstrip(".")

        last_err = None
        for attempt in range(1, MAX_OCO_RETRIES + 1):
            # Always re-fetch current price on each attempt
            try:
                current = float(self.client.get_symbol_ticker(symbol=sym)["price"])
            except Exception as e:
                raise RuntimeError(f"Could not fetch current price for {sym}: {e}") from e

            tp1 = trade["tp1"]  # start with planned TP

            # ── Race condition: price already below SL ─────────────────
            if current <= sl:
                print(f"\n  ⚠  [{sym}] Price {current:.4f} ≤ SL {sl:.4f} at OCO placement.")
                print(f"       Placing MARKET SELL immediately to cut loss.")
                try:
                    resp = self.client.create_order(
                        symbol   = sym,
                        side     = "SELL",
                        type     = "MARKET",
                        quantity = qty_str,
                    )
                    print(f"  ✅ Market SELL placed: {resp.get('orderId')}")
                    trade["_market_sold"] = True
                    return resp
                except BinanceAPIException as e:
                    raise RuntimeError(f"Market sell failed for {sym}: {e}") from e

            # ── Race condition: price already above TP1 ────────────────
            if current >= tp1:
                adjusted_tp = self.round_tick(current * (1 + TP_ADJUST_BUFFER), tick)
                print(f"\n  ⚠  [{sym}] Price {current:.4f} ≥ TP1 {tp1:.4f} — price exceeded target.")
                print(f"       Adjusting TP1 → {adjusted_tp:.4f} (current + {TP_ADJUST_BUFFER*100:.1f}% buffer)")
                print(f"       Position already in profit beyond original target — OCO will protect gains.")
                tp1 = adjusted_tp
                trade["tp1"] = adjusted_tp

            # ── Final constraint check ─────────────────────────────────
            if not (tp1 > current > sl):
                last_err = RuntimeError(
                    f"OCO constraint still invalid after adjustment attempt {attempt}: "
                    f"tp1={tp1:.4f} current={current:.4f} sl={sl:.4f}"
                )
                _time.sleep(2)
                continue

            # ── Build OCO legs ─────────────────────────────────────────
            sl_stop  = self.round_tick(sl, tick)
            sl_limit = self.round_tick(sl * 0.9985, tick)
            if sl_limit >= sl_stop:
                sl_limit = self.round_tick(sl_stop - tick, tick)

            tp_price     = self.round_tick(tp1, tick)
            tp_str       = f"{tp_price:.8f}".rstrip("0").rstrip(".")
            sl_stop_str  = f"{sl_stop:.8f}".rstrip("0").rstrip(".")
            sl_limit_str = f"{sl_limit:.8f}".rstrip("0").rstrip(".")

            try:
                resp = self.client.create_oco_order(
                    symbol           = sym,
                    side             = "SELL",
                    quantity         = qty_str,
                    aboveType        = "LIMIT_MAKER",
                    abovePrice       = tp_str,
                    belowType        = "STOP_LOSS_LIMIT",
                    belowStopPrice   = sl_stop_str,
                    belowPrice       = sl_limit_str,
                    belowTimeInForce = "GTC",
                )
                if attempt > 1:
                    print(f"  ✅ OCO placed on attempt {attempt} with adjusted prices.")
                return resp
            except BinanceAPIException as e:
                err_str = str(e)
                # Only retry on price-constraint errors; fail fast on other errors
                if "price" in err_str.lower() or "-1013" in err_str or "-1021" in err_str:
                    last_err = RuntimeError(f"OCO placement failed (attempt {attempt}): {e}")
                    _time.sleep(2)
                    continue
                raise RuntimeError(f"OCO placement failed: {e}") from e

        raise last_err or RuntimeError(f"OCO placement failed after {MAX_OCO_RETRIES} attempts")

    def check_positions(self, verbose: bool = False, mode: str = "all") -> None:
        """
        Check all OPEN spot trades and drive their state machine:

        Step 1 — Query entry order status from exchange.
        Step 2 — If entry FILLED and no OCO yet → call self.place_exit_orders()
                 (reuses the OCO logic already implemented in this class).
        Step 3 — If OCO placed → poll OCO status; on ALL_DONE find which leg
                 filled → resolve as TP_HIT or SL_HIT, compute realized PnL.
        Step 3.5 — Price-guard: if price has already breached SL and OCO
                   never triggered → resolve as SL_HIT directly.
        Step 4 — Persist dirty state back to Supabase.

        State transitions persisted:
          entry_status            NEW → FILLED
          entry_fill_price        None → float
          entry_qty               None → float
          slippage_pct            None → float
          oco_placed              False → True
          oco_list_id             None → int
          exit_status             OPEN → TP_HIT | SL_HIT
          exit_price              None → float
          exit_time               None → int (ms)
          realized_pnl_usd        None → float
          realized_pnl_pct        None → float
          time_to_resolution_sec  None → int

        TODO: Replace direct supabase_client calls with self.log_trade()
              once log_trade() is fully implemented.
        """
        import sys
        import os
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

        # TODO: Replace with self.log_trade() once implemented
        from supabase_client import fetch_all_spot, update_spot_by_order_id

        trades     = fetch_all_spot()
        open_trades = [t for t in trades if t.get("exit_status") == "OPEN"]

        if not open_trades:
            print("\n  [SpotOrderExecutor] No open spot positions.")
            return

        print(f"\n  [SpotOrderExecutor] Checking {len(open_trades)} open position(s)...")
        dirty: dict[int, dict] = {}   # entry_order_id → fields to persist

        # Batch-fetch current prices once to avoid per-symbol rate hits
        try:
            all_tickers = self.client.get_all_tickers()
            price_map   = {t["symbol"]: float(t["price"]) for t in all_tickers}
        except Exception:
            price_map = {}

        for trade in open_trades:
            sym = trade["symbol"]
            eid = trade.get("entry_order_id")
            if not eid:
                continue

            fields: dict = {}   # accumulate changes for this trade

            # ── Step 1: Query entry order status ─────────────────────────
            try:
                entry_order  = self.client.get_order(symbol=sym, orderId=eid)
                entry_status = entry_order.get("status", "UNKNOWN")
            except Exception as e:
                print(f"  [{sym}] ⚠ Could not query entry order: {e}")
                continue

            # Compute fill price from the most accurate source available
            filled_qty  = float(entry_order.get("executedQty", 0) or 0)
            cum_quote   = float(entry_order.get("cummulativeQuoteQty", 0) or 0)

            if filled_qty > 0 and cum_quote > 0:
                fill_price = cum_quote / filled_qty
            else:
                # Fallback 1: /api/v3/myTrades (most reliable)
                fill_price = None
                try:
                    my_trades = self.client.get_my_trades(symbol=sym, orderId=eid, limit=5)
                    if my_trades:
                        tq  = sum(float(t["qty"])      for t in my_trades)
                        tqq = sum(float(t["quoteQty"]) for t in my_trades)
                        if tq > 0 and tqq > 0:
                            fill_price = tqq / tq
                except Exception:
                    pass
                # Fallback 2: limit price from order
                if fill_price is None:
                    fill_price = float(entry_order.get("price", trade.get("entry_price", 0)))

            # Record entry status change
            if trade.get("entry_status") != entry_status:
                fields["entry_status"] = entry_status

            # Record fill details on first FILLED observation
            if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
                planned = trade.get("entry_price") or fill_price
                fields["entry_fill_price"] = fill_price
                fields["entry_fill_time"]  = entry_order.get("updateTime")
                fields["entry_qty"]        = filled_qty
                fields["slippage_pct"]     = (
                    round((fill_price - planned) / planned * 100, 4)
                    if planned else None
                )
                print(f"  [{sym}] ✅ FILLED @ {fill_price:.6g}"
                      f"  slip={fields['slippage_pct']:+.3f}%")

            # ── Step 2: Place exit OCO if filled and not yet placed ───────
            if entry_status == "FILLED" and not trade.get("oco_placed"):
                # Build a trade dict that place_exit_orders() understands,
                # merging Supabase state with any freshly computed fill info.
                working = {**trade, **fields}
                print(f"  [{sym}] Placing OCO (SL={trade.get('sl')}  TP={trade.get('tp1')})...")
                try:
                    oco_resp = self.place_exit_orders(working)
                except RuntimeError as e:
                    print(f"  [{sym}] ❌ OCO failed: {e}")
                    oco_resp = None

                # Emergency market-sell path: place_exit_orders set _market_sold=True
                if working.get("_market_sold") and oco_resp:
                    entry_fill = working.get("entry_fill_price") or trade.get("entry_price", 0)
                    qty_       = working.get("entry_qty") or filled_qty or 0
                    exec_qty_  = float(oco_resp.get("executedQty", 0) or 0)
                    cum_q_     = float(oco_resp.get("cummulativeQuoteQty", 0) or 0)
                    exit_px    = (cum_q_ / exec_qty_) if exec_qty_ > 0 else trade.get("sl", 0)
                    pnl_usd    = qty_ * (exit_px - entry_fill)
                    pnl_pct    = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                    exit_ts    = int(
                        oco_resp.get("transactTime")
                        or oco_resp.get("updateTime")
                        or __import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc
                        ).timestamp() * 1000
                    )
                    fields.update({
                        "exit_status":          "SL_HIT",
                        "exit_price":           round(exit_px, 6),
                        "exit_time":            exit_ts,
                        "realized_pnl_usd":     round(pnl_usd, 4),
                        "realized_pnl_pct":     round(pnl_pct, 2),
                        "oco_placed":           False,
                        "oco_list_id":          None,
                    })
                    fill_t = working.get("entry_fill_time")
                    if fill_t:
                        fields["time_to_resolution_sec"] = (exit_ts - int(fill_t)) // 1000
                    print(f"  [{sym}] 🔴 Emergency MARKET SELL → SL_HIT  PnL=${pnl_usd:+.4f}")
                    dirty[eid] = {**trade, **fields}
                    # TODO: replace with self.log_trade() update call
                    update_spot_by_order_id(eid, fields)
                    continue

                if oco_resp:
                    reports = oco_resp.get("orderReports", [])
                    fields["oco_placed"]    = True
                    fields["oco_list_id"]   = oco_resp.get("orderListId")
                    fields["oco_order_ids"] = [o["orderId"] for o in reports]
                    print(f"  [{sym}] ✅ OCO placed  List#{fields['oco_list_id']}")
                else:
                    print(f"  [{sym}] ⚠ OCO placement failed — position UNPROTECTED")

            # ── Step 3: Poll OCO status for already-placed orders ─────────
            oco_str = "n/a"
            oco_list_id = trade.get("oco_list_id") or fields.get("oco_list_id")

            if (trade.get("oco_placed") or fields.get("oco_placed")) and oco_list_id:
                try:
                    oco_status  = self.client.v3_get_order_list(orderListId=oco_list_id)
                    list_status = oco_status.get("listOrderStatus", "UNKNOWN")
                    oco_str     = list_status

                    if list_status == "ALL_DONE":
                        for leg_ref in oco_status.get("orders", []):
                            leg = self.client.get_order(symbol=sym, orderId=leg_ref["orderId"])
                            if leg.get("status") == "FILLED":
                                leg_qty   = float(leg.get("executedQty", 0) or 1)
                                leg_quote = float(leg.get("cummulativeQuoteQty", 0) or 0)
                                exit_px   = (
                                    leg_quote / leg_qty if leg_qty > 0 and leg_quote > 0
                                    else float(leg.get("price", 0))
                                )
                                is_sl       = "STOP" in leg.get("type", "")
                                exit_status = "SL_HIT" if is_sl else "TP_HIT"

                                entry_fill  = (
                                    trade.get("entry_fill_price")
                                    or fields.get("entry_fill_price")
                                    or trade.get("entry_price", 0)
                                )
                                qty_        = (
                                    trade.get("entry_qty")
                                    or fields.get("entry_qty")
                                    or 0
                                )
                                notional_   = trade.get("entry_notional") or 1
                                pnl_usd     = qty_ * (exit_px - entry_fill)
                                pnl_pct     = pnl_usd / max(notional_, 0.001) * 100

                                exit_t      = leg.get("updateTime")
                                fill_t      = (
                                    trade.get("entry_fill_time")
                                    or fields.get("entry_fill_time")
                                )
                                ttr = None
                                if fill_t and exit_t:
                                    ttr = (int(exit_t) - int(fill_t)) // 1000

                                fields.update({
                                    "exit_status":          exit_status,
                                    "exit_price":           round(exit_px, 6),
                                    "exit_time":            exit_t,
                                    "realized_pnl_usd":     round(pnl_usd, 4),
                                    "realized_pnl_pct":     round(pnl_pct, 2),
                                    "time_to_resolution_sec": ttr,
                                })
                                icon    = "🟢" if exit_status == "TP_HIT" else "🔴"
                                oco_str = f"{icon} {exit_status}"
                                print(f"  [{sym}] {oco_str}  "
                                      f"exit={exit_px:.6g}  PnL=${pnl_usd:+.4f}")
                                break   # only one leg can be FILLED in ALL_DONE

                        # If resolved, persist and skip to next trade
                        if fields.get("exit_status") in ("TP_HIT", "SL_HIT"):
                            dirty[eid] = {**trade, **fields}
                            # TODO: replace with self.log_trade() update call
                            update_spot_by_order_id(eid, fields)
                            continue

                except Exception as e:
                    oco_str = f"⚠ {e}"

            # ── Step 3.5: Price-guard — SL breach not caught by OCO ───────
            # Spot is always LONG — SL is below entry.
            current = price_map.get(sym)
            if current is None:
                try:
                    current = float(self.client.get_symbol_ticker(symbol=sym)["price"])
                except Exception:
                    current = None

            sl_val = trade.get("sl")
            if (
                entry_status == "FILLED"
                and trade.get("exit_status") == "OPEN"
                and fields.get("exit_status") is None   # not already resolved above
                and current is not None
                and sl_val is not None
                and current <= sl_val
                and (trade.get("oco_placed") or fields.get("oco_placed"))
            ):
                entry_fill = (
                    trade.get("entry_fill_price")
                    or fields.get("entry_fill_price")
                    or trade.get("entry_price", 0)
                )
                qty_       = trade.get("entry_qty") or fields.get("entry_qty") or 0
                pnl_usd    = qty_ * (current - entry_fill)
                pnl_pct    = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                import datetime as _dt
                exit_ts    = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
                fill_t     = trade.get("entry_fill_time") or fields.get("entry_fill_time")
                ttr        = ((exit_ts - int(fill_t)) // 1000) if fill_t else None

                fields.update({
                    "exit_status":          "SL_HIT",
                    "exit_price":           round(current, 6),
                    "exit_time":            exit_ts,
                    "realized_pnl_usd":     round(pnl_usd, 4),
                    "realized_pnl_pct":     round(pnl_pct, 2),
                    "time_to_resolution_sec": ttr,
                })
                print(f"  [{sym}] 🛑 SL_HIT (price-guard): "
                      f"price={current:.6g} ≤ SL={sl_val:.6g}  PnL=${pnl_usd:+.4f}")
                dirty[eid] = {**trade, **fields}
                # TODO: replace with self.log_trade() update call
                update_spot_by_order_id(eid, fields)
                continue

            # ── Step 4: Persist any accumulated field changes ─────────────
            if fields:
                dirty[eid] = {**trade, **fields}
                # TODO: replace with self.log_trade() update call
                update_spot_by_order_id(eid, fields)

            # ── Compact display ───────────────────────────────────────────
            if not verbose:
                status_str = entry_status[:20]
                pnl_disp   = "n/a"
                if entry_status == "FILLED" and current is not None:
                    ref = (
                        trade.get("entry_fill_price")
                        or fields.get("entry_fill_price")
                        or trade.get("entry_price", 0)
                    )
                    qty_ = trade.get("entry_qty") or fields.get("entry_qty") or 0
                    if qty_ and ref:
                        pnl_disp = f"${qty_ * (current - ref):+.3f}"
                print(f"  {sym:<12} {status_str:<22} {pnl_disp:>10}  {oco_str}")

        resolved = sum(
            1 for f in dirty.values()
            if f.get("exit_status") in ("TP_HIT", "SL_HIT")
        )
        print(f"\n  [SpotOrderExecutor] Done — "
              f"{len(dirty)} trade(s) updated, {resolved} resolved.")

    def log_trade(self, order: dict, cand: dict, correlation_cluster_id: str | None = None) -> None:
        # TODO: Isi dengan logika log_trade (memanggil upsert_spot)
        pass


class FuturesOrderExecutor(OrderExecutor):
    """
    Implementasi Order Executor khusus untuk market Binance Futures.
    Mengadaptasi fungsi-fungsi asli dari futures_trade_executor.py.
    """

    def place_entry_order(self, candidate: dict) -> dict:
        # TODO: Isi dengan logika place_futures_limit_order
        # PENTING: Harus handle change_initial_leverage dan positionSide
        pass

    def place_exit_orders(self, trade: dict) -> dict:
        # TODO: Isi dengan logika place_futures_exit_orders
        # PENTING: Membuat 2 order bersyarat terpisah (STOP_MARKET & TAKE_PROFIT_MARKET)
        pass

    def check_positions(self, verbose: bool = False, mode: str = "all") -> None:
        # TODO: Isi dengan logika check_futures_positions
        # PENTING: Pengecekan algoId dan liquidation price
        pass

    def log_trade(self, order: dict, cand: dict, correlation_cluster_id: str | None = None) -> None:
        # TODO: Isi dengan logika log_futures_trade (memanggil upsert_futures)
        pass
