"""
tokocrypto_executor.py — Tokocrypto IDR Spot Trading Entry Point
=================================================================
Called by live_bot.py as a subprocess, same pattern as
paper_trade_executor.py (Binance testnet) and futures_trade_executor.py.

Usage:
    python3 tokocrypto_executor.py --check-positions
    python3 tokocrypto_executor.py --propose

Exit codes:
    0 — success (or no action needed)
    1 — recoverable error (logged, cycle continues)
    2 — exchange unavailable / network error (live_bot.py skips pipeline)

Configuration (env vars):
    ENABLE_TOKO         — "1" to enable (default "0" — must opt in)
    TOKO_MAX_POSITIONS  — max concurrent open positions (default 5)
    TOKO_BUDGET_IDR     — total capital in IDR (default 196000)
    TOKO_TRADING_PHASE  — "PHASE_2" / "PHASE_3" (default "PHASE_3")
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_POSITIONS     = int(os.environ.get("TOKO_MAX_POSITIONS", "5"))
BUDGET_IDR        = float(os.environ.get("TOKO_BUDGET_IDR", "196000"))
TRADING_PHASE     = os.environ.get("TOKO_TRADING_PHASE", "PHASE_3")
SUPERVISED        = TRADING_PHASE == "PHASE_2"   # Phase 3 = fully automated

# Adaptive SL buffer — wider buffer for lower-priced / thinner-volume IDR pairs.
# Applied as sl_limit = sl_stop * (1 - SL_BUFFER_PCT) so fill price has room below trigger.
# The buffer is selected per-trade based on entry price per unit.
SL_BUFFER_TIERS = [
    (100_000, 0.0015),   # > Rp 100,000/unit → 0.15% (liquid: BTC, ETH, BNB, SOL)
    (1_000,   0.0030),   # Rp 1,000–100,000/unit → 0.30% (mid: XRP, ADA, AVAX...)
    (0,       0.0050),   # < Rp 1,000/unit → 0.50% (thin: DOGE, ZIL, HBAR, POL...)
]


def _sl_buffer_pct(entry_price_idr: float) -> float:
    """Return the appropriate SL limit buffer % for this entry price."""
    for threshold, buf in SL_BUFFER_TIERS:
        if entry_price_idr > threshold:
            return buf
    return 0.0050


def _build_client():
    """Build and return a TokocryptoClient."""
    from core.clients.tokocrypto_client import TokocryptoClient
    return TokocryptoClient.build()


def _build_executor(client):
    """Build and return a TokocryptoOrderExecutor."""
    from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
    return TokocryptoOrderExecutor(client, supervised=SUPERVISED, dry_run=False)


def _build_monitor(client, executor):
    """Build and return a TokocryptoPositionMonitor."""
    from core.executors.tokocrypto_position_monitor import TokocryptoPositionMonitor
    return TokocryptoPositionMonitor(client, executor)


def _build_scanner(client):
    """Build and return a TokocryptoCandidateScanner."""
    from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
    return TokocryptoCandidateScanner(client)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def cmd_check_positions() -> None:
    """
    Step 1: Check and update all open Tokocrypto positions.
    - Queries OCO leg statuses via two-leg state machine
    - Resolves TP_HIT / SL_HIT, records actual fill price (never ticker)
    - Handles anomalies (CRITICAL_ANOMALY, STUCK_COUNTERPART, etc.)
    - Sends Telegram alerts via _send_toko_telegram
    """
    print(f"\n{'='*50}", flush=True)
    print("  TOKOCRYPTO — CHECK POSITIONS", flush=True)
    print(f"{'='*50}", flush=True)

    try:
        client   = _build_client()
        executor = _build_executor(client)
        monitor  = _build_monitor(client, executor)
        # Patch SL buffer into executor before monitoring
        executor._sl_buffer_pct_fn = _sl_buffer_pct
        monitor.check_positions(verbose=True)
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["connection", "timeout", "network", "ssl", "5xx", "503", "502"]):
            print(f"⚠️ [TOKO] Exchange unavailable: {e}", flush=True)
            sys.exit(2)
        print(f"⚠️ [TOKO] check_positions error: {e}", flush=True)
        sys.exit(1)


def cmd_propose() -> None:
    """
    Step 2: Scan all 29 IDR pairs, find T1 setups, propose/execute entries.
    - Scans Binance kline data (same chart_analyzer as Binance testnet)
    - Converts prices to IDR via live USDT_IDR rate
    - Same T1/rr_clears/no_tp_in_range filtering as testnet
    - Orders up to (MAX_POSITIONS - open_positions) new slots
    - In PHASE_3 (default): fully automated, no y/n prompt
    - In PHASE_2: supervised mode, requires terminal confirmation
    """
    print(f"\n{'='*50}", flush=True)
    print("  TOKOCRYPTO — PROPOSE NEW TRADES", flush=True)
    print(f"  Phase: {TRADING_PHASE}  Max slots: {MAX_POSITIONS}  Budget: Rp {BUDGET_IDR:,.0f}", flush=True)
    print(f"{'='*50}", flush=True)

    try:
        client   = _build_client()
        executor = _build_executor(client)
        scanner  = _build_scanner(client)

        # Patch adaptive SL buffer into executor
        executor._sl_buffer_pct_fn = _sl_buffer_pct

        # How many open positions do we already have?
        from services.supabase_client import fetch_all_tokocrypto
        open_trades = [t for t in fetch_all_tokocrypto() if t.get("exit_status") == "OPEN"]
        n_open = len(open_trades)
        slots_available = max(0, MAX_POSITIONS - n_open)

        print(f"  Open positions: {n_open} / {MAX_POSITIONS}  |  Slots available: {slots_available}", flush=True)

        if slots_available == 0:
            print("  All slots occupied — skipping scan.", flush=True)
            return

        # Scan — get ALL candidates (up to slots_available), sorted by score
        candidates = scanner.gather_candidates(max_positions=slots_available)
        if not candidates:
            print("  No T1 candidates found this cycle.", flush=True)
            return

        # Fetch live IDR balance — this is the actual budget we can spend
        try:
            bals = client.get_balances()
            idr_bal = next((b.free for b in bals if b.asset == "IDR"), 0.0)
        except Exception:
            idr_bal = BUDGET_IDR  # fallback to configured budget

        print(f"  Available IDR balance: Rp {idr_bal:,.0f}", flush=True)

        if idr_bal < 20_000:
            print(f"  Insufficient IDR balance (Rp {idr_bal:,.0f} < min Rp 20,000). Skipping.", flush=True)
            return

        print(f"  Candidates found: {len(candidates)}", flush=True)

        # Flexible slot sizing — use actual available IDR, not fixed slot_size.
        # For each candidate: try to size an order using available IDR.
        # If IDR is enough for min notional → order, deduct from available.
        # If not enough → skip this candidate, try next.
        # This means: if one coin needs Rp 200,000 and we only have Rp 39,200,
        # we skip it and try the next cheaper coin instead.
        filled = 0
        remaining_idr = idr_bal

        for cand in candidates:
            if filled >= slots_available:
                break
            if remaining_idr < 20_000:
                print(f"  Remaining IDR Rp {remaining_idr:,.0f} below min — stopping.", flush=True)
                break

            # Size using remaining IDR (not fixed slot_size)
            best = scanner.pick_best_candidate([cand], available_idr=remaining_idr)
            if best is None:
                continue

            # Apply adaptive SL buffer
            entry  = best["entry_price"]
            sl_buf = _sl_buffer_pct(entry)
            sl_stop  = best["sl"]
            sl_limit = round(sl_stop * (1 - sl_buf), 10)
            best["sl_stop_price"]  = sl_stop
            best["sl_limit_price"] = sl_limit
            best["sl_buffer_pct"]  = sl_buf * 100

            notional = best.get("sizing", {}).get("notional_idr", entry * best.get("sizing", {}).get("qty", 0))
            print(
                f"\n  [{best['symbol']}]  Entry: Rp {entry:,.2f}  "
                f"SL: Rp {sl_stop:,.2f} (buf {sl_buf*100:.2f}%)  "
                f"TP: Rp {best['tp1']:,.2f}  R:R {best['rr']:.2f}  "
                f"risk {best['risk_pct']:.2f}%  notional: Rp {notional:,.0f}",
                flush=True,
            )

            result = executor.execute_entry(best, notional)
            if result is not None:
                filled += 1
                remaining_idr -= notional   # deduct actual order cost from available
                print(f"  ✅ Entry placed: {best['symbol']}  orderId={result.get('orderId') or result.get('data',{}).get('orderId','?')}  remaining IDR: Rp {remaining_idr:,.0f}", flush=True)
            else:
                print(f"  ⚠ Entry skipped / failed: {best['symbol']}", flush=True)

        print(f"\n  Propose complete. New entries this cycle: {filled}", flush=True)

    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["connection", "timeout", "network", "ssl", "5xx", "503", "502"]):
            print(f"⚠️ [TOKO] Exchange unavailable: {e}", flush=True)
            sys.exit(2)
        print(f"⚠️ [TOKO] propose error: {e}", flush=True)
        import traceback; traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Tokocrypto IDR Spot Executor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-positions", action="store_true",
                       help="Check and update all open Tokocrypto positions")
    group.add_argument("--propose", action="store_true",
                       help="Scan IDR pairs and propose/execute new entry orders")
    args = parser.parse_args()

    if args.check_positions:
        cmd_check_positions()
    elif args.propose:
        cmd_propose()


if __name__ == "__main__":
    main()
