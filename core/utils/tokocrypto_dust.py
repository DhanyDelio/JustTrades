"""
core/utils/tokocrypto_dust.py — Read-only dust balance detection and telemetry for Tokocrypto.

SAFETY INVARIANTS:
  - Strictly read-only: NEVER places orders, transfers, or converts balances.
  - Dust values are informational estimates only: NEVER credited to realized_pnl_idr
    or available IDR trading capital.
  - Fail-closed: Any ambiguity in exchange or database state marks an asset as UNVERIFIED.
  - Active positions / OCO orders (e.g. SOL) are classified as ACTIVE_POSITION_RESIDUAL
    and strictly excluded from eligible dust totals.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------

DEFAULT_DUST_THRESHOLD_IDR = float(os.getenv("DUST_THRESHOLD_IDR", "20000.0"))
DEFAULT_DUST_NOTIFY_COOLDOWN_SEC = float(os.getenv("DUST_NOTIFY_COOLDOWN_SEC", "86400.0"))  # 24 hours
DEFAULT_DUST_NOTIFY_DELTA_IDR = float(os.getenv("DUST_NOTIFY_DELTA_IDR", "5000.0"))

DISCLAIMER_TEXT = (
    "Estimasi saldo kecil untuk referensi. Konversi dilakukan secara "
    "manual melalui aplikasi resmi Tokocrypto."
)

# Classification categories
DUST_CANDIDATE = "DUST_CANDIDATE"
ACTIVE_POSITION_RESIDUAL = "ACTIVE_POSITION_RESIDUAL"
UNVERIFIED = "UNVERIFIED"

ACTIVE_POSITION_WARNING = "JANGAN DIKONVERSI — POSISI/ORDER MASIH AKTIF"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class DustItem:
    """Classified representation of a single crypto asset's balance."""

    asset: str
    free: float
    locked: float
    price_idr: float | None
    estimated_value_idr: float | None
    classification: str
    warning: str | None = None
    reason: str | None = None

    @property
    def is_eligible_dust(self) -> bool:
        return self.classification == DUST_CANDIDATE


@dataclass
class DustSummary:
    """Aggregated dust report across all wallet assets."""

    items: list[DustItem] = field(default_factory=list)
    total_eligible_dust_idr: float = 0.0
    eligible_count: int = 0
    active_residual_count: int = 0
    unverified_count: int = 0
    threshold_idr: float = DEFAULT_DUST_THRESHOLD_IDR
    has_unverified: bool = False
    evaluated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def eligible_items(self) -> list[DustItem]:
        return [i for i in self.items if i.classification == DUST_CANDIDATE]

    @property
    def active_residual_items(self) -> list[DustItem]:
        return [i for i in self.items if i.classification == ACTIVE_POSITION_RESIDUAL]

    @property
    def unverified_items(self) -> list[DustItem]:
        return [i for i in self.items if i.classification == UNVERIFIED]


# ---------------------------------------------------------------------------
# Price Resolution Helper
# ---------------------------------------------------------------------------


def get_asset_price_idr(client: Any, asset: str) -> float | None:
    """
    Fetch live reference price in IDR using Tokocrypto client infrastructure.
    Order of preference:
      1. ASSET_IDR ticker directly
      2. ASSET_USDT ticker multiplied by USDT_IDR ticker
    Returns float price > 0, or None if unavailable/error.
    """
    if not client or not asset or asset.upper() == "IDR":
        return 1.0 if asset and asset.upper() == "IDR" else None

    asset_upper = asset.upper()

    # 1. Direct IDR pair
    try:
        p = client.get_ticker(f"{asset_upper}_IDR")
        if p and p > 0 and math.isfinite(p):
            return float(p)
    except Exception:
        pass

    # 2. Synthetic cross through USDT
    try:
        p_usdt = client.get_ticker(f"{asset_upper}_USDT")
        p_usdt_idr = client.get_ticker("USDT_IDR")
        if p_usdt and p_usdt > 0 and p_usdt_idr and p_usdt_idr > 0:
            val = float(p_usdt * p_usdt_idr)
            if math.isfinite(val) and val > 0:
                return val
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Classification & Aggregation Logic
# ---------------------------------------------------------------------------


