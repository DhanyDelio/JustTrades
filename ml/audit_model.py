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
from sklearn.metrics import roc_auc_score, average_precision_score, confusion_matrix

from services.supabase_client import fetch_all_spot

def main():
    print("--- 1. DATA INTEGRITY AUDIT ---")
    rows = fetch_all_spot()
    df_raw = pd.DataFrame(rows)
    
    total_samples = len(df_raw)
    print(f"Total Samples (N): {total_samples}")
    
    # Check for specific trades (e.g. SEIUSDT)
    sei_trades = df_raw[df_raw['symbol'].str.contains('SEI', na=False, case=False)]
    if not sei_trades.empty:
        print("\nFound SEI trades:")
        for _, row in sei_trades.iterrows():
            print(f" - {row['symbol']} | Status: {row.get('exit_status', 'UNKNOWN')} | PnL: {row.get('realized_pnl_pct', 0)}")
    else:
        print("\nNo SEI trades found.")

    closed_mask = df_raw["exit_status"].isin(["TP_HIT", "SL_HIT"])
    df = df_raw[closed_mask].copy().reset_index(drop=True)
    
    effective_n = len(df)
    print(f"\nEffective N (Closed Trades): {effective_n}")
    
    df["win"] = (df["exit_status"] == "TP_HIT").astype(int)
    wins = df["win"].sum()
    losses = effective_n - wins
    print(f"Win/Loss Ratio: {wins}/{losses} (Win Rate: {wins/effective_n*100:.2f}%)")

    # Features
    numeric = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    df[numeric] = df[numeric].fillna(0)
    X = df[numeric].copy().astype(float)
    df["zone_type"] = df["zone_type"].fillna("T1")
    zone_dummies = pd.get_dummies(df["zone_type"], prefix="zone", drop_first=True)
    X = pd.concat([X, zone_dummies], axis=1)
    
    y = df["win"]

    def _group(row):
        cid = row.get("correlation_cluster_id")
        return cid if cid else f"single_{row.name}"

    df["_group"] = df.apply(_group, axis=1)
    from sklearn.preprocessing import LabelEncoder
    groups = LabelEncoder().fit_transform(df["_group"])

    print("\n--- 2. LOCO CV EVALUATION (OOF) ---")
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(max_iter=1000, solver="lbfgs", C=1.0, random_state=42, class_weight='balanced')),
    ])
    
    # We will compute OOF predictions
    logo = LeaveOneGroupOut()
    
    # Sample Weights (same as train_v2.py)
    weights = []
    for _, row in df.iterrows():
        ml_score = row.get("ml_score")
        status = row.get("exit_status")
        if pd.isna(ml_score):
            weights.append(1.0)
        elif ml_score >= 0.50 and status == "SL_HIT":
            weights.append(2.5)
        elif ml_score < 0.30 and status == "TP_HIT":
            weights.append(2.0)
        else:
            weights.append(1.0)
    sample_weights = np.array(weights)
    
    fit_params = {"lr__sample_weight": sample_weights}

    y_proba_oof = cross_val_predict(
        pipe, X, y, cv=logo, groups=groups, 
        method="predict_proba", params=fit_params
    )[:, 1]

    # ROC-AUC
    roc_auc = roc_auc_score(y, y_proba_oof)
    print(f"ROC-AUC (OOF): {roc_auc:.4f}")
    
    # PR-AUC
    pr_auc = average_precision_score(y, y_proba_oof)
    baseline_pr = y.mean()
    print(f"PR-AUC (OOF): {pr_auc:.4f} (Baseline: {baseline_pr:.4f})")
    
    # Confusion Matrix (Threshold 0.50)
    y_pred_50 = (y_proba_oof >= 0.50).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, y_pred_50).ravel()
    print("\nConfusion Matrix (Threshold 0.50):")
    print(f"True Positive (TP) : {tp}  | False Positive (FP) : {fp}")
    print(f"False Negative (FN): {fn}  | True Negative (TN)  : {tn}")
    
    print("\n--- 3. FEATURE IMPORTANCE ---")
    # Fit on all data for feature importance
    pipe.fit(X, y, lr__sample_weight=sample_weights)
    coefs = pipe.named_steps["lr"].coef_[0]
    feature_names = X.columns
    
    importance = pd.DataFrame({"Feature": feature_names, "Coefficient": coefs})
    importance["Abs_Coef"] = importance["Coefficient"].abs()
    importance = importance.sort_values(by="Abs_Coef", ascending=False).head(5)
    
    for _, row in importance.iterrows():
        sign = "+" if row["Coefficient"] > 0 else "-"
        print(f" - {row['Feature']:<20}: {row['Coefficient']:.4f} ({sign})")

if __name__ == "__main__":
    main()
