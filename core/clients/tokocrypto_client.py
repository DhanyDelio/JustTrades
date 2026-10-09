"""
tokocrypto_client.py — Tokocrypto spot exchange client.

Tahap 1 — reconnaissance / read-only connector.

Architecture:
    ExchangeClient (conceptual)
        └── TokocryptoClient          ← this file

Exposes:
    Public (no credentials required):
        ping()                        health check / connectivity
        get_server_time()             exchange server time (ms)
        get_symbols()                 list[ExchangeSymbol]
        get_symbol(symbol)            ExchangeSymbol for one symbol
        get_ticker(symbol)            current price as float
        get_depth(symbol, limit)      order book bids/asks

    Authenticated READ-ONLY (credentials required):
        get_account()                 raw account info
        get_balances()                list[ExchangeBalance]
        get_balance(asset)            ExchangeBalance for one asset
        authenticated                 whether both credentials are configured

    Normalization helpers (stateless, importable):
        normalize_symbol(raw)         "BTC_USDT" → "BTC_USDT" (no-op, passthrough)
        round_tick(value, tick)       price rounding to Tokocrypto tick
        round_step(value, step)       qty rounding to Tokocrypto step

NON-GOALS for this stage:
    - No BUY/SELL order placement (POST /open/v1/orders).
    - No OCO creation.
    - No real-money execution of any kind.

API reference: https://www.tokocrypto.com/apidocs/
Authentication: HMAC-SHA256, headers X-MBX-APIKEY + signature in query/body.

Credentials are loaded from environment variables — never hardcoded:
    TOKOCRYPTO_API_KEY
    TOKOCRYPTO_API_SECRET

If credentials are absent, all public methods still work.
Authenticated methods raise TokocryptoAuthError with a clear message.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests


# ---------------------------------------------------------------------------
# Typed domain models
# ---------------------------------------------------------------------------

@dataclass
class ExchangeSymbol:
    """
    Normalized representation of a single trading symbol on Tokocrypto.

    All filter fields default to 0.0 / False when not present in the API
    response — callers should check before using.
    """
    symbol:           str
    base_asset:       str
    quote_asset:      str
    status:           str        # "TRADING" inferred from spotTradingEnable flag
    tick_size:        float      # price precision (PRICE_FILTER tickSize)
    step_size:        float      # quantity precision (LOT_SIZE stepSize)
    min_qty:          float      # LOT_SIZE minQty
    min_notional:     float      # NOTIONAL minNotional
    spot_enabled:     bool       # spotTradingEnable == 1
    oco_enabled:      bool       # ocoEnable == 1
    # Self-Trade Prevention metadata (added 2026-06-05 in API changelog)
    # Values are string names (e.g. "EXPIRE_MAKER"), not integer codes.
    # None means the field was absent in the API response — do NOT assume any default.
    # Future order placement MUST read allowed_stp_modes and pick a valid mode.
    default_stp_mode:  str | None = None
    allowed_stp_modes: list[str] = field(default_factory=list)
    raw:              dict = field(default_factory=dict, repr=False)

    # Derived convenience — same shape as FuturesClient / binance_math helpers
    @property
    def constraints(self) -> dict:
        """Return a constraints dict compatible with binance_math helpers."""
        return {
            "tick_size":    self.tick_size,
            "step_size":    self.step_size,
            "min_qty":      self.min_qty,
            "min_notional": self.min_notional,
        }


@dataclass
class ExchangeBalance:
    """Normalized single-asset balance entry."""
    asset:  str
    free:   float
    locked: float

    @property
    def total(self) -> float:
        return self.free + self.locked


@dataclass
class ExecutionRulesRule:
    """
    A single rule entry within ExecutionRules.
    Currently the only ruleType is "PRICE_RANGE".

    Multipliers are stored as float | None — None means the field was absent
    in the response, which means that price direction is unconstrained.

    Do NOT assume this is equivalent to Binance PERCENT_PRICE_BY_SIDE.
    Tokocrypto applies these limits at execution time (taker phase), not
    at order submission time. See API docs FAQs for exact semantics.
    """
    rule_type:           str           # e.g. "PRICE_RANGE"
    bid_limit_mult_up:   float | None = None  # max price = ref * bidLimitMultUp
    bid_limit_mult_down: float | None = None  # min price = ref * bidLimitMultDown
    ask_limit_mult_up:   float | None = None
    ask_limit_mult_down: float | None = None
    raw:                 dict = field(default_factory=dict, repr=False)


@dataclass
class ExecutionRules:
    """
    Parsed execution rules for a single symbol from GET /api/v3/executionRules.

    rules: list of ExecutionRulesRule — may be empty if no rules are configured
           for the symbol. An empty list means no Price Range enforcement.

    IMPORTANT: Both the execution rule AND a non-null referencePrice must be
    present for the Price Range rule to be enforced. Either alone is insufficient.
    See get_reference_price() and the order preflight design comment block.

    raw: the full symbolRules entry for this symbol, for forward compatibility.
    """
    symbol: str
    rules:  list[ExecutionRulesRule] = field(default_factory=list)
    raw:    dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Typed error hierarchy
# ---------------------------------------------------------------------------

class TokocryptoError(RuntimeError):
    """Base for all Tokocrypto client errors."""


class TokocryptoNetworkError(TokocryptoError):
    """Network / transport failure — connection error, timeout, DNS, SSL."""


class TokocryptoAuthError(TokocryptoError):
    """Authentication failure — missing credentials or invalid signature."""


class TokocryptoAPIError(TokocryptoError):
    """
    Exchange rejected the request with a non-zero error code.

    Attributes:
        code    — Tokocrypto error code (int)
        msg     — raw message from exchange
        raw     — full parsed response body
    """
    def __init__(self, code: int, msg: str, raw: dict | None = None):
        self.code = code
        self.msg  = msg
        self.raw  = raw or {}
        super().__init__(f"[Tokocrypto API {code}] {msg}")


class TokocryptoRateLimitError(TokocryptoAPIError):
    """HTTP 429 / 418 — rate limit or IP ban."""


class TokocryptoMalformedResponseError(TokocryptoError):
    """Response was not valid JSON or did not match expected schema."""


class TokocryptoCapabilityError(TokocryptoError):
    """Requested capability is not available for this symbol or account."""


class TokocryptoUnknownOrderStatus(TokocryptoError):
    """
    HTTP 5XX was received in response to an order-related request, or any
    request where the outcome is ambiguous due to a server-side error.

    SEMANTICS: This does NOT mean the order was rejected or failed.
    The order state is UNKNOWN — it may be NEW, FILLED, or may not exist.

    Required action:
        1. Query the order status via GET /open/v1/orders (by orderId or
           clientOrderId) to determine the actual outcome.
        2. Only classify as FAILED after confirming NOT_FOUND via query.
        3. Do NOT automatically retry the POST order — this risks duplicate fills.

    Stage 2 reconciliation flow:
        POST order → HTTP 5XX → TokocryptoUnknownOrderStatus
        → query order → FILLED / NEW / CANCELED / NOT_FOUND
        → only then mark trade outcome

    This class intentionally carries no retry logic.
    """

    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self.body = body or {}
        super().__init__(
            f"HTTP {status_code}: order status UNKNOWN — "
            f"do not assume failure; reconcile via order query"
        )


class TokocryptoSubmissionUnknownError(TokocryptoError):
    """An order POST started but its outcome could not be confirmed."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TokocryptoClient:
    """
    Thin, isolated REST client for Tokocrypto spot exchange.

    Suitable for:
        - public market data (no credentials needed)
        - authenticated read-only operations (balance, account info)

    NOT suitable (this stage):
        - placing any kind of order
        - OCO / stop-loss / take-profit creation
        - withdrawals or any mutation of account state

    Usage — public only (no credentials):
        client = TokocryptoClient.build()
        print(client.get_ticker("BTC_USDT"))

    Usage — authenticated reads:
        client = TokocryptoClient.build()   # reads from env automatically
        balances = client.get_balances()

    Usage — explicit credentials (e.g. testing):
        client = TokocryptoClient(api_key="...", api_secret="...")
    """

    # Primary base URL (type=1 MBX symbols use tokocrypto.site for market data)
    BASE_URL        = "https://www.tokocrypto.com"
    MARKET_BASE_URL = "https://www.tokocrypto.site"

    # Default network timeout (seconds)
    _TIMEOUT = 10

    def __init__(
        self,
        api_key:    str | None = None,
        api_secret: str | None = None,
        timeout:    int = _TIMEOUT,
    ):
        """
        Construct a TokocryptoClient.

        api_key / api_secret may be None — public endpoints will still work.
        Do NOT pass literal credential strings in production code; use build().
        """
        # Credentials are stored but never logged or included in repr.
        self._api_key    = api_key
        self._api_secret = api_secret
        self._timeout    = timeout
        self._session    = requests.Session()
        self._session.headers.update({"Content-Type": "application/x-www-form-urlencoded"})

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, timeout: int = _TIMEOUT) -> "TokocryptoClient":
        """
        Build a TokocryptoClient, loading credentials from environment.

        Reads:
            TOKOCRYPTO_API_KEY
            TOKOCRYPTO_API_SECRET

        If either variable is absent, the client is created without credentials.
        Public endpoints will work; authenticated endpoints will raise
        TokocryptoAuthError.
        """
        api_key    = os.getenv("TOKOCRYPTO_API_KEY", "").strip() or None
        api_secret = os.getenv("TOKOCRYPTO_API_SECRET", "").strip() or None
        return cls(api_key=api_key, api_secret=api_secret, timeout=timeout)

    @property
    def authenticated(self) -> bool:
        """Return whether both credentials are configured without exposing them."""
        return bool(self._api_key and self._api_secret)

    # ------------------------------------------------------------------
    # Connectivity / health
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """
        Check connectivity to Tokocrypto.
        Uses GET /open/v1/common/time (lightweight, returns server time).
        Returns True if reachable, False otherwise.
        """
        try:
            self._public_get("/open/v1/common/time")
            return True
        except TokocryptoNetworkError:
            return False
        except TokocryptoError:
            # API responded (even with error) — exchange is reachable
            return True

    def get_server_time(self) -> int:
        """
        Return exchange server time in milliseconds.
        Maps to GET /open/v1/common/time.
        """
        resp = self._public_get("/open/v1/common/time")
        ts = resp.get("timestamp")
        if ts is None:
            raise TokocryptoMalformedResponseError(
                "get_server_time: 'timestamp' field missing in response"
            )
        return int(ts)

    # ------------------------------------------------------------------
    # Symbol / market data (public)
    # ------------------------------------------------------------------

    def get_symbols(self) -> list[ExchangeSymbol]:
        """
        Fetch all supported trading symbols.
        Maps to GET /open/v1/common/symbols.

        Returns list[ExchangeSymbol] with normalized filter values.
        Only returns symbols where symbolType==1 (MBX / main pairs) to stay
        on the well-documented API surface.  Type-3 (Nextme) symbols use
        separate endpoints and are excluded in this stage.
        """
        resp = self._public_get("/open/v1/common/symbols")
        raw_list = self._extract_data_list(resp, context="get_symbols")
        return [self._parse_symbol(s) for s in raw_list if s.get("type") == 1]

    def get_symbol(self, symbol: str) -> ExchangeSymbol:
        """
        Fetch metadata for a single symbol (e.g. "BTC_USDT").
        Raises TokocryptoCapabilityError if symbol not found.
        """
        symbols = self.get_symbols()
        sym_upper = self.normalize_symbol(symbol)
        compact = sym_upper.replace("_", "")
        for s in symbols:
            if self.normalize_symbol(s.symbol).replace("_", "") == compact:
                return s
        raise TokocryptoCapabilityError(
            f"Symbol '{symbol}' not found in Tokocrypto exchange info"
        )

    def get_ticker(self, symbol: str) -> float:
        """
        Fetch current price for symbol.
        Maps to GET /api/v3/ticker/price (type-1 MBX symbol, no underscore).

        Note: Tokocrypto uses underscores internally ("BTC_USDT") but the
        ticker/price endpoint mirrors the Binance format and expects "BTCUSDT".
        We strip the underscore automatically.
        """
        normalized = self.normalize_symbol(symbol).replace("_", "")
        try:
            resp = self._raw_get(
                f"{self.MARKET_BASE_URL}/api/v3/ticker/price",
                params={"symbol": normalized},
            )
        except TokocryptoAPIError:
            # Some symbols may only exist with underscore on the main endpoint
            # — fall back to the v1 ticker endpoint
            resp = self._raw_get(
                f"{self.MARKET_BASE_URL}/api/v3/ticker/24hr",
                params={"symbol": normalized},
            )

        # Response may be Binance-style ({"price": "..."}) or wrapped
        price = resp.get("price") or resp.get("lastPrice") or (
            resp.get("data", {}).get("price") if isinstance(resp.get("data"), dict) else None
        )
        if price is None:
            raise TokocryptoMalformedResponseError(
                f"get_ticker({symbol}): could not find price in response: {resp}"
            )
        return float(price)

    def get_depth(self, symbol: str, limit: int = 20) -> dict:
        """
        Fetch order book depth for symbol.
        Maps to GET https://www.tokocrypto.site/api/v3/depth (type-1 symbols).

        Returns dict with keys:
            bids: list[[price_str, qty_str], ...]
            asks: list[[price_str, qty_str], ...]
            lastUpdateId: int

        limit: one of [5, 10, 20, 50, 100, 500]
        """
        # type-1 MBX symbols: strip underscore
        normalized = self.normalize_symbol(symbol).replace("_", "")
        valid_limits = {5, 10, 20, 50, 100, 500}
        if limit not in valid_limits:
            limit = min(valid_limits, key=lambda v: abs(v - limit))

        resp = self._raw_get(
            f"{self.MARKET_BASE_URL}/api/v3/depth",
            params={"symbol": normalized, "limit": limit},
        )

        # Response may be wrapped in {code, data} or raw Binance-style
        if "data" in resp and isinstance(resp["data"], dict):
            data = resp["data"]
        elif "bids" in resp:
            data = resp
        else:
            raise TokocryptoMalformedResponseError(
                f"get_depth({symbol}): unexpected response shape: {list(resp.keys())}"
            )

        return {
            "bids":         data.get("bids", []),
            "asks":         data.get("asks", []),
            "lastUpdateId": data.get("lastUpdateId"),
        }

    def get_execution_rules(self, symbol: str) -> "ExecutionRules":
        """
        Fetch execution rules for a single symbol.
        Maps to GET /api/v3/executionRules (base: https://www.tokocrypto.site).

        The symbol must be passed without underscore (e.g. "BTCUSDT") because
        this endpoint follows Binance-style market endpoint conventions.

        Returns ExecutionRules with an empty rules list if the symbol has no
        configured execution rules (not an error).

        Raises:
            ValueError                       — symbol is empty/None
            TokocryptoMalformedResponseError — unexpected response shape
            TokocryptoNetworkError           — transport failure
            TokocryptoUnknownOrderStatus     — HTTP 5XX ambiguous response

        IMPORTANT: Do NOT assume these rules equal Binance PERCENT_PRICE_BY_SIDE.
        Tokocrypto enforces these at execution time (taker phase), not at
        order submission time.
        """
        if not symbol:
            raise ValueError("get_execution_rules: symbol is required")

        normalized = self.normalize_symbol(symbol).replace("_", "")
        resp = self._raw_get(
            f"{self.MARKET_BASE_URL}/api/v3/executionRules",
            params={"symbol": normalized},
        )

        # Response shape: {"symbolRules": [...]}
        # May be bare Binance-style (no envelope) or wrapped in data
        symbol_rules = resp.get("symbolRules")
        if symbol_rules is None:
            data = resp.get("data", {})
            if isinstance(data, dict):
                symbol_rules = data.get("symbolRules")
        if symbol_rules is None:
            raise TokocryptoMalformedResponseError(
                f"get_execution_rules({symbol}): 'symbolRules' missing in response: "
                f"{list(resp.keys())}"
            )

        # Find entry for requested symbol
        for entry in symbol_rules:
            if entry.get("symbol", "").upper() == normalized.upper():
                return self._parse_execution_rules(entry)

        # No entry for this symbol — not an error, means no execution rules apply
        return ExecutionRules(symbol=normalized, rules=[], raw={})

    def get_reference_price(self, symbol: str) -> "float | None":
        """
        Fetch the current reference price for a symbol.
        Maps to GET /api/v3/referencePrice (base: https://www.tokocrypto.site).

        The reference price is used by the Price Range Execution Rule to
        determine allowable execution price bounds. It is calculated by the
        matching engine as a moving average of recent trade prices.

        Returns:
            float — the reference price if available
            None  — if the exchange returns null referencePrice (rule not enforced)

        Raises:
            ValueError                       — symbol is empty/None
            TokocryptoMalformedResponseError — unexpected response shape
            TokocryptoNetworkError           — transport failure
            TokocryptoUnknownOrderStatus     — HTTP 5XX ambiguous response

        Note: The reference price changes continuously. For order preflight,
        use the WebSocket stream <symbol>@referencePrice for real-time data
        (Stage 2). This REST method provides a point-in-time snapshot only.

        The reference price is NOT the same as get_ticker() (last traded price).
        """
        if not symbol:
            raise ValueError("get_reference_price: symbol is required")

        normalized = self.normalize_symbol(symbol).replace("_", "")
        resp = self._raw_get(
            f"{self.MARKET_BASE_URL}/api/v3/referencePrice",
            params={"symbol": normalized},
        )

        # Response shape: {"symbol": "BTCUSDT", "referencePrice": "10.00", "timestamp": ...}
        # May be direct or wrapped in envelope
        ref_price_raw = resp.get("referencePrice")
        if ref_price_raw is None and "data" in resp and isinstance(resp["data"], dict):
            ref_price_raw = resp["data"].get("referencePrice")

        if "referencePrice" not in resp and "data" not in resp:
            raise TokocryptoMalformedResponseError(
                f"get_reference_price({symbol}): 'referencePrice' key missing. "
                f"Response keys: {list(resp.keys())}"
            )

        if ref_price_raw is None:
            return None   # null reference price → rule not enforced
        return float(ref_price_raw)

    # reference price: use get_reference_price() — separate confirmed endpoint on Tokocrypto
    # (GET /api/v3/referencePrice, base https://www.tokocrypto.site)
    # get_ticker() returns the last traded price; reference price is a distinct concept.

    # ------------------------------------------------------------------
    # Authenticated read-only
    # ------------------------------------------------------------------

    def get_account(self) -> dict:
        """
        Fetch full account information (READ-ONLY).
        Maps to GET /open/v1/account/spot (SIGNED).

        Returns the raw 'data' dict from the Tokocrypto response.
        Raises TokocryptoAuthError if credentials are not configured.
        """
        resp = self._signed_get("/open/v1/account/spot", {})
        data = resp.get("data")
        if data is None:
            raise TokocryptoMalformedResponseError(
                "get_account: 'data' field missing in response"
            )
        return data

    def get_balances(self) -> list[ExchangeBalance]:
        """
        Fetch all account asset balances (READ-ONLY).
        Returns list[ExchangeBalance].
        """
        account = self.get_account()
        raw_assets = account.get("accountAssets", [])
        if not isinstance(raw_assets, list):
            raise TokocryptoMalformedResponseError(
                f"get_balances: 'accountAssets' is not a list: {type(raw_assets)}"
            )
        return [
            ExchangeBalance(
                asset=a.get("asset", ""),
                free=float(a.get("free", 0)),
                locked=float(a.get("locked", 0)),
            )
            for a in raw_assets
        ]

    def get_balance(self, asset: str) -> ExchangeBalance:
        """
        Fetch balance for a single asset (READ-ONLY).
        Returns ExchangeBalance with free=0, locked=0 if asset not held.
        """
        balances = self.get_balances()
        asset_upper = asset.upper()
        for b in balances:
            if b.asset == asset_upper:
                return b
        return ExchangeBalance(asset=asset_upper, free=0.0, locked=0.0)

    # ------------------------------------------------------------------
    # Normalization helpers (stateless, safe to import separately)
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_symbol(raw: str) -> str:
        """
        Normalize symbol string for use with this client.
        Tokocrypto uses underscore-separated pairs: "BTC_USDT".
        If given "BTCUSDT" (Binance-style), we cannot know where to split
        without a symbol list — callers must use the underscore form.
        This method is a passthrough that upper-cases the input.
        """
        return raw.strip().upper()

    @staticmethod
    def round_tick(value: float, tick: float) -> float:
        """
        Round price to nearest tick_size increment.
        Mirrors core.utils.binance_math.round_tick.

        Precision uses ceil(-log10(tick)) so that tick sizes like 0.5 (whose
        -log10 = 0.301) are correctly handled as 1 decimal place, not 0.
        """
        if tick <= 0:
            return value
        precision = max(0, math.ceil(-math.log10(tick)))
        return round(round(value / tick) * tick, precision)

    @staticmethod
    def round_step(value: float, step: float) -> float:
        """
        Round quantity DOWN to the nearest step_size increment.
        Mirrors core.utils.binance_math.round_step.

        Precision uses ceil(-log10(step)) for the same reason as round_tick.
        """
        if step <= 0:
            return value
        precision = max(0, math.ceil(-math.log10(step)))
        return round(math.floor(value / step) * step, precision)

    # ------------------------------------------------------------------
    # Private — HTTP helpers
    # ------------------------------------------------------------------

    def _public_get(self, path: str, params: dict | None = None) -> dict:
        """GET against BASE_URL with no authentication."""
        return self._raw_get(f"{self.BASE_URL}{path}", params=params)

    def _raw_get(self, url: str, params: dict | None = None) -> dict:
        """
        Perform a GET request and return parsed JSON.
        Handles Tokocrypto's wrapped response envelope:
            {"code": 0, "msg": "success", "data": {...}, "timestamp": ...}
        For pure Binance-style endpoints on www.tokocrypto.site the envelope
        may be absent — we return the raw dict in that case.
        """
        try:
            r = self._session.get(url, params=params, timeout=self._timeout)
        except requests.exceptions.ConnectionError as e:
            raise TokocryptoNetworkError(f"Connection failed: {e}") from e
        except requests.exceptions.Timeout as e:
            raise TokocryptoNetworkError(f"Request timed out: {e}") from e
        except requests.exceptions.SSLError as e:
            raise TokocryptoNetworkError(f"SSL error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise TokocryptoNetworkError(f"Network error: {e}") from e

        return self._handle_response(r)

    def _signed_get(self, path: str, params: dict) -> dict:
        """
        Perform a SIGNED GET against BASE_URL.
        Appends timestamp + signature to params.
        Raises TokocryptoAuthError if credentials not configured.
        """
        self._require_credentials()
        signed_params = self._sign_params(params)
        url = f"{self.BASE_URL}{path}"
        try:
            r = self._session.get(
                url,
                params=signed_params,
                headers={"X-MBX-APIKEY": self._api_key},  # type: ignore[arg-type]
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as e:
            raise TokocryptoNetworkError(f"Connection failed: {e}") from e
        except requests.exceptions.Timeout as e:
            raise TokocryptoNetworkError(f"Request timed out: {e}") from e
        except requests.exceptions.SSLError as e:
            raise TokocryptoNetworkError(f"SSL error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise TokocryptoNetworkError(f"Network error: {e}") from e

        return self._handle_response(r)

    def _handle_response(self, r: requests.Response) -> dict:
        """
        Decode and validate the HTTP response.

        Error classification:
            HTTP 429 / 418  → TokocryptoRateLimitError
            HTTP 401 / 403  → TokocryptoAuthError
            body code != 0  → TokocryptoAPIError
            bad JSON        → TokocryptoMalformedResponseError
            other HTTP 4xx/5xx → TokocryptoAPIError
        """
        # Rate-limit / IP ban
        if r.status_code in (429, 418):
            retry_after = r.headers.get("Retry-After", "unknown")
            raise TokocryptoRateLimitError(
                -429,
                f"Rate limit exceeded (HTTP {r.status_code}). "
                f"Retry-After: {retry_after}s",
            )

        # Authentication errors at HTTP level
        if r.status_code in (401, 403):
            raise TokocryptoAuthError(
                f"Authentication failed (HTTP {r.status_code}): {r.text[:120]}"
            )

        # HTTP 5XX — ambiguous server error, order state unknown
        # IMPORTANT: do not treat as definitive failure; reconcile via order query.
        # Do NOT blind-retry.
        if r.status_code >= 500:
            try:
                body_5xx = r.json() if r.text else {}
            except ValueError:
                body_5xx = {}
            raise TokocryptoUnknownOrderStatus(
                status_code=r.status_code,
                body=body_5xx if isinstance(body_5xx, dict) else {},
            )

        # Parse JSON — must succeed for all responses we handle
        try:
            body = r.json()
        except ValueError as e:
            raise TokocryptoMalformedResponseError(
                f"Response is not valid JSON (HTTP {r.status_code}): "
                f"{r.text[:120]!r}"
            ) from e

        if not isinstance(body, dict):
            # Some endpoints return a bare list (e.g. ticker/price for multiple)
            return {"_list": body}

        # Tokocrypto envelope: {"code": <int>, "msg": ..., "data": ...}
        code = body.get("code")
        if code is not None and code != 0:
            msg = body.get("msg") or body.get("message") or str(body)
            # -1 maps generically to auth for common patterns
            if r.status_code in (401, 403) or str(code) in ("-2014", "-2015", "-1022"):
                raise TokocryptoAuthError(
                    f"Authentication failed (API code {code}): {msg}"
                )
            raise TokocryptoAPIError(code=int(code), msg=str(msg), raw=body)

        # Non-2xx with no parseable code
        if r.status_code >= 400:
            raise TokocryptoAPIError(
                code=r.status_code,
                msg=f"HTTP {r.status_code}: {r.text[:120]}",
                raw=body,
            )

        return body

    # ------------------------------------------------------------------
    # Private — authentication
    # ------------------------------------------------------------------

    def _require_credentials(self) -> None:
        """Raise TokocryptoAuthError if API key/secret are not configured."""
        if not self._api_key or not self._api_secret:
            raise TokocryptoAuthError(
                "Tokocrypto API credentials not configured.\n"
                "Set TOKOCRYPTO_API_KEY and TOKOCRYPTO_API_SECRET "
                "in your .env file."
            )

    def _sign_params(self, params: dict) -> dict:
        """
        Add timestamp + HMAC-SHA256 signature to a params dict.

        Signature method (from Tokocrypto docs):
            totalParams = urlencode(params_with_timestamp)
            signature   = HMAC-SHA256(secretKey, totalParams).hexdigest()
        """
        # Shallow copy to avoid mutating caller's dict
        p = dict(params)
        p["timestamp"] = int(time.time() * 1000)
        p.setdefault("recvWindow", 5000)

        query_string = urlencode(p)
        # IMPORTANT: api_secret is only used as HMAC key, never logged
        signature = hmac.new(
            self._api_secret.encode("utf-8"),  # type: ignore[union-attr]
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        p["signature"] = signature
        return p

    # ------------------------------------------------------------------
    # Private — parsing helpers
    # ------------------------------------------------------------------

    def _extract_data_list(self, resp: dict, context: str = "") -> list:
        """
        Extract the nested list from a Tokocrypto response envelope.
        GET /open/v1/common/symbols returns {"data": {"list": [...]}}
        """
        data = resp.get("data")
        if data is None:
            raise TokocryptoMalformedResponseError(
                f"{context}: 'data' field missing in response"
            )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            lst = data.get("list")
            if isinstance(lst, list):
                return lst
        raise TokocryptoMalformedResponseError(
            f"{context}: expected list in 'data' or 'data.list', "
            f"got: {type(data)}"
        )

    @staticmethod
    def _parse_execution_rules(raw: dict) -> "ExecutionRules":
        """
        Parse a single symbolRules entry from GET /api/v3/executionRules.

        Expected raw shape:
            {
              "symbol": "BTCUSDT",
              "rules": [
                {
                  "ruleType": "PRICE_RANGE",
                  "bidLimitMultUp":   "2.0000",
                  "bidLimitMultDown": "0.5000",
                  "askLimitMultUp":   "2.0000",
                  "askLimitMultDown": "0.5000"
                }
              ]
            }

        All multiplier fields are optional — missing fields → None (unconstrained).
        Do NOT default to 1.0 or any other fallback value.
        Do NOT assume semantics identical to Binance PERCENT_PRICE_BY_SIDE.
        """
        def _to_float_or_none(val: Any) -> "float | None":
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        parsed_rules: list[ExecutionRulesRule] = []
        for r in raw.get("rules", []):
            parsed_rules.append(ExecutionRulesRule(
                rule_type           = r.get("ruleType", ""),
                bid_limit_mult_up   = _to_float_or_none(r.get("bidLimitMultUp")),
                bid_limit_mult_down = _to_float_or_none(r.get("bidLimitMultDown")),
                ask_limit_mult_up   = _to_float_or_none(r.get("askLimitMultUp")),
                ask_limit_mult_down = _to_float_or_none(r.get("askLimitMultDown")),
                raw                 = r,
            ))

        return ExecutionRules(
            symbol = raw.get("symbol", ""),
            rules  = parsed_rules,
            raw    = raw,
        )

    @staticmethod
    def _parse_symbol(raw: dict) -> "ExchangeSymbol":
        """
        Parse a single symbol entry from GET /open/v1/common/symbols.

        Filter extraction mirrors FuturesClient.get_symbol_constraints() and
        binance_math.get_symbol_constraints() — same pattern, same field names.

        Tokocrypto filter types used here:
            PRICE_FILTER  → tickSize
            LOT_SIZE      → minQty, stepSize
            NOTIONAL      → minNotional (preferred)
            MIN_NOTIONAL  → minNotional (legacy fallback)
        """
        tick_size    = 0.0
        step_size    = 0.0
        min_qty      = 0.0
        min_notional = 0.0

        for f in raw.get("filters", []):
            ft = f.get("filterType", "")
            if ft == "PRICE_FILTER":
                tick_size = float(f.get("tickSize", 0) or 0)
            elif ft == "LOT_SIZE":
                step_size = float(f.get("stepSize", 0) or 0)
                min_qty   = float(f.get("minQty", 0) or 0)
            elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
                # NOTIONAL is the current filter; MIN_NOTIONAL is legacy
                mn = (f.get("minNotional") or f.get("minVal") or 0)
                min_notional = float(mn) if mn else min_notional

        # STP fields are top-level symbol properties, not inside filters (2026-06-05)
        default_stp_mode      = raw.get("defaultSelfTradePreventionMode")   # str or None
        allowed_stp_modes_raw = raw.get("allowedSelfTradePreventionModes", [])
        allowed_stp_modes     = list(allowed_stp_modes_raw) if isinstance(allowed_stp_modes_raw, list) else []

        return ExchangeSymbol(
            symbol            = raw.get("symbol", ""),
            base_asset        = raw.get("baseAsset", ""),
            quote_asset       = raw.get("quoteAsset", ""),
            # Tokocrypto doesn't expose a 'status' string directly;
            # spotTradingEnable==1 is the tradeable flag
            status            = "TRADING" if raw.get("spotTradingEnable") == 1 else "HALTED",
            tick_size         = tick_size,
            step_size         = step_size,
            min_qty           = min_qty,
            min_notional      = min_notional,
            spot_enabled      = raw.get("spotTradingEnable") == 1,
            oco_enabled       = raw.get("ocoEnable") == 1,
            default_stp_mode  = default_stp_mode,
            allowed_stp_modes = allowed_stp_modes,
            raw               = raw,
        )

    # ------------------------------------------------------------------
    # repr — safe, no credentials
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"TokocryptoClient("
            f"authenticated={self.authenticated}, "
            f"base='{self.BASE_URL}')"
        )

    # ---------------------------------------------------------------------------
    # USER DATA STREAM — NOT IMPLEMENTED (Stage 1 / read-only)
    # ---------------------------------------------------------------------------
    # TODO Stage 2: POST /open/v1/user-listen-token (replaces deprecated
    #   POST /open/v1/user-data-stream, decommissioned 2026-04-30)
    # Do NOT implement the old /user-data-stream endpoint.
    #
    # Migration path when ready:
    #   Old: POST /open/v1/user-data-stream → returns listenKey
    #   New: POST /open/v1/user-listen-token → returns listenToken
    #        (different field name: listenToken, not listenKey)
    #   WebSocket subscribe: { "method": "SUBSCRIBE", "params": ["<listenToken>"] }
    #   Unsubscribe / cleanup: no separate HTTP call needed (token has TTL)
    #
    # Current client has no WebSocket layer; entire stream subsystem is Stage 2.
    # ---------------------------------------------------------------------------

    # ---------------------------------------------------------------------------
    # FUTURE ORDER PREFLIGHT DESIGN — Stage 2 interface (NOT IMPLEMENTED)
    # ---------------------------------------------------------------------------
    # When order placement is added in Stage 2, preflight must follow this flow:
    #
    # symbol metadata (get_symbol)
    #   ├── PRICE_FILTER          (tick_size, min/max price)
    #   ├── LOT_SIZE              (step_size, min_qty, max_qty)
    #   ├── NOTIONAL              (min_notional, max_notional)
    #   ├── MAX_NUM_ORDERS        (open order limits)
    #   ├── OCO constraints       (ocoEnable, oco-specific filters)
    #   └── STP allowed modes     (allowed_stp_modes — pick valid configured mode)
    #
    # execution rules (get_execution_rules)
    #   └── PRICE_RANGE           (bid/ask multipliers around reference price)
    #       → executionRules do NOT replace symbol filters — both apply
    #       → executionRules ≠ Binance PERCENT_PRICE_BY_SIDE (different semantics)
    #
    # reference price (get_reference_price)
    #   └── Null reference price → PRICE_RANGE rule not enforced
    #
    # current market price (get_ticker)
    #   └── Sanity check only — do not substitute for reference price
    #
    # ORDER PREFLIGHT (Stage 2):
    #   1. Validate quantity: round_step(), check min_qty, min_notional
    #   2. Validate price: round_tick(), check PRICE_FILTER bounds
    #   3. Check PRICE_RANGE: price within [ref * multDown, ref * multUp]
    #      — only if execution rule exists AND referencePrice is not None
    #   4. Select STP mode: choose from allowed_stp_modes, prefer configured
    #      user setting, fall back to defaultSelfTradePreventionMode
    #   5. Submit POST /open/v1/orders
    #   6. On HTTP 5XX: raise TokocryptoUnknownOrderStatus — query to reconcile
    #
    # ---------------------------------------------------------------------------

    # ---------------------------------------------------------------------------
    # OCO ORDERS — NOT CONFIRMED IN TOKOCRYPTO API DOCS (Stage 1 audit result)
    # ---------------------------------------------------------------------------
    # Status: ocoEnable flag exists on symbol metadata (ExchangeSymbol.oco_enabled).
    # However, the OCO order endpoint, required parameters, price relationship
    # constraints, and stop/limit trigger rules are NOT fully documented in the
    # currently available API contract at https://www.tokocrypto.com/apidocs/.
    #
    # REQUIRED before Stage 2 OCO implementation:
    #   - Confirm POST endpoint path (likely POST /open/v1/oco-orders or similar)
    #   - Confirm required parameters: symbol, side, quantity, price, stopPrice,
    #     stopLimitPrice, stopLimitTimeInForce
    #   - Confirm price relationships: limit vs stop price ordering rules
    #   - Confirm whether executionRules/PRICE_RANGE applies to OCO legs
    #   - Confirm STP mode applicability for OCO
    #   - Confirm quantity constraints (single qty vs per-leg)
    #
    # Do NOT implement OCO until order placement (POST /open/v1/orders) is
    # confirmed working in Stage 2. OCO is Stage 3 at the earliest.
    # ---------------------------------------------------------------------------


    def _signed_post(self, path: str, params: dict) -> dict:
        """
        Perform a SIGNED POST against BASE_URL.
        Params are sent as application/x-www-form-urlencoded body (data=),
        NOT as query params — this is required by Tokocrypto's auth scheme.
        Raises TokocryptoAuthError if credentials not configured.
        """
        self._require_credentials()
        signed_params = self._sign_params(params)
        url = f"{self.BASE_URL}{path}"
        try:
            r = self._session.post(
                url,
                data=signed_params,   # x-www-form-urlencoded body
                headers={"X-MBX-APIKEY": self._api_key},  # type: ignore[arg-type]
                timeout=self._timeout,
            )
        except requests.exceptions.ConnectionError as e:
            raise TokocryptoNetworkError(f"Connection failed: {e}") from e
        except requests.exceptions.Timeout as e:
            raise TokocryptoNetworkError(f"Request timed out: {e}") from e
        except requests.exceptions.RequestException as e:
            raise TokocryptoNetworkError(f"Network error: {e}") from e
        return self._handle_response(r)

    def get_order_detail(self, symbol: str, order_id: str) -> dict:
        """
        Query a single order by orderId (SIGNED GET).
        Maps to GET /open/v1/orders/detail.

        Returns the 'data' sub-dict from the response.
        Raises TokocryptoAPIError (code -2013) if orderId not found.
        Raises TokocryptoMalformedResponseError if 'data' is absent.
        """
        resp = self._signed_get("/open/v1/orders/detail", {
            "symbol":  self.normalize_symbol(symbol),
            "orderId": str(order_id),
        })
        data = resp.get("data")
        if data is None:
            raise TokocryptoMalformedResponseError(
                f"get_order_detail({symbol}, {order_id}): 'data' field missing in response"
            )
        return data


# ---------------------------------------------------------------------------
# Convenience re-exports for callers that only need normalization helpers
# ---------------------------------------------------------------------------

round_tick  = TokocryptoClient.round_tick   # noqa: E305
round_step  = TokocryptoClient.round_step
