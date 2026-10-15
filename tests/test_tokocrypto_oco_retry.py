"""
tests/test_tokocrypto_oco_retry.py — Comprehensive regression test suite for Tokocrypto OCO Retry & Lifecycle.

Covers all 12 mandatory test scenarios specified in audit:
1. UPDATE mengubah baris dan mengembalikan hasil terverifikasi.
2. UPDATE tidak menemukan baris.
3. UPDATE gagal dengan exception.
4. Hasil UPDATE tidak dapat diverifikasi.
5. Kegagalan persistensi OCO_SUBMISSION_PENDING mencegah POST.
6. Exception inspect_open_oco_legs() tidak memicu place_oco().
7. Siklus berikutnya dapat memeriksa exchange kembali.
8. Query berhasil dan tidak menemukan OCO mengikuti guard state yang benar.
9. OCO_SUBMISSION_PENDING tetap fail-closed.
10. Timeout POST tetap fail-closed.
11. OCO_PLACEMENT_FAILED hanya retry jika aman dan attempts < 3.
12. OCO aktif yang ditemukan tidak memicu OCO kedua.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoError
from core.clients.tokocrypto_order_executor import (
    _UNKNOWN_OCO_SUBMISSIONS,
    TokocryptoOrderExecutor,
)
from core.executors.tokocrypto_position_monitor import (
    TokocryptoPositionMonitor,
    _last_reconciliation_warning_ts,
)
from services.supabase_client import update_tokocrypto_by_order_id


class TestDatabaseUpdateVerification(unittest.TestCase):
    """
    Tests for update_tokocrypto_by_order_id verification contract:
    - 1. UPDATE modifies rows and returns verified result.
    - 2. UPDATE finds 0 rows (raises RuntimeError).
    - 3. UPDATE fails with exception (propagates exception).
    - 4. UPDATE result cannot be verified (raises RuntimeError).
    """

    @patch("services.supabase_client.get_client")
    @patch("services.supabase_client._assert_safe_write_environment")
    def test_1_update_modifies_rows_verified(self, mock_env, mock_get_client):
        """1. UPDATE modifies rows and returns verified result."""
        mock_res = MagicMock()
        mock_res.data = [{"entry_order_id": "918969910", "oco_state": "EXECUTING"}]
        mock_res.count = 1

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            mock_res
        )

        # Must succeed without exception
        result = update_tokocrypto_by_order_id("918969910", {"oco_state": "EXECUTING"})
        self.assertIsNone(result)

    @patch("services.supabase_client.get_client")
    @patch("services.supabase_client._assert_safe_write_environment")
    def test_2_update_zero_rows_raises_runtime_error(
        self, mock_env, mock_get_client
    ):
        """2. UPDATE finds 0 rows (matched no records)."""
        mock_res = MagicMock()
        mock_res.data = []
        mock_res.count = 0

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            mock_res
        )

        with self.assertRaises(RuntimeError) as ctx:
            update_tokocrypto_by_order_id("918969910", {"oco_state": "EXECUTING"})
        self.assertIn("0 rows updated", str(ctx.exception))

    @patch("services.supabase_client.get_client")
    @patch("services.supabase_client._assert_safe_write_environment")
    def test_3_update_fails_with_exception(self, mock_env, mock_get_client):
        """3. UPDATE fails with exception (network/db error)."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.table.return_value.update.return_value.eq.return_value.execute.side_effect = (
            ConnectionError("Supabase connection timeout")
        )

        with self.assertRaises(ConnectionError) as ctx:
            update_tokocrypto_by_order_id("918969910", {"oco_state": "EXECUTING"})
        self.assertIn("Supabase connection timeout", str(ctx.exception))

    @patch("services.supabase_client.get_client")
    @patch("services.supabase_client._assert_safe_write_environment")
    def test_4_update_unverified_response_raises_runtime_error(
        self, mock_env, mock_get_client
    ):
        """4. UPDATE result cannot be verified (missing representation and count)."""
        mock_res = MagicMock(spec=[])  # Neither data nor count attribute

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            mock_res
        )

        with self.assertRaises(RuntimeError) as ctx:
            update_tokocrypto_by_order_id("918969910", {"oco_state": "EXECUTING"})
        self.assertIn("update verification failed", str(ctx.exception))

    @patch("services.supabase_client.get_client")
    @patch("services.supabase_client._assert_safe_write_environment")
    def test_4b_update_count_only_verified(self, mock_env, mock_get_client):
        """4b. UPDATE with count > 0 but data=None succeeds; count == 0 raises."""
        mock_res_ok = MagicMock()
        mock_res_ok.data = None
        mock_res_ok.count = 1

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            mock_res_ok
        )

        self.assertIsNone(
            update_tokocrypto_by_order_id("918969910", {"oco_state": "EXECUTING"})
        )

        mock_res_zero = MagicMock()
        mock_res_zero.data = None
        mock_res_zero.count = 0
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            mock_res_zero
        )

        with self.assertRaises(RuntimeError):
            update_tokocrypto_by_order_id("918969910", {"oco_state": "EXECUTING"})


