"""Presentation-only regressions for the ML Shadow Metrics model identity."""
import json
import tempfile
import unittest
from pathlib import Path

from dashboard_ml_metadata import load_ml_shadow_display_metadata


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


if __name__ == "__main__":
    unittest.main()
