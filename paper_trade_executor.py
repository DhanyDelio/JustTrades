"""
paper_trade_executor.py — Semi-Manual Paper Trading on Binance Spot Testnet
============================================================================
READ-ONLY BY DEFAULT. No order is placed without explicit 'y' confirmation.

Budget hard-cap: BUDGET_USD is respected regardless of testnet's fake balance.
Single position at a time — will not propose if a position is already open.

Usage:
    python3 paper_trade_executor.py --propose              # scan → pick best → confirm
    python3 paper_trade_executor.py --propose --scan-n 30  # scan top 30 symbols
    python3 paper_trade_executor.py --check-positions       # status of open trades

Dependencies (already installed from chart_analyzer):
    python-binance python-dotenv pandas numpy requests
"""

from __future__ import annotations

import argparse
import json
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

# Reuse chart_analyzer's analysis engine — no duplication
sys.path.insert(0, str(Path(__file__).parent))
import chart_analyzer as ca

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Budget — hard cap regardless of testnet balance
# Adjust if IDR/USD rate changes: Rp200,000 ÷ ~16,500 ≈ $12.12
BUDGET_USD: float = 12.00

# Per-trade budget for --propose-all batch mode.
# Each position is sized at this amount regardless of testnet's fake large balance,
# so individual trade behavior stays representative of real $12 capital conditions.
PER_TRADE_BUDGET: float = 12.00

# Lab (batch) starting capital for compounding lab pool (separate from single-propose simulated balance)
# Default: $240
LAB_STARTING_CAPITAL: float = 240.0
# Max fraction of budget to risk on a single trade
# At $12 total, 25% = $3 max loss per trade
RISK_FRACTION: float = 0.25

# Binance Spot min notional per order (most USDT pairs: $5, some small caps $1)
# We fetch the real exchange info per symbol, but this is the hard floor
DEFAULT_MIN_NOTIONAL: float = 5.0

# How many symbols to scan when picking a candidate
DEFAULT_SCAN_N: int = 30

# Tiered / paginated scanning for --propose-all
# Part 1 = rank 1–30, Part 2 = 31–60, Part 3 = 61–90, Part 4 = 91–120
PART_SIZE: int = 30
MAX_PARTS: int = 4                  # 4 × 30 = 120 symbols scanned max
MIN_DESIRED_CANDIDATES: int = 5     # kept for reference, no longer stops scan early
SCAN_PART_DELAY_SEC: float = 2.0    # courtesy sleep between parts (throttle handles the rest)
# Binance Spot rate limit: 6000 weight/min. Ceiling = 80% = 4800.
# Each analyze_symbol call uses ~2–5 weight units (ticker + klines).
RATE_LIMIT_WEIGHT_CEILING: int = 4800

# Trade log file
TRADE_LOG_PATH = Path("./trade_log.json")

# Limit order zone buffer: place BUY limit slightly ABOVE the support zone price
# so the order fills when price pulls back to the zone, not immediately.
# 0.15% above zone center = inside the zone band, not below it.
# This anchors entry to the tested support level, not to current price.
ZONE_ENTRY_BUFFER_PCT: float = 0.0015   # 0.15% above zone center for LONG

# Rule version — bump manually when zone/ATR/RR threshold parameters change.
# Used for performance attribution in --stats so results from different
# rule configurations don't get mixed together in analysis.
RULE_VERSION: str = "v1.0.0"

# Taker fee estimate per side (Binance Spot default 0.1%)
TAKER_FEE_PCT: float = 0.001   # 0.1%

# Dual-position threshold (based on ACTUAL exchange balance, not BUDGET_USD).
# Reasoning for $50 default:
#   - Each position needs to clear min notional ($5) + meaningful risk buffer
#   - At $50 with RISK_FRACTION=0.25 → $12.50 max loss budget per slot
#   - Two slots: $12.50 × 2 = $25 max total loss (50% of $50 — acceptable floor)
#   - The remaining $25 stays as undeployed capital providing a cushion
#   - Below $50 the per-slot budget gets too thin to be meaningful
DUAL_POSITION_MIN_BALANCE: float = 50.0

# Hard cap on concurrent positions — never go beyond this regardless of balance
MAX_CONCURRENT_POSITIONS: int = 2


# ---------------------------------------------------------------------------
# 1. TESTNET CLIENT
# ---------------------------------------------------------------------------

def get_testnet_client():
    """Connect to Binance Spot Testnet. Raises if API keys not set."""
    try:
        from binance.client import Client
    except ImportError:
        raise ImportError("pip install python-binance --break-system-packages")

    api_key    = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError(
            "API keys not found in .env\n"
            "Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET"
        )

    client = Client(api_key, api_secret, testnet=True, tld="com")
    return client


def get_actual_usdt_balance(client) -> float:
    """
    Query real free USDT on testnet — used ONLY to verify an order CAN be placed
    (sufficient funds exist). Never used for sizing or dual-position threshold.
    Testnet starts with ~100k fake USDT regardless of trade history.
    """
    try:
        bal = client.get_asset_balance(asset="USDT")
        return float(bal["free"]) if bal else 0.0
    except Exception as e:
        print(f"  [WARN] Could not fetch testnet USDT balance: {e}")
        return 9999.0   # assume sufficient if query fails


def get_simulated_balance(trades: list[dict] | None = None) -> float:
    """
    Compute simulated capital = BUDGET_USD + sum of all realized PnL
    from closed trades (TP_HIT or SL_HIT).

    This is the only number used for:
      - Single vs dual position threshold comparison
      - Per-slot budget allocation
      - Position sizing

    It intentionally ignores testnet wallet balance (which is fake/large)
    and only grows/shrinks based on real trade outcomes logged here.
    """
    if trades is None:
        trades = load_trade_log()
    closed_pnl = sum(
        t.get("realized_pnl_usd") or 0.0
        for t in trades
        if t.get("exit_status") in ("TP_HIT", "SL_HIT")
        and t.get("realized_pnl_usd") is not None
    )
    return BUDGET_USD + closed_pnl


def compute_lab_pool(trades: list[dict] | None = None) -> dict:
    """
    Compute the compounding lab capital pool used by --propose-all batches.

    Returns a dict with keys:
      - lab_capital: LAB_STARTING_CAPITAL + sum(realized_pnl_usd for resolved clustered trades)
      - deployed_capital: sum(PER_TRADE_BUDGET) for clustered OPEN trades
      - available_capital: lab_capital - deployed_capital
      - max_new_positions: floor(available_capital / PER_TRADE_BUDGET)

    Note: only trades with a non-null `correlation_cluster_id` are considered part of the
    lab/batch pool. This keeps it separate from single `--propose` simulated balance.
    """
    import math
    if trades is None:
        trades = load_trade_log()

    # Realized PnL only from resolved clustered trades
    closed_cluster_pnl = sum(
        (t.get("realized_pnl_usd") or 0.0)
        for t in trades
        if t.get("correlation_cluster_id") and t.get("exit_status") in ("TP_HIT", "SL_HIT")
    )

    lab_capital = LAB_STARTING_CAPITAL + closed_cluster_pnl

    # Deployed capital: open clustered trades
    deployed_count = sum(
        1 for t in trades
        if t.get("correlation_cluster_id") and t.get("exit_status") == "OPEN"
    )
    deployed_capital = deployed_count * PER_TRADE_BUDGET

    available_capital = lab_capital - deployed_capital
    max_new_positions = math.floor(max(0.0, available_capital) / PER_TRADE_BUDGET)

    return {
        "lab_capital": lab_capital,
        "closed_cluster_pnl": closed_cluster_pnl,
        "deployed_capital": deployed_capital,
        "available_capital": available_capital,
        "max_new_positions": int(max_new_positions),
        "deployed_count": deployed_count,
    }


# ---------------------------------------------------------------------------
# 2. EXCHANGE INFO — min notional, lot size, tick size
# ---------------------------------------------------------------------------

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
    import math
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)


def round_tick(value: float, tick: float) -> float:
    """Round price to nearest tick_size."""
    if tick <= 0:
        return value
    import math
    precision = max(0, round(-math.log10(tick)))
    return round(round(value / tick) * tick, precision)


# ---------------------------------------------------------------------------
# 3. POSITION SIZING
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 4. CANDIDATE SELECTION
# ---------------------------------------------------------------------------

def gather_candidates(scan_n: int = DEFAULT_SCAN_N) -> list[dict]:
    """
    Run chart_analyzer on top scan_n symbols.
    Extract all setups where:
      - direction is T1 zone-backed (tier_used == "T1")
      - rr_clears is True
      - no_tp_in_range is False
    Returns list of candidate dicts, sorted by SL risk% ASC (smallest risk first).
    """
    print(f"\nScanning top {scan_n} symbols for T1 zone-backed setups...")
    symbols = ca.get_top_symbols_by_volume(scan_n)
    print(f"Symbols to analyze: {symbols}\n")

    candidates: list[dict] = []

    for sym in symbols:
        # Suppress chart_analyzer's per-symbol print noise
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

            # Binance Spot with USDT budget = LONG only.
            # SHORT requires holding the base asset first — not possible
            # from a pure USDT account. Skip SHORT candidates entirely.
            if direction == "short":
                continue

            if not setup.get("rr_clears"):
                continue
            if setup.get("no_tp_in_range"):
                continue
            if setup.get("tier_used") != "T1":
                continue

            sl   = setup.get("sl")
            tp1  = setup["tp"][0] if setup.get("tp") else None
            rr   = setup.get("rr")
            risk_pct = setup.get("risk_pct")

            if not sl or not tp1 or not rr or not risk_pct:
                continue

            # Find the winning T1 zone (for display)
            winning_zone = None
            for cand in setup.get("candidates", []):
                if cand["tier"] == "T1" and cand["tp"] == tp1:
                    winning_zone = cand
                    break
            # Fallback: first T1 candidate in pool
            if not winning_zone:
                for cand in setup.get("candidates", []):
                    if cand["tier"] == "T1":
                        winning_zone = cand
                        break

            candidates.append({
                "symbol":          sym,
                "direction":       direction,
                "current_price":   current_price,
                "entry_price":     current_price,   # refined to zone price in pick_best
                "sl":              sl,
                "tp1":             tp1,
                "tp2":             setup["tp"][1] if len(setup.get("tp", [])) > 1 else None,
                "rr":              rr,
                "risk_pct":        risk_pct,
                "atr":             atr,
                "atr_pct":         atr_pct,
                "winning_zone":    winning_zone,
                "support_zones":   result.get("support_zones", []),
                "resistance_zones": result.get("resistance_zones", []),
                "nearest_sup":     result.get("nearest_sup_dist"),
                "nearest_res":     result.get("nearest_res_dist"),
            })

    # Sort: smallest SL risk% first (primary), R:R descending (tiebreaker)
    candidates.sort(key=lambda c: (c["risk_pct"], -c["rr"]))

    # Compute composite scores for display (Task 1)
    _attach_scores(candidates)

    print(f"Found {len(candidates)} T1 zone-backed candidates across {scan_n} symbols.")
    return candidates


