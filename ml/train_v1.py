"""
ml/train_v1.py — Spot trade ML exploration, rule v1.0.0
=========================================================
EKSPERIMEN AWAL — BUKAN UNTUK PRODUCTION.
Model ini TIDAK diintegrasikan ke --propose atau logic trading manapun.
Tujuan: eksplorasi sinyal dari fitur yang ada dengan n=53.

Menjalankan DUA metode validasi:
  1. LOOCV  — Leave-One-Trade-Out (standard, optimistic estimate)
  2. LOCO   — Leave-One-Cluster-Out (conservative, honest estimate)
             Single trades = masing-masing jadi "cluster" sendiri.

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
from sklearn.model_selection import LeaveOneOut, LeaveOneGroupOut, cross_val_predict
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import (
    accuracy_score, confusion_matrix, classification_report, roc_auc_score
)
import joblib

from supabase_client import fetch_all_spot

# ── Config ────────────────────────────────────────────────────────────
VALID_RULE_VERSIONS = {"v1.0.0", None}
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

    closed_mask = df[TARGET].isin(["TP_HIT", "SL_HIT"])
    rule_mask   = df["rule_version"].isin(["v1.0.0"]) | df["rule_version"].isna()
    df          = df[closed_mask & rule_mask].copy().reset_index(drop=True)

    print(f"  Total closed v1.0.0 trades : {len(df)}")
    print(f"    rule_version=v1.0.0      : {(df['rule_version']=='v1.0.0').sum()}")
    print(f"    rule_version=null        : {df['rule_version'].isna().sum()}")

    df["win"] = (df[TARGET] == "TP_HIT").astype(int)

    tp = df["win"].sum()
    sl = (1 - df["win"]).sum()
    print(f"  TP_HIT (win=1): {tp}   SL_HIT (win=0): {sl}")
    print(f"  Baseline win rate: {tp/len(df)*100:.1f}%")

    # Build cluster group labels for LOCO
    # Single trades (no cluster_id) get their own unique group = their index
    def _group(row):
        cid = row.get("correlation_cluster_id")
        return cid if cid else f"single_{row.name}"

    df["_group"] = df.apply(_group, axis=1)

    n_groups  = df["_group"].nunique()
    n_single  = df["_group"].str.startswith("single_").sum()
    n_cluster = n_groups - n_single
    print(f"\n  Cluster structure for LOCO:")
    print(f"    Total groups   : {n_groups}  ({n_cluster} real clusters + {n_single} singles)")

    from collections import Counter
    gc = Counter(df["_group"])
    multi = {k: v for k, v in gc.items() if v > 1}
    for cid, n in sorted(multi.items(), key=lambda x: -x[1]):
        w = df[df["_group"] == cid]["win"].sum()
        print(f"    {cid}  n={n}  wins={w}  losses={n-w}")

    return df


def build_feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, np.ndarray]:
    numeric = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    X = df[numeric].copy().astype(float)
    zone_dummies = pd.get_dummies(df["zone_type"], prefix="zone", drop_first=True)
    X = pd.concat([X, zone_dummies], axis=1)
    y = df["win"]

    # Encode groups as integers for sklearn
    from sklearn.preprocessing import LabelEncoder
    le     = LabelEncoder()
    groups = le.fit_transform(df["_group"])

    return X, y, groups


def make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr",     LogisticRegression(
            max_iter    = 1000,
            solver      = "lbfgs",
            C           = 1.0,
            random_state= 42,
        )),
    ])


def run_loocv(X, y):
    """Leave-One-Trade-Out — optimistic, ignores cluster structure."""
    loo      = LeaveOneOut()
    y_pred   = cross_val_predict(make_pipe(), X, y, cv=loo)
    y_proba  = cross_val_predict(make_pipe(), X, y, cv=loo, method="predict_proba")
    return y_pred, y_proba[:, 1]


def run_loco(X, y, groups):
    """Leave-One-Cluster-Out — conservative, respects cluster structure."""
    logo     = LeaveOneGroupOut()
    y_pred   = cross_val_predict(make_pipe(), X, y, cv=logo, groups=groups)
    y_proba  = cross_val_predict(make_pipe(), X, y, cv=logo, groups=groups,
                                  method="predict_proba")
    return y_pred, y_proba[:, 1]


def print_cv_block(label: str, note: str, y, y_pred, y_proba, baseline_wr):
    n   = len(y)
    acc = accuracy_score(y, y_pred)
    try:
        auc = roc_auc_score(y, y_proba)
    except Exception:
        auc = float("nan")

    cm  = confusion_matrix(y, y_pred)
    tn, fp, fn, tp_ = (cm.ravel() if cm.shape == (2,2)
                       else (cm[0,0], 0, 0, cm[1,1]))

    print(f"\n{SEP}")
    print(f"  {label}")
    print(f"  {note}")
    print(SEP)
    print(f"  Accuracy  : {acc*100:.1f}%  (baseline {baseline_wr*100:.1f}%,"
          f" delta {(acc-baseline_wr)*100:+.1f}pp)")
    print(f"  ROC-AUC   : {auc:.3f}  (0.5=random, 1.0=perfect)")
    print(f"\n  Confusion matrix:")
    print(f"                Pred SL  Pred TP")
    print(f"    Actual SL     {tn:>4}      {fp:>4}")
    print(f"    Actual TP     {fn:>4}      {tp_:>4}")
    print()
    print(classification_report(y, y_pred,
                                  target_names=["SL_HIT", "TP_HIT"],
                                  digits=3))


def print_coefficients(X, final_model):
    print(f"\n{SEP}")
    print("  FEATURE COEFFICIENTS  (final model, fit on ALL data)")
    print("  Positive = associated with WIN (TP_HIT)")
    print("  Values are in standardised units")
    print(SEP)

    lr        = final_model.named_steps["lr"]
    feat_names = list(X.columns)
    coefs      = lr.coef_[0]

    sorted_idx = np.argsort(np.abs(coefs))[::-1]
    for i in sorted_idx:
        direction = "→ WIN ↑" if coefs[i] > 0 else "→ LOSS ↑"
        print(f"  {feat_names[i]:<25}  coef={coefs[i]:+.4f}  {direction}")
    print(f"\n  Intercept: {lr.intercept_[0]:+.4f}")


def print_comparison(acc_loo, auc_loo, acc_loco, auc_loco, baseline_wr):
    print(f"\n{SEP}")
    print("  COMPARISON SUMMARY")
    print(SEP)
    print(f"  {'Method':<30} {'Accuracy':>10} {'vs Baseline':>13} {'AUC':>8}")
    print(f"  {'-'*63}")
    print(f"  {'Baseline (always SL)':<30} {baseline_wr*100:>9.1f}%"
          f" {'—':>13} {'—':>8}")
    print(f"  {'LOOCV (Leave-One-Trade-Out)':<30} {acc_loo*100:>9.1f}%"
          f" {(acc_loo-baseline_wr)*100:>+12.1f}pp {auc_loo:>8.3f}")
    print(f"  {'LOCO (Leave-One-Cluster-Out)':<30} {acc_loco*100:>9.1f}%"
          f" {(acc_loco-baseline_wr)*100:>+12.1f}pp {auc_loco:>8.3f}")
    print()
    print("  LOOCV vs LOCO gap shows how much cluster leakage inflated LOOCV.")
    print("  LOCO is the honest estimate. AUC is the key metric (accuracy")
    print("  is misleading when classes are imbalanced).")


def save_model(model, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path)
    print(f"\n  Model saved → {path}")
    print("  (for reference only — NOT loaded by any trading logic)")


def print_warning():
    w = """
