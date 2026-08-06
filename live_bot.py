"""
live_bot.py — Production 24/7 Polling Loop
============================================
Runs Spot and Futures trading pipelines on a 1-hour interval.

Architecture:
  ┌─────────────────────────────────────────────────────┐
  │  SPOT PIPELINE (with ML v2 Shadow Scoring)          │
  │  - check-positions → propose-all --yes              │
  │  - ML v2 scores every candidate (observation only)  │
  │  - Scores logged to Supabase, NO veto/reject logic  │
  ├─────────────────────────────────────────────────────┤
  │  FUTURES PIPELINE (independent, NO ML scoring)      │
  │  - check-positions → propose                        │
  │  - Pure rule-based, no ML model dependency          │
  │  - Will get its own ML model when Effective N > 100 │
  └─────────────────────────────────────────────────────┘

Entry point for Docker: runs forever, never exits.

Exchange maintenance handling:
  - Executors exit with code 2 when Binance testnet is unavailable (502/timeout).
  - live_bot.py detects rc=2, marks pipeline as SKIPPED (MAINTENANCE).
  - A single Telegram alert is sent per cycle if any exchange is down.
  - Heartbeat is always sent regardless of exchange status.
  - Bot never crashes or spams retries — it sleeps until the next cycle.
"""

import os
import time
import subprocess
import sys
from datetime import datetime, timezone, timedelta
import pytz

# Interval: 1 hour (matches previous GitHub Actions cron schedule)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 3600))

# ── Pipeline flags ────────────────────────────────────────────────
# Set via environment to disable one pipeline without rebuilding
ENABLE_SPOT    = os.environ.get("ENABLE_SPOT", "1") == "1"
ENABLE_FUTURES = os.environ.get("ENABLE_FUTURES", "1") == "1"

# Exit code produced by executors when exchange is down (maintenance/outage)
_RC_MAINTENANCE = 2

# ── Pipeline result constants ─────────────────────────────────────
_STATUS_SUCCESS     = "SUCCESS"
_STATUS_MAINTENANCE = "SKIPPED (Maintenance)"
_STATUS_ERROR       = "ERROR"


def _run_step(cmd: list[str]) -> str:
    """
    Run a single executor step and return its pipeline status string.

    Returns:
        _STATUS_SUCCESS      — rc == 0
        _STATUS_MAINTENANCE  — rc == 2 (exchange outage)
        _STATUS_ERROR        — any other non-zero rc
    """
    result = subprocess.run(cmd)
    if result.returncode == 0:
        return _STATUS_SUCCESS
    if result.returncode == _RC_MAINTENANCE:
        return _STATUS_MAINTENANCE
    return _STATUS_ERROR


def run_spot_pipeline() -> str:
    """
    Spot pipeline — includes ML v2 Shadow Scoring.
    Returns pipeline status string for cycle summary.
    """
    print(f"\n{'=' * 50}", flush=True)
    print("  SPOT PIPELINE", flush=True)
    print(f"{'=' * 50}", flush=True)

    print("[Spot 1/2] Checking Spot Positions...", flush=True)
    status = _run_step([
        sys.executable,
        "paper_trade_executor.py",
        "--check-positions",
        "--recover-unprotected",
    ])
    if status == _STATUS_MAINTENANCE:
        print("⚠️ Binance Spot Testnet unavailable. Skipping Spot pipeline.", flush=True)
        return _STATUS_MAINTENANCE

    print("[Spot 2/2] Proposing New Spot Trades + Shadow Scoring...", flush=True)
    step2 = _run_step([sys.executable, "paper_trade_executor.py", "--propose-all", "--yes"])
    if step2 == _STATUS_MAINTENANCE:
        print("⚠️ Binance Spot Testnet unavailable on propose step.", flush=True)
        return _STATUS_MAINTENANCE

    return _STATUS_SUCCESS if step2 == _STATUS_SUCCESS else _STATUS_ERROR


def run_futures_pipeline() -> str:
    """
    Futures pipeline — fully independent, NO ML scoring.
    Returns pipeline status string for cycle summary.
    """
    print(f"\n{'=' * 50}", flush=True)
    print("  FUTURES PIPELINE", flush=True)
    print(f"{'=' * 50}", flush=True)

    print("[Futures 1/2] Checking Futures Positions...", flush=True)
    status = _run_step([sys.executable, "futures_trade_executor.py", "--check-positions"])
    if status == _STATUS_MAINTENANCE:
        print("⚠️ Binance Futures Testnet unavailable. Skipping Futures pipeline.", flush=True)
        return _STATUS_MAINTENANCE

    print("[Futures 2/2] Proposing New Futures Trades...", flush=True)
    step2 = _run_step([sys.executable, "futures_trade_executor.py", "--propose"])
    if step2 == _STATUS_MAINTENANCE:
        print("⚠️ Binance Futures Testnet unavailable on propose step.", flush=True)
        return _STATUS_MAINTENANCE

    return _STATUS_SUCCESS if step2 == _STATUS_SUCCESS else _STATUS_ERROR