def _attach_scores(candidates: list[dict]) -> None:
    """
    Attach display scores to each candidate (in-place).
    Scores are 0–10 each, composite is weighted average.

    - Risk Score    : lower risk% = higher score  (weight 0.5 — primary criterion)
    - Zone Strength : strongest zone touches in support_zones pool (weight 0.3)
                      Uses max(support_zones[*].touches) — available at gather time,
                      unlike entry_zone which is only set later in pick_best_candidate
    - R:R Score     : higher R:R = higher score    (weight 0.2)
    """
    if not candidates:
        return

    risk_vals  = [c["risk_pct"] for c in candidates]
    rr_vals    = [c["rr"] for c in candidates]

    # Zone strength = strongest support zone in the candidate's pool by touch count.
    # This is available at gather_candidates time (support_zones is populated).
    # We pick max touches among zones within ATR reach to match what pick_best sees.
    def _best_touches(c: dict) -> int:
        sup = c.get("support_zones", [])
        if not sup:
            return 1
        atr = c.get("atr", 1)
        cur = c.get("current_price", 1)
        min_dist = 0.5 * atr
        # Mirror the qualification filter from pick_best_candidate
        qualified = [z for z in sup
                     if z["touches"] >= 2 and (cur - z["center"]) >= min_dist]
        if qualified:
            return max(z["touches"] for z in qualified)
        # Fallback: best touches in any support zone
        return max(z["touches"] for z in sup)

    touch_vals = [_best_touches(c) for c in candidates]

    def norm_inv(v, vals):   # lower = better → invert
        lo, hi = min(vals), max(vals)
        if lo == hi:
            return 5.0   # all equal → neutral mid-score, not max
        return 10.0 * (1 - (v - lo) / (hi - lo))

    def norm(v, vals):       # higher = better
        lo, hi = min(vals), max(vals)
        if lo == hi:
            return 5.0   # all equal → neutral mid-score, not max
        return 10.0 * (v - lo) / (hi - lo)

    for i, c in enumerate(candidates):
        rs  = norm_inv(risk_vals[i],  risk_vals)
        zs  = norm(touch_vals[i], touch_vals)
        rrs = norm(rr_vals[i],    rr_vals)
        composite = 0.5 * rs + 0.3 * zs + 0.2 * rrs
        c["score_risk"]      = round(rs, 1)
        c["score_zone"]      = round(zs, 1)
        c["score_rr"]        = round(rrs, 1)
        c["score_composite"] = round(composite, 1)
        c["_touch_val"]      = touch_vals[i]   # store for display


def pick_best_candidate(
    candidates: list[dict],
    client,
    budget_for_slot: float = BUDGET_USD,
    symbol_filter: str | None = None,
) -> dict | None:
    """
    From sorted candidates, find the first one that passes exchange
    constraints (min notional, budget fit).

    budget_for_slot: how much USD is allocated to THIS slot (may differ from
                     BUDGET_USD when splitting across two positions).
    symbol_filter:   if set, only consider candidates for this symbol (--symbol override).
    """
    pool = candidates
    if symbol_filter:
        pool = [c for c in candidates if c["symbol"] == symbol_filter.upper()]
        if not pool:
            print(f"  No T1 candidates found for {symbol_filter.upper()} in today's scan.")
            return None

    for cand in pool:
        sym   = cand["symbol"]
        price = cand["current_price"]

        try:
            constraints = get_symbol_constraints(client, sym)
        except Exception as e:
            print(f"  [{sym}] Skipping — could not fetch constraints: {e}")
            continue

        # Entry price anchored to the nearest QUALIFIED support zone for LONG.
        # "Qualified" = must be at least 0.5×ATR below current price AND
        # have ≥ 2 touches (empirically tested level).
        # We skip zones that are too close to current price — those would fill
        # the limit order almost immediately, defeating the "wait for pullback" strategy.
        # If no qualified zone exists, fall back to the strongest available zone.
        if cand["direction"] == "long":
            sup_zones = cand.get("support_zones", [])
            atr       = cand["atr"]
            cur       = cand["current_price"]
            min_dist  = 0.5 * atr   # at least half an ATR below current price

            # Try: zones with ≥2 touches that are far enough below current
            qualified = [
                z for z in sup_zones
                if z["touches"] >= 2 and (cur - z["center"]) >= min_dist
            ]

            if qualified:
                # Pick the closest qualified zone (smallest distance that still passes)
                zone = min(qualified, key=lambda z: cur - z["center"])
            elif sup_zones:
                # Fallback: best touch count among all support zones
                zone = max(sup_zones, key=lambda z: z["touches"])
            else:
                zone = None

            zone_center = zone["center"] if zone else cur
            zone_low    = zone["low"]    if zone else cur
            cand["entry_zone"] = zone   # store for display

            entry = round_tick(
                zone_center * (1 + ZONE_ENTRY_BUFFER_PCT),
                constraints.get("tick_size", 0),
            )

            # CRITICAL: Recalculate SL from the ACTUAL entry_zone chosen here,
            # not from chart_analyzer's sl_tp["sl"] which uses support_zones[0].
            # If a different zone was picked for entry, the original SL could be
            # ABOVE the entry price — which is backwards for a LONG.
            atr_val   = cand["atr"]
            recalc_sl = round_tick(
                zone_low - ca.SL_ATR_BUFFER * atr_val,
                constraints.get("tick_size", 0),
            )
            cand["sl"]       = recalc_sl
            cand["risk_pct"] = (entry - recalc_sl) / entry * 100 if entry > 0 else 0

        cand["entry_price"] = entry

        # ── SAFETY ASSERTION: SL must be below entry for LONG ────────
        sl_val = cand["sl"]
        tp1_val = cand["tp1"]
        if cand["direction"] == "long":
            if not (sl_val < entry < tp1_val):
                print(
                    f"  [{sym} LONG] ⛔ SAFETY CHECK FAILED — "
                    f"SL={sl_val:.4f} entry={entry:.4f} TP1={tp1_val:.4f} "
                    f"— required: SL < entry < TP1. Skipping this candidate."
                )
                continue

        sizing = compute_position_size(
            entry_price   = entry,
            sl_price      = cand["sl"],   # always recalculated from entry_zone above
            budget_usd    = budget_for_slot,
            risk_fraction = RISK_FRACTION,
            constraints   = constraints,
        )
        cand["sizing"]           = sizing
        cand["constraints"]      = constraints
        cand["budget_for_slot"]  = budget_for_slot

        # Hard reject: min notional failure, zero qty, or notional exceeds budget
        fatal = [w for w in sizing["warnings"]
                 if "below exchange minimum" in w or "cannot size" in w
                 or "exceeds total budget" in w]
        if fatal or sizing["qty"] <= 0:
            print(f"  [{sym} {cand['direction'].upper()}] Skipped — {fatal[0] if fatal else 'qty=0'}")
            continue

        return cand   # first clean candidate wins

    return None


# ---------------------------------------------------------------------------
# 5. PROPOSAL DISPLAY
# ---------------------------------------------------------------------------

