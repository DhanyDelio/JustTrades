"""
test_tokocrypto_stale_entry_reconciliation.py
=============================================
Regression & Unit tests for Tokocrypto stale pending entry detection and reconciliation.

Mandatory Test Cases:
1. Order not stale (age < min_days or dist < min_dist_pct, invalid age/ticker).
2. Stale order in shadow mode (TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED=0): no exchange mutation, no DB mutation.
3. Verified zero-fill cancellation (auto_cancel=1, status=3, exec_qty=0): DB set CANCELED, slot released.
4. Cancel raced with partial fill or full fill: routes to fill/OCO, never zero-fill canceled.
5. Cancel timeout and post-cancel unverifiable status: fail-closed, slot retained, DB untouched.
6. Invalid response or missing critical fields: fail-closed, slot retained.
7. Database update fails after confirmed cancel: slot NOT released, exception raised safely.
8. Terminal status (REJECTED/EXPIRED) with executedQty > 0: treated as fill/partial fill, never zero-fill cancel.
9. Prevention of repeated cancel and double slot release (idempotent reconciliation).
10. Filled position invokes OCO protection safely with exact reconciled quantity and safety invariants.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock, patch, call

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoError
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    mark_entry_submission_active,
)
from core.executors.tokocrypto_position_monitor import TokocryptoPositionMonitor
from tokocrypto_executor import calculate_available_slots


class TestTokocryptoStaleEntryReconciliation(unittest.TestCase):

    def setUp(self):
        from core.clients import tokocrypto_order_executor as order_executor_module

        order_executor_module._ACTIVE_ENTRY_SUBMISSIONS.clear()
        order_executor_module._UNKNOWN_ENTRY_SUBMISSIONS.clear()
        order_executor_module._UNKNOWN_OCO_SUBMISSIONS.clear()
        self.mock_client = MagicMock(spec=TokocryptoClient)
        self.mock_client.normalize_symbol = lambda s: s
        self.mock_client.get_open_orders.return_value = []
        self.executor = TokocryptoOrderExecutor(
            self.mock_client,
            supervised=False,
            trading_phase="PHASE_3",
            dry_run=False,
        )
        self.monitor = TokocryptoPositionMonitor(self.mock_client, self.executor)

    # -------------------------------------------------------------------------
    # 1. Order belum stale
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_01_order_not_stale(self, mock_tg, mock_update):
        """Order is NEW, but criteria not met: no cancel, no DB update."""
        now = datetime.now(timezone.utc)
        env = {
            "TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1",
            "TOKO_STALE_ENTRY_MIN_DAYS": "3.0",
            "TOKO_STALE_ENTRY_MIN_DISTANCE_PCT": "15.0",
        }

        # Case A: age < 3 days (1 day old), dist = 20% (>= 15%)
        trade_young = {
            "symbol": "ETH_IDR",
            "entry_order_id": "1001",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": (now - timedelta(days=1)).isoformat(),
        }
        self.mock_client.get_order_detail.return_value = {
            "orderId": "1001",
            "status": 0,
            "executedQty": "0",
            "price": "40000000",
        }
        self.mock_client.get_ticker.return_value = 48000000.0  # +20%

        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order") as mock_cancel:
                self.monitor._check_one(trade_young, verbose=True)
                mock_cancel.assert_not_called()
                mock_update.assert_not_called()

        # Case B: age >= 3 days (5 days old), dist = 5% (< 15%)
        trade_close = {
            "symbol": "ETH_IDR",
            "entry_order_id": "1002",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": (now - timedelta(days=5)).isoformat(),
        }
        self.mock_client.get_order_detail.return_value = {
            "orderId": "1002",
            "status": 0,
            "executedQty": "0",
            "price": "40000000",
        }
        self.mock_client.get_ticker.return_value = 42000000.0  # +5%

        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order") as mock_cancel:
                self.monitor._check_one(trade_close, verbose=True)
                mock_cancel.assert_not_called()
                mock_update.assert_not_called()

        # Case C: Invalid age / missing open_time
        trade_invalid_time = {
            "symbol": "ETH_IDR",
            "entry_order_id": "1003",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": "invalid_date_format",
        }
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order") as mock_cancel:
                self.monitor._check_one(trade_invalid_time, verbose=True)
                mock_cancel.assert_not_called()
                mock_update.assert_not_called()

        # Case D: ticker query fails
        self.mock_client.get_ticker.side_effect = TokocryptoError("Ticker timeout")
        trade_stale_cand = {
            "symbol": "ETH_IDR",
            "entry_order_id": "1004",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": (now - timedelta(days=5)).isoformat(),
        }
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order") as mock_cancel:
                self.monitor._check_one(trade_stale_cand, verbose=True)
                mock_cancel.assert_not_called()
                mock_update.assert_not_called()

    # -------------------------------------------------------------------------
    # 2. Order stale pada shadow mode
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_02_stale_order_shadow_mode_no_mutation(self, mock_tg, mock_update):
        """In shadow mode (AUTO_CANCEL=0), no exchange mutation, no DB mutation."""
        now = datetime.now(timezone.utc)
        trade = {
            "symbol": "XRP_IDR",
            "entry_order_id": "2001",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 8000.0,
            "open_time": (now - timedelta(days=7)).isoformat(),
        }
        self.mock_client.get_order_detail.return_value = {
            "orderId": "2001",
            "status": 0,
            "executedQty": "0",
            "price": "8000",
        }
        self.mock_client.get_ticker.return_value = 10000.0  # +25%

        # Default shadow mode: env var 0 or unset
        for env_val in ("0", ""):
            mock_update.reset_mock()
            with patch.dict("os.environ", {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": env_val}):
                with patch.object(self.executor, "cancel_order") as mock_cancel:
                    self.monitor._check_one(trade, verbose=True)
                    mock_cancel.assert_not_called()
                    mock_update.assert_not_called()

    # -------------------------------------------------------------------------
    # 3. Cancel zero-fill yang terverifikasi
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_03_verified_zero_fill_cancellation(self, mock_tg, mock_update):
        """Verified cancel with 0 fill updates DB to CANCELED and releases slot."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        sym = "ETH_IDR"
        oid = "3001"
        mark_entry_submission_active(sym)
        self.assertIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

        trade = {
            "symbol": sym,
            "entry_order_id": oid,
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": (now - timedelta(days=5)).isoformat(),
        }

        # Initial detail is NEW (0), post-cancel detail is CANCELED (3) with 0 fill
        self.mock_client.get_order_detail.side_effect = [
            {"orderId": oid, "status": 0, "executedQty": "0", "price": "40000000"},
            {"orderId": oid, "status": 3, "executedQty": "0", "price": "40000000"},
        ]
        self.mock_client.get_ticker.return_value = 50000000.0  # +25%

        env = {
            "TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1",
            "TOKO_STALE_ENTRY_MIN_DAYS": "3.0",
            "TOKO_STALE_ENTRY_MIN_DISTANCE_PCT": "15.0",
        }
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order", return_value=True) as mock_cancel:
                self.monitor._check_one(trade, verbose=True)
                mock_cancel.assert_called_once_with(sym, oid)

        mock_update.assert_called_once()
        called_oid, payload = mock_update.call_args[0]
        self.assertEqual(called_oid, oid)
        self.assertEqual(payload["entry_status"], "CANCELED")
        self.assertEqual(payload["exit_status"], "CANCELED")
        self.assertEqual(payload["exit_reason"], "STALE_SETUP_TIMEOUT_CANCELED")

        # Slot must be released in _ACTIVE_ENTRY_SUBMISSIONS
        self.assertNotIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

    # -------------------------------------------------------------------------
    # 4. Cancel raced dengan partial fill atau full fill
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_04_cancel_raced_with_partial_fill_and_full_fill(self, mock_tg, mock_update):
        """Cancel races with partial fill or full fill: routed to OCO, not zero-fill cancel."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        env = {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1"}

        # Subtest 4a: Partial Fill Race (status=3 CANCELED, but executedQty=0.005)
        sym = "FLOKI_IDR"
        oid = "4001"
        mark_entry_submission_active(sym)
        trade_partial = {
            "symbol": sym,
            "entry_order_id": oid,
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 2.0,
            "open_time": (now - timedelta(days=6)).isoformat(),
            "tp_price": 2.5,
            "sl_price": 1.8,
        }
        self.mock_client.get_order_detail.side_effect = [
            {"orderId": oid, "status": 0, "executedQty": "0", "price": "2.0"},
            {"orderId": oid, "status": 3, "executedQty": "50000", "executedPrice": "2.0"},
        ]
        self.mock_client.get_ticker.return_value = 2.4  # +20%

        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order", return_value=True):
                with patch.object(self.executor, "place_oco", return_value={"orderListId": 888}) as mock_oco:
                    self.monitor._check_one(trade_partial, verbose=True)
                    # place_oco must be called to protect partial fill
                    mock_oco.assert_called_once()
                    self.assertEqual(mock_oco.call_args[0][0]["entry_qty"], 50000.0)

        # Slot must NOT be zero-fill released since position is open!
        # First call updates entry_status="FILLED" for partial fill
        first_db_call = mock_update.call_args_list[0][0][1]
        self.assertEqual(first_db_call["entry_status"], "FILLED")
        self.assertNotIn("exit_status", first_db_call)

        # Subtest 4b: Full Fill Race (status=2 FILLED, executedQty=100000)
        mock_update.reset_mock()
        oid_full = "4002"
        trade_full = {
            "symbol": sym,
            "entry_order_id": oid_full,
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 2.0,
            "open_time": (now - timedelta(days=6)).isoformat(),
            "tp_price": 2.5,
            "sl_price": 1.8,
        }
        self.mock_client.get_order_detail.side_effect = [
            {"orderId": oid_full, "status": 0, "executedQty": "0", "price": "2.0"},
            {"orderId": oid_full, "status": 2, "executedQty": "100000", "executedPrice": "2.0"},
        ]
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order", return_value=True):
                with patch.object(self.executor, "place_oco", return_value={"orderListId": 889}) as mock_oco:
                    self.monitor._check_one(trade_full, verbose=True)
                    mock_oco.assert_called_once()
                    self.assertEqual(mock_oco.call_args[0][0]["entry_qty"], 100000.0)

        first_db_call = mock_update.call_args_list[0][0][1]
        self.assertEqual(first_db_call["entry_status"], "FILLED")
        self.assertEqual(first_db_call["entry_qty"], 100000.0)

    # -------------------------------------------------------------------------
    # 5. Cancel timeout dan status pasca-cancel tak bisa diverifikasi
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_05_cancel_timeout_unverifiable_post_cancel_status(self, mock_tg, mock_update):
        """If post-cancel verification fails/times out, fail closed: no slot release, no DB change."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        sym = "HBAR_IDR"
        oid = "5001"
        mark_entry_submission_active(sym)

        trade = {
            "symbol": sym,
            "entry_order_id": oid,
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 1000.0,
            "open_time": (now - timedelta(days=6)).isoformat(),
        }
        # Initial call succeeds, post-cancel verification raises timeout/exception
        self.mock_client.get_order_detail.side_effect = [
            {"orderId": oid, "status": 0, "executedQty": "0", "price": "1000"},
            TokocryptoError("Verification connection timeout"),
        ]
        self.mock_client.get_ticker.return_value = 1300.0  # +30%

        env = {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1"}
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order", side_effect=TokocryptoError("Timeout")):
                self.monitor._check_one(trade, verbose=True)

        # Fail closed: DB update must NOT be called for terminal cancel
        mock_update.assert_not_called()
        # Slot must NOT be released
        self.assertIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

    # -------------------------------------------------------------------------
    # 6. Respons detail tidak valid atau field penting hilang
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_06_invalid_response_or_missing_critical_fields(self, mock_tg, mock_update):
        """Malformed post-cancel responses fail closed without mutating DB or releasing slot."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        sym = "ASTER_IDR"
        oid = "6001"

        malformed_responses = [
            {},                       # empty dict
            {"status": None},         # status is None
            {"status": "UNKNOWN"},    # status cannot be cast to int
            "not_a_dict",             # string response
            None,                     # None response
        ]

        env = {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1"}
        for malformed in malformed_responses:
            mock_update.reset_mock()
            mark_entry_submission_active(sym)
            trade = {
                "symbol": sym,
                "entry_order_id": oid,
                "entry_status": "NEW",
                "exit_status": "OPEN",
                "entry_price": 500.0,
                "open_time": (now - timedelta(days=8)).isoformat(),
            }
            self.mock_client.get_order_detail.side_effect = [
                {"orderId": oid, "status": 0, "executedQty": "0", "price": "500"},
                malformed,
            ]
            self.mock_client.get_ticker.return_value = 700.0  # +40%

            with patch.dict("os.environ", env):
                with patch.object(self.executor, "cancel_order", return_value=True):
                    self.monitor._check_one(trade, verbose=True)

            mock_update.assert_not_called()
            self.assertIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

    # -------------------------------------------------------------------------
    # 6b. Regression test for executedQty invalid, NaN, inf, negative, and missing
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_06b_invalid_executed_qty_never_assumed_zero(self, mock_tg, mock_update):
        """Invalid, NaN, infinity, negative, or missing executedQty is never assumed 0 and retains slot."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        sym = "ASTER_IDR"
        oid = "6002"

        invalid_qty_payloads = [
            {"status": 3},                                # key missing
            {"status": 3, "executedQty": None},           # key is None
            {"status": 3, "executedQty": "abc_corrupt"},  # unparseable string
            {"status": 3, "executedQty": "nan"},          # NaN
            {"status": 3, "executedQty": "inf"},          # Infinity
            {"status": 3, "executedQty": "-1.5"},         # Negative
        ]

        env = {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1"}
        for payload in invalid_qty_payloads:
            mock_update.reset_mock()
            mark_entry_submission_active(sym)
            trade = {
                "symbol": sym,
                "entry_order_id": oid,
                "entry_status": "NEW",
                "exit_status": "OPEN",
                "entry_price": 500.0,
                "open_time": (now - timedelta(days=8)).isoformat(),
            }
            self.mock_client.get_order_detail.side_effect = [
                {"orderId": oid, "status": 0, "executedQty": "0", "price": "500"},
                payload,
            ]
            self.mock_client.get_ticker.return_value = 700.0

            with patch.dict("os.environ", env):
                with patch.object(self.executor, "cancel_order", return_value=True):
                    self.monitor._check_one(trade, verbose=True)

            # Must NEVER update DB as zero-fill cancel and must NEVER release slot!
            mock_update.assert_not_called()
            self.assertIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

        # Initial query with missing, None, or invalid executedQty prevents triggering stale cancel,
        # does not update database, and does not release slot
        initial_invalid_payloads = [
            {"orderId": oid, "status": 0, "price": "500"},                                # missing key
            {"orderId": oid, "status": 0, "executedQty": None, "price": "500"},           # None
            {"orderId": oid, "status": 0, "executedQty": "nan", "price": "500"},          # NaN
            {"orderId": oid, "status": 0, "executedQty": "inf", "price": "500"},          # inf
            {"orderId": oid, "status": 0, "executedQty": "-1.0", "price": "500"},         # negative
            {"orderId": oid, "status": 0, "executedQty": "corrupt_data", "price": "500"}, # corrupt
        ]
        for initial_payload in initial_invalid_payloads:
            mock_update.reset_mock()
            mark_entry_submission_active(sym)
            trade = {
                "symbol": sym,
                "entry_order_id": oid,
                "entry_status": "NEW",
                "exit_status": "OPEN",
                "entry_price": 500.0,
                "open_time": (now - timedelta(days=8)).isoformat(),
            }
            self.mock_client.get_order_detail.side_effect = [initial_payload]
            self.mock_client.get_ticker.return_value = 700.0

            with patch.dict("os.environ", env):
                with patch.object(self.executor, "cancel_order", return_value=True) as mock_cancel:
                    self.monitor._check_one(trade, verbose=True)
                    mock_cancel.assert_not_called()
                    mock_update.assert_not_called()
                    self.assertIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

    # -------------------------------------------------------------------------
    # 7. Update database gagal setelah cancel terkonfirmasi
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_07_db_update_fails_after_confirmed_cancel(self, mock_tg, mock_update):
        """If DB update throws exception after cancel, release_entry_submission is NOT called."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        sym = "ETH_IDR"
        oid = "7001"
        mark_entry_submission_active(sym)

        trade = {
            "symbol": sym,
            "entry_order_id": oid,
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": (now - timedelta(days=5)).isoformat(),
        }
        self.mock_client.get_order_detail.side_effect = [
            {"orderId": oid, "status": 0, "executedQty": "0", "price": "40000000"},
            {"orderId": oid, "status": 3, "executedQty": "0", "price": "40000000"},
        ]
        self.mock_client.get_ticker.return_value = 50000000.0

        mock_update.side_effect = RuntimeError("Supabase connection dropped (PGRST 500)")

        env = {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1"}
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order", return_value=True):
                with self.assertRaises(RuntimeError):
                    self.monitor._check_one(trade, verbose=True)

        # Slot MUST NOT be released when DB update failed
        self.assertIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

    # -------------------------------------------------------------------------
    # 8. Status terminal dengan executedQty > 0
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_08_terminal_status_with_executed_qty_positive(self, mock_tg, mock_update):
        """Terminal statuses 5 (REJECTED) and 6 (EXPIRED) with executedQty > 0 route to fill/OCO."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        for code, expected_name in [(5, "REJECTED"), (6, "EXPIRED")]:
            mock_update.reset_mock()
            sym = f"SOL_IDR_{code}"
            oid = f"800{code}"
            mark_entry_submission_active(sym)

            trade = {
                "symbol": sym,
                "entry_order_id": oid,
                "entry_status": "NEW",
                "exit_status": "OPEN",
                "entry_price": 2000000.0,
                "entry_qty": 0.05,
                "tp_price": 2200000.0,
                "sl_price": 1900000.0,
            }
            # Order detail has status 5 or 6, but executedQty is 0.02
            self.mock_client.get_order_detail.return_value = {
                "orderId": oid,
                "status": code,
                "executedQty": "0.02",
                "executedPrice": "2000000",
                "time": 1700000000000,
            }

            with patch.object(self.executor, "place_oco", return_value={"orderListId": 999}) as mock_oco:
                self.monitor._check_one(trade, verbose=True)
                mock_oco.assert_called_once()
                self.assertEqual(mock_oco.call_args[0][0]["entry_qty"], 0.02)

            # DB update must have set entry_status to FILLED, NOT terminal cancellation
            first_db_payload = mock_update.call_args_list[0][0][1]
            self.assertEqual(first_db_payload["entry_status"], "FILLED")
            self.assertEqual(first_db_payload["entry_qty"], 0.02)
            self.assertNotIn("exit_status", first_db_payload)

    # -------------------------------------------------------------------------
    # 9. Pencegahan cancel berulang dan pelepasan slot ganda
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_09_prevent_repeated_cancel_and_double_slot_release(self, mock_tg, mock_update):
        """If order is already CANCELED on exchange (status=3), no repeat cancel is sent."""
        from core.clients import tokocrypto_order_executor as order_executor_module

        now = datetime.now(timezone.utc)
        sym = "ETH_IDR"
        oid = "9001"
        mark_entry_submission_active(sym)

        trade = {
            "symbol": sym,
            "entry_order_id": oid,
            "entry_status": "NEW",  # DB row still says NEW from prior failed DB update
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "open_time": (now - timedelta(days=5)).isoformat(),
        }
        # Exchange ALREADY reports status 3 (CANCELED) with 0 fill
        self.mock_client.get_order_detail.return_value = {
            "orderId": oid,
            "status": 3,
            "executedQty": "0",
            "price": "40000000",
        }

        env = {"TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED": "1"}
        with patch.dict("os.environ", env):
            with patch.object(self.executor, "cancel_order") as mock_cancel:
                self.monitor._check_one(trade, verbose=True)
                # Must NOT call cancel_order again!
                mock_cancel.assert_not_called()

        # DB must be updated cleanly
        mock_update.assert_called_once()
        self.assertEqual(mock_update.call_args[0][1]["entry_status"], "CANCELED")
        self.assertEqual(mock_update.call_args[0][1]["exit_status"], "CANCELED")
        # Slot released
        self.assertNotIn(sym, order_executor_module._ACTIVE_ENTRY_SUBMISSIONS)

    # -------------------------------------------------------------------------
    # 10. Memastikan jalur posisi terisi memanggil mekanisme proteksi OCO yang tepat
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_10_filled_position_invokes_oco_protection_safely(self, mock_tg, mock_update):
        """When an entry is confirmed filled, it invokes place_oco with exact quantity."""
        oid = "10001"
        trade = {
            "symbol": "BTC_IDR",
            "entry_order_id": oid,
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 1000000000.0,
            "entry_qty": 0.0005,
            "tp_price": 1100000000.0,
            "sl_price": 950000000.0,
        }
        # Exchange detail returns FILLED (status=2)
        self.mock_client.get_order_detail.return_value = {
            "orderId": oid,
            "status": 2,
            "executedQty": "0.0005",
            "executedPrice": "1000000000",
            "time": 1700000000000,
        }
        self.mock_client.get_open_orders.return_value = []

        with patch.object(self.executor, "place_oco", return_value={"orderListId": 5555}) as mock_place_oco:
            self.monitor._check_one(trade, verbose=True)
            mock_place_oco.assert_called_once()
            called_trade = mock_place_oco.call_args[0][0]
            self.assertEqual(called_trade["entry_qty"], 0.0005)
            self.assertEqual(called_trade["entry_status"], "FILLED")

    # -------------------------------------------------------------------------
    # Existing Regression Tests
    # -------------------------------------------------------------------------
    def test_canceled_records_do_not_consume_slots(self):
        """Records with exit_status == CANCELED do not count toward active positions."""
        trades = [
            {"symbol": "BNB_IDR", "entry_order_id": "917603053", "exit_status": "OPEN"},
            {"symbol": "BNB_IDR", "entry_order_id": "917625990", "exit_status": "CANCELED"},
            {"symbol": "SOL_IDR", "entry_order_id": "917510994", "exit_status": "OPEN"},
        ]
        open_trades = [t for t in trades if t.get("exit_status") == "OPEN"]
        self.assertEqual(len(open_trades), 2)

        max_positions = 5
        slots_available = calculate_available_slots(len(open_trades), max_positions)
        self.assertEqual(slots_available, 3)

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_canceled_records_do_not_block_future_entries(self, mock_fetch):
        """A symbol with only CANCELED trades is not considered active."""
        mock_fetch.return_value = [
            {"symbol": "BNB_IDR", "entry_order_id": "917625990", "exit_status": "CANCELED"},
            {"symbol": "ETH_IDR", "entry_order_id": "917111111", "exit_status": "SL_HIT"},
        ]
        self.assertFalse(self.executor.has_active_position("BNB_IDR"))

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_first_filled_bnb_position_remains_untouched(self, mock_tg, mock_update):
        """A legacy FILLED row with no child IDs is reconciled, never blindly retried."""
        first_trade = {
            "symbol": "BNB_IDR",
            "entry_order_id": "917603053",
            "entry_status": "FILLED",
            "entry_price": 13122252.0,
            "entry_fill_price": 13122250.0,
            "entry_qty": 0.002,
            "exit_status": "OPEN",
            "oco_state": "OCO_PLACEMENT_FAILED",
            "oco_placement_attempts": 3,
        }
        self.mock_client.get_open_orders.return_value = []
        self.monitor._check_one(first_trade, verbose=True)

        mock_update.assert_called_once()
        self.assertEqual(
            mock_update.call_args.args[1]["oco_state"], "RECONCILIATION_REQUIRED"
        )
        self.mock_client.get_order_detail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