def classify_and_aggregate_dust(
    balances: list[Any],
    client: Any,
    open_trades: list[dict] | None,
    exchange_open_orders: list[dict] | None,
    threshold_idr: float | None = None,
    price_lookup: dict[str, float] | None = None,
) -> DustSummary:
    """
    Classify wallet balances into DUST_CANDIDATE, ACTIVE_POSITION_RESIDUAL, or UNVERIFIED.

    Invariants:
      - If open_trades is None (DB failure) or exchange_open_orders is None (API failure),
        assets with balance are classified as UNVERIFIED (fail closed).
      - Assets with locked balance > 0, or matching open orders/trades, are classified as
        ACTIVE_POSITION_RESIDUAL.
      - Total eligible dust ONLY sums DUST_CANDIDATE items.
    """
    threshold = (
        float(threshold_idr)
        if threshold_idr is not None
        else DEFAULT_DUST_THRESHOLD_IDR
    )

    # Verification status flags
    db_verified = open_trades is not None
    exchange_verified = exchange_open_orders is not None

    # Collect symbols with active exposure in database
    active_db_symbols: set[str] = set()
    if db_verified:
        for t in open_trades:
            sym = str(t.get("symbol") or "").upper()
            exit_st = str(t.get("exit_status") or "").upper()
            entry_st = str(t.get("entry_status") or "").upper()
            oco_st = str(t.get("oco_state") or "").upper()
            # If trade is not definitely closed and settled, consider it active
            if exit_st == "OPEN" or entry_st in ("NEW", "PARTIALLY_FILLED", "FILLED", "ENTRY_SUBMISSION_PENDING") or oco_st in ("EXECUTING", "RECONCILIATION_REQUIRED", "STUCK_OCO", "CRITICAL_ANOMALY"):
                if sym:
                    active_db_symbols.add(sym)

    # Collect symbols with active working orders on exchange
    active_exchange_symbols: set[str] = set()
    if exchange_verified:
        for o in exchange_open_orders:
            sym = str(o.get("symbol") or "").upper()
            st = str(o.get("status") or "").upper()
            if st in ("0", "1", "NEW", "PARTIALLY_FILLED") or o.get("orderId"):
                if sym:
                    active_exchange_symbols.add(sym)

    items: list[DustItem] = []
    total_eligible = 0.0
    n_eligible = 0
    n_active = 0
    n_unverified = 0

    for bal in balances or []:
        # Extract asset, free, locked from dict or ExchangeBalance object
        if hasattr(bal, "asset"):
            asset = str(bal.asset).upper()
            free = float(bal.free or 0.0)
            locked = float(bal.locked or 0.0)
        elif isinstance(bal, dict):
            asset = str(bal.get("asset") or "").upper()
            free = float(bal.get("free") or 0.0)
            locked = float(bal.get("locked") or 0.0)
        else:
            continue

        # Skip fiat IDR and zero balances
        if not asset or asset == "IDR":
            continue
        if free <= 0.0 and locked <= 0.0:
            continue

        # Resolve price
        price: float | None = None
        if price_lookup and asset in price_lookup:
            price = price_lookup[asset]
        elif client:
            price = get_asset_price_idr(client, asset)

        est_value: float | None = None
        if price is not None and price > 0 and math.isfinite(price):
            est_value = free * price

        # Check association with active positions/orders
        associated_symbol = f"{asset}_IDR"
        is_in_active_db = associated_symbol in active_db_symbols
        is_in_active_exchange = associated_symbol in active_exchange_symbols
        has_locked = locked > 0.0

        # Classification decision tree
        if not db_verified or not exchange_verified:
            # Required verification failed -> fail closed to UNVERIFIED
            classification = UNVERIFIED
            reason = (
                "Verifikasi database gagal"
                if not db_verified
                else "Verifikasi exchange order gagal"
            )
            warning = None
            n_unverified += 1
        elif has_locked or is_in_active_db or is_in_active_exchange:
            # Associated with active position / pending order / OCO
            classification = ACTIVE_POSITION_RESIDUAL
            warning = ACTIVE_POSITION_WARNING
            reason = (
                f"Terkait order aktif ({'locked balance' if has_locked else ''}"
                f"{', open order exchange' if is_in_active_exchange else ''}"
                f"{', posisi DB aktif' if is_in_active_db else ''})".strip()
            )
            n_active += 1
        elif price is None or est_value is None:
            # Missing or unqueryable price -> cannot determine notional
            classification = UNVERIFIED
            warning = None
            reason = "Harga ticker tidak tersedia"
            n_unverified += 1
        elif free > 0.0 and est_value < threshold:
            # Free balance under threshold with zero locked and clean verification
            classification = DUST_CANDIDATE
            warning = None
            reason = f"Saldo kecil bebas (< Rp {threshold:,.0f})"
            total_eligible += est_value
            n_eligible += 1
        else:
            # Balance value meets or exceeds threshold -> normal holding, not dust
            classification = UNVERIFIED
            warning = None
            reason = f"Nilai saldo (Rp {est_value:,.0f}) mencapai/melebihi batas dust (Rp {threshold:,.0f})"
            n_unverified += 1

        items.append(
            DustItem(
                asset=asset,
                free=free,
                locked=locked,
                price_idr=price,
                estimated_value_idr=est_value,
                classification=classification,
                warning=warning,
                reason=reason,
            )
        )

    # Sort items: DUST_CANDIDATE first (highest value desc), then ACTIVE, then UNVERIFIED
    def _sort_key(item: DustItem):
        order = {DUST_CANDIDATE: 0, ACTIVE_POSITION_RESIDUAL: 1, UNVERIFIED: 2}
        val = item.estimated_value_idr or 0.0
        return (order.get(item.classification, 3), -val)

    items.sort(key=_sort_key)

    return DustSummary(
        items=items,
        total_eligible_dust_idr=round(total_eligible, 2),
        eligible_count=n_eligible,
        active_residual_count=n_active,
        unverified_count=n_unverified,
        threshold_idr=threshold,
        has_unverified=n_unverified > 0,
    )


