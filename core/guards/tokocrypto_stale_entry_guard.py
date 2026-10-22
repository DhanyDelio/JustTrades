"""
core/guards/tokocrypto_stale_entry_guard.py
============================================
Conservative stale-entry classification and decision policy for Tokocrypto IDR Spot limit buys.

This module provides pure-function decision helpers without exchange mutations or database side-effects.

POLICY TIERS (Umur >= 3 hari, kapasitas 5 slot Tokocrypto):
-----------------------------------------------------------
1. NORMAL:
   Umur < 3 hari (atau data tidak valid / jarak < 15% pada order muda).
   Tidak ada tindakan stale.

2. OLD_DISTANCE_INSUFFICIENT:
   Umur >= 3 hari dan Jarak < 15%.
   Order tua tetapi harga masih dekat dengan entry; setup tetap dianggap valid untuk observasi.

3. REVIEW_REQUIRED:
   Umur >= 3 hari dan 15% <= Jarak < 20%.
   Tandai untuk review berkala.

4. CANCEL_CANDIDATE:
   Umur >= 3 hari dan Jarak >= 20%.
   Kandidat pembatalan untuk melepaskan slot Tokocrypto yang terbatas (5 slot);
   WAJIB validasi setup struktur chart sebelum boleh dieksekusi.
   (STALE_SERIOUS tetap tersedia jika TOKO_STALE_ENTRY_CANCEL_PCT dinaikkan di atas 20% via env var).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import os


class TokoStaleState(str, Enum):
    """Classification states for Tokocrypto pending limit buy entries."""
    NORMAL = "NORMAL"
    OLD_DISTANCE_INSUFFICIENT = "OLD_DISTANCE_INSUFFICIENT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    STALE_SERIOUS = "STALE_SERIOUS"
    CANCEL_CANDIDATE = "CANCEL_CANDIDATE"


@dataclass(frozen=True)
class TokoStaleThresholds:
    """Configurable age and distance thresholds for Tokocrypto stale entry policy."""
    min_age_days: float = 3.0
    review_pct: float = 15.0
    serious_pct: float = 20.0
    cancel_pct: float = 20.0

    @classmethod
    def from_env(cls) -> "TokoStaleThresholds":
        """Read thresholds from environment variables with fail-safe defaults."""
        def _parse_float(val: str | None, default: float, min_val: float = 0.0) -> float:
            if val is None:
                return default
            try:
                parsed = float(val)
                if math.isnan(parsed) or math.isinf(parsed) or parsed < min_val:
                    return default
                return parsed
            except (ValueError, TypeError):
                return default

        min_age = _parse_float(os.environ.get("TOKO_STALE_ENTRY_MIN_DAYS"), 3.0, min_val=0.1)

        # Backward-compatible with TOKO_STALE_ENTRY_MIN_DISTANCE_PCT
        review_val = os.environ.get("TOKO_STALE_ENTRY_REVIEW_PCT") or os.environ.get("TOKO_STALE_ENTRY_MIN_DISTANCE_PCT")
        review_dist = _parse_float(review_val, 15.0, min_val=0.0)

        serious_dist = _parse_float(os.environ.get("TOKO_STALE_ENTRY_SERIOUS_PCT"), 20.0, min_val=review_dist)
        cancel_dist = _parse_float(os.environ.get("TOKO_STALE_ENTRY_CANCEL_PCT"), 20.0, min_val=review_dist)

        return cls(
            min_age_days=min_age,
            review_pct=review_dist,
            serious_pct=serious_dist,
            cancel_pct=cancel_dist,
        )


def classify_tokocrypto_stale_entry(
    age_days: float | None,
    dist_pct: float | None,
    thresholds: TokoStaleThresholds | None = None,
) -> TokoStaleState:
    """
    Classify a pending BUY order based on age and distance from entry price.

    Rules:
    - If age_days is None or dist_pct is None or non-finite:
        -> NORMAL (fail-closed, not classified as stale)
    - If age_days < thresholds.min_age_days (3.0d):
        -> NORMAL (young order, setup still within normal execution window)
    - If age_days >= thresholds.min_age_days:
        - dist_pct < thresholds.review_pct (15.0%):
            -> OLD_DISTANCE_INSUFFICIENT (aged order, but price is near entry; valid setup)
        - thresholds.review_pct (15.0%) <= dist_pct < thresholds.serious_pct (20.0%):
            -> REVIEW_REQUIRED (flagged for review)
        - thresholds.serious_pct (20.0%) <= dist_pct < thresholds.cancel_pct (30.0%):
            -> STALE_SERIOUS (high priority review due to limited 5 slots)
        - dist_pct >= thresholds.cancel_pct (30.0%):
            -> CANCEL_CANDIDATE (eligible for setup validation before any cancel)
    """
    if age_days is None or dist_pct is None:
        return TokoStaleState.NORMAL

    if math.isnan(age_days) or math.isinf(age_days) or math.isnan(dist_pct) or math.isinf(dist_pct):
        return TokoStaleState.NORMAL

    thr = thresholds or TokoStaleThresholds.from_env()

    if age_days < thr.min_age_days:
        return TokoStaleState.NORMAL

    if dist_pct < thr.review_pct:
        return TokoStaleState.OLD_DISTANCE_INSUFFICIENT

    if dist_pct < thr.serious_pct:
        return TokoStaleState.REVIEW_REQUIRED

    if dist_pct < thr.cancel_pct:
        return TokoStaleState.STALE_SERIOUS

    return TokoStaleState.CANCEL_CANDIDATE
