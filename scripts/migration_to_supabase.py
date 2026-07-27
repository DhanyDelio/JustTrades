"""
migration_to_supabase.py — One-time migration of local JSON logs to Supabase.
=============================================================================
Reads trade_log.json  → inserts into trades_spot    (Supabase)
Reads trade_futures.json → inserts into trades_futures (Supabase)

SAFE: read-only on local files. JSON files are NOT deleted or modified.
Run once, verify row counts match, then proceed to update main executors.

Usage:
    python3 migration_to_supabase.py

    # Dry-run (parse + validate only, no Supabase writes):
    python3 migration_to_supabase.py --dry-run

    # Migrate only one table:
    python3 migration_to_supabase.py --only spot
    python3 migration_to_supabase.py --only futures

Requirements:
    pip3 install supabase python-dotenv --break-system-packages

.env keys required:
    SUPABASE_URL=https://<project-id>.supabase.co
    SUPABASE_SERVICE_KEY=<service_role_key>   # NOT anon key — needs INSERT rights
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

SPOT_LOG_PATH    = Path(__file__).resolve().parent / "trade_log.json"
FUTURES_LOG_PATH = Path(__file__).resolve().parent / "trade_futures.json"


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

def get_supabase_client():
    try:
        from supabase import create_client, Client
    except ImportError:
        print("❌  supabase-py not installed.")
        print("    pip3 install supabase --break-system-packages")
        sys.exit(1)

    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key:
        print("❌  SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        sys.exit(1)

    placeholders = ("your_", "paste_", "replace_", "changeme", "<project")
    for p in placeholders:
        if p in url.lower() or p in key.lower():
            print(f"❌  .env still contains placeholder values — fill in real credentials.")
            sys.exit(1)

    return create_client(url, key)


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float | None:
    """Coerce to float, return None on failure."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    return False


