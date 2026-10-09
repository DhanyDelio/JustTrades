"""
test_tokocrypto_dashboard_badge.py — Unit tests for Tokocrypto OCO badge rendering logic.
"""

import unittest
import os
from unittest.mock import patch

with patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": ""}):
    from dashboard import compute_toko_oco_badge


class TestTokocryptoDashboardBadge(unittest.TestCase):

    def test_oco_id_present_but_protection_failed_shows_failed(self):
        """Regression test: b_order_list_id present but oco_state=OCO_PLACEMENT_FAILED displays ⚠ OCO FAILED, NOT OCO ✓."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "25208291196",
            "oco_state": "OCO_PLACEMENT_FAILED",
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⚠ OCO FAILED", badge)
        self.assertNotIn("OCO ✓", badge)

    def test_oco_id_present_but_protection_state_unknown_shows_unverified(self):
        """Regression test: b_order_list_id present with empty/unverified oco_state displays ⏳ OCO UNVERIFIED, NOT OCO ✓."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "25208289734",
            "oco_state": "",
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⏳ OCO UNVERIFIED", badge)
        self.assertNotIn("OCO ✓", badge)

    def test_legacy_record_missing_fields_shows_no_oco(self):
        """Legacy or bare filled record with missing OCO fields displays ⚠ NO OCO."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": None,
            "oco_state": None,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⚠ NO OCO", badge)
        self.assertNotIn("OCO ✓", badge)

    def test_executing_with_valid_tp_sl_shows_last_check_not_live_protection(self):
        """A DB snapshot cannot certify that exchange protection remains active."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "25208289734",
            "oco_state": "EXECUTING",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 45500000.0,
            "entry_fill_price": 45503757.0,
            "tp_price": 47645461.0,
            "sl_price": 44689248.0,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("LAST CHECK EXECUTING", badge)
        self.assertNotIn("OCO ✓", badge)

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

    def test_tp_sl_ids_present_but_no_valid_oco_state_shows_unverified(self):
        """tp_order_id and sl_order_id filled alone without active OCO state shows UNVERIFIED, not protected."""
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
        self.assertIn("⏳ OCO UNVERIFIED", badge)
        self.assertNotIn("OCO ✓", badge)

    def test_fully_protected_shows_historical_check_not_live_protection(self):
        """Historical FULLY_PROTECTED in DB is not a live exchange verification."""
        trade = {
            "entry_status": "FILLED",
            "oco_state": "FULLY_PROTECTED",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 45500000.0,
            "tp_price": 47645461.0,
            "sl_price": 44689248.0,
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("LAST CHECK PROTECTED", badge)
        self.assertNotIn("OCO ✓", badge)

    def test_fully_protected_without_legs_shows_no_oco(self):
        """Historical FULLY_PROTECTED but missing leg order IDs does NOT show OCO ✓."""
        trade = {
            "entry_status": "FILLED",
            "b_order_list_id": "25208289734",
            "oco_state": "FULLY_PROTECTED",
            "tp_order_id": "",
            "sl_order_id": "",
        }
        badge = compute_toko_oco_badge(trade)
        self.assertIn("⚠ NO OCO", badge)
        self.assertNotIn("OCO ✓", badge)

    def test_unknown_or_unverified_oco_state_shows_unverified(self):
        """oco_state='UNKNOWN' or 'UNVERIFIED' displays ⏳ OCO UNVERIFIED."""
        for st in ["UNKNOWN", "UNVERIFIED"]:
            trade = {
                "entry_status": "FILLED",
                "oco_state": st,
            }
            badge = compute_toko_oco_badge(trade)
            self.assertIn("⏳ OCO UNVERIFIED", badge)
            self.assertNotIn("OCO ✓", badge)

    def test_anomaly_state_shows_anomaly_badge(self):
        """Anomaly states display red alert badge."""
        for anomaly in [
            "CRITICAL_ANOMALY",
            "BOTH_CANCELED_ANOMALY",
            "RECONCILIATION_REQUIRED",
        ]:
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
