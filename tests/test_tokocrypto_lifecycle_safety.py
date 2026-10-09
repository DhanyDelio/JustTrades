from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoNetworkError
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    TokocryptoSubmissionUnknownError,
)
from core.executors.tokocrypto_position_monitor import TokocryptoPositionMonitor


def _trade(**changes):
    trade = {
        "symbol": "LIFECYCLE_TEST_IDR",
        "entry_order_id": "lifecycle-entry-001",
        "entry_status": "FILLED",
        "exit_status": "OPEN",
        "oco_state": "EXECUTING",
        "b_order_list_id": "list-001",
        "tp_order_id": "tp-001",
        "sl_order_id": "sl-001",
        "entry_price": 100.0,
        "entry_fill_price": 100.0,
        "entry_qty": 10.0,
        "tp_price": 110.0,
        "sl_price": 90.0,
    }
    trade.update(changes)
    return trade


def _tp(status=0):
    return {"orderId": "tp-001", "type": 1, "stopPrice": "0", "status": status}


def _sl(status=0):
    return {"orderId": "sl-001", "type": 4, "stopPrice": "90", "status": status}


class TestTokocryptoStaleStateRecovery(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock(spec=TokocryptoClient)
        self.client.get_ticker.return_value = None
        self.executor = TokocryptoOrderExecutor(self.client, dry_run=False)
        self.monitor = TokocryptoPositionMonitor(self.client, self.executor)

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_db_ids_are_verified_against_exchange(self, update, _telegram):
        self.client.get_order_detail.side_effect = lambda _sym, oid: (
            _tp() if oid == "tp-001" else _sl()
        )
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(_trade(), verbose=False)
        place.assert_not_called()
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "EXECUTING"
                for call in update.call_args_list
            )
        )

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_db_ids_not_found_are_unknown_not_unprotected(self, update, _telegram):
        self.client.get_order_detail.side_effect = TokocryptoNetworkError(
            "order does not exist -2013"
        )
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(_trade(), verbose=False)
        place.assert_not_called()
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "RECONCILIATION_REQUIRED"
                for call in update.call_args_list
            )
        )

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_db_ids_timeout_remains_unknown(self, update, _telegram):
        self.client.get_order_detail.side_effect = TokocryptoNetworkError("timeout")
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(_trade(), verbose=False)
        place.assert_not_called()
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "RECONCILIATION_REQUIRED"
                for call in update.call_args_list
            )
        )

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_missing_db_ids_reconstructs_only_unique_exchange_pair(
        self, update, _telegram
    ):
        self.client.get_open_orders.return_value = [
            {
                "orderId": "tp-001",
                "bOrderListId": "list-001",
                "type": 1,
                "stopPrice": "0",
                "status": 0,
            },
            {
                "orderId": "sl-001",
                "bOrderListId": "list-001",
                "type": 4,
                "stopPrice": "90",
                "status": 0,
            },
        ]
        self.client.get_order_detail.side_effect = lambda _sym, oid: (
            _tp() if oid == "tp-001" else _sl()
        )
        trade = _trade(
            oco_state="OCO_SUBMISSION_PENDING",
            b_order_list_id="",
            tp_order_id="",
            sl_order_id="",
        )
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(trade, verbose=False)
        place.assert_not_called()
        self.assertTrue(
            any(
                call.args[1].get("tp_order_id") == "tp-001"
                for call in update.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "EXECUTING"
                for call in update.call_args_list
            )
        )

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_missing_ids_and_exchange_error_do_not_place_oco(self, update, _telegram):
        self.client.get_open_orders.side_effect = TokocryptoNetworkError("timeout")
        trade = _trade(
            oco_state="OCO_SUBMISSION_PENDING",
            b_order_list_id="",
            tp_order_id="",
            sl_order_id="",
        )
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(trade, verbose=False)
        place.assert_not_called()
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "RECONCILIATION_REQUIRED"
                for call in update.call_args_list
            )
        )

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_restart_with_pending_marker_does_not_retry_oco(self, update, _telegram):
        self.client.get_open_orders.return_value = []
        trade = _trade(
            oco_state="OCO_SUBMISSION_PENDING",
            b_order_list_id="",
            tp_order_id="",
            sl_order_id="",
        )
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(trade, verbose=False)
        place.assert_not_called()
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "RECONCILIATION_REQUIRED"
                for call in update.call_args_list
            )
        )

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_one_terminal_leg_and_live_sibling_is_not_replaced(self, update, _telegram):
        self.client.get_order_detail.side_effect = lambda _sym, oid: (
            _tp(3) if oid == "tp-001" else _sl(0)
        )
        with patch.object(self.executor, "place_oco") as place:
            self.monitor._check_one(_trade(), verbose=False)
        place.assert_not_called()
        self.assertTrue(update.called)

    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    def test_oco_post_timeout_is_not_retried(self, update, _telegram):
        self.client.get_open_orders.return_value = []
        trade = _trade(
            oco_state="OCO_NOT_ATTEMPTED",
            b_order_list_id="",
            tp_order_id="",
            sl_order_id="",
        )
        started = threading.Event()
        release = threading.Event()

        def uncertain_submit(_trade):
            started.set()
            if not release.wait(3):
                raise AssertionError("test did not release mocked OCO POST")
            raise TokocryptoSubmissionUnknownError("timeout")

        with patch.object(
            self.executor, "place_oco", side_effect=uncertain_submit
        ) as place:
            first = threading.Thread(
                target=self.monitor._check_one, args=(trade, False)
            )
            second = threading.Thread(
                target=self.monitor._check_one, args=(trade, False)
            )
            first.start()
            self.assertTrue(started.wait(2))
            second.start()
            release.set()
            first.join(3)
            second.join(3)
        place.assert_called_once()
        self.assertTrue(
            any(
                call.args[1].get("oco_state") == "RECONCILIATION_REQUIRED"
                for call in update.call_args_list
            )
        )


