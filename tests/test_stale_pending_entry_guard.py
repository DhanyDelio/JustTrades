"""Regression tests for the non-chasing stale Spot entry decision policy."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.guards.stale_pending_entry_guard import evaluate
from core.executors.spot_position_monitor import SpotPositionMonitor


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def trade(**overrides):
    value = {
        "symbol": "BTCUSDT",
        "entry_status": "NEW",
        "entry_price": 100.0,
        "entry_qty": 0.0,
        "open_time": (NOW - timedelta(days=4)).isoformat(),
        "raw_entry_order": {"pending_entry_guard": {}},
    }
    value.update(overrides)
    return value


class StalePendingEntryGuardTests(unittest.TestCase):
    def test_below_review_threshold_does_nothing(self):
        self.assertEqual(evaluate(trade(), 119.99, NOW)["action"], "NONE")

    def test_review_starts_at_twenty_percent(self):
        result = evaluate(trade(), 120.0, NOW)
        self.assertEqual(result["action"], "REVIEW_REQUIRED")
        self.assertEqual(result["distance_pct"], 20.0)

    def test_thirty_percent_old_order_requires_revalidation(self):
        self.assertEqual(evaluate(trade(), 130.0, NOW)["action"], "REVALIDATE")

    def test_invalid_fresh_zone_is_cancel_eligible_at_forty_percent(self):
        value = trade(raw_entry_order={"pending_entry_guard": {
            "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
            "last_revalidation": {"zone_valid": False},
        }})
        self.assertEqual(evaluate(value, 140.0, NOW)["action"], "CANCEL_ELIGIBLE")

    def test_valid_or_unknown_zone_never_cancels(self):
        for verdict in (True, "unknown", None):
            with self.subTest(verdict=verdict):
                value = trade(raw_entry_order={"pending_entry_guard": {
                    "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
                    "last_revalidation": {"zone_valid": verdict},
                }})
                self.assertEqual(evaluate(value, 160.0, NOW)["action"], "REVIEW_REQUIRED")

    def test_partial_or_filled_order_can_never_be_cancel_eligible(self):
        self.assertEqual(
            evaluate(trade(entry_status="PARTIALLY_FILLED", entry_qty=0.1), 200, NOW)["action"],
            "NONE",
        )
        self.assertEqual(
            evaluate(trade(entry_status="FILLED", entry_qty=1), 200, NOW)["action"],
            "NONE",
        )

    def test_stale_revalidation_is_not_trusted(self):
        value = trade(raw_entry_order={"pending_entry_guard": {
            "last_revalidation_at": (NOW - timedelta(hours=25)).isoformat(),
            "last_revalidation": {"zone_valid": False},
        }})
        self.assertEqual(evaluate(value, 140.0, NOW)["action"], "REVALIDATE")

    def test_default_shadow_mode_never_calls_cancel_executor(self):
        value = trade(entry_order_id=7, raw_entry_order={"pending_entry_guard": {
            "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
            "last_revalidation": {"zone_valid": False},
        }})
        executor = MagicMock()
        monitor = SpotPositionMonitor(MagicMock(), MagicMock(), executor)
        with patch.dict("os.environ", {"STALE_ENTRY_AUTO_CANCEL_ENABLED": "false"}):
            self.assertFalse(monitor._handle_stale_pending_entry(value, 140.0))
        executor.cancel_order.assert_not_called()
        self.assertEqual(
            value["raw_entry_order"]["pending_entry_guard"]["state"], "WOULD_CANCEL"
        )

    def test_opt_in_cancel_rechecks_exchange_and_terminalizes_without_pnl(self):
        value = trade(entry_order_id=7, raw_entry_order={"pending_entry_guard": {
            "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
            "last_revalidation": {"zone_valid": False},
        }})
        client = MagicMock()
        client.get_order.side_effect = [
            {"status": "NEW", "executedQty": "0"},
            {"status": "CANCELED", "executedQty": "0"},
        ]
        executor = MagicMock()
        monitor = SpotPositionMonitor(client, MagicMock(), executor)
        with patch.object(monitor, "_persist_pending_guard", return_value=True), \
             patch.dict("os.environ", {"STALE_ENTRY_AUTO_CANCEL_ENABLED": "true"}):
            self.assertTrue(monitor._handle_stale_pending_entry(value, 140.0))
        executor.cancel_order.assert_called_once_with("BTCUSDT", 7)
        self.assertEqual(value["entry_status"], "CANCELED")
        self.assertEqual(value["exit_status"], "CANCELED")
        self.assertEqual(value["exit_reason"], "STALE_SETUP_CANCELLED")
        self.assertIsNone(value.get("realized_pnl_usd"))


if __name__ == "__main__":
    unittest.main()
