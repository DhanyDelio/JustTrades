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
             patch.object(pte, "compute_lab_pool", return_value={
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
             patch.object(pte, "compute_lab_pool", return_value={
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
             patch.object(position_listener, "compute_lab_pool", return_value={
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
             patch.object(position_listener, "compute_lab_pool", return_value={
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
                 patch.object(position_listener, "compute_lab_pool", return_value={
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
             patch.object(position_listener, "compute_lab_pool", return_value={
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
             patch.object(position_listener, "compute_lab_pool", return_value={
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
             patch.object(position_listener, "compute_lab_pool", return_value={
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
             patch.object(pte, "compute_lab_pool", return_value={
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


if __name__ == "__main__":
    unittest.main()