# ---------------------------------------------------------------------------
# Telegram Formatting & Persistent Cooldown
# ---------------------------------------------------------------------------


def format_dust_telegram_message(summary: DustSummary) -> str:
    """Format a clean, concise Telegram notification body."""
    lines = [
        "🧹 LAPORAN SALDO KECIL (DUST)",
        f"Total estimasi debu bebas: ~Rp {summary.total_eligible_dust_idr:,.0f} IDR",
        "",
    ]

    eligible = summary.eligible_items
    if eligible:
        lines.append("🟢 Kandidat Siap Ditinjau di Aplikasi Tokocrypto:")
        for item in eligible:
            val_s = f"~Rp {item.estimated_value_idr:,.0f}" if item.estimated_value_idr else "?"
            lines.append(f"  • {item.asset}: {item.free:,.8f}".rstrip("0").rstrip(".") + f" ({val_s})")
        lines.append("")

    active = summary.active_residual_items
    if active:
        lines.append("🟡 Terkait Posisi/Order Aktif (JANGAN DIKONVERSI):")
        for item in active:
            val_s = f"~Rp {item.estimated_value_idr:,.0f}" if item.estimated_value_idr else "?"
            lines.append(
                f"  • {item.asset}: free={item.free:,.8f}".rstrip("0").rstrip(".")
                + f", locked={item.locked:,.8f}".rstrip("0").rstrip(".")
                + f" ({val_s})"
            )
        lines.append("")

    unverified = summary.unverified_items
    if unverified:
        lines.append("⚪ Saldo Belum Terverifikasi / Normal:")
        for item in unverified:
            val_s = f"~Rp {item.estimated_value_idr:,.0f}" if item.estimated_value_idr else "?"
            reason_s = f" [{item.reason}]" if item.reason else ""
            lines.append(f"  • {item.asset}: {item.free:,.8f}".rstrip("0").rstrip(".") + f" ({val_s}){reason_s}")
        lines.append("")

    lines.append("ℹ️ PENTING:")
    lines.append("• Bot TIDAK PERNAH mengonversi atau menjual aset secara otomatis.")
    lines.append(f"• {DISCLAIMER_TEXT}")
    lines.append("• Buka menu: Dompet Spot → Tukar Saldo Kecil.")

    return "\n".join(lines)


def get_default_dust_state_path() -> Path:
    """Resolve the persistent state file path in data/json/."""
    base_dir = Path(__file__).resolve().parent.parent.parent
    state_dir = base_dir / "data" / "json"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "tokocrypto_dust_state.json"


