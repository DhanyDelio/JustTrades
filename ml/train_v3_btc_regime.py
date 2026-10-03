"""
train_v3_btc_regime.py
=======================
Part 2: BTC Regime Feature for Spot ML — Research Only.

Builds a discrete BTC market regime from strictly closed 4h candles,
using thresholds pre-registered from BTC price series statistics
BEFORE any outcome correlation was examined.

PRE-REGISTERED THRESHOLDS (from BTC 4h series Jul–Sep 2026, n=485):
  Trend (btc_4h_change_pct, std=0.738%):
    STRONG_UP:   chg > +0.738%   (> +1σ)
    MILD_UP:     chg in [0%, +0.738%]
    NEUTRAL:     chg in [-0.369%, 0%)  (within -0.5σ of 0)
    MILD_DOWN:   chg in [-0.738%, -0.369%)
    STRONG_DOWN: chg < -0.738%   (< -1σ)

  Volatility (realized vol over 6 closed 4h candles, terciles):
    VOL_LOW:  rv < 0.004026  (< p33)
    VOL_MID:  rv in [0.004026, 0.005997]
    VOL_HIGH: rv > 0.005997  (> p67)

  Combined btc_regime (trend × vol, 15 cells → collapsed to interpretable set):
    BULL_HIGH_VOL / BULL_LOW_VOL / BEAR_HIGH_VOL / BEAR_LOW_VOL / NEUTRAL_ANY

  These thresholds were NOT tuned to trade outcomes. They were fixed
  before this file was executed against the outcome dataset.

Closed-candle discipline (identical to ml/train_futures_short_v2.py):
  current_candle_open = floor(entry_ms / CANDLE_MS) * CANDLE_MS  ← never used
  prev_candle_open    = current_candle_open - CANDLE_MS           ← used
  Assertion: prev_candle.close_ts < entry_fill_time (strict <)
  btc_candle_closed_before_entry audit column added per row.

Comparison:
  A. baseline_v2 (no BTC)
  B. corrected single-momentum (btc_4h_change_pct, closed-candle)
  C. regime feature (btc_regime_label — this script)

Run:
    python3 ml/train_v3_btc_regime.py

No model saved. No Supabase writes. Research output only.
"""

from __future__ import annotations

import io
import math
import sys
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import LeaveOneGroupOut, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from services.supabase_client import fetch_all_spot

SEP       = "=" * 72
CANDLE_MS = 4 * 3600 * 1000
HEADERS   = {"User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36"}

# ---------------------------------------------------------------------------
# PRE-REGISTERED THRESHOLDS — fixed before outcome analysis
# Source: BTC 4h series Jul–Sep 2026 statistics, n=485 candles
# ---------------------------------------------------------------------------

# Trend: multiples of 1-sigma (0.738%) from the 4h change distribution
BTC_TREND_STD      = 0.7378   # σ of btc_4h_change_pct
BTC_STRONG_UP_THR  = +BTC_TREND_STD         # > +1σ
BTC_MILD_UP_THR    = 0.0                     # 0 to +1σ
BTC_NEUTRAL_LO_THR = -(BTC_TREND_STD / 2)   # -0.5σ to 0
BTC_MILD_DN_THR    = -BTC_TREND_STD          # -1σ to -0.5σ
# STRONG_DOWN: < -1σ

# Volatility: tercile thresholds from 6-candle realized vol distribution
RV_LOW_THR  = 0.004026   # p33
RV_HIGH_THR = 0.005997   # p67


# ---------------------------------------------------------------------------
# BTC data fetcher
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 20) -> requests.Response:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return requests.get(url, headers=HEADERS, timeout=timeout, verify=False)


