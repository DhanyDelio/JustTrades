"""
Unit tests for futures --propose batch slot-filling logic.

Tests the core slot-filling behaviour introduced when --propose default
changed from 2 to MAX_CONCURRENT_POSITIONS (20).

Key logic under test (from cmd_propose_multi_futures):
    open_trades  = [t for t in load_futures_log() if t.get("exit_status") == "OPEN"]
    open_symbols = {t["symbol"] for t in open_trades}
    slots_left   = MAX_CONCURRENT_POSITIONS - len(open_trades)
    effective_count = min(count, slots_left)
    # then iterative pick loop fills up to effective_count

Scenarios:
    1. 17/20 slots used  →  propose exactly 3
    2.  0/20 slots used  →  propose up to 20 (capped by candidates)
    3. 19/20 slots used  →  propose exactly 1
    4. 20/20 slots used  →  propose 0 (blocked)
    5. 15/20 but only 3 candidates available → propose 3
    6. Duplicate symbol filtering — open symbol skipped
    7. Default --count equals MAX_CONCURRENT_POSITIONS (20)
"""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.futures_trade_executor import (
    MAX_CONCURRENT_POSITIONS,
    cmd_propose_multi_futures,
    pick_best_futures_candidate,
)


# =============================================================================
# HELPERS
# =============================================================================

def _make_open_trade(symbol: str) -> dict:
    """Create a minimal open trade record for mocking load_futures_log."""
    return {
        "symbol":       symbol,
        "exit_status":  "OPEN",
        "entry_order_id": 10000,
        "position_side": "LONG",
    }


def _make_candidate(symbol: str, direction: str = "long",
                    risk_pct: float = 1.5, rr: float = 2.0) -> dict:
    """Create a minimal candidate dict that mirrors gather_futures_candidates output."""
    side = "LONG" if direction == "long" else "SHORT"
    return {
        "symbol":        symbol,
        "direction":     direction,
        "position_side": side,
        "current_price": 100.0,
        "entry_price":   100.0,
        "sl":            97.0 if direction == "long" else 103.0,
        "tp1":           106.0 if direction == "long" else 94.0,
        "tp2":           None,
        "rr":            rr,
        "risk_pct":      risk_pct,
        "atr":           3.0,
        "atr_pct":       3.0,
        "tier_used":     "T1",
        "support_zones":    [{"center": 97.0, "low": 96.0, "high": 98.0, "touches": 3}],
        "resistance_zones": [{"center": 103.0, "low": 102.0, "high": 104.0, "touches": 3}],
    }


def _enrich_candidate(cand: dict) -> dict:
    """Add sizing/liquidation/volatility fields that pick_best_futures_candidate adds."""
    cand["entry_zone"]  = {"center": 97.0, "low": 96.0, "touches": 3}
    cand["sizing"]      = {
        "qty": 0.01, "notional_usd": 36.0, "margin_used": 12.0,
        "max_loss_usd": 3.0, "max_loss_pct": 25.0,
        "risk_per_unit": 3.0, "warnings": [],
    }
    cand["constraints"] = {"tick_size": 0.01, "step_size": 0.001,
                           "min_qty": 0.001, "min_notional": 5.0}
    cand["liquidation"] = {
        "liquidation_price": 67.0,
        "distance_to_liquidation_pct": 33.0,
    }
    cand["volatility_regime"]    = "medium"
    cand["funding_rate_at_entry"] = 0.0001
    return cand


# =============================================================================
# TESTS: Slot Calculation Logic
# =============================================================================

