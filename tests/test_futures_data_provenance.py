"""Regression tests for Futures research provenance logging."""

import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

import core.futures_trade_executor  # noqa: F401
from core.executors.futures_order_executor import FuturesOrderExecutor
from core.executors.futures_position_monitor import FuturesPositionMonitor
from core.repositories.futures_trade_repository import FuturesTradeRepository
from core.scanners.futures_candidate_scanner import FuturesCandidateScanner


class FuturesExitReasonTests(unittest.TestCase):
    def trade(self, **overrides):
        value = {
            "entry_order_id": 555,
            "symbol": "SOLUSDT",
            "position_side": "LONG",
            "entry_status": "FILLED",
            "entry_price": 150.0,
            "entry_fill_price": 150.0,
            "entry_fill_time": 1_700_000_000_000,
            "entry_qty": 1.2,
            "entry_notional": 180.0,
            "sl": 140.0,
            "tp1": 165.0,
            "exit_status": "OPEN",
            "exit_reason": None,
            "exit_orders_placed": True,
            "tp_algo_id": 7001,
            "sl_algo_id": 7002,
            "raw_entry_order": {"orderId": 555},
        }
        value.update(overrides)
        return value

    @staticmethod
    def client(price="150"):
        client = MagicMock()
        client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": price}]
        client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2",
            "cumQuote": "180", "avgPrice": "150", "updateTime": 1,
        }
        client.futures_get_open_algo_orders.return_value = []
        client.futures_get_open_orders.return_value = []
        return client

    def run_monitor(self, trade, client):
        with patch("services.supabase_client.fetch_all_futures",
                   return_value=[trade]), \
             patch("services.supabase_client.update_futures_by_order_id") as update, \
             patch("core.futures_trade_executor.accrue_funding",
                   return_value=False), \
             patch("core.futures_trade_executor.compute_mae_mfe_from_candles",
                   return_value={}), \
             patch("core.executors.futures_position_monitor._send_telegram"):
            FuturesPositionMonitor(client).check_positions()
        persisted = {}
        for call in update.call_args_list:
            persisted.update(call.args[1])
        self.last_update_payloads = [call.args[1]
                                     for call in update.call_args_list]
        return persisted

    def test_exchange_tp_and_sl_reasons(self):
        for terminal_id, expected_status, expected_reason in (
                (7001, "TP_HIT", "EXCHANGE_TP"),
                (7002, "SL_HIT", "EXCHANGE_SL")):
            with self.subTest(expected_reason=expected_reason):
                client = self.client()

                def get_algo(**kwargs):
                    if kwargs.get("algoId") == terminal_id:
                        return {
                            "algoStatus": "EXECUTED", "executedQty": "1.2",
                            "cumQuote": "198" if terminal_id == 7001 else "168",
                            "updateTime": 1_700_000_001_000,
                        }
                    return {"algoStatus": "NEW"}

                client.futures_get_algo_order.side_effect = get_algo
                persisted = self.run_monitor(self.trade(), client)
                self.assertEqual(persisted["exit_status"], expected_status)
                self.assertEqual(persisted["exit_reason"], expected_reason)

    def test_price_guard_reason(self):
        client = self.client(price="135")
        client.futures_get_algo_order.return_value = {"algoStatus": "NEW"}
        client.futures_create_order.return_value = {
            "orderId": 9001, "executedQty": "1.2", "cumQuote": "162",
            "avgPrice": "135", "updateTime": 1_700_000_001_000,
        }
        persisted = self.run_monitor(self.trade(), client)
        self.assertEqual(persisted["exit_status"], "SL_HIT")
        self.assertEqual(persisted["exit_reason"], "EMERGENCY_PRICE_GUARD")
        immutable_columns = {
            "research_snapshot_version", "decision_time", "pre_submit_time",
            "initial_entry_price", "initial_sl", "initial_risk_pct",
            "final_rr", "delta_rr", "atr_at_entry",
            "analysis_candle_open_time", "analysis_candle_close_time",
            "analysis_candle_closed",
        }
        for payload in self.last_update_payloads:
            self.assertTrue(immutable_columns.isdisjoint(payload))

    def test_unprotected_emergency_reason(self):
        client = self.client(price="135")
        result = {
            "tp_order_id": None, "tp_algo_id": None,
            "sl_order_id": 9001, "sl_algo_id": None,
            "success": True, "emergency_exit": True,
            "exit_reason": "EMERGENCY_UNPROTECTED",
            "protection_state": "POSITION_CLOSED", "errors": [],
            "terminal_order_seen": True,
        }
        trade = self.trade(exit_orders_placed=False, tp_algo_id=None,
                           sl_algo_id=None)
        with patch.object(FuturesOrderExecutor, "place_exit_orders",
                          return_value=result):
            persisted = self.run_monitor(trade, client)
        self.assertEqual(persisted["exit_status"], "SL_HIT")
        self.assertEqual(persisted["exit_reason"], "EMERGENCY_UNPROTECTED")

    def test_reconciled_manual_close_reason(self):
        client = self.client()
        result = {
            "tp_order_id": None, "tp_algo_id": None,
            "sl_order_id": None, "sl_algo_id": None,
            "success": False, "protection_state": "POSITION_CLOSED",
            "errors": [], "terminal_order_seen": False,
        }
        trade = self.trade(exit_orders_placed=False, tp_algo_id=None,
                           sl_algo_id=None)
        with patch.object(FuturesOrderExecutor, "place_exit_orders",
                          return_value=result):
            persisted = self.run_monitor(trade, client)
        self.assertEqual(persisted["exit_status"], "MANUALLY_CLOSED")
        self.assertEqual(
            persisted["exit_reason"], "RECONCILED_EXCHANGE_CLOSE")


