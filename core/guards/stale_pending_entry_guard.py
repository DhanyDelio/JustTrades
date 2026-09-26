"""Conservative stale-entry decision policy for unfilled Spot limit buys.

This module never talks to an exchange and never changes an order.  It makes
the decision auditable before the monitor performs the final exchange check.

STATE MACHINE
-------------

ACTIVE_PENDING
    Default state for any NEW order where price hasn't moved significantly.

STALE_REVIEW
    Order age ≥ REVIEW_AFTER_DAYS OR price moved into moderate band.
    Human/system review suggested, but no automatic action.

RUNAWAY
    Price has moved ≥ RUNAWAY_DISTANCE_PCT above entry.
    Entry zone is likely no longer valid.  Revalidation required.

EXPIRED
    Order age ≥ EXPIRE_AFTER_DAYS AND price ≥ CANCEL_PCT above entry.
    The setup has aged beyond any reasonable holding window.
    Eligible for shadow WOULD_CANCEL recording.

WOULD_CANCEL
    Shadow record of what auto-cancel would do.
    Auto-cancel is deliberately OFF until HumanDirector approves.
    No exchange action taken.

CANCEL_ELIGIBLE  (existing — kept for backward compat)
    Decision engine says all conditions for cancellation are met.
    Still gated by STALE_ENTRY_AUTO_CANCEL_ENABLED env flag.

THRESHOLDS (all configurable via environment)
---------------------------------------------
STALE_ENTRY_REVIEW_PCT          default 20   — moderate distance → REVIEW_REQUIRED
STALE_ENTRY_REVALIDATE_PCT      default 30   — larger distance → REVALIDATE
STALE_ENTRY_CANCEL_PCT          default 40   — eligible for cancel
STALE_ENTRY_MIN_AGE_DAYS        default 3    — min age before revalidation
STALE_ENTRY_REVALIDATION_HOURS  default 24   — freshness window for zone recheck
STALE_ENTRY_REVIEW_AFTER_DAYS   default 5    — age at which STALE_REVIEW state starts
STALE_ENTRY_EXPIRE_AFTER_DAYS   default 30   — age at which EXPIRED state can start
STALE_ENTRY_RUNAWAY_DISTANCE_PCT default 10  — runaway classification threshold
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class StaleEntryState(str, Enum):
    """
    Lifecycle states for a pending (unfilled) Spot limit buy.

    Values are plain strings so they serialise to JSON without extra
    conversion and remain backward-compatible with code that stored raw
    strings ("NONE", "REVIEW_REQUIRED", etc.).
    """
    ACTIVE_PENDING  = "ACTIVE_PENDING"
    STALE_REVIEW    = "STALE_REVIEW"
    RUNAWAY         = "RUNAWAY"
    EXPIRED         = "EXPIRED"
    WOULD_CANCEL    = "WOULD_CANCEL"
    CANCEL_ELIGIBLE = "CANCEL_ELIGIBLE"   # kept for backward-compat with monitor
    CANCELLED       = "CANCELLED"
    FILLED          = "FILLED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    # Legacy action values returned by evaluate() — kept for compat
    NONE            = "NONE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVALIDATE      = "REVALIDATE"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Configurable thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StaleEntryThresholds:
    """
    All distance/age thresholds for the guard.

    Frozen dataclass: constructed once from env vars (or test overrides)
    and never mutated.  Pass an instance to evaluate() to override defaults
    in tests without touching os.environ.
    """
    review_pct:            float  # dist% → REVIEW_REQUIRED
    revalidate_pct:        float  # dist% → REVALIDATE
    cancel_pct:            float  # dist% → CANCEL_ELIGIBLE (with fresh bad zone)
    min_age_days:          float  # min age before revalidation counts
    revalidation_hours:    float  # freshness window for zone-recheck result
    review_after_days:     float  # age → STALE_REVIEW lifecycle state
    expire_after_days:     float  # age → EXPIRED lifecycle state
    runaway_distance_pct:  float  # dist% → RUNAWAY lifecycle state

    @classmethod
    def from_env(cls) -> "StaleEntryThresholds":
        """Read thresholds from environment variables, falling back to defaults."""
        return cls(
            review_pct           = float(os.getenv("STALE_ENTRY_REVIEW_PCT",           "20")),
            revalidate_pct       = float(os.getenv("STALE_ENTRY_REVALIDATE_PCT",       "30")),
            cancel_pct           = float(os.getenv("STALE_ENTRY_CANCEL_PCT",           "40")),
            min_age_days         = float(os.getenv("STALE_ENTRY_MIN_AGE_DAYS",         "3")),
            revalidation_hours   = float(os.getenv("STALE_ENTRY_REVALIDATION_HOURS",   "24")),
            review_after_days    = float(os.getenv("STALE_ENTRY_REVIEW_AFTER_DAYS",    "5")),
            expire_after_days    = float(os.getenv("STALE_ENTRY_EXPIRE_AFTER_DAYS",    "30")),
            runaway_distance_pct = float(os.getenv("STALE_ENTRY_RUNAWAY_DISTANCE_PCT", "10")),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def classify_lifecycle(
    distance_pct: float | None,
    age_days:     float | None,
    thresholds:   StaleEntryThresholds,
) -> StaleEntryState:
    """
    Assign a human-readable lifecycle state based on age and distance only.
    This is purely informational — it does NOT gate exchange actions.

    Priority (most severe first):
        EXPIRED      — age ≥ expire_after_days AND dist ≥ cancel_pct
        RUNAWAY      — dist ≥ runaway_distance_pct
        STALE_REVIEW — age ≥ review_after_days
        ACTIVE_PENDING — everything else
    """
    if distance_pct is None:
        return StaleEntryState.ACTIVE_PENDING

    is_old     = age_days is not None and age_days >= thresholds.expire_after_days
    is_runaway = distance_pct >= thresholds.runaway_distance_pct
    is_stale   = age_days is not None and age_days >= thresholds.review_after_days

    if is_old and distance_pct >= thresholds.cancel_pct:
        return StaleEntryState.EXPIRED
    if is_runaway:
        return StaleEntryState.RUNAWAY
    if is_stale:
        return StaleEntryState.STALE_REVIEW
    return StaleEntryState.ACTIVE_PENDING


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate(
    trade:          dict,
    current_price:  float,
    now:            datetime | None = None,
    thresholds:     StaleEntryThresholds | None = None,
) -> dict:
    """
    Return a pure guard decision for a single unfilled entry.

    Only zero-fill NEW entries are eligible; filled or partial orders
    always return action=NONE so the OCO / position monitor is unaffected.

    The returned dict includes:
        action         — NONE | REVIEW_REQUIRED | REVALIDATE | CANCEL_ELIGIBLE
        lifecycle      — StaleEntryState value (informational)
        distance_pct   — how far current price is above entry
        age_days       — order age in days (None if open_time missing)
        zone_valid     — last revalidation verdict (True/False/"unknown"/None)
        revalidation_fresh — whether the last revalidation is within the window

    This function has no side effects and never modifies the trade dict.
    """
    now = now or utc_now()
    thr = thresholds or StaleEntryThresholds.from_env()

    entry        = float(trade.get("entry_price") or 0)
    status       = str(trade.get("entry_status") or "").upper()
    executed_qty = float(trade.get("entry_qty") or 0) if status != "NEW" else 0.0

    if entry <= 0 or current_price <= 0:
        return {
            "action":    StaleEntryState.NONE,
            "lifecycle": StaleEntryState.ACTIVE_PENDING,
            "reason":    "PRICE_UNAVAILABLE",
        }

    if status != "NEW" or executed_qty > 0:
        return {
            "action":    StaleEntryState.NONE,
            "lifecycle": StaleEntryState.FILLED if status == "FILLED"
                         else StaleEntryState.ACTIVE_PENDING,
            "reason":    "NOT_UNFILLED_NEW",
        }

    distance_pct     = (current_price - entry) / entry * 100
    opened           = parse_time(trade.get("open_time"))
    age_days         = ((now - opened).total_seconds() / 86400) if opened else None

    raw              = dict(trade.get("raw_entry_order") or {})
    guard            = dict(raw.get("pending_entry_guard") or {})
    verdict          = guard.get("last_revalidation", {}).get("zone_valid")
    last_reval_at    = parse_time(guard.get("last_revalidation_at"))
    fresh_reval      = bool(
        last_reval_at
        and (now - last_reval_at).total_seconds() <= thr.revalidation_hours * 3600
    )

    lifecycle = classify_lifecycle(distance_pct, age_days, thr)

    base = {
        "distance_pct":         round(distance_pct, 4),
        "age_days":             round(age_days, 4) if age_days is not None else None,
        "zone_valid":           verdict,
        "revalidation_fresh":   fresh_reval,
        "lifecycle":            lifecycle,
    }

    # ── Action decision (unchanged logic, now uses configurable thr) ──
    if distance_pct < thr.review_pct:
        return {"action": StaleEntryState.NONE, **base}

    if distance_pct < thr.revalidate_pct or age_days is None or age_days < thr.min_age_days:
        return {"action": StaleEntryState.REVIEW_REQUIRED, **base}

    if not fresh_reval:
        return {"action": StaleEntryState.REVALIDATE, **base}

    if distance_pct >= thr.cancel_pct and verdict is False:
        return {"action": StaleEntryState.CANCEL_ELIGIBLE, **base}

    return {"action": StaleEntryState.REVIEW_REQUIRED, **base}
