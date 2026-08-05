"""
futures_math.py — Pure math helpers for Binance Futures.

Extracted from core/futures_trade_executor.py (god file) with zero logic
changes. All functions are stateless and have no side effects.

Mirrors the pattern of core/utils/binance_math.py (spot).
round_step() and round_tick() are NOT duplicated here — import them from
core/utils/binance_math.py which already owns the canonical copies.
"""

from __future__ import annotations

import math

from core.utils.binance_math import round_step, round_tick  # noqa: F401 — re-exported for callers


# ---------------------------------------------------------------------------
# Liquidation Price + Distance
# ---------------------------------------------------------------------------

def calculate_liquidation_price(
    entry_price: float,
    leverage: int,
    position_side: str,   # "LONG" or "SHORT"
    margin_mode: str = "isolated",
    mmr: float = 0.004,
) -> dict:
    """
    Calculate isolated margin liquidation price using Binance formula.

    Isolated LONG liquidation:
        liq_price = entry × (1 - 1/leverage + mmr)
        distance  = (entry - liq_price) / entry × 100

    Isolated SHORT liquidation:
        liq_price = entry × (1 + 1/leverage - mmr)
        distance  = (liq_price - entry) / entry × 100

    Returns dict: {liquidation_price, distance_to_liquidation_pct}

    At 3x leverage (default):
        LONG:  liq ≈ entry × (1 - 0.333 + 0.004) = entry × 0.671 → ~32.9% below
        SHORT: liq ≈ entry × (1 + 0.333 - 0.004) = entry × 1.329 → ~32.9% above
    """
    if position_side == "LONG":
        liq_price = entry_price * (1 - 1 / leverage + mmr)
        distance_pct = (entry_price - liq_price) / entry_price * 100
    else:  # SHORT
        liq_price = entry_price * (1 + 1 / leverage - mmr)
        distance_pct = (liq_price - entry_price) / entry_price * 100

    return {
        "liquidation_price":           round(liq_price, 8),
        "distance_to_liquidation_pct": round(distance_pct, 4),
    }


# ---------------------------------------------------------------------------
# Position Sizing
# ---------------------------------------------------------------------------

def compute_futures_position_size(
    entry_price:   float,
    sl_price:      float,
    margin_budget: float,
    risk_fraction: float,
    leverage:      int,
    constraints:   dict,
) -> dict:
    """
    Size futures position based on margin budget and risk fraction.

    Futures sizing logic:
        max_loss_usd  = margin_budget × risk_fraction
        risk_per_unit = |entry - sl| (price distance)
        ideal_qty     = max_loss_usd / risk_per_unit
        notional      = qty × entry_price
        margin_used   = notional / leverage

    Hard cap: margin_used must not exceed margin_budget.

    Returns same shape as spot's compute_position_size for display compatibility.
    """
    warnings_: list[str] = []

    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit <= 0:
        return {"qty": 0, "notional_usd": 0, "margin_used": 0,
                "max_loss_usd": 0, "max_loss_pct": 0,
                "risk_per_unit": 0, "warnings": ["SL equals entry"]}

    max_loss_budget = margin_budget * risk_fraction
    ideal_qty = max_loss_budget / risk_per_unit

    # Hard cap: margin_used = (qty × entry) / leverage ≤ margin_budget
    # → qty ≤ (margin_budget × leverage) / entry_price
    max_qty_by_margin = (margin_budget * leverage) / entry_price
    ideal_qty = min(ideal_qty, max_qty_by_margin)

    step = constraints.get("step_size", 0)
    qty  = round_step(ideal_qty, step) if step > 0 else ideal_qty

    # Enforce min qty — but only if it doesn't violate margin cap
    min_qty = constraints.get("min_qty", 0)
    if qty < min_qty:
        # Check if enforcing min_qty would blow the margin budget
        min_notional_check = min_qty * entry_price / leverage
        if min_notional_check > margin_budget * 1.05:   # 5% tolerance
            warnings_.append(
                f"Min qty {min_qty} requires margin ${min_notional_check:.2f} "
                f"> budget ${margin_budget:.2f} — position too large for this symbol at this price"
            )
            return {"qty": 0, "notional_usd": 0, "margin_used": 0,
                    "max_loss_usd": 0, "max_loss_pct": 0,
                    "risk_per_unit": risk_per_unit, "warnings": warnings_}
        qty = min_qty
        warnings_.append(
            f"Qty rounded up to exchange minimum ({min_qty}) — "
            f"actual risk may exceed target"
        )

    notional_usd = entry_price * qty
    margin_used  = notional_usd / leverage
    max_loss_usd = risk_per_unit * qty
    max_loss_pct = max_loss_usd / margin_budget * 100

    min_notional = constraints.get("min_notional", 5.0)
    if notional_usd < min_notional:
        warnings_.append(
            f"Notional ${notional_usd:.2f} below exchange min ${min_notional:.2f}"
        )

    if margin_used > margin_budget:
        warnings_.append(
            f"Margin used ${margin_used:.2f} exceeds budget ${margin_budget:.2f}"
        )

    return {
        "qty":           qty,
        "notional_usd":  notional_usd,
        "margin_used":   margin_used,
        "max_loss_usd":  max_loss_usd,
        "max_loss_pct":  max_loss_pct,
        "risk_per_unit": risk_per_unit,
        "warnings":      warnings_,
    }


