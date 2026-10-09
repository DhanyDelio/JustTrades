"""
test_tokocrypto_client.py — Unit tests for TokocryptoClient.

All tests use unittest.mock — zero live network calls.
Coverage:
    1.  ExchangeSymbol parsing — full filter set
    2.  ExchangeSymbol parsing — NOTIONAL filter (current API)
    3.  ExchangeSymbol parsing — MIN_NOTIONAL filter (legacy)
    4.  ExchangeSymbol parsing — missing/empty filters (safe defaults)
    5.  ExchangeSymbol.constraints property
    6.  ExchangeBalance.total property
    7.  round_tick — various tick sizes
    8.  round_step — rounds DOWN
    9.  round_tick / round_step — zero tick/step passthrough
    10. normalize_symbol — upper-cases and strips
    11. get_server_time — happy path
    12. get_server_time — missing timestamp field → MalformedResponseError
    13. get_symbols — wrapped response (data.list)
    14. get_symbols — filters out type != 1
    15. get_symbol — found
    16. get_symbol — not found → CapabilityError
    17. get_ticker — Binance-style response {"price": "..."}
    18. get_ticker — wrapped response with data.price
    19. get_ticker — missing price field → MalformedResponseError
    20. get_depth — Binance-style bids/asks
    21. get_depth — wrapped data envelope
    22. get_depth — missing bids/asks key → MalformedResponseError
    23. get_depth — limit snapped to nearest valid
    24. get_balances — happy path, full account response
    25. get_balances — accountAssets not a list → MalformedResponseError
    26. get_balance — specific asset found
    27. get_balance — asset absent → zero balance returned
    28. get_account — data field missing → MalformedResponseError
    29. Unauthenticated client → TokocryptoAuthError on signed call
    30. Network error → TokocryptoNetworkError
    31. Timeout → TokocryptoNetworkError
    32. HTTP 429 → TokocryptoRateLimitError
    33. HTTP 401 → TokocryptoAuthError
    34. API code -2014 in body → TokocryptoAuthError
    35. API code != 0 (generic) → TokocryptoAPIError with code/msg
    36. Non-JSON response → TokocryptoMalformedResponseError
    37. ping — reachable (returns True)
    38. ping — network error (returns False)
    39. ping — API error still returns True (exchange is up)
    40. build() — no env vars → unauthenticated client
    41. build() — env vars present → authenticated client
    42. _sign_params — output contains timestamp, recvWindow, signature
    43. _sign_params — signature is correct HMAC-SHA256
    44. No order-placement methods exist on TokocryptoClient

Stage 1 extension tests (52–66):
    52. ExchangeSymbol parsed with STP fields present
    53. ExchangeSymbol parsed without STP fields (backward compat)
    54. default_stp_mode is None (not 0) when field absent
    55. get_execution_rules — success, correct fields
    56. get_execution_rules — symbol not in response → empty rules
    57. get_execution_rules — malformed response → MalformedResponseError
    58. get_execution_rules — empty symbol → ValueError
    59. get_execution_rules — multiplier partially absent → None fields
    60. get_reference_price — success, returns float
    61. get_reference_price — null referencePrice → None
    62. HTTP 500 → TokocryptoUnknownOrderStatus
    63. HTTP 502 → TokocryptoUnknownOrderStatus with correct status_code
    64. HTTP 404 → TokocryptoAPIError (not UnknownOrderStatus)
    65. HTTP 429 → TokocryptoRateLimitError (not UnknownOrderStatus)
    66. TokocryptoUnknownOrderStatus has no retry attribute
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import requests

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.clients.tokocrypto_client import (
    ExchangeBalance,
    ExchangeSymbol,
    ExecutionRules,
    ExecutionRulesRule,
    TokocryptoAPIError,
    TokocryptoAuthError,
    TokocryptoCapabilityError,
    TokocryptoClient,
    TokocryptoMalformedResponseError,
    TokocryptoNetworkError,
    TokocryptoRateLimitError,
    TokocryptoUnknownOrderStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _raw_symbol(
    symbol="BTC_USDT",
    base="BTC",
    quote="USDT",
    spot=1,
    oco=1,
    sym_type=1,
    tick="0.01",
    step="0.001",
    min_qty="0.001",
    notional_filter="NOTIONAL",
    min_notional="10.0",
) -> dict:
    """Build a minimal raw symbol dict as returned by GET /open/v1/common/symbols."""
    return {
        "type":              sym_type,
        "symbol":            symbol,
        "baseAsset":         base,
        "quoteAsset":        quote,
        "spotTradingEnable": spot,
        "ocoEnable":         oco,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice":   "0.01",
                "maxPrice":   "100000.0",
                "tickSize":   tick,
            },
            {
                "filterType": "LOT_SIZE",
                "minQty":     min_qty,
                "maxQty":     "9000000.0",
                "stepSize":   step,
            },
            {
                "filterType":    notional_filter,
                "minNotional":   min_notional,
                "applyToMarket": True,
            },
        ],
    }


def _symbols_envelope(raw_list: list) -> dict:
    """Wrap a list into the Tokocrypto GET /open/v1/common/symbols envelope."""
    return {"code": 0, "msg": "success", "data": {"list": raw_list}, "timestamp": 1700000000000}


def _make_response(body: dict, status: int = 200) -> MagicMock:
    """Return a mock requests.Response with given JSON body and status code."""
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    r.headers = {}
    return r


def _make_client(api_key="testkey", api_secret="testsecret") -> TokocryptoClient:
    return TokocryptoClient(api_key=api_key, api_secret=api_secret)


def _make_anon_client() -> TokocryptoClient:
    return TokocryptoClient(api_key=None, api_secret=None)


# ---------------------------------------------------------------------------
# Test: ExchangeSymbol parsing
# ---------------------------------------------------------------------------

class TestParseSymbol(unittest.TestCase):
    """Tests 1–6: domain model construction via _parse_symbol."""

    def test_full_filter_set(self):
        """Test 1 — all filters present, values parsed correctly."""
        raw = _raw_symbol(tick="0.01", step="0.001", min_qty="0.001", min_notional="10.0")
        sym = TokocryptoClient._parse_symbol(raw)

        self.assertEqual(sym.symbol,       "BTC_USDT")
        self.assertEqual(sym.base_asset,   "BTC")
        self.assertEqual(sym.quote_asset,  "USDT")
        self.assertEqual(sym.status,       "TRADING")
        self.assertAlmostEqual(sym.tick_size,    0.01)
        self.assertAlmostEqual(sym.step_size,    0.001)
        self.assertAlmostEqual(sym.min_qty,      0.001)
        self.assertAlmostEqual(sym.min_notional, 10.0)
        self.assertTrue(sym.spot_enabled)
        self.assertTrue(sym.oco_enabled)
        self.assertEqual(sym.raw, raw)
        print("✓ Test 1: full filter set parsed correctly")

    def test_notional_filter_current(self):
        """Test 2 — NOTIONAL filter (current API shape)."""
        raw = _raw_symbol(notional_filter="NOTIONAL", min_notional="5.5")
        sym = TokocryptoClient._parse_symbol(raw)
        self.assertAlmostEqual(sym.min_notional, 5.5)
        print("✓ Test 2: NOTIONAL filter parsed")

    def test_min_notional_filter_legacy(self):
        """Test 3 — MIN_NOTIONAL filter (legacy fallback)."""
        raw = _raw_symbol(notional_filter="MIN_NOTIONAL", min_notional="3.0")
        sym = TokocryptoClient._parse_symbol(raw)
        self.assertAlmostEqual(sym.min_notional, 3.0)
        print("✓ Test 3: MIN_NOTIONAL legacy filter parsed")

    def test_missing_filters_safe_defaults(self):
        """Test 4 — no filters → all numeric fields default to 0.0."""
        raw = {
            "type": 1, "symbol": "ETH_USDT",
            "baseAsset": "ETH", "quoteAsset": "USDT",
            "spotTradingEnable": 1, "ocoEnable": 0,
            "filters": [],
        }
        sym = TokocryptoClient._parse_symbol(raw)
        self.assertAlmostEqual(sym.tick_size, 0.0)
        self.assertAlmostEqual(sym.step_size, 0.0)
        self.assertAlmostEqual(sym.min_qty, 0.0)
        self.assertAlmostEqual(sym.min_notional, 0.0)
        self.assertFalse(sym.oco_enabled)
        print("✓ Test 4: missing filters → safe defaults")

    def test_constraints_property(self):
        """Test 5 — constraints property returns binance_math-compatible dict."""
        raw = _raw_symbol(tick="0.1", step="0.01", min_qty="0.01", min_notional="15.0")
        sym = TokocryptoClient._parse_symbol(raw)
        c = sym.constraints
        self.assertEqual(set(c.keys()), {"tick_size", "step_size", "min_qty", "min_notional"})
        self.assertAlmostEqual(c["tick_size"],    0.1)
        self.assertAlmostEqual(c["step_size"],    0.01)
        self.assertAlmostEqual(c["min_qty"],      0.01)
        self.assertAlmostEqual(c["min_notional"], 15.0)
        print("✓ Test 5: constraints property shape matches binance_math")

    def test_exchange_balance_total(self):
        """Test 6 — ExchangeBalance.total = free + locked."""
        b = ExchangeBalance(asset="USDT", free=100.0, locked=25.5)
        self.assertAlmostEqual(b.total, 125.5)
        print("✓ Test 6: ExchangeBalance.total = free + locked")

    def test_halted_symbol_status(self):
        """Bonus: spotTradingEnable==0 → status='HALTED'."""
        raw = _raw_symbol(spot=0)
        sym = TokocryptoClient._parse_symbol(raw)
        self.assertEqual(sym.status, "HALTED")
        self.assertFalse(sym.spot_enabled)
        print("✓ Bonus: spotTradingEnable=0 → status=HALTED")


# ---------------------------------------------------------------------------
# Test: Normalization helpers
# ---------------------------------------------------------------------------

class TestNormalizationHelpers(unittest.TestCase):
    """Tests 7–10: round_tick, round_step, normalize_symbol."""

    def test_round_tick_standard(self):
        """Test 7a — round_tick to 2 decimal places."""
        self.assertAlmostEqual(TokocryptoClient.round_tick(60000.126, 0.01), 60000.13)
        self.assertAlmostEqual(TokocryptoClient.round_tick(60000.124, 0.01), 60000.12)
        print("✓ Test 7a: round_tick 2dp")

    def test_round_tick_various(self):
        """Test 7b — round_tick across different tick sizes."""
        # 0.001234 / 0.0001 = 12.34 → round → 12 → 12 * 0.0001 = 0.0012
        self.assertAlmostEqual(TokocryptoClient.round_tick(0.001234, 0.0001), 0.0012)
        # 1234.5 / 0.5 = 2469.0 → round → 2469 → 2469 * 0.5 = 1234.5
        self.assertAlmostEqual(TokocryptoClient.round_tick(1234.5, 0.5), 1234.5)
        # 1234.7 / 0.5 = 2469.4 → round → 2469 → 2469 * 0.5 = 1234.5  (nearest half)
        self.assertAlmostEqual(TokocryptoClient.round_tick(1234.7, 0.5), 1234.5)
        # 1234.76 / 0.5 = 2469.52 → round → 2470 → 2470 * 0.5 = 1235.0
        self.assertAlmostEqual(TokocryptoClient.round_tick(1234.76, 0.5), 1235.0)
        print("✓ Test 7b: round_tick various tick sizes")

    def test_round_step_rounds_down(self):
        """Test 8 — round_step must truncate (floor), not round up."""
        # 0.0125 with step 0.001 → floor → 0.012  (NOT 0.013)
        self.assertAlmostEqual(TokocryptoClient.round_step(0.0125, 0.001), 0.012)
        self.assertAlmostEqual(TokocryptoClient.round_step(0.0199, 0.001), 0.019)
        self.assertAlmostEqual(TokocryptoClient.round_step(1.9999, 0.01),  1.99)
        print("✓ Test 8: round_step always floors (never rounds up)")

    def test_round_tick_zero_passthrough(self):
        """Test 9a — tick=0 → value returned unchanged."""
        self.assertAlmostEqual(TokocryptoClient.round_tick(1234.567, 0), 1234.567)
        print("✓ Test 9a: tick=0 passthrough")

    def test_round_step_zero_passthrough(self):
        """Test 9b — step=0 → value returned unchanged."""
        self.assertAlmostEqual(TokocryptoClient.round_step(0.01234, 0), 0.01234)
        print("✓ Test 9b: step=0 passthrough")

    def test_normalize_symbol(self):
        """Test 10 — normalize_symbol upper-cases and strips whitespace."""
        self.assertEqual(TokocryptoClient.normalize_symbol("  btc_usdt  "), "BTC_USDT")
        self.assertEqual(TokocryptoClient.normalize_symbol("eth_usdt"), "ETH_USDT")
        self.assertEqual(TokocryptoClient.normalize_symbol("BTC_USDT"), "BTC_USDT")
        print("✓ Test 10: normalize_symbol upper-cases and strips")


# ---------------------------------------------------------------------------
# Test: Public endpoints (mocked session)
# ---------------------------------------------------------------------------

class TestPublicEndpoints(unittest.TestCase):
    """Tests 11–23: ping, get_server_time, get_symbols, get_symbol, get_ticker, get_depth."""

    def _patch_session(self, client: TokocryptoClient, response: MagicMock):
        """Patch client._session.get to return a fixed mock response."""
        client._session.get = MagicMock(return_value=response)
        return client

    # ── get_server_time ────────────────────────────────────────────────

    def test_get_server_time_happy(self):
        """Test 11 — get_server_time returns integer timestamp."""
        client = _make_anon_client()
        resp = _make_response({"code": 0, "msg": "success", "timestamp": 1700000000000})
        self._patch_session(client, resp)

        ts = client.get_server_time()
        self.assertEqual(ts, 1700000000000)
        print("✓ Test 11: get_server_time → 1700000000000")

    def test_get_server_time_missing_timestamp(self):
        """Test 12 — missing timestamp field → MalformedResponseError."""
        client = _make_anon_client()
        resp = _make_response({"code": 0, "msg": "success"})  # no timestamp
        self._patch_session(client, resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_server_time()
        print("✓ Test 12: missing timestamp → MalformedResponseError")

    # ── get_symbols ────────────────────────────────────────────────────

    def test_get_symbols_happy(self):
        """Test 13 — get_symbols parses wrapped data.list correctly."""
        client = _make_anon_client()
        raw = [_raw_symbol("BTC_USDT"), _raw_symbol("ETH_USDT", base="ETH")]
        resp = _make_response(_symbols_envelope(raw))
        self._patch_session(client, resp)

        symbols = client.get_symbols()
        self.assertEqual(len(symbols), 2)
        self.assertIsInstance(symbols[0], ExchangeSymbol)
        self.assertEqual(symbols[0].symbol, "BTC_USDT")
        print("✓ Test 13: get_symbols parses data.list correctly")

    def test_get_symbols_filters_non_type1(self):
        """Test 14 — symbols with type != 1 are excluded."""
        client = _make_anon_client()
        raw = [
            _raw_symbol("BTC_USDT", sym_type=1),
            _raw_symbol("TKO_IDR",  sym_type=3),   # type-3 Nextme — excluded
        ]
        resp = _make_response(_symbols_envelope(raw))
        self._patch_session(client, resp)

        symbols = client.get_symbols()
        self.assertEqual(len(symbols), 1)
        self.assertEqual(symbols[0].symbol, "BTC_USDT")
        print("✓ Test 14: type-3 symbols excluded from get_symbols")

    def test_get_symbol_found(self):
        """Test 15 — get_symbol returns correct ExchangeSymbol."""
        client = _make_anon_client()
        raw = [_raw_symbol("BTC_USDT"), _raw_symbol("ETH_USDT", base="ETH")]
        resp = _make_response(_symbols_envelope(raw))
        self._patch_session(client, resp)

        sym = client.get_symbol("btc_usdt")  # lower-case input
        self.assertEqual(sym.symbol, "BTC_USDT")
        print("✓ Test 15: get_symbol found (case-insensitive)")

    def test_get_symbol_accepts_compact_market_format(self):
        """Metadata lookup accepts BTCUSDT while returning canonical BTC_USDT."""
        client = _make_anon_client()
        resp = _make_response(_symbols_envelope([_raw_symbol("BTC_USDT")]))
        self._patch_session(client, resp)

        sym = client.get_symbol(" btcusdt ")
        self.assertEqual(sym.symbol, "BTC_USDT")

    def test_get_symbol_not_found(self):
        """Test 16 — unknown symbol → TokocryptoCapabilityError."""
        client = _make_anon_client()
        resp = _make_response(_symbols_envelope([_raw_symbol("BTC_USDT")]))
        self._patch_session(client, resp)

        with self.assertRaises(TokocryptoCapabilityError):
            client.get_symbol("XYZ_USDT")
        print("✓ Test 16: unknown symbol → CapabilityError")

    # ── get_ticker ─────────────────────────────────────────────────────

    def test_get_ticker_binance_style(self):
        """Test 17 — Binance-style {"price": "..."} response."""
        client = _make_anon_client()
        resp = _make_response({"price": "65432.10"})
        client._session.get = MagicMock(return_value=resp)

        price = client.get_ticker("BTC_USDT")
        self.assertAlmostEqual(price, 65432.10)
        # Confirm underscore was stripped in the URL call
        call_args = client._session.get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params") or {}
        self.assertEqual(params.get("symbol"), "BTCUSDT")
        print("✓ Test 17: get_ticker Binance-style, underscore stripped")

    def test_get_ticker_wrapped_response(self):
        """Test 18 — wrapped data.price response."""
        client = _make_anon_client()
        resp = _make_response({"code": 0, "data": {"price": "0.04567"}, "timestamp": 1700000000000})
        client._session.get = MagicMock(return_value=resp)

        price = client.get_ticker("ADA_USDT")
        self.assertAlmostEqual(price, 0.04567)
        print("✓ Test 18: get_ticker wrapped data.price")

    def test_get_ticker_missing_price_raises(self):
        """Test 19 — no price field in response → MalformedResponseError."""
        client = _make_anon_client()
        # Primary call returns empty dict (no price); fallback also returns empty
        empty_resp = _make_response({"code": 0, "timestamp": 1700000000000})
        client._session.get = MagicMock(return_value=empty_resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_ticker("UNKNOWN_USDT")
        print("✓ Test 19: missing price → MalformedResponseError")

    # ── get_depth ──────────────────────────────────────────────────────

    def test_get_depth_binance_style(self):
        """Test 20 — raw Binance-style depth response."""
        client = _make_anon_client()
        resp = _make_response({
            "lastUpdateId": 9999,
            "bids": [["65000.0", "1.5"]],
            "asks": [["65001.0", "0.8"]],
        })
        client._session.get = MagicMock(return_value=resp)

        depth = client.get_depth("BTC_USDT", limit=5)
        self.assertEqual(depth["bids"], [["65000.0", "1.5"]])
        self.assertEqual(depth["asks"], [["65001.0", "0.8"]])
        self.assertEqual(depth["lastUpdateId"], 9999)
        print("✓ Test 20: get_depth Binance-style response")

    def test_get_depth_wrapped_envelope(self):
        """Test 21 — depth inside data envelope."""
        client = _make_anon_client()
        resp = _make_response({
            "code": 0,
            "data": {
                "lastUpdateId": 1234,
                "bids": [["100.0", "5.0"]],
                "asks": [["100.1", "3.0"]],
            },
        })
        client._session.get = MagicMock(return_value=resp)

        depth = client.get_depth("ETH_USDT")
        self.assertEqual(depth["bids"], [["100.0", "5.0"]])
        print("✓ Test 21: get_depth wrapped envelope")

    def test_get_depth_malformed_response(self):
        """Test 22 — response missing both 'bids' and 'data' → MalformedResponseError."""
        client = _make_anon_client()
        resp = _make_response({"code": 0, "something_else": True})
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_depth("BTC_USDT")
        print("✓ Test 22: missing bids → MalformedResponseError")

    def test_get_depth_limit_snapped(self):
        """Test 23 — invalid limit is snapped to nearest valid value."""
        client = _make_anon_client()
        resp = _make_response({"bids": [], "asks": [], "lastUpdateId": 0})
        client._session.get = MagicMock(return_value=resp)

        # 15 → nearest valid limit is 10 or 20; either is acceptable
        client.get_depth("BTC_USDT", limit=15)
        call_params = client._session.get.call_args.kwargs.get("params", {})
        self.assertIn(call_params.get("limit"), {5, 10, 20, 50, 100, 500})
        print(f"✓ Test 23: limit=15 snapped to {call_params.get('limit')}")


# ---------------------------------------------------------------------------
# Test: Authenticated read-only endpoints
# ---------------------------------------------------------------------------

class TestAuthenticatedEndpoints(unittest.TestCase):
    """Tests 24–28: get_balances, get_balance, get_account."""

    _ACCOUNT_RESP = {
        "code": 0,
        "msg": "success",
        "data": {
            "makerCommission": "10.0",
            "takerCommission": "10.0",
            "canTrade": 1,
            "accountAssets": [
                {"asset": "USDT",  "free": "500.0",  "locked": "50.0"},
                {"asset": "BTC",   "free": "0.05",   "locked": "0.0"},
                {"asset": "ETH",   "free": "1.2",    "locked": "0.3"},
            ],
        },
        "timestamp": 1700000000000,
    }

    def _patch_signed(self, client: TokocryptoClient, response: MagicMock):
        client._session.get = MagicMock(return_value=response)

    # ── get_balances ───────────────────────────────────────────────────

    def test_get_balances_happy(self):
        """Test 24 — get_balances parses all assets correctly."""
        client = _make_client()
        resp = _make_response(self._ACCOUNT_RESP)
        self._patch_signed(client, resp)

        balances = client.get_balances()
        self.assertEqual(len(balances), 3)

        usdt = next(b for b in balances if b.asset == "USDT")
        self.assertAlmostEqual(usdt.free,   500.0)
        self.assertAlmostEqual(usdt.locked, 50.0)
        self.assertAlmostEqual(usdt.total,  550.0)

        btc = next(b for b in balances if b.asset == "BTC")
        self.assertAlmostEqual(btc.free, 0.05)
        print("✓ Test 24: get_balances parses all assets")

    def test_get_balances_not_a_list(self):
        """Test 25 — accountAssets is not a list → MalformedResponseError."""
        client = _make_client()
        bad_resp = {
            "code": 0, "data": {"accountAssets": "oops"}, "timestamp": 1700000000000
        }
        self._patch_signed(client, _make_response(bad_resp))

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_balances()
        print("✓ Test 25: accountAssets not list → MalformedResponseError")

    # ── get_balance ────────────────────────────────────────────────────

    def test_get_balance_found(self):
        """Test 26 — get_balance returns the correct single-asset balance."""
        client = _make_client()
        resp = _make_response(self._ACCOUNT_RESP)
        self._patch_signed(client, resp)

        bal = client.get_balance("eth")  # lower-case input
        self.assertEqual(bal.asset, "ETH")
        self.assertAlmostEqual(bal.free, 1.2)
        self.assertAlmostEqual(bal.locked, 0.3)
        print("✓ Test 26: get_balance found (case-insensitive)")

    def test_get_balance_absent_returns_zero(self):
        """Test 27 — asset not in account → returns zero ExchangeBalance."""
        client = _make_client()
        resp = _make_response(self._ACCOUNT_RESP)
        self._patch_signed(client, resp)

        bal = client.get_balance("SOL")
        self.assertEqual(bal.asset, "SOL")
        self.assertAlmostEqual(bal.free,   0.0)
        self.assertAlmostEqual(bal.locked, 0.0)
        self.assertAlmostEqual(bal.total,  0.0)
        print("✓ Test 27: absent asset → zero balance (no KeyError)")

    # ── get_account ────────────────────────────────────────────────────

    def test_get_account_missing_data_field(self):
        """Test 28 — 'data' field absent → MalformedResponseError."""
        client = _make_client()
        resp = _make_response({"code": 0, "msg": "success", "timestamp": 1700000000000})
        self._patch_signed(client, resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_account()
        print("✓ Test 28: missing 'data' in account response → MalformedResponseError")


# ---------------------------------------------------------------------------
# Test: Error mapping
# ---------------------------------------------------------------------------

class TestErrorMapping(unittest.TestCase):
    """Tests 29–36: error classification from HTTP status and response body."""

    def _client_with_response(self, resp: MagicMock, authenticated: bool = False):
        client = _make_client() if authenticated else _make_anon_client()
        client._session.get = MagicMock(return_value=resp)
        return client

    def test_unauthenticated_signed_call_raises_auth_error(self):
        """Test 29 — no credentials → TokocryptoAuthError before any HTTP call."""
        client = _make_anon_client()
        with self.assertRaises(TokocryptoAuthError) as ctx:
            client.get_balances()
        self.assertIn("TOKOCRYPTO_API_KEY", str(ctx.exception))
        print("✓ Test 29: unauthenticated signed call → TokocryptoAuthError")

    def test_network_connection_error(self):
        """Test 30 — requests.ConnectionError → TokocryptoNetworkError."""
        client = _make_anon_client()
        client._session.get = MagicMock(
            side_effect=requests.exceptions.ConnectionError("refused")
        )
        # get_server_time() propagates TokocryptoNetworkError (unlike ping() which
        # intentionally swallows it and returns False)
        with self.assertRaises(TokocryptoNetworkError):
            client.get_server_time()
        print("✓ Test 30: ConnectionError → TokocryptoNetworkError")

    def test_timeout_error(self):
        """Test 31 — requests.Timeout → TokocryptoNetworkError."""
        client = _make_anon_client()
        client._session.get = MagicMock(
            side_effect=requests.exceptions.Timeout("timed out")
        )
        with self.assertRaises(TokocryptoNetworkError):
            client.get_server_time()
        print("✓ Test 31: Timeout → TokocryptoNetworkError")

    def test_http_429_rate_limit(self):
        """Test 32 — HTTP 429 → TokocryptoRateLimitError (subclass of TokocryptoAPIError)."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 429
        resp.headers = {"Retry-After": "30"}
        resp.text = "Too Many Requests"
        resp.json.side_effect = ValueError("no json")

        client = self._client_with_response(resp)
        with self.assertRaises(TokocryptoRateLimitError) as ctx:
            client.get_server_time()
        self.assertIn("429", str(ctx.exception))
        print("✓ Test 32: HTTP 429 → TokocryptoRateLimitError")

    def test_http_401_auth_error(self):
        """Test 33 — HTTP 401 → TokocryptoAuthError."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.text = "Unauthorized"
        resp.headers = {}
        resp.json.side_effect = ValueError("no json")

        client = self._client_with_response(resp)
        with self.assertRaises(TokocryptoAuthError):
            client.get_server_time()
        print("✓ Test 33: HTTP 401 → TokocryptoAuthError")

    def test_api_code_2014_auth_error(self):
        """Test 34 — body code -2014 (invalid API key) → TokocryptoAuthError."""
        resp = _make_response(
            {"code": -2014, "msg": "API-key format invalid."}, status=200
        )
        client = _make_anon_client()
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoAuthError):
            client.get_server_time()
        print("✓ Test 34: body code -2014 → TokocryptoAuthError")

    def test_api_generic_error_code(self):
        """Test 35 — non-zero API code → TokocryptoAPIError with correct code and msg."""
        resp = _make_response({"code": 1001, "msg": "Symbol not found"}, status=200)
        client = _make_anon_client()
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoAPIError) as ctx:
            client.get_server_time()
        self.assertEqual(ctx.exception.code, 1001)
        self.assertEqual(ctx.exception.msg,  "Symbol not found")
        print(f"✓ Test 35: API code 1001 → TokocryptoAPIError(code=1001, msg='Symbol not found')")

    def test_non_json_response(self):
        """Test 36 — non-JSON body → TokocryptoMalformedResponseError."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.headers = {}
        resp.text = "<html>Bad Gateway</html>"
        resp.json.side_effect = ValueError("not json")

        client = _make_anon_client()
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_server_time()
        print("✓ Test 36: non-JSON response → TokocryptoMalformedResponseError")