def print_proposal(cand: dict) -> None:
    """Print a clear, readable trade proposal before asking for confirmation."""
    sym       = cand["symbol"]
    direction = cand["direction"].upper()
    entry     = cand["entry_price"]
    sl        = cand["sl"]
    tp1       = cand["tp1"]
    tp2       = cand["tp2"]
    rr        = cand["rr"]
    risk_pct  = cand["risk_pct"]
    sz        = cand["sizing"]
    zone      = cand["winning_zone"]

    W = 68
    sep = "─" * W

    print(f"\n  ╔{'═'*W}╗")
    print(f"  ║  {'TRADE PROPOSAL — BINANCE SPOT TESTNET':<{W-1}}║")
    print(f"  ║  {'No order placed until you confirm with y':<{W-1}}║")
    print(f"  ╠{'═'*W}╣")

    # Header
    print(f"  ║  {'Symbol':<20} {sym:<{W-22}}║")
    print(f"  ║  {'Direction':<20} {direction:<{W-22}}║")
    print(f"  ║  {'Order type':<20} {'LIMIT (not market — waits for zone)':<{W-22}}║")

    # Zone backing
    if zone:
        zone_str = f"{zone['label']}  @  {ca._fmt_price(zone['tp']).strip()}"
    else:
        zone_str = "T1 zone-backed (see chart_analyzer for detail)"
    print(f"  ║  {'Zone backing':<20} {zone_str:<{W-22}}║")

    print(f"  ╠{'═'*W}╣")

    # Prices
    entry_zone = cand.get("entry_zone") or (cand.get("support_zones") or [None])[0]
    if entry_zone:
        dist_from_cur = (cand["current_price"] - entry_zone["center"]) / cand["current_price"] * 100
        zone_str = (f"{ca._fmt_price(entry_zone['center']).strip()}"
                    f"  ({entry_zone['touches']}× tested, -{dist_from_cur:.2f}% from current)")
        print(f"  ║  {'Entry zone':<20} {zone_str:<{W-22}}║")
        print(f"  ║  {'  → limit entry':<20} {'zone_center + 0.15% buffer':<{W-22}}║")
    print(f"  ║  {'Entry (limit)':<20} {ca._fmt_price(entry).strip():>{W-22}}║")
    print(f"  ║  {'Stop-Loss':<20} {ca._fmt_price(sl).strip():>{W-22}}║")
    print(f"  ║  {'TP1':<20} {ca._fmt_price(tp1).strip():>{W-22}}║")
    if tp2:
        print(f"  ║  {'TP2 (reference)':<20} {ca._fmt_price(tp2).strip():>{W-22}}║")
    print(f"  ║  {'R:R':<20} {rr:.2f}:1{'':{W-25}}║")
    print(f"  ║  {'SL distance':<20} {risk_pct:.2f}%{'':{W-25}}║")

    print(f"  ╠{'═'*W}╣")

    # Sizing
    bud = cand.get("budget_for_slot", BUDGET_USD)
    print(f"  ║  {'Slot budget':<20} ${bud:.2f}{'':{W-27}}║")
    print(f"  ║  {'Risk fraction':<20} {RISK_FRACTION*100:.0f}% of slot{'':{W-32}}║")
    print(f"  ║  {'Target max loss':<20} ${bud*RISK_FRACTION:.2f}{'':{W-27}}║")
    print(f"  ║  {'Position qty':<20} {sz['qty']:.8g} {sym.replace('USDT',''):<{W-30}}║")
    print(f"  ║  {'Position value':<20} ${sz['notional_usd']:.2f}{'':{W-27}}║")
    print(f"  ║  {'Max loss if SL hit':<20} ${sz['max_loss_usd']:.2f}  ({sz['max_loss_pct']:.1f}% of budget){'':{W-48}}║")

    print(f"  ╠{'═'*W}╣")

    # Warnings
    if sz["warnings"]:
        print(f"  ║  {'⚠  WARNINGS':<{W-1}}║")
        for w in sz["warnings"]:
            # Wrap long warnings
            while len(w) > W - 5:
                print(f"  ║  {'  ' + w[:W-7]:<{W-1}}║")
                w = w[W-7:]
            print(f"  ║  {'  ' + w:<{W-1}}║")
        print(f"  ╠{'═'*W}╣")

    # ATR context
    print(f"  ║  {'ATR(14)':<20} {cand['atr_pct']:.2f}%  ({ca._fmt_price(cand['atr']).strip()} price units){'':{W-52}}║")

    # Scores (Task 1)
    if cand.get("score_composite") is not None:
        print(f"  ╠{'═'*W}╣")
        rs  = cand.get('score_risk', 0)
        zs  = cand.get('score_zone', 0)
        rrs = cand.get('score_rr', 0)
        cs  = cand.get('score_composite', 0)
        score_str = (f"Risk {rs:.1f}/10  Zone {zs:.1f}/10  R:R {rrs:.1f}/10  "
                     f"→ Composite {cs:.1f}/10")
        print(f"  ║  {'Scores':<20} {score_str:<{W-22}}║")
        print(f"  ║  {'  (weights)':<20} {'Risk×0.5  Zone×0.3  R:R×0.2':<{W-22}}║")

    # ML Score (observation only — does not influence decisions)
    if cand.get("ml_score") is not None:
        print(f"  ╠{'═'*W}╣")
        ms  = cand["ml_score"]
        mv  = cand.get("ml_model_version", "v1")
        ml_str = f"{ms:.2f}  (observation only — not used for decisions)"
        print(f"  ║  {'ML Score ('+mv+')':<20} {ml_str:<{W-22}}║")

    print(f"  ╚{'═'*W}╝")

    bud = cand.get("budget_for_slot", BUDGET_USD)
    print(f"\n  Budget context:")
    print(f"    Slot budget  : ${bud:.2f}")
    print(f"    This trade   : ${sz['notional_usd']:.2f} ({sz['notional_usd']/bud*100:.0f}% of slot)")
    print(f"    Remaining    : ${bud - sz['notional_usd']:.2f} (held in USDT)")
    if sz["notional_usd"] > bud:
        print(f"    ⚠  Position value EXCEEDS slot budget — reduce RISK_FRACTION or pick a cheaper pair")


# ---------------------------------------------------------------------------
# 6. TRADE LOG
# ---------------------------------------------------------------------------

def load_trade_log() -> list[dict]:
    """Load all spot trades from Supabase trades_spot table.
    trade_log.json is kept as a backup but is no longer the source of truth.
    """
    try:
        from supabase_client import fetch_all_spot
        return fetch_all_spot()
    except Exception as e:
        print(f"  [WARN] Supabase read failed, falling back to trade_log.json: {e}")
        if TRADE_LOG_PATH.exists():
            with open(TRADE_LOG_PATH) as f:
                return json.load(f)
        return []


def _match_mode(trade: dict, mode: str) -> bool:
    """Return True if trade matches selected mode filter."""
    cid = trade.get("correlation_cluster_id")
    if mode == "all":
        return True
    if mode == "single":
        return cid is None
    if mode == "lab":
        return cid is not None
    return True


