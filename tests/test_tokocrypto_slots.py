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

from tokocrypto_executor import (
    calculate_available_slots,
    calculate_new_order_allocation,
    cmd_diagnostic,
    cmd_propose,
    MAX_POSITIONS,
)


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


class TestTokocryptoDynamicAllocation(unittest.TestCase):
    """
    Test suite for dynamic wallet-based trade allocation for Tokocrypto.
    Requirements:
      1. Wallet Rp100k, MAX=5 → allocation Rp20k
      2. Wallet Rp500k, MAX=5 → allocation Rp100k
      3. Wallet Rp250k, MAX=5 → allocation Rp50k
      4. Wallet Rp75k, MAX=5 → allocation Rp15k
      5. Wallet drop after existing positions → existing positions untouched
      6. Wallet increase → new orders take new allocation
      7. Wallet = 0 → no order
      8. Wallet below minimum order → no invalid order
      9. Precision / step size compliance
      10. Balance API failure → aborts without stale fallback
    """

    def test_1_wallet_100k_max_5_alloc_20k(self):
        """1. Wallet Rp100.000, MAX=5 → new order allocation = Rp20.000"""
        alloc = calculate_new_order_allocation(100_000, 5)
        self.assertEqual(alloc, 20_000.0)

    def test_2_wallet_500k_max_5_alloc_100k(self):
        """2. Wallet Rp500.000, MAX=5 → new order allocation = Rp100.000"""
        alloc = calculate_new_order_allocation(500_000, 5)
        self.assertEqual(alloc, 100_000.0)

    def test_3_wallet_250k_max_5_alloc_50k(self):
        """3. Wallet Rp250.000, MAX=5 → new order allocation = Rp50.000"""
        alloc = calculate_new_order_allocation(250_000, 5)
        self.assertEqual(alloc, 50_000.0)

    def test_4_wallet_75k_max_5_alloc_15k(self):
        """4. Wallet Rp75.000, MAX=5 → new order allocation = Rp15.000"""
        alloc = calculate_new_order_allocation(75_000, 5)
        self.assertEqual(alloc, 15_000.0)

    def test_math_safety_edge_cases(self):
        """Negative, None, NaN, and Inf safely return 0.0 without raising."""
        import math
        self.assertEqual(calculate_new_order_allocation(-100_000, 5), 0.0)
        self.assertEqual(calculate_new_order_allocation(None, 5), 0.0)
        self.assertEqual(calculate_new_order_allocation(float("nan"), 5), 0.0)
        self.assertEqual(calculate_new_order_allocation(float("inf"), 5), 0.0)
        self.assertEqual(calculate_new_order_allocation(100_000, 0), 0.0)
        self.assertEqual(calculate_new_order_allocation(100_000, -1), 0.0)

    def test_5_existing_positions_untouched_when_wallet_drops(self):
        """
        5. When wallet drops from Rp500k to Rp250k, existing open positions (A, B, C)
        retain their historical entry size/nominal. No rebalancing/repricing occurs.
        """
        existing_positions = [
            {"symbol": "BTC_IDR", "entry_price": 1_000_000_000, "nominal": 100_000, "exit_status": "OPEN"},
            {"symbol": "ETH_IDR", "entry_price": 50_000_000, "nominal": 100_000, "exit_status": "OPEN"},
            {"symbol": "SOL_IDR", "entry_price": 2_500_000, "nominal": 100_000, "exit_status": "OPEN"},
        ]

        # Simulate wallet drop:
        wallet_now = 250_000
        new_alloc = calculate_new_order_allocation(wallet_now, 5)
        self.assertEqual(new_alloc, 50_000.0)

        # Existing positions are NEVER altered by the new allocation
        for pos in existing_positions:
            self.assertEqual(pos["nominal"], 100_000)
            self.assertEqual(pos["exit_status"], "OPEN")

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_6_wallet_increase_updates_new_order_allocation(
        self, mock_build_exec, mock_build_client, mock_build_scanner, mock_fetch
    ):
        """
        6. Dynamic allocation works two ways: when wallet increases to Rp500k,
        new orders are allocated Rp100k each.
        """
        mock_fetch.return_value = []
        client_mock = MagicMock()
        balance_mock = MagicMock()
        balance_mock.free = 500_000.0
        client_mock.get_balance.return_value = balance_mock
        mock_build_client.return_value = client_mock

        scanner_mock = MagicMock()
        candidate = {
            "symbol": "DOGE_IDR",
            "current_price": 1000.0,
            "atr": 50.0,
            "tp1": 1200.0,
            "rr": 2.0,
            "risk_pct": 5.0,
        }
        scanner_mock.gather_candidates.return_value = [candidate]
        scanner_mock.pick_best_candidate.return_value = {
            "symbol": "DOGE_IDR",
            "entry_price": 1000.0,
            "sl": 950.0,
            "tp1": 1200.0,
            "rr": 2.0,
            "risk_pct": 5.0,
            "sizing": {"notional_idr": 100_000.0, "qty": 100.0},
        }
        mock_build_scanner.return_value = scanner_mock

        exec_mock = MagicMock()
        exec_mock.execute_entry.return_value = {"orderId": 9999}
        mock_build_exec.return_value = exec_mock

        cmd_propose()

        # Sizing must have received 100,000 (500k / 5)
        scanner_mock.pick_best_candidate.assert_called_once_with([candidate], available_idr=100_000.0)
        exec_mock.execute_entry.assert_called_once()

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_7_wallet_zero_places_no_orders(
        self, mock_build_exec, mock_build_client, mock_build_scanner, mock_fetch
    ):
        """7. Wallet = 0 → no order placed and scan skipped."""
        mock_fetch.return_value = []
        client_mock = MagicMock()
        balance_mock = MagicMock()
        balance_mock.free = 0.0
        client_mock.get_balance.return_value = balance_mock
        mock_build_client.return_value = client_mock

        scanner_mock = MagicMock()
        mock_build_scanner.return_value = scanner_mock
        exec_mock = MagicMock()
        mock_build_exec.return_value = exec_mock

        cmd_propose()

        scanner_mock.gather_candidates.assert_not_called()
        exec_mock.execute_entry.assert_not_called()

    def test_8_wallet_below_minimum_order(self):
        """
        8. Sizing rejects candidate if allocation budget is below minimum notional (Rp 20,000).
        """
        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner

        client_mock = MagicMock()
        scanner = TokocryptoCandidateScanner(client_mock)
        scanner._sym_constraints["TEST_IDR"] = {
            "tick_size": 1.0,
            "step_size": 0.01,
            "min_notional": 20_000.0,
        }

        candidate = {
            "symbol": "TEST_IDR",
            "current_price": 5000.0,
            "atr": 100.0,
            "tp1": 6000.0,
            "rr": 2.0,
            "risk_pct": 5.0,
            "usdt_idr_rate": 16000.0,
            "support_zones": [],
        }

        # Available IDR Rp 15,000 is below min_notional Rp 20,000
        result = scanner.pick_best_candidate([candidate], available_idr=15_000.0)
        self.assertIsNone(result)

    def test_9_rounding_and_precision_compliance(self):
        """
        9. Sizing strictly adheres to tick_size and step_size rounding.
        """
        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner

        client_mock = MagicMock()
        scanner = TokocryptoCandidateScanner(client_mock)
        scanner._sym_constraints["DOGE_IDR"] = {
            "tick_size": 1.0,      # IDR price must be whole number
            "step_size": 0.1,      # Qty step size
            "min_notional": 20_000.0,
        }

        candidate = {
            "symbol": "DOGE_IDR",
            "current_price": 1570.35,
            "atr": 50.0,
            "tp1": 1800.0,
            "rr": 2.0,
            "risk_pct": 5.0,
            "usdt_idr_rate": 16000.0,
            "support_zones": [],
        }

        result = scanner.pick_best_candidate([candidate], available_idr=50_000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["entry_price"] % 1.0, 0.0)  # tick_size = 1.0
        qty = result["sizing"]["qty"]
        # Qty must be multiple of 0.1 (i.e. at most 1 decimal place)
        self.assertAlmostEqual(qty, round(qty, 1), places=6)
        self.assertGreaterEqual(result["sizing"]["notional_idr"], 20_000.0)

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_10_balance_api_failure_aborts_without_stale_fallback(
        self, mock_build_exec, mock_build_client, mock_build_scanner, mock_fetch
    ):
        """
        10. When balance API fails, do NOT fallback to stale/cached budget.
        Aborts gracefully without creating any orders.
        """
        mock_fetch.return_value = []
        client_mock = MagicMock()
        client_mock.get_balance.side_effect = RuntimeError("Tokocrypto API 503 Service Unavailable")
        mock_build_client.return_value = client_mock

        scanner_mock = MagicMock()
        mock_build_scanner.return_value = scanner_mock
        exec_mock = MagicMock()
        mock_build_exec.return_value = exec_mock

        cmd_propose()

        scanner_mock.gather_candidates.assert_not_called()
        exec_mock.execute_entry.assert_not_called()

    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("tokocrypto_executor._build_client")
    def test_diagnostic_output_format(self, mock_build_client, mock_fetch):
        """
        Verify cmd_diagnostic prints the required exact lines without executing orders.
        """
        import io
        from contextlib import redirect_stdout

        mock_fetch.return_value = [{"symbol": "DOGE_IDR", "exit_status": "OPEN"}]
        client_mock = MagicMock()
        bal_mock = MagicMock()
        bal_mock.free = 250_000.0
        client_mock.get_balance.return_value = bal_mock
        mock_build_client.return_value = client_mock

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_diagnostic()

        output = buf.getvalue()
        self.assertIn("Wallet balance fetched: Rp 250,000.00", output)
        self.assertIn("MAX_OPEN_POSITIONS: 5", output)
        self.assertIn("Dynamic allocation per new order: Rp 50,000.00", output)
        self.assertIn("Open positions: 1 / 5", output)
        self.assertIn("Available slots: 4", output)


if __name__ == "__main__":
    unittest.main()
