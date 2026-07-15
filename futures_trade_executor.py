"""
futures_trade_executor.py — Paper Trading on Binance Futures Testnet
=====================================================================
READ-ONLY BY DEFAULT. No order placed without explicit 'y' confirmation.

Separate from paper_trade_executor.py (spot) — trade log, stats, and
effective-n are tracked independently. Zero modifications to existing files.

Supports LONG and SHORT (futures allows both from USDT margin account).
Zone detection logic reused from chart_analyzer.py — no duplication.

Usage:
    python3 futures_trade_executor.py --propose
    python3 futures_trade_executor.py --check-positions
    python3 futures_trade_executor.py --stats-futures

API keys: Binance Futures Testnet (different from Spot Testnet)
    Register at https://testnet.binancefuture.com
    Set BINANCE_FUTURES_TESTNET_API_KEY and BINANCE_FUTURES_TESTNET_API_SECRET in .env
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import io
import contextlib
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Reuse chart_analyzer's analysis engine — zero duplication
sys.path.insert(0, str(Path(__file__).parent))
import chart_analyzer as ca

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Per-trade margin budget (mirrors spot's PER_TRADE_BUDGET)
FUTURES_BUDGET_USD: float = 12.00

# Leverage — fixed 3x isolated margin
# At 3x: liquidation distance ≈ 33% from entry (well above avg SL ~2-3%)
LEVERAGE: int = 3
MARGIN_MODE: str = "isolated"

# Risk fraction of margin per trade (same as spot)
RISK_FRACTION: float = 0.25

# Maintenance margin rate for isolated USDT-M futures (Binance default tier 1)
# Used for liquidation price calculation. Actual value varies by notional size,
# but 0.4% is correct for most positions under $50k notional.
MAINTENANCE_MARGIN_RATE: float = 0.004  # 0.4%

# Taker fee for futures (Binance Futures Testnet default)
TAKER_FEE_PCT: float = 0.0004  # 0.04% taker

# Minimum R:R to accept a candidate
MIN_RR: float = 1.5

# How many symbols to scan
DEFAULT_SCAN_N: int = 30

# Trade log — completely separate from spot's trade_log.json
FUTURES_LOG_PATH = Path("./trade_futures.json")

# Rule version — bump when parameters change
RULE_VERSION: str = "fv1.0.0"

# Volatility regime thresholds (ATR percentile vs 90-candle rolling window)
# ATR percentile computed over last 90 × 4h candles (~15 days)
VOLATILITY_LOW_PCT: float    = 33.0   # below 33rd percentile → "low"
VOLATILITY_HIGH_PCT: float   = 66.0   # above 66th percentile → "high"
# between 33–66 → "medium"

# Zone entry buffer (same logic as spot)
ZONE_ENTRY_BUFFER_PCT: float = 0.0015  # 0.15%

# Hard cap on concurrent open futures positions
MAX_CONCURRENT_POSITIONS: int = 10


# ---------------------------------------------------------------------------
# 1. FUTURES TESTNET CLIENT
# ---------------------------------------------------------------------------

def get_futures_client():
    """
    Connect to Binance Futures Testnet.
    Keys are SEPARATE from Spot Testnet — register at testnet.binancefuture.com
    """
    try:
        from binance.client import Client
    except ImportError:
        raise ImportError("pip install python-binance --break-system-packages")

    api_key    = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError(
            "Futures API keys not found in .env\n"
            "Set BINANCE_FUTURES_TESTNET_API_KEY and BINANCE_FUTURES_TESTNET_API_SECRET\n"
            "Register at https://testnet.binancefuture.com"
        )

    # python-binance uses testnet_futures=True for futures testnet
    client = Client(api_key, api_secret, testnet=True, tld="com")
    # Override base URL to futures testnet
    client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    return client


def _futures_get(client, path: str, params: dict | None = None) -> dict | list:
    """Direct GET to futures testnet endpoint (for endpoints not in python-binance)."""
    base = "https://testnet.binancefuture.com/fapi/v1"
    try:
        resp = requests.get(f"{base}{path}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Futures API GET {path} failed: {e}") from e


def ping_futures(client) -> bool:
    """Verify futures testnet connectivity."""
    try:
        client.futures_ping()
        return True
    except Exception:
        # Fallback: direct ping
        try:
            resp = requests.get(
                "https://testnet.binancefuture.com/fapi/v1/ping", timeout=5
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 2. FUTURES MARKET INFO
# ---------------------------------------------------------------------------

def get_futures_symbol_constraints(client, symbol: str) -> dict:
    """
    Fetch futures-specific constraints: tick size, step size, min notional.
    Uses futures_exchange_info() — different from spot exchange_info.
    """
    try:
        info = client.futures_exchange_info()
    except Exception:
        # Fallback to direct REST
        info = requests.get(
            "https://testnet.binancefuture.com/fapi/v1/exchangeInfo", timeout=10
        ).json()

    for sym_info in info.get("symbols", []):
        if sym_info["symbol"] != symbol:
            continue
        constraints = {
            "min_qty":      0.0,
            "step_size":    0.0,
            "tick_size":    0.0,
            "min_notional": 5.0,
        }
        for f in sym_info.get("filters", []):
            ft = f["filterType"]
            if ft == "LOT_SIZE":
                constraints["min_qty"]   = float(f["minQty"])
                constraints["step_size"] = float(f["stepSize"])
            elif ft == "PRICE_FILTER":
                constraints["tick_size"] = float(f["tickSize"])
            elif ft == "MIN_NOTIONAL":
                constraints["min_notional"] = float(f.get("notional", 5.0))
        return constraints

    raise ValueError(f"Symbol {symbol} not found in futures exchange info")


def get_futures_price(client, symbol: str) -> float:
    """Fetch current mark price from futures testnet."""
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])
    except Exception:
        resp = requests.get(
            "https://testnet.binancefuture.com/fapi/v1/ticker/price",
            params={"symbol": symbol}, timeout=5
        ).json()
        return float(resp["price"])


def get_funding_rate(client, symbol: str) -> float | None:
    """
    Fetch current funding rate for symbol.
    Returns float (e.g. 0.0001 = 0.01%) or None if unavailable.
    """
    try:
        data = client.futures_funding_rate(symbol=symbol, limit=1)
        if data:
            return float(data[-1]["fundingRate"])
    except Exception:
        try:
            resp = requests.get(
                "https://testnet.binancefuture.com/fapi/v1/fundingRate",
                params={"symbol": symbol, "limit": 1}, timeout=5
            ).json()
            if resp:
                return float(resp[-1]["fundingRate"])
        except Exception:
            pass
    return None


def accrue_funding(client, trade: dict) -> bool:
    """
    Fetch funding payment events since last_funding_check_time and accumulate
    into trade["funding_rate_paid"] and trade["funding_rate_history"].

    Funding is charged every 8 hours (at 00:00, 08:00, 16:00 UTC).
    - LONG pays when rate > 0, receives when rate < 0
    - SHORT pays when rate < 0 (i.e. abs value), receives when rate > 0
    Both are represented as signed cost: positive = paid, negative = received.

    Returns True if the trade dict was modified (caller should set log_dirty).

    De-duplication: events already in funding_rate_history (matched by
    fundingTime) are skipped — safe to call every --check-positions run.
    """
    sym  = trade.get("symbol")
    side = trade.get("position_side", "LONG")   # "LONG" | "SHORT"
    qty  = trade.get("entry_qty", 0)

    # Use entry_fill_time as the lower bound on first call,
    # then last_funding_check_time on subsequent calls
    start_ms = trade.get("last_funding_check_time") or trade.get("entry_fill_time")
    if not start_ms or not sym or not qty:
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Fetch funding rate events in [start_ms, now_ms]
    events = []
    try:
        events = client.futures_funding_rate(
            symbol    = sym,
            startTime = int(start_ms),
            endTime   = now_ms,
            limit     = 100,
        )
    except Exception:
        try:
            resp = requests.get(
                "https://testnet.binancefuture.com/fapi/v1/fundingRate",
                params={
                    "symbol":    sym,
                    "startTime": int(start_ms),
                    "endTime":   now_ms,
                    "limit":     100,
                },
                timeout=8,
            )
            events = resp.json() if resp.status_code == 200 else []
        except Exception:
            return False

    if not events:
        # No new events — still update check time
        trade["last_funding_check_time"] = now_ms
        return True

    # De-duplicate against already-recorded history
    existing_times = {
        e["fundingTime"] for e in trade.get("funding_rate_history", [])
        if isinstance(e, dict) and "fundingTime" in e
    }

    new_events = [e for e in events if int(e.get("fundingTime", 0)) not in existing_times]
    if not new_events:
        trade["last_funding_check_time"] = now_ms
        return True

    total_cost = 0.0
    history    = trade.get("funding_rate_history", [])

    for event in new_events:
        rate        = float(event.get("fundingRate", 0))
        funding_time = int(event.get("fundingTime", 0))
        mark_price  = float(event.get("markPrice") or 0)
        notional    = (mark_price * qty) if mark_price > 0 else (trade.get("entry_notional", 0))

        # Cost sign convention:
        #   LONG:  pays when rate > 0, receives when rate < 0  → cost = +rate × notional
        #   SHORT: pays when rate < 0, receives when rate > 0  → cost = -rate × notional
        if side == "LONG":
            cost = rate * notional
        else:
            cost = -rate * notional

        total_cost += cost
        history.append({
            "fundingTime": funding_time,
            "fundingRate": rate,
            "markPrice":   mark_price,
            "notional":    round(notional, 4),
            "cost_usd":    round(cost, 6),
            "side":        side,
        })

    trade["funding_rate_paid"]      = round(
        (trade.get("funding_rate_paid") or 0.0) + total_cost, 6
    )
    trade["funding_rate_history"]   = history
    trade["last_funding_check_time"] = now_ms
    return True


# ---------------------------------------------------------------------------
# 3. PRECISION HELPERS (mirrors spot — no import to avoid coupling)
# ---------------------------------------------------------------------------

def round_step(value: float, step: float) -> float:
    """Round value DOWN to nearest step_size."""
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


# ---------------------------------------------------------------------------
# 4. LIQUIDATION PRICE + POSITION SIZING
# ---------------------------------------------------------------------------

def calculate_liquidation_price(
    entry_price: float,
    leverage: int,
    position_side: str,   # "LONG" or "SHORT"
    margin_mode: str = MARGIN_MODE,
    mmr: float = MAINTENANCE_MARGIN_RATE,
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
        "liquidation_price":          round(liq_price, 8),
        "distance_to_liquidation_pct": round(distance_pct, 4),
    }


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
        "qty":          qty,
        "notional_usd": notional_usd,
        "margin_used":  margin_used,
        "max_loss_usd": max_loss_usd,
        "max_loss_pct": max_loss_pct,
        "risk_per_unit": risk_per_unit,
        "warnings":     warnings_,
    }


# ---------------------------------------------------------------------------
# 5. VOLATILITY REGIME
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
    try:
        import numpy as np
        df = ca.fetch_klines_api(symbol, ca.INTERVAL, limit=90)
        if len(df) < 20:
            return "unknown"

        # Compute ATR-14 for all candles
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        import pandas as pd
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
# 6. MAE / MFE RECONSTRUCTION (Opsi B — candle-based at exit)
# ---------------------------------------------------------------------------

def compute_mae_mfe_from_candles(
    symbol:         str,
    position_side:  str,       # "LONG" or "SHORT"
    entry_price:    float,
    entry_time_ms:  int,
    exit_time_ms:   int,
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
        import pandas as pd
        import numpy as np

        # Fetch candles covering position lifespan + 1 buffer on each side
        # Use enough limit to cover any position duration
        duration_ms   = exit_time_ms - entry_time_ms
        candle_ms     = 4 * 60 * 60 * 1000   # 4h in ms
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
            worst_price    = float(np.min(lows))
            best_price     = float(np.max(highs))
            mae_pct        = (entry_price - worst_price) / entry_price * 100
            mfe_pct        = (best_price - entry_price)  / entry_price * 100
            # Distance to liq: (price - liq) / entry; min when price is lowest
            dist_liq_min   = (worst_price - liquidation_price) / entry_price * 100
        else:  # SHORT
            # Adverse: price goes UP (against short)
            worst_price    = float(np.max(highs))
            best_price     = float(np.min(lows))
            mae_pct        = (worst_price - entry_price) / entry_price * 100
            mfe_pct        = (entry_price - best_price)  / entry_price * 100
            # Distance to liq: (liq - price) / entry; min when price is highest
            dist_liq_min   = (liquidation_price - worst_price) / entry_price * 100

        return {
            "max_adverse_excursion_pct":      round(max(mae_pct, 0.0), 4),
            "max_favorable_excursion_pct":    round(max(mfe_pct, 0.0), 4),
            "distance_to_liquidation_pct_min": round(dist_liq_min, 4),
        }
    except Exception as e:
        return _empty_excursion()


def _empty_excursion() -> dict:
    return {
        "max_adverse_excursion_pct":      None,
        "max_favorable_excursion_pct":    None,
        "distance_to_liquidation_pct_min": None,
    }


# ---------------------------------------------------------------------------
# 7. TRADE LOG (futures — separate file from spot)
# ---------------------------------------------------------------------------

def load_futures_log() -> list[dict]:
    """Load all futures trades from Supabase trades_futures table.
    trade_futures.json is kept as a backup but is no longer the source of truth.
    """
    try:
        from supabase_client import fetch_all_futures
        return fetch_all_futures()
    except Exception as e:
        print(f"  [WARN] Supabase read failed, falling back to trade_futures.json: {e}")
        if FUTURES_LOG_PATH.exists():
            with open(FUTURES_LOG_PATH) as f:
                return json.load(f)
        return []


def save_futures_log(trades: list[dict]) -> None:
    # trade_futures.json is no longer the write target — Supabase is.
    # Writes are handled per-record via upsert_futures() / update_futures_by_order_id()
    # in supabase_client.py.  This stub is kept so call sites compile without change
    # until each write path is individually migrated to Supabase upserts.
    pass


def log_futures_trade(order: dict, cand: dict,
                      correlation_cluster_id: str | None = None) -> None:
    """Insert new futures trade into Supabase trades_futures table."""
    from supabase_client import upsert_futures
    sizing  = cand["sizing"]
    liq     = cand["liquidation"]
    ez      = cand.get("entry_zone") or {}

    record = {
        # ── Identity ──────────────────────────────────────────────────
        "symbol":               cand["symbol"],
        "position_side":        cand["position_side"],
        "direction":            cand["direction"],
        "margin_budget":        FUTURES_BUDGET_USD,
        "leverage":             LEVERAGE,
        "margin_mode":          MARGIN_MODE,
        "rule_version":         RULE_VERSION,
        "correlation_cluster_id": correlation_cluster_id,

        # ── Entry order ───────────────────────────────────────────────
        "entry_order_id":       order.get("orderId"),
        "entry_client_id":      order.get("clientOrderId"),
        "entry_status":         order.get("status", "NEW"),
        "entry_price":          cand["entry_price"],
        "entry_fill_price":     None,
        "entry_fill_time":      None,
        "entry_qty":            sizing["qty"],
        "entry_notional":       sizing["notional_usd"],
        "margin_used":          sizing["margin_used"],
        "open_time":            datetime.now(timezone.utc).isoformat(),

        # ── Exit orders ───────────────────────────────────────────────
        "tp_order_id":          None,
        "sl_order_id":          None,
        "tp_algo_id":           None,
        "sl_algo_id":           None,
        "exit_orders_placed":   False,

        # ── Levels ────────────────────────────────────────────────────
        "sl":                   cand["sl"],
        "tp1":                  cand["tp1"],
        "tp2":                  cand.get("tp2"),
        "entry_zone_center":    ez.get("center"),
        "entry_zone_touches":   ez.get("touches"),

        # ── Liquidation ───────────────────────────────────────────────
        "liquidation_price":              liq["liquidation_price"],
        "distance_to_liquidation_pct":    liq["distance_to_liquidation_pct"],

        # ── Setup metadata ────────────────────────────────────────────
        "planned_rr":           cand["rr"],
        "risk_pct":             cand["risk_pct"],
        "max_loss_usd":         sizing["max_loss_usd"],
        "zone_type":            cand.get("tier_used", "T1"),
        "zone_touches":         ez.get("touches"),
        "atr_pct_at_entry":     cand["atr_pct"],

        # ── Volatility regime at entry ────────────────────────────────
        "volatility_regime_at_entry":  cand.get("volatility_regime", "unknown"),

        # ── Funding rate at entry (snapshot) ─────────────────────────
        "funding_rate_at_entry":  cand.get("funding_rate_at_entry"),

        # ── Cost estimates ────────────────────────────────────────────
        "fee_usd_roundtrip":   round(sizing["notional_usd"] * TAKER_FEE_PCT * 2, 4),
        "slippage_pct":        None,

        # ── Exit ──────────────────────────────────────────────────────
        "exit_status":         "OPEN",
        "exit_price":          None,
        "exit_time":           None,
        "realized_pnl_usd":    None,
        "realized_pnl_pct":    None,
        "time_in_position_sec": None,

        # ── ML features ───────────────────────────────────────────────
        "max_adverse_excursion_pct":       None,
        "max_favorable_excursion_pct":     None,
        "distance_to_liquidation_pct_min": None,
        "funding_rate_paid":               0.0,
        "funding_rate_history":            [],
        "last_funding_check_time":         None,

        # ── Raw ───────────────────────────────────────────────────────
        "raw_entry_order":     order,
    }
    upsert_futures(record)
    print(f"  Futures trade inserted into Supabase trades_futures (order #{record['entry_order_id']})")


# ---------------------------------------------------------------------------
# 8. CANDIDATE SELECTION (reuses chart_analyzer, supports LONG + SHORT)
# ---------------------------------------------------------------------------

def gather_futures_candidates(scan_n: int = DEFAULT_SCAN_N) -> list[dict]:
    """
    Scan top scan_n symbols. For futures, evaluate BOTH long and short setups.
    Returns candidates sorted by risk% ASC.
    """
    print(f"\nScanning top {scan_n} symbols for futures setups (LONG + SHORT)...")
    symbols = ca.get_top_symbols_by_volume(scan_n)
    print(f"Symbols: {symbols}\n")

    candidates: list[dict] = []

    for sym in symbols:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = ca.analyze_symbol(sym, save_chart=False)
        if result is None:
            continue

        current_price = result["current_price"]
        atr           = result["atr"]
        atr_pct       = result["atr_pct"]

        for direction in ("long", "short"):
            setup = result["sl_tp"].get(direction, {})

            if not setup.get("rr_clears"):
                continue
            if setup.get("no_tp_in_range"):
                continue
            if setup.get("tier_used") not in ("T1", "T2"):
                continue

            sl      = setup.get("sl")
            tp_list = setup.get("tp", [])
            tp1     = tp_list[0] if tp_list else None
            tp2     = tp_list[1] if len(tp_list) > 1 else None
            rr      = setup.get("rr")
            risk_pct = setup.get("risk_pct")

            if not sl or not tp1 or not rr or not risk_pct:
                continue

            # Safety check for direction consistency
            if direction == "long" and not (sl < current_price < tp1):
                continue
            if direction == "short" and not (tp1 < current_price < sl):
                continue

            candidates.append({
                "symbol":        sym,
                "direction":     direction,
                "position_side": "LONG" if direction == "long" else "SHORT",
                "current_price": current_price,
                "entry_price":   current_price,   # refined in pick_best
                "sl":            sl,
                "tp1":           tp1,
                "tp2":           tp2,
                "rr":            rr,
                "risk_pct":      risk_pct,
                "atr":           atr,
                "atr_pct":       atr_pct,
                "tier_used":     setup.get("tier_used", "T1"),
                "support_zones":    result.get("support_zones", []),
                "resistance_zones": result.get("resistance_zones", []),
            })

    candidates.sort(key=lambda c: (c["risk_pct"], -c["rr"]))
    print(f"Found {len(candidates)} futures candidates (LONG+SHORT combined).")
    return candidates


def pick_best_futures_candidate(
    candidates: list[dict],
    client,
    symbol_filter: str | None = None,
    side_filter:   str | None = None,   # "LONG" | "SHORT" | None
) -> dict | None:
    """
    Select best candidate passing exchange constraints + liquidation sanity check.
    Optionally filter by symbol or side.
    """
    pool = candidates
    if symbol_filter:
        pool = [c for c in pool if c["symbol"] == symbol_filter.upper()]
    if side_filter:
        pool = [c for c in pool if c["position_side"] == side_filter.upper()]
    if not pool:
        return None

    for cand in pool:
        sym       = cand["symbol"]
        direction = cand["direction"]

        try:
            constraints = get_futures_symbol_constraints(client, sym)
        except Exception as e:
            print(f"  [{sym}] Skipping — constraints fetch failed: {e}")
            continue

        # Anchor entry to zone (same logic as spot)
        if direction == "long":
            sup_zones = cand.get("support_zones", [])
            atr_v     = cand["atr"]
            cur       = cand["current_price"]
            min_dist  = 0.5 * atr_v
            qualified = [z for z in sup_zones
                         if z["touches"] >= 2 and (cur - z["center"]) >= min_dist]
            zone = (min(qualified, key=lambda z: cur - z["center"])
                    if qualified else
                    max(sup_zones, key=lambda z: z["touches"]) if sup_zones else None)

            zone_center = zone["center"] if zone else cur
            zone_low    = zone["low"]    if zone else cur
            cand["entry_zone"] = zone

            entry = round_tick(
                zone_center * (1 + ZONE_ENTRY_BUFFER_PCT),
                constraints.get("tick_size", 0)
            )
            recalc_sl = round_tick(
                zone_low - ca.SL_ATR_BUFFER * atr_v,
                constraints.get("tick_size", 0)
            )
            cand["sl"]       = recalc_sl
            cand["risk_pct"] = (entry - recalc_sl) / entry * 100 if entry > 0 else 0

        elif direction == "short":
            res_zones = cand.get("resistance_zones", [])
            atr_v     = cand["atr"]
            cur       = cand["current_price"]
            min_dist  = 0.5 * atr_v
            qualified = [z for z in res_zones
                         if z["touches"] >= 2 and (z["center"] - cur) >= min_dist]
            zone = (min(qualified, key=lambda z: z["center"] - cur)
                    if qualified else
                    max(res_zones, key=lambda z: z["touches"]) if res_zones else None)

            zone_center = zone["center"] if zone else cur
            zone_high   = zone["high"]   if zone else cur
            cand["entry_zone"] = zone

            entry = round_tick(
                zone_center * (1 - ZONE_ENTRY_BUFFER_PCT),
                constraints.get("tick_size", 0)
            )
            recalc_sl = round_tick(
                zone_high + ca.SL_ATR_BUFFER * atr_v,
                constraints.get("tick_size", 0)
            )
            cand["sl"]       = recalc_sl
            cand["risk_pct"] = (recalc_sl - entry) / entry * 100 if entry > 0 else 0

        cand["entry_price"] = entry

        # Safety: SL/entry/TP direction check
        if direction == "long" and not (cand["sl"] < entry < cand["tp1"]):
            print(f"  [{sym} LONG] ⛔ Safety check failed — sl/entry/tp1 invalid. Skip.")
            continue
        if direction == "short" and not (cand["tp1"] < entry < cand["sl"]):
            print(f"  [{sym} SHORT] ⛔ Safety check failed — tp1/entry/sl invalid. Skip.")
            continue

        # Position sizing
        sizing = compute_futures_position_size(
            entry_price   = entry,
            sl_price      = cand["sl"],
            margin_budget = FUTURES_BUDGET_USD,
            risk_fraction = RISK_FRACTION,
            leverage      = LEVERAGE,
            constraints   = constraints,
        )
        cand["sizing"]      = sizing
        cand["constraints"] = constraints

        fatal = [w for w in sizing["warnings"]
                 if "below exchange min" in w or "exceeds budget" in w]
        if fatal or sizing["qty"] <= 0:
            print(f"  [{sym} {direction.upper()}] Skipped — {fatal[0] if fatal else 'qty=0'}")
            continue

        # Liquidation price
        liq = calculate_liquidation_price(
            entry_price    = entry,
            leverage       = LEVERAGE,
            position_side  = cand["position_side"],
        )
        cand["liquidation"] = liq

        # Sanity: SL must be hit BEFORE liquidation
        if direction == "long" and cand["sl"] <= liq["liquidation_price"]:
            print(f"  [{sym} LONG] ⚠  SL {cand['sl']:.4f} ≤ liq {liq['liquidation_price']:.4f} — skip")
            continue
        if direction == "short" and cand["sl"] >= liq["liquidation_price"]:
            print(f"  [{sym} SHORT] ⚠  SL {cand['sl']:.4f} ≥ liq {liq['liquidation_price']:.4f} — skip")
            continue

        # Fetch volatility regime and funding rate (pre-entry enrichment)
        cand["volatility_regime"]    = compute_volatility_regime(sym)
        cand["funding_rate_at_entry"] = get_funding_rate(client, sym)

        return cand

    return None


# ---------------------------------------------------------------------------
# 9. ORDER EXECUTION
# ---------------------------------------------------------------------------

def set_leverage_and_margin_mode(client, symbol: str) -> None:
    """Set isolated margin + leverage before placing any order."""
    try:
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
    except Exception as e:
        if "No need to change" not in str(e):
            print(f"  [WARN] Margin mode set: {e}")
    try:
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
    except Exception as e:
        print(f"  [WARN] Leverage set: {e}")


def place_futures_limit_order(client, cand: dict) -> dict:
    """Place futures LIMIT entry order (LONG or SHORT)."""
    from binance.exceptions import BinanceAPIException

    sym   = cand["symbol"]
    side  = "BUY" if cand["position_side"] == "LONG" else "SELL"
    qty   = cand["sizing"]["qty"]
    entry = cand["entry_price"]
    step  = cand["constraints"].get("step_size", 0)
    tick  = cand["constraints"].get("tick_size", 0)

    qty_str   = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
    price_str = f"{round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

    set_leverage_and_margin_mode(client, sym)

    try:
        return client.futures_create_order(
            symbol      = sym,
            side        = side,
            type        = "LIMIT",
            timeInForce = "GTC",
            quantity    = qty_str,
            price       = price_str,
            positionSide = "BOTH",   # one-way mode
        )
    except BinanceAPIException as e:
        raise RuntimeError(f"Futures order failed: {e}") from e


def place_futures_exit_orders(client, trade: dict) -> dict:
    """
    Place TP and SL as two separate orders after entry fills.
    Futures has no OCO — uses TAKE_PROFIT_MARKET + STOP_MARKET.

    Returns dict: {tp_order_id, sl_order_id, success}
    """
    from binance.exceptions import BinanceAPIException

    sym  = trade["symbol"]
    qty  = trade["entry_qty"]
    tp1  = trade["tp1"]
    sl   = trade["sl"]
    side = "SELL" if trade["position_side"] == "LONG" else "BUY"

    # Fetch precision
    try:
        info = client.futures_exchange_info()
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

    # Verify current price constraint before placing
    try:
        current = get_futures_price(client, sym)
        is_long = trade["position_side"] == "LONG"
        emergency_triggered = (current <= sl) if is_long else (current >= sl)
        if emergency_triggered:
            cmp_symbol = "≤" if is_long else "≥"
            print(f"  ⚠  [{sym}] Price {current:.4f} {cmp_symbol} SL {sl:.4f} — placing MARKET exit")
            resp = client.futures_create_order(
                symbol=sym, side=side, type="MARKET",
                quantity=qty_str, positionSide="BOTH",
                reduceOnly=True
            )
            return {"sl_order_id": resp.get("orderId"), "tp_order_id": None,
                    "success": True, "emergency_exit": True}
    except Exception as e:
         print(f"  [WARN] Price check failed: {e} — proceeding anyway")

    results = {"tp_order_id": None, "sl_order_id": None,
               "tp_algo_id": None, "sl_algo_id": None,
               "success": False}

    try:
        tp_resp = client.futures_create_order(
            symbol       = sym,
            side         = side,
            type         = "TAKE_PROFIT_MARKET",
            stopPrice    = tp_str,
            quantity     = qty_str,
            timeInForce  = "GTC",
            positionSide = "BOTH",
            reduceOnly   = True,
            workingType  = "MARK_PRICE",
        )
        # Testnet may return algoId instead of orderId for conditional orders
        tp_order_id = tp_resp.get("orderId") or tp_resp.get("algoId")
        if tp_order_id is not None:
            results["tp_order_id"] = tp_order_id
            results["tp_algo_id"]  = tp_resp.get("algoId")
            print(f"  ✅ TP order placed: #{tp_order_id} @ {tp1}")
        else:
            print(f"  ❌ TP order — no orderId in response. Full response: {tp_resp}")
    except BinanceAPIException as e:
        print(f"  ❌ TP order failed: {e}")
    except Exception as e:
        print(f"  ❌ TP order unexpected error: {type(e).__name__}: {e}")

    try:
        sl_resp = client.futures_create_order(
            symbol       = sym,
            side         = side,
            type         = "STOP_MARKET",
            stopPrice    = sl_str,
            quantity     = qty_str,
            timeInForce  = "GTC",
            positionSide = "BOTH",
            reduceOnly   = True,
            workingType  = "MARK_PRICE",
        )
        sl_order_id = sl_resp.get("orderId") or sl_resp.get("algoId")
        if sl_order_id is not None:
            results["sl_order_id"] = sl_order_id
            results["sl_algo_id"]  = sl_resp.get("algoId")
            print(f"  ✅ SL order placed: #{sl_order_id} @ {sl}")
        else:
            print(f"  ❌ SL order — no orderId in response. Full response: {sl_resp}")
    except BinanceAPIException as e:
        print(f"  ❌ SL order failed: {e}")
    except Exception as e:
        print(f"  ❌ SL order unexpected error: {type(e).__name__}: {e}")

    results["success"] = (results["tp_order_id"] is not None and
                          results["sl_order_id"] is not None)
    return results


# ---------------------------------------------------------------------------
# 10. TELEGRAM (standalone copy — no import from paper_trade_executor)
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    if any(p in f"{token}:{chat_id}".lower()
           for p in ("your_telegram", "replace_me", "placeholder")):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 11. POSITION MONITORING
# ---------------------------------------------------------------------------

def check_futures_positions(client, verbose: bool = False) -> None:
    """
    For each OPEN futures trade:
    1. Query entry order status.
    2. If FILLED and no exit orders → place TP + SL orders.
    3. Check if TP or SL order filled → resolve trade, compute ML features.
    4. Display status.
    """
    trades     = load_futures_log()
    open_trades = [t for t in trades if t.get("exit_status") == "OPEN"]
    log_dirty  = False

    if not open_trades:
        print("\n  No open futures positions.")
        closed = [t for t in trades if t.get("exit_status") != "OPEN"][-5:]
        if closed:
            print(f"\n  Last {len(closed)} closed futures trade(s):")
            for t in closed:
                pnl = f"${t['realized_pnl_usd']:+.2f}" if t.get("realized_pnl_usd") else "n/a"
                side = t.get("position_side", "?")
                print(f"    {t['symbol']:10} {side:5} {t['exit_status']:10}  PnL: {pnl}")
        return

    print(f"\n  ── OPEN FUTURES POSITIONS: {len(open_trades)} ──")
    print(f"  {'Symbol':<12} {'Side':<6} {'Status':<22} {'Unreal PnL':>12}  Exit Orders")
    print(f"  {'─'*65}")

    resolved_this_run = []

    # Fetch all mark prices once
    try:
        all_tickers = {t["symbol"]: float(t["price"])
                       for t in client.futures_symbol_ticker()}
    except Exception:
        all_tickers = {}

    for trade in open_trades:
        sym  = trade["symbol"]
        side = trade.get("position_side", "LONG")
        eid  = trade.get("entry_order_id")

        # ── Step 1: Query entry order ──────────────────────────────────
        try:
            entry_order  = client.futures_get_order(symbol=sym, orderId=eid)
            entry_status = entry_order.get("status", "UNKNOWN")
        except Exception as e:
            print(f"  {sym:<12} ⚠ Could not query: {e}")
            continue

        filled_qty  = float(entry_order.get("executedQty", 0))
        cum_quote   = float(entry_order.get("cumQuote", 0))
        # Primary: cumQuote / executedQty (most accurate)
        # Binance Futures Testnet often returns cumQuote=0 for LIMIT fills,
        # so we fall through a chain of alternatives before using the limit price.
        if filled_qty > 0 and cum_quote > 0:
            fill_price = cum_quote / filled_qty
        else:
            # Fallback 1: avgPrice field in the futures order response
            _avg = float(entry_order.get("avgPrice", 0))
            if _avg > 0:
                fill_price = _avg
            else:
                # Fallback 2: /fapi/v1/userTrades — actual execution price
                _fill_resolved = False
                try:
                    my_trades = client.futures_account_trades(symbol=sym, orderId=eid, limit=5)
                    if my_trades:
                        total_qty   = sum(float(t["qty"])       for t in my_trades)
                        total_quote = sum(float(t["quoteQty"])  for t in my_trades)
                        if total_qty > 0 and total_quote > 0:
                            fill_price = total_quote / total_qty
                            _fill_resolved = True
                except Exception:
                    pass
                if not _fill_resolved:
                    # Fallback 3: limit price from the order (same as entry_price for GTC orders)
                    fill_price = float(entry_order.get("price", trade["entry_price"]))

        if trade.get("entry_status") != entry_status:
            trade["entry_status"] = entry_status
            log_dirty = True

        if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
            trade["entry_fill_price"] = fill_price
            trade["entry_fill_time"]  = int(entry_order.get("updateTime", 0))
            trade["entry_qty"]        = filled_qty
            planned = trade.get("entry_price", fill_price)
            trade["slippage_pct"] = round(
                (fill_price - planned) / planned * 100, 4
            ) if planned else None
            # Seed funding accrual window at fill time
            trade["last_funding_check_time"] = trade["entry_fill_time"]
            log_dirty = True
            _send_telegram(
                f"✅ [FUTURES] Filled: {sym} {side} @ {ca._fmt_price(fill_price).strip()}"
                f" | SL: {ca._fmt_price(trade.get('sl')).strip()}"
                f" | TP: {ca._fmt_price(trade.get('tp1')).strip()}"
                f" | Liq: {ca._fmt_price(trade.get('liquidation_price')).strip()}"
            )

        # ── Step 2: Place exit orders if filled and not yet placed ─────
        if entry_status == "FILLED" and not trade.get("exit_orders_placed"):
            print(f"  {sym:<12} ✅ FILLED — placing TP + SL orders...")
            exit_result = place_futures_exit_orders(client, trade)
            trade["tp_order_id"]        = exit_result.get("tp_order_id")
            trade["sl_order_id"]        = exit_result.get("sl_order_id")
            trade["tp_algo_id"]         = exit_result.get("tp_algo_id")
            trade["sl_algo_id"]         = exit_result.get("sl_algo_id")
            trade["exit_orders_placed"] = exit_result["success"]
            log_dirty = True

            if exit_result.get("emergency_exit"):
                # Market sell already happened — resolve immediately
                current = all_tickers.get(sym, fill_price)
                pnl_usd = (current - fill_price) * filled_qty * (1 if side == "LONG" else -1)
                pnl_pct = pnl_usd / trade.get("entry_notional", 1) * 100
                trade["exit_status"]     = "SL_HIT"
                trade["exit_price"]      = round(current, 6)
                trade["realized_pnl_usd"] = round(pnl_usd, 4)
                trade["realized_pnl_pct"] = round(pnl_pct, 2)
                trade["exit_orders_placed"] = False
                log_dirty = True
                resolved_this_run.append((sym, "SL_HIT", pnl_usd, side))
                continue

            if not exit_result["success"]:
                print(f"\n  {'!'*60}")
                print(f"  !! CRITICAL: Exit orders FAILED for {sym} {side} !!")
                print(f"  !! Position UNPROTECTED — fix manually at testnet.binancefuture.com !!")
                print(f"  {'!'*60}\n")

        # ── Step 2.5: Accrue funding rate for all filled open positions ─
        if entry_status == "FILLED" and trade.get("exit_status") == "OPEN":
            if accrue_funding(client, trade):
                log_dirty = True

        # ── Step 3: Check exit order status ───────────────────────────
        exit_str = "n/a"
        tp_id = trade.get("tp_order_id")
        sl_id = trade.get("sl_order_id")
        tp_algo_id = trade.get("tp_algo_id")
        sl_algo_id = trade.get("sl_algo_id")

        def _query_exit_order(order_id, algo_id, label):
            """
            Query exit order status. Tries regular order first,
            falls back to algo order query if -2013 (not found).
            Returns (status, fill_price, update_time) or None on error.
            """
            # Try regular order endpoint
            try:
                o = client.futures_get_order(symbol=sym, orderId=order_id)
                return o.get("status"), \
                       (float(o.get("cumQuote",0)) / max(float(o.get("executedQty",0)),0.0001)
                        if float(o.get("executedQty",0)) > 0
                        else float(o.get("stopPrice",0))), \
                       o.get("updateTime")
            except Exception as e:
                if "-2013" not in str(e) and "does not exist" not in str(e).lower():
                    return None, None, None
            # Fallback: algo order endpoint
            if algo_id:
                try:
                    import requests as _req
                    import time as _time, hmac, hashlib, urllib.parse
                    api_key    = client.API_KEY
                    api_secret = client.API_SECRET
                    ts = int(_time.time() * 1000)
                    params = f"symbol={sym}&algoId={algo_id}&timestamp={ts}"
                    sig = hmac.new(api_secret.encode(), params.encode(), hashlib.sha256).hexdigest()
                    resp = _req.get(
                        f"https://testnet.binancefuture.com/fapi/v1/algo/orders/history"
                        f"?{params}&signature={sig}",
                        headers={"X-MBX-APIKEY": api_key}, timeout=8
                    ).json()
                    orders = resp.get("orders", [])
                    for ao in orders:
                        if str(ao.get("algoId")) == str(algo_id):
                            status = ao.get("orderStatus") or ao.get("algoStatus", "UNKNOWN")
                            sp = float(ao.get("triggerPrice") or ao.get("stopPrice") or 0)
                            return status, sp, ao.get("updateTime")
                except Exception:
                    pass
            return None, None, None

        if trade.get("exit_orders_placed") and (tp_id or sl_id):
            exit_status_found     = None
            exit_price_found      = None
            exit_time_from_exchange = None

            for order_id, algo_id, order_type in [
                (tp_id, tp_algo_id, "TP"),
                (sl_id, sl_algo_id, "SL"),
            ]:
                if not order_id:
                    continue
                status, ep, upd_time = _query_exit_order(order_id, algo_id, order_type)
                if status in ("FILLED", "EXECUTED", "COMPLETED"):
                    exit_status_found       = "TP_HIT" if order_type == "TP" else "SL_HIT"
                    exit_price_found        = ep or float(trade.get("tp1" if order_type=="TP" else "sl", 0))
                    exit_time_from_exchange = upd_time
                    break
                if status:
                    exit_str = status

            if exit_status_found:
                entry_fill  = trade.get("entry_fill_price") or trade["entry_price"]
                qty         = trade.get("entry_qty", 0)
                pnl_usd     = (exit_price_found - entry_fill) * qty * (1 if side == "LONG" else -1)
                pnl_pct     = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100

                # exit_time: prefer exchange updateTime (accurate), fallback to now
                exit_time_ms = (
                    int(exit_time_from_exchange)
                    if exit_time_from_exchange
                    else int(datetime.now(timezone.utc).timestamp() * 1000)
                )

                trade["exit_status"]      = exit_status_found
                trade["exit_price"]       = round(exit_price_found, 6)
                trade["exit_time"]        = exit_time_ms
                trade["realized_pnl_usd"] = round(pnl_usd, 4)
                trade["realized_pnl_pct"] = round(pnl_pct, 2)

                # time_in_position_sec
                if trade.get("entry_fill_time") and trade["exit_time"]:
                    trade["time_in_position_sec"] = (
                        trade["exit_time"] - int(trade["entry_fill_time"])
                    ) // 1000

                # Cancel the surviving exit order to prevent ghost position
                surviving_id = sl_id if exit_status_found == "TP_HIT" else tp_id
                surviving_type = "SL" if exit_status_found == "TP_HIT" else "TP"
                if surviving_id:
                    try:
                        client.futures_cancel_order(symbol=sym, orderId=surviving_id)
                        print(f"  ✅ {sym} — {surviving_type} order #{surviving_id} canceled (counterpart)")
                    except Exception as cancel_err:
                        print(f"  ⚠  {sym} — Could not cancel {surviving_type} order #{surviving_id}: {cancel_err}")
                        print(f"      Manual action required at testnet.binancefuture.com to avoid ghost position")

                # MAE/MFE reconstruction (Opsi B)
                if trade.get("entry_fill_time") and trade["exit_time"]:
                    excursion = compute_mae_mfe_from_candles(
                        symbol         = sym,
                        position_side  = side,
                        entry_price    = entry_fill,
                        entry_time_ms  = int(trade["entry_fill_time"]),
                        exit_time_ms   = trade["exit_time"],
                        liquidation_price = trade.get("liquidation_price", 0),
                    )
                    trade.update(excursion)

                log_dirty = True
                resolved_this_run.append((sym, exit_status_found, pnl_usd, side))
                exit_str = f"{'🟢' if exit_status_found == 'TP_HIT' else '🔴'} {exit_status_found}"
                continue

        # ── Step 4: Display compact line ──────────────────────────────
        current = all_tickers.get(sym)
        # Fix 2: fallback to single-symbol ticker when batch fetch missed this sym
        if current is None:
            try:
                current = float(client.futures_symbol_ticker(symbol=sym)["price"])
            except Exception:
                current = None

        # ── Step 3.5: Price-guard — catch SL breaches testnet missed ──
        # Testnet sometimes fails to trigger stop orders. If price has
        # already blown through SL, resolve the trade as SL_HIT now
        # rather than letting it show OPEN indefinitely.
        if (entry_status == "FILLED"
                and trade.get("exit_status") == "OPEN"
                and current is not None
                and trade.get("exit_orders_placed")):
            sl  = trade.get("sl")
            _sl_breached = (
                (side == "LONG"  and sl and current <= sl) or
                (side == "SHORT" and sl and current >= sl)
            )
            if _sl_breached:
                print(f"  ⚠  [{sym}] Price {current:.4f} breached SL {sl:.4f} "
                      f"— exchange order may have failed. Resolving as SL_HIT.")
                entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                qty        = trade.get("entry_qty", 0)
                pnl_usd    = (current - entry_fill) * qty * (1 if side == "LONG" else -1)
                pnl_pct    = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                exit_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                trade["exit_status"]       = "SL_HIT"
                trade["exit_price"]        = round(current, 6)
                trade["exit_time"]         = exit_time_ms
                trade["realized_pnl_usd"]  = round(pnl_usd, 4)
                trade["realized_pnl_pct"]  = round(pnl_pct, 2)
                if trade.get("entry_fill_time"):
                    trade["time_in_position_sec"] = (exit_time_ms - int(trade["entry_fill_time"])) // 1000
                # Attempt to cancel any lingering exit orders
                for _oid in [tp_id, sl_id]:
                    if _oid:
                        try:
                            client.futures_cancel_order(symbol=sym, orderId=_oid)
                        except Exception:
                            pass
                log_dirty = True
                resolved_this_run.append((sym, "SL_HIT", pnl_usd, side))
                _send_telegram(
                    f"🛑 [FUTURES] SL_HIT (price-guard): {sym} {side} @ {ca._fmt_price(current).strip()}"
                    f"  |  PnL: ${pnl_usd:+.2f}"
                )
                continue

        pnl_display = "n/a"
        if entry_status == "FILLED" and current and trade.get("entry_qty", 0) > 0:
            ref = trade.get("entry_fill_price") or trade["entry_price"]
            qty = trade.get("entry_qty", 0)
            pnl = (current - ref) * qty * (1 if side == "LONG" else -1)
            pnl_display = f"${pnl:+.3f}"
        elif entry_status in ("NEW", "PARTIALLY_FILLED") and current:
            ep  = trade.get("entry_price", current)
            dist = (ep - current) / current * 100
            pnl_display = f"{dist:+.2f}% fill"

        status_icon = {
            "NEW":              "🕐 NEW (pending)",
            "FILLED":           "✅ FILLED",
            "PARTIALLY_FILLED": "🔄 PARTIAL",
            "CANCELED":         "❌ CANCELED",
        }.get(entry_status, entry_status)[:20]

        print(f"  {sym:<12} {side:<6} {status_icon:<22} {pnl_display:>12}  {exit_str}")

        if verbose:
            # Verbose card — same design as spot's --verbose card
            sym_hdr = f"{sym}  {side}"
            entry_price = trade.get("entry_price")
            entry_fill  = trade.get("entry_fill_price")
            sl  = trade.get("sl")
            tp  = trade.get("tp1")
            liq = trade.get("liquidation_price")
            qty = trade.get("entry_qty") or 0

            def _fp(p):
                return ca._fmt_price(p, width=14).strip() if p is not None else "n/a"

            def _pct(a, b):
                try: return (a - b) / b * 100
                except: return None

            cur_str   = _fp(current)
            entry_str = _fp(entry_fill or entry_price)
            sl_str    = _fp(sl)
            tp_str    = _fp(tp)
            liq_str   = _fp(liq)

            pct_to_entry = _pct(entry_price, current) if current else None
            pct_sl       = _pct(sl, current)          if (current and sl)  else None
            pct_tp       = _pct(tp, current)           if (current and tp)  else None
            pct_liq      = _pct(liq, current)          if (current and liq) else None

            W = 78
            print("\n  " + "╔" + "═" * (W - 2) + "╗")
            print(f"  ║ {sym_hdr:<{W-4}} ║")
            print(f"  ║{'':{W-2}}║")

            if entry_status in ("NEW", "PARTIALLY_FILLED"):
                price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                if pct_to_entry is not None:
                    price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                print(f"  ║ {price_line:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")
                # TP1 / SL / R:R row — always show even before fill
                rr = trade.get("planned_rr")
                tpsl_line = f"TP1: {tp_str:>12}   |   SL: {sl_str:>12}"
                if rr:
                    tpsl_line += f"   |   R:R  {rr:.2f}:1"
                print(f"  ║ {tpsl_line:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")
            elif entry_status == "FILLED":
                entrycur_line = f"Entry: {entry_str:>12}   |  Current: {cur_str:>12}"
                # Fix 3: warn if current price equals entry_fill_price after a FILLED order —
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
                # TP1 / SL with % distances from current
                rr = trade.get("planned_rr")
                tpsl_line = f"TP1: {tp_str:>12}"
                if pct_tp is not None:
                    tpsl_line += f" ({pct_tp:+.2f}%)"
                tpsl_line = tpsl_line.ljust(36)
                tpsl_line += f"  |  SL: {sl_str:>12}"
                if pct_sl is not None:
                    tpsl_line += f" ({pct_sl:+.2f}%)"
                if rr:
                    tpsl_line += f"   R:R {rr:.2f}:1"
                print(f"  ║ {tpsl_line:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")
            else:
                price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                if pct_to_entry is not None:
                    price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                print(f"  ║ {price_line:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")
                rr = trade.get("planned_rr")
                tpsl_line = f"TP1: {tp_str:>12}   |   SL: {sl_str:>12}"
                if rr:
                    tpsl_line += f"   |   R:R  {rr:.2f}:1"
                print(f"  ║ {tpsl_line:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")

            # Futures-specific: liquidation row
            liq_line = f"Liq: {liq_str:>12}"
            if pct_liq is not None:
                liq_line += f" ({pct_liq:+.2f}% from current)"
            liq_line = liq_line.ljust(38)
            liq_line += f"  |  {LEVERAGE}x {MARGIN_MODE}"
            print(f"  ║ {liq_line:<{W-4}} ║")
            print(f"  ║{'':{W-2}}║")

            # Status line
            status_line = f"Status: {entry_status}"
            if entry_status == "FILLED" and qty and current:
                ref   = entry_fill or entry_price
                unreal = qty * (current - ref) * (1 if side == "LONG" else -1)
                status_line += f"  |  Unreal: ${unreal:+.3f}"
            funding = trade.get("funding_rate_paid", 0.0) or 0.0
            if funding != 0.0:
                status_line += f"  |  Funding: ${funding:+.4f}"
            regime = trade.get("volatility_regime_at_entry", "?")
            status_line += f"  |  Regime: {regime}"
            if trade.get("tp_order_id"):
                status_line += f"  |  Exit orders: ✅ placed"
            elif entry_status == "FILLED" and not trade.get("exit_orders_placed"):
                status_line += f"  |  Exit orders: ⚠ NOT placed"
            print(f"  ║ {status_line:<{W-4}} ║")
            print("  " + "╚" + "═" * (W - 2) + "╝\n")

    # ── Summary + notifications ────────────────────────────────────────
    if resolved_this_run:
        print(f"\n  ── Resolved this run: {len(resolved_this_run)} trade(s) ──")
        for sym, status, pnl, ps in resolved_this_run:
            icon = "🟢" if status == "TP_HIT" else "🔴"
            print(f"    {icon} {sym} {ps} {status}  PnL: ${pnl:+.4f}")
            _send_telegram(
                f"{icon} [FUTURES] {'TP HIT' if status == 'TP_HIT' else 'SL HIT'}: "
                f"{sym} {ps} {'+' if pnl >= 0 else ''}{pnl:.4f} USD"
            )

    # ── Save ──────────────────────────────────────────────────────────
    if log_dirty:
        from supabase_client import update_futures_by_order_id
        for ot in open_trades:
            eid = ot.get("entry_order_id")
            if not eid:
                continue
            update_futures_by_order_id(eid, {
                "entry_status":                     ot.get("entry_status"),
                "entry_fill_price":                 ot.get("entry_fill_price"),
                "entry_fill_time":                  ot.get("entry_fill_time"),
                "entry_qty":                        ot.get("entry_qty"),
                "slippage_pct":                     ot.get("slippage_pct"),
                "last_funding_check_time":           ot.get("last_funding_check_time"),
                "tp_order_id":                      ot.get("tp_order_id"),
                "sl_order_id":                      ot.get("sl_order_id"),
                "tp_algo_id":                       ot.get("tp_algo_id"),
                "sl_algo_id":                       ot.get("sl_algo_id"),
                "exit_orders_placed":               ot.get("exit_orders_placed"),
                "funding_rate_paid":                ot.get("funding_rate_paid"),
                "funding_rate_history":             ot.get("funding_rate_history"),
                "exit_status":                      ot.get("exit_status"),
                "exit_price":                       ot.get("exit_price"),
                "exit_time":                        ot.get("exit_time"),
                "realized_pnl_usd":                 ot.get("realized_pnl_usd"),
                "realized_pnl_pct":                 ot.get("realized_pnl_pct"),
                "time_in_position_sec":             ot.get("time_in_position_sec"),
                "max_adverse_excursion_pct":        ot.get("max_adverse_excursion_pct"),
                "max_favorable_excursion_pct":      ot.get("max_favorable_excursion_pct"),
                "distance_to_liquidation_pct_min":  ot.get("distance_to_liquidation_pct_min"),
            })

    print("\n  Run --check-positions again to refresh.")


# ---------------------------------------------------------------------------
# 12. PROPOSAL DISPLAY
# ---------------------------------------------------------------------------

def print_futures_proposal(cand: dict) -> None:
    sym   = cand["symbol"]
    side  = cand["position_side"]
    entry = cand["entry_price"]
    sl    = cand["sl"]
    tp1   = cand["tp1"]
    rr    = cand["rr"]
    sz    = cand["sizing"]
    liq   = cand["liquidation"]
    ez    = cand.get("entry_zone") or {}
    W = 70

    print(f"\n  ╔{'═'*W}╗")
    print(f"  ║  {'FUTURES TRADE PROPOSAL — BINANCE FUTURES TESTNET':<{W-1}}║")
    print(f"  ║  {'No order placed until you confirm with y':<{W-1}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Symbol':<22} {sym:<{W-24}}║")
    print(f"  ║  {'Position side':<22} {side:<{W-24}}║")
    print(f"  ║  {'Leverage':<22} {LEVERAGE}x isolated margin{'':{W-40}}║")
    print(f"  ║  {'Order type':<22} {'LIMIT (waits for zone)':<{W-24}}║")

    if ez:
        dist = abs(cand["current_price"] - ez.get("center", entry)) / cand["current_price"] * 100
        z_str = (f"{ca._fmt_price(ez.get('center')).strip()}  "
                 f"({ez.get('touches','?')}× tested, {dist:.2f}% away)")
        print(f"  ║  {'Entry zone':<22} {z_str:<{W-24}}║")

    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Entry (limit)':<22} {ca._fmt_price(entry).strip():>{W-24}}║")
    print(f"  ║  {'Stop-Loss':<22} {ca._fmt_price(sl).strip():>{W-24}}║")
    print(f"  ║  {'TP1':<22} {ca._fmt_price(tp1).strip():>{W-24}}║")
    print(f"  ║  {'R:R':<22} {rr:.2f}:1{'':{W-27}}║")
    print(f"  ║  {'SL distance':<22} {cand['risk_pct']:.2f}%{'':{W-27}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Liquidation price':<22} {ca._fmt_price(liq['liquidation_price']).strip():>{W-24}}║")
    dist_liq = liq["distance_to_liquidation_pct"]
    print(f"  ║  {'Dist to liquidation':<22} {dist_liq:.2f}%  "
          f"({'✅ safe' if dist_liq > 10 else '⚠ tight'}){'':{W-46}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Margin budget':<22} ${FUTURES_BUDGET_USD:.2f}{'':{W-29}}║")
    print(f"  ║  {'Margin used':<22} ${sz['margin_used']:.2f}{'':{W-29}}║")
    print(f"  ║  {'Notional (3x)':<22} ${sz['notional_usd']:.2f}{'':{W-29}}║")
    print(f"  ║  {'Max loss (risk 25%)':<22} ${sz['max_loss_usd']:.2f}  ({sz['max_loss_pct']:.1f}%){'':{W-46}}║")
    print(f"  ║  {'Qty':<22} {sz['qty']:.8g} {sym.replace('USDT',''):<{W-32}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Volatility regime':<22} {cand.get('volatility_regime','?'):<{W-24}}║")
    fr = cand.get("funding_rate_at_entry")
    fr_str = f"{fr*100:.4f}% per 8h" if fr is not None else "n/a"
    print(f"  ║  {'Funding rate':<22} {fr_str:<{W-24}}║")
    print(f"  ║  {'ATR(14)':<22} {cand['atr_pct']:.2f}%{'':{W-27}}║")

    if sz["warnings"]:
        print(f"  ╠{'═'*W}╣")
        for w in sz["warnings"]:
            print(f"  ║  ⚠  {w:<{W-5}}║")

    print(f"  ╚{'═'*W}╝")


# ---------------------------------------------------------------------------
# 13. STATS (independent from spot, grouped by position_side + rule_version)
# ---------------------------------------------------------------------------

def cmd_stats_futures() -> None:
    """
    Performance statistics from trade_futures.json.
    Grouped by: rule_version × position_side (LONG / SHORT).
    Effective-n and z-score computed independently per group.
    """
    from collections import defaultdict

    trades = load_futures_log()
    closed = [t for t in trades if t.get("exit_status") in ("TP_HIT", "SL_HIT")]

    if not closed:
        print("\n  No closed futures trades yet.")
        return

    print("=" * 70)
    print("Futures Performance Statistics")
    print(f"Total closed trades: {len(closed)}")
    print("(Stats are INDEPENDENT from spot — separate effective-n & z-score)")
    print("=" * 70)

    # Group by rule_version × position_side
    groups: dict[tuple, list] = defaultdict(list)
    for t in closed:
        key = (t.get("rule_version", "unknown"), t.get("position_side", "UNKNOWN"))
        groups[key].append(t)

    for (version, ps), group in sorted(groups.items()):
        n       = len(group)
        wins    = [t for t in group if t["exit_status"] == "TP_HIT"]
        losses  = [t for t in group if t["exit_status"] == "SL_HIT"]
        win_rate = len(wins) / n if n > 0 else 0

        avg_rr       = sum(t.get("planned_rr", 0) for t in group) / n
        be_win_rate  = 1 / (1 + avg_rr) if avg_rr > 0 else 0.5
        avg_win_pct  = sum(t.get("realized_pnl_pct", 0) for t in wins) / len(wins) if wins else 0
        avg_loss_pct = sum(t.get("realized_pnl_pct", 0) for t in losses) / len(losses) if losses else 0
        avg_funding  = sum(abs(t.get("funding_rate_paid", 0) or 0) for t in group) / n
        avg_fee_pct  = sum(
            (t.get("fee_usd_roundtrip", 0) / max(t.get("entry_notional", 1), 1) * 100)
            for t in group
        ) / n

        expectancy = (win_rate * avg_win_pct) - ((1 - win_rate) * abs(avg_loss_pct)) - avg_fee_pct

        # Z-score vs breakeven win rate
        if n >= 2:
            p0  = be_win_rate
            z   = (win_rate - p0) / math.sqrt(p0 * (1 - p0) / n)
            sig = "✅ p<0.05" if abs(z) >= 1.96 else ("🟡 p<0.10" if abs(z) >= 1.645 else "⚠ not sig")
        else:
            z, sig = 0.0, "⚠ n/a"

        # Cluster-based effective-n (mirrors spot cmd_stats logic):
        # trades from --propose-multi share a correlation_cluster_id and co-move,
        # so they count as ONE independent observation per cluster, not N.
        cluster_ids = {t.get("correlation_cluster_id") for t in group
                       if t.get("correlation_cluster_id")}
        n_clusters  = len(cluster_ids)
        n_singles   = sum(1 for t in group if not t.get("correlation_cluster_id"))
        effective_n = n_clusters + n_singles

        # Volatility regime breakdown
        regimes = {}
        for t in group:
            r = t.get("volatility_regime_at_entry", "unknown")
            regimes[r] = regimes.get(r, 0) + 1

        print(f"\n  Rule: {version}  |  Side: {ps}  ({n} trades)")
        print(f"  {'─'*60}")
        print(f"  Win rate          : {win_rate*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Breakeven win rate: {be_win_rate*100:.1f}%  (at avg R:R {avg_rr:.2f}:1)")
        print(f"  Avg win  %        : {avg_win_pct:+.2f}%")
        print(f"  Avg loss %        : {avg_loss_pct:+.2f}%")
        print(f"  Avg fee %         : -{avg_fee_pct:.3f}%")
        print(f"  Avg funding paid  : {avg_funding:.4f} per trade")
        print(f"  Expectancy        : {expectancy:+.3f}%  per trade")
        print(f"  Z-score vs B/E    : {z:+.2f}  {sig}")
        if n < 30:
            print(f"  ⚠  Only {n} trades — z-score unreliable until n≥30")
        print(f"  Clusters          : {n_clusters} independent sessions + {n_singles} single trades")
        print(f"  Effective n       : ~{effective_n} independent observations")
        print(f"  (raw trade count {n} overstates independence if cluster trades co-move)")
        regime_str = "  ".join(f"{r}:{c}" for r, c in sorted(regimes.items()))
        print(f"  Volatility regime : {regime_str}")

        # MAE/MFE summary (if available)
        mae_vals = [t["max_adverse_excursion_pct"] for t in group
                    if t.get("max_adverse_excursion_pct") is not None]
        mfe_vals = [t["max_favorable_excursion_pct"] for t in group
                    if t.get("max_favorable_excursion_pct") is not None]
        if mae_vals:
            print(f"  Avg MAE           : {sum(mae_vals)/len(mae_vals):.2f}%  "
                  f"(max: {max(mae_vals):.2f}%)")
        if mfe_vals:
            print(f"  Avg MFE           : {sum(mfe_vals)/len(mfe_vals):.2f}%  "
                  f"(max: {max(mfe_vals):.2f}%)")

    # Total PnL
    total_pnl = sum(t.get("realized_pnl_usd", 0) or 0 for t in closed)
    total_funding = sum(t.get("funding_rate_paid", 0) or 0 for t in closed)
    print(f"\n  Total realized PnL     : ${total_pnl:+.4f}")
    print(f"  Total funding paid     : ${total_funding:+.4f}")
    print(f"  Net PnL after funding  : ${total_pnl + total_funding:+.4f}")


# ---------------------------------------------------------------------------
# 14. CLI COMMANDS
# ---------------------------------------------------------------------------

def cmd_propose_futures(
    scan_n:        int,
    symbol_filter: str | None = None,
    side_filter:   str | None = None,
    auto_confirm:  bool = False,
) -> None:
    print("=" * 70)
    print("Futures Trade Executor — Binance Futures Testnet")
    print(f"Leverage: {LEVERAGE}x  |  Margin: {MARGIN_MODE}  |  Rule: {RULE_VERSION}")
    print("=" * 70)

    print("\nConnecting to Binance Futures Testnet...")
    try:
        client = get_futures_client()
        ok = ping_futures(client)
        if not ok:
            raise RuntimeError("Ping failed")
        print("✅ Futures testnet connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Gather candidates (LONG + SHORT)
    candidates = gather_futures_candidates(scan_n)
    if not candidates:
        print("\n❌ No futures candidates found.")
        sys.exit(0)

    # Filter open positions to avoid duplicate symbols
    open_trades  = [t for t in load_futures_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}

    # Hard cap on concurrent positions
    if len(open_trades) >= MAX_CONCURRENT_POSITIONS:
        print(f"\n⛔  Max concurrent positions reached: "
              f"{len(open_trades)} / {MAX_CONCURRENT_POSITIONS}")
        print(f"   Wait for at least one position to close (TP_HIT / SL_HIT) before proposing.")
        print(f"   Run --check-positions to see current status.")
        sys.exit(0)

    candidates   = [c for c in candidates if c["symbol"] not in open_symbols]

    if not candidates:
        print("❌ All candidates already have open positions.")
        sys.exit(0)

    # Show top 5
    print(f"\nTop candidates (Risk% ASC):")
    print(f"  {'#':<3} {'Symbol':<12} {'Side':<6} {'Risk%':>6} {'R:R':>5} {'Tier':>4} {'Regime':<8}")
    print(f"  {'─'*55}")
    for i, c in enumerate(candidates[:5], 1):
        print(f"  {i:<3} {c['symbol']:<12} {c['position_side']:<6} "
              f"{c['risk_pct']:>5.2f}% {c['rr']:>5.1f}x {c.get('tier_used','?'):>4}  "
              f"{c.get('volatility_regime','?'):<8}")

    print("\nChecking exchange constraints...")
    best = pick_best_futures_candidate(
        candidates, client,
        symbol_filter = symbol_filter,
        side_filter   = side_filter,
    )

    if best is None:
        print("❌ No candidate passed constraints.")
        sys.exit(0)

    print_futures_proposal(best)

    print("\n" + "─" * 70)
    print("  ⚠  This will place a REAL LIMIT ORDER on Binance Futures TESTNET.")
    print("  Virtual funds only. Type 'y' to confirm.")
    print("─" * 70)

    if auto_confirm:
        print("  [--yes] Auto-confirmed for non-interactive run.")
        answer = "y"
    else:
        try:
            answer = input("  Confirm? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""

    if answer != "y":
        print("\n  Aborted. No order placed.")
        return

    print("\n  Placing futures limit order...")
    try:
        order = place_futures_limit_order(client, best)
    except RuntimeError as e:
        print(f"\n  ❌ Order failed: {e}")
        return

    print(f"\n  ✅ Futures order placed!")
    print(f"     Order ID  : {order.get('orderId')}")
    print(f"     Symbol    : {order.get('symbol')}")
    print(f"     Side      : {order.get('side')}")
    print(f"     Status    : {order.get('status')}")
    print(f"     Price     : {order.get('price')}")
    print(f"     Qty       : {order.get('origQty')}")

    log_futures_trade(order, best)
    print(f"\n  Trade logged to {FUTURES_LOG_PATH}")

    # Telegram notify
    _send_telegram(
        f"📋 [FUTURES] Order placed: {best['symbol']} {best['position_side']}\n"
        f"Entry: {ca._fmt_price(best['entry_price']).strip()}  "
        f"SL: {ca._fmt_price(best['sl']).strip()}  "
        f"TP: {ca._fmt_price(best['tp1']).strip()}\n"
        f"Liq: {ca._fmt_price(best['liquidation']['liquidation_price']).strip()}  "
        f"({best['liquidation']['distance_to_liquidation_pct']:.1f}% away)\n"
        f"Leverage: {LEVERAGE}x | Margin: ${best['sizing']['margin_used']:.2f}"
    )


def cmd_propose_multi_futures(
    scan_n:       int,
    count:        int,
    side_filter:  str | None = None,
    auto_confirm: bool = False,
) -> None:
    """
    --propose-multi: scan, pick up to `count` candidates (LONG+SHORT),
    show a summary table, ask ONE 'y' confirmation, then place all orders.
    All trades in this run share a correlation_cluster_id.
    Candidates that fail exchange constraints are skipped with a report
    at the end — they do not abort the whole batch.
    """
    print("=" * 70)
    print("Futures Trade Executor — BATCH PROPOSAL (--propose-multi)")
    print(f"Leverage: {LEVERAGE}x  |  Margin: {MARGIN_MODE}  |  Rule: {RULE_VERSION}")
    print(f"Requesting up to {count} position(s)")
    print("=" * 70)

    print("\nConnecting to Binance Futures Testnet...")
    try:
        client = get_futures_client()
        if not ping_futures(client):
            raise RuntimeError("Ping failed")
        print("✅ Futures testnet connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Check concurrent-position cap
    open_trades  = [t for t in load_futures_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}
    slots_left   = MAX_CONCURRENT_POSITIONS - len(open_trades)

    if slots_left <= 0:
        print(f"\n⛔  Max concurrent positions reached: "
              f"{len(open_trades)} / {MAX_CONCURRENT_POSITIONS}")
        print("   Wait for a position to close before using --propose-multi.")
        sys.exit(0)

    effective_count = min(count, slots_left)
    if effective_count < count:
        print(f"\n  ℹ️  Only {slots_left} slot(s) free — capping batch at {effective_count}.")

    # Gather + filter
    candidates = gather_futures_candidates(scan_n)
    if not candidates:
        print("\n❌ No futures candidates found.")
        sys.exit(0)

    candidates = [c for c in candidates if c["symbol"] not in open_symbols]
    if not candidates:
        print("❌ All candidates already have open positions.")
        sys.exit(0)

    # ── Iteratively pick up to effective_count passing candidates ────────
    selected:     list[dict] = []   # candidates that passed constraints
    skipped_syms: list[str]  = []   # symbols that failed constraints
    excluded_symbols: set[str] = set(open_symbols)  # grows as we pick

    remaining = list(candidates)
    while len(selected) < effective_count and remaining:
        # Exclude already-selected symbols (avoid same symbol twice in batch)
        pool = [c for c in remaining if c["symbol"] not in excluded_symbols]
        if not pool:
            break

        pick = pick_best_futures_candidate(pool, client, side_filter=side_filter)
        if pick is None:
            # No more candidates pass constraints — collect all remaining as skipped
            skipped_syms.extend(
                c["symbol"] for c in pool
                if c["symbol"] not in excluded_symbols
            )
            break

        selected.append(pick)
        excluded_symbols.add(pick["symbol"])
        # Remove the picked symbol from remaining so we don't re-evaluate it
        remaining = [c for c in remaining if c["symbol"] != pick["symbol"]]

    if not selected:
        print("\n❌ No candidates passed exchange constraints.")
        sys.exit(0)

    # ── Display batch summary table ──────────────────────────────────────
    cluster_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"\n  Batch proposal  [{len(selected)} trade(s)]  cluster: {cluster_id}")
    print(f"  {'#':<3} {'Symbol':<12} {'Side':<6} {'Entry':>12} {'SL':>10} "
          f"{'TP1':>10} {'R:R':>5} {'Risk%':>6} {'Liq%':>6}  Regime")
    print(f"  {'─'*80}")
    for i, c in enumerate(selected, 1):
        liq = c["liquidation"]
        print(f"  {i:<3} {c['symbol']:<12} {c['position_side']:<6} "
              f"{ca._fmt_price(c['entry_price']).strip():>12} "
              f"{ca._fmt_price(c['sl']).strip():>10} "
              f"{ca._fmt_price(c['tp1']).strip():>10} "
              f"{c['rr']:>5.1f} {c['risk_pct']:>5.2f}% "
              f"{liq['distance_to_liquidation_pct']:>5.1f}%  "
              f"{c.get('volatility_regime','?')}")

    if skipped_syms:
        print(f"\n  ⚠  Skipped (failed constraints): {', '.join(skipped_syms)}")

    total_margin = sum(c["sizing"]["margin_used"] for c in selected)
    print(f"\n  Total margin if all fill: ${total_margin:.2f}  "
          f"({len(selected)} × ~${FUTURES_BUDGET_USD:.0f} budget, {LEVERAGE}x)")

    # ── Single confirmation ──────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print(f"  ⚠  This will place {len(selected)} LIMIT ORDER(S) on Binance Futures TESTNET.")
    print(f"  Virtual funds only. Type 'y' to place ALL, anything else to abort.")
    print(f"{'─' * 70}")

    if auto_confirm:
        print("  [--yes] Auto-confirmed for non-interactive run.")
        answer = "y"
    else:
        try:
            answer = input("  Confirm ALL? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""

    if answer != "y":
        print("\n  Aborted. No orders placed.")
        return

    # ── Place orders ─────────────────────────────────────────────────────
    placed:  list[tuple[str, str, int]] = []   # (symbol, side, orderId)
    failed:  list[tuple[str, str]]      = []   # (symbol, error)

    for cand in selected:
        sym  = cand["symbol"]
        side = cand["position_side"]
        try:
            order = place_futures_limit_order(client, cand)
            log_futures_trade(order, cand, correlation_cluster_id=cluster_id)
            oid = order.get("orderId")
            print(f"  ✅ {sym:<12} {side:<5}  order #{oid}  "
                  f"price={order.get('price')}")
            placed.append((sym, side, oid))
            _send_telegram(
                f"📋 [FUTURES MULTI] {sym} {side}\n"
                f"Entry: {ca._fmt_price(cand['entry_price']).strip()}  "
                f"SL: {ca._fmt_price(cand['sl']).strip()}  "
                f"TP: {ca._fmt_price(cand['tp1']).strip()}\n"
                f"Cluster: {cluster_id}"
            )
        except Exception as e:
            print(f"  ❌ {sym:<12} {side:<5}  FAILED: {e}")
            failed.append((sym, str(e)))

    # ── Final report ─────────────────────────────────────────────────────
    print(f"\n  ── Batch result ──")
    print(f"  Placed : {len(placed)}  |  Failed: {len(failed)}  |  Cluster: {cluster_id}")
    if failed:
        print(f"  Failed orders:")
        for sym, err in failed:
            print(f"    {sym}: {err}")
    print(f"\n  Run --check-positions to monitor.")


def cmd_check_futures(verbose: bool = False) -> None:
    print("=" * 70)
    print("Futures Trade Executor — Position Status")
    print("=" * 70)

    try:
        client = get_futures_client()
        if not ping_futures(client):
            raise RuntimeError("Ping failed")
        print("✅ Futures testnet connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    check_futures_positions(client, verbose=verbose)


# ---------------------------------------------------------------------------
# 15. MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Futures paper trading executor — Binance Futures Testnet."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--propose",          action="store_true",
                       help="Scan, pick best candidate, confirm & place")
    group.add_argument("--propose-multi",    action="store_true",
                       help="Batch: pick N candidates, single confirm, place all")
    group.add_argument("--check-positions",  action="store_true",
                       help="Check open futures positions, place TP/SL if filled")
    group.add_argument("--stats-futures",    action="store_true",
                       help="Print futures performance statistics (independent from spot)")

    parser.add_argument("--scan-n",    type=int, default=DEFAULT_SCAN_N)
    parser.add_argument("--count",     type=int, default=2,
                        help="--propose-multi: number of positions to open (default 2)")
    parser.add_argument("--symbol",    type=str, default=None,
                        help="Force a specific symbol")
    parser.add_argument("--side",      type=str, default=None,
                        choices=["LONG", "SHORT"],
                        help="Filter by position side (LONG or SHORT)")
    parser.add_argument("--verbose",   action="store_true",
                        help="Show detailed per-position info")
    parser.add_argument("--yes",       action="store_true",
                        help="Auto-confirm order placement (for non-interactive / CI use)")
    args = parser.parse_args()

    if args.propose:
        cmd_propose_futures(
            scan_n        = args.scan_n,
            symbol_filter = args.symbol,
            side_filter   = args.side,
            auto_confirm  = args.yes,
        )
    elif args.propose_multi:
        cmd_propose_multi_futures(
            scan_n        = args.scan_n,
            count         = args.count,
            side_filter   = args.side,
            auto_confirm  = args.yes,
        )
    elif args.check_positions:
        cmd_check_futures(verbose=args.verbose)
    elif args.stats_futures:
        cmd_stats_futures()


if __name__ == "__main__":
    main()
