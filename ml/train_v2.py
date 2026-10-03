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

    # ── Provenance-aware eligibility filter ───────────────────────────────
    # Only genuine exchange fills are clean labels for ML training.
    CLEAN_EXIT_REASONS = {"TP_HIT", "SL_HIT"}
    EXCLUDED_EXIT_REASONS = {
        "PRICE_GUARD_SL",
        "UNPROTECTED_SL_BREACH",
        "UNPROTECTED_TP_BREACH",
        "OCO_STUCK_MANUAL_RESOLUTION",
        "EMERGENCY_CLOSED",
        "RECOVERED_SL_HIT",
        "STALE_SETUP_CANCELLED",
    }

    closed_mask = df["exit_status"].isin(["TP_HIT", "SL_HIT"])

    # NULL exit_reason rows with TP_HIT/SL_HIT status are INCLUDED (legacy,
    # predate exit_reason field) — included with provenance uncertainty warning.
    null_reason_mask = (
        df["exit_status"].isin(["TP_HIT", "SL_HIT"]) &
        df["exit_reason"].isna()
    )
    if null_reason_mask.sum() > 0:
        print(
            f"  [WARNING] {null_reason_mask.sum()} rows have NULL exit_reason "
            f"with TP_HIT/SL_HIT status — included with provenance uncertainty"
        )

    clean_mask = (
        df["exit_status"].isin(["TP_HIT", "SL_HIT"]) &
        ~df["exit_reason"].isin(EXCLUDED_EXIT_REASONS)
    )

    # We want to train on recent data too, so we allow v1.0.0, v2.0.0, etc or NaN
    # We'll just take all closed spot trades that pass the provenance filter.
    df = df[closed_mask & clean_mask].copy().reset_index(drop=True)

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

