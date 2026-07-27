import json
from datetime import datetime, timezone
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
TRADE_LOG_PATH = BASE_DIR / "trade_log.json"


class SpotTradeRepository:
    """
    Repository pattern for handling Spot Trades I/O operations (Supabase and JSON fallback).
    """
    def __init__(self):
        pass

    def load_trade_log(self) -> list[dict]:
        """Load all spot trades from Supabase trades_spot table.
        trade_log.json is kept as a backup but is no longer the source of truth.
        """
        try:
            from services.supabase_client import fetch_all_spot
            return fetch_all_spot()
        except Exception as e:
            print(f"  [WARN] Supabase read failed, falling back to trade_log.json: {e}")
            if TRADE_LOG_PATH.exists():
                with open(TRADE_LOG_PATH) as f:
                    return json.load(f)
            return []

    def match_mode(self, trade: dict, mode: str) -> bool:
        """Return True if trade matches selected mode filter."""
        cid = trade.get("correlation_cluster_id")
        if mode == "all":
            return True
        if mode == "single":
            return cid is None
        if mode == "lab":
            return cid is not None
        return True

    def export_clean(self, trades: list[dict], mode: str = "lab") -> None:
        """
        Export resolved trades for ML: filtered by mode and exit_status (TP_HIT/SL_HIT).
        Writes `trade_log_<mode>_clean.json` with only selected fields.
        """
        out = []
        for t in trades:
            if not self.match_mode(t, mode):
                continue
            if t.get("exit_status") not in ("TP_HIT", "SL_HIT"):
                continue
            out.append({
                "symbol": t.get("symbol"),
                "entry_price": t.get("entry_price"),
                "sl": t.get("sl"),
                "tp1": t.get("tp1"),
                "zone_touches": t.get("entry_zone_touches") or t.get("zone_touches"),
                "atr_pct": t.get("atr_pct_at_entry") or t.get("atr_pct"),
                "planned_rr": t.get("planned_rr"),
                "risk_pct": t.get("risk_pct"),
                "realized_pnl_usd": t.get("realized_pnl_usd"),
                "realized_pnl_pct": t.get("realized_pnl_pct"),
                "time_to_resolution_sec": t.get("time_to_resolution_sec"),
                "rule_version": t.get("rule_version"),
                "correlation_cluster_id": t.get("correlation_cluster_id"),
            })

        fname = f"trade_log_{mode}_clean.json"
        with open(fname, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Exported {len(out)} resolved trades to {fname}")

    def save_trade_log(self, trades: list[dict]) -> None:
        # trade_log.json is no longer the write target — Supabase is.
        # Writes are handled per-record via upsert_spot() / update_spot_by_order_id()
        # in supabase_client.py.  This stub is kept so call sites compile without change
        # until each write path is individually migrated to Supabase upserts.
        pass

    def has_open_position(self, trades: list[dict]) -> bool:
        """A position is 'open' if exit_status is OPEN (entry placed, not yet resolved)."""
        return any(t.get("exit_status") == "OPEN" for t in trades)

    def should_show_live_position(self, trade: dict, entry_status: str | None = None) -> bool:
        """Return True for trades that should remain visible in the live display block."""
        resolved_statuses = {"TP_HIT", "SL_HIT", "CLOSED"}
        if trade.get("exit_status") in resolved_statuses:
            return False

        status = entry_status if entry_status is not None else trade.get("entry_status")
        return status in {"NEW", "PARTIALLY_FILLED", "FILLED"}

    def count_open_positions(self, trades: list[dict]) -> int:
        """Count how many trades currently have exit_status == OPEN."""
        return sum(1 for t in trades if t.get("exit_status") == "OPEN")

    def log_trade(self, order: dict, cand: dict, correlation_cluster_id: str | None = None) -> None:
        """Insert new spot trade into Supabase trades_spot table."""
        from services.supabase_client import upsert_spot
        # Import constants lazily to avoid circular imports during refactoring
        from core.paper_trade_executor import BUDGET_USD, RULE_VERSION, TAKER_FEE_PCT

        ez = cand.get("entry_zone") or {}
        notional = cand["sizing"]["notional_usd"]
        record = {
            # ── Identity ──────────────────────────────────────────────────
            "symbol":            cand["symbol"],
            "direction":         cand["direction"],
            "budget_usd":        cand.get("budget_for_slot", BUDGET_USD),
            "rule_version":      RULE_VERSION,
            "correlation_cluster_id": correlation_cluster_id,

            # ── Entry order ───────────────────────────────────────────────
            "entry_order_id":    order.get("orderId"),
            "entry_client_id":   order.get("clientOrderId"),
            "entry_status":      order.get("status", "NEW"),
            "entry_price":       cand["entry_price"],
            "entry_fill_price":  None,
            "entry_fill_time":   None,
            "entry_qty":         cand["sizing"]["qty"],
            "entry_notional":    notional,
            "open_time":         datetime.now(timezone.utc).isoformat(),

            # ── OCO ───────────────────────────────────────────────────────
            "oco_placed":        False,
            "oco_order_ids":     None,
            "oco_list_id":       None,

            # ── Levels ────────────────────────────────────────────────────
            "sl":                cand["sl"],
            "tp1":               cand["tp1"],
            "tp2":               cand["tp2"],
            "entry_zone_center": ez.get("center"),
            "entry_zone_touches": ez.get("touches"),

            # ── Setup metadata ────────────────────────────────────────────
            "planned_rr":        cand["rr"],
            "risk_pct":          cand["risk_pct"],
            "max_loss_usd":      cand["sizing"]["max_loss_usd"],
            "zone_type":         cand["winning_zone"]["tier"] if cand.get("winning_zone") else "T1",
            "zone_label":        cand["winning_zone"]["label"] if cand.get("winning_zone") else None,
            "zone_touches":      ez.get("touches"),
            "atr_pct_at_entry":  cand["atr_pct"],

            # ── Cost estimates ────────────────────────────────────────────
            "fee_usd_roundtrip": round(notional * TAKER_FEE_PCT * 2, 4),
            "slippage_pct":      None,
            "time_to_resolution_sec": None,

            # ── Exit ──────────────────────────────────────────────────────
            "exit_status":       "OPEN",
            "exit_price":        None,
            "exit_time":         None,
            "realized_pnl_usd":  None,
            "realized_pnl_pct":  None,

            # ── ML scoring (observation only) ─────────────────────────────
            "ml_score":          cand.get("ml_score"),
            "ml_model_version":  cand.get("ml_model_version"),

            # ── Scan metadata (observation only) ──────────────────────────
            "symbol_rank":       cand.get("symbol_rank"),   # rank from get_top_symbols_by_volume

            # ── Raw ───────────────────────────────────────────────────────
            "raw_entry_order":   order,
        }
        upsert_spot(record)
        print(f"  Trade inserted into Supabase trades_spot (order #{record['entry_order_id']})")
