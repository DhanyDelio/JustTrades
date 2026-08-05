"""
futures_trade_executor.py — CMD layer + orchestration for Binance Futures Testnet.
====================================================================================
READ-ONLY BY DEFAULT. No order placed without explicit 'y' confirmation.

This file is the CLI entry point and orchestration layer. All business logic
has been extracted to dedicated modules (Tahap 1–6 OOP refactor):

  core/clients/futures_client.py          — FuturesClient (connectivity, market data)
  core/utils/futures_math.py              — Pure math (liq price, sizing, ATR, MAE/MFE)
  core/utils/telegram.py                  — Telegram notifier
  core/repositories/futures_trade_repository.py — FuturesTradeRepository (Supabase I/O)
  core/scanners/futures_candidate_scanner.py    — FuturesCandidateScanner (scan + pick)
  core/executors/futures_order_executor.py      — FuturesOrderExecutor (entry + exit orders)
  core/executors/futures_position_monitor.py    — FuturesPositionMonitor (check_positions)

Module-level backward-compatible wrappers for each moved function are retained
below so that any code that imports from this file continues to work unchanged.

Usage:
    python3 futures_trade_executor.py --propose           # scan + place up to N positions
    python3 futures_trade_executor.py --propose --count 3 # up to 3 positions
    python3 futures_trade_executor.py --propose --yes     # non-interactive (CI)
    python3 futures_trade_executor.py --check-positions
    python3 futures_trade_executor.py --stats-futures

API keys: Binance Futures Testnet (different from Spot Testnet)
    Register at https://testnet.binancefuture.com
    Set BINANCE_FUTURES_TESTNET_API_KEY and BINANCE_FUTURES_TESTNET_API_SECRET in .env
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import io
import contextlib
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Root resolution boilerplate for CLI wrapper
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import chart_analyzer as ca

# Pure math helpers — extracted to dedicated module (Tahap 1 refactor)
from core.utils.futures_math import (
    calculate_liquidation_price,
    compute_futures_position_size,
    compute_volatility_regime,
    compute_mae_mfe_from_candles,
    _empty_excursion,
)
from core.utils.binance_math import round_step, round_tick

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Per-trade margin budget (mirrors spot's PER_TRADE_BUDGET)
FUTURES_BUDGET_USD: float = 12.00

# Leverage — fixed 3x isolated margin
# At 3x: liquidation distance ≈ 33% from entry (well above avg SL ~2-3%)
LEVERAGE: int = 3
MARGIN_MODE: str = "isolated"

# Risk fraction of margin per trade (same as spot)
RISK_FRACTION: float = 0.25

# Maintenance margin rate for isolated USDT-M futures (Binance default tier 1)
# Used for liquidation price calculation. Actual value varies by notional size,
# but 0.4% is correct for most positions under $50k notional.
MAINTENANCE_MARGIN_RATE: float = 0.004  # 0.4%

# Taker fee for futures (Binance Futures Testnet default)
TAKER_FEE_PCT: float = 0.0004  # 0.04% taker

# Minimum R:R to accept a candidate
MIN_RR: float = 1.5

# How many symbols to scan
DEFAULT_SCAN_N: int = 100   # raised from 30 — throttle handles rate limiting

# Tiered scanning constants (mirrors spot's gather_all_candidates pattern)
FUTURES_PART_SIZE: int = 25   # symbols per scan part
FUTURES_MAX_PARTS: int = 4    # 4 × 25 = 100 symbols — always scan all parts

# Trade log — completely separate from spot's trade_log.json
FUTURES_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "json" / "trade_futures.json"

# Rule version — bump when parameters change
RULE_VERSION: str = "fv1.0.0"

# Volatility regime thresholds (ATR percentile vs 90-candle rolling window)
# ATR percentile computed over last 90 × 4h candles (~15 days)
VOLATILITY_LOW_PCT: float    = 33.0   # below 33rd percentile → "low"
VOLATILITY_HIGH_PCT: float   = 66.0   # above 66th percentile → "high"
# between 33–66 → "medium"

# Zone entry buffer (same logic as spot)
ZONE_ENTRY_BUFFER_PCT: float = 0.0015  # 0.15%

# Hard cap on concurrent open futures positions
MAX_CONCURRENT_POSITIONS: int = 20


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBLE WRAPPERS — sections 1 & 2
# (FuturesClient — connectivity + market data)
# ---------------------------------------------------------------------------

from core.clients.futures_client import FuturesClient as _FC


def get_futures_client():
    """Backward-compatible wrapper — returns the raw python-binance client."""
    return _FC.build().raw


def _futures_get(client, path: str, params: dict | None = None) -> dict | list:
    """Backward-compatible wrapper — delegates to FuturesClient._get()."""
    return _FC(client)._get(path, params)


def ping_futures(client) -> bool:
    """Backward-compatible wrapper — delegates to FuturesClient.ping()."""
    return _FC(client).ping()


def get_futures_symbol_constraints(client, symbol: str) -> dict:
    """Backward-compatible wrapper — delegates to FuturesClient.get_symbol_constraints()."""
    return _FC(client).get_symbol_constraints(symbol)


def get_futures_price(client, symbol: str) -> float:
    """Backward-compatible wrapper — delegates to FuturesClient.get_price()."""
    return _FC(client).get_price(symbol)


def get_funding_rate(client, symbol: str) -> float | None:
    """Backward-compatible wrapper — delegates to FuturesClient.get_funding_rate()."""
    return _FC(client).get_funding_rate(symbol)


def accrue_funding(client, trade: dict) -> bool:
    """
    Fetch funding payment events since last_funding_check_time and accumulate
    into trade["funding_rate_paid"] and trade["funding_rate_history"].

    Funding is charged every 8 hours (at 00:00, 08:00, 16:00 UTC).
    - LONG pays when rate > 0, receives when rate < 0
    - SHORT pays when rate < 0 (i.e. abs value), receives when rate > 0
    Both are represented as signed cost: positive = paid, negative = received.

    Returns True if the trade dict was modified (caller should set log_dirty).

    De-duplication: events already in funding_rate_history (matched by
    fundingTime) are skipped — safe to call every --check-positions run.
    """
    sym  = trade.get("symbol")
    side = trade.get("position_side", "LONG")   # "LONG" | "SHORT"
    qty  = trade.get("entry_qty", 0)

    # Use entry_fill_time as the lower bound on first call,
    # then last_funding_check_time on subsequent calls
    start_ms = trade.get("last_funding_check_time") or trade.get("entry_fill_time")
    if not start_ms or not sym or not qty:
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Fetch funding rate events in [start_ms, now_ms]
    events = []
    try:
        events = client.futures_funding_rate(
            symbol    = sym,
            startTime = int(start_ms),
            endTime   = now_ms,
            limit     = 100,
        )
    except Exception:
        try:
            resp = requests.get(
                "https://testnet.binancefuture.com/fapi/v1/fundingRate",
                params={
                    "symbol":    sym,
                    "startTime": int(start_ms),
                    "endTime":   now_ms,
                    "limit":     100,
                },
                timeout=8,
            )
            events = resp.json() if resp.status_code == 200 else []
        except Exception:
            return False

    if not events:
        # No new events — still update check time
        trade["last_funding_check_time"] = now_ms
        return True

    # De-duplicate against already-recorded history
    existing_times = {
        e["fundingTime"] for e in trade.get("funding_rate_history", [])
        if isinstance(e, dict) and "fundingTime" in e
    }

    new_events = [e for e in events if int(e.get("fundingTime", 0)) not in existing_times]
    if not new_events:
        trade["last_funding_check_time"] = now_ms
        return True

    total_cost = 0.0
    history    = trade.get("funding_rate_history", [])

    for event in new_events:
        rate        = float(event.get("fundingRate", 0))
        funding_time = int(event.get("fundingTime", 0))
        mark_price  = float(event.get("markPrice") or 0)
        notional    = (mark_price * qty) if mark_price > 0 else (trade.get("entry_notional", 0))

        # Cost sign convention:
        #   LONG:  pays when rate > 0, receives when rate < 0  → cost = +rate × notional
        #   SHORT: pays when rate < 0, receives when rate > 0  → cost = -rate × notional
        if side == "LONG":
            cost = rate * notional
        else:
            cost = -rate * notional

        total_cost += cost
        history.append({
            "fundingTime": funding_time,
            "fundingRate": rate,
            "markPrice":   mark_price,
            "notional":    round(notional, 4),
            "cost_usd":    round(cost, 6),
            "side":        side,
        })

    trade["funding_rate_paid"]      = round(
        (trade.get("funding_rate_paid") or 0.0) + total_cost, 6
    )
    trade["funding_rate_history"]   = history
    trade["last_funding_check_time"] = now_ms
    return True


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBLE WRAPPERS — sections 3–6
# (pure math: already imported at top of file via core.utils.futures_math)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBLE WRAPPERS — section 7 (FuturesTradeRepository)
# ---------------------------------------------------------------------------

from core.repositories.futures_trade_repository import FuturesTradeRepository as _FuturesRepo

# Module-level singleton — same pattern as paper_trade_executor uses `repo`
_repo = _FuturesRepo()


def load_futures_log() -> list[dict]:
    """Backward-compatible wrapper — delegates to FuturesTradeRepository."""
    return _repo.load_futures_log()


def save_futures_log(trades: list[dict]) -> None:
    """Backward-compatible wrapper — delegates to FuturesTradeRepository."""
    _repo.save_futures_log(trades)


def log_futures_trade(
    order: dict,
    cand: dict,
    correlation_cluster_id: str | None = None,
) -> None:
    """Backward-compatible wrapper — delegates to FuturesTradeRepository."""
    _repo.log_futures_trade(order, cand, correlation_cluster_id=correlation_cluster_id)


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBLE WRAPPERS — section 8 (FuturesCandidateScanner)
# ---------------------------------------------------------------------------

from core.scanners.futures_candidate_scanner import FuturesCandidateScanner as _FuturesScanner

# Scanner singleton — instantiated lazily per call since it needs a client.
# Module-level wrappers accept `client` as an explicit argument to mirror the
# original function signatures exactly.


def gather_futures_candidates(scan_n: int = DEFAULT_SCAN_N) -> list[dict]:
    """Backward-compatible wrapper — delegates to FuturesCandidateScanner."""
    # gather_candidates does not need a Binance client (only ca.analyze_symbol)
    scanner = _FuturesScanner(client=None)
    return scanner.gather_candidates(scan_n=scan_n)


def pick_best_futures_candidate(
    candidates: list[dict],
    client,
    symbol_filter: str | None = None,
    side_filter:   str | None = None,
) -> dict | None:
    """Backward-compatible wrapper — delegates to FuturesCandidateScanner."""
    scanner = _FuturesScanner(client=client)
    return scanner.pick_best_candidate(
        candidates,
        symbol_filter=symbol_filter,
        side_filter=side_filter,
    )


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBLE WRAPPERS — section 9 (FuturesOrderExecutor)
# ---------------------------------------------------------------------------

from core.executors.futures_order_executor import FuturesOrderExecutor as _FuturesOE


def set_leverage_and_margin_mode(client, symbol: str) -> None:
    """Backward-compatible wrapper — delegates to FuturesOrderExecutor."""
    _FuturesOE(client)._set_leverage_and_margin_mode(symbol)


def place_futures_exit_orders(client, trade: dict) -> dict:
    """Backward-compatible wrapper — delegates to FuturesOrderExecutor."""
    return _FuturesOE(client).place_exit_orders(trade)


def place_futures_limit_order(client, cand: dict) -> dict:
    """Place futures LIMIT entry order (LONG or SHORT)."""
    from binance.exceptions import BinanceAPIException

    sym   = cand["symbol"]
    side  = "BUY" if cand["position_side"] == "LONG" else "SELL"
    qty   = cand["sizing"]["qty"]
    entry = cand["entry_price"]
    step  = cand["constraints"].get("step_size", 0)
    tick  = cand["constraints"].get("tick_size", 0)

    qty_str   = f"{round_step(qty, step):.8f}".rstrip("0").rstrip(".")
    price_str = f"{round_tick(entry, tick):.8f}".rstrip("0").rstrip(".")

    set_leverage_and_margin_mode(client, sym)

    try:
        return client.futures_create_order(
            symbol       = sym,
            side         = side,
            type         = "LIMIT",
            timeInForce  = "GTC",
            quantity     = qty_str,
            price        = price_str,
            positionSide = "BOTH",   # one-way mode
        )
    except BinanceAPIException as e:
        raise RuntimeError(f"Futures order failed: {e}") from e


# ---------------------------------------------------------------------------
# 10. TELEGRAM — moved to core/utils/telegram.py
#     _send_telegram is imported here for backwards compatibility so any
#     existing call site in this file continues to work unchanged.
# ---------------------------------------------------------------------------

from core.utils.telegram import send_telegram as _send_telegram


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBLE WRAPPERS — section 11 (FuturesPositionMonitor)
# ---------------------------------------------------------------------------

from core.executors.futures_position_monitor import FuturesPositionMonitor as _FuturesPM


def check_futures_positions(client, verbose: bool = False) -> None:
    """Backward-compatible wrapper — delegates to FuturesPositionMonitor."""
    _FuturesPM(client).check_positions(verbose=verbose)


# ---------------------------------------------------------------------------
# 12. PROPOSAL DISPLAY
# ---------------------------------------------------------------------------

def print_futures_proposal(cand: dict) -> None:
    sym   = cand["symbol"]
    side  = cand["position_side"]
    entry = cand["entry_price"]
    sl    = cand["sl"]
    tp1   = cand["tp1"]
    rr    = cand["rr"]
    sz    = cand["sizing"]
    liq   = cand["liquidation"]
    ez    = cand.get("entry_zone") or {}
    W = 70

    print(f"\n  ╔{'═'*W}╗")
    print(f"  ║  {'FUTURES TRADE PROPOSAL — BINANCE FUTURES TESTNET':<{W-1}}║")
    print(f"  ║  {'No order placed until you confirm with y':<{W-1}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Symbol':<22} {sym:<{W-24}}║")
    print(f"  ║  {'Position side':<22} {side:<{W-24}}║")
    print(f"  ║  {'Leverage':<22} {LEVERAGE}x isolated margin{'':{W-40}}║")
    print(f"  ║  {'Order type':<22} {'LIMIT (waits for zone)':<{W-24}}║")

    if ez:
        dist = abs(cand["current_price"] - ez.get("center", entry)) / cand["current_price"] * 100
        z_str = (f"{ca._fmt_price(ez.get('center')).strip()}  "
                 f"({ez.get('touches','?')}× tested, {dist:.2f}% away)")
        print(f"  ║  {'Entry zone':<22} {z_str:<{W-24}}║")

    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Entry (limit)':<22} {ca._fmt_price(entry).strip():>{W-24}}║")
    print(f"  ║  {'Stop-Loss':<22} {ca._fmt_price(sl).strip():>{W-24}}║")
    print(f"  ║  {'TP1':<22} {ca._fmt_price(tp1).strip():>{W-24}}║")
    print(f"  ║  {'R:R':<22} {rr:.2f}:1{'':{W-27}}║")
    print(f"  ║  {'SL distance':<22} {cand['risk_pct']:.2f}%{'':{W-27}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Liquidation price':<22} {ca._fmt_price(liq['liquidation_price']).strip():>{W-24}}║")
    dist_liq = liq["distance_to_liquidation_pct"]
    print(f"  ║  {'Dist to liquidation':<22} {dist_liq:.2f}%  "
          f"({'✅ safe' if dist_liq > 10 else '⚠ tight'}){'':{W-46}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Margin budget':<22} ${FUTURES_BUDGET_USD:.2f}{'':{W-29}}║")
    print(f"  ║  {'Margin used':<22} ${sz['margin_used']:.2f}{'':{W-29}}║")
    print(f"  ║  {'Notional (3x)':<22} ${sz['notional_usd']:.2f}{'':{W-29}}║")
    print(f"  ║  {'Max loss (risk 25%)':<22} ${sz['max_loss_usd']:.2f}  ({sz['max_loss_pct']:.1f}%){'':{W-46}}║")
    print(f"  ║  {'Qty':<22} {sz['qty']:.8g} {sym.replace('USDT',''):<{W-32}}║")
    print(f"  ╠{'═'*W}╣")
    print(f"  ║  {'Volatility regime':<22} {cand.get('volatility_regime','?'):<{W-24}}║")
    fr = cand.get("funding_rate_at_entry")
    fr_str = f"{fr*100:.4f}% per 8h" if fr is not None else "n/a"
    print(f"  ║  {'Funding rate':<22} {fr_str:<{W-24}}║")
    print(f"  ║  {'ATR(14)':<22} {cand['atr_pct']:.2f}%{'':{W-27}}║")

    if sz["warnings"]:
        print(f"  ╠{'═'*W}╣")
        for w in sz["warnings"]:
            print(f"  ║  ⚠  {w:<{W-5}}║")

    print(f"  ╚{'═'*W}╝")


# ---------------------------------------------------------------------------
# 13. STATS (independent from spot, grouped by position_side + rule_version)
# ---------------------------------------------------------------------------

def cmd_stats_futures() -> None:
    """
    Performance statistics from trade_futures.json.
    Grouped by: rule_version × position_side (LONG / SHORT).
    Effective-n and z-score computed independently per group.
    """
    from collections import defaultdict

    trades = load_futures_log()
    closed = [t for t in trades if t.get("exit_status") in ("TP_HIT", "SL_HIT")]

    if not closed:
        print("\n  No closed futures trades yet.")
        return

    print("=" * 70)
    print("Futures Performance Statistics")
    print(f"Total closed trades: {len(closed)}")
    print("(Stats are INDEPENDENT from spot — separate effective-n & z-score)")
    print("=" * 70)

    # Group by rule_version × position_side
    groups: dict[tuple, list] = defaultdict(list)
    for t in closed:
        key = (t.get("rule_version", "unknown"), t.get("position_side", "UNKNOWN"))
        groups[key].append(t)

    for (version, ps), group in sorted(groups.items()):
        n       = len(group)
        wins    = [t for t in group if t["exit_status"] == "TP_HIT"]
        losses  = [t for t in group if t["exit_status"] == "SL_HIT"]
        win_rate = len(wins) / n if n > 0 else 0

        avg_rr       = sum(t.get("planned_rr", 0) for t in group) / n
        be_win_rate  = 1 / (1 + avg_rr) if avg_rr > 0 else 0.5
        avg_win_pct  = sum(t.get("realized_pnl_pct", 0) for t in wins) / len(wins) if wins else 0
        avg_loss_pct = sum(t.get("realized_pnl_pct", 0) for t in losses) / len(losses) if losses else 0
        avg_funding  = sum(abs(t.get("funding_rate_paid", 0) or 0) for t in group) / n
        avg_fee_pct  = sum(
            (t.get("fee_usd_roundtrip", 0) / max(t.get("entry_notional", 1), 1) * 100)
            for t in group
        ) / n

        expectancy = (win_rate * avg_win_pct) - ((1 - win_rate) * abs(avg_loss_pct)) - avg_fee_pct

        # Z-score vs breakeven win rate
        if n >= 2:
            p0  = be_win_rate
            z   = (win_rate - p0) / math.sqrt(p0 * (1 - p0) / n)
            sig = "✅ p<0.05" if abs(z) >= 1.96 else ("🟡 p<0.10" if abs(z) >= 1.645 else "⚠ not sig")
        else:
            z, sig = 0.0, "⚠ n/a"

        # Cluster-based effective-n (mirrors spot cmd_stats logic):
        # trades from --propose-multi share a correlation_cluster_id and co-move,
        # so they count as ONE independent observation per cluster, not N.
        cluster_ids = {t.get("correlation_cluster_id") for t in group
                       if t.get("correlation_cluster_id")}
        n_clusters  = len(cluster_ids)
        n_singles   = sum(1 for t in group if not t.get("correlation_cluster_id"))
        effective_n = n_clusters + n_singles

        # Volatility regime breakdown
        regimes = {}
        for t in group:
            r = t.get("volatility_regime_at_entry", "unknown")
            regimes[r] = regimes.get(r, 0) + 1

        print(f"\n  Rule: {version}  |  Side: {ps}  ({n} trades)")
        print(f"  {'─'*60}")
        print(f"  Win rate          : {win_rate*100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Breakeven win rate: {be_win_rate*100:.1f}%  (at avg R:R {avg_rr:.2f}:1)")
        print(f"  Avg win  %        : {avg_win_pct:+.2f}%")
        print(f"  Avg loss %        : {avg_loss_pct:+.2f}%")
        print(f"  Avg fee %         : -{avg_fee_pct:.3f}%")
        print(f"  Avg funding paid  : {avg_funding:.4f} per trade")
        print(f"  Expectancy        : {expectancy:+.3f}%  per trade")
        print(f"  Z-score vs B/E    : {z:+.2f}  {sig}")
        if n < 30:
            print(f"  ⚠  Only {n} trades — z-score unreliable until n≥30")
        print(f"  Clusters          : {n_clusters} independent sessions + {n_singles} single trades")
        print(f"  Effective n       : ~{effective_n} independent observations")
        print(f"  (raw trade count {n} overstates independence if cluster trades co-move)")
        regime_str = "  ".join(f"{r}:{c}" for r, c in sorted(regimes.items()))
        print(f"  Volatility regime : {regime_str}")

        # MAE/MFE summary (if available)
        mae_vals = [t["max_adverse_excursion_pct"] for t in group
                    if t.get("max_adverse_excursion_pct") is not None]
        mfe_vals = [t["max_favorable_excursion_pct"] for t in group
                    if t.get("max_favorable_excursion_pct") is not None]
        if mae_vals:
            print(f"  Avg MAE           : {sum(mae_vals)/len(mae_vals):.2f}%  "
                  f"(max: {max(mae_vals):.2f}%)")
        if mfe_vals:
            print(f"  Avg MFE           : {sum(mfe_vals)/len(mfe_vals):.2f}%  "
                  f"(max: {max(mfe_vals):.2f}%)")

    # Total PnL
    total_pnl = sum(t.get("realized_pnl_usd", 0) or 0 for t in closed)
    total_funding = sum(t.get("funding_rate_paid", 0) or 0 for t in closed)
    print(f"\n  Total realized PnL     : ${total_pnl:+.4f}")
    print(f"  Total funding paid     : ${total_funding:+.4f}")
    print(f"  Net PnL after funding  : ${total_pnl + total_funding:+.4f}")


# ---------------------------------------------------------------------------
# 14. CLI COMMANDS
# ---------------------------------------------------------------------------

def cmd_propose_futures(
    scan_n:        int,
    symbol_filter: str | None = None,
    side_filter:   str | None = None,
    auto_confirm:  bool = False,
) -> None:
    print("=" * 70)
    print("Futures Trade Executor — Binance Futures Testnet")
    print(f"Leverage: {LEVERAGE}x  |  Margin: {MARGIN_MODE}  |  Rule: {RULE_VERSION}")
    print("=" * 70)

    print("\nConnecting to Binance Futures Testnet...")
    try:
        client = get_futures_client()
        ok = ping_futures(client)
        if not ok:
            raise RuntimeError("Ping failed")
        print("✅ Futures testnet connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Gather candidates (LONG + SHORT)
    candidates = gather_futures_candidates(scan_n)
    if not candidates:
        print("\n❌ No futures candidates found.")
        sys.exit(0)

    # Filter open positions to avoid duplicate symbols
    open_trades  = [t for t in load_futures_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}

    # Hard cap on concurrent positions
    if len(open_trades) >= MAX_CONCURRENT_POSITIONS:
        print(f"\n⛔  Max concurrent positions reached: "
              f"{len(open_trades)} / {MAX_CONCURRENT_POSITIONS}")
        print(f"   Wait for at least one position to close (TP_HIT / SL_HIT) before proposing.")
        print(f"   Run --check-positions to see current status.")
        sys.exit(0)

    candidates   = [c for c in candidates if c["symbol"] not in open_symbols]

    if not candidates:
        print("❌ All candidates already have open positions.")
        sys.exit(0)

    # Show top 5
    print(f"\nTop candidates (Risk% ASC):")
    print(f"  {'#':<3} {'Symbol':<12} {'Side':<6} {'Risk%':>6} {'R:R':>5} {'Tier':>4} {'Regime':<8}")
    print(f"  {'─'*55}")
    for i, c in enumerate(candidates[:5], 1):
        print(f"  {i:<3} {c['symbol']:<12} {c['position_side']:<6} "
              f"{c['risk_pct']:>5.2f}% {c['rr']:>5.1f}x {c.get('tier_used','?'):>4}  "
              f"{c.get('volatility_regime','?'):<8}")

    print("\nChecking exchange constraints...")
    best = pick_best_futures_candidate(
        candidates, client,
        symbol_filter = symbol_filter,
        side_filter   = side_filter,
    )

    if best is None:
        print("❌ No candidate passed constraints.")
        sys.exit(0)

    print_futures_proposal(best)

    print("\n" + "─" * 70)
    print("  ⚠  This will place a REAL LIMIT ORDER on Binance Futures TESTNET.")
    print("  Virtual funds only. Type 'y' to confirm.")
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

    print("\n  Placing futures limit order...")
    try:
        from core.executors.futures_order_executor import FuturesOrderExecutor
        executor = FuturesOrderExecutor(client, dry_run=False, auto_confirm=auto_confirm)
        order = executor.execute(best)
    except RuntimeError as e:
        print(f"\n  ❌ Order failed: {e}")
        return

    print(f"\n  ✅ Futures order placed!")
    print(f"     Order ID  : {order.get('orderId')}")
    print(f"     Symbol    : {order.get('symbol')}")
    print(f"     Side      : {order.get('side')}")
    print(f"     Status    : {order.get('status')}")
    print(f"     Price     : {order.get('price')}")
    print(f"     Qty       : {order.get('origQty')}")

    print(f"\n  Trade logged to Supabase and {FUTURES_LOG_PATH}")

    # Telegram notify
    _send_telegram(
        f"📋 [FUTURES] Order placed: {best['symbol']} {best['position_side']}\n"
        f"Entry: {ca._fmt_price(best['entry_price']).strip()}  "
        f"SL: {ca._fmt_price(best['sl']).strip()}  "
        f"TP: {ca._fmt_price(best['tp1']).strip()}\n"
        f"Liq: {ca._fmt_price(best['liquidation']['liquidation_price']).strip()}  "
        f"({best['liquidation']['distance_to_liquidation_pct']:.1f}% away)\n"
        f"Leverage: {LEVERAGE}x | Margin: ${best['sizing']['margin_used']:.2f}"
    )


def cmd_propose_multi_futures(
    scan_n:       int,
    count:        int,
    side_filter:  str | None = None,
    auto_confirm: bool = False,
) -> None:
    """
    --propose-multi: scan, pick up to `count` candidates (LONG+SHORT),
    show a summary table, ask ONE 'y' confirmation, then place all orders.
    All trades in this run share a correlation_cluster_id.
    Candidates that fail exchange constraints are skipped with a report
    at the end — they do not abort the whole batch.
    """
    print("=" * 70)
    print("Futures Trade Executor — BATCH PROPOSAL (--propose-multi)")
    print(f"Leverage: {LEVERAGE}x  |  Margin: {MARGIN_MODE}  |  Rule: {RULE_VERSION}")
    print(f"Requesting up to {count} position(s)")
    print("=" * 70)

    print("\nConnecting to Binance Futures Testnet...")
    try:
        client = get_futures_client()
        if not ping_futures(client):
            raise RuntimeError("Ping failed")
        print("✅ Futures testnet connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Check concurrent-position cap
    open_trades  = [t for t in load_futures_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}
    slots_left   = MAX_CONCURRENT_POSITIONS - len(open_trades)

    if slots_left <= 0:
        print(f"\n⛔  Max concurrent positions reached: "
              f"{len(open_trades)} / {MAX_CONCURRENT_POSITIONS}")
        print("   Wait for a position to close before using --propose-multi.")
        sys.exit(0)

    effective_count = min(count, slots_left)
    if effective_count < count:
        print(f"\n  ℹ️  Only {slots_left} slot(s) free — capping batch at {effective_count}.")

    # Gather + filter
    candidates = gather_futures_candidates(scan_n)
    if not candidates:
        print("\n❌ No futures candidates found.")
        sys.exit(0)

    candidates = [c for c in candidates if c["symbol"] not in open_symbols]
    if not candidates:
        print("❌ All candidates already have open positions.")
        sys.exit(0)

    # ── Iteratively pick up to effective_count passing candidates ────────
    selected:     list[dict] = []   # candidates that passed constraints
    skipped_syms: list[str]  = []   # symbols that failed constraints
    excluded_symbols: set[str] = set(open_symbols)  # grows as we pick

    remaining = list(candidates)
    while len(selected) < effective_count and remaining:
        # Exclude already-selected symbols (avoid same symbol twice in batch)
        pool = [c for c in remaining if c["symbol"] not in excluded_symbols]
        if not pool:
            break

        pick = pick_best_futures_candidate(pool, client, side_filter=side_filter)
        if pick is None:
            # No more candidates pass constraints — collect all remaining as skipped
            skipped_syms.extend(
                c["symbol"] for c in pool
                if c["symbol"] not in excluded_symbols
            )
            break

        selected.append(pick)
        excluded_symbols.add(pick["symbol"])
        # Remove the picked symbol from remaining so we don't re-evaluate it
        remaining = [c for c in remaining if c["symbol"] != pick["symbol"]]

    if not selected:
        print("\n❌ No candidates passed exchange constraints.")
        sys.exit(0)

    # ── Display batch summary table ──────────────────────────────────────
    cluster_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"\n  Batch proposal  [{len(selected)} trade(s)]  cluster: {cluster_id}")
    print(f"  {'#':<3} {'Symbol':<12} {'Side':<6} {'Entry':>12} {'SL':>10} "
          f"{'TP1':>10} {'R:R':>5} {'Risk%':>6} {'Liq%':>6}  Regime")
    print(f"  {'─'*80}")
    for i, c in enumerate(selected, 1):
        liq = c["liquidation"]
        print(f"  {i:<3} {c['symbol']:<12} {c['position_side']:<6} "
              f"{ca._fmt_price(c['entry_price']).strip():>12} "
              f"{ca._fmt_price(c['sl']).strip():>10} "
              f"{ca._fmt_price(c['tp1']).strip():>10} "
              f"{c['rr']:>5.1f} {c['risk_pct']:>5.2f}% "
              f"{liq['distance_to_liquidation_pct']:>5.1f}%  "
              f"{c.get('volatility_regime','?')}")

    if skipped_syms:
        print(f"\n  ⚠  Skipped (failed constraints): {', '.join(skipped_syms)}")

    total_margin = sum(c["sizing"]["margin_used"] for c in selected)
    print(f"\n  Total margin if all fill: ${total_margin:.2f}  "
          f"({len(selected)} × ~${FUTURES_BUDGET_USD:.0f} budget, {LEVERAGE}x)")

    # ── Single confirmation ──────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print(f"  ⚠  This will place {len(selected)} LIMIT ORDER(S) on Binance Futures TESTNET.")
    print(f"  Virtual funds only. Type 'y' to place ALL, anything else to abort.")
    print(f"{'─' * 70}")

    if auto_confirm:
        print("  [--yes] Auto-confirmed for non-interactive run.")
        answer = "y"
    else:
        try:
            answer = input("  Confirm ALL? (y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""

    if answer != "y":
        print("\n  Aborted. No orders placed.")
        return

    # ── Place orders ─────────────────────────────────────────────────────
    placed:  list[tuple[str, str, int]] = []   # (symbol, side, orderId)
    failed:  list[tuple[str, str]]      = []   # (symbol, error)

    for cand in selected:
        sym  = cand["symbol"]
        side = cand["position_side"]
        try:
            from core.executors.futures_order_executor import FuturesOrderExecutor
            executor = FuturesOrderExecutor(client, dry_run=False, auto_confirm=auto_confirm)
            order = executor.execute(cand, correlation_cluster_id=cluster_id)
            oid = order.get("orderId")
            print(f"  ✅ {sym:<12} {side:<5}  order #{oid}  "
                  f"price={order.get('price')}")
            placed.append((sym, side, oid))
            _send_telegram(
                f"📋 [FUTURES MULTI] {sym} {side}\n"
                f"Entry: {ca._fmt_price(cand['entry_price']).strip()}  "
                f"SL: {ca._fmt_price(cand['sl']).strip()}  "
                f"TP: {ca._fmt_price(cand['tp1']).strip()}\n"
                f"Cluster: {cluster_id}"
            )
        except Exception as e:
            print(f"  ❌ {sym:<12} {side:<5}  FAILED: {e}")
            failed.append((sym, str(e)))

    # ── Final report ─────────────────────────────────────────────────────
    print(f"\n  ── Batch result ──")
    print(f"  Placed : {len(placed)}  |  Failed: {len(failed)}  |  Cluster: {cluster_id}")
    if failed:
        print(f"  Failed orders:")
        for sym, err in failed:
            print(f"    {sym}: {err}")
    print(f"\n  Run --check-positions to monitor.")


def cmd_check_futures(verbose: bool = False) -> None:
    print("=" * 70)
    print("Futures Trade Executor — Position Status")
    print("=" * 70)

    try:
        client = get_futures_client()
        if not ping_futures(client):
            raise RuntimeError("Ping failed")
        print("✅ Futures testnet connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    check_futures_positions(client, verbose=verbose)


# ---------------------------------------------------------------------------
# 15. MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Futures paper trading executor — Binance Futures Testnet."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--propose",          action="store_true",
                       help="Batch: scan, pick up to --count candidates, single confirm, place all")
    group.add_argument("--check-positions",  action="store_true",
                       help="Check open futures positions, place TP/SL if filled")
    group.add_argument("--stats-futures",    action="store_true",
                       help="Print futures performance statistics (independent from spot)")

    parser.add_argument("--scan-n",    type=int, default=DEFAULT_SCAN_N)
    parser.add_argument("--count",     type=int, default=MAX_CONCURRENT_POSITIONS,
                        help=f"Number of positions to open in one batch (default {MAX_CONCURRENT_POSITIONS}, fills up to max slots)")
    parser.add_argument("--side",      type=str, default=None,
                        choices=["LONG", "SHORT"],
                        help="Filter by position side (LONG or SHORT)")
    parser.add_argument("--verbose",   action="store_true",
                        help="Show detailed per-position info")
    parser.add_argument("--yes",       action="store_true", default=True,
                        help="Auto-confirm order placement (DEFAULT: on — for unattended VM/CI)")
    parser.add_argument("--no-yes",    action="store_true",
                        help="Disable auto-confirm — require interactive 'y' confirmation")
    args = parser.parse_args()

    auto_confirm = args.yes and not args.no_yes

    if args.propose:
        cmd_propose_multi_futures(
            scan_n        = args.scan_n,
            count         = args.count,
            side_filter   = args.side,
            auto_confirm  = auto_confirm,
        )
    elif args.check_positions:
        cmd_check_futures(verbose=args.verbose)
    elif args.stats_futures:
        cmd_stats_futures()


if __name__ == "__main__":
    main()
