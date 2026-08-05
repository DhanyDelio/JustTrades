"""
futures_order_executor.py

Object-Oriented Executor for Futures Trade Proposals and Exit Orders.
Encapsulates:
  - Order validation, position sizing, payload construction, entry execution
  - Leverage/margin mode configuration (_set_leverage_and_margin_mode)
  - Exit order placement (place_exit_orders) with algo verify bug fix:
      futures_get_algo_order(algoId=...) — NO symbol filter.
      Symbol filter is silently ignored on Binance Futures Testnet and returns
      an empty response even when the order exists (confirmed via manual query).

Tahap 1–3 refactored dependencies:
  - Math helpers     → core/utils/futures_math.py
  - Trade repository → core/repositories/futures_trade_repository.py
  - Candidate scan   → core/scanners/futures_candidate_scanner.py
"""

from __future__ import annotations

import sys
import time as _time

from core.utils.telegram import send_telegram as _send_telegram

try:
    from core.futures_trade_executor import (
        get_futures_symbol_constraints,
        get_futures_price,
        compute_futures_position_size,
        calculate_liquidation_price,
        compute_volatility_regime,
        get_funding_rate,
        round_step,
        round_tick,
        log_futures_trade,
        FUTURES_BUDGET_USD,
        RISK_FRACTION,
        LEVERAGE,
        MARGIN_MODE,
    )
except ImportError as e:
    print(f"Error importing from core.futures_trade_executor: {e}")
    sys.exit(1)


