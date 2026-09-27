"""
test_reconciliation_write_back.py
==================================
Tests for Part 2 fix: check_positions() Step 1 now persists
RECONCILIATION_REQUIRED to Supabase immediately when a purged
NEW/PARTIAL entry order is detected (-2013 / Order does not exist).

Required scenarios (per task spec):
  1. -2013 for a NEW order → Supabase entry_status = RECONCILIATION_REQUIRED,
     portfolio slot released, within same cycle (not just logged).
  2. Same order detected again next cycle → idempotent, no duplicate writes.
  3. -2013 for a FILLED order → existing correct behaviour unchanged
     (uses persisted fill state, proceeds to OCO check).
  4. exit_status never touched by this path.
  5. portfolio_manager excludes RECONCILIATION_REQUIRED from deployed_count.
  6. portfolio_manager excludes CANCELED from deployed_count (Part 1 fix).

All tests use mocks — zero live exchange or Supabase calls.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import MagicMock, call, patch

from binance.exceptions import BinanceAPIException

import core.paper_trade_executor as pte
from core.executors.spot_position_monitor import SpotPositionMonitor
from core.executors.spot_order_executor import SpotOrderExecutor
from core.managers.portfolio_manager import PortfolioManager
from core.repositories.spot_trade_repository import SpotTradeRepository

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_POOL = {
    "lab_capital": 100.0, "closed_cluster_pnl": 0.0,
    "deployed_capital": 0.0, "available_capital": 100.0,
    "max_new_positions": 5, "deployed_count": 0,
}


def _purge_error():
    """Simulate Binance -2013 'Order does not exist' (testnet purge)."""
    return BinanceAPIException(None, -2013, '{"code":-2013,"msg":"Order does not exist."}')


def _new_trade(**overrides):
    t = {
        "symbol":               "BTCUSDT",
        "entry_order_id":       12345,
        "entry_status":         "NEW",
        "exit_status":          "OPEN",
        "entry_price":          50000.0,
        "entry_fill_price":     None,
        "entry_fill_time":      None,
        "entry_qty":            0.0,
        "sl":                   49000.0,
        "tp1":                  52000.0,
        "direction":            "long",
        "oco_placed":           False,
        "correlation_cluster_id": "cluster_test",
        "raw_entry_order":      {},
        "open_time":            "2026-09-01T00:00:00+00:00",
    }
    t.update(overrides)
    return t


def _filled_trade(**overrides):
    t = _new_trade()
    t.update({
        "entry_status":     "FILLED",
        "entry_fill_price": 50000.0,
        "entry_fill_time":  1700000000000,
        "entry_qty":        0.1,
        "oco_placed":       True,
        "oco_list_id":      99999,
        "oco_order_ids":    [1001, 1002],
    })
    t.update(overrides)
    return t


def _run_check(monitor, trade, *, patches=None):
    """Run check_positions with standard mocks. Returns stdout text."""
    extra = patches or {}
    with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
         patch.object(pte.repo, "save_trade_log"), \
         patch("services.supabase_client.update_spot_by_order_id",
               **extra) as mock_update, \
         patch("core.executors.spot_position_monitor._send_telegram"), \
         patch("core.paper_trade_executor._send_telegram"), \
         patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
               return_value=_POOL):
        buf = io.StringIO()
        with redirect_stdout(buf):
            monitor.check_positions()
    return buf.getvalue(), mock_update


def _make_monitor(client=None, mock_update_side_effect=None):
    if client is None:
        client = MagicMock()
    executor = SpotOrderExecutor(client)
    return SpotPositionMonitor(client, pte.repo, executor), client


# ---------------------------------------------------------------------------
# Scenario 1: -2013 for NEW order → persists RECONCILIATION_REQUIRED
# ---------------------------------------------------------------------------

class TestPurgedNewOrderPersistsImmediately(unittest.TestCase):

    def test_entry_status_set_to_reconciliation_required_in_memory(self):
        """-2013 for NEW → trade entry_status = RECONCILIATION_REQUIRED in-memory."""
        trade  = _new_trade()
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]

        _run_check(monitor, trade)

        self.assertEqual(trade["entry_status"], "RECONCILIATION_REQUIRED",
                         "entry_status must be RECONCILIATION_REQUIRED after purge detection")
        print("✓ Scenario 1a: entry_status=RECONCILIATION_REQUIRED in-memory")

    def test_supabase_persisted_immediately_within_same_cycle(self):
        """-2013 for NEW → update_spot_by_order_id called with RECONCILIATION_REQUIRED."""
        trade  = _new_trade()
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]

        captured_calls = []
        def capture_update(oid, fields):
            captured_calls.append((oid, dict(fields)))

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id",
                   side_effect=capture_update), \
             patch("core.executors.spot_position_monitor._send_telegram"), \
             patch("core.paper_trade_executor._send_telegram"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
                   return_value=_POOL):
            buf = io.StringIO()
            with redirect_stdout(buf):
                monitor.check_positions()

        # At least one call must have entry_status=RECONCILIATION_REQUIRED
        recon_calls = [
            (oid, fields) for oid, fields in captured_calls
            if fields.get("entry_status") == "RECONCILIATION_REQUIRED"
        ]
        self.assertGreater(len(recon_calls), 0,
            "update_spot_by_order_id must be called with entry_status=RECONCILIATION_REQUIRED "
            "within the same cycle — not just logged or deferred")
        oid, fields = recon_calls[0]
        self.assertEqual(oid, 12345)
        self.assertIn("raw_entry_order", fields,
            "raw_entry_order must be included for audit trail")
        print(f"✓ Scenario 1b: Supabase persisted immediately (oid={oid}, "
              f"entry_status={fields['entry_status']})")

    def test_audit_trail_written_to_raw_entry_order(self):
        """-2013 for NEW → raw_entry_order gets reconciliation_required_at and reason."""
        trade  = _new_trade()
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]

        _run_check(monitor, trade)

        raw = trade.get("raw_entry_order") or {}
        self.assertIn("reconciliation_required_at", raw,
            "reconciliation_required_at must be set in raw_entry_order")
        self.assertIn("reconciliation_reason", raw,
            "reconciliation_reason must be set in raw_entry_order")
        self.assertEqual(raw["reconciliation_reason"],
                         "ENTRY_ORDER_NOT_FOUND_ON_EXCHANGE")
        print(f"✓ Scenario 1c: audit trail in raw_entry_order: "
              f"reason={raw['reconciliation_reason']}")

    def test_exit_status_never_touched(self):
        """-2013 for NEW → exit_status must remain OPEN (reserved for TP/SL)."""
        trade  = _new_trade()
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]

        _run_check(monitor, trade)

        self.assertEqual(trade["exit_status"], "OPEN",
            "exit_status must never be changed by reconciliation path — "
            "it is reserved for TP_HIT/SL_HIT completed-trade outcomes")
        print("✓ Scenario 1d: exit_status=OPEN (unchanged)")


# ---------------------------------------------------------------------------
# Scenario 2: Idempotency — same order detected again next cycle
# ---------------------------------------------------------------------------

class TestReconciliationIdempotency(unittest.TestCase):

    def test_second_cycle_no_duplicate_write(self):
        """
        Order already has entry_status=RECONCILIATION_REQUIRED from prior cycle.
        Second cycle detects same -2013 → skips Supabase write, no duplicate.
        """
        trade = _new_trade(entry_status="RECONCILIATION_REQUIRED")
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]

        captured_calls = []
        def capture_update(oid, fields):
            captured_calls.append((oid, dict(fields)))

        with patch.object(pte.repo, "load_trade_log", return_value=[trade]), \
             patch.object(pte.repo, "save_trade_log"), \
             patch("services.supabase_client.update_spot_by_order_id",
                   side_effect=capture_update), \
             patch("core.executors.spot_position_monitor._send_telegram"), \
             patch("core.paper_trade_executor._send_telegram"), \
             patch("core.managers.portfolio_manager.PortfolioManager.compute_lab_pool",
                   return_value=_POOL):
            buf = io.StringIO()
            with redirect_stdout(buf):
                monitor.check_positions()

        # No call with entry_status=RECONCILIATION_REQUIRED should be made
        recon_writes = [
            c for c in captured_calls
            if c[1].get("entry_status") == "RECONCILIATION_REQUIRED"
        ]
        self.assertEqual(len(recon_writes), 0,
            f"Second cycle must not duplicate Supabase write, "
            f"got {len(recon_writes)} write(s): {recon_writes}")
        # entry_status should still be RECONCILIATION_REQUIRED (not changed)
        self.assertEqual(trade["entry_status"], "RECONCILIATION_REQUIRED")
        print("✓ Scenario 2: idempotent — no duplicate write on second cycle")

    def test_second_cycle_entry_status_not_overwritten(self):
        """entry_status=RECONCILIATION_REQUIRED must not be overwritten to NEW."""
        trade = _new_trade(entry_status="RECONCILIATION_REQUIRED")
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]

        _run_check(monitor, trade)

        self.assertEqual(trade["entry_status"], "RECONCILIATION_REQUIRED")
        print("✓ Scenario 2b: entry_status stays RECONCILIATION_REQUIRED on repeat")


# ---------------------------------------------------------------------------
# Scenario 3: -2013 for FILLED order → existing correct behaviour unchanged
# ---------------------------------------------------------------------------

class TestPurgedFilledOrderUnchanged(unittest.TestCase):

    def test_filled_purged_uses_persisted_state_and_proceeds_to_oco(self):
        """
        -2013 for a FILLED order → uses persisted fill state (existing logic).
        entry_status must not become RECONCILIATION_REQUIRED.
        """
        trade  = _filled_trade()
        monitor, client = _make_monitor()

        # First call (get_order for entry) → -2013
        # Then v3_get_order_list for OCO → normal response
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]
        client.v3_get_order_list.return_value = {
            "listOrderStatus": "EXECUTING",
            "orders": [{"orderId": 1001}, {"orderId": 1002}],
        }

        _run_check(monitor, trade)

        # Must NOT be set to RECONCILIATION_REQUIRED
        self.assertNotEqual(trade["entry_status"], "RECONCILIATION_REQUIRED",
            "FILLED order must use persisted fill state, not become RECONCILIATION_REQUIRED")
        # entry_status should remain FILLED
        self.assertEqual(trade["entry_status"], "FILLED")
        print("✓ Scenario 3: FILLED + purged → persisted state, entry_status=FILLED")

    def test_filled_purged_exit_status_unchanged(self):
        """-2013 for FILLED → exit_status stays OPEN (OCO still active)."""
        trade  = _filled_trade()
        monitor, client = _make_monitor()
        client.get_order.side_effect = _purge_error()
        client.get_all_tickers.return_value = [{"symbol": "BTCUSDT", "price": "50000"}]
        client.v3_get_order_list.return_value = {
            "listOrderStatus": "EXECUTING", "orders": [],
        }

        _run_check(monitor, trade)

        self.assertEqual(trade["exit_status"], "OPEN")
        print("✓ Scenario 3b: FILLED + purged → exit_status=OPEN (OCO still guarding)")


# ---------------------------------------------------------------------------
# Scenario 4: exit_status never touched (explicit cross-check)
# ---------------------------------------------------------------------------

class TestExitStatusNeverTouchedByReconciliation(unittest.TestCase):

    def test_new_purged_exit_status_is_open(self):
        for initial_exit in ("OPEN",):
            with self.subTest(initial_exit=initial_exit):
                trade  = _new_trade(exit_status=initial_exit)
                monitor, client = _make_monitor()
                client.get_order.side_effect = _purge_error()
                client.get_all_tickers.return_value = [
                    {"symbol": "BTCUSDT", "price": "50000"}]
                _run_check(monitor, trade)
                self.assertEqual(trade["exit_status"], initial_exit,
                    f"exit_status must stay {initial_exit!r}, got {trade['exit_status']!r}")
        print("✓ Scenario 4: exit_status never touched by reconciliation path")


# ---------------------------------------------------------------------------
# Scenario 5: portfolio_manager excludes RECONCILIATION_REQUIRED
# ---------------------------------------------------------------------------

class TestPortfolioManagerExcludesInactiveEntries(unittest.TestCase):

    def _make_trades(self, *entry_statuses):
        return [
            {
                "symbol": f"COIN{i}USDT",
                "entry_status": es,
                "exit_status": "OPEN",
                "correlation_cluster_id": f"cluster_{i}",
                "realized_pnl_usd": None,
                "exit_price": None,
            }
            for i, es in enumerate(entry_statuses)
        ]

    def test_reconciliation_required_excluded_from_deployed_count(self):
        """entry_status=RECONCILIATION_REQUIRED → not counted as deployed."""
        trades = self._make_trades(
            "FILLED",                   # active
            "RECONCILIATION_REQUIRED",  # purged, must not count
            "NEW",                      # still pending, counts
            "CANCELED",                 # cancelled, must not count
        )
        repo = SpotTradeRepository()
        pm   = PortfolioManager(repo, 12.0, 100.0, 12.0)
        pool = pm.compute_lab_pool(trades)
        # Only FILLED + NEW should count → deployed_count = 2
        self.assertEqual(pool["deployed_count"], 2,
            f"Expected 2 deployed (FILLED+NEW), got {pool['deployed_count']}. "
            f"RECONCILIATION_REQUIRED and CANCELED must be excluded.")
        print(f"✓ Scenario 5: deployed_count=2 (RECON_REQ and CANCELED excluded)")

    def test_canceled_excluded_from_deployed_count(self):
        """entry_status=CANCELED → not counted as deployed (Part 1 fix)."""
        trades = self._make_trades("FILLED", "CANCELED", "CANCELED")
        repo = SpotTradeRepository()
        pm   = PortfolioManager(repo, 12.0, 100.0, 12.0)
        pool = pm.compute_lab_pool(trades)
        self.assertEqual(pool["deployed_count"], 1,
            f"CANCELED must be excluded, expected deployed_count=1, got {pool['deployed_count']}")
        print("✓ Scenario 5b: CANCELED excluded from deployed_count")

    def test_filled_and_new_still_count(self):
        """FILLED and NEW orders still count as deployed (no regression)."""
        trades = self._make_trades("FILLED", "NEW", "PARTIALLY_FILLED")
        repo = SpotTradeRepository()
        pm   = PortfolioManager(repo, 12.0, 100.0, 12.0)
        pool = pm.compute_lab_pool(trades)
        self.assertEqual(pool["deployed_count"], 3,
            f"FILLED/NEW/PARTIALLY_FILLED must count, expected 3, got {pool['deployed_count']}")
        print("✓ Scenario 5c: FILLED/NEW/PARTIAL still counted correctly")


# ---------------------------------------------------------------------------
# Scenario 6: CANCELED also excluded (Part 1 regression guard)
# ---------------------------------------------------------------------------

class TestCanceledNotCountedAsDeployed(unittest.TestCase):

    def test_canceled_entry_with_open_exit_not_deployed(self):
        """
        entry_status=CANCELED, exit_status=OPEN (the new state for 21 cleaned-up rows)
        must NOT be counted as deployed capital.
        """
        trades = [
            {
                "symbol": "XRPUSDT",
                "entry_status": "CANCELED",
                "exit_status": "OPEN",           # per spec: exit_status stays OPEN
                "correlation_cluster_id": "c1",
                "realized_pnl_usd": None,
                "exit_price": None,
            },
            {
                "symbol": "BTCUSDT",
                "entry_status": "FILLED",
                "exit_status": "OPEN",
                "correlation_cluster_id": "c2",
                "realized_pnl_usd": None,
                "exit_price": None,
            },
        ]
        repo = SpotTradeRepository()
        pm   = PortfolioManager(repo, 12.0, 100.0, 12.0)
        pool = pm.compute_lab_pool(trades)
        self.assertEqual(pool["deployed_count"], 1,
            "Only BTCUSDT (FILLED) should be deployed, not XRPUSDT (CANCELED/OPEN)")
        print("✓ Scenario 6: entry=CANCELED, exit=OPEN → not counted as deployed")

    def test_pre_existing_canceled_canceled_not_deployed(self):
        """
        Pre-existing rows: entry_status=CANCELED, exit_status=CANCELED.
        Also must not be deployed.
        """
        trades = [
            {
                "symbol": "OLDCOIN",
                "entry_status": "CANCELED",
                "exit_status": "CANCELED",       # pre-existing pattern
                "correlation_cluster_id": "c1",
                "realized_pnl_usd": None,
                "exit_price": None,
            },
        ]
        repo = SpotTradeRepository()
        pm   = PortfolioManager(repo, 12.0, 100.0, 12.0)
        pool = pm.compute_lab_pool(trades)
        self.assertEqual(pool["deployed_count"], 0,
            "Pre-existing CANCELED/CANCELED must not be deployed")
        print("✓ Scenario 6b: pre-existing CANCELED/CANCELED → deployed_count=0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