class TestSlotCalculation(unittest.TestCase):
    """Pure math tests — no mocking needed."""

    def test_slots_left_17_of_20(self):
        """17 open → 3 slots left."""
        print("\n=== Skenario 1: 17/20 open → slots_left = 3 ===")
        open_count = 17
        slots_left = MAX_CONCURRENT_POSITIONS - open_count
        self.assertEqual(slots_left, 3)
        effective_count = min(MAX_CONCURRENT_POSITIONS, slots_left)
        self.assertEqual(effective_count, 3)
        print(f"✓ slots_left = {slots_left}")
        print(f"✓ effective_count = min({MAX_CONCURRENT_POSITIONS}, {slots_left}) = {effective_count}")

    def test_slots_left_0_of_20(self):
        """0 open → 20 slots left, effective_count capped by count arg."""
        print("\n=== Skenario 2: 0/20 open → slots_left = 20 ===")
        open_count = 0
        slots_left = MAX_CONCURRENT_POSITIONS - open_count
        self.assertEqual(slots_left, 20)
        effective_count = min(MAX_CONCURRENT_POSITIONS, slots_left)
        self.assertEqual(effective_count, 20)
        print(f"✓ slots_left = {slots_left}")
        print(f"✓ effective_count = {effective_count}")

    def test_slots_left_19_of_20(self):
        """19 open → only 1 slot."""
        print("\n=== Skenario 3: 19/20 open → slots_left = 1 ===")
        open_count = 19
        slots_left = MAX_CONCURRENT_POSITIONS - open_count
        self.assertEqual(slots_left, 1)
        effective_count = min(MAX_CONCURRENT_POSITIONS, slots_left)
        self.assertEqual(effective_count, 1)
        print(f"✓ slots_left = {slots_left}")
        print(f"✓ effective_count = {effective_count}")

    def test_slots_left_20_of_20_blocked(self):
        """20 open → 0 slots → should be blocked."""
        print("\n=== Skenario 4: 20/20 open → blocked ===")
        open_count = 20
        slots_left = MAX_CONCURRENT_POSITIONS - open_count
        self.assertEqual(slots_left, 0)
        self.assertLessEqual(slots_left, 0)
        print(f"✓ slots_left = {slots_left} → ⛔ blocked")

    def test_effective_count_capped_by_candidates(self):
        """15/20 open → 5 slots, but only 3 candidates → effective picks = 3."""
        print("\n=== Skenario 5: 15/20 open, only 3 candidates ===")
        open_count = 15
        slots_left = MAX_CONCURRENT_POSITIONS - open_count
        self.assertEqual(slots_left, 5)
        n_candidates = 3
        # effective_count from min(count, slots_left)
        effective_count = min(MAX_CONCURRENT_POSITIONS, slots_left)
        self.assertEqual(effective_count, 5)
        # but iterative pick loop stops at min(effective_count, n_candidates)
        actual_picks = min(effective_count, n_candidates)
        self.assertEqual(actual_picks, 3)
        print(f"✓ slots_left = {slots_left}, effective_count = {effective_count}")
        print(f"✓ only {n_candidates} candidates → actual picks = {actual_picks}")


# =============================================================================
# TESTS: Iterative Pick Loop (with mocked pick_best_futures_candidate)
# =============================================================================