class TestTokocryptoSubmissionRaces(unittest.TestCase):
    def test_entry_response_without_order_id_remains_unresolved(self):
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda value: value
        client.get_open_orders.return_value = []
        client._signed_post.return_value = {"data": {"status": 0}}
        executor = TokocryptoOrderExecutor(client, supervised=False, dry_run=False)
        candidate = {
            "symbol": "ENTRY_NO_ID_TEST_IDR",
            "entry_price": 10.0,
            "tp_price": 11.0,
            "sl_price": 9.0,
        }
        with patch(
            "services.supabase_client.fetch_all_tokocrypto_strict", return_value=[]
        ), patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto"), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id"
        ) as update, patch.object(
            executor, "validate_and_size", return_value=True
        ), patch.object(
            executor,
            "build_entry_payload",
            return_value={
                "symbol": candidate["symbol"],
                "quantity": 1.0,
                "price": 10.0,
            },
        ):
            with self.assertRaises(TokocryptoSubmissionUnknownError):
                executor.execute_entry(candidate, 20_000.0)
            self.assertTrue(
                any(
                    row.args[1].get("oco_state") == "ENTRY_SUBMISSION_UNKNOWN"
                    for row in update.call_args_list
                )
            )
            self.assertTrue(executor.has_active_position(candidate["symbol"]))
        client._signed_post.assert_called_once()

    def test_entry_timeout_is_persisted_unknown_and_blocks_retry(self):
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda value: value
        client.get_open_orders.return_value = []
        client._signed_post.side_effect = TokocryptoNetworkError("timeout")
        executor = TokocryptoOrderExecutor(client, supervised=False, dry_run=False)
        candidate = {
            "symbol": "ENTRY_TIMEOUT_TEST_IDR",
            "entry_price": 10.0,
            "tp_price": 11.0,
            "sl_price": 9.0,
        }
        with patch(
            "services.supabase_client.fetch_all_tokocrypto_strict", return_value=[]
        ), patch(
            "core.clients.tokocrypto_order_executor.upsert_tokocrypto"
        ) as upsert, patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id"
        ) as update, patch.object(
            executor, "validate_and_size", return_value=True
        ), patch.object(
            executor,
            "build_entry_payload",
            return_value={
                "symbol": candidate["symbol"],
                "quantity": 1.0,
                "price": 10.0,
            },
        ):
            with self.assertRaises(TokocryptoSubmissionUnknownError):
                executor.execute_entry(candidate, 20_000.0)
            self.assertTrue(
                any(
                    row.args[0]["entry_status"] == "ENTRY_SUBMISSION_PENDING"
                    for row in upsert.call_args_list
                )
            )
            self.assertTrue(
                any(
                    row.args[1].get("oco_state") == "ENTRY_SUBMISSION_UNKNOWN"
                    for row in update.call_args_list
                )
            )
            self.assertTrue(executor.has_active_position(candidate["symbol"]))
        client._signed_post.assert_called_once()

    def test_two_entry_threads_submit_once_and_keep_claim(self):
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda value: value
        client.get_open_orders.return_value = []
        entered_post = threading.Event()
        allow_post = threading.Event()

        def signed_post(_path, _payload):
            entered_post.set()
            if not allow_post.wait(3):
                raise AssertionError("test did not release mocked POST")
            return {"data": {"orderId": "entry-race-001", "status": 0}}

        client._signed_post.side_effect = signed_post
        executor = TokocryptoOrderExecutor(client, supervised=False, dry_run=False)
        candidate = {
            "symbol": "ENTRY_RACE_TEST_IDR",
            "entry_price": 10.0,
            "tp_price": 11.0,
            "sl_price": 9.0,
        }
        errors = []
        with patch(
            "services.supabase_client.fetch_all_tokocrypto_strict", return_value=[]
        ), patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto"), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id"
        ), patch.object(
            executor, "validate_and_size", return_value=True
        ), patch.object(
            executor,
            "build_entry_payload",
            return_value={
                "symbol": candidate["symbol"],
                "quantity": 1.0,
                "price": 10.0,
            },
        ):

            def run_entry():
                try:
                    executor.execute_entry(candidate, 20_000.0)
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=run_entry)
            second = threading.Thread(target=run_entry)
            first.start()
            self.assertTrue(entered_post.wait(2))
            second.start()
            allow_post.set()
            first.join(3)
            second.join(3)
        self.assertFalse(errors)
        client._signed_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
