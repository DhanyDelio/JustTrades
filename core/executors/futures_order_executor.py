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
import threading
import time as _time
from datetime import datetime, timezone

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

    PROTECTION_STATES = {
        "PENDING", "FULLY_PROTECTED", "SL_ONLY", "TP_ONLY",
        "UNPROTECTED", "UNKNOWN", "STALE_QTY", "POSITION_CLOSED",
    }
    _position_locks: dict[tuple[str, str], threading.RLock] = {}
    _position_locks_guard = threading.Lock()

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

        research = cand.get("research_snapshot")
        if isinstance(research, dict):
            # Timestamp the immutable research contract immediately before the
            # existing exchange-configuration/submission sequence.
            research.setdefault(
                "pre_submit_time", datetime.now(timezone.utc).isoformat())

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
        """Serialize reconciliation/mutation for one logical position."""
        key = (trade["symbol"], trade.get("position_side", "BOTH"))
        with self._position_locks_guard:
            lock = self._position_locks.setdefault(key, threading.RLock())
        with lock:
            trade["_protection_state"] = "PENDING"
            result = self._place_exit_orders_locked(trade)
            trade["_protection_state"] = result["protection_state"]
            return result

    def _place_exit_orders_locked(self, trade: dict) -> dict:
        """Reconcile then idempotently place only missing Futures exit legs."""
        from binance.exceptions import BinanceAPIException

        client = self.client
        symbol = trade["symbol"]
        side = "SELL" if trade["position_side"] == "LONG" else "BUY"
        active_statuses = {"NEW", "WORKING", "EXECUTING", "PARTIALLY_FILLED"}
        executed_statuses = {"FILLED", "EXECUTED", "COMPLETED", "FINISHED"}
        missing_codes = {-2013, -2018}

        try:
            info = client.futures_exchange_info()
            symbol_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
            tick = next(float(f["tickSize"]) for f in symbol_info["filters"]
                        if f["filterType"] == "PRICE_FILTER")
            step = next(float(f["stepSize"]) for f in symbol_info["filters"]
                        if f["filterType"] == "LOT_SIZE")
        except Exception:
            tick, step = 0.01, 0.001

        qty_str = f"{round_step(trade['entry_qty'], step):.8f}".rstrip("0").rstrip(".")
        exit_qty_str = qty_str
        tp_str = f"{round_tick(trade['tp1'], tick):.8f}".rstrip("0").rstrip(".")
        sl_str = f"{round_tick(trade['sl'], tick):.8f}".rstrip("0").rstrip(".")

        entry_id = str(trade.get("entry_order_id") or "unknown")
        exchange_position_side = trade.get("exchange_position_side") or "BOTH"

        def client_id(leg: str) -> str:
            # SDK/API contract: clientAlgoId accepts <=36 chars from this charset.
            return f"jt-{entry_id}-{leg.lower()}"[-36:]

        def status_of(order: dict) -> str:
            return str(order.get("algoStatus") or order.get("status")
                       or order.get("orderStatus") or "UNKNOWN").upper()

        def match(response, algo_id=None, stable_id=None) -> dict:
            rows = response if isinstance(response, list) else [response]
            return next((row for row in (rows or []) if row and (
                (algo_id is not None and str(row.get("algoId")) == str(algo_id))
                or (stable_id and row.get("clientAlgoId") == stable_id)
            )), {})

        def error_record(leg: str, order_type: str, trigger: str,
                         exc=None, message=None) -> dict:
            return {
                "leg": leg, "order_type": order_type, "trigger_price": trigger,
                "algo_type": "CONDITIONAL",
                "symbol": symbol, "side": side,
                "position_side": exchange_position_side,
                "quantity": exit_qty_str, "working_type": "MARK_PRICE",
                "mark_price": position_mark_price,
                "code": getattr(exc, "code", None),
                "message": message or str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        position_error = None
        position_qty = None
        position_mark_price = None
        try:
            positions = client.futures_position_information(symbol=symbol)
            if isinstance(positions, dict):
                positions = [positions]
            position = next((p for p in (positions or [])
                             if p.get("symbol") == symbol
                             and p.get("positionSide", "BOTH") == exchange_position_side), None)
            if position is None:
                raise RuntimeError(
                    f"No {symbol} position row for positionSide={exchange_position_side}")
            signed_qty = float(position.get("positionAmt", 0) or 0)
            position_mark_price = float(position.get("markPrice", 0) or 0) or None
            logical_open = ((trade["position_side"] == "LONG" and signed_qty > 0)
                            or (trade["position_side"] == "SHORT" and signed_qty < 0))
            position_qty = abs(signed_qty) if logical_open else 0.0
            if position_qty > 0:
                exit_qty_str = (
                    f"{round_step(position_qty, step):.8f}".rstrip("0").rstrip(".")
                )
        except Exception as exc:
            position_error = exc

        try:
            response = client.futures_get_open_algo_orders()
            open_orders = (response.get("orders", [])
                           if isinstance(response, dict) else (response or []))
        except Exception:
            open_orders = None

        def order_identity_matches(order: dict, leg: str) -> bool:
            cfg = legs[leg]
            order_type = order.get("orderType") or order.get("type")
            order_trigger = float(order.get("triggerPrice") or order.get("stopPrice") or 0)
            return (
                order.get("symbol") == symbol
                and order.get("positionSide", "BOTH") == exchange_position_side
                and order.get("side") == side
                and order_type == cfg["type"]
                and abs(order_trigger - float(cfg["trigger"])) <= max(tick / 2, 1e-12)
            )

        def order_qty_matches(order: dict) -> bool:
            order_qty = float(order.get("quantity") or order.get("origQty") or 0)
            return abs(order_qty - float(exit_qty_str)) <= max(step / 2, 1e-12)

        def reconcile(leg: str, persisted_id=None) -> dict:
            stable_id = client_id(leg)
            if open_orders is not None:
                identified = [order for order in open_orders if (
                    (persisted_id is not None
                     and str(order.get("algoId")) == str(persisted_id))
                    or order.get("clientAlgoId") == stable_id
                    or order_identity_matches(order, leg)
                ) and status_of(order) in active_statuses]
                if len(identified) > 1:
                    return {"state": "UNKNOWN", "id": persisted_id,
                            "executed": False,
                            "error": RuntimeError(
                                f"Multiple active {leg} orders match {symbol} "
                                f"positionSide={exchange_position_side}")}
                if identified:
                    order = identified[0]
                    if not order_identity_matches(order, leg):
                        return {"state": "UNKNOWN", "id": persisted_id,
                                "executed": False,
                                "error": RuntimeError(
                                    f"Persisted {leg} ID belongs to unexpected order")}
                    if not order_qty_matches(order):
                        return {"state": "STALE", "id": order.get("algoId") or persisted_id,
                                "executed": False,
                                "order_qty": float(order.get("quantity")
                                                   or order.get("origQty") or 0)}
                    return {"state": "EXISTS", "id": order.get("algoId") or persisted_id,
                            "executed": False,
                            "order_qty": float(order.get("quantity")
                                               or order.get("origQty") or 0)}

            lookups = ([{"algoId": persisted_id}] if persisted_id else [])
            lookups.append({"clientAlgoId": stable_id})
            missing_seen = False
            for params in lookups:
                try:
                    response = client.futures_get_algo_order(**params)
                    order = match(response, persisted_id, stable_id)
                    order_status = status_of(order)
                    if order and order_status in active_statuses:
                        if not order_identity_matches(order, leg):
                            return {"state": "UNKNOWN", "id": persisted_id,
                                    "executed": False,
                                    "error": RuntimeError(
                                        f"Queried {leg} order attributes do not match position")}
                        if not order_qty_matches(order):
                            return {"state": "STALE",
                                    "id": order.get("algoId") or persisted_id,
                                    "executed": False,
                                    "order_qty": float(order.get("quantity")
                                                       or order.get("origQty") or 0)}
                        return {"state": "EXISTS",
                                "id": order.get("algoId") or persisted_id,
                                "executed": False,
                                "order_qty": float(order.get("quantity")
                                                   or order.get("origQty") or 0)}
                    if order and order_status in executed_statuses:
                        return {"state": "TERMINAL",
                                "id": order.get("algoId") or persisted_id,
                                "executed": True}
                    if order and order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                        missing_seen = True
                        continue
                    # A successful but empty/malformed response is uncertain.
                    return {"state": "UNKNOWN", "id": persisted_id,
                            "executed": False}
                except BinanceAPIException as exc:
                    if getattr(exc, "code", None) in missing_codes:
                        missing_seen = True
                        continue
                    return {"state": "UNKNOWN", "id": persisted_id,
                            "executed": False, "error": exc}
                except Exception as exc:
                    return {"state": "UNKNOWN", "id": persisted_id,
                            "executed": False, "error": exc}
            if missing_seen or open_orders is not None:
                return {"state": "MISSING", "id": persisted_id, "executed": False}
            return {"state": "UNKNOWN", "id": persisted_id, "executed": False}

        legs = {
            "TP": {"type": "TAKE_PROFIT_MARKET", "trigger": tp_str,
                   "id": trade.get("tp_algo_id") or trade.get("tp_order_id")},
            "SL": {"type": "STOP_MARKET", "trigger": sl_str,
                   "id": trade.get("sl_algo_id") or trade.get("sl_order_id")},
        }
        state = {leg: reconcile(leg, cfg["id"]) for leg, cfg in legs.items()}
        errors = []
        creation_attempted = 0
        previous_meta = ((trade.get("raw_entry_order") or {})
                         .get("exit_protection") or {})
        previous_unknown_cycles = int(
            previous_meta.get("unknown_protection_cycles", 0) or 0)

        def finish(protection_state: str, terminal_seen=False) -> dict:
            unknown_cycles = (previous_unknown_cycles + 1
                              if protection_state == "UNKNOWN" else 0)
            result = {
                "tp_order_id": state["TP"]["id"],
                "sl_order_id": state["SL"]["id"],
                "tp_algo_id": state["TP"]["id"],
                "sl_algo_id": state["SL"]["id"],
                "success": protection_state == "FULLY_PROTECTED",
                "protection_state": protection_state,
                "errors": errors,
                "terminal_order_seen": terminal_seen,
                "creation_attempted": creation_attempted,
                "unknown_protection_cycles": unknown_cycles,
                "position_qty": position_qty,
                "mark_price": position_mark_price,
                "tp_order_qty": state["TP"].get("order_qty"),
                "sl_order_qty": state["SL"].get("order_qty"),
            }
            self._alert_protection_state(trade, result)
            return result

        if position_error is not None:
            errors.append(error_record(
                "POSITION", "POSITION_QUERY", qty_str, exc=position_error))
            return finish("UNKNOWN")

        if any(value["state"] == "TERMINAL" for value in state.values()):
            # Do not replace a just-executed leg. Let the monitor resolve its fill.
            return finish("UNKNOWN", terminal_seen=True)

        if position_qty == 0:
            return finish("POSITION_CLOSED")

        if any(value["state"] == "STALE" for value in state.values()):
            return finish("STALE_QTY")

        if any(value["state"] == "UNKNOWN" for value in state.values()):
            for leg, value in state.items():
                if value.get("error"):
                    cfg = legs[leg]
                    errors.append(error_record(
                        leg, cfg["type"], cfg["trigger"], exc=value["error"]))
            return finish("UNKNOWN")

        # Preserve the existing emergency exit policy, but only after exchange
        # reconciliation and only when no active SL already protects downside.
        if state["SL"]["state"] == "MISSING":
            try:
                current = get_futures_price(client, symbol)
                is_long = trade["position_side"] == "LONG"
                breached = ((current <= trade["sl"]) if is_long
                            else (current >= trade["sl"]))
                if breached:
                    response = client.futures_create_order(
                        symbol=symbol, side=side, type="MARKET",
                        quantity=exit_qty_str, positionSide=exchange_position_side,
                        reduceOnly=True,
                    )
                    return {
                        "sl_order_id": response.get("orderId"), "sl_algo_id": None,
                        "tp_order_id": state["TP"]["id"],
                        "tp_algo_id": state["TP"]["id"],
                        "success": True, "emergency_exit": True,
                        "exit_reason": "EMERGENCY_UNPROTECTED",
                        "protection_state": "POSITION_CLOSED", "errors": [],
                        "terminal_order_seen": True, "creation_attempted": 1,
                        "unknown_protection_cycles": 0,
                        "position_qty": position_qty,
                    }
            except Exception as exc:
                print(f"  [WARN] Price check failed: {exc} — proceeding with exits")

        terminal_seen = any(value["executed"] for value in state.values())

        def place(leg: str) -> None:
            nonlocal creation_attempted
            cfg = legs[leg]
            creation_attempted += 1
            try:
                response = client.futures_create_algo_order(
                    algoType="CONDITIONAL", symbol=symbol, side=side,
                    type=cfg["type"], quantity=exit_qty_str,
                    triggerPrice=cfg["trigger"], positionSide=exchange_position_side,
                    reduceOnly="true", workingType="MARK_PRICE",
                    clientAlgoId=client_id(leg),
                )
            except Exception as exc:
                check = reconcile(leg)
                state[leg] = check
                if check["state"] != "EXISTS":
                    errors.append(error_record(
                        leg, cfg["type"], cfg["trigger"], exc=exc))
                return

            algo_id = response.get("algoId") or response.get("orderId")
            if not algo_id:
                state[leg] = reconcile(leg)
                if state[leg]["state"] != "EXISTS":
                    errors.append(error_record(
                        leg, cfg["type"], cfg["trigger"],
                        message=f"No algoId/orderId in response "
                                f"(code={response.get('code')}, msg={response.get('msg')})"))
                return

            _time.sleep(0.4)
            state[leg] = reconcile(leg, algo_id)
            if state[leg]["state"] != "EXISTS":
                errors.append(error_record(
                    leg, cfg["type"], cfg["trigger"],
                    message=f"Created algoId={algo_id}; verification={state[leg]['state']}"))

        # Never replace an order while a terminal leg awaits monitor resolution.
        if not terminal_seen:
            for leg in ("SL", "TP"):
                if state[leg]["state"] == "MISSING":
                    place(leg)
                    if state[leg]["state"] == "UNKNOWN":
                        break

        if any(value["state"] == "UNKNOWN" for value in state.values()):
            return finish("UNKNOWN", terminal_seen)
        tp_exists = state["TP"]["state"] == "EXISTS"
        sl_exists = state["SL"]["state"] == "EXISTS"
        if tp_exists and sl_exists:
            protection_state = "FULLY_PROTECTED"
        elif sl_exists:
            protection_state = "SL_ONLY"
        elif tp_exists:
            protection_state = "TP_ONLY"
        else:
            protection_state = "UNPROTECTED"
        return finish(protection_state, terminal_seen)

    @staticmethod
    def _alert_protection_state(trade: dict, result: dict) -> None:
        """Alert using the verified protection state, including safe error detail."""
        state = result["protection_state"]
        if state in {"FULLY_PROTECTED", "PENDING"}:
            return
        symbol = trade["symbol"]
        side = trade.get("position_side", "LONG")
        messages = {
            "SL_ONLY": (f"⚠️ [FUTURES] {symbol} {side} PARTIALLY PROTECTED\n"
                        "Stop Loss: ACTIVE\nTake Profit: MISSING\n"
                        "Position downside remains protected.\n"
                        "TP retry/reconciliation required."),
            "TP_ONLY": (f"🚨 [FUTURES] {symbol} {side} CRITICAL\n"
                        "Take Profit: ACTIVE\nStop Loss: MISSING\n"
                        "No downside protection."),
            "UNPROTECTED": (f"🚨 [FUTURES] {symbol} {side} UNPROTECTED\n"
                            "Take Profit: MISSING\nStop Loss: MISSING"),
            "UNKNOWN": (("🚨" if result.get("unknown_protection_cycles", 0) >= 3
                         else "⚠️")
                        + f" [FUTURES] {symbol} {side} PROTECTION STATE UNKNOWN\n"
                        "Exchange reconciliation failed.\n"
                        + ("No new exit orders were created to avoid duplicates."
                           if not result.get("creation_attempted") else
                           "No further exit orders will be created until reconciliation succeeds.")
                        + f"\nConsecutive unknown cycles: "
                          f"{result.get('unknown_protection_cycles', 1)}"),
            "STALE_QTY": (
                f"⚠️ [FUTURES] {symbol} {side} EXIT ORDER QUANTITY MISMATCH\n"
                f"Current position quantity: {result.get('position_qty')}\n"
                f"TP protected quantity: {result.get('tp_order_qty')}\n"
                f"SL protected quantity: {result.get('sl_order_qty')}"
            ),
            "POSITION_CLOSED": (
                f"⚠️ [FUTURES] {symbol} {side} POSITION ABSENT ON EXCHANGE\n"
                "No exit orders were created.\nDB reconciliation required."
            ),
        }
        details = result.get("errors") or []
        reason = ""
        if details:
            error = details[-1]
            code = (f" code={error['code']}"
                    if error.get("code") is not None else "")
            reason = (f"\n{error['leg']} {error['order_type']} failed:{code} "
                      f"{error['message']}\nTrigger: {error['trigger_price']}"
                      + (f"\nMark price: {error['mark_price']}"
                         if error.get("mark_price") is not None else ""))
        _send_telegram(messages[state] + reason)

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
