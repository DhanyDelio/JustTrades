"""
tokocrypto_order_executor.py — Stage 2 order execution layer for Tokocrypto.

Wraps TokocryptoClient with:
  - Supervised confirm gate (Phase 2: every order requires explicit 'y')
  - Dry-run mode (no real POST calls, prints [DRY RUN] prefix)
  - Supabase persistence via upsert_tokocrypto / update_tokocrypto_by_order_id
  - Telegram notifications via _send_toko_telegram (prefixes [Toko_Crypto_Spot])

ALL real-money orders are guarded by:
  1. validate_and_size — notional / min_qty / step checks before any API call
  2. _confirm — explicit 'y' gate when supervised=True
  3. dry_run=True — skips every _signed_post call entirely

NEVER import from binance.* — all exchange calls go through TokocryptoClient.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

from core.clients.tokocrypto_client import (
    TokocryptoClient,
    TokocryptoError,
    TokocryptoMalformedResponseError,
)
from services.supabase_client import upsert_tokocrypto, update_tokocrypto_by_order_id
from core.paper_trade_executor import _send_toko_telegram


# ---------------------------------------------------------------------------
# Order status integer mapping (Tokocrypto uses int codes, not string literals)
# ---------------------------------------------------------------------------
_STATUS = {
    -2: "SYSTEM_PROCESSING",
     0: "NEW",
     1: "PARTIALLY_FILLED",
     2: "FILLED",
     3: "CANCELED",
     4: "PENDING_CANCEL",
     5: "REJECTED",
     6: "EXPIRED",
}


# ---------------------------------------------------------------------------
# Supervised gate — module-level so tests can patch it directly
# ---------------------------------------------------------------------------

def _confirm(prompt: str) -> bool:
    """
    Phase 2 supervised gate.  Returns True only on explicit 'y' / 'yes' answer.

    In non-interactive contexts (CI, tests with stdin closed) input() raises
    EOFError — caught here and returns False (safe default: abort).
    """
    try:
        ans = input(f"\n  {prompt} [y/N]: ").strip().lower()
    except EOFError:
        # Non-interactive context → safe default: abort
        return False
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# Decimal precision helpers & Pre-flight OCO eligibility
# ---------------------------------------------------------------------------

def decimal_round_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor value down to nearest multiple of step."""
    if step <= Decimal("0"):
        return value
    steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


