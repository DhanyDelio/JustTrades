import io
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import core.paper_trade_executor as pte
from services import position_listener


class CheckPositionsGuardTests(unittest.TestCase):
    def setUp(self):
        self.append_log_patcher = patch.object(position_listener, "append_log")
        self.mock_append_log = self.append_log_patcher.start()

    def tearDown(self):
        self.append_log_patcher.stop()

    def test_resolved_trade_does_not_show_live_card(self):
        trade = {
            "symbol": "BTCUSDT",
            "entry_status": "FILLED",
            "exit_status": "TP_HIT",
            "entry_qty": 1,
            "entry_price": 100.0,
        }

        self.assertFalse(pte.repo.should_show_live_position(trade, "FILLED"))

    def test_verbose_pending_card_uses_dynamic_precision_for_low_price_assets(self):
        class FakeClient:
            def get_order(self, symbol, orderId):
                return {
                    "status": "NEW",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0",
                    "price": "0.12345",
                    "updateTime": 1,
                }

            def get_all_tickers(self):
                return [{"symbol": "BTCUSDT", "price": "0.12345"}]

            def get_symbol_ticker(self, symbol):
                return {"price": "0.12345"}

            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "ALL_DONE", "orders": []}

        trade = {
            "symbol": "PEPEUSDT",
            "direction": "long",
            "entry_order_id": 1,
            "entry_status": "NEW",
            "entry_price": 0.12345,
            "entry_qty": 1,
            "exit_status": "OPEN",
            "oco_placed": False,
            "sl": 0.12,
            "tp1": 0.13,
            "entry_notional": 100.0,
        }

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }):
            buf = io.StringIO()
            with redirect_stdout(buf):
                from core.executors.spot_position_monitor import SpotPositionMonitor
                from core.executors.spot_order_executor import SpotOrderExecutor
                client_mock = FakeClient()
                monitor = SpotPositionMonitor(client_mock, pte.repo, SpotOrderExecutor(client_mock))
                monitor.check_positions(verbose=True)

        out = buf.getvalue()
        self.assertIn("Current:      0.12345", out)
        self.assertIn("Entry:      0.12345", out)
        self.assertNotIn("Current:      0.00", out)
        self.assertNotIn("Entry:      0.00", out)

    def test_verbose_check_positions_skips_compact_summary_line(self):
        class FakeClient:
            def get_order(self, symbol, orderId):
                return {
                    "status": "FILLED",
                    "executedQty": "1",
                    "cummulativeQuoteQty": "100",
                    "price": "100",
                    "updateTime": 1,
                }

            def get_all_tickers(self):
                return [{"symbol": "BTCUSDT", "price": "105"}]

            def get_symbol_ticker(self, symbol):
                return {"price": "105"}

            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "ALL_DONE", "orders": []}

        trade = {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_order_id": 1,
            "entry_status": "NEW",
            "entry_price": 100.0,
            "entry_qty": 1,
            "exit_status": "OPEN",
            "oco_placed": True,
            "oco_list_id": 777,
            "sl": 95.0,
            "tp1": 110.0,
            "entry_notional": 100.0,
            "entry_fill_price": 100.0,
            "entry_fill_time": 1,
        }

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }):
            buf = io.StringIO()
            with redirect_stdout(buf):
                from core.executors.spot_position_monitor import SpotPositionMonitor
                from core.executors.spot_order_executor import SpotOrderExecutor
                client_mock = FakeClient()
                monitor = SpotPositionMonitor(client_mock, pte.repo, SpotOrderExecutor(client_mock))
                monitor.check_positions(verbose=True)

        out = buf.getvalue()
        self.assertIn("╔", out)
        self.assertIn("BTCUSDT", out)
        self.assertNotIn("  BTCUSDT    ✅ FILLED", out)

    def test_lab_batch_mode_auto_confirms_without_prompt(self):
        self.assertTrue(pte.should_auto_confirm_batch(is_lab_batch=True))
        self.assertFalse(pte.should_auto_confirm_batch(is_lab_batch=False))

    def test_execution_report_updates_trade_and_triggers_lab_batch(self):
        """Resolve detection (TP/SL) is intentionally disabled in the
        WebSocket listener — Binance testnet does not reliably send
        executionReport for OCO completion.  Resolve is handled by
        --check-positions instead.

        This test verifies the listener correctly returns a no-op for
        events that look like OCO fills.
        """
        trade = {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_order_id": 1,
            "entry_status": "FILLED",
            "entry_price": 100.0,
            "entry_fill_price": 100.0,
            "entry_qty": 1,
            "entry_notional": 100.0,
            "exit_status": "OPEN",
            "oco_placed": True,
            "oco_list_id": 777,
            "correlation_cluster_id": "20260707_000000",
            "sl": 95.0,
            "tp1": 110.0,
        }
        event = {
            "e": "executionReport",
            "s": "BTCUSDT",
            "S": "SELL",
            "o": "LIMIT_MAKER",
            "X": "TRADE",
            "x": "TRADE",
            "p": "110",
            "q": "1",
            "L": "110",
            "l": "1",
            "orderListId": 777,
        }

        with patch.object(position_listener, "load_trade_log", return_value=[trade]), \
             patch.object(position_listener, "save_trade_log") as save_trade_log, \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }):
            result = position_listener.handle_execution_report(
                event,
                batch_runner=lambda *a, **kw: None,
                notifier=lambda *a, **kw: None,
                cooldown_minutes=60,
                now_dt=position_listener.datetime(2026, 7, 7, 0, 0, 0, tzinfo=position_listener.timezone.utc),
            )

        # Resolve path is disabled → no-op return
        self.assertFalse(result["updated"])
        self.assertFalse(result["triggered"])

    def test_entry_placed_and_filled_notifications_are_filtered_and_formatted(self):
        trade = {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_order_id": 1,
            "entry_status": "NEW",
            "entry_price": 100.0,
            "entry_qty": 1,
            "exit_status": "OPEN",
            "oco_placed": True,
            "oco_list_id": 777,
            "sl": 95.0,
            "tp1": 110.0,
            "entry_notional": 100.0,
        }

        messages = []
        with patch.object(position_listener, "load_trade_log", return_value=[trade]), \
             patch.object(position_listener, "save_trade_log"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }), \
             patch.object(position_listener, "send_telegram_notification", side_effect=lambda msg: messages.append(msg)):
            event_new = {
                "e": "executionReport",
                "s": "BTCUSDT",
                "S": "BUY",
                "o": "LIMIT",
                "X": "NEW",
                "x": "NEW",
                "i": 1,
                "p": "100",
                "q": "1",
                "L": "100",
                "l": "1",
                "orderListId": 777,
            }
            position_listener.handle_execution_report(event_new)
            time.sleep(0.5)
            self.assertEqual(len(messages), 1)
            self.assertIn("📥 Order placed", messages[0])
            self.assertIn("BTCUSDT", messages[0])
            self.assertIn("long", messages[0])

            messages.clear()
            trade2 = {
                "symbol": "BTCUSDT",
                "direction": "long",
                "entry_order_id": 2,
                "entry_status": "NEW",
                "entry_price": 100.0,
                "entry_qty": 1,
                "exit_status": "OPEN",
                "oco_placed": True,
                "oco_list_id": 778,
                "sl": 95.0,
                "tp1": 110.0,
                "entry_notional": 100.0,
            }
            with patch.object(position_listener, "load_trade_log", return_value=[trade2]), \
                 patch.object(position_listener, "save_trade_log"), \
                 patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                     "lab_capital": 100.0,
                     "closed_cluster_pnl": 0.0,
                     "deployed_capital": 0.0,
                     "available_capital": 100.0,
                     "max_new_positions": 1,
                     "deployed_count": 0,
                 }), \
                 patch.object(position_listener, "send_telegram_notification", side_effect=lambda msg: messages.append(msg)):
                event_new2 = {
                    "e": "executionReport",
                    "s": "BTCUSDT",
                    "S": "BUY",
                    "o": "LIMIT",
                    "X": "NEW",
                    "x": "NEW",
                    "i": 2,
                    "p": "100",
                    "q": "1",
                    "L": "100",
                    "l": "1",
                    "orderListId": 778,
                }
                event_fill = {
                    "e": "executionReport",
                    "s": "BTCUSDT",
                    "S": "BUY",
                    "o": "LIMIT",
                    "X": "FILLED",
                    "x": "FILLED",
                    "i": 2,
                    "p": "100",
                    "q": "1",
                    "L": "100",
                    "l": "1",
                    "orderListId": 778,
                }
                position_listener.handle_execution_report(event_new2)
                position_listener.handle_execution_report(event_fill)
                time.sleep(0.5)
                self.assertEqual(len(messages), 1)
                self.assertIn("✅ Filled", messages[0])
                self.assertIn("BTCUSDT", messages[0])

    def test_entry_placed_notification_includes_detailed_card_fields(self):
        trade = {
            "symbol": "PEPEUSDT",
            "direction": "long",
            "entry_order_id": 99,
            "entry_status": "NEW",
            "entry_price": 0.00012345,
            "entry_qty": 1000000,
            "exit_status": "OPEN",
            "oco_placed": False,
            "sl": 0.00012,
            "tp1": 0.00013,
            "planned_rr": 1.2,
            "zone_touches": 3,
            "entry_notional": 123.45,
        }
        messages = []
        with patch.object(position_listener, "load_trade_log", return_value=[trade]), \
             patch.object(position_listener, "save_trade_log"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }), \
             patch.object(position_listener, "send_telegram_notification", side_effect=lambda msg: messages.append(msg)):
            event = {
                "e": "executionReport",
                "s": "PEPEUSDT",
                "S": "BUY",
                "o": "LIMIT",
                "X": "NEW",
                "x": "NEW",
                "i": 99,
                "p": "0.00012345",
                "q": "1000000",
                "L": "0.00012345",
                "l": "1000000",
            }
            position_listener.handle_execution_report(event)
            time.sleep(0.5)
            self.assertEqual(len(messages), 1)
            text = messages[0]
            self.assertIn("📥 Order placed", text)
            self.assertIn("Entry:", text)
            self.assertIn("zone:", text)
            self.assertIn("SL:", text)
            self.assertIn("TP:", text)
            self.assertIn("R:R:", text)
            self.assertIn("Qty:", text)

    def test_entry_placed_notification_uses_logged_qty_not_event_payload(self):
        trade = {
            "symbol": "PEPEUSDT",
            "direction": "long",
            "entry_order_id": 99,
            "entry_status": "NEW",
            "entry_price": 0.00000264,
            "entry_qty": 4545454.0,
            "entry_notional": 11.99999856,
            "exit_status": "OPEN",
            "oco_placed": False,
            "sl": 0.00000258,
            "tp1": 0.00000292,
            "planned_rr": 1.66,
            "zone_touches": 1,
        }
        messages = []
        with patch.object(position_listener, "load_trade_log", return_value=[trade]), \
             patch.object(position_listener, "save_trade_log"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }), \
             patch.object(position_listener, "send_telegram_notification", side_effect=lambda msg: messages.append(msg)):
            event = {
                "e": "executionReport",
                "s": "PEPEUSDT",
                "S": "BUY",
                "o": "LIMIT",
                "X": "NEW",
                "x": "NEW",
                "i": 99,
                "p": "0.00000264",
                "q": "1000000",
                "L": "0.00000264",
                "l": "1000000",
            }
            position_listener.handle_execution_report(event)
            time.sleep(0.5)
            self.assertEqual(len(messages), 1)
            text = messages[0]
            self.assertIn("4545454.0", text)
            self.assertIn("$12.00", text)
            self.assertNotIn("1,000,000", text)

    def test_resolved_notifications_disabled_for_websocket(self):
        """Resolve detection (TP/SL) is intentionally disabled in the
        WebSocket listener.  OCO fill events should NOT trigger
        send_telegram_notification — resolve is handled by
        --check-positions instead.
        """
        trade = {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_order_id": 1,
            "entry_status": "FILLED",
            "entry_price": 100.0,
            "entry_fill_price": 100.0,
            "entry_qty": 1,
            "exit_status": "OPEN",
            "oco_placed": True,
            "oco_list_id": 777,
            "sl": 95.0,
            "tp1": 110.0,
            "entry_notional": 100.0,
        }
        messages = []
        with patch.object(position_listener, "load_trade_log", return_value=[trade]), \
             patch.object(position_listener, "save_trade_log"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }), \
             patch.object(position_listener, "send_telegram_notification", side_effect=lambda msg: messages.append(msg)):
            tp_event = {
                "e": "executionReport",
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "MARKET",
                "X": "TRADE",
                "x": "TRADE",
                "L": "110",
                "l": "1",
                "orderListId": 777,
            }
            position_listener.handle_execution_report(tp_event)
            # Resolve path disabled → no notification sent
            self.assertEqual(len(messages), 0)

    def test_default_check_positions_keeps_compact_summary_line(self):
        class FakeClient:
            def get_order(self, symbol, orderId):
                return {
                    "status": "FILLED",
                    "executedQty": "1",
                    "cummulativeQuoteQty": "100",
                    "price": "100",
                    "updateTime": 1,
                }

            def get_all_tickers(self):
                return [{"symbol": "BTCUSDT", "price": "105"}]

            def get_symbol_ticker(self, symbol):
                return {"price": "105"}

            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "ALL_DONE", "orders": []}

        trade = {
            "symbol": "BTCUSDT",
            "direction": "long",
            "entry_order_id": 1,
            "entry_status": "NEW",
            "entry_price": 100.0,
            "entry_qty": 1,
            "exit_status": "OPEN",
            "oco_placed": True,
            "oco_list_id": 777,
            "sl": 95.0,
            "tp1": 110.0,
            "entry_notional": 100.0,
            "entry_fill_price": 100.0,
            "entry_fill_time": 1,
        }

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0,
                 "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0,
                 "available_capital": 100.0,
                 "max_new_positions": 1,
                 "deployed_count": 0,
             }):
            buf = io.StringIO()
            with redirect_stdout(buf):
                from core.executors.spot_position_monitor import SpotPositionMonitor
                from core.executors.spot_order_executor import SpotOrderExecutor
                client_mock = FakeClient()
                monitor = SpotPositionMonitor(client_mock, pte.repo, SpotOrderExecutor(client_mock))
                monitor.check_positions(verbose=False)

        out = buf.getvalue()
        self.assertIn("Symbol", out)
        self.assertIn("PnL/Info", out)
        self.assertNotIn("╔", out)

    # ── Regression: entry order purged (-2013) must not block OCO reconciliation ──

    def test_entry_order_purged_filled_position_reaches_oco_reconciliation(self):
        """
        CASE_1: local entry_status=FILLED, entry_order=-2013, OCO=-2018, asset held.
        Entry-order error must NOT abort. OCO reconciliation must run.
        exit_status=OPEN, realized_pnl_usd=None.
        """
        import io
        from binance.exceptions import BinanceAPIException

        class FakeClient:
            def get_order(self, symbol, orderId):
                raise BinanceAPIException(
                    None, -2013, '{"code":-2013,"msg":"Order does not exist."}'
                )
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def v3_get_order_list(self, orderListId):
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}'
                )

        trade = {
            "symbol": "XLMUSDT", "direction": "long",
            "entry_order_id": 943778, "entry_status": "FILLED",
            "entry_price": 0.1691, "entry_fill_price": 0.1691,
            "entry_fill_time": 1000000, "entry_qty": 70.0,
            "entry_notional": 11.837, "exit_status": "OPEN",
            "oco_placed": True, "oco_list_id": 645378,
            "oco_order_ids": [951495, 951496],
            "sl": 0.1657, "tp1": 0.1810, "realized_pnl_usd": None,
        }

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id"), \
             patch("core.paper_trade_executor._send_telegram"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0, "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0, "available_capital": 100.0,
                 "max_new_positions": 1, "deployed_count": 0,
             }):
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                from core.executors.spot_position_monitor import SpotPositionMonitor
                from core.executors.spot_order_executor import SpotOrderExecutor
                c = FakeClient()
                SpotPositionMonitor(c, pte.repo, SpotOrderExecutor(c)).check_positions()

        out2 = buf2.getvalue()
        self.assertNotIn("Could not query entry order", out2,
                         "Entry -2013 must not abort position processing")
        recon = trade.get("oco_reconciliation_status", "")
        self.assertIn(recon, ("UNPROTECTED", "UNPROTECTED_SL_BREACH"),
                      f"Expected UNPROTECTED*, got {recon!r}")
        self.assertEqual(trade.get("exit_status"), "OPEN")
        self.assertIsNone(trade.get("realized_pnl_usd"))

    def test_entry_order_purged_pending_position_sets_reconciliation_required(self):
        """
        CASE_2: local entry_status=NEW (pending), entry_order=-2013.
        Must NOT assume FILLED. Must set RECONCILIATION_REQUIRED.
        """
        import io
        from binance.exceptions import BinanceAPIException

        class FakeClient:
            def get_order(self, symbol, orderId):
                raise BinanceAPIException(
                    None, -2013, '{"code":-2013,"msg":"Order does not exist."}'
                )
            def get_all_tickers(self):
                return [{"symbol": "BTCUSDT", "price": "100"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "100"}

        trade = {
            "symbol": "BTCUSDT", "direction": "long",
            "entry_order_id": 99999, "entry_status": "NEW",
            "entry_price": 100.0, "entry_qty": None,
            "entry_fill_price": None, "exit_status": "OPEN",
            "oco_placed": False, "sl": 95.0, "tp1": 110.0,
            "entry_notional": 100.0, "realized_pnl_usd": None,
        }

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id"), \
             patch("core.paper_trade_executor._send_telegram"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool", return_value={
                 "lab_capital": 100.0, "closed_cluster_pnl": 0.0,
                 "deployed_capital": 0.0, "available_capital": 100.0,
                 "max_new_positions": 1, "deployed_count": 0,
             }):
            buf3 = io.StringIO()
            with redirect_stdout(buf3):
                from core.executors.spot_position_monitor import SpotPositionMonitor
                from core.executors.spot_order_executor import SpotOrderExecutor
                c = FakeClient()
                SpotPositionMonitor(c, pte.repo, SpotOrderExecutor(c)).check_positions()

        self.assertNotEqual(trade.get("entry_status"), "FILLED")
        self.assertEqual(trade.get("oco_reconciliation_status"), "RECONCILIATION_REQUIRED")
        self.assertEqual(trade.get("exit_status"), "OPEN")
        self.assertIsNone(trade.get("realized_pnl_usd"))



    def test_oco_reconciliation_no_duplicate_telegram_on_second_invocation(self):
        """A second process load deduplicates from the first persisted state."""
        from copy import deepcopy
        from unittest.mock import MagicMock
        from binance.exceptions import BinanceAPIException

        class _FakeXLM:
            def get_order(self, symbol, orderId):
                raise BinanceAPIException(
                    None, -2013, '{"code":-2013,"msg":"Order does not exist."}')
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def v3_get_order_list(self, orderListId):
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')

        base = {
            "symbol": "XLMUSDT", "direction": "long",
            "entry_order_id": 943778, "entry_status": "FILLED",
            "entry_price": 0.1691, "entry_fill_price": 0.1691,
            "entry_fill_time": 1000000, "entry_qty": 70.0,
            "entry_notional": 11.837, "exit_status": "OPEN",
            "oco_placed": True, "oco_list_id": 645378,
            "sl": 0.1657, "tp1": 0.1810, "realized_pnl_usd": None,
        }
        pool = {
            "lab_capital": 100.0, "closed_cluster_pnl": 0.0,
            "deployed_capital": 0.0, "available_capital": 100.0,
            "max_new_positions": 1, "deployed_count": 0,
        }
        persisted = {**base, "oco_reconciliation_status": None}
        loaded = []

        def load_trade_log():
            row = deepcopy(persisted)
            loaded.append(row)
            return [row]

        def update_spot_by_order_id(entry_order_id, fields):
            self.assertEqual(entry_order_id, base["entry_order_id"])
            persisted.update(deepcopy(fields))

        telegram = MagicMock()

        # ── Invocation 1: DB state = None ────────────────────────────────
        with patch.object(pte.repo, "load_trade_log", side_effect=load_trade_log), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id",
                   side_effect=update_spot_by_order_id) as update_spot, \
             patch("core.executors.spot_position_monitor._send_telegram", telegram), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
                   return_value=pool):
            from core.executors.spot_position_monitor import SpotPositionMonitor
            from core.executors.spot_order_executor import SpotOrderExecutor
            client1 = _FakeXLM()
            SpotPositionMonitor(
                client1, pte.repo, SpotOrderExecutor(client1)
            ).check_positions()

            self.assertEqual(telegram.call_count, 1)
            self.assertEqual(
                persisted["oco_reconciliation_status"],
                "UNPROTECTED_SL_BREACH",
            )
            self.assertTrue(any(
                call.args[1] == {
                    "oco_reconciliation_status": "UNPROTECTED_SL_BREACH"
                }
                for call in update_spot.call_args_list
            ))

            telegram.reset_mock()
            update_spot.reset_mock()
            client2 = _FakeXLM()
            SpotPositionMonitor(
                client2, pte.repo, SpotOrderExecutor(client2)
            ).check_positions()

        self.assertEqual(len(loaded), 2)
        self.assertIsNot(loaded[0], loaded[1])
        self.assertEqual(
            loaded[1]["oco_reconciliation_status"],
            "UNPROTECTED_SL_BREACH",
        )
        self.assertEqual(loaded[1]["exit_status"], "OPEN")
        self.assertIsNone(loaded[1]["realized_pnl_usd"])
        self.assertEqual(telegram.call_count, 0)
        self.assertEqual(update_spot.call_count, 0)

if __name__ == "__main__":
    unittest.main()
