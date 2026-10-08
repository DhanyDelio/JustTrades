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
        if max_slots is not None:
            self.MAX_SLOTS  = max_slots

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

        tp_rounded   = self.client.round_tick(tp, tick)
        sl_stop      = self.client.round_tick(sl, tick)
        sl_limit     = self.client.round_tick(sl * 0.9985, tick)

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
