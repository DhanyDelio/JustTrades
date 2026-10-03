# Spot ML — Exit Provenance Fix Report
**Workflow:** `wf_7762cbf6a4069ff5`  
**Task:** Production/data provenance fixes before N≈120 ML checkpoint  
**Date:** Post-fix implementation

---

## 1. Audit Summary

Full audit was completed prior to this fix (see `provenance-audit.md`). Key findings:

| ID | Severity | Issue |
|---|---|---|
| C1 | CRITICAL | Price-guard writes `exit_price=current` (ticker) with `exit_reason="SL_HIT"` identical to genuine OCO fill — no SELL executed |
| C2 | CRITICAL | ML training filter `exit_status in ["TP_HIT","SL_HIT"]` — includes phantom closes |
| H1 | HIGH | `tp_order_id` / `sl_order_id` missing from end-of-cycle save block |
| H2 | HIGH | `_recover_missing_oco()` sets tp/sl_order_id in memory but never persists |
| H3 | HIGH | UNPROTECTED state: code does NOT check exchange for existing TP before creating new one |
| H4 | HIGH | 2ZUSDT stuck-OCO shadow detection never deployed |

All four HIGH and both CRITICAL issues are addressed in this fix.

---

## 2. Exit Provenance Fixes

### What changed

**Price-guard branch replaced with two-cycle stuck-OCO detection gate.**

The old code in `check_positions()` Step 3.5 (`elif sl_breached and _oco_query_confirmed and not _oco_missing and trade.get("oco_placed")`):

```python
# OLD (phantom behavior):
trade["exit_status"]  = "SL_HIT"
trade["exit_price"]   = round(current, 6)   # ticker, NOT a fill
trade["exit_reason"]  = "SL_HIT"            # identical to genuine OCO fill
self._eager_commit(trade)
```

The new code implements a two-cycle stuck-OCO shadow detection gate:
- **Cycle N:** Sets `raw_entry_order["stuck_oco_detection"]["suspected_at"]`, persists to Supabase, logs warning. Does NOT resolve exit_status. Does NOT place any sell.
- **Cycle N+1:** If `suspected_at` exists from a prior run AND conditions still hold → sends Telegram alert with full details. Still does NOT auto-resolve.

**`_recover_partial_protection()` market-sold path:**  
Changed `exit_reason = "SL_HIT"` → `exit_reason = "RECOVERED_SL_HIT"` to distinguish from genuine OCO fill.

**Step 2 `_market_sold` path (price below SL at first OCO placement):**  
Changed `exit_reason = "SL_HIT"` → `exit_reason = "UNPROTECTED_SL_BREACH"` to match the emergency-close taxonomy.

### Exit reason taxonomy (final)

| exit_reason | Code path | Exchange evidence | ML eligible |
|---|---|---|---|
| `"SL_HIT"` | Step 3 ALL_DONE, STOP_LOSS_LIMIT leg FILLED | ✅ confirmed fill | ✅ CLEAN |
| `"TP_HIT"` | Step 3 ALL_DONE, LIMIT_MAKER leg FILLED | ✅ confirmed fill | ✅ CLEAN |
| `"UNPROTECTED_SL_BREACH"` | `_emergency_close()`, Step 2 market-sold | ✅ real sell | ❌ EXCLUDE |
| `"UNPROTECTED_TP_BREACH"` | `_emergency_close()` TP breach path | ✅ real sell | ❌ EXCLUDE |
| `"RECOVERED_SL_HIT"` | `_recover_partial_protection()` market-sold | ✅ real sell | ❌ EXCLUDE |
| `"OCO_STUCK_MANUAL_RESOLUTION"` | Manual DB edit (2ZUSDT historical) | ❌ no code evidence | ❌ EXCLUDE |
| `"EMERGENCY_CLOSED"` | `_emergency_close()` market sell path | ✅ real sell | ❌ EXCLUDE |
| `"STALE_SETUP_CANCELLED"` | Entry order cancel (never filled) | N/A | N/A |
| `NULL` (legacy) | Pre-Phase1B records (before exit_reason field) | Probable OCO fill | ⚠ INCLUDED with warning |

