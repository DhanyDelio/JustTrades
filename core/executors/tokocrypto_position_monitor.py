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
from core.clients.tokocrypto_order_executor import TokocryptoOrderExecutor
from services.supabase_client import (
    fetch_all_tokocrypto,
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
        self.client   = client
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
        trades      = fetch_all_tokocrypto()
        open_trades = [
            t for t in trades
            if (t.get("exit_status") or "").upper() == "OPEN"
        ]

        if not open_trades:
            print("[Toko Monitor] No open positions.")
            closed = [t for t in trades if (t.get("exit_status") or "").upper() != "OPEN"]
            if closed:
                print("[Toko Monitor] Last 5 closed trades:")
                for row in closed[-5:]:
                    sym    = row.get("symbol", "?")
                    exit_s = row.get("exit_status", "?")
                    pnl    = row.get("realized_pnl_idr")
                    pnl_s  = f"Rp {pnl:+,.0f}" if pnl is not None else "?"
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
        """Process one open trade through the full entry → OCO → exit flow."""
        sym           = trade.get("symbol", "?")
        entry_oid     = str(trade.get("entry_order_id", ""))
        entry_status  = (trade.get("entry_status") or "").upper()
        b_order_list  = trade.get("b_order_list_id")

        if verbose:
            print(f"  [{sym}] entry_status={entry_status}  OCO={b_order_list}")

        # ----------------------------------------------------------------
        # Phase A: Entry order not yet confirmed filled
        # ----------------------------------------------------------------
        if entry_status in ("NEW", ""):
            try:
                detail = self.client.get_order_detail(sym, entry_oid)
            except TokocryptoError as e:
                print(f"  ✗ [{sym}] get_order_detail(entry) failed: {e}")
                return

            d_status = int(detail.get("status", -99))
            if d_status == 2:   # FILLED
                fill_price = float(detail.get("executedPrice") or detail.get("price") or 0)
                fill_qty   = float(detail.get("executedQty") or detail.get("origQty") or 0)
                now_iso    = datetime.now(timezone.utc).isoformat()

                update_tokocrypto_by_order_id(entry_oid, {
                    "entry_fill_price":   fill_price,
                    "entry_fill_time":    now_iso,
                    "entry_qty":          fill_qty,
                    "entry_status":       "FILLED",
                    "updated_at":         now_iso,
                })

                # Refresh local trade dict for OCO placement below
                trade = {**trade,
                         "entry_fill_price": fill_price,
                         "entry_qty":        fill_qty,
                         "entry_status":     "FILLED"}

                _send_toko_telegram(
                    f"✅ Entry FILLED: {sym}  qty={fill_qty}  "
                    f"fill_price={fill_price:,.0f} IDR"
                )
                entry_status = "FILLED"
            else:
                if verbose:
                    print(f"  [{sym}] entry not yet filled (status={d_status})")
                return

        # ----------------------------------------------------------------
        # Phase B: Entry filled, OCO not yet placed
        # ----------------------------------------------------------------
        if entry_status == "FILLED" and not b_order_list:
            self.executor.place_oco(trade)
            return   # next cycle will query OCO state

        # ----------------------------------------------------------------
        # Phase C: OCO placed — query its state
        # ----------------------------------------------------------------
        if b_order_list:
            state_dict = self.executor.query_oco_state(trade)
            oco_state  = state_dict["state"]
            now_iso    = datetime.now(timezone.utc).isoformat()

            if verbose:
                print(f"  [{sym}] OCO state: {oco_state}")

            if oco_state == "EXECUTING":
                update_tokocrypto_by_order_id(entry_oid, {
                    "oco_state":  "EXECUTING",
                    "updated_at": now_iso,
                })
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
                            update_tokocrypto_by_order_id(entry_oid, {
                                "raw_entry_order": raw,
                                "updated_at": now_iso,
                            })
                            _send_toko_telegram(
                                f"⚠️ STUCK-OCO SUSPECTED: {sym}\n"
                                f"Price {current:,.2f} ≤ SL {sl_level:,.2f} "
                                f"but OCO still EXECUTING.\n"
                                f"First detected: {now_iso}\n"
                                f"Monitoring next cycle — no action yet."
                            )
                            print(f"  [{sym}] ⚠ Stuck-OCO suspected: "
                                  f"price {current:,.2f} ≤ SL {sl_level:,.2f}", flush=True)
                        else:
                            # Cycle N+1 — confirmed stuck, escalate
                            _send_toko_telegram(
                                f"🚨 STUCK-OCO CONFIRMED: {sym}\n"
                                f"Price {current:,.2f} ≤ SL {sl_level:,.2f} "
                                f"for 2+ cycles, OCO still EXECUTING.\n"
                                f"First detected: {suspected_at}\n"
                                f"Manual intervention required — check OCO legs."
                            )
                            print(f"  [{sym}] 🚨 Stuck-OCO confirmed — "
                                  f"escalated alert sent.", flush=True)

            elif oco_state in ("TP_HIT", "SL_HIT"):
                self._resolve_exit(trade, state_dict)

            elif oco_state == "STUCK_COUNTERPART":
                update_tokocrypto_by_order_id(entry_oid, {
                    "oco_state":  "STUCK_COUNTERPART",
                    "updated_at": now_iso,
                })
                _send_toko_telegram(
                    f"⚠️ STUCK_COUNTERPART: {sym}  "
                    f"one OCO leg filled but counterpart unresolved — manual check needed"
                )

            elif oco_state in ("CRITICAL_ANOMALY", "BOTH_CANCELED_ANOMALY"):
                update_tokocrypto_by_order_id(entry_oid, {
                    "oco_state":            oco_state,
                    "requires_manual_review": True,
                    "updated_at":           now_iso,
                })
                raw_tp_s = str(state_dict.get("raw_tp", {}).get("status", "?"))
                raw_sl_s = str(state_dict.get("raw_sl", {}).get("status", "?"))
                _send_toko_telegram(
                    f"🚨 {oco_state}: {sym}  "
                    f"TP_status={raw_tp_s}  SL_status={raw_sl_s}  "
                    f"detected_at={now_iso}  "
                    f"MANUAL INTERVENTION REQUIRED — do NOT auto-resolve"
                )

            elif oco_state in ("TP_EXPIRED_PENDING", "SL_EXPIRED_PENDING"):
                # Only set expired_leg_detected_at on first detection
                if not trade.get("expired_leg_detected_at"):
                    update_tokocrypto_by_order_id(entry_oid, {
                        "oco_state":               oco_state,
                        "expired_leg_detected_at": now_iso,
                        "updated_at":              now_iso,
                    })
                    _send_toko_telegram(
                        f"⏰ {oco_state}: {sym}  "
                        f"expired_leg_first_detected={now_iso}  "
                        f"awaiting manual confirmation"
                    )
                else:
                    update_tokocrypto_by_order_id(entry_oid, {
                        "oco_state":  oco_state,
                        "updated_at": now_iso,
                    })

            elif oco_state == "RECONCILIATION_REQUIRED":
                update_tokocrypto_by_order_id(entry_oid, {
                    "oco_state":            "RECONCILIATION_REQUIRED",
                    "requires_manual_review": True,
                    "updated_at":           now_iso,
                })
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
        is_tp        = state_dict["state"] == "TP_HIT"
        exit_price   = float(state_dict["exit_price"] or 0)
        entry_oid    = str(trade.get("entry_order_id", ""))
        sym          = trade.get("symbol", "?")
        now_iso      = datetime.now(timezone.utc).isoformat()

        entry_fill_price    = float(trade.get("entry_fill_price") or trade.get("entry_price") or 0)
        qty                 = float(trade.get("entry_qty") or 0)
        entry_notional_idr  = float(trade.get("entry_notional_idr") or (entry_fill_price * qty))

        realized_pnl_idr    = (exit_price - entry_fill_price) * qty
        realized_pnl_pct    = (realized_pnl_idr / entry_notional_idr * 100) if entry_notional_idr else 0.0

        ref_price      = float(trade.get("tp_price") if is_tp else trade.get("sl_price") or 0)
        slippage_pct   = ((exit_price - ref_price) / ref_price * 100) if ref_price else 0.0
        slip_flagged   = state_dict.get("slippage_flagged", False)

        raw_exit_key   = "raw_tp" if is_tp else "raw_sl"
        raw_exit_detail = state_dict.get(raw_exit_key, {})

        update_tokocrypto_by_order_id(entry_oid, {
            "exit_status":                "TP_HIT" if is_tp else "SL_HIT",
            "exit_reason":                "OCO_TRIGGERED",
            "exit_price":                 exit_price,
            "exit_time":                  now_iso,
            "realized_pnl_idr":           round(realized_pnl_idr, 2),
            "realized_pnl_pct":           round(realized_pnl_pct, 4),
            "exit_fill_slippage_pct":     round(slippage_pct, 4),
            "exit_fill_slippage_flagged": slip_flagged,
            "raw_exit_detail":            raw_exit_detail,
            "oco_state":                  "TP_HIT" if is_tp else "SL_HIT",
            "updated_at":                 now_iso,
        })

        label = "✅ TP" if is_tp else "🔴 SL"
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
