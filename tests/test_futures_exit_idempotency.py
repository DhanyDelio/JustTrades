"""Offline regression tests for production Futures exit-order idempotency."""

import unittest
import threading
from unittest.mock import MagicMock, patch

from binance.exceptions import BinanceAPIException

# Initialise the compatibility module before importing the production class.
import core.futures_trade_executor  # noqa: F401
from core.executors.futures_order_executor import FuturesOrderExecutor
from core.executors.futures_position_monitor import FuturesPositionMonitor


def missing_error():
    response = MagicMock()
    response.status_code = 400
    response.text = '{"code":-2013,"msg":"Order does not exist."}'
    response.json.return_value = {"code": -2013, "msg": "Order does not exist."}
    return BinanceAPIException(response, 400, response.text)


class FakeAlgoExchange:
    def __init__(self):
        self.client = MagicMock()
        self.orders = {}
        self.next_id = 100
        self.create_failures = {}
        self.response_without_id = set()
        self.timeout_after_accept = set()
        self.client.futures_exchange_info.return_value = {
            "symbols": [{
                "symbol": "SOLUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                ],
            }]
        }
        self.client.futures_symbol_ticker.return_value = {"price": "150"}
        self.client.futures_position_information.return_value = [{
            "symbol": "SOLUSDT", "positionSide": "BOTH",
            "positionAmt": "1.2", "markPrice": "150",
        }]
        self.client.futures_get_open_algo_orders.side_effect = self.open_orders
        self.client.futures_get_algo_order.side_effect = self.get_order
        self.client.futures_create_algo_order.side_effect = self.create_order

    def open_orders(self):
        return list(self.orders.values())

    def get_order(self, **params):
        for order in self.orders.values():
            if (params.get("algoId") is not None
                    and str(order["algoId"]) == str(params["algoId"])):
                return order
            if (params.get("clientAlgoId")
                    and order["clientAlgoId"] == params["clientAlgoId"]):
                return order
        raise missing_error()

    def create_order(self, **payload):
        leg = "TP" if payload["type"] == "TAKE_PROFIT_MARKET" else "SL"
        failure = self.create_failures.get(leg)
        if failure:
            raise failure
        self.next_id += 1
        order = {
            "algoId": self.next_id,
            "clientAlgoId": payload["clientAlgoId"],
            "symbol": payload["symbol"],
            "side": payload["side"],
            "positionSide": payload["positionSide"],
            "orderType": payload["type"],
            "triggerPrice": payload["triggerPrice"],
            "quantity": payload.get("quantity", "1.2"),
            "algoStatus": "NEW",
        }
        self.orders[order["clientAlgoId"]] = order
        if leg in self.timeout_after_accept:
            raise TimeoutError(f"{leg} POST timed out")
        if leg in self.response_without_id:
            return {"code": "200", "msg": "success"}
        return {"algoId": order["algoId"], "clientAlgoId": order["clientAlgoId"]}


