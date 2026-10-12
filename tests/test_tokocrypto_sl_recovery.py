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
    mock_client.get_open_orders.return_value = []
    mock_client.get_order_history.return_value = []

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
        from core.clients.tokocrypto_order_executor import _ACTIVE_RECOVERY_SUBMISSIONS
        _ACTIVE_RECOVERY_SUBMISSIONS.clear()
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

    def tearDown(self):
        from core.clients.tokocrypto_order_executor import _ACTIVE_RECOVERY_SUBMISSIONS
        _ACTIVE_RECOVERY_SUBMISSIONS.clear()

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
    @patch("core.executors.tokocrypto_position_monitor.claim_tokocrypto_recovery", return_value=True)
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_04_sl_stuck_triggers_recovery_path(self, mock_tg, mock_update, mock_claim):
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
    @patch("core.executors.tokocrypto_position_monitor.claim_tokocrypto_recovery", return_value=True)
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_08_recovery_success_closes_position_as_sl_hit(self, mock_tg, mock_update, mock_claim):
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
    @patch("core.executors.tokocrypto_position_monitor.claim_tokocrypto_recovery", return_value=True)
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_09_recovery_failure_leaves_position_open_not_falsely_closed(self, mock_tg, mock_update, mock_claim):
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

    # ----------------------------------------------------------------------
    # 15. cancel_order: response with status=None verifies via get_order_detail
    # ----------------------------------------------------------------------
    def test_cancel_order_with_none_status_verifies_via_detail_success(self):
        """When POST /orders/cancel returns data.status=None (like GRAM_IDR), verify via get_order_detail."""
        self.client._signed_post.return_value = {"code": 0, "msg": "success", "data": {"status": None, "orderId": "917924367"}}
        self.client.get_order_detail.return_value = {
            "orderId": "917924367",
            "status": 3,  # CANCELED
            "executedQty": "0",
            "origQty": "1.28",
        }
        res = self.executor.cancel_order("GRAM_IDR", "917924367")
        self.assertTrue(res)
        self.client._signed_post.assert_called_once()
        self.client.get_order_detail.assert_called_once_with("GRAM_IDR", "917924367")

    # ----------------------------------------------------------------------
    # 16. cancel_order: verified already FILLED returns False (no fake cancel)
    # ----------------------------------------------------------------------
    def test_cancel_order_verified_already_filled_returns_false(self):
        """When cancel POST returns non-canceled and detail reveals order is already FILLED, return False."""
        self.client._signed_post.return_value = {"code": 0, "msg": "success", "data": {"status": None}}
        self.client.get_order_detail.return_value = {
            "orderId": "917924367",
            "status": 2,  # FILLED
            "executedQty": "1.28",
            "origQty": "1.28",
        }
        res = self.executor.cancel_order("GRAM_IDR", "917924367")
        self.assertFalse(res)

    # ----------------------------------------------------------------------
    # 17. cancel_order: uncertain timeout fails closed (returns False)
    # ----------------------------------------------------------------------
    def test_cancel_order_uncertain_timeout_fails_closed(self):
        """When both cancel POST and get_order_detail fail/timeout, return False (fail-closed)."""
        from core.clients.tokocrypto_client import TokocryptoNetworkError
        self.client._signed_post.side_effect = TokocryptoNetworkError("Post timeout")
        self.client.get_order_detail.side_effect = TokocryptoNetworkError("Detail timeout")
        res = self.executor.cancel_order("GRAM_IDR", "917924367")
        self.assertFalse(res)

    # ----------------------------------------------------------------------
    # 18. recover_stuck_sl: handles locked balance lag via polling
    # ----------------------------------------------------------------------
    @patch("time.sleep", return_value=None)
    def test_recover_stuck_sl_handles_locked_balance_lag(self, mock_sleep):
        """When balance is temporarily locked post-cancel, polling recovers free balance."""
        raw_sl_active = {
            "orderId": "917924367",
            "status": 0,
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "1.28",
        }
        raw_sl_canceled = dict(raw_sl_active, status=3)
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        # Poll 1: 0 free (lag), Poll 2: 1.28 free
        self.client.get_balance.side_effect = [
            ExchangeBalance(asset="GRAM", free=0.0, locked=1.28),
            ExchangeBalance(asset="GRAM", free=1.28, locked=0.0),
        ]
        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                mock_mkt.return_value = {
                    "orderId": "MKT_RECOVER_1",
                    "status": 2,
                    "executedPrice": "25043.0",
                    "executedQty": "1.28",
                }
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertEqual(res["exit_qty"], 1.28)
                mock_mkt.assert_called_once_with("GRAM_IDR", 1.28)
                # Sleep was called during polling
                mock_sleep.assert_called_once()

    # ----------------------------------------------------------------------
    # 19. recover_stuck_sl: partial fill sells only remaining quantity
    # ----------------------------------------------------------------------
    def test_recover_stuck_sl_partial_fill_sells_only_remaining(self):
        """Partial fill: remaining quantity is accurately calculated, sold, and price blended."""
        raw_sl_active = {
            "orderId": "917924367",
            "status": 1,  # PARTIALLY_FILLED
            "executedPrice": "25400.0",
            "executedQty": "0.5",
            "origQty": "1.28",
        }
        raw_sl_canceled = dict(raw_sl_active, status=3)
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        self.client.get_balance.return_value = ExchangeBalance(asset="GRAM", free=0.78, locked=0.0)

        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                mock_mkt.return_value = {
                    "orderId": "MKT_RECOVER_2",
                    "status": 2,
                    "executedPrice": "25000.0",
                    "executedQty": "0.78",
                }
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                # Total exit qty = 0.5 + 0.78 = 1.28
                self.assertAlmostEqual(res["exit_qty"], 1.28, places=4)
                # Market sell must be called for ONLY remaining 0.78
                mock_mkt.assert_called_once_with("GRAM_IDR", 0.78)

    # ----------------------------------------------------------------------
    # 20. recover_stuck_sl: abort if balance remains zero after polling
    # ----------------------------------------------------------------------
    @patch("time.sleep", return_value=None)
    def test_recover_stuck_sl_abort_if_balance_remains_zero(self, mock_sleep):
        """When balance remains zero across all 3 polls, abort without market sell."""
        raw_sl_active = {
            "orderId": "917924367",
            "status": 0,
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "1.28",
        }
        raw_sl_canceled = dict(raw_sl_active, status=3)
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        self.client.get_balance.return_value = ExchangeBalance(asset="GRAM", free=0.0, locked=0.0)

        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNone(res)
                mock_mkt.assert_not_called()
                self.assertEqual(mock_sleep.call_count, 2)

    # ----------------------------------------------------------------------
    # 21. recover_stuck_sl: does not double sell after cancel race
    # ----------------------------------------------------------------------
    def test_recover_stuck_sl_does_not_double_sell_after_cancel_race(self):
        """If order gets FILLED right during/after cancel attempt, reconcile as SL_HIT without market sell."""
        raw_sl_active = {
            "orderId": "917924367",
            "status": 0,  # Initially NEW
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "1.28",
        }
        # Post check or race check reveals it FILLED!
        raw_sl_filled = {
            "orderId": "917924367",
            "status": 2,  # FILLED
            "executedPrice": "25400.0",
            "executedQty": "1.28",
            "origQty": "1.28",
        }
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_filled]

        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        # cancel_order returned False because bursa rejected cancel (order was already filling)
        with patch.object(self.executor, "cancel_order", return_value=False):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertEqual(res["exit_price"], 25400.0)
                self.assertEqual(res["exit_qty"], 1.28)
                self.assertEqual(res["exit_reason"], "OCO_TRIGGERED")
                # Zero market sells placed!
                mock_mkt.assert_not_called()

    # ----------------------------------------------------------------------
    # 22. cancel_order: EXPIRED (6) with zero fill returns True
    # ----------------------------------------------------------------------
    def test_22_cancel_order_expired_with_zero_fill_returns_true(self):
        """When detail confirms status is EXPIRED (6) and executedQty is 0, cancel_order returns True."""
        from core.clients.tokocrypto_client import TokocryptoError
        self.client._signed_post.side_effect = TokocryptoError("Order already expired")
        self.client.get_order_detail.return_value = {
            "orderId": "917924367",
            "status": 6,
            "executedQty": "0",
            "origQty": "1.28",
        }
        res = self.executor.cancel_order("GRAM_IDR", "917924367")
        self.assertTrue(res)

    # ----------------------------------------------------------------------
    # 23. cancel_order: REJECTED (5) with zero fill returns True
    # ----------------------------------------------------------------------
    def test_23_cancel_order_rejected_with_zero_fill_returns_true(self):
        """When detail confirms status is REJECTED (5) and executedQty is 0, cancel_order returns True."""
        from core.clients.tokocrypto_client import TokocryptoError
        self.client._signed_post.side_effect = TokocryptoError("Order already rejected")
        self.client.get_order_detail.return_value = {
            "orderId": "917924367",
            "status": 5,
            "executedQty": "0",
            "origQty": "1.28",
        }
        res = self.executor.cancel_order("GRAM_IDR", "917924367")
        self.assertTrue(res)

    # ----------------------------------------------------------------------
    # 24. cancel_order: EXPIRED (6) with partial fill returns True
    # ----------------------------------------------------------------------
    def test_24_cancel_order_expired_with_partial_fill_returns_true(self):
        """When detail confirms status is EXPIRED (6) with executedQty > 0, cancel_order confirms terminal state."""
        from core.clients.tokocrypto_client import TokocryptoError
        self.client._signed_post.side_effect = TokocryptoError("Order already expired")
        self.client.get_order_detail.return_value = {
            "orderId": "917924367",
            "status": 6,
            "executedQty": "0.5",
            "origQty": "1.28",
        }
        res = self.executor.cancel_order("GRAM_IDR", "917924367")
        self.assertTrue(res)

    # ----------------------------------------------------------------------
    # 25. recover_stuck_sl: handles EXPIRED leg with partial fill
    # ----------------------------------------------------------------------
    def test_25_recover_stuck_sl_with_expired_sl_and_partial_fill(self):
        """When SL is EXPIRED with partial fill, remaining quantity is sold and blended price returned."""
        raw_sl_expired = {
            "orderId": "917924367",
            "status": 6,
            "executedPrice": "25400.0",
            "executedQty": "0.5",
            "origQty": "1.28",
        }
        self.client.get_order_detail.return_value = raw_sl_expired
        self.client.get_balance.return_value = ExchangeBalance(asset="GRAM", free=0.78, locked=0.0)

        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                mock_mkt.return_value = {
                    "orderId": "MKT_RECOVER_EXPIRED",
                    "status": 2,
                    "executedPrice": "25000.0",
                    "executedQty": "0.78",
                }
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertAlmostEqual(res["exit_qty"], 1.28, places=4)
                mock_mkt.assert_called_once_with("GRAM_IDR", 0.78)

    # ----------------------------------------------------------------------
    # 26. recover_stuck_sl: aborts if active open orders remain on exchange
    # ----------------------------------------------------------------------
    def test_26_recover_stuck_sl_aborts_if_open_orders_remain_on_exchange(self):
        """If active open orders remain on exchange for symbol, abort recovery to prevent duplicate sell."""
        raw_sl_active = {
            "orderId": "917924367",
            "status": 0,
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "1.28",
        }
        self.client.get_order_detail.return_value = raw_sl_active
        # Open orders check finds a working order!
        self.client.get_open_orders.return_value = [
            {"orderId": "999888111", "symbol": "GRAM_IDR", "side": 1, "status": 0}
        ]

        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNone(res)
                mock_mkt.assert_not_called()

    # ----------------------------------------------------------------------
    # 27. execute_market_sell: recovers filled order from history on timeout
    # ----------------------------------------------------------------------
    def test_27_execute_market_sell_recovers_filled_order_from_history_on_timeout(self):
        """When POST times out, check order history and recover filled market sell order."""
        from core.clients.tokocrypto_client import TokocryptoNetworkError
        import time as _t
        now_ms = int(_t.time() * 1000)
        self.client.get_balance.return_value = ExchangeBalance(asset="GRAM", free=1.28, locked=0.0)
        self.client._signed_post.side_effect = TokocryptoNetworkError("Read timed out")
        # History contains the filled market sell order that was accepted despite timeout!
        self.client.get_order_history.return_value = [
            {
                "orderId": "MKT_TIMEOUT_RECOVERED",
                "symbol": "GRAM_IDR",
                "side": 1,
                "type": 2,
                "status": 2,
                "executedPrice": "25100.0",
                "executedQty": "1.28",
                "createTime": now_ms,
            }
        ]
        res = self.executor.execute_market_sell("GRAM_IDR", 1.28)
        self.assertIsNotNone(res)
        self.assertEqual(res["orderId"], "MKT_TIMEOUT_RECOVERED")
        self.assertEqual(res["executedPrice"], "25100.0")

    # ----------------------------------------------------------------------
    # 28. execute_market_sell: ambiguous balance drop fails closed
    # ----------------------------------------------------------------------
    def test_28_execute_market_sell_ambiguous_balance_drop_fails_closed(self):
        """When POST times out, order not found in history, but balance dropped: fail-closed without re-selling."""
        from core.clients.tokocrypto_client import TokocryptoNetworkError
        # Initial balance 1.28, post balance 0.0 (dropped!)
        self.client.get_balance.side_effect = [
            ExchangeBalance(asset="GRAM", free=1.28, locked=0.0),
            ExchangeBalance(asset="GRAM", free=0.0, locked=0.0),
        ]
        self.client._signed_post.side_effect = TokocryptoNetworkError("POST dropped connection")
        self.client.get_order_history.return_value = []

        res = self.executor.execute_market_sell("GRAM_IDR", 1.28)
        self.assertIsNone(res)

    # ----------------------------------------------------------------------
    # 29. recover_stuck_sl: reconciles filled market sell from history on zero balance
    # ----------------------------------------------------------------------
    def test_29_recover_stuck_sl_reconciles_previous_filled_sell_from_history_on_zero_balance(self):
        """When recover_stuck_sl encounters 0 balance, it reconciles existing filled sell from order history."""
        import time as _t
        now_ms = int(_t.time() * 1000)
        raw_sl_active = {
            "orderId": "917924367",
            "status": 0,
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "1.28",
        }
        raw_sl_canceled = dict(raw_sl_active, status=3)
        mkt_detail = {
            "orderId": "MKT_PREV_ATTEMPT",
            "symbol": "GRAM_IDR",
            "side": 1,
            "type": 2,
            "status": 2,
            "executedPrice": "25200.0",
            "executedQty": "1.28",
            "createTime": now_ms,
        }
        self.client.get_order_detail.side_effect = lambda s, o: (
            mkt_detail if str(o) == "MKT_PREV_ATTEMPT"
            else raw_sl_canceled if str(o) == "917924367" and self.executor.cancel_order.called
            else raw_sl_active
        )
        # Balance is 0.0
        self.client.get_balance.return_value = ExchangeBalance(asset="GRAM", free=0.0, locked=0.0)
        # History has the market sell that was placed in earlier attempt
        self.client.get_order_history.return_value = [
            {
                "orderId": "MKT_PREV_ATTEMPT",
                "side": 1,
                "status": 2,
                "executedPrice": "25200.0",
                "executedQty": "1.28",
                "createTime": now_ms,
            }
        ]
        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "sl_price": 25478.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                res = self.executor.recover_stuck_sl(gram_trade)
                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertEqual(res["exit_price"], 25200.0)
                self.assertEqual(res["exit_qty"], 1.28)
                mock_mkt.assert_not_called()

    # ----------------------------------------------------------------------
    # 30. recover_stuck_sl: aborts idempotently if trade already resolved
    # ----------------------------------------------------------------------
    def test_30_recover_stuck_sl_aborts_idempotently_if_trade_already_resolved(self):
        """If trade in DB already has exit_status='SL_HIT', recover_stuck_sl aborts immediately without actions."""
        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 1.28,
            "exit_status": "SL_HIT",
        }
        res = self.executor.recover_stuck_sl(gram_trade)
        self.assertIsNone(res)
        self.client.get_order_detail.assert_not_called()

    # ----------------------------------------------------------------------
    # 31. recover_stuck_sl: fails closed if substantial position remains unsold
    # ----------------------------------------------------------------------
    def test_31_recover_stuck_sl_fails_closed_if_substantial_position_remains_unsold(self):
        """If market sell only sells a fraction and substantial amount remains, abort and do NOT declare SL_HIT."""
        raw_sl_active = {
            "orderId": "917924367",
            "status": 0,
            "executedPrice": "0",
            "executedQty": "0",
            "origQty": "10.0",
        }
        raw_sl_canceled = dict(raw_sl_active, status=3)
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        # Only 2.0 free available! (8.0 missing/locked)
        self.client.get_balance.return_value = ExchangeBalance(asset="GRAM", free=2.0, locked=0.0)

        gram_trade = {
            "symbol": "GRAM_IDR",
            "sl_order_id": "917924367",
            "entry_qty": 10.0,
            "sl_price": 25000.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell") as mock_mkt:
                mock_mkt.return_value = {
                    "orderId": "MKT_PARTIAL_SOLD",
                    "status": 2,
                    "executedPrice": "25000.0",
                    "executedQty": "2.0",
                }
                res = self.executor.recover_stuck_sl(gram_trade)
                # Substantial 8.0 remains unsold (> 0.1 min_qty and > 20000 IDR) -> fail closed!
                self.assertIsNone(res)

    # ----------------------------------------------------------------------
    # 32. recover_stuck_sl: residual dust allows SL_HIT closure
    # ----------------------------------------------------------------------
    def test_32_recover_stuck_sl_residual_dust_allows_sl_hit_closure(self):
        """When SL already filled 20.15 and remaining 0.05 is below min_qty, resolve with SL filled amount."""
        raw_sl_active = {
            "orderId": "917549757",
            "status": 1,
            "executedPrice": "1750.0",
            "executedQty": "20.15",
            "origQty": "20.2",
        }
        raw_sl_canceled = dict(raw_sl_active, status=3)
        self.client.get_order_detail.side_effect = [raw_sl_active, raw_sl_canceled]
        # Free balance is only 0.05 (dust)
        self.client.get_balance.return_value = ExchangeBalance(asset="POL", free=0.05, locked=0.0)

        pol_trade = {
            "symbol": "POL_IDR",
            "sl_order_id": "917549757",
            "entry_qty": 20.2,
            "sl_price": 1758.0,
        }
        with patch.object(self.executor, "cancel_order", return_value=True):
            with patch.object(self.executor, "execute_market_sell", return_value=None):
                res = self.executor.recover_stuck_sl(pol_trade)
                self.assertIsNotNone(res)
                self.assertEqual(res["state"], "SL_HIT")
                self.assertEqual(res["exit_qty"], 20.15)
                self.assertEqual(res["exit_price"], 1750.0)

    # ----------------------------------------------------------------------
    # 33. _resolve_exit: idempotent on repeated calls
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_33_resolve_exit_idempotent_skips_duplicate_resolution(self, mock_tg, mock_update):
        """If trade exit_status is already SL_HIT, _resolve_exit skips database update and telegram alert."""
        already_resolved_trade = dict(self.pol_trade, exit_status="SL_HIT")
        state_dict = {
            "state": "SL_HIT",
            "exit_price": 1755.0,
            "exit_qty": 20.2,
            "exit_reason": "OCO_TRIGGERED",
        }
        self.monitor._resolve_exit(already_resolved_trade, state_dict)
        mock_update.assert_not_called()
    # ----------------------------------------------------------------------
    # 34. In-process concurrency guard: concurrent thread skips recovery
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.claim_recovery_submission", return_value=False)
    @patch("core.executors.tokocrypto_position_monitor.claim_tokocrypto_recovery")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_34_recovery_in_process_concurrency_guard_blocks_second_thread(
        self, mock_tg, mock_db_claim, mock_mem_claim
    ):
        """If in-process claim_recovery_submission returns False, _check_one skips recovery immediately."""
        trade = dict(self.pol_trade, expired_leg_detected_at="2026-10-08T16:02:57+00:00")
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}
        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
            self.monitor._check_one(trade, verbose=False)
            mock_recover.assert_not_called()
            mock_db_claim.assert_not_called()

    # ----------------------------------------------------------------------
    # 35. Cross-process atomic claim: DB conflict blocks recovery
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.claim_tokocrypto_recovery", return_value=False)
    @patch("core.executors.tokocrypto_position_monitor.release_recovery_submission")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_35_recovery_cross_process_atomic_claim_blocked(
        self, mock_tg, mock_release_mem, mock_db_claim
    ):
        """If database claim_tokocrypto_recovery returns False (claimed by another process), skip recovery."""
        trade = dict(self.pol_trade, expired_leg_detected_at="2026-10-08T16:02:57+00:00")
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}
        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
            self.monitor._check_one(trade, verbose=False)
            mock_recover.assert_not_called()
            mock_release_mem.assert_called_once_with(str(trade["entry_order_id"]))

    # ----------------------------------------------------------------------
    # 36. Crash recovery: SL_RECOVERY_IN_PROGRESS transitions to manual review
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.transition_tokocrypto_state", return_value=True)
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_36_recovery_interrupted_in_progress_state_blocks_retry(
        self, mock_tg, mock_transition
    ):
        """If oco_state is SL_RECOVERY_IN_PROGRESS from a crashed process, flag RECONCILIATION_REQUIRED without selling."""
        trade = dict(
            self.pol_trade,
            oco_state="SL_RECOVERY_IN_PROGRESS",
            raw_entry_order={"recovery_attempted": True},
        )
        with patch.object(self.executor, "reconcile_emergency_sell_from_history", return_value=None):
            with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
                self.monitor._check_one(trade, verbose=False)
                mock_recover.assert_not_called()
                mock_transition.assert_called_once()
                args, kwargs = mock_transition.call_args
                self.assertEqual(args[0], str(trade["entry_order_id"]))
                from_st = kwargs.get("from_state") or (args[1] if len(args) > 1 else None)
                to_st = kwargs.get("to_state") or (args[2] if len(args) > 2 else None)
                self.assertEqual(from_st, "SL_RECOVERY_IN_PROGRESS")
                self.assertEqual(to_st, "RECONCILIATION_REQUIRED")
                self.assertTrue(kwargs["fields"]["raw_entry_order"]["requires_manual_review"])

    # ----------------------------------------------------------------------
    # 37. claim_tokocrypto_recovery executes conditional update
    # ----------------------------------------------------------------------
    def test_37_claim_tokocrypto_recovery_sql_contract(self):
        """claim_tokocrypto_recovery invokes conditional update filtering out already claimed states."""
        from services.supabase_client import claim_tokocrypto_recovery

        with patch("services.supabase_client._assert_safe_write_environment"):
            with patch("services.supabase_client.is_test_environment", return_value=False):
                with patch("services.supabase_client.get_client") as mock_get_client:
                    mock_client = MagicMock()
                    mock_get_client.return_value = mock_client
                    mock_execute = mock_client.table.return_value.update.return_value.eq.return_value.neq.return_value.neq.return_value.execute
                    mock_execute.return_value.data = [{"entry_order_id": "917549755"}]

                    res = claim_tokocrypto_recovery("917549755", {"oco_state": "SL_RECOVERY_IN_PROGRESS"})
                    self.assertTrue(res)

                    # Simulate another process claimed it (data = [])
                    mock_execute.return_value.data = []
                    res_empty = claim_tokocrypto_recovery("917549755", {"oco_state": "SL_RECOVERY_IN_PROGRESS"})
                    self.assertFalse(res_empty)

                    # Simulate data is None
                    mock_execute.return_value.data = None
                    res_none = claim_tokocrypto_recovery("917549755", {"oco_state": "SL_RECOVERY_IN_PROGRESS"})
                    self.assertFalse(res_none)

    # ----------------------------------------------------------------------
    # 38. transition_tokocrypto_state executes atomic conditional update
    # ----------------------------------------------------------------------
    def test_38_transition_tokocrypto_state_sql_contract(self):
        """transition_tokocrypto_state transitions state only if previous state matches."""
        from services.supabase_client import transition_tokocrypto_state

        with patch("services.supabase_client._assert_safe_write_environment"):
            with patch("services.supabase_client.is_test_environment", return_value=False):
                with patch("services.supabase_client.get_client") as mock_get_client:
                    mock_client = MagicMock()
                    mock_get_client.return_value = mock_client
                    mock_execute = mock_client.table.return_value.update.return_value.eq.return_value.eq.return_value.execute
                    mock_execute.return_value.data = [{"entry_order_id": "917549755"}]

                    res = transition_tokocrypto_state(
                        "917549755", "SL_RECOVERY_IN_PROGRESS", "RECONCILIATION_REQUIRED"
                    )
                    self.assertTrue(res)

                    # If already transitioned by another process (data = []) -> return False
                    mock_execute.return_value.data = []
                    res_contested = transition_tokocrypto_state(
                        "917549755", "SL_RECOVERY_IN_PROGRESS", "RECONCILIATION_REQUIRED"
                    )
                    self.assertFalse(res_contested)

    # ----------------------------------------------------------------------
    # 39. Dual process escalation race on SL_RECOVERY_IN_PROGRESS
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.transition_tokocrypto_state", return_value=False)
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_39_dual_process_escalation_race_contested_process_skips(
        self, mock_tg, mock_transition
    ):
        """When transition_tokocrypto_state returns False (lost race to other process), skip alert and mutations."""
        trade = dict(
            self.pol_trade,
            oco_state="SL_RECOVERY_IN_PROGRESS",
            raw_entry_order={"recovery_attempted": True},
        )
        with patch.object(self.executor, "reconcile_emergency_sell_from_history", return_value=None):
            with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
                self.monitor._check_one(trade, verbose=False)
                mock_recover.assert_not_called()
                mock_transition.assert_called_once()
                # Because transition returned False, no redundant telegram alert sent
                mock_tg.assert_not_called()

    # ----------------------------------------------------------------------
    # 40. Crash after exchange receives MARKET SELL -> Reconciled on restart
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_40_crash_after_exchange_filled_sell_reconciles_cleanly_on_restart(
        self, mock_tg
    ):
        """If market sell filled before process crash, startup reconciles filled sell as SL_HIT without selling again."""
        trade = dict(
            self.pol_trade,
            oco_state="SL_RECOVERY_IN_PROGRESS",
            entry_qty=20.2,
            raw_entry_order={
                "recovery_attempted": True,
                "recovery_claimed_at": "2026-10-12T10:00:00+00:00",
            },
        )
        reconciled_res = {
            "state": "SL_HIT",
            "exit_price": 1740.0,
            "exit_qty": 20.2,
            "exit_reason": "EMERGENCY_SL_MARKET",
            "raw_sl": {"orderId": "77778888", "status": 2},
            "slippage_flagged": False,
        }

        with patch.object(
            self.executor,
            "reconcile_emergency_sell_from_history",
            return_value=reconciled_res,
        ) as mock_hist_check:
            with patch.object(self.monitor, "_resolve_exit") as mock_resolve:
                with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
                    self.monitor._check_one(trade, verbose=False)
                    mock_hist_check.assert_called_once()
                    mock_recover.assert_not_called()
                    mock_resolve.assert_called_once_with(trade, reconciled_res)

    # ----------------------------------------------------------------------
    # 41. Old order on same symbol ignored during crash recovery
    # ----------------------------------------------------------------------
    def test_41_old_order_on_same_symbol_ignored_during_reconciliation(self):
        """Orders before recovery window_start_ms are strictly ignored."""
        old_order = {
            "orderId": "111222",
            "time": 1700000000000,  # Far past
            "side": "SELL",
            "status": 2,
            "executedQty": "20.2",
            "executedPrice": "1740.0",
        }
        self.client.get_order_history.return_value = [old_order]

        # window_start_ms is much newer
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
        )
        self.assertIsNone(res)

    # ----------------------------------------------------------------------
    # 42. Database failure during claim fails closed
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.claim_tokocrypto_recovery", side_effect=Exception("DB down"))
    @patch("core.executors.tokocrypto_position_monitor.release_recovery_submission")
    def test_42_database_failure_during_claim_fails_closed(
        self, mock_release_mem, mock_claim
    ):
        """If database throws during claim, recovery fails closed without placing orders."""
        trade = dict(self.pol_trade, expired_leg_detected_at="2026-10-08T16:02:57+00:00")
        raw_tp = {"orderId": "917549756", "status": 6, "executedPrice": "0", "executedQty": "0"}
        raw_sl = {"orderId": "917549757", "status": 0, "executedPrice": "0", "executedQty": "0"}
        self.client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549756" else raw_sl

        with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
            self.monitor._check_one(trade, verbose=False)
            mock_recover.assert_not_called()
            mock_release_mem.assert_called_once_with(str(trade["entry_order_id"]))


    # ----------------------------------------------------------------------
    # 43. Manual SELL on same symbol (LIMIT type or different qty) rejected
    # ----------------------------------------------------------------------
    def test_43_manual_sell_on_same_symbol_rejected(self):
        """Manual limit sells or sells with mismatched quantities are rejected."""
        # 43a: Manual LIMIT sell (type=1)
        manual_limit_order = {
            "orderId": "MANUAL_LIMIT_01",
            "time": 1750000050000,
            "side": "SELL",
            "type": 1,  # LIMIT
            "status": 2,
            "executedQty": "20.2",
            "executedPrice": "1740.0",
        }
        self.client.get_order_history.return_value = [manual_limit_order]
        self.client.get_order_detail.return_value = dict(manual_limit_order, symbol="POL_IDR")
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res, "Manual LIMIT sell must NOT be reconciled as emergency market sell")

        # 43b: Manual sell of different quantity (e.g. 100 vs 20.2)
        manual_large_order = {
            "orderId": "MANUAL_LARGE_02",
            "time": 1750000050000,
            "side": 1,
            "type": 2,
            "status": 2,
            "executedQty": "100.0",
            "executedPrice": "1740.0",
        }
        self.client.get_order_history.return_value = [manual_large_order]
        self.client.get_order_detail.return_value = dict(manual_large_order, symbol="POL_IDR")
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res, "Different quantity order must NOT be reconciled")

    # ----------------------------------------------------------------------
    # 44. Ambiguous history with multiple matching orders fails closed
    # ----------------------------------------------------------------------
    def test_44_ambiguous_multiple_matching_orders_fails_closed(self):
        """If 2 matching orders of similar quantity exist in window, fail closed."""
        order1 = {
            "orderId": "ORDER_COLLISION_1",
            "time": 1750000030000,
            "side": 1,
            "type": 2,
            "status": 2,
            "executedQty": "20.2",
            "executedPrice": "1740.0",
        }
        order2 = {
            "orderId": "ORDER_COLLISION_2",
            "time": 1750000060000,
            "side": 1,
            "type": 2,
            "status": 2,
            "executedQty": "20.2",
            "executedPrice": "1738.0",
        }
        self.client.get_order_history.return_value = [order1, order2]
        self.client.get_order_detail.side_effect = lambda s, oid: (
            dict(order1, symbol="POL_IDR") if oid == "ORDER_COLLISION_1"
            else dict(order2, symbol="POL_IDR")
        )
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res, "Ambiguous multiple orders must fail closed")

    # ----------------------------------------------------------------------
    # 45. Order history delayed in appearing fails closed to manual review
    # ----------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.transition_tokocrypto_state", return_value=True)
    def test_45_delayed_order_history_transitions_to_reconciliation_required(self, mock_trans):
        """When history is empty/delayed, Phase 0 transitions to RECONCILIATION_REQUIRED."""
        trade = dict(
            self.pol_trade,
            oco_state="SL_RECOVERY_IN_PROGRESS",
            raw_entry_order={"recovery_claimed_at": "2026-10-12T10:00:00+00:00"},
        )
        # History is empty (delayed indexing)
        self.client.get_order_history.return_value = []

        with patch.object(self.monitor, "_resolve_exit") as mock_resolve:
            with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
                self.monitor._check_one(trade, verbose=False)
                mock_recover.assert_not_called()
                mock_resolve.assert_not_called()
                mock_trans.assert_called_once()
                self.assertEqual(mock_trans.call_args[1]["from_state"], "SL_RECOVERY_IN_PROGRESS")
                self.assertEqual(mock_trans.call_args[1]["to_state"], "RECONCILIATION_REQUIRED")

    # ----------------------------------------------------------------------
    # 46. Timestamp validation: unit normalization and invalid ISO
    # ----------------------------------------------------------------------
    def test_46_timestamp_validation_and_unit_normalization(self):
        """Test timestamp in seconds normalizes correctly, missing timestamp rejected."""
        # 46a: Order timestamp in seconds (10 digits) correctly normalized to ms
        sec_order = {
            "orderId": "SEC_TIMESTAMP_ORDER",
            "time": 1750000050,  # in seconds
            "side": 1,
            "type": 2,
            "status": 2,
            "executedQty": "20.2",
            "executedPrice": "1740.0",
        }
        self.client.get_order_history.return_value = [sec_order]
        self.client.get_order_detail.return_value = dict(sec_order, symbol="POL_IDR")
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["state"], "SL_HIT")

        # 46b: Order timestamp is 0 / missing -> rejected
        zero_order = dict(sec_order, orderId="ZERO_TIME", time=0)
        self.client.get_order_history.return_value = [zero_order]
        self.client.get_order_detail.return_value = dict(zero_order, symbol="POL_IDR")
        res_zero = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res_zero, "Zero/missing timestamp must be rejected")

    @patch("core.executors.tokocrypto_position_monitor.transition_tokocrypto_state", return_value=True)
    def test_46c_invalid_iso_in_claimed_at_fails_closed(self, mock_trans):
        """Invalid or empty recovery_claimed_at fails closed without guessing 15m window."""
        trade = dict(
            self.pol_trade,
            oco_state="SL_RECOVERY_IN_PROGRESS",
            raw_entry_order={"recovery_claimed_at": "INVALID_TIMESTAMP"},
        )
        with patch.object(self.executor, "reconcile_emergency_sell_from_history") as mock_reconcile:
            self.monitor._check_one(trade, verbose=False)
            mock_reconcile.assert_not_called()
            mock_trans.assert_called_once_with(
                str(trade["entry_order_id"]),
                from_state="SL_RECOVERY_IN_PROGRESS",
                to_state="RECONCILIATION_REQUIRED",
                fields=mock_trans.call_args[1]["fields"],
            )

    # ----------------------------------------------------------------------
    # 47. Partial fill not reaching 98-99% is rejected
    # ----------------------------------------------------------------------
    def test_47_partial_fill_rejected_by_quantity_filter(self):
        """Partial fill (e.g. 10.0 out of 20.2) must not be reconciled as full exit."""
        partial_order = {
            "orderId": "PARTIAL_MKT_01",
            "time": 1750000050000,
            "side": 1,
            "type": 2,
            "status": 2,
            "executedQty": "10.0",  # Only ~50%
            "executedPrice": "1740.0",
        }
        self.client.get_order_history.return_value = [partial_order]
        self.client.get_order_detail.return_value = dict(partial_order, symbol="POL_IDR")
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res, "Partial fill < 98% must be rejected")

    # ----------------------------------------------------------------------
    # 48. Order in history with conflicting or invalid detail rejected
    # ----------------------------------------------------------------------
    def test_48_conflicting_order_detail_rejected(self):
        """If get_order_detail fails, or returns status != FILLED or wrong symbol, reject."""
        hist_order = {
            "orderId": "DETAIL_CHECK_01",
            "time": 1750000050000,
            "side": 1,
            "status": 2,
        }
        self.client.get_order_history.return_value = [hist_order]

        # 48a: get_order_detail throws exception
        self.client.get_order_detail.side_effect = Exception("API connection dropped")
        res = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res)

        # 48b: detail has different symbol (e.g. BTC_IDR)
        self.client.get_order_detail.side_effect = None
        self.client.get_order_detail.return_value = {
            "orderId": "DETAIL_CHECK_01",
            "symbol": "BTC_IDR",
            "side": 1,
            "type": 2,
            "status": 2,
            "executedQty": "20.2",
            "executedPrice": "1740.0",
        }
        res_sym = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res_sym, "Wrong symbol in detail must be rejected")

        # 48c: detail has status CANCELED (3)
        self.client.get_order_detail.return_value = {
            "orderId": "DETAIL_CHECK_01",
            "symbol": "POL_IDR",
            "side": 1,
            "type": 2,
            "status": 3,  # CANCELED
            "executedQty": "20.2",
            "executedPrice": "1740.0",
        }
        res_status = self.executor.reconcile_emergency_sell_from_history(
            "POL_IDR",
            remaining_qty=20.2,
            window_start_ms=1750000000000,
            window_end_ms=1750000100000,
        )
        self.assertIsNone(res_status, "Canceled detail status must be rejected")

    # ----------------------------------------------------------------------
    # 49. Database failure during/after reconciliation prevents re-sell
    # ----------------------------------------------------------------------
    def test_49_database_failure_after_reconciliation_prevents_duplicate_sell(self):
        """If database throws during _resolve_exit, subsequent runs do NOT re-sell."""
        trade = dict(
            self.pol_trade,
            oco_state="SL_RECOVERY_IN_PROGRESS",
            raw_entry_order={"recovery_claimed_at": "2026-10-12T10:00:00+00:00"},
        )
        reconciled_res = {
            "state": "SL_HIT",
            "exit_price": 1740.0,
            "exit_qty": 20.2,
            "exit_reason": "EMERGENCY_SL_MARKET",
            "raw_sl": {"orderId": "999888", "status": 2},
            "slippage_flagged": False,
        }

        with patch.object(
            self.executor, "reconcile_emergency_sell_from_history", return_value=reconciled_res
        ):
            with patch.object(self.monitor, "_resolve_exit", side_effect=Exception("DB write failure")):
                with patch.object(self.executor, "recover_stuck_sl") as mock_recover:
                    # Run 1: DB throws
                    with self.assertRaises(Exception):
                        self.monitor._check_one(trade, verbose=False)
                    mock_recover.assert_not_called()

            # Run 2: Trade is still in SL_RECOVERY_IN_PROGRESS in DB
            with patch.object(self.monitor, "_resolve_exit") as mock_resolve_2:
                with patch.object(self.executor, "recover_stuck_sl") as mock_recover_2:
                    self.monitor._check_one(trade, verbose=False)
                    mock_recover_2.assert_not_called()
                    mock_resolve_2.assert_called_once_with(trade, reconciled_res)


if __name__ == "__main__":
    unittest.main()