╔══════════════════════════════════════════════════════════════╗
║  ⚠  EXPERIMENTAL — DO NOT USE FOR TRADING DECISIONS         ║
╠══════════════════════════════════════════════════════════════╣
║  • n=53 is far too small for reliable ML inference           ║
║  • LOOCV inflates performance — use LOCO as honest estimate  ║
║  • Logistic Regression assumes linear decision boundary      ║
║  • No walk-forward / time-series validation                  ║
║  • Results below are for EXPLORATION ONLY                    ║
║  • Model is NOT integrated into --propose or any bot logic   ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(w)


def main():
    print_warning()

    df          = load_data()
    baseline_wr = df["win"].mean()

    X, y, groups = build_feature_matrix(df)

    print(f"\n  Feature columns: {list(X.columns)}")
    print(f"  zone_type dist : {df['zone_type'].value_counts().to_dict()}")

    # ── LOOCV ─────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  RUNNING LOOCV (Leave-One-Trade-Out)")
    print(SEP)
    y_pred_loo, y_proba_loo = run_loocv(X, y)
    print_cv_block(
        "LOOCV — Leave-One-Trade-Out  [OPTIMISTIC — ignores clusters]",
        "Each fold holds out 1 trade. Trades from the same cluster remain in training.",
        y, y_pred_loo, y_proba_loo, baseline_wr,
    )

    # ── LOCO ──────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  RUNNING LOCO (Leave-One-Cluster-Out)")
    print(SEP)
    y_pred_loco, y_proba_loco = run_loco(X, y, groups)
    print_cv_block(
        "LOCO — Leave-One-Cluster-Out  [CONSERVATIVE — honest estimate]",
        "Each fold holds out all trades from one cluster/session.",
        y, y_pred_loco, y_proba_loco, baseline_wr,
    )

    # ── Comparison ────────────────────────────────────────────────────
    print_comparison(
        accuracy_score(y, y_pred_loo),
        roc_auc_score(y, y_proba_loo),
        accuracy_score(y, y_pred_loco),
        roc_auc_score(y, y_proba_loco),
        baseline_wr,
    )

    # ── Final model (all data) for coefficients ────────────────────────
    final_model = make_pipe()
    final_model.fit(X, y)
    print_coefficients(X, final_model)

    save_model(final_model, MODEL_PATH)

    print_warning()


if __name__ == "__main__":
    main()
