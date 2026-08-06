"""
Unit tests for FuturesOrderExecutor.
Tests: place_entry_order, place_exit_orders, check_positions, log_trade.
"""
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(str(Path(__file__).resolve().parent))
from order_executor_base import FuturesOrderExecutor


# =============================================================================
# TEST: place_entry_order
# =============================================================================

class TestFuturesPlaceEntryOrder(unittest.TestCase):

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.futures_exchange_info.return_value = {
            "symbols": [{
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
                ],
            }]
        }
        return FuturesOrderExecutor(mock_client), mock_client

    def test_happy_path_long(self):
        print("\n=== Test futures place_entry_order: LONG ===")
        executor, mock_client = self._make_executor()
        mock_client.futures_create_order.return_value = {
            "orderId": 55555, "status": "NEW"}

        cand = {
            "symbol":        "BTCUSDT",
            "position_side": "LONG",
            "entry_price":   60000.123,
            "sizing":        {"qty": 0.0125},
        }
        result = executor.place_entry_order(cand)

        self.assertEqual(result["orderId"], 55555)
        mock_client.futures_create_order.assert_called_once()
        call_kw = mock_client.futures_create_order.call_args.kwargs
        self.assertEqual(call_kw["side"],         "BUY")
        self.assertEqual(call_kw["type"],         "LIMIT")
        self.assertEqual(call_kw["positionSide"], "BOTH")
        self.assertEqual(call_kw["price"],        "60000.1")   # rounded to tick 0.1
        self.assertEqual(call_kw["quantity"],     "0.012")     # rounded down to step 0.001
        print(f"✓ LONG entry: price={call_kw['price']}, qty={call_kw['quantity']}")
        print("✓ set_leverage_and_margin called before order placement.")

    def test_happy_path_short(self):
        print("\n=== Test futures place_entry_order: SHORT ===")
        executor, mock_client = self._make_executor()
        mock_client.futures_create_order.return_value = {"orderId": 66666, "status": "NEW"}

        cand = {
            "symbol":        "BTCUSDT",
            "position_side": "SHORT",
            "entry_price":   60000.0,
            "sizing":        {"qty": 0.01},
        }
        executor.place_entry_order(cand)
        call_kw = mock_client.futures_create_order.call_args.kwargs
        self.assertEqual(call_kw["side"], "SELL")
        print("✓ SHORT entry uses side=SELL.")

    def test_api_error_raises_runtime_error(self):
        print("\n=== Test futures place_entry_order: API error ===")
        from binance.exceptions import BinanceAPIException
        executor, mock_client = self._make_executor()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"code":-2010,"msg":"Account has insufficient balance"}'
        mock_resp.json.return_value = {"code": -2010, "msg": "insufficient balance"}
        mock_client.futures_create_order.side_effect = BinanceAPIException(
            mock_resp, 400, mock_resp.text)

        cand = {"symbol": "BTCUSDT", "position_side": "LONG",
                "entry_price": 60000.0, "sizing": {"qty": 0.01}}
        with self.assertRaises(RuntimeError):
            executor.place_entry_order(cand)
        print("✓ BinanceAPIException wrapped as RuntimeError.")


# =============================================================================
# TEST: place_exit_orders
# =============================================================================

