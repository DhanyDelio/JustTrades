"""
binance_math.py — Pure math and API constraint helpers.
Extracted from paper_trade_executor.py for decoupling.
"""

import math

DEFAULT_MIN_NOTIONAL: float = 5.0

def get_symbol_constraints(client, symbol: str) -> dict:
    """
    Fetch lot size (min qty, step), tick size (price precision), and
    min notional from Binance exchange info.
    Returns dict with keys: min_qty, step_size, tick_size, min_notional
    """
    info = client.get_symbol_info(symbol)
    if not info:
        raise ValueError(f"Symbol {symbol} not found on testnet exchange info")

    constraints = {
        "min_qty":      0.0,
        "step_size":    0.0,
        "tick_size":    0.0,
        "min_notional": DEFAULT_MIN_NOTIONAL,
    }

    for f in info.get("filters", []):
        ft = f["filterType"]
        if ft == "LOT_SIZE":
            constraints["min_qty"]   = float(f["minQty"])
            constraints["step_size"] = float(f["stepSize"])
        elif ft == "PRICE_FILTER":
            constraints["tick_size"] = float(f["tickSize"])
        elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
            constraints["min_notional"] = float(f.get("minNotional", f.get("minVal", DEFAULT_MIN_NOTIONAL)))

    return constraints


def round_step(value: float, step: float) -> float:
    """Round value DOWN to the nearest step_size increment."""
    if step <= 0:
        return value
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)


def round_tick(value: float, tick: float) -> float:
    """Round price to nearest tick_size."""
    if tick <= 0:
        return value
    precision = max(0, round(-math.log10(tick)))
    return round(round(value / tick) * tick, precision)


def compute_position_size(
    entry_price:   float,
    sl_price:      float,
    budget_usd:    float,
    risk_fraction: float,
    constraints:   dict,
) -> dict:
    """
    Size the position so that worst-case loss (if SL is hit) equals
    risk_fraction * budget_usd.

    Returns dict with:
      qty           — base asset quantity (rounded to step_size)
      notional_usd  — entry_price × qty (position value in USD)
      max_loss_usd  — (entry - SL) × qty
      max_loss_pct  — max_loss_usd / budget_usd
      risk_per_unit — $ loss if SL hit, per unit of base asset
      warnings      — list of warning strings (empty if clean)
    """
    warnings_: list[str] = []

    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit <= 0:
        return {"qty": 0, "notional_usd": 0, "max_loss_usd": 0,
                "max_loss_pct": 0, "risk_per_unit": 0,
                "warnings": ["SL price equals entry — cannot size position"]}

    max_loss_budget = budget_usd * risk_fraction   # e.g. $3 at 25%
    ideal_qty       = max_loss_budget / risk_per_unit

    # Hard cap: position value must not exceed total budget
    # (can't spend more than you have on spot)
    max_qty_by_budget = budget_usd / entry_price
    ideal_qty = min(ideal_qty, max_qty_by_budget)

    # Round down to lot step
    step = constraints.get("step_size", 0)
    qty  = round_step(ideal_qty, step) if step > 0 else ideal_qty

    # Enforce min qty
    min_qty = constraints.get("min_qty", 0)
    if qty < min_qty:
        qty = min_qty
        warnings_.append(
            f"Qty rounded up to exchange minimum ({min_qty}) — "
            f"actual risk may exceed target"
        )

    notional_usd = entry_price * qty
    max_loss_usd = risk_per_unit * qty
    max_loss_pct = max_loss_usd / budget_usd * 100

    # Check min notional
    min_notional = constraints.get("min_notional", DEFAULT_MIN_NOTIONAL)
    if notional_usd < min_notional:
        warnings_.append(
            f"Position notional ${notional_usd:.2f} is below exchange "
            f"minimum ${min_notional:.2f} — order would be rejected"
        )

    # Check if max loss exceeds budget sanity limit (>50%)
    if max_loss_pct > 50:
        warnings_.append(
            f"Max loss {max_loss_pct:.1f}% of total budget (${max_loss_usd:.2f}) "
            f"— position too large relative to $12 account"
        )

    # Check if whole notional > budget (would require margin)
    if notional_usd > budget_usd:
        warnings_.append(
            f"Position value ${notional_usd:.2f} exceeds total budget "
            f"${budget_usd:.2f} — reduce qty or use a cheaper coin"
        )

    return {
        "qty":          qty,
        "notional_usd": notional_usd,
        "max_loss_usd": max_loss_usd,
        "max_loss_pct": max_loss_pct,
        "risk_per_unit": risk_per_unit,
        "warnings":     warnings_,
    }