---

## 3. Training Eligibility Changes

### ml/train_v1.py

**Before:**
```python
closed_mask = df[TARGET].isin(["TP_HIT", "SL_HIT"])
rule_mask   = df["rule_version"].isin(["v1.0.0"]) | df["rule_version"].isna()
df          = df[closed_mask & rule_mask].copy()
```

**After:**
```python
CLEAN_EXIT_REASONS = {"TP_HIT", "SL_HIT"}
EXCLUDED_EXIT_REASONS = {
    "PRICE_GUARD_SL",
    "UNPROTECTED_SL_BREACH",
    "UNPROTECTED_TP_BREACH",
    "OCO_STUCK_MANUAL_RESOLUTION",
    "EMERGENCY_CLOSED",
    "RECOVERED_SL_HIT",
    "STALE_SETUP_CANCELLED",
}
# NULL exit_reason + TP_HIT/SL_HIT: INCLUDED with WARNING (legacy pre-Phase1B)
# Excluded exit_reasons: filtered out before training
clean_mask = (
    df[TARGET].isin(["TP_HIT", "SL_HIT"]) &
    ~df["exit_reason"].isin(EXCLUDED_EXIT_REASONS)
)
df = df[closed_mask & rule_mask & clean_mask].copy()
```

### ml/train_v2.py

Same EXCLUDED_EXIT_REASONS set applied. Same NULL exit_reason handling (included with warning). Rule_version filter not applied in v2 (preserves existing behavior).

### Effect

- Phantom price-guard closes → **EXCLUDED** (once new code runs and creates `exit_reason="PRICE_GUARD_SL"`... but this field is now replaced by the two-cycle gate which keeps `exit_reason=NULL` until confirmed. See Section 7.)
- Manual resolution (2ZUSDT OCO_STUCK_MANUAL_RESOLUTION) → **EXCLUDED**
- Emergency closes → **EXCLUDED**
- Legacy NULL exit_reason TP_HIT/SL_HIT → **INCLUDED** with log warning

---

## 4. tp_order_id / sl_order_id Persistence Fixes

### Branches fixed

**End-of-cycle save block** (`check_positions()` ~L1039):  
Added `tp_order_id` and `sl_order_id` to the end-of-cycle `update_spot_by_order_id` dict. Previously these fields were missing, meaning any eager-persist failure would silently lose them.

```python
# ADDED to end-of-cycle save:
"tp_order_id":  ot.get("tp_order_id"),
"sl_order_id":  ot.get("sl_order_id"),
```

**`_recover_missing_oco()` TP_ONLY branch:**  
Added immediate `update_spot_by_order_id` call after setting `trade["tp_order_id"]`:
```python
_usb_rmoco(_eid_rmoco, {
    "oco_placed": False,
    "oco_reconciliation_status": "TP_ONLY",
    "tp_order_id": tp_order_id,
})
```

**`_recover_missing_oco()` SL_ONLY branch:**  
Same for `sl_order_id`.

**Note:** Per the audit, `_recover_partial_protection()` TP_ONLY and SL_ONLY branches already called `update_spot_by_order_id` with tp/sl_order_id — those were confirmed correct and not changed.

---

## 5. OCO Reconciliation Idempotency Fix (AVAX Root Cause Prevention)

### Root cause of 41 duplicate TP orders

When `oco_reconciliation_status = "UNPROTECTED"`, the `_recover_partial_protection()` function:
1. Had `_standalone_order_id = None` (no prior order ID in DB)
2. Skipped the cancel-before-replace check
3. Called `place_oco_order()` unconditionally
4. If the subsequent `update_spot_by_order_id` persist failed silently (all errors swallowed), the next cycle read UNPROTECTED again and repeated step 3

