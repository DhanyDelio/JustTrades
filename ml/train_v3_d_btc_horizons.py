"""
train_v3_d_btc_horizons.py
===========================
Pre-registered, multiple-testing-aware systematic exploration of
BTC-context feature variants at N=120.

Pre-registered variants (FIXED before any results seen):
  V1: BTC return, 4h lookback   — already tested, included from prior run
  V2: BTC return, 1d (24h) lookback  — 6 candles back
  V3: BTC return, 3d (72h) lookback  — 18 candles back
  V4: BTC return, 7d (168h) lookback — 42 candles back
  V5: BTC realized volatility, 1d    — std of 6 most recent closed returns
  V6: Combined V2 + V5               — only if both V2 and V5 LOCO > 0.48

Methodology:
  - All variants use the verified closed-candle protocol:
    prev_candle.close_ts < entry_fill_time (strict <)
  - Audit column btc_candle_closed_before_entry present per row
  - Provenance exclusion filter applied (same as train_v1.py/v2.py)
  - Threshold: LOCO > 0.52 AND Time > 0.52 (dual-gate, both must align)
  - ALL variants reported, no cherry-picking

Signal threshold: LOCO AUC > 0.52 (consistent with N=120 report)
Stability threshold: AUC spread |LOCO-Time| <= 0.15
"""

import io
import warnings
import zipfile

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import LeaveOneGroupOut, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.supabase_client import fetch_all_spot

SEP       = "=" * 72
CANDLE_MS = 4 * 3600 * 1000
HEADERS   = {"User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36"}

_EXCLUDED = {
    "PRICE_GUARD_SL", "UNPROTECTED_SL_BREACH", "UNPROTECTED_TP_BREACH",
    "OCO_STUCK_MANUAL_RESOLUTION", "EMERGENCY_CLOSED",
    "RECOVERED_SL_HIT", "STALE_SETUP_CANCELLED",
}

# ---------------------------------------------------------------------------
# BTC data fetcher — reused from train_v3_c_btc_corrected.py
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 20) -> requests.Response:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return requests.get(url, headers=HEADERS, timeout=timeout, verify=False)


def _parse_zip_with_close_ts(content: bytes) -> dict:
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
        open_ts = ts_raw // 1000 if ts_raw > 1_000_000_000_000_000 else ts_raw
        ct_raw  = int(p[6])
        close_ts = ct_raw // 1000 if ct_raw > 1_000_000_000_000_000 else ct_raw
        result[open_ts] = {
            "open_ts":  open_ts,
            "close_ts": close_ts,
            "close":    float(p[4]),
        }
    return result


def build_btc_map_full() -> dict:
    """Returns {open_ts: {open_ts, close_ts, close}}"""
    print(f"\n{SEP}\n  FETCHING BTC 4H DATA\n{SEP}")
    full_map = {}
    for ym in ["2026-07", "2026-08"]:
        url = (f"https://data.binance.vision/data/spot/monthly/klines/"
               f"BTCUSDT/4h/BTCUSDT-4h-{ym}.zip")
        r = _get(url)
        if r.status_code == 200:
            parsed = _parse_zip_with_close_ts(r.content)
            full_map.update(parsed)
            print(f"  {ym} monthly: {len(parsed)} candles ✓")
        else:
            print(f"  {ym} monthly: HTTP {r.status_code} ✗")
    sep_loaded = 0
    for day in [f"{d:02d}" for d in range(1, 20)]:
        url = (f"https://data.binance.vision/data/spot/daily/klines/"
               f"BTCUSDT/4h/BTCUSDT-4h-2026-09-{day}.zip")
        r = _get(url)
        if r.status_code == 200:
            parsed = _parse_zip_with_close_ts(r.content)
            full_map.update(parsed)
            sep_loaded += len(parsed)
    print(f"  Sep 2026 daily (01-18): {sep_loaded} candles ✓")
    print(f"  Total: {len(full_map)} candles")
    return full_map


# ---------------------------------------------------------------------------
# Feature computation — horizon variants with closed-candle assertion
# ---------------------------------------------------------------------------

