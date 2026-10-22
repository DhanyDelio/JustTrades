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
import math
import os
import time

from core.clients.tokocrypto_client import TokocryptoClient, TokocryptoError
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    claim_oco_submission,
    claim_recovery_submission,
    lifecycle_lock,
    release_entry_submission,
    release_oco_submission,
    release_recovery_submission,
)
from services.supabase_client import (
    claim_tokocrypto_recovery,
    fetch_all_tokocrypto_strict,
    transition_tokocrypto_state,
    update_tokocrypto_by_order_id,
    TABLE_TOKOCRYPTO,
)
from core.paper_trade_executor import _send_toko_telegram


_RECONCILIATION_WARN_COOLDOWN_SEC = 300.0  # 5 minutes
_last_reconciliation_warning_ts: dict[str, float] = {}


def _notify_transient_reconciliation_failure(sym: str, error_msg: str) -> None:
    """Send throttled Telegram notification on transient exchange reconciliation query failure."""
    now = time.time()
    last = _last_reconciliation_warning_ts.get(sym, 0.0)
    if now - last >= _RECONCILIATION_WARN_COOLDOWN_SEC:
        _last_reconciliation_warning_ts[sym] = now
        _send_toko_telegram(
            f"⚠️ OCO RECONCILIATION TRANSIENT ERROR: {sym}\n"
            f"Failed to query open orders from exchange: {error_msg[:120]}\n"
            "Skipping OCO placement this cycle. Will retry exchange inspection next cycle."
        )


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
            self._check_and_notify_dust(verbose=verbose)
            return

        print(f"[Toko Monitor] Checking {len(open_trades)} open position(s)...")
        for trade in open_trades:
            try:
                self._check_one(trade, verbose)
            except Exception as exc:
                sym = trade.get("symbol", "?")
                print(f"  ✗ _check_one({sym}): unhandled error: {exc}")

        self._check_and_notify_dust(verbose=verbose)

    def _check_and_notify_dust(self, verbose: bool = False) -> None:
        """
        Evaluate wallet dust balances and send throttled Telegram notification.
        Fail-safe: Never raises or interrupts position monitoring.
        """
        try:
            from core.utils.tokocrypto_dust import (
                classify_and_aggregate_dust,
                notify_dust_summary_if_needed,
            )

            # 1. Fetch balances
            try:
                balances = self.client.get_balances()
            except Exception as exc:
                if verbose:
                    print(f"  [Toko Dust] Could not fetch balances: {exc}", flush=True)
                return

            # 2. Database state (fail closed to None on exception)
            try:
                db_trades = fetch_all_tokocrypto_strict()
            except Exception:
                db_trades = None

            # 3. Exchange open orders (fail closed to None on exception)
            try:
                exchange_orders = self.client.get_open_orders()
            except Exception:
                exchange_orders = None

            summary = classify_and_aggregate_dust(
                balances=balances,
                client=self.client,
                open_trades=db_trades,
                exchange_open_orders=exchange_orders,
            )

            sent = notify_dust_summary_if_needed(
                summary=summary,
                sender_fn=_send_toko_telegram,
            )
            if sent and verbose:
                print(
                    f"  [Toko Dust] Sent dust notification to Telegram "
                    f"(Eligible: Rp {summary.total_eligible_dust_idr:,.0f})",
                    flush=True,
                )
        except Exception as exc:
            if verbose:
                print(f"  [Toko Dust] Non-fatal check error: {exc}", flush=True)

    def _evaluate_and_cancel_stale_entry(
        self, trade: dict, detail: dict, sym: str, entry_oid: str, verbose: bool
    ) -> dict | None:
        """
        Evaluate if an unfilled limit buy (status=0) is stale.
        If stale and TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED=1, attempts cancel and re-queries detail.
        Returns the re-queried detail dict if cancel was executed and post-cancel verified; None otherwise.
        Fail-closed: On any parsing, network, or verification error, returns None without mutating state.
        """
        auto_cancel = os.environ.get("TOKO_STALE_ENTRY_AUTO_CANCEL_ENABLED", "0") == "1"
        try:
            min_days = float(os.environ.get("TOKO_STALE_ENTRY_MIN_DAYS", "3.0"))
            if math.isnan(min_days) or math.isinf(min_days) or min_days <= 0:
                min_days = 3.0
        except (ValueError, TypeError):
            min_days = 3.0
        try:
            min_dist_pct = float(os.environ.get("TOKO_STALE_ENTRY_MIN_DISTANCE_PCT", "15.0"))
            if math.isnan(min_dist_pct) or math.isinf(min_dist_pct):
                min_dist_pct = 15.0
        except (ValueError, TypeError):
            min_dist_pct = 15.0

        # Parse order age from open_time or created_at
        open_time_str = trade.get("open_time") or trade.get("created_at")
        if not open_time_str:
            return None

        try:
            open_dt = datetime.fromisoformat(str(open_time_str).replace("Z", "+00:00"))
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - open_dt).total_seconds() / 86400.0
            if age_days < 0 or math.isnan(age_days) or math.isinf(age_days):
                return None
        except Exception:
            return None

        # Parse entry price
        try:
            entry_price = float(trade.get("entry_price") or detail.get("price") or 0.0)
            if entry_price <= 0 or math.isnan(entry_price) or math.isinf(entry_price):
                return None
        except (ValueError, TypeError):
            return None

        # Fetch current ticker
        try:
            cur_price = float(self.client.get_ticker(sym))
            if cur_price <= 0 or math.isnan(cur_price) or math.isinf(cur_price):
                return None
            dist_pct = ((cur_price - entry_price) / entry_price) * 100.0
            if math.isnan(dist_pct) or math.isinf(dist_pct):
                return None
        except Exception as exc:
            if verbose:
                print(f"  [{sym}] stale check: get_ticker failed: {exc}", flush=True)
            return None

        # Evaluate conjunctive criteria
        if age_days < min_days or dist_pct < min_dist_pct:
            return None

        # Stale criteria met!
        if not auto_cancel:
            if verbose:
                print(
                    f"  [{sym}] ℹ [STALE SHADOW] Would cancel: age={age_days:.1f}d >= {min_days}d, "
                    f"dist={dist_pct:+.1f}% >= {min_dist_pct}%. (auto-cancel disabled)",
                    flush=True,
                )
            return None

        # Auto-cancel enabled — execute cancel
        print(
            f"  [{sym}] ⚠️ Stale entry detected (age={age_days:.1f}d >= {min_days}d, "
            f"dist={dist_pct:+.1f}% >= {min_dist_pct}%). Initiating cancel...",
            flush=True,
        )
        try:
            self.executor.cancel_order(sym, entry_oid)
        except Exception as exc:
            print(f"  [{sym}] ✗ cancel_order failed with exception: {exc}", flush=True)

        # Mandatory post-cancel verification from exchange
        try:
            post_detail = self.client.get_order_detail(sym, entry_oid)
            if not isinstance(post_detail, dict):
                print(
                    f"  [{sym}] ✗ post-cancel detail not a dict: {post_detail!r}; retaining slot.",
                    flush=True,
                )
                return None
            if "status" not in post_detail or post_detail.get("status") is None:
                print(
                    f"  [{sym}] ✗ post-cancel detail missing status: {post_detail!r}; retaining slot.",
                    flush=True,
                )
                return None
            try:
                _ = int(post_detail["status"])
            except (ValueError, TypeError):
                print(
                    f"  [{sym}] ✗ post-cancel detail invalid status format: {post_detail!r}; retaining slot.",
                    flush=True,
                )
                return None

            # Verify executedQty is present, parseable as finite, non-negative float
            if "executedQty" not in post_detail or post_detail.get("executedQty") is None:
                print(
                    f"  [{sym}] ✗ post-cancel detail missing executedQty: {post_detail!r}; retaining slot.",
                    flush=True,
                )
                return None
            try:
                parsed_exec = float(post_detail["executedQty"])
                if math.isnan(parsed_exec) or math.isinf(parsed_exec) or parsed_exec < 0:
                    print(
                        f"  [{sym}] ✗ post-cancel detail invalid executedQty ({parsed_exec!r}); retaining slot.",
                        flush=True,
                    )
                    return None
            except (ValueError, TypeError):
                print(
                    f"  [{sym}] ✗ post-cancel detail unparseable executedQty ({post_detail.get('executedQty')!r}); retaining slot.",
                    flush=True,
                )
                return None

            return post_detail
        except Exception as exc:
            print(f"  [{sym}] ✗ post-cancel verification failed: {exc}; retaining slot.", flush=True)
            return None

    def _check_one(self, trade: dict, verbose: bool) -> None:
        with lifecycle_lock(str(trade.get("symbol", "?"))):
            self._check_one_locked(trade, verbose)

    def _check_one_locked(self, trade: dict, verbose: bool) -> None:
        """Process one open trade through the full entry → OCO → exit flow."""
        sym = trade.get("symbol", "?")
        entry_oid = str(trade.get("entry_order_id", ""))
        entry_status = (trade.get("entry_status") or "").upper()
        exit_status_raw = trade.get("exit_status")
        exit_status = (exit_status_raw or "").upper()
        b_order_list = str(trade.get("b_order_list_id") or "").strip()
        tp_oid = str(trade.get("tp_order_id") or "").strip()
        sl_oid = str(trade.get("sl_order_id") or "").strip()
        has_oco = bool(tp_oid and sl_oid)

        CLOSED_EXIT_STATES = {
            "TP_HIT",
            "SL_HIT",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
            "MANUAL_CLOSED",
            "CLOSED",
        }
        if exit_status in CLOSED_EXIT_STATES:
            if verbose:
                print(f"  [{sym}] trade is closed (exit_status={exit_status}); skipping.")
            return

        if verbose:
            print(
                f"  [{sym}] entry_status={entry_status}  OCO={b_order_list or ('legs:' + tp_oid)}"
            )

        # ----------------------------------------------------------------
        # Phase 0: Interrupted Recovery Guard for SL_RECOVERY_IN_PROGRESS
        # ----------------------------------------------------------------
        if trade.get("oco_state") == "SL_RECOVERY_IN_PROGRESS":
            now_iso = datetime.now(timezone.utc).isoformat()
            raw_meta = (
                dict(trade.get("raw_entry_order") or {})
                if isinstance(trade.get("raw_entry_order"), dict)
                else {}
            )
            claimed_at_iso = raw_meta.get("recovery_claimed_at")
            window_start_ms = 0
            window_end_ms = 0
            if claimed_at_iso:
                try:
                    dt = datetime.fromisoformat(claimed_at_iso.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    claim_ms = int(dt.timestamp() * 1000)
                    if claim_ms > 0:
                        window_start_ms = claim_ms - 10_000
                        window_end_ms = claim_ms + 180_000
                except Exception:
                    pass

            # Only attempt history reconciliation if recovery_claimed_at is a valid timestamp.
            # If timestamp is missing/unparseable, fail closed to RECONCILIATION_REQUIRED directly.
            if window_start_ms > 0 and window_end_ms > 0:
                entry_qty = float(trade.get("entry_qty") or 0)
                reconciled = self.executor.reconcile_emergency_sell_from_history(
                    sym, remaining_qty=entry_qty, window_start_ms=window_start_ms, window_end_ms=window_end_ms
                )
                if reconciled and reconciled.get("state") == "SL_HIT":
                    self._resolve_exit(trade, reconciled)
                    _send_toko_telegram(
                        f"🚨 STUCK-SL RECOVERED (CRASH RECONCILIATION): {sym}\n"
                        f"Prior recovery SELL confirmed filled on exchange.\n"
                        f"Exit price: Rp {float(reconciled.get('exit_price', 0)):,.2f}\n"
                        f"Reason: {reconciled.get('exit_reason')}"
                    )
                    return

            # If not verified filled, atomically transition to RECONCILIATION_REQUIRED
            raw_meta["requires_manual_review"] = True
            raw_meta["interrupted_at"] = now_iso
            transitioned = transition_tokocrypto_state(
                entry_oid,
                from_state="SL_RECOVERY_IN_PROGRESS",
                to_state="RECONCILIATION_REQUIRED",
                fields={
                    "raw_entry_order": raw_meta,
                    "updated_at": now_iso,
                },
            )
            if transitioned:
                _send_toko_telegram(
                    f"⚠️ RECOVERY_INTERRUPTED: {sym}  "
                    f"Trade was in SL_RECOVERY_IN_PROGRESS from prior run.  "
                    f"Auto-retry blocked to prevent double sell — manual reconciliation required."
                )
            return

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
            try:
                raw_exec = detail.get("executedQty")
                if raw_exec is None:
                    exec_qty = -1.0
                else:
                    exec_qty = float(raw_exec)
                    if math.isnan(exec_qty) or math.isinf(exec_qty) or exec_qty < 0:
                        exec_qty = -1.0
            except (ValueError, TypeError):
                exec_qty = -1.0
            fill_price = float(detail.get("executedPrice") or detail.get("price") or 0)
            now_iso = datetime.now(timezone.utc).isoformat()

            # entry_fill_time is bigint (epoch ms) in Supabase — use exchange time
            fill_time_ms = int(detail.get("createTime") or detail.get("time") or 0)
            if fill_time_ms == 0:
                import time as _time

            is_stale_cancel = False
            if d_status == 0 and exec_qty == 0:
                post_detail = self._evaluate_and_cancel_stale_entry(
                    trade, detail, sym, entry_oid, verbose
                )
                if post_detail is not None:
                    detail = post_detail
                    try:
                        d_status = int(detail.get("status", -99))
                    except (ValueError, TypeError):
                        d_status = -99
                    try:
                        raw_post_exec = detail.get("executedQty")
                        if raw_post_exec is None:
                            exec_qty = -1.0
                        else:
                            exec_qty = float(raw_post_exec)
                            if math.isnan(exec_qty) or math.isinf(exec_qty) or exec_qty < 0:
                                exec_qty = -1.0
                    except (ValueError, TypeError):
                        exec_qty = -1.0
                    try:
                        fill_price = float(
                            detail.get("executedPrice") or detail.get("price") or fill_price
                        )
                    except (ValueError, TypeError):
                        pass
                    fill_time_ms = int(
                        detail.get("createTime") or detail.get("time") or fill_time_ms
                    )
                    is_stale_cancel = True

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
                exit_reason = (
                    "STALE_SETUP_TIMEOUT_CANCELED"
                    if is_stale_cancel
                    else f"ENTRY_{terminal_name}"
                )

                update_tokocrypto_by_order_id(
                    entry_oid,
                    {
                        "entry_status": terminal_name,
                        "exit_status": terminal_name,
                        "exit_reason": exit_reason,
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

            elif d_status in (1, 3, 5, 6) and exec_qty > 0:
                # Partially filled terminal or active partial fill:
                # Reconcile actual fills first — do NOT treat as zero-fill cancellation!
                is_terminal = d_status in (3, 5, 6)
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
            # Telegram spam. The counter persists in Supabase.
            MAX_OCO_RETRIES = 3
            attempts = int(trade.get("oco_placement_attempts") or 0)
            oco_state_cur = (trade.get("oco_state") or "").upper()

            # Determine if this state is eligible for initial placement or retry
            is_initial_attempt = (oco_state_cur == "OCO_NOT_ATTEMPTED")
            is_retryable_failure = (
                oco_state_cur == "OCO_PLACEMENT_FAILED"
                and attempts < MAX_OCO_RETRIES
            )
            is_placement_eligible = is_initial_attempt or is_retryable_failure

            exchange_check_failed = False
            try:
                discovered, safe_to_start = self.executor.inspect_open_oco_legs(
                    sym, b_order_list
                )
            except Exception as exc:
                discovered = None
                safe_to_start = False
                exchange_check_failed = True
                print(
                    f"  [{sym}] ⚠️ OCO open-order reconciliation query failed: {exc}",
                    flush=True,
                )
                _notify_transient_reconciliation_failure(sym, str(exc))

            if exchange_check_failed:
                # Transient exchange failure during open order query:
                # Do NOT permanently lock into RECONCILIATION_REQUIRED,
                # do NOT call place_oco(), stop processing this trade this cycle.
                # Next cycle will re-query the exchange cleanly.
                if verbose:
                    print(
                        f"  [{sym}] transient exchange query failure; aborting OCO processing this cycle."
                    )
                return

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
                or not is_placement_eligible
                or b_order_list
            ):
                if oco_state_cur != "RECONCILIATION_REQUIRED":
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
                else:
                    if verbose:
                        print(
                            f"  [{sym}] awaiting manual reconciliation (state={oco_state_cur})"
                        )
                return

            if not has_oco and not claim_oco_submission(entry_oid):
                if verbose:
                    print(
                        f"  [{sym}] OCO submission is already claimed or unresolved; skipping."
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
                except Exception as pend_exc:
                    release_oco_submission(entry_oid)
                    print(
                        f"  [{sym}] 🚨 Failed to persist OCO_SUBMISSION_PENDING to DB: {pend_exc}",
                        flush=True,
                    )
                    _send_toko_telegram(
                        f"🚨 OCO PERSISTENCE FAILED BEFORE SUBMISSION: {sym}\n"
                        f"{str(pend_exc)[:120]}\n"
                        "Exchange POST aborted for safety."
                    )
                    return

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
                        attempts += 1
                        is_exhausted = (attempts >= MAX_OCO_RETRIES)
                        next_state = "RECONCILIATION_REQUIRED" if is_exhausted else "OCO_PLACEMENT_FAILED"
                        try:
                            update_tokocrypto_by_order_id(
                                entry_oid,
                                {
                                    "oco_state": next_state,
                                    "oco_placement_attempts": attempts,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                        except Exception as db_exc:
                            # State persistence failed: do NOT release in-memory submission claim,
                            # keeping it flagged as unresolved. Escalate failure explicitly.
                            print(
                                f"  [{sym}] 🚨 Failed to persist OCO failure state to database: {db_exc}",
                                flush=True,
                            )
                            _send_toko_telegram(
                                f"🚨 OCO STATE PERSISTENCE FAILED: {sym}\n"
                                f"Pre-submit rejected, but DB state update failed ({str(db_exc)[:120]}).\n"
                                "Submission claim retained as unresolved; manual reconciliation required."
                            )
                            raise
                        else:
                            # DB update confirmed: release submission claim so next cycle can retry cleanly
                            release_oco_submission(entry_oid)
                            if is_exhausted:
                                _send_toko_telegram(
                                    f"🚨 OCO RETRIES EXHAUSTED: {sym} ({attempts}/{MAX_OCO_RETRIES} attempts failed).\n"
                                    "Pre-submit checks failed repeatedly; automatic retry stopped. Manual reconciliation required."
                                )
                            else:
                                _send_toko_telegram(
                                    f"⚠️ OCO NOT PLACED: {sym} (attempt {attempts}/{MAX_OCO_RETRIES} failed pre-submit). "
                                    "Will retry on next cycle if price/conditions recover."
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
                    # 1. In-process concurrency guard (threads within same process)
                    if not claim_recovery_submission(entry_oid):
                        if verbose:
                            print(
                                f"  [{sym}] Recovery already claimed by concurrent thread, skipping."
                            )
                        return

                    # 2. Cross-process atomic claim & persistent DB lock BEFORE any exchange action
                    try:
                        raw_meta = (
                            dict(trade.get("raw_entry_order") or {})
                            if isinstance(trade.get("raw_entry_order"), dict)
                            else {}
                        )
                        raw_meta["recovery_attempted"] = True
                        raw_meta["recovery_claimed_at"] = now_iso
                        try:
                            claimed = claim_tokocrypto_recovery(
                                entry_oid,
                                {
                                    "oco_state": "SL_RECOVERY_IN_PROGRESS",
                                    "raw_entry_order": raw_meta,
                                    "updated_at": now_iso,
                                },
                            )
                        except Exception as exc:
                            print(f"  [{sym}] claim_tokocrypto_recovery exception: {exc}")
                            claimed = False

                        if not claimed:
                            if verbose:
                                print(
                                    f"  [{sym}] Recovery already claimed or resolved by another process in database, skipping."
                                )
                            return

                        # Attempt safe recovery via executor once
                        recovery_dict = self.executor.recover_stuck_sl(trade)
                    finally:
                        release_recovery_submission(entry_oid)

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

            elif oco_state == "SL_RECOVERY_IN_PROGRESS":
                # Previous recovery attempt crashed or was interrupted mid-flight.
                # Never retry automatic market sell to prevent duplicate selling!
                raw_meta = (
                    dict(trade.get("raw_entry_order") or {})
                    if isinstance(trade.get("raw_entry_order"), dict)
                    else {}
                )
                raw_meta["requires_manual_review"] = True
                transitioned = transition_tokocrypto_state(
                    entry_oid,
                    from_state="SL_RECOVERY_IN_PROGRESS",
                    to_state="RECONCILIATION_REQUIRED",
                    fields={
                        "raw_entry_order": raw_meta,
                        "updated_at": now_iso,
                    },
                )
                if transitioned:
                    _send_toko_telegram(
                        f"⚠️ RECOVERY_INTERRUPTED: {sym}  "
                        f"Trade was in SL_RECOVERY_IN_PROGRESS from prior run.  "
                        f"Auto-retry blocked to prevent double sell — manual reconciliation required."
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
        sym = trade.get("symbol", "?")
        # Idempotency guard: if position is already resolved, do NOT re-process or duplicate alerts
        if trade.get("exit_status") in ("TP_HIT", "SL_HIT"):
            print(
                f"  [{sym}] Position already resolved as {trade.get('exit_status')}. Skipping redundant exit resolution."
            )
            return

        is_tp = state_dict["state"] == "TP_HIT"
        exit_price = float(state_dict["exit_price"] or 0)
        entry_oid = str(trade.get("entry_order_id", ""))
        now_iso = datetime.now(timezone.utc).isoformat()

        entry_fill_price = float(
            trade.get("entry_fill_price") or trade.get("entry_price") or 0
        )
        exit_qty = float(state_dict.get("exit_qty") or trade.get("entry_qty") or 0)
        entry_notional_idr = float(
            trade.get("entry_notional_idr") or (entry_fill_price * exit_qty)
        )

        realized_pnl_idr = (exit_price - entry_fill_price) * exit_qty
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
