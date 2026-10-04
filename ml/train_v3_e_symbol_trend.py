"""
train_v3_e_symbol_trend.py
===========================
Pre-registered hypothesis test — Own-Symbol Trend (single candidate).

Pre-registered specification:
  C_SYM1: baseline_v2                                (mandatory baseline)
  C_SYM2: baseline_v2 + own_symbol_1d_return          (primary candidate)

own_symbol_1d_return = traded coin's own price return over 1d/24h
  (6 × 4h candles), using the SAME closed-candle discipline as every
  BTC-context script: prev_candle.close_ts < entry_fill_time (strict <).

Audit column: symbol_candle_closed_before_entry

This is a SINGLE pre-registered test. One primary candidate against one
mandatory baseline. No multiple-lookback search in this run.

Signal threshold: LOCO > 0.52 AND Time > 0.52 (dual-gate, both must align).
Stability threshold: |LOCO-Time| <= 0.15.
(Threshold resolution: 0.52 is the project standard from N=120 report.)
"""

import io
import time
import warnings
import zipfile
from collections import defaultdict

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import LeaveOneGroupOut, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.supabase_client import fetch_all_spot

SEP       = "=" * 72
CANDLE_MS = 4 * 3600 * 1000  # 4h in milliseconds
LOOKBACK  = 6                 # 6 × 4h = 24h (1d)
HEADERS   = {"User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36"}

_EXCLUDED = {
    "PRICE_GUARD_SL", "UNPROTECTED_SL_BREACH", "UNPROTECTED_TP_BREACH",
    "OCO_STUCK_MANUAL_RESOLUTION", "EMERGENCY_CLOSED",
    "RECOVERED_SL_HIT", "STALE_SETUP_CANCELLED",
}

# ---------------------------------------------------------------------------
# Kline fetcher — per symbol, 4h resolution
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 20) -> requests.Response:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return requests.get(url, headers=HEADERS, timeout=timeout, verify=False)


def _parse_kline_zip(content: bytes) -> dict:
    """Parse Binance kline zip → {open_ts_ms: {open_ts, close_ts, close}}"""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    result = {}
    for line in raw.strip().split("\n"):
        if not line or line.startswith("open"):
            continue
        p = line.split(",")
        if len(p) < 7:
            continue
        ts_raw = int(p[0])
        open_ts  = ts_raw // 1000 if ts_raw > 1_000_000_000_000_000 else ts_raw
        ct_raw   = int(p[6])
        close_ts = ct_raw // 1000 if ct_raw > 1_000_000_000_000_000 else ct_raw
        result[open_ts] = {
            "open_ts":  open_ts,
            "close_ts": close_ts,
            "close":    float(p[4]),
        }
    return result


def build_symbol_map(symbol: str) -> dict:
    """
    Fetch 4h klines for *symbol* covering Jul-Sep 2026.
    Returns {open_ts_ms: {open_ts, close_ts, close}} or {} on failure.
    """
    full_map = {}
    for ym in ["2026-07", "2026-08"]:
        url = (f"https://data.binance.vision/data/spot/monthly/klines/"
               f"{symbol}/4h/{symbol}-4h-{ym}.zip")
        r = _get(url)
        if r.status_code == 200:
            full_map.update(_parse_kline_zip(r.content))
        elif r.status_code == 404:
            return {}   # symbol not on Binance — skip silently
    # Sep 2026 daily
    for day in [f"{d:02d}" for d in range(1, 20)]:
        url = (f"https://data.binance.vision/data/spot/daily/klines/"
               f"{symbol}/4h/{symbol}-4h-2026-09-{day}.zip")
        r = _get(url)
        if r.status_code == 200:
            full_map.update(_parse_kline_zip(r.content))
    return full_map


# ---------------------------------------------------------------------------
# Feature computation — closed-candle discipline, same as BTC scripts
# ---------------------------------------------------------------------------

