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
import sys
import time
from functools import lru_cache
from typing import Any

from services.timing_logger import log_timing

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def is_test_environment() -> bool:
    """
    Check if the current process is running in a test or evaluation environment.
    Uses defense-in-depth:
    1. APP_ENV == 'test'
    2. TESTING env var set to 'true' or '1'
    3. pytest in sys.modules
    4. sys.argv contains test runner names or test file patterns.
    """
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env == "test":
        return True
    if os.getenv("TESTING", "").strip().lower() in ("true", "1"):
        return True
    if "pytest" in sys.modules:
        return True
    for arg in sys.argv:
        base = os.path.basename(arg).lower()
        if "unittest" in arg or "pytest" in arg or "test" in base:
            return True
    return False


def is_production_environment() -> bool:
    """
    Check if the current process is explicitly configured for production.
    Fail-closed:
    - If in test environment: NEVER production (returns False).
    - If APP_ENV is anything other than 'production': returns False.
    """
    if is_test_environment():
        return False
    app_env = os.getenv("APP_ENV", "").strip().lower()
    return app_env == "production"


def _assert_safe_write_environment(caller_name: str) -> None:
    """
    Enforce strict fail-closed database mutation safety.
    Rejects:
    1. Any test environment (unittest, pytest, custom runner, test subprocess).
       ALLOW_PRODUCTION_DB_WRITE is strictly ignored and cannot override this.
    2. Any non-production environment (unset APP_ENV, development, test).
       Production writes are only permitted when APP_ENV == 'production'.
    """
    if is_test_environment():
        raise RuntimeError(
            f"🚨 CRITICAL PRODUCTION DATABASE WRITE BLOCKED: {caller_name}() was called "
            f"in a test environment! All database mutations in tests must be mocked. "
            f"(ALLOW_PRODUCTION_DB_WRITE cannot override test environment protection)"
        )

    if not is_production_environment():
        app_env = os.getenv("APP_ENV", "").strip() or "<unset>"
        raise RuntimeError(
            f"🚨 CRITICAL PRODUCTION DATABASE WRITE BLOCKED: {caller_name}() was called "
            f"in non-production environment (APP_ENV={app_env}). "
            f"Writes to production Supabase are strictly fail-closed unless APP_ENV=production."
        )


