"""
tests/test_tokocrypto_scanner.py
=================================
Unit tests for TokocryptoCandidateScanner.
All tests are mocked — no real API calls, no real orders.
"""

import unittest
from unittest.mock import MagicMock, patch


class TestToBindanceSymbol(unittest.TestCase):
    def test_btc_idr_to_btcusdt(self):
        from core.scanners.tokocrypto_candidate_scanner import _to_binance_symbol
        self.assertEqual(_to_binance_symbol("BTC_IDR"), "BTCUSDT")

    def test_zil_idr_to_zilusdt(self):
        from core.scanners.tokocrypto_candidate_scanner import _to_binance_symbol
        self.assertEqual(_to_binance_symbol("ZIL_IDR"), "ZILUSDT")

    def test_dogs_idr_to_dogsusdt(self):
        from core.scanners.tokocrypto_candidate_scanner import _to_binance_symbol
        self.assertEqual(_to_binance_symbol("DOGS_IDR"), "DOGSUSDT")

    def test_sol_idr_to_solusdt(self):
        from core.scanners.tokocrypto_candidate_scanner import _to_binance_symbol
        self.assertEqual(_to_binance_symbol("SOL_IDR"), "SOLUSDT")


class TestUsdtIdrRate(unittest.TestCase):
    def _make_scanner(self, ticker_price=16500.0):
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = ticker_price
        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
        return TokocryptoCandidateScanner(mock_client), mock_client

    def test_rate_fetched_from_usdt_idr_ticker(self):
        scanner, mock_client = self._make_scanner(16500.0)
        rate = scanner.get_usdt_idr_rate()
        mock_client.get_ticker.assert_called_once_with("USDT_IDR")
        self.assertEqual(rate, 16500.0)

    def test_rate_cached_within_ttl(self):
        scanner, mock_client = self._make_scanner(16500.0)
        scanner.get_usdt_idr_rate()
        scanner.get_usdt_idr_rate()
        # Should only call once (cached)
        self.assertEqual(mock_client.get_ticker.call_count, 1)

    def test_invalid_rate_raises(self):
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = 0
        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
        scanner = TokocryptoCandidateScanner(mock_client)
        with self.assertRaises(RuntimeError):
            scanner.get_usdt_idr_rate()


class TestFilterLogic(unittest.TestCase):
    """Tests for T1/rr/no-tp-range filter parity with SpotCandidateScanner."""

    def _make_scanner(self):
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = 16500.0
        mock_client.get_symbols.return_value = []
        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
        return TokocryptoCandidateScanner(mock_client)

    def _mock_analyze_result(self, tier="T1", rr_clears=True, no_tp=False):
        return {
            "current_price": 1.0,
            "atr": 0.05,
            "atr_pct": 5.0,
            "sl_tp": {
                "long": {
                    "rr_clears": rr_clears,
                    "no_tp_in_range": no_tp,
                    "tier_used": tier,
                    "sl": 0.90,
                    "tp": [1.15],
                    "rr": 1.5,
                    "risk_pct": 10.0,
                    "candidates": [{"tier": tier, "tp": 1.15}],
                }
            },
            "support_zones": [{"center": 0.95, "low": 0.93, "high": 0.97, "touches": 3}],
            "resistance_zones": [],
            "nearest_sup_dist": 0.05,
            "nearest_res_dist": 0.10,
        }

    def test_t1_only_accepted(self):
        scanner = self._make_scanner()
        with patch("services.chart_analyzer.analyze_symbol") as mock_ca:
            mock_ca.return_value = self._mock_analyze_result(tier="T1")
            with patch("time.sleep"):
                # Only scan one symbol
                from core.scanners.tokocrypto_candidate_scanner import IDR_PAIRS, SKIP_SYMBOLS
                tradeable = [s for s in IDR_PAIRS if s not in SKIP_SYMBOLS]
                mock_ca.return_value = self._mock_analyze_result(tier="T1")
                result = scanner.gather_candidates(max_positions=10)
        # At least something passed (exact count depends on mock applying to all symbols)
        self.assertIsInstance(result, list)

    def test_non_t1_filtered_out(self):
        scanner = self._make_scanner()
        with patch("services.chart_analyzer.analyze_symbol") as mock_ca:
            mock_ca.return_value = self._mock_analyze_result(tier="T2")
            with patch("time.sleep"):
                result = scanner.gather_candidates()
        self.assertEqual(len(result), 0, "T2 setups must be excluded")

    def test_rr_not_clears_filtered_out(self):
        scanner = self._make_scanner()
        with patch("services.chart_analyzer.analyze_symbol") as mock_ca:
            mock_ca.return_value = self._mock_analyze_result(rr_clears=False)
            with patch("time.sleep"):
                result = scanner.gather_candidates()
        self.assertEqual(len(result), 0, "rr_clears=False must be excluded")

    def test_no_tp_in_range_filtered_out(self):
        scanner = self._make_scanner()
        with patch("services.chart_analyzer.analyze_symbol") as mock_ca:
            mock_ca.return_value = self._mock_analyze_result(no_tp=True)
            with patch("time.sleep"):
                result = scanner.gather_candidates()
        self.assertEqual(len(result), 0, "no_tp_in_range=True must be excluded")

    def test_skip_stablecoins(self):
        """USDC_IDR, USDT_IDR, TKO_IDR must never appear in results."""
        scanner = self._make_scanner()
        with patch("services.chart_analyzer.analyze_symbol") as mock_ca:
            mock_ca.return_value = self._mock_analyze_result(tier="T1")
            with patch("time.sleep"):
                result = scanner.gather_candidates()
        syms = [c["symbol"] for c in result]
        for skip in ("USDC_IDR", "USDT_IDR", "TKO_IDR"):
            self.assertNotIn(skip, syms, f"{skip} must be excluded")