# ---------------------------------------------------------------------------
# Test: ping
# ---------------------------------------------------------------------------

class TestPing(unittest.TestCase):
    """Tests 37–39: ping() return value under various conditions."""

    def test_ping_reachable(self):
        """Test 37 — successful /time response → True."""
        client = _make_anon_client()
        client._session.get = MagicMock(
            return_value=_make_response({"code": 0, "timestamp": 1700000000000})
        )
        self.assertTrue(client.ping())
        print("✓ Test 37: ping() reachable → True")

    def test_ping_network_error_returns_false(self):
        """Test 38 — ConnectionError → False (not raised)."""
        client = _make_anon_client()
        client._session.get = MagicMock(
            side_effect=requests.exceptions.ConnectionError("refused")
        )
        self.assertFalse(client.ping())
        print("✓ Test 38: ping() ConnectionError → False")

    def test_ping_api_error_still_true(self):
        """Test 39 — API responds with error code → True (exchange is up)."""
        client = _make_anon_client()
        client._session.get = MagicMock(
            return_value=_make_response({"code": 500, "msg": "maintenance"})
        )
        self.assertTrue(client.ping())
        print("✓ Test 39: ping() API error → True (exchange reachable)")


# ---------------------------------------------------------------------------
# Test: build() factory
# ---------------------------------------------------------------------------

