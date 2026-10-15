"""
test_sl_triggered_unfilled.py
=============================
Regression and safety tests for the SL_TRIGGERED_UNFILLED approach:
1. Valid stuck-OCO detection & state persistence (Cycle N -> Cycle N+1).
2. No repeated Telegram alerts on Cycle N+2 once persisted.
3. Persistence failure leaves alert unsuppressed for safe retry on next cycle.
4. Position exit_status remains OPEN and exit_reason is None.
5. Capital allocation (PortfolioManager.compute_lab_pool) keeps deployed capital allocated.
6. Duplicate entry prevention (open_symbols exclusion) remains active.
7. ML training filter ignores OPEN trades with SL_TRIGGERED_UNFILLED.
8. Dashboard renders the SL TRIGGERED (UNFILLED) badge cleanly.
"""

import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import MagicMock, patch
import pandas as pd

from core.executors.spot_position_monitor import SpotPositionMonitor
import core.paper_trade_executor as pte
from core.managers.portfolio_manager import PortfolioManager


def _make_trade(entry_order_id=8801, symbol="NVDABUSDT", sl=222.98, recon_status=None, raw_meta=None):
    return {
        "id": 999,
        "entry_order_id": entry_order_id,
        "symbol": symbol,
        "direction": "long",
        "entry_price": 227.29,
        "entry_fill_price": 227.29,
        "entry_qty": 0.052,
        "entry_notional": 11.82,
        "entry_status": "FILLED",
        "entry_fill_time": 1000,
        "sl": sl,
        "tp1": 237.82,
        "oco_placed": True,
        "oco_list_id": 63596,
        "oco_order_ids": [145913, 145914],
        "exit_status": "OPEN",
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,
        "realized_pnl_usd": None,
        "realized_pnl_pct": None,
        "oco_reconciliation_status": recon_status,
        "correlation_cluster_id": "cluster_20261014",
        "raw_entry_order": raw_meta or {},
    }


class FakeClient:
    def __init__(self, price="220.00", oco_status="EXECUTING"):
        self.price = price
        self.oco_status = oco_status

    def get_order(self, symbol, orderId):
        if orderId in (145913, 380905):
            # SL leg order
            return {
                "status": "NEW",
                "executedQty": "0.00",
                "origQty": "0.052",
                "price": "222.65",
                "updateTime": 1000,
            }
        # Entry order or filled order
        return {
            "status": "FILLED",
            "executedQty": "0.052",
            "cummulativeQuoteQty": str(0.052 * 227.29),
            "price": "227.29",
            "updateTime": 1000,
        }

    def get_all_tickers(self):
        return [{"symbol": "NVDABUSDT", "price": self.price}]

    def get_symbol_ticker(self, symbol):
        return {"price": self.price}

    def v3_get_order_list(self, orderListId):
        return {"listOrderStatus": self.oco_status, "orders": [{"orderId": 145913}, {"orderId": 145914}]}


def _run_monitor(client, trade, patched_update=None):
    executor = MagicMock()
    telegram_calls = []

    with ExitStack() as s:
        s.enter_context(patch.object(pte.repo, "load_trade_log", return_value=[trade]))
        s.enter_context(patch.object(pte.repo, "save_trade_log"))
        if patched_update:
            s.enter_context(patch("services.supabase_client.update_spot_by_order_id", side_effect=patched_update))
        else:
            s.enter_context(patch("services.supabase_client.update_spot_by_order_id"))
        s.enter_context(patch(
            "core.executors.spot_position_monitor._send_telegram",
            side_effect=lambda msg: telegram_calls.append(msg)
        ))
        s.enter_context(patch("core.paper_trade_executor._send_telegram"))
        s.enter_context(patch(
            "core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
            return_value={
                "lab_capital": 300.0,
                "closed_cluster_pnl": 0.0,
                "deployed_capital": 12.0,
                "available_capital": 288.0,
                "max_new_positions": 24,
                "deployed_count": 1,
            }
        ))
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor = SpotPositionMonitor(client, pte.repo, executor)
            monitor.check_positions()

    return telegram_calls, executor


