# Spot Stale Pending Entry Policy

**Status:** SHADOW / RESEARCH — auto-cancel is currently OFF  
**Last updated:** 2026-09-26  
**Audit date:** 2026-09-25 (Supabase read-only forensic)  
**Relevant commit:** `6b9de95` feat(spot): add stale pending entry guard

---

## 1. Why Stale Pending Orders Exist

A Spot entry is a **LIMIT BUY** placed at a support zone identified by
the chart scanner at decision time.  The order only fills if price
**pulls back** to that zone.

If price moves away (up) instead of pulling back, the order remains open
indefinitely on Binance Testnet.  This is by design — the system does not
chase price.

Stale orders accumulate when:

1. Price rallies away from the entry zone and never returns.
2. Multiple batch cycles propose entries in the same broad zone across
   different correlation clusters, each producing a separate LIMIT BUY.
3. Testnet orders are never auto-expired by the exchange (Testnet has no
   GTC order expiry enforced in practice).

**Audit findings (25 Sep 2026):**

| Metric | Value |
|---|---|
| Total pending NEW orders | 23 |
| Oldest order | AAVEUSDT — 83.2 days (since 4 Jul 2026) |
| Median age | 52.8 days |
| Price ≥10% above entry | 19 / 23 (83%) |
| Extreme runaway (>30%) | 16 / 23 (70%) |
| `pending_entry_guard` populated | 0 / 23 before backfill |
| After backfill (26 Sep 2026) | 23 / 23 |

---

## 2. Two Distinct Concepts — Do Not Mix

### 2.1 Pending LIMIT BUY (active exchange order)

The order has been **submitted to the exchange**.  It has an `orderId`,
an `entry_order_id` in Supabase, and appears in the Binance order book.

```
entry_status = NEW
exit_status  = OPEN
raw_entry_order = { exchange response + pending_entry_guard }
```

This is the subject of this policy document.  It has a live exchange
lifecycle and must be managed deliberately.

### 2.2 Pre-order Candidate Observation (future feature — not implemented)

A candidate that the scanner evaluated but for which **no order was
submitted**.  Currently the system moves directly from candidate
evaluation to order placement.  A pre-order observation layer (where a
candidate is logged without submitting an order) does not exist yet.

This distinction matters: if a coin rallied without a fill it is because
the LIMIT BUY was pending (Section 2.1), **not** because the system
"observed" a missed trade.

---

## 3. State Machine

Every pending NEW entry carries a `pending_entry_guard` block inside
`raw_entry_order`.  States are defined in `StaleEntryState` (str enum,
backward-compatible with string comparisons).

```
ACTIVE_PENDING
    Default for any unfilled NEW order where price hasn't moved much.
    Age < review_after_days AND distance < runaway_distance_pct.

STALE_REVIEW
    Age ≥ review_after_days (default 5d) but price still near entry.
    Informational: zone may still be valid. No automatic action.

RUNAWAY
    Price ≥ runaway_distance_pct (default 10%) above entry.
    Entry zone is likely no longer relevant.
    Revalidation recommended.

EXPIRED
    Age ≥ expire_after_days (default 30d) AND
    distance ≥ cancel_pct (default 40%).
    Setup has aged beyond any reasonable holding window.
    Eligible for WOULD_CANCEL shadow recording.

WOULD_CANCEL
    All cancel conditions are met, but auto-cancel is OFF.
    Shadow record written to Supabase for audit / human review.
    No exchange action.

CANCEL_ELIGIBLE
    Internal action value — all conditions for cancellation met.
    Gated by STALE_ENTRY_AUTO_CANCEL_ENABLED env flag (default: false).

CANCELLED
    Exchange order confirmed cancelled, Supabase updated.
    exit_status → CANCELLED, exit_reason → STALE_SETUP_CANCELLED.
    No PnL recorded (never filled).

FILLED
    Order filled normally before stale conditions were reached.
    Guard becomes irrelevant; OCO protection lifecycle takes over.

RECONCILIATION_REQUIRED
    Exchange or Supabase state mismatch detected during cancel attempt.
    Requires manual review.
```

**State transition diagram:**

```
NEW order created
    ↓
ACTIVE_PENDING
    ↓ age ≥ review_after_days
STALE_REVIEW ──────────────────────────────────────── price fills → FILLED
    ↓ dist ≥ runaway_distance_pct
RUNAWAY
    ↓ age ≥ expire_after_days AND dist ≥ cancel_pct
EXPIRED
    ↓ guard evaluates zone (revalidation)
    ├─ zone still valid → STALE_REVIEW (kept alive)
    └─ zone invalid + fresh reval
         ↓
     CANCEL_ELIGIBLE
         ├─ flag OFF → WOULD_CANCEL  (shadow, no exchange touch)
         └─ flag ON  → cancel exchange → CANCELLED
```

---

## 4. Age vs Runaway — Two Independent Axes

Both dimensions are tracked separately.  Neither alone is sufficient to
cancel.

| Axis | Meaning | Default threshold |
|---|---|---|
| **Age** | Calendar days since order placement | review: 5d, expire: 30d |
| **Distance** | (current_price − entry) / entry × 100 | runaway: 10%, cancel: 40% |

A 3-day-old order at +50% runaway gets REVALIDATE, not CANCEL_ELIGIBLE —
because it hasn't aged enough (min_age_days=3).

An 80-day-old order at +1% distance gets STALE_REVIEW lifecycle but
action=NONE — because price is still near entry and might fill.

Both age ≥ expire_after_days **AND** distance ≥ cancel_pct must be true
(plus a fresh bad zone revalidation) to reach CANCEL_ELIGIBLE.

---

## 5. Shadow Evaluation — How It Works Today

`check_positions()` in `SpotPositionMonitor` runs Step 1.5 for every
NEW order:

```python
if entry_status == "NEW" and price_map.get(sym) is not None:
    terminalized = self._handle_stale_pending_entry(trade, price_map[sym])
```

`_handle_stale_pending_entry` calls `evaluate()`, writes the guard state
into `trade["raw_entry_order"]["pending_entry_guard"]` in-memory, and
sets `log_dirty = True` if the guard changed.  At the end of
`check_positions()`, the updated `raw_entry_order` is persisted to
Supabase via `update_spot_by_order_id`.

**Current limitation:** `_persist_pending_guard` (the immediate Supabase
write) is only called when the system is about to cancel.  Shadow
evaluations that set WOULD_CANCEL are only persisted at the end of the
full `check_positions()` cycle.  This means a mid-cycle failure leaves
the in-memory guard state unwritten.  Known gap; acceptable for now.

---

## 6. Why Old Orders Had `pending_entry_guard = None`

Commit `6b9de95` added `pending_entry_guard` to the order record at
**creation time** (`spot_trade_repository.log_trade()`).  Orders placed
before this commit have only the raw Binance exchange response in
`raw_entry_order`, with no `pending_entry_guard` key.

Additionally, even for post-commit orders, shadow evaluations (WOULD_CANCEL
state) are not eagerly persisted — they are written at end-of-cycle in the
batch `update_spot_by_order_id` call that writes all lifecycle fields.
If `check_positions` has never successfully run to completion for an order,
the guard field stays at its creation value (`{"state": "NONE", ...}`).

**Backfill** (`scripts/backfill_stale_pending_guard.py`) addresses this:
it evaluates all 23 pending orders and writes their guard state to
Supabase without touching exchange orders.

---

## 7. Configurable Thresholds

All thresholds are read from environment variables at runtime.  Change
them in `.env` (or VM environment) without code changes.

| Env var | Default | Meaning |
|---|---|---|
| `STALE_ENTRY_REVIEW_PCT` | `20` | Distance % that triggers REVIEW_REQUIRED action |
| `STALE_ENTRY_REVALIDATE_PCT` | `30` | Distance % that requires zone recheck |
| `STALE_ENTRY_CANCEL_PCT` | `40` | Distance % at which cancel is eligible |
| `STALE_ENTRY_MIN_AGE_DAYS` | `3` | Minimum age before revalidation is trusted |
| `STALE_ENTRY_REVALIDATION_HOURS` | `24` | Zone recheck result freshness window |
| `STALE_ENTRY_REVIEW_AFTER_DAYS` | `5` | Age that sets lifecycle to STALE_REVIEW |
| `STALE_ENTRY_EXPIRE_AFTER_DAYS` | `30` | Age + distance that sets lifecycle to EXPIRED |
| `STALE_ENTRY_RUNAWAY_DISTANCE_PCT` | `10` | Distance that sets lifecycle to RUNAWAY |
| `STALE_ENTRY_AUTO_CANCEL_ENABLED` | `false` | **Master switch — exchange cancel ON/OFF** |