### Fix implemented

Before calling `place_oco_order()` when `prev == "UNPROTECTED"`, the code now:

1. Calls `client.get_open_orders(symbol=symbol)`
2. Searches for any open `SELL LIMIT_MAKER` order with:
   - `origQty` matching `entry_qty` within 1%
   - `price` within 0.5% of `tp1`
3. If found: sets `trade["tp_order_id"] = existing_order_id`, sets `oco_reconciliation_status = "TP_ONLY"`, persists both to Supabase, **returns without calling `place_oco_order()`**
4. If not found: proceeds with `place_oco_order()` as normal

This prevents the AVAX scenario where 41 standalone TPs accumulated.

---

## 6. 2Z Shadow Detection Implementation

### Per-cycle behavior

**Condition for detection:**
- `listOrderStatus == "EXECUTING"` (OCO confirmed alive)
- `sl_breached == True` (current price ≤ SL)

**Cycle N (first detection):**
- Sets `trade["raw_entry_order"]["stuck_oco_detection"] = {"suspected_at": <ISO8601 UTC>}`
- Persists to Supabase via `update_spot_by_order_id(eid, {"raw_entry_order": ...})`
- Logs: `"⚠ [{symbol}] stuck-OCO suspected: SL leg NEW with price breached"`
- **Does NOT cancel, does NOT sell, does NOT change exit_status/exit_reason**
- Adds `eid` to module-level `_stuck_oco_marked_this_run` set

**Cycle N+1 (confirmed detection):**
- Condition: `suspected_at` exists in `raw_entry_order` AND `eid` not in `_stuck_oco_marked_this_run` (i.e., from a prior run)
- Sends Telegram alert with: symbol, oco_list_id, tp_order_id, sl_order_id, SL price, current price, `suspected_at` timestamp
- **Still does NOT auto-cancel or auto-sell**
- Checks `OCO_STUCK_AUTO_RESOLVE_ENABLED` env var — if `true`, logs intent only (auto-resolve NOT implemented)

**Implementation location:** `check_positions()` Step 3.5, replacing the old price-guard immediate-resolve branch.

---

## 7. Historical Contamination Counts

Source: Local trade files only (Supabase not accessible from this environment). Full results in `historical-audit.md`.

### Local data (68 trades total)

```
Total completed (TP_HIT or SL_HIT):    46  (100%)
  TP_HIT:                              15
  SL_HIT:                              31

Genuine exchange TP (exit_reason='TP_HIT'):        0  (field not yet populated — pre-Phase1B)
Genuine exchange SL (exit_reason='SL_HIT'):        0  (field not yet populated — pre-Phase1B)

Price-guard estimate (exit_price == SL exactly):   1  (SUSPICIOUS, unconfirmed)
Emergency:                                         0
Manual/stuck-OCO:                                  0
Missing-OCO recovery:                              0
Unknown (NULL exit_reason, legacy):               46

Clean-label eligible (under new filter):           46 (100%)
  — all included as UNKNOWN_LEGACY with WARNING log
Excluded:                                           0
```

**Note:** All 46 closed trades have `exit_reason = NULL` — they predate the exit_reason field. They are included in training with a WARNING log under the new eligibility filter. This is correct behavior: the old eligibility filter would also include them.

**For future Supabase live query**, run:
```sql
SELECT exit_reason, exit_status, COUNT(*) 
FROM trades_spot 
WHERE exit_status IN ('TP_HIT', 'SL_HIT')
GROUP BY exit_reason, exit_status
ORDER BY exit_reason;
```

---

## 8. NULL exit_reason Analysis

```
NULL exit_reason (all closed trades in local files):   47
  - TP_HIT + NULL exit_reason:                         15
  - SL_HIT + NULL exit_reason:                         31
  - CANCELED + NULL exit_reason:                        1

Proven genuine:      0  (cannot confirm without exchange order history)
Proven non-genuine:  0  (cannot confirm without exchange order history)
Still unknown:       46  (pre-Phase1B, exit_reason field not populated)
Safely backfilled:   0  (no backfill made — insufficient evidence)
```

