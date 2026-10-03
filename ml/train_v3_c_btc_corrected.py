"""
train_v3_c_btc_corrected.py
============================
Part 1 Audit: Re-run of Experiment C2 (baseline_v2 + btc_4h_change_pct)
with corrected closed-candle protocol.

AUDIT FINDING (Part 1):
-----------------------
train_v3_c_btc_context.py is LEAKY.

Exact bug (lines 223-227):
    aligned  = (ms // CANDLE_MS) * CANDLE_MS   # floor → FORMING candle open
    prev4h   = aligned - CANDLE_MS
    btc_now  = btc_map.get(aligned)             # close of FORMING candle → future data
    btc_prev = btc_map.get(prev4h)              # close of prev CLOSED candle → clean

    btc_4h_change_pct = (btc_now - btc_prev) / btc_prev * 100

btc_map stores {open_ts: close_price}. The key `aligned` is the open of
the candle that entry_fill_time falls INSIDE. That candle has NOT yet
closed at the time of entry. Using its close_price is look-ahead.

The correct approach (proven in ml/train_futures_short_v2.py):
    prev_candle_open  = aligned - CANDLE_MS   # most recent FULLY CLOSED candle
    pprev_candle_open = aligned - 2*CANDLE_MS
    btc_4h_change_pct = (btc_map[prev_close] - btc_map[pprev_close]) / btc_map[pprev_close] * 100

Assertion: prev_candle.close_ts < entry_fill_time (strict <)

This script re-runs the exact same experiment (C2: baseline_v2 +
btc_4h_change_pct) with the corrected protocol, and prints old vs
corrected numbers side by side.

DO NOT DEPLOY OR USE FOR PRODUCTION DECISIONS.

Run:
    python3 ml/train_v3_c_btc_corrected.py
"""

from __future__ import annotations

import io
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
# BTC data fetcher — extended for Jul–Sep 2026
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 20) -> requests.Response:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return requests.get(url, headers=HEADERS, timeout=timeout, verify=False)


def _parse_zip_with_close_ts(content: bytes) -> dict[int, dict]:
    """
    Parse data.binance.vision klines zip.
    Returns {open_ts_ms: {open_ts, close_ts, close_price}}
    close_ts is needed for the strict < assertion.
    """
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