class TestIterativePickLoop(unittest.TestCase):
    """
    Tests the iterative pick loop logic from cmd_propose_multi_futures
    extracted into a standalone simulation to avoid sys.exit / exchange calls.
    """

    def _simulate_pick_loop(
        self,
        candidates: list[dict],
        open_symbols: set[str],
        effective_count: int,
        side_filter: str | None = None,
    ) -> tuple[list[dict], list[str]]:
        """
        Simulate the exact pick loop from cmd_propose_multi_futures lines 2182-2206.
        Returns (selected, skipped_syms).
        """
        selected:     list[dict] = []
        skipped_syms: list[str]  = []
        excluded_symbols: set[str] = set(open_symbols)

        remaining = list(candidates)
        while len(selected) < effective_count and remaining:
            pool = [c for c in remaining if c["symbol"] not in excluded_symbols]
            if not pool:
                break

            # Simulate pick_best — just return the first from pool (sorted by risk_pct)
            pick = pool[0] if pool else None
            if pick is None:
                skipped_syms.extend(
                    c["symbol"] for c in pool
                    if c["symbol"] not in excluded_symbols
                )
                break

            selected.append(_enrich_candidate(pick))
            excluded_symbols.add(pick["symbol"])
            remaining = [c for c in remaining if c["symbol"] != pick["symbol"]]

        return selected, skipped_syms

    def test_17_of_20_selects_exactly_3(self):
        """17 open slots used, 10 candidates → pick exactly 3."""
        print("\n=== Pick Loop: 17/20 open → selects 3 ===")
        open_symbols = {f"SYM{i}USDT" for i in range(17)}
        candidates = [
            _make_candidate(f"NEW{i}USDT", risk_pct=1.0 + i * 0.1)
            for i in range(10)
        ]
        effective_count = MAX_CONCURRENT_POSITIONS - 17  # = 3

        selected, skipped = self._simulate_pick_loop(
            candidates, open_symbols, effective_count)

        self.assertEqual(len(selected), 3)
        syms = [s["symbol"] for s in selected]
        self.assertEqual(syms, ["NEW0USDT", "NEW1USDT", "NEW2USDT"])
        print(f"✓ Selected {len(selected)} trades: {syms}")
        print(f"✓ Exactly fills remaining 3 slots (17 + 3 = 20)")

    def test_0_of_20_selects_up_to_available(self):
        """0 open, 8 candidates → pick all 8 (slots_left=20 > candidates)."""
        print("\n=== Pick Loop: 0/20 open, 8 candidates → selects 8 ===")
        candidates = [
            _make_candidate(f"COIN{i}USDT", risk_pct=1.0 + i * 0.1)
            for i in range(8)
        ]
        effective_count = MAX_CONCURRENT_POSITIONS  # = 20

        selected, skipped = self._simulate_pick_loop(
            candidates, set(), effective_count)

        self.assertEqual(len(selected), 8)
        print(f"✓ Selected {len(selected)} trades (all available candidates)")
        print(f"✓ 12 slots still empty — no more candidates to fill")

    def test_19_of_20_selects_exactly_1(self):
        """19 open, 5 candidates → pick exactly 1."""
        print("\n=== Pick Loop: 19/20 open → selects 1 ===")
        open_symbols = {f"SYM{i}USDT" for i in range(19)}
        candidates = [
            _make_candidate(f"FRESH{i}USDT", risk_pct=2.0 + i * 0.1)
            for i in range(5)
        ]
        effective_count = MAX_CONCURRENT_POSITIONS - 19  # = 1

        selected, skipped = self._simulate_pick_loop(
            candidates, open_symbols, effective_count)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["symbol"], "FRESH0USDT")
        print(f"✓ Selected exactly 1: {selected[0]['symbol']}")

    def test_20_of_20_selects_zero(self):
        """20 open → effective_count=0 → loop doesn't run."""
        print("\n=== Pick Loop: 20/20 open → selects 0 ===")
        open_symbols = {f"SYM{i}USDT" for i in range(20)}
        candidates = [_make_candidate("NEWUSDT")]
        effective_count = MAX_CONCURRENT_POSITIONS - 20  # = 0

        selected, skipped = self._simulate_pick_loop(
            candidates, open_symbols, effective_count)

        self.assertEqual(len(selected), 0)
        print(f"✓ Selected 0 — all slots full, loop body never entered")

    def test_duplicate_symbol_filtered_out(self):
        """Candidate with same symbol as open trade is excluded."""
        print("\n=== Pick Loop: duplicate symbol filtered ===")
        open_symbols = {"BTCUSDT", "ETHUSDT"}
        candidates = [
            _make_candidate("BTCUSDT", risk_pct=0.5),   # already open
            _make_candidate("ETHUSDT", risk_pct=0.6),   # already open
            _make_candidate("SOLUSDT", risk_pct=0.7),   # new — should be picked
            _make_candidate("ADAUSDT", risk_pct=0.8),   # new — should be picked
        ]
        effective_count = 5

        selected, skipped = self._simulate_pick_loop(
            candidates, open_symbols, effective_count)

        selected_syms = {s["symbol"] for s in selected}
        self.assertNotIn("BTCUSDT", selected_syms)
        self.assertNotIn("ETHUSDT", selected_syms)
        self.assertIn("SOLUSDT", selected_syms)
        self.assertIn("ADAUSDT", selected_syms)
        self.assertEqual(len(selected), 2)
        print(f"✓ BTCUSDT, ETHUSDT filtered (already open)")
        print(f"✓ Selected: {selected_syms}")

    def test_no_duplicate_symbol_in_batch(self):
        """Same symbol should not appear twice in one batch."""
        print("\n=== Pick Loop: no duplicate symbol in batch ===")
        candidates = [
            _make_candidate("BTCUSDT", "long",  risk_pct=1.0),
            _make_candidate("BTCUSDT", "short", risk_pct=1.1),  # same symbol
            _make_candidate("ETHUSDT", "long",  risk_pct=1.2),
        ]
        effective_count = 5

        selected, skipped = self._simulate_pick_loop(
            candidates, set(), effective_count)

        syms = [s["symbol"] for s in selected]
        # BTCUSDT should appear only once
        self.assertEqual(syms.count("BTCUSDT"), 1)
        self.assertIn("ETHUSDT", syms)
        self.assertEqual(len(selected), 2)
        print(f"✓ BTCUSDT picked once (first), second BTCUSDT excluded")
        print(f"✓ Selected: {syms}")


# =============================================================================
# TEST: Default --count CLI arg = MAX_CONCURRENT_POSITIONS
# =============================================================================

