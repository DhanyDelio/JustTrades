"""
futures_order_executor.py

Object-Oriented Executor for Futures Trade Proposals.
Encapsulates order validation, position sizing, payload construction, and execution.
This class operates using the mathematical constants and validation loops
from the legacy futures_trade_executor.py.
"""

import sys

try:
    from core.futures_trade_executor import (
        get_futures_symbol_constraints,
        compute_futures_position_size,
        calculate_liquidation_price,
        compute_volatility_regime,
        get_funding_rate,
        round_step,
        round_tick,
        set_leverage_and_margin_mode,
        log_futures_trade,
        FUTURES_BUDGET_USD,
        RISK_FRACTION,
        LEVERAGE
    )
except ImportError as e:
    print(f"Error importing from core.futures_trade_executor: {e}")
    sys.exit(1)


class FuturesOrderExecutor:
    """
    Object-Oriented Executor for Futures Trade Proposals.
    """
    def __init__(self, client, dry_run: bool = False, auto_confirm: bool = False):
        self.client = client
        self.dry_run = dry_run
        self.auto_confirm = auto_confirm

    def validate_and_size(self, cand: dict) -> bool:
        """
        Validates candidate constraints, computes futures sizing, checks liquidation limits,
        and enriches with funding/regime data. Mutates cand in-place.
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

        fatal = [w for w in sizing["warnings"] if "below exchange min" in w or "exceeds budget" in w]
        if fatal or sizing["qty"] <= 0:
            print(f"  [{cand['symbol']} {cand['position_side']}] Skipped — {fatal[0] if fatal else 'qty=0'}")
            return False

        liq = calculate_liquidation_price(
            entry_price    = cand["entry_price"],
            leverage       = LEVERAGE,
            position_side  = cand["position_side"],
        )
        cand["liquidation"] = liq

        direction = cand["direction"]
        if direction == "long" and cand["sl"] <= liq["liquidation_price"]:
            print(f"  [{cand['symbol']} LONG] ⚠  SL {cand['sl']:.4f} ≤ liq {liq['liquidation_price']:.4f} — skip")
            return False
        if direction == "short" and cand["sl"] >= liq["liquidation_price"]:
            print(f"  [{cand['symbol']} SHORT] ⚠  SL {cand['sl']:.4f} ≥ liq {liq['liquidation_price']:.4f} — skip")
            return False

        cand["volatility_regime"]    = compute_volatility_regime(cand["symbol"])
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
            "symbol": sym,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty_str,
            "price": price_str,
            "positionSide": "BOTH",
        }

    def execute(self, cand: dict, correlation_cluster_id: str | None = None) -> dict:
        """Executes the futures order and logs it."""
        from binance.exceptions import BinanceAPIException

        if self.dry_run:
            print(f"\n  [DRY RUN] FuturesOrderExecutor active: skipping actual execution for {cand['symbol']}")
            payload = self.build_payload(cand)
            print(f"  [DRY RUN] Payload generated: {payload}")
            return {
                "orderId": f"DRY_FUT_{cand['symbol']}_123",
                "symbol": payload["symbol"],
                "side": payload["side"],
                "status": "NEW",
                "price": payload["price"],
                "origQty": payload["quantity"]
            }

        set_leverage_and_margin_mode(self.client, cand["symbol"])
        payload = self.build_payload(cand)
        try:
            order = self.client.futures_create_order(**payload)
        except BinanceAPIException as e:
            raise RuntimeError(f"Futures order failed: {e}") from e

        log_futures_trade(order, cand, correlation_cluster_id=correlation_cluster_id)
        return order