def compute_symbol_return(entry_ms: int, sym_map: dict, lookback: int = 6) -> dict:
    """
    Own-symbol return over `lookback` × 4h candles (default 6 = 1d).

    Protocol:
      current_open = floor(entry_ms / CANDLE_MS) * CANDLE_MS  — FORMING, never used
      prev_open    = current_open - CANDLE_MS                  — last CLOSED candle
      start_open   = prev_open - lookback * CANDLE_MS           — start of window

    Assertion: prev_candle.close_ts < entry_ms (strict <)
    Also:      start_candle.close_ts < entry_ms (oldest also closed)

    Return: (prev_candle.close - start_candle.close) / start_candle.close * 100
    """
    result = {
        "feature_value":                       None,
        "symbol_candle_closed_before_entry":   None,
    }
    if not entry_ms or entry_ms <= 0 or not sym_map:
        return result

    ms           = int(entry_ms)
    current_open = (ms // CANDLE_MS) * CANDLE_MS   # FORMING — never used
    prev_open    = current_open - CANDLE_MS         # last CLOSED candle
    start_open   = prev_open - lookback * CANDLE_MS

    prev_data  = sym_map.get(prev_open)
    start_data = sym_map.get(start_open)

    if prev_data is None:
        return result

    # Strict assertion: prev candle must be fully closed before entry
    if prev_data["close_ts"] >= ms:
        result["symbol_candle_closed_before_entry"] = False
        return result

    if start_data is None:
        result["symbol_candle_closed_before_entry"] = True  # prev OK, start missing
        return result

    # Also assert oldest candle is closed before entry
    if start_data["close_ts"] >= ms:
        result["symbol_candle_closed_before_entry"] = False
        return result

    start_close = start_data["close"]
    prev_close  = prev_data["close"]

    if start_close <= 0:
        return result

    result["symbol_candle_closed_before_entry"] = True
    result["feature_value"] = (prev_close - start_close) / start_close * 100
    return result


# ---------------------------------------------------------------------------
# Pipeline + evaluation (reused from BTC scripts)
# ---------------------------------------------------------------------------

def make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            max_iter=1000, solver="lbfgs", C=1.0,
            class_weight="balanced", random_state=42,
        )),
    ])


def build_X(df: pd.DataFrame, extra_cols: list[str]) -> pd.DataFrame:
    base = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    cols = base + extra_cols
    cat  = pd.get_dummies(
        df["zone_type"].fillna("T1").astype(str), prefix="zone_type", drop_first=True
    )
    X = pd.concat([
        df[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0), cat
    ], axis=1)
    return X


def evaluate_loco(X, y, groups, label: str = "") -> dict:
    loco      = LeaveOneGroupOut()
    n_splits  = loco.get_n_splits(X, y, groups)
    if n_splits < 3:
        return {"auc": np.nan, "n_splits": n_splits, "per_fold": []}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oof = cross_val_predict(
            make_pipe(), X, y, cv=loco, groups=groups,
            method="predict_proba", n_jobs=1,
        )[:, 1]
    auc = roc_auc_score(y, oof)
    fold_aucs = []
    for tr_idx, te_idx in loco.split(X, y, groups):
        yte = y.iloc[te_idx]
        if yte.nunique() > 1:
            fold_aucs.append(round(float(roc_auc_score(yte, oof[te_idx])), 3))
    return {"auc": float(auc), "n_splits": int(n_splits), "per_fold": fold_aucs}


def evaluate_time(X, y, init_frac=0.70, step_frac=0.10,
                  min_train=10, min_test=1) -> dict:
    n      = len(X)
    init_n = max(min_train, int(n * init_frac))
    step_n = max(min_test,  int(n * step_frac))
    all_yt, all_yp, fold_aucs = [], [], []
    cursor = init_n
    while cursor < n:
        te   = min(cursor + step_n, n)
        Xtr, ytr = X.iloc[:cursor], y.iloc[:cursor]
        Xte, yte = X.iloc[cursor:te],  y.iloc[cursor:te]
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
            fold_aucs.append(round(float(roc_auc_score(yte, probs)), 3))
        cursor += step_n
    if len(set(all_yt)) < 2:
        return {"auc": np.nan, "fold_std": np.nan, "per_fold": []}
    return {
        "auc":      float(roc_auc_score(all_yt, all_yp)),
        "fold_std": float(np.std(fold_aucs)) if fold_aucs else np.nan,
        "per_fold": fold_aucs,
    }


