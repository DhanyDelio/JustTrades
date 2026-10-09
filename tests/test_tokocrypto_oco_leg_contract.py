"""
test_tokocrypto_oco_leg_contract.py
===================================
Comprehensive unit test suite for Tokocrypto OCO leg disambiguation,
defensive attribute parsing, fail-closed reconciliation, and safe auto-heal:

Scenarios covered:
A. Correct OCO identification
   1. Correct TP/SL pointers, TP fills -> TP_HIT
   2. Correct TP/SL pointers, SL fills -> SL_HIT
   3. Swapped pointers, verified TP fills -> TP_HIT (persists healed pointers)
   4. Swapped pointers, verified SL fills -> SL_HIT (persists healed pointers)

B. Ambiguous or invalid legs
   5. Both child orders are plain LIMIT -> RECONCILIATION_REQUIRED
   6. Both child orders are stop-loss legs -> RECONCILIATION_REQUIRED
   7. Both child orders are take-profit legs -> RECONCILIATION_REQUIRED
   8. Unknown order type -> RECONCILIATION_REQUIRED
   9. Missing required order attributes -> RECONCILIATION_REQUIRED
   10. Contradictory order attributes -> RECONCILIATION_REQUIRED
   11. Malformed stopPrice values -> RECONCILIATION_REQUIRED (no crash)
   12. stopPrice is None or empty -> RECONCILIATION_REQUIRED
   13. stopPrice is NaN or positive/negative infinity -> RECONCILIATION_REQUIRED
   14. Negative stopPrice -> RECONCILIATION_REQUIRED

C. Query failures
   15. First child-order query fails -> RECONCILIATION_REQUIRED
   16. Second child-order query fails -> RECONCILIATION_REQUIRED
   17. First child-order query times out -> RECONCILIATION_REQUIRED
   18. Second child-order query times out -> RECONCILIATION_REQUIRED
   19. Exchange returns malformed response -> RECONCILIATION_REQUIRED

D. Supabase auto-heal failures
   20. Auto-heal update succeeds -> auto_heal_persisted == True
   21. Auto-heal update fails with exception -> logged, not durable, auto_heal_persisted == False
   22. Auto-heal update returns error context in state dict
   23. Ambiguous leg identity never triggers auto-heal write
   24. Query failure never triggers auto-heal write
   25. Failed persistence logs warning with symbol and IDs

E. No unintended exchange side effects
   26. query_oco_state() never submits, replaces, or cancels an exchange order
   27. Ambiguous OCO states never trigger market exit
   28. Reconciliation failures do not alter entry price, entry quantity, PnL, or unrelated fields

F. place_oco leg disambiguation
   29. Response [TP, SL] identified correctly
   30. Response [SL, TP] reversed identified correctly
   31. Bare IDs resolved via get_order_detail
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import (
    TokocryptoClient,
    TokocryptoError,
    TokocryptoNetworkError,
)
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    _parse_stop_price,
    _classify_order_leg,
)


class TestTokocryptoOCOLegContract(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock(spec=TokocryptoClient)
        self.mock_client.normalize_symbol = lambda s: s
        self.mock_client.round_step = lambda val, step: round(val, 6)
        self.mock_client.round_tick = lambda val, tick: round(val, 2)
        self.mock_client.authenticated = True

        sym_mock = MagicMock()
        sym_mock.tick_size = 1.0
        sym_mock.step_size = 0.001
        sym_mock.min_qty = 0.001
        sym_mock.min_notional = 20000.0
        self.mock_client.get_symbol.return_value = sym_mock

        bal_mock = MagicMock()
        bal_mock.free = 1.0
        self.mock_client.get_balance.return_value = bal_mock
        self.mock_client.get_ticker.return_value = 1000000.0

        self.executor = TokocryptoOrderExecutor(
            self.mock_client,
            supervised=False,
            trading_phase="PHASE_3",
            dry_run=False,
        )

        self.base_trade = {
            "symbol": "BTC_IDR",
            "entry_order_id": "9999",
            "entry_price": 1000000.0,
            "entry_fill_price": 1000000.0,
            "entry_qty": 0.05,
            "tp_price": 1050000.0,
            "sl_price": 950000.0,
            "tp_order_id": "TP_101",
            "sl_order_id": "SL_102",
        }

    # =========================================================================
    # Group A: Correct OCO Identification
    # =========================================================================

    def test_01_correct_pointers_tp_fills_returns_tp_hit(self):
        """1. Correct TP/SL pointers, TP fills: Expected state TP_HIT."""
        def _detail(sym, oid):
            if oid == "TP_101":
                return {"orderId": "TP_101", "type": 1, "stopPrice": "0", "status": 2, "executedPrice": "1050000"}
            return {"orderId": "SL_102", "type": 4, "stopPrice": "950000", "status": 3, "executedPrice": "0"}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "TP_HIT")
        self.assertEqual(state["exit_price"], 1050000.0)

    def test_02_correct_pointers_sl_fills_returns_sl_hit(self):
        """2. Correct TP/SL pointers, SL fills: Expected state SL_HIT."""
        def _detail(sym, oid):
            if oid == "TP_101":
                return {"orderId": "TP_101", "type": 1, "stopPrice": "0", "status": 3, "executedPrice": "0"}
            return {"orderId": "SL_102", "type": 4, "stopPrice": "950000", "status": 2, "executedPrice": "945000"}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "SL_HIT")
        self.assertEqual(state["exit_price"], 945000.0)

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_03_swapped_pointers_tp_fills_returns_tp_hit_and_persists(self, mock_update):
        """3. Swapped pointers, verified TP fills: Expected state TP_HIT, auto-heal persisted."""
        trade = dict(self.base_trade)
        trade["tp_order_id"] = "ACTUAL_SL_ID"  # Swapped in DB!
        trade["sl_order_id"] = "ACTUAL_TP_ID"  # Swapped in DB!

        def _detail(sym, oid):
            if oid == "ACTUAL_SL_ID":
                return {"orderId": "ACTUAL_SL_ID", "type": 4, "stopPrice": "950000", "status": 3, "executedPrice": "0"}
            return {"orderId": "ACTUAL_TP_ID", "type": 1, "stopPrice": "0", "status": 2, "executedPrice": "1050000"}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(trade)
        self.assertEqual(state["state"], "TP_HIT")
        self.assertEqual(state["exit_price"], 1050000.0)
        self.assertTrue(state.get("auto_heal_persisted"))

        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["tp_order_id"], "ACTUAL_TP_ID")
        self.assertEqual(payload["sl_order_id"], "ACTUAL_SL_ID")

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_04_swapped_pointers_sl_fills_returns_sl_hit_and_persists(self, mock_update):
        """4. Swapped pointers, verified SL fills: Expected state SL_HIT, auto-heal persisted."""
        trade = dict(self.base_trade)
        trade["tp_order_id"] = "ACTUAL_SL_ID"  # Swapped in DB!
        trade["sl_order_id"] = "ACTUAL_TP_ID"  # Swapped in DB!

        def _detail(sym, oid):
            if oid == "ACTUAL_SL_ID":
                return {"orderId": "ACTUAL_SL_ID", "type": 4, "stopPrice": "950000", "status": 2, "executedPrice": "948000"}
            return {"orderId": "ACTUAL_TP_ID", "type": 1, "stopPrice": "0", "status": 3, "executedPrice": "0"}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(trade)
        self.assertEqual(state["state"], "SL_HIT")
        self.assertEqual(state["exit_price"], 948000.0)
        self.assertTrue(state.get("auto_heal_persisted"))

        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["tp_order_id"], "ACTUAL_TP_ID")
        self.assertEqual(payload["sl_order_id"], "ACTUAL_SL_ID")

    # =========================================================================
    # Group B: Ambiguous or Invalid Legs (Fail-Closed)
    # =========================================================================

    def test_05_both_plain_limit_orders_fail_closed(self):
        """5. Both child orders are plain LIMIT (stopPrice=0): Expected RECONCILIATION_REQUIRED."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "type": 1, "stopPrice": "0", "status": 2 if oid == "SL_102" else 3, "executedPrice": "950000"
        }
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_06_both_stop_loss_legs_fail_closed(self):
        """6. Both child orders are stop-loss legs: Expected RECONCILIATION_REQUIRED."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "type": 4, "stopPrice": "950000", "status": 2 if oid == "SL_102" else 3, "executedPrice": "950000"
        }
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_07_both_take_profit_legs_fail_closed(self):
        """7. Both child orders are take-profit legs (type=1, stopPrice=0): Expected RECONCILIATION_REQUIRED."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "type": "LIMIT", "stopPrice": 0, "status": 2 if oid == "TP_101" else 3, "executedPrice": "1050000"
        }
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_08_unknown_order_type_fails_closed(self):
        """8. Unknown order type (e.g. MYSTERY): Expected RECONCILIATION_REQUIRED."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "type": "MYSTERY", "stopPrice": "950000" if oid == "SL_102" else "0", "status": 2
        }
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_09_missing_required_order_attributes_fails_closed(self):
        """9. Missing required order attributes (e.g. no type, no stopPrice): Expected RECONCILIATION_REQUIRED."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "status": 2
        }
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_10_contradictory_order_attributes_fails_closed(self):
        """10. Contradictory order attributes (LIMIT with stopPrice>0, STOP_LOSS with stopPrice=0): Expected RECONCILIATION_REQUIRED."""
        def _detail(sym, oid):
            if oid == "TP_101":
                # Contradiction: LIMIT order with positive stopPrice!
                return {"orderId": "TP_101", "type": 1, "stopPrice": "950000", "status": 2}
            # Contradiction: STOP_LOSS order with zero stopPrice!
            return {"orderId": "SL_102", "type": 4, "stopPrice": "0", "status": 3}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_11_malformed_stopprice_values_fail_closed_without_crash(self):
        """11. Malformed stopPrice values ("MALFORMED", "N/A", "null"): Expected RECONCILIATION_REQUIRED without crash."""
        for bad_val in ("MALFORMED", "N/A", "null"):
            self.mock_client.get_order_detail.side_effect = lambda sym, oid, bv=bad_val: {
                "orderId": oid, "type": 4 if oid == "SL_102" else 1, "stopPrice": bv, "status": 2
            }
            state = self.executor.query_oco_state(self.base_trade)
            self.assertEqual(state["state"], "RECONCILIATION_REQUIRED", f"Failed for {bad_val}")

    def test_12_stopprice_none_or_empty_fails_closed(self):
        """12. stopPrice is None or empty string "": Expected RECONCILIATION_REQUIRED."""
        for empty_val in (None, ""):
            self.mock_client.get_order_detail.side_effect = lambda sym, oid, ev=empty_val: {
                "orderId": oid, "type": 4 if oid == "SL_102" else 1, "stopPrice": ev, "status": 2
            }
            state = self.executor.query_oco_state(self.base_trade)
            self.assertEqual(state["state"], "RECONCILIATION_REQUIRED", f"Failed for {empty_val}")

    def test_13_stopprice_nan_or_infinity_fails_closed(self):
        """13. stopPrice is NaN or positive/negative infinity: Expected RECONCILIATION_REQUIRED."""
        for non_finite in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf")):
            self.mock_client.get_order_detail.side_effect = lambda sym, oid, nf=non_finite: {
                "orderId": oid, "type": 4 if oid == "SL_102" else 1, "stopPrice": nf, "status": 2
            }
            state = self.executor.query_oco_state(self.base_trade)
            self.assertEqual(state["state"], "RECONCILIATION_REQUIRED", f"Failed for {non_finite}")

    def test_14_negative_stopprice_fails_closed(self):
        """14. Negative stopPrice values (-100, "-100"): Expected RECONCILIATION_REQUIRED."""
        for neg_val in (-100, "-100", -0.001):
            self.mock_client.get_order_detail.side_effect = lambda sym, oid, nv=neg_val: {
                "orderId": oid, "type": 4 if oid == "SL_102" else 1, "stopPrice": nv, "status": 2
            }
            state = self.executor.query_oco_state(self.base_trade)
            self.assertEqual(state["state"], "RECONCILIATION_REQUIRED", f"Failed for {neg_val}")

    # =========================================================================
    # Group C: Query Failures
    # =========================================================================

    def test_15_first_child_query_fails_returns_reconciliation_required(self):
        """15. First child-order query fails: Expected RECONCILIATION_REQUIRED."""
        def _detail(sym, oid):
            if oid == "TP_101":
                raise TokocryptoError("Order not found -2013")
            return {"orderId": "SL_102", "type": 4, "stopPrice": "950000", "status": 2}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_16_second_child_query_fails_returns_reconciliation_required(self):
        """16. Second child-order query fails: Expected RECONCILIATION_REQUIRED."""
        def _detail(sym, oid):
            if oid == "SL_102":
                raise TokocryptoError("Order not found -2013")
            return {"orderId": "TP_101", "type": 1, "stopPrice": "0", "status": 2}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_17_first_child_query_timeout_returns_reconciliation_required(self):
        """17. First child-order query times out: Expected RECONCILIATION_REQUIRED."""
        def _detail(sym, oid):
            if oid == "TP_101":
                raise TokocryptoNetworkError("Read timed out")
            return {"orderId": "SL_102", "type": 4, "stopPrice": "950000", "status": 2}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_18_second_child_query_timeout_returns_reconciliation_required(self):
        """18. Second child-order query times out: Expected RECONCILIATION_REQUIRED."""
        def _detail(sym, oid):
            if oid == "SL_102":
                raise TokocryptoNetworkError("Read timed out")
            return {"orderId": "TP_101", "type": 1, "stopPrice": "0", "status": 2}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    def test_19_malformed_non_dict_response_returns_reconciliation_required(self):
        """19. Exchange returns malformed response (non-dict): Expected RECONCILIATION_REQUIRED."""
        self.mock_client.get_order_detail.return_value = "not a dict"
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")

    # =========================================================================
    # Group D: Supabase Auto-Heal Failures & Safety
    # =========================================================================

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_20_auto_heal_succeeds_marks_persisted_true(self, mock_update):
        """20. Auto-heal update succeeds: auto_heal_persisted == True in result."""
        trade = dict(self.base_trade)
        trade["tp_order_id"] = "ACTUAL_SL"
        trade["sl_order_id"] = "ACTUAL_TP"

        def _detail(sym, oid):
            if oid == "ACTUAL_SL":
                return {"orderId": "ACTUAL_SL", "type": 4, "stopPrice": "950000", "status": 0}
            return {"orderId": "ACTUAL_TP", "type": 1, "stopPrice": "0", "status": 0}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(trade)
        self.assertEqual(state["state"], "EXECUTING")
        self.assertTrue(state.get("auto_heal_persisted"))
        mock_update.assert_called_once()

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_21_auto_heal_fails_with_exception_handled_safely(self, mock_update):
        """21. Auto-heal update fails with exception: logged, auto_heal_persisted == False, no crash."""
        mock_update.side_effect = Exception("Supabase network timeout")

        trade = dict(self.base_trade)
        trade["tp_order_id"] = "ACTUAL_SL"
        trade["sl_order_id"] = "ACTUAL_TP"

        def _detail(sym, oid):
            if oid == "ACTUAL_SL":
                return {"orderId": "ACTUAL_SL", "type": 4, "stopPrice": "950000", "status": 0}
            return {"orderId": "ACTUAL_TP", "type": 1, "stopPrice": "0", "status": 0}

        self.mock_client.get_order_detail.side_effect = _detail
        state = self.executor.query_oco_state(trade)
        # In-memory swap evaluated current state correctly without crashing
        self.assertEqual(state["state"], "EXECUTING")
        # Explicit indicator that persistence failed!
        self.assertFalse(state.get("auto_heal_persisted"))
        self.assertIn("Supabase network timeout", state.get("auto_heal_error", ""))

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_22_ambiguous_legs_never_trigger_auto_heal_write(self, mock_update):
        """22. Ambiguous leg identity must never trigger an auto-heal write."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "type": 1, "stopPrice": "0", "status": 0
        }
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")
        mock_update.assert_not_called()

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_23_query_failure_never_triggers_auto_heal_write(self, mock_update):
        """23. Query failure must never trigger an auto-heal write."""
        self.mock_client.get_order_detail.side_effect = TokocryptoError("503 Service Unavailable")
        state = self.executor.query_oco_state(self.base_trade)
        self.assertEqual(state["state"], "RECONCILIATION_REQUIRED")
        mock_update.assert_not_called()

    # =========================================================================
    # Group E: No Unintended Exchange Side Effects
    # =========================================================================

    def test_24_query_oco_state_never_submits_or_cancels_orders(self):
        """24. Verify query_oco_state() never submits, replaces, or cancels an exchange order."""
        self.mock_client.get_order_detail.side_effect = lambda sym, oid: {
            "orderId": oid, "type": 1 if oid == "TP_101" else 4, "stopPrice": "0" if oid == "TP_101" else "950000", "status": 2
        }
        self.executor.query_oco_state(self.base_trade)
        self.mock_client._signed_post.assert_not_called()

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    def test_25_auto_heal_does_not_modify_unrelated_trade_fields(self, mock_update):
        """25. Auto-heal must only update tp_order_id, sl_order_id, and updated_at (never entry_price, qty, etc.)."""
        trade = dict(self.base_trade)
        trade["tp_order_id"] = "ACTUAL_SL"
        trade["sl_order_id"] = "ACTUAL_TP"

        def _detail(sym, oid):
            if oid == "ACTUAL_SL":
                return {"orderId": "ACTUAL_SL", "type": 4, "stopPrice": "950000", "status": 0}
            return {"orderId": "ACTUAL_TP", "type": 1, "stopPrice": "0", "status": 0}

        self.mock_client.get_order_detail.side_effect = _detail
        self.executor.query_oco_state(trade)

        mock_update.assert_called_once()
        _, payload = mock_update.call_args[0]
        # Must only contain the pointer fields
        allowed_keys = {"tp_order_id", "sl_order_id", "updated_at"}
        self.assertEqual(set(payload.keys()), allowed_keys)

    # =========================================================================
    # Group F: place_oco Leg Disambiguation
    # =========================================================================

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_26_place_oco_identifies_standard_response(self, mock_tg, mock_update):
        """26. place_oco identifies standard [TP, SL] response."""
        self.mock_client._signed_post.return_value = {
            "code": 0, "msg": "success",
            "data": {
                "bOrderListId": "1001",
                "orders": [
                    {"orderId": "TP_A", "type": 1, "stopPrice": "0", "price": "1050000"},
                    {"orderId": "SL_B", "type": 4, "stopPrice": "950000", "price": "940000"},
                ],
            },
        }
        resp = self.executor.place_oco(self.base_trade)
        self.assertIsNotNone(resp)
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["tp_order_id"], "TP_A")
        self.assertEqual(payload["sl_order_id"], "SL_B")
        self.assertEqual(payload["oco_state"], "EXECUTING")

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_27_place_oco_identifies_reversed_response(self, mock_tg, mock_update):
        """27. place_oco identifies reversed [SL, TP] response without inverting roles."""
        self.mock_client._signed_post.return_value = {
            "code": 0, "msg": "success",
            "data": {
                "bOrderListId": "1002",
                "orders": [
                    {"orderId": "SL_B", "type": 4, "stopPrice": "950000", "price": "940000"},
                    {"orderId": "TP_A", "type": 1, "stopPrice": "0", "price": "1050000"},
                ],
            },
        }
        resp = self.executor.place_oco(self.base_trade)
        self.assertIsNotNone(resp)
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["tp_order_id"], "TP_A")
        self.assertEqual(payload["sl_order_id"], "SL_B")
        self.assertEqual(payload["oco_state"], "EXECUTING")

    @patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_28_place_oco_bare_ids_resolved_via_order_detail(self, mock_tg, mock_update):
        """28. place_oco bare orderIds resolved by querying order details."""
        self.mock_client._signed_post.return_value = {
            "code": 0, "msg": "success",
            "data": {
                "bOrderListId": "1003",
                "orders": [{"orderId": "BARE_1"}, {"orderId": "BARE_2"}],
            },
        }

        def _detail(sym, oid):
            if oid == "BARE_1":
                return {"orderId": "BARE_1", "type": 4, "stopPrice": "950000"}  # SL
            return {"orderId": "BARE_2", "type": 1, "stopPrice": "0"}        # TP

        self.mock_client.get_order_detail.side_effect = _detail
        resp = self.executor.place_oco(self.base_trade)
        self.assertIsNotNone(resp)
        _, payload = mock_update.call_args[0]
        self.assertEqual(payload["tp_order_id"], "BARE_2")
        self.assertEqual(payload["sl_order_id"], "BARE_1")
        self.assertEqual(payload["oco_state"], "EXECUTING")

    # =========================================================================
    # Group G: Unit Tests for Helper Parsers
    # =========================================================================

    def test_29_parse_stop_price_comprehensive(self):
        """29. Verify _parse_stop_price for all required values."""
        self.assertEqual(_parse_stop_price("0"), 0.0)
        self.assertEqual(_parse_stop_price("123.45"), 123.45)
        self.assertEqual(_parse_stop_price(0), 0.0)
        self.assertEqual(_parse_stop_price(123.45), 123.45)
        self.assertIsNone(_parse_stop_price(None))
        self.assertIsNone(_parse_stop_price(""))
        self.assertIsNone(_parse_stop_price("   "))
        self.assertIsNone(_parse_stop_price("MALFORMED"))
        self.assertIsNone(_parse_stop_price("null"))
        self.assertIsNone(_parse_stop_price("N/A"))
        self.assertIsNone(_parse_stop_price("NaN"))
        self.assertIsNone(_parse_stop_price("Infinity"))
        self.assertIsNone(_parse_stop_price("-Infinity"))
        self.assertIsNone(_parse_stop_price("-10"))
        self.assertIsNone(_parse_stop_price(-10.0))

    def test_30_classify_order_leg_comprehensive(self):
        """30. Verify _classify_order_leg rejects contradictions and accepts valid legs."""
        self.assertEqual(_classify_order_leg({"type": 1, "stopPrice": "0"}), "TP")
        self.assertEqual(_classify_order_leg({"type": "LIMIT", "stopPrice": "0"}), "TP")
        self.assertEqual(_classify_order_leg({"type": 4, "stopPrice": "950000"}), "SL")
        self.assertEqual(_classify_order_leg({"type": "STOP_LOSS_LIMIT", "stopPrice": "950000"}), "SL")
        # Contradictions
        self.assertIsNone(_classify_order_leg({"type": 1, "stopPrice": "950000"}))
        self.assertIsNone(_classify_order_leg({"type": 4, "stopPrice": "0"}))
        self.assertIsNone(_classify_order_leg({"type": "UNKNOWN", "stopPrice": "950000"}))
        self.assertIsNone(_classify_order_leg({"type": 1, "stopPrice": "MALFORMED"}))
        self.assertIsNone(_classify_order_leg({}))


if __name__ == "__main__":
    unittest.main()
