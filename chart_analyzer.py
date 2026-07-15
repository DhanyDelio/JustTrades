"""
chart_analyzer.py — Manual Pre-Entry Chart Analysis Tool
=========================================================
READ-ONLY / INFORMATIONAL — tidak pernah menaruh order.

Usage:
    python3 chart_analyzer.py --symbol BTCUSDT
    python3 chart_analyzer.py --scan-top 20
    python3 chart_analyzer.py --symbols BTCUSDT ETHUSDT SOLUSDT

Features per symbol:
    • Auto-detect support/resistance zones (swing high/low clustering)
    • Fibonacci retracement of the most recent significant swing
    • ATR-based SL/TP suggestion (buffer beyond zone edge, not exact level)
    • Candlestick chart saved as PNG with all overlays

Dependencies:
    pip install mplfinance requests pandas numpy --break-system-packages
"""

from __future__ import annotations

import argparse
import sys
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import mplfinance as mpf
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

PUBLIC_API_URLS = [
    "https://data-api.binance.vision/api/v3",
    "https://api.binance.com/api/v3",
]

CHARTS_DIR   = Path("./charts")
LOOKBACK_DAYS = 90          # candle history untuk analisis
INTERVAL      = "4h"
SWING_N       = 5           # swing high/low: N candle di kiri & kanan
CLUSTER_PCT   = 0.01        # 1% cluster radius untuk zone grouping
MIN_SWING_PCT = 0.08        # min move 8% untuk fib swing yang valid
ATR_PERIOD    = 14
SL_ATR_BUFFER = 0.5         # SL = zone_edge ± 0.5 × ATR
TOP_ZONES     = 4           # jumlah zone terkuat yang ditampilkan (above + below)
MIN_RR        = 1.5         # scan table: filter out setups dengan R:R < threshold

# Stablecoin / fiat-pegged tokens — excluded from --scan-top auto-ranking
# These track USD or other fiat currencies and have near-zero volatility.
STABLECOIN_KEYWORDS = [
    "USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD",
    "USD1", "USDE", "PYUSD", "SUSD", "LUSD", "GUSD",
    "FRAX", "CUSD", "EURC", "EUR", "AEUR",
    "RLUSD",  # Ripple USD (pegged)
]

# Non-USD fiat-pegged tokens
FIAT_KEYWORDS = [
    "EURT",   # Tether EUR
    "GBPT",   # Tether GBP
    "CNHT",   # Tether CNH
    "XSGD",   # XSGD Singapore Dollar
    "IDRT",   # Indonesian Rupiah Token
    "BIDR",   # Binance IDR
    "BVND",   # Binance VND
    "TRYB",   # BiLira Turkish Lira
    "BRZT",   # BRZ Brazilian Real
    "JPYC",   # JPY Coin
    "CADC",   # CAD Coin
]

# Commodity / Real-World Asset (RWA) pegged tokens.
# These track gold, silver, or other commodities — fundamentally different
# volatility/behavior from crypto. Zone/ATR/RR strategy assumptions do not apply.
COMMODITY_RWA_KEYWORDS = [
    # Gold-backed
    "PAXG",   # PAX Gold (1 token = 1 troy oz gold)
    "XAUT",   # Tether Gold
    "DGLD",   # wrapped gold
    "WGOLD",  # wrapped gold variants
    "PMGT",   # Perth Mint Gold Token
    "AUX",    # Alloy by Tether (gold-backed)
    "CACHE",  # Cache Gold
    # Silver-backed
    "SLVT",   # silver token
    "AXSL",   # tokenized silver
    # Oil / energy
    "PETRO",  # Petro (oil-backed)
    # Broad RWA prefix catch
    "OUNCE",
]

# ATR threshold: symbols below this ATR% are likely pegged/wrapped assets
MIN_ATR_PCT = 0.1  # 0.1% — anything lower = probably a stablecoin/pegged asset


# ---------------------------------------------------------------------------
# 1. PUBLIC API HELPERS
# ---------------------------------------------------------------------------

def _public_get(path: str, params: dict | None = None) -> dict | list:
    """GET from Binance public market-data mirror, fallback to mainnet."""
    for base in PUBLIC_API_URLS:
        try:
            resp = requests.get(f"{base}{path}", params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException:
            continue
    raise RuntimeError(f"All public API endpoints failed for {path}")


def get_top_symbols_by_volume(n: int = 25) -> list[str]:
    """
    Return top-n USDT pairs ranked by 24h quoteVolume.
    Filters out:
    - Leveraged tokens (UP/DOWN/BULL/BEAR)
    - Stablecoin/fiat-pegged (STABLECOIN_KEYWORDS + FIAT_KEYWORDS)
    - Commodity/RWA-pegged (COMMODITY_RWA_KEYWORDS): gold, silver, oil, etc.
    - ATR proxy < MIN_ATR_PCT: catches unlisted pegged assets by behavior
    """
    tickers = _public_get("/ticker/24hr")
    filtered = []

    # Track exclusions per category for accurate log output
    skipped_stable: list[str] = []
    skipped_fiat:   list[str] = []
    skipped_rwa:    list[str] = []
    skipped_atr:    list[str] = []

    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        if any(x in sym for x in ["UP", "DOWN", "BULL", "BEAR"]):
            continue

        # ── Stablecoin / USD-pegged ──────────────────────────────────
        if any(sym.startswith(sc) for sc in STABLECOIN_KEYWORDS):
            skipped_stable.append(sym)
            continue

        # ── Non-USD fiat-pegged ──────────────────────────────────────
        if any(sym.startswith(fk) for fk in FIAT_KEYWORDS):
            skipped_fiat.append(sym)
            continue

        # ── Commodity / RWA-pegged (gold, silver, oil, etc.) ────────
        if any(sym.startswith(rwa) for rwa in COMMODITY_RWA_KEYWORDS):
            skipped_rwa.append(sym)
            continue

        if float(t["quoteVolume"]) <= 0:
            continue

        # ── ATR proxy fallback: catches unlisted pegged assets ───────
        try:
            h = float(t["highPrice"])
            l = float(t["lowPrice"])
            w = float(t["weightedAvgPrice"])
            atr_proxy_pct = (h - l) / w * 100 if w > 0 else 99
        except (ValueError, ZeroDivisionError):
            atr_proxy_pct = 99

        if atr_proxy_pct < MIN_ATR_PCT:
            skipped_atr.append(f"{sym} ({atr_proxy_pct:.3f}%)")
            continue

        filtered.append(t)

    # ── Log exclusions per category ──────────────────────────────────
    if skipped_stable:
        print(f"  [Filter] Excluded {len(skipped_stable)} stablecoin/USD-pegged: "
              f"{', '.join(skipped_stable[:8])}"
              f"{'…' if len(skipped_stable) > 8 else ''}")
    if skipped_fiat:
        print(f"  [Filter] Excluded {len(skipped_fiat)} non-USD fiat-pegged: "
              f"{', '.join(skipped_fiat)}")
    if skipped_rwa:
        print(f"  [Filter] Excluded {len(skipped_rwa)} commodity/RWA-pegged: "
              f"{', '.join(skipped_rwa)}")
    if skipped_atr:
        print(f"  [Filter] Excluded {len(skipped_atr)} low-ATR pegged (heuristic): "
              f"{', '.join(skipped_atr[:8])}"
              f"{'…' if len(skipped_atr) > 8 else ''}")

    filtered.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in filtered[:n]]


