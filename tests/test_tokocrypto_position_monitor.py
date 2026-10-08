"""
test_tokocrypto_position_monitor.py — Unit tests and regression tests for TokocryptoPositionMonitor.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import TokocryptoClient
from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
from core.executors.tokocrypto_position_monitor import TokocryptoPositionMonitor


class TestTokocryptoPositionMonitor(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock(spec=TokocryptoClient)
        self.mock_client.normalize_symbol = lambda s: s
        self.executor = TokocryptoOrderExecutor(
            self.mock_client,
            supervised=False,
            trading_phase="PHASE_3",
            dry_run=False,
        )
        self.monitor = TokocryptoPositionMonitor(self.mock_client, self.executor)

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_sl_hit_expired_sibling_reconciliation_payload(self, mock_tg, mock_update):
        """
        Regression test:
        TP=EXPIRED (6) + SL=FILLED (2)
        Verifies that update_tokocrypto_by_order_id receives exit_time as a bigint (epoch ms int),
        NOT an ISO-8601 string, and time_to_resolution_sec as an int.
        """
        trade = {
            "symbol": "DOGE_IDR",
            "entry_order_id": "917204762",
            "entry_status": "FILLED",
            "b_order_list_id": "25178172225",
            "tp_order_id": "917242376",
            "sl_order_id": "917242377",
            "entry_price": 1639.0,
            "entry_fill_price": 1639.0,
            "entry_fill_time": 1791334182352,
            "entry_qty": 119.0,
            "entry_notional_idr": 195041.0,
            "tp_price": 1705.0,
            "sl_price": 1554.0,
            "exit_status": "OPEN",
        }

        raw_tp = {
            "orderId": "917242376",
            "status": 6,  # EXPIRED
            "executedPrice": "0",
            "executedQty": "0",
            "createTime": 1791346150197,
            "time": 1791346150197,
        }
        raw_sl = {
            "orderId": "917242377",
            "status": 2,  # FILLED
            "executedPrice": "1552",
            "executedQty": "118",
            "createTime": 1791346150197,
            "time": 1791346150197,
        }

        def mock_get_order_detail(sym, oid):
            if str(oid) == "917242376":
                return raw_tp
            if str(oid) == "917242377":
                return raw_sl
            raise ValueError(f"Unknown order id {oid}")

        self.mock_client.get_order_detail.side_effect = mock_get_order_detail

        self.monitor._check_one(trade, verbose=True)

        mock_update.assert_called_once()
        call_entry_oid, payload = mock_update.call_args[0]

        self.assertEqual(call_entry_oid, "917204762")
        self.assertEqual(payload["exit_status"], "SL_HIT")
        self.assertEqual(payload["oco_state"], "SL_HIT")
        self.assertEqual(payload["exit_reason"], "OCO_TRIGGERED")
        self.assertEqual(payload["exit_price"], 1552.0)

        # Critical: exit_time must be bigint int (epoch ms), NOT an ISO string
        self.assertIsInstance(payload["exit_time"], int)
        self.assertEqual(payload["exit_time"], 1791346150197)

        # Critical: time_to_resolution_sec must be bigint int
        expected_ttr = (1791346150197 - 1791334182352) // 1000
        self.assertIsInstance(payload["time_to_resolution_sec"], int)
        self.assertEqual(payload["time_to_resolution_sec"], expected_ttr)

        # PnL calculations
        self.assertAlmostEqual(payload["realized_pnl_idr"], (1552.0 - 1639.0) * 119.0, places=2)

        # Telegram notification
        mock_tg.assert_called()
        self.assertTrue(any("SL HIT: DOGE_IDR" in str(arg) for arg in mock_tg.call_args[0]))

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_tp_hit_expired_sibling_reconciliation_payload(self, mock_tg, mock_update):
        """
        Regression test:
        TP=FILLED (2) + SL=EXPIRED (6)
        Verifies that exit_status is TP_HIT and exit_time is integer epoch ms.
        """
        trade = {
            "symbol": "BTC_IDR",
            "entry_order_id": "111222333",
            "entry_status": "FILLED",
            "b_order_list_id": "999888777",
            "tp_order_id": "111222334",
            "sl_order_id": "111222335",
            "entry_price": 1000000.0,
            "entry_fill_price": 1000000.0,
            "entry_fill_time": 1791330000000,
            "entry_qty": 0.5,
            "entry_notional_idr": 500000.0,
            "tp_price": 1030000.0,
            "sl_price": 970000.0,
            "exit_status": "OPEN",
        }

        raw_tp = {
            "orderId": "111222334",
            "status": 2,  # FILLED
            "executedPrice": "1030000",
            "executedQty": "0.5",
            "createTime": 1791335000000,
            "time": 1791335000000,
        }
        raw_sl = {
            "orderId": "111222335",
            "status": 6,  # EXPIRED
            "executedPrice": "0",
            "executedQty": "0",
            "createTime": 1791335000000,
            "time": 1791335000000,
        }

        def mock_get_order_detail(sym, oid):
            if str(oid) == "111222334":
                return raw_tp
            if str(oid) == "111222335":
                return raw_sl
            raise ValueError(f"Unknown order id {oid}")

        self.mock_client.get_order_detail.side_effect = mock_get_order_detail

        self.monitor._check_one(trade, verbose=True)

        mock_update.assert_called_once()
        call_entry_oid, payload = mock_update.call_args[0]

        self.assertEqual(call_entry_oid, "111222333")
        self.assertEqual(payload["exit_status"], "TP_HIT")
        self.assertEqual(payload["oco_state"], "TP_HIT")
        self.assertEqual(payload["exit_price"], 1030000.0)
        self.assertIsInstance(payload["exit_time"], int)
        self.assertEqual(payload["exit_time"], 1791335000000)
        self.assertIsInstance(payload["time_to_resolution_sec"], int)
        self.assertEqual(payload["time_to_resolution_sec"], (1791335000000 - 1791330000000) // 1000)

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_fallback_timestamp_when_order_detail_lacks_epoch(self, mock_tg, mock_update):
        """
        When raw_exit_detail does not have createTime or time, exit_time must fallback
        to current epoch ms integer (never ISO string).
        """
        trade = {
            "symbol": "DOGE_IDR",
            "entry_order_id": "917204762",
            "entry_status": "FILLED",
            "b_order_list_id": "25178172225",
            "tp_order_id": "917242376",
            "sl_order_id": "917242377",
            "entry_price": 1639.0,
            "entry_fill_price": 1639.0,
            "entry_fill_time": None,
            "entry_qty": 119.0,
            "exit_status": "OPEN",
        }

        raw_tp = {"status": 6, "executedPrice": "0"}
        raw_sl = {"status": 2, "executedPrice": "1552"}  # missing time and createTime

        self.mock_client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917242376" else raw_sl

        self.monitor._check_one(trade, verbose=False)

        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        self.assertIsInstance(payload["exit_time"], int)
        self.assertGreater(payload["exit_time"], 1_700_000_000_000)
        self.assertIsNone(payload["time_to_resolution_sec"])

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_monitor_recognizes_legs_and_backfills_list_id(self, mock_tg, mock_update):
        """
        When b_order_list_id is empty in Supabase row, but tp_order_id and sl_order_id exist:
        - Monitor recognizes it as has_oco (does NOT re-place OCO)
        - Discovers bOrderListId from leg details and backfills it into Supabase
        - Updates oco_state to EXECUTING
        """
        trade = {
            "symbol": "ETH_IDR",
            "entry_order_id": "917510981",
            "entry_status": "FILLED",
            "b_order_list_id": "",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 45503763.0,
            "entry_fill_price": 45503757.0,
            "tp_price": 47645461.0,
            "sl_price": 44689248.0,
            "exit_status": "OPEN",
        }

        raw_tp = {
            "orderId": "917549739",
            "bOrderListId": "25208289734",
            "status": 0,  # NEW (active)
            "price": "47645461",
        }
        raw_sl = {
            "orderId": "917549740",
            "bOrderListId": "25208289734",
            "status": 0,  # NEW (active)
            "price": "44622214",
            "stopPrice": "44689248",
        }

        self.mock_client.get_order_detail.side_effect = lambda sym, oid: raw_tp if str(oid) == "917549739" else raw_sl
        with patch.object(self.executor, "place_oco") as mock_place_oco:
            self.monitor._check_one(trade, verbose=True)
            mock_place_oco.assert_not_called()

        # Check update was called with backfilled list id and EXECUTING state
        self.assertTrue(mock_update.called)
        # At least one call should have b_order_list_id backfilled
        backfilled = any(
            call.args[1].get("b_order_list_id") == "25208289734"
            for call in mock_update.call_args_list
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        )
        self.assertTrue(backfilled)

    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_monitor_unprotected_calls_place_oco(self, mock_tg, mock_update):
        """
        When filled position has NO b_order_list_id and NO leg order ids,
        it is recognized as unprotected and triggers place_oco.
        """
        trade = {
            "symbol": "SOL_IDR",
            "entry_order_id": "917510994",
            "entry_status": "FILLED",
            "b_order_list_id": "",
            "tp_order_id": "",
            "sl_order_id": "",
            "entry_price": 1926658.0,
            "entry_fill_price": 1926658.0,
            "tp_price": 2147050.0,
            "sl_price": 1899240.0,
            "exit_status": "OPEN",
        }

        with patch.object(self.executor, "place_oco", return_value={"bOrderListId": "999"}) as mock_place_oco:
            self.monitor._check_one(trade, verbose=False)
            mock_place_oco.assert_called_once_with(trade)


if __name__ == "__main__":
    unittest.main()
