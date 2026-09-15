"""Conservative stale-entry decision policy for unfilled Spot limit buys.

This module never talks to an exchange and never changes an order.  It makes
the decision auditable before the monitor performs the final exchange check.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os


REVIEW_PCT = float(os.getenv("STALE_ENTRY_REVIEW_PCT", "20"))
REVALIDATE_PCT = float(os.getenv("STALE_ENTRY_REVALIDATE_PCT", "30"))
CANCEL_PCT = float(os.getenv("STALE_ENTRY_CANCEL_PCT", "40"))
MIN_AGE_DAYS = float(os.getenv("STALE_ENTRY_MIN_AGE_DAYS", "3"))
REVALIDATION_HOURS = float(os.getenv("STALE_ENTRY_REVALIDATION_HOURS", "24"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def evaluate(trade: dict, current_price: float, now: datetime | None = None) -> dict:
    """Return a pure guard decision; only zero-fill NEW entries are eligible."""
    now = now or utc_now()
    entry = float(trade.get("entry_price") or 0)
    status = str(trade.get("entry_status") or "").upper()
    executed_qty = float(trade.get("entry_qty") or 0) if status != "NEW" else 0.0
    if entry <= 0 or current_price <= 0:
        return {"action": "NONE", "reason": "PRICE_UNAVAILABLE"}
    if status != "NEW" or executed_qty > 0:
        return {"action": "NONE", "reason": "NOT_UNFILLED_NEW"}

    distance_pct = (current_price - entry) / entry * 100
    opened = parse_time(trade.get("open_time"))
    age_days = ((now - opened).total_seconds() / 86400) if opened else None
    raw = dict(trade.get("raw_entry_order") or {})
    guard = dict(raw.get("pending_entry_guard") or {})
    verdict = guard.get("last_revalidation", {}).get("zone_valid")
    last_revalidation = parse_time(guard.get("last_revalidation_at"))
    fresh_revalidation = bool(
        last_revalidation
        and (now - last_revalidation).total_seconds() <= REVALIDATION_HOURS * 3600
    )

    result = {
        "distance_pct": round(distance_pct, 4),
        "age_days": round(age_days, 4) if age_days is not None else None,
        "zone_valid": verdict,
        "revalidation_fresh": fresh_revalidation,
    }
    if distance_pct < REVIEW_PCT:
        return {"action": "NONE", **result}
    if distance_pct < REVALIDATE_PCT or age_days is None or age_days < MIN_AGE_DAYS:
        return {"action": "REVIEW_REQUIRED", **result}
    if not fresh_revalidation:
        return {"action": "REVALIDATE", **result}
    if distance_pct >= CANCEL_PCT and verdict is False:
        return {"action": "CANCEL_ELIGIBLE", **result}
    return {"action": "REVIEW_REQUIRED", **result}
