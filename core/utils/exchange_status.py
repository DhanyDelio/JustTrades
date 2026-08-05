"""
exchange_status.py — Deteksi maintenance / outage Binance Testnet.

Dipakai oleh paper_trade_executor.py dan futures_trade_executor.py
sebagai single source of truth untuk menentukan apakah koneksi gagal
karena outage exchange atau karena error lain.

Exit code convention (dipakai oleh live_bot.py untuk membedakan):
    0  — sukses
    1  — error fatal non-maintenance (bug, config error, dll)
    2  — maintenance / outage exchange  ← live_bot.py akan skip cycle
"""

from __future__ import annotations

import re


# ── Typed exception ────────────────────────────────────────────────────────

class ExchangeOutage(RuntimeError):
    """
    Raised ketika exchange testnet tidak tersedia karena maintenance / outage.
    Berbeda dari error konfigurasi atau bug kode.
    """
    def __init__(self, exchange: str, reason: str):
        self.exchange = exchange   # "spot" | "futures"
        self.reason   = reason     # human-readable, masuk ke log & Telegram
        super().__init__(f"[{exchange.upper()} OUTAGE] {reason}")


# ── Classifier ─────────────────────────────────────────────────────────────

# Pola error yang mengindikasikan outage/maintenance, bukan bug kode
_OUTAGE_PATTERNS = (
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "maintenance",
    "temporarily unavailable",
    "connection refused",
    "connectionerror",
    "connectionreseteerror",
    "remotedisconnected",
    "max retries exceeded",
    "read timed out",
    "timed out",
    "sslerror",
    "name or service not known",
    "network is unreachable",
    "no route to host",
)

_HTML_TAG_RE = re.compile(r"<html[^>]*>", re.IGNORECASE)


def is_outage_error(exc: Exception) -> bool:
    """
    Kembalikan True jika exception merupakan tanda outage/maintenance exchange,
    bukan bug kode atau konfigurasi yang salah.

    Cek:
    1. Teks error mengandung salah satu _OUTAGE_PATTERNS
    2. Response body berisi tag HTML (502/maintenance page)
    3. Class name merupakan requests connectivity error
    """
    msg = str(exc).lower()

    # Cek pola teks
    if any(p in msg for p in _OUTAGE_PATTERNS):
        return True

    # Cek HTML tag (502 Bad Gateway / nginx maintenance page)
    if _HTML_TAG_RE.search(str(exc)):
        return True

    # Cek class name untuk requests / urllib3 errors
    cls_name = type(exc).__name__.lower()
    if any(p in cls_name for p in (
        "connectionerror", "connecttimeout", "readtimeout",
        "sslerror", "proxyerror", "chunkedencodingerror",
    )):
        return True

    # Binance python-binance BinanceAPIException dengan status 502/503
    # — repr menyertakan status code sebagai string
    if hasattr(exc, "status_code") and getattr(exc, "status_code", 0) in (502, 503, 504):
        return True

    return False


def classify_connection_error(exc: Exception, exchange: str) -> ExchangeOutage | None:
    """
    Jika exc adalah outage, kembalikan ExchangeOutage yang siap di-raise.
    Jika bukan outage (bug kode, config error), kembalikan None.

    Contoh usage:
        try:
            client = get_futures_client()
        except Exception as e:
            outage = classify_connection_error(e, "futures")
            if outage:
                raise outage
            raise   # re-raise original untuk error lain
    """
    if not is_outage_error(exc):
        return None

    msg = str(exc)
    # Buat reason yang singkat dan informatif
    if "502" in msg or "bad gateway" in msg.lower():
        reason = "502 Bad Gateway"
    elif "503" in msg or "service unavailable" in msg.lower():
        reason = "503 Service Unavailable"
    elif "504" in msg or "gateway timeout" in msg.lower():
        reason = "504 Gateway Timeout"
    elif "maintenance" in msg.lower():
        reason = "Scheduled Maintenance"
    elif any(p in msg.lower() for p in ("timed out", "timeout")):
        reason = "Connection Timeout"
    elif any(p in msg.lower() for p in ("connectionerror", "connection refused",
                                         "network is unreachable")):
        reason = "Connection Failed"
    else:
        # Ambil saja baris pertama tanpa traceback panjang
        reason = msg.splitlines()[0][:80]

    return ExchangeOutage(exchange=exchange, reason=reason)


# ── EXIT CODE constant ─────────────────────────────────────────────────────

EXIT_MAINTENANCE = 2   # live_bot.py checks rc == EXIT_MAINTENANCE
EXIT_FATAL       = 1   # unrecoverable error bukan maintenance