The `StaleEntryThresholds` dataclass in `stale_pending_entry_guard.py`
documents the full set and supports constructor-level overrides for tests.

---

## 8. Future Expiry Policy — Proposed (NOT ACTIVE)

When HumanDirector approves production expiry, the recommended policy is:

```
if age ≥ expire_after_days AND distance ≥ cancel_pct:
    → revalidate zone (check if original support zone still exists)
    if zone_valid == False AND revalidation_fresh:
        → CANCEL_ELIGIBLE
        if STALE_ENTRY_AUTO_CANCEL_ENABLED == true:
            → cancel exchange order
            → set entry_status = CANCELED, exit_status = CANCELED
            → set exit_reason = STALE_SETUP_CANCELLED
            → NO PnL recorded (never filled)
            → Telegram notification
```

This is already implemented in `_handle_stale_pending_entry()`.  Only
the env flag needs to be flipped.

**Before activating:**
1. Run `--check-positions` manually and review all WOULD_CANCEL orders.
2. Confirm zone revalidation logic works for current market conditions.
3. Set `STALE_ENTRY_AUTO_CANCEL_ENABLED=true` in staging env first.
4. Observe one full cycle before enabling on production VM.
5. Get explicit HumanDirector approval.

---

## 9. Why Historical Orders Are Retained

Cancelled or runaway orders provide valuable research data:

- **Missed-entry observation:** which setups the system proposed but
  market moved away from — useful for understanding entry zone validity
  over time.
- **Zone analysis:** how often a scanner-identified zone was "wrong"
  (price never returned) vs "right" (eventually filled or briefly visited).
- **ML training signal:** `exit_reason = STALE_SETUP_CANCELLED` + distance
  at cancel time is a legitimate data point for future entry-quality models.

`exit_status = CANCELED` + `exit_reason = STALE_SETUP_CANCELLED` is the
correct classification.  These rows must **not** be included in strategy
PnL calculations (filter: `exit_status IN ('TP_HIT', 'SL_HIT')`).

---

## 10. Pre-order Candidate Observation (Gap / Future Feature)

Currently the system flow is:

```
scanner → candidate → LIMIT BUY submitted → NEW → (fill / stale)
```

There is no layer between candidate detection and order placement.
This means:

- If a coin rallies before the next cycle's `--propose-all`, no record
  exists of the "missed" candidate.
- The only evidence of a runaway is a pending NEW order that never filled.

A future **pre-order observation** layer would:
- Log candidates that passed scanning but were not submitted (e.g. capital
  budget exhausted, or a "watch mode").
- Enable calculation of true "fill rate" vs "miss rate" per zone type.
- Not submit any exchange order — pure observation.

This is documented here as a gap.  Do not implement without a separate
design task.

---

## 11. Current Status (26 Sep 2026)

```
STALE_ENTRY_AUTO_CANCEL_ENABLED = false  (shadow/research mode)
```

| Category | Count | Examples |
|---|---|---|
| STALE_RUNAWAY (≥30d, ≥10% dist) | 15 | NEAR +201%, UNI +149%, FF +134% |
| RUNAWAY (<30d, ≥10% dist) | 4 | SUIUSDT +51%, SOLUSDT +22% |
| MODERATE (5–10% dist) | 3 | SPCXBUSDT +8%, SNDKBUSDT +9% |
| STALE_REVIEW (≥30d, <10% dist) | 1 | QQQBUSDT +7% |
| WOULD_CANCEL | 0 (auto-cancel OFF) | — |
| Exchange orders modified | 0 | — |

All 23 rows now have `pending_entry_guard` populated in Supabase.
Exchange orders are unchanged.  Awaiting HumanDirector decision on
whether to activate the expiry policy.