def _attach_time_order(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    entry_fill_dt = pd.to_datetime(df.get("entry_fill_time"), unit="ms", utc=True, errors="coerce")
    open_time_dt = pd.to_datetime(df.get("open_time"), utc=True, errors="coerce")
    created_at_dt = pd.to_datetime(df.get("created_at"), utc=True, errors="coerce")

    df["_entry_sort_dt"] = entry_fill_dt
    df.loc[df["_entry_sort_dt"].isna(), "_entry_sort_dt"] = open_time_dt[df["_entry_sort_dt"].isna()]
    df.loc[df["_entry_sort_dt"].isna(), "_entry_sort_dt"] = created_at_dt[df["_entry_sort_dt"].isna()]

    missing_time = int(df["_entry_sort_dt"].isna().sum())
    if missing_time:
        print(f"  [WARN] {missing_time} row(s) missing entry timestamp for time-based eval — dropping them.")

    return df.dropna(subset=["_entry_sort_dt"]).sort_values("_entry_sort_dt").reset_index(drop=True)


def evaluate_time_based(df: pd.DataFrame, use_sample_weight: bool = True) -> float:
    df_time = _attach_time_order(df)
    n = len(df_time)
    mode_label = "Weighted" if use_sample_weight else "Uniform"

    print(f"\n{SEP}")
    print(f"  TIME-BASED EVALUATION (Walk-Forward, {mode_label})")
    print(SEP)

    if n < 20:
        print(f"  Not enough timestamped samples for time-based evaluation (n={n}).")
        return float("nan")

    min_train = max(int(np.ceil(n * 0.70)), 10)
    test_window = max(int(np.ceil(n * 0.10)), 1)

    if min_train >= n:
        print(f"  Not enough holdout samples after 70/30 split rule (n={n}).")
        return float("nan")

    oof_indices: list[int] = []
    oof_probs: list[float] = []
    fold_count = 0

    print(f"  Samples with valid timestamps      : {n}")
    print(f"  Initial train window (70%)        : {min_train}")
    print(f"  Walk-forward test window          : {test_window}")
    print(f"  Sample weighting mode             : {mode_label}")

    for test_start in range(min_train, n, test_window):
        test_end = min(test_start + test_window, n)
        train_df = df_time.iloc[:test_start].copy()
        test_df = df_time.iloc[test_start:test_end].copy()

        if len(test_df) == 0 or train_df["win"].nunique() < 2:
            continue

        X_train, y_train, _ = build_feature_matrix(train_df.copy())
        X_test, y_test, _ = build_feature_matrix(test_df.copy())
        X_test = X_test.reindex(columns=X_train.columns, fill_value=0)
        train_weights = train_df["sample_weight"].values if use_sample_weight else np.ones(len(train_df), dtype=float)

        model = make_pipe()
        model.fit(X_train, y_train, lr__sample_weight=train_weights)
        fold_probs = model.predict_proba(X_test)[:, 1]

        oof_indices.extend(test_df.index.tolist())
        oof_probs.extend(fold_probs.tolist())
        fold_count += 1

    if not oof_probs:
        print("  Could not produce walk-forward predictions.")
        return float("nan")

    eval_df = df_time.loc[oof_indices].copy()
    eval_df["time_prob"] = oof_probs

    if eval_df["win"].nunique() < 2:
        print("  Walk-forward holdout contains only one class — ROC-AUC undefined.")
        return float("nan")

    auc_time = roc_auc_score(eval_df["win"], eval_df["time_prob"])
    print(f"  Walk-forward folds evaluated      : {fold_count}")
    print(f"  Time-based ROC-AUC Score          : {auc_time:.3f}")
    print(f"  Holdout predictions used          : {len(eval_df)}")
    if not eval_df.empty:
        print(
            f"  Holdout span                      : "
            f"{eval_df['_entry_sort_dt'].min()}  →  {eval_df['_entry_sort_dt'].max()}"
        )

    return auc_time


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
    
def _run_loco_eval(X, y, groups, sample_weights, use_sample_weight: bool = True):
    mode_label = "Weighted" if use_sample_weight else "Uniform"
    print(f"\n  Running LOCO (Leave-One-Cluster-Out, {mode_label})...")
    logo = LeaveOneGroupOut()
    eval_weights = sample_weights if use_sample_weight else np.ones(len(y), dtype=float)
    fit_params = {"lr__sample_weight": eval_weights}

    y_proba_loco = cross_val_predict(
        make_pipe(), X, y, cv=logo, groups=groups,
        method="predict_proba", params=fit_params
    )[:, 1]  # type: ignore

    try:
        auc_loco = roc_auc_score(y, y_proba_loco)
    except Exception:
        auc_loco = float("nan")

    print(f"  LOCO ROC-AUC Score ({mode_label}): {auc_loco:.3f}")
    return auc_loco, y_proba_loco


def main():
    df = load_data()
    X, y, groups = build_feature_matrix(df)
    sample_weights = df["sample_weight"].values

    auc_loco_weighted, y_proba_loco_weighted = _run_loco_eval(
        X, y, groups, sample_weights, use_sample_weight=True
    )
    auc_loco_uniform, _ = _run_loco_eval(
        X, y, groups, sample_weights, use_sample_weight=False
    )

    auc_time_weighted = evaluate_time_based(df, use_sample_weight=True)
    auc_time_uniform = evaluate_time_based(df, use_sample_weight=False)

    print(f"\n{SEP}")
    print("  EVALUATION SUMMARY")
    print(SEP)
    print(f"  Raw N                  : {len(df)}")
    print(f"  Effective N            : {df['_group'].nunique()}")
    print(f"  LOCO ROC-AUC (weighted): {auc_loco_weighted:.3f}" if not np.isnan(auc_loco_weighted) else "  LOCO ROC-AUC (weighted): nan")
    print(f"  LOCO ROC-AUC (uniform) : {auc_loco_uniform:.3f}" if not np.isnan(auc_loco_uniform) else "  LOCO ROC-AUC (uniform) : nan")
    print(f"  Time ROC-AUC (weighted): {auc_time_weighted:.3f}" if not np.isnan(auc_time_weighted) else "  Time ROC-AUC (weighted): nan")
    print(f"  Time ROC-AUC (uniform) : {auc_time_uniform:.3f}" if not np.isnan(auc_time_uniform) else "  Time ROC-AUC (uniform) : nan")

    analyze_thresholds(df, y_proba_loco_weighted)

    final_model = make_pipe()
    final_model.fit(X, y, lr__sample_weight=sample_weights)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    print(f"\n  [✅] Model saved successfully to {MODEL_PATH}")

if __name__ == "__main__":
    main()