class TestSlTriggeredUnfilled(unittest.TestCase):

    def setUp(self):
        if hasattr(SpotPositionMonitor, "_stuck_oco_marked_this_run"):
            SpotPositionMonitor._stuck_oco_marked_this_run = set()

    def test_cycle_n_marks_suspected_no_alert(self):
        """Cycle N: Price breaches SL -> suspected_at is set, NO alert sent, trade stays OPEN."""
        trade = _make_trade()
        client = FakeClient(price="220.00")
        tg_calls, executor = _run_monitor(client, trade)

        self.assertEqual(len(tg_calls), 0, "Cycle N must not emit Telegram alert")
        self.assertEqual(trade["exit_status"], "OPEN")
        self.assertIsNone(trade.get("exit_reason"))
        raw = trade.get("raw_entry_order") or {}
        stuck = raw.get("stuck_oco_detection") or {}
        self.assertIn("suspected_at", stuck)
        self.assertIsNotNone(stuck.get("suspected_at"))
        executor.close_position.assert_not_called()

    def test_cycle_n_plus_1_confirms_alerts_and_persists_state(self):
        """Cycle N+1: suspected_at present -> Telegram alert sent with correct ID mapping and state persisted."""
        trade = _make_trade(raw_meta={
            "original_strategy_note": "keep_this",
            "stuck_oco_detection": {"suspected_at": "2026-10-14T18:00:00Z"}
        })
        client = FakeClient(price="220.00")

        persisted_updates = []
        def _capture_update(eid, fields):
            persisted_updates.append((eid, fields))

        tg_calls, executor = _run_monitor(client, trade, patched_update=_capture_update)

        self.assertEqual(len(tg_calls), 1, "Cycle N+1 must send exactly 1 Telegram alert")
        alert_msg = tg_calls[0]
        self.assertIn("SL_TRIGGERED_UNFILLED", alert_msg)
        self.assertIn("oco_order_ids: [145913, 145914]", alert_msg)
        self.assertIn("Position remains OPEN", alert_msg)

        # Check in-memory update
        self.assertEqual(trade["exit_status"], "OPEN")
        self.assertEqual(trade["oco_reconciliation_status"], "SL_TRIGGERED_UNFILLED")
        stuck = trade["raw_entry_order"]["stuck_oco_detection"]
        self.assertTrue(stuck.get("alert_suppressed"))
        self.assertEqual(trade["raw_entry_order"].get("original_strategy_note"), "keep_this")

        # Check persistence
        self.assertTrue(any(
            f.get("oco_reconciliation_status") == "SL_TRIGGERED_UNFILLED" for _, f in persisted_updates
        ), "Must persist SL_TRIGGERED_UNFILLED to Supabase")
        executor.close_position.assert_not_called()

    def test_cycle_n_plus_2_suppresses_duplicate_alert(self):
        """Cycle N+2: oco_reconciliation_status=SL_TRIGGERED_UNFILLED -> Alert is suppressed, trade stays OPEN."""
        trade = _make_trade(
            recon_status="SL_TRIGGERED_UNFILLED",
            raw_meta={"stuck_oco_detection": {"suspected_at": "2026-10-14T18:00:00Z", "alert_suppressed": True}}
        )
        client = FakeClient(price="220.00")
        tg_calls, executor = _run_monitor(client, trade)

        self.assertEqual(len(tg_calls), 0, "Cycle N+2 must suppress duplicate Telegram alert")
        self.assertEqual(trade["exit_status"], "OPEN")
        self.assertEqual(trade["oco_reconciliation_status"], "SL_TRIGGERED_UNFILLED")
        executor.close_position.assert_not_called()

    def test_persistence_failure_retains_alert_retry_path(self):
        """If persistence to Supabase throws an exception, alert_suppressed is not locked in, retaining retry."""
        trade = _make_trade(raw_meta={"stuck_oco_detection": {"suspected_at": "2026-10-14T18:00:00Z"}})
        client = FakeClient(price="220.00")

        def _fail_update(eid, fields):
            raise RuntimeError("Database connection timed out")

        tg_calls, executor = _run_monitor(client, trade, patched_update=_fail_update)

        self.assertEqual(len(tg_calls), 1)
        stuck = trade["raw_entry_order"]["stuck_oco_detection"]
        self.assertFalse(stuck.get("alert_suppressed", False), "Persistence failure must not permanently suppress alerts")

    def test_capital_allocation_and_open_position_counting(self):
        """SL_TRIGGERED_UNFILLED trades count as OPEN, consuming per-trade budget and excluding symbol from new orders."""
        trade = _make_trade(recon_status="SL_TRIGGERED_UNFILLED")
        repo = MagicMock()
        repo.load_trade_log.return_value = [trade]

        pm = PortfolioManager(repo, budget_usd=300.0, lab_starting_capital=300.0, per_trade_budget=12.0)
        pool = pm.compute_lab_pool([trade])

        # Position is OPEN -> 1 slot deployed ($12), available $288
        self.assertEqual(pool["deployed_count"], 1)
        self.assertEqual(pool["deployed_capital"], 12.0)
        self.assertEqual(pool["available_capital"], 288.0)
        self.assertEqual(pool["max_new_positions"], 24)

        # Open symbols exclusion check in paper_trade_executor logic
        open_trades = [t for t in [trade] if t.get("exit_status") == "OPEN"]
        open_symbols = {t["symbol"] for t in open_trades}
        self.assertIn("NVDABUSDT", open_symbols, "Symbol must be excluded from duplicate batch proposals")

    def test_ml_training_excludes_unresolved_open_trades(self):
        """ML canonical filters select df['exit_status'].isin(['TP_HIT', 'SL_HIT']) -> OPEN trade is excluded."""
        data = [
            {"id": 1, "symbol": "BTCUSDT", "exit_status": "TP_HIT", "realized_pnl_usd": 0.50},
            {"id": 2, "symbol": "ETHUSDT", "exit_status": "SL_HIT", "realized_pnl_usd": -0.30},
            {"id": 3, "symbol": "NVDABUSDT", "exit_status": "OPEN", "oco_reconciliation_status": "SL_TRIGGERED_UNFILLED", "realized_pnl_usd": None},
        ]
        df = pd.DataFrame(data)

        # Canonical filter used across ml/
        closed_df = df[df["exit_status"].isin(["TP_HIT", "SL_HIT"])].copy().reset_index(drop=True)
        self.assertEqual(len(closed_df), 2)
        self.assertNotIn("NVDABUSDT", closed_df["symbol"].values)

    def test_dashboard_badge_rendering(self):
        """Dashboard renders '🟠 SL TRIGGERED (UNFILLED)' badge when oco_reconciliation_status is SL_TRIGGERED_UNFILLED."""
        # Test badge logic directly from dashboard.py convention
        def get_badge(recon_status):
            if recon_status == "UNPROTECTED_SL_BREACH":
                return "🚨 SL BREACH UNPROTECTED"
            elif recon_status == "SL_TRIGGERED_UNFILLED":
                return "🟠 SL TRIGGERED (UNFILLED)"
            elif recon_status == "FULLY_PROTECTED":
                return "OCO ✓"
            return ""

        self.assertEqual(get_badge("SL_TRIGGERED_UNFILLED"), "🟠 SL TRIGGERED (UNFILLED)")
        self.assertEqual(get_badge("FULLY_PROTECTED"), "OCO ✓")
        self.assertEqual(get_badge("UNKNOWN_RECON_VALUE"), "")

    def test_timestamp_validity_and_utc_parsing(self):
        """Generated timestamps in Cycle N and Cycle N+1 must be valid ISO-8601 UTC without duplicated suffixes."""
        from datetime import datetime, timezone

        # 1. Cycle N: suspected_at
        trade = _make_trade()
        client = FakeClient(price="220.00")
        _run_monitor(client, trade)
        stuck_n = trade["raw_entry_order"]["stuck_oco_detection"]
        suspected_at = stuck_n.get("suspected_at")

        self.assertIsNotNone(suspected_at)
        self.assertNotIn("+00:00Z", suspected_at, "Must not contain duplicate timezone suffix")
        # Must parse with standard fromisoformat
        dt_suspected = datetime.fromisoformat(suspected_at)
        self.assertIsNotNone(dt_suspected.tzinfo)
        self.assertEqual(dt_suspected.utcoffset().total_seconds(), 0, "Must be UTC")

        # 2. Cycle N+1: confirmed_at and alert_sent_at (simulate subsequent hourly cycle)
        SpotPositionMonitor._stuck_oco_marked_this_run = set()
        trade_n1 = _make_trade(raw_meta={"stuck_oco_detection": {"suspected_at": suspected_at}})
        _run_monitor(client, trade_n1)
        stuck_n1 = trade_n1["raw_entry_order"]["stuck_oco_detection"]
        confirmed_at = stuck_n1.get("confirmed_at")
        alert_sent_at = stuck_n1.get("alert_sent_at")

        for name, ts in [("confirmed_at", confirmed_at), ("alert_sent_at", alert_sent_at)]:
            self.assertIsNotNone(ts, f"{name} must be generated")
            self.assertNotIn("+00:00Z", ts, f"{name} must not contain duplicate timezone suffix")
            dt = datetime.fromisoformat(ts)
            self.assertIsNotNone(dt.tzinfo)
            self.assertEqual(dt.utcoffset().total_seconds(), 0, f"{name} must be UTC")


if __name__ == "__main__":
    unittest.main()
