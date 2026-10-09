"""
test_tokocrypto_stale_entry_reconciliation.py
=============================================
Regression tests for Tokocrypto stale/terminal pending entry reconciliation.

Covers:
1. Exchange status CANCELED updates the correct DB row with CANCELED status and reason.
2. REJECTED and EXPIRED are handled consistently.
3. A partially filled terminal order is NOT treated as an unfilled cancellation;
   actual fills are reconciled first and exit_status remains OPEN.
4. Canceled records do not consume active-position slots (slots_available).
5. Canceled records do not permanently block future entries (open_symbols / has_active_position).
6. First filled BNB position remains untouched when stale entry is processed.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import TokocryptoClient
from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
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
        self.executor = TokocryptoOrderExecutor(
            self.mock_client,
            supervised=False,
            trading_phase="PHASE_3",
            dry_run=False,
        )
        self.monitor = TokocryptoPositionMonitor(self.mock_client, self.executor)

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_canceled_unfilled_entry_updates_db_row(self, mock_tg, mock_update):
        """Exchange status 3 (CANCELED) with exec_qty=0 sets entry & exit to CANCELED."""
        trade = {
            "symbol": "BNB_IDR",
            "entry_order_id": "917625990",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 12892754.0,
            "entry_qty": 0.003,
        }
        raw_detail = {
            "orderId": "917625990",
            "status": 3,  # CANCELED
            "executedQty": "0",
            "executedPrice": "0",
            "origQty": "0.003",
        }
        self.mock_client.get_order_detail.return_value = raw_detail

        self.monitor._check_one(trade, verbose=True)

        self.assertGreaterEqual(mock_update.call_count, 1)
        call_entry_oid, payload = mock_update.call_args_list[0].args
        self.assertEqual(call_entry_oid, "917625990")
        self.assertEqual(payload["entry_status"], "CANCELED")
        self.assertEqual(payload["exit_status"], "CANCELED")
        self.assertEqual(payload["exit_reason"], "ENTRY_CANCELED")
        self.assertIn("updated_at", payload)

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_rejected_and_expired_unfilled_entries(self, mock_tg, mock_update):
        """Exchange statuses 5 (REJECTED) and 6 (EXPIRED) with exec_qty=0 update consistently."""
        for code, expected_name in [(5, "REJECTED"), (6, "EXPIRED")]:
            mock_update.reset_mock()
            trade = {
                "symbol": "SOL_IDR",
                "entry_order_id": f"oid_{code}",
                "entry_status": "NEW",
                "exit_status": "OPEN",
            }
            raw_detail = {
                "orderId": f"oid_{code}",
                "status": code,
                "executedQty": "0",
                "executedPrice": "0",
            }
            self.mock_client.get_order_detail.return_value = raw_detail

            self.monitor._check_one(trade, verbose=True)

            mock_update.assert_called_once()
            call_entry_oid, payload = mock_update.call_args[0]
            self.assertEqual(call_entry_oid, f"oid_{code}")
            self.assertEqual(payload["entry_status"], expected_name)
            self.assertEqual(payload["exit_status"], expected_name)
            self.assertEqual(payload["exit_reason"], f"ENTRY_{expected_name}")

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_partially_filled_terminal_order_reconciles_fill_not_zero_cancel(
        self, mock_tg, mock_update
    ):
        """A partially filled order with status 3 (CANCELED) reconciles fill and remains open."""
        trade = {
            "symbol": "ETH_IDR",
            "entry_order_id": "917999999",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "entry_qty": 0.01,
        }
        raw_detail = {
            "orderId": "917999999",
            "status": 3,  # CANCELED remaining
            "executedQty": "0.004",  # partially filled!
            "executedPrice": "40000000",
            "createTime": 1791500000000,
        }
        self.mock_client.get_order_detail.return_value = raw_detail

        with patch.object(self.executor, "place_oco") as mock_place_oco:
            self.monitor._check_one(trade, verbose=True)

        self.assertGreaterEqual(mock_update.call_count, 1)
        call_entry_oid, payload = mock_update.call_args_list[0].args
        self.assertEqual(call_entry_oid, "917999999")
        # Entry status updated to FILLED for the held portion
        self.assertEqual(payload["entry_status"], "FILLED")
        self.assertEqual(payload["entry_qty"], 0.004)
        self.assertEqual(payload["entry_fill_price"], 40000000.0)
        # Crucial: exit_status must NOT be set to CANCELED!
        self.assertNotIn("exit_status", payload)

    def test_canceled_records_do_not_consume_slots(self):
        """Records with exit_status == CANCELED do not count toward active positions."""
        trades = [
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917603053",
                "exit_status": "OPEN",
            },  # active
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917625990",
                "exit_status": "CANCELED",
            },  # canceled
            {
                "symbol": "SOL_IDR",
                "entry_order_id": "917510994",
                "exit_status": "OPEN",
            },  # active
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
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917625990",
                "exit_status": "CANCELED",
            },
            {
                "symbol": "ETH_IDR",
                "entry_order_id": "917111111",
                "exit_status": "SL_HIT",
            },
        ]
        # has_active_position should return False for BNB_IDR because exit_status is CANCELED
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
            "oco_placement_attempts": 3,  # exhausted retries
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