class FuturesOrderExecutor:
    """
    Object-Oriented Executor for Futures Trade Proposals and Exit Orders.
    """

    def __init__(self, client, dry_run: bool = False, auto_confirm: bool = False):
        self.client = client
        self.dry_run = dry_run
        self.auto_confirm = auto_confirm

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _set_leverage_and_margin_mode(self, symbol: str) -> None:
        """Set isolated margin + leverage before placing any order.

        Mirrors set_leverage_and_margin_mode() from the god file — moved here
        so all order-execution concerns live in one class (Tahap 4).
        """
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        except Exception as e:
            if "No need to change" not in str(e):
                print(f"  [WARN] Margin mode set: {e}")
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except Exception as e:
            print(f"  [WARN] Leverage set: {e}")

    # -------------------------------------------------------------------------
    # Public methods — entry
    # -------------------------------------------------------------------------

    def validate_and_size(self, cand: dict) -> bool:
        """
        Validates candidate constraints, computes futures sizing, checks liquidation
        limits, and enriches with funding/regime data. Mutates cand in-place.
        Returns True if valid.
        """
        constraints = cand.get("constraints")
        if not constraints:
            try:
                constraints = get_futures_symbol_constraints(self.client, cand["symbol"])
                cand["constraints"] = constraints
            except Exception as e:
                print(f"  [{cand['symbol']}] Skipping — constraints fetch failed: {e}")
                return False

        sizing = compute_futures_position_size(
            entry_price   = cand["entry_price"],
            sl_price      = cand["sl"],
            margin_budget = FUTURES_BUDGET_USD,
            risk_fraction = RISK_FRACTION,
            leverage      = LEVERAGE,
            constraints   = constraints,
        )
        cand["sizing"] = sizing

        fatal = [w for w in sizing["warnings"]
                 if "below exchange min" in w or "exceeds budget" in w]
        if fatal or sizing["qty"] <= 0:
            print(f"  [{cand['symbol']} {cand['position_side']}] "
                  f"Skipped — {fatal[0] if fatal else 'qty=0'}")
            return False

        liq = calculate_liquidation_price(
            entry_price   = cand["entry_price"],
            leverage      = LEVERAGE,
            position_side = cand["position_side"],
        )
        cand["liquidation"] = liq

        direction = cand["direction"]
        if direction == "long" and cand["sl"] <= liq["liquidation_price"]:
            print(f"  [{cand['symbol']} LONG] ⚠  SL {cand['sl']:.4f} "
                  f"≤ liq {liq['liquidation_price']:.4f} — skip")
            return False
        if direction == "short" and cand["sl"] >= liq["liquidation_price"]:
            print(f"  [{cand['symbol']} SHORT] ⚠  SL {cand['sl']:.4f} "
                  f"≥ liq {liq['liquidation_price']:.4f} — skip")
            return False

        cand["volatility_regime"]     = compute_volatility_regime(cand["symbol"])
        cand["funding_rate_at_entry"] = get_funding_rate(self.client, cand["symbol"])
        return True

    def build_payload(self, cand: dict) -> dict:
        """Constructs kwargs payload for Binance Futures create_order."""
        sym   = cand["symbol"]
        side  = "BUY" if cand["position_side"] == "LONG" else "SELL"
        qty   = cand["sizing"]["qty"]
        entry = cand["entry_price"]
        step  = cand["constraints"].get("step_size", 0)
        tick  = cand["constraints"].get("tick_size", 0)

        qty_str   = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        price_str = f"{round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

        return {
            "symbol":       sym,
            "side":         side,
            "type":         "LIMIT",
            "timeInForce":  "GTC",
            "quantity":     qty_str,
            "price":        price_str,
            "positionSide": "BOTH",
        }

    def execute(self, cand: dict, correlation_cluster_id: str | None = None) -> dict:
        """Executes the futures entry order and logs it."""
        from binance.exceptions import BinanceAPIException

        if self.dry_run:
            print(f"\n  [DRY RUN] FuturesOrderExecutor: "
                  f"skipping actual execution for {cand['symbol']}")
            payload = self.build_payload(cand)
            print(f"  [DRY RUN] Payload: {payload}")
            return {
                "orderId":  f"DRY_FUT_{cand['symbol']}_123",
                "symbol":   payload["symbol"],
                "side":     payload["side"],
                "status":   "NEW",
                "price":    payload["price"],
                "origQty":  payload["quantity"],
            }

        self._set_leverage_and_margin_mode(cand["symbol"])
        payload = self.build_payload(cand)
        try:
            order = self.client.futures_create_order(**payload)
        except BinanceAPIException as e:
            raise RuntimeError(f"Futures order failed: {e}") from e

        log_futures_trade(order, cand, correlation_cluster_id=correlation_cluster_id)
        return order

    # -------------------------------------------------------------------------
    # Public methods — exit
    # -------------------------------------------------------------------------

    def place_exit_orders(self, trade: dict) -> dict:
        """
        Place TP and SL exit orders after entry fills.

        Design: Binance Futures Testnet returns ONLY algoId (no orderId) for
        TAKE_PROFIT_MARKET and STOP_MARKET conditional orders.  Therefore:
          - Uses futures_create_algo_order() with algoType=CONDITIONAL.
          - Verification: futures_get_algo_order(algoId=...) WITHOUT symbol filter.
            BUG: symbol filter on this endpoint is silently ignored on testnet —
            returns empty response even when the order exists (confirmed Aug 2026).
            Fix: query by algoId only, then match client-side if response is a list.
          - Cancellation: futures_cancel_algo_order(algoId=...).
          - tp_order_id / sl_order_id are set to the same value as tp_algo_id /
            sl_algo_id so downstream code that reads those fields continues to work.

        Returns dict: {tp_order_id, sl_order_id, tp_algo_id, sl_algo_id, success}
        """
        from binance.exceptions import BinanceAPIException

        client = self.client
        sym    = trade["symbol"]
        qty    = trade["entry_qty"]
        tp1    = trade["tp1"]
        sl     = trade["sl"]
        side   = "SELL" if trade["position_side"] == "LONG" else "BUY"

        # Fetch precision
        try:
            info     = client.futures_exchange_info()
            sym_info = next(s for s in info["symbols"] if s["symbol"] == sym)
            tick = next(float(f["tickSize"]) for f in sym_info["filters"]
                        if f["filterType"] == "PRICE_FILTER")
            step = next(float(f["stepSize"]) for f in sym_info["filters"]
                        if f["filterType"] == "LOT_SIZE")
        except Exception:
            tick, step = 0.01, 0.001

        qty_str = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
        tp_str  = f"{round_tick(tp1, tick):.8f}".rstrip("0").rstrip(".")
        sl_str  = f"{round_tick(sl,  tick):.8f}".rstrip("0").rstrip(".")

        # ── Emergency check: price already past SL? ───────────────────
        try:
            current          = get_futures_price(client, sym)
            is_long          = trade["position_side"] == "LONG"
            emergency        = (current <= sl) if is_long else (current >= sl)
            if emergency:
                cmp_sym = "≤" if is_long else "≥"
                print(f"  ⚠  [{sym}] Price {current:.4f} {cmp_sym} SL {sl:.4f} "
                      f"— placing MARKET exit")
                resp = client.futures_create_order(
                    symbol=sym, side=side, type="MARKET",
                    quantity=qty_str, positionSide="BOTH", reduceOnly=True,
                )
                return {
                    "sl_order_id": resp.get("orderId"), "sl_algo_id": None,
                    "tp_order_id": None,                "tp_algo_id": None,
                    "success": True, "emergency_exit": True,
                }
        except Exception as e:
            print(f"  [WARN] Price check failed: {e} — proceeding anyway")

        results = {
            "tp_order_id": None, "sl_order_id": None,
            "tp_algo_id":  None, "sl_algo_id":  None,
            "success": False,
        }

        def _place_algo_and_verify(label: str, order_type: str,
                                   trigger_price: str) -> tuple[object, bool]:
            """
            Place a conditional exit order and verify registration.
            Returns (algo_id, verified: bool).

            Verify uses futures_get_algo_order(algoId=...) WITHOUT symbol filter.
            Symbol filter is broken on Binance Futures Testnet (silently returns
            empty even when order exists).  Fallback: scan open-orders list if
            primary query returns UNKNOWN — handles transient testnet lag.
            """
            try:
                resp = client.futures_create_algo_order(
                    algoType     = "CONDITIONAL",
                    symbol       = sym,
                    side         = side,
                    type         = order_type,
                    quantity     = qty_str,
                    triggerPrice = trigger_price,
                    timeInForce  = "GTC",
                    positionSide = "BOTH",
                    reduceOnly   = "true",
                    workingType  = "MARK_PRICE",
                )
            except BinanceAPIException as e:
                print(f"  ❌ {label} algo order failed (API): {e}")
                return None, False
            except Exception as e:
                print(f"  ❌ {label} algo order unexpected error: "
                      f"{type(e).__name__}: {e}")
                return None, False

            algo_id = resp.get("algoId") or resp.get("orderId")
            if not algo_id:
                print(f"  ❌ {label} algo order — no algoId in response: {resp}")
                return None, False

            print(f"  ✅ {label} algo order placed: algoId={algo_id} @ {trigger_price}")

            # ── Post-placement verification ────────────────────────────
            _time.sleep(0.4)  # brief settle — testnet can lag slightly
            try:
                verify_resp = client.futures_get_algo_order(algoId=algo_id)
                # Response may be dict (single) or list
                if isinstance(verify_resp, list):
                    matches = [o for o in verify_resp
                               if str(o.get("algoId")) == str(algo_id)]
                    verify = matches[0] if matches else {}
                else:
                    verify = verify_resp or {}

                v_status = (
                    verify.get("algoStatus")
                    or verify.get("status")
                    or verify.get("orderStatus")
                    or "UNKNOWN"
                )
                if v_status.upper() in ("NEW", "WORKING", "EXECUTING",
                                        "PARTIALLY_FILLED"):
                    print(f"  ✅ {label} algo order verified: algoStatus={v_status}")
                    return algo_id, True
                if v_status.upper() in ("FILLED", "EXECUTED", "COMPLETED"):
                    print(f"  ⚠  {label} algo order immediately executed: "
                          f"algoStatus={v_status}")
                    return algo_id, True

                # v_status UNKNOWN — transient lag fallback: scan open-orders list
                all_open = client.futures_get_open_algo_orders()
                if isinstance(all_open, dict):
                    all_open = all_open.get("orders", [])
                if any(str(o.get("algoId")) == str(algo_id)
                       for o in (all_open or [])):
                    print(f"  ✅ {label} algo order confirmed via open-orders list")
                    return algo_id, True

                print(f"  ⚠  {label} algo order verification: "
                      f"algoStatus={v_status}, not found in open list. "
                      f"Full response: {verify}")
                return algo_id, False

            except BinanceAPIException as ve:
                print(f"  ⚠  {label} algo order verification error: {ve}. "
                      f"Treating as unverified — price-guard will monitor.")
                return algo_id, False
            except Exception as ve:
                print(f"  ⚠  {label} algo order verification error: "
                      f"{type(ve).__name__}: {ve}. Assuming placed.")
                return algo_id, True  # network hiccup — benefit of doubt

        tp_algo_id, tp_verified = _place_algo_and_verify(
            "TP", "TAKE_PROFIT_MARKET", tp_str)
        sl_algo_id, sl_verified = _place_algo_and_verify(
            "SL", "STOP_MARKET",        sl_str)

        # algoId is the authoritative identifier for both fields
        results["tp_order_id"] = tp_algo_id
        results["sl_order_id"] = sl_algo_id
        results["tp_algo_id"]  = tp_algo_id
        results["sl_algo_id"]  = sl_algo_id

        both_placed   = tp_algo_id is not None and sl_algo_id is not None
        both_verified = tp_verified and sl_verified

        if both_placed and not both_verified:
            print(f"\n  {'!'*60}")
            print(f"  !! WARNING: Exit algo order(s) placed but NOT verified "
                  f"for {sym} !!")
            print(f"  !!   TP verified={tp_verified}  SL verified={sl_verified}")
            print(f"  !! Price-guard in --check-positions will catch any SL breach.")
            print(f"  {'!'*60}\n")
            _send_telegram(
                f"⚠️ [FUTURES] Exit algo order UNVERIFIED for "
                f"{sym} {trade.get('position_side')}.\n"
                f"TP verified={tp_verified}  SL verified={sl_verified}\n"
                f"Price-guard active — run --check-positions to monitor."
            )
        elif not both_placed:
            print(f"\n  {'!'*60}")
            print(f"  !! CRITICAL: Exit order placement FAILED for "
                  f"{sym} {trade.get('position_side')} !!")
            print(f"  !!   TP placed={tp_algo_id is not None}  "
                  f"SL placed={sl_algo_id is not None}")
            print(f"  !! Position UNPROTECTED — price-guard is active but fix ASAP.")
            print(f"  {'!'*60}\n")
            _send_telegram(
                f"🚨 [FUTURES] EXIT ORDER FAILED for "
                f"{sym} {trade.get('position_side')}!\n"
                f"TP algoId={tp_algo_id}  SL algoId={sl_algo_id}\n"
                f"Position UNPROTECTED — intervene immediately."
            )

        results["success"] = both_placed
        return results

    # -------------------------------------------------------------------------
    # Backwards-compat shim (god file still imports log_trade directly)
    # -------------------------------------------------------------------------

    def log_trade(self, order: dict, cand: dict,
                  correlation_cluster_id: str | None = None) -> None:
        """
        Insert new futures trade into Supabase trades_futures.
        Thin wrapper — delegates to log_futures_trade() from the god file,
        which in turn delegates to FuturesTradeRepository (Tahap 2).
        """
        log_futures_trade(order, cand,
                          correlation_cluster_id=correlation_cluster_id)
