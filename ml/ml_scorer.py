"""Leakage-safe Spot ML v3 shadow scoring.

Observation only: this module never accepts/rejects/ranks/sizes a candidate.
Every public entry point is fail-open so trading continues when ML is absent.
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

_MODEL_DIR = Path(__file__).parent / "models" / "v3"
_MODEL_PATH = _MODEL_DIR / "v3_e2_btc_regime.joblib"
_METADATA_PATH = _MODEL_DIR / "metadata.json"
_SCHEMA_PATH = _MODEL_DIR / "feature_schema.json"
_MODEL_VERSION = "v3_e2_btc_regime"
_KLINE_URL = "https://testnet.binance.vision/api/v3/klines"

_bundle = None
_load_failed = False


def _load_model():
    """Load and validate the research artifact once. Never raises."""
    global _bundle, _load_failed
    if _bundle is not None:
        return _bundle
    if _load_failed:
        return None
    try:
        import joblib
        bundle = joblib.load(_MODEL_PATH)
        metadata = json.loads(_METADATA_PATH.read_text())
        schema = json.loads(_SCHEMA_PATH.read_text())
        if metadata.get("model_version") != _MODEL_VERSION:
            raise ValueError("model metadata version mismatch")
        if metadata.get("mode") != "SHADOW_ONLY":
            raise ValueError("artifact is not marked SHADOW_ONLY")
        bundle["metadata"] = metadata
        bundle["schema"] = schema
        _bundle = bundle
        return _bundle
    except Exception as exc:
        _load_failed = True
        print(f"  [ML SHADOW] ⚠ model unavailable; passthrough: {exc}")
        return None


def _fetch_btc_klines() -> list:
    response = requests.get(
        _KLINE_URL,
        params={"symbol": "BTCUSDT", "interval": "4h", "limit": 12},
        timeout=5,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected BTC kline response: {payload}")
    return payload


def _btc_features(klines: list, now_ms: int) -> dict:
    """Use only candles whose close timestamp is strictly before scoring time."""
    closed = [row for row in klines if len(row) >= 7 and int(row[6]) < now_ms]
    if len(closed) < 7:
        raise ValueError("fewer than 7 fully closed BTC 4H candles")
    close = np.asarray([float(row[4]) for row in closed], dtype=float)
    ret = lambda periods: close[-1] / close[-1 - periods] - 1.0
    log_returns = np.diff(np.log(close[-7:]))
    return {
        "btc_return_4h": float(ret(1)),
        "btc_return_12h": float(ret(3)),
        "btc_return_24h": float(ret(6)),
        "btc_distance_from_ma": float(close[-1] / close[-6:].mean() - 1.0),
        "btc_volatility": float(np.std(log_returns, ddof=1)),
        "last_closed_candle_close_time": int(closed[-1][6]),
    }


def _build_feature_snapshot(cand: dict, metadata: dict, *, now_ms: int,
                            btc_klines: list | None = None) -> dict:
    btc = _btc_features(btc_klines if btc_klines is not None else _fetch_btc_klines(),
                        now_ms)
    entry_zone = cand.get("entry_zone") or {}
    winning_zone = cand.get("winning_zone") or {}
    r24 = btc["btc_return_24h"]
    distance = btc["btc_distance_from_ma"]
    q1, q2 = metadata["regime_thresholds"]["btc_volatility_terciles"]
    volatility = btc["btc_volatility"]
    snapshot = {
        "zone_touches": float(entry_zone.get("touches")
                              or winning_zone.get("touches") or 1),
        "planned_rr": float(cand.get("rr", 0)),
        "risk_pct": float(cand.get("risk_pct", 0)),
        "atr_pct_at_entry": float(cand.get("atr_pct", 0)),
        "zone_type": str(winning_zone.get("tier") or cand.get("tier_used") or "T1"),
        **{key: btc[key] for key in (
            "btc_return_4h", "btc_return_12h", "btc_return_24h",
            "btc_distance_from_ma", "btc_volatility")},
        "btc_trend_direction": "up" if r24 > 0 else "down" if r24 < 0 else "flat",
        "btc_trend_state": ("bull" if r24 > 0 and distance > 0 else
                            "bear" if r24 < 0 and distance < 0 else "mixed"),
        "volatility_regime": ("low" if volatility <= q1 else
                              "mid" if volatility <= q2 else "high"),
        "risk_state": ("risk_on" if r24 > 0 and distance > 0 else
                       "risk_off" if r24 < 0 and distance < 0 else "neutral"),
        "last_closed_candle_close_time": btc["last_closed_candle_close_time"],
        "score_timestamp_ms": now_ms,
    }
    return snapshot


def compute_ml_score(cand: dict, *, now_ms: int | None = None,
                     btc_klines: list | None = None) -> dict:
    """Return shadow metadata; any failure returns score=None and never raises."""
    scored_at = datetime.now(timezone.utc)
    result = {
        "ml_score": None,
        "ml_model_version": _MODEL_VERSION,
        "prediction_probability": None,
        "confidence": None,
        "feature_snapshot": None,
        "created_at": scored_at.isoformat(),
        "mode": "OBSERVATION_ONLY",
        "error": None,
    }
    try:
        bundle = _load_model()
        if bundle is None:
            return result
        timestamp_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        snapshot = _build_feature_snapshot(
            cand, bundle["metadata"], now_ms=timestamp_ms, btc_klines=btc_klines)
        model_input = pd.DataFrame([{key: snapshot.get(key)
                                    for key in bundle["schema"]["model_features"]}])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            probability = float(bundle["model"].predict_proba(model_input)[0, 1])
        result.update({
            "ml_score": round(probability, 4),
            "prediction_probability": round(probability, 6),
            "confidence": round(probability, 6),
            "feature_snapshot": snapshot,
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  [ML SHADOW] ⚠ scoring unavailable; passthrough: {result['error']}")
    return result


def attach_shadow_score(cand: dict, **kwargs) -> dict:
    """Attach observation metadata without changing any strategy field."""
    try:
        result = compute_ml_score(cand, **kwargs)
    except Exception as exc:  # defence in depth around the fail-open scorer
        result = {"ml_score": None, "ml_model_version": _MODEL_VERSION,
                  "prediction_probability": None, "confidence": None,
                  "feature_snapshot": None,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "mode": "OBSERVATION_ONLY",
                  "error": f"{type(exc).__name__}: {exc}"}
    cand["ml_score"] = result.get("ml_score")
    cand["ml_model_version"] = result.get("ml_model_version", _MODEL_VERSION)
    cand["ml_shadow"] = result
    if result.get("ml_score") is not None:
        print(
            "\n[ML SHADOW]\n"
            f"Symbol: {cand.get('symbol')}\n"
            f"Model: {cand['ml_model_version']}\n"
            f"Confidence: {result['ml_score']:.2f}\n"
            "Mode: OBSERVATION_ONLY\n"
        )
    return result
