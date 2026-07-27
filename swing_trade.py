"""
Swing Trade — Walk-Forward Pipeline (Real Historical Data)
===========================================================
Data source: Binance Public Data Archive (no API key required)
  https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/{INTERVAL}/

Testnet: dipakai HANYA untuk verifikasi koneksi & live price check.
         Bukan sumber training data.

Pipeline:
    Archive download (real OHLCV) -> feature engineering -> labeling ->
    walk-forward split -> train RandomForest -> evaluasi tiap fold

Setup:
    pip install python-binance python-dotenv pandas numpy scikit-learn requests --break-system-packages
    cp .env.example .env  # isi API key testnet (opsional, hanya untuk live price check)
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

COINS: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL: str = "4h"

ARCHIVE_START: tuple[int, int] = (2023, 1)   # (year, month) inklusif
ARCHIVE_END:   tuple[int, int] = (2025, 5)   # bulan terakhir yang lengkap

CACHE_DIR: Path = Path("./data_cache")

# Walk-forward params
TRAIN_WINDOW_DAYS: int = 365
TEST_WINDOW_DAYS:  int = 60
STEP_DAYS:         int = 60

# Label params
HORIZON: int = 12  # 12 candle x 4h = ~2 hari ke depan

# ATR-relative labeling (Task 2)
# threshold_dynamic = ATR_MULTIPLIER * atr_pct (per candle)
# Artinya: LONG/SHORT hanya kalau forward move > N kali ATR saat ini.
# Fixed threshold lama (0.02) dihapus — sekarang fully volatility-adjusted.
ATR_MULTIPLIER: float = 1.5

ARCHIVE_BASE = "https://data.binance.vision/data/spot/monthly/klines"


# ---------------------------------------------------------------------------
# 1. DATA LOADING — Binance Public Archive
# ---------------------------------------------------------------------------

def _month_range(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    months = []
    y, m = start
    ey, em = end
    while (y, m) <= (ey, em):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _download_month(symbol: str, interval: str, year: int, month: int) -> pd.DataFrame | None:
    filename  = f"{symbol}-{interval}-{year}-{month:02d}.zip"
    url       = f"{ARCHIVE_BASE}/{symbol}/{interval}/{filename}"
    cache_path = CACHE_DIR / filename

    if cache_path.exists():
        raw_bytes = cache_path.read_bytes()
    else:
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 404:
                print(f"    [SKIP] {filename} — tidak ada di archive (404)", flush=True)
                return None
            resp.raise_for_status()
            raw_bytes = resp.content
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(raw_bytes)
        except requests.RequestException as e:
            print(f"    [WARN] Gagal download {filename}: {e}", flush=True)
            return None

    col_names = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                df = pd.read_csv(f, header=None, names=col_names)
    except Exception as e:
        print(f"    [WARN] Gagal parse {filename}: {e}", flush=True)
        cache_path.unlink(missing_ok=True)
        return None

    # Buang header row kalau ada
    df = df[pd.to_numeric(df["open_time"], errors="coerce").notna()].copy()

    # Deteksi unit: ms epoch 2023 ~1.67e12, us epoch 2025 ~1.74e15
    ts_raw = df["open_time"].astype(np.int64)
    unit   = "us" if ts_raw.iloc[0] > 1_000_000_000_000_000 else "ms"
    df["open_time"] = pd.to_datetime(ts_raw, unit=unit).dt.floor("ms")
    df = df.set_index("open_time")

    # Buang timestamp yang jelas anomali
    valid_mask = (df.index.year >= 2010) & (df.index.year <= 2030)
    n_bad = (~valid_mask).sum()
    if n_bad > 0:
        print(f"    [WARN] {filename}: {n_bad} baris timestamp anomali dibuang", flush=True)
    df = df.loc[valid_mask]

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df[["open", "high", "low", "close", "volume"]]


def fetch_historical_klines_archive(
    symbol: str,
    interval: str,
    start_year_month: tuple[int, int],
    end_year_month: tuple[int, int],
) -> pd.DataFrame:
    months  = _month_range(start_year_month, end_year_month)
    frames: list[pd.DataFrame] = []
    skipped = 0

    print(f"  [{symbol}] Downloading {len(months)} bulan data ({interval})...", flush=True)

    for year, month in months:
        df_month = _download_month(symbol, interval, year, month)
        if df_month is None:
            skipped += 1
            continue
        frames.append(df_month)

    if not frames:
        raise RuntimeError(f"Tidak ada data yang berhasil di-download untuk {symbol}.")

    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df["coin"] = symbol

    idx = df.index
    print(
        f"  [{symbol}] ✅ {len(df):,} candle  "
        f"({idx.min().year}-{idx.min().month:02d}-{idx.min().day:02d} "
        f"s/d {idx.max().year}-{idx.max().month:02d}-{idx.max().day:02d})  "
        f"[{skipped} bulan di-skip]",
        flush=True,
    )
    return df


# ---------------------------------------------------------------------------
# 2. TESTNET — hanya untuk verifikasi koneksi & live price
# ---------------------------------------------------------------------------

def get_testnet_client():
    try:
        from binance.client import Client
    except ImportError:
        raise ImportError("pip install python-binance --break-system-packages")

    api_key    = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("API key testnet belum diset di .env")

    client = Client(api_key, api_secret, testnet=True, tld="com")
    return client


def check_testnet_live_prices(client, coins: list[str]) -> None:
    print("\n=== Live Price dari Binance Testnet ===")
    for coin in coins:
        try:
            ticker = client.get_symbol_ticker(symbol=coin)
            print(f"  {coin}: {float(ticker['price']):,.2f} USDT")
        except Exception as e:
            print(f"  {coin}: gagal fetch ({e})")
    print()


# ---------------------------------------------------------------------------
# 3. FEATURE ENGINEERING
# ---------------------------------------------------------------------------

def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ma_fast"]  = df["close"].rolling(12).mean()
    df["ma_slow"]  = df["close"].rolling(48).mean()
    df["ma_cross"] = (df["ma_fast"] - df["ma_slow"]) / df["ma_slow"]

    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR-14: dipakai HANYA di label_forward_return() sebagai threshold.
    # TIDAK masuk FEATURE_COLS — menghindari label leakage.
    df["atr"]     = (df["high"] - df["low"]).rolling(14).mean()
    df["atr_pct"] = df["atr"] / df["close"]

    # ATR-50 (slow): volatility feature yang BERBEDA dari label threshold.
    # Lebih panjang lookback → menangkap regime volatility, bukan bar-per-bar threshold.
    # Inilah yang masuk FEATURE_COLS sebagai pengganti atr_pct.
    atr_slow          = (df["high"] - df["low"]).rolling(50).mean()
    df["atr_pct_slow"] = atr_slow / df["close"]

    vol_mean         = df["volume"].rolling(48).mean()
    vol_std          = df["volume"].rolling(48).std()
    df["vol_zscore"] = (df["volume"] - vol_mean) / vol_std

    df["return_6"]  = df["close"].pct_change(6)
    df["return_24"] = df["close"].pct_change(24)

    return df


# atr_pct (ATR-14) sengaja TIDAK ada di sini — itu milik label threshold, bukan model.
# atr_pct_slow (ATR-50) adalah volatility feature yang bebas dari leakage.
FEATURE_COLS_NO_ATR  = ["ma_cross", "rsi", "vol_zscore", "return_6", "return_24"]
FEATURE_COLS_WITH_SLOW = ["ma_cross", "rsi", "atr_pct_slow", "vol_zscore", "return_6", "return_24"]

# Aktif: Task 1 dulu (tanpa ATR sama sekali), lalu Task 2 (dengan atr_pct_slow)
FEATURE_COLS = FEATURE_COLS_WITH_SLOW


# ---------------------------------------------------------------------------
# 4. LABELING — ATR-relative threshold (Task 2)
# ---------------------------------------------------------------------------

def label_forward_return(df: pd.DataFrame, atr_multiplier: float = ATR_MULTIPLIER) -> pd.DataFrame:
    """
    Threshold dinamis per-candle: LONG/SHORT hanya kalau forward move
    melampaui atr_multiplier × atr_pct (volatility-adjusted bar).

    Keunggulan vs fixed threshold:
    - Periode volatil (BTC bull run): bar lebih tinggi → lebih selektif
    - Periode tenang: bar lebih rendah → tidak melewatkan genuine move
    - Konsisten antar coin yang punya volatilitas beda (BTC vs SOL)

    ⚠️  atr_pct harus sudah dihitung di add_features() sebelum fungsi ini dipanggil.
    ⚠️  Threshold masih belum memperhitungkan biaya transaksi / funding rate.
    """
    df  = df.copy()
    fwd = df["close"].shift(-HORIZON) / df["close"] - 1

    # Dynamic threshold per baris
    threshold_dynamic = atr_multiplier * df["atr_pct"]

    df["label"] = np.select(
        [fwd >= threshold_dynamic, fwd <= -threshold_dynamic],
        ["LONG", "SHORT"],
        default="FLAT",
    )
    df["forward_return"]    = fwd
    df["threshold_dynamic"] = threshold_dynamic
    return df


def label_distribution_str(series: pd.Series) -> str:
    total = len(series.dropna())
    vc    = series.value_counts()
    parts = [f"{lbl}: {cnt:,} ({cnt/total*100:.1f}%)" for lbl, cnt in vc.items()]
    return "  " + "\n  ".join(parts)


def weighted_random_baseline(series: pd.Series) -> float:
    """
    Weighted random baseline = sum(p_i^2).
    Lebih akurat dari 1/n_classes kalau distribusi tidak seimbang.
    """
    vc    = series.value_counts(normalize=True)
    return float((vc ** 2).sum())


# ---------------------------------------------------------------------------
# 5. WALK-FORWARD SPLIT
# ---------------------------------------------------------------------------

@dataclass
class Fold:
    train_start: pd.Timestamp
    train_end:   pd.Timestamp
    test_start:  pd.Timestamp
    test_end:    pd.Timestamp


def make_walk_forward_folds(index: pd.DatetimeIndex) -> list[Fold]:
    folds: list[Fold] = []
    start       = index.min()
    end         = index.max()
    train_start = start

    while True:
        train_end  = train_start + pd.Timedelta(days=TRAIN_WINDOW_DAYS)
        test_start = train_end
        test_end   = test_start + pd.Timedelta(days=TEST_WINDOW_DAYS)
        if test_end > end:
            break
        folds.append(Fold(train_start, train_end, test_start, test_end))
        train_start = train_start + pd.Timedelta(days=STEP_DAYS)

    return folds


# ---------------------------------------------------------------------------
# 6. TRAIN + EVALUATE PER FOLD  (Task 1: simpan feature importances)
# ---------------------------------------------------------------------------

@dataclass
class FoldResult:
    fold:         Fold
    accuracy:     float
    report:       str
    confusion:    np.ndarray
    n_train:      int
    n_test:       int
    importances:  np.ndarray   # shape (n_features,), same order as FEATURE_COLS
    baseline:     float        # weighted random baseline for this fold's test set


def run_walk_forward(df_all: pd.DataFrame, folds: list[Fold]) -> list[FoldResult]:
    results: list[FoldResult] = []

    for fold in folds:
        train_mask = (df_all.index >= fold.train_start) & (df_all.index < fold.train_end)
        test_mask  = (df_all.index >= fold.test_start)  & (df_all.index < fold.test_end)

        req_cols = FEATURE_COLS + ["label"]
        train_df = df_all.loc[train_mask].dropna(subset=req_cols)
        test_df  = df_all.loc[test_mask].dropna(subset=req_cols)

        if len(train_df) < 100 or len(test_df) < 20:
            print(
                f"  [SKIP fold {fold.test_start.date()}] "
                f"data kurang: train={len(train_df)} test={len(test_df)}"
            )
            continue

        X_train, y_train = train_df[FEATURE_COLS], train_df["label"]
        X_test,  y_test  = test_df[FEATURE_COLS],  test_df["label"]

        clf = RandomForestClassifier(
            n_estimators=200,
            max_depth=6,
            min_samples_leaf=20,
            class_weight="balanced",
            random_state=42,
        )
        clf.fit(X_train, y_train)
        y_pred = clf.predict(X_test)

        acc      = (y_pred == y_test.values).mean()
        report   = classification_report(y_test, y_pred, zero_division=0)
        cm       = confusion_matrix(y_test, y_pred, labels=["LONG", "SHORT", "FLAT"])
        baseline = weighted_random_baseline(y_test)

        results.append(FoldResult(
            fold, acc, report, cm,
            len(train_df), len(test_df),
            clf.feature_importances_.copy(),
            baseline,
        ))

    return results


# ---------------------------------------------------------------------------
# 7. FEATURE IMPORTANCE REPORT  (Task 1)
# ---------------------------------------------------------------------------

def print_feature_importance(results: list[FoldResult]) -> None:
    print("─" * 65)
    print("TASK 1 — FEATURE IMPORTANCE (per fold + rata-rata)")
    print("─" * 65)

    all_imp = np.array([r.importances for r in results])  # (n_folds, n_features)
    mean_imp = all_imp.mean(axis=0)
    std_imp  = all_imp.std(axis=0)

    # Header
    print(f"{'Feature':<14s}", end="")
    for i, r in enumerate(results):
        print(f"  F{i+1:>2d}   ", end="")
    print(f"  {'Mean':>6s}  {'Std':>5s}  Signal?")
    print("-" * 65)

    # Sort by mean importance descending
    order = np.argsort(mean_imp)[::-1]
    for idx in order:
        feat = FEATURE_COLS[idx]
        row  = f"{feat:<14s}"
        for fi in range(len(results)):
            row += f"  {all_imp[fi, idx]:.3f} "
        signal = "✅ keep" if mean_imp[idx] >= 0.10 else ("🟡 weak" if mean_imp[idx] >= 0.05 else "❌ near-zero")
        row += f"  {mean_imp[idx]:.3f}  {std_imp[idx]:.3f}  {signal}"
        print(row)

    print()
    near_zero = [FEATURE_COLS[i] for i in range(len(FEATURE_COLS)) if mean_imp[i] < 0.05]
    if near_zero:
        print(f"  Kandidat drop (mean importance < 0.05): {near_zero}")
    else:
        print("  Semua fitur di atas threshold 0.05 — tidak ada kandidat drop saat ini.")
    print()


# ---------------------------------------------------------------------------
# 8. MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 65)
    print("Swing Trade — Walk-Forward Pipeline (Real Archive Data)")
    print("=" * 65)
    print(f"Coins          : {COINS}")
    print(f"Interval       : {INTERVAL}")
    print(f"Archive        : {ARCHIVE_START[0]}-{ARCHIVE_START[1]:02d}  →  {ARCHIVE_END[0]}-{ARCHIVE_END[1]:02d}")
    print(f"Labeling       : ATR-relative  (multiplier={ATR_MULTIPLIER}×, horizon={HORIZON} candle)")
    print(f"Features ({len(FEATURE_COLS)})    : {FEATURE_COLS}")
    print(f"Cache          : {CACHE_DIR.resolve()}")
    print()

    # --- (A) Testnet connectivity check (opsional) ---
    api_key    = os.getenv("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    if api_key and api_secret:
        try:
            print("Menghubungkan ke Binance Testnet (verifikasi + live price)...")
            client = get_testnet_client()
            client.ping()
            print("✅ Testnet ping OK")
            check_testnet_live_prices(client, COINS)
        except Exception as e:
            print(f"⚠️  Testnet tidak tersambung ({e}) — lanjut tanpa live price.\n")
    else:
        print("ℹ️  API key testnet tidak ditemukan di .env — skip live price check.\n")

    # --- (B) Download data REAL dari Binance Archive ---
    print("Mengunduh data historis dari Binance Public Archive...")
    print(f"(File di-cache di {CACHE_DIR} — re-run tidak re-download)\n")

    all_data:   list[pd.DataFrame] = []
    coin_stats: dict[str, int]     = {}

    for coin in COINS:
        raw      = fetch_historical_klines_archive(coin, INTERVAL, ARCHIVE_START, ARCHIVE_END)
        coin_stats[coin] = len(raw)
        featured = add_features(raw)
        labeled  = label_forward_return(featured, atr_multiplier=ATR_MULTIPLIER)
        all_data.append(labeled)

    # --- (C) Gabung ---
    df_all = pd.concat(all_data).sort_index()

    # --- (D) Label distribution stats ---
    print()
    print("─" * 65)
    print("STATISTIK DATA & LABEL")
    print("─" * 65)
    for coin, n in coin_stats.items():
        print(f"  {coin:<10s}: {n:,} candle (real)")
    print(f"  {'TOTAL':<10s}: {len(df_all):,} candle")
    print()

    print("Distribusi label — ATR-relative (multiplier={:.1f}×):".format(ATR_MULTIPLIER))
    print(label_distribution_str(df_all["label"]))
    print()

    # Tunjukkan perbandingan distribusi ATR vs fixed 2%
    # (recompute fixed untuk referensi tanpa mengubah df_all)
    fwd_ref = df_all["close"].shift(-HORIZON) / df_all["close"] - 1
    fixed_labels = np.select(
        [fwd_ref >= 0.02, fwd_ref <= -0.02], ["LONG", "SHORT"], default="FLAT"
    )
    fixed_series = pd.Series(fixed_labels, index=df_all.index).dropna()
    print("Distribusi label — FIXED 2% (lama, untuk referensi):")
    total_ref = len(fixed_series)
    for lbl, cnt in fixed_series.value_counts().items():
        print(f"  {lbl}: {cnt:,} ({cnt/total_ref*100:.1f}%)")
    print()

    # Weighted random baseline dari distribusi ATR-label
    labeled_clean = df_all["label"].dropna()
    baseline_global = weighted_random_baseline(labeled_clean)
    print(f"Weighted random baseline (ATR label): {baseline_global:.3f}")
    print(f"  (vs naive 1/3 = 0.333 — berubah kalau distribusi tidak 33/33/33)")
    print()

    # --- (E) Walk-forward ---
    folds = make_walk_forward_folds(df_all.index)
    print(f"Fold walk-forward terbentuk: {len(folds)}")
    if len(folds) == 0:
        print("❌ Tidak ada fold yang bisa dibentuk.")
        sys.exit(1)
    print()

    results = run_walk_forward(df_all, folds)

    if not results:
        print("❌ Tidak ada fold yang berhasil dijalankan.")
        sys.exit(1)

    # --- (F) Feature importance (Task 1) ---
    print_feature_importance(results)

    # --- (G) Per-fold accuracy ---
    print("─" * 65)
    print("HASIL PER FOLD  (Task 3)")
    print("─" * 65)
    accuracies: list[float] = []
    baselines:  list[float] = []

    for i, r in enumerate(results, 1):
        above = r.accuracy - r.baseline
        flag  = " ⚠️  (~random)" if above < 0.03 else (" 🟡" if above < 0.07 else " 🟢")
        print(
            f"Fold {i:2d} | test {r.fold.test_start.date()} – {r.fold.test_end.date()} | "
            f"train={r.n_train:,}  test={r.n_test:,} | "
            f"acc={r.accuracy:.3f}  base={r.baseline:.3f}  Δ={above:+.3f}{flag}"
        )
        accuracies.append(r.accuracy)
        baselines.append(r.baseline)

    # --- (H) Ringkasan ---
    mean_acc  = np.mean(accuracies)
    std_acc   = np.std(accuracies)
    mean_base = np.mean(baselines)
    above_avg = mean_acc - mean_base

    print()
    print("=" * 65)
    print("RINGKASAN  (Task 3)")
    print("=" * 65)
    print(f"Fold valid              : {len(accuracies)} dari {len(folds)}")
    print(f"Rata-rata accuracy      : {mean_acc:.3f}")
    print(f"Std dev accuracy        : {std_acc:.3f}")
    print(f"Min / Max               : {min(accuracies):.3f} / {max(accuracies):.3f}")
    print(f"Rata-rata baseline      : {mean_base:.3f}  (weighted, bukan 1/3)")
    print(f"Rata-rata Δ above base  : {above_avg:+.3f}")
    print()

    # --- (I) Verdict jujur ---
    print("─" * 65)
    print("VERDICT")
    print("─" * 65)

    if above_avg < 0.03:
        print(
            "⚠️  TIDAK ADA PENINGKATAN SIGNIFIKAN setelah label ATR-relative.\n"
            f"   Δ accuracy vs baseline hanya {above_avg:+.3f} (threshold meaningful: >0.05).\n"
            "\n"
            "   Artinya: masalah bukan di threshold label — fitur yang ada\n"
            "   memang belum cukup membawa signal prediktif.\n"
            "\n"
            "   Next step logis:\n"
            "   1. Lihat feature importance di atas — drop fitur near-zero\n"
            "   2. Tambah fitur yang lebih informatif (MACD, Bollinger Band,\n"
            "      candle structure, cross-coin correlation)\n"
            "   3. Atau naikkan horizon label (lebih panjang = lebih predictable)"
        )
    elif above_avg < 0.07:
        print(
            f"🟡 Label ATR-relative memberi sedikit perbaikan (Δ={above_avg:+.3f}).\n"
            "   Ada hint signal tapi belum cukup untuk trading edge.\n"
            "   Feature engineering lanjutan kemungkinan akan membantu."
        )
    else:
        print(
            f"🟢 Label ATR-relative memberikan perbaikan nyata (Δ={above_avg:+.3f}).\n"
            "   Label fix adalah lever utama — ini signal yang bisa dieksplorasi.\n"
            "   Validasi selanjutnya: backtest PnL dengan realistic transaction cost."
        )

    if std_acc > 0.05:
        print(
            f"\n⚠️  Std dev tinggi ({std_acc:.3f}) — model tidak stabil antar periode.\n"
            "   Curiga regime-sensitive atau training window terlalu pendek."
        )

    # Detail fold terakhir
    last = results[-1]
    print(
        f"\nClassification report fold terakhir "
        f"(test {last.fold.test_start.date()} – {last.fold.test_end.date()}):"
    )
    print(last.report)


if __name__ == "__main__":
    main()
