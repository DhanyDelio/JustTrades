"""
test_tokocrypto_executor.py — Unit tests for TokocryptoOrderExecutor.

All tests use unittest.mock — zero real API or Supabase calls.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor, _confirm
from core.clients.tokocrypto_client import (
    TokocryptoClient,
    ExchangeSymbol,
    TokocryptoAPIError,
    TokocryptoNetworkError,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _mock_client(tick=100.0, step=0.001, min_qty=0.001, min_notional=10_000.0):
    """Build a MagicMock TokocryptoClient with sensible defaults."""
    client = MagicMock(spec=TokocryptoClient)

    sym = MagicMock(spec=ExchangeSymbol)
    sym.tick_size    = tick
    sym.step_size    = step
    sym.min_qty      = min_qty
    sym.min_notional = min_notional
    sym.oco_enabled  = True
    sym.constraints  = {
        "tick_size":    tick,
        "step_size":    step,
        "min_qty":      min_qty,
        "min_notional": min_notional,
    }

    client.get_symbol.return_value = sym
    # Wire static methods from real class
    client.round_tick = TokocryptoClient.round_tick
    client.round_step = TokocryptoClient.round_step
    client.normalize_symbol = TokocryptoClient.normalize_symbol
    balance = MagicMock()
    balance.free = 0.0
    client.get_balance.return_value = balance
    return client, sym


def _make_executor(supervised=False, dry_run=True):
    client, sym = _mock_client()
    executor = TokocryptoOrderExecutor(client, supervised=supervised, dry_run=dry_run)
    return executor, client, sym


def _base_cand(symbol="BTC_IDR", entry_price=1_000_000.0,
               tp_price=1_030_000.0, sl_price=970_000.0):
    return {
        "symbol":      symbol,
        "entry_price": entry_price,
        "tp_price":    tp_price,
        "sl_price":    sl_price,
    }


def _base_trade(**kwargs):
    defaults = {
        "symbol":          "BTC_IDR",
        "entry_order_id":  "12345",
        "tp_order_id":     "11111",
        "sl_order_id":     "22222",
        "b_order_list_id": "9999",
        "entry_price":     1_000_000.0,
        "tp_price":        1_030_000.0,
        "sl_price":        970_000.0,
        "entry_qty":       0.001,
        "entry_fill_price": 1_000_000.0,
        "entry_notional_idr": 1_000.0,
    }
    defaults.update(kwargs)
    return defaults


# ===========================================================================
# A. validate_and_size
# ===========================================================================

class TestValidateAndSize(unittest.TestCase):

    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    def test_valid_case(self, mock_upsert, mock_tg):
        """Happy path: qty computed, cand['sizing'] set, returns True."""
        executor, client, sym = _make_executor()
        cand = _base_cand()
        # available_idr = 10 slots × 100_000 = 1_000_000 IDR
        # slot_size = 100_000 IDR
        # qty = 100_000 / 1_000_000 = 0.1 → round_step(0.1, 0.001) = 0.1
        # notional = 0.1 × 1_000_000 = 100_000 ≥ 10_000 ✓
        result = executor.validate_and_size(cand, available_idr=1_000_000.0)
        self.assertTrue(result)
        self.assertIn("sizing", cand)
        self.assertGreater(cand["sizing"]["qty"], 0)
        self.assertIn("constraints", cand)

    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    def test_below_min_notional(self, mock_upsert, mock_tg):
        """qty × price < min_notional → returns False."""
        executor, client, sym = _make_executor()
        sym.min_notional = 10_000.0
        # entry_price = 1_000_000, slot_size_idr = 1_000 → qty=0.001
        # notional = 0.001 × 1_000_000 = 1_000 < 10_000 → False
        cand = _base_cand(entry_price=1_000_000.0)
        result = executor.validate_and_size(cand, available_idr=10_000.0)
        self.assertFalse(result)

    @patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
    @patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
    def test_zero_qty_after_rounding(self, mock_upsert, mock_tg):
        """Very high entry_price → qty rounds to 0 → returns False."""
        executor, client, sym = _make_executor()
        sym.step_size = 0.001
        sym.min_qty   = 0.001
        # slot = 10 IDR / 10 slots = 1 IDR; qty = 1 / 1_000_000_000 → 0.0
        cand = _base_cand(entry_price=1_000_000_000.0)
        result = executor.validate_and_size(cand, available_idr=10.0)
        self.assertFalse(result)


# ===========================================================================
# B. build_entry_payload
# ===========================================================================

class TestBuildEntryPayload(unittest.TestCase):

    def test_field_mapping(self):
        """side=0, type=1, timeInForce=1, quantity/price are numeric."""
        executor, client, sym = _make_executor()
        cand = _base_cand()
        cand["sizing"]      = {"qty": 0.1, "slot_size_idr": 100_000.0}
        cand["constraints"] = {"tick_size": 100.0, "step_size": 0.001,
                                "min_qty": 0.001, "min_notional": 10_000.0}

        payload = executor.build_entry_payload(cand)

        self.assertEqual(payload["side"], 0)
        self.assertEqual(payload["type"], 1)
        self.assertEqual(payload["timeInForce"], 1)
        self.assertIsInstance(payload["quantity"], float)
        self.assertIsInstance(payload["price"], float)
        self.assertEqual(payload["symbol"], "BTC_IDR")


# ===========================================================================
# C. query_oco_state — all state machine branches
# ===========================================================================

PATCHES = [
    "core.clients.tokocrypto_order_executor._send_toko_telegram",
    "core.clients.tokocrypto_order_executor.upsert_tokocrypto",
    "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id",
]


def _order_detail(status: int, executed_price: float = 0.0) -> dict:
    return {"status": status, "executedPrice": executed_price, "price": 0.0}


@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestQueryOcoState(unittest.TestCase):

    def _executor_with_orders(self, tp_status, sl_status,
                               tp_exec_price=0.0, sl_exec_price=0.0):
        executor, client, sym = _make_executor()
        client.get_order_detail.side_effect = [
            _order_detail(tp_status, tp_exec_price),
            _order_detail(sl_status, sl_exec_price),
        ]
        return executor, client

    # (0, 0) → EXECUTING
    def test_executing_0_0(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(0, 0)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "EXECUTING")

    # (2, 3) → TP_HIT, exit_price from executedPrice
    def test_tp_hit_2_3(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(2, 3, tp_exec_price=1_032_000.0)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "TP_HIT")
        self.assertAlmostEqual(result["exit_price"], 1_032_000.0)

    # (3, 2) → SL_HIT, exit_price from executedPrice
    def test_sl_hit_3_2(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(3, 2, sl_exec_price=968_000.0)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "SL_HIT")
        self.assertAlmostEqual(result["exit_price"], 968_000.0)

    # Tokocrypto expires the OCO sibling after the filled exit leg.
    def test_tp_hit_2_6(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(2, 6, tp_exec_price=1_032_000.0)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "TP_HIT")
        self.assertAlmostEqual(result["exit_price"], 1_032_000.0)

    def test_sl_hit_6_2(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(6, 2, sl_exec_price=968_000.0)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "SL_HIT")
        self.assertAlmostEqual(result["exit_price"], 968_000.0)

    # (2, 2) → CRITICAL_ANOMALY
    def test_critical_anomaly_2_2(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(2, 2)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "CRITICAL_ANOMALY")

    # (3, 3) → BOTH_CANCELED_ANOMALY
    def test_both_canceled_3_3(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(3, 3)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "BOTH_CANCELED_ANOMALY")

    # (6, 0) → TP_EXPIRED_PENDING
    def test_tp_expired_6_0(self, mock_tg, mock_up, mock_upd):
        executor, client = self._executor_with_orders(6, 0)
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "TP_EXPIRED_PENDING")

    # get_order_detail raises → RECONCILIATION_REQUIRED
    def test_error_raises_reconciliation(self, mock_tg, mock_up, mock_upd):
        executor, client, sym = _make_executor()
        client.get_order_detail.side_effect = TokocryptoNetworkError("timeout")
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")


# ===========================================================================
# D. place_oco
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestPlaceOco(unittest.TestCase):

    def test_tp_le_ref_price_returns_none(self, mock_tg, mock_up, mock_upd):
        """OCO constraint violated (tp <= ref_price) → returns None, no exchange call."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        # tp_price=1_030_000 but ref/ticker = 1_050_000 (above tp)
        client.get_ticker.return_value = 1_050_000.0
        trade = _base_trade(tp_price=1_030_000.0, sl_price=970_000.0)
        result = executor.place_oco(trade)
        self.assertIsNone(result)
        client._signed_post.assert_not_called()

    def test_supervised_n_aborts(self, mock_tg, mock_up, mock_upd):
        """supervised=True, _confirm returns False → returns None, no _signed_post."""
        executor, client, sym = _make_executor(supervised=True, dry_run=False)
        client.get_ticker.return_value = 1_000_000.0  # ref inside tp/sl spread
        trade = _base_trade(tp_price=1_030_000.0, sl_price=970_000.0)
        with patch("core.clients.tokocrypto_order_executor._confirm", return_value=False):
            result = executor.place_oco(trade)
        self.assertIsNone(result)
        client._signed_post.assert_not_called()

    def test_place_oco_borderlistid_at_root(self, mock_tg, mock_up, mock_upd):
        """bOrderListId at root of response is parsed and saved as EXECUTING."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        client.get_ticker.return_value = 1_000_000.0
        client._signed_post.return_value = {
            "code": 0,
            "msg": "success",
            "data": {
                "bOrderListId": "25208289734",
                "orders": [
                    {"orderId": "917549739"},
                    {"orderId": "917549740"},
                ],
            },
        }
        trade = _base_trade(entry_order_id="9999", tp_price=1_030_000.0, sl_price=970_000.0)
        resp = executor.place_oco(trade)
        self.assertIsNotNone(resp)
        mock_upd.assert_called_once()
        call_entry_oid, payload = mock_upd.call_args[0]
        self.assertEqual(call_entry_oid, "9999")
        self.assertEqual(payload["b_order_list_id"], "25208289734")
        self.assertEqual(payload["tp_order_id"], "917549739")
        self.assertEqual(payload["sl_order_id"], "917549740")
        self.assertEqual(payload["oco_state"], "EXECUTING")
        mock_tg.assert_called_once()

    def test_place_oco_borderlistid_in_child_orders(self, mock_tg, mock_up, mock_upd):
        """bOrderListId absent at root but present in orders[0] is successfully parsed."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        client.get_ticker.return_value = 1_000_000.0
        client._signed_post.return_value = {
            "code": 0,
            "msg": "success",
            "data": {
                "orders": [
                    {"orderId": "917549756", "bOrderListId": "25208291196"},
                    {"orderId": "917549757", "bOrderListId": "25208291196"},
                ],
            },
        }
        trade = _base_trade(entry_order_id="8888", tp_price=1_030_000.0, sl_price=970_000.0)
        resp = executor.place_oco(trade)
        self.assertIsNotNone(resp)
        mock_upd.assert_called_once()
        call_entry_oid, payload = mock_upd.call_args[0]
        self.assertEqual(call_entry_oid, "8888")
        self.assertEqual(payload["b_order_list_id"], "25208291196")
        self.assertEqual(payload["tp_order_id"], "917549756")
        self.assertEqual(payload["sl_order_id"], "917549757")
        self.assertEqual(payload["oco_state"], "EXECUTING")
        mock_tg.assert_called_once()

    def test_place_oco_orderlistid_fallback(self, mock_tg, mock_up, mock_upd):
        """orderListId naming fallback is supported both at root and inside orders."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        client.get_ticker.return_value = 1_000_000.0
        client._signed_post.return_value = {
            "orderListId": "777888",
            "orders": [
                {"orderId": "111"},
                {"orderId": "222"},
            ],
        }
        trade = _base_trade(entry_order_id="7777", tp_price=1_030_000.0, sl_price=970_000.0)
        resp = executor.place_oco(trade)
        self.assertIsNotNone(resp)
        _, payload = mock_upd.call_args[0]
        self.assertEqual(payload["b_order_list_id"], "777888")
        self.assertEqual(payload["oco_state"], "EXECUTING")

    def test_place_oco_missing_list_id_still_sets_executing(self, mock_tg, mock_up, mock_upd):
        """Missing list id gracefully defaults to empty string without raising, state is EXECUTING."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        client.get_ticker.return_value = 1_000_000.0
        client._signed_post.return_value = {
            "orders": [
                {"orderId": "111"},
                {"orderId": "222"},
            ],
        }
        trade = _base_trade(entry_order_id="6666", tp_price=1_030_000.0, sl_price=970_000.0)
        resp = executor.place_oco(trade)
        self.assertIsNotNone(resp)
        _, payload = mock_upd.call_args[0]
        self.assertEqual(payload["b_order_list_id"], "")
        self.assertEqual(payload["tp_order_id"], "111")
        self.assertEqual(payload["sl_order_id"], "222")
        self.assertEqual(payload["oco_state"], "EXECUTING")