class TestBuildFactory(unittest.TestCase):
    """Tests 40–41: TokocryptoClient.build() reads env correctly."""

    def test_build_no_env_vars(self):
        """Test 40 — no env vars → client has no credentials."""
        with patch.dict("os.environ", {}, clear=False):
            # Remove keys if present
            import os
            os.environ.pop("TOKOCRYPTO_API_KEY",    None)
            os.environ.pop("TOKOCRYPTO_API_SECRET", None)
            client = TokocryptoClient.build()
        self.assertIsNone(client._api_key)
        self.assertIsNone(client._api_secret)
        print("✓ Test 40: build() without env vars → no credentials")

    def test_build_with_env_vars(self):
        """Test 41 — env vars present → credentials loaded (value not logged)."""
        with patch.dict("os.environ", {
            "TOKOCRYPTO_API_KEY":    "mykey123",
            "TOKOCRYPTO_API_SECRET": "mysecret456",
        }):
            client = TokocryptoClient.build()
        self.assertEqual(client._api_key, "mykey123")
        # We verify secret was loaded (truthy), but do NOT print or assert its value
        self.assertTrue(bool(client._api_secret))
        print("✓ Test 41: build() with env vars → credentials loaded")


# ---------------------------------------------------------------------------
# Test: HMAC signature
# ---------------------------------------------------------------------------

