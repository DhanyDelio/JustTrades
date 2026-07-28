# Swing Trade — Command Reference

Semua command dijalankan dari folder `/Users/dhanydelio/Swing_Trade`.

---

## Setup Awal

### 1. Install dependencies

```bash
pip3 install -r requirements.txt --break-system-packages
```

### 2. Buat file `.env`

```bash
cp .env.example .env
```

Isi `.env`:

```
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_API_SECRET=...
BINANCE_FUTURES_TESTNET_API_KEY=...
BINANCE_FUTURES_TESTNET_API_SECRET=...
SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

- Spot testnet key: [testnet.binance.vision](https://testnet.binance.vision)
- Futures testnet key: [testnet.binancefuture.com](https://testnet.binancefuture.com)
- Telegram bot: [@BotFather](https://t.me/BotFather)

### 3. Jalankan dashboard

```bash
streamlit run dashboard.py
```

---

## paper_trade_executor.py — Spot Paper Trading (Testnet)

### `--propose` — Single trade terbaik

```bash
# Preview (jawab n)
echo "n" | python3 paper_trade_executor.py --propose

# Real
python3 paper_trade_executor.py --propose

# Force symbol tertentu
python3 paper_trade_executor.py --propose --symbol BTCUSDT

# Non-interaktif (CI)
python3 paper_trade_executor.py --propose --yes
```

### `--propose-all` — Batch data collection

```bash
# Preview (tidak place order)
python3 paper_trade_executor.py --propose-all --dry-run

# Place semua kandidat
python3 paper_trade_executor.py --propose-all

# Non-interaktif (CI)
python3 paper_trade_executor.py --propose-all --yes
```

Scan tiered otomatis — selalu jalan 4 parts × 30 = **120 coin**:
- Part 1: rank 1–30
- Part 2: rank 31–60
- Part 3: rank 61–90
- Part 4: rank 91–120

Throttle adaptif membaca header `X-MBX-Used-Weight-1M` tiap part.
Stop hanya jika rate-limit ceiling (80% dari 6000/menit) tercapai.

Setiap trade dicatat dengan `symbol_rank` (posisi rank saat scan) dan
`ml_score` (model v1, observasi saja — tidak mempengaruhi keputusan).

### `--check-positions` — Monitor posisi + trigger OCO

```bash
python3 paper_trade_executor.py --check-positions
python3 paper_trade_executor.py --check-positions --verbose
```

Yang dilakukan:
1. Query entry order status dari exchange
2. Entry FILLED → catat fill price, kirim Telegram notif
3. FILLED + belum ada OCO → place OCO (SL + TP)
4. OCO `ALL_DONE` → detect TP_HIT / SL_HIT, hitung PnL, update Supabase
5. Price-guard: kalau harga sudah breach SL meski OCO tidak trigger → resolve SL_HIT
6. Auto-cancel: order pending > 3 hari dengan gap > 30% dari entry → cancel otomatis

### `--stats` — Performance statistics

```bash
python3 paper_trade_executor.py --stats
```

---

## futures_trade_executor.py — Futures Paper Trading (Testnet)

Konfigurasi: **3x leverage, isolated margin**.
Log terpisah di Supabase `trades_futures` — tidak campur dengan spot.

### `--propose` — Batch propose (selalu multi)

```bash
# Default: isi sampai slot penuh (maksimal 20 posisi)
python3 futures_trade_executor.py --propose

# Tentukan jumlah
python3 futures_trade_executor.py --propose --count 3

# Filter side
python3 futures_trade_executor.py --propose --side LONG
python3 futures_trade_executor.py --propose --side SHORT

# Non-interaktif / CI (sekarang DEFAULT ON)
python3 futures_trade_executor.py --propose