# ---------------------------------------------------------------------------
# Volatility Regime
# ---------------------------------------------------------------------------

def compute_volatility_regime(symbol: str) -> str:
    """
    Classify current ATR as "low" | "medium" | "high" relative to
    the last 90 × 4h candles (~15 days of data).

    Method:
        1. Fetch 90 candles of 4h OHLCV
        2. Compute ATR(14) for each rolling window
        3. Find percentile of the LAST ATR value vs the full distribution
        4. Classify: < 33rd pct → low, > 66th pct → high, else → medium

    Thresholds (VOLATILITY_LOW_PCT=33, VOLATILITY_HIGH_PCT=66) are
    intentionally symmetric. Window of 90 candles ≈ 15 days — short
    enough to be regime-relevant, long enough to be statistically stable.

    Returns "low" | "medium" | "high"
    """
    VOLATILITY_LOW_PCT: float  = 33.0
    VOLATILITY_HIGH_PCT: float = 66.0

    try:
        import numpy as np
        import pandas as pd
        from services import chart_analyzer as ca

        df = ca.fetch_klines_api(symbol, ca.INTERVAL, limit=90)
        if len(df) < 20:
            return "unknown"

        # Compute ATR-14 for all candles
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_series = tr.rolling(14).mean().dropna()

        if len(atr_series) < 2:
            return "unknown"

        current_atr = float(atr_series.iloc[-1])
        pct_rank = float(np.sum(atr_series <= current_atr) / len(atr_series) * 100)

        if pct_rank < VOLATILITY_LOW_PCT:
            return "low"
        elif pct_rank > VOLATILITY_HIGH_PCT:
            return "high"
        else:
            return "medium"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# MAE / MFE Reconstruction (Opsi B — candle-based at exit)
# ---------------------------------------------------------------------------

def compute_mae_mfe_from_candles(
    symbol:            str,
    position_side:     str,       # "LONG" or "SHORT"
    entry_price:       float,
    entry_time_ms:     int,
    exit_time_ms:      int,
    liquidation_price: float,
) -> dict:
    """
    Reconstruct MAE, MFE, and distance_to_liq_min from 4h candles
    covering the position's lifespan. Called once at position close.

    MAE (Max Adverse Excursion): largest move against the position
    MFE (Max Favorable Excursion): largest move in the profit direction
    distance_to_liq_min: closest price got to liquidation during position

    Uses 4h candle high/low as price extremes — level of accuracy is
    appropriate for swing trades; misses intra-candle spikes but provides
    consistent, reproducible data for ML.

    Parameters:
        entry_time_ms / exit_time_ms — epoch milliseconds
    """
    try:
        import numpy as np
        import pandas as pd
        from services import chart_analyzer as ca

        # Fetch candles covering position lifespan + 1 buffer on each side
        # Use enough limit to cover any position duration
        duration_ms    = exit_time_ms - entry_time_ms
        candle_ms      = 4 * 60 * 60 * 1000   # 4h in ms
        candles_needed = max(int(duration_ms / candle_ms) + 4, 10)
        candles_needed = min(candles_needed, 500)   # API limit

        df = ca.fetch_klines_api(symbol, ca.INTERVAL, limit=candles_needed)
        if df.empty:
            return _empty_excursion()

        # Filter to candles within [entry_time, exit_time]
        entry_dt = pd.Timestamp(entry_time_ms, unit="ms", tz="UTC")
        exit_dt  = pd.Timestamp(exit_time_ms,  unit="ms", tz="UTC")
        df.index = pd.to_datetime(df.index, utc=True)
        mask = (df.index >= entry_dt) & (df.index <= exit_dt)
        position_df = df.loc[mask]

        if position_df.empty:
            return _empty_excursion()

        highs = position_df["high"].values
        lows  = position_df["low"].values

        if position_side == "LONG":
            # Adverse: price goes DOWN (against long)
            worst_price  = float(np.min(lows))
            best_price   = float(np.max(highs))
            mae_pct      = (entry_price - worst_price) / entry_price * 100
            mfe_pct      = (best_price - entry_price)  / entry_price * 100
            # Distance to liq: (price - liq) / entry; min when price is lowest
            dist_liq_min = (worst_price - liquidation_price) / entry_price * 100
        else:  # SHORT
            # Adverse: price goes UP (against short)
            worst_price  = float(np.max(highs))
            best_price   = float(np.min(lows))
            mae_pct      = (worst_price - entry_price) / entry_price * 100
            mfe_pct      = (entry_price - best_price)  / entry_price * 100
            # Distance to liq: (liq - price) / entry; min when price is highest
            dist_liq_min = (liquidation_price - worst_price) / entry_price * 100

        return {
            "max_adverse_excursion_pct":       round(max(mae_pct, 0.0), 4),
            "max_favorable_excursion_pct":     round(max(mfe_pct, 0.0), 4),
            "distance_to_liquidation_pct_min": round(dist_liq_min, 4),
        }
    except Exception:
        return _empty_excursion()


def _empty_excursion() -> dict:
    return {
        "max_adverse_excursion_pct":       None,
        "max_favorable_excursion_pct":     None,
        "distance_to_liquidation_pct_min": None,
    }