class TestSignParams(unittest.TestCase):
    """Tests 42–43: _sign_params produces correct timestamp, recvWindow, signature."""

    def test_sign_params_fields_present(self):
        """Test 42 — output contains timestamp, recvWindow, signature."""
        client = _make_client(api_key="k", api_secret="s")
        result = client._sign_params({"symbol": "BTC_USDT"})
        self.assertIn("timestamp",   result)
        self.assertIn("recvWindow",  result)
        self.assertIn("signature",   result)
        self.assertIn("symbol",      result)
        self.assertEqual(result["recvWindow"], 5000)
        print("✓ Test 42: _sign_params output has timestamp, recvWindow, signature")

    def test_sign_params_signature_correct(self):
        """Test 43 — signature matches manual HMAC-SHA256 computation."""
        client = _make_client(api_key="testkey", api_secret="testsecret")

        # Freeze timestamp by injecting directly
        base_params = {"symbol": "BTC_USDT", "timestamp": 1700000000000, "recvWindow": 5000}
        qs = urlencode(base_params)
        expected_sig = hmac.new(
            b"testsecret", qs.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Patch time.time so _sign_params uses our fixed timestamp
        with patch("core.clients.tokocrypto_client.time.time", return_value=1700000000.0):
            result = client._sign_params({"symbol": "BTC_USDT"})

        self.assertEqual(result["signature"], expected_sig)
        print(f"✓ Test 43: HMAC signature correct: {expected_sig[:16]}...")

    def test_sign_params_does_not_mutate_input(self):
        """_sign_params must not mutate the caller's original dict."""
        client = _make_client()
        original = {"symbol": "ETH_USDT"}
        original_copy = dict(original)
        client._sign_params(original)
        self.assertEqual(original, original_copy)
        print("✓ Bonus: _sign_params does not mutate caller's dict")


# ---------------------------------------------------------------------------
# Test: Safety — no order-placement methods
# ---------------------------------------------------------------------------

class TestNoOrderPlacement(unittest.TestCase):
    """Test 44 — ensure no accidentally exposed order methods exist."""

    _FORBIDDEN = [
        "create_order", "place_order", "new_order",
        "buy", "sell",
        "create_oco_order", "place_oco_order", "place_oco",
        "futures_create_order",
        "cancel_order",
        "withdraw", "transfer",
    ]

    def test_no_order_methods(self):
        """Test 44 — none of the forbidden order/mutation methods exist."""
        client = _make_anon_client()
        for name in self._FORBIDDEN:
            self.assertFalse(
                hasattr(client, name),
                msg=f"TokocryptoClient must NOT have method '{name}' at this stage",
            )
        print(f"✓ Test 44: none of {self._FORBIDDEN} exist on TokocryptoClient")


# ---------------------------------------------------------------------------
# Test: repr safety
# ---------------------------------------------------------------------------

class TestRepr(unittest.TestCase):
    """__repr__ must not expose credentials."""

    def test_repr_no_secret(self):
        client = _make_client(api_key="MYKEY", api_secret="TOPSECRET")
        r = repr(client)
        self.assertNotIn("MYKEY",      r)
        self.assertNotIn("TOPSECRET",  r)
        self.assertIn("authenticated=True", r)
        print(f"✓ repr safe: {r}")

    def test_repr_unauthenticated(self):
        client = _make_anon_client()
        self.assertIn("authenticated=False", repr(client))
        print("✓ repr unauthenticated shows authenticated=False")




# ---------------------------------------------------------------------------
# Stage 1 Extension Tests
# ---------------------------------------------------------------------------

class TestSTPSymbolParsing(unittest.TestCase):
    """Tests 52–54: ExchangeSymbol STP field parsing (backward compat)."""

    def _raw_symbol_with_stp(self) -> dict:
        """Raw symbol dict with STP fields present (2026-06-05 API shape)."""
        raw = _raw_symbol()
        raw["defaultSelfTradePreventionMode"]  = "EXPIRE_MAKER"
        raw["allowedSelfTradePreventionModes"] = ["EXPIRE_TAKER", "EXPIRE_MAKER", "EXPIRE_BOTH"]
        return raw

    def test_symbol_parser_with_stp_fields(self):
        """Test 52 — STP fields parsed correctly when present."""
        sym = TokocryptoClient._parse_symbol(self._raw_symbol_with_stp())
        self.assertEqual(sym.default_stp_mode,  "EXPIRE_MAKER")
        self.assertEqual(sym.allowed_stp_modes, ["EXPIRE_TAKER", "EXPIRE_MAKER", "EXPIRE_BOTH"])
        print("✓ Test 52: STP fields parsed when present")

    def test_symbol_parser_without_stp_fields(self):
        """Test 53 — No STP fields in response → safe defaults, no KeyError."""
        raw = _raw_symbol()
        # Ensure keys are definitely absent
        raw.pop("defaultSelfTradePreventionMode",  None)
        raw.pop("allowedSelfTradePreventionModes", None)
        sym = TokocryptoClient._parse_symbol(raw)
        self.assertIsNone(sym.default_stp_mode)
        self.assertEqual(sym.allowed_stp_modes, [])
        # Existing fields must be unaffected
        self.assertEqual(sym.symbol, "BTC_USDT")
        self.assertAlmostEqual(sym.tick_size, 0.01)
        print("✓ Test 53: No STP fields → safe defaults, no KeyError (backward compat)")

    def test_symbol_parser_stp_default_not_zero(self):
        """Test 54 — default_stp_mode must be None (not 0) when field absent."""
        raw = _raw_symbol()
        raw.pop("defaultSelfTradePreventionMode", None)
        sym = TokocryptoClient._parse_symbol(raw)
        # Must be None, NOT integer 0 — STP=0 must never be hardcoded as fallback
        self.assertIsNone(sym.default_stp_mode)
        self.assertNotEqual(sym.default_stp_mode, 0)
        print("✓ Test 54: default_stp_mode is None (not 0) when absent — no hardcoded STP=0")


class TestGetExecutionRules(unittest.TestCase):
    """Tests 55–59: get_execution_rules() method."""

    _EXECUTION_RULES_RESP = {
        "symbolRules": [
            {
                "symbol": "BTCUSDT",
                "rules": [
                    {
                        "ruleType":         "PRICE_RANGE",
                        "bidLimitMultUp":   "2.0",
                        "bidLimitMultDown": "0.5",
                        "askLimitMultUp":   "2.0",
                        "askLimitMultDown": "0.5",
                    }
                ],
            }
        ]
    }

    def test_get_execution_rules_success(self):
        """Test 55 — successful response parsed into ExecutionRules."""
        client = _make_anon_client()
        client._session.get = MagicMock(return_value=_make_response(self._EXECUTION_RULES_RESP))

        result = client.get_execution_rules("BTC_USDT")

        self.assertIsInstance(result, ExecutionRules)
        self.assertEqual(result.symbol, "BTCUSDT")
        self.assertEqual(len(result.rules), 1)
        rule = result.rules[0]
        self.assertIsInstance(rule, ExecutionRulesRule)
        self.assertEqual(rule.rule_type, "PRICE_RANGE")
        self.assertAlmostEqual(rule.bid_limit_mult_up,   2.0)
        self.assertAlmostEqual(rule.bid_limit_mult_down, 0.5)
        self.assertAlmostEqual(rule.ask_limit_mult_up,   2.0)
        self.assertAlmostEqual(rule.ask_limit_mult_down, 0.5)
        self.assertIsInstance(result.raw, dict)

        # Confirm URL used the MARKET_BASE_URL (tokocrypto.site) and executionRules path
        call_args = client._session.get.call_args
        called_url = call_args[0][0] if call_args[0] else call_args.args[0]
        self.assertIn("tokocrypto.site",  called_url)
        self.assertIn("executionRules",   called_url)
        print("✓ Test 55: get_execution_rules success — correct fields, correct URL")

    def test_get_execution_rules_symbol_not_in_response(self):
        """Test 56 — symbol not present in symbolRules → empty ExecutionRules (not error)."""
        client = _make_anon_client()
        resp = {"symbolRules": [{"symbol": "ETHUSDT", "rules": []}]}
        client._session.get = MagicMock(return_value=_make_response(resp))

        result = client.get_execution_rules("BTC_USDT")

        self.assertIsInstance(result, ExecutionRules)
        self.assertEqual(result.rules, [])
        print("✓ Test 56: symbol not in symbolRules → empty ExecutionRules (not error)")

    def test_get_execution_rules_malformed_response(self):
        """Test 57 — no 'symbolRules' key → MalformedResponseError."""
        client = _make_anon_client()
        # Response has neither 'symbolRules' top-level nor in 'data'
        resp = {"code": 0, "data": {"something": "else"}}
        client._session.get = MagicMock(return_value=_make_response(resp))

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_execution_rules("BTC_USDT")
        print("✓ Test 57: no symbolRules → MalformedResponseError")

    def test_get_execution_rules_symbol_required(self):
        """Test 58 — empty symbol raises ValueError."""
        client = _make_anon_client()
        with self.assertRaises(ValueError):
            client.get_execution_rules("")
        with self.assertRaises(ValueError):
            client.get_execution_rules(None)
        print("✓ Test 58: empty/None symbol → ValueError")

    def test_get_execution_rules_multiplier_partially_absent(self):
        """Test 59 — rule with only bidLimitMultUp; other multipliers absent → None."""
        client = _make_anon_client()
        resp = {
            "symbolRules": [
                {
                    "symbol": "BTCUSDT",
                    "rules": [
                        {
                            "ruleType":       "PRICE_RANGE",
                            "bidLimitMultUp": "1.5",
                            # bidLimitMultDown, askLimitMultUp, askLimitMultDown absent
                        }
                    ],
                }
            ]
        }
        client._session.get = MagicMock(return_value=_make_response(resp))

        result = client.get_execution_rules("BTCUSDT")

        self.assertIsInstance(result.rules[0], ExecutionRulesRule)
        self.assertAlmostEqual(result.rules[0].bid_limit_mult_up, 1.5)
        self.assertIsNone(result.rules[0].bid_limit_mult_down)
        self.assertIsNone(result.rules[0].ask_limit_mult_up)
        self.assertIsNone(result.rules[0].ask_limit_mult_down)
        print("✓ Test 59: partial multipliers → missing fields are None")


class TestGetReferencePrice(unittest.TestCase):
    """Tests 60–61: get_reference_price() method."""

    def test_get_reference_price_success(self):
        """Test 60 — successful response returns float."""
        client = _make_anon_client()
        resp_body = {"symbol": "BTCUSDT", "referencePrice": "65432.10", "timestamp": 1700000000000}
        client._session.get = MagicMock(return_value=_make_response(resp_body))

        result = client.get_reference_price("BTC_USDT")

        self.assertIsInstance(result, float)
        self.assertAlmostEqual(result, 65432.10)

        # Confirm URL used tokocrypto.site and referencePrice path
        call_args = client._session.get.call_args
        called_url = call_args[0][0] if call_args[0] else call_args.args[0]
        self.assertIn("tokocrypto.site", called_url)
        self.assertIn("referencePrice",  called_url)
        print("✓ Test 60: get_reference_price returns float, correct URL")

    def test_get_reference_price_null(self):
        """Test 61 — null referencePrice → None (rule not enforced, not an error)."""
        client = _make_anon_client()
        resp_body = {"symbol": "BTCUSDT", "referencePrice": None, "timestamp": 1700000000000}
        client._session.get = MagicMock(return_value=_make_response(resp_body))

        result = client.get_reference_price("BTCUSDT")

        self.assertIsNone(result)
        print("✓ Test 61: null referencePrice → None (price range rule not enforced)")


class TestHTTP5XXSemantics(unittest.TestCase):
    """Tests 62–66: HTTP 5XX raises TokocryptoUnknownOrderStatus, not generic error."""

    def _make_5xx_response(self, status: int, body: dict | None = None, text: str = "") -> MagicMock:
        r = MagicMock(spec=requests.Response)
        r.status_code = status
        r.headers = {}
        if body is not None:
            r.json.return_value = body
            r.text = str(body)
        else:
            r.json.side_effect = ValueError("not json")
            r.text = text or f"HTTP {status} error"
        return r

    def test_http_5xx_raises_unknown_order_status(self):
        """Test 62 — HTTP 500 → TokocryptoUnknownOrderStatus (subclass of TokocryptoError)."""
        client = _make_anon_client()
        resp = self._make_5xx_response(500, body={"code": 500, "msg": "Internal Server Error"})
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoUnknownOrderStatus) as ctx:
            client.get_server_time()

        self.assertIsInstance(ctx.exception, TokocryptoUnknownOrderStatus)
        self.assertIsInstance(ctx.exception, Exception)  # subclass chain
        # Confirm it is also a TokocryptoError
        from core.clients.tokocrypto_client import TokocryptoError
        self.assertIsInstance(ctx.exception, TokocryptoError)
        print("✓ Test 62: HTTP 500 → TokocryptoUnknownOrderStatus (subclass of TokocryptoError)")

    def test_http_502_raises_unknown_order_status(self):
        """Test 63 — HTTP 502 plain text body → TokocryptoUnknownOrderStatus with correct code."""
        client = _make_anon_client()
        resp = self._make_5xx_response(502, text="Bad Gateway")
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoUnknownOrderStatus) as ctx:
            client.get_server_time()

        self.assertEqual(ctx.exception.status_code, 502)
        print("✓ Test 63: HTTP 502 → TokocryptoUnknownOrderStatus with status_code=502")

    def test_unknown_order_status_not_raised_for_4xx(self):
        """Test 64 — HTTP 404 → TokocryptoAPIError (not TokocryptoUnknownOrderStatus)."""
        client = _make_anon_client()
        resp = _make_response({"code": 404, "msg": "Not found"}, status=404)
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoAPIError) as ctx:
            client.get_server_time()

        # Must NOT be TokocryptoUnknownOrderStatus
        self.assertNotIsInstance(ctx.exception, TokocryptoUnknownOrderStatus)
        print("✓ Test 64: HTTP 404 → TokocryptoAPIError (not UnknownOrderStatus)")

    def test_unknown_order_status_not_raised_for_429(self):
        """Test 65 — HTTP 429 → TokocryptoRateLimitError (rate-limit check has priority)."""
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 429
        resp.headers = {"Retry-After": "10"}
        resp.text = "Too Many Requests"
        resp.json.side_effect = ValueError("no json")

        client = _make_anon_client()
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoRateLimitError):
            client.get_server_time()
        print("✓ Test 65: HTTP 429 → TokocryptoRateLimitError (not UnknownOrderStatus)")

    def test_no_blind_retry_attribute(self):
        """Test 66 — TokocryptoUnknownOrderStatus has no retry/should_retry attribute."""
        exc = TokocryptoUnknownOrderStatus(status_code=500)
        self.assertFalse(hasattr(exc, "retry"))
        self.assertFalse(hasattr(exc, "should_retry"))
        print("✓ Test 66: TokocryptoUnknownOrderStatus has no retry attribute (no blind retry)")


