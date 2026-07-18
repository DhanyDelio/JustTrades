"""
ml/ml_scorer.py — Passive ML v1 scoring for paper trading
==========================================================
Loads the trained v1 model and scores trade candidates.

IMPORTANT: This module is OBSERVATION-ONLY.
  - ml_score is logged alongside trades for future analysis
  - It does NOT influence propose/skip decisions
  - Failures are silently handled (return None) — never blocks trading

Usage (from paper_trade_executor.py):
    from ml.ml_scorer import compute_ml_score
    result = compute_ml_score(candidate_dict)
    # result = {"ml_score": 0.73, "ml_model_version": "v1"}
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

_MODEL_PATH = Path(__file__).parent / "models" / "v1.pkl"
_MODEL_VERSION = "v1"

# ── Cached model singleton ────────────────────────────────────────────────
_model = None
_model_load_failed = False


def _load_model():
    """Load model once, cache in module globals. Never raises."""
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        return None
    try:
        import joblib
        _model = joblib.load(_MODEL_PATH)
        return _model
    except Exception as e:
        _model_load_failed = True
        print(f"  [ML] ⚠  Could not load model {_MODEL_PATH}: {e}")
        return None


def _build_features(cand: dict) -> pd.DataFrame:
    """
    Extract features from a candidate dict and build the same DataFrame
    the v1 model was trained on.

    Feature columns (from train_v1.py build_feature_matrix):
      numeric: zone_touches, planned_rr, risk_pct, atr_pct_at_entry
      dummy:   zone_T2  (from pd.get_dummies(zone_type, prefix="zone", drop_first=True))
               T1 is the dropped (baseline) category.

    The saved model's scaler expects exactly these 5 columns in order:
      ['zone_touches', 'planned_rr', 'risk_pct', 'atr_pct_at_entry', 'zone_T2']
    """
    # Extract raw feature values from candidate dict
    ez = cand.get("entry_zone") or {}
    wz = cand.get("winning_zone") or {}

    zone_touches = (
        ez.get("touches")
        or wz.get("touches")
        or 1  # safe fallback
    )
    planned_rr       = cand.get("rr", 0)
    risk_pct         = cand.get("risk_pct", 0)
    atr_pct_at_entry = cand.get("atr_pct", 0)

    # zone_type: "T1" or "T2" — maps to dummy column zone_T2
    zone_type = wz.get("tier", "T1") if wz else "T1"
    zone_t2   = 1.0 if zone_type == "T2" else 0.0

    # Build DataFrame with exact column order the model expects
    X = pd.DataFrame([{
        "zone_touches":      float(zone_touches),
        "planned_rr":        float(planned_rr),
        "risk_pct":          float(risk_pct),
        "atr_pct_at_entry":  float(atr_pct_at_entry),
        "zone_T2":           zone_t2,
    }])

    return X


def compute_ml_score(cand: dict) -> dict:
    """
    Score a single trade candidate using ML v1.

    Returns dict with:
        ml_score:          float (0.0–1.0 probability of win) or None on failure
        ml_model_version:  "v1"

    Never raises — scoring failure returns ml_score=None.
    """
    result = {"ml_score": None, "ml_model_version": _MODEL_VERSION}

    model = _load_model()
    if model is None:
        return result

    try:
        X = _build_features(cand)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            proba = model.predict_proba(X)

        # proba shape: (1, 2) — column 1 is P(win=1)
        score = float(proba[0, 1])
        result["ml_score"] = round(score, 4)

    except Exception as e:
        print(f"  [ML] ⚠  Scoring failed: {e}")

    return result
