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
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_POSITIONS = int(os.environ.get("TOKO_MAX_POSITIONS", "5"))
BUDGET_IDR = float(os.environ.get("TOKO_BUDGET_IDR", "196000"))
TRADING_PHASE = os.environ.get("TOKO_TRADING_PHASE", "PHASE_3")
SUPERVISED = TRADING_PHASE == "PHASE_2"  # Phase 3 = fully automated

# Adaptive SL buffer — wider buffer for lower-priced / thinner-volume IDR pairs.
# Applied as sl_limit = sl_stop * (1 - SL_BUFFER_PCT) so fill price has room below trigger.
# The buffer is selected per-trade based on entry price per unit.
SL_BUFFER_TIERS = [
    (100_000, 0.0015),  # > Rp 100,000/unit → 0.15% (liquid: BTC, ETH, BNB, SOL)
    (1_000, 0.0030),  # Rp 1,000–100,000/unit → 0.30% (mid: XRP, ADA, AVAX...)
    (0, 0.0050),  # < Rp 1,000/unit → 0.50% (thin: DOGE, ZIL, HBAR, POL...)
]


MIN_NOTIONAL_IDR = 20_000.0
_PROPOSE_LOCK = threading.Lock()


def calculate_available_slots(
    open_count: int, max_positions: int = MAX_POSITIONS
) -> int:
    """
    Calculate remaining slots available for new positions.
    Guarantees non-negative result even if open_count exceeds max_positions.
    """
    return max(0, max_positions - max(0, open_count))


def calculate_new_order_allocation(
    wallet_balance: float, slots: int = MAX_POSITIONS
) -> float:
    """
    Calculate dynamic allocation per new order given wallet balance and slot divisor.
    Formula: allocation = wallet_balance / slots

    Guarantees:
      - Returns 0.0 if wallet_balance <= 0 or slots <= 0
      - Does not return negative, NaN, or Infinity
    """
    if wallet_balance is None or slots is None:
        return 0.0
    try:
        w = float(wallet_balance)
        m = int(slots)
    except (TypeError, ValueError):
        return 0.0

    if w <= 0.0 or m <= 0 or math.isnan(w) or math.isinf(w):
        return 0.0

    return w / m


def calculate_adaptive_allocation(
    wallet_balance: float,
    available_slots: int,
    min_notional: float = MIN_NOTIONAL_IDR,
) -> tuple[int, float]:
    """
    Calculate adaptive (target_slots, allocation_per_slot) based on free balance,
    available slots, and minimum notional.

    Logic:
      1. If wallet_balance <= 0 or available_slots <= 0, return (0, 0.0).
      2. Step down target_slots from available_slots down to 1:
         alloc = calculate_new_order_allocation(wallet_balance, target_slots)
         If alloc >= min_notional:
             return target_slots, alloc
      3. If even 1 slot cannot meet min_notional (wallet_balance < min_notional):
         return 0, 0.0 (NO ENTRY).
    """
    if wallet_balance is None or available_slots is None:
        return 0, 0.0
    try:
        w = float(wallet_balance)
        s = int(available_slots)
        m = float(min_notional)
    except (TypeError, ValueError):
        return 0, 0.0

    if w <= 0.0 or s <= 0 or math.isnan(w) or math.isinf(w):
        return 0, 0.0

    for target in range(s, 0, -1):
        alloc = calculate_new_order_allocation(w, target)
        if alloc >= m:
            return target, alloc

    return 0, 0.0


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

    return TokocryptoOrderExecutor(
        client,
        supervised=SUPERVISED,
        trading_phase=TRADING_PHASE,
        dry_run=False,
        max_slots=MAX_POSITIONS,
    )


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
        client = _build_client()
        executor = _build_executor(client)
        monitor = _build_monitor(client, executor)
        # Patch SL buffer into executor before monitoring
        executor._sl_buffer_pct_fn = _sl_buffer_pct
        monitor.check_positions(verbose=True)
    except Exception as e:
        err = str(e).lower()
        if any(
            x in err
            for x in ["connection", "timeout", "network", "ssl", "5xx", "503", "502"]
        ):
            print(f"⚠️ [TOKO] Exchange unavailable: {e}", flush=True)
            sys.exit(2)
        print(f"⚠️ [TOKO] check_positions error: {e}", flush=True)
        sys.exit(1)


def cmd_propose() -> None:
    """Serialize slot and IDR accounting between propose threads in this process."""
    if not _PROPOSE_LOCK.acquire(blocking=False):
        print(
            "  [GUARD] A propose cycle is already running in this process; skipping.",
            flush=True,
        )
        return
    try:
        _cmd_propose_locked()
    finally:
        _PROPOSE_LOCK.release()


