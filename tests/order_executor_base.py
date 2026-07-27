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
        Step 4 — Persist dirty state back to Supabase via self._persist_update().

        Persistence model:
          log_trade()        → INSERT new row when trade is first opened
          _persist_update()  → PATCH existing row throughout trade lifecycle

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
        """
        import sys
        import os
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

        from services.supabase_client import fetch_all_spot

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
                    self._persist_update(eid, fields)
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
                            self._persist_update(eid, fields)
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
                self._persist_update(eid, fields)
                continue

            # ── Step 4: Persist any accumulated field changes ─────────────
            if fields:
                dirty[eid] = {**trade, **fields}
                self._persist_update(eid, fields)

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

    def _persist_update(self, entry_order_id: int, fields: dict) -> None:
        """
        Patch specific fields on an existing trades_spot row.

        This is the internal persistence method used by check_positions()
        for state updates (fill detection, OCO placement, exit resolution).
        Wraps update_spot_by_order_id() so check_positions stays self-contained
        without direct supabase_client imports scattered through the method.

        log_trade()   → INSERT new row  (called once at trade open)
        _persist_update() → PATCH existing row (called during lifecycle)
        """
        from services.supabase_client import update_spot_by_order_id
        update_spot_by_order_id(entry_order_id, fields)

    # ── Class-level constants (mirror paper_trade_executor.py) ───────────────
    RULE_VERSION:    str   = "v1.0.0"
    BUDGET_USD:      float = 12.00
    TAKER_FEE_PCT:   float = 0.001   # 0.1% taker fee

    def log_trade(self, order: dict, cand: dict,
                  correlation_cluster_id: str | None = None) -> None:
        """
        Insert a new spot trade into Supabase trades_spot.

        Field schema is identical to log_trade() in paper_trade_executor.py —
        same field names, same types, same defaults. This ensures trades logged
        via SpotOrderExecutor are fully compatible with the dashboard and
        ml/train_v1.py without any schema migration.

        Parameters
        ----------
        order : dict
            Raw response from Binance create_order (entry limit order).
            Must contain at minimum: orderId, clientOrderId, status.
        cand : dict
            Candidate dict built by gather_candidates() / gather_all_candidates().
            Required keys: symbol, direction, entry_price, sl, tp1, rr,
            risk_pct, atr_pct, sizing (notional_usd, qty, max_loss_usd).
            Optional: entry_zone, winning_zone, tp2, ml_score,
            ml_model_version, symbol_rank, budget_for_slot.
        correlation_cluster_id : str | None
            Cluster ID from --propose-all batch run, None for single --propose.
        """
        from services.supabase_client import upsert_spot
        from datetime import datetime, timezone

        ez       = cand.get("entry_zone") or {}
        sizing   = cand["sizing"]
        notional = sizing["notional_usd"]

        record = {
            # ── Identity ──────────────────────────────────────────────
            "symbol":                 cand["symbol"],
            "direction":              cand["direction"],
            "budget_usd":             cand.get("budget_for_slot", self.BUDGET_USD),
            "rule_version":           self.RULE_VERSION,
            "correlation_cluster_id": correlation_cluster_id,

            # ── Entry order ───────────────────────────────────────────
            "entry_order_id":         order.get("orderId"),
            "entry_client_id":        order.get("clientOrderId"),
            "entry_status":           order.get("status", "NEW"),
            "entry_price":            cand["entry_price"],
            "entry_fill_price":       None,
            "entry_fill_time":        None,
            "entry_qty":              sizing["qty"],
            "entry_notional":         notional,
            "open_time":              datetime.now(timezone.utc).isoformat(),

            # ── OCO ───────────────────────────────────────────────────
            "oco_placed":             False,
            "oco_order_ids":          None,
            "oco_list_id":            None,

            # ── Levels ────────────────────────────────────────────────
            "sl":                     cand["sl"],
            "tp1":                    cand["tp1"],
            "tp2":                    cand.get("tp2"),
            "entry_zone_center":      ez.get("center"),
            "entry_zone_touches":     ez.get("touches"),

            # ── Setup metadata ────────────────────────────────────────
            "planned_rr":             cand["rr"],
            "risk_pct":               cand["risk_pct"],
            "max_loss_usd":           sizing["max_loss_usd"],
            "zone_type":              (cand["winning_zone"]["tier"]
                                       if cand.get("winning_zone") else "T1"),
            "zone_label":             (cand["winning_zone"]["label"]
                                       if cand.get("winning_zone") else None),
            "zone_touches":           ez.get("touches"),
            "atr_pct_at_entry":       cand["atr_pct"],

            # ── Cost estimates ────────────────────────────────────────
            "fee_usd_roundtrip":      round(notional * self.TAKER_FEE_PCT * 2, 4),
            "slippage_pct":           None,
            "time_to_resolution_sec": None,

            # ── Exit ──────────────────────────────────────────────────
            "exit_status":            "OPEN",
            "exit_price":             None,
            "exit_time":              None,
            "realized_pnl_usd":       None,
            "realized_pnl_pct":       None,

            # ── ML scoring (observation only) ─────────────────────────
            "ml_score":               cand.get("ml_score"),
            "ml_model_version":       cand.get("ml_model_version"),

            # ── Scan metadata (observation only) ──────────────────────
            "symbol_rank":            cand.get("symbol_rank"),

            # ── Raw ───────────────────────────────────────────────────
            "raw_entry_order":        order,
        }
        upsert_spot(record)
        print(f"  Trade inserted into Supabase trades_spot "
              f"(order #{record['entry_order_id']})")


class FuturesOrderExecutor(OrderExecutor):
    """
    Implementasi Order Executor untuk Binance Futures Testnet.
    Mengadaptasi fungsi-fungsi dari futures_trade_executor.py.
    """

    # ── Class-level constants (mirror futures_trade_executor.py) ─────────────
    LEVERAGE:       int   = 3
    MARGIN_MODE:    str   = "isolated"
    RULE_VERSION:   str   = "fv1.0.0"
    BUDGET_USD:     float = 12.00
    TAKER_FEE_PCT:  float = 0.0004   # 0.04% taker
    RISK_FRACTION:  float = 0.25
    MMR:            float = 0.004    # maintenance margin rate tier-1

    def get_symbol_constraints(self, symbol: str) -> dict:
        """Override: use futures_exchange_info() instead of spot get_symbol_info()."""
        now    = time.time()
        cached = self._constraints_cache.get(symbol)
        if cached and (now - cached["timestamp"]) <= self.CACHE_TTL_SECONDS:
            return cached["data"]

        info = self.client.futures_exchange_info()
        constraints = {"tick_size": 0.01, "step_size": 0.001,
                       "min_qty": 0.0, "min_notional": 5.0}
        for s in info.get("symbols", []):
            if s["symbol"] != symbol:
                continue
            for f in s.get("filters", []):
                ft = f["filterType"]
                if ft == "PRICE_FILTER":
                    constraints["tick_size"] = float(f["tickSize"])
                elif ft == "LOT_SIZE":
                    constraints["step_size"] = float(f["stepSize"])
                    constraints["min_qty"]   = float(f["minQty"])
                elif ft == "MIN_NOTIONAL":
                    constraints["min_notional"] = float(f.get("notional", 5.0))
            break

        self._constraints_cache[symbol] = {"data": constraints, "timestamp": now}
        return constraints

    def _set_leverage_and_margin(self, symbol: str) -> None:
        """Set isolated margin + leverage before placing any futures order."""
        try:
            self.client.futures_change_margin_type(
                symbol=symbol, marginType="ISOLATED")
        except Exception as e:
            if "No need to change" not in str(e):
                print(f"  [WARN] Margin mode: {e}")
        try:
            self.client.futures_change_leverage(
                symbol=symbol, leverage=self.LEVERAGE)
        except Exception as e:
            print(f"  [WARN] Leverage set: {e}")

    def _persist_update(self, entry_order_id: int, fields: dict) -> None:
        """Patch fields on existing trades_futures row."""
        from services.supabase_client import update_futures_by_order_id
        update_futures_by_order_id(entry_order_id, fields)


    def place_entry_order(self, candidate: dict) -> dict:
        """
        Place futures LIMIT entry order (LONG or SHORT).
        Sets leverage + margin mode before placing.
        """
        from binance.exceptions import BinanceAPIException

        sym   = candidate["symbol"]
        side  = "BUY" if candidate["position_side"] == "LONG" else "SELL"
        qty   = candidate["sizing"]["qty"]
        entry = candidate["entry_price"]

        constraints = self.get_symbol_constraints(sym)
        step = constraints.get("step_size", 0)
        tick = constraints.get("tick_size", 0)

        qty_str   = f"{self.round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        price_str = f"{self.round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

        self._set_leverage_and_margin(sym)

        try:
            return self.client.futures_create_order(
                symbol       = sym,
                side         = side,
                type         = "LIMIT",
                timeInForce  = "GTC",
                quantity     = qty_str,
                price        = price_str,
                positionSide = "BOTH",
            )
        except BinanceAPIException as e:
            raise RuntimeError(f"Futures order failed: {e}") from e


    def place_exit_orders(self, trade: dict) -> dict:
        """
        Place TP + SL as two separate algo orders after entry fills.
        Uses futures_create_algo_order(algoType=CONDITIONAL) because
        TAKE_PROFIT_MARKET / STOP_MARKET on Binance Futures always return
        algoId (not orderId) — standard behaviour for conditional orders.

        Returns dict: {tp_order_id, sl_order_id, tp_algo_id, sl_algo_id, success}
        """
        from binance.exceptions import BinanceAPIException
        import time as _time

        sym  = trade["symbol"]
        qty  = trade["entry_qty"]
        tp1  = trade["tp1"]
        sl   = trade["sl"]
        side = "SELL" if trade["position_side"] == "LONG" else "BUY"

        constraints = self.get_symbol_constraints(sym)
        tick = constraints.get("tick_size", 0.01)
        step = constraints.get("step_size", 0.001)

        qty_str = f"{self.round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        tp_str  = f"{self.round_tick(tp1, tick):.8f}".rstrip("0").rstrip(".")
        sl_str  = f"{self.round_tick(sl,  tick):.8f}".rstrip("0").rstrip(".")

        # Emergency check: price already past SL?
        try:
            current = float(self.client.futures_symbol_ticker(symbol=sym)["price"])
            is_long = trade["position_side"] == "LONG"
            if (is_long and current <= sl) or (not is_long and current >= sl):
                resp = self.client.futures_create_order(
                    symbol=sym, side=side, type="MARKET",
                    quantity=qty_str, positionSide="BOTH", reduceOnly=True,
                )
                return {"sl_order_id": resp.get("orderId"), "sl_algo_id": None,
                        "tp_order_id": None, "tp_algo_id": None,
                        "success": True, "emergency_exit": True}
        except Exception:
            pass

        results = {"tp_order_id": None, "sl_order_id": None,
                   "tp_algo_id": None,  "sl_algo_id": None, "success": False}

        def _place_and_verify(label: str, order_type: str, trigger: str):
            try:
                resp = self.client.futures_create_algo_order(
                    algoType=    "CONDITIONAL",
                    symbol=      sym,
                    side=        side,
                    type=        order_type,
                    quantity=    qty_str,
                    triggerPrice=trigger,
                    timeInForce= "GTC",
                    positionSide="BOTH",
                    reduceOnly=  "true",
                    workingType= "MARK_PRICE",
                )
            except Exception as e:
                print(f"  ❌ {label} algo order failed: {e}")
                return None, False

            algo_id = resp.get("algoId") or resp.get("orderId")
            if not algo_id:
                print(f"  ❌ {label} no algoId in response: {resp}")
                return None, False

            print(f"  ✅ {label} algo order: algoId={algo_id} @ {trigger}")
            _time.sleep(0.4)

            # Verify via open-orders list (symbol filter broken on testnet)
            try:
                all_open = self.client.futures_get_open_algo_orders()
                if isinstance(all_open, dict):
                    all_open = all_open.get("orders", [])
                found = any(str(o.get("algoId")) == str(algo_id)
                            for o in (all_open or []))
                if found:
                    return algo_id, True
                # Fallback: direct query without symbol
                verify = self.client.futures_get_algo_order(algoId=algo_id)
                if isinstance(verify, list):
                    verify = next((o for o in verify
                                   if str(o.get("algoId")) == str(algo_id)), {})
                v_status = (verify.get("algoStatus") or
                            verify.get("status") or "UNKNOWN")
                return algo_id, v_status.upper() in (
                    "NEW", "WORKING", "EXECUTING", "PARTIALLY_FILLED",
                    "FILLED", "EXECUTED", "COMPLETED")
            except Exception:
                return algo_id, True  # network hiccup — benefit of doubt

        tp_id, tp_ok = _place_and_verify("TP", "TAKE_PROFIT_MARKET", tp_str)
        sl_id, sl_ok = _place_and_verify("SL", "STOP_MARKET",        sl_str)

        results.update({"tp_order_id": tp_id, "sl_order_id": sl_id,
                        "tp_algo_id":  tp_id, "sl_algo_id":  sl_id,
                        "success": (tp_id is not None and sl_id is not None)})
        return results


    def check_positions(self, verbose: bool = False, mode: str = "all") -> None:
        """
        Check all OPEN futures trades and drive their state machine:

        Step 1 — Query entry order status (futures_get_order).
        Step 2 — If FILLED + no exit orders → place_exit_orders().
        Step 3 — Query TP/SL algo order status via futures_get_algo_order.
                 On FILLED/EXECUTED → resolve TP_HIT or SL_HIT.
        Step 3.5 — Price-guard: if price breached SL → cancel all open
                   algo orders for symbol + resolve SL_HIT.
        Step 4 — Persist dirty state via _persist_update().
        """
        import sys
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
        from services.supabase_client import fetch_all_futures
        from datetime import datetime, timezone

        trades      = fetch_all_futures()
        open_trades = [t for t in trades if t.get("exit_status") == "OPEN"]

        if not open_trades:
            print("\n  [FuturesOrderExecutor] No open futures positions.")
            return

        print(f"\n  [FuturesOrderExecutor] Checking {len(open_trades)} open position(s)...")

        try:
            all_tickers = {t["symbol"]: float(t["price"])
                           for t in self.client.futures_symbol_ticker()}
        except Exception:
            all_tickers = {}

        for trade in open_trades:
            sym  = trade["symbol"]
            side = trade.get("position_side", "LONG")
            eid  = trade.get("entry_order_id")
            if not eid:
                continue

            fields: dict = {}

            # ── Step 1: Entry order status ────────────────────────────
            try:
                entry_order  = self.client.futures_get_order(
                    symbol=sym, orderId=eid)
                entry_status = entry_order.get("status", "UNKNOWN")
            except Exception as e:
                print(f"  [{sym}] ⚠ Could not query entry order: {e}")
                continue

            filled_qty = float(entry_order.get("executedQty", 0) or 0)
            cum_quote  = float(entry_order.get("cumQuote", 0) or 0)
            if filled_qty > 0 and cum_quote > 0:
                fill_price = cum_quote / filled_qty
            else:
                avg = float(entry_order.get("avgPrice", 0) or 0)
                fill_price = avg if avg > 0 else float(
                    entry_order.get("price", trade.get("entry_price", 0)))

            if trade.get("entry_status") != entry_status:
                fields["entry_status"] = entry_status

            if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
                planned = trade.get("entry_price") or fill_price
                fields.update({
                    "entry_fill_price": fill_price,
                    "entry_fill_time":  entry_order.get("updateTime"),
                    "entry_qty":        filled_qty,
                    "slippage_pct":     (round((fill_price - planned)
                                               / planned * 100, 4)
                                         if planned else None),
                    "last_funding_check_time": entry_order.get("updateTime"),
                })
                print(f"  [{sym}] ✅ FILLED @ {fill_price:.6g}  "
                      f"slip={fields['slippage_pct']:+.3f}%")

            # ── Step 2: Place exit orders if needed ───────────────────
            if entry_status == "FILLED" and not trade.get("exit_orders_placed"):
                working = {**trade, **fields}
                print(f"  [{sym}] Placing TP+SL algo orders...")
                exit_result = self.place_exit_orders(working)

                if exit_result.get("emergency_exit"):
                    current = all_tickers.get(sym, fill_price)
                    qty_    = working.get("entry_qty") or filled_qty or 0
                    pnl     = (current - fill_price) * qty_ * (
                        1 if side == "LONG" else -1)
                    fields.update({
                        "exit_status":    "SL_HIT",
                        "exit_price":     round(current, 6),
                        "exit_time":      int(datetime.now(timezone.utc)
                                              .timestamp() * 1000),
                        "realized_pnl_usd": round(pnl, 4),
                        "exit_orders_placed": False,
                    })
                    self._persist_update(eid, fields)
                    continue

                if exit_result.get("success"):
                    fields.update({
                        "tp_order_id":      exit_result["tp_order_id"],
                        "sl_order_id":      exit_result["sl_order_id"],
                        "tp_algo_id":       exit_result["tp_algo_id"],
                        "sl_algo_id":       exit_result["sl_algo_id"],
                        "exit_orders_placed": True,
                    })
                    print(f"  [{sym}] ✅ Exit orders placed")
                else:
                    print(f"  [{sym}] ❌ Exit order placement failed — "
                          f"position UNPROTECTED")

            # ── Step 3: Check algo order status ───────────────────────
            tp_algo = trade.get("tp_algo_id") or fields.get("tp_algo_id")
            sl_algo = trade.get("sl_algo_id") or fields.get("sl_algo_id")
            exit_str = "n/a"

            if (trade.get("exit_orders_placed") or fields.get("exit_orders_placed")) \
                    and (tp_algo or sl_algo):

                def _query_algo(algo_id):
                    if not algo_id:
                        return None, None
                    try:
                        r = self.client.futures_get_algo_order(algoId=algo_id)
                        if isinstance(r, list):
                            r = next((o for o in r
                                      if str(o.get("algoId")) == str(algo_id)), {})
                        status = (r.get("algoStatus") or r.get("status")
                                  or "UNKNOWN")
                        qty_f  = float(r.get("executedQty") or 0)
                        quote  = float(r.get("cumQuote") or 0)
                        price  = (quote / qty_f if qty_f > 0 and quote > 0
                                  else float(r.get("triggerPrice") or 0))
                        return status, price
                    except Exception:
                        return None, None

                for algo_id, label in [(tp_algo, "TP"), (sl_algo, "SL")]:
                    status, price = _query_algo(algo_id)
                    if status and status.upper() in (
                            "FILLED", "EXECUTED", "COMPLETED"):
                        is_tp  = (label == "TP")
                        exit_s = "TP_HIT" if is_tp else "SL_HIT"
                        ef     = (trade.get("entry_fill_price")
                                  or fields.get("entry_fill_price")
                                  or trade.get("entry_price", 0))
                        qty_   = (trade.get("entry_qty")
                                  or fields.get("entry_qty") or 0)
                        mult   = 1 if side == "LONG" else -1
                        pnl    = (price - ef) * qty_ * mult
                        exit_t = int(datetime.now(timezone.utc).timestamp() * 1000)
                        fill_t = (trade.get("entry_fill_time")
                                  or fields.get("entry_fill_time"))
                        fields.update({
                            "exit_status":     exit_s,
                            "exit_price":      round(price, 6),
                            "exit_time":       exit_t,
                            "realized_pnl_usd": round(pnl, 4),
                            "realized_pnl_pct": round(
                                pnl / max(trade.get("entry_notional", 1),
                                          0.001) * 100, 2),
                            "time_in_position_sec": (
                                (exit_t - int(fill_t)) // 1000
                                if fill_t else None),
                        })
                        icon = "🟢" if is_tp else "🔴"
                        print(f"  [{sym}] {icon} {exit_s}  "
                              f"exit={price:.6g}  PnL=${pnl:+.4f}")
                        # Cancel counterpart + any ghost orders
                        self._cancel_all_open_algo_orders(sym)
                        self._persist_update(eid, fields)
                        break
                    elif status:
                        exit_str = status

            if fields.get("exit_status") in ("TP_HIT", "SL_HIT"):
                continue   # already persisted above

            # ── Step 3.5: Price-guard ─────────────────────────────────
            current = all_tickers.get(sym)
            if current is None:
                try:
                    current = float(
                        self.client.futures_symbol_ticker(symbol=sym)["price"])
                except Exception:
                    current = None

            sl_val = trade.get("sl")
            if (entry_status == "FILLED"
                    and trade.get("exit_status") == "OPEN"
                    and fields.get("exit_status") is None
                    and current is not None and sl_val is not None
                    and (trade.get("exit_orders_placed")
                         or fields.get("exit_orders_placed"))):
                breached = ((side == "LONG"  and current <= sl_val) or
                            (side == "SHORT" and current >= sl_val))
                if breached:
                    ef   = (trade.get("entry_fill_price")
                            or fields.get("entry_fill_price")
                            or trade.get("entry_price", 0))
                    qty_ = trade.get("entry_qty") or fields.get("entry_qty") or 0
                    mult = 1 if side == "LONG" else -1
                    pnl  = (current - ef) * qty_ * mult
                    exit_t = int(datetime.now(timezone.utc).timestamp() * 1000)
                    fill_t = (trade.get("entry_fill_time")
                              or fields.get("entry_fill_time"))
                    fields.update({
                        "exit_status":     "SL_HIT",
                        "exit_price":      round(current, 6),
                        "exit_time":       exit_t,
                        "realized_pnl_usd": round(pnl, 4),
                        "realized_pnl_pct": round(
                            pnl / max(trade.get("entry_notional", 1),
                                      0.001) * 100, 2),
                        "time_in_position_sec": (
                            (exit_t - int(fill_t)) // 1000 if fill_t else None),
                    })
                    print(f"  [{sym}] 🛑 SL_HIT (price-guard)  "
                          f"price={current:.6g}  PnL=${pnl:+.4f}")
                    self._cancel_all_open_algo_orders(sym)
                    self._persist_update(eid, fields)
                    continue

            # ── Step 4: Persist accumulated changes ───────────────────
            if fields:
                self._persist_update(eid, fields)

            if not verbose:
                pnl_disp = "n/a"
                if entry_status == "FILLED" and current is not None:
                    ef   = (trade.get("entry_fill_price")
                            or fields.get("entry_fill_price")
                            or trade.get("entry_price", 0))
                    qty_ = (trade.get("entry_qty")
                            or fields.get("entry_qty") or 0)
                    mult = 1 if side == "LONG" else -1
                    if ef and qty_:
                        pnl_disp = f"${(current - ef) * qty_ * mult:+.4f}"
                print(f"  {sym:<12} {side:<6} {entry_status:<20} "
                      f"{pnl_disp:>10}  {exit_str}")

        print(f"\n  [FuturesOrderExecutor] Done.")

    def _cancel_all_open_algo_orders(self, symbol: str) -> None:
        """Cancel all open algo + regular exit orders for a symbol."""
        # Pass 1: algo orders (unfiltered — symbol filter broken on testnet)
        try:
            all_algos = self.client.futures_get_open_algo_orders()
            if isinstance(all_algos, dict):
                all_algos = all_algos.get("orders", [])
            for o in (all_algos or []):
                if o.get("symbol") == symbol:
                    aid = o.get("algoId")
                    if aid:
                        try:
                            self.client.futures_cancel_algo_order(
                                symbol=symbol, algoId=aid)
                        except Exception:
                            pass
        except Exception:
            pass
        # Pass 2: regular reduceOnly exit orders
        try:
            EXIT_TYPES = {"TAKE_PROFIT_MARKET", "STOP_MARKET",
                          "TAKE_PROFIT", "STOP"}
            opens = self.client.futures_get_open_orders(symbol=symbol)
            if isinstance(opens, dict):
                opens = opens.get("orders", [])
            for o in (opens or []):
                if o.get("type") in EXIT_TYPES and o.get("reduceOnly"):
                    try:
                        self.client.futures_cancel_order(
                            symbol=symbol, orderId=o["orderId"])
                    except Exception:
                        pass
        except Exception:
            pass


    def log_trade(self, order: dict, cand: dict,
                  correlation_cluster_id: str | None = None) -> None:
        """
        Insert new futures trade into Supabase trades_futures.
        Schema identical to log_futures_trade() in futures_trade_executor.py.
        """
        from services.supabase_client import upsert_futures
        from datetime import datetime, timezone

        sizing = cand["sizing"]
        liq    = cand["liquidation"]
        ez     = cand.get("entry_zone") or {}

        record = {
            # Identity
            "symbol":                cand["symbol"],
            "position_side":         cand["position_side"],
            "direction":             cand["direction"],
            "margin_budget":         self.BUDGET_USD,
            "leverage":              self.LEVERAGE,
            "margin_mode":           self.MARGIN_MODE,
            "rule_version":          self.RULE_VERSION,
            "correlation_cluster_id": correlation_cluster_id,
            # Entry
            "entry_order_id":        order.get("orderId"),
            "entry_client_id":       order.get("clientOrderId"),
            "entry_status":          order.get("status", "NEW"),
            "entry_price":           cand["entry_price"],
            "entry_fill_price":      None,
            "entry_fill_time":       None,
            "entry_qty":             sizing["qty"],
            "entry_notional":        sizing["notional_usd"],
            "margin_used":           sizing["margin_used"],
            "open_time":             datetime.now(timezone.utc).isoformat(),
            # Exit orders
            "tp_order_id":           None,
            "sl_order_id":           None,
            "tp_algo_id":            None,
            "sl_algo_id":            None,
            "exit_orders_placed":    False,
            # Levels
            "sl":                    cand["sl"],
            "tp1":                   cand["tp1"],
            "tp2":                   cand.get("tp2"),
            "entry_zone_center":     ez.get("center"),
            "entry_zone_touches":    ez.get("touches"),
            # Liquidation
            "liquidation_price":             liq["liquidation_price"],
            "distance_to_liquidation_pct":   liq["distance_to_liquidation_pct"],
            # Setup metadata
            "planned_rr":            cand["rr"],
            "risk_pct":              cand["risk_pct"],
            "max_loss_usd":          sizing["max_loss_usd"],
            "zone_type":             cand.get("tier_used", "T1"),
            "zone_touches":          ez.get("touches"),
            "atr_pct_at_entry":      cand["atr_pct"],
            "volatility_regime_at_entry": cand.get("volatility_regime", "unknown"),
            "funding_rate_at_entry": cand.get("funding_rate_at_entry"),
            # Cost
            "fee_usd_roundtrip":     round(
                sizing["notional_usd"] * self.TAKER_FEE_PCT * 2, 4),
            "slippage_pct":          None,
            # Exit
            "exit_status":           "OPEN",
            "exit_price":            None,
            "exit_time":             None,
            "realized_pnl_usd":      None,
            "realized_pnl_pct":      None,
            "time_in_position_sec":  None,
            # ML excursion fields
            "max_adverse_excursion_pct":       None,
            "max_favorable_excursion_pct":     None,
            "distance_to_liquidation_pct_min": None,
            "funding_rate_paid":               0.0,
            "funding_rate_history":            [],
            "last_funding_check_time":         None,
            # Raw
            "raw_entry_order":       order,
        }
        upsert_futures(record)
        print(f"  Futures trade inserted into Supabase "
              f"(order #{record['entry_order_id']})")
