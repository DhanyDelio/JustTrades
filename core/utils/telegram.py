"""
telegram.py — Standalone Telegram notifier.

Extracted from core/futures_trade_executor.py so it can be imported by any
module (futures_order_executor, futures_position_monitor, etc.) without
creating circular import chains.

No dependencies on the god file or any other internal module.
"""

from __future__ import annotations

import os

import requests


def send_telegram(message: str) -> None:
    """Send a plain-text Telegram message.  Silently no-ops if keys are absent."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    if any(p in f"{token}:{chat_id}".lower()
           for p in ("your_telegram", "replace_me", "placeholder")):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass
