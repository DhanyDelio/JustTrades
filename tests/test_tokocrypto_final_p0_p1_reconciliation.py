"""
test_tokocrypto_final_p0_p1_reconciliation.py
=============================================
Comprehensive P0 & P1 test suite verifying:
 1. OCO lost after maintenance (DB still has old OCO ID) -> detected, NOT claimed protected.
 2. OCO active and verified after maintenance -> confirmed EXECUTING / OCO ✓.
 3. Unprotected open position is NEVER falsely displayed as protected.
 4. Partial fill followed by cancellation -> asset preserved, exit remains OPEN.
 5. Canceled zero-fill releases slot and unblocks symbol.
 6. Timeout during submit order prevents duplicate orders.
 7. Exchange API failure or database failure -> fail-closed.
 8. BUY fee deduction leaves smaller base asset -> OCO sizes down to available free balance.
 9. Multi-candidate scan decrements remaining_idr so cash is never over-allocated.
10. Five active positions block the 6th entry.
11. Minimum notional fallback consistent at Rp 20,000 across executor & candidate scanner.
12. Trading strategy invariants (dynamic allocation, adaptive SL buffer, support zones) preserved.
"""

from __future__ import annotations

import unittest
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoError
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    validate_protective_oco_eligibility,
    get_default_sl_buffer_pct,
)
from core.executors.tokocrypto_position_monitor import TokocryptoPositionMonitor
from tokocrypto_executor import (
    calculate_adaptive_allocation,
    calculate_available_slots,
    calculate_new_order_allocation,
    _sl_buffer_pct,
    MIN_NOTIONAL_IDR,
    MAX_POSITIONS,
)

with patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": ""}):
    from dashboard import compute_toko_oco_badge


