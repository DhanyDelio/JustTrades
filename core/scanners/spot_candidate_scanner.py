import io
import contextlib
import time
import services.chart_analyzer as ca
from core.utils.binance_math import get_symbol_constraints, round_tick, compute_position_size
from core.paper_trade_executor import ZONE_ENTRY_BUFFER_PCT, RISK_FRACTION, DEFAULT_SCAN_N, PART_SIZE, MAX_PARTS, SCAN_PART_DELAY_SEC, RATE_LIMIT_WEIGHT_CEILING, BUDGET_USD, PER_TRADE_BUDGET

class SpotCandidateScanner:
    def __init__(self, client, repo):
        self.client = client
        self.repo = repo

    def gather_candidates(self, scan_n: int = DEFAULT_SCAN_N) -> list[dict]:
        """
        Run chart_analyzer on top scan_n symbols.
        Extract all setups where:
          - direction is T1 zone-backed (tier_used == "T1")
          - rr_clears is True
          - no_tp_in_range is False
        Returns list of candidate dicts, sorted by SL risk% ASC (smallest risk first).
        """
        print(f"\nScanning top {scan_n} symbols for T1 zone-backed setups...")
        symbols = ca.get_top_symbols_by_volume(scan_n)
        print(f"Symbols to analyze: {symbols}\n")
    
        candidates: list[dict] = []
    
        for sym in symbols:
            # Suppress chart_analyzer's per-symbol print noise
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
    
                # Binance Spot with USDT budget = LONG only.
                # SHORT requires holding the base asset first — not possible
                # from a pure USDT account. Skip SHORT candidates entirely.
                if direction == "short":
                    continue
    
                if not setup.get("rr_clears"):
                    continue
                if setup.get("no_tp_in_range"):
                    continue
                if setup.get("tier_used") != "T1":
                    continue
    
                sl   = setup.get("sl")
                tp1  = setup["tp"][0] if setup.get("tp") else None
                rr   = setup.get("rr")
                risk_pct = setup.get("risk_pct")
    
                if not sl or not tp1 or not rr or not risk_pct:
                    continue
    
                # Find the winning T1 zone (for display)
                winning_zone = None
                for cand in setup.get("candidates", []):
                    if cand["tier"] == "T1" and cand["tp"] == tp1:
                        winning_zone = cand
                        break
                # Fallback: first T1 candidate in pool
                if not winning_zone:
                    for cand in setup.get("candidates", []):
                        if cand["tier"] == "T1":
                            winning_zone = cand
                            break
    
                candidates.append({
                    "symbol":          sym,
                    "direction":       direction,
                    "current_price":   current_price,
                    "entry_price":     current_price,   # refined to zone price in pick_best
                    "sl":              sl,
                    "tp1":             tp1,
                    "tp2":             setup["tp"][1] if len(setup.get("tp", [])) > 1 else None,
                    "rr":              rr,
                    "risk_pct":        risk_pct,
                    "atr":             atr,
                    "atr_pct":         atr_pct,
                    "winning_zone":    winning_zone,
                    "support_zones":   result.get("support_zones", []),
                    "resistance_zones": result.get("resistance_zones", []),
                    "nearest_sup":     result.get("nearest_sup_dist"),
                    "nearest_res":     result.get("nearest_res_dist"),
                })
    
        # Sort: smallest SL risk% first (primary), R:R descending (tiebreaker)
        candidates.sort(key=lambda c: (c["risk_pct"], -c["rr"]))
    
        # Compute composite scores for display (Task 1)
        self._attach_scores(candidates)
    
        print(f"Found {len(candidates)} T1 zone-backed candidates across {scan_n} symbols.")
        return candidates


    def _attach_scores(self, candidates: list[dict]) -> None:
        """
        Attach display scores to each candidate (in-place).
        Scores are 0–10 each, composite is weighted average.
    
        - Risk Score    : lower risk% = higher score  (weight 0.5 — primary criterion)
        - Zone Strength : strongest zone touches in support_zones pool (weight 0.3)
                          Uses max(support_zones[*].touches) — available at gather time,
                          unlike entry_zone which is only set later in pick_best_candidate
        - R:R Score     : higher R:R = higher score    (weight 0.2)
        """
        if not candidates:
            return
    
        risk_vals  = [c["risk_pct"] for c in candidates]
        rr_vals    = [c["rr"] for c in candidates]
    
        # Zone strength = strongest support zone in the candidate's pool by touch count.
        # This is available at gather_candidates time (support_zones is populated).
        # We pick max touches among zones within ATR reach to match what pick_best sees.
        def _best_touches(c: dict) -> int:
            sup = c.get("support_zones", [])
            if not sup:
                return 1
            atr = c.get("atr", 1)
            cur = c.get("current_price", 1)
            min_dist = 0.5 * atr
            # Mirror the qualification filter from pick_best_candidate
            qualified = [z for z in sup
                         if z["touches"] >= 2 and (cur - z["center"]) >= min_dist]
            if qualified:
                return max(z["touches"] for z in qualified)
            # Fallback: best touches in any support zone
            return max(z["touches"] for z in sup)
    
        touch_vals = [_best_touches(c) for c in candidates]
    
        def norm_inv(v, vals):   # lower = better → invert
            lo, hi = min(vals), max(vals)
            if lo == hi:
                return 5.0   # all equal → neutral mid-score, not max
            return 10.0 * (1 - (v - lo) / (hi - lo))
    
        def norm(v, vals):       # higher = better
            lo, hi = min(vals), max(vals)
            if lo == hi:
                return 5.0   # all equal → neutral mid-score, not max
            return 10.0 * (v - lo) / (hi - lo)
    
        for i, c in enumerate(candidates):
            rs  = norm_inv(risk_vals[i],  risk_vals)
            zs  = norm(touch_vals[i], touch_vals)
            rrs = norm(rr_vals[i],    rr_vals)
            composite = 0.5 * rs + 0.3 * zs + 0.2 * rrs
            c["score_risk"]      = round(rs, 1)
            c["score_zone"]      = round(zs, 1)
            c["score_rr"]        = round(rrs, 1)
            c["score_composite"] = round(composite, 1)
            c["_touch_val"]      = touch_vals[i]


    def pick_best_candidate(self, 
        candidates: list[dict],
        client,
        budget_for_slot: float = BUDGET_USD,
        symbol_filter: str | None = None,
    ) -> dict | None:
        """
        From sorted candidates, find the first one that passes exchange
        constraints (min notional, budget fit).
    
        budget_for_slot: how much USD is allocated to THIS slot (may differ from
                         BUDGET_USD when splitting across two positions).
        symbol_filter:   if set, only consider candidates for this symbol (--symbol override).
        """
        pool = candidates
        if symbol_filter:
            pool = [c for c in candidates if c["symbol"] == symbol_filter.upper()]
            if not pool:
                print(f"  No T1 candidates found for {symbol_filter.upper()} in today's scan.")
                return None
    
        for cand in pool:
            sym   = cand["symbol"]
            price = cand["current_price"]
    
            try:
                constraints = get_symbol_constraints(client, sym)
            except Exception as e:
                print(f"  [{sym}] Skipping — could not fetch constraints: {e}")
                continue
    
            # Entry price anchored to the nearest QUALIFIED support zone for LONG.
            # "Qualified" = must be at least 0.5×ATR below current price AND
            # have ≥ 2 touches (empirically tested level).
            # We skip zones that are too close to current price — those would fill
            # the limit order almost immediately, defeating the "wait for pullback" strategy.
            # If no qualified zone exists, fall back to the strongest available zone.
            if cand["direction"] == "long":
                sup_zones = cand.get("support_zones", [])
                atr       = cand["atr"]
                cur       = cand["current_price"]
                min_dist  = 0.5 * atr   # at least half an ATR below current price
    
                # Try: zones with ≥2 touches that are far enough below current
                qualified = [
                    z for z in sup_zones
                    if z["touches"] >= 2 and (cur - z["center"]) >= min_dist
                ]
    
                if qualified:
                    # Pick the closest qualified zone (smallest distance that still passes)
                    zone = min(qualified, key=lambda z: cur - z["center"])
                elif sup_zones:
                    # Fallback: best touch count among all support zones
                    zone = max(sup_zones, key=lambda z: z["touches"])
                else:
                    zone = None
    
                zone_center = zone["center"] if zone else cur
                zone_low    = zone["low"]    if zone else cur
                cand["entry_zone"] = zone   # store for display
    
                entry = round_tick(
                    zone_center * (1 + ZONE_ENTRY_BUFFER_PCT),
                    constraints.get("tick_size", 0),
                )
    
                # CRITICAL: Recalculate SL from the ACTUAL entry_zone chosen here,
                # not from chart_analyzer's sl_tp["sl"] which uses support_zones[0].
                # If a different zone was picked for entry, the original SL could be
                # ABOVE the entry price — which is backwards for a LONG.
                atr_val   = cand["atr"]
                recalc_sl = round_tick(
                    zone_low - ca.SL_ATR_BUFFER * atr_val,
                    constraints.get("tick_size", 0),
                )
                cand["sl"]       = recalc_sl
                cand["risk_pct"] = (entry - recalc_sl) / entry * 100 if entry > 0 else 0
    
            cand["entry_price"] = entry
    
            # ── SAFETY ASSERTION: SL must be below entry for LONG ────────
            sl_val = cand["sl"]
            tp1_val = cand["tp1"]
            if cand["direction"] == "long":
                if not (sl_val < entry < tp1_val):
                    print(
                        f"  [{sym} LONG] ⛔ SAFETY CHECK FAILED — "
                        f"SL={sl_val:.4f} entry={entry:.4f} TP1={tp1_val:.4f} "
                        f"— required: SL < entry < TP1. Skipping this candidate."
                    )
                    continue
    
            sizing = compute_position_size(
                entry_price   = entry,
                sl_price      = cand["sl"],   # always recalculated from entry_zone above
                budget_usd    = budget_for_slot,
                risk_fraction = RISK_FRACTION,
                constraints   = constraints,
            )
            cand["sizing"]           = sizing
            cand["constraints"]      = constraints
            cand["budget_for_slot"]  = budget_for_slot
    
            # Hard reject: min notional failure, zero qty, or notional exceeds budget
            fatal = [w for w in sizing["warnings"]
                     if "below exchange minimum" in w or "cannot size" in w
                     or "exceeds total budget" in w]
            if fatal or sizing["qty"] <= 0:
                print(f"  [{sym} {cand['direction'].upper()}] Skipped — {fatal[0] if fatal else 'qty=0'}")
                continue
    
            return cand   # first clean candidate wins
    
        return None


    def gather_all_candidates(self, scan_n: int, open_symbols: set[str] | None = None) -> list[dict]:
        """
        Tiered/paginated candidate scan for --propose-all.
    
        Scans symbols in parts (Part 1 = rank 1-30, Part 2 = 31-60, Part 3 = 61-90).
        Proceeds to the next part only if fewer than MIN_DESIRED_CANDIDATES valid
        candidates were found so far, and only if rate-limit weight is safe.
    
        Each candidate is tagged with scan_part (1/2/3) so the batch table shows
        where it came from.
        """
        import time as _time
        import requests as _req
    
        open_symbols = open_symbols or set()
    
        from services.binance_throttle import SpotThrottle
        _throttle = SpotThrottle()
    
        def _get_used_weight() -> int:
            """Fetch current used-weight from Binance Spot API headers (1 weight unit)."""
            return _throttle.fetch_used_weight()
    
        def _scan_part(symbols: list[str], part_num: int, start_rank: int) -> list[dict]:
            """Scan a list of symbols and return raw valid candidate dicts.
            start_rank: 1-based rank of the first symbol in this batch."""
            raw: list[dict] = []
            skipped = 0
            for sym_idx, sym in enumerate(symbols):
                sym_rank = start_rank + sym_idx   # 1-based rank from volume list
                if sym in open_symbols:
                    skipped += 1
                    continue
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    result = ca.analyze_symbol(sym, save_chart=False)
                if not result:
                    continue
    
                cur     = result["current_price"]
                atr     = result["atr"]
                atr_pct = result["atr_pct"]
    
                for direction in ("long",):
                    setup = result["sl_tp"].get(direction, {})
                    if setup.get("no_tp_in_range"):
                        continue
                    if not setup.get("rr_clears"):
                        continue
    
                    sl  = setup.get("sl")
                    tp1 = setup["tp"][0] if setup.get("tp") else None
                    rr  = setup.get("rr")
                    if not sl or not tp1 or not rr:
                        continue
    
                    # Part 2/3 extra scrutiny: require minimum 24h quote volume
                    # Lower-ranked symbols are less liquid — add a floor
                    if part_num >= 2:
                        min_vol = 5_000_000   # $5M 24h volume floor for Part 2+
                        try:
                            ticker = _req.get(
                                "https://api.binance.com/api/v3/ticker/24hr",
                                params={"symbol": sym},
                                timeout=8,
                            ).json()
                            vol = float(ticker.get("quoteVolume", 0))
                            if vol < min_vol:
                                continue
                        except Exception:
                            pass  # if we can't check, let it through
    
                    winning_zone = next(
                        (c for c in setup.get("candidates", []) if c["tp"] == tp1), None
                    )
                    raw.append({
                        "symbol":           sym,
                        "direction":        direction,
                        "current_price":    cur,
                        "entry_price":      cur,
                        "sl":               sl,
                        "tp1":              tp1,
                        "tp2":              setup["tp"][1] if len(setup.get("tp", [])) > 1 else None,
                        "rr":               rr,
                        "risk_pct":         setup.get("risk_pct", 0),
                        "atr":              atr,
                        "atr_pct":          atr_pct,
                        "winning_zone":     winning_zone,
                        "support_zones":    result.get("support_zones", []),
                        "resistance_zones": result.get("resistance_zones", []),
                        "tier_used":        setup.get("tier_used", "T1"),
                        "scan_part":        part_num,
                        "symbol_rank":      sym_rank,
                    })
            if skipped:
                print(f"  [Part {part_num}] Skipped {skipped} already-open symbol(s)")
            return raw
    
        # ── Get full ranked symbol list once ──────────────────────────────────
        total_needed = PART_SIZE * MAX_PARTS
        all_symbols  = ca.get_top_symbols_by_volume(total_needed)
    
        all_raw: list[dict] = []
    
        for part in range(1, MAX_PARTS + 1):
            start_idx = (part - 1) * PART_SIZE
            end_idx   = start_idx + PART_SIZE
            part_syms = all_symbols[start_idx:end_idx]
            if not part_syms:
                print(f"  [Part {part}] No symbols available — stopping.")
                break
    
            print(f"\n  ── Part {part} scan: rank {start_idx+1}–{end_idx} "
                  f"({len(part_syms)} symbols) ──")
    
            # Rate-limit check before each part (except part 1)
            if part > 1:
                weight = _get_used_weight()
                print(f"  [Rate limit/Spot] Used weight before Part {part}: {weight} / {_throttle._limit}")
                if weight >= RATE_LIMIT_WEIGHT_CEILING:
                    print(f"  [Rate limit/Spot] ⚠  Weight {weight} ≥ ceiling {RATE_LIMIT_WEIGHT_CEILING} "
                          f"— skipping Part {part}+ to avoid ban.")
                    break
                _throttle.check_weight(weight)
                _throttle.between_parts_sleep()
    
            part_raw = _scan_part(part_syms, part, start_rank=start_idx + 1)
            all_raw.extend(part_raw)
    
            valid_so_far = len([c for c in all_raw])  # will be filtered below
            print(f"  [Part {part}] Raw candidates found this part: {len(part_raw)}  "
                  f"(cumulative raw: {len(all_raw)})")
    
            if part < MAX_PARTS:
                print(f"  [Part {part}] Continuing to Part {part+1}...")
    
        # ── Sort + score ───────────────────────────────────────────────────────
        all_raw.sort(key=lambda c: (c["risk_pct"], -c["rr"]))
        self._attach_scores(all_raw)
    
        # ── Apply exchange constraints + entry zone anchoring + sanity check ──
        valid: list[dict] = []
        for cand in all_raw:
            sym = cand["symbol"]
            try:
                constraints = get_symbol_constraints(self.client, sym)
            except Exception:
                continue
    
            if cand["direction"] == "long":
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
    
                entry     = round_tick(zone_center * (1 + ZONE_ENTRY_BUFFER_PCT),
                                       constraints.get("tick_size", 0))
                recalc_sl = round_tick(zone_low - ca.SL_ATR_BUFFER * atr_v,
                                       constraints.get("tick_size", 0))
                cand["sl"]             = recalc_sl
                cand["risk_pct"]       = (entry - recalc_sl) / entry * 100 if entry > 0 else 0
                cand["entry_price"]    = entry
                cand["budget_for_slot"] = PER_TRADE_BUDGET
    
            if not (cand["sl"] < cand["entry_price"] < cand["tp1"]):
                continue
    
            sizing = compute_position_size(
                entry_price   = cand["entry_price"],
                sl_price      = cand["sl"],
                budget_usd    = PER_TRADE_BUDGET,
                risk_fraction = RISK_FRACTION,
                constraints   = constraints,
            )
            cand["sizing"]      = sizing
            cand["constraints"] = constraints
    
            fatal = [w for w in sizing["warnings"]
                     if "below exchange minimum" in w or "cannot size" in w
                     or "exceeds total budget" in w]
            if fatal or sizing["qty"] <= 0:
                continue
    
            if cand["risk_pct"] > 10.0:
                continue
    
            valid.append(cand)
    
        print(f"\nFound {len(valid)} valid candidates total "
              f"(from {len(all_raw)} raw across {min(MAX_PARTS, len(all_raw)+1)} part(s)).")
        return valid


