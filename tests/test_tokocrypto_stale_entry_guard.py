"""
tests/test_tokocrypto_stale_entry_guard.py
==========================================
Unit tests for Tokocrypto Spot stale pending entry classification policy.

Covers:
1. Distance boundaries (exactly 15%, 20%, 30%).
2. Age boundaries (exactly 3.0 days vs < 3.0 days).
3. The 5 audit fixture pairs:
   - ETH_IDR (6.9d, +25.03%): STALE_SERIOUS
   - XRP_IDR (6.9d, +21.52%): STALE_SERIOUS
   - FLOKI_IDR (6.5d, +15.79%): REVIEW_REQUIRED
   - ASTER_IDR (9.1d, +6.80%): OLD_DISTANCE_INSUFFICIENT
   - HBAR_IDR (6.9d, +4.65%): OLD_DISTANCE_INSUFFICIENT
4. Fail-closed parsing (None, NaN, inf, invalid types).
5. Configurable thresholds via environment variables.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.guards.tokocrypto_stale_entry_guard import (
    TokoStaleState,
    TokoStaleThresholds,
    classify_tokocrypto_stale_entry,
)


class TestTokocryptoStaleEntryGuard(unittest.TestCase):
    def setUp(self):
        self.default_thr = TokoStaleThresholds(
            min_age_days=3.0,
            review_pct=15.0,
            serious_pct=20.0,
            cancel_pct=30.0,
        )

    # -------------------------------------------------------------------------
    # 1. Boundary Distance Tests (at age >= 3.0 days)
    # -------------------------------------------------------------------------
    def test_distance_boundaries_at_age_3_days(self):
        age = 3.0

        # Just below 15.0% -> OLD_DISTANCE_INSUFFICIENT
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 14.999, self.default_thr),
            TokoStaleState.OLD_DISTANCE_INSUFFICIENT,
        )

        # Exactly 15.0% -> REVIEW_REQUIRED
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 15.0, self.default_thr),
            TokoStaleState.REVIEW_REQUIRED,
        )

        # Just below 20.0% -> REVIEW_REQUIRED
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 19.999, self.default_thr),
            TokoStaleState.REVIEW_REQUIRED,
        )

        # Exactly 20.0% -> STALE_SERIOUS
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 20.0, self.default_thr),
            TokoStaleState.STALE_SERIOUS,
        )

        # Just below 30.0% -> STALE_SERIOUS
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 29.999, self.default_thr),
            TokoStaleState.STALE_SERIOUS,
        )

        # Exactly 30.0% -> CANCEL_CANDIDATE
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 30.0, self.default_thr),
            TokoStaleState.CANCEL_CANDIDATE,
        )

        # Well above 30.0% -> CANCEL_CANDIDATE
        self.assertEqual(
            classify_tokocrypto_stale_entry(age, 45.0, self.default_thr),
            TokoStaleState.CANCEL_CANDIDATE,
        )

    # -------------------------------------------------------------------------
    # 2. Age Boundaries Tests (< 3.0 days vs >= 3.0 days)
    # -------------------------------------------------------------------------
    def test_age_boundaries_under_min_age(self):
        # Order younger than 3.0 days is always NORMAL regardless of distance
        young_age = 2.99

        for dist in [5.0, 15.0, 20.0, 30.0, 50.0]:
            with self.subTest(dist=dist):
                self.assertEqual(
                    classify_tokocrypto_stale_entry(young_age, dist, self.default_thr),
                    TokoStaleState.NORMAL,
                )

    def test_age_boundary_at_exactly_min_age(self):
        # At exactly 3.0 days, distance classification activates
        self.assertEqual(
            classify_tokocrypto_stale_entry(3.0, 10.0, self.default_thr),
            TokoStaleState.OLD_DISTANCE_INSUFFICIENT,
        )
        self.assertEqual(
            classify_tokocrypto_stale_entry(3.0, 16.0, self.default_thr),
            TokoStaleState.REVIEW_REQUIRED,
        )
        self.assertEqual(
            classify_tokocrypto_stale_entry(3.0, 25.0, self.default_thr),
            TokoStaleState.STALE_SERIOUS,
        )
        self.assertEqual(
            classify_tokocrypto_stale_entry(3.0, 35.0, self.default_thr),
            TokoStaleState.CANCEL_CANDIDATE,
        )

    # -------------------------------------------------------------------------
    # 3. Five Audit Fixture Pairs Tests
    # -------------------------------------------------------------------------
    def test_five_audit_fixture_classifications(self):
        fixtures = [
            # symbol, age_days, dist_pct, expected_state
            ("ETH_IDR", 6.9, 25.03, TokoStaleState.STALE_SERIOUS),
            ("XRP_IDR", 6.9, 21.52, TokoStaleState.STALE_SERIOUS),
            ("FLOKI_IDR", 6.5, 15.79, TokoStaleState.REVIEW_REQUIRED),
            ("ASTER_IDR", 9.1, 6.80, TokoStaleState.OLD_DISTANCE_INSUFFICIENT),
            ("HBAR_IDR", 6.9, 4.65, TokoStaleState.OLD_DISTANCE_INSUFFICIENT),
        ]

        for sym, age, dist, expected in fixtures:
            with self.subTest(sym=sym):
                state = classify_tokocrypto_stale_entry(age, dist, self.default_thr)
                self.assertEqual(state, expected, f"Failed for {sym}: got {state}, expected {expected}")

    # -------------------------------------------------------------------------
    # 4. Fail-closed and Invalid Inputs
    # -------------------------------------------------------------------------
    def test_fail_closed_on_invalid_or_none_inputs(self):
        invalid_cases = [
            (None, 25.0),
            (5.0, None),
            (None, None),
            (float("nan"), 25.0),
            (5.0, float("nan")),
            (float("inf"), 25.0),
            (5.0, float("inf")),
            (-1.0, 25.0),
        ]

        for age, dist in invalid_cases:
            with self.subTest(age=age, dist=dist):
                self.assertEqual(
                    classify_tokocrypto_stale_entry(age, dist, self.default_thr),
                    TokoStaleState.NORMAL,
                )

    # -------------------------------------------------------------------------
    # 5. Environment Variable Overrides
    # -------------------------------------------------------------------------
    def test_thresholds_from_env_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            thr = TokoStaleThresholds.from_env()
            self.assertEqual(thr.min_age_days, 3.0)
            self.assertEqual(thr.review_pct, 15.0)
            self.assertEqual(thr.serious_pct, 20.0)
            self.assertEqual(thr.cancel_pct, 30.0)

    def test_thresholds_from_env_custom_overrides(self):
        env = {
            "TOKO_STALE_ENTRY_MIN_DAYS": "5.0",
            "TOKO_STALE_ENTRY_REVIEW_PCT": "18.0",
            "TOKO_STALE_ENTRY_SERIOUS_PCT": "25.0",
            "TOKO_STALE_ENTRY_CANCEL_PCT": "40.0",
        }
        with patch.dict("os.environ", env):
            thr = TokoStaleThresholds.from_env()
            self.assertEqual(thr.min_age_days, 5.0)
            self.assertEqual(thr.review_pct, 18.0)
            self.assertEqual(thr.serious_pct, 25.0)
            self.assertEqual(thr.cancel_pct, 40.0)


if __name__ == "__main__":
    unittest.main()
