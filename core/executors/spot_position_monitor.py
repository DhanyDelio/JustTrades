
import sys
import atexit
import re
import os
from datetime import datetime, timezone
from collections import Counter, defaultdict

import services.chart_analyzer as ca
from core.paper_trade_executor import (
    _fmt_order_status,
    _send_telegram,
    LAB_STARTING_CAPITAL,
    BUDGET_USD,
    PER_TRADE_BUDGET
)
from core.managers.portfolio_manager import PortfolioManager
from core.guards.stale_pending_entry_guard import evaluate as evaluate_stale_entry


def _is_confirmed_missing_oco_error(exc: Exception) -> bool:
    """True only when Binance explicitly reports an expected OCO order absent."""
    code = getattr(exc, "code", None)
    if code in (-2018, -2013):
        return True
    message = str(exc)
    return bool(re.search(r"(?:code\s*[=:]\s*|\"code\"\s*:\s*)(-2018|-2013)\b", message))

class SpotPositionMonitor:
    def __init__(self, client, repo, order_executor):
        self.client = client
        self.repo = repo
        self.order_executor = order_executor

    @staticmethod
    def _auto_cancel_enabled() -> bool:
        """Real cancellation is deliberately opt-in after shadow validation."""
        return os.getenv("STALE_ENTRY_AUTO_CANCEL_ENABLED", "false").lower() in {
            "1", "true", "yes", "on"
        }

    @staticmethod
    def _set_pending_guard(trade: dict, **fields) -> None:
        raw = dict(trade.get("raw_entry_order") or {})
        guard = dict(raw.get("pending_entry_guard") or {})
        guard.update(fields)
        raw["pending_entry_guard"] = guard
        trade["raw_entry_order"] = raw

    def _persist_pending_guard(self, trade: dict) -> bool:
        """Durably record an intent before an optional exchange cancellation."""
        try:
            from services.supabase_client import update_spot_by_order_id
            update_spot_by_order_id(
                trade["entry_order_id"], {"raw_entry_order": trade["raw_entry_order"]}
            )
            return True
        except Exception as exc:
            self._set_pending_guard(
                trade, state="RECONCILIATION_REQUIRED",
                reconciliation_reason="SUPABASE_PERSIST_FAILED",
                reconciliation_error=str(type(exc).__name__),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            return False

    def _revalidate_pending_entry(self, trade: dict) -> dict:
        """Fresh structural read; failures are UNKNOWN and can never cancel."""
        try:
            result = ca.analyze_symbol(trade["symbol"], save_chart=False)
            if not result:
                return {"zone_valid": "unknown", "reason": "ANALYSIS_UNAVAILABLE"}
            entry = float(trade["entry_price"])
            # The original entry is valid only if a current support zone still
            # contains it (with a small entry-buffer tolerance).
            for zone in result.get("support_zones", []):
                low, high = float(zone["low"]), float(zone["high"])
                if low <= entry <= high * 1.003:
                    return {"zone_valid": True, "reason": "SUPPORT_ZONE_RETAINED",
                            "zone": {k: zone.get(k) for k in ("low", "high", "center", "touches")}}
            return {"zone_valid": False, "reason": "ENTRY_ZONE_NOT_RETAINED"}
        except Exception:
            return {"zone_valid": "unknown", "reason": "ANALYSIS_ERROR"}

    def _handle_stale_pending_entry(self, trade: dict, current: float) -> bool:
        """Review/revalidate an unfilled entry. Returns True when terminalized."""
        decision = evaluate_stale_entry(trade, current)
        if decision["action"] == "NONE":
            return False
        now = datetime.now(timezone.utc).isoformat()
        self._set_pending_guard(trade, state=decision["action"], updated_at=now,
                                last_price=current, last_distance_pct=decision.get("distance_pct"),
                                order_age_days=decision.get("age_days"))
        if decision["action"] == "REVALIDATE":
            verdict = self._revalidate_pending_entry(trade)
            self._set_pending_guard(trade, state="REVIEW_REQUIRED",
                                    last_revalidation_at=now, last_revalidation=verdict)
            decision = evaluate_stale_entry(trade, current)

        if decision["action"] != "CANCEL_ELIGIBLE":
            return False
        self._set_pending_guard(trade, state="CANCEL_ELIGIBLE", updated_at=now,
                                cancel_reason="STALE_SETUP_CANCELLED",
                                cancel_subreason="PRICE_RUNAWAY_AND_ZONE_INVALID")
        if not self._auto_cancel_enabled():
            self._set_pending_guard(trade, state="WOULD_CANCEL", updated_at=now)
            print(f"  {trade['symbol']:<10} ℹ Stale-entry shadow: would cancel "
                  f"({decision['distance_pct']:+.2f}% above entry; zone invalid).")
            return False

        # Two durable checks bracket the exchange mutation. A DB outage or
        # exchange state change always fails closed rather than cancelling.
        self._set_pending_guard(trade, state="CANCEL_IN_FLIGHT", cancel_requested_at=now)
        if not self._persist_pending_guard(trade):
            print(f"  {trade['symbol']:<10} ⚠ Stale-entry cancel skipped: audit persistence unavailable.")
            return False
        try:
            latest = self.client.get_order(symbol=trade["symbol"], orderId=trade["entry_order_id"])
            if (str(latest.get("status", "")).upper() != "NEW"
                    or float(latest.get("executedQty", 0) or 0) != 0):
                trade["entry_status"] = latest.get("status", trade.get("entry_status"))
                self._set_pending_guard(trade, state="RECONCILIATION_REQUIRED",
                                        reconciliation_reason="EXCHANGE_STATE_CHANGED")
                return False
            self.order_executor.cancel_order(trade["symbol"], trade["entry_order_id"])
            confirmed = self.client.get_order(
                symbol=trade["symbol"], orderId=trade["entry_order_id"]
            )
            if str(confirmed.get("status", "")).upper() != "CANCELED":
                trade["entry_status"] = confirmed.get("status", trade.get("entry_status"))
                self._set_pending_guard(trade, state="RECONCILIATION_REQUIRED",
                                        reconciliation_reason="CANCEL_NOT_CONFIRMED")
                return False
        except Exception:
            self._set_pending_guard(trade, state="RECONCILIATION_REQUIRED",
                                    reconciliation_reason="CANCEL_UNCONFIRMED")
            return False

        trade["entry_status"] = "CANCELED"
        trade["exit_status"] = "CANCELED"
        trade["exit_reason"] = "STALE_SETUP_CANCELLED"
        self._set_pending_guard(trade, state="STALE_SETUP_CANCELLED",
                                cancel_confirmed_at=datetime.now(timezone.utc).isoformat())
        print(f"  {trade['symbol']:<10} ✅ Stale pending entry canceled after exchange re-check.")
        return True

    def check_positions(self, verbose: bool = False, mode: str = "all",
                        recover_unprotected: bool = False) -> None:
        client = self.client
        repo = self.repo
        SpotOrderExecutor = lambda c: self.order_executor  # wrapper to mock SpotOrderExecutor(client) calls inside

        """
        For each trade with exit_status == OPEN:
        1. Query entry order status from exchange.
        2. If entry FILLED and no OCO yet → place OCO, update log.
        3. If OCO placed → check OCO legs for TP_HIT / SL_HIT, update log.
        4. Print grouped summary (compact by default, detailed with --verbose).
        """
        trades     = repo.load_trade_log()
        # Filter trades according to mode (single/lab/all) for display and operations
        filtered_trades = [t for t in trades if repo.match_mode(t, mode)]
        open_trades = [t for t in filtered_trades if t.get("exit_status") == "OPEN"]
        log_dirty  = False
    
        if not open_trades:
            print("\n  No open positions in trade_log.json")
            closed = [t for t in filtered_trades if t.get("exit_status") != "OPEN"][-5:]
            if closed:
                print(f"\n  Last {len(closed)} closed trade(s):")
                for t in closed:
                    pnl = f"${t['realized_pnl_usd']:+.2f}" if t.get("realized_pnl_usd") is not None else "n/a"
                    hrs = f"{t['time_to_resolution_sec']//3600}h" if t.get("time_to_resolution_sec") else "n/a"
                    print(f"    {t['symbol']:10} {t['direction'].upper():5} "
                          f"{t['exit_status']:15}  PnL: {pnl:>8}  held: {hrs}")
            return
    
        # Show lab pool status (if any clustered trades exist)
        pm = PortfolioManager(repo, BUDGET_USD, LAB_STARTING_CAPITAL, PER_TRADE_BUDGET)
        pool = pm.compute_lab_pool(trades)
        lab_cap = pool["lab_capital"]
        net_pnl = pool["closed_cluster_pnl"]
        deployed = pool["deployed_capital"]
        available = pool["available_capital"]
        max_new = pool["max_new_positions"]
        print(f"\n  Lab capital: ${lab_cap:.2f} (started ${LAB_STARTING_CAPITAL:.0f}, net P&L ${net_pnl:+.2f})  |  Deployed: ${deployed:.2f}  |  Available: ${available:.2f}  |  Max new positions: {max_new}")
    
        # ── Group by correlation_cluster_id ───────────────────────────────
        from collections import Counter, defaultdict
        clusters: dict[str, list] = defaultdict(list)
        for t in open_trades:
            cid = t.get("correlation_cluster_id") or "single"
            clusters[cid].append(t)
    
        dup_syms = [sym for sym, count in Counter(t["symbol"] for t in open_trades).items() if count > 1]
        if dup_syms:
            print("\n  ⚠ DUPLICATE OPEN SYMBOL(S) DETECTED — review before adding more positions:")
            for sym in dup_syms:
                entries = sorted(
                    [t for t in open_trades if t["symbol"] == sym],
                    key=lambda t: t.get("open_time", "")
                )
                print(f"\n    {sym}:")
                for t in entries:
                    cluster_label = t.get("correlation_cluster_id") or "single --propose"
                    print(f"      Order #{t['entry_order_id']:<10}  cluster={cluster_label}")
                    print(f"        status={t.get('entry_status','?')}  opened={t.get('open_time','?')[:19]}")
                # Identify the older single-propose entry to suggest cancellation
                single_entries = [t for t in entries if not t.get("correlation_cluster_id")]
                if single_entries:
                    stale = single_entries[0]
                    print(f"\n      ⚠  Order #{stale['entry_order_id']} is a stale single --propose entry.")
                    print(f"         If you no longer want it, cancel it at testnet.binance.vision,")
                    print(f"         then update trade_log.json: set that entry's exit_status to 'CANCELED'.")
                    print(f"         Until canceled, BOTH orders may fill — doubling your {sym} exposure.")
    
        n_filled   = sum(1 for t in open_trades if t.get("entry_status") == "FILLED")
        n_oco      = sum(1 for t in open_trades if t.get("oco_placed"))
        n_pending  = len(open_trades) - n_filled
    
        print(f"\n  ── OPEN POSITIONS: {len(open_trades)} total  "
              f"({n_pending} pending fill, {n_filled} filled, {n_oco} OCO active) ──")
    
        resolved_this_run = []
    
        for cid, group in clusters.items():
            cluster_label = f"Cluster {cid}" if cid != "single" else "Single trade"
            print(f"\n  [{cluster_label}  —  {len(group)} position(s)]")
    
            # Keep the compact table header for navigation, but only print the
            # per-symbol summary row in non-verbose mode.
            print(f"  {'Symbol':<10} {'Status':<22} {'PnL/Info':>12}  OCO")
            print(f"  {'─'*55}")
    
            # Batch-fetch current prices for this group's symbols to avoid per-symbol rate hits
            try:
                all_tickers = client.get_all_tickers()
                price_map = {t.get('symbol'): float(t.get('price')) for t in all_tickers}
            except Exception:
                price_map = {}
    
            for trade in group:
                sym  = trade["symbol"]
                dirn = trade["direction"].upper()
                eid  = trade.get("entry_order_id")
    
                # ── Step 1: Query entry order ──────────────────────────────
                # If entry order returns -2013 (purged after testnet reset):
                #   - local entry_status=FILLED  → use persisted state, continue to OCO
                #   - local entry_status=PENDING  → ambiguous, mark RECONCILIATION_REQUIRED
                #   - other errors               → skip this trade this cycle
                _entry_order_missing = False
                try:
                    entry_order  = client.get_order(symbol=sym, orderId=eid)
                    entry_status = entry_order.get("status", "UNKNOWN")
                except Exception as e:
                    err_str      = str(e)
                    _purged      = "-2013" in err_str or "Order does not exist" in err_str
                    local_status = trade.get("entry_status", "")

                    if _purged and local_status == "FILLED":
                        # Entry order history gone but position was already FILLED.
                        # Persisted entry_status / entry_fill_price / entry_qty are
                        # sufficient — proceed directly to OCO reconciliation.
                        entry_status         = "FILLED"
                        _entry_order_missing = True
                        print(
                            f"  {sym:<10} ℹ Entry order {eid} not found (purged). "
                            f"Using persisted FILLED state — continuing to OCO check."
                        )
                    elif _purged and local_status in ("NEW", "PARTIALLY_FILLED", ""):
                        # Pending entry order purged — cannot confirm fill.
                        # IMPORTANT: persist this state to Supabase immediately so:
                        #   (a) the next cycle does not re-detect and loop silently forever
                        #   (b) the portfolio slot is released (entry_status=RECONCILIATION_REQUIRED
                        #       is excluded from deployed_count by portfolio_manager)
                        # Idempotent: skip the write if already RECONCILIATION_REQUIRED.
                        _already_recon = (local_status == "RECONCILIATION_REQUIRED")
                        if not _already_recon:
                            trade["entry_status"] = "RECONCILIATION_REQUIRED"
                            # Also set oco_reconciliation_status for backward-compat
                            # with any code that reads this field for display/audit.
                            trade["oco_reconciliation_status"] = "RECONCILIATION_REQUIRED"
                            # Record audit trail in raw_entry_order
                            _raw_recon = dict(trade.get("raw_entry_order") or {})
                            _raw_recon["reconciliation_required_at"] = (
                                datetime.now(timezone.utc).isoformat()
                            )
                            _raw_recon["reconciliation_reason"] = (
                                "ENTRY_ORDER_NOT_FOUND_ON_EXCHANGE"
                            )
                            _raw_recon["reconciliation_exchange_error"] = err_str[:120]
                            trade["raw_entry_order"] = _raw_recon
                            log_dirty = True
                            # Persist immediately — do not wait for end-of-cycle save.
                            try:
                                from services.supabase_client import update_spot_by_order_id
                                update_spot_by_order_id(eid, {
                                    "entry_status":    "RECONCILIATION_REQUIRED",
                                    "raw_entry_order": _raw_recon,
                                })
                            except Exception as _persist_exc:
                                # Non-fatal: in-memory state is updated; end-of-cycle
                                # save will retry. Never block the monitoring loop.
                                print(
                                    f"  {sym:<10} ⚠ RECONCILIATION_REQUIRED persist failed "
                                    f"(will retry): {_persist_exc}"
                                )
                        print(
                            f"  {sym:<10} ⚠ Entry order {eid} not found "
                            f"({'already ' if _already_recon else ''}RECONCILIATION_REQUIRED), "
                            f"local_status={local_status!r}. Portfolio slot released."
                        )
                        continue
                    else:
                        # Non-purge error (network, auth, etc.) — skip this cycle.
                        print(f"  {sym:<10} ⚠ Could not query entry order: {e}")
                        continue
    
                # If entry order was purged, skip fill-price derivation and
                # use values already persisted in the trade dict.
                if _entry_order_missing:
                    filled_qty  = float(trade.get("entry_qty") or 0)
                    fill_price  = float(trade.get("entry_fill_price") or trade.get("entry_price") or 0)
                    # entry_status is already set above; nothing more to derive here.
                    # Jump past all entry-order-derived logic below.
                    # The goto-equivalent: set entry_order to an empty dict so
                    # downstream reads like entry_order.get(...) return None safely.
                    entry_order = {}
                else:
                    filled_qty  = float(entry_order.get("executedQty", 0))
                    actual_fill = float(entry_order.get("cummulativeQuoteQty", 0))
                # fill_price derivation — only when entry order was actually fetched
                if not _entry_order_missing:
                    if filled_qty > 0 and actual_fill > 0:
                        fill_price = actual_fill / filled_qty
                    else:
                        # Fallback 1: /api/v3/myTrades — most reliable actual fill price
                        _fill_resolved = False
                        try:
                            my_trades = client.get_my_trades(symbol=sym, orderId=eid, limit=5)
                            if my_trades:
                                total_qty   = sum(float(t["qty"])   for t in my_trades)
                                total_quote = sum(float(t["quoteQty"]) for t in my_trades)
                                if total_qty > 0 and total_quote > 0:
                                    fill_price = total_quote / total_qty
                                    _fill_resolved = True
                        except Exception:
                            pass
                        if not _fill_resolved:
                            # Fallback 2: limit price from the order
                            fill_price = float(entry_order.get("price", trade["entry_price"]))
    
                if trade.get("entry_status") != entry_status:
                    trade["entry_status"] = entry_status
                    log_dirty = True
                # Only update fill details from exchange if entry order was fetched.
                # For purged orders (_entry_order_missing), fill details are already
                # persisted in the trade dict — do not overwrite with None/defaults.
                if not _entry_order_missing:
                    if entry_status == "FILLED" and trade.get("entry_fill_price") is None:
                        trade["entry_fill_price"] = fill_price
                        trade["entry_fill_time"]  = entry_order.get("updateTime")
                        trade["entry_qty"]        = filled_qty
                        planned = trade.get("entry_price", fill_price)
                        trade["slippage_pct"] = round(
                            (fill_price - planned) / planned * 100, 4
                        ) if planned else None
                        log_dirty = True
                        # Notify on NEW → FILLED transition
                        _send_telegram(
                            f"✅ Filled: {sym} {trade.get('direction','').lower()} @ {ca._fmt_price(fill_price).strip()}"
                            f" | SL: {ca._fmt_price(trade.get('sl')).strip()}"
                            f" | TP: {ca._fmt_price(trade.get('tp1')).strip()}"
                        )

                # ── Step 1.5: stale unfilled-entry guard ────────────────
                # This never re-prices/chases a limit buy.  In its default
                # shadow mode it only records what would be canceled.
                if entry_status == "NEW" and price_map.get(sym) is not None:
                    before_guard = dict(trade.get("raw_entry_order") or {})
                    terminalized = self._handle_stale_pending_entry(
                        trade, price_map[sym]
                    )
                    if trade.get("raw_entry_order") != before_guard:
                        log_dirty = True
                    if terminalized:
                        log_dirty = True
                        continue

                # ── Step 2: Place OCO if filled and no OCO yet ─────────────
                #
                # place_oco_order now returns a structured dict:
                #   {"protection_state": FULLY_PROTECTED|TP_ONLY|SL_ONLY|UNPROTECTED,
                #    "oco_resp": dict|None, "tp_order_id": int|None,
                #    "sl_order_id": int|None, "filter_reason": str|None,
                #    "_market_sold": bool}
                #
                # oco_placed=True is ONLY set for FULLY_PROTECTED (real OCO list).
                # TP_ONLY / SL_ONLY set their own fields and oco_reconciliation_status.
                # UNPROTECTED stores the state so next cycle can retry.
                # No-spam idempotency: skip this whole Step 2 block if the trade
                # already has a partial-protection state from a prior cycle AND the
                # prices have not changed — the next-cycle recovery in Step 3.5 handles
                # the upgrade attempt.
                _partial_states = {"TP_ONLY", "SL_ONLY", "UNPROTECTED"}
                _already_partial = trade.get("oco_reconciliation_status") in _partial_states
                if entry_status == "FILLED" and not trade.get("oco_placed") and not _already_partial:
                    print(f"  {sym:<10} ✅ FILLED — placing protection orders...")
                    prot_result, last_err = None, None
                    for attempt in range(1, 3):
                        try:
                            prot_result = self.order_executor.place_oco_order(trade)
                            break
                        except RuntimeError as e:
                            last_err = e
                            if attempt < 2:
                                import time; time.sleep(3)

                    if prot_result is None:
                        # Both attempts raised RuntimeError — show critical banner
                        print(
                            f"\n  {'!'*60}\n"
                            f"  !! CRITICAL: Protection FAILED for {sym} — UNPROTECTED !!\n"
                            f"  !! Error: {str(last_err)[:48]:<50}!!\n"
                            f"  !! SL: {ca._fmt_price(trade['sl']).strip():<30} "
                            f"TP: {ca._fmt_price(trade['tp1']).strip():<20}!!\n"
                            f"  !! Fix manually at testnet.binance.vision              !!\n"
                            f"  {'!'*60}\n"
                        )
                        trade["oco_reconciliation_status"] = "UNPROTECTED"
                        log_dirty = True
                        from services.supabase_client import update_spot_by_order_id
                        try:
                            update_spot_by_order_id(eid, {"oco_reconciliation_status": "UNPROTECTED"})
                        except Exception:
                            pass
                        import atexit
                        atexit.register(lambda: sys.exit(2))

                    else:
                        # ── Backward-compat: normalize raw exchange dict (from mocks /
                        # legacy code) to the new structured format ────────────────
                        if isinstance(prot_result, dict) and "protection_state" not in prot_result:
                            # Raw OCO response (orderListId present) → FULLY_PROTECTED
                            prot_result = {
                                "protection_state": "FULLY_PROTECTED",
                                "oco_resp": prot_result,
                                "tp_order_id": None,
                                "sl_order_id": None,
                                "filter_reason": None,
                            }
                        protection_state = prot_result.get("protection_state", "UNPROTECTED")
                        _market_sold     = prot_result.get("_market_sold") or trade.get("_market_sold")

                        # ── Market-sell path (price dropped to/below SL) ──────────
                        if _market_sold:
                            raw_resp   = prot_result.get("oco_resp") or {}
                            entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                            exit_px    = float(raw_resp.get("fills", [{}])[0].get("price", 0) or 0) \
                                         if raw_resp.get("fills") else None
                            if not exit_px and raw_resp:
                                exec_qty  = float(raw_resp.get("executedQty", 0) or 0)
                                cum_quote = float(raw_resp.get("cummulativeQuoteQty", 0) or 0)
                                exit_px   = cum_quote / exec_qty if exec_qty > 0 else None
                            if not exit_px:
                                exit_px = trade["sl"]
                            pnl_usd = (exit_px - entry_fill) * trade["entry_qty"]
                            pnl_pct = pnl_usd / trade.get("entry_notional", 1) * 100
                            exit_ts = (
                                raw_resp.get("transactTime") or raw_resp.get("updateTime")
                                or int(datetime.now(timezone.utc).timestamp() * 1000)
                            )
                            trade["exit_status"]      = "SL_HIT"
                            trade["exit_price"]       = round(exit_px, 6)
                            trade["exit_time"]        = int(exit_ts)
                            trade["exit_reason"]      = "SL_HIT"
                            trade["realized_pnl_usd"] = round(pnl_usd, 4)
                            trade["realized_pnl_pct"] = round(pnl_pct, 2)
                            fill_t = trade.get("entry_fill_time")
                            if fill_t:
                                trade["time_to_resolution_sec"] = (int(exit_ts) - int(fill_t)) // 1000
                            trade["oco_placed"]       = False
                            trade["oco_list_id"]      = None
                            trade.pop("_market_sold", None)
                            log_dirty = True
                            resolved_this_run.append((sym, "SL_HIT", pnl_usd))
                            self._eager_commit(trade)
                            print(f"  {sym:<10} 🔴 Emergency market sell — SL_HIT logged  PnL: ${pnl_usd:+.4f}")
                            continue   # ← skip Step 3 entirely

                        # ── FULLY_PROTECTED: standard OCO placed ──────────────────
                        elif protection_state == "FULLY_PROTECTED":
                            oco_resp   = prot_result.get("oco_resp") or {}
                            oco_orders = oco_resp.get("orderReports", [])
                            trade["oco_placed"]                = True
                            trade["oco_order_ids"]             = [o["orderId"] for o in oco_orders]
                            trade["oco_list_id"]               = oco_resp.get("orderListId")
                            trade["oco_reconciliation_status"] = "FULLY_PROTECTED"
                            log_dirty = True
                            from services.supabase_client import update_spot_by_order_id
                            update_spot_by_order_id(eid, {
                                "entry_status":              trade.get("entry_status"),
                                "entry_fill_price":          trade.get("entry_fill_price"),
                                "entry_fill_time":           trade.get("entry_fill_time"),
                                "entry_qty":                 trade.get("entry_qty"),
                                "slippage_pct":              trade.get("slippage_pct"),
                                "oco_placed":                True,
                                "oco_order_ids":             trade.get("oco_order_ids"),
                                "oco_list_id":               trade.get("oco_list_id"),
                                "oco_reconciliation_status": "FULLY_PROTECTED",
                            })
                            print(f"  {sym:<10} ✅ OCO placed (FULLY_PROTECTED)  List#{trade['oco_list_id']}")

                        # ── TP_ONLY: standalone TP LIMIT_MAKER placed; SL filter-invalid ─
                        elif protection_state == "TP_ONLY":
                            tp_order_id = prot_result.get("tp_order_id")
                            trade["oco_placed"]                = False   # no OCO list
                            trade["oco_list_id"]               = None
                            trade["tp_order_id"]               = tp_order_id
                            trade["oco_reconciliation_status"] = "TP_ONLY"
                            log_dirty = True
                            from services.supabase_client import update_spot_by_order_id
                            update_spot_by_order_id(eid, {
                                "entry_status":              trade.get("entry_status"),
                                "entry_fill_price":          trade.get("entry_fill_price"),
                                "entry_fill_time":           trade.get("entry_fill_time"),
                                "entry_qty":                 trade.get("entry_qty"),
                                "slippage_pct":              trade.get("slippage_pct"),
                                "oco_placed":                False,
                                "oco_reconciliation_status": "TP_ONLY",
                                "tp_order_id":               tp_order_id,
                            })
                            _send_telegram(
                                f"🟡 [SPOT] PARTIAL PROTECTION (TP_ONLY): {sym}\n"
                                f"TP order placed (orderId={tp_order_id}). "
                                f"SL {ca._fmt_price(trade.get('sl')).strip()} is filter-invalid "
                                f"(PERCENT_PRICE_BY_SIDE).\n"
                                f"SL value preserved — NOT shifted. "
                                f"Recovery attempted next cycle if filter clears."
                            )
                            print(
                                f"  {sym:<10} 🟡 TP_ONLY: standalone TP placed "
                                f"(orderId={tp_order_id}), SL filter-invalid"
                            )

                        # ── SL_ONLY: standalone SL STOP_LOSS_LIMIT placed; TP filter-invalid ─
                        elif protection_state == "SL_ONLY":
                            sl_order_id = prot_result.get("sl_order_id")
                            trade["oco_placed"]                = False
                            trade["oco_list_id"]               = None
                            trade["sl_order_id"]               = sl_order_id
                            trade["oco_reconciliation_status"] = "SL_ONLY"
                            log_dirty = True
                            from services.supabase_client import update_spot_by_order_id
                            update_spot_by_order_id(eid, {
                                "entry_status":              trade.get("entry_status"),
                                "entry_fill_price":          trade.get("entry_fill_price"),
                                "entry_fill_time":           trade.get("entry_fill_time"),
                                "entry_qty":                 trade.get("entry_qty"),
                                "slippage_pct":              trade.get("slippage_pct"),
                                "oco_placed":                False,
                                "oco_reconciliation_status": "SL_ONLY",
                                "sl_order_id":               sl_order_id,
                            })
                            _send_telegram(
                                f"🟠 [SPOT] PARTIAL PROTECTION (SL_ONLY): {sym}\n"
                                f"SL order placed (orderId={sl_order_id}). "
                                f"TP {ca._fmt_price(trade.get('tp1')).strip()} is filter-invalid.\n"
                                f"Recovery attempted next cycle if filter clears."
                            )
                            print(
                                f"  {sym:<10} 🟠 SL_ONLY: standalone SL placed "
                                f"(orderId={sl_order_id}), TP filter-invalid"
                            )

                        # ── UNPROTECTED: both legs invalid, no exchange call ───────
                        else:  # UNPROTECTED
                            filter_reason = prot_result.get("filter_reason", "UNKNOWN")
                            trade["oco_placed"]                = False
                            trade["oco_list_id"]               = None
                            trade["oco_reconciliation_status"] = "UNPROTECTED"
                            log_dirty = True
                            from services.supabase_client import update_spot_by_order_id
                            update_spot_by_order_id(eid, {
                                "entry_status":              trade.get("entry_status"),
                                "entry_fill_price":          trade.get("entry_fill_price"),
                                "entry_fill_time":           trade.get("entry_fill_time"),
                                "entry_qty":                 trade.get("entry_qty"),
                                "slippage_pct":              trade.get("slippage_pct"),
                                "oco_placed":                False,
                                "oco_reconciliation_status": "UNPROTECTED",
                            })
                            _send_telegram(
                                f"🚨 [SPOT] UNPROTECTED: {sym}\n"
                                f"Both TP and SL fail PERCENT_PRICE_BY_SIDE filter ({filter_reason}).\n"
                                f"No protection orders placed. Recovery attempted next cycle."
                            )
                            print(
                                f"\n  {'!'*60}\n"
                                f"  !! UNPROTECTED: {sym} — both legs filter-invalid ({filter_reason}) !!\n"
                                f"  !! SL: {ca._fmt_price(trade['sl']).strip():<30} "
                                f"TP: {ca._fmt_price(trade['tp1']).strip():<20}!!\n"
                                f"  !! Recovery will be attempted next cycle               !!\n"
                                f"  {'!'*60}\n"
                            )

                # ── Step 2b: Next-cycle recovery for partial protection states ──
                # If the trade already has TP_ONLY / SL_ONLY / UNPROTECTED from a
                # prior cycle, attempt to add the missing leg on this cycle.
                # Idempotency: only attempts when the filter conditions may have changed
                # (i.e. we don't spam every cycle when the symbol is genuinely stale).
                # No-spam guard: use oco_reconciliation_status as the primary gate;
                # a successful upgrade changes the status and stops future retries.
                elif (entry_status == "FILLED"
                      and not trade.get("oco_placed")
                      and _already_partial):
                    self._recover_partial_protection(
                        trade, eid, log_dirty, resolved_this_run
                    )
                    # Re-read log_dirty in case recovery set it — use a reference trick
                    # by checking if trade state changed (eager_commit already persisted).
                    if trade.get("oco_reconciliation_status") == "FULLY_PROTECTED":
                        log_dirty = True
    
                # ── Step 3: Check OCO status ────────────────────────────────
                prev_recon = trade.get("oco_reconciliation_status", "")
                _oco_missing = False
                _oco_query_confirmed = False
                oco_str = "n/a"
                if trade.get("oco_placed") and trade.get("oco_list_id"):
                    try:
                        oco_status  = client.v3_get_order_list(orderListId=trade["oco_list_id"])
                        _oco_query_confirmed = True
                        list_status = oco_status.get("listOrderStatus", "UNKNOWN")
                        oco_str     = list_status
    
                        if list_status == "ALL_DONE":
                            for leg_ref in oco_status.get("orders", []):
                                leg = client.get_order(symbol=sym, orderId=leg_ref["orderId"])
                                if leg.get("status") == "FILLED":
                                    exec_qty   = float(leg.get("executedQty", 0) or 1)
                                    cum_quote  = float(leg.get("cummulativeQuoteQty", 0))
                                    exit_price = cum_quote / exec_qty if exec_qty > 0 \
                                                 else float(leg.get("price", 0))
                                    exit_status = "SL_HIT" if "STOP" in leg.get("type","") else "TP_HIT"
                                    entry_fill  = trade.get("entry_fill_price") or trade["entry_price"]
                                    pnl_usd     = (exit_price - entry_fill) * trade["entry_qty"]
                                    pnl_pct     = pnl_usd / trade["entry_notional"] * 100
                                    trade["exit_status"]     = exit_status
                                    trade["exit_price"]      = round(exit_price, 6)
                                    trade["exit_time"]       = leg.get("updateTime")
                                    trade["exit_reason"]     = exit_status
                                    trade["realized_pnl_usd"] = round(pnl_usd, 4)
                                    trade["realized_pnl_pct"] = round(pnl_pct, 2)
                                    fill_t = trade.get("entry_fill_time")
                                    exit_t = leg.get("updateTime")
                                    if fill_t and exit_t:
                                        trade["time_to_resolution_sec"] = (int(exit_t)-int(fill_t))//1000
                                    elif exit_t and trade.get("open_time"):
                                        # Fallback: use open_time (order placed time) if fill_time missing
                                        try:
                                            open_ms = int(datetime.fromisoformat(
                                                trade["open_time"]
                                            ).timestamp() * 1000)
                                            trade["time_to_resolution_sec"] = (int(exit_t) - open_ms) // 1000
                                        except Exception:
                                            pass
                                    log_dirty = True
                                    resolved_this_run.append((sym, exit_status, pnl_usd))
                                    oco_str = f"{'🟢' if exit_status=='TP_HIT' else '🔴'} {exit_status}"
                                    self._eager_commit(trade)
                                    break
    
                            if trade.get("exit_status") != "OPEN":
                                continue
                    except Exception as e:
                        # The whole OCO/child query must complete before protection
                        # can be treated as confirmed for downstream price guards.
                        _oco_query_confirmed = False
                        err_str = str(e)
                        # ── Step 3a: OCO Protection Reconciliation ─────────
                        # -2018: order list does not exist (purged / testnet reset)
                        # -2013: child order does not exist
                        # Either means oco_placed=True in DB but exchange has no record.
                        # SAFE RULE: order-not-found = MISSING PROTECTION.
                        # Do NOT infer SL executed — no fill, no sold balance confirmed.
                        _is_missing = _is_confirmed_missing_oco_error(e)
                        if _is_missing:
                            # OCO order-not-found: protection is missing.
                            # Do NOT set state or send alert here.
                            # Final state (UNPROTECTED vs UNPROTECTED_SL_BREACH) is
                            # determined below in Step 3.5 after fetching current price.
                            # This prevents the in-memory UNPROTECTED interim value from
                            # causing a spurious alert on every cycle.
                            _oco_missing = True
                            oco_str = "⚠ UNPROTECTED"
                            print(
                                f"  ⚠  [{sym}] OCO missing on exchange (order-not-found). "
                                f"oco_list_id={trade.get('oco_list_id')} error: {err_str[:60]}"
                            )
                        else:
                            # Unknown is not missing. Preserve OCO IDs/placement data,
                            # record the query uncertainty, and forbid recovery/guards.
                            if trade.get("oco_reconciliation_status") != "RECONCILIATION_REQUIRED":
                                trade["oco_reconciliation_status"] = "RECONCILIATION_REQUIRED"
                                log_dirty = True
                            oco_str = f"⚠ {err_str[:30]}"
    
                if not repo.should_show_live_position(trade, entry_status):
                    continue
    
                # ── Step 4: Compact line + optional verbose card ────────────
                status_str = _fmt_order_status(entry_status)[:20]
                try:
                    current = price_map.get(sym)
                    if current is None:
                        current = float(client.get_symbol_ticker(symbol=sym)["price"])
                except Exception:
                    current = None
    
                # ── Step 3.5: Final-state OCO reconciliation + SL breach handling ──
                # Derive one final state from OCO presence and current price.
                # Compare it with the state loaded from Supabase before sending
                # or persisting, so an invocation never emits an interim state.

                if (entry_status == "FILLED"
                        and trade.get("exit_status") == "OPEN"
                        and current is not None):
                    sl_level    = trade.get("sl")
                    tp_level    = trade.get("tp1")
                    sl_breached = sl_level is not None and current <= float(sl_level)
                    tp_breached = tp_level is not None and current >= float(tp_level)

                    if _oco_missing and sl_breached:
                        # OCO missing + price below SL → emergency close immediately.
                        # This path does NOT require --recover-unprotected; it is
                        # unconditional because the risk contract has already been
                        # violated and the asset must be sold.
                        if prev_recon != "UNPROTECTED_SL_BREACH":
                            trade["oco_reconciliation_status"] = "UNPROTECTED_SL_BREACH"
                            log_dirty = True
                        print(
                            f"  🚨 [{sym}] UNPROTECTED_SL_BREACH: "
                            f"price {current:.6f} below SL {sl_level:.6f}. "
                            f"Executing emergency close."
                        )
                        if self._emergency_close(
                                trade, current, resolved_this_run,
                                exit_reason="UNPROTECTED_SL_BREACH"):
                            log_dirty = True
                            oco_str = "🔴 EMERGENCY_CLOSED"
                            continue
                        # _emergency_close() failed (balance check or sell error).
                        # Fall back to alert so the operator can act manually.
                        entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                        est_pnl    = trade.get("entry_qty", 0) * (current - entry_fill)
                        if prev_recon != "UNPROTECTED_SL_BREACH":
                            _send_telegram(
                                f"🚨 [SPOT] UNPROTECTED SL BREACH: {sym}\n"
                                f"Price {ca._fmt_price(current).strip()} below SL "
                                f"{ca._fmt_price(trade.get('sl')).strip()}.\n"
                                f"OCO MISSING — emergency close FAILED.\n"
                                f"Est. unrealized loss: ${est_pnl:+.4f} USDT\n"
                                f"Manual intervention required immediately."
                            )
                        oco_str = "🚨 UNPROTECTED_SL_BREACH"

                    elif _oco_missing and tp_breached:
                        print(
                            f"  🚨 [{sym}] UNPROTECTED_TP_BREACH: "
                            f"price {current:.6f} reached TP {tp_level:.6f}. "
                            f"Executing emergency close."
                        )
                        if self._emergency_close(
                                trade, current, resolved_this_run,
                                exit_reason="UNPROTECTED_TP_BREACH"):
                            log_dirty = True
                            oco_str = "🟢 EMERGENCY_CLOSED"
                            continue
                        if prev_recon != "UNPROTECTED_TP_BREACH":
                            trade["oco_reconciliation_status"] = "UNPROTECTED_TP_BREACH"
                            log_dirty = True
                            _send_telegram(
                                f"🚨 [SPOT] UNPROTECTED TP BREACH: {sym}\n"
                                f"Price {ca._fmt_price(current).strip()} reached TP "
                                f"{ca._fmt_price(tp_level).strip()}.\n"
                                f"OCO MISSING — emergency close FAILED.\n"
                                f"Manual intervention required immediately."
                            )
                        oco_str = "🚨 UNPROTECTED_TP_BREACH"

                    elif _oco_missing:
                        # Missing protection is always recovered while the original
                        # contract remains live; this is no longer opt-in.
                        if self._recover_missing_oco(
                                trade, current, resolved_this_run):
                            log_dirty = True
                            oco_str = "✅ RECOVERED"
                            continue
                        # Final state: OCO missing but price still above SL
                        final_state = "UNPROTECTED"
                        print(
                            f"  ⚠  [{sym}] UNPROTECTED: OCO missing, price above SL."
                        )
                        if prev_recon != final_state:
                            trade["oco_reconciliation_status"] = final_state
                            log_dirty = True
                            cur_disp = ca._fmt_price(price_map.get(sym)).strip() if price_map.get(sym) else "?"
                            _send_telegram(
                                f"🚨 [SPOT] UNPROTECTED POSITION: {sym}\n"
                                f"OCO {trade.get('oco_list_id')} not found on exchange.\n"
                                f"Cause: testnet reset / OCO purged.\n"
                                f"SL: {ca._fmt_price(trade.get('sl')).strip()}  Current: {cur_disp}\n"
                                f"Asset still in wallet — NOT auto-sold.\n"
                                f"Manual intervention required."
                            )
                        oco_str = "⚠ UNPROTECTED"

                    elif (sl_breached and _oco_query_confirmed
                          and not _oco_missing and trade.get("oco_placed")):
                        # OCO exists on exchange (confirmed in Step 3) but didn't fire.
                        # Price-guard: safe to resolve as SL_HIT.
                        print(
                            f"  ⚠  [{sym}] Price {current:.4f} breached SL {sl_level:.4f} "
                            f"— OCO confirmed placed but not triggered. Resolving as SL_HIT."
                        )
                        entry_fill   = trade.get("entry_fill_price") or trade["entry_price"]
                        qty          = trade.get("entry_qty", 0)
                        pnl_usd      = qty * (current - entry_fill)
                        pnl_pct      = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                        exit_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                        trade["exit_status"]      = "SL_HIT"
                        trade["exit_price"]       = round(current, 6)
                        trade["exit_time"]        = exit_time_ms
                        trade["exit_reason"]      = "SL_HIT"
                        trade["realized_pnl_usd"] = round(pnl_usd, 4)
                        trade["realized_pnl_pct"] = round(pnl_pct, 2)
                        if trade.get("entry_fill_time") and exit_time_ms:
                            trade["time_to_resolution_sec"] = (
                                exit_time_ms - int(trade["entry_fill_time"])
                            ) // 1000
                        log_dirty = True
                        resolved_this_run.append((sym, "SL_HIT", pnl_usd))
                        self._eager_commit(trade)
                        _send_telegram(
                            f"🛑 [SPOT] SL_HIT (price-guard, OCO confirmed): "
                            f"{sym} @ {ca._fmt_price(current).strip()}"
                            f"  |  PnL: ${pnl_usd:+.2f}"
                        )
                        continue

                    elif (sl_breached
                          and not trade.get("oco_placed")
                          and prev_recon in ("TP_ONLY", "SL_ONLY", "UNPROTECTED")):
                        # ── Partial-protection SL breach: price guard for states
                        # without a full OCO list.
                        #
                        # TP_ONLY: standalone LIMIT_MAKER at TP is live, but SL had
                        #   no protection.  Price has now dropped to/below SL.
                        # SL_ONLY: standalone STOP_LOSS_LIMIT exists but may not have
                        #   triggered yet (e.g. exchange lag or testnet quirk).
                        # UNPROTECTED: no protection at all.
                        #
                        # In all three cases, SL being breached means the risk
                        # contract is violated and we must close immediately.
                        # Use _emergency_close() which also checks balance first.
                        if prev_recon != "UNPROTECTED_SL_BREACH":
                            trade["oco_reconciliation_status"] = "UNPROTECTED_SL_BREACH"
                            log_dirty = True
                        print(
                            f"  🚨 [{sym}] SL BREACH on {prev_recon}: "
                            f"price {current:.6f} ≤ SL {sl_level:.6f}. "
                            f"Executing emergency close."
                        )
                        if self._emergency_close(
                                trade, current, resolved_this_run,
                                exit_reason="UNPROTECTED_SL_BREACH"):
                            log_dirty = True
                            oco_str = "🔴 EMERGENCY_CLOSED"
                            continue
                        # _emergency_close() failed — alert and fall through
                        entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
                        est_pnl    = trade.get("entry_qty", 0) * (current - entry_fill)
                        if prev_recon != "UNPROTECTED_SL_BREACH":
                            _send_telegram(
                                f"🚨 [SPOT] SL BREACH ({prev_recon}): {sym}\n"
                                f"Price {ca._fmt_price(current).strip()} below SL "
                                f"{ca._fmt_price(trade.get('sl')).strip()}.\n"
                                f"Emergency close FAILED — manual intervention required.\n"
                                f"Est. loss: ${est_pnl:+.4f} USDT"
                            )
                        oco_str = "🚨 UNPROTECTED_SL_BREACH"
    
                # Compact view: show PnL only for filled positions;
                # for pending orders show distance to entry instead (more useful than "n/a")
                pnl_display = "n/a"
                if entry_status == "FILLED" and trade.get("entry_qty", 0) > 0 and current is not None:
                    ref_price = trade.get("entry_fill_price") or trade["entry_price"]
                    qty = trade.get("entry_qty", 0)
                    pnl_usd = qty * (current - ref_price)
                    pnl_display = f"${pnl_usd:+.3f}"
                elif entry_status in ("NEW", "PARTIALLY_FILLED") and current is not None:
                    entry_limit = trade.get("entry_price")
                    if entry_limit and current:
                        dist_pct = (entry_limit - current) / current * 100
                        # Positive dist_pct = entry is above current (limit BUY waiting for pullback)
                        pnl_display = f"{dist_pct:+.2f}% fill"
    
                if not verbose:
                    print(f"  {sym:<10} {status_str:<22} {pnl_display:>12}  {oco_str}")
    
                if verbose:
                    # Verbose: spacious, aligned info card
                    sym_hdr = f"{sym}  {dirn}"
                    entry_price = trade.get("entry_price")
                    entry_fill = trade.get("entry_fill_price")
                    sl = trade.get("sl")
                    tp = trade.get("tp1")
                    qty = trade.get("entry_qty") or 0
    
                    cur_str = ca._fmt_price(current, width=14).strip() if current is not None else "n/a"
                    entry_str = ca._fmt_price(entry_fill or entry_price, width=14).strip() if (entry_fill or entry_price) is not None else "n/a"
                    sl_str = ca._fmt_price(sl, width=14).strip() if sl is not None else "n/a"
                    tp_str = ca._fmt_price(tp, width=14).strip() if tp is not None else "n/a"
    
                    def pct(a, b):
                        try:
                            return (a - b) / b * 100
                        except Exception:
                            return None
    
                    if current is not None:
                        pct_to_entry = pct(entry_price, current)
                        pct_sl = pct(sl, current) if sl else None
                        pct_tp = pct(tp, current) if tp else None
                    else:
                        pct_to_entry = pct_sl = pct_tp = None
    
                    W = 78
                    print("\n  " + "╔" + "═" * (W - 2) + "╗")
                    print(f"  ║ {sym_hdr:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                    # Conditional content based on order status
                    if entry_status in ("NEW", "PARTIALLY_FILLED"):
                        # Show only the 'to fill' line
                        price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                        if pct_to_entry is not None:
                            price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                        print(f"  ║ {price_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                    elif entry_status == "FILLED":
                        # Show Entry + Current, then SL / TP distances
                        entrycur_line = f"Entry: {entry_str:>12}   |  Current: {cur_str:>12}"
                        # Fix 3: warn if current price equals entry_fill_price after FILLED —
                        # this usually means the testnet price is stale or fill_price fallback fired.
                        _entry_ref = entry_fill or entry_price
                        _price_unchanged = (
                            current is not None
                            and _entry_ref is not None
                            and abs(current - _entry_ref) < 1e-9
                        )
                        if _price_unchanged:
                            entrycur_line += "  ⚠ stale?"
                        print(f"  ║ {entrycur_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                        sltp_line = f"SL: {sl_str:>12}"
                        if pct_sl is not None:
                            sltp_line += f" ({pct_sl:+.2f}%)"
                        sltp_line = sltp_line.ljust(38)
                        sltp_line += f"  |  TP: {tp_str:>12}"
                        if pct_tp is not None:
                            sltp_line += f" ({pct_tp:+.2f}%)"
                        print(f"  ║ {sltp_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                    else:
                        # Fallback: show both lines
                        price_line = f"Current: {cur_str:>12}   →   Entry: {entry_str:>12}"
                        if pct_to_entry is not None:
                            price_line += f"   ({pct_to_entry:+.2f}% to fill)"
                        print(f"  ║ {price_line:<{W-4}} ║")
                        print(f"  ║{'':{W-2}}║")
                        sltp_line = f"SL: {sl_str:>12}"
                        if pct_sl is not None:
                            sltp_line += f" ({pct_sl:+.2f}%)"
                        sltp_line = sltp_line.ljust(38)
                        sltp_line += f"  |  TP: {tp_str:>12}"
                        if pct_tp is not None:
                            sltp_line += f" ({pct_tp:+.2f}%)"
                        print(f"  ║ {sltp_line:<{W-4}} ║")
                    print(f"  ║{'':{W-2}}║")
                    status_line = f"Status: {_fmt_order_status(entry_status)}"
                    if entry_status == 'FILLED' and qty:
                        r_pnl = trade.get('realized_pnl_usd')
                        if r_pnl is not None:
                            status_line += f"  |  Realized: ${r_pnl:+.4f}"
                        if current is not None and qty:
                            ref = trade.get('entry_fill_price') or trade.get('entry_price')
                            unreal = qty * (current - ref)
                            status_line += f"  |  Unreal: ${unreal:+.3f}"
                    if trade.get('oco_list_id'):
                        status_line += f"  |  OCO List: {trade['oco_list_id']}"
                    print(f"  ║ {status_line:<{W-4}} ║")
                    print("  " + "╚" + "═" * (W - 2) + "╝\n")
    
        # ── Summary of what changed this run ───────────────────────────────
        if resolved_this_run:
            print(f"\n  ── Resolved this run: {len(resolved_this_run)} trade(s) ──")
            for sym, status, pnl in resolved_this_run:
                icon = "🟢" if status == "TP_HIT" else "🔴"
                print(f"    {icon} {sym}  {status}  PnL: ${pnl:+.4f}")
    
            # ── Telegram notif ──────────────────────────────────────────────
            for sym, status, pnl in resolved_this_run:
                icon = "🟢" if status == "TP_HIT" else "🔴"
                emoji_label = "TP HIT" if status == "TP_HIT" else "SL HIT"
                _send_telegram(
                    f"{icon} {emoji_label}: {sym} "
                    f"{'+'if pnl>=0 else ''}{pnl:.4f} USD\n"
                    f"(detected via --check-positions)"
                )
    
        # ── Save updates ───────────────────────────────────────────────────
        if log_dirty:
            from services.supabase_client import update_spot_by_order_id
            for ot in open_trades:
                eid = ot.get("entry_order_id")
                if not eid:
                    continue
                update_spot_by_order_id(eid, {
                    "entry_status":               ot.get("entry_status"),
                    "entry_fill_price":           ot.get("entry_fill_price"),
                    "entry_fill_time":            ot.get("entry_fill_time"),
                    "entry_qty":                  ot.get("entry_qty"),
                    "slippage_pct":               ot.get("slippage_pct"),
                    "oco_placed":                 ot.get("oco_placed"),
                    "oco_order_ids":              ot.get("oco_order_ids"),
                    "oco_list_id":                ot.get("oco_list_id"),
                    "tp1":                        ot.get("tp1"),
                    "exit_status":                ot.get("exit_status"),
                    "exit_price":                 ot.get("exit_price"),
                    "exit_time":                  ot.get("exit_time"),
                    "exit_reason":                ot.get("exit_reason"),
                    "realized_pnl_usd":           ot.get("realized_pnl_usd"),
                    "realized_pnl_pct":           ot.get("realized_pnl_pct"),
                    "time_to_resolution_sec":     ot.get("time_to_resolution_sec"),
                    "raw_entry_order":            ot.get("raw_entry_order"),
                })
                # Persist oco_reconciliation_status separately — requires
                # the column to exist in trades_spot (see docs/migrations/).
                # Safe to skip if column not yet present; in-memory state is
                # still correct for this cycle.
                recon = ot.get("oco_reconciliation_status")
                if recon:
                    try:
                        update_spot_by_order_id(eid, {"oco_reconciliation_status": recon})
                    except Exception:
                        pass  # column not yet migrated — silently skip
    
        if not verbose and len(open_trades) > 1:
            print(f"\n  ℹ️  Use --verbose for detailed per-position cards.")
        print("\n  Run --check-positions again to refresh.")
        print("  To manually close: testnet.binance.vision → spot trading → cancel OCO")

    @staticmethod
    def _eager_commit(trade: dict) -> None:
        """
        Write the resolved exit fields to Supabase immediately after a trade
        is closed in-memory, before the end-of-cycle save block runs.

        Purpose: prevent a concurrent --check-positions invocation from
        seeing exit_status == "OPEN" for a trade that was just resolved,
        which would cause a duplicate Telegram alert and a double-resolve.

        The full field update in the save block at the end of check_positions()
        is idempotent and will overwrite these same values — this is fine.
        Non-fatal: if Supabase is unreachable, the end-of-cycle save will
        still persist everything.
        """
        eid = trade.get("entry_order_id")
        if not eid:
            return
        try:
            from services.supabase_client import update_spot_by_order_id as _usb
            _usb(eid, {
                "exit_status":            trade.get("exit_status"),
                "exit_price":             trade.get("exit_price"),
                "exit_time":              trade.get("exit_time"),
                "exit_reason":            trade.get("exit_reason"),
                "realized_pnl_usd":       trade.get("realized_pnl_usd"),
                "realized_pnl_pct":       trade.get("realized_pnl_pct"),
                "time_to_resolution_sec": trade.get("time_to_resolution_sec"),
            })
        except Exception:
            pass  # non-fatal — full save follows at end of check_positions()

    def _emergency_close(self, trade: dict, current: float,
                          resolved_this_run: list,
                          exit_reason: str = "UNPROTECTED_SL_BREACH") -> bool:
        """
        Close a position whose missing OCO has crossed its stored SL or TP.

        This is the last-resort enforcement of the risk contract.  It MUST
        NOT be gated behind --recover-unprotected; it fires automatically
        whenever either unprotected boundary breach is detected.

        Design:
        - Idempotency guard: if exit_status != "OPEN" (already closed by a
          concurrent path or a previous cycle), return True silently.
        - Reuses the spot executor's close_position() market-sell path.
        - Records the supplied infrastructure-specific exit reason.
        - On exchange or balance failure: returns False so the caller can
          fall back to a manual-intervention alert.

        Returns True if the position was successfully closed.
        """
        sym = trade["symbol"]

        # ── Idempotency guard ─────────────────────────────────────────
        if trade.get("exit_status") != "OPEN":
            # Position already closed by a concurrent path — do not sell again.
            return True

        qty   = float(trade.get("entry_qty") or 0)
        asset = sym.replace("USDT", "").replace("BUSD", "").replace("BTC", "")\
                    .replace("ETH", "").replace("BNB", "")
        # More robust: strip known quote assets from right side
        for quote in ("USDT", "BUSD", "BTC", "ETH", "BNB"):
            if sym.upper().endswith(quote):
                asset = sym.upper()[:-len(quote)]
                break
        exit_status = "TP_HIT" if exit_reason == "UNPROTECTED_TP_BREACH" else "SL_HIT"

        if qty <= 0:
            print(f"  ⚠  [{sym}] Emergency close skipped: entry_qty={qty} — cannot size sell.")
            return False

        # ── Balance confirmation before selling ───────────────────────
        # Prevents a double-sell if another path already sold the asset.
        try:
            balance  = self.client.get_asset_balance(asset=asset) or {}
            free_qty = float(balance.get("free") or 0)
        except Exception as exc:
            print(f"  ⚠  [{sym}] Emergency close: balance check failed — {exc}")
            return False

        if free_qty + 1e-12 < qty:
            # Asset no longer free → position was already closed externally.
            # Mark as closed so downstream stops processing this trade.
            print(
                f"  ℹ  [{sym}] Emergency close: free {asset}={free_qty:.8f} < "
                f"required {qty:.8f}. Position already closed externally — "
                f"updating state only."
            )
            entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
            pnl_usd    = qty * (current - entry_fill)
            pnl_pct    = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
            exit_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
            trade.update({
                "exit_status":      exit_status,
                "exit_price":       round(current, 6),
                "exit_time":        exit_ms,
                "exit_reason":      exit_reason,
                "realized_pnl_usd": round(pnl_usd, 4),
                "realized_pnl_pct": round(pnl_pct, 2),
                "oco_placed":       False,
                "oco_list_id":      None,
                "oco_reconciliation_status": "EMERGENCY_CLOSED",
            })
            fill_ts = trade.get("entry_fill_time")
            if fill_ts:
                trade["time_to_resolution_sec"] = (exit_ms - int(fill_ts)) // 1000
            resolved_this_run.append((sym, exit_status, pnl_usd))
            self._eager_commit(trade)
            _send_telegram(
                f"🛑 [SPOT] EMERGENCY_CLOSED (externally): {sym}\n"
                f"Price {ca._fmt_price(current).strip()} crossed the stored "
                f"{exit_status} contract.\n"
                f"Asset already sold — state updated.\n"
                f"PnL: ${pnl_usd:+.4f} USDT"
            )
            return True

        # ── Issue market sell via the existing spot order executor ────
        try:
            resp = self.order_executor.close_position(trade)
        except RuntimeError as exc:
            print(f"  ⚠  [{sym}] Emergency close: market sell failed — {exc}")
            return False

        # ── Market sell succeeded — resolve the trade ─────────────────
        exit_px    = None
        fills      = resp.get("fills", []) if resp else []
        if fills:
            exit_px = float(fills[0].get("price", 0) or 0)
        if not exit_px and resp:
            exec_qty  = float(resp.get("executedQty", 0) or 0)
            cum_quote = float(resp.get("cummulativeQuoteQty", 0) or 0)
            exit_px   = cum_quote / exec_qty if exec_qty > 0 else None
        exit_px = exit_px or current   # conservative fallback

        entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
        pnl_usd    = (exit_px - entry_fill) * qty
        pnl_pct    = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
        exit_ms    = int(
            resp.get("transactTime") or resp.get("updateTime")
            or datetime.now(timezone.utc).timestamp() * 1000
        ) if resp else int(datetime.now(timezone.utc).timestamp() * 1000)

        trade.update({
            "exit_status":      exit_status,
            "exit_price":       round(exit_px, 6),
            "exit_time":        exit_ms,
            "exit_reason":      exit_reason,
            "realized_pnl_usd": round(pnl_usd, 4),
            "realized_pnl_pct": round(pnl_pct, 2),
            "oco_placed":       False,
            "oco_list_id":      None,
            "oco_reconciliation_status": "EMERGENCY_CLOSED",
        })
        fill_ts = trade.get("entry_fill_time")
        if fill_ts:
            trade["time_to_resolution_sec"] = (exit_ms - int(fill_ts)) // 1000

        resolved_this_run.append((sym, exit_status, pnl_usd))
        self._eager_commit(trade)
        print(
            f"  🔴 [{sym}] EMERGENCY_CLOSED: market sold @ {ca._fmt_price(exit_px).strip()}  "
            f"PnL: ${pnl_usd:+.4f}"
        )
        _send_telegram(
            f"🛑 [SPOT] EMERGENCY_CLOSED: {sym}\n"
            f"OCO was missing. Market sold @ {ca._fmt_price(exit_px).strip()}.\n"
            f"SL was {ca._fmt_price(trade.get('sl')).strip()}  "
            f"Exit: {ca._fmt_price(exit_px).strip()}\n"
            f"PnL: ${pnl_usd:+.4f} USDT  |  exit_reason: {exit_reason}"
        )
        return True

    def _recover_missing_oco(self, trade: dict, current: float,
                             resolved_this_run: list) -> bool:
        """Restore missing protection only after confirming the asset is free."""
        sym = trade["symbol"]
        qty = float(trade.get("entry_qty") or 0)
        asset = sym[:-4] if sym.endswith("USDT") else sym

        try:
            balance = self.client.get_asset_balance(asset=asset) or {}
            free_qty = float(balance.get("free") or 0)
        except Exception as exc:
            print(f"  ⚠  [{sym}] Recovery skipped: balance check failed: {exc}")
            return False

        if qty <= 0 or free_qty + 1e-12 < qty:
            print(
                f"  ⚠  [{sym}] Recovery skipped: free {asset}={free_qty:.8f}, "
                f"required={qty:.8f}."
            )
            return False

        try:
            prot_result = self.order_executor.place_oco_order(trade)
        except RuntimeError as exc:
            print(f"  ⚠  [{sym}] Recovery failed: {exc}")
            return False

        if prot_result is None:
            # place_oco_order returned None — treat as failed recovery
            trade["oco_reconciliation_status"] = "UNPROTECTED"
            print(f"  ⚠  [{sym}] Recovery failed: place_oco_order returned None")
            return False

        # place_oco_order now returns a structured dict; unwrap it.
        # Guard against legacy raw-dict returns (e.g. from mocks) and None.
        if isinstance(prot_result, dict) and "protection_state" not in prot_result:
            prot_result = {
                "protection_state": "FULLY_PROTECTED",
                "oco_resp": prot_result,
                "tp_order_id": None,
                "sl_order_id": None,
                "filter_reason": None,
            }
        response       = prot_result.get("oco_resp") if isinstance(prot_result, dict) else prot_result
        protection_state = prot_result.get("protection_state", "UNPROTECTED") if isinstance(prot_result, dict) else "FULLY_PROTECTED"
        _market_sold   = (prot_result.get("_market_sold") if isinstance(prot_result, dict) else False) or trade.pop("_market_sold", False)

        if _market_sold:
            exit_px = None
            fills = response.get("fills", []) if response else []
            if fills:
                exit_px = float(fills[0].get("price", 0) or 0)
            if not exit_px and response:
                exec_qty = float(response.get("executedQty", 0) or 0)
                quote = float(response.get("cummulativeQuoteQty", 0) or 0)
                exit_px = quote / exec_qty if exec_qty > 0 else None
            exit_px = exit_px or current
            entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
            pnl_usd = (exit_px - entry_fill) * qty
            pnl_pct = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
            exit_ts = (response or {}).get("transactTime") or int(
                datetime.now(timezone.utc).timestamp() * 1000
            )
            trade.update({
                "exit_status": "SL_HIT",
                "exit_price": round(exit_px, 6),
                "exit_time": int(exit_ts),
                "exit_reason": "UNPROTECTED_SL_BREACH",
                "realized_pnl_usd": round(pnl_usd, 4),
                "realized_pnl_pct": round(pnl_pct, 2),
                "oco_placed": False,
                "oco_order_ids": None,
                "oco_list_id": None,
                "oco_reconciliation_status": "RECOVERED_SL_HIT",
            })
            fill_ts = trade.get("entry_fill_time")
            if fill_ts:
                trade["time_to_resolution_sec"] = (
                    int(exit_ts) - int(fill_ts)
                ) // 1000
            resolved_this_run.append((sym, "SL_HIT", pnl_usd))
            self._eager_commit(trade)
            print(f"  ✅ [{sym}] Reset recovery: market sold and logged SL_HIT.")
            return True

        reports = response.get("orderReports", []) if response else []
        list_id = response.get("orderListId") if response else None

        # If recovery returned a partial-protection state, record it and return True
        # so the caller marks this cycle as "handled" (no more UNPROTECTED alerts).
        if protection_state == "TP_ONLY":
            tp_order_id = prot_result.get("tp_order_id") if isinstance(prot_result, dict) else None
            trade.update({
                "oco_placed": False,
                "oco_list_id": None,
                "tp_order_id": tp_order_id,
                "oco_reconciliation_status": "TP_ONLY",
            })
            _send_telegram(
                f"🟡 [SPOT] PARTIAL RECOVERY (TP_ONLY): {sym}\n"
                f"TP order placed (orderId={tp_order_id}). "
                f"SL {ca._fmt_price(trade.get('sl')).strip()} still filter-invalid.\n"
                f"Will attempt full OCO next cycle."
            )
            print(f"  🟡 [{sym}] Recovery: TP_ONLY (orderId={tp_order_id})")
            return True

        if protection_state == "SL_ONLY":
            sl_order_id = prot_result.get("sl_order_id") if isinstance(prot_result, dict) else None
            trade.update({
                "oco_placed": False,
                "oco_list_id": None,
                "sl_order_id": sl_order_id,
                "oco_reconciliation_status": "SL_ONLY",
            })
            _send_telegram(
                f"🟠 [SPOT] PARTIAL RECOVERY (SL_ONLY): {sym}\n"
                f"SL order placed (orderId={sl_order_id}). "
                f"TP {ca._fmt_price(trade.get('tp1')).strip()} still filter-invalid.\n"
                f"Will attempt full OCO next cycle."
            )
            print(f"  🟠 [{sym}] Recovery: SL_ONLY (orderId={sl_order_id})")
            return True

        if protection_state == "UNPROTECTED" and not _market_sold:
            # Recovery attempted but both legs still filter-invalid
            trade["oco_reconciliation_status"] = "UNPROTECTED"
            print(f"  ⚠  [{sym}] Recovery: still UNPROTECTED (both legs filter-invalid)")
            return False

        # FULLY_PROTECTED path: OCO list placed
        if not list_id:
            print(f"  ⚠  [{sym}] Recovery failed: exchange returned no OCO list id.")
            return False

        trade.update({
            "oco_placed": True,
            "oco_order_ids": [row["orderId"] for row in reports],
            "oco_list_id": list_id,
            "oco_reconciliation_status": "FULLY_PROTECTED",
        })
        _send_telegram(
            f"🛡️ [SPOT] OCO RECOVERED (FULLY_PROTECTED): {sym}\n"
            f"New OCO: {list_id}\n"
            f"SL: {ca._fmt_price(trade.get('sl')).strip()}  "
            f"TP: {ca._fmt_price(trade.get('tp1')).strip()}"
        )
        print(f"  ✅ [{sym}] Reset recovery: OCO restored List#{list_id}.")
        return True
    
    
    def _recover_partial_protection(
        self,
        trade: dict,
        eid: int,
        log_dirty: bool,
        resolved_this_run: list,
    ) -> None:
        """
        Attempt to upgrade a TP_ONLY / SL_ONLY / UNPROTECTED position to
        FULLY_PROTECTED on the next monitoring cycle.

        Design:
        - Called from Step 2b when the trade already has a partial-protection state
          from a previous cycle and oco_placed is still False.
        - Calls place_oco_order again; if conditions have improved (filter cleared,
          price moved back inside the band) it will succeed.
        - On success: updates trade in-place and persists to Supabase immediately.
        - On failure (still partial or still unprotected): updates the state so the
          dashboard shows the current reality but does NOT re-send Telegram alerts
          if the state is unchanged (idempotent alert guard).
        - Never shifts SL or TP values.

        Cancel-before-replace protocol (Bug 1+2 fix):
        - For TP_ONLY: before calling place_oco_order(), query the exchange for
          trade["tp_order_id"].
            * If FILLED → position exited at TP; resolve as TP_HIT immediately.
            * If OPEN/NEW/PARTIALLY_FILLED → cancel it first, then proceed with
              place_oco_order() so we don't create a duplicate SELL.
            * If NOT_FOUND or already CANCELED → proceed directly (safe).
        - Same logic for SL_ONLY using trade["sl_order_id"].
        - This guarantees at most one open SELL order for the position at any time.
        """
        sym  = trade["symbol"]
        prev = trade.get("oco_reconciliation_status")

        # ── Cancel-before-replace: ensure no duplicate SELL order ─────────────
        # For TP_ONLY and SL_ONLY states, a standalone order from a prior cycle
        # may still be open on the exchange.  We MUST cancel it before placing any
        # new order (OCO or standalone), otherwise we end up with two simultaneous
        # SELL orders for the same position.
        _standalone_order_id = None
        if prev == "TP_ONLY":
            _standalone_order_id = trade.get("tp_order_id")
        elif prev == "SL_ONLY":
            _standalone_order_id = trade.get("sl_order_id")

        if _standalone_order_id:
            try:
                existing = self.client.get_order(
                    symbol=sym, orderId=_standalone_order_id
                )
                existing_status = str(existing.get("status", "")).upper()

                if existing_status == "FILLED":
                    # Standalone order already filled — this is a TP_HIT (or SL_HIT
                    # for SL_ONLY).  Resolve the trade now; no further order needed.
                    exec_qty  = float(existing.get("executedQty", 0) or 1)
                    cum_quote = float(existing.get("cummulativeQuoteQty", 0) or 0)
                    exit_px   = cum_quote / exec_qty if (exec_qty > 0 and cum_quote > 0) \
                                else float(existing.get("price", trade.get("tp1", 0)))
                    exit_status = "SL_HIT" if prev == "SL_ONLY" else "TP_HIT"
                    entry_fill  = trade.get("entry_fill_price") or trade["entry_price"]
                    pnl_usd     = (exit_px - entry_fill) * float(trade.get("entry_qty", 0))
                    pnl_pct     = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
                    exit_ts     = int(
                        existing.get("updateTime")
                        or datetime.now(timezone.utc).timestamp() * 1000
                    )
                    trade.update({
                        "exit_status":      exit_status,
                        "exit_price":       round(exit_px, 6),
                        "exit_time":        exit_ts,
                        "exit_reason":      exit_status,
                        "realized_pnl_usd": round(pnl_usd, 4),
                        "realized_pnl_pct": round(pnl_pct, 2),
                        "oco_placed":       False,
                        "oco_list_id":      None,
                        "oco_reconciliation_status": "EMERGENCY_CLOSED",
                    })
                    fill_t = trade.get("entry_fill_time")
                    if fill_t:
                        trade["time_to_resolution_sec"] = (exit_ts - int(fill_t)) // 1000
                    resolved_this_run.append((sym, exit_status, pnl_usd))
                    self._eager_commit(trade)
                    icon = "🟢" if exit_status == "TP_HIT" else "🔴"
                    print(
                        f"  {sym:<10} {icon} Standalone {prev} filled @ "
                        f"{ca._fmt_price(exit_px).strip()} — resolved as {exit_status}  "
                        f"PnL: ${pnl_usd:+.4f}"
                    )
                    _send_telegram(
                        f"{icon} [SPOT] {exit_status} (standalone {prev}): {sym}\n"
                        f"Exit @ {ca._fmt_price(exit_px).strip()}  "
                        f"PnL: ${pnl_usd:+.4f} USDT"
                    )
                    return

                elif existing_status in ("NEW", "PARTIALLY_FILLED"):
                    # Cancel the open standalone order before placing a new one.
                    print(
                        f"  {sym:<10} ℹ Cancelling existing {prev} order "
                        f"(orderId={_standalone_order_id}) before recovery attempt."
                    )
                    try:
                        self.order_executor.cancel_order(sym, _standalone_order_id)
                        print(f"  {sym:<10} ✅ Cancelled orderId={_standalone_order_id}")
                    except RuntimeError as cancel_exc:
                        # Cancel failed — abort recovery to avoid duplicate SELL risk.
                        print(
                            f"  ⚠  [{sym}] Recovery aborted: cancel of existing "
                            f"{prev} order failed: {cancel_exc}"
                        )
                        return
                # CANCELED, EXPIRED, NOT_FOUND → safe to proceed with new order

            except Exception as query_exc:
                err_str = str(query_exc)
                # -2013 / Order does not exist → standalone order is gone; safe to proceed
                if "-2013" in err_str or "Order does not exist" in err_str:
                    print(
                        f"  {sym:<10} ℹ {prev} order {_standalone_order_id} "
                        f"not found on exchange — proceeding with fresh placement."
                    )
                else:
                    # Unknown query error — abort to stay safe
                    print(
                        f"  ⚠  [{sym}] Recovery aborted: could not query {prev} "
                        f"order {_standalone_order_id}: {query_exc}"
                    )
                    return

        try:
            prot_result = self.order_executor.place_oco_order(trade)
        except RuntimeError as exc:
            print(f"  ⚠  [{sym}] Partial-protection recovery failed: {exc}")
            return

        if prot_result is None:
            return

        # Backward-compat: normalize raw exchange dict to structured format
        if isinstance(prot_result, dict) and "protection_state" not in prot_result:
            prot_result = {
                "protection_state": "FULLY_PROTECTED",
                "oco_resp": prot_result,
                "tp_order_id": None,
                "sl_order_id": None,
                "filter_reason": None,
            }
        protection_state = prot_result.get("protection_state", "UNPROTECTED")
        _market_sold     = prot_result.get("_market_sold") or trade.pop("_market_sold", False)

        if _market_sold:
            # Emergency market sell — price dropped to/past SL during recovery attempt
            raw_resp   = prot_result.get("oco_resp") or {}
            entry_fill = trade.get("entry_fill_price") or trade["entry_price"]
            exit_px    = None
            fills = raw_resp.get("fills", [])
            if fills:
                exit_px = float(fills[0].get("price", 0) or 0)
            if not exit_px and raw_resp:
                exec_qty  = float(raw_resp.get("executedQty", 0) or 0)
                cum_quote = float(raw_resp.get("cummulativeQuoteQty", 0) or 0)
                exit_px   = cum_quote / exec_qty if exec_qty > 0 else None
            exit_px = exit_px or trade["sl"]
            pnl_usd = (exit_px - entry_fill) * float(trade.get("entry_qty", 0))
            pnl_pct = pnl_usd / max(trade.get("entry_notional", 1), 0.001) * 100
            exit_ts = int(
                raw_resp.get("transactTime")
                or raw_resp.get("updateTime")
                or datetime.now(timezone.utc).timestamp() * 1000
            )
            trade.update({
                "exit_status": "SL_HIT", "exit_price": round(exit_px, 6),
                "exit_time": exit_ts, "exit_reason": "SL_HIT",
                "realized_pnl_usd": round(pnl_usd, 4),
                "realized_pnl_pct": round(pnl_pct, 2),
                "oco_placed": False, "oco_list_id": None,
            })
            fill_t = trade.get("entry_fill_time")
            if fill_t:
                trade["time_to_resolution_sec"] = (exit_ts - int(fill_t)) // 1000
            resolved_this_run.append((sym, "SL_HIT", pnl_usd))
            self._eager_commit(trade)
            print(f"  {sym:<10} 🔴 Emergency market sell (partial-protection recovery) PnL: ${pnl_usd:+.4f}")
            return

        from services.supabase_client import update_spot_by_order_id

        if protection_state == "FULLY_PROTECTED":
            oco_resp   = prot_result.get("oco_resp") or {}
            oco_orders = oco_resp.get("orderReports", [])
            list_id    = oco_resp.get("orderListId")
            trade.update({
                "oco_placed":                True,
                "oco_order_ids":             [o["orderId"] for o in oco_orders],
                "oco_list_id":               list_id,
                "oco_reconciliation_status": "FULLY_PROTECTED",
            })
            update_spot_by_order_id(eid, {
                "oco_placed":                True,
                "oco_order_ids":             trade["oco_order_ids"],
                "oco_list_id":               list_id,
                "oco_reconciliation_status": "FULLY_PROTECTED",
            })
            _send_telegram(
                f"🛡️ [SPOT] UPGRADED TO FULLY_PROTECTED: {sym}\n"
                f"Previous state: {prev}. New OCO List#{list_id}.\n"
                f"SL: {ca._fmt_price(trade.get('sl')).strip()}  "
                f"TP: {ca._fmt_price(trade.get('tp1')).strip()}"
            )
            print(
                f"  {sym:<10} ✅ FULLY_PROTECTED (upgraded from {prev})  List#{list_id}"
            )

        elif protection_state == "TP_ONLY":
            tp_order_id = prot_result.get("tp_order_id")
            new_state   = "TP_ONLY"
            trade.update({
                "oco_placed": False, "oco_list_id": None,
                "tp_order_id": tp_order_id,
                "oco_reconciliation_status": new_state,
            })
            update_spot_by_order_id(eid, {
                "oco_placed": False,
                "oco_reconciliation_status": new_state,
                "tp_order_id": tp_order_id,
            })
            if prev != new_state:
                _send_telegram(
                    f"🟡 [SPOT] PARTIAL PROTECTION (TP_ONLY): {sym}\n"
                    f"Previous state: {prev}. TP orderId={tp_order_id}. "
                    f"SL still filter-invalid."
                )
            print(f"  {sym:<10} 🟡 TP_ONLY (orderId={tp_order_id})")

        elif protection_state == "SL_ONLY":
            sl_order_id = prot_result.get("sl_order_id")
            new_state   = "SL_ONLY"
            trade.update({
                "oco_placed": False, "oco_list_id": None,
                "sl_order_id": sl_order_id,
                "oco_reconciliation_status": new_state,
            })
            update_spot_by_order_id(eid, {
                "oco_placed": False,
                "oco_reconciliation_status": new_state,
                "sl_order_id": sl_order_id,
            })
            if prev != new_state:
                _send_telegram(
                    f"🟠 [SPOT] PARTIAL PROTECTION (SL_ONLY): {sym}\n"
                    f"Previous state: {prev}. SL orderId={sl_order_id}. "
                    f"TP still filter-invalid."
                )
            print(f"  {sym:<10} 🟠 SL_ONLY (orderId={sl_order_id})")

        else:  # still UNPROTECTED
            new_state = "UNPROTECTED"
            trade["oco_reconciliation_status"] = new_state
            update_spot_by_order_id(eid, {"oco_reconciliation_status": new_state})
            if prev != new_state:
                _send_telegram(
                    f"🚨 [SPOT] STILL UNPROTECTED: {sym}\n"
                    f"Both legs remain filter-invalid. Will retry next cycle."
                )
            print(f"  {sym:<10} ⚠ Still UNPROTECTED (both legs filter-invalid)")


    # ---------------------------------------------------------------------------
    # 9. MAIN — --propose and --check-positions