def compute_direction_feature(entry_ms: int, full_map: dict,
                               lookback_candles: int) -> dict:
    """
    BTC return over `lookback_candles` 4h-candles, strict closed-candle.

    Window: [prev_open - lookback_candles*CANDLE_MS, prev_open]
      prev_candle  = last closed candle before entry (close_ts < entry_ms)
      start_candle = lookback_candles steps earlier

    Assertion: prev_candle.close_ts < entry_ms  (same as corrected protocol)
    Also assert: start_candle.close_ts < entry_ms  (oldest candle also closed)
    """
    result = {
        "feature_value":                  None,
        "btc_candle_closed_before_entry": None,
    }
    if not entry_ms or entry_ms <= 0:
        return result

    ms = int(entry_ms)
    current_open = (ms // CANDLE_MS) * CANDLE_MS  # FORMING — never used
    prev_open    = current_open - CANDLE_MS        # last CLOSED candle

    prev_data = full_map.get(prev_open)
    if prev_data is None:
        return result

    # Strict closed-candle assertion
    if prev_data["close_ts"] >= ms:
        result["btc_candle_closed_before_entry"] = False
        return result

    # Find the start candle (lookback_candles steps before prev_open)
    start_open = prev_open - lookback_candles * CANDLE_MS
    start_data = full_map.get(start_open)
    if start_data is None:
        result["btc_candle_closed_before_entry"] = True  # prev is valid
        return result

    # Also assert the oldest candle is closed before entry
    if start_data["close_ts"] >= ms:
        result["btc_candle_closed_before_entry"] = False
        return result

    result["btc_candle_closed_before_entry"] = True
    prev_close  = prev_data["close"]
    start_close = start_data["close"]
    if start_close <= 0:
        return result

    result["feature_value"] = (prev_close - start_close) / start_close * 100
    return result


def compute_volatility_feature(entry_ms: int, full_map: dict,
                                lookback_candles: int = 6) -> dict:
    """
    BTC realized volatility = std of per-candle returns over lookback_candles.
    Uses the lookback_candles most recent CLOSED candles before entry.

    Returns: std(r_t) where r_t = close_t/close_{t-1} - 1
    Needs lookback_candles + 1 candles (for the returns).
    All candles must have close_ts < entry_ms.
    """
    result = {
        "feature_value":                  None,
        "btc_candle_closed_before_entry": None,
    }
    if not entry_ms or entry_ms <= 0:
        return result

    ms = int(entry_ms)
    current_open = (ms // CANDLE_MS) * CANDLE_MS
    prev_open    = current_open - CANDLE_MS

    prev_data = full_map.get(prev_open)
    if prev_data is None:
        return result

    if prev_data["close_ts"] >= ms:
        result["btc_candle_closed_before_entry"] = False
        return result

    # Collect lookback_candles + 1 closed candles
    closes = []
    for i in range(lookback_candles + 1):
        candle_open = prev_open - i * CANDLE_MS
        candle = full_map.get(candle_open)
        if candle is None or candle["close_ts"] >= ms:
            break
        closes.append(candle["close"])

    if len(closes) < lookback_candles + 1:
        result["btc_candle_closed_before_entry"] = True
        return result

    closes = list(reversed(closes))  # chronological order
    returns = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
    result["btc_candle_closed_before_entry"] = True
    result["feature_value"] = float(np.std(returns))
    return result


# ---------------------------------------------------------------------------
# Pipeline + evaluation helpers
# ---------------------------------------------------------------------------

def make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            max_iter=1000, solver="lbfgs", C=1.0,
            class_weight="balanced", random_state=42,
        )),
    ])