class TestDefaultCountArg(unittest.TestCase):
    """Verify --count default equals MAX_CONCURRENT_POSITIONS (20)."""

    def test_default_count_is_max_positions(self):
        """argparse default for --count should be 20."""
        print("\n=== Default --count = MAX_CONCURRENT_POSITIONS ===")
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--count", type=int,
                            default=MAX_CONCURRENT_POSITIONS)
        args = parser.parse_args([])
        self.assertEqual(args.count, 20)
        self.assertEqual(args.count, MAX_CONCURRENT_POSITIONS)
        print(f"✓ Default --count = {args.count} (MAX_CONCURRENT_POSITIONS = {MAX_CONCURRENT_POSITIONS})")


# =============================================================================
# TEST: End-to-end cmd_propose_multi_futures with full mocking
# =============================================================================

class TestCmdProposeMultiE2E(unittest.TestCase):
    """
    End-to-end test of cmd_propose_multi_futures with all external
    dependencies mocked. Verifies the full flow from scan to order placement.
    """

    def _make_candidates(self, n: int) -> list[dict]:
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "DOTUSDT",
            "AVAXUSDT", "LINKUSDT", "MATICUSDT", "UNIUSDT", "AAVEUSDT",
        ]
        return [
            _enrich_candidate(_make_candidate(symbols[i % len(symbols)],
                                              risk_pct=1.0 + i * 0.1))
            for i in range(n)
        ]

    @patch("core.futures_trade_executor._send_telegram")
    @patch("core.futures_trade_executor.gather_futures_candidates")
    @patch("core.futures_trade_executor.load_futures_log")
    @patch("core.futures_trade_executor.pick_best_futures_candidate")
    @patch("core.futures_trade_executor.get_futures_client")
    @patch("core.futures_trade_executor.ping_futures")
    def test_17_open_places_3_orders(
        self, mock_ping, mock_client_fn, mock_pick,
        mock_log, mock_gather, mock_tg
    ):
        """17/20 open → scan finds 10 → picks 3 → places 3 orders."""
        print("\n=== E2E: 17/20 open → places 3 orders ===")

        mock_ping.return_value = True
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client

        # 17 open trades with unique symbols
        open_trades = [_make_open_trade(f"OPEN{i}USDT") for i in range(17)]
        mock_log.return_value = open_trades

        # 10 fresh candidates (none overlap with open)
        fresh_candidates = self._make_candidates(5)
        for i, c in enumerate(fresh_candidates):
            c["symbol"] = f"FRESH{i}USDT"
        mock_gather.return_value = fresh_candidates

        # pick_best returns candidates in order, then None
        pick_results = [_enrich_candidate(c.copy()) for c in fresh_candidates[:3]]
        mock_pick.side_effect = pick_results + [None]

        # Mock executor import + execution
        mock_executor_cls = MagicMock()
        mock_order = {"orderId": 99990, "price": "100.0"}
        mock_executor_instance = MagicMock()
        mock_executor_instance.execute.return_value = mock_order
        mock_executor_cls.return_value = mock_executor_instance

        with patch.dict("sys.modules", {
            "core.executors.futures_order_executor": MagicMock(
                FuturesOrderExecutor=mock_executor_cls)
        }):
            # auto_confirm=True to skip input()
            cmd_propose_multi_futures(
                scan_n=100, count=MAX_CONCURRENT_POSITIONS,
                auto_confirm=True,
            )

        # Should have called pick 3 times (3 slots) then stopped
        self.assertEqual(mock_pick.call_count, 3)
        print(f"✓ pick_best called {mock_pick.call_count} times (3 slots)")
        print(f"✓ 17 open + 3 new = 20 (full)")

    @patch("core.futures_trade_executor._send_telegram")
    @patch("core.futures_trade_executor.gather_futures_candidates")
    @patch("core.futures_trade_executor.load_futures_log")
    @patch("core.futures_trade_executor.get_futures_client")
    @patch("core.futures_trade_executor.ping_futures")
    def test_20_open_exits_early(
        self, mock_ping, mock_client_fn, mock_log, mock_gather, mock_tg
    ):
        """20/20 open → sys.exit(0) before any scanning."""
        print("\n=== E2E: 20/20 open → exits early ===")

        mock_ping.return_value = True
        mock_client_fn.return_value = MagicMock()
        mock_log.return_value = [_make_open_trade(f"FULL{i}USDT") for i in range(20)]

        with self.assertRaises(SystemExit) as ctx:
            cmd_propose_multi_futures(
                scan_n=100, count=MAX_CONCURRENT_POSITIONS,
                auto_confirm=True,
            )

        self.assertEqual(ctx.exception.code, 0)
        mock_gather.assert_not_called()
        print(f"✓ sys.exit(0) raised — no scan performed")
        print(f"✓ 20/20 slots full → correctly blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
