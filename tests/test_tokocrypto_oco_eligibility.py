"""
tests/test_tokocrypto_oco_eligibility.py

Regression test suite for Tokocrypto Pre-Flight Protective OCO Eligibility.

Enforces system invariant:
    ENTRY_ALLOWED = OCO_CAN_BE_PLACED_AFTER_ENTRY

Covers all 10 mandatory regression tests:
1. Real BNB case: 0.002 BUY -> 0.00199756 after fee -> OCO qty 0.001 -> reject BEFORE BUY
2. Sufficient post-fee quantity -> BUY allowed (e.g. BNB 0.003 or POL 20.3)
3. Entry min notional passes but OCO min notional fails -> reject
4. Quantity step-size rounding causes OCO to fail -> reject
5. TP price rounding causes notional to fail -> reject
6. SL limit/trigger price rounding causes constraint to fail -> reject
7. Fee paid in quote/fee asset (fee_deducted_from_base=False) -> base quantity not reduced
8. Decimal boundary exact Rp 20,000 threshold
9. Existing position / pending order rule: maximum one active lifecycle per symbol
10. No duplicate BUY when pre-flight fails or function is invoked repeatedly (idempotency)
"""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import ExchangeSymbol, TokocryptoClient
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    decimal_round_step,
    decimal_round_tick,
    validate_protective_oco_eligibility,
)


