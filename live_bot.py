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
from datetime import datetime

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
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 60}", flush=True)
    print(f"[{timestamp}] Starting Trading Cycle", flush=True)
    print(f"{'=' * 60}\n", flush=True)

    if ENABLE_SPOT:
        run_spot_pipeline()
    else:
        print("[SKIP] Spot pipeline disabled (ENABLE_SPOT=0)", flush=True)

    if ENABLE_FUTURES:
        run_futures_pipeline()
    else:
        print("[SKIP] Futures pipeline disabled (ENABLE_FUTURES=0)", flush=True)

    print(f"\n--- Cycle complete. Sleeping {POLL_INTERVAL}s ---", flush=True)


def main():
    print("=" * 60, flush=True)
    print("  Live Trading Bot — Production Mode (24/7)", flush=True)
    print(f"  Spot Pipeline:    {'ENABLED (ML v2 Shadow)' if ENABLE_SPOT else 'DISABLED'}", flush=True)
    print(f"  Futures Pipeline: {'ENABLED (Rule-Based)' if ENABLE_FUTURES else 'DISABLED'}", flush=True)
    print(f"  Poll Interval:    {POLL_INTERVAL}s", flush=True)
    print("=" * 60, flush=True)

    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"[ERROR] Cycle failed: {e}", flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
