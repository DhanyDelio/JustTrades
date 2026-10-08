"""
test_tokocrypto_dashboard_badge.py — Unit tests for Tokocrypto OCO badge rendering logic.
"""

import unittest
from dashboard import compute_toko_oco_badge


class TestTokocryptoDashboardBadge(unittest.TestCase):

    def test_valid_borderlistid_shows_protected(self):
        """Valid non-empty b_order_list_id displays OCO ✓."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "25208289734",
            "oco_state": "",
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("OCO ✓", badge)

    def test_executing_with_valid_tp_sl_shows_protected(self):
        """Status EXECUTING with valid TP and SL orders/prices displays OCO ✓."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "",
            "oco_state": "EXECUTING",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 45500000.0,
            "entry_fill_price": 45503757.0,
            "tp_price": 47645461.0,
            "sl_price": 44689248.0,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("OCO ✓", badge)
        self.assertNotIn("NO OCO", badge)

    def test_executing_with_invalid_prices_shows_no_oco(self):
        """Status EXECUTING but TP <= SL (invalid bracket) is not claimed as protected."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "",
            "oco_state": "EXECUTING",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 45500000.0,
            "tp_price": 44000000.0,  # Below SL!
            "sl_price": 46000000.0,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⚠ NO OCO", badge)

    def test_executing_without_order_ids_shows_no_oco(self):
        """Status EXECUTING but missing leg order IDs is not claimed as protected."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "",
            "oco_state": "EXECUTING",
            "tp_order_id": "",
            "sl_order_id": "",
            "tp_price": 47000000.0,
            "sl_price": 44000000.0,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⚠ NO OCO", badge)

    def test_tp_sl_ids_present_but_no_valid_oco_state_shows_no_oco(self):
        """tp_order_id and sl_order_id filled alone without active OCO state does not claim protected."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "",
            "oco_state": "",  # Missing OCO state
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "tp_price": 47000000.0,
            "sl_price": 44000000.0,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⚠ NO OCO", badge)

    def test_fully_protected_shows_protected(self):
        """FULLY_PROTECTED status displays OCO ✓."""
        trade = {
            "entry_status": "FILLED",
            "oco_state": "FULLY_PROTECTED",
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("OCO ✓", badge)

    def test_anomaly_state_shows_anomaly_badge(self):
        """Anomaly states display red alert badge."""
        for anomaly in ["CRITICAL_ANOMALY", "BOTH_CANCELED_ANOMALY", "RECONCILIATION_REQUIRED"]:
            trade = {
                "entry_status": "FILLED",
                "oco_state": anomaly,
            }
            badge = compute_toko_oco_badge(trade)
            self.assertIn(f"🚨 {anomaly}", badge)

    def test_unfilled_entry_shows_no_badge(self):
        """Pending fill (NEW / PARTIALLY_FILLED) shows empty string."""
        trade = {
            "entry_status": "NEW",
            "oco_state": "",
        }
        badge = compute_toko_oco_badge(trade)
        self.assertEqual(badge, "")


if __name__ == "__main__":
    unittest.main()
