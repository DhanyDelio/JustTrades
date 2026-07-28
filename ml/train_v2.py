"""
ml/train_v2.py — Spot trade ML Phase 2
========================================
Focuses on finding the optimal probability threshold on LOCO CV
to filter out bad entries (increase Win Rate / Precision)
without destroying positive Expected Value.

Uses trades_spot only.
Model saved to ml/models/v2.pkl.
"""

import sys, os
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneGroupOut, cross_val_predict
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import roc_auc_score
import joblib

from services.supabase_client import fetch_all_spot

MODEL_PATH = ROOT / "ml" / "models" / "v2.pkl"
SEP = "=" * 65

def load_data() -> pd.DataFrame:
    print(f"\n{SEP}")
    print("  LOADING DATA (SPOT ONLY)")
    print(SEP)

    rows = fetch_all_spot()
    df = pd.DataFrame(rows)

    closed_mask = df["exit_status"].isin(["TP_HIT", "SL_HIT"])
    # We want to train on recent data too, so we allow v1.0.0, v2.0.0, etc or NaN
    # We'll just take all closed spot trades.
    df = df[closed_mask].copy().reset_index(drop=True)

    print(f"  Total closed spot trades (Raw N) : {len(df)}")
    
    df["win"] = (df["exit_status"] == "TP_HIT").astype(int)

    # ── Error Analysis & Sample Weighting ──
    weights = []
    c_null, c_fp, c_fn, c_normal = 0, 0, 0, 0
    
    for _, row in df.iterrows():
        ml_score = row.get("ml_score")
        status = row.get("exit_status")
        
        if pd.isna(ml_score):
            weights.append(1.0)
            c_null += 1
        elif ml_score >= 0.50 and status == "SL_HIT":
            weights.append(2.5)  # False Positive (Trap)
            c_fp += 1
        elif ml_score < 0.30 and status == "TP_HIT":
            weights.append(2.0)  # False Negative (Worth It)
            c_fn += 1
        else:
            weights.append(1.0)  # True Positive / True Negative / Standard
            c_normal += 1
            
    df["sample_weight"] = weights
    
    print(f"  Old Data (ml_score null) : {c_null}")
    print(f"  New Data (ml_score set)  : {c_fp + c_fn + c_normal}")
    print(f"  Traps (FP, w=2.5)        : {c_fp}")
    print(f"  Worth It (FN, w=2.0)     : {c_fn}")

    def _group(row):
        cid = row.get("correlation_cluster_id")
        return cid if cid else f"single_{row.name}"

    df["_group"] = df.apply(_group, axis=1)
    n_groups = df["_group"].nunique()
    print(f"  Effective N (Clusters/Groups)    : {n_groups}")
    
    return df

def build_feature_matrix(df: pd.DataFrame):
    numeric = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    # Handle NaNs if any
    df[numeric] = df[numeric].fillna(0)
    X = df[numeric].copy().astype(float)
    
    # Fill zone_type NaN with 'T1'
    df["zone_type"] = df["zone_type"].fillna("T1")
    zone_dummies = pd.get_dummies(df["zone_type"], prefix="zone", drop_first=True)
    X = pd.concat([X, zone_dummies], axis=1)
    
    y = df["win"]
    
    from sklearn.preprocessing import LabelEncoder
    groups = LabelEncoder().fit_transform(df["_group"])
    
    return X, y, groups

def make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            max_iter=1000, solver="lbfgs", C=1.0, random_state=42, class_weight='balanced'
        )),
    ])

def analyze_thresholds(df, y_proba_loco):
    df["loco_prob"] = y_proba_loco
    baseline_wr = df["win"].mean()
    print(f"\n{SEP}")
    print(f"  THRESHOLD ANALYSIS (LOCO CV)")
    print(f"  Baseline Win Rate (All Trades): {baseline_wr*100:.1f}%")
    print(SEP)
    
    avg_win = df[df["win"]==1]["realized_pnl_pct"].mean() if "realized_pnl_pct" in df else 2.0
    avg_loss = abs(df[df["win"]==0]["realized_pnl_pct"].mean()) if "realized_pnl_pct" in df else 1.0
    
    print(f"{'Threshold':<10} | {'Trades':<8} | {'Win Rate':<10} | {'Filtered':<9} | {'EV Proxy':<10}")
    print("-" * 55)
    
    best_thresh = 0.5
    best_ev = -9999
    
    for thresh in np.arange(0.1, 0.95, 0.05):
        approved = df[df["loco_prob"] >= thresh]
        n_approved = len(approved)
        n_filtered = len(df) - n_approved
        
        if n_approved == 0:
            continue
            
        wr = approved["win"].mean()
        # EV Proxy: (WR * AvgWin) - (LossRate * AvgLoss) * n_approved
        # Here we just look at per-trade EV
        ev_per_trade = (wr * avg_win) - ((1 - wr) * avg_loss)
        total_ev_proxy = ev_per_trade * n_approved
        
        if total_ev_proxy > best_ev and n_approved > 10:
            best_ev = total_ev_proxy
            best_thresh = thresh
            
        print(f"> {thresh:<8.2f} | {n_approved:<8} | {wr*100:>5.1f}%     | {n_filtered:<9} | {total_ev_proxy:>8.2f}")
    
    print(f"\n  => OPTIMAL THRESHOLD (Max EV, n>10): > {best_thresh:.2f}")
    
def main():
    df = load_data()
    X, y, groups = build_feature_matrix(df)
    sample_weights = df["sample_weight"].values
    
    print(f"\n  Running LOCO (Leave-One-Cluster-Out)...")
    logo = LeaveOneGroupOut()
    fit_params = {"lr__sample_weight": sample_weights}
    
    y_proba_loco = cross_val_predict(
        make_pipe(), X, y, cv=logo, groups=groups, 
        method="predict_proba", params=fit_params
    )[:, 1]  # type: ignore
    
    try:
        auc_loco = roc_auc_score(y, y_proba_loco)
    except Exception:
        auc_loco = float("nan")
        
    print(f"  LOCO ROC-AUC Score: {auc_loco:.3f}")
    
    analyze_thresholds(df, y_proba_loco)
    
    final_model = make_pipe()
    final_model.fit(X, y, lr__sample_weight=sample_weights)
    
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    print(f"\n  [✅] Model saved successfully to {MODEL_PATH}")

if __name__ == "__main__":
    main()
