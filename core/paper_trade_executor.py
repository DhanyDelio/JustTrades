"""
paper_trade_executor.py — Semi-Manual Paper Trading on Binance Spot Testnet
============================================================================
READ-ONLY BY DEFAULT. No order is placed without explicit 'y' confirmation.

Budget hard-cap: BUDGET_USD is respected regardless of testnet's fake balance.
Single position at a time — will not propose if a position is already open.

Usage:
    python3 paper_trade_executor.py --propose              # scan → pick best → confirm
    python3 paper_trade_executor.py --propose --scan-n 30  # scan top 30 symbols
    python3 paper_trade_executor.py --check-positions       # status of open trades

Dependencies (already installed from chart_analyzer):
    python-binance python-dotenv pandas numpy requests
"""

from __future__ import annotations

# Standard library
import argparse
import contextlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Third-party
import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Project local
import services.chart_analyzer as ca
from core.executors.spot_order_executor import SpotOrderExecutor
from core.repositories.spot_trade_repository import SpotTradeRepository
from core.utils.binance_math import (
    compute_position_size,
    get_symbol_constraints,
    round_step,
    round_tick,
)

repo = SpotTradeRepository()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Budget — hard cap regardless of testnet balance
# Adjust if IDR/USD rate changes: Rp200,000 ÷ ~16,500 ≈ $12.12
BUDGET_USD: float = 12.00

# Per-trade budget for --propose-all batch mode.
# Each position is sized at this amount regardless of testnet's fake large balance,
# so individual trade behavior stays representative of real $12 capital conditions.
PER_TRADE_BUDGET: float = 12.00

# Lab (batch) starting capital for compounding lab pool (separate from single-propose simulated balance)
# Default: $240
LAB_STARTING_CAPITAL: float = 240.0
# Max fraction of budget to risk on a single trade
# At $12 total, 25% = $3 max loss per trade
RISK_FRACTION: float = 0.25

# Binance Spot min notional per order (most USDT pairs: $5, some small caps $1)
# We fetch the real exchange info per symbol, but this is the hard floor
DEFAULT_MIN_NOTIONAL: float = 5.0

# How many symbols to scan when picking a candidate
DEFAULT_SCAN_N: int = 30

# Tiered / paginated scanning for --propose-all
# Part 1 = rank 1–30, Part 2 = 31–60, Part 3 = 61–90, Part 4 = 91–120
PART_SIZE: int = 30
MAX_PARTS: int = 4                  # 4 × 30 = 120 symbols scanned max
MIN_DESIRED_CANDIDATES: int = 5     # kept for reference, no longer stops scan early
SCAN_PART_DELAY_SEC: float = 2.0    # courtesy sleep between parts (throttle handles the rest)
# Binance Spot rate limit: 6000 weight/min. Ceiling = 80% = 4800.
# Each analyze_symbol call uses ~2–5 weight units (ticker + klines).
RATE_LIMIT_WEIGHT_CEILING: int = 4800

# Trade log file
TRADE_LOG_PATH = Path("./trade_log.json")

# Limit order zone buffer: place BUY limit slightly ABOVE the support zone price
# so the order fills when price pulls back to the zone, not immediately.
# 0.15% above zone center = inside the zone band, not below it.
# This anchors entry to the tested support level, not to current price.
ZONE_ENTRY_BUFFER_PCT: float = 0.0015   # 0.15% above zone center for LONG

# Rule version — bump manually when zone/ATR/RR threshold parameters change.
# Used for performance attribution in --stats so results from different
# rule configurations don't get mixed together in analysis.
RULE_VERSION: str = "v1.0.0"

# Taker fee estimate per side (Binance Spot default 0.1%)
TAKER_FEE_PCT: float = 0.001   # 0.1%

# Dual-position threshold (based on ACTUAL exchange balance, not BUDGET_USD).
# Reasoning for $50 default:
#   - Each position needs to clear min notional ($5) + meaningful risk buffer
#   - At $50 with RISK_FRACTION=0.25 → $12.50 max loss budget per slot
#   - Two slots: $12.50 × 2 = $25 max total loss (50% of $50 — acceptable floor)
#   - The remaining $25 stays as undeployed capital providing a cushion
#   - Below $50 the per-slot budget gets too thin to be meaningful
DUAL_POSITION_MIN_BALANCE: float = 50.0

# Hard cap on concurrent positions — never go beyond this regardless of balance
MAX_CONCURRENT_POSITIONS: int = 2


# ---------------------------------------------------------------------------
# 1. TESTNET CLIENT
# ---------------------------------------------------------------------------