def _cmd_propose_locked() -> None:
    """
    Step 2: Scan IDR pairs, find T1 setups, propose/execute entries.
    - Sizing: dynamic based on live wallet balance from API (current_wallet_balance / MAX_OPEN_POSITIONS)
    - Orders up to (MAX_POSITIONS - open_positions) new slots
    - In PHASE_3 (default): fully automated, no y/n prompt
    - In PHASE_2: supervised mode, requires terminal confirmation
    """
    print(f"\n{'='*50}", flush=True)
    print("  TOKOCRYPTO — PROPOSE NEW TRADES", flush=True)
    print(f"  Phase: {TRADING_PHASE}  Max slots: {MAX_POSITIONS}", flush=True)
    print(f"{'='*50}", flush=True)

    try:
        client = _build_client()
        executor = _build_executor(client)
        scanner = _build_scanner(client)

        # Patch adaptive SL buffer into executor
        executor._sl_buffer_pct_fn = _sl_buffer_pct

        # How many open positions do we already have?
        from services.supabase_client import fetch_all_tokocrypto_strict

        open_trades = [
            t
            for t in (fetch_all_tokocrypto_strict() or [])
            if t.get("exit_status") == "OPEN"
            and str(t.get("entry_status", "")).upper()
            not in ("CANCELED", "REJECTED", "EXPIRED")
        ]
        open_symbols = {t.get("symbol") for t in open_trades if t.get("symbol")}

        # Include any working orders on exchange that might not yet be recorded
        try:
            exchange_open = client.get_open_orders()
            for o in exchange_open or []:
                sym_ex = o.get("symbol")
                if sym_ex:
                    open_symbols.add(sym_ex)
        except Exception as exc:
            print(f"  [WARN] Could not fetch exchange open orders: {exc}", flush=True)
            return

        n_open = max(len(open_trades), len(open_symbols))
        slots_available = calculate_available_slots(n_open, MAX_POSITIONS)

        print(
            f"  Open positions: {n_open} / {MAX_POSITIONS}  |  Slots available: {slots_available}",
            flush=True,
        )

        if slots_available == 0:
            print("  All slots occupied — skipping scan.", flush=True)
            return

        # Fetch live IDR balance from API (source of truth)
        try:
            balance_obj = client.get_balance("IDR")
            idr_bal = float(balance_obj.free) if balance_obj else 0.0
        except Exception as e:
            print(
                f"⚠️ [TOKO] Failed to fetch live IDR wallet balance from API: {e}. Skipping propose.",
                flush=True,
            )
            return

        target_slots, alloc_per_order = calculate_adaptive_allocation(
            wallet_balance=idr_bal,
            available_slots=slots_available,
            min_notional=MIN_NOTIONAL_IDR,
        )

        # Runtime verification log
        print(f"Wallet balance fetched: Rp {idr_bal:,.2f}", flush=True)
        print(f"MAX_OPEN_POSITIONS: {MAX_POSITIONS}", flush=True)
        print(f"Open positions: {n_open} / {MAX_POSITIONS}", flush=True)
        print(f"Available slots: {slots_available}", flush=True)
        print(f"Target slots to allocate: {target_slots}", flush=True)
        print(
            f"Dynamic allocation per new order: Rp {alloc_per_order:,.2f}", flush=True
        )

        if idr_bal <= 0:
            print(
                f"  Wallet IDR balance is Rp {idr_bal:,.2f} — no available funds. Skipping.",
                flush=True,
            )
            return

        if target_slots <= 0 or alloc_per_order < MIN_NOTIONAL_IDR:
            print(
                f"  Available balance (Rp {idr_bal:,.2f}) cannot meet minimum notional (Rp {MIN_NOTIONAL_IDR:,.2f}). Skipping.",
                flush=True,
            )
            return

        # Scan — get ALL candidates (up to slots_available), sorted by score
        candidates = scanner.gather_candidates(max_positions=slots_available)
        if not candidates:
            print("  No T1 candidates found this cycle.", flush=True)
            return

        print(f"  Candidates found: {len(candidates)}", flush=True)

        filled = 0
        remaining_idr = idr_bal

        for cand in candidates:
            if filled >= target_slots:
                break

            cand_sym = cand.get("symbol")
            if cand_sym in open_symbols:
                print(
                    f"  ⏭ Symbol {cand_sym} already has an active OPEN position — skipping.",
                    flush=True,
                )
                continue

            slot_budget = min(alloc_per_order, remaining_idr)
            if slot_budget <= 0:
                print("  Remaining wallet IDR depleted — stopping.", flush=True)
                break

            print(
                f"  Slot budget: Rp {slot_budget:,.2f}  "
                f"({target_slots - filled} slot(s) left)",
                flush=True,
            )

            best = scanner.pick_best_candidate([cand], available_idr=slot_budget)
            if best is None:
                continue

            # Apply adaptive SL buffer
            entry = best["entry_price"]
            sl_buf = _sl_buffer_pct(entry)
            sl_stop = best["sl"]
            sl_limit = round(sl_stop * (1 - sl_buf), 10)
            best["sl_stop_price"] = sl_stop
            best["sl_limit_price"] = sl_limit
            best["sl_buffer_pct"] = sl_buf * 100

            notional = best.get("sizing", {}).get(
                "notional_idr", entry * best.get("sizing", {}).get("qty", 0)
            )
            if notional > remaining_idr:
                print(
                    f"  ⚠ Notional Rp {notional:,.2f} exceeds remaining IDR Rp {remaining_idr:,.2f} — skipping.",
                    flush=True,
                )
                continue

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
                open_symbols.add(best["symbol"])
                remaining_idr -= notional  # deduct actual order cost from available
                print(
                    f"  ✅ Entry placed: {best['symbol']}  orderId={result.get('orderId') or result.get('data',{}).get('orderId','?')}  remaining IDR: Rp {remaining_idr:,.0f}",
                    flush=True,
                )
            else:
                print(f"  ⚠ Entry skipped / failed: {best['symbol']}", flush=True)

        print(f"\n  Propose complete. New entries this cycle: {filled}", flush=True)

    except Exception as e:
        err = str(e).lower()
        if any(
            x in err
            for x in ["connection", "timeout", "network", "ssl", "5xx", "503", "502"]
        ):
            print(f"⚠️ [TOKO] Exchange unavailable: {e}", flush=True)
            sys.exit(2)
        print(f"⚠️ [TOKO] propose error: {e}", flush=True)
        import traceback

        traceback.print_exc()
        sys.exit(1)