class FuturesExitIdempotencyTests(unittest.TestCase):
    def trade(self, **overrides):
        value = {
            "entry_order_id": 555,
            "symbol": "SOLUSDT",
            "position_side": "LONG",
            "entry_qty": 1.2,
            "tp1": 165.0,
            "sl": 140.0,
            "tp_algo_id": None,
            "sl_algo_id": None,
        }
        value.update(overrides)
        return value

    def run_executor(self, exchange, trade):
        with patch("core.executors.futures_order_executor._time.sleep"), \
             patch("core.executors.futures_order_executor._send_telegram"):
            return FuturesOrderExecutor(exchange.client).place_exit_orders(trade)

    def test_a_both_succeed_sl_first(self):
        exchange = FakeAlgoExchange()
        result = self.run_executor(exchange, self.trade())
        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(exchange.client.futures_create_algo_order.call_count, 2)
        types = [call.kwargs["type"]
                 for call in exchange.client.futures_create_algo_order.call_args_list]
        self.assertEqual(types, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        for call in exchange.client.futures_create_algo_order.call_args_list:
            self.assertNotIn("timeInForce", call.kwargs)

    def test_h_both_confirmed_absent_create_sl_then_tp(self):
        exchange = FakeAlgoExchange()
        result = self.run_executor(exchange, self.trade())
        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(
            [call.kwargs["type"]
             for call in exchange.client.futures_create_algo_order.call_args_list],
            ["STOP_MARKET", "TAKE_PROFIT_MARKET"],
        )

    def test_b_sl_only_retry_creates_only_tp(self):
        exchange = FakeAlgoExchange()
        exchange.create_failures["TP"] = RuntimeError("TP rejected")
        trade = self.trade()
        first = self.run_executor(exchange, trade)
        self.assertEqual(first["protection_state"], "SL_ONLY")
        sl_id = first["sl_algo_id"]

        trade.update(first)
        exchange.create_failures.pop("TP")
        before = exchange.client.futures_create_algo_order.call_count
        second = self.run_executor(exchange, trade)
        new_calls = exchange.client.futures_create_algo_order.call_args_list[before:]
        self.assertEqual(second["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(second["sl_algo_id"], sl_id)
        self.assertEqual([call.kwargs["type"] for call in new_calls],
                         ["TAKE_PROFIT_MARKET"])

    def test_c_tp_only_retry_creates_only_sl(self):
        exchange = FakeAlgoExchange()
        exchange.create_failures["SL"] = RuntimeError("SL rejected")
        trade = self.trade()
        first = self.run_executor(exchange, trade)
        self.assertEqual(first["protection_state"], "TP_ONLY")
        tp_id = first["tp_algo_id"]

        trade.update(first)
        exchange.create_failures.pop("SL")
        before = exchange.client.futures_create_algo_order.call_count
        second = self.run_executor(exchange, trade)
        new_calls = exchange.client.futures_create_algo_order.call_args_list[before:]
        self.assertEqual(second["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(second["tp_algo_id"], tp_id)
        self.assertEqual([call.kwargs["type"] for call in new_calls], ["STOP_MARKET"])

    def test_d_existing_sl_is_not_recreated(self):
        exchange = FakeAlgoExchange()
        exchange.create_order(
            type="STOP_MARKET", clientAlgoId="legacy-random-sl", symbol="SOLUSDT",
            side="SELL", positionSide="BOTH", triggerPrice="140", quantity="1.2")
        result = self.run_executor(exchange, self.trade())
        created = exchange.client.futures_create_algo_order.call_args_list
        self.assertEqual(result["sl_algo_id"], 101)
        self.assertFalse(any(c.kwargs["type"] == "STOP_MARKET" for c in created))

    def test_e_existing_tp_is_not_recreated(self):
        exchange = FakeAlgoExchange()
        exchange.create_order(
            type="TAKE_PROFIT_MARKET", clientAlgoId="legacy-random-tp", symbol="SOLUSDT",
            side="SELL", positionSide="BOTH", triggerPrice="165", quantity="1.2")
        result = self.run_executor(exchange, self.trade())
        created = exchange.client.futures_create_algo_order.call_args_list
        self.assertEqual(result["tp_algo_id"], 101)
        self.assertFalse(any(c.kwargs["type"] == "TAKE_PROFIT_MARKET" for c in created))

    def test_f_timeout_after_accept_reconciles_deterministic_id(self):
        exchange = FakeAlgoExchange()
        exchange.timeout_after_accept.add("TP")
        result = self.run_executor(exchange, self.trade())
        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(sum(c.kwargs["type"] == "TAKE_PROFIT_MARKET"
                             for c in exchange.client.futures_create_algo_order.call_args_list), 1)

    def test_g_reconciliation_failure_creates_nothing(self):
        exchange = FakeAlgoExchange()
        exchange.client.futures_get_open_algo_orders.side_effect = TimeoutError("outage")
        exchange.client.futures_get_algo_order.side_effect = TimeoutError("outage")
        result = self.run_executor(exchange, self.trade())
        self.assertEqual(result["protection_state"], "UNKNOWN")
        exchange.client.futures_create_algo_order.assert_not_called()

    def test_i_unknown_escalates_without_mutation(self):
        exchange = FakeAlgoExchange()
        exchange.client.futures_get_open_algo_orders.side_effect = TimeoutError("outage")
        exchange.client.futures_get_algo_order.side_effect = TimeoutError("outage")
        trade = self.trade(raw_entry_order={})
        for expected in (1, 2, 3):
            result = self.run_executor(exchange, trade)
            self.assertEqual(result["unknown_protection_cycles"], expected)
            trade["raw_entry_order"] = {"exit_protection": {
                "unknown_protection_cycles": expected}}
        exchange.client.futures_create_algo_order.assert_not_called()

    def test_i_missing_response_id_is_reconciled_not_duplicated(self):
        exchange = FakeAlgoExchange()
        exchange.response_without_id.add("TP")
        trade = self.trade()
        first = self.run_executor(exchange, trade)
        trade.update(first)
        before = exchange.client.futures_create_algo_order.call_count
        second = self.run_executor(exchange, trade)
        self.assertEqual(first["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(second["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(exchange.client.futures_create_algo_order.call_count, before)

    def test_j_alert_classification(self):
        trade = self.trade()
        expected = {
            "SL_ONLY": "PARTIALLY PROTECTED",
            "TP_ONLY": "CRITICAL",
            "UNPROTECTED": "UNPROTECTED",
            "UNKNOWN": "PROTECTION STATE UNKNOWN",
            "STALE_QTY": "QUANTITY MISMATCH",
        }
        with patch("core.executors.futures_order_executor._send_telegram") as notify:
            FuturesOrderExecutor._alert_protection_state(
                trade, {"protection_state": "FULLY_PROTECTED", "errors": []})
            notify.assert_not_called()
            for state, text in expected.items():
                FuturesOrderExecutor._alert_protection_state(
                    trade, {"protection_state": state, "errors": []})
                self.assertIn(text, notify.call_args.args[0])

    def test_k_concurrent_invocations_create_one_pair(self):
        exchange = FakeAlgoExchange()
        executor = FuturesOrderExecutor(exchange.client)
        results = []

        def invoke():
            with patch("core.executors.futures_order_executor._time.sleep"), \
                 patch("core.executors.futures_order_executor._send_telegram"):
                results.append(executor.place_exit_orders(self.trade()))

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(exchange.client.futures_create_algo_order.call_count, 2)
        self.assertTrue(all(r["protection_state"] == "FULLY_PROTECTED"
                            for r in results))

    def test_l_crash_after_accept_recovers_without_local_ids(self):
        exchange = FakeAlgoExchange()
        first = self.run_executor(exchange, self.trade())
        self.assertEqual(first["protection_state"], "FULLY_PROTECTED")
        before = exchange.client.futures_create_algo_order.call_count
        recovered = self.run_executor(exchange, self.trade())
        self.assertEqual(recovered["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(exchange.client.futures_create_algo_order.call_count, before)

    def test_m_closed_position_creates_nothing(self):
        exchange = FakeAlgoExchange()
        exchange.client.futures_position_information.return_value = [{
            "symbol": "SOLUSDT", "positionSide": "BOTH",
            "positionAmt": "0", "markPrice": "150",
        }]
        result = self.run_executor(exchange, self.trade())
        self.assertEqual(result["protection_state"], "POSITION_CLOSED")
        exchange.client.futures_create_algo_order.assert_not_called()

    def test_n_terminal_orders_are_not_active_protection(self):
        exchange = FakeAlgoExchange()
        exchange.orders["jt-555-sl"] = {
            "algoId": 900, "clientAlgoId": "jt-555-sl", "symbol": "SOLUSDT",
            "side": "SELL", "positionSide": "BOTH", "orderType": "STOP_MARKET",
            "triggerPrice": "140", "quantity": "1.2", "algoStatus": "EXECUTED",
        }
        result = self.run_executor(exchange, self.trade(sl_algo_id=900))
        self.assertEqual(result["protection_state"], "UNKNOWN")
        exchange.client.futures_create_algo_order.assert_not_called()

    def test_n_executed_order_wins_over_zero_position_for_fill_resolution(self):
        exchange = FakeAlgoExchange()
        exchange.client.futures_position_information.return_value = [{
            "symbol": "SOLUSDT", "positionSide": "BOTH",
            "positionAmt": "0", "markPrice": "150",
        }]
        exchange.orders["jt-555-sl"] = {
            "algoId": 900, "clientAlgoId": "jt-555-sl", "symbol": "SOLUSDT",
            "side": "SELL", "positionSide": "BOTH", "orderType": "STOP_MARKET",
            "triggerPrice": "140", "quantity": "1.2", "algoStatus": "EXECUTED",
        }
        result = self.run_executor(exchange, self.trade(sl_algo_id=900))
        self.assertEqual(result["protection_state"], "UNKNOWN")
        self.assertTrue(result["terminal_order_seen"])
        exchange.client.futures_create_algo_order.assert_not_called()

    def test_n_canceled_and_expired_orders_are_replaced_not_counted_active(self):
        for terminal_status in ("CANCELED", "EXPIRED"):
            with self.subTest(status=terminal_status):
                exchange = FakeAlgoExchange()
                exchange.orders["jt-555-sl"] = {
                    "algoId": 900, "clientAlgoId": "jt-555-sl",
                    "symbol": "SOLUSDT", "side": "SELL",
                    "positionSide": "BOTH", "orderType": "STOP_MARKET",
                    "triggerPrice": "140", "quantity": "1.2",
                    "algoStatus": terminal_status,
                }
                result = self.run_executor(exchange, self.trade(sl_algo_id=900))
                self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
                self.assertEqual(
                    [call.kwargs["type"] for call in
                     exchange.client.futures_create_algo_order.call_args_list],
                    ["STOP_MARKET", "TAKE_PROFIT_MARKET"],
                )

    def test_o_partial_position_detects_stale_quantity(self):
        exchange = FakeAlgoExchange()
        exchange.client.futures_position_information.return_value = [{
            "symbol": "SOLUSDT", "positionSide": "BOTH",
            "positionAmt": "0.7", "markPrice": "150",
        }]
        exchange.orders["legacy-sl"] = {
            "algoId": 901, "clientAlgoId": "legacy-sl", "symbol": "SOLUSDT",
            "side": "SELL", "positionSide": "BOTH", "orderType": "STOP_MARKET",
            "triggerPrice": "140", "quantity": "1.2", "algoStatus": "NEW",
        }
        result = self.run_executor(exchange, self.trade(sl_algo_id=901))
        self.assertEqual(result["protection_state"], "STALE_QTY")
        exchange.client.futures_create_algo_order.assert_not_called()

    def test_p_position_side_isolation(self):
        exchange = FakeAlgoExchange()
        exchange.client.futures_position_information.return_value = [
            {"symbol": "SOLUSDT", "positionSide": "LONG",
             "positionAmt": "1.2", "markPrice": "150"},
            {"symbol": "SOLUSDT", "positionSide": "SHORT",
             "positionAmt": "-1.2", "markPrice": "150"},
        ]
        long_trade = self.trade(exchange_position_side="LONG")
        short_trade = self.trade(entry_order_id=556, position_side="SHORT",
                                 exchange_position_side="SHORT", tp1=135.0, sl=160.0)
        long_result = self.run_executor(exchange, long_trade)
        short_result = self.run_executor(exchange, short_trade)
        self.assertEqual(long_result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(short_result["protection_state"], "FULLY_PROTECTED")
        sides = {(call.kwargs["side"], call.kwargs["positionSide"])
                 for call in exchange.client.futures_create_algo_order.call_args_list}
        self.assertIn(("SELL", "LONG"), sides)
        self.assertIn(("BUY", "SHORT"), sides)

    def test_q_tp_rejection_details_are_captured(self):
        exchange = FakeAlgoExchange()
        response = MagicMock()
        response.status_code = 400
        response.text = '{"code":-2021,"msg":"Order would immediately trigger."}'
        exchange.create_failures["TP"] = BinanceAPIException(
            response, 400, response.text)
        result = self.run_executor(exchange, self.trade())
        error = next(e for e in result["errors"] if e["leg"] == "TP")
        self.assertEqual(error["code"], -2021)
        self.assertEqual(error["symbol"], "SOLUSDT")
        self.assertEqual(error["position_side"], "BOTH")
        self.assertEqual(error["working_type"], "MARK_PRICE")
        self.assertIn("immediately trigger", error["message"])

    @patch("services.supabase_client.update_futures_by_order_id")
    @patch("services.supabase_client.fetch_all_futures")
    @patch("core.futures_trade_executor.accrue_funding", return_value=False)
    def test_monitor_preserves_partial_id_and_persists_state_and_error(
            self, _funding, fetch, update):
        trade = self.trade(
            entry_status="FILLED", entry_price=150.0, entry_fill_price=150.0,
            entry_fill_time=1, entry_notional=180.0, exit_status="OPEN",
            exit_orders_placed=False, raw_entry_order={"orderId": 555},
        )
        fetch.return_value = [trade]
        client = MagicMock()
        client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": "150"}]
        client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2", "cumQuote": "180",
            "avgPrice": "150", "updateTime": 1,
        }
        error = {
            "leg": "TP", "order_type": "TAKE_PROFIT_MARKET",
            "trigger_price": "165", "code": -2021,
            "message": "Order would immediately trigger", "timestamp": "now",
        }
        partial = {
            "tp_order_id": None, "tp_algo_id": None,
            "sl_order_id": 700, "sl_algo_id": 700,
            "success": False, "protection_state": "SL_ONLY",
            "errors": [error], "terminal_order_seen": False,
        }
        with patch.object(FuturesOrderExecutor, "place_exit_orders",
                          return_value=partial), \
             patch("core.executors.futures_position_monitor._send_telegram"):
            FuturesPositionMonitor(client).check_positions()

        persisted = {}
        for call in update.call_args_list:
            persisted.update(call.args[1])
        self.assertEqual(persisted["sl_algo_id"], 700)
        self.assertFalse(persisted["exit_orders_placed"])
        metadata = persisted["raw_entry_order"]["exit_protection"]
        self.assertEqual(metadata["state"], "SL_ONLY")
        self.assertEqual(metadata["errors"][0]["code"], -2021)

    @patch("services.supabase_client.update_futures_by_order_id")
    @patch("services.supabase_client.fetch_all_futures")
    @patch("core.futures_trade_executor.accrue_funding", return_value=False)
    def test_monitor_position_closed_eagerly_terminalizes_without_fake_pnl(
            self, _funding, fetch, update):
        trade = self.trade(
            entry_status="FILLED", entry_price=150.0, entry_fill_price=150.0,
            entry_fill_time=1, entry_notional=180.0, exit_status="OPEN",
            exit_orders_placed=False, raw_entry_order={"orderId": 555},
            realized_pnl_usd=None, realized_pnl_pct=None,
            exit_price=None, exit_time=None,
        )
        fetch.return_value = [trade]
        client = MagicMock()
        client.futures_symbol_ticker.return_value = [
            {"symbol": "SOLUSDT", "price": "150"}]
        client.futures_get_order.return_value = {
            "status": "FILLED", "executedQty": "1.2", "cumQuote": "180",
            "avgPrice": "150", "updateTime": 1,
        }
        closed = {
            "tp_order_id": None, "tp_algo_id": None,
            "sl_order_id": None, "sl_algo_id": None,
            "success": False, "protection_state": "POSITION_CLOSED",
            "errors": [], "terminal_order_seen": False,
            "position_qty": 0,
        }
        with patch.object(FuturesOrderExecutor, "place_exit_orders",
                          return_value=closed), \
             patch("core.executors.futures_position_monitor._send_telegram"):
            FuturesPositionMonitor(client).check_positions()

        persisted = {}
        for call in update.call_args_list:
            persisted.update(call.args[1])
        self.assertEqual(persisted["exit_status"], "MANUALLY_CLOSED")
        self.assertFalse(persisted["exit_orders_placed"])
        self.assertIsNone(persisted["exit_price"])
        self.assertIsNone(persisted["exit_time"])
        self.assertIsNone(persisted["realized_pnl_usd"])
        self.assertIsNone(persisted["realized_pnl_pct"])


if __name__ == "__main__":
    unittest.main()