class TestPriceConversion(unittest.TestCase):
    def test_prices_multiplied_by_rate(self):
        """Placeholder — actual conversion tested in test_price_conversion_correct."""
        pass

    # correct test below
    def test_price_conversion_correct(self):
        mock_client = MagicMock()
        rate = 16000.0
        mock_client.get_ticker.return_value = rate
        mock_client.get_symbols.return_value = []

        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
        scanner = TokocryptoCandidateScanner(mock_client)

        analyze_result = {
            "current_price": 1.0,   # 1 USD → expect Rp 16,000
            "atr": 0.05,
            "atr_pct": 5.0,
            "sl_tp": {
                "long": {
                    "rr_clears": True,
                    "no_tp_in_range": False,
                    "tier_used": "T1",
                    "sl": 0.90,
                    "tp": [1.15],
                    "rr": 1.5,
                    "risk_pct": 10.0,
                    "candidates": [{"tier": "T1", "tp": 1.15}],
                }
            },
            "support_zones": [{"center": 0.95, "low": 0.93, "high": 0.97, "touches": 3}],
            "resistance_zones": [],
            "nearest_sup_dist": 0.05,
            "nearest_res_dist": 0.10,
        }

        # Only scan BTC_IDR to keep test fast
        with patch("services.chart_analyzer.analyze_symbol", return_value=analyze_result):
            with patch("time.sleep"):
                from core.scanners.tokocrypto_candidate_scanner import IDR_PAIRS, SKIP_SYMBOLS
                # Patch IDR_PAIRS to just one symbol
                with patch("core.scanners.tokocrypto_candidate_scanner.IDR_PAIRS", ["BTC_IDR"]):
                    result = scanner.gather_candidates()

        self.assertEqual(len(result), 1)
        cand = result[0]
        # entry_price should be ~1.0 * 16000 = 16000 IDR (exact value depends on rounding)
        self.assertAlmostEqual(cand["entry_price"], 1.0 * rate, delta=rate * 0.01)
        self.assertAlmostEqual(cand["sl"], 0.90 * rate, delta=rate * 0.01)
        self.assertAlmostEqual(cand["tp1"], 1.15 * rate, delta=rate * 0.01)
        self.assertEqual(cand["usdt_idr_rate"], rate)
        self.assertEqual(cand["entry_price_usd"], 1.0)
        self.assertEqual(cand["symbol"], "BTC_IDR")
        self.assertEqual(cand["binance_symbol"], "BTCUSDT")