def _parse_zip_close_only(content: bytes) -> dict[int, float]:
    """Simplified map {open_ts_ms: close_price} — same as original script."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    result = {}
    for line in raw.strip().split("\n"):
        if not line or line.startswith("open"):
            continue
        p = line.split(",")
        if len(p) < 5:
            continue
        ts_raw = int(p[0])
        open_ts = ts_raw // 1000 if ts_raw > 1_000_000_000_000_000 else ts_raw
        result[open_ts] = float(p[4])
    return result


def build_btc_map_full() -> tuple[dict[int, dict], dict[int, float]]:
    """
    Build full BTC 4h candle maps for Jul–Sep 2026.
    Returns:
      full_map:   {open_ts: {open_ts, close_ts, close}} — for corrected protocol
      simple_map: {open_ts: close}                      — for leaky protocol (audit only)
    """
    print(f"\n{SEP}")
    print("  FETCHING BTC 4H DATA — Jul–Sep 2026")
    print(SEP)

    full_map   = {}
    simple_map = {}

    for ym in ["2026-07", "2026-08"]:
        url = (f"https://data.binance.vision/data/spot/monthly/klines/"
               f"BTCUSDT/4h/BTCUSDT-4h-{ym}.zip")
        r = _get(url)
        if r.status_code == 200:
            parsed = _parse_zip_with_close_ts(r.content)
            full_map.update(parsed)
            simple_map.update({k: v["close"] for k, v in parsed.items()})
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
            simple_map.update({k: v["close"] for k, v in parsed.items()})
            sep_loaded += len(parsed)
    print(f"  Sep 2026 daily (01-18): {sep_loaded} candles ✓")
    print(f"  Total: {len(full_map)} candles")
    return full_map, simple_map


# ---------------------------------------------------------------------------
# Feature engineering — LEAKY (original, for audit comparison only)
# ---------------------------------------------------------------------------

def compute_btc_feature_leaky(
    entry_ms: int, simple_map: dict[int, float]
) -> dict:
    """
    LEAKY version — mirrors exactly what train_v3_c_btc_context.py does.
    Uses btc_map[aligned] where aligned = floor(entry_ms / 4h) * 4h.
    That candle is FORMING at entry time — its close is future data.
    Used ONLY to reproduce the original (suspect) numbers for comparison.
    """
    if not entry_ms:
        return {"btc_4h_change_pct": None, "used_candle": "forming"}

    aligned = (entry_ms // CANDLE_MS) * CANDLE_MS
    prev4h  = aligned - CANDLE_MS

    btc_now  = simple_map.get(aligned)
    btc_prev = simple_map.get(prev4h)

    if btc_now is not None and btc_prev is not None and btc_prev > 0:
        chg = (btc_now - btc_prev) / btc_prev * 100
        return {"btc_4h_change_pct": chg, "used_candle": "forming"}
    return {"btc_4h_change_pct": None, "used_candle": "forming"}


# ---------------------------------------------------------------------------
# Feature engineering — CORRECTED (closed-candle only)
# ---------------------------------------------------------------------------

def compute_btc_feature_corrected(
    entry_ms: int, full_map: dict[int, dict]
) -> dict:
    """
    Corrected version — identical protocol to ml/train_futures_short_v2.py.
    Uses prev_candle (one full 4h period before the forming candle),
    asserts prev_candle.close_ts < entry_fill_time (strict <).
    """
    result = {
        "btc_4h_change_pct":              None,
        "btc_candle_closed_before_entry": None,
        "btc_prev_candle_open_ts":        None,
        "btc_prev_candle_close_ts":       None,
    }

    if not entry_ms or entry_ms <= 0:
        return result

    ms = int(entry_ms)
    current_candle_open = (ms // CANDLE_MS) * CANDLE_MS   # FORMING — never used
    prev_open           = current_candle_open - CANDLE_MS  # last CLOSED candle
    pprev_open          = current_candle_open - 2 * CANDLE_MS

    prev_data  = full_map.get(prev_open)
    pprev_data = full_map.get(pprev_open)

    if prev_data is None:
        return result

    result["btc_prev_candle_open_ts"]  = prev_data["open_ts"]
    result["btc_prev_candle_close_ts"] = prev_data["close_ts"]

    # Strict closed-candle assertion
    if prev_data["close_ts"] >= ms:
        result["btc_candle_closed_before_entry"] = False
        return result

    result["btc_candle_closed_before_entry"] = True

    if pprev_data is None:
        return result

    prev_close  = prev_data["close"]
    pprev_close = pprev_data["close"]

    if pprev_close <= 0:
        return result

    result["btc_4h_change_pct"] = (prev_close - pprev_close) / pprev_close * 100
    return result


# ---------------------------------------------------------------------------
# Dataset builder — builds BOTH leaky and corrected datasets
# ---------------------------------------------------------------------------

def build_datasets(
    full_map: dict[int, dict],
    simple_map: dict[int, float],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (df_leaky, df_corrected).
    df_leaky reproduces the original experiment for comparison.
    df_corrected uses strict closed-candle protocol.
    """
    rows = fetch_all_spot()
    df   = pd.DataFrame(rows)
    df   = df[df["exit_status"].isin(["TP_HIT", "SL_HIT"])].copy().reset_index(drop=True)
    # Provenance filter — mirrors ml/train_v1.py & train_v2.py. Excludes non-genuine
    # exits (phantom price-guard, emergency, manual resolution) so BTC candidates
    # use the same row set as baseline_v2.
    _EXCLUDED = {"PRICE_GUARD_SL","UNPROTECTED_SL_BREACH","UNPROTECTED_TP_BREACH",
                 "OCO_STUCK_MANUAL_RESOLUTION","EMERGENCY_CLOSED",
                 "RECOVERED_SL_HIT","STALE_SETUP_CANCELLED"}
    if "exit_reason" in df.columns:
        df = df[~df["exit_reason"].isin(_EXCLUDED)].copy().reset_index(drop=True)
    df["win"] = (df["exit_status"] == "TP_HIT").astype(int)

    # Groups
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

    # Sort by time
    eft = pd.to_numeric(df.get("entry_fill_time"), errors="coerce")
    ot  = pd.to_datetime(df.get("open_time"), utc=True, errors="coerce")
    cat = pd.to_datetime(df.get("created_at"), utc=True, errors="coerce")
    sort_ms = eft.copy()
    df["_sort_time"] = sort_ms
    df = df.sort_values("_sort_time").reset_index(drop=True)

    # Coerce numeric baselines
    for col in ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # --- LEAKY enrichment (original script behavior) ---
    btc_leaky = []
    for _, row in df.iterrows():
        ms_raw = row.get("entry_fill_time")
        try:    ms = int(float(ms_raw))
        except: ms = 0
        result = compute_btc_feature_leaky(ms, simple_map)
        btc_leaky.append(result["btc_4h_change_pct"])

    df_leaky = df.copy()
    df_leaky["btc_4h_change_pct"]              = pd.to_numeric(btc_leaky, errors="coerce")
    df_leaky["btc_candle_closed_before_entry"] = "LEAKY"

    # --- CORRECTED enrichment ---
    btc_corr   = []
    btc_closed = []
    n_clean = n_ambiguous = n_missing = 0

    for _, row in df.iterrows():
        ms_raw = row.get("entry_fill_time")
        try:    ms = int(float(ms_raw))
        except: ms = 0
        result = compute_btc_feature_corrected(ms, full_map)
        btc_corr.append(result["btc_4h_change_pct"])
        btc_closed.append(result["btc_candle_closed_before_entry"])
        if result["btc_candle_closed_before_entry"] is True and result["btc_4h_change_pct"] is not None:
            n_clean += 1
        elif result["btc_candle_closed_before_entry"] is False:
            n_ambiguous += 1
        else:
            n_missing += 1

    df_corr_raw = df.copy()
    df_corr_raw["btc_4h_change_pct"]              = pd.to_numeric(btc_corr, errors="coerce")
    df_corr_raw["btc_candle_closed_before_entry"] = btc_closed

    # Exclude rows where closed candle data is not available
    df_corrected = df_corr_raw[
        (df_corr_raw["btc_candle_closed_before_entry"] == True) &
        df_corr_raw["btc_4h_change_pct"].notna()
    ].copy().reset_index(drop=True)

    print(f"\n  Dataset composition:")
    print(f"  {'Metric':<40} {'Leaky':>8} {'Corrected':>10}")
    print(f"  {'─'*60}")
    print(f"  {'Raw N (after filter)':<40} {len(df_leaky[df_leaky['btc_4h_change_pct'].notna()]):>8} {len(df_corrected):>10}")
    print(f"  {'TP_HIT wins':<40} {int(df_leaky[df_leaky['btc_4h_change_pct'].notna()]['win'].sum()):>8} {int(df_corrected['win'].sum()):>10}")
    print(f"  {'Win rate':<40} {df_leaky[df_leaky['btc_4h_change_pct'].notna()]['win'].mean()*100:>7.1f}% {df_corrected['win'].mean()*100:>9.1f}%")
    print(f"  {'Effective N (cluster)':<40} {df_leaky[df_leaky['btc_4h_change_pct'].notna()]['_group'].nunique():>8} {df_corrected['_group'].nunique():>10}")

    print(f"\n  Corrected BTC enrichment:")
    print(f"    Clean (closed before entry):   {n_clean}")
    print(f"    Ambiguous (close_ts >= entry):  {n_ambiguous}  [excluded]")
    print(f"    Missing BTC data:               {n_missing}  [excluded]")

    return df_leaky[df_leaky["btc_4h_change_pct"].notna()].reset_index(drop=True), df_corrected


