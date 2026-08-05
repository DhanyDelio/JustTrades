"""
futures_candidate_scanner.py — Candidate scanning for Binance Futures.

Extracted from core/futures_trade_executor.py (god file) with zero logic
changes. Mirrors the pattern of core/scanners/spot_candidate_scanner.py.

Wraps:
    gather_futures_candidates()  → FuturesCandidateScanner.gather_candidates()
    pick_best_futures_candidate() → FuturesCandidateScanner.pick_best_candidate()
"""

from __future__ import annotations

import io
import contextlib

from services import chart_analyzer as ca
from core.utils.binance_math import round_tick
from core.utils.futures_math import (
    calculate_liquidation_price,
    compute_futures_position_size,
    compute_volatility_regime,
)


class FuturesCandidateScanner:
    """
    Tiered scanner for Binance Futures trade candidates.
    Evaluates both LONG and SHORT setups via chart_analyzer.
    Mirrors SpotCandidateScanner interface.
    """

    def __init__(self, client):
        self.client = client

    # -------------------------------------------------------------------------
    # gather_candidates
    # -------------------------------------------------------------------------

    def gather_candidates(self, scan_n: int | None = None) -> list[dict]:
        """
        Tiered scan for futures candidates — evaluates BOTH long and short setups.

        Scans up to scan_n symbols in parts of FUTURES_PART_SIZE each.
        After each part, reads the X-MBX-Used-Weight-1M header from a lightweight
        futures API call and pauses if usage is approaching the 2400/min limit.
        Stops early once FUTURES_MIN_CANDIDATES are found.

        Returns candidates sorted by risk% ASC.
        """
        from services.binance_throttle import FuturesThrottle
        from core.futures_trade_executor import (
            DEFAULT_SCAN_N,
            FUTURES_PART_SIZE,
            FUTURES_MAX_PARTS,
        )

        if scan_n is None:
            scan_n = DEFAULT_SCAN_N

        _throttle  = FuturesThrottle()
        total_syms = ca.get_top_symbols_by_volume(scan_n)
        n_parts    = max(1, (len(total_syms) + FUTURES_PART_SIZE - 1) // FUTURES_PART_SIZE)
        n_parts    = min(n_parts, FUTURES_MAX_PARTS)

        print(f"\nScanning top {len(total_syms)} symbols for futures setups "
              f"({n_parts} part(s) × {FUTURES_PART_SIZE}) ...")

        all_candidates: list[dict] = []

        for part in range(1, n_parts + 1):
            start_idx = (part - 1) * FUTURES_PART_SIZE
            end_idx   = start_idx + FUTURES_PART_SIZE
            part_syms = total_syms[start_idx:end_idx]
            if not part_syms:
                break

            print(f"\n  ── Futures Part {part}: rank {start_idx+1}–{end_idx} "
                  f"({len(part_syms)} symbols) ──")

            # Rate-limit check between parts
            if part > 1:
                weight = _throttle.fetch_used_weight()
                print(f"  [Rate limit/Futures] Used weight before Part {part}: "
                      f"{weight} / {_throttle._limit}")
                if weight >= int(_throttle._limit * 0.80):
                    print(f"  [Rate limit/Futures] ⚠  Ceiling reached — stopping scan.")
                    break
                _throttle.between_parts_sleep()

            part_candidates: list[dict] = []

            for sym in part_syms:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    result = ca.analyze_symbol(sym, save_chart=False)
                if result is None:
                    continue

                current_price = result["current_price"]
                atr           = result["atr"]
                atr_pct       = result["atr_pct"]

                for direction in ("long", "short"):
                    setup = result["sl_tp"].get(direction, {})

                    if not setup.get("rr_clears"):
                        continue
                    if setup.get("no_tp_in_range"):
                        continue
                    if setup.get("tier_used") not in ("T1", "T2"):
                        continue

                    sl       = setup.get("sl")
                    tp_list  = setup.get("tp", [])
                    tp1      = tp_list[0] if tp_list else None
                    tp2      = tp_list[1] if len(tp_list) > 1 else None
                    rr       = setup.get("rr")
                    risk_pct = setup.get("risk_pct")

                    if not sl or not tp1 or not rr or not risk_pct:
                        continue

                    if direction == "long"  and not (sl < current_price < tp1):
                        continue
                    if direction == "short" and not (tp1 < current_price < sl):
                        continue

                    part_candidates.append({
                        "symbol":           sym,
                        "direction":        direction,
                        "position_side":    "LONG" if direction == "long" else "SHORT",
                        "current_price":    current_price,
                        "entry_price":      current_price,
                        "sl":               sl,
                        "tp1":              tp1,
                        "tp2":              tp2,
                        "rr":               rr,
                        "risk_pct":         risk_pct,
                        "atr":              atr,
                        "atr_pct":          atr_pct,
                        "tier_used":        setup.get("tier_used", "T1"),
                        "support_zones":    result.get("support_zones", []),
                        "resistance_zones": result.get("resistance_zones", []),
                    })

            all_candidates.extend(part_candidates)
            print(f"  [Futures Part {part}] {len(part_candidates)} candidate(s) found  "
                  f"(cumulative: {len(all_candidates)})")

        all_candidates.sort(key=lambda c: (c["risk_pct"], -c["rr"]))
        print(f"\nFound {len(all_candidates)} futures candidates (LONG+SHORT combined).")
        return all_candidates

    # -------------------------------------------------------------------------
    # pick_best_candidate
    # -------------------------------------------------------------------------

    def pick_best_candidate(
        self,
        candidates: list[dict],
        symbol_filter: str | None = None,
        side_filter:   str | None = None,   # "LONG" | "SHORT" | None
    ) -> dict | None:
        """
        Select best candidate passing exchange constraints + liquidation sanity check.
        Optionally filter by symbol or side.
        """
        from core.futures_trade_executor import (
            get_futures_symbol_constraints,
            get_funding_rate,
            FUTURES_BUDGET_USD,
            RISK_FRACTION,
            LEVERAGE,
            ZONE_ENTRY_BUFFER_PCT,
        )

        pool = candidates
        if symbol_filter:
            pool = [c for c in pool if c["symbol"] == symbol_filter.upper()]
        if side_filter:
            pool = [c for c in pool if c["position_side"] == side_filter.upper()]
        if not pool:
            return None

        for cand in pool:
            sym       = cand["symbol"]
            direction = cand["direction"]

            try:
                constraints = get_futures_symbol_constraints(self.client, sym)
            except Exception as e:
                print(f"  [{sym}] Skipping — constraints fetch failed: {e}")
                continue

            # Anchor entry to zone (same logic as spot)
            if direction == "long":
                sup_zones = cand.get("support_zones", [])
                atr_v     = cand["atr"]
                cur       = cand["current_price"]
                min_dist  = 0.5 * atr_v
                qualified = [z for z in sup_zones
                             if z["touches"] >= 2 and (cur - z["center"]) >= min_dist]
                zone = (min(qualified, key=lambda z: cur - z["center"])
                        if qualified else
                        max(sup_zones, key=lambda z: z["touches"]) if sup_zones else None)

                zone_center = zone["center"] if zone else cur
                zone_low    = zone["low"]    if zone else cur
                cand["entry_zone"] = zone

                entry = round_tick(
                    zone_center * (1 + ZONE_ENTRY_BUFFER_PCT),
                    constraints.get("tick_size", 0)
                )
                recalc_sl = round_tick(
                    zone_low - ca.SL_ATR_BUFFER * atr_v,
                    constraints.get("tick_size", 0)
                )
                cand["sl"]       = recalc_sl
                cand["risk_pct"] = (entry - recalc_sl) / entry * 100 if entry > 0 else 0

            elif direction == "short":
                res_zones = cand.get("resistance_zones", [])
                atr_v     = cand["atr"]
                cur       = cand["current_price"]
                min_dist  = 0.5 * atr_v
                qualified = [z for z in res_zones
                             if z["touches"] >= 2 and (z["center"] - cur) >= min_dist]
                zone = (min(qualified, key=lambda z: z["center"] - cur)
                        if qualified else
                        max(res_zones, key=lambda z: z["touches"]) if res_zones else None)

                zone_center = zone["center"] if zone else cur
                zone_high   = zone["high"]   if zone else cur
                cand["entry_zone"] = zone

                entry = round_tick(
                    zone_center * (1 - ZONE_ENTRY_BUFFER_PCT),
                    constraints.get("tick_size", 0)
                )
                recalc_sl = round_tick(
                    zone_high + ca.SL_ATR_BUFFER * atr_v,
                    constraints.get("tick_size", 0)
                )
                cand["sl"]       = recalc_sl
                cand["risk_pct"] = (recalc_sl - entry) / entry * 100 if entry > 0 else 0

            cand["entry_price"] = entry

            # Safety: SL/entry/TP direction check
            if direction == "long" and not (cand["sl"] < entry < cand["tp1"]):
                print(f"  [{sym} LONG] ⛔ Safety check failed — sl/entry/tp1 invalid. Skip.")
                continue
            if direction == "short" and not (cand["tp1"] < entry < cand["sl"]):
                print(f"  [{sym} SHORT] ⛔ Safety check failed — tp1/entry/sl invalid. Skip.")
                continue

            # Position sizing
            sizing = compute_futures_position_size(
                entry_price   = entry,
                sl_price      = cand["sl"],
                margin_budget = FUTURES_BUDGET_USD,
                risk_fraction = RISK_FRACTION,
                leverage      = LEVERAGE,
                constraints   = constraints,
            )
            cand["sizing"]      = sizing
            cand["constraints"] = constraints

            fatal = [w for w in sizing["warnings"]
                     if "below exchange min" in w or "exceeds budget" in w]
            if fatal or sizing["qty"] <= 0:
                print(f"  [{sym} {direction.upper()}] Skipped — {fatal[0] if fatal else 'qty=0'}")
                continue

            # Liquidation price
            liq = calculate_liquidation_price(
                entry_price   = entry,
                leverage      = LEVERAGE,
                position_side = cand["position_side"],
            )
            cand["liquidation"] = liq

            # Sanity: SL must be hit BEFORE liquidation
            if direction == "long" and cand["sl"] <= liq["liquidation_price"]:
                print(f"  [{sym} LONG] ⚠  SL {cand['sl']:.4f} ≤ liq {liq['liquidation_price']:.4f} — skip")
                continue
            if direction == "short" and cand["sl"] >= liq["liquidation_price"]:
                print(f"  [{sym} SHORT] ⚠  SL {cand['sl']:.4f} ≥ liq {liq['liquidation_price']:.4f} — skip")
                continue

            # Fetch volatility regime and funding rate (pre-entry enrichment)
            cand["volatility_regime"]     = compute_volatility_regime(sym)
            cand["funding_rate_at_entry"] = get_funding_rate(self.client, sym)

            return cand

        return None