# Interaktif (paksa minta konfirmasi 'y')
python3 futures_trade_executor.py --propose --no-yes
```

Scan tiered — selalu jalan 4 parts × 25 = **100 coin** (LONG + SHORT).
Throttle adaptif membaca `X-MBX-Used-Weight-1M` (limit 2400/menit).

Satu batch = satu `correlation_cluster_id`. Satu konfirmasi `y` untuk semua (jika pakai `--no-yes`).

### `--check-positions` — Monitor + place TP/SL

```bash
python3 futures_trade_executor.py --check-positions
python3 futures_trade_executor.py --check-positions --verbose
```

Yang dilakukan:
1. Query entry order status
2. FILLED → place TP (`TAKE_PROFIT_MARKET`) + SL (`STOP_MARKET`) via algo order
3. Algo order verified via `futures_get_algo_order()` (tanpa symbol filter — testnet bug)
4. TP/SL hit → resolve trade, hitung PnL + MAE/MFE, update Supabase
5. Price-guard: harga breach SL → cancel semua open algo order symbol itu + resolve SL_HIT
6. Cancel cleanup: `_cancel_all_open_algo_orders_for_sym()` pakai dua pass:
   - Pass 1: `/openAlgoOrders` (unfiltered, filter client-side)
   - Pass 2: `/openOrders` (regular exit orders dari kode lama)

### `--stats-futures`

```bash
python3 futures_trade_executor.py --stats-futures
```

---

## chart_analyzer.py — Chart Analysis (Read-only)

```bash
python3 chart_analyzer.py --symbol BTCUSDT
python3 chart_analyzer.py --symbol BTCUSDT --no-chart
python3 chart_analyzer.py --symbols BTCUSDT ETHUSDT SOLUSDT --no-chart
python3 chart_analyzer.py --scan-top 20 --no-chart
```

---

## ml/train_v1.py — ML Exploration (Spot Only)

```bash
python3 ml/train_v1.py
```

- Data: `trades_spot` (n=53, rule v1.0.0)
- Model: Logistic Regression
- Validasi: LOOCV (per-trade) + LOCO (per-cluster) — keduanya ditampilkan
- Output: accuracy, AUC, koefisien tiap fitur, simpan model ke `ml/models/v1.pkl`
- **Observasi saja** — TIDAK dipakai untuk keputusan trading

---

## dashboard.py — Monitoring Dashboard (Local)

```bash
streamlit run dashboard.py
```

- **Murni Event-Driven**: Menggunakan Supabase Realtime WebSockets. UI hanya re-render seketika saat menerima event `INSERT` atau `UPDATE` dari VM, tanpa polling periodik.
- **Tab**: 📈 Spot | ⚡ Futures | 📋 Open Positions | 🧪 ML Shadow Metrics
- **Header Live Indicators**: Menampilkan status koneksi WebSocket (🟢 Realtime Connected), status aktivitas VM bot (🟢 VM Active), dan timestamp event terakhir (Last Event: HH:MM:SS).
- **Manual Control**: Tombol `🔄 Refresh data` tersedia sebagai fallback. Tab Open Positions masih memiliki tombol untuk menjalankan `--check-positions` secara manual jika diperlukan.

---

## VM Oracle — 24/7 Live Trading Bot (Docker)

Bot saat ini berjalan 24/7 di VM Oracle (Singapore) menggunakan Docker, menggantikan sistem cron GitHub Actions lama.

### Arsitektur Deployment

```
VM Oracle (Docker Container)
  ├── Live Spot Executor Loop (dengan ML Shadow Scoring v2)
  ├── Live Futures Executor Loop (Terpisah secara independen)
           ↓ (INSERT/UPDATE realtime)
       Supabase (trades_spot, trades_futures)
           ↓ (WebSockets Push)
MacBook M1 Local (streamlit run dashboard.py) ← Live Monitoring
```

- **Containerization**: Menggunakan `Dockerfile` dan `docker-compose.yml`. Container dikonfigurasi dengan `restart: always`.
- **Entrypoint**: Container menjalankan loop `while true` (sleep/polling interval) untuk produksi 24/7, menghindari *restart-loop* (`exited with code 0`).
- **Pemisahan Pipeline**: Pipeline Spot dan Futures berjalan independen. Model ML v2 saat ini dikhususkan untuk Spot sebagai *Shadow Scoring* (pasif, tidak memblokir order) untuk mengumpulkan evaluasi metrik secara live.

### Menjalankan di VM

```bash
# Build dan jalankan di background
docker-compose up -d --build

# Cek log bot
docker-compose logs -f
```

---

## Quick Reference

### 1. Root Wrappers
*Executed from root directory.*

```bash
# ── Spot ──────────────────────────────────────────────────────
python3 paper_trade_executor.py --propose                  # single best
python3 paper_trade_executor.py --propose --symbol ETHUSDT
python3 paper_trade_executor.py --propose-all --dry-run    # preview batch
python3 paper_trade_executor.py --propose-all              # place batch
python3 paper_trade_executor.py --check-positions
python3 paper_trade_executor.py --check-positions --verbose
python3 paper_trade_executor.py --stats

# ── Futures ───────────────────────────────────────────────────
python3 futures_trade_executor.py --propose                # batch (isi sisa slot sampai 20)
python3 futures_trade_executor.py --propose --count 3
python3 futures_trade_executor.py --propose --side LONG
python3 futures_trade_executor.py --check-positions
python3 futures_trade_executor.py --check-positions --verbose
python3 futures_trade_executor.py --stats-futures

# ── Dashboard ─────────────────────────────────────────────────
python3 dashboard.py
# atau
streamlit run dashboard.py

# ── Chart analysis ────────────────────────────────────────────
python3 chart_analyzer.py --symbol BTCUSDT
python3 chart_analyzer.py --scan-top 30 --no-chart
```

### 2. Services
*Background services and listeners.*

```bash
# ── Position Listener ─────────────────────────────────────────
python3 services/position_listener.py --test-notify        # Test Telegram notification
python3 services/position_listener.py                      # Run listener in background
```

### 3. Tests & Scripts
*Testing, migrations, and machine learning scripts.*

```bash
# ── Tests ─────────────────────────────────────────────────────
python3 -m unittest discover tests/                        # Run all tests
python3 tests/test_check_positions_guard.py                # Run individual test

# ── Scripts ───────────────────────────────────────────────────
python3 scripts/migration_to_supabase.py                   # Run Supabase migration

# ── ML ────────────────────────────────────────────────────────
python3 ml/train_v1.py                                     # Train ML model v1
```
