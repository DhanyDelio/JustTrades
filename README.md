# Swing Trade — Semi-Automated Crypto Paper Trading System

Sistem trading swing berbasis Python untuk Binance Spot Testnet (simulasi uang virtual). Dirancang untuk akun kecil (~$12), fully manual workflow, dengan notifikasi Telegram.

---

## Untuk AI / Model yang Melanjutkan Development

**Baca bagian ini dulu sebelum menyentuh kode apapun.**

### Stack & File Utama

```
paper_trade_executor.py  → CORE. Semua logika order, OCO, monitoring.
chart_analyzer.py        → Analisis S/R zone, fib, SL/TP suggestion.
futures_trade_executor.py → Futures paper trading (TERPISAH dari spot).
position_listener.py     → WebSocket listener. TIDAK DIPAKAI (lihat §Listener).
dashboard.py             → Streamlit dashboard. Read-only dari trade_log.json.
swing_trade.py           → ML pipeline research. Terpisah dari trading.
trade_log.json           → Log spot trades. Jangan campur dengan futures.
trade_futures.json       → Log futures trades. Auto-generated saat pertama trade.
COMMANDS.md              → Quick reference semua command yang bisa dijalankan.
```

### Workflow Aktif Saat Ini (Manual)

```
1. python3 paper_trade_executor.py --propose-all
   → Scan coin, place batch limit orders, kirim notif Telegram per order

2. python3 paper_trade_executor.py --check-positions
   → Query exchange status, detect fill/TP/SL, kirim notif Telegram, update log
   → Jika entry baru FILLED: kirim "✅ Filled: XXXX long @ price | SL | TP"
   → Jika TP/SL hit: kirim "🟢 TP HIT / 🔴 SL HIT: XXXX +/- $PnL"

3. python3 paper_trade_executor.py --stats
   → Lihat win rate, expectancy, z-score

4. streamlit run dashboard.py
   → Dashboard visual (tidak menulis ke trade_log.json)
```

### Listener Status: DIMATIKAN

`position_listener.py` ditulis untuk WebSocket real-time detection, tapi **tidak digunakan** karena Binance testnet tidak reliable mengirim `executionReport` untuk OCO completion via WebSocket.

- Jangan jalankan `position_listener.py` sebagai background process
- Jangan tambahkan auto-propose trigger
- Semua notifikasi Telegram sekarang berjalan lewat `--check-positions` dan `--propose-all`

### Telegram Notifications

Dihandle oleh `_send_telegram()` di `paper_trade_executor.py`.
Konfigurasi di `.env`: `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`.

Notif dikirim saat:
- Order limit placed (dalam `--propose-all`)
- Entry order FILLED (detected di `--check-positions`)
- TP_HIT atau SL_HIT (detected di `--check-positions`)

### Hal-Hal Kritis yang Harus Diketahui

**OCO API:**
```python
# Gunakan ini (python-binance ≥1.0.37):
client.create_oco_order(
    symbol           = sym,
    side             = "SELL",
    quantity         = qty_str,
    aboveType        = "LIMIT_MAKER",   # TP leg
    abovePrice       = tp_str,
    belowType        = "STOP_LOSS_LIMIT",  # SL leg
    belowStopPrice   = sl_stop_str,
    belowPrice       = sl_limit_str,
    belowTimeInForce = "GTC",
)
# JANGAN gunakan format lama (price/stopPrice) — deprecated

# Cek status OCO:
client.v3_get_order_list(orderListId=trade["oco_list_id"])
# JANGAN gunakan get_oco_order() — tidak ada di python-binance 1.0.37
```

**Race condition OCO (sudah di-handle di `place_oco_order`):**
- Jika `current >= tp1` saat OCO placement → TP di-adjust ke `current * 1.003`
- Jika `current <= sl` saat OCO placement → market SELL langsung, log sebagai `SL_HIT`
- Retry 3x dengan fresh price setiap attempt

**Entry price logic:**
- Entry = `zone_center * (1 + 0.0015)` — bukan current price
- Zone harus ≥ 2× tested DAN ≥ 0.5× ATR di bawah current price
- SL di-recalculate dari `zone_low - 0.5 × ATR` setiap kali zone dipilih

**Tiered scanning (`gather_all_candidates`):**
- Part 1 = rank 1–30, Part 2 = rank 31–60, Part 3 = rank 61–90
- Lanjut ke Part 2 hanya jika candidates < `MIN_DESIRED_CANDIDATES` (default 5)
- Delay `SCAN_PART_DELAY_SEC` (default 8s) antar part
- Cek used weight sebelum tiap part, skip jika ≥ `RATE_LIMIT_WEIGHT_CEILING` (800)
- Setiap candidate punya field `scan_part` (1/2/3) yang tampil di batch table kolom `P`

**Filter simbol:**
```python
STABLECOIN_KEYWORDS   # USD-pegged: USDC, BUSD, dll
FIAT_KEYWORDS         # Non-USD fiat: EURT, GBPT, IDRT, dll
COMMODITY_RWA_KEYWORDS  # Gold/silver/commodity: PAXG, XAUT, dll
# + ATR proxy heuristic: pair dengan range harian < 0.1% di-exclude
```

**trade_log.json:**
- Jangan delete atau overwrite manual kecuali darurat
- `exit_status`: `OPEN` / `TP_HIT` / `SL_HIT` / `MANUALLY_CLOSED` / `CANCELED`
- `correlation_cluster_id`: null untuk single `--propose`, timestamp untuk `--propose-all` batch
- `oco_list_id`: null atau integer valid — jangan biarkan bernilai `-1` (broken state)