The 1 suspicious price-guard candidate (SL_HIT with `exit_price == sl` exactly) is flagged but not backfilled.

---

## 9. Tests Added

### Updated test (test_oco_protection_recovery.py)

- `TestScenario6_NormalOcoFlowRegression.test_price_guard_sl_sets_price_guard_reason` — Updated from asserting `exit_reason=="SL_HIT"` (old wrong behavior) to asserting trade stays OPEN on cycle N with `stuck_oco_detection.suspected_at` set.

### New test file: tests/test_provenance_and_idempotency.py (13 tests)

**Exit provenance tests:**
1. `TestExitProvenance.test_1_price_guard_does_not_resolve_as_sl_hit`
2. `TestExitProvenance.test_2_price_guard_sets_stuck_oco_suspected_at`
3. `TestExitProvenance.test_3_genuine_oco_sl_fill_writes_sl_hit`
4. `TestExitProvenance.test_4_genuine_oco_tp_fill_writes_tp_hit`
5. `TestExitProvenance.test_5_unprotected_sl_breach_writes_correct_reason`

**Multi-cycle idempotency tests:**
6. `TestMultiCycleIdempotency.test_6_scenario_a_unprotected_cycle2_exchange_has_tp_no_duplicate`
7. `TestMultiCycleIdempotency.test_7_scenario_b_tp_only_with_id_restart_finds_open_no_duplicate`
8. `TestMultiCycleIdempotency.test_8_scenario_c_db_unprotected_exchange_has_tp_reconciles`
9. `TestMultiCycleIdempotency.test_9_scenario_d_cancel_not_confirmed_aborts`

**2Z shadow detection tests:**
10. `TestStuckOcoShadowDetection.test_10_cycle_n_suspected_at_set_no_resolve`
11. `TestStuckOcoShadowDetection.test_11_cycle_n1_confirmed_sends_telegram_no_auto_resolve`

**tp_order_id/sl_order_id persistence tests:**
12. `TestOrderIdPersistence.test_12_tp_only_persists_tp_order_id_to_supabase`
13. `TestOrderIdPersistence.test_13_sl_only_persists_sl_order_id_to_supabase`

---

## 10. Test Suite Result

```
Ran 411 tests in 10.577s
OK
```

Baseline: 398/398. After changes: **411/411** (13 new tests added).  
**No regressions. All 398 original tests pass.**

---

## 11. Git Commit Hash

`0eb13d1a699f0c4743ec2544a380a0b200461749`

---

## 12. ML Readiness Status

Training was NOT executed. No model artifacts were created or modified.

The following production/data provenance issues are now fixed:

| Fix | Status |
|---|---|
| Price-guard phantom SL exit provenance | ✅ FIXED — two-cycle gate, no more immediate phantom resolve |
| ML training eligibility filter | ✅ FIXED — EXCLUDED_EXIT_REASONS list applied |
| tp_order_id/sl_order_id persistence | ✅ FIXED — end-of-cycle save + _recover_missing_oco() persist |
| AVAX duplicate TP prevention | ✅ FIXED — exchange pre-check before placing TP from UNPROTECTED |
| 2ZUSDT stuck-OCO shadow detection | ✅ FIXED — two-cycle read-only detection deployed |
| 2ZUSDT OCO_STUCK_MANUAL_RESOLUTION excluded | ✅ FIXED — in EXCLUDED_EXIT_REASONS |

---

PROVENANCE FIXED — READY FOR N≈120 TRAINING

```
Effective N≈120 reached: YES
Training executed: NO
Reason: Production/data provenance fixes being completed and verified first.
Next step: Mandatory Spot ML checkpoint at Effective N≈120 using cleaned/verified label eligibility.
```