# ===========================================================================
# E. cancel_order
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestCancelOrder(unittest.TestCase):

    def test_confirmed_cancel_returns_true(self, mock_tg, mock_up, mock_upd):
        """_signed_post returns status CANCELED (int 3) → returns True."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        client._signed_post.return_value = {"data": {"status": 3, "orderId": "55555"}}
        result = executor.cancel_order("BTC_IDR", "55555")
        self.assertTrue(result)
        client._signed_post.assert_called_once()

    def test_non_canceled_status_returns_false(self, mock_tg, mock_up, mock_upd):
        """Response status != CANCELED → returns False."""
        executor, client, sym = _make_executor(supervised=False, dry_run=False)
        client._signed_post.return_value = {"data": {"status": 0, "orderId": "55555"}}
        result = executor.cancel_order("BTC_IDR", "55555")
        self.assertFalse(result)


# ===========================================================================
# F. PnL from executedPrice, not ticker
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestPnlFromExecutedPrice(unittest.TestCase):

    def test_exit_price_from_executed_price_not_ticker(self, mock_tg, mock_up, mock_upd):
        """
        query_oco_state must populate exit_price from raw_sl['executedPrice'],
        never from client.get_ticker.
        """
        executor, client, sym = _make_executor()
        ticker_price   = 980_000.0   # ticker — must NOT be used
        executed_price = 968_000.0   # executedPrice in order detail — must be used

        client.get_ticker.return_value = ticker_price
        client.get_order_detail.side_effect = [
            _order_detail(3, 0.0),              # TP: CANCELED
            _order_detail(2, executed_price),   # SL: FILLED
        ]

        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "SL_HIT")
        self.assertAlmostEqual(result["exit_price"], executed_price)
        # get_ticker must never have been called for PnL purposes
        client.get_ticker.assert_not_called()


# ===========================================================================
# G. Slippage flag
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestSlippageFlag(unittest.TestCase):

    def test_tp_slippage_flag_at_0_15_pct(self, mock_tg, mock_up, mock_upd):
        """TP exit 0.15% off tp_price → slippage_flagged=True (threshold 0.1%)."""
        executor, client, sym = _make_executor()
        tp_price     = 1_030_000.0
        # 0.15% above tp_price
        exit_price   = tp_price * (1 + 0.0015)

        client.get_order_detail.side_effect = [
            _order_detail(2, exit_price),  # TP: FILLED
            _order_detail(3, 0.0),         # SL: CANCELED
        ]
        trade = _base_trade(tp_price=tp_price, sl_price=970_000.0)
        result = executor.query_oco_state(trade)
        self.assertEqual(result["state"], "TP_HIT")
        self.assertTrue(result["slippage_flagged"])

    def test_tp_no_slippage_flag_at_0_05_pct(self, mock_tg, mock_up, mock_upd):
        """TP exit 0.05% off tp_price → slippage_flagged=False (below 0.1% threshold)."""
        executor, client, sym = _make_executor()
        tp_price  = 1_030_000.0
        exit_price = tp_price * (1 + 0.0005)

        client.get_order_detail.side_effect = [
            _order_detail(2, exit_price),
            _order_detail(3, 0.0),
        ]
        trade = _base_trade(tp_price=tp_price)
        result = executor.query_oco_state(trade)
        self.assertEqual(result["state"], "TP_HIT")
        self.assertFalse(result["slippage_flagged"])


# ===========================================================================
# H. supervised=True — 'n' aborts execute_entry
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestSupervisedEntry(unittest.TestCase):

    def test_supervised_n_aborts_execute_entry(self, mock_tg, mock_up, mock_upd):
        """supervised=True, _confirm patched False → execute_entry returns None."""
        executor, client, sym = _make_executor(supervised=True, dry_run=False)
        cand = _base_cand()
        with patch("core.clients.tokocrypto_order_executor._confirm", return_value=False):
            result = executor.execute_entry(cand, slot_size_idr=100_000.0)
        self.assertIsNone(result)
        client._signed_post.assert_not_called()

    def test_supervised_y_proceeds_to_post(self, mock_tg, mock_up, mock_upd):
        """supervised=True, _confirm patched True, dry_run=False → _signed_post called once."""
        executor, client, sym = _make_executor(supervised=True, dry_run=False)
        cand = _base_cand()
        fake_resp = {"data": {"orderId": "99999", "status": 0}}
        client._signed_post.return_value = fake_resp
        with patch("core.clients.tokocrypto_order_executor._confirm", return_value=True):
            result = executor.execute_entry(cand, slot_size_idr=100_000.0)
        self.assertIsNotNone(result)
        client._signed_post.assert_called_once()
        # Verify supervised and trading_phase written to DB — not column default
        call_kwargs = mock_up.call_args[0][0]  # first positional arg to upsert_tokocrypto
        self.assertEqual(call_kwargs["supervised"], True,
                         "supervised=True must be written to DB, not left to column default")
        self.assertIn("trading_phase", call_kwargs,
                      "trading_phase must be explicitly written to DB")

    def test_phase3_unsupervised_writes_false_to_db(self, mock_tg, mock_up, mock_upd):
        """PHASE_3 executor (supervised=False) writes supervised=False to DB row."""
        from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
        client, sym = _mock_client()
        executor = TokocryptoOrderExecutor(
            client, supervised=False, trading_phase="PHASE_3", dry_run=False
        )
        cand = _base_cand()
        fake_resp = {"data": {"orderId": "88888", "status": 0}}
        client._signed_post.return_value = fake_resp
        result = executor.execute_entry(cand, slot_size_idr=100_000.0)
        self.assertIsNotNone(result)
        call_kwargs = mock_up.call_args[0][0]
        self.assertIs(call_kwargs["supervised"], False,
                      "PHASE_3 must write supervised=False — not column default True")
        self.assertEqual(call_kwargs["trading_phase"], "PHASE_3")

    def test_phase2_supervised_writes_true_to_db(self, mock_tg, mock_up, mock_upd):
        """PHASE_2 executor (supervised=True) writes supervised=True and trading_phase=PHASE_2."""
        from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
        client, sym = _mock_client()
        executor = TokocryptoOrderExecutor(
            client, supervised=True, trading_phase="PHASE_2", dry_run=False
        )
        cand = _base_cand()
        fake_resp = {"data": {"orderId": "77777", "status": 0}}
        client._signed_post.return_value = fake_resp
        with patch("core.clients.tokocrypto_order_executor._confirm", return_value=True):
            result = executor.execute_entry(cand, slot_size_idr=100_000.0)
        self.assertIsNotNone(result)
        call_kwargs = mock_up.call_args[0][0]
        self.assertIs(call_kwargs["supervised"], True)
        self.assertEqual(call_kwargs["trading_phase"], "PHASE_2")


# ===========================================================================
# I. dry_run=True — no API calls
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestDryRun(unittest.TestCase):

    def test_dry_run_no_api_calls(self, mock_tg, mock_up, mock_upd):
        """dry_run=True on execute_entry → _signed_post never called; returns fake dict."""
        executor, client, sym = _make_executor(supervised=False, dry_run=True)
        cand = _base_cand()
        result = executor.execute_entry(cand, slot_size_idr=100_000.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.get("_dry_run"))
        client._signed_post.assert_not_called()


# ===========================================================================
# Additional edge cases
# ===========================================================================

@patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
@patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
@patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
class TestEdgeCases(unittest.TestCase):

    def test_cancel_supervised_n_aborts(self, mock_tg, mock_up, mock_upd):
        """supervised=True, _confirm returns False → cancel_order returns False."""
        executor, client, sym = _make_executor(supervised=True, dry_run=False)
        with patch("core.clients.tokocrypto_order_executor._confirm", return_value=False):
            result = executor.cancel_order("BTC_IDR", "55555")
        self.assertFalse(result)
        client._signed_post.assert_not_called()

    def test_sl_expired_state(self, mock_tg, mock_up, mock_upd):
        """(0, 6) → SL_EXPIRED_PENDING."""
        executor, client, sym = _make_executor()
        client.get_order_detail.side_effect = [
            _order_detail(0, 0.0),   # TP: NEW
            _order_detail(6, 0.0),   # SL: EXPIRED
        ]
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "SL_EXPIRED_PENDING")

    @patch("time.sleep")
    def test_stuck_counterpart_after_requery(self, mock_sleep, mock_tg, mock_up, mock_upd):
        """(2, 0) requeried still (2, 0) → STUCK_COUNTERPART."""
        executor, client, sym = _make_executor()
        # Four calls: initial tp, initial sl, requery tp, requery sl
        client.get_order_detail.side_effect = [
            _order_detail(2, 1_032_000.0),   # tp FILLED
            _order_detail(0, 0.0),            # sl NEW
            _order_detail(2, 1_032_000.0),   # re-tp FILLED
            _order_detail(0, 0.0),            # re-sl NEW
        ]
        result = executor.query_oco_state(_base_trade())
        self.assertEqual(result["state"], "STUCK_COUNTERPART")
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep")
    def test_requery_resolves_to_tp_hit_2_3(self, mock_sleep, mock_tg, mock_up, mock_upd):
        """
        (2, 0) race window → re-query resolves (2, 3) → TP_HIT (not RECONCILIATION_REQUIRED).

        This covers finding #2 from the review: the clean-exit (2,3)/(3,2) branches
        must be checked immediately after the re-query, before the anomaly catch-all.
        """
        executor, client, sym = _make_executor()
        executed_price = 1_032_000.0
        # initial: tp FILLED, sl NEW → triggers re-query
        # re-query: tp FILLED, sl CANCELED → exchange processed cancel during 1-second window
        client.get_order_detail.side_effect = [
            _order_detail(2, executed_price),  # initial tp: FILLED
            _order_detail(0, 0.0),             # initial sl: NEW
            _order_detail(2, executed_price),  # re-query tp: FILLED
            _order_detail(3, 0.0),             # re-query sl: CANCELED
        ]
        trade = _base_trade(tp_price=1_030_000.0)
        result = executor.query_oco_state(trade)
        self.assertEqual(result["state"], "TP_HIT")
        self.assertAlmostEqual(result["exit_price"], executed_price)
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep")
    def test_requery_resolves_to_sl_hit_3_2(self, mock_sleep, mock_tg, mock_up, mock_upd):
        """
        (0, 2) race window → re-query resolves (3, 2) → SL_HIT (not RECONCILIATION_REQUIRED).

        Symmetric case to test_requery_resolves_to_tp_hit_2_3.
        """
        executor, client, sym = _make_executor()
        executed_price = 968_000.0
        # initial: tp NEW, sl FILLED → triggers re-query
        # re-query: tp CANCELED, sl FILLED → exchange processed cancel during 1-second window
        client.get_order_detail.side_effect = [
            _order_detail(0, 0.0),             # initial tp: NEW
            _order_detail(2, executed_price),  # initial sl: FILLED
            _order_detail(3, 0.0),             # re-query tp: CANCELED
            _order_detail(2, executed_price),  # re-query sl: FILLED
        ]
        trade = _base_trade(sl_price=970_000.0)
        result = executor.query_oco_state(trade)
        self.assertEqual(result["state"], "SL_HIT")
        self.assertAlmostEqual(result["exit_price"], executed_price)
        mock_sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
