"""
test_tokocrypto_duplicate_guard.py
==================================
Unit and regression tests for TokocryptoOrderExecutor.has_active_position().

Tests:
1. Live open orders exist on exchange -> blocks entry (returns True).
2. Exchange open orders query fails (network/auth exception) -> fails closed (returns True).
3. Database fetch fails -> fails closed (returns True).
4. Database has active FILLED position holding assets -> blocks entry (returns True).
5. Database has stale NEW trade where exchange confirms CANCELED with 0 fill -> does NOT block entry (returns False).
6. Database has stale NEW trade where exchange confirms order is actually FILLED -> blocks entry (returns True).
7. Database has stale NEW trade but exchange order verification fails -> fails closed (returns True).
8. Clean state (no exchange open orders and no DB open trades) -> permits entry (returns False).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoError
from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor


class TestTokocryptoDuplicateGuard(unittest.TestCase):

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

    def test_live_open_orders_on_exchange_blocks_entry(self):
        """Live active working order on exchange blocks new entry."""
        self.mock_client.get_open_orders.return_value = [
            {"orderId": "12345", "symbol": "BNB_IDR", "status": "0", "side": 0}
        ]
        self.assertTrue(self.executor.has_active_position("BNB_IDR"))

    def test_exchange_query_failure_fails_closed(self):
        """Network/API exception querying open orders fails closed to prevent duplicate entry."""
        self.mock_client.get_open_orders.side_effect = TokocryptoError(
            "Connection timeout"
        )
        self.assertTrue(self.executor.has_active_position("BNB_IDR"))

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_database_query_failure_fails_closed(self, mock_fetch):
        """Database error querying trades fails closed to prevent duplicate entry."""
        self.mock_client.get_open_orders.return_value = []
        mock_fetch.side_effect = RuntimeError("Supabase connection down")
        self.assertTrue(self.executor.has_active_position("BNB_IDR"))

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_database_filled_position_blocks_entry(self, mock_fetch):
        """Database with active FILLED position blocks new entry."""
        self.mock_client.get_open_orders.return_value = []
        mock_fetch.return_value = [
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917603053",
                "entry_status": "FILLED",
                "exit_status": "OPEN",
            }
        ]
        self.assertTrue(self.executor.has_active_position("BNB_IDR"))

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_stale_canceled_db_record_does_not_block_entry(self, mock_fetch):
        """Stale DB record (status=NEW) where exchange confirms order is CANCELED (0 fill) does NOT block entry."""
        self.mock_client.get_open_orders.return_value = []
        mock_fetch.return_value = [
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917625990",
                "entry_status": "NEW",
                "exit_status": "OPEN",
            }
        ]
        self.mock_client.get_order_detail.return_value = {
            "orderId": "917625990",
            "status": 3,  # CANCELED
            "executedQty": "0",
        }
        self.assertFalse(self.executor.has_active_position("BNB_IDR"))

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_stale_new_record_actually_filled_on_exchange_blocks_entry(
        self, mock_fetch
    ):
        """Stale DB record (status=NEW) where exchange shows order was actually FILLED blocks entry."""
        self.mock_client.get_open_orders.return_value = []
        mock_fetch.return_value = [
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917625990",
                "entry_status": "NEW",
                "exit_status": "OPEN",
            }
        ]
        self.mock_client.get_order_detail.return_value = {
            "orderId": "917625990",
            "status": 2,  # FILLED
            "executedQty": "0.003",
        }
        self.assertTrue(self.executor.has_active_position("BNB_IDR"))

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_stale_new_record_order_detail_failure_fails_closed(self, mock_fetch):
        """Stale DB record where order detail verification fails unexpectedly fails closed."""
        self.mock_client.get_open_orders.return_value = []
        mock_fetch.return_value = [
            {
                "symbol": "BNB_IDR",
                "entry_order_id": "917625990",
                "entry_status": "NEW",
                "exit_status": "OPEN",
            }
        ]
        self.mock_client.get_order_detail.side_effect = TokocryptoError(
            "Rate limit exceeded"
        )
        self.assertTrue(self.executor.has_active_position("BNB_IDR"))

    @patch("services.supabase_client.fetch_all_tokocrypto_strict")
    def test_clean_state_permits_entry(self, mock_fetch):
        """Clean state with no exchange open orders and no open DB trades permits entry."""
        self.mock_client.get_open_orders.return_value = []
        mock_fetch.return_value = [
            {
                "symbol": "ETH_IDR",
                "entry_order_id": "111",
                "entry_status": "FILLED",
                "exit_status": "SL_HIT",
            },
            {
                "symbol": "SOL_IDR",
                "entry_order_id": "222",
                "entry_status": "CANCELED",
                "exit_status": "CANCELED",
            },
        ]
        self.assertFalse(self.executor.has_active_position("BNB_IDR"))


if __name__ == "__main__":
    unittest.main()