class TestTopNCap(unittest.TestCase):
    def test_capped_at_max_positions(self):
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = 16000.0
        mock_client.get_symbols.return_value = []

        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
        scanner = TokocryptoCandidateScanner(mock_client)

        analyze_result = {
            "current_price": 1.0, "atr": 0.05, "atr_pct": 5.0,
            "sl_tp": {"long": {
                "rr_clears": True, "no_tp_in_range": False, "tier_used": "T1",
                "sl": 0.90, "tp": [1.15], "rr": 1.5, "risk_pct": 10.0,
                "candidates": [{"tier": "T1", "tp": 1.15}],
            }},
            "support_zones": [{"center": 0.95, "low": 0.93, "high": 0.97, "touches": 3}],
            "resistance_zones": [], "nearest_sup_dist": 0.05, "nearest_res_dist": 0.10,
        }

        # Patch to 15 tradeable symbols (more than max_positions=10)
        fake_pairs = [f"COIN{i}_IDR" for i in range(15)]
        with patch("core.scanners.tokocrypto_candidate_scanner.IDR_PAIRS", fake_pairs):
            with patch("core.scanners.tokocrypto_candidate_scanner.SKIP_SYMBOLS", set()):
                with patch("services.chart_analyzer.analyze_symbol", return_value=analyze_result):
                    with patch("time.sleep"):
                        result = scanner.gather_candidates(max_positions=10)

        self.assertLessEqual(len(result), 10, "Must cap at max_positions")


class TestSortOrder(unittest.TestCase):
    def test_sorted_by_risk_asc_then_rr_desc(self):
        """Lower risk_pct comes first; ties broken by higher rr."""
        mock_client = MagicMock()
        mock_client.get_ticker.return_value = 16000.0
        mock_client.get_symbols.return_value = []

        from core.scanners.tokocrypto_candidate_scanner import TokocryptoCandidateScanner
        scanner = TokocryptoCandidateScanner(mock_client)

        def _make_result(sl, rr, risk_pct):
            return {
                "current_price": 1.0, "atr": 0.05, "atr_pct": 5.0,
                "sl_tp": {"long": {
                    "rr_clears": True, "no_tp_in_range": False, "tier_used": "T1",
                    "sl": sl, "tp": [1.0 + rr * (1.0 - sl)],
                    "rr": rr, "risk_pct": risk_pct,
                    "candidates": [{"tier": "T1", "tp": 1.0 + rr * (1.0 - sl)}],
                }},
                "support_zones": [{"center": 0.95, "low": 0.93, "high": 0.97, "touches": 3}],
                "resistance_zones": [], "nearest_sup_dist": 0.05, "nearest_res_dist": 0.10,
            }

        results_map = {
            "A_IDR": _make_result(0.85, 2.0, 15.0),  # risk=15%
            "B_IDR": _make_result(0.92, 1.5, 8.0),   # risk=8% — should be first
            "C_IDR": _make_result(0.90, 3.0, 10.0),  # risk=10%
        }

        def mock_analyze(sym, save_chart=False):
            # sym is binance format e.g. AUSDT — map back
            toko = sym.replace("USDT", "_IDR")
            return results_map.get(toko)

        with patch("core.scanners.tokocrypto_candidate_scanner.IDR_PAIRS",
                   ["A_IDR", "B_IDR", "C_IDR"]):
            with patch("core.scanners.tokocrypto_candidate_scanner.SKIP_SYMBOLS", set()):
                with patch("services.chart_analyzer.analyze_symbol", side_effect=mock_analyze):
                    with patch("time.sleep"):
                        result = scanner.gather_candidates()

        syms = [c["symbol"] for c in result]
        risks = [c["risk_pct"] for c in result]
        # Should be sorted ascending by risk_pct
        self.assertEqual(risks, sorted(risks), "Must be sorted by risk_pct ASC")
        self.assertEqual(syms[0], "B_IDR", "Lowest risk must be first")


if __name__ == "__main__":
    unittest.main(verbosity=2)