def export_clean(trades: list[dict], mode: str = "lab") -> None:
    """
    Export resolved trades for ML: filtered by mode and exit_status (TP_HIT/SL_HIT).
    Writes `trade_log_<mode>_clean.json` with only selected fields.
    """
    import csv
    out = []
    for t in trades:
        if not _match_mode(t, mode):
            continue
        if t.get("exit_status") not in ("TP_HIT", "SL_HIT"):
            continue
        out.append({
            "symbol": t.get("symbol"),
            "entry_price": t.get("entry_price"),
            "sl": t.get("sl"),
            "tp1": t.get("tp1"),
            "zone_touches": t.get("entry_zone_touches") or t.get("zone_touches"),
            "atr_pct": t.get("atr_pct_at_entry") or t.get("atr_pct"),
            "planned_rr": t.get("planned_rr"),
            "risk_pct": t.get("risk_pct"),
            "realized_pnl_usd": t.get("realized_pnl_usd"),
            "realized_pnl_pct": t.get("realized_pnl_pct"),
            "time_to_resolution_sec": t.get("time_to_resolution_sec"),
            "rule_version": t.get("rule_version"),
            "correlation_cluster_id": t.get("correlation_cluster_id"),
        })

    fname = f"trade_log_{mode}_clean.json"
    with open(fname, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Exported {len(out)} resolved trades to {fname}")


def save_trade_log(trades: list[dict]) -> None:
    # trade_log.json is no longer the write target — Supabase is.
    # Writes are handled per-record via upsert_spot() / update_spot_by_order_id()
    # in supabase_client.py.  This stub is kept so call sites compile without change
    # until each write path is individually migrated to Supabase upserts.
    pass


def has_open_position(trades: list[dict]) -> bool:
    """A position is 'open' if exit_status is OPEN (entry placed, not yet resolved)."""
    return any(t.get("exit_status") == "OPEN" for t in trades)


def should_show_live_position(trade: dict, entry_status: str | None = None) -> bool:
    """Return True for trades that should remain visible in the live display block."""
    resolved_statuses = {"TP_HIT", "SL_HIT", "CLOSED"}
    if trade.get("exit_status") in resolved_statuses:
        return False

    status = entry_status if entry_status is not None else trade.get("entry_status")
    return status in {"NEW", "PARTIALLY_FILLED", "FILLED"}


def count_open_positions(trades: list[dict]) -> int:
    """Count how many trades currently have exit_status == OPEN."""
    return sum(1 for t in trades if t.get("exit_status") == "OPEN")


def log_trade(order: dict, cand: dict,
              correlation_cluster_id: str | None = None) -> None:
    """Insert new spot trade into Supabase trades_spot table."""
    from supabase_client import upsert_spot
    ez = cand.get("entry_zone") or {}
    notional = cand["sizing"]["notional_usd"]
    record = {
        # ── Identity ──────────────────────────────────────────────────
        "symbol":            cand["symbol"],
        "direction":         cand["direction"],
        "budget_usd":        cand.get("budget_for_slot", BUDGET_USD),
        "rule_version":      RULE_VERSION,
        "correlation_cluster_id": correlation_cluster_id,

        # ── Entry order ───────────────────────────────────────────────
        "entry_order_id":    order.get("orderId"),
        "entry_client_id":   order.get("clientOrderId"),
        "entry_status":      order.get("status", "NEW"),
        "entry_price":       cand["entry_price"],
        "entry_fill_price":  None,
        "entry_fill_time":   None,
        "entry_qty":         cand["sizing"]["qty"],
        "entry_notional":    notional,
        "open_time":         datetime.now(timezone.utc).isoformat(),

        # ── OCO ───────────────────────────────────────────────────────
        "oco_placed":        False,
        "oco_order_ids":     None,
        "oco_list_id":       None,

        # ── Levels ────────────────────────────────────────────────────
        "sl":                cand["sl"],
        "tp1":               cand["tp1"],
        "tp2":               cand["tp2"],
        "entry_zone_center": ez.get("center"),
        "entry_zone_touches": ez.get("touches"),

        # ── Setup metadata ────────────────────────────────────────────
        "planned_rr":        cand["rr"],
        "risk_pct":          cand["risk_pct"],
        "max_loss_usd":      cand["sizing"]["max_loss_usd"],
        "zone_type":         cand["winning_zone"]["tier"] if cand.get("winning_zone") else "T1",
        "zone_label":        cand["winning_zone"]["label"] if cand.get("winning_zone") else None,
        "zone_touches":      ez.get("touches"),
        "atr_pct_at_entry":  cand["atr_pct"],

        # ── Cost estimates ────────────────────────────────────────────
        "fee_usd_roundtrip": round(notional * TAKER_FEE_PCT * 2, 4),
        "slippage_pct":      None,
        "time_to_resolution_sec": None,

        # ── Exit ──────────────────────────────────────────────────────
        "exit_status":       "OPEN",
        "exit_price":        None,
        "exit_time":         None,
        "realized_pnl_usd":  None,
        "realized_pnl_pct":  None,

        # ── ML scoring (observation only) ─────────────────────────────
        "ml_score":          cand.get("ml_score"),
        "ml_model_version":  cand.get("ml_model_version"),

        # ── Raw ───────────────────────────────────────────────────────
        "raw_entry_order":   order,
    }
    upsert_spot(record)
    print(f"  Trade inserted into Supabase trades_spot (order #{record['entry_order_id']})")


# ---------------------------------------------------------------------------
# 8. ORDER EXECUTION
# ---------------------------------------------------------------------------

def place_limit_order(client, cand: dict) -> dict:
    """Place entry limit BUY on Binance Spot Testnet."""
    from binance.enums import SIDE_BUY, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC
    from binance.exceptions import BinanceAPIException

    sym       = cand["symbol"]
    qty       = cand["sizing"]["qty"]
    entry     = cand["entry_price"]
    step      = cand["constraints"].get("step_size", 0)
    tick      = cand["constraints"].get("tick_size", 0)

    qty_str   = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
    price_str = f"{round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

    try:
        return client.create_order(
            symbol      = sym,
            side        = SIDE_BUY,
            type        = ORDER_TYPE_LIMIT,
            timeInForce = TIME_IN_FORCE_GTC,
            quantity    = qty_str,
            price       = price_str,
        )
    except BinanceAPIException as e:
        raise RuntimeError(f"Binance API error: {e}") from e


def place_oco_order(client, trade: dict) -> dict:
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
        info = client.get_symbol_info(sym)
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
            current = float(client.get_symbol_ticker(symbol=sym)["price"])
        except Exception as e:
            raise RuntimeError(f"Could not fetch current price for {sym}: {e}") from e

        tp1 = trade["tp1"]  # start with planned TP

        # ── Race condition: price already below SL ─────────────────────
        if current <= sl:
            # Place immediate market sell — position already at/past SL
            print(f"\n  ⚠  [{sym}] Price {current:.4f} ≤ SL {sl:.4f} at OCO placement.")
            print(f"       Placing MARKET SELL immediately to cut loss.")
            try:
                from binance.enums import SIDE_SELL, ORDER_TYPE_MARKET
                resp = client.create_order(
                    symbol   = sym,
                    side     = SIDE_SELL,
                    type     = ORDER_TYPE_MARKET,
                    quantity = qty_str,
                )
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
            resp = client.create_oco_order(
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


# ---------------------------------------------------------------------------
# 9. POSITION MONITORING
# ---------------------------------------------------------------------------

def _fmt_order_status(status: str) -> str:
    icons = {
        "NEW": "🕐 NEW (pending fill)",
        "PARTIALLY_FILLED": "🔄 PARTIALLY_FILLED",
        "FILLED": "✅ FILLED",
        "CANCELED": "❌ CANCELED",
        "REJECTED": "❌ REJECTED",
        "EXPIRED": "⏱ EXPIRED",
    }
    return icons.get(status, status)


# ---------------------------------------------------------------------------
# Telegram helper + post-resolve auto-propose
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    """Send a Telegram message using TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from .env."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    placeholders = ("your_telegram", "your", "replace_me", "placeholder", "changeme")
    if any(p in f"{token}:{chat_id}".lower() for p in placeholders):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass


def check_positions(client, verbose: bool = False, mode: str = "all") -> None:
    """
    For each trade with exit_status == OPEN:
    1. Query entry order status from exchange.
    2. If entry FILLED and no OCO yet → place OCO, update log.
    3. If OCO placed → check OCO legs for TP_HIT / SL_HIT, update log.
    4. Print grouped summary (compact by default, detailed with --verbose).
    """
    trades     = load_trade_log()
    # Filter trades according to mode (single/lab/all) for display and operations
    filtered_trades = [t for t in trades if _match_mode(t, mode)]
    open_trades = [t for t in filtered_trades if t.get("exit_status") == "OPEN"]
    log_dirty  = False

    if not open_trades:
        print("\n  No open positions in trade_log.json")
        closed = [t for t in filtered_trades if t.get("exit_status") != "OPEN"][-5:]
        if closed:
            print(f"\n  Last {len(closed)} closed trade(s):")
            for t in closed:
                pnl = f"${t['realized_pnl_usd']:+.2f}" if t.get("realized_pnl_usd") is not None else "n/a"
                hrs = f"{t['time_to_resolution_sec']//3600}h" if t.get("time_to_resolution_sec") else "n/a"
                print(f"    {t['symbol']:10} {t['direction'].upper():5} "
                      f"{t['exit_status']:15}  PnL: {pnl:>8}  held: {hrs}")
        return

    # Show lab pool status (if any clustered trades exist)
    pool = compute_lab_pool(trades)
    lab_cap = pool["lab_capital"]
    net_pnl = pool["closed_cluster_pnl"]
    deployed = pool["deployed_capital"]
    available = pool["available_capital"]
    max_new = pool["max_new_positions"]
    print(f"\n  Lab capital: ${lab_cap:.2f} (started ${LAB_STARTING_CAPITAL:.0f}, net P&L ${net_pnl:+.2f})  |  Deployed: ${deployed:.2f}  |  Available: ${available:.2f}  |  Max new positions: {max_new}")

    # ── Group by correlation_cluster_id ───────────────────────────────
    from collections import Counter, defaultdict
    clusters: dict[str, list] = defaultdict(list)
    for t in open_trades:
        cid = t.get("correlation_cluster_id") or "single"
        clusters[cid].append(t)

    dup_syms = [sym for sym, count in Counter(t["symbol"] for t in open_trades).items() if count > 1]
    if dup_syms:
        print("\n  ⚠ DUPLICATE OPEN SYMBOL(S) DETECTED — review before adding more positions:")
        for sym in dup_syms:
            entries = sorted(
                [t for t in open_trades if t["symbol"] == sym],
                key=lambda t: t.get("open_time", "")
            )
            print(f"\n    {sym}:")
            for t in entries:
                cluster_label = t.get("correlation_cluster_id") or "single --propose"
                print(f"      Order #{t['entry_order_id']:<10}  cluster={cluster_label}")
                print(f"        status={t.get('entry_status','?')}  opened={t.get('open_time','?')[:19]}")
            # Identify the older single-propose entry to suggest cancellation
            single_entries = [t for t in entries if not t.get("correlation_cluster_id")]
            if single_entries:
                stale = single_entries[0]
                print(f"\n      ⚠  Order #{stale['entry_order_id']} is a stale single --propose entry.")
                print(f"         If you no longer want it, cancel it at testnet.binance.vision,")
                print(f"         then update trade_log.json: set that entry's exit_status to 'CANCELED'.")
                print(f"         Until canceled, BOTH orders may fill — doubling your {sym} exposure.")

    n_filled   = sum(1 for t in open_trades if t.get("entry_status") == "FILLED")
    n_oco      = sum(1 for t in open_trades if t.get("oco_placed"))
    n_pending  = len(open_trades) - n_filled

    print(f"\n  ── OPEN POSITIONS: {len(open_trades)} total  "
          f"({n_pending} pending fill, {n_filled} filled, {n_oco} OCO active) ──")

    resolved_this_run = []

    for cid, group in clusters.items():
        cluster_label = f"Cluster {cid}" if cid != "single" else "Single trade"
        print(f"\n  [{cluster_label}  —  {len(group)} position(s)]")

        # Keep the compact table header for navigation, but only print the
        # per-symbol summary row in non-verbose mode.
        print(f"  {'Symbol':<10} {'Status':<22} {'PnL/Info':>12}  OCO")
        print(f"  {'─'*55}")

        # Batch-fetch current prices for this group's symbols to avoid per-symbol rate hits
        try:
            all_tickers = client.get_all_tickers()
            price_map = {t.get('symbol'): float(t.get('price')) for t in all_tickers}
        except Exception:
            price_map = {}

        for trade in group:
            sym  = trade["symbol"]
            dirn = trade["direction"].upper()
            eid  = trade.get("entry_order_id")

            # ── Step 1: Query entry order ──────────────────────────────
            try:
                entry_order  = client.get_order(symbol=sym, orderId=eid)
                entry_status = entry_order.get("status", "UNKNOWN")
            except Exception as e:
                print(f"  {sym:<10} ⚠ Could not query: {e}")
                continue

            filled_qty  = float(entry_order.get("executedQty", 0))
            actual_fill = float(entry_order.get("cummulativeQuoteQty", 0))
            # Primary: cummulativeQuoteQty / executedQty (most accurate)
            # Binance Spot Testnet often returns cummulativeQuoteQty=0 for LIMIT fills,
            # so we fall through a chain of alternatives before using the limit price.
            if filled_qty > 0 and actual_fill > 0:
                fill_price = actual_fill / filled_qty
            else:
                # Fallback 1: /api/v3/myTrades — most reliable actual fill price
                _fill_resolved = False
                try:
                    my_trades = client.get_my_trades(symbol=sym, orderId=eid, limit=5)
                    if my_trades:
                        total_qty   = sum(float(t["qty"])   for t in my_trades)
                        total_quote = sum(float(t["quoteQty"]) for t in my_trades)
                        if total_qty > 0 and total_quote > 0:
                            fill_price = total_quote / total_qty
                            _fill_resolved = True
                except Exception:
                    pass
                if not _fill_resolved:
                    # Fallback 2: limit price from the order (same as entry_price for our GTC orders)
                    fill_price = float(entry_order.get("price", trade["entry_price"]))

            if trade.get("entry_status") != entry_status:
                trade["entry_status"] = entry_status
                log_dirty = True
            if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
                trade["entry_fill_price"] = fill_price
                trade["entry_fill_time"]  = entry_order.get("updateTime")
                trade["entry_qty"]        = filled_qty
                planned = trade.get("entry_price", fill_price)
                trade["slippage_pct"] = round(
                    (fill_price - planned) / planned * 100, 4
                ) if planned else None
                log_dirty = True
                # Notify on NEW → FILLED transition
                _send_telegram(
                    f"✅ Filled: {sym} {trade.get('direction','').lower()} @ {ca._fmt_price(fill_price).strip()}"
                    f" | SL: {ca._fmt_price(trade.get('sl')).strip()}"
                    f" | TP: {ca._fmt_price(trade.get('tp1')).strip()}"
                )

            # ── Step 2: Place OCO if filled and no OCO yet ─────────────
            if entry_status == "FILLED" and not trade.get("oco_placed"):
                print(f"  {sym:<10} ✅ FILLED — placing OCO...")
                oco_resp, last_err = None, None
                for attempt in range(1, 3):
                    try:
                        oco_resp = place_oco_order(client, trade)
                        break
                    except RuntimeError as e:
                        last_err = e
                        if attempt < 2:
                            import time; time.sleep(3)

                # ── Market-sell path: place_oco_order already sold the position ──
                # trade["_market_sold"] is set by the emergency market-sell branch.
                # Update the log as SL_HIT and skip ALL further OCO logic for this trade.
                if trade.get("_market_sold"):
                    entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                    exit_px    = float(oco_resp.get("fills", [{}])[0].get("price", 0) or 0) \
                                 if oco_resp and oco_resp.get("fills") else None
                    # Fallback: use cummulativeQuoteQty / executedQty
                    if not exit_px and oco_resp:
                        exec_qty  = float(oco_resp.get("executedQty", 0) or 0)
                        cum_quote = float(oco_resp.get("cummulativeQuoteQty", 0) or 0)
                        exit_px   = cum_quote / exec_qty if exec_qty > 0 else None
                    if not exit_px:
                        exit_px = trade["sl"]   # conservative fallback
                    pnl_usd = (exit_px - entry_fill) * trade["entry_qty"]
                    pnl_pct = pnl_usd / trade.get("entry_notional", 1) * 100
                    # exit_time: use transactTime from market sell response (accurate)
                    # fallback to updateTime, then entry_fill_time + small offset
                    exit_ts = (
                        oco_resp.get("transactTime")
                        or oco_resp.get("updateTime")
                        if oco_resp else None
                    )
                    if not exit_ts:
                        exit_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                    trade["exit_status"]      = "SL_HIT"
                    trade["exit_price"]       = round(exit_px, 6)
                    trade["exit_time"]        = int(exit_ts)
                    trade["realized_pnl_usd"] = round(pnl_usd, 4)
                    trade["realized_pnl_pct"] = round(pnl_pct, 2)
                    # time_to_resolution_sec from fill to market sell
                    fill_t = trade.get("entry_fill_time")
                    if fill_t:
                        trade["time_to_resolution_sec"] = (int(exit_ts) - int(fill_t)) // 1000
                    trade["oco_placed"]       = False
                    trade["oco_list_id"]      = None
                    trade.pop("_market_sold", None)
                    log_dirty = True
                    resolved_this_run.append((sym, "SL_HIT", pnl_usd))
                    print(f"  {sym:<10} 🔴 Emergency market sell — SL_HIT logged  PnL: ${pnl_usd:+.4f}")
                    continue   # ← skip OCO placement and Step 3 entirely

                if oco_resp:
                    oco_orders = oco_resp.get("orderReports", [])
                    trade["oco_placed"]    = True
                    trade["oco_order_ids"] = [o["orderId"] for o in oco_orders]
                    trade["oco_list_id"]   = oco_resp.get("orderListId")
                    log_dirty = True
                    print(f"  {sym:<10} ✅ OCO placed  List#{trade['oco_list_id']}")
                else:
                    print(
                        f"\n  {'!'*60}\n"
                        f"  !! CRITICAL: OCO FAILED for {sym} — POSITION UNPROTECTED !!\n"
                        f"  !! Error: {str(last_err)[:48]:<50}!!\n"
                        f"  !! SL: {ca._fmt_price(trade['sl']).strip():<30} "
                        f"TP: {ca._fmt_price(trade['tp1']).strip():<20}!!\n"
                        f"  !! Fix manually at testnet.binance.vision              !!\n"
                        f"  {'!'*60}\n"
                    )
                    import atexit
                    atexit.register(lambda: sys.exit(2))

            # ── Step 3: Check OCO status ────────────────────────────────
            oco_str = "n/a"
            if trade.get("oco_placed") and trade.get("oco_list_id"):
                try:
                    oco_status  = client.v3_get_order_list(orderListId=trade["oco_list_id"])
                    list_status = oco_status.get("listOrderStatus", "UNKNOWN")
                    oco_str     = list_status

                    if list_status == "ALL_DONE":
                        for leg_ref in oco_status.get("orders", []):
                            leg = client.get_order(symbol=sym, orderId=leg_ref["orderId"])
                            if leg.get("status") == "FILLED":
                                exec_qty   = float(leg.get("executedQty", 0) or 1)
                                cum_quote  = float(leg.get("cummulativeQuoteQty", 0))
                                exit_price = cum_quote / exec_qty if exec_qty > 0 \
                                             else float(leg.get("price", 0))
                                exit_status = "SL_HIT" if "STOP" in leg.get("type","") else "TP_HIT"
                                entry_fill  = trade.get("entry_fill_price") or trade["entry_price"]
                                pnl_usd     = (exit_price - entry_fill) * trade["entry_qty"]
                                pnl_pct     = pnl_usd / trade["entry_notional"] * 100
                                trade["exit_status"]     = exit_status
                                trade["exit_price"]      = round(exit_price, 6)
                                trade["exit_time"]       = leg.get("updateTime")
                                trade["realized_pnl_usd"] = round(pnl_usd, 4)
                                trade["realized_pnl_pct"] = round(pnl_pct, 2)
                                fill_t = trade.get("entry_fill_time")
                                exit_t = leg.get("updateTime")
                                if fill_t and exit_t:
                                    trade["time_to_resolution_sec"] = (int(exit_t)-int(fill_t))//1000
                                elif exit_t and trade.get("open_time"):
                                    # Fallback: use open_time (order placed time) if fill_time missing
                                    try:
                                        open_ms = int(datetime.fromisoformat(
                                            trade["open_time"]
                                        ).timestamp() * 1000)
                                        trade["time_to_resolution_sec"] = (int(exit_t) - open_ms) // 1000
                                    except Exception:
                                        pass
                                log_dirty = True
                                resolved_this_run.append((sym, exit_status, pnl_usd))
                                oco_str = f"{'🟢' if exit_status=='TP_HIT' else '🔴'} {exit_status}"
                                break

                        if trade.get("exit_status") != "OPEN":
                            continue
                except Exception as e:
                    oco_str = f"⚠ {e}"

            if not should_show_live_position(trade, entry_status):
                continue

            # ── Step 4: Compact line + optional verbose card ────────────
            status_str = _fmt_order_status(entry_status)[:20]
            try:
                current = price_map.get(sym)
                if current is None:
                    current = float(client.get_symbol_ticker(symbol=sym)["price"])
            except Exception:
                current = None

            # ── Step 3.5: Price-guard — catch SL breaches testnet missed ─
            # Spot is always LONG — SL is below entry price.
            if (entry_status == "FILLED"
                    and trade.get("exit_status") == "OPEN"
                    and current is not None
                    and trade.get("oco_placed")):
                sl = trade.get("sl")
                if sl and current <= sl:
                    print(f"  ⚠  [{sym}] Price {current:.4f} breached SL {sl:.4f} "
                          f"— OCO may have failed. Resolving as SL_HIT.")
                    entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                    qty        = trade.get("entry_qty", 0)
                    pnl_usd    = qty * (current - entry_fill)
                    pnl_pct    = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                    exit_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                    trade["exit_status"]      = "SL_HIT"
                    trade["exit_price"]       = round(current, 6)
                    trade["exit_time"]        = exit_time_ms
                    trade["realized_pnl_usd"] = round(pnl_usd, 4)
                    trade["realized_pnl_pct"] = round(pnl_pct, 2)
                    if trade.get("entry_fill_time") and exit_time_ms:
                        trade["time_to_resolution_sec"] = (exit_time_ms - int(trade["entry_fill_time"])) // 1000
                    log_dirty = True
                    resolved_this_run.append((sym, "SL_HIT", pnl_usd))
                    _send_telegram(
                        f"🛑 [SPOT] SL_HIT (price-guard): {sym} @ {ca._fmt_price(current).strip()}"
                        f"  |  PnL: ${pnl_usd:+.2f}"
                    )
                    continue

            # Compact view: show PnL only for filled positions;
            # for pending orders show distance to entry instead (more useful than "n/a")
            pnl_display = "n/a"
            if entry_status == "FILLED" and trade.get("entry_qty", 0) > 0 and current is not None:
                ref_price = trade.get("entry_fill_price") or trade["entry_price"]
                qty = trade.get("entry_qty", 0)
                pnl_usd = qty * (current - ref_price)
                pnl_display = f"${pnl_usd:+.3f}"
            elif entry_status in ("NEW", "PARTIALLY_FILLED") and current is not None:
                entry_limit = trade.get("entry_price")
                if entry_limit and current:
                    dist_pct = (entry_limit - current) / current * 100
                    # Positive dist_pct = entry is above current (limit BUY waiting for pullback)
                    pnl_display = f"{dist_pct:+.2f}% fill"

            if not verbose:
                print(f"  {sym:<10} {status_str:<22} {pnl_display:>12}  {oco_str}")

            if verbose:
                # Verbose: spacious, aligned info card
                sym_hdr = f"{sym}  {dirn}"
                entry_price = trade.get("entry_price")
                entry_fill = trade.get("entry_fill_price")
                sl = trade.get("sl")
                tp = trade.get("tp1")
                qty = trade.get("entry_qty") or 0

                cur_str = ca._fmt_price(current, width=14).strip() if current is not None else "n/a"
                entry_str = ca._fmt_price(entry_fill or entry_price, width=14).strip() if (entry_fill or entry_price) is not None else "n/a"
                sl_str = ca._fmt_price(sl, width=14).strip() if sl is not None else "n/a"
                tp_str = ca._fmt_price(tp, width=14).strip() if tp is not None else "n/a"

                def pct(a, b):
                    try:
                        return (a - b) / b * 100
                    except Exception:
                        return None

                if current is not None:
                    pct_to_entry = pct(entry_price, current)
                    pct_sl = pct(sl, current) if sl else None
                    pct_tp = pct(tp, current) if tp else None
                else:
                    pct_to_entry = pct_sl = pct_tp = None

                W = 78
                print("\n  " + "╔" + "═" * (W - 2) + "╗")
                print(f"  ║ {sym_hdr:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")
                # Conditional content based on order status
                if entry_status in ("NEW", "PARTIALLY_FILLED"):
                    # Show only the 'to fill' line
                    price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                    if pct_to_entry is not None:
                        price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                    print(f"  ║ {price_line:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                elif entry_status == "FILLED":
                    # Show Entry + Current, then SL / TP distances
                    entrycur_line = f"Entry: {entry_str:>12}   |  Current: {cur_str:>12}"
                    # Fix 3: warn if current price equals entry_fill_price after FILLED —
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
                    sltp_line = f"SL: {sl_str:>12}"
                    if pct_sl is not None:
                        sltp_line += f" ({pct_sl:+.2f}%)"
                    sltp_line = sltp_line.ljust(38)
                    sltp_line += f"  |  TP: {tp_str:>12}"
                    if pct_tp is not None:
                        sltp_line += f" ({pct_tp:+.2f}%)"
                    print(f"  ║ {sltp_line:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                else:
                    # Fallback: show both lines
                    price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                    if pct_to_entry is not None:
                        price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                    print(f"  ║ {price_line:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                    sltp_line = f"SL: {sl_str:>12}"
                    if pct_sl is not None:
                        sltp_line += f" ({pct_sl:+.2f}%)"
                    sltp_line = sltp_line.ljust(38)
                    sltp_line += f"  |  TP: {tp_str:>12}"
                    if pct_tp is not None:
                        sltp_line += f" ({pct_tp:+.2f}%)"
                    print(f"  ║ {sltp_line:<{W-4}} ║")
                print(f"  ║{'':{W-2}}║")
                status_line = f"Status: {_fmt_order_status(entry_status)}"
                if entry_status == 'FILLED' and qty:
                    r_pnl = trade.get('realized_pnl_usd')
                    if r_pnl is not None:
                        status_line += f"  |  Realized: ${r_pnl:+.4f}"
                    if current is not None and qty:
                        ref = trade.get('entry_fill_price') or trade.get('entry_price')
                        unreal = qty * (current - ref)
                        status_line += f"  |  Unreal: ${unreal:+.3f}"
                if trade.get('oco_list_id'):
                    status_line += f"  |  OCO List: {trade['oco_list_id']}"
                print(f"  ║ {status_line:<{W-4}} ║")
                print("  " + "╚" + "═" * (W - 2) + "╝\n")

    # ── Summary of what changed this run ───────────────────────────────
    if resolved_this_run:
        print(f"\n  ── Resolved this run: {len(resolved_this_run)} trade(s) ──")
        for sym, status, pnl in resolved_this_run:
            icon = "🟢" if status == "TP_HIT" else "🔴"
            print(f"    {icon} {sym}  {status}  PnL: ${pnl:+.4f}")

        # ── Telegram notif ──────────────────────────────────────────────
        for sym, status, pnl in resolved_this_run:
            icon = "🟢" if status == "TP_HIT" else "🔴"
            emoji_label = "TP HIT" if status == "TP_HIT" else "SL HIT"
            _send_telegram(
                f"{icon} {emoji_label}: {sym} "
                f"{'+'if pnl>=0 else ''}{pnl:.4f} USD\n"
                f"(detected via --check-positions)"
            )

    # ── Save updates ───────────────────────────────────────────────────
    if log_dirty:
        from supabase_client import update_spot_by_order_id
        for ot in open_trades:
            eid = ot.get("entry_order_id")
            if not eid:
                continue
            update_spot_by_order_id(eid, {
                "entry_status":          ot.get("entry_status"),
                "entry_fill_price":      ot.get("entry_fill_price"),
                "entry_fill_time":       ot.get("entry_fill_time"),
                "entry_qty":             ot.get("entry_qty"),
                "slippage_pct":          ot.get("slippage_pct"),
                "oco_placed":            ot.get("oco_placed"),
                "oco_order_ids":         ot.get("oco_order_ids"),
                "oco_list_id":           ot.get("oco_list_id"),
                "tp1":                   ot.get("tp1"),   # may be adjusted by OCO race-condition handler
                "exit_status":           ot.get("exit_status"),
                "exit_price":            ot.get("exit_price"),
                "exit_time":             ot.get("exit_time"),
                "realized_pnl_usd":      ot.get("realized_pnl_usd"),
                "realized_pnl_pct":      ot.get("realized_pnl_pct"),
                "time_to_resolution_sec": ot.get("time_to_resolution_sec"),
            })

    if not verbose and len(open_trades) > 1:
        print(f"\n  ℹ️  Use --verbose for detailed per-position cards.")
    print("\n  Run --check-positions again to refresh.")
    print("  To manually close: testnet.binance.vision → spot trading → cancel OCO")


# ---------------------------------------------------------------------------
# 9. MAIN — --propose and --check-positions
# ---------------------------------------------------------------------------

def cmd_propose(scan_n: int, symbol_filter: str | None = None,
                simulate_balance: float | None = None,
                auto_confirm: bool = False) -> None:
    print("=" * 70)
    print("Paper Trade Executor — Binance Spot Testnet")
    print("=" * 70)

    # Connect to testnet
    print("\nConnecting to Binance Testnet...")
    try:
        client = get_testnet_client()
        client.ping()
        print("✅ Testnet connected")
    except Exception as e:
        print(f"❌ Testnet connection failed: {e}")
        sys.exit(1)

    # ── Simulated balance (Task 3 correction) ────────────────────────
    # Use simulated capital (BUDGET_USD + closed PnL), NOT real testnet wallet.
    # Real testnet wallet (~100k+) is irrelevant for scaling decisions.
    trades = load_trade_log()

    if simulate_balance is not None:
        sim_balance = simulate_balance
        print(f"\n⚠️  [TEST ONLY] Simulating balance: ${sim_balance:.2f}")
        print(f"   This flag is for dry-run testing only — NOT used in real operation.")
    else:
        sim_balance = get_simulated_balance(trades)
        closed_trades = [t for t in trades if t.get("exit_status") in ("TP_HIT", "SL_HIT")]
        closed_pnl    = sum(t.get("realized_pnl_usd") or 0 for t in closed_trades)
        print(f"\nSimulated capital  : ${sim_balance:.2f}")
        print(f"  Started at       : ${BUDGET_USD:.2f}")
        print(f"  Closed trades    : {len(closed_trades)}  (total PnL: ${closed_pnl:+.2f})")
        print(f"  (Real testnet wallet balance is ignored for sizing/threshold decisions)")

    # ── Determine position limit and slots available ───────────────────
    open_count   = count_open_positions(trades)
    open_trades  = [t for t in trades if t.get("exit_status") == "OPEN"]

    if sim_balance >= DUAL_POSITION_MIN_BALANCE:
        position_limit = MAX_CONCURRENT_POSITIONS  # 2
        slots_available = position_limit - open_count
        if open_count == 0:
            budget_for_slot = sim_balance / 2
            mode_str = f"Dual-position mode (${sim_balance:.2f} ≥ ${DUAL_POSITION_MIN_BALANCE:.0f} threshold)"
        else:
            used_notional   = sum(t.get("entry_notional", 0) for t in open_trades)
            budget_for_slot = max(sim_balance - used_notional, 0)
            mode_str = f"Dual-position mode — filling slot 2 (${budget_for_slot:.2f} available)"
    else:
        position_limit  = 1
        slots_available = position_limit - open_count
        budget_for_slot = sim_balance
        mode_str = (f"Single-position mode "
                    f"(${sim_balance:.2f} < ${DUAL_POSITION_MIN_BALANCE:.0f} threshold — "
                    f"keep growing to unlock dual-position)")

    print(f"Mode: {mode_str}")
    print(f"Open positions: {open_count} / {position_limit}  |  Budget for this slot: ${budget_for_slot:.2f}")
    print(f"Max risk this slot: {RISK_FRACTION*100:.0f}% × ${budget_for_slot:.2f} = ${budget_for_slot*RISK_FRACTION:.2f}")

    # ── Block if all slots full ────────────────────────────────────────
    if slots_available <= 0:
        print(f"\n⛔  All {position_limit} position slot(s) are occupied:")
        for t in open_trades:
            oco = "OCO ✅" if t.get("oco_placed") else "⚠ no OCO"
            print(f"   {t['symbol']:10} {t['direction'].upper():5}  "
                  f"entry={ca._fmt_price(t['entry_price']).strip()}  "
                  f"{t.get('entry_status','?')}  {oco}")
        print(f"\n   Run --check-positions to see full status.")
        print(f"   Wait for a position to close (TP_HIT / SL_HIT / MANUALLY_CLOSED) before proposing.")
        sys.exit(0)

    # ── Gather candidates ─────────────────────────────────────────────
    candidates = gather_candidates(scan_n)

    if not candidates:
        print("\n❌ No T1 zone-backed candidates found. Try again later.")
        sys.exit(0)

    # Filter out symbols already in an open position
    open_symbols = {t["symbol"] for t in open_trades}
    excluded_symbols = sorted({c["symbol"] for c in candidates} & open_symbols)
    candidates = [c for c in candidates if c["symbol"] not in open_symbols]
    if excluded_symbols:
        print(f"\n  Excluded (already open): {', '.join(excluded_symbols)}")
    if not candidates:
        print(f"\n❌ All T1 candidates are already in open positions ({', '.join(sorted(open_symbols))}). Try again later.")
        sys.exit(0)

    print(f"\nTop 5 candidates  (Risk×0.5 + Zone×0.3 + R:R×0.2 = Composite):")
    print(f"  {'#':<3} {'Symbol':<12} {'Risk%':>6} {'R:R':>5}  "
          f"{'RiskS':>5} {'ZoneS':>5} {'R:RS':>5} {'Comp':>5}  "
          f"{'EntryZ':>6} {'TPZ':>4}")
    print(f"  {'─'*66}")
    for i, c in enumerate(candidates[:5], 1):
        ez_touches = c.get("_touch_val", "?")
        # TP zone touches: from winning_zone label e.g. "Zone 5×" → extract number
        wz = c.get("winning_zone") or {}
        wz_label = wz.get("label", "")
        # label format is "Zone N×" — extract N
        import re as _re
        tp_touch_match = _re.search(r"(\d+)", wz_label)
        tp_touches = tp_touch_match.group(1) + "×" if tp_touch_match else "?"
        print(f"  {i:<3} {c['symbol']:<12} {c['risk_pct']:>5.2f}% {c['rr']:>5.1f}x  "
              f"{c.get('score_risk',0):>5.1f} {c.get('score_zone',0):>5.1f} "
              f"{c.get('score_rr',0):>5.1f} {c.get('score_composite',0):>5.1f}  "
              f"{str(ez_touches)+'×':>6} {tp_touches:>4}")

    # ── Pick best (or user-specified symbol) ──────────────────────────
    print("\nChecking exchange constraints...")
    best = pick_best_candidate(
        candidates, client,
        budget_for_slot = budget_for_slot,
        symbol_filter   = symbol_filter,
    )

    if best is None:
        what = f"for {symbol_filter}" if symbol_filter else ""
        print(f"\n❌ No candidate {what} passed exchange minimum notional constraints.")
        print(f"   Slot budget ${budget_for_slot:.2f} may be too small for min notional.")
        sys.exit(0)

    # ── Compute ML score (observation only — does not affect decisions) ─
    from ml.ml_scorer import compute_ml_score
    ml_result = compute_ml_score(best)
    best["ml_score"] = ml_result["ml_score"]
    best["ml_model_version"] = ml_result["ml_model_version"]

    # ── Display proposal ──────────────────────────────────────────────
    print_proposal(best)

    # ── Confirm ───────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    if simulate_balance is not None:
        print("  ⚠️  [TEST ONLY — simulated balance] No real order will be placed.")
        print("  This dry run confirms proposal logic only.")
        print("─" * 70)
        return   # never prompt in simulate mode

    print("  ⚠  This will place a REAL LIMIT ORDER on Binance Spot TESTNET.")
    print("  The testnet uses virtual funds — not real money.")
    print("  Type 'y' to confirm, anything else to abort.")
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

    # ── Place order ───────────────────────────────────────────────────
    print("\n  Placing limit order on testnet...")
    try:
        order = place_limit_order(client, best)
    except RuntimeError as e:
        print(f"\n  ❌ Order failed: {e}")
        return

    print(f"\n  ✅ Order placed successfully!")
    print(f"     Order ID  : {order.get('orderId')}")
    print(f"     Symbol    : {order.get('symbol')}")
    print(f"     Side      : {order.get('side')}")
    print(f"     Status    : {order.get('status')}")
    print(f"     Price     : {order.get('price')}")
    print(f"     Qty       : {order.get('origQty')}")

    log_trade(order, best)
    print(f"\n  Trade logged to {TRADE_LOG_PATH}")

    # Remind if a 2nd slot is still available
    new_open = count_open_positions(load_trade_log())
    if new_open < position_limit:
        remaining_slots = position_limit - new_open
        print(f"\n  ℹ️  {remaining_slots} slot(s) still available for another position.")


def cmd_check_positions(verbose: bool = False, mode: str = "all") -> None:
    print("=" * 70)
    print("Paper Trade Executor — Position Status")
    print("=" * 70)

    try:
        client = get_testnet_client()
        client.ping()
        print("✅ Testnet connected")
    except Exception as e:
        print(f"❌ Testnet connection failed: {e}")
        sys.exit(1)

    check_positions(client, verbose=verbose, mode=mode)


# ---------------------------------------------------------------------------
# 11. --propose-all: BATCH DATA COLLECTION
# ---------------------------------------------------------------------------

def gather_all_candidates(scan_n: int, client, open_symbols: set[str] | None = None) -> list[dict]:
    """
    Tiered/paginated candidate scan for --propose-all.

    Scans symbols in parts (Part 1 = rank 1-30, Part 2 = 31-60, Part 3 = 61-90).
    Proceeds to the next part only if fewer than MIN_DESIRED_CANDIDATES valid
    candidates were found so far, and only if rate-limit weight is safe.

    Each candidate is tagged with scan_part (1/2/3) so the batch table shows
    where it came from.
    """
    import time as _time
    import requests as _req

    open_symbols = open_symbols or set()

    from binance_throttle import SpotThrottle
    _throttle = SpotThrottle()

    def _get_used_weight() -> int:
        """Fetch current used-weight from Binance Spot API headers (1 weight unit)."""
        return _throttle.fetch_used_weight()

    def _scan_part(symbols: list[str], part_num: int) -> list[dict]:
        """Scan a list of symbols and return raw valid candidate dicts."""
        raw: list[dict] = []
        skipped = 0
        for sym in symbols:
            if sym in open_symbols:
                skipped += 1
                continue
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = ca.analyze_symbol(sym, save_chart=False)
            if not result:
                continue

            cur     = result["current_price"]
            atr     = result["atr"]
            atr_pct = result["atr_pct"]

            for direction in ("long",):
                setup = result["sl_tp"].get(direction, {})
                if setup.get("no_tp_in_range"):
                    continue
                if not setup.get("rr_clears"):
                    continue

                sl  = setup.get("sl")
                tp1 = setup["tp"][0] if setup.get("tp") else None
                rr  = setup.get("rr")
                if not sl or not tp1 or not rr:
                    continue

                # Part 2/3 extra scrutiny: require minimum 24h quote volume
                # Lower-ranked symbols are less liquid — add a floor
                if part_num >= 2:
                    min_vol = 5_000_000   # $5M 24h volume floor for Part 2+
                    try:
                        ticker = _req.get(
                            "https://api.binance.com/api/v3/ticker/24hr",
                            params={"symbol": sym},
                            timeout=8,
                        ).json()
                        vol = float(ticker.get("quoteVolume", 0))
                        if vol < min_vol:
                            continue
                    except Exception:
                        pass  # if we can't check, let it through

                winning_zone = next(
                    (c for c in setup.get("candidates", []) if c["tp"] == tp1), None
                )
                raw.append({
                    "symbol":           sym,
                    "direction":        direction,
                    "current_price":    cur,
                    "entry_price":      cur,
                    "sl":               sl,
                    "tp1":              tp1,
                    "tp2":              setup["tp"][1] if len(setup.get("tp", [])) > 1 else None,
                    "rr":               rr,
                    "risk_pct":         setup.get("risk_pct", 0),
                    "atr":              atr,
                    "atr_pct":          atr_pct,
                    "winning_zone":     winning_zone,
                    "support_zones":    result.get("support_zones", []),
                    "resistance_zones": result.get("resistance_zones", []),
                    "tier_used":        setup.get("tier_used", "T1"),
                    "scan_part":        part_num,
                })
        if skipped:
            print(f"  [Part {part_num}] Skipped {skipped} already-open symbol(s)")
        return raw

    # ── Get full ranked symbol list once ──────────────────────────────────
    total_needed = PART_SIZE * MAX_PARTS
    all_symbols  = ca.get_top_symbols_by_volume(total_needed)

    all_raw: list[dict] = []

    for part in range(1, MAX_PARTS + 1):
        start_idx = (part - 1) * PART_SIZE
        end_idx   = start_idx + PART_SIZE
        part_syms = all_symbols[start_idx:end_idx]
        if not part_syms:
            print(f"  [Part {part}] No symbols available — stopping.")
            break

        print(f"\n  ── Part {part} scan: rank {start_idx+1}–{end_idx} "
              f"({len(part_syms)} symbols) ──")

        # Rate-limit check before each part (except part 1)
        if part > 1:
            weight = _get_used_weight()
            print(f"  [Rate limit/Spot] Used weight before Part {part}: {weight} / {_throttle._limit}")
            if weight >= RATE_LIMIT_WEIGHT_CEILING:
                print(f"  [Rate limit/Spot] ⚠  Weight {weight} ≥ ceiling {RATE_LIMIT_WEIGHT_CEILING} "
                      f"— skipping Part {part}+ to avoid ban.")
                break
            _throttle.check_weight(weight)
            _throttle.between_parts_sleep()

        part_raw = _scan_part(part_syms, part)
        all_raw.extend(part_raw)

        valid_so_far = len([c for c in all_raw])  # will be filtered below
        print(f"  [Part {part}] Raw candidates found this part: {len(part_raw)}  "
              f"(cumulative raw: {len(all_raw)})")

        if part < MAX_PARTS:
            print(f"  [Part {part}] Continuing to Part {part+1}...")

    # ── Sort + score ───────────────────────────────────────────────────────
    all_raw.sort(key=lambda c: (c["risk_pct"], -c["rr"]))
    _attach_scores(all_raw)

    # ── Apply exchange constraints + entry zone anchoring + sanity check ──
    valid: list[dict] = []
    for cand in all_raw:
        sym = cand["symbol"]
        try:
            constraints = get_symbol_constraints(client, sym)
        except Exception:
            continue

        if cand["direction"] == "long":
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

            entry     = round_tick(zone_center * (1 + ZONE_ENTRY_BUFFER_PCT),
                                   constraints.get("tick_size", 0))
            recalc_sl = round_tick(zone_low - ca.SL_ATR_BUFFER * atr_v,
                                   constraints.get("tick_size", 0))
            cand["sl"]             = recalc_sl
            cand["risk_pct"]       = (entry - recalc_sl) / entry * 100 if entry > 0 else 0
            cand["entry_price"]    = entry
            cand["budget_for_slot"] = PER_TRADE_BUDGET

        if not (cand["sl"] < cand["entry_price"] < cand["tp1"]):
            continue

        sizing = compute_position_size(
            entry_price   = cand["entry_price"],
            sl_price      = cand["sl"],
            budget_usd    = PER_TRADE_BUDGET,
            risk_fraction = RISK_FRACTION,
            constraints   = constraints,
        )
        cand["sizing"]      = sizing
        cand["constraints"] = constraints

        fatal = [w for w in sizing["warnings"]
                 if "below exchange minimum" in w or "cannot size" in w
                 or "exceeds total budget" in w]
        if fatal or sizing["qty"] <= 0:
            continue

        if cand["risk_pct"] > 10.0:
            continue

        valid.append(cand)

    print(f"\nFound {len(valid)} valid candidates total "
          f"(from {len(all_raw)} raw across {min(MAX_PARTS, len(all_raw)+1)} part(s)).")
    return valid


def should_auto_confirm_batch(is_lab_batch: bool) -> bool:
    """Only lab/batch mode is auto-confirmed; single-propose stays manual."""
    return bool(is_lab_batch)


def cmd_propose_all(scan_n: int, dry_run: bool = False,
                    auto_confirm: bool = False) -> None:
    print("=" * 70)
    print("Paper Trade Executor — BATCH DATA COLLECTION (--propose-all)")
    print(f"Each position sized at ${PER_TRADE_BUDGET:.2f}  |  Rule: {RULE_VERSION}")
    print("This mode is for accelerated data collection, not live capital deployment.")
    print("=" * 70)

    try:
        client = get_testnet_client()
        client.ping()
        print("✅ Testnet connected\n")
    except Exception as e:
        print(f"❌ Testnet connection failed: {e}")
        sys.exit(1)

    open_trades = [t for t in load_trade_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}
    if open_symbols:
        print(f"  Excluding already open symbol(s) from this batch: {', '.join(sorted(open_symbols))}\n")

    # Compute lab capital pool (compounding) and display status
    all_trades = load_trade_log()
    pool = compute_lab_pool(all_trades)
    lab_cap = pool["lab_capital"]
    net_pnl = pool["closed_cluster_pnl"]
    deployed = pool["deployed_capital"]
    available = pool["available_capital"]
    max_new = pool["max_new_positions"]

    print(f"\n  Lab capital: ${lab_cap:.2f} (started ${LAB_STARTING_CAPITAL:.0f}, net P&L ${net_pnl:+.2f})  |  Deployed: ${deployed:.2f}  |  Available: ${available:.2f}  |  Max new positions: {max_new}")

    if max_new <= 0:
        print("\n⛔  Lab pool depleted or fully deployed — no new proposals allowed.")
        print("   Either wait for cluster trades to resolve (TP_HIT/SL_HIT) or increase LAB_STARTING_CAPITAL.")
        return

    candidates = gather_all_candidates(scan_n, client, open_symbols=open_symbols)
    if not candidates:
        if open_symbols:
            print(f"❌ No valid candidates found. Symbols already open: {', '.join(sorted(open_symbols))}.")
        else:
            print("❌ No valid candidates found.")
        sys.exit(0)

    # Respect lab pool limit: only allow up to max_new new positions
    if len(candidates) > max_new:
        print(f"\n  Note: limiting proposals to top {max_new} candidates due to lab pool available capital")
        candidates = candidates[:max_new]

    excluded_symbols = sorted(open_symbols)
    if excluded_symbols:
        print(f"\n  Excluded (already open): {', '.join(excluded_symbols)}")

    # Correlation cluster ID — shared by all trades in this batch
    cluster_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Print batch summary table
    print(f"\n  Batch proposal  [{len(candidates)} trades]  cluster: {cluster_id}")
    print(f"  {'#':<3} {'Symbol':<13} {'Entry':>10} {'SL':>10} {'TP1':>10} "
          f"{'R:R':>5} {'Risk%':>6} {'T':>2} {'Z':>3} {'P':>2}  Score")
    print(f"  {'─'*73}")
    for i, c in enumerate(candidates, 1):
        ez   = c.get("entry_zone") or {}
        tier = c.get("tier_used", "T1")[1]   # "1" or "2"
        ez_t = ez.get("touches", "?")
        part = c.get("scan_part", 1)
        print(f"  {i:<3} {c['symbol']:<13} "
              f"{ca._fmt_price(c['entry_price']).strip():>10} "
              f"{ca._fmt_price(c['sl']).strip():>10} "
              f"{ca._fmt_price(c['tp1']).strip():>10} "
              f"{c['rr']:>5.1f} {c['risk_pct']:>5.2f}% "
              f"T{tier} {str(ez_t)+'×':>3} P{part}  "
              f"{c.get('score_composite',0):.1f}")

    total_notional = sum(c["sizing"]["notional_usd"] for c in candidates)
    print(f"\n  Total notional if all fill: ${total_notional:.2f} "
          f"({len(candidates)} × ~${PER_TRADE_BUDGET:.0f})")
    print(f"  Note: testnet virtual balance absorbs this; "
          f"each trade behaves as if placed with ${PER_TRADE_BUDGET:.2f} real capital.")

    if dry_run:
        print(f"\n  [DRY RUN] No orders placed. Cluster ID would be: {cluster_id}")
        return

    print(f"\n  {'─'*70}")
    print(f"  ⚠  This will place {len(candidates)} LIMIT ORDERS on Binance Spot TESTNET.")
    print(f"  All virtual funds. Type 'y' to place ALL, anything else to abort.")
    print(f"  {'─'*70}")

    if should_auto_confirm_batch(is_lab_batch=True) or auto_confirm:
        if auto_confirm:
            print("  [--yes] Auto-confirmed for non-interactive run.")
        else:
            print("  Auto-confirm enabled for lab/batch mode — proceeding without manual prompt.")
        answer = "y"
    else:
        try:
            answer = input("  Confirm ALL? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""

    if answer != "y":
        print("\n  Aborted. No orders placed.")
        return

    placed, failed = 0, 0
    from ml.ml_scorer import compute_ml_score
    for cand in candidates:
        try:
            # ML score (observation only — does not affect decisions)
            ml_result = compute_ml_score(cand)
            cand["ml_score"] = ml_result["ml_score"]
            cand["ml_model_version"] = ml_result["ml_model_version"]

            order = place_limit_order(client, cand)
            log_trade(order, cand, correlation_cluster_id=cluster_id)
            ml_tag = f"  ml={cand['ml_score']:.2f}" if cand.get('ml_score') is not None else ""
            print(f"  ✅ {cand['symbol']:<12} order #{order.get('orderId')}  "
                  f"price={order.get('price')}{ml_tag}")
            placed += 1
        except Exception as e:
            print(f"  ❌ {cand['symbol']:<12} FAILED: {e}")
            failed += 1

    print(f"\n  Placed: {placed}  Failed: {failed}  Cluster: {cluster_id}")
    print(f"  Run --check-positions to monitor.")


# ---------------------------------------------------------------------------
# 12. --stats
# ---------------------------------------------------------------------------

def cmd_stats(mode: str = "all") -> None:
    """Compute and print performance statistics from trade_log.json.

    Mode filters trades: 'single' (no cluster id), 'lab' (clustered), or 'all'.
    """
    from collections import defaultdict
    import math

    trades = load_trade_log()
    # Filter trades by mode
    closed = [t for t in trades
              if t.get("exit_status") in ("TP_HIT", "SL_HIT") and _match_mode(t, mode)]

    if not closed:
        print("\n  No closed trades yet. Run --check-positions after trades resolve.")
        return

    print("=" * 70)
    print("Performance Statistics")
    print(f"Mode: {mode}  |  Total closed trades: {len(closed)}")
    print("=" * 70)

    # Group by rule_version
    by_version: dict[str, list] = defaultdict(list)
    for t in closed:
        by_version[t.get("rule_version") or "unknown"].append(t)

    for version, group in sorted(by_version.items(), key=lambda x: (x[0] is None, x[0])):
        n          = len(group)
        wins       = [t for t in group if t["exit_status"] == "TP_HIT"]
        losses     = [t for t in group if t["exit_status"] == "SL_HIT"]
        win_rate   = len(wins) / n
        avg_rr     = sum((t.get("planned_rr") or 0) for t in group) / n
        be_win_rate = 1 / (1 + avg_rr) if avg_rr > 0 else 0.5

        avg_win_pct  = (sum((t.get("realized_pnl_pct") or 0) for t in wins)  / len(wins))  if wins   else 0
        avg_loss_pct = (sum((t.get("realized_pnl_pct") or 0) for t in losses) / len(losses)) if losses else 0
        avg_fee_slip = sum(
            ((t.get("fee_usd_roundtrip") or 0) / max((t.get("entry_notional") or 1), 1) * 100)
            + abs(t.get("slippage_pct") or 0)
            for t in group
        ) / n
        expectancy   = (win_rate * avg_win_pct) - ((1 - win_rate) * abs(avg_loss_pct)) - avg_fee_slip

        # Z-score vs breakeven
        if n >= 2:
            p0   = be_win_rate
            z    = (win_rate - p0) / math.sqrt(p0 * (1 - p0) / n)
            sig  = "✅ p<0.05" if abs(z) >= 1.96 else ("🟡 p<0.10" if abs(z) >= 1.645 else "⚠ not sig")
        else:
            z, sig = 0.0, "⚠ n/a"

        # Cluster analysis
        cluster_ids = set(t.get("correlation_cluster_id") for t in group
                          if t.get("correlation_cluster_id"))
        n_clusters   = len(cluster_ids) if cluster_ids else n
        n_singles    = sum(1 for t in group if not t.get("correlation_cluster_id"))

        print(f"\n  Rule version: {version}  ({n} trades)")
        print(f"  {'─'*60}")
        print(f"  Win rate          : {win_rate*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Breakeven win rate: {be_win_rate*100:.1f}%  (at avg R:R {avg_rr:.2f}:1)")
        print(f"  Avg win  %        : {avg_win_pct:+.2f}%")
        print(f"  Avg loss %        : {avg_loss_pct:+.2f}%")
        print(f"  Avg fee+slip %    : -{avg_fee_slip:.3f}%")
        print(f"  Expectancy        : {expectancy:+.3f}%  per trade")
        print(f"  Z-score vs B/E    : {z:+.2f}  {sig}")
        if n < 30:
            print(f"  ⚠  Only {n} trades — z-score unreliable until n≥30")
        print(f"  Clusters          : {n_clusters} independent sessions + {n_singles} single trades")
        print(f"  Effective n       : ~{n_clusters + n_singles} independent observations")
        print(f"  (raw trade count {n} overstates independence if cluster trades co-move)")

    # Total realized PnL
    total_pnl = sum(t.get("realized_pnl_usd", 0) or 0 for t in closed)
    sim_bal   = get_simulated_balance(trades)
    print(f"\n  Total realized PnL: ${total_pnl:+.4f}")
    print(f"  Simulated capital : ${sim_bal:.2f}  (started: ${BUDGET_USD:.2f})")


def main():
    parser = argparse.ArgumentParser(
        description="Paper trading executor — Binance Spot Testnet. No auto-execution."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--propose",       action="store_true",
                       help="Scan, pick best low-risk T1 candidate, confirm & place")
    group.add_argument("--propose-all",   action="store_true",
                       help="Batch: propose ALL valid candidates for data collection")
    group.add_argument("--check-positions", action="store_true",
                       help="Check status of open positions, place OCO if filled")
    group.add_argument("--stats",         action="store_true",
                       help="Print performance statistics from trade_log.json")

    parser.add_argument("--scan-n",         type=int, default=DEFAULT_SCAN_N)
    parser.add_argument("--symbol",         type=str, default=None,
                        help="--propose only: manually specify symbol")
    parser.add_argument("--simulate-balance", type=float, default=None, metavar="USD",
                        help="[TEST ONLY] Override simulated balance for dry-run testing")
    parser.add_argument("--dry-run",        action="store_true",
                        help="--propose-all: show batch table but don't place orders")
    parser.add_argument("--verbose",        action="store_true",
                        help="--check-positions: show detailed per-position cards")
    parser.add_argument("--mode", choices=["single", "lab", "all"], default="all",
                        help="Filter trades by mode: single (no cluster id), lab (clustered), or all")
    parser.add_argument("--export-clean", action="store_true",
                        help="Export resolved lab trades to a clean ML-ready JSON and exit")
    parser.add_argument("--yes", action="store_true",
                        help="Auto-confirm order placement (for non-interactive / CI use)")
    args = parser.parse_args()

    if args.propose:
        cmd_propose(scan_n=args.scan_n, symbol_filter=args.symbol,
                    simulate_balance=args.simulate_balance,
                    auto_confirm=args.yes)
    elif args.propose_all:
        cmd_propose_all(scan_n=args.scan_n, dry_run=args.dry_run,
                        auto_confirm=args.yes)
    elif args.check_positions:
        cmd_check_positions(verbose=args.verbose, mode=args.mode)
    elif args.stats:
        cmd_stats(mode=args.mode)

    # Export-clean is a convenience to write a filtered ML-ready dataset then exit
    if args.export_clean:
        all_trades = load_trade_log()
        export_clean(all_trades, mode=args.mode)
        return


if __name__ == "__main__":
    main()