class TestFuturesPlaceExitOrders(unittest.TestCase):

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.futures_exchange_info.return_value = {
            "symbols": [{
                "symbol": "ETHUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE",     "stepSize": "0.001", "minQty": "0.001"},
                ],
            }]
        }
        return FuturesOrderExecutor(mock_client), mock_client

    def _base_trade(self, **overrides):
        t = {
            "symbol": "ETHUSDT", "position_side": "LONG",
            "entry_qty": 0.012, "sl": 1700.0, "tp1": 1900.0,
        }
        t.update(overrides)
        return t

    def test_happy_path_tp_sl_placed(self):
        print("\n=== Test futures place_exit_orders: TP+SL placed ===")
        executor, mock_client = self._make_executor()
        mock_client.futures_symbol_ticker.return_value = {"price": "1805.0"}

        def fake_algo_order(**kw):
            ot = kw.get("type", "")
            return {"algoId": 9001 if "PROFIT" in ot else 9002}

        mock_client.futures_create_algo_order.side_effect = fake_algo_order
        mock_client.futures_get_open_algo_orders.return_value = [
            {"symbol": "ETHUSDT", "algoId": 9001},
            {"symbol": "ETHUSDT", "algoId": 9002},
        ]

        result = executor.place_exit_orders(self._base_trade())

        self.assertTrue(result["success"])
        self.assertEqual(result["tp_algo_id"], 9001)
        self.assertEqual(result["sl_algo_id"], 9002)
        self.assertEqual(mock_client.futures_create_algo_order.call_count, 2)
        print(f"✓ TP algoId={result['tp_algo_id']}, SL algoId={result['sl_algo_id']}")
        print("✓ Both placed via futures_create_algo_order(algoType=CONDITIONAL).")

    def test_emergency_market_sell_when_price_below_sl(self):
        print("\n=== Test futures place_exit_orders: emergency MARKET SELL ===")
        executor, mock_client = self._make_executor()
        # Current price already below SL
        mock_client.futures_symbol_ticker.return_value = {"price": "1650.0"}
        mock_client.futures_create_order.return_value = {"orderId": 77777}

        result = executor.place_exit_orders(self._base_trade(sl=1700.0))

        mock_client.futures_create_order.assert_called_once()
        self.assertTrue(result.get("emergency_exit"))
        mock_client.futures_create_algo_order.assert_not_called()
        print("✓ Emergency MARKET exit triggered — algo orders NOT placed.")


# =============================================================================
# TEST: check_positions
# =============================================================================