def verdict(loco_auc: float, time_auc: float, spread: float) -> str:
    if np.isnan(loco_auc) or np.isnan(time_auc):
        return "INSUFFICIENT_EVIDENCE"
    both_above = loco_auc > 0.52 and time_auc > 0.52
    stable     = spread <= 0.15
    if both_above and stable:
        return "SUPPORTED"
    if (loco_auc > 0.52 or time_auc > 0.52) and stable:
        return "MARGINAL (one gate)"
    if (loco_auc > 0.52 or time_auc > 0.52) and not stable:
        return "UNSTABLE"
    return "NO_SIGNAL"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{SEP}")
    print("  OWN-SYMBOL TREND — C_SYM1 (baseline) vs C_SYM2 (baseline + 1d return)")
    print(f"  Signal threshold: LOCO > 0.52 AND Time > 0.52 (dual-gate)")
    print(f"  Stability threshold: |LOCO-Time| <= 0.15")
    print(f"  Lookback: {LOOKBACK} × 4h = {LOOKBACK*4}h (1d)")
    print(f"{SEP}")

    # ── Load and filter base dataset ──────────────────────────────────────
    rows = fetch_all_spot()
    df   = pd.DataFrame(rows)
    df   = df[df["exit_status"].isin(["TP_HIT", "SL_HIT"])].copy().reset_index(drop=True)
    if "exit_reason" in df.columns:
        df = df[~df["exit_reason"].isin(_EXCLUDED)].copy().reset_index(drop=True)
    df["win"] = (df["exit_status"] == "TP_HIT").astype(int)

    def _grp(row):
        cid = row.get("correlation_cluster_id")
        return cid if cid else f"single_{row.name}"
    df["_group"] = df.apply(_grp, axis=1)

    def _grp_ded(row):
        sym = row.get("symbol") or "unknown"
        try:   pk = round(float(row.get("entry_price") or 0), 2)
        except: pk = 0.0
        return f"{sym}@{pk:.2f}"
    df["_group_deduped"] = df.apply(_grp_ded, axis=1)

    sort_ms = pd.to_numeric(df.get("entry_fill_time"), errors="coerce")
    df["_sort_time"] = sort_ms
    df = df.sort_values("_sort_time").reset_index(drop=True)
    for col in ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"\n  Base dataset: {len(df)} rows, eff_N={df['_group'].nunique()}")

    # ── Fetch per-symbol kline maps ────────────────────────────────────────
    print(f"\n{SEP}\n  FETCHING PER-SYMBOL 4H KLINES\n{SEP}")
    unique_syms = df["symbol"].dropna().unique().tolist()
    sym_maps: dict[str, dict] = {}
    n_ok = n_miss = 0
    for sym in sorted(unique_syms):
        m = build_symbol_map(sym)
        sym_maps[sym] = m
        if m:
            n_ok += 1
        else:
            n_miss += 1
            print(f"  {sym}: NOT FOUND on Binance (testnet-only?) — will be excluded")
        time.sleep(0.05)  # polite rate limiting
    print(f"\n  Symbols fetched: {n_ok} ok, {n_miss} missing")

    # ── Compute own-symbol feature per row ────────────────────────────────
    print(f"\n{SEP}\n  COMPUTING OWN-SYMBOL TREND FEATURE\n{SEP}")
    feat_vals = []
    closed_flags = []
    n_clean = n_ambiguous = n_missing_data = 0

    for _, row in df.iterrows():
        sym    = row.get("symbol")
        ms_raw = row.get("entry_fill_time")
        try:    ms = int(float(ms_raw))
        except: ms = 0
        sym_map = sym_maps.get(sym, {})
        r = compute_symbol_return(ms, sym_map, LOOKBACK)
        feat_vals.append(r["feature_value"])
        closed_flags.append(r["symbol_candle_closed_before_entry"])
        if r["symbol_candle_closed_before_entry"] is True and r["feature_value"] is not None:
            n_clean += 1
        elif r["symbol_candle_closed_before_entry"] is False:
            n_ambiguous += 1
        else:
            n_missing_data += 1

    df["own_symbol_1d_return"]             = pd.to_numeric(feat_vals, errors="coerce")
    df["symbol_candle_closed_before_entry"] = closed_flags

    print(f"  Clean rows (closed before entry): {n_clean}")
    print(f"  Ambiguous (close_ts >= entry):    {n_ambiguous}  [EXCLUDED — protocol violation]")
    print(f"  Missing data:                     {n_missing_data}  [excluded]")
    assert n_ambiguous == 0, f"PROTOCOL VIOLATION: {n_ambiguous} ambiguous rows detected!"
    print(f"  ✅ Closed-candle assertion: PASSED (0 ambiguous rows)")

    # ── Collinearity check ─────────────────────────────────────────────────
    print(f"\n{SEP}\n  COLLINEARITY CHECK\n{SEP}")
    df_clean = df[
        (df["symbol_candle_closed_before_entry"] == True) &
        df["own_symbol_1d_return"].notna()
    ].copy().reset_index(drop=True)

    for base_feat in ["atr_pct_at_entry", "planned_rr", "risk_pct", "zone_touches"]:
        col = pd.to_numeric(df_clean.get(base_feat), errors="coerce").fillna(0)
        feat_col = df_clean["own_symbol_1d_return"]
        if col.std() > 0 and feat_col.std() > 0:
            corr = col.corr(feat_col)
            flag = " ⚠ HIGH" if abs(corr) > 0.5 else ""
            print(f"  corr(own_symbol_1d_return, {base_feat:<20}) = {corr:+.3f}{flag}")
        else:
            print(f"  corr(own_symbol_1d_return, {base_feat:<20}) = n/a (zero variance)")

    # ── Evaluate C_SYM1: baseline_v2 (full dataset, no BTC filter) ────────
    print(f"\n{SEP}\n  C_SYM1: baseline_v2\n{SEP}")
    df_base_full = df.copy()
    y_base  = df_base_full["win"]
    grp_b   = df_base_full["_group"].fillna(pd.Series(range(len(df_base_full)), index=df_base_full.index).astype(str)).astype(str)
    grp_b_d = df_base_full["_group_deduped"].fillna(pd.Series([f"unk_{i}" for i in range(len(df_base_full))], index=df_base_full.index)).astype(str)
    X_base  = build_X(df_base_full, [])

    loco_b = evaluate_loco(X_base, y_base, grp_b, "baseline_v2")
    time_b = evaluate_time(X_base, y_base)
    spread_b = abs(loco_b["auc"] - time_b["auc"])
    verd_b   = verdict(loco_b["auc"], time_b["auc"], spread_b)

    print(f"  N={len(df_base_full)}  eff_N={grp_b.nunique()}")
    print(f"  LOCO AUC:  {loco_b['auc']:.4f}  (folds: {loco_b['per_fold'][:6]})")
    print(f"  Time AUC:  {time_b['auc']:.4f}  fold_std={time_b['fold_std']:.4f}  (folds: {time_b['per_fold']})")
    print(f"  Spread:    {spread_b:.4f}  ({'✓ stable' if spread_b<=0.15 else '✗ unstable'})")
    print(f"  Verdict:   {verd_b}")

    # ── Evaluate C_SYM2: baseline_v2 + own_symbol_1d_return ───────────────
    print(f"\n{SEP}\n  C_SYM2: baseline_v2 + own_symbol_1d_return\n{SEP}")

    df_sym = df_clean.copy()
    y_sym   = df_sym["win"]
    grp_s   = df_sym["_group"].fillna(pd.Series(range(len(df_sym)), index=df_sym.index).astype(str)).astype(str)
    grp_s_d = df_sym["_group_deduped"].fillna(pd.Series([f"unk_{i}" for i in range(len(df_sym))], index=df_sym.index)).astype(str)
    X_sym   = build_X(df_sym, ["own_symbol_1d_return"])

    loco_s     = evaluate_loco(X_sym, y_sym, grp_s,   "C_SYM2")
    loco_s_ded = evaluate_loco(X_sym, y_sym, grp_s_d, "C_SYM2-ded")
    time_s     = evaluate_time(X_sym, y_sym)
    spread_s   = abs(loco_s["auc"] - time_s["auc"])
    verd_s     = verdict(loco_s["auc"], time_s["auc"], spread_s)

    print(f"  N={len(df_sym)}  eff_N={grp_s.nunique()}  ded_N={grp_s_d.nunique()}")
    print(f"  LOCO AUC:        {loco_s['auc']:.4f}  (folds: {loco_s['per_fold'][:6]})")
    print(f"  Deduped LOCO:    {loco_s_ded['auc']:.4f}")
    print(f"  Time AUC:        {time_s['auc']:.4f}  fold_std={time_s['fold_std']:.4f}  (folds: {time_s['per_fold']})")
    print(f"  Spread:          {spread_s:.4f}  ({'✓ stable' if spread_s<=0.15 else '✗ unstable'})")
    print(f"  Verdict:         {verd_s}")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{SEP}\n  SUMMARY TABLE\n{SEP}")
    print(f"  {'Candidate':<40} {'N':>5} {'eff_N':>6} {'LOCO':>7} {'Ded':>7} {'Time':>7} {'Spread':>7} {'Stable':>7}")
    print(f"  {'─'*85}")
    print(f"  {'C_SYM1  baseline_v2':<40} {len(df_base_full):>5} {grp_b.nunique():>6} {loco_b['auc']:>7.4f} {'—':>7} {time_b['auc']:>7.4f} {spread_b:>7.4f} {'✓' if spread_b<=0.15 else '✗':>7}")
    print(f"  {'C_SYM2  +own_symbol_1d_return':<40} {len(df_sym):>5} {grp_s.nunique():>6} {loco_s['auc']:>7.4f} {loco_s_ded['auc']:>7.4f} {time_s['auc']:>7.4f} {spread_s:>7.4f} {'✓' if spread_s<=0.15 else '✗':>7}")

    print(f"\n  C_SYM1 verdict: {verd_b}")
    print(f"  C_SYM2 verdict: {verd_s}")

    # ── Recommendation ────────────────────────────────────────────────────
    print(f"\n{SEP}\n  RECOMMENDATION\n{SEP}")
    if verd_s == "SUPPORTED":
        print(f"  ⚠ SUPPORTED — but this is a first-time test on this feature family.")
        print(f"  Requires independent replication at N=150 before being trusted.")
        print(f"  Do NOT promote any model based on this result alone.")
    elif "MARGINAL" in verd_s:
        delta = loco_s["auc"] - loco_b["auc"]
        print(f"  MARGINAL result (LOCO delta vs baseline: {delta:+.4f}).")
        if abs(delta) < 0.02:
            print(f"  Delta is negligible — own-symbol 1d return adds no information.")
            print(f"  Recommendation: move to next feature family (market breadth).")
        else:
            print(f"  Delta is non-trivial but only one gate cleared.")
            print(f"  Recommendation: consider a single follow-up test at N=150 if the")
            print(f"  LOCO result is directionally consistent with the unstable gate.")
    else:
        delta = loco_s["auc"] - loco_b["auc"]
        print(f"  NO_SIGNAL (LOCO delta vs baseline: {delta:+.4f}).")
        print(f"  Own-symbol 1d return does not improve on baseline_v2.")
        print(f"  Recommendation: move to next feature family (market breadth).")

    print(f"\n  Production behavior changed: NO")
    print(f"  No model saved. No Supabase writes.")


if __name__ == "__main__":
    main()
