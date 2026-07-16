"""
ml/train_v1.py — Spot trade ML exploration, rule v1.0.0
=========================================================
EKSPERIMEN AWAL — BUKAN UNTUK PRODUCTION.
Model ini TIDAK diintegrasikan ke --propose atau logic trading manapun.
Tujuan: eksplorasi sinyal dari fitur yang ada dengan n=53.

WARNING dicetak ulang di akhir output.

Jalankan dari root project:
    python3 ml/train_v1.py
"""

from __future__ import annotations

import os, sys, warnings
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import (
    accuracy_score, confusion_matrix, classification_report, roc_auc_score
)
import joblib

from supabase_client import fetch_all_spot

# ── Config ────────────────────────────────────────────────────────────
VALID_RULE_VERSIONS = {"v1.0.0", None}   # None = confirmed same logic
MODEL_PATH          = ROOT / "ml" / "models" / "v1.pkl"
FEATURES            = ["zone_touches", "planned_rr", "risk_pct",
                       "atr_pct_at_entry", "zone_type"]
TARGET              = "exit_status"

SEP = "=" * 65


def load_data() -> pd.DataFrame:
    print(f"\n{SEP}")
    print("  LOADING DATA")
    print(SEP)

    rows = fetch_all_spot()
    df   = pd.DataFrame(rows)

    # Filter: closed trades only, rule v1.0.0 (+ null confirmed same)
    closed_mask = df[TARGET].isin(["TP_HIT", "SL_HIT"])
    rule_mask   = df["rule_version"].isin(["v1.0.0"]) | df["rule_version"].isna()
    df          = df[closed_mask & rule_mask].copy()

    print(f"  Total closed v1.0.0 trades : {len(df)}")
    print(f"    rule_version=v1.0.0      : {(df['rule_version']=='v1.0.0').sum()}")
    print(f"    rule_version=null        : {df['rule_version'].isna().sum()}")

    # Build target
    df["win"] = (df[TARGET] == "TP_HIT").astype(int)

    tp = df["win"].sum()
    sl = (1 - df["win"]).sum()
    print(f"  TP_HIT (win=1): {tp}   SL_HIT (win=0): {sl}")
    print(f"  Baseline win rate: {tp/len(df)*100:.1f}%")

    # Feature null check
    print(f"\n  Feature null counts:")
    for f in FEATURES:
        n = df[f].isna().sum()
        print(f"    {f:<25} nulls={n}")

    return df


