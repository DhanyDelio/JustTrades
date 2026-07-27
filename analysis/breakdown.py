"""
analysis/breakdown.py — Exploratory breakdown of spot trade_log.json
======================================================================
READ-ONLY. Does not modify any file or rule.
Run: python3 analysis/breakdown.py

Breakdowns:
  1. Win rate by zone_type (T1 vs T2)
  2. Win rate by zone_touches (1x / 2x / 3x / 4x+)
  3. Win rate by atr_pct_at_entry bucket
  4. Win rate by risk_pct bucket
  5. Trade count per cluster (distribution sanity check)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "trade_log.json"


# ---------------------------------------------------------------------------
# Load & filter
# ---------------------------------------------------------------------------

def load_closed() -> list[dict]:
    trades = json.load(open(LOG_PATH))
    closed = [t for t in trades if t.get("exit_status") in ("TP_HIT", "SL_HIT")]
    return closed


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

W = 68

def _header(title: str) -> None:
    print(f"\n{'═'*W}")
    print(f"  {title}")
    print(f"{'═'*W}")
    print(f"  {'Group':<20} {'n':>4}  {'Win%':>6}  {'Avg PnL%':>9}  {'Wins':>5}  {'Losses':>7}")
    print(f"  {'─'*60}")


def _row(label: str, trades: list[dict]) -> None:
    n      = len(trades)
    wins   = [t for t in trades if t["exit_status"] == "TP_HIT"]
    losses = [t for t in trades if t["exit_status"] == "SL_HIT"]
    wr     = len(wins) / n * 100 if n else 0
    avg_pnl = (sum(t.get("realized_pnl_pct", 0) or 0 for t in trades) / n) if n else 0
    warn   = "  ⚠ n<5" if n < 5 else ""
    print(f"  {label:<20} {n:>4}  {wr:>5.1f}%  {avg_pnl:>+8.2f}%  "
          f"{len(wins):>5}W  {len(losses):>5}L{warn}")


def _note(msg: str) -> None:
    print(f"\n  ℹ  {msg}")


# ---------------------------------------------------------------------------
# 1. Zone type
# ---------------------------------------------------------------------------

def breakdown_zone_type(closed: list[dict]) -> None:
    _header("1. Win rate by zone_type (T1 vs T2)")
    groups: dict[str, list] = defaultdict(list)
    for t in closed:
        zt = (t.get("zone_type") or "unknown").upper()
        groups[zt].append(t)
    for label in sorted(groups):
        _row(label, groups[label])
    _note("T1 = empirically-tested S/R zone. T2 = fib fallback.")


# ---------------------------------------------------------------------------
# 2. Zone touches
# ---------------------------------------------------------------------------

def breakdown_zone_touches(closed: list[dict]) -> None:
    _header("2. Win rate by zone_touches")

    def _bucket(t: dict) -> str:
        zt = t.get("zone_touches") or t.get("entry_zone_touches")
        try:
            zt = int(zt)
        except (TypeError, ValueError):
            return "unknown"
        if zt <= 1:   return "1×"
        if zt == 2:   return "2×"
        if zt == 3:   return "3×"
        return "4×+"

    groups: dict[str, list] = defaultdict(list)
    for t in closed:
        groups[_bucket(t)].append(t)

    for label in ["1×", "2×", "3×", "4×+", "unknown"]:
        if label in groups:
            _row(label, groups[label])
    _note("Higher touch count = zone more historically respected.")


# ---------------------------------------------------------------------------
# 3. ATR% bucket at entry
# ---------------------------------------------------------------------------

def breakdown_atr(closed: list[dict]) -> None:
    _header("3. Win rate by atr_pct_at_entry bucket")

    def _bucket(t: dict) -> str:
        v = t.get("atr_pct_at_entry")
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "unknown"
        if v < 1.0:   return "<1%"
        if v < 2.0:   return "1-2%"
        if v < 3.0:   return "2-3%"
        return ">3%"

    groups: dict[str, list] = defaultdict(list)
    for t in closed:
        groups[_bucket(t)].append(t)

    for label in ["<1%", "1-2%", "2-3%", ">3%", "unknown"]:
        if label in groups:
            _row(label, groups[label])
    _note("ATR% = daily volatility proxy at time of entry.")


# ---------------------------------------------------------------------------
# 4. Risk% bucket (SL distance)
# ---------------------------------------------------------------------------

def breakdown_risk_pct(closed: list[dict]) -> None:
    _header("4. Win rate by risk_pct (SL distance) bucket")

    def _bucket(t: dict) -> str:
        v = t.get("risk_pct")
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "unknown"
        if v < 1.0:   return "<1%"
        if v < 2.0:   return "1-2%"
        if v < 3.0:   return "2-3%"
        return ">3%"

    groups: dict[str, list] = defaultdict(list)
    for t in closed:
        groups[_bucket(t)].append(t)

    for label in ["<1%", "1-2%", "2-3%", ">3%", "unknown"]:
        if label in groups:
            _row(label, groups[label])
    _note("risk_pct = (entry - SL) / entry * 100. Tighter SL = smaller bucket.")


# ---------------------------------------------------------------------------
# 5. Cluster distribution
# ---------------------------------------------------------------------------

def breakdown_cluster(closed: list[dict]) -> None:
    _header("5. Trade count per correlation_cluster_id")
    print(f"  {'Cluster ID':<28} {'Total':>5}  {'TP':>4}  {'SL':>4}")
    print(f"  {'─'*50}")

    groups: dict[str, list] = defaultdict(list)
    for t in closed:
        cid = t.get("correlation_cluster_id") or "single"
        groups[cid].append(t)

    # Sort by count desc
    for cid, trades in sorted(groups.items(), key=lambda x: -len(x[1])):
        wins   = sum(1 for t in trades if t["exit_status"] == "TP_HIT")
        losses = sum(1 for t in trades if t["exit_status"] == "SL_HIT")
        print(f"  {cid:<28} {len(trades):>5}  {wins:>4}  {losses:>4}")

    n_clusters = len([c for c in groups if c != "single"])
    n_singles  = len(groups.get("single", []))
    total      = len(closed)
    print(f"\n  Clusters: {n_clusters}  |  Single trades: {n_singles}  |  "
          f"Effective independent observations: ~{n_clusters + n_singles}")
    _note("Trades in same cluster are correlated — not independent samples.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    closed = load_closed()

    print(f"\n{'═'*W}")
    print(f"  Spot Trade Exploratory Breakdown")
    print(f"  Source: trade_log.json")
    print(f"  Closed trades: {len(closed)}  "
          f"(TP: {sum(1 for t in closed if t['exit_status']=='TP_HIT')}  "
          f"SL: {sum(1 for t in closed if t['exit_status']=='SL_HIT')})")
    print(f"{'═'*W}")
    print(f"\n  ⚠  STATISTICAL WARNING: n={len(closed)} total, effective n much smaller")
    print(f"     after cluster correction. Per-bucket n is tiny.")
    print(f"     All patterns below are HYPOTHESES only — not statistically validated.")

    breakdown_zone_type(closed)
    breakdown_zone_touches(closed)
    breakdown_atr(closed)
    breakdown_risk_pct(closed)
    breakdown_cluster(closed)

    print(f"\n{'═'*W}")
    print(f"  End of exploratory breakdown")
    print(f"{'═'*W}\n")


if __name__ == "__main__":
    main()
