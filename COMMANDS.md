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
# Default: up to 2 posisi
python3 futures_trade_executor.py --propose

# Tentukan jumlah
python3 futures_trade_executor.py --propose --count 3

# Filter side
python3 futures_trade_executor.py --propose --side LONG
python3 futures_trade_executor.py --propose --side SHORT

# Non-interaktif (CI)
python3 futures_trade_executor.py --propose --yes
```

Scan tiered — selalu jalan 4 parts × 25 = **100 coin** (LONG + SHORT).
Throttle adaptif membaca `X-MBX-Used-Weight-1M` (limit 2400/menit).

Satu batch = satu `correlation_cluster_id`. Satu konfirmasi `y` untuk semua.

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

## dashboard.py — Monitoring Dashboard

```bash
streamlit run dashboard.py
```

- Auto-refresh tiap 5 menit
- Tab: Spot analysis | Futures analysis | Open Positions
- Open Positions: tombol **📈 Check Spot** dan **⚡ Check Futures** untuk jalankan
  `--check-positions` langsung dari browser
- Header: timestamp last refresh + countdown ke refresh berikutnya

---

## GitHub Actions — Automated Hourly Trading

Bot jalan otomatis tiap jam via `.github/workflows/hourly-trade.yml`.

### Arsitektur

```
GitHub Actions (cron tiap 1 jam, self-hosted runner di Mac)
  ├── paper_trade_executor.py --check-positions
  ├── futures_trade_executor.py --check-positions
  ├── paper_trade_executor.py --propose-all --yes
  └── futures_trade_executor.py --propose --yes
           ↓
      Supabase (trades_spot + trades_futures)
           ↑
  streamlit run dashboard.py   ← monitoring (read-only)
```

### GitHub Secrets (Settings → Secrets and variables → Actions)

| Secret | Sumber |
|--------|--------|
| `BINANCE_TESTNET_API_KEY` | testnet.binance.vision |
| `BINANCE_TESTNET_API_SECRET` | testnet.binance.vision |
| `BINANCE_FUTURES_TESTNET_API_KEY` | testnet.binancefuture.com |
| `BINANCE_FUTURES_TESTNET_API_SECRET` | testnet.binancefuture.com |
| `SUPABASE_URL` | Supabase → Settings → API |
| `SUPABASE_SERVICE_KEY` | Supabase → service_role key |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | @userinfobot |

### Trigger manual

GitHub → Actions → **Hourly Trade Bot** → **Run workflow**

---

## Quick Reference

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
python3 futures_trade_executor.py --propose                # batch (default 2)
python3 futures_trade_executor.py --propose --count 3
python3 futures_trade_executor.py --propose --side LONG
python3 futures_trade_executor.py --check-positions
python3 futures_trade_executor.py --check-positions --verbose
python3 futures_trade_executor.py --stats-futures

# ── Chart analysis ────────────────────────────────────────────
python3 chart_analyzer.py --symbol BTCUSDT
python3 chart_analyzer.py --scan-top 30 --no-chart

# ── ML ────────────────────────────────────────────────────────
python3 ml/train_v1.py

# ── Dashboard ─────────────────────────────────────────────────
streamlit run dashboard.py

# ── Telegram test ─────────────────────────────────────────────
python3 position_listener.py --test-notify
```
