"""Presentation-only regressions for the ML Shadow Metrics model identity."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from dashboard_ml_metadata import (
    load_ml_shadow_display_metadata,
    split_ml_shadow_rows,
)


class DashboardMLMetadataTests(unittest.TestCase):
    def test_reads_v3_model_identity_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.json"
            metadata_path.write_text(json.dumps({
                "model_version": "v3_e2_btc_regime",
                "status": "RESEARCH",
                "mode": "SHADOW_ONLY",
            }), encoding="utf-8")

            display = load_ml_shadow_display_metadata(metadata_path)

        self.assertEqual(display["model"], "v3_e2_btc_regime")
        self.assertEqual(display["version"], "v3_e2_btc_regime")
        self.assertEqual(display["mode"], "RESEARCH / SHADOW_ONLY")

    def test_missing_metadata_uses_v3_safe_fallback(self):
        display = load_ml_shadow_display_metadata(Path("missing-metadata.json"))

        self.assertEqual(display["model"], "v3_e2_btc_regime")
        self.assertEqual(display["version"], "v3_e2_btc_regime")
        self.assertEqual(display["mode"], "RESEARCH / SHADOW_ONLY")

    def test_forward_metrics_include_only_active_v3_scores(self):
        trades = pd.DataFrame([
            {"id": 1, "ml_score": 0.40, "ml_model_version": "v2.0.0"},
            {"id": 2, "ml_score": 0.67,
             "ml_model_version": "v3_e2_btc_regime"},
            {"id": 3, "ml_score": None, "ml_model_version": None},
        ])

        active, legacy, unscored = split_ml_shadow_rows(
            trades, "v3_e2_btc_regime")

        self.assertEqual(active["id"].tolist(), [2])
        self.assertEqual(legacy["id"].tolist(), [1])
        self.assertEqual(unscored["id"].tolist(), [3])

    def test_missing_version_column_never_relabels_old_scores_as_v3(self):
        trades = pd.DataFrame([
            {"id": 1, "ml_score": 0.40},
            {"id": 2, "ml_score": None},
        ])

        active, legacy, unscored = split_ml_shadow_rows(
            trades, "v3_e2_btc_regime")

        self.assertTrue(active.empty)
        self.assertEqual(legacy["id"].tolist(), [1])
        self.assertEqual(unscored["id"].tolist(), [2])


if __name__ == "__main__":
    unittest.main()