def build_feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Build X (features) and y (target), with one-hot for zone_type."""
    numeric = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    X = df[numeric].copy().astype(float)

    # One-hot encode zone_type (T1/T2) — drop_first to avoid multicollinearity
    zone_dummies = pd.get_dummies(df["zone_type"], prefix="zone", drop_first=True)
    X = pd.concat([X, zone_dummies], axis=1)

    y = df["win"]
    return X, y


def run_loocv(X: pd.DataFrame, y: pd.Series) -> np.ndarray:
    """Run LOOCV with LogisticRegression inside a scaling pipeline."""
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("lr",     LogisticRegression(
            max_iter    = 1000,
            solver      = "lbfgs",
            C           = 1.0,     # default regularisation — conservative for small n
            random_state= 42,
        )),
    ])

    loo        = LeaveOneOut()
    y_pred     = cross_val_predict(pipe, X, y, cv=loo)
    y_pred_proba = cross_val_predict(pipe, X, y, cv=loo, method="predict_proba")

    return y_pred, y_pred_proba[:, 1]


def fit_final_model(X: pd.DataFrame, y: pd.Series) -> Pipeline:
    """Fit on ALL data — for coefficient inspection and model saving only."""
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("lr",     LogisticRegression(
            max_iter    = 1000,
            solver      = "lbfgs",
            C           = 1.0,
            random_state= 42,
        )),
    ])
    pipe.fit(X, y)
    return pipe


def print_results(
    X: pd.DataFrame,
    y: pd.Series,
    y_pred: np.ndarray,
    y_pred_proba: np.ndarray,
    final_model: Pipeline,
    baseline_wr: float,
) -> None:
    n = len(y)

    print(f"\n{SEP}")
    print("  LOOCV RESULTS  (n={n})".format(n=n))
    print(SEP)

    acc      = accuracy_score(y, y_pred)
    try:
        auc  = roc_auc_score(y, y_pred_proba)
    except Exception:
        auc  = float("nan")

    cm       = confusion_matrix(y, y_pred)
    tp_pred  = cm[1, 1] if cm.shape == (2, 2) else "?"
    tn_pred  = cm[0, 0] if cm.shape == (2, 2) else "?"
    fp_pred  = cm[0, 1] if cm.shape == (2, 2) else "?"
    fn_pred  = cm[1, 0] if cm.shape == (2, 2) else "?"

    print(f"  LOOCV Accuracy : {acc*100:.1f}%")
    print(f"  Baseline (WR)  : {baseline_wr*100:.1f}%")
    print(f"  Delta vs base  : {(acc - baseline_wr)*100:+.1f}pp")
    print(f"  LOOCV ROC-AUC  : {auc:.3f}  (0.5 = random, 1.0 = perfect)")
    print()
    print(f"  Confusion matrix (LOOCV):")
    print(f"                Pred SL  Pred TP")
    print(f"    Actual SL     {tn_pred:>4}      {fp_pred:>4}")
    print(f"    Actual TP     {fn_pred:>4}      {tp_pred:>4}")
    print()
    print("  Classification report (LOOCV):")
    print(classification_report(y, y_pred,
                                 target_names=["SL_HIT", "TP_HIT"],
                                 digits=3))

    # ── Coefficients from final model ─────────────────────────────────
    print(f"\n{SEP}")
    print("  FEATURE COEFFICIENTS  (final model, fit on all data)")
    print("  Positive = associated with WIN (TP_HIT)")
    print("  Values are in standardised units")
    print(SEP)

    lr        = final_model.named_steps["lr"]
    scaler    = final_model.named_steps["scaler"]
    feat_names = list(X.columns)
    coefs      = lr.coef_[0]

    # Sort by absolute value descending
    sorted_idx = np.argsort(np.abs(coefs))[::-1]
    for i in sorted_idx:
        direction = "→ WIN ↑" if coefs[i] > 0 else "→ LOSS ↑"
        print(f"  {feat_names[i]:<25}  coef={coefs[i]:+.4f}  {direction}")

    print(f"\n  Intercept: {lr.intercept_[0]:+.4f}")


def save_model(model: Pipeline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path)
    print(f"\n  Model saved → {path}")
    print("  (for reference only — NOT loaded by any trading logic)")


def print_warning() -> None:
    w = """
╔══════════════════════════════════════════════════════════════╗
║  ⚠  EXPERIMENTAL — DO NOT USE FOR TRADING DECISIONS         ║
╠══════════════════════════════════════════════════════════════╣
║  • n=53 is far too small for reliable ML inference           ║
║  • LOOCV reduces but does not eliminate overfitting risk     ║
║  • Logistic Regression assumes linear decision boundary —    ║
║    market dynamics are rarely linear                         ║
║  • No walk-forward validation — temporal leakage possible    ║
║  • Results below are for EXPLORATION ONLY                    ║
║  • Model is NOT integrated into --propose or any bot logic   ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(w)


def main() -> None:
    print_warning()

    df           = load_data()
    baseline_wr  = df["win"].mean()

    X, y         = build_feature_matrix(df)

    print(f"\n{SEP}")
    print("  FEATURE MATRIX")
    print(SEP)
    print(f"  Shape: {X.shape}  (rows=trades, cols=features)")
    print(f"  Columns: {list(X.columns)}")
    print(f"  zone_type distribution:\n{df['zone_type'].value_counts().to_string()}")

    print(f"\n{SEP}")
    print("  RUNNING LOOCV  (Leave-One-Out, n folds = n samples)")
    print(SEP)
    y_pred, y_pred_proba = run_loocv(X, y)

    final_model = fit_final_model(X, y)

    print_results(X, y, y_pred, y_pred_proba, final_model, baseline_wr)

    save_model(final_model, MODEL_PATH)

    print_warning()


if __name__ == "__main__":
    main()