def _parse_zip(content: bytes) -> dict[int, dict]:
    """Returns {open_ts_ms: {open_ts, close_ts, close}}."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    result = {}
    for line in raw.strip().split("\n"):
        if not line or line.startswith("open"):
            continue
        p = line.split(",")
        if len(p) < 7:
            continue
        ts_raw   = int(p[0])
        open_ts  = ts_raw // 1000 if ts_raw > 1_000_000_000_000_000 else ts_raw
        ct_raw   = int(p[6])
        close_ts = ct_raw // 1000 if ct_raw > 1_000_000_000_000_000 else ct_raw
        result[open_ts] = {"open_ts": open_ts, "close_ts": close_ts, "close": float(p[4])}
    return result


def build_btc_map() -> dict[int, dict]:
    print(f"\n{SEP}")
    print("  FETCHING BTC 4H DATA (Jul–Sep 2026)")
    print(SEP)
    btc_map: dict[int, dict] = {}
    for ym in ["2026-07", "2026-08"]:
        url = (f"https://data.binance.vision/data/spot/monthly/klines/"
               f"BTCUSDT/4h/BTCUSDT-4h-{ym}.zip")
        r = _get(url)
        if r.status_code == 200:
            p = _parse_zip(r.content)
            btc_map.update(p)
            print(f"  {ym}: {len(p)} candles ✓")
        else:
            print(f"  {ym}: HTTP {r.status_code} ✗")

    sep_n = 0
    for day in [f"{d:02d}" for d in range(1, 20)]:
        url = (f"https://data.binance.vision/data/spot/daily/klines/"
               f"BTCUSDT/4h/BTCUSDT-4h-2026-09-{day}.zip")
        r = _get(url)
        if r.status_code == 200:
            p = _parse_zip(r.content)
            btc_map.update(p)
            sep_n += len(p)
    print(f"  Sep 01-18 daily: {sep_n} candles ✓")
    print(f"  Total: {len(btc_map)} candles")
    return btc_map


# ---------------------------------------------------------------------------
# Closed-candle feature computation
# ---------------------------------------------------------------------------

def compute_btc_regime_features(entry_ms: int, btc_map: dict[int, dict]) -> dict:
    """
    Compute BTC regime features using ONLY fully closed 4h candles
    strictly before entry_fill_time.

    Returns:
      btc_4h_change_pct:              float | None
      btc_rv_6candle:                 float | None  (realized vol, 6 candles)
      btc_trend_label:                str | None    (STRONG_UP/MILD_UP/NEUTRAL/MILD_DOWN/STRONG_DOWN)
      btc_vol_label:                  str | None    (VOL_LOW/VOL_MID/VOL_HIGH)
      btc_regime_label:               str | None    (combined, 5-bucket)
      btc_candle_closed_before_entry: True|False|None
      btc_prev_candle_close_ts:       int | None    (audit)
    """
    result = {
        "btc_4h_change_pct":              None,
        "btc_rv_6candle":                 None,
        "btc_trend_label":                None,
        "btc_vol_label":                  None,
        "btc_regime_label":               None,
        "btc_candle_closed_before_entry": None,
        "btc_prev_candle_close_ts":       None,
    }

    if not entry_ms or entry_ms <= 0:
        return result

    ms = int(entry_ms)
    curr_open  = (ms // CANDLE_MS) * CANDLE_MS   # FORMING — never used
    prev_open  = curr_open - CANDLE_MS            # last fully closed candle

    prev_data = btc_map.get(prev_open)
    if prev_data is None:
        return result

    result["btc_prev_candle_close_ts"] = prev_data["close_ts"]

    # Strict closed-candle assertion
    if prev_data["close_ts"] >= ms:
        result["btc_candle_closed_before_entry"] = False
        return result

    result["btc_candle_closed_before_entry"] = True

    # --- 4h momentum: prev vs pprev ---
    pprev_open = curr_open - 2 * CANDLE_MS
    pprev_data = btc_map.get(pprev_open)
    if pprev_data is not None and pprev_data["close"] > 0:
        result["btc_4h_change_pct"] = (
            (prev_data["close"] - pprev_data["close"]) / pprev_data["close"] * 100
        )

    # --- Realized volatility: std of log returns over 6 closed candles ---
    # Need candles at: prev, prev-1, prev-2, prev-3, prev-4, prev-5
    # That is: curr_open - 1*4h, -2*4h, ..., -6*4h
    closes_6 = []
    for k in range(1, 8):   # need 7 closes to get 6 log-returns
        c_open = curr_open - k * CANDLE_MS
        c_data = btc_map.get(c_open)
        if c_data is None:
            break
        closes_6.append(c_data["close"])

    if len(closes_6) >= 7:
        try:
            log_rets = []
            for i in range(len(closes_6) - 1):
                if closes_6[i + 1] <= 0 or closes_6[i] <= 0:
                    log_rets = []
                    break
                log_rets.append(math.log(closes_6[i] / closes_6[i + 1]))
            if len(log_rets) >= 2:
                result["btc_rv_6candle"] = float(np.std(log_rets, ddof=1))
        except (ValueError, ZeroDivisionError):
            pass   # guard against zero/negative closes

    # --- Classify trend label (pre-registered thresholds) ---
    chg = result["btc_4h_change_pct"]
    if chg is not None:
        if chg > BTC_STRONG_UP_THR:
            result["btc_trend_label"] = "STRONG_UP"
        elif chg >= BTC_MILD_UP_THR:
            result["btc_trend_label"] = "MILD_UP"
        elif chg >= BTC_NEUTRAL_LO_THR:
            result["btc_trend_label"] = "NEUTRAL"
        elif chg >= BTC_MILD_DN_THR:
            result["btc_trend_label"] = "MILD_DOWN"
        else:
            result["btc_trend_label"] = "STRONG_DOWN"

    # --- Classify volatility label (pre-registered tercile thresholds) ---
    rv = result["btc_rv_6candle"]
    if rv is not None:
        if rv < RV_LOW_THR:
            result["btc_vol_label"] = "VOL_LOW"
        elif rv <= RV_HIGH_THR:
            result["btc_vol_label"] = "VOL_MID"
        else:
            result["btc_vol_label"] = "VOL_HIGH"

    # --- Combined regime: 5 interpretable buckets ---
    # Collapsed from 5×3=15 to reduce sparsity at N=155.
    # Logic: high-vol distinguishes regimes; neutral collapses.
    trend = result["btc_trend_label"]
    vol   = result["btc_vol_label"]
    if trend is not None and vol is not None:
        if trend in ("STRONG_UP", "MILD_UP") and vol == "VOL_HIGH":
            result["btc_regime_label"] = "BULL_HIGH_VOL"
        elif trend in ("STRONG_UP", "MILD_UP"):
            result["btc_regime_label"] = "BULL_LOW_MID_VOL"
        elif trend in ("STRONG_DOWN", "MILD_DOWN") and vol == "VOL_HIGH":
            result["btc_regime_label"] = "BEAR_HIGH_VOL"
        elif trend in ("STRONG_DOWN", "MILD_DOWN"):
            result["btc_regime_label"] = "BEAR_LOW_MID_VOL"
        else:
            result["btc_regime_label"] = "NEUTRAL_ANY_VOL"

    return result


# ---------------------------------------------------------------------------
# Dataset builder
# ---------------------------------------------------------------------------

def build_dataset(btc_map: dict[int, dict]) -> pd.DataFrame:
    print(f"\n{SEP}")
    print("  STEP 1: BUILD DATASET")
    print(SEP)

    rows = fetch_all_spot()
    df   = pd.DataFrame(rows)
    df   = df[df["exit_status"].isin(["TP_HIT", "SL_HIT"])].copy().reset_index(drop=True)
    # Provenance filter — mirrors ml/train_v1.py & train_v2.py.
    _EXCLUDED = {"PRICE_GUARD_SL","UNPROTECTED_SL_BREACH","UNPROTECTED_TP_BREACH",
                 "OCO_STUCK_MANUAL_RESOLUTION","EMERGENCY_CLOSED",
                 "RECOVERED_SL_HIT","STALE_SETUP_CANCELLED"}
    if "exit_reason" in df.columns:
        df = df[~df["exit_reason"].isin(_EXCLUDED)].copy().reset_index(drop=True)
    df["win"] = (df["exit_status"] == "TP_HIT").astype(int)

    for col in ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Groups
    def _grp(row):
        cid = row.get("correlation_cluster_id")
        return str(cid) if pd.notna(cid) and cid else f"single_{row.name}"
    def _grp_ded(row):
        sym = row.get("symbol") or "unknown"
        try:   pk = round(float(row.get("entry_price") or 0), 2)
        except: pk = 0.0
        return f"{sym}@{pk:.2f}"
    df["_group"]         = df.apply(_grp, axis=1)
    df["_group_deduped"] = df.apply(_grp_ded, axis=1)

    # Sort by entry time
    df["_sort_time"] = pd.to_numeric(df.get("entry_fill_time"), errors="coerce")
    df = df.sort_values("_sort_time").reset_index(drop=True)

    # Enrich with BTC regime features
    regime_rows = []
    for _, row in df.iterrows():
        ms_raw = row.get("entry_fill_time")
        try:    ms = int(float(ms_raw))
        except: ms = 0
        regime_rows.append(compute_btc_regime_features(ms, btc_map))

    reg_df = pd.DataFrame(regime_rows)
    df = pd.concat([df.reset_index(drop=True), reg_df.reset_index(drop=True)], axis=1)

    # Audit
    n_clean     = int((df["btc_candle_closed_before_entry"] == True).sum())
    n_ambig     = int((df["btc_candle_closed_before_entry"] == False).sum())
    n_missing   = int(df["btc_candle_closed_before_entry"].isna().sum())
    n_full      = int(df["btc_regime_label"].notna().sum())

    print(f"\n  Total closed trades:      {len(df)}")
    print(f"  btc_candle_closed=True:   {n_clean}")
    print(f"  btc_candle_closed=False:  {n_ambig}  [excluded for regime]")
    print(f"  btc_candle_closed=None:   {n_missing}  [excluded for regime]")
    print(f"  Rows with full regime:    {n_full}")

    # Bucket sizes — check BEFORE any outcome analysis
    print(f"\n  Regime bucket sizes (N — before outcome analysis):")
    for lbl in ["BULL_HIGH_VOL", "BULL_LOW_MID_VOL", "BEAR_HIGH_VOL",
                "BEAR_LOW_MID_VOL", "NEUTRAL_ANY_VOL"]:
        n = int((df["btc_regime_label"] == lbl).sum())
        flag = "  ⚠ N<15 — interpret with caution" if n < 15 else ""
        print(f"    {lbl:<22}: {n:>4}{flag}")

    # Trend bucket sizes
    print(f"\n  Trend label sizes:")
    for lbl in ["STRONG_UP", "MILD_UP", "NEUTRAL", "MILD_DOWN", "STRONG_DOWN"]:
        n = int((df["btc_trend_label"] == lbl).sum())
        flag = "  ⚠ N<15" if n < 15 else ""
        print(f"    {lbl:<14}: {n:>4}{flag}")

    print(f"\n  Volatility label sizes:")
    for lbl in ["VOL_LOW", "VOL_MID", "VOL_HIGH"]:
        n = int((df["btc_vol_label"] == lbl).sum())
        print(f"    {lbl:<12}: {n:>4}")

    return df


# ---------------------------------------------------------------------------
# Per-bucket descriptive analysis (AFTER dataset is finalized, outcome revealed)
# ---------------------------------------------------------------------------

def descriptive_by_regime(df: pd.DataFrame) -> None:
    print(f"\n{SEP}")
    print("  DESCRIPTIVE: WIN RATE BY BTC REGIME BUCKET")
    print("  (Informational only — not used to tune thresholds)")
    print(SEP)

    base_wr = df["win"].mean()
    print(f"\n  Baseline win rate: {base_wr:.1%}  (N={len(df)})")

    for col, name in [("btc_regime_label", "Combined regime"),
                      ("btc_trend_label",  "Trend only"),
                      ("btc_vol_label",    "Volatility only")]:
        print(f"\n  {name}:")
        print(f"  {'Bucket':<22} {'N':>4} {'Win rate':>9} {'Avg PnL%':>9} {'Vs base':>9}")
        print(f"  {'─'*58}")
        for bucket in sorted(df[col].dropna().unique()):
            sub = df[df[col] == bucket]
            wr  = sub["win"].mean()
            pnl_col = "realized_pnl_pct"
            avg_pnl = pd.to_numeric(sub.get(pnl_col, pd.Series()), errors="coerce").mean()
            flag = "  ⚠ N<15" if len(sub) < 15 else ""
            pnl_str = f"{avg_pnl:>+9.2f}" if not pd.isna(avg_pnl) else "       —"
            print(f"  {bucket:<22} {len(sub):>4}  {wr:>9.1%} {pnl_str}  {wr-base_wr:>+8.1%}{flag}")


# ---------------------------------------------------------------------------
# Pipeline + evaluation (same protocol as corrected script)
# ---------------------------------------------------------------------------

def make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            max_iter=1000, solver="lbfgs", C=1.0,
            class_weight="balanced", random_state=42,
        )),
    ])


def build_X(df: pd.DataFrame, feature_set: str) -> pd.DataFrame:
    """
    feature_set options:
      'baseline'       — zone_touches, planned_rr, risk_pct, atr_pct_at_entry, zone_type
      'momentum_only'  — baseline + btc_4h_change_pct (corrected)
      'regime_label'   — baseline + btc_regime_label (one-hot)
      'trend_label'    — baseline + btc_trend_label (one-hot)
      'full_regime'    — baseline + btc_4h_change_pct + btc_rv_6candle + btc_regime_label
    """
    num_base = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    cat_dummies = pd.get_dummies(
        df["zone_type"].fillna("T1").astype(str),
        prefix="zone_type", drop_first=True,
    )
    parts = [df[num_base].apply(pd.to_numeric, errors="coerce").fillna(0.0), cat_dummies]

    if feature_set in ("momentum_only", "full_regime"):
        chg = pd.to_numeric(df.get("btc_4h_change_pct", pd.Series(0.0, index=df.index)),
                             errors="coerce").fillna(0.0)
        parts.append(chg.rename("btc_4h_change_pct").to_frame())

    if feature_set == "full_regime":
        rv = pd.to_numeric(df.get("btc_rv_6candle", pd.Series(0.0, index=df.index)),
                            errors="coerce").fillna(0.0)
        parts.append(rv.rename("btc_rv_6candle").to_frame())

    if feature_set in ("regime_label", "full_regime"):
        dummies = pd.get_dummies(
            df["btc_regime_label"].fillna("UNKNOWN").astype(str),
            prefix="regime", drop_first=True,
        )
        parts.append(dummies)

    if feature_set == "trend_label":
        dummies = pd.get_dummies(
            df["btc_trend_label"].fillna("UNKNOWN").astype(str),
            prefix="trend", drop_first=True,
        )
        parts.append(dummies)

    return pd.concat(parts, axis=1)


def evaluate_loco(X, y, groups, label: str) -> dict:
    loco = LeaveOneGroupOut()
    n_splits = loco.get_n_splits(X, y, groups)
    if n_splits < 3:
        return {"auc": np.nan, "n_splits": n_splits}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oof = cross_val_predict(
            make_pipe(), X, y, cv=loco, groups=groups,
            method="predict_proba", n_jobs=1,
        )[:, 1]
    try:
        auc = roc_auc_score(y, oof)
    except Exception:
        auc = np.nan
    return {"auc": float(auc), "n_splits": int(n_splits)}


def evaluate_time(X, y, label="",
                  init_frac=0.70, step_frac=0.10,
                  min_train=10, min_test=1) -> dict:
    n      = len(X)
    init_n = max(min_train, int(n * init_frac))
    step_n = max(min_test,  int(n * step_frac))
    all_yt, all_yp, fold_aucs = [], [], []
    cursor = init_n

    while cursor < n:
        te  = min(cursor + step_n, n)
        Xtr, ytr = X.iloc[:cursor], y.iloc[:cursor]
        Xte, yte = X.iloc[cursor:te], y.iloc[cursor:te]
        if len(ytr) < min_train or len(yte) < min_test or ytr.nunique() < 2:
            cursor += step_n; continue
        p = make_pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p.fit(Xtr, ytr)
            probs = p.predict_proba(Xte)[:, 1]
        all_yt.extend(yte.tolist())
        all_yp.extend(probs.tolist())
        if yte.nunique() > 1:
            fold_aucs.append(float(roc_auc_score(yte, probs)))
        cursor += step_n

    if not all_yt or len(np.unique(all_yt)) < 2:
        return {"auc": np.nan, "fold_std": np.nan, "n_folds": len(fold_aucs)}

    agg_auc  = float(roc_auc_score(all_yt, all_yp))
    fold_std = float(np.std(fold_aucs)) if fold_aucs else np.nan
    return {
        "auc": agg_auc, "fold_std": fold_std,
        "n_folds": len(fold_aucs), "fold_aucs": fold_aucs,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{SEP}")
    print("  PART 2: BTC REGIME FEATURE — SPOT ML")
    print("  Research only — no production changes, no model saved")
    print(SEP)

    print(f"\n  PRE-REGISTERED THRESHOLDS (fixed before outcome analysis):")
    print(f"    Trend std (σ): {BTC_TREND_STD:.4f}%")
    print(f"    STRONG_UP:   btc_4h_chg > +{BTC_STRONG_UP_THR:.4f}%")
    print(f"    MILD_UP:     btc_4h_chg in [+{BTC_MILD_UP_THR:.4f}%, +{BTC_STRONG_UP_THR:.4f}%)")
    print(f"    NEUTRAL:     btc_4h_chg in [{BTC_NEUTRAL_LO_THR:.4f}%, +{BTC_MILD_UP_THR:.4f}%)")
    print(f"    MILD_DOWN:   btc_4h_chg in [{BTC_MILD_DN_THR:.4f}%, {BTC_NEUTRAL_LO_THR:.4f}%)")
    print(f"    STRONG_DOWN: btc_4h_chg < {BTC_MILD_DN_THR:.4f}%")
    print(f"    VOL_LOW:  rv_6c < {RV_LOW_THR:.6f}")
    print(f"    VOL_MID:  rv_6c in [{RV_LOW_THR:.6f}, {RV_HIGH_THR:.6f}]")
    print(f"    VOL_HIGH: rv_6c > {RV_HIGH_THR:.6f}")

    # ── Fetch BTC data ────────────────────────────────────────────────────
    btc_map = build_btc_map()

    # ── Build dataset ─────────────────────────────────────────────────────
    df = build_dataset(btc_map)

    # Restrict to rows with full regime data for regime experiments
    df_full = df[df["btc_regime_label"].notna()].copy().reset_index(drop=True)

    y_all   = df["win"]
    y_full  = df_full["win"]

    def _groups(d):
        return d["_group"].fillna(pd.Series(
            {i: f"single_{i}" for i in d.index}, dtype=str)).astype(str)
    def _groups_ded(d):
        return d["_group_deduped"].fillna("unknown").astype(str)

    # ── Descriptive per-bucket analysis ───────────────────────────────────
    descriptive_by_regime(df_full)

    # ── Validation ────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  STEP 2: LOCO + TIME-BASED EVALUATION")
    print("  Comparing: baseline vs corrected momentum vs regime")
    print(SEP)

    experiments = [
        ("baseline_v2",      df,      "baseline"),
        ("momentum_only",    df_full, "momentum_only"),   # corrected, closed-candle
        ("regime_label",     df_full, "regime_label"),    # combined 5-bucket
        ("trend_label",      df_full, "trend_label"),     # trend-only 5-bucket
        ("full_regime",      df_full, "full_regime"),     # momentum + rv + regime
    ]

    results = {}
    for name, df_use, fs in experiments:
        y      = df_use["win"]
        groups = _groups(df_use)
        grp_d  = _groups_ded(df_use)
        X      = build_X(df_use, fs)

        loco     = evaluate_loco(X, y, groups, name)
        loco_ded = evaluate_loco(X, y, grp_d,  name + "-ded")
        time_r   = evaluate_time(X, y, name)

        spread = abs(loco["auc"] - time_r["auc"]) if not (np.isnan(loco["auc"]) or np.isnan(time_r["auc"])) else np.nan
        stable = spread <= 0.15 if not np.isnan(spread) else False

        results[name] = {
            "n":          len(df_use),
            "eff_n":      int(groups.nunique()),
            "loco_auc":   loco["auc"],
            "loco_ded":   loco_ded["auc"],
            "time_auc":   time_r["auc"],
            "fold_std":   time_r.get("fold_std", np.nan),
            "fold_aucs":  time_r.get("fold_aucs", []),
            "spread":     spread,
            "stable":     stable,
        }

        print(f"\n  [{name}]  N={len(df_use)}, eff_N={int(groups.nunique())}")
        print(f"    LOCO AUC:    {loco['auc']:.4f}  (deduped: {loco_ded['auc']:.4f})")
        print(f"    Time AUC:    {time_r['auc']:.4f}  (fold std: {time_r.get('fold_std', np.nan):.4f})")
        print(f"    AUC spread:  {spread:.4f}  {'✓ stable' if stable else '✗ UNSTABLE'}")
        if time_r.get("fold_aucs"):
            print(f"    Per-fold:    {[round(a,3) for a in time_r['fold_aucs']]}")

    # ── Comparison table ──────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  COMPARISON TABLE: baseline vs corrected momentum vs regime")
    print(SEP)
    print(f"\n  {'Experiment':<22} {'N':>4} {'LOCO':>7} {'Ded':>7} {'Time':>7} {'Spread':>8} {'Stable':>8} {'FoldStd':>8}")
    print(f"  {'─'*76}")

    for name, r in results.items():
        stable_str = "✓" if r["stable"] else "✗"
        print(f"  {name:<22} {r['n']:>4} {r['loco_auc']:>7.4f} {r['loco_ded']:>7.4f} "
              f"{r['time_auc']:>7.4f} {r['spread']:>8.4f} {stable_str:>8} "
              f"{r['fold_std']:>8.4f}")

    print(f"\n  Reference for comparison (from train_v3_c_btc_corrected.py):")
    print(f"    corrected single-momentum: LOCO=0.4853, Time=0.6230, spread=0.1377")
    print(f"    original LEAKY (suspect):  LOCO=0.6190, Time=0.7016, spread=0.0826")

    # ── Final verdict ─────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  PART 2 VERDICT")
    print(SEP)

    print(f"\n  Stability threshold:  AUC spread <= 0.15")
    print(f"  Signal threshold:     LOCO AUC > 0.52")
    print()

    for name, r in results.items():
        if name == "baseline_v2":
            continue
        loco_ok  = r["loco_auc"] > 0.52
        time_ok  = r["time_auc"] > 0.52
        stable   = r["stable"]
        improves = (r["loco_auc"] > results["momentum_only"]["loco_auc"] + 0.02 or
                    r["time_auc"] > results["momentum_only"]["time_auc"] + 0.02) \
                   if name != "momentum_only" else None

        if stable and loco_ok and time_ok:
            v = "PROMISING (meets stability + signal bars)"
        elif stable and (loco_ok or time_ok):
            v = "MARGINAL (stable but only one AUC > threshold)"
        elif not stable:
            v = "UNSTABLE (spread > 0.15)"
        else:
            v = "NO_SIGNAL (both AUCs below threshold)"

        better_str = ""
        if improves is not None:
            better_str = "  OUTPERFORMS momentum" if improves else "  does NOT outperform momentum"

        print(f"  {name:<22}: {v}{better_str}")

    print(f"\n  Production behavior changed: NO")
    print(f"  No model saved. No Supabase writes.")


if __name__ == "__main__":
    main()