def _parse_open_time(v: Any) -> str | None:
    """
    open_time is stored as ISO string in JSON.
    Supabase TIMESTAMPTZ accepts ISO 8601 directly.
    Returns None if unparseable.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v  # already ISO string — Supabase handles it
    return None


def _int_list_or_none(v: Any) -> list[int] | None:
    """Convert oco_order_ids (list or None) to list[int] | None."""
    if v is None:
        return None
    if isinstance(v, list):
        return [int(x) for x in v if x is not None]
    return None


# ---------------------------------------------------------------------------
# Spot record mapper
# ---------------------------------------------------------------------------

def map_spot_record(raw: dict) -> dict:
    """
    Map one raw trade_log.json record to the trades_spot column schema.
    Handles the 'entry_statusa' typo in older records.
    """
    # Fix typo: 'entry_statusa' → 'entry_status'
    entry_status = raw.get("entry_status") or raw.get("entry_statusa") or "UNKNOWN"

    return {
        # Identity
        "symbol":                   raw.get("symbol"),
        "direction":                raw.get("direction"),
        "budget_usd":               _to_float(raw.get("budget_usd")),
        "rule_version":             raw.get("rule_version"),
        "correlation_cluster_id":   raw.get("correlation_cluster_id"),

        # Entry order
        "entry_order_id":           _to_int(raw.get("entry_order_id")),
        "entry_client_id":          raw.get("entry_client_id"),
        "entry_status":             entry_status,
        "entry_price":              _to_float(raw.get("entry_price")),
        "entry_fill_price":         _to_float(raw.get("entry_fill_price")),
        "entry_fill_time":          _to_int(raw.get("entry_fill_time")),
        "entry_qty":                _to_float(raw.get("entry_qty")),
        "entry_notional":           _to_float(raw.get("entry_notional")),
        "open_time":                _parse_open_time(raw.get("open_time")),

        # OCO
        "oco_placed":               _to_bool(raw.get("oco_placed")),
        "oco_order_ids":            _int_list_or_none(raw.get("oco_order_ids")),
        "oco_list_id":              _to_int(raw.get("oco_list_id")),

        # Levels
        "sl":                       _to_float(raw.get("sl")),
        "tp1":                      _to_float(raw.get("tp1")),
        "tp2":                      _to_float(raw.get("tp2")),
        "entry_zone_center":        _to_float(raw.get("entry_zone_center")),
        "entry_zone_touches":       _to_int(raw.get("entry_zone_touches")),

        # Setup metadata
        "planned_rr":               _to_float(raw.get("planned_rr")),
        "risk_pct":                 _to_float(raw.get("risk_pct")),
        "max_loss_usd":             _to_float(raw.get("max_loss_usd")),
        "zone_type":                raw.get("zone_type"),
        "zone_label":               raw.get("zone_label"),
        "zone_touches":             _to_int(raw.get("zone_touches")),
        "atr_pct_at_entry":         _to_float(raw.get("atr_pct_at_entry")),

        # Cost estimates
        "fee_usd_roundtrip":        _to_float(raw.get("fee_usd_roundtrip")),
        "slippage_pct":             _to_float(raw.get("slippage_pct")),
        "time_to_resolution_sec":   _to_int(raw.get("time_to_resolution_sec")),

        # Exit
        "exit_status":              raw.get("exit_status") or "OPEN",
        "exit_price":               _to_float(raw.get("exit_price")),
        "exit_time":                _to_int(raw.get("exit_time")),
        "realized_pnl_usd":         _to_float(raw.get("realized_pnl_usd")),
        "realized_pnl_pct":         _to_float(raw.get("realized_pnl_pct")),

        # Raw snapshot
        "raw_entry_order":          raw.get("raw_entry_order"),
    }


# ---------------------------------------------------------------------------
# Futures record mapper
# ---------------------------------------------------------------------------

def map_futures_record(raw: dict) -> dict:
    """Map one raw trade_futures.json record to the trades_futures column schema."""
    return {
        # Identity
        "symbol":                           raw.get("symbol"),
        "position_side":                    raw.get("position_side"),
        "direction":                        raw.get("direction"),
        "margin_budget":                    _to_float(raw.get("margin_budget")),
        "leverage":                         _to_int(raw.get("leverage")),
        "margin_mode":                      raw.get("margin_mode"),
        "rule_version":                     raw.get("rule_version"),
        "correlation_cluster_id":           raw.get("correlation_cluster_id"),

        # Entry order
        "entry_order_id":                   _to_int(raw.get("entry_order_id")),
        "entry_client_id":                  raw.get("entry_client_id"),
        "entry_status":                     raw.get("entry_status") or "UNKNOWN",
        "entry_price":                      _to_float(raw.get("entry_price")),
        "entry_fill_price":                 _to_float(raw.get("entry_fill_price")),
        "entry_fill_time":                  _to_int(raw.get("entry_fill_time")),
        "entry_qty":                        _to_float(raw.get("entry_qty")),
        "entry_notional":                   _to_float(raw.get("entry_notional")),
        "margin_used":                      _to_float(raw.get("margin_used")),
        "open_time":                        _parse_open_time(raw.get("open_time")),

        # Exit orders
        "tp_order_id":                      _to_int(raw.get("tp_order_id")),
        "sl_order_id":                      _to_int(raw.get("sl_order_id")),
        "tp_algo_id":                       _to_int(raw.get("tp_algo_id")),
        "sl_algo_id":                       _to_int(raw.get("sl_algo_id")),
        "exit_orders_placed":               _to_bool(raw.get("exit_orders_placed")),

        # Levels
        "sl":                               _to_float(raw.get("sl")),
        "tp1":                              _to_float(raw.get("tp1")),
        "tp2":                              _to_float(raw.get("tp2")),
        "entry_zone_center":                _to_float(raw.get("entry_zone_center")),
        "entry_zone_touches":               _to_int(raw.get("entry_zone_touches")),

        # Liquidation
        "liquidation_price":                _to_float(raw.get("liquidation_price")),
        "distance_to_liquidation_pct":      _to_float(raw.get("distance_to_liquidation_pct")),

        # Setup metadata
        "planned_rr":                       _to_float(raw.get("planned_rr")),
        "risk_pct":                         _to_float(raw.get("risk_pct")),
        "max_loss_usd":                     _to_float(raw.get("max_loss_usd")),
        "zone_type":                        raw.get("zone_type"),
        "zone_touches":                     _to_int(raw.get("zone_touches")),
        "atr_pct_at_entry":                 _to_float(raw.get("atr_pct_at_entry")),
        "volatility_regime_at_entry":       raw.get("volatility_regime_at_entry"),

        # Funding
        "funding_rate_at_entry":            _to_float(raw.get("funding_rate_at_entry")),
        "funding_rate_paid":                _to_float(raw.get("funding_rate_paid")),
        "funding_rate_history":             raw.get("funding_rate_history") or [],
        "last_funding_check_time":          _to_int(raw.get("last_funding_check_time")),

        # Cost estimates
        "fee_usd_roundtrip":                _to_float(raw.get("fee_usd_roundtrip")),
        "slippage_pct":                     _to_float(raw.get("slippage_pct")),
        "time_in_position_sec":             _to_int(raw.get("time_in_position_sec")),

        # Exit
        "exit_status":                      raw.get("exit_status") or "OPEN",
        "exit_price":                       _to_float(raw.get("exit_price")),
        "exit_time":                        _to_int(raw.get("exit_time")),
        "realized_pnl_usd":                 _to_float(raw.get("realized_pnl_usd")),
        "realized_pnl_pct":                 _to_float(raw.get("realized_pnl_pct")),

        # ML features
        "max_adverse_excursion_pct":        _to_float(raw.get("max_adverse_excursion_pct")),
        "max_favorable_excursion_pct":      _to_float(raw.get("max_favorable_excursion_pct")),
        "distance_to_liquidation_pct_min":  _to_float(raw.get("distance_to_liquidation_pct_min")),

        # Raw snapshot
        "raw_entry_order":                  raw.get("raw_entry_order"),
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_spot_record(rec: dict, index: int) -> list[str]:
    """Return list of validation warnings for a spot record (not hard errors)."""
    warnings = []
    if not rec.get("symbol"):
        warnings.append(f"  [spot #{index}] missing symbol")
    if rec.get("entry_order_id") is None:
        warnings.append(f"  [spot #{index}] missing entry_order_id")
    if rec.get("exit_status") not in ("OPEN","TP_HIT","SL_HIT","CANCELED","MANUALLY_CLOSED"):
        warnings.append(f"  [spot #{index}] unexpected exit_status: {rec.get('exit_status')!r}")
    if rec.get("direction") not in ("long","short"):
        warnings.append(f"  [spot #{index}] unexpected direction: {rec.get('direction')!r}")
    return warnings


def validate_futures_record(rec: dict, index: int) -> list[str]:
    warnings = []
    if not rec.get("symbol"):
        warnings.append(f"  [futures #{index}] missing symbol")
    if rec.get("entry_order_id") is None:
        warnings.append(f"  [futures #{index}] missing entry_order_id")
    if rec.get("exit_status") not in ("OPEN","TP_HIT","SL_HIT","CANCELED","MANUALLY_CLOSED"):
        warnings.append(f"  [futures #{index}] unexpected exit_status: {rec.get('exit_status')!r}")
    if rec.get("position_side") not in ("LONG","SHORT"):
        warnings.append(f"  [futures #{index}] unexpected position_side: {rec.get('position_side')!r}")
    return warnings


# ---------------------------------------------------------------------------
# Insert helpers — batched upsert with on_conflict = entry_order_id
# ---------------------------------------------------------------------------

BATCH_SIZE = 50   # Supabase recommends ≤100 rows per insert call


def insert_batched(client, table: str, rows: list[dict], dry_run: bool) -> tuple[int, int]:
    """
    Insert rows in batches. Uses upsert on entry_order_id so re-running is safe.
    Returns (inserted_count, error_count).
    """
    inserted = 0
    errors   = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]

        if dry_run:
            inserted += len(batch)
            continue

        try:
            result = (
                client.table(table)
                .upsert(batch, on_conflict="entry_order_id")
                .execute()
            )
            inserted += len(batch)
        except Exception as e:
            print(f"  ❌  Batch {start}–{start+len(batch)} failed: {e}")
            errors += len(batch)

    return inserted, errors


# ---------------------------------------------------------------------------
# Migrate spot
# ---------------------------------------------------------------------------

def migrate_spot(client, dry_run: bool) -> None:
    print("\n" + "=" * 60)
    print(f"SPOT  —  source: {SPOT_LOG_PATH}")
    print("=" * 60)

    if not SPOT_LOG_PATH.exists():
        print("  ⚠  trade_log.json not found — skipping spot migration.")
        return

    with open(SPOT_LOG_PATH) as f:
        raw_trades = json.load(f)

    print(f"  Records in trade_log.json   : {len(raw_trades)}")

    # Count typo field occurrences for transparency
    typo_count = sum(1 for t in raw_trades if "entry_statusa" in t and "entry_status" not in t)
    if typo_count:
        print(f"  Records with 'entry_statusa' typo: {typo_count} — will be remapped to entry_status")

    # Map all records
    mapped   = []
    all_warnings = []
    for i, raw in enumerate(raw_trades):
        rec = map_spot_record(raw)
        warnings = validate_spot_record(rec, i)
        all_warnings.extend(warnings)
        mapped.append(rec)

    if all_warnings:
        print(f"\n  Validation warnings ({len(all_warnings)}):")
        for w in all_warnings:
            print(w)
    else:
        print("  Validation: ✅ no warnings")

    print(f"\n  {'[DRY RUN] Would insert' if dry_run else 'Inserting'} {len(mapped)} rows "
          f"into trades_spot ...")

    inserted, errors = insert_batched(client, "trades_spot", mapped, dry_run)

    print(f"  {'Simulated' if dry_run else 'Inserted'}: {inserted}  |  Errors: {errors}")

    if not dry_run and errors == 0:
        # Verify row count in Supabase
        try:
            result = client.table("trades_spot").select("id", count="exact").execute()
            db_count = result.count
            local_count = len(mapped)
            match = "✅ MATCH" if db_count >= local_count else "⚠ MISMATCH"
            print(f"\n  Row count check:")
            print(f"    Local JSON : {local_count}")
            print(f"    Supabase   : {db_count}  {match}")
            if db_count < local_count:
                print(f"    ⚠  {local_count - db_count} row(s) missing — check errors above")
        except Exception as e:
            print(f"  ⚠  Could not verify row count: {e}")


# ---------------------------------------------------------------------------
# Migrate futures
# ---------------------------------------------------------------------------

def migrate_futures(client, dry_run: bool) -> None:
    print("\n" + "=" * 60)
    print(f"FUTURES  —  source: {FUTURES_LOG_PATH}")
    print("=" * 60)

    if not FUTURES_LOG_PATH.exists():
        print("  ⚠  trade_futures.json not found — skipping futures migration.")
        return

    with open(FUTURES_LOG_PATH) as f:
        raw_trades = json.load(f)

    print(f"  Records in trade_futures.json: {len(raw_trades)}")

    mapped      = []
    all_warnings = []
    for i, raw in enumerate(raw_trades):
        rec = map_futures_record(raw)
        warnings = validate_futures_record(rec, i)
        all_warnings.extend(warnings)
        mapped.append(rec)

    if all_warnings:
        print(f"\n  Validation warnings ({len(all_warnings)}):")
        for w in all_warnings:
            print(w)
    else:
        print("  Validation: ✅ no warnings")

    print(f"\n  {'[DRY RUN] Would insert' if dry_run else 'Inserting'} {len(mapped)} rows "
          f"into trades_futures ...")

    inserted, errors = insert_batched(client, "trades_futures", mapped, dry_run)

    print(f"  {'Simulated' if dry_run else 'Inserted'}: {inserted}  |  Errors: {errors}")

    if not dry_run and errors == 0:
        try:
            result = client.table("trades_futures").select("id", count="exact").execute()
            db_count = result.count
            local_count = len(mapped)
            match = "✅ MATCH" if db_count >= local_count else "⚠ MISMATCH"
            print(f"\n  Row count check:")
            print(f"    Local JSON : {local_count}")
            print(f"    Supabase   : {db_count}  {match}")
            if db_count < local_count:
                print(f"    ⚠  {local_count - db_count} row(s) missing — check errors above")
        except Exception as e:
            print(f"  ⚠  Could not verify row count: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time migration of local JSON trade logs to Supabase."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate records but do NOT write to Supabase",
    )
    parser.add_argument(
        "--only", choices=["spot", "futures"], default=None,
        help="Migrate only one table (default: both)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Trade Log → Supabase Migration")
    print("=" * 60)
    if args.dry_run:
        print("MODE: DRY RUN — no data will be written to Supabase")
    else:
        print("MODE: LIVE — data will be inserted into Supabase")
        print("      JSON files will NOT be deleted or modified.")
    print()

    client = get_supabase_client()

    if args.only == "spot":
        migrate_spot(client, dry_run=args.dry_run)
    elif args.only == "futures":
        migrate_futures(client, dry_run=args.dry_run)
    else:
        migrate_spot(client, dry_run=args.dry_run)
        migrate_futures(client, dry_run=args.dry_run)

    print("\n" + "=" * 60)
    if args.dry_run:
        print("Dry run complete — no data written.")
        print("Run without --dry-run to execute migration.")
    else:
        print("Migration complete.")
        print("Next steps:")
        print("  1. Open Supabase table editor and spot-check a few records")
        print("  2. Verify row count matches the numbers printed above")
        print("  3. Then proceed to update paper_trade_executor.py and")
        print("     futures_trade_executor.py to read/write from Supabase")
    print("=" * 60)


if __name__ == "__main__":
    main()
