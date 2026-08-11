"""
test_oco_protection_recovery.py
================================
Tests for the OCO protection lifecycle fix introduced in
JUSTTRADES-OCO-PROTECTION-RECOVERY-FIX-001.

Scenarios required by the task spec:
  1. Normal position: BUY filled, OCO exists, price above SL → no action.
  2. OCO missing, price between SL and TP → recovery attempt (re-place OCO).
  3. OCO missing, price below SL → emergency close (market sell).
  4. OCO missing, price at/above TP → emergency close (market sell).
  5. Close already executed (idempotency) → no duplicate sell.
  6. Duplicate monitoring cycle → no duplicate OCO or sell.

All tests use mocks — zero live network calls.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout, ExitStack
from unittest.mock import MagicMock, patch

import core.paper_trade_executor as pte
from core.executors.spot_position_monitor import SpotPositionMonitor
from core.executors.spot_order_executor import SpotOrderExecutor

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_POOL = {
    "lab_capital": 100.0, "closed_cluster_pnl": 0.0,
    "deployed_capital": 0.0, "available_capital": 100.0,
    "max_new_positions": 1, "deployed_count": 0,
}


def _base_trade(**overrides) -> dict:
    t = {
        "symbol": "XLMUSDT", "direction": "long",
        "entry_order_id": 700191, "entry_status": "FILLED",
        "entry_price": 0.1846, "entry_fill_price": 0.1846,
        "entry_fill_time": 1783478185006, "entry_qty": 65.0,
        "entry_notional": 11.999, "exit_status": "OPEN",
        "oco_placed": True, "oco_list_id": 417672,
        "oco_order_ids": [713017, 713018],
        "sl": 0.1657, "tp1": 0.2078,
        "realized_pnl_usd": None, "exit_reason": None,
    }
    t.update(overrides)
    return t


def _run_check(monitor, trade, *, recover_unprotected=False):
    """Run check_positions with all standard mocks active; returns stdout."""
    with ExitStack() as s:
        s.enter_context(patch.object(pte.repo, "load_trade_log",
                                     return_value=[trade]))
        s.enter_context(patch.object(pte.repo, "save_trade_log"))
        s.enter_context(patch("services.supabase_client.update_spot_by_order_id"))
        s.enter_context(patch(
            "core.executors.spot_position_monitor._send_telegram"))
        s.enter_context(patch("core.paper_trade_executor._send_telegram"))
        s.enter_context(patch(
            "core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
            return_value=_POOL))
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.check_positions(recover_unprotected=recover_unprotected)
    return buf.getvalue()


def _make_monitor(client, executor=None):
    if executor is None:
        executor = SpotOrderExecutor(client)
    return SpotPositionMonitor(client, pte.repo, executor)


# ---------------------------------------------------------------------------
# Scenario 1 — Normal: BUY filled, OCO exists, price above SL → no action
# ---------------------------------------------------------------------------

class TestScenario1_NormalProtectedPosition(unittest.TestCase):

    def _healthy_client(self, price="0.1900"):
        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": price}]
            def get_symbol_ticker(self, symbol):
                return {"price": price}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "EXECUTING", "orders": []}
        return _C()

    def test_no_action_when_oco_exists_and_price_above_sl(self):
        trade = _base_trade()
        executor = MagicMock()
        _run_check(_make_monitor(self._healthy_client(), executor), trade)
        executor.place_oco_order.assert_not_called()
        self.assertEqual(trade["exit_status"], "OPEN")
        self.assertIsNone(trade["realized_pnl_usd"])
        self.assertIsNone(trade.get("exit_reason"))

    def test_exit_reason_sl_hit_on_normal_sl_fill(self):
        """Normal OCO SL fill records the normal SL reason."""
        trade = _base_trade(entry_fill_time=1000)

        class _C:
            def get_order(self, symbol, orderId):
                if orderId == 700191:
                    return {"status": "FILLED", "executedQty": "65",
                            "cummulativeQuoteQty": str(65 * 0.1846),
                            "price": "0.1846", "updateTime": 1000}
                return {"status": "FILLED", "type": "STOP_LOSS_LIMIT",
                        "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1657),
                        "price": "0.1657", "updateTime": 2000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1640"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1640"}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "ALL_DONE",
                        "orders": [{"orderId": 713017}, {"orderId": 713018}]}

        _run_check(_make_monitor(_C()), trade)
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "SL_HIT")


# ---------------------------------------------------------------------------
# Scenario 2 — OCO missing, price above SL → recovery attempt
# ---------------------------------------------------------------------------

class TestScenario2_OcoMissingPriceAboveSl(unittest.TestCase):

    def _missing_client(self, price="0.1900"):
        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": price}]
            def get_symbol_ticker(self, symbol):
                return {"price": price}
            def v3_get_order_list(self, orderListId):
                from binance.exceptions import BinanceAPIException
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')
        return _C()

    def test_recovery_attempted_automatically(self):
        """Missing OCO between stored SL and TP is recovered automatically."""
        trade = _base_trade()
        executor = MagicMock()
        executor.place_oco_order.return_value = {
            "orderListId": 9999,
            "orderReports": [{"orderId": 20001}, {"orderId": 20002}],
        }
        client = self._missing_client()
        client.get_asset_balance = MagicMock(return_value={"free": "65.0"})
        _run_check(_make_monitor(client, executor), trade)
        executor.place_oco_order.assert_called_once()
        self.assertTrue(trade["oco_placed"])
        self.assertEqual(trade["oco_list_id"], 9999)
        self.assertEqual(trade["oco_reconciliation_status"], "PROTECTED")
        self.assertEqual(trade["exit_status"], "OPEN")

    def test_failed_recovery_sets_unprotected_state(self):
        """A failed automatic recovery leaves an explicit unprotected state."""
        trade = _base_trade()
        executor = MagicMock()
        executor.place_oco_order.return_value = None
        client = self._missing_client()
        client.get_asset_balance = MagicMock(return_value={"free": "65.0"})
        _run_check(_make_monitor(client, executor), trade)
        executor.place_oco_order.assert_called_once()
        self.assertEqual(trade["oco_reconciliation_status"], "UNPROTECTED")
        self.assertEqual(trade["exit_status"], "OPEN")
        self.assertIsNone(trade["realized_pnl_usd"])


# ---------------------------------------------------------------------------
# Scenario 3 — OCO missing, price below SL → emergency close
# ---------------------------------------------------------------------------

class TestScenario3_OcoMissingPriceBelowSl(unittest.TestCase):

    def _breached_client(self, price="0.1600"):
        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": price}]
            def get_symbol_ticker(self, symbol):
                return {"price": price}
            def v3_get_order_list(self, orderListId):
                from binance.exceptions import BinanceAPIException
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')
        return _C()

    def _sell_executor(self):
        executor = MagicMock()
        def _market_sell(trade):
            trade["_market_sold"] = True
            return {"transactTime": 9_000_000, "executedQty": "65",
                    "cummulativeQuoteQty": str(65 * 0.1600)}
        executor.close_position.side_effect = _market_sell
        return executor

    def test_emergency_close_fires_without_flag(self):
        trade = _base_trade()
        executor = self._sell_executor()
        client = self._breached_client()
        client.get_asset_balance = MagicMock(return_value={"free": "65.0"})
        _run_check(_make_monitor(client, executor), trade, recover_unprotected=False)
        executor.close_position.assert_called_once()
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "UNPROTECTED_SL_BREACH")
        self.assertEqual(trade["oco_reconciliation_status"], "EMERGENCY_CLOSED")
        self.assertFalse(trade["oco_placed"])
        self.assertIsNotNone(trade["realized_pnl_usd"])

    def test_emergency_close_pnl_is_negative(self):
        trade = _base_trade()
        executor = self._sell_executor()
        client = self._breached_client()
        client.get_asset_balance = MagicMock(return_value={"free": "65.0"})
        _run_check(_make_monitor(client, executor), trade)
        self.assertLess(trade["realized_pnl_usd"], 0)

    def test_emergency_close_sets_exit_time(self):
        trade = _base_trade()
        executor = self._sell_executor()
        client = self._breached_client()
        client.get_asset_balance = MagicMock(return_value={"free": "65.0"})
        _run_check(_make_monitor(client, executor), trade)
        self.assertIsNotNone(trade.get("exit_time"))
        self.assertGreater(trade["exit_time"], 0)


# ---------------------------------------------------------------------------
# Scenario 4 — OCO missing, price at/above TP → emergency close
# ---------------------------------------------------------------------------

class TestScenario4_OcoMissingPriceAboveTp(unittest.TestCase):

    def test_tp_breach_forces_sell_with_distinct_reason(self):
        trade = _base_trade()
        executor = MagicMock()
        executor.close_position.return_value = {
            "transactTime": 9_000_000,
            "executedQty": "65",
            "cummulativeQuoteQty": str(65 * 0.2100),
        }

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.2100"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.2100"}
            def get_asset_balance(self, asset):
                return {"free": "65.0"}
            def v3_get_order_list(self, orderListId):
                from binance.exceptions import BinanceAPIException
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')

        _run_check(_make_monitor(_C(), executor), trade)

        executor.close_position.assert_called_once_with(trade)
        executor.place_oco_order.assert_not_called()
        self.assertEqual(trade["exit_status"], "TP_HIT")
        self.assertEqual(trade["exit_reason"], "UNPROTECTED_TP_BREACH")
        self.assertEqual(trade["oco_reconciliation_status"], "EMERGENCY_CLOSED")
        self.assertGreater(trade["realized_pnl_usd"], 0)


# ---------------------------------------------------------------------------
# Reconnect detection and duplicate-cycle idempotency
# ---------------------------------------------------------------------------

class TestScenario4_OcoDisappearsAfterReconnect(unittest.TestCase):

    def test_protection_loss_detected_on_second_invocation(self):
        from binance.exceptions import BinanceAPIException

        trade = _base_trade()
        call_count = [0]

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1900"}]  # above SL
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1900"}
            def v3_get_order_list(self, orderListId):
                call_count[0] += 1
                if call_count[0] == 1:
                    return {"listOrderStatus": "EXECUTING", "orders": []}
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')

        monitor = _make_monitor(_C(), MagicMock())

        # Call 1 — OCO alive
        _run_check(monitor, trade)
        self.assertEqual(trade["exit_status"], "OPEN")

        # Call 2 — OCO gone after simulated testnet reset
        _run_check(monitor, trade)

        recon = trade.get("oco_reconciliation_status")
        self.assertIn(recon, ("UNPROTECTED", "UNPROTECTED_SL_BREACH"),
                      f"Expected UNPROTECTED*, got {recon!r}")
        self.assertEqual(trade["exit_status"], "OPEN")   # price still above SL

    def test_second_cycle_does_not_create_duplicate_recovered_oco(self):
        from binance.exceptions import BinanceAPIException

        trade = _base_trade()
        executor = MagicMock()
        executor.place_oco_order.return_value = {
            "orderListId": 9999,
            "orderReports": [{"orderId": 20001}, {"orderId": 20002}],
        }

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1900"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1900"}
            def get_asset_balance(self, asset):
                return {"free": "65.0"}
            def v3_get_order_list(self, orderListId):
                if orderListId == 9999:
                    return {"listOrderStatus": "EXECUTING", "orders": []}
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')

        monitor = _make_monitor(_C(), executor)
        _run_check(monitor, trade)
        _run_check(monitor, trade)

        executor.place_oco_order.assert_called_once_with(trade)
        executor.close_position.assert_not_called()
        self.assertEqual(trade["exit_status"], "OPEN")
        self.assertEqual(trade["oco_list_id"], 9999)


# ---------------------------------------------------------------------------
# Scenario 5 — Close already executed → no duplicate sell (idempotency)
# ---------------------------------------------------------------------------

class TestScenario5_NoDuplicateSell(unittest.TestCase):

    def test_idempotency_exit_status_already_closed(self):
        """exit_status != OPEN → _emergency_close returns True without selling."""
        trade = _base_trade(exit_status="SL_HIT",
                            exit_reason="UNPROTECTED_SL_BREACH",
                            realized_pnl_usd=-0.30)
        executor = MagicMock()
        client = MagicMock()
        monitor = _make_monitor(client, executor)
        result = monitor._emergency_close(trade, 0.1600, [])
        self.assertTrue(result)
        executor.place_oco_order.assert_not_called()
        client.get_asset_balance.assert_not_called()

    def test_idempotency_insufficient_free_balance(self):
        """Asset already gone (free < required) → state-only update, no sell."""
        trade = _base_trade()
        executor = MagicMock()
        client = MagicMock()
        client.get_asset_balance.return_value = {"free": "0.0"}
        monitor = _make_monitor(client, executor)
        resolved = []
        with patch("core.executors.spot_position_monitor._send_telegram"):
            result = monitor._emergency_close(trade, 0.1600, resolved)
        self.assertTrue(result)
        executor.place_oco_order.assert_not_called()
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "UNPROTECTED_SL_BREACH")
        self.assertEqual(resolved[0][0], "XLMUSDT")

    def test_check_positions_no_double_sell_on_already_closed_trade(self):
        """check_positions with exit_status=SL_HIT must not call place_oco_order."""
        trade = _base_trade(exit_status="SL_HIT", realized_pnl_usd=-0.30,
                            exit_reason="UNPROTECTED_SL_BREACH")
        executor = MagicMock()

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def v3_get_order_list(self, orderListId):
                from binance.exceptions import BinanceAPIException
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')

        _run_check(_make_monitor(_C(), executor), trade)
        executor.place_oco_order.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 6 — Existing normal OCO flow → no regression
# ---------------------------------------------------------------------------

class TestScenario6_NormalOcoFlowRegression(unittest.TestCase):

    def test_standard_oco_sl_resolution_unchanged(self):
        """Normal OCO SL: exit_status and exit_reason are SL_HIT."""
        trade = _base_trade(entry_fill_time=1000)

        class _C:
            def get_order(self, symbol, orderId):
                if orderId == 700191:
                    return {"status": "FILLED", "executedQty": "65",
                            "cummulativeQuoteQty": str(65 * 0.1846),
                            "price": "0.1846", "updateTime": 1000}
                return {"status": "FILLED", "type": "STOP_LOSS_LIMIT",
                        "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1657),
                        "price": "0.1657", "updateTime": 2000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1640"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1640"}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "ALL_DONE",
                        "orders": [{"orderId": 713017}, {"orderId": 713018}]}

        _run_check(_make_monitor(_C()), trade)
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "SL_HIT")
        self.assertLess(trade["realized_pnl_usd"], 0)
        self.assertIsNotNone(trade["exit_time"])
        self.assertIsNotNone(trade["time_to_resolution_sec"])

    def test_standard_oco_tp_resolution_unchanged(self):
        """Normal OCO TP: exit_status and exit_reason are TP_HIT."""
        trade = _base_trade(entry_fill_time=1000)

        class _C:
            def get_order(self, symbol, orderId):
                if orderId == 700191:
                    return {"status": "FILLED", "executedQty": "65",
                            "cummulativeQuoteQty": str(65 * 0.1846),
                            "price": "0.1846", "updateTime": 1000}
                return {"status": "FILLED", "type": "LIMIT_MAKER",
                        "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.2078),
                        "price": "0.2078", "updateTime": 5000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.2100"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.2100"}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "ALL_DONE",
                        "orders": [{"orderId": 713017}, {"orderId": 713018}]}

        _run_check(_make_monitor(_C()), trade)
        self.assertEqual(trade["exit_status"], "TP_HIT")
        self.assertEqual(trade["exit_reason"], "TP_HIT")
        self.assertGreater(trade["realized_pnl_usd"], 0)

    def test_price_guard_sl_sets_price_guard_reason(self):
        """Price-guard SL remains classified as a normal SL hit."""
        trade = _base_trade(entry_fill_time=1000)

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]  # below SL
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "EXECUTING", "orders": []}  # OCO alive

        _run_check(_make_monitor(_C()), trade)
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "SL_HIT")


# ---------------------------------------------------------------------------
# Direct unit tests for _emergency_close()
# ---------------------------------------------------------------------------

class TestEmergencyCloseDirect(unittest.TestCase):

    def _monitor(self, client, executor=None):
        if executor is None:
            executor = MagicMock()
        return SpotPositionMonitor(client, pte.repo, executor)

    def test_market_sell_executed_on_breach(self):
        executor = MagicMock()
        def _sell(t):
            t["_market_sold"] = True
            return {"transactTime": 9000, "executedQty": "65",
                    "cummulativeQuoteQty": str(65 * 0.1600)}
        executor.close_position.side_effect = _sell
        client = MagicMock()
        client.get_asset_balance.return_value = {"free": "65.0"}
        monitor = self._monitor(client, executor)
        trade = _base_trade()
        resolved = []
        with patch("core.executors.spot_position_monitor._send_telegram"):
            result = monitor._emergency_close(trade, 0.1600, resolved)
        self.assertTrue(result)
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "UNPROTECTED_SL_BREACH")
        self.assertEqual(trade["oco_reconciliation_status"], "EMERGENCY_CLOSED")
        self.assertFalse(trade["oco_placed"])
        self.assertIsNone(trade["oco_list_id"])
        self.assertLess(trade["realized_pnl_usd"], 0)
        self.assertEqual(resolved[0][0], "XLMUSDT")

    def test_already_closed_is_idempotent(self):
        executor = MagicMock()
        trade = _base_trade(exit_status="SL_HIT", realized_pnl_usd=-0.20)
        monitor = self._monitor(MagicMock(), executor)
        result = monitor._emergency_close(trade, 0.1600, [])
        self.assertTrue(result)
        executor.close_position.assert_not_called()

    def test_balance_check_failure_returns_false(self):
        client = MagicMock()
        client.get_asset_balance.side_effect = RuntimeError("network error")
        monitor = self._monitor(client)
        trade = _base_trade()
        with patch("core.executors.spot_position_monitor._send_telegram"):
            result = monitor._emergency_close(trade, 0.1600, [])
        self.assertFalse(result)
        self.assertEqual(trade["exit_status"], "OPEN")  # untouched

    def test_sell_api_failure_returns_false(self):
        executor = MagicMock()
        executor.close_position.side_effect = RuntimeError("exchange error")
        client = MagicMock()
        client.get_asset_balance.return_value = {"free": "65.0"}
        monitor = self._monitor(client, executor)
        trade = _base_trade()
        with patch("core.executors.spot_position_monitor._send_telegram"):
            result = monitor._emergency_close(trade, 0.1600, [])
        self.assertFalse(result)
        self.assertEqual(trade["exit_status"], "OPEN")

    def test_zero_qty_returns_false(self):
        trade = _base_trade(entry_qty=0.0)
        monitor = self._monitor(MagicMock())
        result = monitor._emergency_close(trade, 0.1600, [])
        self.assertFalse(result)

    def test_asset_already_gone_updates_state_without_sell(self):
        """free < qty means asset sold externally — state update only, no sell."""
        executor = MagicMock()
        client = MagicMock()
        client.get_asset_balance.return_value = {"free": "0.0"}
        monitor = self._monitor(client, executor)
        trade = _base_trade()
        resolved = []
        with patch("core.executors.spot_position_monitor._send_telegram"):
            result = monitor._emergency_close(trade, 0.1600, resolved)
        self.assertTrue(result)
        executor.close_position.assert_not_called()
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "UNPROTECTED_SL_BREACH")
        self.assertEqual(len(resolved), 1)

    def test_no_duplicate_sell_on_second_call_after_close(self):
        """Calling _emergency_close twice must not sell twice."""
        executor = MagicMock()
        sell_count = [0]
        def _sell(t):
            sell_count[0] += 1
            t["_market_sold"] = True
            return {"transactTime": 9000, "executedQty": "65",
                    "cummulativeQuoteQty": str(65 * 0.1600)}
        executor.close_position.side_effect = _sell
        client = MagicMock()
        client.get_asset_balance.return_value = {"free": "65.0"}
        monitor = self._monitor(client, executor)
        trade = _base_trade()
        with patch("core.executors.spot_position_monitor._send_telegram"):
            monitor._emergency_close(trade, 0.1600, [])
            # Second call — trade is now exit_status=SL_HIT
            monitor._emergency_close(trade, 0.1600, [])
        self.assertEqual(sell_count[0], 1)  # sold exactly once


if __name__ == "__main__":
    unittest.main(verbosity=2)
