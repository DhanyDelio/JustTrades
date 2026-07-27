"""
binance_throttle.py — Adaptive rate-limit helper for Binance API calls.
========================================================================
Reads the actual used-weight headers from Binance API responses and
pauses automatically when approaching the rate limit ceiling.

Binance rate limits (per rolling 1-minute window):
  Spot:    X-MBX-Used-Weight-1M  limit = 6000 requests / minute
  Futures: X-MBX-Used-Weight-1M  limit = 2400 requests / minute
           (Binance docs: /fapi endpoint = 2400 weight/min)

We pause when used weight > 80% of the relevant limit.

Usage:
    from binance_throttle import SpotThrottle, FuturesThrottle

    throttle = SpotThrottle()

    # After each Binance API call that returns a requests.Response:
    throttle.check(response)

    # Or manually check with a known weight value:
    throttle.check_weight(used_weight)
"""

from __future__ import annotations
import time


# ── Binance official rate limits ─────────────────────────────────────────────
SPOT_WEIGHT_LIMIT:    int = 6000   # /api/v3 endpoints, per rolling minute
FUTURES_WEIGHT_LIMIT: int = 2400   # /fapi/v1 endpoints, per rolling minute

# Pause when used weight exceeds this fraction of the limit
THROTTLE_THRESHOLD: float = 0.80   # 80%

# How long to pause when threshold is crossed (seconds)
THROTTLE_PAUSE_SEC: float = 5.0

# Short courtesy sleep between parts even when under threshold (avoid burst)
BETWEEN_PARTS_SLEEP_SEC: float = 2.0


class _BaseThrottle:
    def __init__(self, limit: int, label: str) -> None:
        self._limit   = limit
        self._label   = label
        self._ceiling = int(limit * THROTTLE_THRESHOLD)

    def check(self, response) -> int:
        """
        Extract used weight from a requests.Response object and throttle if needed.
        Tries both header variants (Binance uses different casings).
        Returns the weight value found (0 if header missing).
        """
        weight = 0
        if response is None:
            return weight
        headers = getattr(response, "headers", {})
        for key in ("x-mbx-used-weight-1m", "X-MBX-Used-Weight-1M",
                    "x-mbx-used-weight",    "X-MBX-Used-Weight"):
            val = headers.get(key)
            if val is not None:
                try:
                    weight = int(val)
                    break
                except (ValueError, TypeError):
                    pass
        self.check_weight(weight)
        return weight

    def check_weight(self, used_weight: int) -> None:
        """
        Given a known used-weight value, pause if it exceeds the threshold.
        Logs clearly when throttling is active.
        """
        if used_weight <= 0:
            return
        pct = used_weight / self._limit * 100
        if used_weight >= self._ceiling:
            print(f"  [Rate limit/{self._label}] ⚠  {used_weight}/{self._limit} "
                  f"({pct:.0f}%) — pausing {THROTTLE_PAUSE_SEC:.0f}s")
            time.sleep(THROTTLE_PAUSE_SEC)
        else:
            # Only log when notably high (>50%) to avoid noise
            if pct > 50:
                print(f"  [Rate limit/{self._label}] {used_weight}/{self._limit} "
                      f"({pct:.0f}%) — within limit, continuing")

    def fetch_used_weight(self, base_url: str) -> int:
        """
        Actively query a lightweight endpoint to read the current used weight.
        Uses GET /time which costs 1 weight unit on both spot and futures.
        Returns 0 on any error.
        """
        import requests as _req
        try:
            resp = _req.get(f"{base_url}/time", timeout=5)
            return self.check(resp)
        except Exception:
            return 0

    def between_parts_sleep(self) -> None:
        """Courtesy sleep between scan parts when weight is not near ceiling."""
        if BETWEEN_PARTS_SLEEP_SEC > 0:
            time.sleep(BETWEEN_PARTS_SLEEP_SEC)


class SpotThrottle(_BaseThrottle):
    """
    Throttle for Binance Spot API (/api/v3).
    Limit: 6000 weight / rolling minute.
    Ceiling (80%): 4800.
    """
    SPOT_BASE = "https://api.binance.com/api/v3"

    def __init__(self) -> None:
        super().__init__(SPOT_WEIGHT_LIMIT, "Spot")

    def fetch_used_weight(self) -> int:  # type: ignore[override]
        return super().fetch_used_weight(self.SPOT_BASE)


class FuturesThrottle(_BaseThrottle):
    """
    Throttle for Binance Futures Testnet API (/fapi/v1).
    Limit: 2400 weight / rolling minute.
    Ceiling (80%): 1920.

    Futures testnet uses the same header (X-MBX-Used-Weight-1M) but
    the limit is lower — 2400 vs spot's 6000.
    """
    FUTURES_TESTNET_BASE = "https://testnet.binancefuture.com/fapi/v1"

    def __init__(self) -> None:
        super().__init__(FUTURES_WEIGHT_LIMIT, "Futures")

    def fetch_used_weight(self) -> int:  # type: ignore[override]
        return super().fetch_used_weight(self.FUTURES_TESTNET_BASE)