class FuturesResearchSnapshotTests(unittest.TestCase):
    @patch("core.scanners.futures_candidate_scanner.compute_volatility_regime",
           return_value="medium")
    @patch("core.futures_trade_executor.get_funding_rate", return_value=0.0)
    @patch("core.futures_trade_executor.get_futures_symbol_constraints")
    def test_reanchor_records_final_r_without_changing_planned_r(
            self, constraints, _funding, _regime):
        constraints.return_value = {
            "tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001,
            "min_notional": 5.0,
        }
        candidate = {
            "symbol": "SOLUSDT", "direction": "long",
            "position_side": "LONG", "current_price": 150.0,
            "entry_price": 150.0, "sl": 140.0, "tp1": 165.0,
            "tp2": None, "rr": 1.5, "risk_pct": 6.6667,
            "atr": 2.0, "atr_pct": 1.3333, "tier_used": "T1",
            "support_zones": [{"center": 148.0, "low": 147.0,
                               "high": 149.0, "touches": 3}],
            "resistance_zones": [],
            "research_snapshot": {
                "schema_version": "futures_pre_submit_v1",
                "initial_entry": 150.0, "initial_sl": 140.0,
                "initial_planned_r": 1.5,
            },
        }
        picked = FuturesCandidateScanner(MagicMock()).pick_best_candidate(
            [candidate])
        research = picked["research_snapshot"]

        self.assertEqual(picked["rr"], 1.5)
        self.assertEqual(research["initial_entry"], 150.0)
        self.assertEqual(research["initial_sl"], 140.0)
        expected = abs(picked["tp1"] - picked["entry_price"]) / abs(
            picked["entry_price"] - picked["sl"])
        self.assertAlmostEqual(research["final_pre_submit_r"], expected)
        self.assertAlmostEqual(research["delta_r"], expected - 1.5)

    @patch("services.supabase_client.upsert_futures")
    def test_snapshot_is_copied_and_survives_lifecycle_metadata(self, upsert):
        snapshot = {
            "schema_version": "futures_pre_submit_v1",
            "analysis_time": "2026-08-20T04:00:01+00:00",
            "pre_submit_time": "2026-08-20T04:01:02+00:00",
            "last_candle_open_time": 1_766_203_200_000,
            "last_candle_close_time": 1_766_217_599_999,
            "last_candle_was_closed": True,
            "initial_entry": 150.0,
            "initial_sl": 140.0,
            "initial_risk_pct": 6.6667,
            "final_pre_submit_entry": 148.0,
            "final_pre_submit_sl": 139.0,
            "final_pre_submit_r": 17 / 9,
            "delta_r": (17 / 9) - 1.5,
            "atr": 3.0,
        }
        cand = {
            "symbol": "SOLUSDT", "position_side": "LONG",
            "direction": "long", "entry_price": 148.0, "sl": 139.0,
            "tp1": 165.0, "tp2": None, "rr": 1.5, "risk_pct": 6.08,
            "atr_pct": 2.0, "tier_used": "T1", "entry_zone": {
                "center": 148.0, "touches": 3},
            "volatility_regime": "medium", "funding_rate_at_entry": 0.0,
            "sizing": {"qty": 1.0, "notional_usd": 148.0,
                       "margin_used": 49.33, "max_loss_usd": 9.0},
            "liquidation": {"liquidation_price": 99.3,
                            "distance_to_liquidation_pct": 32.9},
            "research_snapshot": snapshot,
        }
        order = {"orderId": 555, "status": "NEW"}
        FuturesTradeRepository().log_futures_trade(order, cand)
        record = upsert.call_args.args[0]
        stored = deepcopy(record["raw_entry_order"]["research_snapshot"])

        snapshot["final_pre_submit_r"] = 999
        record["raw_entry_order"]["exit_protection"] = {
            "state": "FULLY_PROTECTED"}

        self.assertEqual(
            record["raw_entry_order"]["research_snapshot"], stored)
        self.assertNotEqual(
            record["raw_entry_order"]["research_snapshot"]["final_pre_submit_r"],
            snapshot["final_pre_submit_r"])
        self.assertIsNone(record["exit_reason"])
        self.assertEqual(
            record["research_snapshot_version"], "futures_pre_submit_v1")
        self.assertEqual(record["decision_time"], snapshot["analysis_time"])
        self.assertEqual(record["pre_submit_time"], snapshot["pre_submit_time"])
        self.assertEqual(record["initial_entry_price"], 150.0)
        self.assertEqual(record["initial_sl"], 140.0)
        self.assertEqual(record["initial_risk_pct"], 6.6667)
        self.assertAlmostEqual(record["final_rr"], 17 / 9)
        self.assertAlmostEqual(record["delta_rr"], (17 / 9) - 1.5)
        self.assertEqual(record["atr_at_entry"], 3.0)
        self.assertEqual(
            record["analysis_candle_open_time"],
            "2025-12-20T04:00:00+00:00")
        self.assertEqual(
            record["analysis_candle_close_time"],
            "2025-12-20T07:59:59.999000+00:00")
        self.assertTrue(record["analysis_candle_closed"])
        # Existing canonical structured fields remain the final contract.
        self.assertEqual(record["entry_price"], 148.0)
        self.assertEqual(record["sl"], 139.0)
        self.assertEqual(record["tp1"], 165.0)
        self.assertEqual(record["planned_rr"], 1.5)


if __name__ == "__main__":
    unittest.main()