def fetch_klines_api(symbol: str, interval: str = INTERVAL,
                     limit: int = 540) -> pd.DataFrame:
    """
    Fetch recent OHLCV candles via public REST (no auth).
    limit=540 ≈ 90 days of 4h candles.
    """
    raw = _public_get("/klines", params={
        "symbol": symbol, "interval": interval, "limit": limit
    })
    col_names = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "tbbase", "tbquote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=col_names)
    df["open_time"] = pd.to_datetime(df["open_time"].astype(np.int64), unit="ms")
    df = df.set_index("open_time")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df.index.name = "Date"
    return df[["open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# 2. SUPPORT / RESISTANCE ZONES
# ---------------------------------------------------------------------------

def find_swing_points(df: pd.DataFrame, n: int = SWING_N) -> tuple[list[float], list[float]]:
    """
    Return (swing_highs, swing_lows) as lists of price levels.
    A candle is a swing high if its high > all highs in the N candles
    before and after it. Same logic inverted for swing lows.
    """
    highs = df["high"].values
    lows  = df["low"].values
    swing_highs, swing_lows = [], []

    for i in range(n, len(df) - n):
        window_h = np.concatenate([highs[i-n:i], highs[i+1:i+n+1]])
        if highs[i] > window_h.max():
            swing_highs.append(float(highs[i]))

        window_l = np.concatenate([lows[i-n:i], lows[i+1:i+n+1]])
        if lows[i] < window_l.min():
            swing_lows.append(float(lows[i]))

    return swing_highs, swing_lows


def cluster_zones(levels: list[float], cluster_pct: float = CLUSTER_PCT) -> list[dict]:
    """
    Group nearby price levels into zones.
    Returns list of dicts: {center, low, high, touches}
    sorted by touch count descending.
    """
    if not levels:
        return []

    sorted_lvl = sorted(levels)
    zones: list[dict] = []

    for price in sorted_lvl:
        placed = False
        for zone in zones:
            if abs(price - zone["center"]) / zone["center"] <= cluster_pct:
                zone["prices"].append(price)
                zone["center"] = float(np.mean(zone["prices"]))
                zone["low"]    = min(zone["prices"]) * (1 - cluster_pct / 2)
                zone["high"]   = max(zone["prices"]) * (1 + cluster_pct / 2)
                zone["touches"] += 1
                placed = True
                break
        if not placed:
            zones.append({
                "center":  price,
                "low":     price * (1 - cluster_pct / 2),
                "high":    price * (1 + cluster_pct / 2),
                "prices":  [price],
                "touches": 1,
            })

    zones.sort(key=lambda z: z["touches"], reverse=True)
    return zones


def get_sr_zones(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """
    Detect and cluster all S/R zones.
    Returns (resistance_zones, support_zones) each sorted by touch count,
    filtered to above/below current price respectively.
    """
    current_price = float(df["close"].iloc[-1])
    sh, sl = find_swing_points(df)

    all_levels = sh + sl
    zones = cluster_zones(all_levels)

    resistance = sorted(
        [z for z in zones if z["center"] > current_price],
        key=lambda z: z["center"]
    )
    support = sorted(
        [z for z in zones if z["center"] < current_price],
        key=lambda z: z["center"],
        reverse=True
    )
    return resistance, support


# ---------------------------------------------------------------------------
# 3. FIBONACCI RETRACEMENT
# ---------------------------------------------------------------------------

FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_LABELS = ["0%", "23.6%", "38.2%", "50%", "61.8%", "78.6%", "100%"]


def find_significant_swing(df: pd.DataFrame,
                            min_move_pct: float = MIN_SWING_PCT) -> dict | None:
    """
    Find the most recent high-to-low or low-to-high move that exceeds
    min_move_pct. Scans from the most recent candle backwards.
    Returns dict with keys: swing_high, swing_low, direction ('up'|'down'),
    high_idx, low_idx.
    """
    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)

    best = None
    best_pct = min_move_pct

    # Look at all pairs (i, j) where i < j, find largest move
    # For speed, only check swing points rather than all pairs
    sh, sl = find_swing_points(df, n=SWING_N)
    all_pts = (
        [(float(h), "high", i) for i, h in enumerate(highs) if h in sh] +
        [(float(l), "low",  i) for i, l in enumerate(lows)  if l in sl]
    )
    # Take last 30 swing points to keep it recent
    all_pts_sorted = sorted(all_pts, key=lambda x: x[2])[-30:]

    for ai in range(len(all_pts_sorted) - 1, 0, -1):
        pa, ta, ia = all_pts_sorted[ai]
        for bi in range(ai - 1, -1, -1):
            pb, tb, ib = all_pts_sorted[bi]
            if ta == tb:
                continue
            move = abs(pa - pb) / min(pa, pb)
            if move > best_pct:
                best_pct = move
                if pa > pb:
                    best = {"swing_high": pa, "swing_low": pb,
                            "direction": "down", "high_idx": ia, "low_idx": ib}
                else:
                    best = {"swing_high": pa, "swing_low": pb,
                            "direction": "up", "high_idx": ib, "low_idx": ia}
                break  # take most recent pair that qualifies
        if best:
            break

    return best


def compute_fib_levels(swing: dict) -> list[dict]:
    """Return list of {ratio, label, price} for each fib level."""
    sh, sl = swing["swing_high"], swing["swing_low"]
    levels = []
    for ratio, label in zip(FIB_RATIOS, FIB_LABELS):
        # Fib levels go from low to high regardless of swing direction
        price = sl + (1 - ratio) * (sh - sl) if swing["direction"] == "down" \
                else sh - (1 - ratio) * (sh - sl)
        levels.append({"ratio": ratio, "label": label, "price": float(price)})
    return levels


# ---------------------------------------------------------------------------
# 4. ATR + SL/TP SUGGESTION
# ---------------------------------------------------------------------------

def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def suggest_sl_tp(
    current_price: float,
    atr: float,
    support_zones: list[dict],
    resistance_zones: list[dict],
    fib_levels: list[dict],
    min_rr: float = MIN_RR,
) -> dict:
    """
    Two-tier TP selection:

    TIER 1 — S/R zones, sorted by touch count DESC (most-tested first),
             then by distance if tied. These are empirically-validated levels.
    TIER 2 — Fib retracement levels, sorted by distance (nearest first).
             Only tried if NO Tier-1 zone clears min_rr. These are
             math-derived and untested as real S/R in this dataset.

    Selection per direction:
      1. Iterate Tier 1: pick first zone whose R:R >= min_rr
      2. If none found, iterate Tier 2: pick first fib whose R:R >= min_rr
      3. If still none, fallback = nearest Tier-1 zone, flag ⚠

    'candidates' list (for debug display) preserves all evaluated levels with
    their tier label so callers can see exactly what was checked.
    """
    result: dict = {}

    # ── ATR-based distance cap ─────────────────────────────────────────
    # A TP is only eligible if it's within MAX_TP_ATR_MULTIPLE × ATR.
    # This scales with each coin's actual volatility — a 4× ATR move is
    # roughly the upper bound of what's achievable in a ~2-day swing hold
    # (12 × 4h candles). Replaces the old flat 15% cap.
    MAX_TP_ATR_MULTIPLE: float = 4.0
    atr_cap_abs = MAX_TP_ATR_MULTIPLE * atr   # absolute price distance cap

    def _atr_dist(tp: float) -> float:
        """Distance from current price in ATR multiples."""
        return abs(tp - current_price) / atr if atr > 0 else 0.0

    def _within_cap(tp: float) -> bool:
        return abs(tp - current_price) <= atr_cap_abs

    def _tier1_long() -> list[dict]:
        """Resistance zones above price, within ATR cap, T1 sort: touches DESC then dist ASC."""
        zones = [z for z in resistance_zones
                 if z["center"] > current_price and _within_cap(z["center"])]
        return sorted(zones, key=lambda z: (-z["touches"], z["center"]))

    def _tier1_short() -> list[dict]:
        """Support zones below price, within ATR cap, T1 sort: touches DESC then dist ASC."""
        zones = [z for z in support_zones
                 if z["center"] < current_price and _within_cap(z["center"])]
        return sorted(zones, key=lambda z: (-z["touches"], -z["center"]))

    def _tier2_long() -> list[dict]:
        """Fib levels above price, within ATR cap, nearest first."""
        return sorted(
            [fl for fl in fib_levels
             if fl["price"] > current_price and _within_cap(fl["price"])],
            key=lambda fl: fl["price"],
        )

    def _tier2_short() -> list[dict]:
        """Fib levels below price, within ATR cap, nearest first (desc)."""
        return sorted(
            [fl for fl in fib_levels
             if fl["price"] < current_price and _within_cap(fl["price"])],
            key=lambda fl: -fl["price"],
        )

    def _run_selection(
        t1_zones: list[dict],
        t2_fibs:  list[dict],
        risk:     float | None,
        direction: str,
    ) -> dict:
        """
        Two-tier selection within ATR-capped candidate pool.
        Each candidate carries atr_dist so the debug display can show
        how many ATR multiples away each level is.
        If the capped pool is empty, returns no-realistic-tp result.
        """
        candidates: list[dict] = []

        # ── Tier 1: S/R zones ─────────────────────────────────────────
        tier1_winner = None
        for z in t1_zones:
            tp      = z["center"]
            rr      = (abs(tp - current_price) / risk) if (risk and risk > 0) else None
            ad      = _atr_dist(tp)
            lbl     = f"Zone {z['touches']}×"
            ev      = {"tp": tp, "rr": rr, "tier": "T1", "label": lbl, "atr_dist": ad}
            candidates.append(ev)
            if tier1_winner is None and rr and rr >= min_rr:
                tier1_winner = ev

        # ── Tier 2: Fib levels (only if T1 failed) ────────────────────
        tier2_winner = None
        for fl in t2_fibs:
            tp      = fl["price"]
            rr      = (abs(tp - current_price) / risk) if (risk and risk > 0) else None
            ad      = _atr_dist(tp)
            lbl     = f"Fib {fl['label']}"
            ev      = {"tp": tp, "rr": rr, "tier": "T2", "label": lbl, "atr_dist": ad}
            candidates.append(ev)
            if tier1_winner is None and tier2_winner is None and rr and rr >= min_rr:
                tier2_winner = ev

        # ── Pick ───────────────────────────────────────────────────────
        winner = tier1_winner or tier2_winner

        if not candidates:
            # No zone or fib within ATR cap at all
            return {
                "picked_tp":      None,
                "picked_rr":      None,
                "rr_clears":      False,
                "tier_used":      "none",
                "tp2":            None,
                "candidates":     [],
                "no_tp_in_range": True,
            }

        if winner:
            picked_tp = winner["tp"]
            picked_rr = winner["rr"]
            rr_clears = True
            tier_used = winner["tier"]
        else:
            # Candidates exist within cap but none clear MIN_RR
            fallback  = candidates[0]
            picked_tp = fallback["tp"]
            picked_rr = fallback["rr"]
            rr_clears = False
            tier_used = fallback["tier"]

        tp2 = next(
            (e["tp"] for e in candidates if e["tp"] != picked_tp), None
        )

        return {
            "picked_tp":      picked_tp,
            "picked_rr":      picked_rr,
            "rr_clears":      rr_clears,
            "tier_used":      tier_used,
            "tp2":            tp2,
            "candidates":     candidates,
            "no_tp_in_range": False,
        }

    # ── LONG ──────────────────────────────────────────────────────────
    long_sl   = (support_zones[0]["low"] - SL_ATR_BUFFER * atr) if support_zones else None
    long_risk = (current_price - long_sl) if long_sl else None

    long_sel = _run_selection(
        _tier1_long(), _tier2_long(), long_risk, "long"
    )

    result["long"] = {
        "sl":             long_sl,
        "tp":             [t for t in [long_sel["picked_tp"], long_sel["tp2"]] if t is not None],
        "risk_pct":       long_risk / current_price * 100 if long_risk else None,
        "rr":             long_sel["picked_rr"],
        "rr_clears":      long_sel["rr_clears"],
        "tier_used":      long_sel["tier_used"],
        "candidates":     long_sel["candidates"],
        "no_tp_in_range": long_sel["no_tp_in_range"],
        "atr_cap":        MAX_TP_ATR_MULTIPLE,
    }

    # ── SHORT ─────────────────────────────────────────────────────────
    short_sl   = (resistance_zones[0]["high"] + SL_ATR_BUFFER * atr) if resistance_zones else None
    short_risk = (short_sl - current_price) if short_sl else None

    short_sel = _run_selection(
        _tier1_short(), _tier2_short(), short_risk, "short"
    )

    result["short"] = {
        "sl":             short_sl,
        "tp":             [t for t in [short_sel["picked_tp"], short_sel["tp2"]] if t is not None],
        "risk_pct":       short_risk / current_price * 100 if short_risk else None,
        "rr":             short_sel["picked_rr"],
        "rr_clears":      short_sel["rr_clears"],
        "tier_used":      short_sel["tier_used"],
        "candidates":     short_sel["candidates"],
        "no_tp_in_range": short_sel["no_tp_in_range"],
        "atr_cap":        MAX_TP_ATR_MULTIPLE,
    }

    return result


# ---------------------------------------------------------------------------
# 5. CHARTING
# ---------------------------------------------------------------------------

def plot_chart(
    df: pd.DataFrame,
    symbol: str,
    resistance_zones: list[dict],
    support_zones: list[dict],
    fib_levels: list[dict],
    sl_tp: dict,
    save_path: Path,
) -> None:
    """
    Render candlestick chart with color-coded overlays:
      GREEN bands/lines = support zones + LONG SL/TP
      RED   bands/lines = resistance zones + SHORT SL/TP
      Legend in top-left corner confirms the color scheme.
    """
    current_price = float(df["close"].iloc[-1])
    plot_df = df.tail(120).copy()

    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        title=f"{symbol}  |  {INTERVAL}  |  Current: {current_price:,.4f}",
        ylabel="Price",
        volume=True,
        figsize=(16, 9),
        returnfig=True,
        tight_layout=True,
    )
    ax = axes[0]
    price_min = plot_df["low"].min()
    price_max = plot_df["high"].max()
    n_candles = len(plot_df)

    # Helper: clamp check
    def in_range(p, lo_factor=0.88, hi_factor=1.12):
        return price_min * lo_factor <= p <= price_max * hi_factor

    # ── Support zones — GREEN bands (LONG entry candidates) ──────────────
    for z in support_zones[:TOP_ZONES]:
        if in_range(z["center"]):
            ax.axhspan(z["low"], z["high"], alpha=0.18, color="#00c853", zorder=1)
            ax.axhline(z["center"], color="#00c853", linewidth=1.0,
                       linestyle="--", alpha=0.7, zorder=2)
            ax.text(n_candles * 0.01, z["center"],
                    f"S {z['center']:,.2f}  ({z['touches']}×)",
                    fontsize=7.5, color="#69f0ae", va="bottom", fontweight="bold")

    # ── Resistance zones — RED bands (SHORT entry candidates) ────────────
    for z in resistance_zones[:TOP_ZONES]:
        if in_range(z["center"]):
            ax.axhspan(z["low"], z["high"], alpha=0.18, color="#d50000", zorder=1)
            ax.axhline(z["center"], color="#ff5252", linewidth=1.0,
                       linestyle="--", alpha=0.7, zorder=2)
            ax.text(n_candles * 0.01, z["center"],
                    f"R {z['center']:,.2f}  ({z['touches']}×)",
                    fontsize=7.5, color="#ff5252", va="bottom", fontweight="bold")

    # ── Fibonacci levels ─────────────────────────────────────────────────
    fib_colors = {
        "23.6%": "#ce93d8", "38.2%": "#64b5f6",
        "50%":   "#ffb74d", "61.8%": "#ef9a9a", "78.6%": "#b71c1c",
    }
    for fl in fib_levels:
        if fl["label"] in ("0%", "100%"):
            continue
        p = fl["price"]
        if in_range(p):
            color = fib_colors.get(fl["label"], "#90a4ae")
            ax.axhline(p, color=color, linewidth=0.9, linestyle=":",
                       alpha=0.85, zorder=2)
            ax.text(n_candles * 0.52, p,
                    f"Fib {fl['label']}  {p:,.2f}",
                    fontsize=7, color=color, va="bottom")

    # ── Current price ─────────────────────────────────────────────────────
    ax.axhline(current_price, color="#ffffff", linewidth=0.8,
               linestyle="-", alpha=0.4, zorder=2)

    # ── LONG SL/TP — GREEN-tinted lines ──────────────────────────────────
    long  = sl_tp.get("long",  {})
    short = sl_tp.get("short", {})

    if long.get("sl") and in_range(long["sl"]):
        ax.axhline(long["sl"], color="#ff6b6b", linewidth=2.0,
                   linestyle="--", zorder=4)
        rr = f"  R:R {long['rr']:.1f}:1" if long.get("rr") else ""
        ax.text(n_candles * 0.72, long["sl"],
                f"▼ SL-L  {long['sl']:,.2f}{rr}",
                fontsize=8, color="#ff6b6b", va="top", fontweight="bold")

    long_tp_colors = ["#00e676", "#69f0ae"]
    for ti, tp_price in enumerate(long.get("tp", [])):
        if in_range(tp_price):
            c = long_tp_colors[ti]
            ax.axhline(tp_price, color=c, linewidth=2.0, linestyle="--", zorder=4)
            ax.text(n_candles * 0.72, tp_price,
                    f"▲ TP{ti+1}-L  {tp_price:,.2f}",
                    fontsize=8, color=c, va="bottom", fontweight="bold")

    # ── SHORT SL/TP — RED-tinted lines ───────────────────────────────────
    if short.get("sl") and in_range(short["sl"]):
        ax.axhline(short["sl"], color="#ff1744", linewidth=2.0,
                   linestyle="-.", zorder=4)
        rr = f"  R:R {short['rr']:.1f}:1" if short.get("rr") else ""
        ax.text(n_candles * 0.72, short["sl"],
                f"▲ SL-S  {short['sl']:,.2f}{rr}",
                fontsize=8, color="#ff1744", va="bottom", fontweight="bold")

    short_tp_colors = ["#40c4ff", "#80d8ff"]
    for ti, tp_price in enumerate(short.get("tp", [])):
        if in_range(tp_price):
            c = short_tp_colors[ti]
            ax.axhline(tp_price, color=c, linewidth=2.0, linestyle="-.", zorder=4)
            ax.text(n_candles * 0.72, tp_price,
                    f"▼ TP{ti+1}-S  {tp_price:,.2f}",
                    fontsize=8, color=c, va="top", fontweight="bold")

    # ── Legend ────────────────────────────────────────────────────────────
    legend_handles = [
        mpatches.Patch(facecolor="#00c853", alpha=0.6,
                       label="Support zone  (green = LONG entry area)"),
        mpatches.Patch(facecolor="#d50000", alpha=0.6,
                       label="Resistance zone  (red = SHORT entry area)"),
        mpatches.Patch(facecolor="#00e676", alpha=0.9,
                       label="LONG SL/TP  (green lines, dashed --)"),
        mpatches.Patch(facecolor="#ff1744", alpha=0.9,
                       label="SHORT SL/TP  (red lines, dash-dot -.)"),
        mpatches.Patch(facecolor="#ffb74d", alpha=0.9,
                       label="Fibonacci levels  (dotted)"),
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper left",
        fontsize=7.5,
        framealpha=0.65,
        facecolor="#1e1e2e",
        edgecolor="#555555",
        labelcolor="white",
    )

    CHARTS_DIR.mkdir(exist_ok=True)
    fig.savefig(save_path, dpi=150, bbox_inches="tight", facecolor="#131722")
    plt.close(fig)
    print(f"  📊 Chart saved: {save_path}")


# ---------------------------------------------------------------------------
# 6. CORE ANALYSIS FUNCTION
# ---------------------------------------------------------------------------

def analyze_symbol(symbol: str, save_chart: bool = True) -> dict | None:
    """
    Full analysis for one symbol. Returns a result dict with all findings.
    Returns None if fetch fails.
    """
    print(f"\n{'─'*55}")
    print(f"Analyzing: {symbol}")
    print(f"{'─'*55}")

    # Fetch OHLCV
    try:
        df = fetch_klines_api(symbol, INTERVAL, limit=540)
    except Exception as e:
        print(f"  ❌ Failed to fetch {symbol}: {e}")
        return None

    if len(df) < 50:
        print(f"  ❌ Not enough candles for {symbol}: {len(df)}")
        return None

    current_price = float(df["close"].iloc[-1])
    atr           = compute_atr(df)

    # S/R zones
    resistance_zones, support_zones = get_sr_zones(df)

    # Fibonacci
    swing    = find_significant_swing(df)
    fib_levels: list[dict] = []
    if swing:
        fib_levels = compute_fib_levels(swing)
        swing_desc = (f"{swing['direction'].upper()} swing  "
                      f"{swing['swing_low']:,.4f} → {swing['swing_high']:,.4f}  "
                      f"({abs(swing['swing_high']-swing['swing_low'])/swing['swing_low']*100:.1f}%)")
    else:
        swing_desc = "No significant swing found (move < 8%)"

    # SL/TP
    sl_tp = suggest_sl_tp(
        current_price, atr,
        support_zones, resistance_zones, fib_levels,
    )

    # Nearest zone distances (for scan ranking)
    nearest_sup_dist = (
        abs(current_price - support_zones[0]["center"]) / current_price * 100
        if support_zones else None
    )
    nearest_res_dist = (
        abs(resistance_zones[0]["center"] - current_price) / current_price * 100
        if resistance_zones else None
    )
    nearest_zone_dist = min(
        d for d in [nearest_sup_dist, nearest_res_dist] if d is not None
    ) if (nearest_sup_dist or nearest_res_dist) else None

    # Text summary
    _print_text_summary(
        symbol, current_price, atr, swing_desc,
        resistance_zones, support_zones, fib_levels, sl_tp,
        nearest_sup_dist, nearest_res_dist,
    )

    # Chart
    if save_chart:
        ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        save_path = CHARTS_DIR / f"{symbol}_{ts}.png"
        try:
            plot_chart(df, symbol, resistance_zones, support_zones,
                       fib_levels, sl_tp, save_path)
        except Exception as e:
            print(f"  ⚠️  Chart render failed: {e}")
            save_path = None
    else:
        save_path = None

    return {
        "symbol":            symbol,
        "current_price":     current_price,
        "atr":               atr,
        "atr_pct":           atr / current_price * 100,
        "swing_desc":        swing_desc,
        "resistance_zones":  resistance_zones,
        "support_zones":     support_zones,
        "fib_levels":        fib_levels,
        "sl_tp":             sl_tp,
        "nearest_sup_dist":  nearest_sup_dist,
        "nearest_res_dist":  nearest_res_dist,
        "nearest_zone_dist": nearest_zone_dist,
        "chart_path":        str(save_path) if save_path else None,
    }


# ---------------------------------------------------------------------------
# 7. TEXT SUMMARY PRINTER
# ---------------------------------------------------------------------------

def _fmt_price(p: float | None, width: int = 14) -> str:
    if p is None:
        return "n/a".rjust(width)
    # Preserve enough fractional precision for low-price assets without
    # over-formatting large values or collapsing nearby prices to 0.01.
    if p == 0:
        return "0".rjust(width)

    import math
    abs_p = abs(p)
    if abs_p >= 1000:
        decimals = 2
    elif abs_p >= 1:
        decimals = 4
    elif abs_p >= 0.1:
        decimals = 5
    elif abs_p >= 0.01:
        decimals = 6
    elif abs_p >= 0.001:
        decimals = 6
    elif abs_p >= 1e-4:
        decimals = 8
    elif abs_p >= 1e-5:
        decimals = 9
    elif abs_p >= 1e-6:
        decimals = 10
    else:
        decimals = 12

    fmt = f"{p:>{width},.{decimals}f}"
    return fmt


def _print_text_summary(
    symbol, current_price, atr, swing_desc,
    resistance_zones, support_zones, fib_levels, sl_tp,
    nearest_sup_dist, nearest_res_dist,
):
    long  = sl_tp.get("long",  {})
    short = sl_tp.get("short", {})
    W = 70  # total width of the box

    # ── Header ──────────────────────────────────────────────────────────
    print(f"\n  ╔{'═'*(W-4)}╗")
    title = f"  {symbol}  |  {INTERVAL}  |  Price: {_fmt_price(current_price).strip()}  |  ATR: {atr/current_price*100:.2f}%"
    print(f"  ║  {title:<{W-6}}║")
    print(f"  ║  Fib swing: {swing_desc:<{W-16}}║")
    print(f"  ╠{'═'*(W-4)}╣")

    # ── Side-by-side zones ───────────────────────────────────────────────
    res_rows = resistance_zones[:TOP_ZONES]
    sup_rows = support_zones[:TOP_ZONES]
    col_w = (W - 6) // 2

    lh = "  RESISTANCE (RED / SHORT zones)  ↑"
    rh = "  SUPPORT (GREEN / LONG zones)  ↓"
    print(f"  ║  {'':─<{col_w-1}} ║ {'':─<{col_w-2}}║")
    print(f"  ║  {lh:<{col_w-1}} ║ {rh:<{col_w-2}}║")
    print(f"  ║  {'':─<{col_w-1}} ║ {'':─<{col_w-2}}║")

    max_rows = max(len(res_rows), len(sup_rows), 1)
    for i in range(max_rows):
        if i < len(res_rows):
            z   = res_rows[i]
            d   = (z["center"] - current_price) / current_price * 100
            lc  = f"R {_fmt_price(z['center']).strip():>12}  +{d:.2f}%  {z['touches']}×"
        else:
            lc = ""
        if i < len(sup_rows):
            z   = sup_rows[i]
            d   = (current_price - z["center"]) / current_price * 100
            rc  = f"S {_fmt_price(z['center']).strip():>12}  -{d:.2f}%  {z['touches']}×"
        else:
            rc = ""
        print(f"  ║  {lc:<{col_w-1}} ║ {rc:<{col_w-2}}║")

    # ── Fibonacci ────────────────────────────────────────────────────────
    print(f"  ╠{'═'*(W-4)}╣")
    print(f"  ║  {'FIBONACCI LEVELS':<{W-6}}║")
    print(f"  ║  {'─'*(W-6)}║")
    fib_line_parts = []
    for fl in fib_levels:
        arrow  = "↑" if fl["price"] > current_price else "↓"
        marker = "◀" if abs(fl["price"] - current_price) / current_price < 0.005 else " "
        fib_line_parts.append(
            f"{fl['label']:>6} {_fmt_price(fl['price']).strip():>12} {arrow}{marker}"
        )
    mid = (len(fib_line_parts) + 1) // 2
    for i in range(mid):
        lc = fib_line_parts[i]
        rc = fib_line_parts[i + mid] if (i + mid) < len(fib_line_parts) else ""
        print(f"  ║    {lc:<{col_w-3}} ║  {rc:<{col_w-4}}║")

    # ── Zone iteration detail ────────────────────────────────────────────
    atr_cap = long.get("atr_cap", 4.0)
    print(f"  ╠{'═'*(W-4)}╣")
    print(f"  ║  {'TP CANDIDATES  [T1=Zone  T2=Fib]  cap: ' + str(atr_cap) + '× ATR':<{W-6}}║")
    print(f"  ║  {'─'*(W-6)}║")

    l_cands = long.get("candidates", [])[:8]
    s_cands = short.get("candidates", [])[:8]
    lh3 = "  LONG  ↑ (T1 by touch count, then T2 fib)"
    rh3 = "  SHORT  ↓ (T1 by touch count, then T2 fib)"
    print(f"  ║  {lh3:<{col_w-1}} ║ {rh3:<{col_w-2}}║")

    if not l_cands:
        l_no_tp = f"  (no zone or fib within {atr_cap}× ATR)"
        print(f"  ║{l_no_tp:<{col_w+1}} ║{'':>{col_w}}║")
    if not s_cands:
        s_no_tp = f"  (no zone or fib within {atr_cap}× ATR)"

    max_c = max(len(l_cands), len(s_cands), 1)
    for i in range(max_c):
        if i < len(l_cands):
            e   = l_cands[i]
            ok  = "✓" if e["rr"] and e["rr"] >= MIN_RR else "✗"
            t   = e.get("tier", "??")
            lb  = e.get("label", "")[:11]
            ad  = e.get("atr_dist", 0.0)
            rr_s = f"{e['rr']:.2f}" if e["rr"] else " n/a"
            lc  = f"  {t} {_fmt_price(e['tp']).strip():>10}  {rr_s} {ok}  [{lb}, {ad:.1f}×ATR]"
        else:
            lc = ""
        if i < len(s_cands):
            e   = s_cands[i]
            ok  = "✓" if e["rr"] and e["rr"] >= MIN_RR else "✗"
            t   = e.get("tier", "??")
            lb  = e.get("label", "")[:11]
            ad  = e.get("atr_dist", 0.0)
            rr_s = f"{e['rr']:.2f}" if e["rr"] else " n/a"
            rc  = f"  {t} {_fmt_price(e['tp']).strip():>10}  {rr_s} {ok}  [{lb}, {ad:.1f}×ATR]"
        else:
            rc = ""
        print(f"  ║{lc:<{col_w+1}} ║{rc:<{col_w}}║")

    # ── SL/TP side-by-side ───────────────────────────────────────────────
    print(f"  ╠{'═'*(W-4)}╣")
    l_clears = long.get("rr_clears", False)
    s_clears = short.get("rr_clears", False)
    l_tier   = long.get("tier_used", "")
    s_tier   = short.get("tier_used", "")

    def _tier_label(clears, tier, no_tp):
        if no_tp:           return "⛔ no TP within ATR cap"
        if not clears:      return "⚠ no zone clears R:R"
        if tier == "T1":    return "✅ T1 zone-backed"
        if tier == "T2":    return "🟡 T2 fib fallback"
        return "✅ clears R:R"

    lh2 = f"  LONG SETUP  {_tier_label(l_clears, l_tier, long.get('no_tp_in_range', False))}"
    rh2 = f"  SHORT SETUP  {_tier_label(s_clears, s_tier, short.get('no_tp_in_range', False))}"
    print(f"  ║  {lh2:<{col_w-1}} ║ {rh2:<{col_w-2}}║")
    print(f"  ║  {'─'*(col_w-1)} ║ {'─'*(col_w-2)}║")

    l_sl = f"SL  {_fmt_price(long['sl']).strip():>12}  risk {long['risk_pct']:.2f}%" \
           if long.get("sl") else "SL  n/a"
    s_sl = f"SL  {_fmt_price(short['sl']).strip():>12}  risk {short['risk_pct']:.2f}%" \
           if short.get("sl") else "SL  n/a"
    print(f"  ║  {l_sl:<{col_w-1}} ║ {s_sl:<{col_w-2}}║")

    l_tps = long.get("tp", [])
    s_tps = short.get("tp", [])
    for ti in range(max(len(l_tps), len(s_tps))):
        lc = f"TP{ti+1} {_fmt_price(l_tps[ti]).strip():>12}" if ti < len(l_tps) else ""
        rc = f"TP{ti+1} {_fmt_price(s_tps[ti]).strip():>12}" if ti < len(s_tps) else ""
        print(f"  ║  {lc:<{col_w-1}} ║ {rc:<{col_w-2}}║")

    l_rr = f"R:R  {long['rr']:.2f}:1"  if long.get("rr")  else "R:R  n/a"
    s_rr = f"R:R  {short['rr']:.2f}:1" if short.get("rr") else "R:R  n/a"
    print(f"  ║  {l_rr:<{col_w-1}} ║ {s_rr:<{col_w-2}}║")

    # ── Footer ───────────────────────────────────────────────────────────
    print(f"  ╠{'═'*(W-4)}╣")
    sup_str = f"-{nearest_sup_dist:.2f}%" if nearest_sup_dist else "n/a"
    res_str = f"+{nearest_res_dist:.2f}%" if nearest_res_dist else "n/a"
    footer  = f"  Nearest support: {sup_str}   Nearest resistance: {res_str}  from current price"
    print(f"  ║  {footer:<{W-6}}║")
    print(f"  ╚{'═'*(W-4)}╝")


# ---------------------------------------------------------------------------
# 8. SCAN TABLE PRINTER
# ---------------------------------------------------------------------------

def print_scan_table(results: list[dict]) -> None:
    """
    Print ranked summary table for --scan-top mode.
    Stablecoins already filtered at fetch time.
    Flags setups where R:R < MIN_RR so they're visible but not buried.
    Uses two-line-per-symbol layout so it fits in a standard 80-col terminal.
    """
    valid = [r for r in results if r is not None]
    valid.sort(key=lambda r: r["nearest_zone_dist"] if r["nearest_zone_dist"] else 999)

    W = 78
    SEP = "─" * W

    print(f"\n  ╔{SEP}╗")
    print(f"  ║  {'SCAN SUMMARY — ranked by proximity to nearest S/R zone':<{W-2}}║")
    print(f"  ║  {'Stablecoins excluded  │  ⚠ = R:R below ' + str(MIN_RR) + ':1':<{W-2}}║")
    print(f"  ╠{SEP}╣")

    # Header
    h1 = f"  {'Symbol':<11}  {'Price':>13}  {'ATR%':>5}  {'NrSupp':>7}  {'NrRes':>7}"
    h2 = f"  {'Dir':<11}  {'SL':>13}  {'TP1':>13}  {'R:R':>6}"
    print(f"  ║{h1:<{W-1}}║")
    print(f"  ║{h2:<{W-1}}║")
    print(f"  ╠{SEP}╣")

    for idx, r in enumerate(valid):
        long  = r["sl_tp"].get("long",  {})
        short = r["sl_tp"].get("short", {})

        sup_s = f"-{r['nearest_sup_dist']:.2f}%" if r["nearest_sup_dist"] else "    n/a"
        res_s = f"+{r['nearest_res_dist']:.2f}%" if r["nearest_res_dist"] else "    n/a"

        # Price formatting: auto-scale decimals
        p = r["current_price"]
        pfmt = f"{p:>13,.2f}" if p >= 1 else f"{p:>13,.6f}"

        # Row 1 — symbol / price / atr / zone proximity
        row1 = (f"  {r['symbol']:<11}  {pfmt}  {r['atr_pct']:>4.2f}%  "
                f"{sup_s:>7}  {res_s:>7}")

        # SL/TP values
        l_sl  = _fmt_price(long.get("sl")).strip()[:10]   if long.get("sl")  else "n/a"
        l_tp1 = _fmt_price(long["tp"][0]).strip()[:10]    if long.get("tp")  else "n/a"
        l_rr  = long.get("rr")
        l_rrs = f"{l_rr:.1f}" if l_rr else "n/a"
        l_tier = long.get("tier_used", "")
        if long.get("no_tp_in_range", False):
            l_flag = " ⛔"   # no candidate within ATR cap
        elif not long.get("rr_clears", False):
            l_flag = " ⚠"   # candidates exist but none clear 1.5
        elif l_tier == "T2":
            l_flag = " ~"   # clears via fib fallback only
        else:
            l_flag = "  "   # clean T1 zone-backed

        s_sl  = _fmt_price(short.get("sl")).strip()[:10]  if short.get("sl") else "n/a"
        s_tp1 = _fmt_price(short["tp"][0]).strip()[:10]   if short.get("tp") else "n/a"
        s_rr  = short.get("rr")
        s_rrs = f"{s_rr:.1f}" if s_rr else "n/a"
        s_tier = short.get("tier_used", "")
        if short.get("no_tp_in_range", False):
            s_flag = " ⛔"
        elif not short.get("rr_clears", False):
            s_flag = " ⚠"
        elif s_tier == "T2":
            s_flag = " ~"
        else:
            s_flag = "  "

        row2 = (f"  {'LONG ':<11}  {l_sl:>13}  {l_tp1:>13}  {l_rrs:>4}{l_flag}")
        row3 = (f"  {'SHORT':<11}  {s_sl:>13}  {s_tp1:>13}  {s_rrs:>4}{s_flag}")

        print(f"  ║{row1:<{W-1}}║")
        print(f"  ║{row2:<{W-1}}║")
        print(f"  ║{row3:<{W-1}}║")
        if idx < len(valid) - 1:
            print(f"  ║{'·'*W}║")

    print(f"  ╠{SEP}╣")
    note1 = f"  Total: {len(valid)} symbols  │  ⛔=no TP in {MIN_RR}×ATR range  ⚠=no zone clears R:R  ~=fib-only (T2)  (none)=T1 zone"
    note2 = "  READ-ONLY — not a trade signal. Apply your own judgment before entering."
    print(f"  ║{note1:<{W-1}}║")
    print(f"  ║{note2:<{W-1}}║")
    print(f"  ╚{SEP}╝")


# ---------------------------------------------------------------------------
# 9. CLI ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Manual pre-entry chart analysis tool — READ-ONLY, no orders placed."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol",    type=str,
                       help="Analyze a single symbol, e.g. BTCUSDT")
    group.add_argument("--symbols",   nargs="+",
                       help="Analyze multiple symbols, e.g. BTCUSDT ETHUSDT")
    group.add_argument("--scan-top",  type=int, metavar="N",
                       help="Scan top N symbols by 24h volume")

    parser.add_argument("--no-chart", action="store_true",
                        help="Skip chart generation (text summary only)")
    parser.add_argument("--swing-n",  type=int, default=SWING_N,
                        help=f"Swing detection window (default: {SWING_N})")
    args = parser.parse_args()

    save_chart = not args.no_chart

    print("=" * 55)
    print("Chart Analyzer — Manual Pre-Entry Decision Support")
    print("READ-ONLY. No orders will be placed.")
    print("=" * 55)

    # Resolve symbol list
    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:  # --scan-top N
        print(f"\nFetching top {args.scan_top} symbols by 24h volume...")
        try:
            symbols = get_top_symbols_by_volume(args.scan_top)
            print(f"Symbols: {symbols}")
        except Exception as e:
            print(f"❌ Failed to fetch top symbols: {e}")
            sys.exit(1)

    # Run analysis
    results = []
    for sym in symbols:
        result = analyze_symbol(sym, save_chart=save_chart)
        results.append(result)

    # Scan table if multiple symbols
    if len(symbols) > 1:
        print_scan_table(results)
    else:
        if results[0] and results[0].get("chart_path"):
            print(f"\nChart saved to: {results[0]['chart_path']}")


if __name__ == "__main__":
    main()