def _send_outage_telegram(spot_status: str, futures_status: str) -> None:
    """
    Kirim SATU Telegram alert kalau ada pipeline yang kena maintenance.
    Tidak dipanggil kalau keduanya sukses.
    """
    try:
        from core.utils.telegram import send_telegram
    except ImportError:
        return

    lines = ["⚠️ Exchange Alert"]

    if spot_status == _STATUS_MAINTENANCE:
        lines.append("")
        lines.append("Spot Testnet:")
        lines.append("  Tidak tersedia (maintenance / outage)")
        lines.append("  Trading Spot dilewati.")

    if futures_status == _STATUS_MAINTENANCE:
        lines.append("")
        lines.append("Futures Testnet:")
        lines.append("  Tidak tersedia (maintenance / outage)")
        lines.append("  Trading Futures dilewati.")

    if spot_status == _STATUS_MAINTENANCE and futures_status == _STATUS_MAINTENANCE:
        lines.append("")
        lines.append("Semua trading dilewati.")
    else:
        lines.append("")
        lines.append("Pipeline lain tetap berjalan normal.")

    lines.append("")
    lines.append("Bot tetap hidup dan akan mencoba lagi pada cycle berikutnya.")
    send_telegram("\n".join(lines))


def run_cycle() -> float:
    wib = pytz.timezone("Asia/Jakarta")
    now_utc = datetime.now(timezone.utc)
    now_wib = now_utc.astimezone(wib)
    timestamp = now_wib.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 60}", flush=True)
    print(f"[{timestamp} WIB] Starting Trading Cycle", flush=True)
    print(f"{'=' * 60}\n", flush=True)

    spot_status    = _STATUS_SUCCESS
    futures_status = _STATUS_SUCCESS

    if ENABLE_SPOT:
        spot_status = run_spot_pipeline()
    else:
        print("[SKIP] Spot pipeline disabled (ENABLE_SPOT=0)", flush=True)
        spot_status = "DISABLED"

    if ENABLE_FUTURES:
        futures_status = run_futures_pipeline()
    else:
        print("[SKIP] Futures pipeline disabled (ENABLE_FUTURES=0)", flush=True)
        futures_status = "DISABLED"

    # ── Kirim Telegram alert jika ada outage (sekali per cycle) ──────
    any_maintenance = (
        spot_status    == _STATUS_MAINTENANCE or
        futures_status == _STATUS_MAINTENANCE
    )
    if any_maintenance:
        _send_outage_telegram(spot_status, futures_status)

    # ── Heartbeat — selalu dikirim, meski exchange down ───────────────
    completed_utc = datetime.now(timezone.utc)
    completed_wib = completed_utc.astimezone(wib)

    next_hour_wib = (completed_wib + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    sleep_secs = max(0, (next_hour_wib - completed_wib).total_seconds())

    # Tentukan bot_status untuk heartbeat
    if spot_status == _STATUS_MAINTENANCE and futures_status == _STATUS_MAINTENANCE:
        bot_status = "EXCHANGE_DOWN"
    elif spot_status == _STATUS_MAINTENANCE:
        bot_status = "SPOT_MAINTENANCE"
    elif futures_status == _STATUS_MAINTENANCE:
        bot_status = "FUTURES_MAINTENANCE"
    else:
        bot_status = "ONLINE"

    print(f"\n--- Cycle complete. Sending Heartbeat ---", flush=True)
    try:
        from services.supabase_client import upsert_heartbeat, send_heartbeat
        upsert_heartbeat(
            last_seen_at=completed_utc.isoformat(),
            next_expected_at=next_hour_wib.isoformat(),
        )
        send_heartbeat()
        print(
            f"💓 [HEARTBEAT] last_seen={completed_wib.strftime('%H:%M:%S')} WIB  "
            f"next_expected={next_hour_wib.strftime('%H:%M:%S')} WIB  "
            f"status={bot_status}",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ [HEARTBEAT] Failed: {e}", flush=True)

    # ── Cycle summary ─────────────────────────────────────────────────
    print(f"\n{'=' * 50}", flush=True)
    print("  Cycle Summary", flush=True)
    print(f"{'=' * 50}", flush=True)
    print(f"  Spot     : {spot_status}", flush=True)
    print(f"  Futures  : {futures_status}", flush=True)
    print(f"  Heartbeat: SENT  (status={bot_status})", flush=True)
    print(f"{'=' * 50}", flush=True)

    print(
        f"\n[CYCLE COMPLETE] Completed at {completed_wib.strftime('%H:%M:%S')} WIB. "
        f"Sleeping {sleep_secs:.0f}s. "
        f"Next run scheduled at {next_hour_wib.strftime('%H:%M:%S')} WIB.",
        flush=True,
    )
    return sleep_secs


def main():
    print("=" * 60, flush=True)
    print("  Live Trading Bot — Production Mode (24/7)", flush=True)
    print(f"  Spot Pipeline:    {'ENABLED (ML v2 Shadow)' if ENABLE_SPOT else 'DISABLED'}", flush=True)
    print(f"  Futures Pipeline: {'ENABLED (Rule-Based)' if ENABLE_FUTURES else 'DISABLED'}", flush=True)
    print(f"  Sleep mode:       Aligned to top of next hour (WIB)", flush=True)
    print(f"  Outage handling:  rc=2 → skip pipeline, Telegram alert, heartbeat sent", flush=True)
    print("=" * 60, flush=True)

    while True:
        sleep_secs = POLL_INTERVAL  # fallback if run_cycle throws
        try:
            sleep_secs = run_cycle()
        except Exception as e:
            print(f"[ERROR] Cycle failed: {e}", flush=True)

        time.sleep(max(sleep_secs, 60))  # never sleep less than 60s


if __name__ == "__main__":
    main()
