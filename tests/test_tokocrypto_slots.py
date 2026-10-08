"""
tests/test_tokocrypto_slots.py
==============================
Unit tests for Tokocrypto dynamic slot calculation and position limit enforcement.
Ensures:
  - max slots = 5 (source of truth: TOKO_MAX_POSITIONS)
  - open = 0 → available = 5
  - open = 1 → available = 4
  - open = 4 → available = 1
  - open = 5 → available = 0 and scanner skip
  - open > 5 does not produce negative slots
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from tokocrypto_executor import calculate_available_slots, MAX_POSITIONS, cmd_propose


class TestTokocryptoSlotCalculation(unittest.TestCase):

    def test_default_max_positions_is_5(self):
        """Default MAX_POSITIONS must be 5 unless overridden by env."""
        self.assertEqual(MAX_POSITIONS, 5)

    def test_slot_scenarios(self):
        """
        Verify required scenario matrix:
          - open = 0 → available = 5
          - open = 1 → available = 4
          - open = 4 → available = 1
          - open = 5 → available = 0
          - open > 5 → available = 0 (never negative)
        """
        max_slots = 5
        self.assertEqual(calculate_available_slots(0, max_slots), 5)
        self.assertEqual(calculate_available_slots(1, max_slots), 4)
        self.assertEqual(calculate_available_slots(4, max_slots), 1)
        self.assertEqual(calculate_available_slots(5, max_slots), 0)
        self.assertEqual(calculate_available_slots(6, max_slots), 0)
        self.assertEqual(calculate_available_slots(10, max_slots), 0)
        self.assertEqual(calculate_available_slots(-1, max_slots), 5)

    def test_env_var_override(self):
        """calculate_available_slots respects custom max_positions."""
        self.assertEqual(calculate_available_slots(1, max_positions=3), 2)
        self.assertEqual(calculate_available_slots(3, max_positions=3), 0)
        self.assertEqual(calculate_available_slots(4, max_positions=3), 0)

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_all_slots_occupied_skips_scanner(
        self, mock_build_exec, mock_build_client, mock_build_scanner, mock_fetch
    ):
        """When 5 open trades exist, available slots = 0 and scanner.gather_candidates is skipped."""
        mock_fetch.return_value = [
            {"entry_order_id": f"100{i}", "symbol": f"COIN{i}_IDR", "exit_status": "OPEN"}
            for i in range(5)
        ]
        scanner_mock = MagicMock()
        mock_build_scanner.return_value = scanner_mock

        cmd_propose()

        scanner_mock.gather_candidates.assert_not_called()

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_partially_occupied_passes_available_slots_to_scanner(
        self, mock_build_exec, mock_build_client, mock_build_scanner, mock_fetch
    ):
        """When 1 open trade exists, available slots = 4 and scanner is called with max_positions=4."""
        mock_fetch.return_value = [
            {"entry_order_id": "1001", "symbol": "DOGE_IDR", "exit_status": "OPEN"}
        ]
        scanner_mock = MagicMock()
        scanner_mock.gather_candidates.return_value = []
        mock_build_scanner.return_value = scanner_mock

        cmd_propose()

        scanner_mock.gather_candidates.assert_called_once_with(max_positions=4)

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_over_capacity_does_not_fail_or_produce_negative_slots(
        self, mock_build_exec, mock_build_client, mock_build_scanner, mock_fetch
    ):
        """When open trades > 5 (e.g. 6), slots_available = 0, no exception, scan skipped."""
        mock_fetch.return_value = [
            {"entry_order_id": f"100{i}", "symbol": f"COIN{i}_IDR", "exit_status": "OPEN"}
            for i in range(6)
        ]
        scanner_mock = MagicMock()
        mock_build_scanner.return_value = scanner_mock

        cmd_propose()

        scanner_mock.gather_candidates.assert_not_called()


class TestDashboardConfig(unittest.TestCase):

    def test_dashboard_max_toko_slots_is_5(self):
        """Dashboard MAX_TOKO_SLOTS must default to 5."""
        from dashboard import MAX_TOKO_SLOTS
        self.assertEqual(MAX_TOKO_SLOTS, 5)


if __name__ == "__main__":
    unittest.main()