class TestTokocryptoOcoRetry(unittest.TestCase):

    def setUp(self):
        from core.clients import tokocrypto_order_executor as order_executor_module

        order_executor_module._ACTIVE_ENTRY_SUBMISSIONS.clear()
        order_executor_module._UNKNOWN_ENTRY_SUBMISSIONS.clear()
        order_executor_module._UNKNOWN_OCO_SUBMISSIONS.clear()
        _last_reconciliation_warning_ts.clear()

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

    # ------------------------------------------------------------------
    # 5. Kegagalan persistensi OCO_SUBMISSION_PENDING mencegah POST
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_5_pending_persistence_failure_aborts_post(self, mock_tg, mock_update):
        """
        If persisting OCO_SUBMISSION_PENDING fails:
        - place_oco must NOT be called.
        - In-memory claim is released.
        - Fail-closed alert sent.
        """
        trade = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "",
            "tp_order_id": "",
            "sl_order_id": "",
            "oco_state": "OCO_NOT_ATTEMPTED",
            "oco_placement_attempts": 0,
        }

        mock_update.side_effect = RuntimeError("DB write error for OCO_SUBMISSION_PENDING")

        with patch.object(self.executor, "place_oco") as mock_place_oco:
            self.monitor._check_one(trade, verbose=False)
            mock_place_oco.assert_not_called()

        # In-memory claim must be released so next cycle is not stuck
        self.assertNotIn("918969910", _UNKNOWN_OCO_SUBMISSIONS)

        # Telegram notification sent
        self.assertTrue(
            any("OCO PERSISTENCE FAILED BEFORE SUBMISSION" in str(c.args[0]) for c in mock_tg.call_args_list)
        )

    # ------------------------------------------------------------------
    # 6. Exception inspect_open_oco_legs() tidak memicu place_oco()
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_6_reconciliation_transient_exception_aborts_cycle_no_reconciliation_required(
        self, mock_tg, mock_update
    ):
        """
        When inspect_open_oco_legs raises a transient exception:
        - place_oco is NOT called.
        - State is NOT changed to RECONCILIATION_REQUIRED.
        - Alert sent without locking the trade permanently.
        """
        trade = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "",
            "tp_order_id": "",
            "sl_order_id": "",
            "oco_state": "OCO_NOT_ATTEMPTED",
            "oco_placement_attempts": 0,
        }

        with patch.object(
            self.executor,
            "inspect_open_oco_legs",
            side_effect=TokocryptoError("502 Bad Gateway / Cloudflare error"),
        ), patch.object(self.executor, "place_oco") as mock_place_oco:
            self.monitor._check_one(trade, verbose=False)
            mock_place_oco.assert_not_called()

        # Must NOT update DB to RECONCILIATION_REQUIRED
        for call in mock_update.call_args_list:
            payload = call.args[1]
            self.assertNotEqual(payload.get("oco_state"), "RECONCILIATION_REQUIRED")

        # Telegram transient warning sent
        self.assertTrue(
            any("OCO RECONCILIATION TRANSIENT ERROR" in str(c.args[0]) for c in mock_tg.call_args_list)
        )

    # ------------------------------------------------------------------
    # 7. Siklus berikutnya dapat memeriksa exchange kembali
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_7_next_cycle_retries_exchange_inspection_after_transient_failure(
        self, mock_tg, mock_update
    ):
        """
        Cycle 1: transient exchange error aborts cycle cleanly.
        Cycle 2: exchange recovers, inspect_open_oco_legs succeeds -> place_oco proceeds.
        """
        trade = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "",
            "tp_order_id": "",
            "sl_order_id": "",
            "entry_price": 0.73,
            "entry_fill_price": 0.73,
            "entry_qty": 49267.0,
            "tp_price": 0.77,
            "sl_price": 0.69,
            "oco_state": "OCO_NOT_ATTEMPTED",
            "oco_placement_attempts": 0,
        }

        # Cycle 1: Exchange fails
        with patch.object(
            self.executor,
            "inspect_open_oco_legs",
            side_effect=TokocryptoError("Read timed out"),
        ), patch.object(self.executor, "place_oco") as mock_place_oco_c1:
            self.monitor._check_one(trade, verbose=False)
            mock_place_oco_c1.assert_not_called()

        # State remains OCO_NOT_ATTEMPTED for Cycle 2
        mock_update.reset_mock()
        mock_tg.reset_mock()

        # Cycle 2: Exchange recovers, inspect succeeds (empty open orders)
        with patch.object(
            self.executor,
            "inspect_open_oco_legs",
            return_value=(None, True),
        ), patch.object(
            self.executor,
            "place_oco",
            return_value={"bOrderListId": "25355107787", "orders": []},
        ) as mock_place_oco_c2:
            self.monitor._check_one(trade, verbose=False)
            mock_place_oco_c2.assert_called_once()

    # ------------------------------------------------------------------
    # 8. Query berhasil dan tidak menemukan OCO mengikuti guard state yang benar
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_8_query_success_empty_orders_follows_guard_state(
        self, mock_tg, mock_update
    ):
        """
        When open orders are confirmed empty:
        - OCO_NOT_ATTEMPTED: allowed to place OCO.
        - OCO_PLACEMENT_FAILED (attempts < 3): allowed to retry OCO.
        - RECONCILIATION_REQUIRED: NOT allowed to place OCO.
        - OCO_SUBMISSION_PENDING: NOT allowed to place OCO (fails closed to RECONCILIATION_REQUIRED).
        """
        # 1. OCO_NOT_ATTEMPTED -> allowed
        t_not_attempted = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "1",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_NOT_ATTEMPTED",
            "oco_placement_attempts": 0,
        }
        with patch.object(self.executor, "place_oco", return_value={"bOrderListId": "10"}) as m1:
            self.monitor._check_one(t_not_attempted, verbose=False)
            m1.assert_called_once()

        # 2. OCO_PLACEMENT_FAILED (attempts=1) -> allowed
        t_failed_retryable = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "2",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_PLACEMENT_FAILED",
            "oco_placement_attempts": 1,
        }
        with patch.object(self.executor, "place_oco", return_value={"bOrderListId": "11"}) as m2:
            self.monitor._check_one(t_failed_retryable, verbose=False)
            m2.assert_called_once()

        # 3. RECONCILIATION_REQUIRED -> NOT allowed
        t_recon = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "3",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "RECONCILIATION_REQUIRED",
            "oco_placement_attempts": 1,
        }
        with patch.object(self.executor, "place_oco") as m3:
            self.monitor._check_one(t_recon, verbose=False)
            m3.assert_not_called()

        # 4. OCO_SUBMISSION_PENDING -> NOT allowed
        t_pending = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "4",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_SUBMISSION_PENDING",
            "oco_placement_attempts": 1,
        }
        with patch.object(self.executor, "place_oco") as m4:
            self.monitor._check_one(t_pending, verbose=False)
            m4.assert_not_called()

    # ------------------------------------------------------------------
    # 9. OCO_SUBMISSION_PENDING tetap fail-closed
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_9_submission_pending_remains_fail_closed(self, mock_tg, mock_update):
        """
        If trade was in OCO_SUBMISSION_PENDING and open orders are empty:
        - place_oco must NOT be called.
        - State transitions to RECONCILIATION_REQUIRED.
        """
        trade_pending = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "",
            "tp_order_id": "",
            "sl_order_id": "",
            "oco_state": "OCO_SUBMISSION_PENDING",
            "oco_placement_attempts": 0,
        }

        self.mock_client.get_open_orders.return_value = []

        with patch.object(self.executor, "place_oco") as mock_place_oco:
            self.monitor._check_one(trade_pending, verbose=False)
            mock_place_oco.assert_not_called()

        # Must have transitioned to RECONCILIATION_REQUIRED
        self.assertTrue(
            any(c.args[1].get("oco_state") == "RECONCILIATION_REQUIRED" for c in mock_update.call_args_list)
        )

    # ------------------------------------------------------------------
    # 10. Timeout POST tetap fail-closed
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_10_post_timeout_fails_closed_no_blind_retry(
        self, mock_tg, mock_update
    ):
        """
        When place_oco encounters a network exception during POST:
        - Fails closed to RECONCILIATION_REQUIRED.
        - On subsequent cycles, it remains in RECONCILIATION_REQUIRED and never blind retries.
        """
        trade = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_NOT_ATTEMPTED",
            "oco_placement_attempts": 0,
        }

        self.mock_client.get_open_orders.return_value = []

        # Cycle 1: Timeout during POST
        with patch.object(self.executor, "place_oco", side_effect=TimeoutError("POST connection drop")):
            self.monitor._check_one(trade, verbose=False)

        # Verified RECONCILIATION_REQUIRED
        self.assertTrue(
            any(c.args[1].get("oco_state") == "RECONCILIATION_REQUIRED" for c in mock_update.call_args_list)
        )

        # Cycle 2: Already in RECONCILIATION_REQUIRED
        trade_next = {**trade, "oco_state": "RECONCILIATION_REQUIRED"}
        with patch.object(self.executor, "place_oco") as mock_place_oco_2:
            self.monitor._check_one(trade_next, verbose=False)
            mock_place_oco_2.assert_not_called()

    # ------------------------------------------------------------------
    # 11. OCO_PLACEMENT_FAILED hanya retry jika aman dan attempts < 3
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_11_max_oco_retries_strictly_enforced_at_3(
        self, mock_tg, mock_update
    ):
        """
        - attempts=0: allowed (cycle 0)
        - attempts=1: allowed (retry 1)
        - attempts=2: allowed (retry 2)
        - attempts=3: NOT allowed (escalates to RECONCILIATION_REQUIRED)
        """
        self.mock_client.get_open_orders.return_value = []

        # attempts = 1: allowed
        trade_1 = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969911",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_PLACEMENT_FAILED",
            "oco_placement_attempts": 1,
        }
        with patch.object(self.executor, "place_oco", return_value={"bOrderListId": "1"}) as m1:
            self.monitor._check_one(trade_1, verbose=False)
            m1.assert_called_once()

        # attempts = 2: allowed
        trade_2 = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969912",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_PLACEMENT_FAILED",
            "oco_placement_attempts": 2,
        }
        with patch.object(self.executor, "place_oco", return_value={"bOrderListId": "2"}) as m2:
            self.monitor._check_one(trade_2, verbose=False)
            m2.assert_called_once()

        # attempts = 3: exhausted, must NOT call place_oco
        trade_3 = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969913",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "oco_state": "OCO_PLACEMENT_FAILED",
            "oco_placement_attempts": 3,
        }
        with patch.object(self.executor, "place_oco") as m3:
            self.monitor._check_one(trade_3, verbose=False)
            m3.assert_not_called()

        self.assertTrue(
            any(c.args[1].get("oco_state") == "RECONCILIATION_REQUIRED" for c in mock_update.call_args_list)
        )

    # ------------------------------------------------------------------
    # 12. OCO aktif yang ditemukan tidak memicu OCO kedua
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_12_active_oco_found_prevents_duplicate_oco(
        self, mock_tg, mock_update
    ):
        """
        - If exchange has active OCO legs discovered, they are backfilled and place_oco is NOT called.
        - If DB already has tp_order_id and sl_order_id, place_oco is NEVER called.
        """
        # Case A: Discovered via inspect_open_oco_legs
        trade_pending = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "",
            "tp_order_id": "",
            "sl_order_id": "",
            "oco_state": "OCO_SUBMISSION_PENDING",
        }

        with patch.object(
            self.executor,
            "inspect_open_oco_legs",
            return_value=(("919157223", "919157224"), False),
        ), patch.object(self.executor, "place_oco") as mock_place_oco_a:
            self.monitor._check_one(trade_pending, verbose=False)
            mock_place_oco_a.assert_not_called()

        # Case B: Already has OCO legs in DB
        trade_executing = {
            "symbol": "DOGS_IDR",
            "entry_order_id": "918969910",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "25355107787",
            "tp_order_id": "919157223",
            "sl_order_id": "919157224",
            "oco_state": "EXECUTING",
        }

        with patch.object(self.executor, "place_oco") as mock_place_oco_b, \
             patch.object(self.executor, "query_oco_state", return_value={"state": "EXECUTING", "exit_price": None, "slippage_flagged": False}):
            self.monitor._check_one(trade_executing, verbose=False)
            mock_place_oco_b.assert_not_called()

    # ------------------------------------------------------------------
    # Additional Guard Tests: Terminal / Legacy Exit Statuses
    # ------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_closed_position_terminal_states_never_places_oco(
        self, mock_tg, mock_update
    ):
        """
        If trade has exit_status in CLOSED_EXIT_STATES (SL_HIT, TP_HIT, CANCELED, etc.):
        - It must immediately return and NEVER call place_oco.
        """
        for closed_status in ("SL_HIT", "TP_HIT", "CANCELED", "REJECTED", "EXPIRED", "MANUAL_CLOSED", "CLOSED"):
            trade = {
                "symbol": "DOGS_IDR",
                "entry_order_id": "918969910",
                "entry_status": "FILLED",
                "exit_status": closed_status,
                "b_order_list_id": "",
                "tp_order_id": "",
                "sl_order_id": "",
                "oco_state": "OCO_NOT_ATTEMPTED",
            }
            with patch.object(self.executor, "place_oco") as mock_place_oco:
                self.monitor._check_one(trade, verbose=False)
                mock_place_oco.assert_not_called()

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_legacy_position_with_null_or_empty_exit_status_not_skipped(
        self, mock_tg, mock_update
    ):
        """
        If a trade dictionary has exit_status=None, '', or missing:
        - It must NOT be treated as closed.
        - It must proceed to Phase B reconciliation / placement.
        """
        for exit_val in (None, "", "UNKNOWN_FIELD_OMITTED"):
            trade = {
                "symbol": "DOGS_IDR",
                "entry_order_id": f"OID_{exit_val}",
                "entry_status": "FILLED",
                "b_order_list_id": "",
                "tp_order_id": "",
                "sl_order_id": "",
                "entry_price": 0.73,
                "entry_fill_price": 0.73,
                "entry_qty": 49267.0,
                "tp_price": 0.77,
                "sl_price": 0.69,
                "oco_state": "OCO_NOT_ATTEMPTED",
                "oco_placement_attempts": 0,
            }
            if exit_val != "UNKNOWN_FIELD_OMITTED":
                trade["exit_status"] = exit_val

            self.mock_client.get_open_orders.return_value = []

            with patch.object(
                self.executor,
                "place_oco",
                return_value={"bOrderListId": "123", "orders": [{"orderId": "1"}, {"orderId": "2"}]},
            ) as mock_place_oco:
                self.monitor._check_one(trade, verbose=False)
                mock_place_oco.assert_called_once()


if __name__ == "__main__":
    unittest.main()