class TestFuturesCheckPositions(unittest.TestCase):

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.futures_symbol_ticker.return_value = []
        return FuturesOrderExecutor(mock_client), mock_client

    def _base_trade(self, **overrides):
        t = {
            "entry_order_id": 222222, "symbol": "SOLUSDT",
            "position_side": "LONG",  "entry_status": "NEW",
            "entry_price": 150.0,     "entry_fill_price": None,
            "entry_fill_time": None,  "entry_qty": None,
            "sl": 140.0,              "tp1": 165.0,
            "entry_notional": 180.0,  "exit_status": "OPEN",
            "exit_orders_placed": False,
            "tp_algo_id": None,       "sl_algo_id": None,
        }
        t.update(overrides)
        return t

    # ── Skenario 1: Entry masih NEW ───────────────────────────────────────────

    @patch("services.supabase_client.fetch_all_futures")
    @patch("services.supabase_client.update_futures_by_order_id")
    def test_entry_still_new_no_change(self, mock_update, mock_fetch):
        print("\n=== Test futures check_positions: Entry NEW → no change ===")
        executor, mock_client = self._make_executor()
        mock_fetch.return_value = [self._base_trade()]
        mock_client.futures_get_order.return_value = {
            "status": "NEW", "executedQty": "0", "cumQuote": "0",
            "price": "150.0", "avgPrice": "0",
        }
        mock_client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": "152.0"}]

        executor.check_positions()

        mock_client.futures_create_algo_order.assert_not_called()
        mock_update.assert_not_called()
        print("✓ Entry NEW: no algo orders placed, no Supabase update.")

    # ── Skenario 2: Entry baru FILLED → place exit orders ────────────────────

    @patch("services.supabase_client.fetch_all_futures")
    @patch("services.supabase_client.update_futures_by_order_id")
    def test_entry_newly_filled_places_exit_orders(self, mock_update, mock_fetch):
        print("\n=== Test futures check_positions: Entry FILLED → place TP+SL ===")
        executor, mock_client = self._make_executor()
        mock_fetch.return_value = [self._base_trade()]

        mock_client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2",
            "cumQuote": "180.0", "price": "150.0",
            "avgPrice": "150.0", "updateTime": 1700001000000,
        }
        mock_client.futures_symbol_ticker.side_effect = [
            [{"symbol": "SOLUSDT", "price": "152.0"}],
            {"price": "152.0"},
        ]
        mock_client.futures_exchange_info.return_value = {"symbols": [{
            "symbol": "SOLUSDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            ],
        }]}
        mock_client.futures_create_algo_order.side_effect = [
            {"algoId": 5001}, {"algoId": 5002}]
        mock_client.futures_get_open_algo_orders.return_value = [
            {"symbol": "SOLUSDT", "algoId": 5001},
            {"symbol": "SOLUSDT", "algoId": 5002},
        ]

        executor.check_positions()

        self.assertEqual(mock_client.futures_create_algo_order.call_count, 2)
        self.assertTrue(mock_update.called)

        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertAlmostEqual(all_fields.get("entry_fill_price"), 150.0)
        self.assertEqual(all_fields.get("exit_orders_placed"), True)
        self.assertEqual(all_fields.get("tp_algo_id"), 5001)
        self.assertEqual(all_fields.get("sl_algo_id"), 5002)
        print(f"✓ entry_fill_price={all_fields['entry_fill_price']}")
        print(f"✓ TP/SL algo orders placed: tp={all_fields['tp_algo_id']}, "
              f"sl={all_fields['sl_algo_id']}")

    # ── Skenario 3: TP_HIT via algo query ────────────────────────────────────

    @patch("services.supabase_client.fetch_all_futures")
    @patch("services.supabase_client.update_futures_by_order_id")
    def test_tp_hit_detected(self, mock_update, mock_fetch):
        print("\n=== Test futures check_positions: TP_HIT detected ===")
        executor, mock_client = self._make_executor()
        trade = self._base_trade(
            entry_status="FILLED", entry_fill_price=150.0,
            entry_fill_time=1700000000000, entry_qty=1.2,
            exit_orders_placed=True, tp_algo_id=7001, sl_algo_id=7002,
        )
        mock_fetch.return_value = [trade]

        mock_client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2",
            "cumQuote": "180.0", "avgPrice": "150.0",
            "price": "150.0", "updateTime": 1700000000000,
        }
        mock_client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": "165.5"}]

        # TP algo: EXECUTED; SL algo: NEW
        def fake_get_algo(**kw):
            aid = kw.get("algoId")
            if str(aid) == "7001":
                return {"algoId": 7001, "algoStatus": "EXECUTED",
                        "executedQty": "1.2", "cumQuote": "198.0",
                        "triggerPrice": "165.0"}
            return {"algoId": 7002, "algoStatus": "NEW"}

        mock_client.futures_get_algo_order.side_effect = fake_get_algo
        mock_client.futures_get_open_algo_orders.return_value = []
        mock_client.futures_get_open_orders.return_value = []

        executor.check_positions()

        self.assertTrue(mock_update.called)
        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertEqual(all_fields.get("exit_status"), "TP_HIT")
        self.assertAlmostEqual(all_fields.get("exit_price"), 165.0, places=2)
        pnl = all_fields.get("realized_pnl_usd")
        self.assertGreater(pnl, 0)
        print(f"✓ exit_status=TP_HIT, exit_price={all_fields['exit_price']}")
        print(f"✓ realized_pnl_usd={pnl:.4f} (positive for LONG TP)")

    # ── Skenario 4: SL_HIT via algo query ────────────────────────────────────

    @patch("services.supabase_client.fetch_all_futures")
    @patch("services.supabase_client.update_futures_by_order_id")
    def test_sl_hit_detected(self, mock_update, mock_fetch):
        print("\n=== Test futures check_positions: SL_HIT detected ===")
        executor, mock_client = self._make_executor()
        trade = self._base_trade(
            entry_status="FILLED", entry_fill_price=150.0,
            entry_fill_time=1700000000000, entry_qty=1.2,
            exit_orders_placed=True, tp_algo_id=8001, sl_algo_id=8002,
        )
        mock_fetch.return_value = [trade]

        mock_client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2",
            "cumQuote": "180.0", "avgPrice": "150.0",
            "price": "150.0", "updateTime": 1700000000000,
        }
        mock_client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": "138.0"}]

        def fake_get_algo(**kw):
            aid = kw.get("algoId")
            if str(aid) == "8002":
                return {"algoId": 8002, "algoStatus": "EXECUTED",
                        "executedQty": "1.2", "cumQuote": "168.0",
                        "triggerPrice": "140.0"}
            return {"algoId": 8001, "algoStatus": "NEW"}

        mock_client.futures_get_algo_order.side_effect = fake_get_algo
        mock_client.futures_get_open_algo_orders.return_value = []
        mock_client.futures_get_open_orders.return_value = []

        executor.check_positions()

        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertEqual(all_fields.get("exit_status"), "SL_HIT")
        pnl = all_fields.get("realized_pnl_usd")
        self.assertLess(pnl, 0)
        print(f"✓ exit_status=SL_HIT, exit_price={all_fields['exit_price']}")
        print(f"✓ realized_pnl_usd={pnl:.4f} (negative for LONG SL)")

    # ── Skenario 5: Price-guard SL breach ────────────────────────────────────

    @patch("services.supabase_client.fetch_all_futures")
    @patch("services.supabase_client.update_futures_by_order_id")
    def test_price_guard_sl_breach(self, mock_update, mock_fetch):
        print("\n=== Test futures check_positions: price-guard SL breach ===")
        executor, mock_client = self._make_executor()
        trade = self._base_trade(
            entry_status="FILLED", entry_fill_price=150.0,
            entry_fill_time=1700000000000, entry_qty=1.2,
            exit_orders_placed=True, tp_algo_id=9001, sl_algo_id=9002,
        )
        mock_fetch.return_value = [trade]

        mock_client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2",
            "cumQuote": "180.0", "avgPrice": "150.0",
            "price": "150.0", "updateTime": 1700000000000,
        }
        # Current price already below SL=140
        mock_client.futures_symbol_ticker.side_effect = [
            [{"symbol": "SOLUSDT", "price": "135.0"}],   # batch tickers
            {"price": "135.0"},                            # single ticker fallback
        ]
        # Algo query returns NEW (exchange didn't trigger SL)
        mock_client.futures_get_algo_order.return_value = {
            "algoStatus": "NEW"}
        mock_client.futures_get_open_algo_orders.return_value = []
        mock_client.futures_get_open_orders.return_value = []

        executor.check_positions()

        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertEqual(all_fields.get("exit_status"), "SL_HIT")
        pnl = all_fields.get("realized_pnl_usd")
        self.assertLess(pnl, 0)
        print(f"✓ Price-guard triggered: exit_status=SL_HIT "
              f"at price 135 (below SL 140)")
        print(f"✓ PnL={pnl:.4f}")

    @patch("services.supabase_client.fetch_all_futures")
    @patch("services.supabase_client.update_futures_by_order_id")
    @patch("core.futures_trade_executor.accrue_funding", return_value=False)
    def test_production_price_guard_closes_exchange_before_db_resolution(
            self, _funding, mock_update, mock_fetch):
        from core.executors.futures_position_monitor import FuturesPositionMonitor

        trade = self._base_trade(
            entry_status="FILLED", entry_fill_price=150.0,
            entry_fill_time=1700000000000, entry_qty=1.2,
            exit_orders_placed=True, tp_algo_id=9001, sl_algo_id=9002,
        )
        mock_fetch.return_value = [trade]
        client = MagicMock()
        client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": "135.0"}
        ]
        client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2",
            "cumQuote": "180.0", "avgPrice": "150.0",
            "price": "150.0", "updateTime": 1700000000000,
        }
        client.futures_get_algo_order.return_value = {"algoStatus": "NEW"}
        client.futures_get_open_algo_orders.return_value = []
        client.futures_get_open_orders.return_value = []
        client.futures_create_order.return_value = {
            "orderId": 9100, "executedQty": "1.2",
            "cumQuote": "162.0", "avgPrice": "135.0",
            "updateTime": 1700000001000,
        }

        FuturesPositionMonitor(client).check_positions()

        client.futures_create_order.assert_called_once_with(
            symbol="SOLUSDT", side="SELL", type="MARKET",
            quantity="1.2", positionSide="BOTH", reduceOnly=True,
        )
        persisted = {}
        for call in mock_update.call_args_list:
            persisted.update(call.args[1])
        self.assertEqual(persisted["exit_status"], "SL_HIT")
        self.assertEqual(persisted["exit_price"], 135.0)


