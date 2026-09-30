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

    # ------------------------------------------------------------------
    # Filter Preflight
    # ------------------------------------------------------------------

    def _check_percent_price_filter(self, sym: str, price: float) -> dict:
        """
        Fetches the PERCENT_PRICE_BY_SIDE filter for *sym* from the exchange and
        evaluates whether *price* (a SELL limit/stop price) is within the allowed
        band relative to the current avgPrice.

        Returns:
            {
                "valid":             bool,
                "filter_found":      bool,
                "avg_price":         float | None,
                "min_allowed":       float | None,  # askMultiplierDown * avgPrice
                "max_allowed":       float | None,  # askMultiplierUp   * avgPrice
                "ratio":             float | None,  # price / avgPrice
                "ask_multiplier_down": float | None,
            }

        If the filter is NOT present on the symbol or cannot be fetched, returns
        valid=True, filter_found=False so we fall back to the live OCO call
        (which already handles PERCENT_PRICE_BY_SIDE as a fail-fast error).
        """
        result: dict = {
            "valid": True, "filter_found": False,
            "avg_price": None, "min_allowed": None, "max_allowed": None,
            "ratio": None, "ask_multiplier_down": None,
        }
        try:
            info = self.client.get_symbol_info(sym)
            if not info:
                return result
            ppbs = next(
                (f for f in info["filters"] if f["filterType"] == "PERCENT_PRICE_BY_SIDE"),
                None,
            )
            if ppbs is None:
                return result

            ask_down = float(ppbs.get("askMultiplierDown", 0))
            ask_up   = float(ppbs.get("askMultiplierUp", 999))
            avg_mins = int(ppbs.get("avgPriceMins", 5))

            # avgPrice: use ticker averagePrice endpoint (avgPriceMins window).
            # python-binance exposes it via get_avg_price(); fall back to last price.
            avg_price = None
            try:
                avg_resp  = self.client.get_avg_price(symbol=sym)
                avg_price = float(avg_resp.get("price", 0) or 0)
            except Exception:
                pass
            if not avg_price:
                avg_price = float(
                    self.client.get_symbol_ticker(symbol=sym).get("price", 0) or 0
                )
            if not avg_price:
                return result

            min_allowed = ask_down * avg_price
            max_allowed = ask_up   * avg_price
            ratio = price / avg_price

            result.update({
                "filter_found":        True,
                "avg_price":           avg_price,
                "min_allowed":         min_allowed,
                "max_allowed":         max_allowed,
                "ratio":               ratio,
                "ask_multiplier_down": ask_down,
                "valid":               min_allowed <= price <= max_allowed,
            })
        except Exception:
            # On any unexpected error return valid=True / filter_found=False so
            # we don't incorrectly block a valid OCO attempt.
            pass
        return result

    def place_oco_order(self, trade: dict) -> dict:
        """
        Place OCO SELL after LONG entry is filled.  Returns a structured dict:

            {
                "protection_state": "FULLY_PROTECTED" | "TP_ONLY" | "SL_ONLY" | "UNPROTECTED",
                "oco_resp":         dict | None,    # raw exchange response for full OCO
                "tp_order_id":      int  | None,    # standalone TP order id (TP_ONLY)
                "sl_order_id":      int  | None,    # standalone SL order id (SL_ONLY)
                "filter_reason":    str  | None,    # which leg failed the preflight
                "_market_sold":     bool,           # True when position was emergency-sold
            }

        Preflight:
          Checks both TP and SL against the exchange PERCENT_PRICE_BY_SIDE filter
          using the live avgPrice *before* sending any order.  This avoids burning
          retry attempts on structurally-invalid prices.

        Paths:
          FULLY_PROTECTED  — Both legs valid → standard OCO placed.
          TP_ONLY          — TP valid, SL outside filter → standalone LIMIT_MAKER SELL at TP.
          SL_ONLY          — SL valid, TP invalid (rare) → standalone STOP_LOSS_LIMIT at SL.
          UNPROTECTED      — Both invalid → no exchange call; position needs monitoring.

        Race-condition handling (unchanged):
          - Price already ≤ SL → emergency MARKET SELL immediately (_market_sold=True).
          - Price already ≥ TP1 → adjust TP1 upward by TP_ADJUST_BUFFER and continue.

        NEVER mutates trade["sl"] or trade["tp1"] for preflight failures —
        those values remain the original strategy values.

        Raises RuntimeError only for fatal non-filter errors.
        """
        from binance.exceptions import BinanceAPIException

        MAX_OCO_RETRIES = 3
        TP_ADJUST_BUFFER = 0.003  # 0.3% buffer above current price for TP adjustment

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

        # ── Fetch current price for race-condition checks ──────────────────────
        try:
            current = float(self.client.get_symbol_ticker(symbol=sym)["price"])
        except Exception as e:
            raise RuntimeError(f"Could not fetch current price for {sym}: {e}") from e

        # ── Race condition: price already below SL ─────────────────────────────
        if current <= sl:
            print(f"\n  ⚠  [{sym}] Price {current:.4f} ≤ SL {sl:.4f} at OCO placement.")
            print(f"       Placing MARKET SELL immediately to cut loss.")
            try:
                resp = self.close_position(trade)
                print(f"  ✅ Market SELL placed: {resp.get('orderId')}")
                trade["_market_sold"] = True
                return {
                    "protection_state": "UNPROTECTED",
                    "oco_resp": resp,
                    "tp_order_id": None,
                    "sl_order_id": None,
                    "filter_reason": "PRICE_BELOW_SL",
                    "_market_sold": True,
                }
            except BinanceAPIException as e:
                raise RuntimeError(f"Market sell failed for {sym}: {e}") from e

        # ── Race condition: price already above TP1 ────────────────────────────
        tp1 = trade["tp1"]
        if current >= tp1:
            adjusted_tp = round_tick(current * (1 + TP_ADJUST_BUFFER), tick)
            print(f"\n  ⚠  [{sym}] Price {current:.4f} ≥ TP1 {tp1:.4f} — price exceeded target.")
            print(f"       Adjusting TP1 → {adjusted_tp:.4f} (current + {TP_ADJUST_BUFFER*100:.1f}% buffer)")
            tp1 = adjusted_tp
            trade["tp1"] = adjusted_tp  # update so log reflects actual OCO price

        # ── Preflight: check both legs against PERCENT_PRICE_BY_SIDE ──────────
        tp_check = self._check_percent_price_filter(sym, round_tick(tp1, tick))
        sl_check = self._check_percent_price_filter(sym, round_tick(sl,  tick))
        tp_valid = tp_check["valid"]
        sl_valid = sl_check["valid"]

        # Build precision strings used in all branches
        sl_stop  = round_tick(sl, tick)
        sl_limit = round_tick(sl * 0.9985, tick)
        if sl_limit >= sl_stop:
            sl_limit = round_tick(sl_stop - tick, tick)
        tp_price     = round_tick(tp1, tick)
        tp_str       = f"{tp_price:.8f}".rstrip("0").rstrip(".")
        sl_stop_str  = f"{sl_stop:.8f}".rstrip("0").rstrip(".")
        sl_limit_str = f"{sl_limit:.8f}".rstrip("0").rstrip(".")

        # ── Branch: both invalid → UNPROTECTED ────────────────────────────────
        if not tp_valid and not sl_valid:
            print(
                f"  ⚠  [{sym}] Both TP ({tp_str}) and SL ({sl_stop_str}) fail "
                f"PERCENT_PRICE_BY_SIDE preflight — UNPROTECTED (no exchange call)."
            )
            return {
                "protection_state": "UNPROTECTED",
                "oco_resp": None,
                "tp_order_id": None,
                "sl_order_id": None,
                "filter_reason": "BOTH_LEGS_FILTER_INVALID",
            }

        # ── Branch: only TP valid → TP_ONLY ───────────────────────────────────
        if tp_valid and not sl_valid:
            ratio = sl_check.get("ratio")
            ratio_str = f"{ratio:.3f}" if ratio is not None else "n/a"
            min_allowed = sl_check.get("min_allowed")
            min_str = f"{min_allowed:.4f}" if min_allowed is not None else "n/a"
            print(
                f"  ⚠  [{sym}] SL {sl_stop_str} fails PERCENT_PRICE_BY_SIDE preflight "
                f"(ratio={ratio_str} < askMultiplierDown threshold, min_allowed={min_str}). "
                f"Placing standalone TP LIMIT_MAKER SELL at {tp_str}."
            )
            try:
                tp_resp = self.client.create_order(
                    symbol      = sym,
                    side        = "SELL",
                    type        = "LIMIT_MAKER",
                    quantity    = qty_str,
                    price       = tp_str,
                )
                tp_order_id = tp_resp.get("orderId")
                print(f"  ✅ [{sym}] Standalone TP order placed: orderId={tp_order_id}")
                return {
                    "protection_state": "TP_ONLY",
                    "oco_resp": None,
                    "tp_order_id": tp_order_id,
                    "sl_order_id": None,
                    "filter_reason": "SL_FILTER_INVALID",
                }
            except BinanceAPIException as e:
                raise RuntimeError(
                    f"[{sym}] Standalone TP order failed: {e}"
                ) from e

        # ── Branch: only SL valid → SL_ONLY ───────────────────────────────────
        if sl_valid and not tp_valid:
            ratio = tp_check.get("ratio")
            ratio_str = f"{ratio:.3f}" if ratio is not None else "n/a"
            print(
                f"  ⚠  [{sym}] TP {tp_str} fails PERCENT_PRICE_BY_SIDE preflight "
                f"(ratio={ratio_str}). "
                f"Placing standalone SL STOP_LOSS_LIMIT SELL at {sl_stop_str}."
            )
            try:
                sl_resp = self.client.create_order(
                    symbol        = sym,
                    side          = "SELL",
                    type          = "STOP_LOSS_LIMIT",
                    timeInForce   = "GTC",
                    quantity      = qty_str,
                    stopPrice     = sl_stop_str,
                    price         = sl_limit_str,
                )
                sl_order_id = sl_resp.get("orderId")
                print(f"  ✅ [{sym}] Standalone SL order placed: orderId={sl_order_id}")
                return {
                    "protection_state": "SL_ONLY",
                    "oco_resp": None,
                    "tp_order_id": None,
                    "sl_order_id": sl_order_id,
                    "filter_reason": "TP_FILTER_INVALID",
                }
            except BinanceAPIException as e:
                raise RuntimeError(
                    f"[{sym}] Standalone SL order failed: {e}"
                ) from e

        # ── Branch: both valid → FULLY_PROTECTED (standard OCO) ───────────────
        # OCO constraint: abovePrice > lastPrice > belowStopPrice
        if not (tp1 > current > sl):
            raise RuntimeError(
                f"OCO constraint invalid after preflight: "
                f"tp1={tp1:.4f} current={current:.4f} sl={sl:.4f}"
            )

        last_err = None
        for attempt in range(1, MAX_OCO_RETRIES + 1):
            # Re-fetch price on each retry attempt
            if attempt > 1:
                try:
                    current = float(self.client.get_symbol_ticker(symbol=sym)["price"])
                except Exception as e:
                    raise RuntimeError(
                        f"Could not fetch current price for {sym}: {e}"
                    ) from e
                # Rebuild precision strings with fresh price (TP may need adjustment)
                if current >= tp1:
                    tp1 = round_tick(current * (1 + TP_ADJUST_BUFFER), tick)
                    trade["tp1"] = tp1
                    tp_price = tp1
                    tp_str = f"{tp_price:.8f}".rstrip("0").rstrip(".")
                    print(
                        f"  ⚠  [{sym}] Retry {attempt}: TP re-adjusted to "
                        f"{tp1:.4f} (price moved to {current:.4f})"
                    )

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
                return {
                    "protection_state": "FULLY_PROTECTED",
                    "oco_resp": resp,
                    "tp_order_id": None,
                    "sl_order_id": None,
                    "filter_reason": None,
                }
            except BinanceAPIException as e:
                err_str = str(e)

                # ── Fatal: PERCENT_PRICE_BY_SIDE (-1013) ──────────────────────
                # Preflight passed but exchange still rejected — this can happen
                # when avgPrice shifts between preflight and the actual call.
                # Fail immediately (structural, not transient).
                if "PERCENT_PRICE_BY_SIDE" in err_str or (
                    "-1013" in err_str and "percent" in err_str.lower()
                ):
                    raise RuntimeError(
                        f"[{sym}] OCO rejected: SL {sl_stop_str} is outside the "
                        f"exchange price-band filter (PERCENT_PRICE_BY_SIDE). "
                        f"Current price {current:.4f}, original SL {sl:.4f} "
                        f"({(current - sl) / current * 100:.1f}% gap). "
                        f"Original error: {e}"
                    ) from e

                # ── Fatal: wrong parameter format (-1102) ──────────────────────
                if "-1102" in err_str:
                    raise RuntimeError(
                        f"[{sym}] OCO rejected: missing mandatory parameter (-1102). "
                        f"Original error: {e}"
                    ) from e

                # ── Transient price-constraint errors → retry ──────────────────
                if "price" in err_str.lower() or "-1013" in err_str or "-1021" in err_str:
                    last_err = RuntimeError(f"OCO placement failed (attempt {attempt}): {e}")
                    import time as _time; _time.sleep(2)
                    continue

                # ── All other errors: fail immediately ─────────────────────────
                raise RuntimeError(f"OCO placement failed: {e}") from e

        raise last_err or RuntimeError(
            f"OCO placement failed after {MAX_OCO_RETRIES} attempts"
        )
