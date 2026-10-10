"""
test_database_safety_guard.py — Comprehensive regression test suite for
fail-closed Supabase production write protection.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import services.supabase_client as sbc


class TestDatabaseSafetyGuard(unittest.TestCase):
    def setUp(self):
        # Clear LRU cache before each test
        sbc.get_client.cache_clear()

    def tearDown(self):
        sbc.get_client.cache_clear()

    # -------------------------------------------------------------------------
    # 1. Environment kosong: akses tulis production ditolak
    # -------------------------------------------------------------------------
    def test_01_empty_environment_blocks_writes(self):
        """When APP_ENV is unset and no test indicators present, writes must fail-closed."""
        env_clean = {k: v for k, v in os.environ.items() if k not in ("APP_ENV", "TESTING", "ALLOW_PRODUCTION_DB_WRITE")}
        with patch.dict(os.environ, env_clean, clear=True):
            with patch.object(sys, "argv", ["runner.py"]):
                with patch.dict(sys.modules, {k: v for k, v in sys.modules.items() if "pytest" not in k}):
                    self.assertFalse(sbc.is_production_environment())
                    with self.assertRaises(RuntimeError) as ctx:
                        sbc._assert_safe_write_environment("test_caller")
                    self.assertIn("non-production environment (APP_ENV=<unset>)", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 2. APP_ENV=test: akses client dan operasi tulis production ditolak
    # -------------------------------------------------------------------------
    def test_02_app_env_test_blocks_client_and_writes(self):
        """When APP_ENV=test, unmocked client instantiation and writes must be blocked."""
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
            self.assertTrue(sbc.is_test_environment())
            self.assertFalse(sbc.is_production_environment())

            # Writes blocked
            with self.assertRaises(RuntimeError) as ctx:
                sbc._assert_safe_write_environment("upsert_tokocrypto")
            self.assertIn("CRITICAL PRODUCTION DATABASE WRITE BLOCKED", str(ctx.exception))

            # Unmocked get_client blocked if attempting to connect to production
            with patch.dict(os.environ, {"TEST_SUPABASE_URL": ""}):
                with self.assertRaises(RuntimeError) as ctx_client:
                    sbc.get_client()
                self.assertIn("Cannot instantiate production Supabase client during test execution", str(ctx_client.exception))

    # -------------------------------------------------------------------------
    # 3. Custom runner tanpa kata 'test': tetap aman
    # -------------------------------------------------------------------------
    def test_03_custom_runner_without_test_in_name_is_safe(self):
        """Custom scripts (e.g. runner.py, sim.py) without APP_ENV=production cannot write."""
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            with patch.object(sys, "argv", ["simulate_portfolio.py", "--fast"]):
                with patch.dict(sys.modules, {k: v for k, v in sys.modules.items() if "pytest" not in k}):
                    self.assertFalse(sbc.is_production_environment())
                    with self.assertRaises(RuntimeError) as ctx:
                        sbc._assert_safe_write_environment("custom_caller")
                    self.assertIn("non-production environment (APP_ENV=development)", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 4. Subprocess test: tetap aman
    # -------------------------------------------------------------------------
    def test_04_subprocess_test_inherits_protection(self):
        """Subprocess spawned during testing inherits APP_ENV=test and blocks writes."""
        code = (
            "import os, sys\n"
            "from services.supabase_client import upsert_tokocrypto\n"
            "try:\n"
            "    upsert_tokocrypto({'symbol': 'BTC_IDR'})\n"
            "    sys.exit(0)\n"
            "except RuntimeError as e:\n"
            "    sys.exit(42)\n"
        )
        env = os.environ.copy()
        env["APP_ENV"] = "test"
        env["TESTING"] = "true"
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 42, f"Subprocess should have failed with exit code 42, got {proc.returncode}")

    # -------------------------------------------------------------------------
    # 5. ALLOW_PRODUCTION_DB_WRITE=true saat test: tetap ditolak
    # -------------------------------------------------------------------------
    def test_05_allow_production_db_write_cannot_bypass_during_test(self):
        """ALLOW_PRODUCTION_DB_WRITE=true must NEVER allow writes when in test environment."""
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true", "ALLOW_PRODUCTION_DB_WRITE": "true"}):
            self.assertTrue(sbc.is_test_environment())
            with self.assertRaises(RuntimeError) as ctx:
                sbc._assert_safe_write_environment("upsert_tokocrypto")
            self.assertIn("ALLOW_PRODUCTION_DB_WRITE cannot override test environment protection", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 6. Akses langsung melalui get_client(): tidak bisa melewati keamanan
    # -------------------------------------------------------------------------
    def test_06_direct_client_table_mutation_blocked(self):
        """Direct access via client.table().insert/upsert/update/delete triggers guard."""
        mock_raw_client = MagicMock()
        mock_table = MagicMock()
        mock_raw_client.table.return_value = mock_table

        guarded = sbc._GuardedClient(mock_raw_client)
        tbl = guarded.table("Toko_Crypto_Spot")

        # Test insert, upsert, update, delete directly on table
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
            with self.assertRaises(RuntimeError):
                tbl.insert({"symbol": "BTC_IDR"})
            with self.assertRaises(RuntimeError):
                tbl.upsert({"symbol": "BTC_IDR"})
            with self.assertRaises(RuntimeError):
                tbl.update({"symbol": "BTC_IDR"})
            with self.assertRaises(RuntimeError):
                tbl.delete()

        # Raw methods must never have been called on mock_table
        mock_table.insert.assert_not_called()
        mock_table.upsert.assert_not_called()
        mock_table.update.assert_not_called()
        mock_table.delete.assert_not_called()

    # -------------------------------------------------------------------------
    # 7. Wrapper penulisan: semua mengikuti kebijakan yang sama
    # -------------------------------------------------------------------------
    def test_07_all_write_wrappers_enforce_policy(self):
        """All write wrapper functions in supabase_client raise RuntimeError when unmocked in test."""
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
            with self.assertRaises(RuntimeError):
                sbc.upsert_spot({"symbol": "BTC"})
            with self.assertRaises(RuntimeError):
                sbc.upsert_futures({"symbol": "BTC"})
            with self.assertRaises(RuntimeError):
                sbc.update_spot_by_order_id(123, {"status": "FILLED"})
            with self.assertRaises(RuntimeError):
                sbc.update_futures_by_order_id(123, {"status": "FILLED"})
            with self.assertRaises(RuntimeError):
                sbc.upsert_heartbeat("2026-10-10T00:00:00Z", "2026-10-10T01:00:00Z")
            with self.assertRaises(RuntimeError):
                sbc.upsert_tokocrypto({"symbol": "BTC_IDR"})
            with self.assertRaises(RuntimeError):
                sbc.update_tokocrypto_by_order_id("123", {"status": "FILLED"})

    # -------------------------------------------------------------------------
    # 8. Skrip migrasi: tidak dapat melakukan mutasi tanpa otorisasi eksplisit
    # -------------------------------------------------------------------------
    def test_08_migration_script_write_blocked_without_explicit_authorization(self):
        """Migration insert_batched raises RuntimeError unless APP_ENV=production and ALLOW_MIGRATION_WRITE=true."""
        import scripts.migration_to_supabase as mig

        mock_client = MagicMock()
        rows = [{"entry_order_id": 1, "symbol": "BTC_IDR"}]

        # Under test env: blocked
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
            with self.assertRaises(RuntimeError) as ctx:
                mig.insert_batched(mock_client, "trades_spot", rows, dry_run=False)
            self.assertIn("Migration writes to Supabase are strictly BLOCKED in test environment", str(ctx.exception))

        # Under development env: blocked
        with patch.dict(os.environ, {"APP_ENV": "development", "ALLOW_MIGRATION_WRITE": "false"}, clear=True):
            with patch.object(sys, "argv", ["migration_to_supabase.py"]):
                with patch.dict(sys.modules, {k: v for k, v in sys.modules.items() if "pytest" not in k}):
                    with self.assertRaises(RuntimeError) as ctx_dev:
                        mig.insert_batched(mock_client, "trades_spot", rows, dry_run=False)
                    self.assertIn("require APP_ENV=production and ALLOW_MIGRATION_WRITE=true", str(ctx_dev.exception))

        # Dry run: allowed without writing to client
        inserted, errors = mig.insert_batched(mock_client, "trades_spot", rows, dry_run=True)
        self.assertEqual(inserted, 1)
        self.assertEqual(errors, 0)
        mock_client.table.assert_not_called()

    # -------------------------------------------------------------------------
    # 9. Environment production yang sah tetap bisa beroperasi melalui mock
    # -------------------------------------------------------------------------
    def test_09_legitimate_production_environment_succeeds_with_mock(self):
        """In APP_ENV=production (non-test), writes are allowed through mocked client."""
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                self.assertTrue(sbc.is_production_environment())
                # _assert_safe_write_environment must succeed without exception
                sbc._assert_safe_write_environment("upsert_tokocrypto")

                # Test calling wrapper with mock get_client
                with patch("services.supabase_client.get_client") as mock_gc:
                    mock_table = MagicMock()
                    mock_gc.return_value.table.return_value = mock_table
                    sbc.upsert_tokocrypto({"symbol": "BTC_IDR", "entry_order_id": "REAL_123"})
                    mock_table.upsert.assert_called_once()

    # -------------------------------------------------------------------------
    # 10. Jalur exception tidak boleh menelan pelanggaran kebijakan
    # -------------------------------------------------------------------------
    def test_10_exception_handlers_do_not_swallow_security_policy_errors(self):
        """send_heartbeat must re-raise RuntimeError if write guard triggers."""
        with patch("services.supabase_client.fetch_all_spot", return_value=[{"entry_order_id": 999}]):
            with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
                with self.assertRaises(RuntimeError) as ctx:
                    sbc.send_heartbeat()
                self.assertIn("CRITICAL PRODUCTION DATABASE WRITE BLOCKED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 11. Akses atribut client internal dibatasi untuk mencegah bypass
    # -------------------------------------------------------------------------
    def test_11_raw_client_attribute_access_restricted(self):
        """Direct access to _client, raw_client, _raw_client on _GuardedClient must be blocked."""
        mock_raw = MagicMock()
        guarded = sbc._GuardedClient(mock_raw)

        for attr in ("_client", "raw_client", "_raw_client"):
            with self.assertRaises(AttributeError) as ctx:
                getattr(guarded, attr)
            self.assertIn("restricted for safety", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 12. RPC calls diintersepsi dan ditolak di luar production yang valid
    # -------------------------------------------------------------------------
    def test_12_rpc_calls_blocked_during_test_environment(self):
        """Remote procedure calls must be blocked in test environment and allowed in mock production."""
        mock_raw = MagicMock()
        guarded = sbc._GuardedClient(mock_raw)

        # In test environment: rpc must raise RuntimeError
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
            with self.assertRaises(RuntimeError) as ctx:
                guarded.rpc("execute_custom_sql", {"param": 1})
            self.assertIn("client.rpc(execute_custom_sql)", str(ctx.exception))
        mock_raw.rpc.assert_not_called()

        # In legitimate production environment: rpc succeeds through mock
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                guarded.rpc("get_status")
                mock_raw.rpc.assert_called_once_with("get_status", {})

    # -------------------------------------------------------------------------
    # 13. Schema chaining mempertahankan _GuardedClient dan _GuardedTable
    # -------------------------------------------------------------------------
    def test_13_schema_chaining_maintains_guard(self):
        """client.schema().table().insert() must remain guarded."""
        mock_raw = MagicMock()
        mock_schema_client = MagicMock()
        mock_raw.schema.return_value = mock_schema_client
        mock_table = MagicMock()
        mock_schema_client.table.return_value = mock_table

        guarded = sbc._GuardedClient(mock_raw)
        guarded_schema = guarded.schema("custom_schema")

        # Must return another _GuardedClient instance
        self.assertIsInstance(guarded_schema, sbc._GuardedClient)

        tbl = guarded_schema.table("some_table")
        with patch.dict(os.environ, {"APP_ENV": "test", "TESTING": "true"}):
            with self.assertRaises(RuntimeError):
                tbl.insert({"key": "val"})

        mock_table.insert.assert_not_called()

    # -------------------------------------------------------------------------
    # 14. DELETE tanpa filter ditolak (mencegah penghapusan seluruh tabel)
    # -------------------------------------------------------------------------
    def test_14_delete_without_filter_blocked(self):
        """Unfiltered delete().execute() must be blocked with RuntimeError."""
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                mock_raw = MagicMock()
                mock_table = MagicMock()
                mock_delete_builder = MagicMock()
                mock_raw.table.return_value = mock_table
                mock_table.delete.return_value = mock_delete_builder

                guarded = sbc._GuardedClient(mock_raw)
                tbl = guarded.table("trades_spot")

                # delete().execute() with NO filter must raise RuntimeError
                with self.assertRaises(RuntimeError) as ctx:
                    tbl.delete().execute()

                self.assertIn("MASS DELETE BLOCKED", str(ctx.exception))
                mock_delete_builder.execute.assert_not_called()

    # -------------------------------------------------------------------------
    # 15. DELETE dengan filter non-ID yang berpotensi massal ditolak
    # -------------------------------------------------------------------------
    def test_15_mass_delete_by_broad_filter_blocked(self):
        """DELETE filtering only by broad non-identifier columns (e.g. 'symbol') must be blocked."""
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                mock_raw = MagicMock()
                mock_table = MagicMock()
                mock_delete_builder = MagicMock()
                mock_raw.table.return_value = mock_table
                mock_table.delete.return_value = mock_delete_builder

                guarded = sbc._GuardedClient(mock_raw)
                tbl = guarded.table("trades_spot")

                # Deleting by symbol alone is a mass-delete risk
                with self.assertRaises(RuntimeError) as ctx:
                    tbl.delete().eq("symbol", "BTC_IDR").execute()

                self.assertIn("BROAD/MASS DELETE BLOCKED", str(ctx.exception))
                mock_delete_builder.execute.assert_not_called()

    # -------------------------------------------------------------------------
    # 16. DELETE dengan nilai filter null, string kosong, atau list kosong ditolak
    # -------------------------------------------------------------------------
    def test_16_delete_with_empty_or_null_id_blocked(self):
        """DELETE with None, empty string, or empty list must raise ValueError immediately."""
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                mock_raw = MagicMock()
                mock_table = MagicMock()
                mock_delete_builder = MagicMock()
                mock_raw.table.return_value = mock_table
                mock_table.delete.return_value = mock_delete_builder

                guarded = sbc._GuardedClient(mock_raw)
                tbl = guarded.table("trades_spot")

                with self.assertRaises(ValueError):
                    tbl.delete().eq("id", None)

                with self.assertRaises(ValueError):
                    tbl.delete().eq("id", "")

                with self.assertRaises(ValueError):
                    tbl.delete().eq("id", "   ")

                with self.assertRaises(ValueError):
                    tbl.delete().in_("id", [])

                with self.assertRaises(ValueError):
                    tbl.delete().in_("id", [None, ""])

                mock_delete_builder.execute.assert_not_called()

    # -------------------------------------------------------------------------
    # 17. DELETE terhadap record spesifik (targeted) tetap diizinkan
    # -------------------------------------------------------------------------
    def test_17_targeted_delete_specific_record_allowed(self):
        """DELETE with targeted identifier filter ('id', 'entry_order_id', 'order_id') must succeed."""
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                mock_raw = MagicMock()
                mock_table = MagicMock()
                mock_delete_builder = MagicMock()
                mock_raw.table.return_value = mock_table
                mock_table.delete.return_value = mock_delete_builder
                mock_delete_builder.eq.return_value = mock_delete_builder
                mock_delete_builder.in_.return_value = mock_delete_builder
                mock_delete_builder.execute.return_value = {"data": [{"id": 11}]}

                guarded = sbc._GuardedClient(mock_raw)
                tbl = guarded.table("trades_spot")

                # 1. By ID
                res1 = tbl.delete().eq("id", 11).execute()
                self.assertEqual(res1, {"data": [{"id": 11}]})
                mock_delete_builder.eq.assert_called_with("id", 11)

                # 2. By entry_order_id
                mock_delete_builder.reset_mock()
                res2 = tbl.delete().eq("entry_order_id", "ORD_12345").execute()
                mock_delete_builder.eq.assert_called_with("entry_order_id", "ORD_12345")

                # 3. By in_([11, 12])
                mock_delete_builder.reset_mock()
                res3 = tbl.delete().in_("id", [11, 12]).execute()
                mock_delete_builder.in_.assert_called_with("id", [11, 12])

    # -------------------------------------------------------------------------
    # 18. INSERT dan UPDATE yang sah tidak terblokir
    # -------------------------------------------------------------------------
    def test_18_legitimate_insert_and_update_not_blocked(self):
        """Legitimate insert and update calls must not be affected by the delete guard."""
        with patch("services.supabase_client.is_test_environment", return_value=False):
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                mock_raw = MagicMock()
                mock_table = MagicMock()
                mock_raw.table.return_value = mock_table
                mock_table.insert.return_value = {"status": "ok"}
                mock_table.update.return_value = {"status": "ok"}

                guarded = sbc._GuardedClient(mock_raw)
                tbl = guarded.table("trades_spot")

                ins_res = tbl.insert({"symbol": "BTC_IDR", "price": 1000.0})
                self.assertEqual(ins_res, {"status": "ok"})
                mock_table.insert.assert_called_once_with({"symbol": "BTC_IDR", "price": 1000.0})

                upd_res = tbl.update({"exit_price": 1050.0})
                self.assertEqual(upd_res, {"status": "ok"})
                mock_table.update.assert_called_once_with({"exit_price": 1050.0})


# =============================================================================
# Architectural Control: Python AST Supabase Access Detector
# =============================================================================
# NOTE: This AST guard is a CI architectural control designed to detect
# unapproved client creation, direct imports, or credential access in pull requests
# or modifications made by automated agents. It acts as an automated static policy
# checker across the repository codebase, but is not an absolute kernel/database
# security boundary.
# =============================================================================

APPROVED_SUPABASE_ALLOWLIST = {
    "services/supabase_client.py",
    "scripts/migration_to_supabase.py",
    "dashboard.py",
}

EXCLUDED_SCAN_DIRS = {".git", ".venv", "venv", "__pycache__", ".agents", "data_cache"}


class SupabaseASTDetector(ast.NodeVisitor):
    """
    Parses a Python AST and detects any unapproved imports, constructor calls,
    or credential reads related to the Supabase client.
    """

    def __init__(self, filename="<string>"):
        self.filename = filename
        self.violations = []
        # Dynamic tracking of known factory/constructor names
        self.client_factory_aliases = {"create_client", "create_async_client"}

    def visit_Import(self, node):
        for alias in node.names:
            base_mod = alias.name.split(".")[0]
            if base_mod == "supabase":
                as_info = f" as {alias.asname}" if alias.asname else ""
                self.violations.append((
                    node.lineno,
                    f"Disallowed direct import of package '{alias.name}{as_info}'"
                ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            base_mod = node.module.split(".")[0]
            if base_mod == "supabase":
                for alias in node.names:
                    # Register any symbol imported from supabase for call tracking
                    name_imported = alias.asname or alias.name
                    self.client_factory_aliases.add(name_imported)
                names_str = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in node.names)
                self.violations.append((
                    node.lineno,
                    f"Disallowed import from '{node.module}': [{names_str}]"
                ))
        self.generic_visit(node)

    def visit_Assign(self, node):
        # Track alias assignments: e.g. custom_factory = create_client
        if isinstance(node.value, ast.Name) and node.value.id in self.client_factory_aliases:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.client_factory_aliases.add(target.id)
        elif isinstance(node.value, ast.Attribute) and node.value.attr in self.client_factory_aliases:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.client_factory_aliases.add(target.id)
        self.generic_visit(node)

    def visit_Call(self, node):
        fn = node.func
        fn_name = None
        if isinstance(fn, ast.Name):
            fn_name = fn.id
        elif isinstance(fn, ast.Attribute):
            fn_name = fn.attr

        # Detect direct or aliased constructor / factory calls
        if fn_name in self.client_factory_aliases:
            self.violations.append((
                node.lineno,
                f"Disallowed call to Supabase client constructor/factory: '{fn_name}()'"
            ))

        # Detect credential reads: os.getenv('SUPABASE_SERVICE_KEY') or getenv(...)
        if fn_name in ("getenv", "get"):
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "SUPABASE_SERVICE_KEY":
                self.violations.append((
                    node.lineno,
                    f"Disallowed credential access: reading 'SUPABASE_SERVICE_KEY' via {fn_name}()"
                ))
        self.generic_visit(node)

    def visit_Subscript(self, node):
        # Detect os.environ['SUPABASE_SERVICE_KEY']
        if isinstance(node.slice, ast.Constant) and node.slice.value == "SUPABASE_SERVICE_KEY":
            val = node.value
            val_name = None
            if isinstance(val, ast.Name):
                val_name = val.id
            elif isinstance(val, ast.Attribute):
                val_name = val.attr
            if val_name == "environ":
                self.violations.append((
                    node.lineno,
                    "Disallowed credential access: reading 'SUPABASE_SERVICE_KEY' via os.environ[]"
                ))
        self.generic_visit(node)


class TestSupabaseASTArchitecturalGuard(unittest.TestCase):
    """
    Automated architectural test guard verifying that no unauthorized code
    can introduce new direct Supabase client imports or credential access.
    """

    def test_repository_contains_no_unapproved_supabase_access_paths(self):
        """
        Scan all .py files in the repository. Ensure only approved files in the
        minimal allowlist import from supabase or read SUPABASE_SERVICE_KEY.
        Fails with clear file path and line number if violations are found.
        """
        repo_root = Path(__file__).resolve().parent.parent
        violations = []

        for py_path in repo_root.rglob("*.py"):
            rel_path = str(py_path.relative_to(repo_root))
            if any(part in EXCLUDED_SCAN_DIRS for part in py_path.parts):
                continue
            if rel_path in APPROVED_SUPABASE_ALLOWLIST:
                continue
            if rel_path == "tests/test_database_safety_guard.py":
                # Skip the test suite defining these AST rules
                continue

            try:
                content = py_path.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=rel_path)
            except Exception as exc:
                violations.append(f"{rel_path}:0: Parse error: {exc}")
                continue

            detector = SupabaseASTDetector(filename=rel_path)
            detector.visit(tree)

            for lineno, msg in detector.violations:
                violations.append(f"❌ {rel_path}:{lineno} -> {msg}")

        if violations:
            msg = (
                "Unapproved Supabase database access path detected!\n"
                "All database mutations and client operations must use 'services.supabase_client'.\n"
                + "\n".join(violations)
            )
            self.fail(msg)

    def test_ast_guard_detects_direct_import_violations(self):
        """Verify AST detector catches direct import of package and factory."""
        code = "import supabase\nfrom supabase import create_client\n"
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        self.assertEqual(len(detector.violations), 2)
        self.assertIn("direct import of package 'supabase'", detector.violations[0][1])
        self.assertIn("Disallowed import from 'supabase'", detector.violations[1][1])

    def test_ast_guard_detects_aliased_import_and_call(self):
        """Verify AST detector catches aliased imports and calls via those aliases."""
        code = (
            "from supabase import create_client as make_client\n"
            "c = make_client('http://dummy', 'key')\n"
        )
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        self.assertEqual(len(detector.violations), 2)
        self.assertIn("create_client as make_client", detector.violations[0][1])
        self.assertIn("Disallowed call to Supabase client constructor/factory: 'make_client()'", detector.violations[1][1])

    def test_ast_guard_detects_package_alias_and_call(self):
        """Verify AST detector catches import supabase as sb and sb.create_client()."""
        code = (
            "import supabase as sb\n"
            "c = sb.create_client('http://dummy', 'key')\n"
        )
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        self.assertEqual(len(detector.violations), 2)
        self.assertIn("import of package 'supabase as sb'", detector.violations[0][1])
        self.assertIn("Disallowed call to Supabase client constructor/factory: 'create_client()'", detector.violations[1][1])

    def test_ast_guard_detects_assigned_factory_alias(self):
        """Verify AST detector catches assigned aliases: factory = create_client; factory()."""
        code = (
            "from supabase import create_async_client\n"
            "custom_factory = create_async_client\n"
            "c = custom_factory('http://dummy', 'key')\n"
        )
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        # 1: import, 2: call custom_factory
        call_violations = [v for v in detector.violations if "custom_factory()" in v[1]]
        self.assertTrue(len(call_violations) >= 1)

    def test_ast_guard_detects_credential_access(self):
        """Verify AST detector catches reading SUPABASE_SERVICE_KEY through getenv and os.environ."""
        code = (
            "import os\n"
            "k1 = os.getenv('SUPABASE_SERVICE_KEY')\n"
            "k2 = os.environ['SUPABASE_SERVICE_KEY']\n"
            "k3 = os.environ.get('SUPABASE_SERVICE_KEY')\n"
        )
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        self.assertEqual(len(detector.violations), 3)
        self.assertIn("via getenv()", detector.violations[0][1])
        self.assertIn("via os.environ[]", detector.violations[1][1])
        self.assertIn("via get()", detector.violations[2][1])

    def test_ast_guard_does_not_false_positive_on_patch_dict(self):
        """Verify AST detector does not flag patch.dict(os.environ, {'SUPABASE_SERVICE_KEY': ''})."""
        code = (
            "from unittest.mock import patch\n"
            "import os\n"
            "with patch.dict(os.environ, {'SUPABASE_SERVICE_KEY': ''}):\n"
            "    pass\n"
        )
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        self.assertEqual(len(detector.violations), 0)

    def test_ast_guard_does_not_false_positive_on_binance_client(self):
        """Verify AST detector does not flag binance.client.Client constructor calls."""
        code = (
            "from binance.client import Client\n"
            "client = Client('api_key', 'api_secret')\n"
        )
        detector = SupabaseASTDetector()
        detector.visit(ast.parse(code))
        self.assertEqual(len(detector.violations), 0)


if __name__ == "__main__":
    unittest.main()