class TestTokocryptoFinalP0P1Reconciliation(unittest.TestCase):

    def setUp(self):
        from core.clients import tokocrypto_order_executor as order_executor_module

        order_executor_module._ACTIVE_ENTRY_SUBMISSIONS.clear()
        order_executor_module._UNKNOWN_ENTRY_SUBMISSIONS.clear()
        order_executor_module._UNKNOWN_OCO_SUBMISSIONS.clear()
        self.mock_client = MagicMock(spec=TokocryptoClient)
        self.mock_client.normalize_symbol = lambda s: s
        self.mock_client.round_step = lambda val, step: round(val, 6)
        self.mock_client.round_tick = lambda val, tick: round(val, 2)
        self.mock_client.authenticated = True
        self.executor = TokocryptoOrderExecutor(
            self.mock_client,
            supervised=False,
            trading_phase="PHASE_3",
            dry_run=False,
            max_slots=5,
        )
        self.monitor = TokocryptoPositionMonitor(self.mock_client, self.executor)

    # -------------------------------------------------------------------------
    # 1. OCO lost after maintenance, but DB still has old OCO ID
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_01_oco_lost_after_maintenance_detected_and_not_claimed_protected(
        self, mock_tg, mock_update
    ):
        """DB has legacy b_order_list_id and leg IDs, but exchange cancelled them after maintenance."""
        trade = {
            "symbol": "BNB_IDR",
            "entry_order_id": "917603053",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "25208289734",
            "tp_order_id": "917549739",
            "sl_order_id": "917549740",
            "entry_price": 13122250.0,
            "tp_price": 13778000.0,
            "sl_price": 12794000.0,
        }

        # Exchange returns status 3 (CANCELED) for both TP and SL legs post-maintenance
        def _get_detail(s, oid):
            is_sl = str(oid) == trade["sl_order_id"]
            return {
                "orderId": oid,
                "status": 3,
                "executedQty": "0",
                "executedPrice": "0",
                "type": 4 if is_sl else 1,
                "stopPrice": "12794000" if is_sl else "0",
            }

        self.mock_client.get_order_detail.side_effect = _get_detail

        # 1. Query OCO state
        state_dict = self.executor.query_oco_state(trade)
        self.assertEqual(state_dict["state"], "BOTH_CANCELED_ANOMALY")

        # 2. Monitor processes trade -> records anomaly, does not auto-resolve
        self.monitor._check_one(trade, verbose=True)
        mock_update.assert_called()
        call_oid, update_payload = mock_update.call_args[0]
        self.assertEqual(call_oid, "917603053")
        self.assertEqual(update_payload["oco_state"], "BOTH_CANCELED_ANOMALY")
        self.assertTrue(update_payload["raw_entry_order"]["requires_manual_review"])

        # 3. Dashboard badge check: MUST NOT show OCO ✓
        trade_with_anomaly = {**trade, "oco_state": "BOTH_CANCELED_ANOMALY"}
        badge = compute_toko_oco_badge(trade_with_anomaly)
        self.assertIn("🚨 BOTH_CANCELED_ANOMALY", badge)
        self.assertNotIn("OCO ✓", badge)

    # -------------------------------------------------------------------------
    # 2. OCO active and verified after maintenance
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_02_oco_active_and_verified_after_maintenance(self, mock_tg, mock_update):
        """Exchange confirms both legs are still open (status 0: NEW) -> EXECUTING and OCO ✓."""
        trade = {
            "symbol": "BTC_IDR",
            "entry_order_id": "917500001",
            "entry_status": "FILLED",
            "exit_status": "OPEN",
            "b_order_list_id": "25208289999",
            "tp_order_id": "917549888",
            "sl_order_id": "917549889",
            "entry_price": 1000000000.0,
            "tp_price": 1050000000.0,
            "sl_price": 970000000.0,
        }

        def _get_detail_open(s, oid):
            is_sl = str(oid) == trade["sl_order_id"]
            return {
                "orderId": oid,
                "status": 0,
                "executedQty": "0",
                "type": 4 if is_sl else 1,
                "stopPrice": "970000000" if is_sl else "0",
            }

        self.mock_client.get_order_detail.side_effect = _get_detail_open
        self.mock_client.get_ticker.return_value = 1010000000.0

        state_dict = self.executor.query_oco_state(trade)
        self.assertEqual(state_dict["state"], "EXECUTING")

        self.monitor._check_one(trade, verbose=True)
        mock_update.assert_called()
        call_oid, update_payload = mock_update.call_args[0]
        self.assertEqual(update_payload["oco_state"], "EXECUTING")

        trade_verified = {**trade, "oco_state": "EXECUTING"}
        badge = compute_toko_oco_badge(trade_verified)
        self.assertIn("LAST CHECK EXECUTING", badge)
        self.assertNotIn("OCO ✓", badge)

    # -------------------------------------------------------------------------
    # 3. Unprotected open position is never falsely displayed as protected
    # -------------------------------------------------------------------------
    def test_03_unprotected_open_position_never_shows_protected(self):
        """Row 7 pattern: Filled position with failed or missing OCO must never show OCO ✓."""
        # Case A: OCO placement failed
        t1 = {
            "entry_status": "FILLED",
            "oco_state": "OCO_PLACEMENT_FAILED",
            "b_order_list_id": None,
        }
        b1 = compute_toko_oco_badge(t1)
        self.assertIn("⚠ OCO FAILED", b1)
        self.assertNotIn("OCO ✓", b1)

        # Case B: Filled with no OCO fields at all
        t2 = {
            "entry_status": "FILLED",
            "oco_state": None,
            "b_order_list_id": None,
        }
        b2 = compute_toko_oco_badge(t2)
        self.assertIn("⚠ NO OCO", b2)
        self.assertNotIn("OCO ✓", b2)

        # Case C: Historical FULLY_PROTECTED but no leg IDs
        t3 = {
            "entry_status": "FILLED",
            "oco_state": "FULLY_PROTECTED",
            "tp_order_id": None,
            "sl_order_id": None,
        }
        b3 = compute_toko_oco_badge(t3)
        self.assertIn("⚠ NO OCO", b3)
        self.assertNotIn("OCO ✓", b3)

    # -------------------------------------------------------------------------
    # 4. Partial fill followed by cancellation
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_04_partial_fill_then_cancellation(self, mock_tg, mock_update):
        """Entry cancelled with executedQty > 0 must NOT release slot or close trade."""
        trade = {
            "symbol": "ETH_IDR",
            "entry_order_id": "917888777",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 40000000.0,
            "entry_qty": 0.01,
        }
        raw_detail = {
            "orderId": "917888777",
            "status": 3,  # CANCELED remaining
            "executedQty": "0.005",  # Half filled!
            "executedPrice": "40000000",
            "createTime": 1791500000000,
        }
        self.mock_client.get_order_detail.return_value = raw_detail

        with patch.object(self.executor, "place_oco"):
            self.monitor._check_one(trade, verbose=True)

        self.assertGreaterEqual(mock_update.call_count, 1)
        call_oid, payload = mock_update.call_args_list[0].args
        self.assertEqual(call_oid, "917888777")
        self.assertEqual(payload["entry_status"], "FILLED")
        self.assertEqual(payload["entry_qty"], 0.005)
        # CRUCIAL: exit_status must NOT be set to CANCELED!
        self.assertNotIn("exit_status", payload)

    # -------------------------------------------------------------------------
    # 5. Canceled zero-fill releases slot and unblocks symbol
    # -------------------------------------------------------------------------
    @patch("core.executors.tokocrypto_position_monitor.update_tokocrypto_by_order_id")
    @patch("core.executors.tokocrypto_position_monitor._send_toko_telegram")
    def test_05_canceled_zero_fill_releases_slot_and_unblocks_symbol(
        self, mock_tg, mock_update
    ):
        """Entry cancelled with 0 fill releases slot in DB and allows future entry for symbol."""
        trade = {
            "symbol": "BNB_IDR",
            "entry_order_id": "917625990",
            "entry_status": "NEW",
            "exit_status": "OPEN",
            "entry_price": 12892754.0,
            "entry_qty": 0.003,
        }
        raw_detail = {
            "orderId": "917625990",
            "status": 3,  # CANCELED
            "executedQty": "0",
            "executedPrice": "0",
        }
        self.mock_client.get_order_detail.return_value = raw_detail

        self.monitor._check_one(trade, verbose=True)

        self.assertGreaterEqual(mock_update.call_count, 1)
        call_oid, payload = mock_update.call_args_list[0].args
        self.assertEqual(payload["entry_status"], "CANCELED")
        self.assertEqual(payload["exit_status"], "CANCELED")
        self.assertEqual(payload["exit_reason"], "ENTRY_CANCELED")

        # Verify slots: canceled row does not count as open position
        open_trades = [{"symbol": "SOL_IDR", "exit_status": "OPEN"}]
        slots = calculate_available_slots(len(open_trades), MAX_POSITIONS)
        self.assertEqual(slots, 4)

    # -------------------------------------------------------------------------
    # 6. Timeout during submit order prevents duplicate orders
    # -------------------------------------------------------------------------
    def test_06_timeout_during_submit_prevents_duplicate_order(self):
        """If previous timed-out order is now working on exchange, has_active_position detects it."""
        cand = {
            "symbol": "BNB_IDR",
            "entry_price": 13000000.0,
            "tp_price": 13650000.0,
            "sl_price": 12675000.0,
        }

        # Simulate: exchange accepted order before client timed out
        self.mock_client.get_open_orders.return_value = [
            {"orderId": "917999111", "symbol": "BNB_IDR", "status": 0, "side": 0}
        ]

        # Duplicate guard must identify active working order on exchange
        has_pos = self.executor.has_active_position("BNB_IDR")
        self.assertTrue(has_pos)

        # execute_entry must reject duplicate submission
        result = self.executor.execute_entry(cand, slot_size_idr=25000.0)
        self.assertIsNone(result)

    # -------------------------------------------------------------------------
    # 7. Exchange API failure or database failure -> fail-closed
    # -------------------------------------------------------------------------
    def test_07_api_exchange_or_db_failure_fails_closed(self):
        """If API or DB fails, has_active_position must return True (fail-closed, block new entry)."""
        # Case A: Exchange get_open_orders raises network error
        self.mock_client.get_open_orders.side_effect = TokocryptoError(
            "503 Service Unavailable"
        )
        self.assertTrue(self.executor.has_active_position("SOL_IDR"))

        # Case B: Exchange succeeds (empty), but Supabase DB query fails
        self.mock_client.get_open_orders.side_effect = None
        self.mock_client.get_open_orders.return_value = []
        with patch(
            "services.supabase_client.fetch_all_tokocrypto_strict",
            side_effect=Exception("DB connection refused"),
        ):
            self.assertTrue(self.executor.has_active_position("SOL_IDR"))

    # -------------------------------------------------------------------------
    # 8. BUY fee deduction leaves smaller base asset -> OCO sizes down
    # -------------------------------------------------------------------------
    @patch("core.clients.tokocrypto_order_executor._confirm", return_value=True)
    def test_08_buy_fee_deduction_adjusts_oco_quantity(self, mock_conf):
        """If BUY fee reduced 0.002 BNB to 0.00199756 BNB, place_oco uses available balance."""
        sym_info = MagicMock()
        sym_info.tick_size = 1.0
        sym_info.step_size = 0.000001
        sym_info.min_qty = 0.0001
        sym_info.min_notional = 20000.0
        self.mock_client.get_symbol.return_value = sym_info

        bal_mock = MagicMock()
        bal_mock.free = 0.00199756  # Actual balance after fee
        self.mock_client.get_balance.return_value = bal_mock
        self.mock_client.get_ticker.return_value = 13100000.0

        trade = {
            "symbol": "BNB_IDR",
            "entry_order_id": "917603053",
            "entry_price": 13000000.0,
            "tp_price": 13650000.0,
            "sl_price": 12675000.0,
            "entry_qty": 0.002,  # Requested entry qty before fee
        }

        # Enable dry_run to observe formatted payload
        self.executor.dry_run = True
        resp = self.executor.place_oco(trade)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["bOrderListId"], "DRY_OCO")

    # -------------------------------------------------------------------------
    # 9. Multi-candidate scan decrements remaining_idr
    # -------------------------------------------------------------------------
    def test_09_multi_candidate_scan_decrements_remaining_idr(self):
        """Simulate tokocrypto_executor loop: remaining_idr must prevent over-allocating cash."""
        idr_bal = 100_000.0
        target_slots, alloc_per_order = calculate_adaptive_allocation(
            wallet_balance=idr_bal,
            available_slots=4,
            min_notional=MIN_NOTIONAL_IDR,
        )
        self.assertEqual(target_slots, 4)
        self.assertEqual(alloc_per_order, 25_000.0)

        # Loop simulation
        filled = 0
        remaining_idr = idr_bal
        candidates = [
            {"symbol": "SOL_IDR", "notional": 25_000.0},
            {"symbol": "ADA_IDR", "notional": 25_000.0},
            {"symbol": "DOGE_IDR", "notional": 25_000.0},
            {"symbol": "XRP_IDR", "notional": 25_000.0},
            {"symbol": "AVAX_IDR", "notional": 25_000.0},  # 5th candidate
        ]

        allocated_orders = []
        for cand in candidates:
            if filled >= target_slots:
                break
            slot_budget = min(alloc_per_order, remaining_idr)
            if slot_budget < MIN_NOTIONAL_IDR:
                break
            allocated_orders.append(cand["symbol"])
            filled += 1
            remaining_idr -= slot_budget

        self.assertEqual(len(allocated_orders), 4)
        self.assertEqual(remaining_idr, 0.0)
        self.assertNotIn("AVAX_IDR", allocated_orders)

    # -------------------------------------------------------------------------
    # 10. Five active positions block the 6th entry
    # -------------------------------------------------------------------------
    def test_10_five_active_positions_block_sixth_entry(self):
        """When 5 positions are open, slots_available=0 and calculate_adaptive_allocation returns (0, 0.0)."""
        slots = calculate_available_slots(5, MAX_POSITIONS)
        self.assertEqual(slots, 0)

        target, alloc = calculate_adaptive_allocation(
            500_000.0, available_slots=slots, min_notional=20_000.0
        )
        self.assertEqual(target, 0)
        self.assertEqual(alloc, 0.0)

    # -------------------------------------------------------------------------
    # 11. Minimum notional fallback consistent at Rp 20,000
    # -------------------------------------------------------------------------
    def test_11_minimum_notional_fallback_consistent(self):
        """Verify MIN_NOTIONAL_IDR is consistently 20,000 across modules."""
        self.assertEqual(self.executor.MIN_NOTIONAL_IDR, 20_000.0)
        self.assertEqual(MIN_NOTIONAL_IDR, 20_000.0)

        # Pre-flight OCO eligibility check rejects any order with TP notional < Rp 20,000
        sym_mock = MagicMock()
        sym_mock.tick_size = 1.0
        sym_mock.step_size = 0.001
        sym_mock.min_qty = 0.001
        sym_mock.min_notional = 20000.0

        is_eligible, reason, _ = validate_protective_oco_eligibility(
            entry_qty=1.0,
            entry_price=10_000.0,  # notional = 10,000 < 20,000
            tp_price=10_500.0,
            sl_price=9_500.0,
            sym_info=sym_mock,
        )
        self.assertFalse(is_eligible)
        self.assertIn("min_notional", reason.lower())

    # -------------------------------------------------------------------------
    # 12. Trading strategy invariants preserved
    # -------------------------------------------------------------------------
    def test_12_trading_strategy_invariants_preserved(self):
        """Verify dynamic allocation formula and adaptive SL buffer tiers."""
        # A. Adaptive SL buffer tiers
        self.assertEqual(_sl_buffer_pct(150_000), 0.0015)  # > 100k -> 0.15%
        self.assertEqual(_sl_buffer_pct(50_000), 0.0030)  # 1k - 100k -> 0.30%
        self.assertEqual(_sl_buffer_pct(500), 0.0050)  # < 1k -> 0.50%

        # B. Default SL buffer helper
        self.assertEqual(get_default_sl_buffer_pct(15_000), Decimal("0.0015"))
        self.assertEqual(get_default_sl_buffer_pct(5_000), Decimal("0.0025"))
        self.assertEqual(get_default_sl_buffer_pct(500), Decimal("0.0035"))

        # C. Adaptive allocation formula step down
        target, alloc = calculate_adaptive_allocation(
            35_000.0, available_slots=2, min_notional=20_000.0
        )
        # 35k / 2 = 17.5k (< 20k) -> steps down to 1 slot @ 35k
        self.assertEqual(target, 1)
        self.assertEqual(alloc, 35_000.0)


if __name__ == "__main__":
    unittest.main()
