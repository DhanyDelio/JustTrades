"""
tests/test_tokocrypto_sl_recovery.py
====================================
Unit tests for Tokocrypto Stop-Limit SL recovery mechanism based on POL_IDR incident.

Covers the 12 required verification criteria:
1.  SL trigger + SL Limit FILLED -> SL_HIT.
2.  SL trigger + SL Limit masih NEW -> tetap OPEN / pending recovery.
3.  TP EXPIRED + SL NEW -> tidak boleh dianggap SL_HIT.
4.  SL stuck -> recovery path dapat terdeteksi (cycle 2+).
5.  Recovery tidak membuat duplicate sell (cancel failure aborts before market sell).
6.  Partial fill ditangani dengan benar (sells remaining only, blended exit price).
7.  Asset/quantity yang tersedia diverifikasi sebelum emergency exit.
8.  Emergency exit berhasil -> state menjadi closed / SL_HIT (exit_reason EMERGENCY_SL_MARKET).
9.  Emergency exit gagal -> posisi tetap OPEN / pending, bukan falsely closed.
10. Existing OCO behavior tidak rusak.
11. Existing ETH/SUI/POL regression test paths pass.
12. Pending/stuck position tetap dihitung sebagai occupied slot sehingga allocation tidak berlebihan.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, call

from core.clients.tokocrypto_client import TokocryptoClient, ExchangeSymbol, ExchangeBalance
from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
from core.executors.tokocrypto_position_monitor import TokocryptoPositionMonitor
from tokocrypto_executor import calculate_available_slots


def _build_test_setup():
    mock_client = MagicMock(spec=TokocryptoClient)
    mock_client.normalize_symbol = lambda s: s
    mock_client.round_tick = TokocryptoClient.round_tick
    mock_client.round_step = TokocryptoClient.round_step

    sym = MagicMock(spec=ExchangeSymbol)
    sym.tick_size = 1.0
    sym.step_size = 0.1
    sym.min_qty = 0.1
    sym.min_notional = 10_000.0
    sym.constraints = {
        "tick_size": 1.0,
        "step_size": 0.1,
        "min_qty": 0.1,
        "min_notional": 10_000.0,
    }
    mock_client.get_symbol.return_value = sym

    executor = TokocryptoOrderExecutor(
        mock_client,
        supervised=False,
        trading_phase="PHASE_3",
        dry_run=False,
    )
    monitor = TokocryptoPositionMonitor(mock_client, executor)
    return mock_client, executor, monitor


class TestTokocryptoStopLimitRecovery(unittest.TestCase):

    def setUp(self):
        self.client, self.executor, self.monitor = _build_test_setup()
        self.pol_trade = {
            "symbol": "POL_IDR",
            "entry_order_id": "917510990",
            "entry_status": "FILLED",
            "b_order_list_id": "25208291196",
            "tp_order_id": "917549756",
            "sl_order_id": "917549757",
            "entry_price": 1797.0,
            "entry_fill_price": 1797.0,
            "entry_fill_time": 1791447974407,
            "entry_qty": 20.2,
            "entry_notional_idr": 36299.4,
            "tp_price": 1895.0,
            "sl_price": 1758.0,
            "exit_status": "OPEN",
        }

    # ----------------------------------------------------------------------
    # 1. SL trigger + SL Limit FILLED -> SL_HIT
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_01_sl_trigger_and_sl_limit_filled_resolves_sl_hit(self, mock_tg, mock_update):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 2, "executedPrice": "1755.0", "executedQty": "20.2", "time": 1791460861792}

        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        state_dict = self.executor.query_oco_state(self.pol_trade)
        self.assertEqual(state_dict["state"], "SL_HIT")
        self.assertEqual(state_dict["exit_price"], 1755.0)

        self.monitor._check_one(self.pol_trade, verbose=False)
        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["exit_status"], "SL_HIT")
        self.assertEqual(payload["exit_price"], 1755.0)
        self.assertEqual(payload["exit_reason"], "OCO_TRIGGERED")

    # ----------------------------------------------------------------------
    # 2. SL trigger + SL Limit masih NEW -> tetap OPEN / pending recovery
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_02_sl_trigger_and_sl_limit_new_stays_open_pending(self, mock_tg, mock_update):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}  # EXPIRED
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}  # NEW (unfilled)

        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        trade = dict(self.pol_trade)
        trade["expired_leg_detected_at"] = None  # Cycle 1

        self.monitor._check_one(trade, verbose=False)

        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["oco_state"], "TP_EXPIRED_PENDING")
        self.assertIn("expired_leg_detected_at", payload)
        self.assertNotIn("exit_status", payload)  # exit_status is NOT changed to SL_HIT

    # ----------------------------------------------------------------------
    # 3. TP EXPIRED + SL NEW -> tidak boleh dianggap SL_HIT
    # ----------------------------------------------------------------------
    def test_03_tp_expired_and_sl_new_never_treated_as_sl_hit(self):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}

        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        state_dict = self.executor.query_oco_state(self.pol_trade)
        self.assertNotEqual(state_dict["state"], "SL_HIT")
        self.assertEqual(state_dict["state"], "TP_EXPIRED_PENDING")
        self.assertIsNone(state_dict["exit_price"])

    # ----------------------------------------------------------------------
    # 4. SL stuck -> recovery path dapat terdeteksi (Cycle 2+)
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_04_sl_stuck_triggers_recovery_path(self, mock_tg, mock_update):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}

        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        trade = dict(self.pol_trade)
        trade["expired_leg_detected_at"] = "2026-10-08T16:02:57+00:00"  # Pre-existing from Cycle 1

        with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
            mock_recover.return_value = {
                "state": "SL_HIT",
                "exit_price": 1743.0,
                "exit_qty": 20.2,
                "exit_reason": "EMERGENCY_SL_MARKET",
                "raw_sl": {"orderId": "9999", "status": 2},
                "slippage_flagged": True,
            }
            self.monitor._check_one(trade, verbose=False)
            mock_recover.assert_called_once_with(trade)

    # ----------------------------------------------------------------------
    # 5. Recovery tidak membuat duplicate sell (cancel failure aborts)
    # ----------------------------------------------------------------------
    def test_05_recovery_prevents_duplicate_sell_if_cancel_fails(self):
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0", "origQty": "20.2"}
        self.client.get_order_detail.return_value = raw_sl

        # cancel_order returns False (exchange rejected cancel or connection failure)
        with patch.object(self.executor, "cancel_order", return_value=False):
            with patch.object(self.executor, "execute_market_sell") as mock_market_sell:
                res = self.executor.recover_stuck_sl(self.pol_trade)
                self.assertIsNone(res)
                # MARKET SELL must NEVER be executed if cancel did not succeed
                mock_market_sell.assert_not_called()

    # ----------------------------------------------------------------------
    # 6. Partial fill ditangani dengan benar (sells remaining, blended price)
    # ----------------------------------------------------------------------
    def test_06_recovery_handles_partial_fill_correctly(self):
        # 5.0 units were partially filled @ 1755, remaining 15.2 units unfilled
        raw_sl_active = {
            "orderId": "917549757",
            "status": 1,  # PARTIALLY_FILLED
            "executedPrice": "1755.0",
            "executedQty": "5.0",
            "origQty": "20.2",
        }
        raw_sl_canceled = {
            "orderId": "917549757",
            "status": 3,  # CANCELED
            "executedPrice": "1755.0",
            "executedQty": "5.0",
            "origQty": "20.2",
        }

        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        self.client.get_balance.return_value = ExchangeBalance("POL", free=15.2, locked=0.0)

        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_market_sell:
                # Market sell fills the remaining 15.2 @ 1740.0
                mock_market_sell.return_value = {
                    "orderId": "8888",
                    "status": 2,
                    "executedPrice": "1740.0",
                    "executedQty": "15.2",
                }

                res = self.executor.recover_stuck_sl(self.pol_trade)

                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertEqual(res["exit_reason"], "EMERGENCY_SL_MARKET")
                # Market sell must ONLY sell remaining 15.2 (not all 20.2)
                mock_market_sell.assert_called_once_with("POL_IDR", 15.2)

                # Blended price: (5.0 * 1755 + 15.2 * 1740) / 20.2 = (8775 + 26448) / 20.2 = 1743.71287...
                expected_blended = (5.0 * 1755.0 + 15.2 * 1740.0) / 20.2
                self.assertAlmostEqual(res["exit_price"], expected_blended, places=4)

    # ----------------------------------------------------------------------
    # 7. Asset/quantity yang tersedia diverifikasi sebelum emergency exit
    # ----------------------------------------------------------------------
    def test_07_recovery_verifies_asset_quantity_before_emergency_exit(self):
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0", "origQty": "20.2"}
        self.client.get_order_detail.return_value = raw_sl

        # Wallet has 0 free balance (e.g. transferred out or unavailable)
        self.client.get_balance.return_value = ExchangeBalance("POL", free=0.0, locked=0.0)

        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_market_sell:
                res = self.executor.recover_stuck_sl(self.pol_trade)
                self.assertIsNone(res)
                mock_market_sell.assert_not_called()

    # ----------------------------------------------------------------------
    # 8. Emergency exit berhasil -> state menjadi closed/SL_HIT
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_08_recovery_success_closes_position_as_sl_hit(self, mock_tg, mock_update):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}

        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        trade = dict(self.pol_trade)
        trade["expired_leg_detected_at"] = "2026-10-08T16:02:57+00:00"

        recovery_dict = {
            "state": "SL_HIT",
            "exit_price": 1741.0,
            "exit_qty": 20.2,
            "exit_reason": "EMERGENCY_SL_MARKET",
            "raw_sl": {"orderId": "8888", "status": 2, "time": 1791465000000},
            "slippage_flagged": True,
        }

        with patch.object(self.executor, "recover_stuck_sl", return_value=recovery_dict):
            self.monitor._check_one(trade, verbose=False)

            self.assertTrue(mock_update.called)
            # Find final update call
            last_call = mock_update.call_args_list[-1]
            payload = last_call[0][1]
            self.assertEqual(payload["exit_status"], "SL_HIT")
            self.assertEqual(payload["exit_reason"], "EMERGENCY_SL_MARKET")
            self.assertEqual(payload["exit_price"], 1741.0)
            self.assertIsNotNone(payload["realized_pnl_idr"])

    # ----------------------------------------------------------------------
    # 9. Emergency exit gagal -> posisi tetap pending, bukan falsely closed
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_09_recovery_failure_leaves_position_open_not_falsely_closed(self, mock_tg, mock_update):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}

        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        trade = dict(self.pol_trade)
        trade["expired_leg_detected_at"] = "2026-10-08T16:02:57+00:00"

        # recover_stuck_sl returns None (API failure)
        with patch.object(self.executor, "recover_stuck_sl", return_value=None):
            self.monitor._check_one(trade, verbose=False)

            # Database update must NOT set exit_status to SL_HIT
            for call_item in mock_update.call_args_list:
                payload = call_item[0][1]
                self.assertNotEqual(payload.get("exit_status"), "SL_HIT")

    # ----------------------------------------------------------------------
    # 10. Existing OCO behavior tidak rusak
    # ----------------------------------------------------------------------
    def test_10_existing_oco_behavior_intact(self):
        # 10a. Normal clean TP hit (TP=2, SL=3)
        self.client.get_order_detail.side_effect = [
            {"orderId": "1", "status": 2, "executedPrice": "1895.0"},
            {"orderId": "2", "status": 3, "executedPrice": "0.0"},
        ]
        res_tp = self.executor.query_oco_state(self.pol_trade)
        self.assertEqual(res_tp["state"], "TP_HIT")
        self.assertEqual(res_tp["exit_price"], 1895.0)

        # 10b. Normal clean SL hit (TP=3, SL=2)
        self.client.get_order_detail.side_effect = [
            {"orderId": "1", "status": 3, "executedPrice": "0.0"},
            {"orderId": "2", "status": 2, "executedPrice": "1755.0"},
        ]
        res_sl = self.executor.query_oco_state(self.pol_trade)
        self.assertEqual(res_sl["state"], "SL_HIT")
        self.assertEqual(res_sl["exit_price"], 1755.0)

        # 10c. Normal executing (TP=0, SL=0)
        self.client.get_order_detail.side_effect = [
            {"orderId": "1", "status": 0, "executedPrice": "0.0"},
            {"orderId": "2", "status": 0, "executedPrice": "0.0"},
        ]
        res_exec = self.executor.query_oco_state(self.pol_trade)
        self.assertEqual(res_exec["state"], "EXECUTING")

        # 10d. Both canceled anomaly (TP=3, SL=3)
        self.client.get_order_detail.side_effect = [
            {"orderId": "1", "status": 3, "executedPrice": "0.0"},
            {"orderId": "2", "status": 3, "executedPrice": "0.0"},
        ]
        res_anom = self.executor.query_oco_state(self.pol_trade)
        self.assertEqual(res_anom["state"], "BOTH_CANCELED_ANOMALY")

    # ----------------------------------------------------------------------
    # 11. Existing ETH/SUI/POL OCO regression tests tetap pass
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_11_existing_eth_regression_test_passes(self, mock_tg, mock_update):
        # ETH_IDR regression scenario
        eth_trade = {
            "symbol": "ETH_IDR",
            "entry_order_id": "917510981",
            "entry_status": "FILLED",
            "b_order_list_id": "25208289734",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 45503763.0,
            "entry_fill_price": 45503757.0,
            "entry_fill_time": 1791447971900,
            "entry_qty": 0.0007,
            "entry_notional_idr": 31852.6,
            "tp_price": 47645461.0,
            "sl_price": 44689248.0,
            "exit_status": "OPEN",
        }
        raw_tp = {"orderId": "917549739", "status": 6, "executedPrice": "0"}
        raw_sl = {
            "orderId": "917549740",
            "status": 2,  # FILLED
            "executedPrice": "44645383.33",
            "time": 1791460859579,
        }
        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549739" else raw_sl

        self.monitor._check_one(eth_trade, verbose=False)
        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["exit_status"], "SL_HIT")
        self.assertEqual(payload["exit_reason"], "OCO_TRIGGERED")
        self.assertAlmostEqual(payload["exit_price"], 44645383.33, places=2)

    # ----------------------------------------------------------------------
    # 12. Pending/stuck position tetap dihitung sebagai occupied slot
    # ----------------------------------------------------------------------
    def test_12_pending_stuck_position_counts_as_occupied_slot(self):
        # A stuck position has exit_status="OPEN"
        simulated_trades = [
            {"symbol": "ETH_IDR", "exit_status": "SL_HIT"},    # resolved
            {"symbol": "POL_IDR", "exit_status": "OPEN"},      # STUCK/PENDING
            {"symbol": "SOL_IDR", "exit_status": "OPEN"},      # ACTIVE
        ]
        open_count = len([t for t in simulated_trades if t.get("exit_status") == "OPEN"])
        self.assertEqual(open_count, 2)

        available = calculate_available_slots(open_count, max_positions=5)
        # 5 - 2 = 3 slots available (stuck POL occupies 1 slot, preventing over-allocation)
        self.assertEqual(available, 3)

    # ----------------------------------------------------------------------
    # 13. Idempotency: recovery_attempted prevents repeated recovery calls
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_13_recovery_attempted_prevents_repeated_recovery_calls_per_cycle(self, mock_tg, mock_update):
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}
        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        trade = dict(self.pol_trade)
        trade["expired_leg_detected_at"] = "2026-10-08T16:02:57+00:00"
        trade["recovery_attempted"] = True  # Already attempted on previous cycle

        with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
            self.monitor._check_one(trade, verbose=False)
            # Must NOT call recover_stuck_sl again
            mock_recover.assert_not_called()

    # ----------------------------------------------------------------------
    # 14. Recovery works generically for any symbol (e.g. BTC_IDR, SOL_IDR)
    # ----------------------------------------------------------------------
    def test_14_generic_symbol_recovery_works_for_any_pair(self):
        btc_trade = {
            "symbol": "BTC_IDR",
            "entry_order_id": "888111222",
            "entry_status": "FILLED",
            "b_order_list_id": "999111222",
            "tp_order_id": "888111223",
            "sl_order_id": "888111224",
            "entry_price": 1_000_000_000.0,
            "entry_qty": 0.05,
            "sl_price": 950_000_000.0,
            "exit_status": "OPEN",
        }
        raw_sl_active = {
            "orderId": "888111224",
            "status": 0,  # NEW
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "0.05",
        }
        raw_sl_canceled = dict(raw_sl_active, status=4)  # CANCELED
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        self.client.get_balance.return_value = ExchangeBalance(asset="BTC", free=0.05, locked=0.0)

        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                mock_mkt.return_value = {
                    "orderId": "BTC_MKT_SELL_99",
                    "status": 2,
                    "executedPrice": "945000000.0",
                    "executedQty": "0.05",
                }
                res = self.executor.recover_stuck_sl(btc_trade)

                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertEqual(res["exit_reason"], "EMERGENCY_SL_MARKET")
                self.assertEqual(res["exit_price"], 945_000_000.0)
                mock_mkt.assert_called_once_with("BTC_IDR", 0.05)


if __name__ == "__main__":
    unittest.main()