---

## Arsitektur

```
Binance Public Archive ──→ swing_trade.py
(data historis 2023-2025)    (walk-forward ML, riset signal)

Binance REST API ──→ chart_analyzer.py
(harga live, candles)   (deteksi S/R zone, fib, SL/TP)
                              ↓
                    paper_trade_executor.py
                    (scan → propose → confirm → order → OCO → monitor)
                              ↓
                       trade_log.json ──→ dashboard.py
                    (log semua trade)      (Streamlit viz)
                              ↓
                    Telegram notifications
                    (filled / TP / SL hit)
```

---

## File Structure

```
Swing_Trade/
├── paper_trade_executor.py  # Core executor — semua logika order
├── chart_analyzer.py        # Chart analysis, S/R zone detection
├── position_listener.py     # WebSocket listener (TIDAK AKTIF)
├── dashboard.py             # Streamlit dashboard (read-only)
├── swing_trade.py           # ML walk-forward pipeline (research)
├── COMMANDS.md              # Quick reference semua command
├── README.md                # File ini
├── .env                     # API keys + Telegram config (tidak di-commit)
├── .env.example             # Template .env
├── trade_log.json           # Log semua trade (auto-generated)
├── trade_log_lab_clean.json # Export ML-ready (dari --export-clean)
├── position_listener.log    # Log listener (referensi historis)
├── data_cache/              # Cache zip archive Binance (auto-generated)
└── charts/                  # Chart PNG output (auto-generated)
```

---

## Setup

### 1. Install dependencies

```bash
pip3 install pandas numpy scikit-learn python-binance python-dotenv requests mplfinance streamlit plotly websocket-client --break-system-packages
```

### 2. Konfigurasi `.env`

```bash
cp .env.example .env
```

Daftar API key di [testnet.binance.vision](https://testnet.binance.vision) (login GitHub → Generate HMAC_SHA256 Key):

```
BINANCE_TESTNET_API_KEY=isi_disini
BINANCE_TESTNET_API_SECRET=isi_disini
TELEGRAM_BOT_TOKEN=isi_disini
TELEGRAM_CHAT_ID=isi_disini
```

Test notif Telegram:
```bash
python3 position_listener.py --test-notify
```

---

## Parameter Penting

| Parameter | File | Default | Keterangan |
|-----------|------|---------|-----------|
| `BUDGET_USD` | executor | `12.0` | Hard cap single propose |
| `PER_TRADE_BUDGET` | executor | `12.0` | Budget per trade di --propose-all |
| `LAB_STARTING_CAPITAL` | executor | `240.0` | Modal awal lab pool |
| `RISK_FRACTION` | executor | `0.25` | Max loss per trade (25% of budget) |
| `DEFAULT_SCAN_N` | executor | `30` | Jumlah coin scan default |
| `PART_SIZE` | executor | `30` | Symbols per scan part |
| `MAX_PARTS` | executor | `3` | Max scan parts (top 90 total) |
| `MIN_DESIRED_CANDIDATES` | executor | `5` | Threshold lanjut ke part berikutnya |
| `SCAN_PART_DELAY_SEC` | executor | `8.0` | Delay antar scan parts |
| `RATE_LIMIT_WEIGHT_CEILING` | executor | `800` | Skip part jika weight ≥ ini |
| `MIN_RR` | analyzer | `1.5` | Minimum R:R untuk kandidat valid |
| `SL_ATR_BUFFER` | analyzer | `0.5` | SL = zone_low − 0.5 × ATR |
| `ZONE_ENTRY_BUFFER_PCT` | executor | `0.0015` | Entry = zone_center + 0.15% |
| `MIN_ATR_PCT` | analyzer | `0.1` | Filter stablecoin by ATR proxy |
| `RULE_VERSION` | executor | `"v1.0.0"` | Bump manual saat parameter berubah |

---

## Trade Log Schema

```json
{
  "symbol": "ETHUSDT",
  "direction": "long",
  "correlation_cluster_id": "20260704_093428",
  "entry_order_id": 5019044,
  "entry_status": "FILLED",
  "entry_price": 1699.18,
  "entry_fill_price": 1699.20,
  "entry_qty": 0.007,
  "entry_notional": 11.89,
  "oco_placed": true,
  "oco_list_id": 367290,
  "sl": 1689.71,
  "tp1": 1779.90,
  "tp2": null,
  "exit_status": "TP_HIT",
  "exit_price": 1779.90,
  "realized_pnl_usd": 0.5650,
  "realized_pnl_pct": 4.75,
  "planned_rr": 1.86,
  "risk_pct": 0.56,
  "zone_touches": 4,
  "zone_type": "T1",
  "atr_pct_at_entry": 1.88,
  "rule_version": "v1.0.0",
  "fee_usd_roundtrip": 0.0238,
  "time_to_resolution_sec": 14400
}
```

`exit_status` values: `OPEN` / `TP_HIT` / `SL_HIT` / `MANUALLY_CLOSED` / `CANCELED`

---

## Keterbatasan

- **Spot only, LONG only** — tidak ada short dari akun USDT murni
- **Testnet** — harga real, tapi order book tipis, OCO WebSocket events tidak reliable
- **Budget $12/trade** — beberapa pair minimum notional $5–10, tidak semua kandidat lolos
- **ML belum punya edge** — paper trading ini untuk validasi eksekusi dan data collection
- **Check-positions manual** — tidak ada polling otomatis, kamu yang jalankan sendiri