def build_X(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    base_cols = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    num_cols  = base_cols + feature_cols
    cat_dummies = pd.get_dummies(
        df["zone_type"].fillna("T1").astype(str), prefix="zone_type", drop_first=True
    )
    X = pd.concat([
        df[num_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0),
        cat_dummies,
    ], axis=1)
    return X


def evaluate_loco(X, y, groups, label: str = "") -> dict:
    loco = LeaveOneGroupOut()
    n_splits = loco.get_n_splits(X, y, groups)
    if n_splits < 3:
        return {"auc": np.nan, "n_splits": n_splits, "per_fold": []}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oof = cross_val_predict(
            make_pipe(), X, y, cv=loco, groups=groups,
            method="predict_proba", n_jobs=1,
        )[:, 1]
    auc = roc_auc_score(y, oof)
    # per-fold AUCs
    fold_aucs = []
    for tr_idx, te_idx in loco.split(X, y, groups):
        yte = y.iloc[te_idx]
        if yte.nunique() > 1:
            fold_aucs.append(round(float(roc_auc_score(yte, oof[te_idx])), 2))
    return {
        "auc":      float(auc),
        "n_splits": int(n_splits),
        "per_fold": fold_aucs,
    }


def evaluate_time(X, y, init_frac=0.70, step_frac=0.10,
                  min_train=10, min_test=1) -> dict:
    n = len(X)
    init_n = max(min_train, int(n * init_frac))
    step_n = max(min_test, int(n * step_frac))
    all_yt, all_yp, fold_aucs = [], [], []
    cursor = init_n
    while cursor < n:
        te = min(cursor + step_n, n)
        Xtr, ytr = X.iloc[:cursor], y.iloc[:cursor]
        Xte, yte = X.iloc[cursor:te], y.iloc[cursor:te]
        if len(ytr) < min_train or len(yte) < min_test or ytr.nunique() < 2:
            cursor += step_n
            continue
        p = make_pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p.fit(Xtr, ytr)
            probs = p.predict_proba(Xte)[:, 1]
        all_yt.extend(yte.tolist())
        all_yp.extend(probs.tolist())
        if yte.nunique() > 1:
            fold_aucs.append(round(float(roc_auc_score(yte, probs)), 2))
        cursor += step_n
    if len(set(all_yt)) < 2:
        return {"auc": np.nan, "fold_std": np.nan, "per_fold": []}
    return {
        "auc":      float(roc_auc_score(all_yt, all_yp)),
        "fold_std": float(np.std(fold_aucs)) if fold_aucs else np.nan,
        "per_fold": fold_aucs,
    }


# ---------------------------------------------------------------------------
# Dataset loader
# ---------------------------------------------------------------------------

def load_base_df() -> pd.DataFrame:
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
    return df


# ---------------------------------------------------------------------------
# Evaluate one feature variant
# ---------------------------------------------------------------------------

def eval_variant(df_sub: pd.DataFrame, feature_cols: list[str],
                 label: str) -> dict:
    y      = df_sub["win"]
    groups = df_sub["_group"].fillna(pd.Series(df_sub.index.astype(str), index=df_sub.index)).astype(str)
    groups_ded = df_sub["_group_deduped"].fillna(pd.Series(df_sub.index.map(lambda i: f"unk_{i}"), index=df_sub.index)).astype(str)
    X      = build_X(df_sub, feature_cols)

    loco     = evaluate_loco(X, y, groups,     label)
    loco_ded = evaluate_loco(X, y, groups_ded, label + "-ded")
    time_r   = evaluate_time(X, y)

    spread = abs(loco["auc"] - time_r["auc"]) if not np.isnan(loco["auc"]) and not np.isnan(time_r["auc"]) else np.nan
    stable = spread <= 0.15 if not np.isnan(spread) else False

    # Verdict: LOCO > 0.52 AND Time > 0.52 AND stable
    loco_ok = (not np.isnan(loco["auc"])) and loco["auc"] > 0.52
    time_ok = (not np.isnan(time_r["auc"])) and time_r["auc"] > 0.52
    if loco_ok and time_ok and stable:
        verdict = "SUPPORTED"
    elif loco_ok or time_ok:
        verdict = "MARGINAL (single gate only)"
    elif np.isnan(loco["auc"]):
        verdict = "INSUFFICIENT_EVIDENCE"
    elif loco["auc"] > 0.50 or time_r["auc"] > 0.50:
        verdict = "NO_SIGNAL"
    else:
        verdict = "NO_SIGNAL (below random)"

    return {
        "label":        label,
        "n":            len(df_sub),
        "eff_n":        groups.nunique(),
        "ded_n":        groups_ded.nunique(),
        "loco":         round(loco["auc"], 4) if not np.isnan(loco["auc"]) else np.nan,
        "loco_ded":     round(loco_ded["auc"], 4) if not np.isnan(loco_ded["auc"]) else np.nan,
        "time":         round(time_r["auc"], 4) if not np.isnan(time_r["auc"]) else np.nan,
        "fold_std":     round(time_r["fold_std"], 4) if not np.isnan(time_r["fold_std"]) else np.nan,
        "spread":       round(spread, 4) if not np.isnan(spread) else np.nan,
        "stable":       stable,
        "per_fold_time": time_r["per_fold"],
        "per_fold_loco": loco["per_fold"][:6],
        "verdict":      verdict,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{SEP}")
    print("  BTC HORIZON EXPLORATION — PRE-REGISTERED VARIANTS V1-V6")
    print(f"  Signal threshold: LOCO > 0.52 AND Time > 0.52 (dual-gate)")
    print(f"  Stability threshold: |LOCO-Time| <= 0.15")
    print(f"{SEP}")

    full_map = build_btc_map_full()
    df_base  = load_base_df()
    print(f"\n  Base dataset: {len(df_base)} rows, eff_N={df_base['_group'].nunique()}")

    # ── Enrich all rows with all horizon features ──────────────────────────
    print(f"\n{SEP}\n  COMPUTING HORIZON FEATURES\n{SEP}")

    horizons = {
        "v2_1d_return":   (6,  "direction"),   # 6 × 4h = 24h
        "v3_3d_return":   (18, "direction"),   # 18 × 4h = 72h
        "v4_7d_return":   (42, "direction"),   # 42 × 4h = 168h
        "v5_1d_vol":      (6,  "volatility"),  # 6 returns = 1d realized vol
    }

    all_feat_data = {name: [] for name in horizons}
    all_closed    = {name: [] for name in horizons}

    for _, row in df_base.iterrows():
        ms_raw = row.get("entry_fill_time")
        try:    ms = int(float(ms_raw))
        except: ms = 0

        for name, (lb, ftype) in horizons.items():
            if ftype == "direction":
                r = compute_direction_feature(ms, full_map, lb)
            else:
                r = compute_volatility_feature(ms, full_map, lb)
            all_feat_data[name].append(r["feature_value"])
            all_closed[name].append(r["btc_candle_closed_before_entry"])

    for name in horizons:
        df_base[f"feat_{name}"] = pd.to_numeric(all_feat_data[name], errors="coerce")
        df_base[f"closed_{name}"] = all_closed[name]
        n_clean = sum(1 for v in all_closed[name] if v is True)
        n_miss  = sum(1 for v in all_closed[name] if v is None)
        n_amb   = sum(1 for v in all_closed[name] if v is False)
        print(f"  {name:<18}: clean={n_clean}  ambiguous={n_amb}  missing={n_miss}")

    # ── Evaluate each variant ──────────────────────────────────────────────
    print(f"\n{SEP}\n  EVALUATING VARIANTS\n{SEP}")
    results = []

    # V1 — already tested, include prior result directly
    v1_prior = {
        "label":         "V1  4h-return  [prior run, not re-executed]",
        "n":             154, "eff_n": 104, "ded_n": 117,
        "loco":          0.4771, "loco_ded": 0.4218,
        "time":          0.6190, "fold_std": 0.3345, "spread": 0.1419,
        "stable":        True,
        "per_fold_time": [0.92, 0.58, 0.64, 0.0],
        "per_fold_loco": [],
        "verdict":       "UNSTABLE (LOCO < 0.52, fold collapse to 0.0)",
    }
    results.append(v1_prior)
    print(f"  V1: LOCO={v1_prior['loco']:.4f}  Time={v1_prior['time']:.4f}  [prior result, not re-run]")

    # V2 — 1d direction
    mask_v2 = (df_base["closed_v2_1d_return"] == True) & df_base["feat_v2_1d_return"].notna()
    df_v2   = df_base[mask_v2].copy().reset_index(drop=True)
    df_v2["btc_feature"] = df_v2["feat_v2_1d_return"]
    r2 = eval_variant(df_v2, ["btc_feature"], "V2  1d-return")
    results.append(r2)
    print(f"  V2: LOCO={r2['loco']:.4f}  Time={r2['time']:.4f}  spread={r2['spread']:.4f}  {r2['verdict']}")

    # V3 — 3d direction
    mask_v3 = (df_base["closed_v3_3d_return"] == True) & df_base["feat_v3_3d_return"].notna()
    df_v3   = df_base[mask_v3].copy().reset_index(drop=True)
    df_v3["btc_feature"] = df_v3["feat_v3_3d_return"]
    r3 = eval_variant(df_v3, ["btc_feature"], "V3  3d-return")
    results.append(r3)
    print(f"  V3: LOCO={r3['loco']:.4f}  Time={r3['time']:.4f}  spread={r3['spread']:.4f}  {r3['verdict']}")

    # V4 — 7d direction
    mask_v4 = (df_base["closed_v4_7d_return"] == True) & df_base["feat_v4_7d_return"].notna()
    df_v4   = df_base[mask_v4].copy().reset_index(drop=True)
    df_v4["btc_feature"] = df_v4["feat_v4_7d_return"]
    r4 = eval_variant(df_v4, ["btc_feature"], "V4  7d-return")
    results.append(r4)
    print(f"  V4: LOCO={r4['loco']:.4f}  Time={r4['time']:.4f}  spread={r4['spread']:.4f}  {r4['verdict']}")

    # V5 — 1d volatility
    mask_v5 = (df_base["closed_v5_1d_vol"] == True) & df_base["feat_v5_1d_vol"].notna()
    df_v5   = df_base[mask_v5].copy().reset_index(drop=True)
    df_v5["btc_feature"] = df_v5["feat_v5_1d_vol"]
    r5 = eval_variant(df_v5, ["btc_feature"], "V5  1d-vol")
    results.append(r5)
    print(f"  V5: LOCO={r5['loco']:.4f}  Time={r5['time']:.4f}  spread={r5['spread']:.4f}  {r5['verdict']}")

    # V6 — Combined V2 + V5 (only if both LOCO > 0.48)
    v2_ok = r2["loco"] > 0.48
    v5_ok = r5["loco"] > 0.48
    if v2_ok and v5_ok:
        print(f"\n  V6 ELIGIBLE: V2 LOCO={r2['loco']:.4f} > 0.48 AND V5 LOCO={r5['loco']:.4f} > 0.48 → running")
        mask_v6 = (mask_v2) & (mask_v5) & df_base["feat_v2_1d_return"].notna() & df_base["feat_v5_1d_vol"].notna()
        df_v6   = df_base[mask_v6].copy().reset_index(drop=True)
        df_v6["btc_1d_return"] = df_v6["feat_v2_1d_return"]
        df_v6["btc_1d_vol"]    = df_v6["feat_v5_1d_vol"]
        r6 = eval_variant(df_v6, ["btc_1d_return", "btc_1d_vol"], "V6  1d-return+vol")
        results.append(r6)
        print(f"  V6: LOCO={r6['loco']:.4f}  Time={r6['time']:.4f}  spread={r6['spread']:.4f}  {r6['verdict']}")
    else:
        skip_reason = []
        if not v2_ok: skip_reason.append(f"V2 LOCO={r2['loco']:.4f} ≤ 0.48")
        if not v5_ok: skip_reason.append(f"V5 LOCO={r5['loco']:.4f} ≤ 0.48")
        skip_msg = "  V6 SKIPPED: " + "; ".join(skip_reason)
        print(f"\n{skip_msg}")
        v6_skipped = {
            "label": f"V6  1d-return+vol [SKIPPED: {'; '.join(skip_reason)}]",
            "n": 0, "eff_n": 0, "ded_n": 0,
            "loco": np.nan, "loco_ded": np.nan, "time": np.nan,
            "fold_std": np.nan, "spread": np.nan, "stable": False,
            "per_fold_time": [], "per_fold_loco": [],
            "verdict": f"SKIPPED — {'; '.join(skip_reason)}",
        }
        results.append(v6_skipped)

    # ── Summary table ─────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  FULL RESULTS TABLE — ALL PRE-REGISTERED VARIANTS")
    print(f"{SEP}")
    hdr = f"  {'Variant':<28} {'N':>5} {'eff_N':>6} {'LOCO':>7} {'Ded':>7} {'Time':>7} {'Spread':>7} {'Stable':>7}"
    print(hdr)
    print(f"  {'─'*80}")
    for r in results:
        loco_s = f"{r['loco']:.4f}" if not np.isnan(r['loco'] if r['loco'] is not None else np.nan) else "n/a"
        ded_s  = f"{r['loco_ded']:.4f}" if not np.isnan(r['loco_ded'] if r['loco_ded'] is not None else np.nan) else "n/a"
        time_s = f"{r['time']:.4f}" if not np.isnan(r['time'] if r['time'] is not None else np.nan) else "n/a"
        spr_s  = f"{r['spread']:.4f}" if not np.isnan(r['spread'] if r['spread'] is not None else np.nan) else "n/a"
        stb_s  = "✓" if r["stable"] else "✗"
        print(f"  {r['label']:<28} {r['n']:>5} {r['eff_n']:>6} {loco_s:>7} {ded_s:>7} {time_s:>7} {spr_s:>7} {stb_s:>7}")
        if r.get("per_fold_time"):
            print(f"  {'':>28}  time-folds: {r['per_fold_time']}")

    # ── Overall verdict ───────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  OVERALL VERDICT")
    print(f"{SEP}")
    supported   = [r for r in results if r["verdict"] == "SUPPORTED"]
    marginal    = [r for r in results if "MARGINAL" in r["verdict"]]
    no_signal   = [r for r in results if "NO_SIGNAL" in r["verdict"] or "UNSTABLE" in r["verdict"]]
    skipped     = [r for r in results if "SKIPPED" in r["verdict"]]

    print(f"\n  SUPPORTED (dual-gate cleared): {len(supported)}")
    print(f"  MARGINAL  (one gate only):     {len(marginal)}")
    print(f"  NO_SIGNAL / UNSTABLE:          {len(no_signal)}")
    print(f"  SKIPPED:                       {len(skipped)}")

    all_loco = [r["loco"] for r in results if not (np.isnan(r["loco"]) if isinstance(r["loco"], float) else False) and r["loco"] is not None and r["n"] > 0]
    if supported:
        print(f"\n  ⚠ MULTIPLE-TESTING WARNING: {len(supported)} variant(s) cleared dual gate")
        print(f"    across {len([r for r in results if r['n']>0])} tested variants.")
        print(f"    This is weak evidence due to multiple comparisons.")
        print(f"    Any 'supported' result REQUIRES independent replication at N=150")
        print(f"    before being considered confirmed signal.")
    elif marginal:
        print(f"\n  MARGINAL results only. No variant cleared both LOCO > 0.52 AND Time > 0.52.")
        print(f"  Multiple-testing caveat still applies to the marginal result(s).")
    else:
        print(f"\n  NO variant cleared the dual gate (LOCO > 0.52 AND Time > 0.52).")
        if all_loco:
            print(f"  LOCO AUC range across tested variants: {min(all_loco):.4f} – {max(all_loco):.4f}")
            print(f"  All LOCO values at or below baseline level.")
        print(f"\n  CONCLUSION: The BTC-context hypothesis family (direction and volatility")
        print(f"  at 4h, 1d, 3d, 7d horizons) is EXHAUSTED at the horizon-variation level.")
        print(f"  More BTC horizon variants are unlikely to surface signal.")
        print(f"  Effort should redirect to a different feature family")
        print(f"  (own-symbol trend, market breadth, volume, or sentiment).")

    print(f"\n  Production behavior changed: NO")
    print(f"  No model saved. No Supabase writes.")


if __name__ == "__main__":
    main()
