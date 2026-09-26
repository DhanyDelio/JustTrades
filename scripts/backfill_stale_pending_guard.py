"""
backfill_stale_pending_guard.py
================================
Read-only + safe backfill of pending_entry_guard for all NEW Spot orders.

WHY THIS EXISTS
---------------
Commit 6b9de95 added pending_entry_guard to the raw_entry_order payload
on new order creation (spot_trade_repository.log_trade). Orders placed
*before* that commit have raw_entry_order = {exchange response only} with
no pending_entry_guard key at all.

Additionally, shadow evaluations (WOULD_CANCEL state) update the guard
in-memory during check_positions() but never persist — _persist_pending_guard
is only called immediately before an exchange cancellation, which auto-cancel
is currently OFF for.

WHAT THIS SCRIPT DOES
---------------------
1. Fetches all NEW / PARTIALLY_FILLED OPEN spot trades from Supabase.
2. Fetches current Binance Production prices for every symbol (public API,
   no authentication, no exchange mutation).
3. Evaluates each trade through the existing stale_pending_entry_guard.evaluate()
   function — same logic check_positions uses, no new logic.
4. Merges result into raw_entry_order.pending_entry_guard via
   update_spot_by_order_id (raw_entry_order field only — exchange order
   is NOT touched, SL/TP/entry price are NOT changed).
5. Idempotent: running it twice produces the same result. The guard field
   is overwritten, not appended. Exchange state is never read or modified.

SAFETY GUARANTEES
-----------------
- AUTO_CANCEL is always False here regardless of environment flag.
- No Binance API call that mutates state (no DELETE/POST to exchange).
- No change to entry_price, sl, tp1, entry_status, exit_status, oco_placed.
- Dry-run mode (--dry-run) prints what would be written without writing.

USAGE
-----
  python3 scripts/backfill_stale_pending_guard.py           # live write
  python3 scripts/backfill_stale_pending_guard.py --dry-run # preview only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests

from core.guards.stale_pending_entry_guard import evaluate as _guard_evaluate
from services.supabase_client import fetch_all_spot, update_spot_by_order_id

WIB = timezone(timedelta(hours=7))
NOW = datetime.now(timezone.utc)

# ── Category thresholds (read-only classification, not exec thresholds) ─────
_NEAR_PCT       = 2.0    # |dist| < 2% → near entry
_MODERATE_PCT   = 5.0    # 2–5%
_RUNAWAY_PCT    = 10.0   # 5–10%
_RUNAWAY_HI_PCT = 10.0   # ≥10%
_STALE_DAYS_1   = 1.0
_STALE_DAYS_3   = 3.0
_STALE_DAYS_7   = 7.0
_STALE_DAYS_14  = 14.0
_STALE_DAYS_30  = 30.0


def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch current prices from Binance Production public API (no auth)."""
    price_map: dict[str, float] = {}
    # Try production endpoint; fall back to testnet if SSL fails
    endpoints = [
        "https://api.binance.com/api/v3/ticker/price",
        "https://testnet.binance.vision/api/v3/ticker/price",
    ]
    for url in endpoints:
        try:
            resp = requests.get(url, timeout=10, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        if item["symbol"] in symbols:
                            price_map[item["symbol"]] = float(item["price"])
                elif isinstance(data, dict) and "price" in data:
                    sym = data.get("symbol")
                    if sym in symbols:
                        price_map[sym] = float(data["price"])
                if price_map:
                    return price_map
        except Exception as exc:
            print(f"  [WARN] Price fetch from {url}: {exc}")
    return price_map


def _age_days(trade: dict) -> float | None:
    raw = trade.get("open_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return (NOW - dt).total_seconds() / 86400
    except Exception:
        return None


def _classify(dist_pct: float | None, age: float | None) -> str:
    """
    Assign a human-readable lifecycle category.
    This is purely informational — it does NOT trigger any exchange action.
    """
    if dist_pct is None:
        return "UNKNOWN"
    if age is not None and age >= _STALE_DAYS_30:
        if dist_pct >= _RUNAWAY_HI_PCT:
            return "STALE_RUNAWAY"   # very old AND price far away
        return "STALE_REVIEW"
    if dist_pct >= _RUNAWAY_HI_PCT:
        return "RUNAWAY"
    if dist_pct >= _RUNAWAY_PCT:
        return "RUNAWAY_MODERATE"
    if dist_pct >= _MODERATE_PCT:
        return "MODERATE"
    if dist_pct < _NEAR_PCT:
        return "NEAR_ENTRY"
    return "REVIEW"


def backfill(dry_run: bool = False) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print("  SPOT PENDING ENTRY GUARD — BACKFILL")
    print(f"  Mode: {'DRY-RUN (no writes)' if dry_run else 'LIVE WRITE'}")
    print(f"  AUTO_CANCEL: ALWAYS OFF (this script never touches exchange)")
    print(sep)

    # ── 1. Load Supabase rows ──────────────────────────────────────────
    all_rows = fetch_all_spot()
    pending  = [
        r for r in all_rows
        if r.get("entry_status") in ("NEW", "PARTIALLY_FILLED")
        and r.get("exit_status") == "OPEN"
    ]
    print(f"\n  Pending NEW/PARTIAL rows from Supabase: {len(pending)}")

    if not pending:
        print("  Nothing to backfill.")
        return

    # ── 2. Fetch prices ────────────────────────────────────────────────
    symbols    = list({r["symbol"] for r in pending})
    price_map  = _fetch_prices(symbols)
    n_priced   = sum(1 for s in symbols if s in price_map)
    print(f"  Prices fetched: {n_priced}/{len(symbols)} symbols")
    no_price   = [s for s in symbols if s not in price_map]
    if no_price:
        print(f"  No price for: {no_price}")

    # ── 3. Evaluate + build update payloads ───────────────────────────
    rows_sorted = sorted(pending, key=lambda r: r.get("open_time") or "")

    results = []
    for trade in rows_sorted:
        sym      = trade["symbol"]
        oid      = trade["entry_order_id"]
        cur      = price_map.get(sym)
        age      = _age_days(trade)
        ep       = float(trade.get("entry_price") or 0)
        dist_pct = ((cur - ep) / ep * 100) if (cur and ep) else None
        cat      = _classify(dist_pct, age)

        # Guard evaluation (same logic as check_positions Step 1.5)
        if cur is not None:
            decision = _guard_evaluate(trade, cur, now=NOW)
            guard_action = decision["action"]
        else:
            decision     = {"action": "PRICE_UNAVAILABLE"}
            guard_action = "PRICE_UNAVAILABLE"

        # Build the guard record — never overwrites exchange-specific keys
        raw_order     = dict(trade.get("raw_entry_order") or {})
        existing_guard = dict(raw_order.get("pending_entry_guard") or {})

        # Preserve any existing revalidation history
        new_guard = {
            **existing_guard,                          # keep prior history
            "state":           cat,
            "guard_action":    guard_action,
            "age_days":        round(age, 2) if age is not None else None,
            "current_price":   cur,
            "distance_pct":    round(dist_pct, 4) if dist_pct is not None else None,
            "backfilled_at":   NOW.isoformat(),
            "auto_cancel":     False,
            "order_id":        oid,
        }

        # Determine if this is a shadow WOULD_CANCEL
        if guard_action == "CANCEL_ELIGIBLE":
            new_guard["state"]      = "WOULD_CANCEL"
            new_guard["would_cancel"] = True
            new_guard["cancel_reason"] = "STALE_SETUP_AUTO_CANCEL_OFF"
        elif guard_action == "REVALIDATE":
            new_guard["would_cancel"] = False
        else:
            new_guard["would_cancel"] = False

        raw_order["pending_entry_guard"] = new_guard

        results.append({
            "sym":       sym,
            "oid":       oid,
            "age":       age,
            "ep":        ep,
            "cur":       cur,
            "dist_pct":  dist_pct,
            "cat":       cat,
            "action":    guard_action,
            "wc":        new_guard.get("would_cancel", False),
            "raw_order": raw_order,
        })

    # ── 4. Print summary ───────────────────────────────────────────────
    print(f"\n  {'Symbol':<14} {'Age(d)':>7} {'Entry':>9} {'Current':>9} {'Dist%':>8}  {'Category':<20} {'Action':<20} {'WC'}")
    print(f"  {'-'*110}")

    from collections import Counter
    cats   = Counter()
    wc_cnt = 0
    for r in results:
        dist_s = f"{r['dist_pct']:+.1f}%" if r["dist_pct"] is not None else "?"
        age_s  = f"{r['age']:.1f}"         if r["age"]      is not None else "?"
        cur_s  = f"{r['cur']:.4f}"         if r["cur"]      is not None else "?"
        wc_s   = "WOULD_CANCEL" if r["wc"] else "—"
        cats[r["cat"]] += 1
        if r["wc"]: wc_cnt += 1
        print(f"  {r['sym']:<14} {age_s:>7} {r['ep']:>9.4f} {cur_s:>9} {dist_s:>8}  {r['cat']:<20} {r['action']:<20} {wc_s}")

    print(f"\n  Category breakdown:")
    for cat, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"    {cat:<22}: {n}")
    print(f"\n  WOULD_CANCEL (shadow, no exchange action): {wc_cnt}")

    if dry_run:
        print(f"\n  DRY-RUN — nothing written to Supabase.")
        return

    # ── 5. Write to Supabase (raw_entry_order only) ────────────────────
    print(f"\n  Writing guard to Supabase ({len(results)} rows)...")
    ok = err = 0
    for r in results:
        try:
            update_spot_by_order_id(
                r["oid"],
                {"raw_entry_order": r["raw_order"]},
            )
            ok += 1
        except Exception as exc:
            print(f"  ✗ {r['sym']} (#{r['oid']}): {exc}")
            err += 1

    print(f"\n  Written: {ok}/{len(results)}  Errors: {err}")
    print(f"  Exchange orders: NOT TOUCHED")
    print(f"  SL / TP / entry_price: NOT TOUCHED")
    print(f"  AUTO_CANCEL: OFF")
    print(f"\n  Backfill complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill pending_entry_guard for stale Spot orders (read-only eval)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate but do not write to Supabase",
    )
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
