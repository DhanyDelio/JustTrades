"""
tokocrypto_candidate_scanner.py
================================
Candidate scanner for Tokocrypto IDR spot trading.

Strategy:
  - Dynamically discover all IDR pairs from Tokocrypto API (auto-adapts
    to new listings and delistings — no hardcoded pair list to maintain)
  - Analyze each pair using Binance USDT data via chart_analyzer
    (mature, proven — same logic as Binance testnet scanner)
  - Convert entry/SL/TP prices to IDR using live USDT_IDR rate from Tokocrypto
  - Apply IDENTICAL filtering as SpotCandidateScanner:
      * T1 zone-backed (tier_used == "T1")
      * rr_clears == True
      * no_tp_in_range == False
      * LONG only (no short from IDR account)
  - Sort by risk_pct ASC then -rr, return top max_positions (default 10)

Rate-limit discipline:
  - 0.1s sleep between analyze_symbol calls (~3 symbols/sec)
  - Well within 1,200 weight/min Tokocrypto limit
"""

import contextlib
import io
import time

import services.chart_analyzer as ca
from core.paper_trade_executor import ZONE_ENTRY_BUFFER_PCT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fallback list — used ONLY if API discovery fails (e.g. network error).
# The scanner normally discovers IDR pairs dynamically from get_symbols().
_FALLBACK_IDR_PAIRS = [
    "ADA_IDR", "ALCH_IDR", "ARB_IDR", "ASTER_IDR", "AVAX_IDR",
    "BNB_IDR", "BTC_IDR", "CARV_IDR", "DOGE_IDR", "DOGS_IDR",
    "DRX_IDR", "ETH_IDR", "FLOKI_IDR", "GOAT_IDR", "GRAM_IDR",
    "HBAR_IDR", "JELLYJELLY_IDR", "MANTA_IDR", "MOODENG_IDR", "NBT_IDR",
    "ONDO_IDR", "POL_IDR", "RENDER_IDR", "SCR_IDR", "SKYA_IDR",
    "SOL_IDR", "SOON_IDR", "SPX_IDR", "SUI_IDR", "TAO_IDR",
    "TKO_IDR", "USDC_IDR", "USDT_IDR", "U_IDR", "VELO_IDR",
    "VIRTUAL_IDR", "WIF_IDR", "WLD_IDR", "XRP_IDR", "ZIL_IDR",
]

# Skip rules — reuse the same stablecoin / fiat / commodity filters from
# chart_analyzer so both scanners stay consistent. Applied to the base asset
# of each IDR pair (e.g. base of "USDC_IDR" is "USDC").
from services.chart_analyzer import (
    STABLECOIN_KEYWORDS,
    FIAT_KEYWORDS,
    COMMODITY_RWA_KEYWORDS,
)

# Exchange token + USDT (which is base asset in USDT_IDR — no point swing-trading it)
_EXCHANGE_TOKENS = {"TKO", "USDT"}


def _is_skip_base(base_asset: str) -> bool:
    """Return True if base_asset should be skipped (stablecoin/fiat/commodity/exchange token)."""
    b = base_asset.upper()
    if b in _EXCHANGE_TOKENS:
        return True
    if any(b == sc or b.startswith(sc) for sc in STABLECOIN_KEYWORDS):
        return True
    if any(b == fk or b.startswith(fk) for fk in FIAT_KEYWORDS):
        return True
    if any(b == rw or b.startswith(rw) for rw in COMMODITY_RWA_KEYWORDS):
        return True
    return False

# Max concurrent positions (slot budget)
MAX_TOKO_SLOTS = 10

# Min IDR notional per trade (from exchange symbol info)
MIN_NOTIONAL_IDR = 20_000.0

# Sleep between analyze_symbol calls (seconds) — polite rate limiting
SCAN_SLEEP_SEC = 0.1

# USDT/IDR rate cache TTL (seconds)
_RATE_CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _to_binance_symbol(toko_symbol: str) -> str:
    """Convert Tokocrypto IDR pair to Binance USDT pair for chart analysis.

    Examples:
        BTC_IDR  → BTCUSDT
        ZIL_IDR  → ZILUSDT
        DOGS_IDR → DOGSUSDT
    """
    base = toko_symbol.replace("_IDR", "")
    return f"{base}USDT"


# ---------------------------------------------------------------------------
# Scanner class
# ---------------------------------------------------------------------------

