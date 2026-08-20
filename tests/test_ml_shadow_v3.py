"""Offline safety regressions for Spot v3 shadow scoring."""
import copy
import unittest
from unittest.mock import patch

import ml.ml_scorer as scorer
from core.repositories.spot_trade_repository import SpotTradeRepository


def candles(now_ms=1_000_000):
    rows = []
    for index in range(8):
        close = 100 + index
        rows.append([index * 100_000, str(close - 1), str(close + 1),
                     str(close - 2), str(close), "10",
                     index * 100_000 + 99_999, "0", 1, "0", "0", "0"])
    # Active candle deliberately contains an extreme future close.
    rows.append([900_000, "107", "10000", "1", "9999", "10",
                 now_ms + 50_000, "0", 1, "0", "0", "0"])
    return rows


def candidate():
    return {
        "symbol": "SOLUSDT", "direction": "long", "rr": 2.0,
        "risk_pct": 1.2, "atr_pct": 0.8,
        "entry_price": 100.0, "sl": 98.8, "tp1": 102.4,
        "entry_zone": {"touches": 3},
        "winning_zone": {"tier": "T1", "touches": 3, "label": "Zone 3×"},
    }


class MLShadowV3Tests(unittest.TestCase):
    def setUp(self):
        scorer._bundle = None
        scorer._load_failed = False

    def test_model_artifact_loads_as_shadow_only(self):
        bundle = scorer._load_model()
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["metadata"]["model_version"], "v3_e2_btc_regime")
        self.assertEqual(bundle["metadata"]["mode"], "SHADOW_ONLY")
        self.assertTrue(bundle["schema"]["observer_only"])

    def test_feature_generation_uses_only_fully_closed_candles(self):
        result = scorer.compute_ml_score(
            candidate(), now_ms=1_000_000, btc_klines=candles())
        self.assertIsNotNone(result["ml_score"])
        snapshot = result["feature_snapshot"]
        self.assertLess(snapshot["last_closed_candle_close_time"], 1_000_000)
        self.assertLess(abs(snapshot["btc_return_4h"]), 0.1)
        self.assertEqual(result["mode"], "OBSERVATION_ONLY")

    def test_missing_candles_fails_open(self):
        result = scorer.compute_ml_score(
            candidate(), now_ms=1_000_000, btc_klines=candles()[:3])
        self.assertIsNone(result["ml_score"])
        self.assertIn("fewer than 7", result["error"])

    def test_attach_failure_does_not_change_strategy_fields(self):
        cand = candidate()
        before = copy.deepcopy(cand)
        with patch("ml.ml_scorer.compute_ml_score", side_effect=RuntimeError("boom")):
            result = scorer.attach_shadow_score(cand)
        for key, value in before.items():
            self.assertEqual(cand[key], value)
        self.assertIsNone(result["ml_score"])
        self.assertEqual(cand["ml_model_version"], "v3_e2_btc_regime")
        self.assertEqual(cand["ml_shadow"]["mode"], "OBSERVATION_ONLY")

    def test_scoring_never_selects_rejects_or_resizes(self):
        cand = candidate()
        protected = copy.deepcopy(cand)
        scorer.attach_shadow_score(cand, now_ms=1_000_000, btc_klines=candles())
        for key in ("symbol", "direction", "rr", "risk_pct", "atr_pct",
                    "entry_price", "sl", "tp1", "entry_zone", "winning_zone"):
            self.assertEqual(cand[key], protected[key])

    @patch("services.supabase_client.upsert_spot")
    def test_existing_trade_row_persists_shadow_snapshot(self, upsert):
        cand = candidate()
        cand.update({
            "tp2": 103.0, "sizing": {"qty": 0.1, "notional_usd": 10.0,
                                       "max_loss_usd": 0.12},
            "ml_score": 0.67, "ml_model_version": "v3_e2_btc_regime",
            "ml_shadow": {"prediction_probability": 0.67,
                          "feature_snapshot": {"btc_return_4h": 0.01},
                          "mode": "OBSERVATION_ONLY"},
        })
        SpotTradeRepository().log_trade(
            {"orderId": 123, "clientOrderId": "entry-123", "status": "NEW"},
            cand)
        record = upsert.call_args.args[0]
        self.assertEqual(record["ml_score"], 0.67)
        self.assertEqual(record["ml_model_version"], "v3_e2_btc_regime")
        self.assertEqual(record["raw_entry_order"]["orderId"], 123)
        self.assertEqual(record["raw_entry_order"]["ml_shadow"]["mode"],
                         "OBSERVATION_ONLY")


if __name__ == "__main__":
    unittest.main()