def cmd_diagnostic() -> None:
    """
    Read-only diagnostic: displays wallet balance, MAX_OPEN_POSITIONS, dynamic allocation,
    open positions, and available slots without scanning or placing any orders.
    """
    print(f"\n{'='*50}", flush=True)
    print("  TOKOCRYPTO — ALLOCATION DIAGNOSTIC", flush=True)
    print(f"{'='*50}", flush=True)
    try:
        from services.supabase_client import fetch_all_tokocrypto_strict

        open_trades = [
            t
            for t in (fetch_all_tokocrypto_strict() or [])
            if t.get("exit_status") == "OPEN"
            and str(t.get("entry_status", "")).upper()
            not in ("CANCELED", "REJECTED", "EXPIRED")
        ]
        n_open = len(open_trades)
    except Exception as e:
        print(f"  ⚠ Failed to query open positions from DB: {e}", flush=True)
        n_open = 0

    slots_available = calculate_available_slots(n_open, MAX_POSITIONS)

    try:
        client = _build_client()
        balance_obj = client.get_balance("IDR")
        idr_bal = float(getattr(balance_obj, "free", 0.0))
    except Exception as e:
        print(
            f"⚠️ [TOKO] Failed to fetch live IDR wallet balance from API: {e}",
            flush=True,
        )
        idr_bal = 0.0

    target_slots, alloc_per_order = calculate_adaptive_allocation(
        wallet_balance=idr_bal,
        available_slots=slots_available,
        min_notional=MIN_NOTIONAL_IDR,
    )

    print(f"Wallet balance fetched: Rp {idr_bal:,.2f}", flush=True)
    print(f"MAX_OPEN_POSITIONS: {MAX_POSITIONS}", flush=True)
    print(f"Open positions: {n_open} / {MAX_POSITIONS}", flush=True)
    print(f"Available slots: {slots_available}", flush=True)
    print(f"Target slots to allocate: {target_slots}", flush=True)
    print(f"Dynamic allocation per new order: Rp {alloc_per_order:,.2f}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Tokocrypto IDR Spot Executor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check-positions",
        action="store_true",
        help="Check and update all open Tokocrypto positions",
    )
    group.add_argument(
        "--propose",
        action="store_true",
        help="Scan IDR pairs and propose/execute new entry orders",
    )
    group.add_argument(
        "--diagnostic",
        action="store_true",
        help="Read-only diagnostic of wallet balance, slots, and allocation",
    )
    args = parser.parse_args()

    if args.check_positions:
        cmd_check_positions()
    elif args.propose:
        cmd_propose()
    elif args.diagnostic:
        cmd_diagnostic()


if __name__ == "__main__":
    main()