# ---------------------------------------------------------------------------
# Test: get_open_orders (Fail-Closed & Contract Verification)
# ---------------------------------------------------------------------------

class TestGetOpenOrders(unittest.TestCase):
    """Regression tests for get_open_orders: valid envelopes, empty responses, malformed payloads, and API errors."""

    def test_get_open_orders_nested_list_success(self):
        """Valid nested list format: {"code": 0, "msg": "success", "data": {"list": [...]}}."""
        client = _make_client()
        orders_data = [
            {"orderId": "1001", "symbol": "BTC_USDT", "side": 0, "type": 1, "price": "60000"},
            {"orderId": "1002", "symbol": "BTC_USDT", "side": 1, "type": 1, "price": "65000"},
        ]
        body = {"code": 0, "msg": "success", "data": {"list": orders_data}}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        result = client.get_open_orders("BTC_USDT")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["orderId"], "1001")
        self.assertEqual(result[1]["orderId"], "1002")

        # Verify parameters passed to GET request
        call_kwargs = client._session.get.call_args[1]
        params = call_kwargs["params"]
        self.assertEqual(params["type"], 1)
        self.assertEqual(params["symbol"], "BTC_USDT")

    def test_get_open_orders_direct_list_success(self):
        """Valid direct list format: {"code": 0, "msg": "success", "data": [...]}}."""
        client = _make_client()
        orders_data = [
            {"orderId": "2001", "symbol": "ETH_IDR", "side": 0, "type": 1, "price": "50000000"}
        ]
        body = {"code": 0, "msg": "success", "data": orders_data}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        result = client.get_open_orders("eth_idr")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["orderId"], "2001")

        call_kwargs = client._session.get.call_args[1]
        params = call_kwargs["params"]
        self.assertEqual(params["symbol"], "ETH_IDR")

    def test_get_open_orders_empty_list_valid(self):
        """Valid empty response: zero open orders returns empty list []."""
        client = _make_client()
        body = {"code": 0, "msg": "success", "data": {"list": []}}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        result = client.get_open_orders()
        self.assertEqual(result, [])

    def test_get_open_orders_without_symbol_filter(self):
        """Calling without symbol parameter does not include 'symbol' in params."""
        client = _make_client()
        body = {"code": 0, "msg": "success", "data": {"list": []}}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        result = client.get_open_orders()
        self.assertEqual(result, [])
        call_kwargs = client._session.get.call_args[1]
        params = call_kwargs["params"]
        self.assertNotIn("symbol", params)
        self.assertEqual(params["type"], 1)

    def test_get_open_orders_missing_data_field_raises_malformed(self):
        """Missing 'data' field raises TokocryptoMalformedResponseError (fail-closed)."""
        client = _make_client()
        body = {"code": 0, "msg": "success"}  # data key missing
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_open_orders("BTC_USDT")

    def test_get_open_orders_data_none_raises_malformed(self):
        """'data': None raises TokocryptoMalformedResponseError (fail-closed)."""
        client = _make_client()
        body = {"code": 0, "msg": "success", "data": None}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_open_orders()

    def test_get_open_orders_data_unexpected_type_raises_malformed(self):
        """'data' as integer, string, or boolean raises TokocryptoMalformedResponseError."""
        client = _make_client()
        for invalid_data in [123, "not_a_list", False]:
            body = {"code": 0, "msg": "success", "data": invalid_data}
            resp = _make_response(body)
            client._session.get = MagicMock(return_value=resp)

            with self.assertRaises(TokocryptoMalformedResponseError):
                client.get_open_orders()

    def test_get_open_orders_nested_list_not_a_list_raises_malformed(self):
        """'data': {'list': 'string'} raises TokocryptoMalformedResponseError."""
        client = _make_client()
        body = {"code": 0, "msg": "success", "data": {"list": "malformed"}}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_open_orders()

    def test_get_open_orders_list_items_not_dicts_raises_malformed(self):
        """Items inside the list that are not dicts raise TokocryptoMalformedResponseError."""
        client = _make_client()
        body = {"code": 0, "msg": "success", "data": {"list": ["order_id_string", 12345]}}
        resp = _make_response(body)
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoMalformedResponseError):
            client.get_open_orders()

    def test_get_open_orders_api_error_code_raises_api_error(self):
        """API returning code != 0 raises TokocryptoAPIError."""
        client = _make_client()
        body = {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow"}
        resp = _make_response(body, status=200)
        client._session.get = MagicMock(return_value=resp)

        with self.assertRaises(TokocryptoAPIError) as ctx:
            client.get_open_orders()
        self.assertEqual(ctx.exception.code, -1021)

    def test_get_open_orders_network_error_raises_network_error(self):
        """Connection timeout raises TokocryptoNetworkError."""
        client = _make_client()
        client._session.get = MagicMock(side_effect=requests.exceptions.Timeout("Connection timed out"))

        with self.assertRaises(TokocryptoNetworkError):
            client.get_open_orders()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
