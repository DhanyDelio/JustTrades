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


def run_spot_pipeline():
    """
    Spot pipeline — includes ML v2 Shadow Scoring.
    ML model: ml/models/v2.pkl (Spot-only, trained on trades_spot).
    Scoring is passive: logged to Supabase but never vetoes trades.
    """
    print("─── SPOT PIPELINE (ML v2 Shadow Scoring Active) ───", flush=True)

    print("[Spot 1/2] Checking Spot Positions...", flush=True)
    subprocess.run([sys.executable, "paper_trade_executor.py", "--check-positions"])

    print("[Spot 2/2] Proposing New Spot Trades + Shadow Scoring...", flush=True)
    subprocess.run([sys.executable, "paper_trade_executor.py", "--propose-all", "--yes"])


def run_futures_pipeline():
    """
    Futures pipeline — fully independent, NO ML scoring.
    Uses pure rule-based selection only.
    A dedicated Futures ML model will be trained separately
    once Effective N (closed futures trades) reaches ~100+.
    """
    print("─── FUTURES PIPELINE (Rule-Based, No ML) ───", flush=True)

    print("[Futures 1/2] Checking Futures Positions...", flush=True)
    subprocess.run([sys.executable, "futures_trade_executor.py", "--check-positions"])

    print("[Futures 2/2] Proposing New Futures Trades...", flush=True)
    subprocess.run([sys.executable, "futures_trade_executor.py", "--propose"])


def run_cycle():
    wib = pytz.timezone("Asia/Jakarta")
    now_utc = datetime.now(timezone.utc)
    now_wib = now_utc.astimezone(wib)
    timestamp = now_wib.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 60}", flush=True)
    print(f"[{timestamp} WIB] Starting Trading Cycle", flush=True)
    print(f"{'=' * 60}\n", flush=True)

    if ENABLE_SPOT:
        run_spot_pipeline()
    else:
        print("[SKIP] Spot pipeline disabled (ENABLE_SPOT=0)", flush=True)

    if ENABLE_FUTURES:
        run_futures_pipeline()
    else:
        print("[SKIP] Futures pipeline disabled (ENABLE_FUTURES=0)", flush=True)

    # ── Heartbeat + aligned sleep ─────────────────────────────────────
    completed_utc = datetime.now(timezone.utc)
    completed_wib = completed_utc.astimezone(wib)

    # Calculate top of NEXT hour (XX:00:00 WIB)
    next_hour_wib = (completed_wib + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )

    # Formula requested by architecture spec:
    # sleep_seconds = (next_hour_00_00 - current_time).total_seconds()
    sleep_secs = max(0, (next_hour_wib - completed_wib).total_seconds())

    # Upsert heartbeat with promised next cycle time
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
            f"next_expected={next_hour_wib.strftime('%H:%M:%S')} WIB",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ [HEARTBEAT] Failed: {e}", flush=True)

    # Aligned sleep — wake exactly at top of next hour
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