def get_testnet_client():
    """Connect to Binance Spot Testnet. Raises if API keys not set."""
    try:
        from binance.client import Client
    except ImportError:
        raise ImportError("pip install python-binance --break-system-packages")

    api_key    = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError(
            "API keys not found in .env\n"
            "Set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET"
        )

    client = Client(api_key, api_secret, testnet=True, tld="com")
    return client


def get_actual_usdt_balance(client) -> float:
    """
    Query real free USDT on testnet — used ONLY to verify an order CAN be placed
    (sufficient funds exist). Never used for sizing or dual-position threshold.
    Testnet starts with ~100k fake USDT regardless of trade history.
    """
    try:
        bal = client.get_asset_balance(asset="USDT")
        return float(bal["free"]) if bal else 0.0
    except Exception as e:
        print(f"  [WARN] Could not fetch testnet USDT balance: {e}")
        return 9999.0   # assume sufficient if query fails







# ---------------------------------------------------------------------------
# 5. PROPOSAL DISPLAY
# ---------------------------------------------------------------------------

def print_proposal(cand: dict) -> None:
    """Print a clear, readable trade proposal before asking for confirmation."""
    sym       = cand["symbol"]
    direction = cand["direction"].upper()
    entry     = cand["entry_price"]
    sl        = cand["sl"]
    tp1       = cand["tp1"]
    tp2       = cand["tp2"]
    rr        = cand["rr"]
    risk_pct  = cand["risk_pct"]
    sz        = cand["sizing"]
    zone      = cand["winning_zone"]

    W = 68
    sep = "─" * W

    print(f"\n  ╔{'═'*W}╗")
    print(f"  ║  {'TRADE PROPOSAL — BINANCE SPOT TESTNET':<{W-1}}║")
    print(f"  ║  {'No order placed until you confirm with y':<{W-1}}║")
    print(f"  ╠{'═'*W}╣")

    # Header
    print(f"  ║  {'Symbol':<20} {sym:<{W-22}}║")
    print(f"  ║  {'Direction':<20} {direction:<{W-22}}║")
    print(f"  ║  {'Order type':<20} {'LIMIT (not market — waits for zone)':<{W-22}}║")

    # Zone backing
    if zone:
        zone_str = f"{zone['label']}  @  {ca._fmt_price(zone['tp']).strip()}"
    else:
        zone_str = "T1 zone-backed (see chart_analyzer for detail)"
    print(f"  ║  {'Zone backing':<20} {zone_str:<{W-22}}║")

    print(f"  ╠{'═'*W}╣")

    # Prices
    entry_zone = cand.get("entry_zone") or (cand.get("support_zones") or [None])[0]
    if entry_zone:
        dist_from_cur = (cand["current_price"] - entry_zone["center"]) / cand["current_price"] * 100
        zone_str = (f"{ca._fmt_price(entry_zone['center']).strip()}"
                    f"  ({entry_zone['touches']}× tested, -{dist_from_cur:.2f}% from current)")
        print(f"  ║  {'Entry zone':<20} {zone_str:<{W-22}}║")
        print(f"  ║  {'  → limit entry':<20} {'zone_center + 0.15% buffer':<{W-22}}║")
    print(f"  ║  {'Entry (limit)':<20} {ca._fmt_price(entry).strip():>{W-22}}║")
    print(f"  ║  {'Stop-Loss':<20} {ca._fmt_price(sl).strip():>{W-22}}║")
    print(f"  ║  {'TP1':<20} {ca._fmt_price(tp1).strip():>{W-22}}║")
    if tp2:
        print(f"  ║  {'TP2 (reference)':<20} {ca._fmt_price(tp2).strip():>{W-22}}║")
    print(f"  ║  {'R:R':<20} {rr:.2f}:1{'':{W-25}}║")
    print(f"  ║  {'SL distance':<20} {risk_pct:.2f}%{'':{W-25}}║")

    print(f"  ╠{'═'*W}╣")

    # Sizing
    bud = cand.get("budget_for_slot", BUDGET_USD)
    print(f"  ║  {'Slot budget':<20} ${bud:.2f}{'':{W-27}}║")
    print(f"  ║  {'Risk fraction':<20} {RISK_FRACTION*100:.0f}% of slot{'':{W-32}}║")
    print(f"  ║  {'Target max loss':<20} ${bud*RISK_FRACTION:.2f}{'':{W-27}}║")
    print(f"  ║  {'Position qty':<20} {sz['qty']:.8g} {sym.replace('USDT',''):<{W-30}}║")
    print(f"  ║  {'Position value':<20} ${sz['notional_usd']:.2f}{'':{W-27}}║")
    print(f"  ║  {'Max loss if SL hit':<20} ${sz['max_loss_usd']:.2f}  ({sz['max_loss_pct']:.1f}% of budget){'':{W-48}}║")

    print(f"  ╠{'═'*W}╣")

    # Warnings
    if sz["warnings"]:
        print(f"  ║  {'⚠  WARNINGS':<{W-1}}║")
        for w in sz["warnings"]:
            # Wrap long warnings
            while len(w) > W - 5:
                print(f"  ║  {'  ' + w[:W-7]:<{W-1}}║")
                w = w[W-7:]
            print(f"  ║  {'  ' + w:<{W-1}}║")
        print(f"  ╠{'═'*W}╣")

    # ATR context
    print(f"  ║  {'ATR(14)':<20} {cand['atr_pct']:.2f}%  ({ca._fmt_price(cand['atr']).strip()} price units){'':{W-52}}║")

    # Scores (Task 1)
    if cand.get("score_composite") is not None:
        print(f"  ╠{'═'*W}╣")
        rs  = cand.get('score_risk', 0)
        zs  = cand.get('score_zone', 0)
        rrs = cand.get('score_rr', 0)
        cs  = cand.get('score_composite', 0)
        score_str = (f"Risk {rs:.1f}/10  Zone {zs:.1f}/10  R:R {rrs:.1f}/10  "
                     f"→ Composite {cs:.1f}/10")
        print(f"  ║  {'Scores':<20} {score_str:<{W-22}}║")
        print(f"  ║  {'  (weights)':<20} {'Risk×0.5  Zone×0.3  R:R×0.2':<{W-22}}║")

    # ML Score + scan rank (observation only — does not influence decisions)
    if cand.get("ml_score") is not None or cand.get("symbol_rank") is not None:
        print(f"  ╠{'═'*W}╣")
    if cand.get("ml_score") is not None:
        ms     = cand["ml_score"]
        mv     = cand.get("ml_model_version", "v1")
        ml_str = f"{ms:.2f}  (observation only — not used for decisions)"
        print(f"  ║  {'ML Score ('+mv+')':<20} {ml_str:<{W-22}}║")
    if cand.get("symbol_rank") is not None:
        rank_str = f"#{cand['symbol_rank']}  (observation only)"
        print(f"  ║  {'Symbol rank':<20} {rank_str:<{W-22}}║")

    print(f"  ╚{'═'*W}╝")

    bud = cand.get("budget_for_slot", BUDGET_USD)
    print(f"\n  Budget context:")
    print(f"    Slot budget  : ${bud:.2f}")
    print(f"    This trade   : ${sz['notional_usd']:.2f} ({sz['notional_usd']/bud*100:.0f}% of slot)")
    print(f"    Remaining    : ${bud - sz['notional_usd']:.2f} (held in USDT)")
    if sz["notional_usd"] > bud:
        print(f"    ⚠  Position value EXCEEDS slot budget — reduce RISK_FRACTION or pick a cheaper pair")



