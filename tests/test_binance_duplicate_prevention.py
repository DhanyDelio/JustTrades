"""
tests/test_binance_duplicate_prevention.py
==========================================
Unit tests for Binance duplicate order prevention system invariant:
"Untuk satu symbol + satu active setup/signal, maksimal satu entry order aktif."

Covers the 12 required scenarios:
1.  Existing BNB NEW order -> tidak membuat order kedua.
2.  Existing BNB PARTIALLY_FILLED -> tidak membuat order kedua.
3.  Existing BNB position -> tidak membuat entry kedua.
4.  Duplicate candidate dalam satu cycle/batch -> hanya satu order.
5.  Duplicate execution call -> hanya satu order.
6.  Timeout setelah exchange menerima order -> reconciliation menemukan order -> NO RETRY.
7.  Timeout + exchange benar-benar tidak punya order -> retry diperbolehkan / error propagated.
8.  Process restart dengan existing exchange order -> tidak membuat duplicate.
9.  DB belum update tetapi exchange sudah punya order -> tidak membuat duplicate.
10. Dua execution path/race -> hanya satu entry (thread-safe lock).
11. Existing behavior / risk limits tetap pass.
12. Test khusus BNB regression untuk failure yang baru terjadi.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

from binance.exceptions import BinanceAPIException
import requests

from core.executors.spot_order_executor import SpotOrderExecutor
from core.repositories.spot_trade_repository import SpotTradeRepository


def _make_bnb_cand():
    return {
        "symbol": "BNBUSDT",
        "entry_price": 718.5,
        "sl": 705.0,
        "tp1": 750.0,
        "sizing": {
            "qty": 0.016,
            "notional_usd": 11.496,
            "warnings": [],
        },
        "constraints": {
            "step_size": 0.001,
            "tick_size": 0.1,
            "min_qty": 0.001,
            "min_notional": 10.0,
        },
    }


class TestBinanceDuplicateOrderPrevention(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_repo = MagicMock(spec=SpotTradeRepository)
        self.mock_repo.load_trade_log.return_value = []
        self.executor = SpotOrderExecutor(
            self.mock_client,
            dry_run=False,
            auto_confirm=True,
            repo=self.mock_repo,
        )
        self.cand = _make_bnb_cand()

    # ----------------------------------------------------------------------
    # 1. Existing BNB NEW order -> tidak membuat order kedua
    # ----------------------------------------------------------------------
    def test_01_existing_bnb_new_order_prevents_second_order(self):
        existing_order = {
            "orderId": 197116,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.50000000",
            "origQty": "0.01600000",
        }
        self.mock_client.get_open_orders.return_value = [existing_order]

        result = self.executor.execute(self.cand)

        self.assertEqual(result["orderId"], 197116)
        self.mock_client.create_order.assert_not_called()

    # ----------------------------------------------------------------------
    # 2. Existing BNB PARTIALLY_FILLED -> tidak membuat order kedua
    # ----------------------------------------------------------------------
    def test_02_existing_bnb_partially_filled_prevents_second_order(self):
        existing_order = {
            "orderId": 197117,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "PARTIALLY_FILLED",
            "price": "718.50000000",
            "origQty": "0.01600000",
            "executedQty": "0.00800000",
        }
        self.mock_client.get_open_orders.return_value = [existing_order]

        result = self.executor.execute(self.cand)

        self.assertEqual(result["orderId"], 197117)
        self.mock_client.create_order.assert_not_called()

    # ----------------------------------------------------------------------
    # 3. Existing BNB position -> tidak membuat entry kedua
    # ----------------------------------------------------------------------
    def test_03_existing_bnb_position_prevents_second_entry(self):
        self.mock_client.get_open_orders.return_value = []
        self.mock_client.get_asset_balance.return_value = {"free": "0.016", "locked": "0.0"}
        self.mock_repo.load_trade_log.return_value = [
            {"entry_order_id": 197110, "symbol": "BNBUSDT", "exit_status": "OPEN"}
        ]

        result = self.executor.execute(self.cand)

        self.assertEqual(result["orderId"], 197110)
        self.mock_client.create_order.assert_not_called()

    # ----------------------------------------------------------------------
    # 4. Duplicate candidate dalam satu cycle -> hanya satu order
    # ----------------------------------------------------------------------
    def test_04_duplicate_candidate_in_single_cycle_places_only_one_order(self):
        # First call has no open orders
        created_order = {
            "orderId": 197118,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.side_effect = [
            [],               # Call 1 pre-flight: empty
            [created_order],  # Call 2 pre-flight: finds order from Call 1
        ]
        self.mock_client.get_asset_balance.return_value = {"free": "0.0", "locked": "0.0"}
        self.mock_client.create_order.return_value = created_order

        # Call 1
        res1 = self.executor.execute(self.cand)
        # Call 2 with identical candidate in same cycle
        res2 = self.executor.execute(self.cand)

        self.assertEqual(res1["orderId"], 197118)
        self.assertEqual(res2["orderId"], 197118)
        # create_order must be called EXACTLY ONCE
        self.assertEqual(self.mock_client.create_order.call_count, 1)

    # ----------------------------------------------------------------------
    # 5. Duplicate execution call -> hanya satu order
    # ----------------------------------------------------------------------
    def test_05_duplicate_execution_call_idempotent(self):
        created_order = {
            "orderId": 197119,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.side_effect = [
            [],
            [created_order],
            [created_order],
        ]
        self.mock_client.get_asset_balance.return_value = {"free": "0.0", "locked": "0.0"}
        self.mock_client.create_order.return_value = created_order

        self.executor.execute(self.cand)
        self.executor.execute(self.cand)
        self.executor.execute(self.cand)

        self.assertEqual(self.mock_client.create_order.call_count, 1)

    # ----------------------------------------------------------------------
    # 6. Timeout setelah exchange menerima order -> reconciliation menemukan order -> NO RETRY
    # ----------------------------------------------------------------------
    def test_06_timeout_after_exchange_acceptance_reconciles_without_retry(self):
        accepted_order = {
            "orderId": 197120,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.side_effect = [
            [],                # Pre-flight: empty
            [accepted_order],  # Post-timeout reconciliation: order found!
        ]
        self.mock_client.get_asset_balance.return_value = {"free": "0.0", "locked": "0.0"}
        # create_order raises network timeout after exchange accepted it
        self.mock_client.create_order.side_effect = requests.exceptions.Timeout("Read timed out")

        # Must NOT raise, must return the reconciled order
        res = self.executor.execute(self.cand)

        self.assertEqual(res["orderId"], 197120)
        self.assertEqual(self.mock_client.create_order.call_count, 1)  # NO RETRY
        self.mock_repo.log_trade.assert_called_once_with(accepted_order, self.cand, correlation_cluster_id=None)

    # ----------------------------------------------------------------------
    # 7. Timeout + exchange benar-benar tidak punya order -> error propagated
    # ----------------------------------------------------------------------
    def test_07_timeout_with_no_exchange_order_propagates_error(self):
        self.mock_client.get_open_orders.side_effect = [
            [],  # Pre-flight
            [],  # Post-timeout reconciliation: confirmed absent
        ]
        self.mock_client.get_asset_balance.return_value = {"free": "0.0", "locked": "0.0"}
        self.mock_client.create_order.side_effect = requests.exceptions.Timeout("Connection refused")

        with self.assertRaises(RuntimeError) as ctx:
            self.executor.execute(self.cand)

        self.assertIn("verified absent on exchange", str(ctx.exception))
        self.mock_repo.log_trade.assert_not_called()

    # ----------------------------------------------------------------------
    # 8. Process restart dengan existing exchange order -> tidak membuat duplicate
    # ----------------------------------------------------------------------
    def test_08_process_restart_with_existing_exchange_order_blocks_duplicate(self):
        # Simulate fresh executor instance on fresh process restart
        fresh_executor = SpotOrderExecutor(
            self.mock_client,
            dry_run=False,
            auto_confirm=True,
            repo=self.mock_repo,
        )
        existing_order = {
            "orderId": 197116,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.return_value = [existing_order]

        res = fresh_executor.execute(self.cand)

        self.assertEqual(res["orderId"], 197116)
        self.mock_client.create_order.assert_not_called()

    # ----------------------------------------------------------------------
    # 9. DB belum update tetapi exchange sudah punya order -> tidak membuat duplicate
    # ----------------------------------------------------------------------
    def test_09_db_lag_with_existing_exchange_order_blocks_duplicate(self):
        # Supabase returned empty list (e.g. read replica lag)
        self.mock_repo.load_trade_log.return_value = []
        existing_order = {
            "orderId": 197116,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.return_value = [existing_order]

        res = self.executor.execute(self.cand)

        self.assertEqual(res["orderId"], 197116)
        self.mock_client.create_order.assert_not_called()
        # Backfills to repo since DB was lagging
        self.mock_repo.log_trade.assert_called_once_with(existing_order, self.cand, correlation_cluster_id=None)

    # ----------------------------------------------------------------------
    # 10. Dua execution path/race -> hanya satu entry
    # ----------------------------------------------------------------------
    def test_10_concurrent_execution_race_places_only_one_order(self):
        created_order = {
            "orderId": 197125,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        shared_orders = []

        def mock_get_open_orders(symbol=None):
            return list(shared_orders)

        def mock_create_order(**kwargs):
            time.sleep(0.01)  # small window
            shared_orders.append(created_order)
            return created_order

        self.mock_client.get_open_orders.side_effect = mock_get_open_orders
        self.mock_client.create_order.side_effect = mock_create_order
        self.mock_client.get_asset_balance.return_value = {"free": "0.0", "locked": "0.0"}

        results = []

        def worker():
            res = self.executor.execute(self.cand)
            results.append(res)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        # Both workers received order #197125
        self.assertEqual(results[0]["orderId"], 197125)
        self.assertEqual(results[1]["orderId"], 197125)
        # create_order was called exactly 1 time
        self.assertEqual(self.mock_client.create_order.call_count, 1)

    # ----------------------------------------------------------------------
    # 11. Existing behavior/risk limits tetap pass
    # ----------------------------------------------------------------------
    def test_11_existing_behavior_and_dry_run_pass(self):
        dry_executor = SpotOrderExecutor(
            self.mock_client,
            dry_run=True,
            auto_confirm=True,
            repo=self.mock_repo,
        )
        res = dry_executor.execute(self.cand)
        self.assertEqual(res["status"], "NEW")
        self.assertTrue(res["orderId"].startswith("DRY_"))
        self.mock_client.create_order.assert_not_called()

    # ----------------------------------------------------------------------
    # 12. Test khusus BNB regression untuk failure yang baru terjadi
    # ----------------------------------------------------------------------
    def test_12_bnb_specific_regression_acceptance_criteria(self):
        """
        Acceptance criteria verification:
        BNB duplicate scenario:
          first execution  = 1 order placed
          second execution = 0 new orders placed (reconciled from exchange)
          total exchange order = 1
        """
        order_1 = {
            "orderId": 197116,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.50000000",
            "origQty": "0.01600000",
        }
        exchange_store = []

        def mock_get_open_orders(symbol=None):
            return list(exchange_store)

        def mock_create_order(**kwargs):
            exchange_store.append(order_1)
            return order_1

        self.mock_client.get_open_orders.side_effect = mock_get_open_orders
        self.mock_client.create_order.side_effect = mock_create_order
        self.mock_client.get_asset_balance.return_value = {"free": "0.0", "locked": "0.0"}

        # Step 1: First execution
        first_res = self.executor.execute(self.cand)
        self.assertEqual(first_res["orderId"], 197116)
        self.assertEqual(len(exchange_store), 1)
        self.assertEqual(self.mock_client.create_order.call_count, 1)

        # Step 2: Second execution (retry, duplicate cycle, or restart)
        second_res = self.executor.execute(self.cand)
        self.assertEqual(second_res["orderId"], 197116)
        # Total exchange orders remain 1!
        self.assertEqual(len(exchange_store), 1)
        self.assertEqual(self.mock_client.create_order.call_count, 1)  # 0 new orders!

    # ----------------------------------------------------------------------
    # 13. Arbitrary BNB balance (e.g. for fee discount/dust) allows entry
    # ----------------------------------------------------------------------
    def test_13_arbitrary_bnb_balance_without_open_trade_allows_entry(self):
        """
        Base asset balance exists (e.g. 0.5 BNB held for fees/dust),
        but NO active trade in repo and NO open orders on exchange.
        Must NOT falsely treat as active position -> valid entry ALLOWED.
        """
        created_order = {
            "orderId": 197130,
            "symbol": "BNBUSDT",
            "side": "BUY",
            "status": "NEW",
            "price": "718.5",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.return_value = []
        self.mock_client.get_asset_balance.return_value = {"free": "0.500", "locked": "0.0"}
        self.mock_repo.load_trade_log.return_value = []  # No open bot trades!
        self.mock_client.create_order.return_value = created_order

        res = self.executor.execute(self.cand)

        self.assertEqual(res["orderId"], 197130)
        self.mock_client.create_order.assert_called_once()

    # ----------------------------------------------------------------------
    # 14. Active SELL order on exchange blocks duplicate entry
    # ----------------------------------------------------------------------
    def test_14_active_exchange_sell_order_blocks_second_entry(self):
        """
        Exchange has an active SELL order (e.g. OCO TP/SL or Limit Sell)
        even if repo/DB has not updated yet.
        Must detect as active position and prevent second entry.
        """
        active_sell_order = {
            "orderId": 197140,
            "symbol": "BNBUSDT",
            "side": "SELL",
            "status": "NEW",
            "price": "750.0",
            "origQty": "0.016",
        }
        self.mock_client.get_open_orders.return_value = [active_sell_order]
        self.mock_repo.load_trade_log.return_value = []

        res = self.executor.execute(self.cand)

        self.mock_client.create_order.assert_not_called()
        self.assertEqual(res["symbol"], "BNBUSDT")
        self.assertEqual(res["status"], "FILLED")


if __name__ == "__main__":
    unittest.main()

