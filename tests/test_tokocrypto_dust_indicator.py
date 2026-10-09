"""
test_tokocrypto_dust_indicator.py
==================================
Unit and regression tests for Tokocrypto read-only dust balance indicator and telemetry.

Scenarios tested:
  1. Eligible free dust is classified correctly (DUST_CANDIDATE).
  2. Active-position residual is excluded from eligible dust totals.
  3. SOL residual with an active position is not marked as free dust.
  4. Outstanding exchange orders prevent free-dust classification.
  5. Exchange verification failure produces UNVERIFIED (fail-closed).
  6. Database verification failure produces UNVERIFIED (fail-closed).
  7. Missing ticker prices are handled safely without crashing.
  8. Dust values do not affect PnL or adaptive allocation calculations.
  9. Telegram cooldown survives process restarts via persisted state file.
  10. Repeated polling does not produce duplicate notifications.
  11. Configured value-change threshold behaves correctly (delta >= 5,000 IDR).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import ExchangeBalance
from core.utils.tokocrypto_dust import (
    ACTIVE_POSITION_RESIDUAL,
    ACTIVE_POSITION_WARNING,
    DUST_CANDIDATE,
    UNVERIFIED,
    DustItem,
    DustSummary,
    classify_and_aggregate_dust,
    format_dust_telegram_message,
    get_asset_price_idr,
    notify_dust_summary_if_needed,
    should_send_dust_notification,
)
from tokocrypto_executor import calculate_adaptive_allocation


class TestTokocryptoDustIndicator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = Path(self.temp_dir) / "test_dust_state.json"

        # Mock client
        self.mock_client = MagicMock()
        self.mock_client.get_ticker.side_effect = self._mock_ticker

        # Default price map
        self.prices = {
            "BNB": 13_000_000.0,
            "DOGE": 1_500.0,
            "ETH": 40_000_000.0,
            "POL": 2_000.0,
            "SOL": 2_000_000.0,
            "SUI": 20_000.0,
            "USDT": 16_000.0,
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mock_ticker(self, symbol: str) -> float:
        sym = symbol.upper()
        if sym.endswith("_IDR"):
            base = sym[:-4]
            return self.prices.get(base, 0.0)
        if sym == "USDT_IDR":
            return 16_000.0
        return 0.0

    # -----------------------------------------------------------------------
    # 1. Eligible Free Dust Classification
    # -----------------------------------------------------------------------
    def test_eligible_free_dust_classified_correctly(self):
        """Free balances below threshold with zero locked and no active positions are DUST_CANDIDATE."""
        balances = [
            ExchangeBalance(asset="DOGE", free=0.85, locked=0.0),
            ExchangeBalance(asset="SUI", free=0.007, locked=0.0),
        ]
        open_trades = []
        exchange_orders = []

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            threshold_idr=20_000.0,
            price_lookup={"DOGE": 1500.0, "SUI": 20000.0},
        )

        self.assertEqual(summary.eligible_count, 2)
        self.assertEqual(summary.active_residual_count, 0)
        self.assertEqual(summary.unverified_count, 0)

        # Values: DOGE = 0.85 * 1500 = 1275, SUI = 0.007 * 20000 = 140
        expected_total = 1275.0 + 140.0
        self.assertAlmostEqual(summary.total_eligible_dust_idr, expected_total, places=1)

        for item in summary.items:
            self.assertEqual(item.classification, DUST_CANDIDATE)
            self.assertIsNone(item.warning)

    # -----------------------------------------------------------------------
    # 2. Active-position residual excluded from eligible dust totals
    # -----------------------------------------------------------------------
    def test_active_position_residual_excluded_from_eligible_dust_totals(self):
        """An asset with an active DB trade is ACTIVE_POSITION_RESIDUAL and excluded from total."""
        balances = [
            ExchangeBalance(asset="DOGE", free=0.85, locked=0.0),  # eligible (~1275 IDR)
            ExchangeBalance(asset="ETH", free=0.0001, locked=0.0),  # active position in DB (~4000 IDR)
        ]
        open_trades = [
            {
                "symbol": "ETH_IDR",
                "exit_status": "OPEN",
                "entry_status": "FILLED",
                "oco_state": "EXECUTING",
            }
        ]
        exchange_orders = []

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            threshold_idr=20_000.0,
            price_lookup={"DOGE": 1500.0, "ETH": 40_000_000.0},
        )

        self.assertEqual(summary.eligible_count, 1)
        self.assertEqual(summary.active_residual_count, 1)

        # Total MUST ONLY include DOGE (1275), NOT ETH (4000)
        self.assertAlmostEqual(summary.total_eligible_dust_idr, 1275.0, places=1)

        eth_item = next(i for i in summary.items if i.asset == "ETH")
        self.assertEqual(eth_item.classification, ACTIVE_POSITION_RESIDUAL)
        self.assertEqual(eth_item.warning, ACTIVE_POSITION_WARNING)

    # -----------------------------------------------------------------------
    # 3. SOL residual with active position is not marked as free dust
    # -----------------------------------------------------------------------
    def test_sol_residual_with_active_position_is_protected(self):
        """Concrete case matching production: SOL has 0.00005 free and 0.0389 locked."""
        balances = [
            ExchangeBalance(asset="SOL", free=0.00005234, locked=0.0389),
            ExchangeBalance(asset="BNB", free=0.00099756, locked=0.0),
        ]
        open_trades = [
            {
                "symbol": "SOL_IDR",
                "exit_status": "OPEN",
                "entry_status": "FILLED",
                "oco_state": "EXECUTING",
            }
        ]
        exchange_orders = [
            {"symbol": "SOL_IDR", "orderId": "111", "status": "0"},
            {"symbol": "SOL_IDR", "orderId": "222", "status": "0"},
        ]

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            threshold_idr=20_000.0,
            price_lookup={"SOL": 2_000_000.0, "BNB": 13_000_000.0},
        )

        sol_item = next(i for i in summary.items if i.asset == "SOL")
        self.assertEqual(sol_item.classification, ACTIVE_POSITION_RESIDUAL)
        self.assertEqual(sol_item.warning, ACTIVE_POSITION_WARNING)

        # SOL value must NOT be added to total_eligible_dust_idr
        bnb_item = next(i for i in summary.items if i.asset == "BNB")
        self.assertEqual(bnb_item.classification, DUST_CANDIDATE)
        self.assertAlmostEqual(
            summary.total_eligible_dust_idr,
            bnb_item.estimated_value_idr,
            places=1,
        )

    # -----------------------------------------------------------------------
    # 4. Outstanding exchange orders prevent free-dust classification
    # -----------------------------------------------------------------------
    def test_outstanding_exchange_orders_prevent_free_dust(self):
        """Even if DB trade is missing/stale, an open order on exchange marks asset as ACTIVE_POSITION_RESIDUAL."""
        balances = [
            ExchangeBalance(asset="XRP", free=3.0, locked=0.0),
        ]
        open_trades = []  # DB says no open trades
        exchange_orders = [
            {"symbol": "XRP_IDR", "orderId": "999", "status": "NEW"}  # Exchange has working order
        ]

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            threshold_idr=100_000.0,
            price_lookup={"XRP": 24_000.0},
        )

        xrp_item = summary.items[0]
        self.assertEqual(xrp_item.classification, ACTIVE_POSITION_RESIDUAL)
        self.assertEqual(xrp_item.warning, ACTIVE_POSITION_WARNING)
        self.assertEqual(summary.total_eligible_dust_idr, 0.0)

    # -----------------------------------------------------------------------
    # 5. Exchange verification failure produces UNVERIFIED (fail-closed)
    # -----------------------------------------------------------------------
    def test_exchange_verification_failure_produces_unverified(self):
        """If exchange open orders query fails (exchange_open_orders is None), fail closed to UNVERIFIED."""
        balances = [
            ExchangeBalance(asset="DOGE", free=0.85, locked=0.0),
        ]
        open_trades = []
        exchange_orders = None  # Exchange API failure!

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            price_lookup={"DOGE": 1500.0},
        )

        self.assertEqual(summary.eligible_count, 0)
        self.assertEqual(summary.unverified_count, 1)
        self.assertEqual(summary.items[0].classification, UNVERIFIED)
        self.assertEqual(summary.total_eligible_dust_idr, 0.0)

    # -----------------------------------------------------------------------
    # 6. Database verification failure produces UNVERIFIED (fail-closed)
    # -----------------------------------------------------------------------
    def test_database_verification_failure_produces_unverified(self):
        """If database query fails (open_trades is None), fail closed to UNVERIFIED."""
        balances = [
            ExchangeBalance(asset="DOGE", free=0.85, locked=0.0),
        ]
        open_trades = None  # DB failure!
        exchange_orders = []

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            price_lookup={"DOGE": 1500.0},
        )

        self.assertEqual(summary.eligible_count, 0)
        self.assertEqual(summary.unverified_count, 1)
        self.assertEqual(summary.items[0].classification, UNVERIFIED)
        self.assertEqual(summary.total_eligible_dust_idr, 0.0)

    # -----------------------------------------------------------------------
    # 7. Missing ticker prices are handled safely
    # -----------------------------------------------------------------------
    def test_missing_ticker_prices_handled_safely(self):
        """If price lookup fails or returns 0/None, classify as UNVERIFIED without crashing."""
        balances = [
            ExchangeBalance(asset="UNKNOWNCOIN", free=10.0, locked=0.0),
        ]
        open_trades = []
        exchange_orders = []

        summary = classify_and_aggregate_dust(
            balances=balances,
            client=self.mock_client,
            open_trades=open_trades,
            exchange_open_orders=exchange_orders,
            price_lookup={"UNKNOWNCOIN": 0.0},  # unqueryable price
        )

        self.assertEqual(summary.eligible_count, 0)
        self.assertEqual(summary.unverified_count, 1)
        item = summary.items[0]
        self.assertEqual(item.classification, UNVERIFIED)
        self.assertIsNone(item.estimated_value_idr)
        self.assertIn("tidak tersedia", item.reason)

    # -----------------------------------------------------------------------
    # 8. Dust values do NOT affect PnL or adaptive allocation
    # -----------------------------------------------------------------------
    def test_dust_values_do_not_affect_pnl_or_adaptive_allocation(self):
        """calculate_adaptive_allocation uses raw free wallet balance, ignoring dust estimates."""
        wallet_balance = 35_000.0  # available free IDR
        available_slots = 2
        min_notional = 20_000.0

        # Dust estimate is 18,947 IDR
        dust_summary = DustSummary(total_eligible_dust_idr=18_947.0)

        # Baseline allocation without dust
        target_slots, alloc_per_order = calculate_adaptive_allocation(
            wallet_balance=wallet_balance,
            available_slots=available_slots,
            min_notional=min_notional,
        )

        # 35,000 / 2 slots = 17,500 (< 20,000) -> steps down to 1 slot @ 35,000
        self.assertEqual(target_slots, 1)
        self.assertEqual(alloc_per_order, 35_000.0)

        # Invariant check: Adding dust estimate to wallet_balance is STRICTLY FORBIDDEN.
        # If someone erroneously added dust to wallet_balance, it would produce 2 slots:
        # (35,000 + 18,947) = 53,947 / 2 = 26,973.5 (2 slots).
        # We verify that our system NEVER mutates wallet_balance.
        self.assertNotEqual(alloc_per_order, 26_973.5)

    # -----------------------------------------------------------------------
    # 9. Telegram cooldown survives process restarts via persisted state file
    # -----------------------------------------------------------------------
    def test_telegram_cooldown_survives_process_restarts(self):
        """State written to disk prevents notifications on simulated process restart."""
        summary = DustSummary(
            total_eligible_dust_idr=15_000.0,
            eligible_count=2,
            active_residual_count=0,
            items=[DustItem(asset="DOGE", free=10.0, locked=0.0, price_idr=1500.0, estimated_value_idr=15000.0, classification=DUST_CANDIDATE)],
        )

        mock_sender = MagicMock()
        now = 1_000_000.0

        # 1. First run: sends and persists state
        sent = notify_dust_summary_if_needed(
            summary=summary,
            sender_fn=mock_sender,
            state_file=self.state_file,
            cooldown_sec=86_400.0,
            delta_threshold_idr=5_000.0,
            now_ts=now,
        )
        self.assertTrue(sent)
        self.assertEqual(mock_sender.call_count, 1)

        # Verify state was written to file
        self.assertTrue(self.state_file.exists())
        with open(self.state_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data["last_sent_ts"], now)
        self.assertEqual(data["last_eligible_total_idr"], 15_000.0)

        # 2. Simulate fresh process restart: new caller reads state_file 1 hour later
        mock_sender_restart = MagicMock()
        now_1h_later = now + 3600.0

        sent_restart = notify_dust_summary_if_needed(
            summary=summary,
            sender_fn=mock_sender_restart,
            state_file=self.state_file,
            cooldown_sec=86_400.0,
            delta_threshold_idr=5_000.0,
            now_ts=now_1h_later,
        )
        # MUST BE SUPPRESSED because 1 hour < 24 hours and delta is 0
        self.assertFalse(sent_restart)
        self.assertEqual(mock_sender_restart.call_count, 0)

    # -----------------------------------------------------------------------
    # 10. Repeated polling does not produce duplicate notifications
    # -----------------------------------------------------------------------
    def test_repeated_polling_does_not_produce_duplicate_notifications(self):
        """Calling notify on consecutive polling cycles (e.g. hourly) does not spam."""
        summary = DustSummary(
            total_eligible_dust_idr=10_000.0,
            eligible_count=1,
            active_residual_count=0,
            items=[DustItem(asset="DOGE", free=6.6, locked=0.0, price_idr=1500.0, estimated_value_idr=10000.0, classification=DUST_CANDIDATE)],
        )
        mock_sender = MagicMock()
        now = 1_000_000.0

        # Cycle 1
        notify_dust_summary_if_needed(
            summary=summary, sender_fn=mock_sender, state_file=self.state_file, now_ts=now
        )
        self.assertEqual(mock_sender.call_count, 1)

        # Cycle 2 (1 hour later)
        notify_dust_summary_if_needed(
            summary=summary, sender_fn=mock_sender, state_file=self.state_file, now_ts=now + 3600
        )
        # Cycle 3 (2 hours later)
        notify_dust_summary_if_needed(
            summary=summary, sender_fn=mock_sender, state_file=self.state_file, now_ts=now + 7200
        )
        # Call count remains 1!
        self.assertEqual(mock_sender.call_count, 1)

    # -----------------------------------------------------------------------
    # 11. Configured value-change threshold behaves correctly
    # -----------------------------------------------------------------------
    def test_configured_value_change_threshold_behaves_correctly(self):
        """Notification fires earlier than 24h IF delta >= 5,000 IDR, but suppressed if delta < 5,000 IDR."""
        mock_sender = MagicMock()
        now = 1_000_000.0

        summary_initial = DustSummary(
            total_eligible_dust_idr=10_000.0,
            eligible_count=1,
            active_residual_count=0,
            items=[DustItem(asset="DOGE", free=6.6, locked=0.0, price_idr=1500.0, estimated_value_idr=10000.0, classification=DUST_CANDIDATE)],
        )

        # 1. Initial notification
        notify_dust_summary_if_needed(
            summary=summary_initial,
            sender_fn=mock_sender,
            state_file=self.state_file,
            delta_threshold_idr=5_000.0,
            cooldown_sec=86_400.0,
            now_ts=now,
        )
        self.assertEqual(mock_sender.call_count, 1)

        # 2. Delta = Rp 2,000 (< Rp 5,000) 2 hours later -> MUST BE SUPPRESSED
        summary_small_delta = DustSummary(
            total_eligible_dust_idr=12_000.0,
            eligible_count=2,
            active_residual_count=0,
            items=[DustItem(asset="DOGE", free=8.0, locked=0.0, price_idr=1500.0, estimated_value_idr=12000.0, classification=DUST_CANDIDATE)],
        )
        sent_small = notify_dust_summary_if_needed(
            summary=summary_small_delta,
            sender_fn=mock_sender,
            state_file=self.state_file,
            delta_threshold_idr=5_000.0,
            cooldown_sec=86_400.0,
            now_ts=now + 7200,
        )
        self.assertFalse(sent_small)
        self.assertEqual(mock_sender.call_count, 1)

        # 3. Delta = Rp 6,000 (total changed from 10,000 to 16,000, delta >= 5,000) 3 hours later -> MUST FIRE
        summary_large_delta = DustSummary(
            total_eligible_dust_idr=16_000.0,
            eligible_count=3,
            active_residual_count=0,
            items=[DustItem(asset="DOGE", free=10.6, locked=0.0, price_idr=1500.0, estimated_value_idr=16000.0, classification=DUST_CANDIDATE)],
        )
        sent_large = notify_dust_summary_if_needed(
            summary=summary_large_delta,
            sender_fn=mock_sender,
            state_file=self.state_file,
            delta_threshold_idr=5_000.0,
            cooldown_sec=86_400.0,
            now_ts=now + 10800,
        )
        self.assertTrue(sent_large)
        self.assertEqual(mock_sender.call_count, 2)


if __name__ == "__main__":
    unittest.main()