# ---------------------------------------------------------------------------
# 9. POSITION MONITORING
# ---------------------------------------------------------------------------

def _fmt_order_status(status: str) -> str:
    icons = {
        "NEW": "🕐 NEW (pending fill)",
        "PARTIALLY_FILLED": "🔄 PARTIALLY_FILLED",
        "FILLED": "✅ FILLED",
        "CANCELED": "❌ CANCELED",
        "REJECTED": "❌ REJECTED",
        "EXPIRED": "⏱ EXPIRED",
    }
    return icons.get(status, status)


# ---------------------------------------------------------------------------
# Telegram helper + post-resolve auto-propose
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    """Send a Telegram message using TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from .env."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    placeholders = ("your_telegram", "your", "replace_me", "placeholder", "changeme")
    if any(p in f"{token}:{chat_id}".lower() for p in placeholders):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------

def cmd_propose(scan_n: int, symbol_filter: str | None = None,
                simulate_balance: float | None = None,
                auto_confirm: bool = False) -> None:
    print("=" * 70)
    print("Paper Trade Executor — Binance Spot Testnet")
    print("=" * 70)

    # Connect to testnet
    print("\nConnecting to Binance Testnet...")
    try:
        client = get_testnet_client()
        client.ping()
        print("✅ Testnet connected")
    except Exception as e:
        print(f"❌ Testnet connection failed: {e}")
        sys.exit(1)

    # ── Simulated balance (Task 3 correction) ────────────────────────
    # Use simulated capital (BUDGET_USD + closed PnL), NOT real testnet wallet.
    # Real testnet wallet (~100k+) is irrelevant for scaling decisions.
    trades = repo.load_trade_log()

    if simulate_balance is not None:
        sim_balance = simulate_balance
        print(f"\n⚠️  [TEST ONLY] Simulating balance: ${sim_balance:.2f}")
        print(f"   This flag is for dry-run testing only — NOT used in real operation.")
    else:
        from core.managers.portfolio_manager import PortfolioManager
        pm = PortfolioManager(repo, BUDGET_USD, LAB_STARTING_CAPITAL, PER_TRADE_BUDGET)
        sim_balance = pm.get_simulated_balance(trades)
        closed_trades = [t for t in trades if t.get("exit_status") in ("TP_HIT", "SL_HIT")]
        closed_pnl    = sum(t.get("realized_pnl_usd") or 0 for t in closed_trades)
        print(f"\nSimulated capital  : ${sim_balance:.2f}")
        print(f"  Started at       : ${BUDGET_USD:.2f}")
        print(f"  Closed trades    : {len(closed_trades)}  (total PnL: ${closed_pnl:+.2f})")
        print(f"  (Real testnet wallet balance is ignored for sizing/threshold decisions)")

    # ── Determine position limit and slots available ───────────────────
    open_count   = repo.count_open_positions(trades)
    open_trades  = [t for t in trades if t.get("exit_status") == "OPEN"]

    if sim_balance >= DUAL_POSITION_MIN_BALANCE:
        position_limit = MAX_CONCURRENT_POSITIONS  # 2
        slots_available = position_limit - open_count
        if open_count == 0:
            budget_for_slot = sim_balance / 2
            mode_str = f"Dual-position mode (${sim_balance:.2f} ≥ ${DUAL_POSITION_MIN_BALANCE:.0f} threshold)"
        else:
            used_notional   = sum(t.get("entry_notional", 0) for t in open_trades)
            budget_for_slot = max(sim_balance - used_notional, 0)
            mode_str = f"Dual-position mode — filling slot 2 (${budget_for_slot:.2f} available)"
    else:
        position_limit  = 1
        slots_available = position_limit - open_count
        budget_for_slot = sim_balance
        mode_str = (f"Single-position mode "
                    f"(${sim_balance:.2f} < ${DUAL_POSITION_MIN_BALANCE:.0f} threshold — "
                    f"keep growing to unlock dual-position)")

    print(f"Mode: {mode_str}")
    print(f"Open positions: {open_count} / {position_limit}  |  Budget for this slot: ${budget_for_slot:.2f}")
    print(f"Max risk this slot: {RISK_FRACTION*100:.0f}% × ${budget_for_slot:.2f} = ${budget_for_slot*RISK_FRACTION:.2f}")

    # ── Block if all slots full ────────────────────────────────────────
    if slots_available <= 0:
        print(f"\n⛔  All {position_limit} position slot(s) are occupied:")
        for t in open_trades:
            oco = "OCO ✅" if t.get("oco_placed") else "⚠ no OCO"
            print(f"   {t['symbol']:10} {t['direction'].upper():5}  "
                  f"entry={ca._fmt_price(t['entry_price']).strip()}  "
                  f"{t.get('entry_status','?')}  {oco}")
        print(f"\n   Run --check-positions to see full status.")
        print(f"   Wait for a position to close (TP_HIT / SL_HIT / MANUALLY_CLOSED) before proposing.")
        sys.exit(0)

    # ── Gather candidates ─────────────────────────────────────────────
    from core.scanners.spot_candidate_scanner import SpotCandidateScanner
    scanner = SpotCandidateScanner(client, repo)
    candidates = scanner.gather_candidates(scan_n)

    if not candidates:
        print("\n❌ No T1 zone-backed candidates found. Try again later.")
        sys.exit(0)

    # Filter out symbols already in an open position
    open_symbols = {t["symbol"] for t in open_trades}
    excluded_symbols = sorted({c["symbol"] for c in candidates} & open_symbols)
    candidates = [c for c in candidates if c["symbol"] not in open_symbols]
    if excluded_symbols:
        print(f"\n  Excluded (already open): {', '.join(excluded_symbols)}")
    if not candidates:
        print(f"\n❌ All T1 candidates are already in open positions ({', '.join(sorted(open_symbols))}). Try again later.")
        sys.exit(0)

    print(f"\nTop 5 candidates  (Risk×0.5 + Zone×0.3 + R:R×0.2 = Composite):")
    print(f"  {'#':<3} {'Symbol':<12} {'Risk%':>6} {'R:R':>5}  "
          f"{'RiskS':>5} {'ZoneS':>5} {'R:RS':>5} {'Comp':>5}  "
          f"{'EntryZ':>6} {'TPZ':>4}")
    print(f"  {'─'*66}")
    for i, c in enumerate(candidates[:5], 1):
        ez_touches = c.get("_touch_val", "?")
        # TP zone touches: from winning_zone label e.g. "Zone 5×" → extract number
        wz = c.get("winning_zone") or {}
        wz_label = wz.get("label", "")
        # label format is "Zone N×" — extract N
        import re as _re
        tp_touch_match = _re.search(r"(\d+)", wz_label)
        tp_touches = tp_touch_match.group(1) + "×" if tp_touch_match else "?"
        print(f"  {i:<3} {c['symbol']:<12} {c['risk_pct']:>5.2f}% {c['rr']:>5.1f}x  "
              f"{c.get('score_risk',0):>5.1f} {c.get('score_zone',0):>5.1f} "
              f"{c.get('score_rr',0):>5.1f} {c.get('score_composite',0):>5.1f}  "
              f"{str(ez_touches)+'×':>6} {tp_touches:>4}")

    # ── Pick best (or user-specified symbol) ──────────────────────────
    print("\nChecking exchange constraints...")
    best = scanner.pick_best_candidate(
        candidates, client,
        budget_for_slot = budget_for_slot,
        symbol_filter   = symbol_filter,
    )

    if best is None:
        what = f"for {symbol_filter}" if symbol_filter else ""
        print(f"\n❌ No candidate {what} passed exchange minimum notional constraints.")
        print(f"   Slot budget ${budget_for_slot:.2f} may be too small for min notional.")
        sys.exit(0)

    # ── Compute ML score (observation only — does not affect decisions) ─
    from ml.ml_scorer import compute_ml_score
    ml_result = compute_ml_score(best)
    best["ml_score"] = ml_result["ml_score"]
    best["ml_model_version"] = ml_result["ml_model_version"]

    if best["ml_score"] is not None:
        print(f"\n  [ML SHADOW] Score: {best['ml_score']:.2f} | Version: {best['ml_model_version']} | Status: PASSTHROUGH (No Veto)")

    # ── Display proposal ──────────────────────────────────────────────
    print_proposal(best)

    # ── Confirm ───────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    if simulate_balance is not None:
        print("  ⚠️  [TEST ONLY — simulated balance] No real order will be placed.")
        print("  This dry run confirms proposal logic only.")
        print("─" * 70)
        return   # never prompt in simulate mode

    print("  ⚠  This will place a REAL LIMIT ORDER on Binance Spot TESTNET.")
    print("  The testnet uses virtual funds — not real money.")
    print("  Type 'y' to confirm, anything else to abort.")
    print("─" * 70)

    if auto_confirm:
        print("  [--yes] Auto-confirmed for non-interactive run.")
        answer = "y"
    else:
        try:
            answer = input("  Confirm? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""

    if answer != "y":
        print("\n  Aborted. No order placed.")
        return

    # ── Place order ───────────────────────────────────────────────────
    print("\n  Placing limit order on testnet...")
    try:
        executor = SpotOrderExecutor(client, auto_confirm=True, repo=repo)
        order = executor.execute(best)
    except RuntimeError as e:
        print(f"\n  ❌ Order failed: {e}")
        return

    print(f"\n  ✅ Order placed successfully!")
    print(f"     Order ID  : {order.get('orderId')}")
    print(f"     Symbol    : {order.get('symbol')}")
    print(f"     Side      : {order.get('side')}")
    print(f"     Status    : {order.get('status')}")
    print(f"     Price     : {order.get('price')}")
    print(f"     Qty       : {order.get('origQty')}")

    print(f"\n  Trade logged to {TRADE_LOG_PATH}")

    # Remind if a 2nd slot is still available
    new_open = repo.count_open_positions(repo.load_trade_log())
    if new_open < position_limit:
        remaining_slots = position_limit - new_open
        print(f"\n  ℹ️  {remaining_slots} slot(s) still available for another position.")


def cmd_check_positions(verbose: bool = False, mode: str = "all") -> None:
    print("=" * 70)
    print("Paper Trade Executor — Position Status")
    print("=" * 70)

    try:
        client = get_testnet_client()
        client.ping()
        print("✅ Testnet connected")
    except Exception as e:
        print(f"❌ Testnet connection failed: {e}")
        sys.exit(1)

    from core.executors.spot_position_monitor import SpotPositionMonitor
    executor = SpotOrderExecutor(client)
    monitor = SpotPositionMonitor(client, repo, executor)
    monitor.check_positions(verbose=verbose, mode=mode)


# ---------------------------------------------------------------------------
# 11. --propose-all: BATCH DATA COLLECTION
# ---------------------------------------------------------------------------



def should_auto_confirm_batch(is_lab_batch: bool) -> bool:
    """Only lab/batch mode is auto-confirmed; single-propose stays manual."""
    return bool(is_lab_batch)


def cmd_propose_all(scan_n: int, dry_run: bool = False,
                    auto_confirm: bool = False) -> None:
    print("=" * 70)
    print("Paper Trade Executor — BATCH DATA COLLECTION (--propose-all)")
    print(f"Each position sized at ${PER_TRADE_BUDGET:.2f}  |  Rule: {RULE_VERSION}")
    print("This mode is for accelerated data collection, not live capital deployment.")
    print("=" * 70)

    try:
        client = get_testnet_client()
        client.ping()
        print("✅ Testnet connected\n")
    except Exception as e:
        print(f"❌ Testnet connection failed: {e}")
        sys.exit(1)

    open_trades = [t for t in repo.load_trade_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}
    if open_symbols:
        print(f"  Excluding already open symbol(s) from this batch: {', '.join(sorted(open_symbols))}\n")

    # Compute lab capital pool (compounding) and display status
    all_trades = repo.load_trade_log()
    from core.managers.portfolio_manager import PortfolioManager
    pm = PortfolioManager(repo, BUDGET_USD, LAB_STARTING_CAPITAL, PER_TRADE_BUDGET)
    pool = pm.compute_lab_pool(all_trades)
    lab_cap = pool["lab_capital"]
    net_pnl = pool["closed_cluster_pnl"]
    deployed = pool["deployed_capital"]
    available = pool["available_capital"]
    max_new = pool["max_new_positions"]

    print(f"\n  Lab capital: ${lab_cap:.2f} (started ${LAB_STARTING_CAPITAL:.0f}, net P&L ${net_pnl:+.2f})  |  Deployed: ${deployed:.2f}  |  Available: ${available:.2f}  |  Max new positions: {max_new}")

    if max_new <= 0:
        print("\n⛔  Lab pool depleted or fully deployed — no new proposals allowed.")
        print("   Either wait for cluster trades to resolve (TP_HIT/SL_HIT) or increase LAB_STARTING_CAPITAL.")
        return

    from core.scanners.spot_candidate_scanner import SpotCandidateScanner
    scanner = SpotCandidateScanner(client, repo)
    candidates = scanner.gather_all_candidates(scan_n, open_symbols=open_symbols)
    if not candidates:
        if open_symbols:
            print(f"❌ No valid candidates found. Symbols already open: {', '.join(sorted(open_symbols))}.")
        else:
            print("❌ No valid candidates found.")
        sys.exit(0)

    # Respect lab pool limit: only allow up to max_new new positions
    if len(candidates) > max_new:
        print(f"\n  Note: limiting proposals to top {max_new} candidates due to lab pool available capital")
        candidates = candidates[:max_new]

    excluded_symbols = sorted(open_symbols)
    if excluded_symbols:
        print(f"\n  Excluded (already open): {', '.join(excluded_symbols)}")

    # Correlation cluster ID — shared by all trades in this batch
    cluster_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Print batch summary table
    print(f"\n  Batch proposal  [{len(candidates)} trades]  cluster: {cluster_id}")
    print(f"  {'#':<3} {'Symbol':<13} {'Entry':>10} {'SL':>10} {'TP1':>10} "
          f"{'R:R':>5} {'Risk%':>6} {'T':>2} {'Z':>3} {'P':>2}  Score")
    print(f"  {'─'*73}")
    for i, c in enumerate(candidates, 1):
        ez   = c.get("entry_zone") or {}
        tier = c.get("tier_used", "T1")[1]   # "1" or "2"
        ez_t = ez.get("touches", "?")
        part = c.get("scan_part", 1)
        print(f"  {i:<3} {c['symbol']:<13} "
              f"{ca._fmt_price(c['entry_price']).strip():>10} "
              f"{ca._fmt_price(c['sl']).strip():>10} "
              f"{ca._fmt_price(c['tp1']).strip():>10} "
              f"{c['rr']:>5.1f} {c['risk_pct']:>5.2f}% "
              f"T{tier} {str(ez_t)+'×':>3} P{part}  "
              f"{c.get('score_composite',0):.1f}")

    total_notional = sum(c["sizing"]["notional_usd"] for c in candidates)
    print(f"\n  Total notional if all fill: ${total_notional:.2f} "
          f"({len(candidates)} × ~${PER_TRADE_BUDGET:.0f})")
    print(f"  Note: testnet virtual balance absorbs this; "
          f"each trade behaves as if placed with ${PER_TRADE_BUDGET:.2f} real capital.")

    if dry_run:
        print(f"\n  [DRY RUN] No orders placed. Cluster ID would be: {cluster_id}")
        return

    print(f"\n  {'─'*70}")
    print(f"  ⚠  This will place {len(candidates)} LIMIT ORDERS on Binance Spot TESTNET.")
    print(f"  All virtual funds. Type 'y' to place ALL, anything else to abort.")
    print(f"  {'─'*70}")

    if should_auto_confirm_batch(is_lab_batch=True) or auto_confirm:
        if auto_confirm:
            print("  [--yes] Auto-confirmed for non-interactive run.")
        else:
            print("  Auto-confirm enabled for lab/batch mode — proceeding without manual prompt.")
        answer = "y"
    else:
        try:
            answer = input("  Confirm ALL? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""

    if answer != "y":
        print("\n  Aborted. No orders placed.")
        return

    placed, failed = 0, 0
    from ml.ml_scorer import compute_ml_score
    for cand in candidates:
        try:
            # ML score (observation only — does not affect decisions)
            ml_result = compute_ml_score(cand)
            cand["ml_score"] = ml_result["ml_score"]
            cand["ml_model_version"] = ml_result["ml_model_version"]

            if cand["ml_score"] is not None:
                print(f"  [ML SHADOW] Score: {cand['ml_score']:.2f} | Version: {cand['ml_model_version']} | Status: PASSTHROUGH (No Veto)")

            executor = SpotOrderExecutor(client, auto_confirm=True, repo=repo)
            order = executor.execute(cand, correlation_cluster_id=cluster_id)
            ml_tag   = f"  ml={cand['ml_score']:.2f}" if cand.get('ml_score') is not None else ""
            rank_tag = f"  rank=#{cand['symbol_rank']}" if cand.get('symbol_rank') is not None else ""
            print(f"  ✅ {cand['symbol']:<12} order #{order.get('orderId')}  "
                  f"price={order.get('price')}{ml_tag}{rank_tag}")
            placed += 1
        except Exception as e:
            print(f"  ❌ {cand['symbol']:<12} FAILED: {e}")
            failed += 1

    print(f"\n  Placed: {placed}  Failed: {failed}  Cluster: {cluster_id}")
    print(f"  Run --check-positions to monitor.")


# ---------------------------------------------------------------------------
# 12. --stats
# ---------------------------------------------------------------------------

def cmd_stats(mode: str = "all") -> None:
    """Compute and print performance statistics from trade_log.json.

    Mode filters trades: 'single' (no cluster id), 'lab' (clustered), or 'all'.
    """
    from collections import defaultdict
    import math

    trades = repo.load_trade_log()
    # Filter trades by mode
    closed = [t for t in trades
              if t.get("exit_status") in ("TP_HIT", "SL_HIT") and repo.match_mode(t, mode)]

    if not closed:
        print("\n  No closed trades yet. Run --check-positions after trades resolve.")
        return

    print("=" * 70)
    print("Performance Statistics")
    print(f"Mode: {mode}  |  Total closed trades: {len(closed)}")
    print("=" * 70)

    # Group by rule_version
    by_version: dict[str, list] = defaultdict(list)
    for t in closed:
        by_version[t.get("rule_version") or "unknown"].append(t)

    for version, group in sorted(by_version.items(), key=lambda x: (x[0] is None, x[0])):
        n          = len(group)
        wins       = [t for t in group if t["exit_status"] == "TP_HIT"]
        losses     = [t for t in group if t["exit_status"] == "SL_HIT"]
        win_rate   = len(wins) / n
        avg_rr     = sum((t.get("planned_rr") or 0) for t in group) / n
        be_win_rate = 1 / (1 + avg_rr) if avg_rr > 0 else 0.5

        avg_win_pct  = (sum((t.get("realized_pnl_pct") or 0) for t in wins)  / len(wins))  if wins   else 0
        avg_loss_pct = (sum((t.get("realized_pnl_pct") or 0) for t in losses) / len(losses)) if losses else 0
        avg_fee_slip = sum(
            ((t.get("fee_usd_roundtrip") or 0) / max((t.get("entry_notional") or 1), 1) * 100)
            + abs(t.get("slippage_pct") or 0)
            for t in group
        ) / n
        expectancy   = (win_rate * avg_win_pct) - ((1 - win_rate) * abs(avg_loss_pct)) - avg_fee_slip

        # Z-score vs breakeven
        if n >= 2:
            p0   = be_win_rate
            z    = (win_rate - p0) / math.sqrt(p0 * (1 - p0) / n)
            sig  = "✅ p<0.05" if abs(z) >= 1.96 else ("🟡 p<0.10" if abs(z) >= 1.645 else "⚠ not sig")
        else:
            z, sig = 0.0, "⚠ n/a"

        # Cluster analysis
        cluster_ids = set(t.get("correlation_cluster_id") for t in group
                          if t.get("correlation_cluster_id"))
        n_clusters   = len(cluster_ids) if cluster_ids else n
        n_singles    = sum(1 for t in group if not t.get("correlation_cluster_id"))

        print(f"\n  Rule version: {version}  ({n} trades)")
        print(f"  {'─'*60}")
        print(f"  Win rate          : {win_rate*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Breakeven win rate: {be_win_rate*100:.1f}%  (at avg R:R {avg_rr:.2f}:1)")
        print(f"  Avg win  %        : {avg_win_pct:+.2f}%")
        print(f"  Avg loss %        : {avg_loss_pct:+.2f}%")
        print(f"  Avg fee+slip %    : -{avg_fee_slip:.3f}%")
        print(f"  Expectancy        : {expectancy:+.3f}%  per trade")
        print(f"  Z-score vs B/E    : {z:+.2f}  {sig}")
        if n < 30:
            print(f"  ⚠  Only {n} trades — z-score unreliable until n≥30")
        print(f"  Clusters          : {n_clusters} independent sessions + {n_singles} single trades")
        print(f"  Effective n       : ~{n_clusters + n_singles} independent observations")
        print(f"  (raw trade count {n} overstates independence if cluster trades co-move)")

    # Total realized PnL
    total_pnl = sum(t.get("realized_pnl_usd", 0) or 0 for t in closed)
    from core.managers.portfolio_manager import PortfolioManager
    pm = PortfolioManager(repo, BUDGET_USD, LAB_STARTING_CAPITAL, PER_TRADE_BUDGET)
    sim_bal   = pm.get_simulated_balance(trades)
    print(f"\n  Total realized PnL: ${total_pnl:+.4f}")
    print(f"  Simulated capital : ${sim_bal:.2f}  (started: ${BUDGET_USD:.2f})")


def main():
    parser = argparse.ArgumentParser(
        description="Paper trading executor — Binance Spot Testnet. No auto-execution."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--propose",       action="store_true",
                       help="Scan, pick best low-risk T1 candidate, confirm & place")
    group.add_argument("--propose-all",   action="store_true",
                       help="Batch: propose ALL valid candidates for data collection")
    group.add_argument("--check-positions", action="store_true",
                       help="Check status of open positions, place OCO if filled")
    group.add_argument("--stats",         action="store_true",
                       help="Print performance statistics from trade_log.json")

    parser.add_argument("--scan-n",         type=int, default=DEFAULT_SCAN_N)
    parser.add_argument("--symbol",         type=str, default=None,
                        help="--propose only: manually specify symbol")
    parser.add_argument("--simulate-balance", type=float, default=None, metavar="USD",
                        help="[TEST ONLY] Override simulated balance for dry-run testing")
    parser.add_argument("--dry-run",        action="store_true",
                        help="--propose-all: show batch table but don't place orders")
    parser.add_argument("--verbose",        action="store_true",
                        help="--check-positions: show detailed per-position cards")
    parser.add_argument("--mode", choices=["single", "lab", "all"], default="all",
                        help="Filter trades by mode: single (no cluster id), lab (clustered), or all")
    parser.add_argument("--export-clean", action="store_true",
                        help="Export resolved lab trades to a clean ML-ready JSON and exit")
    parser.add_argument("--yes", action="store_true",
                        help="Auto-confirm order placement (for non-interactive / CI use)")
    args = parser.parse_args()

    if args.propose:
        cmd_propose(scan_n=args.scan_n, symbol_filter=args.symbol,
                    simulate_balance=args.simulate_balance,
                    auto_confirm=args.yes)
    elif args.propose_all:
        cmd_propose_all(scan_n=args.scan_n, dry_run=args.dry_run,
                        auto_confirm=args.yes)
    elif args.check_positions:
        cmd_check_positions(verbose=args.verbose, mode=args.mode)
    elif args.stats:
        cmd_stats(mode=args.mode)

    # Export-clean is a convenience to write a filtered ML-ready dataset then exit
    if args.export_clean:
        all_trades = repo.load_trade_log()
        repo.export_clean(all_trades, mode=args.mode)
        return


if __name__ == "__main__":
    main()