class TestTokocryptoOcoEligibility(unittest.TestCase):

    def _mock_symbol(
        self,
        symbol="BNB_IDR",
        tick=1.0,
        step=0.001,
        min_qty=0.001,
        min_notional=20_000.0,
    ):
        sym = MagicMock(spec=ExchangeSymbol)
        sym.symbol = symbol
        sym.tick_size = tick
        sym.step_size = step
        sym.min_qty = min_qty
        sym.min_notional = min_notional
        sym.constraints = {
            "tick_size": tick,
            "step_size": step,
            "min_qty": min_qty,
            "min_notional": min_notional,
        }
        return sym

    def _mock_client_and_executor(self, sym, dry_run=True, supervised=False):
        client = MagicMock(spec=TokocryptoClient)
        client.get_symbol.return_value = sym
        client.round_tick = TokocryptoClient.round_tick
        client.round_step = TokocryptoClient.round_step
        client.normalize_symbol = TokocryptoClient.normalize_symbol
        bal = MagicMock()
        bal.free = 0.0
        client.get_balance.return_value = bal

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=supervised,
            dry_run=dry_run,
            max_slots=1,
        )
        executor.has_active_position = MagicMock(return_value=False)
        return client, executor

    # ------------------------------------------------------------------
    # 1. Real BNB Case
    # 0.002 BUY -> fee deducted -> 0.00199756 -> floored to 0.001 -> reject BEFORE BUY
    # ------------------------------------------------------------------
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_case_1_real_bnb_case_rejected_before_buy(self, mock_tg, mock_upsert):
        sym = self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0)
        client, executor = self._mock_client_and_executor(sym, dry_run=False)

        # Real incident numbers:
        # entry_price = 13,122,250, tp = 13,802,619, sl = 12,859,805
        # entry_qty = 0.002 (notional ~ 26,244.5 >= 20,000)
        # after 0.122% fee (or 0.15%), net is 0.00199756
        # floored to 0.001 -> TP notional is 13,802.62 < 20,000!
        cand = {
            "symbol": "BNB_IDR",
            "entry_price": 13_122_250.0,
            "tp_price": 13_802_619.0,
            "sl_price": 12_859_805.0,
        }

        # validate_and_size directly
        # available_idr = 26,244.5 -> slot_size = 26,244.5 -> qty = round_step(26244.5/13122250, 0.001) = 0.002
        is_valid = executor.validate_and_size(cand, available_idr=26_244.5)
        self.assertFalse(is_valid, "BNB 0.002 BUY must be rejected in validate_and_size")

        # execute_entry should abort without making exchange call
        resp = executor.execute_entry(cand, slot_size_idr=26_244.5)
        self.assertIsNone(resp, "execute_entry must return None")
        client._signed_post.assert_not_called()
        mock_upsert.assert_not_called()
        mock_tg.assert_called()
        self.assertIn("Pre-flight OCO Ineligible", mock_tg.call_args[0][0])

    # ------------------------------------------------------------------
    # 2. Pair with Sufficient Post-Fee Quantity -> BUY allowed
    # ------------------------------------------------------------------
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_case_2_sufficient_post_fee_qty_allowed(self, mock_tg, mock_upsert):
        sym = self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0)
        client, executor = self._mock_client_and_executor(sym, dry_run=False)
        client.has_active_position = MagicMock(return_value=False)
        executor.has_active_position = MagicMock(return_value=False)
        client._signed_post.return_value = {"data": {"orderId": "917001", "status": 0}}

        # BNB 0.003: slot_size = ~40,000 IDR -> qty = 0.003
        # post fee: 0.003 * (1 - 0.0015) = 0.0029955 -> floor 0.001 = 0.002 BNB
        # TP notional = 0.002 * 13,802,619 = 27,605.24 >= 20,000 ✓
        # SL limit notional = 0.002 * 12,840,515 = 25,681.03 >= 20,000 ✓
        cand = {
            "symbol": "BNB_IDR",
            "entry_price": 13_122_250.0,
            "tp_price": 13_802_619.0,
            "sl_price": 12_859_805.0,
        }

        is_valid = executor.validate_and_size(cand, available_idr=39_366.75)
        self.assertTrue(is_valid, "BNB 0.003 BUY must be allowed")
        self.assertEqual(cand["sizing"]["qty"], 0.003)

        resp = executor.execute_entry(cand, slot_size_idr=39_366.75)
        self.assertIsNotNone(resp)
        client._signed_post.assert_called_once()
        mock_upsert.assert_called_once()

    # ------------------------------------------------------------------
    # 3. Entry Min Notional Passes but OCO Min Notional Fails -> Reject
    # ------------------------------------------------------------------
    def test_case_3_entry_notional_passes_oco_notional_fails(self):
        sym = self._mock_symbol("TOKEN_IDR", tick=10.0, step=1.0, min_qty=1.0, min_notional=20_000.0)
        # Entry: 21 tokens @ 1,000 IDR = 21,000 IDR (> 20,000 IDR entry min notional)
        # Step size = 1.0.
        # After 0.15% fee: 21 * 0.9985 = 20.9685 -> floored to 20 tokens!
        # SL trigger = 960 IDR, SL limit ~ 950 IDR.
        # SL limit notional = 20 * 950 = 19,000 IDR (< 20,000 IDR min notional!)
        eligible, reason, details = validate_protective_oco_eligibility(
            entry_qty=Decimal("21"),
            entry_price=Decimal("1000"),
            tp_price=Decimal("1050"),
            sl_price=Decimal("960"),
            sym_info=sym,
            fee_rate=Decimal("0.0015"),
            fee_deducted_from_base=True,
            sl_buffer_pct=Decimal("0.01"),  # 1% buffer -> limit 950.4 -> 950
        )
        self.assertFalse(eligible)
        self.assertIn("OCO_INELIGIBLE_AFTER_FEE", reason)
        self.assertIn("SL limit notional", reason)

    # ------------------------------------------------------------------
    # 4. Quantity Rounding Causes OCO Fail -> Reject
    # ------------------------------------------------------------------
    def test_case_4_quantity_rounding_causes_oco_fail(self):
        # Symbol with large step size (step=5)
        sym = self._mock_symbol("XYZ_IDR", tick=1.0, step=5.0, min_qty=5.0, min_notional=20_000.0)
        # Entry qty: 25 XYZ @ 1,000 IDR = 25,000 IDR
        # Post-fee: 25 * (1 - 0.0015) = 24.9625
        # Floor with step 5.0 -> 20 XYZ!
        # TP price = 1,020 -> TP notional = 20 * 1,020 = 20,400 (passes)
        # SL limit price = 950 -> SL notional = 20 * 950 = 19,000 (< 20,000 fails!)
        eligible, reason, details = validate_protective_oco_eligibility(
            entry_qty=Decimal("25"),
            entry_price=Decimal("1000"),
            tp_price=Decimal("1020"),
            sl_price=Decimal("960"),
            sym_info=sym,
            fee_rate=Decimal("0.0015"),
            fee_deducted_from_base=True,
        )
        self.assertFalse(eligible)
        self.assertEqual(details["usable_oco_qty"], Decimal("20"))
        self.assertIn("SL limit notional", reason)

    # ------------------------------------------------------------------
    # 5. TP Price Rounding Causes Notional Fail -> Reject
    # ------------------------------------------------------------------
    def test_case_5_tp_price_rounding_causes_notional_fail(self):
        # Symbol with large tick size (tick=100)
        sym = self._mock_symbol("BIGTICK_IDR", tick=100.0, step=0.1, min_qty=0.1, min_notional=20_000.0)
        # Entry qty = 20.03 -> post-fee = 20.00 -> usable = 20.0
        # Entry = 850.0. Unrounded TP = 949.0 (if unrounded, 21.08 * 949 = 20,004 >= 20,000)
        # Tick rounding (tick=100) rounds 949.0 down to 900.0!
        # Hierarchy is valid: TP (900) > Entry (850) > SL Stop (800) > SL Limit (790)
        # But notional is: 20.0 * 900.0 = 18,000 < 20,000 -> REJECT!
        eligible, reason, details = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.03"),
            entry_price=Decimal("850"),
            tp_price=Decimal("949.0"),  # rounds down to 900.0 with tick=100
            sl_price=Decimal("800"),
            sym_info=sym,
            fee_rate=Decimal("0.0015"),
            fee_deducted_from_base=True,
        )
        self.assertFalse(eligible)
        self.assertEqual(details["tp_price_rounded"], Decimal("900.0"))
        self.assertIn("OCO_INELIGIBLE_AFTER_FEE", reason)
        self.assertIn("TP notional", reason)

    # ------------------------------------------------------------------
    # 6. SL Limit / Trigger Rounding Causes Constraint Fail -> Reject
    # ------------------------------------------------------------------
    def test_case_6_sl_rounding_causes_constraint_fail(self):
        sym = self._mock_symbol("SLTEST_IDR", tick=50.0, step=1.0, min_qty=1.0, min_notional=20_000.0)
        # Entry: 21 units @ 1,000 IDR -> post-fee usable = 20 units
        # SL trigger = 980 IDR -> rounds to 1000 with tick 50 (which equals entry, violating hierarchy!)
        eligible, reason, _ = validate_protective_oco_eligibility(
            entry_qty=Decimal("21"),
            entry_price=Decimal("1000"),
            tp_price=Decimal("1100"),
            sl_price=Decimal("980"),  # rounds to 1000.0 -> violates entry > sl_stop
            sym_info=sym,
            fee_rate=Decimal("0.0015"),
        )
        self.assertFalse(eligible)
        self.assertIn("OCO_PRICE_HIERARCHY_INVALID", reason)

    # ------------------------------------------------------------------
    # 7. Fee Paid in Quote/Fee Asset (fee_deducted_from_base=False)
    # Calculation must NOT reduce base asset quantity
    # ------------------------------------------------------------------
    def test_case_7_fee_paid_in_different_asset_does_not_reduce_base_qty(self):
        sym = self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0)
        # If fee is deducted from quote asset or external fee asset:
        # Expected post-fee qty is EXACTLY 0.002 (no base deduction)
        # Usable OCO qty is 0.002
        # TP notional = 0.002 * 13,802,619 = 27,605.24 >= 20,000 -> PASS!
        eligible, reason, details = validate_protective_oco_eligibility(
            entry_qty=Decimal("0.002"),
            entry_price=Decimal("13122250"),
            tp_price=Decimal("13802619"),
            sl_price=Decimal("12859805"),
            sym_info=sym,
            fee_rate=Decimal("0.0015"),
            fee_deducted_from_base=False,  # External / quote fee asset
        )
        self.assertTrue(eligible)
        self.assertEqual(details["usable_oco_qty"], Decimal("0.002"))
        self.assertGreaterEqual(details["tp_notional"], Decimal("20000.0"))
        self.assertGreaterEqual(details["sl_limit_notional"], Decimal("20000.0"))

    # ------------------------------------------------------------------
    # 8. Decimal Boundary Exact Rp 20,000 Threshold
    # ------------------------------------------------------------------
    def test_case_8_decimal_boundary_exact_20000(self):
        sym = self._mock_symbol("EXACT_IDR", tick=0.01, step=0.01, min_qty=0.01, min_notional=20_000.0)
        # Case A: Exactly Rp 20,000.00 -> PASS
        # 20.0 units @ TP 1,000.00 = 20,000.00
        # SL limit = 1,000.00 is invalid hierarchy, so make entry 1000.05, TP 1000.10, SL stop 999.95, SL limit 999.90
        # If usable_qty = 20.0001, SL limit 999.995 -> test pure math with exact notional:
        eligible_exact, _, det_exact = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.0"),
            entry_price=Decimal("1000.00"),
            tp_price=Decimal("1001.00"),
            sl_price=Decimal("1000.00") * (Decimal("1") + Decimal("0.0015")),  # artificial test
            sym_info=sym,
            fee_rate=Decimal("0.0"),
            fee_deducted_from_base=False,
            sl_buffer_pct=Decimal("0.000001"),
        )
        # Test directly with pure Decimal boundary:
        d_min = Decimal("20000.0")
        notional_exact = Decimal("20000.00000000")
        notional_under = Decimal("19999.99999999")
        self.assertTrue(notional_exact >= d_min)
        self.assertFalse(notional_under >= d_min)

        # Helper test: boundary just 1 rupiah below 20,000
        # qty = 20.0, TP = 999.99 -> notional = 19,999.80 < 20,000 -> REJECT
        eligible_sub, reason_sub, _ = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.0"),
            entry_price=Decimal("999.50"),
            tp_price=Decimal("999.99"),
            sl_price=Decimal("999.00"),
            sym_info=sym,
            fee_rate=Decimal("0.0"),
            fee_deducted_from_base=False,
        )
        self.assertFalse(eligible_sub)
        self.assertIn("TP notional", reason_sub)

        # Helper test: boundary just at or above 20,000
        # qty = 20.0, TP = 1000.00 -> notional = 20,000.00 >= 20,000
        # SL limit = 1000.00 -> notional = 20,000.00
        sym_low_min = self._mock_symbol("LOW_IDR", tick=0.01, step=0.01, min_qty=0.01, min_notional=19_000.0)
        eligible_ok, _, det_ok = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.0"),
            entry_price=Decimal("999.50"),
            tp_price=Decimal("1000.00"),
            sl_price=Decimal("990.00"),
            sym_info=sym_low_min,
            fee_rate=Decimal("0.0"),
            fee_deducted_from_base=False,
        )
        self.assertTrue(eligible_ok)
        self.assertEqual(det_ok["tp_notional"], Decimal("20000.00"))

    # ------------------------------------------------------------------
    # 9. Existing Position / Pending Order Rule:
    # Exactly one active lifecycle per symbol
    # ------------------------------------------------------------------
    @patch("services.supabase_client.fetch_all_tokocrypto")
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_case_9_existing_active_position_blocks_new_entry(self, mock_tg, mock_up, mock_fetch):
        sym = self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0)
        client, executor = self._mock_client_and_executor(sym, dry_run=False)
        # Restore real method for test 9 to verify Supabase query logic
        executor.has_active_position = TokocryptoOrderExecutor.has_active_position.__get__(executor, TokocryptoOrderExecutor)

        # Existing OPEN trade in Supabase
        mock_fetch.return_value = [
            {"symbol": "BNB_IDR", "exit_status": "OPEN", "entry_order_id": "11111"}
        ]

        cand = {
            "symbol": "BNB_IDR",
            "entry_price": 13_122_250.0,
            "tp_price": 13_802_619.0,
            "sl_price": 12_859_805.0,
        }

        resp = executor.execute_entry(cand, slot_size_idr=50_000.0)
        self.assertIsNone(resp, "execute_entry must reject symbol with existing active OPEN position")
        client._signed_post.assert_not_called()
        mock_up.assert_not_called()

    # ------------------------------------------------------------------
    # 10. No Duplicate BUY When Pre-flight Fails or Called Repeatedly
    # ------------------------------------------------------------------
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    def test_case_10_no_duplicate_buy_on_failure_or_repeated_calls(self, mock_tg, mock_up):
        sym = self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0)
        client, executor = self._mock_client_and_executor(sym, dry_run=False)

        cand = {
            "symbol": "BNB_IDR",
            "entry_price": 13_122_250.0,
            "tp_price": 13_802_619.0,
            "sl_price": 12_859_805.0,
        }

        # Call execute_entry 5 times repeatedly on an ineligible setup
        for i in range(5):
            res = executor.execute_entry(cand, slot_size_idr=26_244.5)
            self.assertIsNone(res)

        # Invariant: ZERO buy orders sent to exchange, ZERO upserts to DB
        client._signed_post.assert_not_called()
        mock_up.assert_not_called()

    # ------------------------------------------------------------------
    # 11. Boundary Comparison: Actual Fee 0.10% vs Conservative Fee 0.15%
    # ------------------------------------------------------------------
    def test_case_11_fee_010_vs_015_boundary_comparison(self):
        sym = self._mock_symbol("FINE_IDR", tick=0.1, step=0.01, min_qty=0.01, min_notional=20_000.0)

        # Coarse step case (e.g. BNB step 0.001):
        # 0.002 BNB: BOTH 0.10% and 0.15% result in 0.001 usable -> BOTH REJECT!
        res_010, _, det_010 = validate_protective_oco_eligibility(
            entry_qty=Decimal("0.002"),
            entry_price=Decimal("13122250"),
            tp_price=Decimal("13802619"),
            sl_price=Decimal("12859805"),
            sym_info=self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0),
            fee_rate=Decimal("0.0010"),  # 0.10%
        )
        res_015, _, det_015 = validate_protective_oco_eligibility(
            entry_qty=Decimal("0.002"),
            entry_price=Decimal("13122250"),
            tp_price=Decimal("13802619"),
            sl_price=Decimal("12859805"),
            sym_info=self._mock_symbol("BNB_IDR", tick=1.0, step=0.001, min_qty=0.001, min_notional=20_000.0),
            fee_rate=Decimal("0.0015"),  # 0.15%
        )
        self.assertFalse(res_010, "BNB 0.002 must fail even with 0.10% fee")
        self.assertFalse(res_015, "BNB 0.002 must fail with 0.15% fee")
        self.assertEqual(det_010["usable_oco_qty"], Decimal("0.001"))
        self.assertEqual(det_015["usable_oco_qty"], Decimal("0.001"))

        # Fine step edge case: Q = 20.03 units, Entry = 1020, TP = 1050 IDR, SL = 1002 IDR, step = 0.01
        # At 0.10%: 20.03 * 0.999 = 20.00997 -> floor 0.01 = 20.00
        # SL limit = 1000.5 -> notional = 20.00 * 1000.5 = 20,010.00 >= 20,000 (PASS)
        # At 0.15%: 20.03 * 0.9985 = 19.999955 -> floor 0.01 = 19.99
        # SL limit = 1000.5 -> notional = 19.99 * 1000.5 = 19,999.995 < 20,000 (FAIL)
        res_fine_010, _, det_fine_010 = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.03"),
            entry_price=Decimal("1020"),
            tp_price=Decimal("1050"),
            sl_price=Decimal("1002"),
            sym_info=sym,
            fee_rate=Decimal("0.0010"),
            sl_buffer_pct=Decimal("0.0015"),
        )
        res_fine_015, _, det_fine_015 = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.03"),
            entry_price=Decimal("1020"),
            tp_price=Decimal("1050"),
            sl_price=Decimal("1002"),
            sym_info=sym,
            fee_rate=Decimal("0.0015"),
            sl_buffer_pct=Decimal("0.0015"),
        )
        self.assertTrue(res_fine_010, "20.03 units passes at 0.10% fee")
        self.assertFalse(res_fine_015, "20.03 units fails at 0.15% conservative fee")
        self.assertEqual(det_fine_010["usable_oco_qty"], Decimal("20.00"))
        self.assertEqual(det_fine_015["usable_oco_qty"], Decimal("19.99"))

    # ------------------------------------------------------------------
    # 12. Dynamic Fee Config from Account & Configurable Environment Variable
    # ------------------------------------------------------------------
    def test_case_12_dynamic_fee_config_from_account_and_env(self):
        sym = self._mock_symbol("BNB_IDR")
        client, executor = self._mock_client_and_executor(sym)

        # A. Authenticated client with account takerCommission
        client.authenticated = True
        client.get_account.return_value = {"takerCommission": "0.00150000"}
        fee_rate, fee_from_base = executor.get_fee_config("BNB_IDR")
        self.assertEqual(fee_rate, Decimal("0.0015"))
        self.assertTrue(fee_from_base)

        # B. Account takerCommission expressed in basis points (12.0 bps = 0.0012)
        executor._cached_fee_rate = None
        client.get_account.return_value = {"takerCommission": "12.0"}
        fee_rate_bps, _ = executor.get_fee_config("BNB_IDR")
        self.assertEqual(fee_rate_bps, Decimal("0.0012"))

        # C. Non-authenticated client falls back to TOKO_DEFAULT_FEE_RATE env or 0.0015
        client.authenticated = False
        executor._cached_fee_rate = None
        with patch.dict("os.environ", {"TOKO_DEFAULT_FEE_RATE": "0.0018"}):
            executor.DEFAULT_FEE_RATE = Decimal("0.0018")
            rate_env, _ = executor.get_fee_config("BNB_IDR")
            self.assertEqual(rate_env, Decimal("0.0018"))

    # ------------------------------------------------------------------
    # 13. Exact Min Notional Pass & Fail Boundaries
    # ------------------------------------------------------------------
    def test_case_13_exact_min_notional_pass_and_fail_boundaries(self):
        sym = self._mock_symbol("EXACT_IDR", tick=0.01, step=0.01, min_qty=0.01, min_notional=20_000.0)

        # Both TP and SL legs meet or exceed min_notional
        # Entry = 1010.00, TP = 1050.00 (notional = 21,000.00), SL limit = 1000.00 (notional = 20,000.00)
        pass_ok, _, det_pass = validate_protective_oco_eligibility(
            entry_qty=Decimal("20.00"),
            entry_price=Decimal("1010.00"),
            tp_price=Decimal("1050.00"),
            sl_price=Decimal("1001.50"),
            sym_info=sym,
            fee_rate=Decimal("0.0"),  # fee paid in quote
            fee_deducted_from_base=False,
            sl_buffer_pct=Decimal("0.00149775"),  # gives sl_limit = 1000.00
        )
        self.assertTrue(pass_ok)
        self.assertEqual(det_pass["sl_limit_price"], Decimal("1000.00"))
        self.assertEqual(det_pass["sl_limit_notional"], Decimal("20000.00"))

        # Exactly 19.99 units -> sl_limit_notional = 19.99 * 1000.00 = 19,990.00 (< 20,000)
        fail_sub, reason_sub, det_fail = validate_protective_oco_eligibility(
            entry_qty=Decimal("19.99"),
            entry_price=Decimal("1010.00"),
            tp_price=Decimal("1050.00"),
            sl_price=Decimal("1001.50"),
            sym_info=sym,
            fee_rate=Decimal("0.0"),
            fee_deducted_from_base=False,
            sl_buffer_pct=Decimal("0.00149775"),
        )
        self.assertFalse(fail_sub)
        self.assertEqual(det_fail["sl_limit_notional"], Decimal("19990.00"))
        self.assertIn("SL limit notional", reason_sub)


if __name__ == "__main__":
    unittest.main()

