"""Presentation-only metadata loader for the dashboard ML Shadow tab."""
import json
from pathlib import Path


ML_V3_METADATA_PATH = (
    Path(__file__).parent / "ml" / "models" / "v3" / "metadata.json"
)


def load_ml_shadow_display_metadata(
    metadata_path: Path = ML_V3_METADATA_PATH,
) -> dict[str, str]:
    """Load the displayed model identity from the V3 artifact metadata."""
    fallback = {
        "model": "v3_e2_btc_regime",
        "version": "v3_e2_btc_regime",
        "mode": "RESEARCH / SHADOW_ONLY",
    }
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        model_version = str(metadata["model_version"])
        status = str(metadata["status"])
        mode = str(metadata["mode"])
        return {
            "model": model_version,
            "version": model_version,
            "mode": f"{status} / {mode}",
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return fallback
