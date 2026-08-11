"""
spot_order_executor.py

Object-Oriented Executor for Spot Trade Proposals.
Encapsulates order validation, position sizing, payload construction, and execution.
Imports helper math/precision/Supabase functions from the legacy paper_trade_executor.py.
"""

import sys

try:
    from core.utils.binance_math import (
        get_symbol_constraints,
        compute_position_size,
        round_step,
        round_tick,
    )
except ImportError as e:
    print(f"Error importing dependencies: {e}")
    sys.exit(1)


class SpotOrderExecutor:
    """
    Object-Oriented Executor for Spot Trade Proposals.
    """

    def __init__(self, client, dry_run: bool = False, auto_confirm: bool = False, repo=None):
        from core.repositories.spot_trade_repository import SpotTradeRepository
        self.client = client
        self.dry_run = dry_run
        self.auto_confirm = auto_confirm
        self.repo = repo or SpotTradeRepository()

    # ------------------------------------------------------------------
    # Validation & Sizing
    # ------------------------------------------------------------------
    def validate_and_size(self, cand: dict, budget_usd: float) -> bool:
        """
        Validates candidate against exchange constraints and computes position size.
        Mutates cand in-place by attaching 'sizing' and 'constraints'.
        Returns True if valid.
        """
        try:
            constraints = get_symbol_constraints(self.client, cand["symbol"])
        except Exception:
            return False

        from core.paper_trade_executor import RISK_FRACTION

        sizing = compute_position_size(
            entry_price   = cand["entry_price"],
            sl_price      = cand["sl"],
            budget_usd    = budget_usd,
            risk_fraction = RISK_FRACTION,
            constraints   = constraints,
        )
        cand["sizing"]      = sizing
        cand["constraints"] = constraints

        fatal = [w for w in sizing["warnings"]
                 if "below exchange minimum" in w or "cannot size" in w
                 or "exceeds total budget" in w]
        if fatal or sizing["qty"] <= 0:
            return False

        return True

    # ------------------------------------------------------------------
    # Payload Construction
    # ------------------------------------------------------------------
    def build_payload(self, cand: dict) -> dict:
        """Constructs the kwargs payload for Binance Spot create_order."""
        from binance.enums import SIDE_BUY, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC

        sym       = cand["symbol"]
        qty       = cand["sizing"]["qty"]
        entry     = cand["entry_price"]
        step      = cand["constraints"].get("step_size", 0)
        tick      = cand["constraints"].get("tick_size", 0)

        qty_str   = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        price_str = f"{round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

        return {
            "symbol": sym,
            "side": SIDE_BUY,
            "type": ORDER_TYPE_LIMIT,
            "timeInForce": TIME_IN_FORCE_GTC,
            "quantity": qty_str,
            "price": price_str,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self, cand: dict, correlation_cluster_id: str | None = None) -> dict:
        """Executes the order on testnet and logs it to Supabase."""
        from binance.exceptions import BinanceAPIException

        if self.dry_run:
            print(f"\n  [DRY RUN] SpotOrderExecutor: skipping execution for {cand['symbol']}")
            payload = self.build_payload(cand)
            print(f"  [DRY RUN] Payload: {payload}")
            return {
                "orderId": f"DRY_{cand['symbol']}_123",
                "symbol": payload["symbol"],
                "side": payload["side"],
                "status": "NEW",
                "price": payload["price"],
                "origQty": payload["quantity"],
            }

        payload = self.build_payload(cand)
        try:
            order = self.client.create_order(**payload)
        except BinanceAPIException as e:
            raise RuntimeError(f"Binance API error: {e}") from e

        self.repo.log_trade(order, cand, correlation_cluster_id=correlation_cluster_id)
        return order

    # ------------------------------------------------------------------
    # Lifecycle Management (OCO, Cancel, Status)
    # ------------------------------------------------------------------
    def get_order_status(self, symbol: str, order_id: int) -> dict:
        """Query order status from Binance API."""
        from binance.exceptions import BinanceAPIException
        try:
            return self.client.get_order(symbol=symbol, orderId=order_id)
        except BinanceAPIException as e:
            raise RuntimeError(f"Get order status failed for {symbol}: {e}") from e

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an entry or stale order."""
        if self.dry_run:
            print(f"  [DRY RUN] SpotOrderExecutor: cancelling order {order_id} for {symbol}")
            return {"status": "CANCELED"}
        from binance.exceptions import BinanceAPIException
        try:
            return self.client.cancel_order(symbol=symbol, orderId=order_id)
        except BinanceAPIException as e:
            raise RuntimeError(f"Cancel order failed for {symbol}: {e}") from e

    def close_position(self, trade: dict) -> dict:
        """Market-sell a filled spot position through the shared executor."""
        from binance.exceptions import BinanceAPIException
        from binance.enums import SIDE_SELL, ORDER_TYPE_MARKET

        sym = trade["symbol"]
        qty = trade["entry_qty"]
        try:
            info = self.client.get_symbol_info(sym)
            step = next(
                float(f["stepSize"]) for f in info["filters"]
                if f["filterType"] == "LOT_SIZE"
            )
        except Exception:
            step = 0.001

        qty_str = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        try:
            return self.client.create_order(
                symbol=sym,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=qty_str,
            )
        except BinanceAPIException as e:
            raise RuntimeError(f"Market sell failed for {sym}: {e}") from e

    def place_oco_order(self, trade: dict) -> dict:
        """
        Place OCO SELL after LONG entry is filled.

        New Binance OCO API (python-binance ≥1.0.37) uses above/below leg structure:
          above leg (price > current) = LIMIT_MAKER  → TP
          below leg (price < current) = STOP_LOSS_LIMIT → SL

        For SELL OCO:
            aboveType = LIMIT_MAKER       (TP: fills when price rises to tp1)
            abovePrice = tp1

            belowType = STOP_LOSS_LIMIT   (SL: triggers when price drops to sl)
            belowStopPrice  = sl          (trigger price)
            belowPrice      = sl * 0.9985 (limit fill price, 0.15% below trigger)
            belowTimeInForce = GTC

        Price constraint the exchange enforces:
            abovePrice > lastPrice > belowStopPrice

        Race condition handling:
            - If price already exceeded TP1 at OCO placement time → adjust TP1 to
              current + small buffer so OCO is still valid and position is protected.
            - If price already dropped below SL at placement time → place market SELL
              immediately to cut loss, do not attempt OCO.
            - Retries up to MAX_OCO_RETRIES with fresh price each time on constraint errors.

        Raises RuntimeError only if all retries exhausted or fatal API error.
        """
        from binance.exceptions import BinanceAPIException

        MAX_OCO_RETRIES = 3
        # Buffer above current price when TP needs to be adjusted (0.3%)
        TP_ADJUST_BUFFER = 0.003

        sym = trade["symbol"]
        qty = trade["entry_qty"]
        sl  = trade["sl"]

        # Fetch symbol precision once
        try:
            info = self.client.get_symbol_info(sym)
            tick = next(
                float(f["tickSize"]) for f in info["filters"]
                if f["filterType"] == "PRICE_FILTER"
            )
            step = next(
                float(f["stepSize"]) for f in info["filters"]
                if f["filterType"] == "LOT_SIZE"
            )
        except Exception:
            tick, step = 0.01, 0.001

        qty_str = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")

        last_err = None
        for attempt in range(1, MAX_OCO_RETRIES + 1):
            # Always re-fetch current price on each attempt
            try:
                current = float(self.client.get_symbol_ticker(symbol=sym)["price"])
            except Exception as e:
                raise RuntimeError(f"Could not fetch current price for {sym}: {e}") from e

            tp1 = trade["tp1"]  # start with planned TP

            # ── Race condition: price already below SL ─────────────────────
            if current <= sl:
                # Place immediate market sell — position already at/past SL
                print(f"\n  ⚠  [{sym}] Price {current:.4f} ≤ SL {sl:.4f} at OCO placement.")
                print(f"       Placing MARKET SELL immediately to cut loss.")
                try:
                    resp = self.close_position(trade)
                    print(f"  ✅ Market SELL placed: {resp.get('orderId')}")
                    # Mark the trade dict so caller can update log
                    trade["_market_sold"] = True
                    return resp
                except BinanceAPIException as e:
                    raise RuntimeError(f"Market sell failed for {sym}: {e}") from e

            # ── Race condition: price already above TP1 ────────────────────
            if current >= tp1:
                # Adjust TP1 upward: current + buffer, so OCO constraint holds
                adjusted_tp = round_tick(current * (1 + TP_ADJUST_BUFFER), tick)
                print(f"\n  ⚠  [{sym}] Price {current:.4f} ≥ TP1 {tp1:.4f} — price exceeded target.")
                print(f"       Adjusting TP1 → {adjusted_tp:.4f} (current + {TP_ADJUST_BUFFER*100:.1f}% buffer)")
                print(f"       Position already in profit beyond original target — OCO will protect gains.")
                tp1 = adjusted_tp
                trade["tp1"] = adjusted_tp  # update so log reflects actual OCO price

            # ── Final constraint check ─────────────────────────────────────
            if not (tp1 > current > sl):
                last_err = RuntimeError(
                    f"OCO constraint still invalid after adjustment attempt {attempt}: "
                    f"tp1={tp1:.4f} current={current:.4f} sl={sl:.4f}"
                )
                import time as _time; _time.sleep(2)
                continue

            # ── Build OCO legs ─────────────────────────────────────────────
            sl_stop  = round_tick(sl, tick)
            sl_limit = round_tick(sl * 0.9985, tick)
            if sl_limit >= sl_stop:
                sl_limit = round_tick(sl_stop - tick, tick)

            tp_price     = round_tick(tp1, tick)
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
                    import time as _time; _time.sleep(2)
                    continue
                raise RuntimeError(f"OCO placement failed: {e}") from e

        raise last_err or RuntimeError(f"OCO placement failed after {MAX_OCO_RETRIES} attempts")
