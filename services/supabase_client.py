"""
supabase_client.py — Shared Supabase client for all modules.
=============================================================
Used by paper_trade_executor.py, futures_trade_executor.py, and dashboard.py.

Required .env keys:
    SUPABASE_URL         = https://<project-id>.supabase.co
    SUPABASE_SERVICE_KEY = <service_role_key>   # NOT anon key

The client is initialised once at import time and cached in _CLIENT.
Call get_client() everywhere — it returns the cached instance.
"""

from __future__ import annotations

import os
import time
from functools import lru_cache

from services.timing_logger import log_timing

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@lru_cache(maxsize=1)
def get_client():
    """
    Return a cached Supabase client.
    Raises RuntimeError on missing / placeholder credentials.
    Raises ImportError if supabase-py is not installed.
    """
    try:
        from supabase import create_client
    except ImportError as exc:
        raise ImportError(
            "supabase-py not installed.\n"
            "    pip3 install supabase --break-system-packages"
        ) from exc

    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
        )

    placeholders = ("your_", "paste_", "replace_", "changeme", "<project")
    for p in placeholders:
        if p in url.lower() or p in key.lower():
            raise RuntimeError(
                ".env still contains placeholder values — fill in real Supabase credentials."
            )

    return create_client(url, key)


# ---------------------------------------------------------------------------
# Table constants — single source of truth
# ---------------------------------------------------------------------------
TABLE_SPOT      = "trades_spot"
TABLE_FUTURES   = "trades_futures"
TABLE_HEARTBEAT = "system_heartbeat"   # bot liveness + next cycle promise


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def fetch_all_spot() -> list[dict]:
    """
    Return all rows from trades_spot as list[dict].
    Columns match the JSON schema used by paper_trade_executor.py
    (field names are identical to trade_log.json keys).
    """
    _t0 = time.perf_counter()
    client = get_client()
    result = client.table(TABLE_SPOT).select("*").order("id").execute()
    _elapsed_ms = (time.perf_counter() - _t0) * 1000
    log_timing(f"[TIMING] query_fetch_all_spot: {_elapsed_ms:.0f}ms")
    return result.data or []


def fetch_all_futures() -> list[dict]:
    """
    Return all rows from trades_futures as list[dict].
    Field names match trade_futures.json keys.
    """
    _t0 = time.perf_counter()
    client = get_client()
    result = client.table(TABLE_FUTURES).select("*").order("id").execute()
    _elapsed_ms = (time.perf_counter() - _t0) * 1000
    log_timing(f"[TIMING] query_fetch_all_futures: {_elapsed_ms:.0f}ms")
    return result.data or []


def upsert_spot(record: dict) -> None:
    """Insert or update a single spot trade row (keyed on entry_order_id)."""
    get_client().table(TABLE_SPOT).upsert(record, on_conflict="entry_order_id").execute()


def upsert_futures(record: dict) -> None:
    """Insert or update a single futures trade row (keyed on entry_order_id)."""
    get_client().table(TABLE_FUTURES).upsert(record, on_conflict="entry_order_id").execute()


def update_spot_by_order_id(entry_order_id: int, fields: dict) -> None:
    """Patch specific fields on an existing spot row."""
    (get_client()
     .table(TABLE_SPOT)
     .update(fields)
     .eq("entry_order_id", entry_order_id)
     .execute())


def update_futures_by_order_id(entry_order_id: int, fields: dict) -> None:
    """Patch specific fields on an existing futures row."""
    (get_client()
     .table(TABLE_FUTURES)
     .update(fields)
     .eq("entry_order_id", entry_order_id)
     .execute())


def send_heartbeat():
    try:
        from datetime import datetime, timezone
        rows = fetch_all_spot()
        if rows:
            last_id = rows[-1]['entry_order_id'] # Use the most recent record
            now_iso = datetime.now(timezone.utc).isoformat()
            update_spot_by_order_id(last_id, {"updated_at": now_iso})
            print(f"💓 [HEARTBEAT] Pushed for Spot Order ID: {last_id}")
    except Exception as e:
        print(f"⚠️ [HEARTBEAT] Skipped: {e}")


def upsert_heartbeat(last_seen_at: str, next_expected_at: str) -> None:
    """
    Upsert a single row into system_heartbeat (id=1, always the same row).
    Fields:
        last_seen_at     — ISO timestamp (UTC) when the cycle completed
        next_expected_at — ISO timestamp (UTC) of the next promised cycle

    Table DDL (run once in Supabase SQL Editor):
        CREATE TABLE IF NOT EXISTS system_heartbeat (
            id               int PRIMARY KEY DEFAULT 1,
            last_seen_at     timestamptz NOT NULL,
            next_expected_at timestamptz NOT NULL,
            updated_at       timestamptz DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS system_heartbeat_singleton
            ON system_heartbeat (id);
    """
    try:
        get_client().table(TABLE_HEARTBEAT).upsert(
            {
                "id":               1,
                "last_seen_at":     last_seen_at,
                "next_expected_at": next_expected_at,
                "updated_at":       last_seen_at,
            },
            on_conflict="id",
        ).execute()
    except Exception as e:
        print(f"⚠️ [HEARTBEAT] upsert_heartbeat failed: {e}")


def fetch_heartbeat() -> dict | None:
    """
    Fetch the single system_heartbeat row.
    Returns dict with last_seen_at / next_expected_at, or None if table not yet created.
    """
    _t0 = time.perf_counter()
    try:
        result = get_client().table(TABLE_HEARTBEAT).select("*").eq("id", 1).execute()
        return result.data[0] if result.data else None
    except Exception:
        return None
    finally:
        _elapsed_ms = (time.perf_counter() - _t0) * 1000
        log_timing(f"[TIMING] query_fetch_heartbeat: {_elapsed_ms:.0f}ms")

