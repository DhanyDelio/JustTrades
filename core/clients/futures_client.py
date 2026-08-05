"""
futures_client.py — Binance Futures Testnet client + market data helpers.

Extracted from core/futures_trade_executor.py (god file) — Tahap 6 refactor.
Zero logic changes; purely relocated from module-level functions to a class.

Wraps:
    get_futures_client()             → FuturesClient.build()       (class method)
    ping_futures(client)             → FuturesClient.ping()
    get_futures_symbol_constraints() → FuturesClient.get_symbol_constraints()
    get_futures_price()              → FuturesClient.get_price()
    get_funding_rate()               → FuturesClient.get_funding_rate()
    _futures_get()                   → FuturesClient._get()         (private)
"""

from __future__ import annotations

import os

import requests


class FuturesClient:
    """
    Thin wrapper around a python-binance Client configured for
    Binance Futures Testnet.  All market-data helpers live here so
    callers never import directly from the god file for connectivity
    or price-fetching concerns.
    """

    FUTURES_BASE = "https://testnet.binancefuture.com/fapi"

    def __init__(self, client):
        """Wrap an already-constructed python-binance Client."""
        self._client = client

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(cls) -> "FuturesClient":
        """
        Connect to Binance Futures Testnet and return a FuturesClient.
        Keys: BINANCE_FUTURES_TESTNET_API_KEY / _API_SECRET in .env.
        """
        try:
            from binance.client import Client
        except ImportError:
            raise ImportError("pip install python-binance --break-system-packages")

        api_key    = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY")
        api_secret = os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET")

        if not api_key or not api_secret:
            raise RuntimeError(
                "Futures API keys not found in .env\n"
                "Set BINANCE_FUTURES_TESTNET_API_KEY and "
                "BINANCE_FUTURES_TESTNET_API_SECRET\n"
                "Register at https://testnet.binancefuture.com"
            )

        client = Client(api_key, api_secret, testnet=True, tld="com")
        client.FUTURES_URL = cls.FUTURES_BASE
        return cls(client)

    # ------------------------------------------------------------------
    # Expose underlying client (needed by callers that call SDK methods
    # directly, e.g. futures_create_order, futures_get_order, etc.)
    # ------------------------------------------------------------------

    @property
    def raw(self):
        """The underlying python-binance Client instance."""
        return self._client

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Verify futures testnet connectivity."""
        try:
            self._client.futures_ping()
            return True
        except Exception:
            try:
                resp = requests.get(
                    f"{self.FUTURES_BASE}/v1/ping", timeout=5)
                return resp.status_code == 200
            except Exception:
                return False

    # ------------------------------------------------------------------
    # Market data helpers
    # ------------------------------------------------------------------

    def get_symbol_constraints(self, symbol: str) -> dict:
        """
        Fetch futures-specific constraints: tick size, step size, min notional.
        Uses futures_exchange_info() — different from spot exchange_info.
        """
        try:
            info = self._client.futures_exchange_info()
        except Exception:
            info = requests.get(
                f"{self.FUTURES_BASE}/v1/exchangeInfo", timeout=10
            ).json()

        for sym_info in info.get("symbols", []):
            if sym_info["symbol"] != symbol:
                continue
            constraints = {
                "min_qty":      0.0,
                "step_size":    0.0,
                "tick_size":    0.0,
                "min_notional": 5.0,
            }
            for f in sym_info.get("filters", []):
                ft = f["filterType"]
                if ft == "LOT_SIZE":
                    constraints["min_qty"]   = float(f["minQty"])
                    constraints["step_size"] = float(f["stepSize"])
                elif ft == "PRICE_FILTER":
                    constraints["tick_size"] = float(f["tickSize"])
                elif ft == "MIN_NOTIONAL":
                    constraints["min_notional"] = float(f.get("notional", 5.0))
            return constraints

        raise ValueError(f"Symbol {symbol} not found in futures exchange info")

    def get_price(self, symbol: str) -> float:
        """Fetch current mark price from futures testnet."""
        try:
            ticker = self._client.futures_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except Exception:
            resp = requests.get(
                f"{self.FUTURES_BASE}/v1/ticker/price",
                params={"symbol": symbol}, timeout=5,
            ).json()
            return float(resp["price"])

    def get_funding_rate(self, symbol: str) -> float | None:
        """
        Fetch current funding rate for symbol.
        Returns float (e.g. 0.0001 = 0.01%) or None if unavailable.
        """
        try:
            data = self._client.futures_funding_rate(symbol=symbol, limit=1)
            if data:
                return float(data[-1]["fundingRate"])
        except Exception:
            pass
        try:
            resp = requests.get(
                f"{self.FUTURES_BASE}/v1/fundingRate",
                params={"symbol": symbol, "limit": 1}, timeout=5,
            ).json()
            if resp:
                return float(resp[-1]["fundingRate"])
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Private low-level GET helper
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        """Direct GET to futures testnet endpoint."""
        base = f"{self.FUTURES_BASE}/v1"
        try:
            resp = requests.get(f"{base}{path}", params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Futures API GET {path} failed: {e}") from e