class TokocryptoCandidateScanner:
    """
    Scans all Tokocrypto IDR pairs for T1 zone-backed long setups,
    using Binance USDT kline data as the analysis source and converting
    all prices to IDR via the live USDT_IDR rate.

    IDR pairs are discovered dynamically from the API — new listings are
    automatically included and delisted pairs are excluded.

    Filtering logic is identical to SpotCandidateScanner to ensure
    consistent candidate quality between testnet and production.
    """

    def __init__(self, toko_client, repo=None):
        """
        toko_client : TokocryptoClient instance (for live rate + symbol info)
        repo        : optional, for checking existing open positions
        """
        self.toko_client = toko_client
        self.repo        = repo
        self._rate_cache: float | None = None
        self._rate_cached_at: float    = 0.0
        # Cache Tokocrypto symbol info (constraints + discovered pairs)
        self._sym_constraints: dict[str, dict] = {}
        self._discovered_pairs: list[str] | None = None

    # ------------------------------------------------------------------
    # Public — USDT/IDR rate
    # ------------------------------------------------------------------

    def get_usdt_idr_rate(self) -> float:
        """
        Return the live USDT/IDR conversion rate from Tokocrypto.
        Caches for RATE_CACHE_TTL seconds within one scan cycle.

        Raises RuntimeError if the rate cannot be fetched.
        """
        now = time.monotonic()
        if (self._rate_cache is not None
                and (now - self._rate_cached_at) < _RATE_CACHE_TTL):
            return self._rate_cache

        try:
            price = self.toko_client.get_ticker("USDT_IDR")
        except Exception as e:
            raise RuntimeError(f"Cannot fetch USDT_IDR rate: {e}") from e

        if not price or price <= 0:
            raise RuntimeError(f"Invalid USDT_IDR rate returned: {price!r}")

        self._rate_cache     = float(price)
        self._rate_cached_at = now
        return self._rate_cache

    # ------------------------------------------------------------------
    # Public — Symbol constraints
    # ------------------------------------------------------------------

    def _load_symbols(self) -> None:
        """Fetch all symbols from API and populate constraints + IDR pairs cache."""
        syms = self.toko_client.get_symbols()
        idr_pairs = []
        for s in syms:
            self._sym_constraints[s.symbol] = {
                "tick_size":    s.tick_size,
                "step_size":    s.step_size,
                "min_notional": s.min_notional or MIN_NOTIONAL_IDR,
            }
            if s.quote_asset == "IDR" and s.spot_enabled:
                idr_pairs.append(s.symbol)
        self._discovered_pairs = sorted(idr_pairs)

    def _discover_idr_pairs(self) -> list[str]:
        """
        Dynamically discover all spot-enabled IDR pairs from Tokocrypto API.

        Results are cached for the scanner's lifetime (one scan cycle).
        Falls back to _FALLBACK_IDR_PAIRS if the API call fails.
        """
        if self._discovered_pairs is not None:
            return self._discovered_pairs

        try:
            self._load_symbols()
        except Exception as e:
            print(f"  ⚠ Failed to discover IDR pairs from API: {e}")
            print(f"  ⚠ Falling back to static list ({len(_FALLBACK_IDR_PAIRS)} pairs)")
            self._discovered_pairs = list(_FALLBACK_IDR_PAIRS)
            return self._discovered_pairs

        return self._discovered_pairs  # type: ignore[return-value]

    def _get_constraints(self, toko_symbol: str) -> dict:
        """
        Return tick_size and step_size for a Tokocrypto IDR symbol.
        Uses cached Tokocrypto symbol list — no extra API call per symbol.
        """
        if toko_symbol not in self._sym_constraints:
            # Lazy-load all symbols once and cache
            if not self._sym_constraints:
                self._load_symbols()
            if toko_symbol not in self._sym_constraints:
                return {"tick_size": 1.0, "step_size": 0.01, "min_notional": MIN_NOTIONAL_IDR}
        return self._sym_constraints[toko_symbol]

    # ------------------------------------------------------------------
    # Public — Candidate gathering
    # ------------------------------------------------------------------

    def gather_candidates(self, max_positions: int = MAX_TOKO_SLOTS) -> list[dict]:
        """
        Scan all tradeable IDR pairs, apply T1/rr/no-tp-range filters,
        convert prices to IDR, score and return top max_positions candidates.

        Analysis uses Binance USDT kline data (via chart_analyzer).
        Prices are converted to IDR using the live USDT_IDR rate.

        Returns list of candidate dicts, sorted by risk_pct ASC then -rr.
        """
        usdt_idr = self.get_usdt_idr_rate()

        # Dynamic pair discovery — adapts to new listings / delistings
        idr_pairs = self._discover_idr_pairs()
        skipped = []
        tradeable = []
        for p in idr_pairs:
            base = p.replace("_IDR", "")
            if _is_skip_base(base):
                skipped.append(p)
            else:
                tradeable.append(p)

        print(f"\n  USDT/IDR rate: {usdt_idr:,.0f}")
        print(f"  Scanning {len(tradeable)} IDR pairs "
              f"(discovered {len(idr_pairs)} total, skipped {len(skipped)}: "
              f"{', '.join(skipped) if skipped else 'none'})...\n")

        candidates: list[dict] = []

        for toko_sym in tradeable:

            binance_sym = _to_binance_symbol(toko_sym)

            # Analyze using Binance USDT data — suppress per-symbol noise
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    result = ca.analyze_symbol(binance_sym, save_chart=False)
                except Exception:
                    result = None

            time.sleep(SCAN_SLEEP_SEC)   # polite rate limiting

            if result is None:
                continue

            current_usd = result["current_price"]
            atr_usd     = result["atr"]
            atr_pct     = result["atr_pct"]

            for direction in ("long",):   # LONG only — IDR spot account
                setup = result["sl_tp"].get(direction, {})

                # ── Identical filters as SpotCandidateScanner ────────────
                if not setup.get("rr_clears"):
                    continue
                if setup.get("no_tp_in_range"):
                    continue
                if setup.get("tier_used") != "T1":
                    continue

                sl_usd  = setup.get("sl")
                tp1_usd = setup["tp"][0] if setup.get("tp") else None
                rr      = setup.get("rr")
                risk_pct = setup.get("risk_pct")

                if not sl_usd or not tp1_usd or not rr or not risk_pct:
                    continue

                # ── Convert USD → IDR ────────────────────────────────────
                constraints  = self._get_constraints(toko_sym)
                tick_size    = constraints["tick_size"]

                def _round_tick(v: float, tick: float) -> float:
                    if tick <= 0:
                        return v
                    import math
                    return round(math.floor(v / tick) * tick, 10)

                entry_idr = _round_tick(current_usd * usdt_idr, tick_size)
                sl_idr    = _round_tick(sl_usd      * usdt_idr, tick_size)
                tp1_idr   = _round_tick(tp1_usd     * usdt_idr, tick_size)

                # ── Find winning T1 zone (same as SpotCandidateScanner) ──
                winning_zone = None
                for cand_z in setup.get("candidates", []):
                    if cand_z["tier"] == "T1" and cand_z.get("tp") == tp1_usd:
                        winning_zone = cand_z
                        break
                if not winning_zone:
                    for cand_z in setup.get("candidates", []):
                        if cand_z["tier"] == "T1":
                            winning_zone = cand_z
                            break

                candidates.append({
                    # Tokocrypto-facing fields (IDR)
                    "symbol":           toko_sym,
                    "direction":        "long",
                    "current_price":    entry_idr,
                    "entry_price":      entry_idr,    # refined in pick_best_candidate
                    "sl":               sl_idr,
                    "tp1":              tp1_idr,
                    "tp2":              (_round_tick(setup["tp"][1] * usdt_idr, tick_size)
                                         if len(setup.get("tp", [])) > 1 else None),
                    "rr":               rr,
                    "risk_pct":         risk_pct,
                    "atr":              atr_usd * usdt_idr,   # IDR-denominated ATR
                    "atr_pct":          atr_pct,
                    "winning_zone":     winning_zone,
                    "support_zones":    result.get("support_zones", []),
                    "resistance_zones": result.get("resistance_zones", []),
                    "nearest_sup":      result.get("nearest_sup_dist"),
                    "nearest_res":      result.get("nearest_res_dist"),
                    # Audit fields
                    "binance_symbol":   binance_sym,
                    "usdt_idr_rate":    usdt_idr,
                    "entry_price_usd":  current_usd,
                    "sl_usd":           sl_usd,
                    "tp1_usd":          tp1_usd,
                })

        # ── Sort: same as SpotCandidateScanner ───────────────────────────
        candidates.sort(key=lambda c: (c["risk_pct"], -c["rr"]))

        # ── Score and cap ─────────────────────────────────────────────────
        self._attach_scores(candidates)

        n = len(candidates)
        print(f"  Found {n} T1 zone-backed IDR candidates.")
        if n > max_positions:
            print(f"  Capping to top {max_positions} (sorted by risk_pct, then R:R).")
            candidates = candidates[:max_positions]

        return candidates

    # ------------------------------------------------------------------
    # Public — Pick best (with sizing)
    # ------------------------------------------------------------------

    def pick_best_candidate(
        self,
        candidates: list[dict],
        available_idr: float,
        symbol_filter: str | None = None,
    ) -> dict | None:
        """
        From sorted candidates, find the first one that passes sizing checks.

        available_idr : total IDR available for trading
        symbol_filter : if set, only consider this specific symbol
        """
        pool = candidates
        if symbol_filter:
            sym_up = symbol_filter.upper()
            if not sym_up.endswith("_IDR"):
                sym_up = f"{sym_up}_IDR"
            pool = [c for c in candidates if c["symbol"] == sym_up]
            if not pool:
                print(f"  No T1 candidates found for {sym_up} in this scan.")
                return None

        slot_size_idr = available_idr   # per-slot budget (caller divides total across slots)

        for cand in pool:
            toko_sym    = cand["symbol"]
            constraints = self._get_constraints(toko_sym)
            tick_size   = constraints["tick_size"]
            step_size   = constraints["step_size"]
            min_notional= constraints["min_notional"]

            atr_idr  = cand["atr"]
            cur_idr  = cand["current_price"]
            sup_zones = cand.get("support_zones", [])

            # ── Refine entry to support zone (same logic as SpotCandidateScanner) ─
            min_dist = 0.5 * atr_idr
            qualified = [
                z for z in sup_zones
                if z["touches"] >= 2 and (cur_idr - z["center"] * cand["usdt_idr_rate"]) >= min_dist
            ]

            if qualified:
                zone = min(qualified, key=lambda z: cur_idr - z["center"] * cand["usdt_idr_rate"])
            elif sup_zones:
                zone = max(sup_zones, key=lambda z: z["touches"])
            else:
                zone = None

            if zone:
                zone_center_idr = zone["center"] * cand["usdt_idr_rate"]
                zone_low_idr    = zone["low"]    * cand["usdt_idr_rate"]
            else:
                zone_center_idr = cur_idr
                zone_low_idr    = cur_idr

            import math

            def _round_tick(v, tick):
                if tick <= 0: return v
                return round(math.floor(v / tick) * tick, 10)

            def _round_step(v, step):
                if step <= 0: return v
                return round(math.floor(v / step) * step, 10)

            entry_idr = _round_tick(zone_center_idr * (1 + ZONE_ENTRY_BUFFER_PCT), tick_size)

            # Recalculate SL from actual zone (same pattern as SpotCandidateScanner)
            sl_idr = _round_tick(
                zone_low_idr - ca.SL_ATR_BUFFER * atr_idr,
                tick_size,
            )

            # Recalculate risk_pct from actual entry/SL
            risk_pct = (entry_idr - sl_idr) / entry_idr * 100 if entry_idr > 0 else 0

            # ── Safety assertion: SL < entry < TP ────────────────────────
            tp1_idr = cand["tp1"]
            if not (sl_idr < entry_idr < tp1_idr):
                print(f"  [{toko_sym}] ⛔ Safety check failed — "
                      f"SL={sl_idr:,.0f} entry={entry_idr:,.0f} TP={tp1_idr:,.0f}")
                continue

            # ── Sizing ───────────────────────────────────────────────────
            qty = _round_step(slot_size_idr / entry_idr, step_size)

            # If rounding down causes notional to fall below min_notional,
            # round UP by one step — we still stay within slot budget.
            if qty > 0 and entry_idr * qty < min_notional:
                qty = _round_step(qty + step_size, step_size)

            if qty <= 0:
                print(f"  [{toko_sym}] ⛔ Cannot size — qty=0 (slot={slot_size_idr:,.0f} IDR)")
                continue

            notional = entry_idr * qty
            if notional < min_notional:
                print(f"  [{toko_sym}] ⛔ Below min notional "
                      f"(Rp {notional:,.0f} < Rp {min_notional:,.0f})")
                continue

            # ── Attach sizing and update prices ───────────────────────────
            cand["entry_price"] = entry_idr
            cand["sl"]          = sl_idr
            cand["risk_pct"]    = risk_pct
            cand["entry_zone"]  = zone
            cand["sizing"] = {
                "qty":            qty,
                "step_size":      step_size,
                "tick_size":      tick_size,
                "slot_size_idr":  slot_size_idr,
                "notional_idr":   notional,
            }

            return cand

        return None

    # ------------------------------------------------------------------
    # Private — Scoring (identical to SpotCandidateScanner)
    # ------------------------------------------------------------------

    def _attach_scores(self, candidates: list[dict]) -> None:
        """Attach composite scores (0-10) to each candidate in-place."""
        if not candidates:
            return

        risk_vals  = [c["risk_pct"] for c in candidates]
        rr_vals    = [c["rr"] for c in candidates]

        def _best_touches(c: dict) -> int:
            sup = c.get("support_zones", [])
            if not sup:
                return 1
            atr = c.get("atr", 1)
            cur = c.get("current_price", 1)
            rate = c.get("usdt_idr_rate", 1)
            min_dist = 0.5 * atr
            qualified = [z for z in sup
                         if z["touches"] >= 2
                         and (cur - z["center"] * rate) >= min_dist]
            if qualified:
                return max(z["touches"] for z in qualified)
            return max(z["touches"] for z in sup)

        touch_vals = [_best_touches(c) for c in candidates]

        def norm_inv(v, vals):
            lo, hi = min(vals), max(vals)
            if lo == hi: return 5.0
            return 10.0 * (1 - (v - lo) / (hi - lo))

        def norm(v, vals):
            lo, hi = min(vals), max(vals)
            if lo == hi: return 5.0
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
