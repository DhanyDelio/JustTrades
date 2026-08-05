"""
futures_trade_repository.py — I/O layer for Futures trades.

Extracted from core/futures_trade_executor.py (god file) with zero logic
changes. Mirrors the pattern of core/repositories/spot_trade_repository.py.

Source of truth: Supabase trades_futures table.
Fallback: data/json/trade_futures.json (read-only backup).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Path to JSON backup — kept in sync with god file constant FUTURES_LOG_PATH
FUTURES_LOG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "json" / "trade_futures.json"
)


class FuturesTradeRepository:
    """
    Repository pattern for Futures trade I/O (Supabase + JSON fallback).
    Mirrors SpotTradeRepository — same interface shape for consistency.
    """

    # -------------------------------------------------------------------------
    # Load
    # -------------------------------------------------------------------------

    def load_futures_log(self) -> list[dict]:
        """Load all futures trades from Supabase trades_futures table.

        trade_futures.json is kept as a backup but is no longer the source of
        truth.  Falls back to the JSON file only if Supabase is unreachable.
        """
        try:
            from services.supabase_client import fetch_all_futures
            return fetch_all_futures()
        except Exception as e:
            print(f"  [WARN] Supabase read failed, falling back to trade_futures.json: {e}")
            if FUTURES_LOG_PATH.exists():
                with open(FUTURES_LOG_PATH) as f:
                    return json.load(f)
            return []

    # -------------------------------------------------------------------------
    # Save (stub — Supabase is the write target)
    # -------------------------------------------------------------------------

    def save_futures_log(self, trades: list[dict]) -> None:
        # trade_futures.json is no longer the write target — Supabase is.
        # Writes are handled per-record via upsert_futures() /
        # update_futures_by_order_id() in supabase_client.py.
        # This stub is kept so call sites compile without change until each
        # write path is individually migrated to Supabase upserts.
        pass

    # -------------------------------------------------------------------------
    # Insert (new trade)
    # -------------------------------------------------------------------------

    def log_futures_trade(
        self,
        order: dict,
        cand: dict,
        correlation_cluster_id: str | None = None,
    ) -> None:
        """Insert new futures trade into Supabase trades_futures table."""
        from services.supabase_client import upsert_futures

        # Import config constants lazily to avoid circular imports.
        # These are module-level constants in the god file — importing them
        # here keeps this class decoupled from the god file's full import chain.
        from core.futures_trade_executor import (
            FUTURES_BUDGET_USD,
            LEVERAGE,
            MARGIN_MODE,
            RULE_VERSION,
            TAKER_FEE_PCT,
        )

        sizing = cand["sizing"]
        liq    = cand["liquidation"]
        ez     = cand.get("entry_zone") or {}

        record = {
            # ── Identity ──────────────────────────────────────────────
            "symbol":               cand["symbol"],
            "position_side":        cand["position_side"],
            "direction":            cand["direction"],
            "margin_budget":        FUTURES_BUDGET_USD,
            "leverage":             LEVERAGE,
            "margin_mode":          MARGIN_MODE,
            "rule_version":         RULE_VERSION,
            "correlation_cluster_id": correlation_cluster_id,

            # ── Entry order ───────────────────────────────────────────
            "entry_order_id":       order.get("orderId"),
            "entry_client_id":      order.get("clientOrderId"),
            "entry_status":         order.get("status", "NEW"),
            "entry_price":          cand["entry_price"],
            "entry_fill_price":     None,
            "entry_fill_time":      None,
            "entry_qty":            sizing["qty"],
            "entry_notional":       sizing["notional_usd"],
            "margin_used":          sizing["margin_used"],
            "open_time":            datetime.now(timezone.utc).isoformat(),

            # ── Exit orders ───────────────────────────────────────────
            "tp_order_id":          None,
            "sl_order_id":          None,
            "tp_algo_id":           None,
            "sl_algo_id":           None,
            "exit_orders_placed":   False,

            # ── Levels ────────────────────────────────────────────────
            "sl":                   cand["sl"],
            "tp1":                  cand["tp1"],
            "tp2":                  cand.get("tp2"),
            "entry_zone_center":    ez.get("center"),
            "entry_zone_touches":   ez.get("touches"),

            # ── Liquidation ───────────────────────────────────────────
            "liquidation_price":             liq["liquidation_price"],
            "distance_to_liquidation_pct":   liq["distance_to_liquidation_pct"],

            # ── Setup metadata ────────────────────────────────────────
            "planned_rr":           cand["rr"],
            "risk_pct":             cand["risk_pct"],
            "max_loss_usd":         sizing["max_loss_usd"],
            "zone_type":            cand.get("tier_used", "T1"),
            "zone_touches":         ez.get("touches"),
            "atr_pct_at_entry":     cand["atr_pct"],

            # ── Volatility regime at entry ─────────────────────────────
            "volatility_regime_at_entry": cand.get("volatility_regime", "unknown"),

            # ── Funding rate at entry (snapshot) ──────────────────────
            "funding_rate_at_entry":  cand.get("funding_rate_at_entry"),

            # ── Cost estimates ────────────────────────────────────────
            "fee_usd_roundtrip":    round(sizing["notional_usd"] * TAKER_FEE_PCT * 2, 4),
            "slippage_pct":         None,

            # ── Exit ──────────────────────────────────────────────────
            "exit_status":          "OPEN",
            "exit_price":           None,
            "exit_time":            None,
            "realized_pnl_usd":     None,
            "realized_pnl_pct":     None,
            "time_in_position_sec": None,

            # ── ML features ───────────────────────────────────────────
            "max_adverse_excursion_pct":       None,
            "max_favorable_excursion_pct":     None,
            "distance_to_liquidation_pct_min": None,
            "funding_rate_paid":               0.0,
            "funding_rate_history":            [],
            "last_funding_check_time":         None,

            # ── Raw ───────────────────────────────────────────────────
            "raw_entry_order":      order,
        }
        upsert_futures(record)
        print(
            f"  Futures trade inserted into Supabase trades_futures "
            f"(order #{record['entry_order_id']})"
        )