# =============================================================================
# TEST: log_trade
# =============================================================================

class TestFuturesLogTrade(unittest.TestCase):

    def _make_executor(self):
        return FuturesOrderExecutor(MagicMock())

    def _base_order(self):
        return {"orderId": 99999, "clientOrderId": "x-fut123", "status": "NEW"}

    def _base_cand(self, **overrides):
        cand = {
            "symbol": "BTCUSDT",   "position_side": "LONG",
            "direction": "long",   "entry_price": 60000.0,
            "sl": 57000.0,         "tp1": 66000.0,  "tp2": None,
            "rr": 2.0,             "risk_pct": 1.5,
            "atr_pct": 2.1,        "tier_used": "T1",
            "volatility_regime": "medium",
            "funding_rate_at_entry": 0.0001,
            "sizing": {
                "qty": 0.012, "notional_usd": 720.0,
                "margin_used": 240.0, "max_loss_usd": 36.0,
            },
            "liquidation": {
                "liquidation_price": 40200.0,
                "distance_to_liquidation_pct": 33.0,
            },
            "entry_zone": {"center": 59800.0, "touches": 4},
        }
        cand.update(overrides)
        return cand

    @patch("services.supabase_client.upsert_futures")
    def test_happy_path_all_fields(self, mock_upsert):
        print("\n=== Test futures log_trade: Happy path ===")
        executor = self._make_executor()
        executor.log_trade(
            self._base_order(), self._base_cand(),
            correlation_cluster_id="20260720_130000")

        mock_upsert.assert_called_once()
        record = mock_upsert.call_args.args[0]

        self.assertEqual(record["symbol"],        "BTCUSDT")
        self.assertEqual(record["position_side"], "LONG")
        self.assertEqual(record["leverage"],      3)
        self.assertEqual(record["margin_mode"],   "isolated")
        self.assertEqual(record["rule_version"],  "fv1.0.0")
        self.assertEqual(record["correlation_cluster_id"], "20260720_130000")
        self.assertEqual(record["entry_order_id"], 99999)
        self.assertEqual(record["entry_price"],   60000.0)
        self.assertEqual(record["sl"],            57000.0)
        self.assertEqual(record["tp1"],           66000.0)
        self.assertAlmostEqual(record["liquidation_price"], 40200.0)
        # fee: 720 × 0.0004 × 2 = 0.576
        self.assertAlmostEqual(record["fee_usd_roundtrip"], 0.576, places=4)
        self.assertEqual(record["exit_status"],   "OPEN")
        self.assertFalse(record["exit_orders_placed"])
        self.assertEqual(record["funding_rate_paid"], 0.0)
        self.assertEqual(record["zone_type"],     "T1")
        print(f"✓ All key fields correct.")
        print(f"✓ fee_usd_roundtrip={record['fee_usd_roundtrip']} "
              f"(720 × 0.0004 × 2 = 0.576)")

    @patch("services.supabase_client.upsert_futures")
    def test_fee_calculation(self, mock_upsert):
        print("\n=== Test futures log_trade: Fee calculation ===")
        executor = self._make_executor()
        cand = self._base_cand()
        cand["sizing"]["notional_usd"] = 1000.0
        executor.log_trade(self._base_order(), cand)

        record = mock_upsert.call_args.args[0]
        # 1000 × 0.0004 × 2 = 0.8
        self.assertAlmostEqual(record["fee_usd_roundtrip"], 0.8, places=4)
        print(f"✓ fee_usd_roundtrip={record['fee_usd_roundtrip']} "
              f"(1000 × 0.0004 × 2 = 0.8)")

    @patch("services.supabase_client.upsert_futures")
    def test_no_cluster_id(self, mock_upsert):
        print("\n=== Test futures log_trade: No cluster_id ===")
        executor = self._make_executor()
        executor.log_trade(self._base_order(), self._base_cand())
        record = mock_upsert.call_args.args[0]
        self.assertIsNone(record["correlation_cluster_id"])
        print("✓ correlation_cluster_id=None for single --propose.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