def decimal_round_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Round price to nearest multiple of tick using ROUND_HALF_UP."""
    if tick <= Decimal("0"):
        return value
    ticks = (value / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return ticks * tick


def get_default_sl_buffer_pct(entry_price: Decimal | float | str) -> Decimal:
    """
    Compute stop-limit slippage buffer percentage based on entry price tier.
      < Rp 1,000      → 0.35%  (micro/penny coins: wide spreads, fast drops)
      Rp 1,000-10,000 → 0.25%  (mid-low tier)
      > Rp 10,000     → 0.15%  (major coins: deep books, tight spreads)
    """
    ep = Decimal(str(entry_price))
    if ep < Decimal("1000"):
        return Decimal("0.0035")
    elif ep <= Decimal("10000"):
        return Decimal("0.0025")
    else:
        return Decimal("0.0015")


def validate_protective_oco_eligibility(
    entry_qty: Decimal | float | str,
    entry_price: Decimal | float | str,
    tp_price: Decimal | float | str,
    sl_price: Decimal | float | str,
    sym_info: Any,
    fee_rate: Decimal | float | str = Decimal("0.0015"),
    fee_deducted_from_base: bool = True,
    sl_buffer_pct: Decimal | float | str | None = None,
) -> tuple[bool, str, dict]:
    """
    Generic pre-flight protective OCO eligibility calculation.

    Evaluates whether an entry BUY order will leave sufficient quantity
    and notional to place a protective OCO sell order after:
      1. Trading fee deduction
      2. Step-size precision rounding (floor)
      3. Minimum quantity constraint
      4. Minimum notional constraint on TP leg
      5. Minimum notional constraint on SL stop-limit leg
      6. Tick-size price rounding on TP and SL legs
      7. Price hierarchy validation: TP > Entry > SL Stop > SL Limit

    Returns:
        (is_eligible: bool, reason: str, details: dict)
    """
    d_entry_qty   = Decimal(str(entry_qty))
    d_entry_price = Decimal(str(entry_price))
    d_tp_price    = Decimal(str(tp_price))
    d_sl_price    = Decimal(str(sl_price))
    d_fee_rate    = Decimal(str(fee_rate))

    # Extract symbol filter constraints
    tick_size = getattr(sym_info, "tick_size", None)
    if tick_size is None and isinstance(sym_info, dict):
        tick_size = sym_info.get("tick_size")
    d_tick = Decimal(str(tick_size or "0"))

    step_size = getattr(sym_info, "step_size", None)
    if step_size is None and isinstance(sym_info, dict):
        step_size = sym_info.get("step_size")
    d_step = Decimal(str(step_size or "0"))

    min_qty = getattr(sym_info, "min_qty", None)
    if min_qty is None and isinstance(sym_info, dict):
        min_qty = sym_info.get("min_qty")
    d_min_qty = Decimal(str(min_qty or "0"))

    min_notional = getattr(sym_info, "min_notional", None)
    if min_notional is None and isinstance(sym_info, dict):
        min_notional = sym_info.get("min_notional")
    d_min_notional = Decimal(str(min_notional or "0"))
    if d_min_notional <= Decimal("0"):
        d_min_notional = Decimal("20000.0")

    # Protective order prices
    if sl_buffer_pct is None:
        d_buf_pct = get_default_sl_buffer_pct(d_entry_price)
    else:
        d_buf_pct = Decimal(str(sl_buffer_pct))

    d_tp_rounded = decimal_round_tick(d_tp_price, d_tick)
    d_sl_stop    = decimal_round_tick(d_sl_price, d_tick)
    d_sl_limit_raw = d_sl_price * (Decimal("1") - d_buf_pct)
    d_sl_limit   = decimal_round_tick(d_sl_limit_raw, d_tick)

    # Guard: SL limit must be strictly below SL stop
    if d_sl_limit >= d_sl_stop and d_tick > Decimal("0"):
        d_sl_limit = decimal_round_tick(d_sl_stop - d_tick, d_tick)

    # 1. Expected filled quantity
    expected_fill_qty = d_entry_qty

    # 2. Expected post-fee quantity
    if fee_deducted_from_base:
        expected_post_fee_qty = expected_fill_qty * (Decimal("1") - d_fee_rate)
    else:
        expected_post_fee_qty = expected_fill_qty

    # 3. Usable OCO quantity after step-size rounding (floor down)
    usable_oco_qty = decimal_round_step(expected_post_fee_qty, d_step)

    # Notionals
    tp_notional = usable_oco_qty * d_tp_rounded
    sl_limit_notional = usable_oco_qty * d_sl_limit

    details = {
        "expected_fill_qty":      expected_fill_qty,
        "expected_post_fee_qty":  expected_post_fee_qty,
        "usable_oco_qty":         usable_oco_qty,
        "tp_price_rounded":       d_tp_rounded,
        "sl_stop_price":          d_sl_stop,
        "sl_limit_price":         d_sl_limit,
        "tp_notional":            tp_notional,
        "sl_limit_notional":      sl_limit_notional,
        "min_notional":           d_min_notional,
        "min_qty":                d_min_qty,
        "step_size":              d_step,
        "tick_size":              d_tick,
        "fee_rate":               d_fee_rate,
        "fee_deducted_from_base": fee_deducted_from_base,
    }

    # Price hierarchy check: TP > entry > SL stop > SL limit
    if not (d_tp_rounded > d_entry_price > d_sl_stop > d_sl_limit):
        return (
            False,
            f"OCO_PRICE_HIERARCHY_INVALID: tp={d_tp_rounded} entry={d_entry_price} "
            f"sl_stop={d_sl_stop} sl_limit={d_sl_limit}",
            details,
        )

    # Usable quantity checks
    if usable_oco_qty <= Decimal("0"):
        return (
            False,
            f"OCO_INELIGIBLE_AFTER_FEE: usable quantity {usable_oco_qty} rounded to 0 "
            f"(post_fee={expected_post_fee_qty}, step={d_step})",
            details,
        )

    if usable_oco_qty < d_min_qty:
        return (
            False,
            f"OCO_INELIGIBLE_AFTER_FEE: usable quantity {usable_oco_qty} < min_qty {d_min_qty}",
            details,
        )

    # Leg notional checks
    if tp_notional < d_min_notional:
        return (
            False,
            f"OCO_INELIGIBLE_AFTER_FEE: TP notional Rp {tp_notional:,.2f} < min_notional Rp {d_min_notional:,.2f}",
            details,
        )

    if sl_limit_notional < d_min_notional:
        return (
            False,
            f"OCO_INELIGIBLE_AFTER_FEE: SL limit notional Rp {sl_limit_notional:,.2f} < min_notional Rp {d_min_notional:,.2f}",
            details,
        )

    return True, "OK", details


# ---------------------------------------------------------------------------
# Main executor class
# ---------------------------------------------------------------------------

class TokocryptoOrderExecutor:
    """
    Phase 2 order executor for Tokocrypto real-money spot trading.

    Parameters
    ----------
    client      : TokocryptoClient instance (authenticated)
    supervised  : If True (default), every order requires an explicit 'y'
                  confirmation via stdin before the POST is sent.
    dry_run     : If True, skip all _signed_post calls and print [DRY RUN]
                  lines instead.  Safe to use in all test/staging contexts.
    """

    MIN_NOTIONAL_IDR: float = 10_000.0   # Phase 2 hard floor
    MAX_SLOTS: int = int(os.environ.get("TOKO_MAX_POSITIONS", "5"))

    DEFAULT_FEE_RATE: Decimal = Decimal(os.environ.get("TOKO_DEFAULT_FEE_RATE", "0.0015"))  # 0.15% standard taker fee

    def __init__(
        self,
        client: TokocryptoClient,
        supervised: bool = True,
        trading_phase: str = "PHASE_3",
        dry_run: bool = False,
        max_slots: int | None = None,
    ) -> None:
        self.client         = client
        self.supervised     = supervised
        self._trading_phase = trading_phase   # written to DB at upsert — never rely on column default
        self.dry_run        = dry_run
        self._cached_fee_rate: Decimal | None = None
        if max_slots is not None:
            self.MAX_SLOTS  = max_slots

    def get_fee_config(self, symbol: str) -> tuple[Decimal, bool]:
        """
        Return (fee_rate, fee_deducted_from_base) for the given symbol.

        Tokocrypto Spot IDR pairs:
        - Trading fee on BUY is deducted from the received base asset (fee_deducted_from_base=True).
        - Fee rate is dynamically resolved from account takerCommission if client is authenticated,
          otherwise falls back to TOKO_DEFAULT_FEE_RATE env / DEFAULT_FEE_RATE (0.0015 / 0.15%).
        """
        fee_rate = self.DEFAULT_FEE_RATE
        if hasattr(self.client, "authenticated") and self.client.authenticated:
            if self._cached_fee_rate is None:
                try:
                    acc = self.client.get_account()
                    comm = acc.get("takerCommission")
                    if comm is not None:
                        val = Decimal(str(comm))
                        # Normalize basis points (e.g. 15.0 bps) vs decimal fraction (0.0015)
                        if val > Decimal("0.01"):
                            val = val / Decimal("10000")
                        if val > Decimal("0"):
                            self._cached_fee_rate = val
                except Exception:
                    self._cached_fee_rate = self.DEFAULT_FEE_RATE
            if self._cached_fee_rate:
                fee_rate = self._cached_fee_rate

        fee_deducted_from_base = True
        return fee_rate, fee_deducted_from_base

    def has_active_position(self, symbol: str) -> bool:
        """
        Check if an active lifecycle already exists for this symbol in Supabase.
        Only one active lifecycle (exit_status == 'OPEN') is allowed per symbol.
        """
        try:
            from services.supabase_client import fetch_all_tokocrypto
            trades = fetch_all_tokocrypto() or []
            return any(t.get("symbol") == symbol and t.get("exit_status") == "OPEN" for t in trades)
        except Exception as e:
            print(f"  [WARN] Failed to query active Tokocrypto positions for {symbol}: {e}")
            return False

    # ------------------------------------------------------------------
    # validate_and_size
    # ------------------------------------------------------------------

    def validate_and_size(self, cand: dict, available_idr: float) -> bool:
        """
        Validate sizing constraints and populate cand['sizing'] + cand['constraints'].

        Returns False (without raising) if:
          - symbol info cannot be fetched
          - qty rounds to 0 after step rounding
          - qty < sym.min_qty
          - notional (qty × price) < min_notional
          - pre-flight protective OCO eligibility fails

        On success, mutates cand in-place:
            cand["sizing"]["qty"]           — rounded quantity
            cand["sizing"]["slot_size_idr"] — IDR allocated to this slot
            cand["constraints"]             — sym.constraints dict
        """
        try:
            sym = self.client.get_symbol(cand["symbol"])
        except TokocryptoError as e:
            print(f"  ✗ validate_and_size: get_symbol failed: {e}")
            return False

        slot_size_idr = available_idr / self.MAX_SLOTS
        entry_price   = float(cand["entry_price"])

        if entry_price <= 0:
            print("  ✗ validate_and_size: entry_price must be > 0")
            return False

        qty = self.client.round_step(slot_size_idr / entry_price, sym.step_size)

        if qty <= 0:
            print(f"  ✗ validate_and_size: qty rounded to 0 (slot={slot_size_idr:.0f} IDR, price={entry_price})")
            return False

        if qty < sym.min_qty:
            print(f"  ✗ validate_and_size: qty={qty} < min_qty={sym.min_qty}")
            return False

        min_notional = sym.min_notional if sym.min_notional > 0 else self.MIN_NOTIONAL_IDR
        notional     = qty * entry_price
        if notional < min_notional:
            print(f"  ✗ validate_and_size: notional={notional:.0f} < min_notional={min_notional:.0f}")
            return False

        # --- PRE-FLIGHT OCO ELIGIBILITY CHECK ---
        # Invariant: ENTRY_ALLOWED = OCO_CAN_BE_PLACED_AFTER_ENTRY
        tp_cand = cand.get("tp_price") or cand.get("tp1")
        sl_cand = cand.get("sl_price") or cand.get("sl")

        if tp_cand is not None and sl_cand is not None:
            buf_pct = None
            if hasattr(self, "_sl_buffer_pct_fn") and callable(self._sl_buffer_pct_fn):
                try:
                    buf_pct = self._sl_buffer_pct_fn(entry_price)
                except Exception:
                    buf_pct = None

            fee_rate, fee_from_base = self.get_fee_config(cand["symbol"])

            is_eligible, reason, details = validate_protective_oco_eligibility(
                entry_qty=qty,
                entry_price=entry_price,
                tp_price=float(tp_cand),
                sl_price=float(sl_cand),
                sym_info=sym,
                fee_rate=fee_rate,
                fee_deducted_from_base=fee_from_base,
                sl_buffer_pct=buf_pct,
            )

            if not is_eligible:
                sym_str = cand.get("symbol", "?")
                print(f"  ✗ validate_and_size: {reason} (symbol={sym_str})")
                _send_toko_telegram(
                    f"⛔ [TOKO] Pre-flight OCO Ineligible: {sym_str}\n"
                    f"Reason: {reason}\n"
                    f"BUY aborted before submission."
                )
                return False

        # Success — mutate cand
        cand.setdefault("sizing", {})
        cand["sizing"]["qty"]           = qty
        cand["sizing"]["slot_size_idr"] = slot_size_idr
        cand["constraints"]             = sym.constraints
        return True

    # ------------------------------------------------------------------
    # build_entry_payload
    # ------------------------------------------------------------------

    def build_entry_payload(self, cand: dict) -> dict:
        """
        Build the POST /open/v1/orders payload for a LIMIT BUY entry.

        Returns numeric types (not strings) — _signed_post handles urlencode.
        """
        tick  = cand["constraints"]["tick_size"]
        step  = cand["constraints"]["step_size"]

        return {
            "symbol":      cand["symbol"],
            "side":        0,            # 0 = BUY
            "type":        1,            # 1 = LIMIT
            "timeInForce": 1,            # 1 = GTC
            "quantity":    self.client.round_step(cand["sizing"]["qty"], step),
            "price":       self.client.round_tick(cand["entry_price"], tick),
            "timestamp":   int(time.time() * 1000),
        }

    # ------------------------------------------------------------------
    # execute_entry
    # ------------------------------------------------------------------

    def execute_entry(self, cand: dict, slot_size_idr: float) -> dict | None:
        """
        Full entry flow: validate → confirm → post → persist → notify.

        Returns the raw exchange response dict on success, None on abort/failure.

        slot_size_idr is used to compute available_idr = slot_size_idr * MAX_SLOTS.
        """
        # 0. Active position guard (one active lifecycle per symbol)
        sym = cand["symbol"]
        if self.has_active_position(sym):
            print(f"  ✗ execute_entry: active position already exists for {sym} (duplicate entry rejected)")
            return None

        # 1. Validate and size
        available_idr = slot_size_idr * self.MAX_SLOTS
        if not self.validate_and_size(cand, available_idr):
            return None

        # 2. Build payload
        payload = self.build_entry_payload(cand)
        sym      = cand["symbol"]
        qty      = payload["quantity"]
        price    = payload["price"]

        # 3. Supervised gate
        if self.supervised:
            if not _confirm(f"Place ENTRY BUY {sym}  qty={qty}  @ {price:,.0f} IDR?"):
                print("  ✗ Entry aborted by operator.")
                return None

        # 4. Dry-run bypass
        if self.dry_run:
            fake_order_id = f"DRY_{int(time.time())}"
            print(f"[DRY RUN] execute_entry: BUY {sym} qty={qty} @ {price}")
            return {
                "data": {"orderId": fake_order_id, "status": 0},
                "_dry_run": True,
            }

        # 5. Exchange call
        try:
            resp = self.client._signed_post("/open/v1/orders", payload)
        except TokocryptoError as e:
            raise RuntimeError(f"execute_entry: exchange call failed: {e}") from e

        # 6. Persist to Supabase
        order_data  = resp.get("data") or resp
        entry_oid   = str(order_data.get("orderId", ""))
        now_iso     = datetime.now(timezone.utc).isoformat()

        upsert_tokocrypto({
            "symbol":             sym,
            "entry_order_id":     entry_oid,
            "entry_price":        float(cand["entry_price"]),
            "tp_price":           float(cand.get("tp_price") or cand.get("tp1") or 0),
            "sl_price":           float(cand.get("sl_price") or cand.get("sl") or 0),
            "entry_qty":          float(qty),
            "entry_status":       "NEW",
            "exit_status":        "OPEN",
            "entry_notional_idr": float(qty) * float(cand["entry_price"]),
            # Provenance — always write actual runtime values, never rely on column defaults
            "supervised":         self.supervised,
            "trading_phase":      self._trading_phase,
            # Strategy metadata
            "planned_rr":         cand.get("rr"),
            "risk_pct":           cand.get("risk_pct"),
            "slot_size_idr":      slot_size_idr,
            "created_at":         now_iso,
            "updated_at":         now_iso,
        })

        # 7. Telegram
        _send_toko_telegram(
            f"📋 Entry placed: {sym}  qty={qty}  @ {price:,.0f} IDR  "
            f"orderId={entry_oid}"
        )

        return resp

    # ------------------------------------------------------------------
    # place_oco
    # ------------------------------------------------------------------

    def place_oco(self, trade: dict) -> dict | None:
        """
        Place an OCO (One-Cancels-Other) SELL order for an open position.

        Uses old Tokocrypto OCO format (price / stopPrice / stopLimitPrice).
        No aboveType/belowType — that is the Binance v3 format, not Tokocrypto.

        Returns the exchange response dict on success, None on abort/constraint fail.
        """
        sym   = trade["symbol"]
        tp    = float(trade["tp_price"])
        sl    = float(trade["sl_price"])
        qty   = float(trade.get("entry_qty", 0))

        # Fetch constraints if not already in trade
        try:
            sym_info = self.client.get_symbol(sym)
        except TokocryptoError as e:
            print(f"  ✗ place_oco: get_symbol failed: {e}")
            return None

        tick = sym_info.tick_size
        step = sym_info.step_size

        # Fetch actual available balance — entry_qty may exceed free balance
        # because trading fee was deducted from the bought amount.
        try:
            base_asset = sym.replace("_IDR", "")
            bal = self.client.get_balance(base_asset)
            available = bal.free if bal else 0.0
            if available > 0 and available < qty:
                print(f"  ℹ place_oco: adjusting qty from {qty} to {available} "
                      f"(fee-adjusted balance)")
                qty = available
        except TokocryptoError:
            pass  # fall through with original qty

        # OCO constraint check — tp must be above current price, sl below
        try:
            ref = self.client.get_ticker(sym)
        except TokocryptoError as e:
            print(f"  ✗ place_oco: get_ticker failed: {e}")
            return None

        buf_pct = 0.0015
        if hasattr(self, "_sl_buffer_pct_fn") and callable(self._sl_buffer_pct_fn):
            try:
                buf_pct = float(self._sl_buffer_pct_fn(float(trade.get("entry_price") or sl)))
            except Exception:
                buf_pct = 0.0015

        tp_rounded   = self.client.round_tick(tp, tick)
        sl_stop      = self.client.round_tick(sl, tick)
        sl_limit     = self.client.round_tick(sl * (1.0 - buf_pct), tick)

        # Guard: sl_limit must be strictly below sl_stop
        if sl_limit >= sl_stop:
            sl_limit = self.client.round_tick(sl_stop - tick, tick)

        if not (tp_rounded > ref > sl_stop):
            print(
                f"  ⚠ OCO constraint violated: "
                f"tp={tp_rounded}  ref={ref}  sl_stop={sl_stop}"
            )
            return None

        qty_rounded = self.client.round_step(qty, step)

        # Runtime safety invariants: check usable qty, min_qty, and notionals
        min_notional = sym_info.min_notional if (sym_info and sym_info.min_notional > 0) else self.MIN_NOTIONAL_IDR
        tp_notional = qty_rounded * tp_rounded
        sl_notional = qty_rounded * sl_limit

        if qty_rounded <= 0:
            print(f"  ✗ place_oco: qty rounded to 0 (raw_qty={qty}, step={step})")
            return None

        if sym_info.min_qty > 0 and qty_rounded < sym_info.min_qty:
            print(f"  ✗ place_oco: qty={qty_rounded} < min_qty={sym_info.min_qty}")
            return None

        if min_notional > 0 and tp_notional < min_notional:
            print(f"  ✗ place_oco: TP notional Rp {tp_notional:,.2f} < min_notional Rp {min_notional:,.2f}")
            return None

        if min_notional > 0 and sl_notional < min_notional:
            print(f"  ✗ place_oco: SL limit notional Rp {sl_notional:,.2f} < min_notional Rp {min_notional:,.2f}")
            return None

        # Supervised gate
        if self.supervised:
            if not _confirm(
                f"Place OCO SELL {sym}  qty={qty_rounded}  "
                f"tp={tp_rounded:,.0f}  sl_stop={sl_stop:,.0f}?"
            ):
                print("  ✗ OCO placement aborted by operator.")
                return None

        # Dry-run bypass
        if self.dry_run:
            print(f"[DRY RUN] place_oco: SELL {sym} qty={qty_rounded} tp={tp_rounded} sl={sl_stop}")
            return {"bOrderListId": "DRY_OCO", "orders": []}

        # Exchange call (old Tokocrypto OCO format)
        payload = {
            "symbol":               sym,
            "side":                 1,           # 1 = SELL
            "quantity":             qty_rounded,
            "price":                tp_rounded,
            "stopPrice":            sl_stop,
            "stopLimitPrice":       sl_limit,
            "stopLimitTimeInForce": "GTC",
            "timestamp":            int(time.time() * 1000),
        }

        try:
            resp = self.client._signed_post("/open/v1/orders/oco", payload)
        except TokocryptoError as e:
            raise RuntimeError(f"place_oco: exchange call failed: {e}") from e

        # Parse order IDs from response
        resp_data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        orders    = resp_data.get("orders") or resp.get("orders") or []

        # Support bOrderListId/orderListId at root, resp_data, or inside child orders
        raw_list_id = (
            resp_data.get("bOrderListId")
            or resp_data.get("orderListId")
            or resp.get("bOrderListId")
            or resp.get("orderListId")
        )
        if not raw_list_id and isinstance(orders, list):
            for o in orders:
                if isinstance(o, dict):
                    cid = o.get("bOrderListId") or o.get("orderListId")
                    if cid:
                        raw_list_id = cid
                        break
        b_order_list_id = str(raw_list_id or "")

        tp_order_id = ""
        sl_order_id = ""
        if len(orders) >= 2:
            # Convention: first order = limit (TP), second = stop-limit (SL)
            tp_order_id = str(orders[0].get("orderId", ""))
            sl_order_id = str(orders[1].get("orderId", ""))

        now_iso = datetime.now(timezone.utc).isoformat()
        update_tokocrypto_by_order_id(
            str(trade.get("entry_order_id", "")),
            {
                "b_order_list_id": b_order_list_id,
                "tp_order_id":     tp_order_id,
                "sl_order_id":     sl_order_id,
                "oco_state":       "EXECUTING",
                "updated_at":      now_iso,
            },
        )

        _send_toko_telegram(
            f"🛡 OCO placed: {sym}\n"
            f"Fill price:  Rp {float(trade.get('entry_fill_price', 0)):,.2f}\n"
            f"TP:          Rp {tp_rounded:,.2f}  (orderId={tp_order_id})\n"
            f"SL trigger:  Rp {sl_stop:,.2f}  limit={sl_limit:,.2f}  (orderId={sl_order_id})\n"
            f"OCO listId:  {b_order_list_id}\n"
            f"Qty:         {trade.get('entry_qty', '?')} {sym.replace('_IDR', '')}"
        )

        return resp

    # ------------------------------------------------------------------
    # query_oco_state
    # ------------------------------------------------------------------

    def query_oco_state(self, trade: dict) -> dict:
        """
        Query both OCO legs and derive the current state.

        Returns a dict:
            {
                "state":            <STATE_STR>,
                "exit_price":       float | None,
                "slippage_flagged": bool,
                "raw_tp":           dict,
                "raw_sl":           dict,
            }

        State names (exact):
            EXECUTING               — normal, waiting
            TP_HIT                  — TP leg filled
            SL_HIT                  — SL leg filled
            STUCK_COUNTERPART       — one leg filled but counterpart stuck
            CRITICAL_ANOMALY        — both legs filled (impossible normally)
            BOTH_CANCELED_ANOMALY   — both legs canceled (manual intervention?)
            TP_EXPIRED_PENDING      — TP leg expired, awaiting confirmation
            SL_EXPIRED_PENDING      — SL leg expired, awaiting confirmation
            RECONCILIATION_REQUIRED — query failed or unrecognized combination
        """
        sym       = trade["symbol"]
        tp_oid    = str(trade.get("tp_order_id", ""))
        sl_oid    = str(trade.get("sl_order_id", ""))

        _empty    = {"state": "RECONCILIATION_REQUIRED",
                     "exit_price": None, "slippage_flagged": False,
                     "raw_tp": {}, "raw_sl": {}}

        # Query both legs — a single failure → RECONCILIATION_REQUIRED
        try:
            raw_tp = self.client.get_order_detail(sym, tp_oid)
            raw_sl = self.client.get_order_detail(sym, sl_oid)
        except TokocryptoError:
            return {**_empty, "state": "RECONCILIATION_REQUIRED"}

        tp_status = int(raw_tp.get("status", -99))
        sl_status = int(raw_sl.get("status", -99))

        def _exit_price_from(raw: dict) -> float:
            """Always use executedPrice, never price (limit) or ticker."""
            return float(raw.get("executedPrice", 0) or 0)

        def _slippage_flag(exit_price: float, ref_price: float, is_tp: bool) -> bool:
            if ref_price <= 0:
                return False
            slip = abs(exit_price - ref_price) / ref_price
            threshold = 0.001 if is_tp else 0.003   # 0.1% TP, 0.3% SL
            return slip > threshold

        # -----------------------------------------------------------------
        # State machine — evaluate in priority order
        # -----------------------------------------------------------------

        # System processing — transient, treat as EXECUTING
        if tp_status == -2 or sl_status == -2:
            return {"state": "EXECUTING", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        # Partial fill — wait
        if tp_status == 1 or sl_status == 1:
            return {"state": "EXECUTING", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        # Both NEW — waiting for market to move
        if tp_status == 0 and sl_status == 0:
            return {"state": "EXECUTING", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        # Clean exits
        if tp_status == 2 and sl_status == 3:
            exit_price = _exit_price_from(raw_tp)
            return {
                "state":            "TP_HIT",
                "exit_price":       exit_price,
                "slippage_flagged": _slippage_flag(exit_price, float(trade.get("tp_price", 0)), is_tp=True),
                "raw_tp":           raw_tp,
                "raw_sl":           raw_sl,
            }

        if tp_status == 3 and sl_status == 2:
            exit_price = _exit_price_from(raw_sl)
            return {
                "state":            "SL_HIT",
                "exit_price":       exit_price,
                "slippage_flagged": _slippage_flag(exit_price, float(trade.get("sl_price", 0)), is_tp=False),
                "raw_tp":           raw_tp,
                "raw_sl":           raw_sl,
            }

        # Tokocrypto can mark the OCO sibling as EXPIRED (6), rather than
        # CANCELED (3), after the other leg fills.  Both combinations are a
        # confirmed completed exit, not a pending expired order.
        if tp_status == 2 and sl_status == 6:
            exit_price = _exit_price_from(raw_tp)
            return {
                "state":            "TP_HIT",
                "exit_price":       exit_price,
                "slippage_flagged": _slippage_flag(exit_price, float(trade.get("tp_price", 0)), is_tp=True),
                "raw_tp":           raw_tp,
                "raw_sl":           raw_sl,
            }

        if tp_status == 6 and sl_status == 2:
            exit_price = _exit_price_from(raw_sl)
            return {
                "state":            "SL_HIT",
                "exit_price":       exit_price,
                "slippage_flagged": _slippage_flag(exit_price, float(trade.get("sl_price", 0)), is_tp=False),
                "raw_tp":           raw_tp,
                "raw_sl":           raw_sl,
            }

        # One filled, counterpart stuck — wait 1s and re-query
        if (tp_status == 2 and sl_status == 0) or (tp_status == 0 and sl_status == 2):
            time.sleep(1)
            try:
                raw_tp2 = self.client.get_order_detail(sym, tp_oid)
                raw_sl2 = self.client.get_order_detail(sym, sl_oid)
            except TokocryptoError:
                return {**_empty, "state": "RECONCILIATION_REQUIRED"}
            tp2 = int(raw_tp2.get("status", -99))
            sl2 = int(raw_sl2.get("status", -99))
            if (tp2 == 2 and sl2 == 0) or (tp2 == 0 and sl2 == 2):
                return {"state": "STUCK_COUNTERPART", "exit_price": None,
                        "slippage_flagged": False, "raw_tp": raw_tp2, "raw_sl": raw_sl2}
            # Clean exits resolved during the 1-second window — evaluate before anomaly checks
            if tp2 == 2 and sl2 == 3:
                exit_price = _exit_price_from(raw_tp2)
                return {
                    "state":            "TP_HIT",
                    "exit_price":       exit_price,
                    "slippage_flagged": _slippage_flag(exit_price, float(trade.get("tp_price", 0)), is_tp=True),
                    "raw_tp":           raw_tp2,
                    "raw_sl":           raw_sl2,
                }
            if tp2 == 3 and sl2 == 2:
                exit_price = _exit_price_from(raw_sl2)
                return {
                    "state":            "SL_HIT",
                    "exit_price":       exit_price,
                    "slippage_flagged": _slippage_flag(exit_price, float(trade.get("sl_price", 0)), is_tp=False),
                    "raw_tp":           raw_tp2,
                    "raw_sl":           raw_sl2,
                }
            # Re-evaluate with refreshed status for remaining anomaly checks
            raw_tp, raw_sl   = raw_tp2, raw_sl2
            tp_status, sl_status = tp2, sl2

        # Both filled — critical anomaly
        if tp_status == 2 and sl_status == 2:
            return {"state": "CRITICAL_ANOMALY", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        # Both canceled
        if tp_status == 3 and sl_status == 3:
            return {"state": "BOTH_CANCELED_ANOMALY", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        # Expired legs
        if tp_status == 6:
            return {"state": "TP_EXPIRED_PENDING", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        if sl_status == 6:
            return {"state": "SL_EXPIRED_PENDING", "exit_price": None,
                    "slippage_flagged": False, "raw_tp": raw_tp, "raw_sl": raw_sl}

        # Catch-all — unknown combination
        return {**_empty, "state": "RECONCILIATION_REQUIRED",
                "raw_tp": raw_tp, "raw_sl": raw_sl}

    # ------------------------------------------------------------------
    # cancel_order
    # ------------------------------------------------------------------

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel a single order by orderId.

        Returns True if confirmed canceled, False otherwise (including operator abort).
        """
        # Supervised gate
        if self.supervised:
            if not _confirm(f"Cancel order {order_id} on {symbol}?"):
                print("  ✗ Cancel aborted by operator.")
                return False

        # Dry-run bypass
        if self.dry_run:
            print(f"[DRY RUN] cancel_order: {symbol} orderId={order_id}")
            return True

        # Exchange call
        try:
            resp = self.client._signed_post(
                "/open/v1/orders/cancel",
                {
                    "symbol":    self.client.normalize_symbol(symbol),
                    "orderId":   str(order_id),
                    "timestamp": int(time.time() * 1000),
                },
            )
        except TokocryptoError as e:
            print(f"  ✗ cancel_order: exchange call failed: {e}")
            return False

        # Confirm cancellation
        data   = resp.get("data") or {}
        status = data.get("status") if isinstance(data, dict) else resp.get("status")
        # Tokocrypto returns status as int (3) or string "CANCELED"
        if status in (3, "CANCELED", "3"):
            return True

        print(f"  ⚠ cancel_order: unexpected status={status!r} for orderId={order_id}")
        return False

    # ------------------------------------------------------------------
    # execute_market_sell
    # ------------------------------------------------------------------

    def execute_market_sell(self, symbol: str, quantity: float) -> dict | None:
        """
        Execute emergency MARKET SELL order for Tokocrypto spot.

        Parameters
        ----------
        symbol   : str (e.g. "POL_IDR")
        quantity : float (unrounded base asset amount)

        Returns
        -------
        dict with order fill details or None on failure/abort.
        """
        try:
            sym_info = self.client.get_symbol(symbol)
        except TokocryptoError as e:
            print(f"  ✗ execute_market_sell: get_symbol failed: {e}")
            return None

        qty_rounded = self.client.round_step(quantity, sym_info.step_size)
        if qty_rounded <= 0 or qty_rounded < sym_info.min_qty:
            print(f"  ✗ execute_market_sell: qty {qty_rounded} < min_qty {sym_info.min_qty}")
            return None

        if self.supervised:
            if not _confirm(f"Emergency MARKET SELL {symbol} qty={qty_rounded}?"):
                print("  ✗ Emergency MARKET SELL aborted by operator.")
                return None

        if self.dry_run:
            print(f"[DRY RUN] execute_market_sell: SELL {symbol} qty={qty_rounded} MARKET")
            ticker_p = 0.0
            try:
                ticker_p = float(self.client.get_ticker(symbol))
            except Exception:
                pass
            return {
                "orderId": "DRY_MARKET_SELL",
                "status": 2,
                "executedQty": str(qty_rounded),
                "executedPrice": str(ticker_p),
                "time": int(time.time() * 1000),
            }

        payload = {
            "symbol":    self.client.normalize_symbol(symbol),
            "side":      1,            # 1 = SELL
            "type":      2,            # 2 = MARKET
            "quantity":  qty_rounded,
            "timestamp": int(time.time() * 1000),
        }

        try:
            resp = self.client._signed_post("/open/v1/orders", payload)
        except TokocryptoError as e:
            print(f"  ✗ execute_market_sell: exchange call failed: {e}")
            return None

        resp_data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        oid = str(resp_data.get("orderId", ""))
        status = int(resp_data.get("status", -99))
        exec_price = float(resp_data.get("executedPrice", 0) or 0)

        # If not fully detailed in post response, query order detail
        if oid and (status != 2 or exec_price <= 0):
            try:
                detail = self.client.get_order_detail(symbol, oid)
                if isinstance(detail, dict):
                    resp_data = detail
            except Exception as exc:
                print(f"  ⚠ execute_market_sell: query detail failed: {exc}")

        return resp_data

    # ------------------------------------------------------------------
    # recover_stuck_sl
    # ------------------------------------------------------------------

    def recover_stuck_sl(self, trade: dict) -> dict | None:
        """
        Safely recover a stuck SL limit order:
        1. Query latest status of SL order from exchange.
        2. If already FILLED, return clean SL_HIT dict without placing new orders.
        3. If active, CANCEL the order. Abort if cancel fails to prevent duplicate sell.
        4. Re-query order detail to confirm cancellation and get exact executedQty.
        5. Verify available wallet balance.
        6. Execute MARKET SELL for remaining unfilled quantity.
        7. Compute blended exit price if partial fill occurred.
        """
        sym    = trade.get("symbol", "")
        sl_oid = str(trade.get("sl_order_id", ""))
        entry_qty = float(trade.get("entry_qty") or 0)

        if not sl_oid:
            print(f"  ✗ recover_stuck_sl({sym}): missing sl_order_id")
            return None

        # Step 1: Query order detail
        try:
            sl_detail = self.client.get_order_detail(sym, sl_oid)
        except TokocryptoError as e:
            print(f"  ✗ recover_stuck_sl({sym}): get_order_detail failed: {e}")
            return None

        sl_status = int(sl_detail.get("status", -99))
        sl_exec_qty = float(sl_detail.get("executedQty", 0) or 0)
        sl_exec_price = float(sl_detail.get("executedPrice", 0) or 0)
        sl_orig_qty = float(sl_detail.get("origQty", 0) or entry_qty)

        # Step 2: If already filled
        if sl_status == 2 or (sl_orig_qty > 0 and sl_exec_qty >= sl_orig_qty):
            return {
                "state": "SL_HIT",
                "exit_price": sl_exec_price,
                "exit_qty": sl_exec_qty,
                "exit_reason": "OCO_TRIGGERED",
                "raw_sl": sl_detail,
                "slippage_flagged": False,
            }

        # Step 3: Atomic Cancel
        canceled = self.cancel_order(sym, sl_oid)
        if not canceled:
            print(f"  ✗ recover_stuck_sl({sym}): cancel_order({sl_oid}) failed. Aborting recovery to prevent duplicate sell.")
            return None

        # Step 4: Re-query post-cancel
        try:
            sl_detail_post = self.client.get_order_detail(sym, sl_oid)
            sl_exec_qty = float(sl_detail_post.get("executedQty", 0) or 0)
            sl_exec_price = float(sl_detail_post.get("executedPrice", 0) or 0)
            sl_orig_qty = float(sl_detail_post.get("origQty", 0) or sl_orig_qty)
        except Exception:
            sl_detail_post = sl_detail

        remaining_qty = max(0.0, sl_orig_qty - sl_exec_qty)
        if remaining_qty <= 0:
            # Filled during cancel window
            return {
                "state": "SL_HIT",
                "exit_price": sl_exec_price,
                "exit_qty": sl_exec_qty,
                "exit_reason": "OCO_TRIGGERED",
                "raw_sl": sl_detail_post,
                "slippage_flagged": False,
            }

        if "_" in sym:
            base_asset = sym.split("_")[0]
        elif sym.endswith("IDR"):
            base_asset = sym[:-3]
        elif sym.endswith("USDT"):
            base_asset = sym[:-4]
        else:
            base_asset = sym.replace("_IDR", "").replace("IDR", "")
        try:
            bal = self.client.get_balance(base_asset)
            free_bal = float(bal.free) if bal else 0.0
        except Exception as e:
            print(f"  ✗ recover_stuck_sl({sym}): get_balance({base_asset}) failed: {e}")
            return None

        if free_bal <= 0:
            print(f"  ✗ recover_stuck_sl({sym}): zero available balance for {base_asset} (free={free_bal})")
            return None

        sell_qty = min(remaining_qty, free_bal)

        # Step 6: Execute MARKET SELL
        mkt_resp = self.execute_market_sell(sym, sell_qty)
        if not mkt_resp:
            print(f"  ✗ recover_stuck_sl({sym}): emergency market sell failed.")
            return None

        mkt_exec_price = float(mkt_resp.get("executedPrice", 0) or 0)
        mkt_exec_qty = float(mkt_resp.get("executedQty", 0) or sell_qty)

        # Step 7: Blended price calculation
        total_exit_qty = sl_exec_qty + mkt_exec_qty
        if total_exit_qty > 0:
            blended_price = (sl_exec_qty * sl_exec_price + mkt_exec_qty * mkt_exec_price) / total_exit_qty
        else:
            blended_price = mkt_exec_price

        ref_price = float(trade.get("sl_price") or 0)
        slip_flag = False
        if ref_price > 0:
            slip_pct = abs(blended_price - ref_price) / ref_price
            slip_flag = slip_pct > 0.003  # 0.3%

        return {
            "state": "SL_HIT",
            "exit_price": blended_price,
            "exit_qty": total_exit_qty,
            "exit_reason": "EMERGENCY_SL_MARKET",
            "raw_sl": mkt_resp,
            "slippage_flagged": slip_flag,
        }