def load_dust_notification_state(state_file: str | Path | None = None) -> dict:
    """Load notification state from disk, returning defaults on any error."""
    path = Path(state_file) if state_file else get_default_dust_state_path()
    if not path.exists():
        return {
            "last_sent_ts": 0.0,
            "last_sent_iso": "",
            "last_eligible_total_idr": 0.0,
            "last_eligible_assets": [],
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.warning("Could not read dust notification state file (%s): %s", path, exc)

    return {
        "last_sent_ts": 0.0,
        "last_sent_iso": "",
        "last_eligible_total_idr": 0.0,
        "last_eligible_assets": [],
    }


def save_dust_notification_state(
    state: dict, state_file: str | Path | None = None
) -> None:
    """Persist notification state to disk atomically."""
    path = Path(state_file) if state_file else get_default_dust_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp_path.replace(path)
    except Exception as exc:
        logger.warning("Could not save dust notification state file (%s): %s", path, exc)


def should_send_dust_notification(
    summary: DustSummary,
    state_file: str | Path | None = None,
    cooldown_sec: float | None = None,
    delta_threshold_idr: float | None = None,
    now_ts: float | None = None,
) -> bool:
    """
    Evaluate whether a Telegram notification should be sent based on cooldown and value changes.

    Rules:
      1. If there are NO eligible dust items and NO active residuals, suppress notification.
      2. If never notified before, return True.
      3. If eligible dust total changes by >= delta_threshold_idr (default Rp 5,000), return True.
      4. If elapsed time >= cooldown_sec (default 24h), return True.
      5. Otherwise, return False (cooldown active, delta insufficient).
    """
    if summary.eligible_count == 0 and summary.active_residual_count == 0:
        return False

    cooldown = (
        float(cooldown_sec)
        if cooldown_sec is not None
        else DEFAULT_DUST_NOTIFY_COOLDOWN_SEC
    )
    delta_thresh = (
        float(delta_threshold_idr)
        if delta_threshold_idr is not None
        else DEFAULT_DUST_NOTIFY_DELTA_IDR
    )
    current_ts = float(now_ts) if now_ts is not None else time.time()

    state = load_dust_notification_state(state_file)
    last_sent_ts = float(state.get("last_sent_ts") or 0.0)
    last_total = float(state.get("last_eligible_total_idr") or 0.0)

    # First time sending
    if last_sent_ts <= 0.0:
        return True

    # Check value change delta
    delta = abs(summary.total_eligible_dust_idr - last_total)
    if delta >= delta_thresh:
        return True

    # Check time cooldown (24h)
    elapsed = current_ts - last_sent_ts
    if elapsed >= cooldown:
        return True

    return False


def record_dust_notification_sent(
    summary: DustSummary,
    state_file: str | Path | None = None,
    now_ts: float | None = None,
) -> None:
    """Record that a notification was sent."""
    current_ts = float(now_ts) if now_ts is not None else time.time()
    state = {
        "last_sent_ts": current_ts,
        "last_sent_iso": datetime.now(timezone.utc).isoformat(),
        "last_eligible_total_idr": summary.total_eligible_dust_idr,
        "last_eligible_assets": [i.asset for i in summary.eligible_items],
    }
    save_dust_notification_state(state, state_file)


def notify_dust_summary_if_needed(
    summary: DustSummary,
    sender_fn: Callable[[str], None] | None = None,
    state_file: str | Path | None = None,
    cooldown_sec: float | None = None,
    delta_threshold_idr: float | None = None,
    now_ts: float | None = None,
) -> bool:
    """
    Format and dispatch dust summary via sender_fn if deduplication allows it.
    Returns True if sent, False if suppressed.
    """
    if not sender_fn:
        return False

    if not should_send_dust_notification(
        summary=summary,
        state_file=state_file,
        cooldown_sec=cooldown_sec,
        delta_threshold_idr=delta_threshold_idr,
        now_ts=now_ts,
    ):
        return False

    msg = format_dust_telegram_message(summary)
    try:
        sender_fn(msg)
        record_dust_notification_sent(
            summary=summary, state_file=state_file, now_ts=now_ts
        )
        return True
    except Exception as exc:
        logger.warning("Failed to send dust notification via sender_fn: %s", exc)
        return False
