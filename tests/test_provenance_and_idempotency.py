"""
test_provenance_and_idempotency.py
====================================
Tests for SUB-TASK 7 from the provenance-fix workflow:

Exit provenance tests (5):
  1. Price-guard path writes exit_reason='PRICE_GUARD_SL' / does NOT resolve as SL_HIT.
  2. Price-guard path sets stuck_oco_detection in raw_entry_order (exit_price_is_estimate gate).
  3. Genuine OCO SL fill (Step 3 ALL_DONE STOP leg FILLED) writes exit_reason='SL_HIT'.
  4. Genuine OCO TP fill (Step 3 ALL_DONE LIMIT_MAKER leg FILLED) writes exit_reason='TP_HIT'.
  5. UNPROTECTED_SL_BREACH emergency close writes exit_reason='UNPROTECTED_SL_BREACH'.

Multi-cycle idempotency tests (4):
  6. Scenario A: UNPROTECTED → cycle 1 creates TP. Cycle 2: exchange already has
     matching SELL LIMIT_MAKER → reconcile to TP_ONLY, NO new TP created. Total = 1.
  7. Scenario B: TP_ONLY with tp_order_id set → restart → existing TP OPEN → no duplicate.
  8. Scenario C: DB=UNPROTECTED, exchange already has SELL LIMIT_MAKER → TP_ONLY, no new order.
  9. Scenario D: cancel succeeds but re-query returns NEW status → abort, no replacement.

2Z shadow detection tests (2):
  10. Cycle N: sl_breached + OCO EXECUTING → suspected_at set, no cancel/sell/exit_status change.
  11. Cycle N+1: same + suspected_at from prior cycle → Telegram alert sent, no cancel/sell.

tp_order_id/sl_order_id persistence tests (2):
  12. TP_ONLY branch: tp_order_id written to Supabase structured column.
  13. SL_ONLY branch: sl_order_id written to Supabase structured column.

All tests use mocks — zero live network calls.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout, ExitStack
from unittest.mock import MagicMock, patch, call

import core.paper_trade_executor as pte
from core.executors.spot_position_monitor import SpotPositionMonitor
from core.executors.spot_order_executor import SpotOrderExecutor

# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_oco_protection_recovery.py)
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
        "tp_order_id": None, "sl_order_id": None,
        "oco_reconciliation_status": "FULLY_PROTECTED",
    }
    t.update(overrides)
    return t


def _avax_trade(**overrides) -> dict:
    """AVAX-style trade for UNPROTECTED/TP_ONLY idempotency tests."""
    t = {
        "symbol": "AVAXUSDT", "direction": "long",
        "entry_order_id": 1001, "entry_status": "FILLED",
        "entry_price": 7.836, "entry_fill_price": 7.836,
        "entry_fill_time": 1000000, "entry_qty": 1.53,
        "entry_notional": 11.99, "exit_status": "OPEN",
        "oco_placed": False, "oco_list_id": None,
        "oco_reconciliation_status": "UNPROTECTED",
        "tp_order_id": None, "sl_order_id": None,
        "sl": 7.58, "tp1": 11.799,
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


def _run_check_tracking_supabase(monitor, trade, *, recover_unprotected=False):
    """Run check_positions while capturing all update_spot_by_order_id calls."""
    supabase_calls = []

    def _capture_update(eid, fields):
        supabase_calls.append((eid, dict(fields)))

    with ExitStack() as s:
        s.enter_context(patch.object(pte.repo, "load_trade_log",
                                     return_value=[trade]))
        s.enter_context(patch.object(pte.repo, "save_trade_log"))
        s.enter_context(patch("services.supabase_client.update_spot_by_order_id",
                              side_effect=_capture_update))
        s.enter_context(patch(
            "core.executors.spot_position_monitor._send_telegram"))
        s.enter_context(patch("core.paper_trade_executor._send_telegram"))
        s.enter_context(patch(
            "core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
            return_value=_POOL))
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.check_positions(recover_unprotected=recover_unprotected)
    return supabase_calls


def _make_monitor(client, executor=None):
    if executor is None:
        executor = SpotOrderExecutor(client)
    return SpotPositionMonitor(client, pte.repo, executor)


# ---------------------------------------------------------------------------
# Test group 1: Exit provenance
# ---------------------------------------------------------------------------

class TestExitProvenance(unittest.TestCase):
    """Tests 1-5: correct exit_reason values for each code path."""

    # ── Test 1 & 2: price-guard path ────────────────────────────────────

    def test_1_price_guard_does_not_resolve_as_sl_hit(self):
        """
        Test 1: The price-guard branch (sl_breached + OCO EXECUTING confirmed)
        must NOT resolve the trade as SL_HIT. The new two-cycle stuck-OCO gate
        keeps the trade OPEN on cycle N, setting suspected_at only.
        """
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
                return {"listOrderStatus": "EXECUTING", "orders": []}

        # Clear module-level state before test
        if hasattr(SpotPositionMonitor, "_stuck_oco_marked_this_run"):
            SpotPositionMonitor._stuck_oco_marked_this_run = set()

        _run_check(_make_monitor(_C()), trade)
        # Must stay OPEN — price-guard no longer resolves immediately
        self.assertEqual(trade["exit_status"], "OPEN",
                         "Price-guard cycle N must NOT resolve trade as SL_HIT")
        self.assertIsNone(trade.get("exit_reason"),
                          "exit_reason must be None on price-guard cycle N")
        print("✓ Test 1: price-guard does not resolve as SL_HIT on cycle N")

    def test_2_price_guard_sets_stuck_oco_suspected_at(self):
        """
        Test 2: The price-guard path must set stuck_oco_detection.suspected_at
        inside raw_entry_order on cycle N (the 'estimate gate' marker).
        """
        trade = _base_trade(entry_fill_time=1000)

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "EXECUTING", "orders": []}

        if hasattr(SpotPositionMonitor, "_stuck_oco_marked_this_run"):
            SpotPositionMonitor._stuck_oco_marked_this_run = set()

        _run_check(_make_monitor(_C()), trade)
        raw = trade.get("raw_entry_order") or {}
        stuck = raw.get("stuck_oco_detection") or {}
        self.assertIn("suspected_at", stuck,
                      "suspected_at must be set in raw_entry_order.stuck_oco_detection")
        self.assertIsNotNone(stuck["suspected_at"])
        print(f"✓ Test 2: stuck_oco_detection.suspected_at = {stuck.get('suspected_at')}")

    # ── Test 3: genuine OCO SL fill ──────────────────────────────────────

    def test_3_genuine_oco_sl_fill_writes_sl_hit(self):
        """
        Test 3: Step 3 ALL_DONE, STOP_LOSS_LIMIT leg FILLED → exit_reason='SL_HIT'.
        This is the canonical genuine exchange fill path.
        """
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
        self.assertEqual(trade["exit_reason"], "SL_HIT",
                         "Genuine OCO STOP leg fill must write exit_reason='SL_HIT'")
        self.assertIsNotNone(trade.get("exit_price"))
        print(f"✓ Test 3: genuine SL fill → exit_reason='SL_HIT' exit_price={trade['exit_price']}")

    # ── Test 4: genuine OCO TP fill ──────────────────────────────────────

    def test_4_genuine_oco_tp_fill_writes_tp_hit(self):
        """
        Test 4: Step 3 ALL_DONE, LIMIT_MAKER leg FILLED → exit_reason='TP_HIT'.
        This is the canonical genuine exchange fill path.
        """
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
        self.assertEqual(trade["exit_reason"], "TP_HIT",
                         "Genuine OCO LIMIT_MAKER fill must write exit_reason='TP_HIT'")
        print(f"✓ Test 4: genuine TP fill → exit_reason='TP_HIT' exit_price={trade['exit_price']}")

    # ── Test 5: UNPROTECTED_SL_BREACH emergency close ────────────────────

    def test_5_unprotected_sl_breach_writes_correct_reason(self):
        """
        Test 5: Emergency close (_emergency_close) writes exit_reason='UNPROTECTED_SL_BREACH'.
        """
        from binance.exceptions import BinanceAPIException

        trade = _base_trade()
        executor = MagicMock()
        executor.close_position.return_value = {
            "transactTime": 9_000_000,
            "executedQty": "65",
            "cummulativeQuoteQty": str(65 * 0.1600),
            "fills": [{"price": "0.1600"}],
        }

        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def get_asset_balance(self, asset):
                return {"free": "65.0"}
            def v3_get_order_list(self, orderListId):
                raise BinanceAPIException(
                    None, -2018, '{"code":-2018,"msg":"Order list does not exist."}')

        _run_check(_make_monitor(_C(), executor), trade)
        self.assertEqual(trade["exit_status"], "SL_HIT")
        self.assertEqual(trade["exit_reason"], "UNPROTECTED_SL_BREACH",
                         "Emergency close must write exit_reason='UNPROTECTED_SL_BREACH'")
        print(f"✓ Test 5: emergency SL breach → exit_reason='UNPROTECTED_SL_BREACH'")


# ---------------------------------------------------------------------------
# Test group 2: Multi-cycle idempotency
# ---------------------------------------------------------------------------

class TestMultiCycleIdempotency(unittest.TestCase):
    """Tests 6-9: AVAX duplicate-TP prevention and TP_ONLY idempotency."""

    # ── Test 6: Scenario A ───────────────────────────────────────────────

    def test_6_scenario_a_unprotected_cycle2_exchange_has_tp_no_duplicate(self):
        """
        Test 6 (Scenario A):
        Cycle 1: UNPROTECTED → create TP.
        Cycle 2: same UNPROTECTED state (persist failed), exchange already has
                 matching SELL LIMIT_MAKER → reconcile to TP_ONLY, NO new TP.
        Total TP orders created = 1.
        """
        trade = _avax_trade()
        executor = MagicMock()

        # Cycle 1: place_oco_order creates a TP_ONLY order
        executor.place_oco_order.return_value = {
            "protection_state": "TP_ONLY",
            "oco_resp": None,
            "tp_order_id": 77001,
            "sl_order_id": None,
            "filter_reason": "SL_FILTER_INVALID",
        }

        class _ClientCycle1:
            def get_order(self, symbol, orderId):
                if orderId == 1001:
                    return {"status": "FILLED", "executedQty": "1.53",
                            "cummulativeQuoteQty": str(1.53 * 7.836),
                            "price": "7.836", "updateTime": 1}
                return {"status": "UNKNOWN"}
            def get_all_tickers(self):
                return [{"symbol": "AVAXUSDT", "price": "11.00"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "11.00"}
            def v3_get_order_list(self, orderListId):
                raise Exception("no OCO")
            def get_open_orders(self, symbol):
                return []  # cycle 1: no existing TP

        monitor = _make_monitor(_ClientCycle1(), executor)
        _run_check(monitor, trade)
        self.assertEqual(executor.place_oco_order.call_count, 1)

        # Simulate persist failure: DB still shows UNPROTECTED, tp_order_id=None
        trade["oco_reconciliation_status"] = "UNPROTECTED"
        trade["tp_order_id"] = None

        # Cycle 2: exchange already has a SELL LIMIT_MAKER from cycle 1
        class _ClientCycle2:
            def get_order(self, symbol, orderId):
                if orderId == 1001:
                    return {"status": "FILLED", "executedQty": "1.53",
                            "cummulativeQuoteQty": str(1.53 * 7.836),
                            "price": "7.836", "updateTime": 1}
                return {"status": "UNKNOWN"}
            def get_all_tickers(self):
                return [{"symbol": "AVAXUSDT", "price": "11.00"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "11.00"}
            def v3_get_order_list(self, orderListId):
                raise Exception("no OCO")
            def get_open_orders(self, symbol):
                # Exchange already has the TP from cycle 1
                return [{"orderId": 77001, "side": "SELL", "type": "LIMIT_MAKER",
                         "origQty": "1.53", "price": "11.799", "symbol": "AVAXUSDT"}]

        monitor2 = _make_monitor(_ClientCycle2(), executor)
        _run_check(monitor2, trade)

        # place_oco_order must NOT be called again
        self.assertEqual(executor.place_oco_order.call_count, 1,
                         "Cycle 2 must NOT create another TP when exchange already has one")
        self.assertEqual(trade["oco_reconciliation_status"], "TP_ONLY")
        self.assertEqual(trade["tp_order_id"], 77001)
        print("✓ Test 6: Scenario A — only 1 TP created, cycle 2 reconciles to TP_ONLY")

    # ── Test 7: Scenario B ───────────────────────────────────────────────

    def test_7_scenario_b_tp_only_with_id_restart_finds_open_no_duplicate(self):
        """
        Test 7 (Scenario B): TP_ONLY with tp_order_id set → process restart →
        reconciliation finds existing TP OPEN → no duplicate created.
        """
        # Trade with tp_order_id already set (from prior cycle's persist)
        trade = _avax_trade(
            oco_reconciliation_status="TP_ONLY",
            tp_order_id=55001,
        )
        executor = MagicMock()
        # place_oco_order returns TP_ONLY again (SL still filter-invalid)
        executor.place_oco_order.return_value = {
            "protection_state": "TP_ONLY",
            "oco_resp": None,
            "tp_order_id": 55099,
            "sl_order_id": None,
            "filter_reason": "SL_FILTER_INVALID",
        }

        class _C:
            def get_order(self, symbol, orderId):
                if orderId == 1001:
                    return {"status": "FILLED", "executedQty": "1.53",
                            "cummulativeQuoteQty": str(1.53 * 7.836),
                            "price": "7.836", "updateTime": 1}
                if orderId == 55001:  # existing TP order — OPEN
                    return {"status": "NEW", "executedQty": "0",
                            "cummulativeQuoteQty": "0", "price": "11.799",
                            "updateTime": 1}
                return {"status": "UNKNOWN"}
            def get_all_tickers(self):
                return [{"symbol": "AVAXUSDT", "price": "11.00"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "11.00"}
            def v3_get_order_list(self, orderListId):
                raise Exception("no OCO")

        # The cancel-before-replace protocol: when TP_ONLY and tp_order_id is set,
        # the code queries get_order(55001) → finds NEW → cancels it → then places new.
        # This is the correct idempotency behavior for TP_ONLY.
        executor.cancel_order.return_value = None  # cancel succeeds

        # Need to simulate cancel confirmation — second get_order call returns CANCELED
        call_counts = {"55001": 0}
        original_get_order = _C.get_order

        def get_order_with_cancel(self_c, symbol, orderId):
            if orderId == 55001:
                call_counts["55001"] += 1
                if call_counts["55001"] == 1:
                    return {"status": "NEW", "executedQty": "0",
                            "cummulativeQuoteQty": "0", "price": "11.799", "updateTime": 1}
                return {"status": "CANCELED", "executedQty": "0",
                        "cummulativeQuoteQty": "0", "price": "11.799", "updateTime": 2}
            return original_get_order(self_c, symbol, orderId)

        _C.get_order = get_order_with_cancel
        monitor = _make_monitor(_C(), executor)
        _run_check(monitor, trade)

        # cancel_order + place_oco_order should both be called (existing is cancelled first)
        executor.cancel_order.assert_called_once()
        executor.place_oco_order.assert_called_once()
        # After replacement, TP_ONLY should be maintained with new tp_order_id
        self.assertEqual(trade["oco_reconciliation_status"], "TP_ONLY")
        print("✓ Test 7: Scenario B — restart finds existing TP, cancels before replace")

    # ── Test 8: Scenario C ───────────────────────────────────────────────

    def test_8_scenario_c_db_unprotected_exchange_has_tp_reconciles(self):
        """
        Test 8 (Scenario C): DB=UNPROTECTED, exchange already has matching
        SELL LIMIT_MAKER → reconcile to TP_ONLY, no new order placed.
        """
        trade = _avax_trade()  # UNPROTECTED, tp_order_id=None
        executor = MagicMock()

        class _C:
            def get_order(self, symbol, orderId):
                if orderId == 1001:
                    return {"status": "FILLED", "executedQty": "1.53",
                            "cummulativeQuoteQty": str(1.53 * 7.836),
                            "price": "7.836", "updateTime": 1}
                return {"status": "UNKNOWN"}
            def get_all_tickers(self):
                return [{"symbol": "AVAXUSDT", "price": "11.00"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "11.00"}
            def v3_get_order_list(self, orderListId):
                raise Exception("no OCO")
            def get_open_orders(self, symbol):
                return [{"orderId": 88001, "side": "SELL", "type": "LIMIT_MAKER",
                         "origQty": "1.53", "price": "11.799", "symbol": "AVAXUSDT"}]

        monitor = _make_monitor(_C(), executor)
        _run_check(monitor, trade)

        executor.place_oco_order.assert_not_called()
        self.assertEqual(trade["oco_reconciliation_status"], "TP_ONLY")
        self.assertEqual(trade["tp_order_id"], 88001)
        print("✓ Test 8: Scenario C — DB=UNPROTECTED + exchange TP found → reconciled, no new order")

    # ── Test 9: Scenario D ───────────────────────────────────────────────

    def test_9_scenario_d_cancel_not_confirmed_aborts(self):
        """
        Test 9 (Scenario D): Cancel succeeds (API call made) but re-query still
        returns NEW status → abort, no replacement created.
        """
        # TP_ONLY with an existing order that refuses to cancel
        trade = _avax_trade(
            oco_reconciliation_status="TP_ONLY",
            tp_order_id=66001,
        )
        executor = MagicMock()
        executor.cancel_order.return_value = None  # cancel API succeeds

        call_count = [0]

        class _C:
            def get_order(self, symbol, orderId):
                if orderId == 1001:
                    return {"status": "FILLED", "executedQty": "1.53",
                            "cummulativeQuoteQty": str(1.53 * 7.836),
                            "price": "7.836", "updateTime": 1}
                if orderId == 66001:
                    call_count[0] += 1
                    # Both queries return NEW — cancel not confirmed
                    return {"status": "NEW", "executedQty": "0",
                            "cummulativeQuoteQty": "0", "price": "11.799", "updateTime": 1}
                return {"status": "UNKNOWN"}
            def get_all_tickers(self):
                return [{"symbol": "AVAXUSDT", "price": "11.00"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "11.00"}
            def v3_get_order_list(self, orderListId):
                raise Exception("no OCO")

        monitor = _make_monitor(_C(), executor)
        _run_check(monitor, trade)

        # place_oco_order must NOT be called after failed cancel confirmation
        executor.place_oco_order.assert_not_called()
        self.assertEqual(trade["exit_status"], "OPEN",
                         "Trade must remain OPEN when cancel not confirmed")
        print("✓ Test 9: Scenario D — cancel not confirmed → abort, no replacement")


# ---------------------------------------------------------------------------
# Test group 3: 2Z shadow detection
# ---------------------------------------------------------------------------

class TestStuckOcoShadowDetection(unittest.TestCase):
    """Tests 10-11: 2ZUSDT-style stuck-OCO two-cycle detection."""

    def _stuck_oco_client(self):
        """Client where OCO is EXECUTING but price is below SL."""
        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1600"}]  # below SL (0.1657)
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1600"}
            def v3_get_order_list(self, orderListId):
                return {"listOrderStatus": "EXECUTING", "orders": []}  # OCO alive
        return _C()

    # ── Test 10: Cycle N — first detection ──────────────────────────────

    def test_10_cycle_n_suspected_at_set_no_resolve(self):
        """
        Test 10: Cycle N — sl_breached + OCO EXECUTING.
        suspected_at must be set in raw_entry_order.stuck_oco_detection.
        NO cancel, NO sell, NO exit_status change.
        """
        trade = _base_trade(entry_fill_time=1000)

        # Clear module-level state
        if hasattr(SpotPositionMonitor, "_stuck_oco_marked_this_run"):
            SpotPositionMonitor._stuck_oco_marked_this_run = set()

        executor = MagicMock()
        _run_check(_make_monitor(self._stuck_oco_client(), executor), trade)

        # Trade must remain OPEN
        self.assertEqual(trade["exit_status"], "OPEN",
                         "Cycle N must not resolve exit_status")
        self.assertIsNone(trade.get("exit_reason"),
                          "Cycle N must not set exit_reason")

        # suspected_at must be set
        raw = trade.get("raw_entry_order") or {}
        stuck = raw.get("stuck_oco_detection") or {}
        self.assertIn("suspected_at", stuck)
        self.assertIsNotNone(stuck["suspected_at"])

        # No sell order must be placed
        executor.close_position.assert_not_called()
        executor.place_oco_order.assert_not_called()
        print(f"✓ Test 10: Cycle N — suspected_at={stuck['suspected_at']}, trade OPEN")

    # ── Test 11: Cycle N+1 — confirmed detection sends Telegram ─────────

    def test_11_cycle_n1_confirmed_sends_telegram_no_auto_resolve(self):
        """
        Test 11: Cycle N+1 — same conditions + suspected_at from prior cycle.
        Telegram alert must be sent. NO cancel, NO sell.
        """
        trade = _base_trade(entry_fill_time=1000)

        # Simulate cycle N already ran: set suspected_at from a prior run
        trade["raw_entry_order"] = {
            "stuck_oco_detection": {
                "suspected_at": "2025-01-01T00:00:00.000000Z"
            }
        }

        # Ensure this entry_order_id is NOT in the "marked this run" set
        if hasattr(SpotPositionMonitor, "_stuck_oco_marked_this_run"):
            SpotPositionMonitor._stuck_oco_marked_this_run.discard(700191)
        else:
            SpotPositionMonitor._stuck_oco_marked_this_run = set()

        telegram_calls = []
        executor = MagicMock()

        with ExitStack() as s:
            s.enter_context(patch.object(pte.repo, "load_trade_log",
                                         return_value=[trade]))
            s.enter_context(patch.object(pte.repo, "save_trade_log"))
            s.enter_context(patch("services.supabase_client.update_spot_by_order_id"))
            tg_mock = s.enter_context(patch(
                "core.executors.spot_position_monitor._send_telegram",
                side_effect=lambda msg: telegram_calls.append(msg)))
            s.enter_context(patch("core.paper_trade_executor._send_telegram"))
            s.enter_context(patch(
                "core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
                return_value=_POOL))
            buf = io.StringIO()
            with redirect_stdout(buf):
                _make_monitor(self._stuck_oco_client(), executor).check_positions()

        # Telegram must be sent with stuck-OCO warning
        self.assertGreater(len(telegram_calls), 0,
                           "Cycle N+1 must send a Telegram alert for confirmed stuck OCO")
        stuck_alert = any(
            "STUCK" in msg.upper() or "stuck" in msg.lower()
            for msg in telegram_calls
        )
        self.assertTrue(stuck_alert,
                        f"Telegram must mention stuck OCO. Got: {telegram_calls}")

        # Trade must NOT be resolved
        self.assertEqual(trade["exit_status"], "OPEN",
                         "Cycle N+1 must not auto-resolve exit_status")
        self.assertIsNone(trade.get("exit_reason"),
                          "Cycle N+1 must not set exit_reason")

        # No sell order
        executor.close_position.assert_not_called()
        print("✓ Test 11: Cycle N+1 — Telegram sent, trade still OPEN, no auto-resolve")


# ---------------------------------------------------------------------------
# Test group 4: tp_order_id / sl_order_id persistence
# ---------------------------------------------------------------------------

class TestOrderIdPersistence(unittest.TestCase):
    """Tests 12-13: verify tp_order_id/sl_order_id reach Supabase structured columns."""

    def _filled_entry_client(self):
        """Client for a freshly filled entry (no OCO yet)."""
        class _C:
            def get_order(self, symbol, orderId):
                return {"status": "FILLED", "executedQty": "65",
                        "cummulativeQuoteQty": str(65 * 0.1846),
                        "price": "0.1846", "updateTime": 1000}
            def get_all_tickers(self):
                return [{"symbol": "XLMUSDT", "price": "0.1900"}]
            def get_symbol_ticker(self, symbol):
                return {"price": "0.1900"}
            def get_my_trades(self, symbol, orderId, limit):
                return []
        return _C()

    # ── Test 12: TP_ONLY persistence ─────────────────────────────────────

    def test_12_tp_only_persists_tp_order_id_to_supabase(self):
        """
        Test 12: When protection_state=TP_ONLY from place_oco_order(),
        tp_order_id must be written to the Supabase structured column
        via update_spot_by_order_id (not only in raw_entry_order).
        """
        # Trade with entry FILLED and no OCO yet (fresh entry)
        trade = _base_trade(
            oco_placed=False,
            oco_list_id=None,
            oco_order_ids=None,
            oco_reconciliation_status=None,
            tp_order_id=None,
            sl_order_id=None,
        )
        executor = MagicMock()
        executor.place_oco_order.return_value = {
            "protection_state": "TP_ONLY",
            "oco_resp": None,
            "tp_order_id": 99001,
            "sl_order_id": None,
            "filter_reason": "SL_FILTER_INVALID",
        }

        supabase_calls = _run_check_tracking_supabase(
            _make_monitor(self._filled_entry_client(), executor), trade
        )

        # Find any call that includes tp_order_id
        tp_id_persisted = any(
            fields.get("tp_order_id") == 99001
            for eid, fields in supabase_calls
        )
        self.assertTrue(tp_id_persisted,
                        f"tp_order_id=99001 must appear in a Supabase update call. "
                        f"Calls: {supabase_calls}")
        print(f"✓ Test 12: tp_order_id=99001 persisted to Supabase structured column")

    # ── Test 13: SL_ONLY persistence ─────────────────────────────────────

    def test_13_sl_only_persists_sl_order_id_to_supabase(self):
        """
        Test 13: When protection_state=SL_ONLY from place_oco_order(),
        sl_order_id must be written to the Supabase structured column
        via update_spot_by_order_id.
        """
        trade = _base_trade(
            oco_placed=False,
            oco_list_id=None,
            oco_order_ids=None,
            oco_reconciliation_status=None,
            tp_order_id=None,
            sl_order_id=None,
        )
        executor = MagicMock()
        executor.place_oco_order.return_value = {
            "protection_state": "SL_ONLY",
            "oco_resp": None,
            "tp_order_id": None,
            "sl_order_id": 99002,
            "filter_reason": "TP_FILTER_INVALID",
        }

        supabase_calls = _run_check_tracking_supabase(
            _make_monitor(self._filled_entry_client(), executor), trade
        )

        sl_id_persisted = any(
            fields.get("sl_order_id") == 99002
            for eid, fields in supabase_calls
        )
        self.assertTrue(sl_id_persisted,
                        f"sl_order_id=99002 must appear in a Supabase update call. "
                        f"Calls: {supabase_calls}")
        print(f"✓ Test 13: sl_order_id=99002 persisted to Supabase structured column")


if __name__ == "__main__":
    unittest.main(verbosity=2)
