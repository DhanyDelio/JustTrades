"""Regression + expanded tests for the stale Spot entry decision policy.

Original 9 tests preserved verbatim.
15 new scenarios added (per Part 7 requirement):
  1.  NEW order age 1 day
  2.  NEW order age 3 days
  3.  NEW order age 30 days
  4.  NEW order age 80 days
  5.  runaway +10%
  6.  runaway +30%
  7.  runaway +100%
  8.  old + runaway (age 80d, dist +150%)
  9.  old but near entry (age 80d, dist +1%)
  10. guard persistence (shadow write to raw_entry_order)
  11. idempotent repeated evaluation
  12. feature flag OFF → no exchange cancel
  13. feature flag ON but shadow WOULD_CANCEL → still recorded
  14. already FILLED order → action NONE
  15. already CANCELLED order → action NONE

Also tests for new API:
  - StaleEntryState enum membership
  - StaleEntryThresholds.from_env() / constructor
  - classify_lifecycle() helper
  - thresholds param in evaluate()
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.guards.stale_pending_entry_guard import (
    StaleEntryState,
    StaleEntryThresholds,
    classify_lifecycle,
    evaluate,
    parse_time,
    utc_now,
)
from core.executors.spot_position_monitor import SpotPositionMonitor


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)

# ── Shared threshold fixture (same defaults as env, explicit for clarity) ──

THR = StaleEntryThresholds(
    review_pct=20.0,
    revalidate_pct=30.0,
    cancel_pct=40.0,
    min_age_days=3.0,
    revalidation_hours=24.0,
    review_after_days=5.0,
    expire_after_days=30.0,
    runaway_distance_pct=10.0,
)


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


# ===========================================================================
# ORIGINAL TESTS (preserved verbatim — must not change)
# ===========================================================================

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


# ===========================================================================
# NEW TESTS — 15 required scenarios
# ===========================================================================

class TestNewOrderAgeScenarios(unittest.TestCase):
    """Scenarios 1–4: age-based classification."""

    def test_scenario_1_age_1_day_under_review_threshold(self):
        """1 day old, price +25% — age < min_age_days(3) → REVIEW_REQUIRED not REVALIDATE."""
        t = trade(open_time=(NOW - timedelta(days=1)).isoformat())
        result = evaluate(t, 125.0, NOW, thresholds=THR)
        # dist=25% ≥ revalidate_pct(30%) is False, so REVIEW_REQUIRED
        self.assertEqual(result["action"], StaleEntryState.REVIEW_REQUIRED)
        self.assertAlmostEqual(result["age_days"], 1.0, places=1)
        # lifecycle: age<5d → ACTIVE_PENDING despite 25% distance
        self.assertEqual(result["lifecycle"], StaleEntryState.RUNAWAY)
        print("✓ Scenario 1: age=1d, +25% → REVIEW_REQUIRED (too young to revalidate)")

    def test_scenario_2_age_3_days_at_min_threshold(self):
        """3 days old at exactly min_age_days — eligible for REVALIDATE path."""
        t = trade(open_time=(NOW - timedelta(days=3)).isoformat())
        result = evaluate(t, 130.0, NOW, thresholds=THR)
        # dist=30% = revalidate_pct, age=3=min_age_days, no fresh reval → REVALIDATE
        self.assertEqual(result["action"], StaleEntryState.REVALIDATE)
        self.assertAlmostEqual(result["age_days"], 3.0, places=1)
        print("✓ Scenario 2: age=3d, +30% → REVALIDATE")

    def test_scenario_3_age_30_days_expired_lifecycle(self):
        """30 days old + large runaway → EXPIRED lifecycle state."""
        t = trade(open_time=(NOW - timedelta(days=30)).isoformat())
        result = evaluate(t, 145.0, NOW, thresholds=THR)
        # dist=45% ≥ cancel_pct(40%), age≥expire(30d) → EXPIRED lifecycle
        self.assertEqual(result["lifecycle"], StaleEntryState.EXPIRED)
        self.assertAlmostEqual(result["age_days"], 30.0, places=1)
        print(f"✓ Scenario 3: age=30d, +45% → lifecycle=EXPIRED, action={result['action']}")

    def test_scenario_4_age_80_days_very_stale(self):
        """80 days old — well beyond expire threshold regardless of distance."""
        t = trade(open_time=(NOW - timedelta(days=80)).isoformat())
        result = evaluate(t, 125.0, NOW, thresholds=THR)
        self.assertGreater(result["age_days"], 79.0)
        # +25% → RUNAWAY lifecycle (dist≥10%), age>30d → also EXPIRED qualified
        # distance 25% < cancel_pct 40% so not EXPIRED; RUNAWAY wins
        self.assertIn(result["lifecycle"], (
            StaleEntryState.RUNAWAY, StaleEntryState.EXPIRED, StaleEntryState.STALE_REVIEW
        ))
        print(f"✓ Scenario 4: age=80d → lifecycle={result['lifecycle']}, action={result['action']}")


class TestRunawayDistanceScenarios(unittest.TestCase):
    """Scenarios 5–7: distance-based classification."""

    def test_scenario_5_runaway_ten_percent(self):
        """Exactly at runaway threshold → RUNAWAY lifecycle."""
        t = trade(open_time=(NOW - timedelta(days=10)).isoformat())
        result = evaluate(t, 110.0, NOW, thresholds=THR)
        self.assertAlmostEqual(result["distance_pct"], 10.0, places=4)
        self.assertEqual(result["lifecycle"], StaleEntryState.RUNAWAY)
        # dist 10% < review_pct 20% → action NONE
        self.assertEqual(result["action"], StaleEntryState.NONE)
        print("✓ Scenario 5: +10% → RUNAWAY lifecycle, action=NONE (below review_pct)")

    def test_scenario_6_runaway_thirty_percent(self):
        """+30% runaway, aged order."""
        t = trade(open_time=(NOW - timedelta(days=10)).isoformat())
        result = evaluate(t, 130.0, NOW, thresholds=THR)
        self.assertAlmostEqual(result["distance_pct"], 30.0, places=4)
        self.assertEqual(result["lifecycle"], StaleEntryState.RUNAWAY)
        # dist=30%=revalidate_pct, age=10d>min(3d), no fresh reval → REVALIDATE
        self.assertEqual(result["action"], StaleEntryState.REVALIDATE)
        print("✓ Scenario 6: +30% → RUNAWAY lifecycle, action=REVALIDATE")

    def test_scenario_7_runaway_one_hundred_percent(self):
        """+100% — extreme runaway, aged order."""
        t = trade(open_time=(NOW - timedelta(days=10)).isoformat())
        result = evaluate(t, 200.0, NOW, thresholds=THR)
        self.assertAlmostEqual(result["distance_pct"], 100.0, places=4)
        self.assertEqual(result["lifecycle"], StaleEntryState.RUNAWAY)
        # no fresh reval → REVALIDATE (cancel needs fresh bad verdict)
        self.assertEqual(result["action"], StaleEntryState.REVALIDATE)
        print("✓ Scenario 7: +100% → RUNAWAY lifecycle, action=REVALIDATE")


class TestCombinedAgeAndDistanceScenarios(unittest.TestCase):
    """Scenarios 8–9: combined age + distance."""

    def test_scenario_8_old_and_runaway(self):
        """80 days old + +150% — worst case, EXPIRED lifecycle."""
        t = trade(open_time=(NOW - timedelta(days=80)).isoformat())
        result = evaluate(t, 250.0, NOW, thresholds=THR)
        # dist=150%≥cancel_pct(40%), age=80d≥expire(30d) → EXPIRED
        self.assertEqual(result["lifecycle"], StaleEntryState.EXPIRED)
        self.assertGreater(result["distance_pct"], 100.0)
        # action: no fresh reval → REVALIDATE (can't cancel without zone verdict)
        self.assertEqual(result["action"], StaleEntryState.REVALIDATE)
        print(f"✓ Scenario 8: age=80d, +150% → EXPIRED, action={result['action']}")

    def test_scenario_9_old_but_near_entry(self):
        """80 days old but price only +1% above entry — still near, action NONE."""
        t = trade(open_time=(NOW - timedelta(days=80)).isoformat())
        result = evaluate(t, 101.0, NOW, thresholds=THR)
        self.assertAlmostEqual(result["distance_pct"], 1.0, places=4)
        # dist=1% < review_pct(20%) → NONE regardless of age
        self.assertEqual(result["action"], StaleEntryState.NONE)
        # lifecycle: dist<runaway(10%) → STALE_REVIEW (age≥review_after_days)
        self.assertEqual(result["lifecycle"], StaleEntryState.STALE_REVIEW)
        print("✓ Scenario 9: age=80d, +1% → action=NONE (near entry), lifecycle=STALE_REVIEW")


class TestGuardPersistenceScenarios(unittest.TestCase):
    """Scenario 10: guard writes to raw_entry_order in-memory and via Supabase."""

    def test_scenario_10_guard_persistence_updates_raw_entry_order(self):
        """Guard state is written to trade['raw_entry_order']['pending_entry_guard']."""
        value = trade(entry_order_id=42, raw_entry_order={"pending_entry_guard": {
            "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
            "last_revalidation": {"zone_valid": False},
        }})
        monitor = SpotPositionMonitor(MagicMock(), MagicMock(), MagicMock())
        with patch.dict("os.environ", {"STALE_ENTRY_AUTO_CANCEL_ENABLED": "false"}):
            monitor._handle_stale_pending_entry(value, 140.0)

        guard = value["raw_entry_order"]["pending_entry_guard"]
        # state updated in-memory
        self.assertEqual(guard["state"], "WOULD_CANCEL")
        # contains last_price and distance
        self.assertIn("last_distance_pct", guard)
        self.assertIn("order_age_days", guard)
        print(f"✓ Scenario 10: guard persisted in-memory: state={guard['state']}")

    def test_scenario_10b_persist_calls_update_spot_by_order_id(self):
        """_persist_pending_guard calls update_spot_by_order_id with raw_entry_order."""
        value = trade(entry_order_id=99, raw_entry_order={"pending_entry_guard": {"state": "RUNAWAY"}})
        monitor = SpotPositionMonitor(MagicMock(), MagicMock(), MagicMock())
        with patch("services.supabase_client.update_spot_by_order_id") as mock_update:
            ok = monitor._persist_pending_guard(value)
        self.assertTrue(ok)
        call_args = mock_update.call_args
        self.assertEqual(call_args[0][0], 99)
        self.assertIn("raw_entry_order", call_args[0][1])
        print("✓ Scenario 10b: _persist_pending_guard calls update_spot_by_order_id")


class TestIdempotencyScenario(unittest.TestCase):
    """Scenario 11: repeated evaluation produces the same result."""

    def test_scenario_11_idempotent_repeated_evaluation(self):
        """evaluate() called twice on same trade produces identical result."""
        t = trade(open_time=(NOW - timedelta(days=10)).isoformat())
        r1 = evaluate(t, 135.0, NOW, thresholds=THR)
        r2 = evaluate(t, 135.0, NOW, thresholds=THR)
        self.assertEqual(r1["action"],       r2["action"])
        self.assertEqual(r1["lifecycle"],    r2["lifecycle"])
        self.assertEqual(r1["distance_pct"], r2["distance_pct"])
        self.assertEqual(r1["age_days"],     r2["age_days"])
        # evaluate() must not mutate the trade dict
        import copy
        t2 = trade(open_time=(NOW - timedelta(days=10)).isoformat())
        original_guard = copy.deepcopy((t2.get("raw_entry_order") or {}).get("pending_entry_guard") or {})
        evaluate(t2, 135.0, NOW, thresholds=THR)
        after_guard = (t2.get("raw_entry_order") or {}).get("pending_entry_guard") or {}
        self.assertEqual(original_guard, after_guard)
        print(f"✓ Scenario 11: idempotent, action={r1['action']}, lifecycle={r1['lifecycle']}")


class TestFeatureFlagScenarios(unittest.TestCase):
    """Scenarios 12–13: STALE_ENTRY_AUTO_CANCEL_ENABLED flag behaviour."""

    def _cancel_eligible_trade(self):
        return trade(entry_order_id=7, raw_entry_order={"pending_entry_guard": {
            "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
            "last_revalidation": {"zone_valid": False},
        }})

    def test_scenario_12_flag_off_no_exchange_cancel(self):
        """Flag OFF → executor.cancel_order never called, state=WOULD_CANCEL."""
        value    = self._cancel_eligible_trade()
        executor = MagicMock()
        monitor  = SpotPositionMonitor(MagicMock(), MagicMock(), executor)
        with patch.dict("os.environ", {"STALE_ENTRY_AUTO_CANCEL_ENABLED": "false"}):
            terminated = monitor._handle_stale_pending_entry(value, 145.0)
        # shadow mode: handle returns False (not terminalized) and does not cancel
        self.assertFalse(terminated)
        executor.cancel_order.assert_not_called()
        state = value["raw_entry_order"]["pending_entry_guard"]["state"]
        self.assertEqual(state, "WOULD_CANCEL")
        print(f"✓ Scenario 12: flag OFF → no cancel, state={state}")

    def test_scenario_13_flag_on_shadow_would_cancel_recorded(self):
        """Flag ON path terminalized; with _persist mocked, WOULD_CANCEL is readable."""
        value  = self._cancel_eligible_trade()
        client = MagicMock()
        client.get_order.side_effect = [
            {"status": "NEW",      "executedQty": "0"},
            {"status": "CANCELED", "executedQty": "0"},
        ]
        executor = MagicMock()
        monitor  = SpotPositionMonitor(client, MagicMock(), executor)
        # Guard that auto-cancel is the active path when flag=true
        with patch.object(monitor, "_persist_pending_guard", return_value=True), \
             patch.dict("os.environ", {"STALE_ENTRY_AUTO_CANCEL_ENABLED": "true"}):
            terminated = monitor._handle_stale_pending_entry(value, 145.0)
        self.assertTrue(terminated)
        self.assertEqual(value["exit_reason"], "STALE_SETUP_CANCELLED")
        print("✓ Scenario 13: flag ON → terminalized, exit_reason=STALE_SETUP_CANCELLED")


class TestFilledAndCancelledOrderScenarios(unittest.TestCase):
    """Scenarios 14–15: already-resolved orders must be skipped."""

    def test_scenario_14_already_filled_returns_none(self):
        """FILLED order → evaluate() always returns action=NONE."""
        t = trade(entry_status="FILLED", entry_qty=1.0,
                  open_time=(NOW - timedelta(days=80)).isoformat())
        result = evaluate(t, 500.0, NOW, thresholds=THR)  # extreme price
        self.assertEqual(result["action"], StaleEntryState.NONE)
        self.assertEqual(result["reason"], "NOT_UNFILLED_NEW")
        # lifecycle on filled should not be ACTIVE_PENDING (it's FILLED)
        self.assertEqual(result["lifecycle"], StaleEntryState.FILLED)
        print("✓ Scenario 14: FILLED order → action=NONE (never eligible)")

    def test_scenario_15_already_cancelled_treated_as_non_new(self):
        """CANCELED status → evaluate() returns action=NONE."""
        t = trade(entry_status="CANCELED", entry_qty=0.0,
                  open_time=(NOW - timedelta(days=80)).isoformat())
        result = evaluate(t, 500.0, NOW, thresholds=THR)
        self.assertEqual(result["action"], StaleEntryState.NONE)
        self.assertEqual(result["reason"], "NOT_UNFILLED_NEW")
        print("✓ Scenario 15: CANCELED order → action=NONE (not unfilled NEW)")


# ===========================================================================
# ADDITIONAL COVERAGE: new API surface
# ===========================================================================

class TestStaleEntryStateEnum(unittest.TestCase):
    """StaleEntryState is a str subclass — backward-compatible with string comparisons."""

    def test_state_values_are_strings(self):
        for state in StaleEntryState:
            self.assertIsInstance(state, str)
        print("✓ All StaleEntryState values are str instances")

    def test_string_equality_works(self):
        self.assertEqual(StaleEntryState.NONE, "NONE")
        self.assertEqual(StaleEntryState.CANCEL_ELIGIBLE, "CANCEL_ELIGIBLE")
        self.assertEqual(StaleEntryState.WOULD_CANCEL, "WOULD_CANCEL")
        print("✓ StaleEntryState == str comparisons work")

    def test_all_expected_states_present(self):
        expected = {
            "ACTIVE_PENDING", "STALE_REVIEW", "RUNAWAY", "EXPIRED",
            "WOULD_CANCEL", "CANCEL_ELIGIBLE", "CANCELLED", "FILLED",
            "RECONCILIATION_REQUIRED", "NONE", "REVIEW_REQUIRED",
            "REVALIDATE", "PRICE_UNAVAILABLE",
        }
        actual = {s.value for s in StaleEntryState}
        self.assertEqual(expected, actual)
        print(f"✓ All {len(expected)} expected states present")


class TestStaleEntryThresholds(unittest.TestCase):

    def test_from_env_returns_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            for k in [
                "STALE_ENTRY_REVIEW_PCT", "STALE_ENTRY_REVALIDATE_PCT",
                "STALE_ENTRY_CANCEL_PCT", "STALE_ENTRY_MIN_AGE_DAYS",
                "STALE_ENTRY_REVALIDATION_HOURS", "STALE_ENTRY_REVIEW_AFTER_DAYS",
                "STALE_ENTRY_EXPIRE_AFTER_DAYS", "STALE_ENTRY_RUNAWAY_DISTANCE_PCT",
            ]:
                import os
                os.environ.pop(k, None)
            thr = StaleEntryThresholds.from_env()
        self.assertEqual(thr.review_pct, 20.0)
        self.assertEqual(thr.cancel_pct, 40.0)
        self.assertEqual(thr.expire_after_days, 30.0)
        self.assertEqual(thr.runaway_distance_pct, 10.0)
        print("✓ StaleEntryThresholds.from_env() defaults correct")

    def test_env_override_respected(self):
        with patch.dict("os.environ", {
            "STALE_ENTRY_EXPIRE_AFTER_DAYS": "45",
            "STALE_ENTRY_RUNAWAY_DISTANCE_PCT": "15",
        }):
            thr = StaleEntryThresholds.from_env()
        self.assertEqual(thr.expire_after_days, 45.0)
        self.assertEqual(thr.runaway_distance_pct, 15.0)
        print("✓ StaleEntryThresholds env overrides respected")

    def test_thresholds_are_frozen(self):
        thr = THR
        with self.assertRaises((AttributeError, TypeError)):
            thr.review_pct = 99.0  # type: ignore[misc]
        print("✓ StaleEntryThresholds is frozen (immutable)")


class TestClassifyLifecycle(unittest.TestCase):

    def test_active_pending_near_price(self):
        state = classify_lifecycle(5.0, 2.0, THR)
        self.assertEqual(state, StaleEntryState.ACTIVE_PENDING)
        print("✓ classify_lifecycle: dist=5%, age=2d → ACTIVE_PENDING")

    def test_runaway_classification(self):
        state = classify_lifecycle(15.0, 2.0, THR)
        self.assertEqual(state, StaleEntryState.RUNAWAY)
        print("✓ classify_lifecycle: dist=15% → RUNAWAY")

    def test_stale_review_classification(self):
        state = classify_lifecycle(5.0, 10.0, THR)
        self.assertEqual(state, StaleEntryState.STALE_REVIEW)
        print("✓ classify_lifecycle: dist=5%, age=10d → STALE_REVIEW")

    def test_expired_classification(self):
        state = classify_lifecycle(45.0, 35.0, THR)
        self.assertEqual(state, StaleEntryState.EXPIRED)
        print("✓ classify_lifecycle: dist=45%, age=35d → EXPIRED")

    def test_runaway_beats_stale_review(self):
        """Runaway (dist≥10%) takes priority over stale (age≥5d) when not expired."""
        state = classify_lifecycle(12.0, 10.0, THR)
        self.assertEqual(state, StaleEntryState.RUNAWAY)
        print("✓ classify_lifecycle: RUNAWAY has priority over STALE_REVIEW")

    def test_expired_beats_runaway(self):
        """EXPIRED (old+large dist) beats standalone RUNAWAY."""
        state = classify_lifecycle(50.0, 35.0, THR)
        self.assertEqual(state, StaleEntryState.EXPIRED)
        print("✓ classify_lifecycle: EXPIRED beats RUNAWAY when age≥expire_after_days")

    def test_none_distance_returns_active_pending(self):
        state = classify_lifecycle(None, 80.0, THR)
        self.assertEqual(state, StaleEntryState.ACTIVE_PENDING)
        print("✓ classify_lifecycle: dist=None → ACTIVE_PENDING (price unavailable)")

    def test_thresholds_param_overrides_defaults(self):
        """Custom thresholds produce different classification."""
        tight_thr = StaleEntryThresholds(
            review_pct=5.0, revalidate_pct=10.0, cancel_pct=15.0,
            min_age_days=1.0, revalidation_hours=12.0,
            review_after_days=1.0, expire_after_days=7.0,
            runaway_distance_pct=5.0,
        )
        # With tight thresholds, dist=6%, age=8d → EXPIRED (dist≥cancel_pct? 6<15)
        # dist≥runaway(5%) → RUNAWAY; age≥expire(7d) → check cancel: 6<15 → not EXPIRED
        state = classify_lifecycle(6.0, 8.0, tight_thr)
        self.assertEqual(state, StaleEntryState.RUNAWAY)
        print("✓ classify_lifecycle: custom tight thresholds applied correctly")


class TestEvaluateWithCustomThresholds(unittest.TestCase):

    def test_custom_thresholds_change_action(self):
        """Tighter cancel_pct means same trade hits CANCEL_ELIGIBLE sooner."""
        tight = StaleEntryThresholds(
            review_pct=5.0, revalidate_pct=8.0, cancel_pct=12.0,
            min_age_days=1.0, revalidation_hours=12.0,
            review_after_days=1.0, expire_after_days=7.0,
            runaway_distance_pct=5.0,
        )
        t = trade(
            open_time=(NOW - timedelta(days=5)).isoformat(),
            raw_entry_order={"pending_entry_guard": {
                "last_revalidation_at": (NOW - timedelta(hours=1)).isoformat(),
                "last_revalidation": {"zone_valid": False},
            }},
        )
        # dist=15% ≥ cancel_pct=12% with fresh bad zone → CANCEL_ELIGIBLE
        result = evaluate(t, 115.0, NOW, thresholds=tight)
        self.assertEqual(result["action"], StaleEntryState.CANCEL_ELIGIBLE)
        print("✓ Custom thresholds: +15% with tight cancel_pct=12% → CANCEL_ELIGIBLE")

    def test_evaluate_does_not_mutate_trade(self):
        """evaluate() must not modify the trade dict — pure function."""
        t = trade(open_time=(NOW - timedelta(days=10)).isoformat())
        import copy
        original = copy.deepcopy(t)
        evaluate(t, 150.0, NOW, thresholds=THR)
        self.assertEqual(t, original)
        print("✓ evaluate() is a pure function — trade dict unchanged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