# ---------------------------------------------------------------------------
# Pipeline + evaluation helpers (identical to existing protocol)
# ---------------------------------------------------------------------------

def make_pipe() -> Pipeline:
    return Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(
            max_iter=1000, solver="lbfgs", C=1.0,
            class_weight="balanced", random_state=42,
        )),
    ])


def build_X(df: pd.DataFrame, include_btc: bool) -> pd.DataFrame:
    num_cols = ["zone_touches", "planned_rr", "risk_pct", "atr_pct_at_entry"]
    if include_btc:
        num_cols = num_cols + ["btc_4h_change_pct"]
    cat_dummies = pd.get_dummies(df["zone_type"].fillna("T1").astype(str),
                                  prefix="zone_type", drop_first=True)
    X = pd.concat([
        df[num_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0),
        cat_dummies,
    ], axis=1)
    return X


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
    auc = roc_auc_score(y, oof)
    return {"auc": float(auc), "n_splits": int(n_splits), "oof": oof}


def evaluate_time(X, y, init_frac=0.70, step_frac=0.10,
                  min_train=10, min_test=1, label="") -> dict:
    n = len(X)
    init_n = max(min_train, int(n * init_frac))
    step_n = max(min_test,  int(n * step_frac))
    all_yt, all_yp, fold_aucs = [], [], []
    cursor = init_n

    while cursor < n:
        te = min(cursor + step_n, n)
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
    return {"auc": agg_auc, "fold_std": fold_std, "n_folds": len(fold_aucs),
            "fold_aucs": fold_aucs}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{SEP}")
    print("  PART 1 AUDIT: SPOT BTC MOMENTUM LEAKAGE RE-RUN")
    print(f"  Research only — no production changes")
    print(SEP)

    print(f"\n  AUDIT FINDING:")
    print(f"  {'─'*60}")
    print(f"  Script audited: ml/train_v3_c_btc_context.py")
    print(f"  Lines 223-234 (enrich_features):")
    print(f"    aligned  = (ms // CANDLE_MS) * CANDLE_MS  ← FORMING candle open")
    print(f"    btc_now  = btc_map.get(aligned)            ← FORMING candle close")
    print(f"    btc_4h_change_pct = (btc_now - btc_prev) / btc_prev * 100")
    print(f"                        ↑ future data          ← LEAKY")
    print(f"  Verdict: LEAKY")
    print(f"  Cited numbers (LOCO AUC 0.618, fold std 0.015) require re-run.")
    print(f"  {'─'*60}")

    # ── Fetch data ────────────────────────────────────────────────────────
    full_map, simple_map = build_btc_map_full()

    # ── Build datasets ────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  BUILDING DATASETS")
    print(SEP)
    df_leaky, df_corr = build_datasets(full_map, simple_map)

    # ── Evaluate both versions of C2 feature set ─────────────────────────
    print(f"\n{SEP}")
    print("  EVALUATING — C2: baseline_v2 + btc_4h_change_pct")
    print("  Both LEAKY (original) and CORRECTED protocols")
    print(SEP)

    results = {}

    for tag, df_use in [("LEAKY (original)", df_leaky),
                         ("CORRECTED",        df_corr)]:
        y          = df_use["win"]
        idx_map    = {i: f"single_{i}" for i in df_use.index}
        groups     = df_use["_group"].fillna(pd.Series(idx_map)).astype(str)
        groups_ded = df_use["_group_deduped"].fillna("unknown").astype(str)

        # baseline_v2 (no BTC — same for both, use corrected df for consistency)
        X_base = build_X(df_use, include_btc=False)
        loco_base = evaluate_loco(X_base, y, groups, "baseline_v2")
        time_base = evaluate_time(X_base, y)

        # C2 feature set (+ btc_4h_change_pct)
        X_c2   = build_X(df_use, include_btc=True)
        loco_c2     = evaluate_loco(X_c2, y, groups,     "C2")
        loco_c2_ded = evaluate_loco(X_c2, y, groups_ded, "C2-deduped")
        time_c2     = evaluate_time(X_c2, y)

        print(f"\n  [{tag}]  N={len(df_use)}, wins={int(y.sum())}, "
              f"eff_N={groups.nunique()}, ded_N={groups_ded.nunique()}")
        print(f"  {'Metric':<30} {'baseline_v2':>12} {'C2 (+BTC)':>12} {'C2 deduped':>12}")
        print(f"  {'─'*70}")
        print(f"  {'LOCO AUC':<30} {loco_base['auc']:>12.4f} {loco_c2['auc']:>12.4f} {loco_c2_ded['auc']:>12.4f}")
        print(f"  {'Time AUC':<30} {time_base['auc']:>12.4f} {time_c2['auc']:>12.4f} {'—':>12}")
        print(f"  {'Time fold std':<30} {'—':>12} {time_c2.get('fold_std', np.nan):>12.4f} {'—':>12}")
        print(f"  {'AUC spread |LOCO-Time|':<30} {'—':>12} {abs(loco_c2['auc']-time_c2['auc']):>12.4f} {'—':>12}")
        if time_c2.get("fold_aucs"):
            faucs = time_c2["fold_aucs"]
            print(f"  {'Per-fold AUCs':<30} {'':>12} {str([round(a,3) for a in faucs]):>12} {'':>12}")

        results[tag] = {
            "n": len(df_use), "wins": int(y.sum()),
            "loco_base": loco_base["auc"], "time_base": time_base["auc"],
            "loco_c2": loco_c2["auc"], "loco_c2_ded": loco_c2_ded["auc"],
            "time_c2": time_c2["auc"], "fold_std_c2": time_c2.get("fold_std", np.nan),
        }

    # ── Side-by-side comparison ───────────────────────────────────────────
    print(f"\n{SEP}")
    print("  PART 1 AUDIT: SIDE-BY-SIDE COMPARISON")
    print(SEP)

    orig_n = results.get("LEAKY (original)", {})
    corr_n = results.get("CORRECTED", {})

    print(f"\n  {'Metric':<38} {'LEAKY (original)':>16} {'CORRECTED':>12} {'Δ':>8}")
    print(f"  {'─'*76}")

    rows_ = [
        ("Dataset N",            orig_n.get('n', '?'),      corr_n.get('n', '?'),      None),
        ("Wins (TP_HIT)",        orig_n.get('wins', '?'),   corr_n.get('wins', '?'),   None),
        ("LOCO AUC (baseline)", orig_n.get('loco_base', np.nan), corr_n.get('loco_base', np.nan), True),
        ("Time AUC (baseline)", orig_n.get('time_base', np.nan), corr_n.get('time_base', np.nan), True),
        ("LOCO AUC (C2 + BTC)", orig_n.get('loco_c2', np.nan),  corr_n.get('loco_c2', np.nan),  True),
        ("LOCO AUC (C2 deduped)",orig_n.get('loco_c2_ded', np.nan),corr_n.get('loco_c2_ded',np.nan),True),
        ("Time AUC (C2 + BTC)", orig_n.get('time_c2', np.nan),  corr_n.get('time_c2', np.nan),  True),
        ("Time fold std (C2)",  orig_n.get('fold_std_c2', np.nan), corr_n.get('fold_std_c2', np.nan), True),
    ]

    for label, ov, cv, is_float in rows_:
        if is_float:
            d = cv - ov if (not np.isnan(ov) and not np.isnan(cv)) else np.nan
            d_str = f"{d:>+8.4f}" if not np.isnan(d) else "       —"
            print(f"  {label:<38} {ov:>16.4f} {cv:>12.4f} {d_str}")
        else:
            print(f"  {label:<38} {str(ov):>16} {str(cv):>12}")

    print(f"\n  Reference (cited in docs/ml_direction_notes.md §20.2):")
    print(f"    LOCO AUC C2 = 0.618, fold std = 0.015")
    print(f"    Dataset at time of original run: Raw N~110, Effective N~70")

    loco_delta = corr_n.get('loco_c2', np.nan) - orig_n.get('loco_c2', np.nan)
    time_delta = corr_n.get('time_c2', np.nan) - orig_n.get('time_c2', np.nan)

    print(f"\n  LOCO AUC drop (Corrected vs Leaky): {loco_delta:+.4f}")
    print(f"  Time AUC drop (Corrected vs Leaky): {time_delta:+.4f}")

    spread_orig = abs(orig_n.get('loco_c2', 0) - orig_n.get('time_c2', 0))
    spread_corr = abs(corr_n.get('loco_c2', 0) - corr_n.get('time_c2', 0))
    print(f"  AUC spread (|LOCO-Time|) Leaky:     {spread_orig:.4f}")
    print(f"  AUC spread (|LOCO-Time|) Corrected: {spread_corr:.4f}")
    print(f"  Stability threshold:                 0.1500")

    print(f"\n  {'─'*76}")
    if spread_corr <= 0.15 and corr_n.get('loco_c2', 0) > 0.52:
        verdict = "PARTIALLY SURVIVES — signal weakened but not eliminated"
    elif corr_n.get('loco_c2', 0.5) < 0.52 or spread_corr > 0.15:
        verdict = "DOES NOT SURVIVE — leakage was primary driver of cited numbers"
    else:
        verdict = "MARGINAL — insufficient data to conclude"
    print(f"  PART 1 VERDICT: {verdict}")
    print(f"  {'─'*76}")
    print(f"\n  Original numbers (LOCO 0.618, fold std 0.015) should be")
    print(f"  treated as SUSPECT until corrected numbers are confirmed.")
    print(f"  Use corrected numbers from this run as the new reference.")
    print(f"\n  Production behavior changed: NO")


if __name__ == "__main__":
    main()
