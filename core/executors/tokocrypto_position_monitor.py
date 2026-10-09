"""
tokocrypto_position_monitor.py — Position monitoring loop for Tokocrypto.

Runs one check cycle over all open Tokocrypto positions:
  - Polls entry order status; if FILLED, places OCO
  - Queries OCO state and resolves TP_HIT / SL_HIT via _resolve_exit
  - Flags anomaly states (CRITICAL_ANOMALY, BOTH_CANCELED_ANOMALY,
    RECONCILIATION_REQUIRED) with high-severity Telegram alerts
  - Never auto-resolves anomalies — human must intervene

PnL always computed from executedPrice in the order detail response.
Never from get_ticker().

Safety invariants:
  - Does NOT place or cancel orders autonomously — delegates to executor.
  - Does NOT auto-resolve CRITICAL_ANOMALY or BOTH_CANCELED_ANOMALY.
  - Does NOT import from binance.* — only TokocryptoClient + executor.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoError
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    claim_oco_submission,
    lifecycle_lock,
    release_entry_submission,
    release_oco_submission,
)
from services.supabase_client import (
    fetch_all_tokocrypto_strict,
    update_tokocrypto_by_order_id,
    TABLE_TOKOCRYPTO,
)
from core.paper_trade_executor import _send_toko_telegram


class TokocryptoPositionMonitor:
    """
    One-cycle position monitor for Tokocrypto real-money trades.

    Parameters
    ----------
    client   : TokocryptoClient instance (authenticated)
    executor : TokocryptoOrderExecutor instance (same client preferred)
    """

    def __init__(
        self,
        client: TokocryptoClient,
        executor: TokocryptoOrderExecutor,
    ) -> None:
        self.client = client
        self.executor = executor

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def check_positions(self, verbose: bool = False) -> None:
        """
        Run one monitoring cycle over all Tokocrypto trades.

        Fetches all rows from Toko_Crypto_Spot and processes each open trade.
        Prints a summary of the last 5 closed trades if no open positions exist.
        """
        try:
            trades = fetch_all_tokocrypto_strict()
        except Exception as exc:
            print(
                f"[Toko Monitor] Could not load lifecycle state: {exc}; no automated actions taken."
            )
            return
        open_trades = [
            t for t in trades if (t.get("exit_status") or "").upper() == "OPEN"
        ]

        if not open_trades:
            print("[Toko Monitor] No open positions.")
            closed = [
                t for t in trades if (t.get("exit_status") or "").upper() != "OPEN"
            ]
            if closed:
                print("[Toko Monitor] Last 5 closed trades:")
                for row in closed[-5:]:
                    sym = row.get("symbol", "?")
                    exit_s = row.get("exit_status", "?")
                    pnl = row.get("realized_pnl_idr")
                    pnl_s = f"Rp {pnl:+,.0f}" if pnl is not None else "?"
                    print(f"  {sym}  {exit_s}  {pnl_s}")
            return

        print(f"[Toko Monitor] Checking {len(open_trades)} open position(s)...")
        for trade in open_trades:
            try:
                self._check_one(trade, verbose)
            except Exception as exc:
                sym = trade.get("symbol", "?")
                print(f"  ✗ _check_one({sym}): unhandled error: {exc}")

    # ------------------------------------------------------------------
    # Internal: single trade check
    # ------------------------------------------------------------------

    def _check_one(self, trade: dict, verbose: bool) -> None:
        with lifecycle_lock(str(trade.get("symbol", "?"))):
            self._check_one_locked(trade, verbose)

    def _check_one_locked(self, trade: dict, verbose: bool) -> None:
        """Process one open trade through the full entry → OCO → exit flow."""
        sym = trade.get("symbol", "?")
        entry_oid = str(trade.get("entry_order_id", ""))
        entry_status = (trade.get("entry_status") or "").upper()
        b_order_list = str(trade.get("b_order_list_id") or "").strip()
        tp_oid = str(trade.get("tp_order_id") or "").strip()
        sl_oid = str(trade.get("sl_order_id") or "").strip()
        has_oco = bool(tp_oid and sl_oid)

        if verbose:
            print(
                f"  [{sym}] entry_status={entry_status}  OCO={b_order_list or ('legs:' + tp_oid)}"
            )

        # ----------------------------------------------------------------
        # Phase A: Entry order not yet confirmed filled
        # ----------------------------------------------------------------
        if entry_status in ("NEW", "PARTIALLY_FILLED", ""):
            try:
                detail = self.client.get_order_detail(sym, entry_oid)
            except TokocryptoError as e:
                err_str = str(e)
                if "-2013" in err_str or "order does not exist" in err_str.lower():
                    now_iso = datetime.now(timezone.utc).isoformat()
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "entry_status": "RECONCILIATION_REQUIRED",
                            "exit_status": "RECONCILIATION_REQUIRED",
                            "exit_reason": "ENTRY_ORDER_NOT_FOUND_ON_EXCHANGE",
                            "updated_at": now_iso,
                        },
                    )
                    print(
                        f"  ✗ [{sym}] entry order {entry_oid} not found on exchange (-2013) -> RECONCILIATION_REQUIRED"
                    )
                else:
                    print(f"  ✗ [{sym}] get_order_detail(entry) failed: {e}")
                return

            d_status = int(detail.get("status", -99))
            exec_qty = float(detail.get("executedQty") or 0)
            fill_price = float(detail.get("executedPrice") or detail.get("price") or 0)
            now_iso = datetime.now(timezone.utc).isoformat()

            # entry_fill_time is bigint (epoch ms) in Supabase — use exchange time
            fill_time_ms = int(detail.get("createTime") or detail.get("time") or 0)
            if fill_time_ms == 0:
                import time as _time

                fill_time_ms = int(_time.time() * 1000)

            if d_status == 2:  # FILLED
                fill_qty = (
                    exec_qty if exec_qty > 0 else float(detail.get("origQty") or 0)
                )

                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "entry_fill_price": fill_price,
                        "entry_fill_time": fill_time_ms,
                        "entry_qty": fill_qty,
                        "entry_status": "FILLED",
                        "oco_state": (
                            "OCO_NOT_ATTEMPTED"
                            if not has_oco
                            else trade.get("oco_state")
                        ),
                        "updated_at": now_iso,
                    },
                )

                # Refresh local trade dict for OCO placement below
                trade = {
                    **trade,
                    "entry_fill_price": fill_price,
                    "entry_qty": fill_qty,
                    "entry_status": "FILLED",
                    "oco_state": (
                        "OCO_NOT_ATTEMPTED" if not has_oco else trade.get("oco_state")
                    ),
                }

                _send_toko_telegram(
                    f"✅ Entry FILLED: {sym}  qty={fill_qty}  "
                    f"fill_price={fill_price:,.0f} IDR"
                )
                entry_status = "FILLED"

            elif d_status in (3, 5, 6) and exec_qty == 0:
                # Terminal unfilled: CANCELED (3), REJECTED (5), EXPIRED (6)
                status_names = {3: "CANCELED", 5: "REJECTED", 6: "EXPIRED"}
                terminal_name = status_names.get(d_status, "CANCELED")

                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "entry_status": terminal_name,
                        "exit_status": terminal_name,
                        "exit_reason": f"ENTRY_{terminal_name}",
                        "updated_at": now_iso,
                    },
                )
                release_entry_submission(sym)

                _send_toko_telegram(
                    f"ℹ️ Entry {terminal_name}: {sym}  orderId={entry_oid}  (0 fill, slot released)"
                )
                if verbose:
                    print(
                        f"  [{sym}] entry {terminal_name} (0 fill) — synchronized to Supabase."
                    )
                return

            elif d_status in (1, 3, 6) and exec_qty > 0:
                # Partially filled terminal or active partial fill:
                # Reconcile actual fills first — do NOT treat as zero-fill cancellation!
                is_terminal = d_status in (3, 6)
                new_entry_status = "PARTIALLY_FILLED" if not is_terminal else "FILLED"

                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "entry_fill_price": fill_price,
                        "entry_fill_time": fill_time_ms,
                        "entry_qty": exec_qty,
                        "entry_status": new_entry_status,
                        "oco_state": (
                            "OCO_NOT_ATTEMPTED"
                            if is_terminal and not has_oco
                            else trade.get("oco_state")
                        ),
                        "updated_at": now_iso,
                    },
                )

                trade = {
                    **trade,
                    "entry_fill_price": fill_price,
                    "entry_qty": exec_qty,
                    "entry_status": new_entry_status,
                    "oco_state": (
                        "OCO_NOT_ATTEMPTED"
                        if is_terminal and not has_oco
                        else trade.get("oco_state")
                    ),
                }

                term_label = " (CANCELED remainder)" if is_terminal else " (Working)"
                _send_toko_telegram(
                    f"⚠️ Entry PARTIAL FILL{term_label}: {sym}  qty={exec_qty}  "
                    f"fill_price={fill_price:,.0f} IDR"
                )
                if is_terminal:
                    # Entry order is terminal with partial fill; position is open and needs OCO sizing for exec_qty
                    entry_status = "FILLED"
                else:
                    if verbose:
                        print(
                            f"  [{sym}] entry partially filled (qty={exec_qty}), still working."
                        )
                    return
            else:
                if verbose:
                    print(f"  [{sym}] entry not yet filled (status={d_status})")
                return

        # ----------------------------------------------------------------
        # Phase B: Entry filled, OCO not yet placed
        # ----------------------------------------------------------------
        if entry_status == "FILLED" and not has_oco:
            # Retry guard — stop retrying after MAX_OCO_RETRIES to avoid
            # Telegram spam.  The counter persists in Supabase.
            MAX_OCO_RETRIES = 3
            attempts = int(trade.get("oco_placement_attempts") or 0)
            oco_state_cur = (trade.get("oco_state") or "").upper()

            if oco_state_cur != "OCO_NOT_ATTEMPTED" or b_order_list or not has_oco:
                try:
                    discovered, safe_to_start = self.executor.inspect_open_oco_legs(
                        sym, b_order_list
                    )
                except Exception as exc:
                    discovered = None
                    safe_to_start = False
                    print(
                        f"  [{sym}] OCO open-order reconciliation failed: {exc}",
                        flush=True,
                    )
                if discovered:
                    tp_oid, sl_oid = discovered
                    trade = {**trade, "tp_order_id": tp_oid, "sl_order_id": sl_oid}
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "tp_order_id": tp_oid,
                            "sl_order_id": sl_oid,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    has_oco = True
                elif (
                    not safe_to_start
                    or oco_state_cur != "OCO_NOT_ATTEMPTED"
                    or b_order_list
                ):
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "oco_state": "RECONCILIATION_REQUIRED",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    _send_toko_telegram(
                        f"🚨 OCO STATE UNKNOWN: {sym}\n"
                        "Stored state and exchange open orders do not prove a complete TP/SL pair; "
                        "automatic OCO placement stopped for manual reconciliation."
                    )
                    return

            if not has_oco and not claim_oco_submission(entry_oid):
                if verbose:
                    print(
                        f"  [{sym}] OCO submission is already claimed or unresolved; skipping."
                    )
                return

            if (
                not has_oco
                and oco_state_cur == "OCO_PLACEMENT_FAILED"
                and attempts >= MAX_OCO_RETRIES
            ):
                # Already exhausted retries — do NOT retry or re-alert.
                # One-time escalation was sent on the last attempt.
                if verbose:
                    print(
                        f"  [{sym}] OCO retries exhausted ({attempts}/{MAX_OCO_RETRIES}), "
                        f"awaiting manual intervention."
                    )
                return

            if not has_oco:
                try:
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "oco_state": "OCO_SUBMISSION_PENDING",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception:
                    release_oco_submission(entry_oid)
                    raise

                try:
                    oco_result = self.executor.place_oco(trade)
                except Exception as oco_exc:
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "oco_state": "RECONCILIATION_REQUIRED",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    _send_toko_telegram(
                        f"🚨 OCO SUBMISSION UNKNOWN: {sym}\n"
                        f"{str(oco_exc)[:160]}\n"
                        "No automatic retry; verify exchange orders manually."
                    )
                    print(
                        f"  [{sym}] 🚨 OCO outcome unknown; automatic retry blocked: {oco_exc}",
                        flush=True,
                    )
                else:
                    if oco_result is None:
                        release_oco_submission(entry_oid)
                        attempts += 1
                        update_tokocrypto_by_order_id(
                            entry_oid,
                            {
                                "oco_state": "OCO_PLACEMENT_FAILED",
                                "oco_placement_attempts": attempts,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        _send_toko_telegram(
                            f"⚠️ OCO NOT PLACED: {sym} (known pre-submit rejection/abort). "
                            "No exchange POST was made; manual review required before retry."
                        )
                return

        # ----------------------------------------------------------------
        # Phase C: OCO placed — query its state
        # ----------------------------------------------------------------
        if has_oco:
            state_dict = self.executor.query_oco_state(trade)
            oco_state = state_dict["state"]
            now_iso = datetime.now(timezone.utc).isoformat()

            # Backfill b_order_list_id if it was missing locally
            if not b_order_list:
                raw_tp = state_dict.get("raw_tp") or {}
                raw_sl = state_dict.get("raw_sl") or {}
                discovered = (
                    raw_tp.get("bOrderListId")
                    or raw_tp.get("orderListId")
                    or raw_sl.get("bOrderListId")
                    or raw_sl.get("orderListId")
                )
                if discovered:
                    b_order_list = str(discovered).strip()
                    trade["b_order_list_id"] = b_order_list
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "b_order_list_id": b_order_list,
                            "updated_at": now_iso,
                        },
                    )

            if verbose:
                print(f"  [{sym}] OCO state: {oco_state}")

            if oco_state == "EXECUTING":
                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "oco_state": "EXECUTING",
                        "updated_at": now_iso,
                    },
                )
                # ── Stuck-OCO shadow detection ────────────────────────────
                # If OCO shows EXECUTING but price has already breached SL,
                # the SL leg may be stuck (same 2ZUSDT pattern from Binance).
                # Cycle N:   set stuck_oco_suspected_at, alert, do NOT cancel.
                # Cycle N+1: if still stuck → escalate alert.
                try:
                    ticker = self.client.get_ticker(sym)
                    current = float(ticker) if ticker else None
                except Exception:
                    current = None

                if current is not None and trade.get("sl_price"):
                    sl_level = float(trade["sl_price"])
                    if current <= sl_level:
                        raw = dict(trade.get("raw_entry_order") or {})
                        detection = raw.get("stuck_oco_detection", {})
                        suspected_at = detection.get("suspected_at")

                        if not suspected_at:
                            # Cycle N — first detection
                            detection["suspected_at"] = now_iso
                            raw["stuck_oco_detection"] = detection
                            update_tokocrypto_by_order_id(
                                entry_oid,
                                {
                                    "raw_entry_order": raw,
                                    "updated_at": now_iso,
                                },
                            )
                            _send_toko_telegram(
                                f"⚠️ STUCK-OCO SUSPECTED: {sym}\n"
                                f"Price {current:,.2f} ≤ SL {sl_level:,.2f} "
                                f"but OCO still EXECUTING.\n"
                                f"First detected: {now_iso}\n"
                                f"Monitoring next cycle — no action yet."
                            )
                            print(
                                f"  [{sym}] ⚠ Stuck-OCO suspected: "
                                f"price {current:,.2f} ≤ SL {sl_level:,.2f}",
                                flush=True,
                            )
                        else:
                            # Cycle N+1 — confirmed stuck, escalate
                            _send_toko_telegram(
                                f"🚨 STUCK-OCO CONFIRMED: {sym}\n"
                                f"Price {current:,.2f} ≤ SL {sl_level:,.2f} "
                                f"for 2+ cycles, OCO still EXECUTING.\n"
                                f"First detected: {suspected_at}\n"
                                f"Manual intervention required — check OCO legs."
                            )
                            print(
                                f"  [{sym}] 🚨 Stuck-OCO confirmed — "
                                f"escalated alert sent.",
                                flush=True,
                            )

            elif oco_state in ("TP_HIT", "SL_HIT"):
                self._resolve_exit(trade, state_dict)

            elif oco_state == "STUCK_COUNTERPART":
                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "oco_state": "STUCK_COUNTERPART",
                        "updated_at": now_iso,
                    },
                )
                _send_toko_telegram(
                    f"⚠️ STUCK_COUNTERPART: {sym}  "
                    f"one OCO leg filled but counterpart unresolved — manual check needed"
                )

            elif oco_state in ("CRITICAL_ANOMALY", "BOTH_CANCELED_ANOMALY"):
                raw_meta = (
                    dict(trade.get("raw_entry_order") or {})
                    if isinstance(trade.get("raw_entry_order"), dict)
                    else {}
                )
                raw_meta["requires_manual_review"] = True
                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "oco_state": oco_state,
                        "raw_entry_order": raw_meta,
                        "updated_at": now_iso,
                    },
                )
                raw_tp_s = str(state_dict.get("raw_tp", {}).get("status", "?"))
                raw_sl_s = str(state_dict.get("raw_sl", {}).get("status", "?"))
                _send_toko_telegram(
                    f"🚨 {oco_state}: {sym}  "
                    f"TP_status={raw_tp_s}  SL_status={raw_sl_s}  "
                    f"detected_at={now_iso}  "
                    f"MANUAL INTERVENTION REQUIRED — do NOT auto-resolve"
                )

            elif oco_state in ("TP_EXPIRED_PENDING", "SL_EXPIRED_PENDING"):
                is_recovery_done = bool(
                    trade.get("recovery_attempted")
                    or (
                        isinstance(trade.get("raw_entry_order"), dict)
                        and trade.get("raw_entry_order", {}).get("recovery_attempted")
                    )
                )
                # Only set expired_leg_detected_at on first detection
                if not trade.get("expired_leg_detected_at"):
                    update_tokocrypto_by_order_id(
                        entry_oid,
                        {
                            "oco_state": oco_state,
                            "expired_leg_detected_at": now_iso,
                            "updated_at": now_iso,
                        },
                    )
                    _send_toko_telegram(
                        f"⏰ {oco_state}: {sym}  "
                        f"expired_leg_first_detected={now_iso}  "
                        f"awaiting fill or recovery on next cycle"
                    )
                elif is_recovery_done:
                    # Idempotency guard: Recovery was already attempted.
                    # Do NOT retry recovery repeatedly every cycle — await manual intervention.
                    if verbose:
                        print(
                            f"  [{sym}] Recovery already attempted previously, awaiting manual resolution."
                        )
                else:
                    # Second+ cycle: SL leg is stuck unfilled!
                    # Attempt safe recovery via executor once
                    recovery_dict = self.executor.recover_stuck_sl(trade)
                    if recovery_dict and recovery_dict.get("state") == "SL_HIT":
                        self._resolve_exit(trade, recovery_dict)
                        _send_toko_telegram(
                            f"🚨 STUCK-SL RECOVERED: {sym}\n"
                            f"Position closed via emergency exit.\n"
                            f"Exit price: Rp {float(recovery_dict.get('exit_price', 0)):,.2f}\n"
                            f"Reason: {recovery_dict.get('exit_reason')}"
                        )
                    else:
                        raw_meta = (
                            dict(trade.get("raw_entry_order") or {})
                            if isinstance(trade.get("raw_entry_order"), dict)
                            else {}
                        )
                        raw_meta["recovery_attempted"] = True
                        raw_meta["requires_manual_review"] = True
                        update_tokocrypto_by_order_id(
                            entry_oid,
                            {
                                "oco_state": "RECONCILIATION_REQUIRED",
                                "raw_entry_order": raw_meta,
                                "updated_at": now_iso,
                            },
                        )
                        _send_toko_telegram(
                            f"🚨 STUCK-SL RECOVERY FAILED: {sym}\n"
                            f"Order remains OPEN and UNRESOLVED.\n"
                            f"Manual action required immediately!"
                        )

            elif oco_state == "RECONCILIATION_REQUIRED":
                raw_meta = (
                    dict(trade.get("raw_entry_order") or {})
                    if isinstance(trade.get("raw_entry_order"), dict)
                    else {}
                )
                raw_meta["requires_manual_review"] = True
                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "oco_state": "RECONCILIATION_REQUIRED",
                        "raw_entry_order": raw_meta,
                        "updated_at": now_iso,
                    },
                )
                _send_toko_telegram(
                    f"⚠️ RECONCILIATION_REQUIRED: {sym}  "
                    f"OCO query failed or unrecognized state  "
                    f"flagged_at={now_iso}  do NOT auto-act"
                )

    # ------------------------------------------------------------------
    # Internal: resolve TP or SL exit
    # ------------------------------------------------------------------

    def _resolve_exit(self, trade: dict, state_dict: dict) -> None:
        """
        Persist exit outcome and send Telegram for a confirmed TP or SL hit.

        exit_price ALWAYS from state_dict["exit_price"], which was populated
        from executedPrice in the order detail — never from get_ticker().
        """
        is_tp = state_dict["state"] == "TP_HIT"
        exit_price = float(state_dict["exit_price"] or 0)
        entry_oid = str(trade.get("entry_order_id", ""))
        sym = trade.get("symbol", "?")
        now_iso = datetime.now(timezone.utc).isoformat()

        entry_fill_price = float(
            trade.get("entry_fill_price") or trade.get("entry_price") or 0
        )
        qty = float(trade.get("entry_qty") or 0)
        entry_notional_idr = float(
            trade.get("entry_notional_idr") or (entry_fill_price * qty)
        )

        realized_pnl_idr = (exit_price - entry_fill_price) * qty
        realized_pnl_pct = (
            (realized_pnl_idr / entry_notional_idr * 100) if entry_notional_idr else 0.0
        )

        ref_price = float(
            trade.get("tp_price") if is_tp else trade.get("sl_price") or 0
        )
        slippage_pct = (
            ((exit_price - ref_price) / ref_price * 100) if ref_price else 0.0
        )
        slip_flagged = state_dict.get("slippage_flagged", False)

        raw_exit_key = "raw_tp" if is_tp else "raw_sl"
        raw_exit_detail = state_dict.get(raw_exit_key, {})

        # exit_time is bigint (epoch ms) in Supabase — match entry_fill_time pattern
        exit_time_ms = int(
            raw_exit_detail.get("createTime") or raw_exit_detail.get("time") or 0
        )
        if exit_time_ms == 0:
            import time as _time

            exit_time_ms = int(_time.time() * 1000)

        time_to_res = None
        entry_fill_ms = trade.get("entry_fill_time")
        if entry_fill_ms:
            try:
                time_to_res = max(0, (exit_time_ms - int(entry_fill_ms)) // 1000)
            except (TypeError, ValueError):
                time_to_res = None

        exit_reason = state_dict.get("exit_reason") or "OCO_TRIGGERED"
        update_tokocrypto_by_order_id(
            entry_oid,
            {
                "exit_status": "TP_HIT" if is_tp else "SL_HIT",
                "exit_reason": exit_reason,
                "exit_price": exit_price,
                "exit_time": exit_time_ms,
                "time_to_resolution_sec": time_to_res,
                "realized_pnl_idr": round(realized_pnl_idr, 2),
                "realized_pnl_pct": round(realized_pnl_pct, 4),
                "exit_fill_slippage_pct": round(slippage_pct, 4),
                "exit_fill_slippage_flagged": slip_flagged,
                "raw_exit_detail": raw_exit_detail,
                "oco_state": "TP_HIT" if is_tp else "SL_HIT",
                "updated_at": now_iso,
            },
        )
        release_entry_submission(sym)

        if is_tp:
            label = "✅ TP"
        elif exit_reason == "EMERGENCY_SL_MARKET":
            label = "🚨 EMERGENCY SL"
        else:
            label = "🔴 SL"
        slip_label = "slippage flagged" if slip_flagged else "clean"

        _send_toko_telegram(
            f"{label} HIT: {sym}  "
            f"exit={exit_price:,.0f}  PnL: Rp {realized_pnl_idr:+,.0f}  "
            f"({slip_label})"
        )

        if slip_flagged:
            _send_toko_telegram(
                f"⚠️ Slippage flag: {sym}  "
                f"exit={exit_price:,.0f}  ref={ref_price:,.0f}  "
                f"slip={slippage_pct:.4f}%  "
                f"threshold={'0.1%' if is_tp else '0.3%'}"
            )