class _GuardedDeleteBuilder:
    """
    Wrapper around PostgREST delete query builder to prevent accidental mass deletion.
    Enforces:
    1. Unfiltered delete() is strictly blocked.
    2. Empty or None filter values are blocked.
    3. At least one filter must target an identifier column ('id', 'entry_order_id', 'order_id').
    4. Broad deletes (e.g. only on non-identifier columns like 'symbol') are blocked.
    """

    def __init__(self, builder, table_name: str = "<unknown>"):
        self._builder = builder
        self._table_name = table_name
        self._filters_applied: list[tuple[str, str, Any]] = []

    def eq(self, column: str, value: Any):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                f"🚨 DANGEROUS DELETE BLOCKED on table '{self._table_name}': "
                f".eq('{column}', {value!r}) has empty or null value."
            )
        self._filters_applied.append((column, "eq", value))
        self._builder = self._builder.eq(column, value)
        return self

    def in_(self, column: str, values: Any):
        if not values or not all(v is not None and (not isinstance(v, str) or v.strip()) for v in values):
            raise ValueError(
                f"🚨 DANGEROUS DELETE BLOCKED on table '{self._table_name}': "
                f".in_('{column}', ...) must contain valid, non-empty values."
            )
        self._filters_applied.append((column, "in", values))
        self._builder = self._builder.in_(column, values)
        return self

    def execute(self, *args, **kwargs):
        if not self._filters_applied:
            raise RuntimeError(
                f"🚨 MASS DELETE BLOCKED on table '{self._table_name}': "
                f"Attempted to execute delete() without any filter! "
                f"Unfiltered deletion of table data is strictly forbidden."
            )
        target_cols = {col for col, op, val in self._filters_applied}
        allowed_id_cols = {"id", "entry_order_id", "order_id"}
        if not (target_cols & allowed_id_cols):
            raise RuntimeError(
                f"🚨 BROAD/MASS DELETE BLOCKED on table '{self._table_name}': "
                f"DELETE must target specific identifier columns ({', '.join(allowed_id_cols)}). "
                f"Filters applied: {self._filters_applied}"
            )
        return self._builder.execute(*args, **kwargs)

    def __getattr__(self, name):
        attr = getattr(self._builder, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                res = attr(*args, **kwargs)
                if res is self._builder:
                    return self
                return res
            return wrapper
        return attr


class _GuardedTable:
    """Wrapper around a Supabase table/builder ensuring all write operations invoke safety guards."""

    def __init__(self, table, table_name: str = "<unknown>"):
        self._table = table
        self._table_name = table_name

    def insert(self, *args, **kwargs):
        _assert_safe_write_environment(f"client.table('{self._table_name}').insert")
        return self._table.insert(*args, **kwargs)

    def upsert(self, *args, **kwargs):
        _assert_safe_write_environment(f"client.table('{self._table_name}').upsert")
        return self._table.upsert(*args, **kwargs)

    def update(self, *args, **kwargs):
        _assert_safe_write_environment(f"client.table('{self._table_name}').update")
        return self._table.update(*args, **kwargs)

    def delete(self, *args, **kwargs):
        _assert_safe_write_environment(f"client.table('{self._table_name}').delete")
        raw_delete = self._table.delete(*args, **kwargs)
        return _GuardedDeleteBuilder(raw_delete, self._table_name)

    def __getattr__(self, name):
        return getattr(self._table, name)


class _GuardedClient:
    """Wrapper around a Supabase Client ensuring table and RPC operations are safely proxied."""

    def __init__(self, client):
        self.__raw_client = client

    def table(self, table_name: str):
        raw_table = self.__raw_client.table(table_name)
        return _GuardedTable(raw_table, table_name)

    def from_(self, table_name: str):
        raw_table = getattr(self.__raw_client, "from_")(table_name)
        return _GuardedTable(raw_table, table_name)

    def schema(self, schema_name: str):
        raw_schema = self.__raw_client.schema(schema_name)
        return _GuardedClient(raw_schema)

    def rpc(self, fn: str, params: dict | None = None):
        """
        Remote procedure calls may execute mutating SQL functions.
        Fail-closed policy: require safe write environment before invoking RPC.
        """
        _assert_safe_write_environment(f"client.rpc({fn})")
        return self.__raw_client.rpc(fn, params or {})

    def __getattr__(self, name):
        if name in ("_client", "raw_client", "_raw_client"):
            raise AttributeError(f"Direct access to internal raw client '{name}' is restricted for safety.")
        return getattr(self.__raw_client, name)


@lru_cache(maxsize=1)
def get_client():
    """
    Return a cached Supabase client.
    Raises RuntimeError on missing / placeholder credentials or unmocked test access.
    Raises ImportError if supabase-py is not installed.
    """
    # Guard against unmocked production client instantiation during test execution
    if is_test_environment() and not os.getenv("TEST_SUPABASE_URL"):
        # Allow if create_client is mocked by unittest.mock
        try:
            from supabase import create_client as _raw_create_client
            is_mocked = hasattr(_raw_create_client, "mock_calls") or hasattr(_raw_create_client, "return_value")
        except ImportError:
            is_mocked = False

        if not is_mocked:
            raise RuntimeError(
                "🚨 CRITICAL: Cannot instantiate production Supabase client during test execution without mocking. "
                "Mock get_client() or configure TEST_SUPABASE_URL."
            )

    try:
        from supabase import create_client
    except ImportError as exc:
        raise ImportError(
            "supabase-py not installed.\n"
            "    pip3 install supabase --break-system-packages"
        ) from exc

    url = os.getenv("TEST_SUPABASE_URL", "").strip() or os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("TEST_SUPABASE_KEY", "").strip() or os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    placeholders = ("your_", "paste_", "replace_", "changeme", "<project")
    for p in placeholders:
        if p in url.lower() or p in key.lower():
            raise RuntimeError(
                ".env still contains placeholder values — fill in real Supabase credentials."
            )

    raw_client = create_client(url, key)
    return _GuardedClient(raw_client)


# ---------------------------------------------------------------------------
# Table constants — single source of truth
# ---------------------------------------------------------------------------
TABLE_SPOT = "trades_spot"
TABLE_FUTURES = "trades_futures"
TABLE_HEARTBEAT = "system_heartbeat"  # bot liveness + next cycle promise
TABLE_TOKOCRYPTO = "Toko_Crypto_Spot"


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
    _assert_safe_write_environment("upsert_spot")
    get_client().table(TABLE_SPOT).upsert(
        record, on_conflict="entry_order_id"
    ).execute()


def upsert_futures(record: dict) -> None:
    """Insert or update a single futures trade row (keyed on entry_order_id)."""
    _assert_safe_write_environment("upsert_futures")
    get_client().table(TABLE_FUTURES).upsert(
        record, on_conflict="entry_order_id"
    ).execute()


def update_spot_by_order_id(entry_order_id: int, fields: dict) -> None:
    """Patch specific fields on an existing spot row."""
    _assert_safe_write_environment("update_spot_by_order_id")
    (
        get_client()
        .table(TABLE_SPOT)
        .update(fields)
        .eq("entry_order_id", entry_order_id)
        .execute()
    )


def update_futures_by_order_id(entry_order_id: int, fields: dict) -> None:
    """Patch specific fields on an existing futures row."""
    _assert_safe_write_environment("update_futures_by_order_id")
    (
        get_client()
        .table(TABLE_FUTURES)
        .update(fields)
        .eq("entry_order_id", entry_order_id)
        .execute()
    )


def send_heartbeat():
    try:
        from datetime import datetime, timezone

        rows = fetch_all_spot()
        if rows:
            last_id = rows[-1]["entry_order_id"]  # Use the most recent record
            now_iso = datetime.now(timezone.utc).isoformat()
            update_spot_by_order_id(last_id, {"updated_at": now_iso})
            print(f"💓 [HEARTBEAT] Pushed for Spot Order ID: {last_id}")
    except RuntimeError:
        raise
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
    _assert_safe_write_environment("upsert_heartbeat")
    try:
        get_client().table(TABLE_HEARTBEAT).upsert(
            {
                "id": 1,
                "last_seen_at": last_seen_at,
                "next_expected_at": next_expected_at,
                "updated_at": last_seen_at,
            },
            on_conflict="id",
        ).execute()
    except RuntimeError:
        raise
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


def fetch_all_tokocrypto() -> list[dict]:
    """Return all rows from Toko_Crypto_Spot. Returns [] if table does not exist."""
    try:
        _t0 = time.perf_counter()
        client = get_client()
        result = client.table(TABLE_TOKOCRYPTO).select("*").order("id").execute()
        _elapsed_ms = (time.perf_counter() - _t0) * 1000
        log_timing(f"[TIMING] query_fetch_all_tokocrypto: {_elapsed_ms:.0f}ms")
        return result.data or []
    except Exception:
        return []  # graceful: table may not exist yet


def fetch_all_tokocrypto_strict() -> list[dict]:
    """Fetch Tokocrypto rows without masking storage errors for safety-critical decisions."""
    _t0 = time.perf_counter()
    client = get_client()
    result = client.table(TABLE_TOKOCRYPTO).select("*").order("id").execute()
    _elapsed_ms = (time.perf_counter() - _t0) * 1000
    log_timing(f"[TIMING] query_fetch_all_tokocrypto_strict: {_elapsed_ms:.0f}ms")
    return result.data or []


def upsert_tokocrypto(record: dict) -> None:
    _assert_safe_write_environment("upsert_tokocrypto")
    try:
        get_client().table(TABLE_TOKOCRYPTO).upsert(
            record, on_conflict="entry_order_id"
        ).execute()
    except Exception as e:
        print(f"⚠️ upsert_tokocrypto failed: {e}")
        raise


def update_tokocrypto_by_order_id(entry_order_id: str, fields: dict) -> None:
    _assert_safe_write_environment("update_tokocrypto_by_order_id")
    try:
        (
            get_client()
            .table(TABLE_TOKOCRYPTO)
            .update(fields)
            .eq("entry_order_id", entry_order_id)
            .execute()
        )
    except Exception as e:
        print(f"⚠️ update_tokocrypto_by_order_id failed: {e}")
        raise
