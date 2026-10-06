"""
core/clients — exchange client adapters.

Exports:
    FuturesClient       — Binance Futures Testnet
    TokocryptoClient    — Tokocrypto spot (read-only, stage 1)
    ExchangeSymbol      — normalized symbol model
    ExchangeBalance     — normalized balance model
"""

from core.clients.futures_client import FuturesClient
from core.clients.tokocrypto_client import (
    TokocryptoClient,
    ExchangeSymbol,
    ExchangeBalance,
    TokocryptoError,
    TokocryptoNetworkError,
    TokocryptoAuthError,
    TokocryptoAPIError,
    TokocryptoRateLimitError,
    TokocryptoMalformedResponseError,
    TokocryptoCapabilityError,
)

__all__ = [
    "FuturesClient",
    "TokocryptoClient",
    "ExchangeSymbol",
    "ExchangeBalance",
    "TokocryptoError",
    "TokocryptoNetworkError",
    "TokocryptoAuthError",
    "TokocryptoAPIError",
    "TokocryptoRateLimitError",
    "TokocryptoMalformedResponseError",
    "TokocryptoCapabilityError",
]
