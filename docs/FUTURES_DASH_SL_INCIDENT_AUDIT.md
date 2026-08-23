# Futures DASHUSDT SL Incident Audit

Audit date: 2026-08-23 (Asia/Jakarta)

Scope: read-only investigation using Binance Futures Testnet and Supabase as sources of truth. No order, position, database row, production code, deployment, or repository history was changed.

## 1. Executive Summary

**DASHUSDT is not open on Binance Futures Testnet.** The dashboard display of a SHORT position at approximately 40.88–41.20 is stale because Supabase trade `id=104` remains `exit_status=OPEN` after the exchange SL closed the entire position.

The planned SL worked correctly:

- Expected trigger: `36.49`
- Actual algo type: `STOP_MARKET`
- Actual trigger: `36.49`
- Trigger basis: `MARK_PRICE`
- Exit side: `BUY` for a SHORT
- `positionSide=BOTH`
- `reduceOnly=true`
- Protected quantity: `1.010`, equal to the filled/live position quantity
- Status: `FINISHED`
- Generated market order: `1294416214`
- Fill: `1.010 @ 36.27`
- Realized PnL reported by exchange fill: `-0.68680000 USDT`

The exchange position was closed on 2026-08-22 04:15:08 WIB. The current mark observed during this audit was approximately 41.20, but there was no DASH position and no open DASH order at that time.

The failure is lifecycle reconciliation, not protection execution. The Supabase row currently has `exit_orders_placed=false`. On every monitor cycle, this sends the row through `place_exit_orders()`. Binance Testnet no longer returns a zero-quantity DASH position row, so the code returns `UNKNOWN` on `position_error` before honoring the already-observed terminal SL state. Supabase now records 38 consecutive unknown cycles and remains OPEN.

Supported classifications:

- `STALE_DASHBOARD_ONLY` — dashboard reflects the stale Supabase row, not exchange state.
- `STALE_DATABASE` — Supabase remains OPEN despite a confirmed terminal SL fill.
- `RECONCILIATION_BUG` — terminal algo evidence is suppressed by the missing position-row error and by the `exit_orders_placed` gate.

Not supported by evidence:

- SL missing, rejected, wrong trigger, wrong semantics, failed execution, canceled/expired, quantity undercoverage, emergency guard failure, old runtime, or exchange anomaly.

## 2. Actual Exchange State

Read-only exchange queries returned:

| Field | Result |
|---|---|
| DASH actually open | **NO** |
| Position-information result | No DASH row, including the all-position query |
| Current position quantity | `0` (no position row) |
| Open regular DASH orders | 0 |
| Open DASH algo orders | 0 |
| Current mark during audit | `41.20000000` |

Because no live position exists, entry price, unrealized PnL, liquidation price, leverage, and margin mode are not currently returned for DASH. The values shown by the dashboard are historical values computed from the stale database row.

Historical exchange evidence for the incident position:

| Item | Exchange value |
|---|---|
| Entry order | `1294225777` |
| Entry side/type | SELL LIMIT |
| Entry status | FILLED |
| Fill | `1.010 @ 35.59` |
| TP algo | `1000000176261315` |
| TP status | CANCELED |
| SL algo | `1000000176261298` |
| SL status | FINISHED |
| SL actual market order | `1294416214` |
| SL fill | `1.010 @ 36.27` |
| Exchange realized PnL | `-0.68680000 USDT` |

## 3. Supabase State

Supabase contains four DASH trades. The earlier three are resolved. The incident is row `id=104`.

| Field | Supabase value | Assessment |
|---|---|---|
| `id` | 104 | Incident row |
| `position_side` | SHORT | Correct |
| `entry_status` | FILLED | Correct |
| `exit_status` | OPEN | **Stale** |
| `exit_reason` | null | **Missing** |
| `entry_order_id` | `1294225777` | Correct |
| `entry_price` / fill | `35.59` | Correct |
| `entry_qty` | `1.01` | Correct |
| `sl` | `36.49` | Correct |
| `tp1` | `33.12333333` | Correct planned value |
| `sl_algo_id` | `1000000176261298` | Correct |
| `tp_algo_id` | `1000000176261315` | Correct |
| `exit_orders_placed` | false | Inconsistent with persisted IDs/history |
| `exit_price` / `exit_time` | null | **Stale/missing** |
| realized PnL | null | **Stale/missing** |

The latest persisted protection metadata is:

- State: `UNKNOWN`
- Error: no DASH position row for `positionSide=BOTH`
- `unknown_protection_cycles=38`
- Position quantity and mark price: null
- Persisted TP order quantity: `1.01`
- Last row update: 2026-08-23 18:00:32 WIB

This metadata proves the active runtime continues to process the stale row hourly but cannot resolve it.

The immutable research snapshot exists and is internally consistent. It is unrelated to the lifecycle failure.

## 4. Lifecycle Timeline

All times below are derived from persisted exchange or database timestamps.

| Time (WIB) | Evidence-backed event |
|---|---|
| 2026-08-21 16:01:12 | Candidate decision/research snapshot recorded |
| 2026-08-21 16:01:34 | Pre-submit timestamp and SELL LIMIT entry order created |
| 2026-08-22 03:20:20 | Entry order fully filled: `1.010 @ 35.59` |
| 2026-08-22 04:00:28 | SL algo created: `1000000176261298` |
| 2026-08-22 04:00:29 | TP algo created: `1000000176261315` |
| 2026-08-22 04:15:08.072 | SL triggered at mark-price condition 36.49 |
| 2026-08-22 04:15:08.111 | Generated BUY MARKET filled `1.010 @ 36.27` |
| 2026-08-22 04:15:08.149 | SL algo reached `FINISHED` |
| Unknown | `exit_orders_placed` became/remained false; no historical Supabase revisions are available to prove the precise transition |
| 2026-08-23 18:00:32 | Supabase still OPEN; protection state UNKNOWN, cycle 38 |
| 2026-08-23 18:27:09 | TP algo update shows CANCELED |

There is a roughly 40-minute interval between entry fill and protection creation because fill/protection discovery occurs on the hourly monitor cycle. No evidence shows a loss during that interval, and the eventual SL executed correctly, but the interval is a separate operational exposure worth tracking.

No persisted evidence establishes exactly when or why `exit_orders_placed` changed to false. That uncertainty does not affect the proven root cause: current reconciliation has sufficient terminal SL evidence but fails to consume it.

## 5. SL Order Evidence

The SL order is semantically correct for a one-way-mode SHORT:

| Property | Actual | Correctness |
|---|---|---|
| Type | `STOP_MARKET` | Correct |
| Side | BUY | Correct SHORT exit side |
| Position side | BOTH | Correct for one-way mode |
| Trigger | 36.49 | Matches plan |
| Working type | MARK_PRICE | Intended configuration |
| Price protection | false | Accepted; no protected-price suppression |
| Reduce-only | true | Correct |
| Close-position flag | false | Quantity-based reduce-only order |
| Quantity | 1.010 | Full position coverage |
| Status | FINISHED | Executed terminal state |
| Actual type | MARKET | Expected STOP_MARKET child behavior |
| Actual price | 36.27 | Confirmed by account trade fill |

The SL should have fired when mark price reached/crossed 36.49 for the SHORT, and it did. It did not remain ACTIVE at 40.88. The premise that an active SL failed beyond its trigger is disproven by exchange history.

Quantity precision was not a factor. DASH position and both exit legs used `1.010`; no remainder was left open. The previously identified LTC/DOT one-step float-floor issue did not occur on this DASH trade.

## 6. Reconciliation State

The intended state model includes PENDING, FULLY_PROTECTED, SL_ONLY, TP_ONLY, UNPROTECTED, UNKNOWN, STALE_QTY, and POSITION_CLOSED.

For current DASH evidence, the correct lifecycle conclusion is:

1. SL algo is terminal/FINISHED.
2. Its child market fill closed the full position.
3. Exchange has no remaining DASH position.
4. Database should resolve to `SL_HIT`, `exit_reason=EXCHANGE_SL`, exit price `36.27`, exchange exit timestamp, and actual realized PnL.

The code currently fails through two interacting gates:

1. Step 3 checks TP/SL terminal status only when `exit_orders_placed` is true.
2. Because DASH has `exit_orders_placed=false`, Step 2 calls `place_exit_orders()`.
3. `place_exit_orders()` queries both position and algo orders. It can identify the SL as TERMINAL.
4. However, it checks `position_error` before the terminal-leg condition.
5. Binance Testnet returns no DASH position row after closure, producing `position_error`.
6. The function returns UNKNOWN with `terminal_order_seen=false`, discarding the stronger terminal evidence.
7. The row repeats this path every cycle and never reaches the monitor's terminal-fill persistence branch.

This is a deterministic reconciliation ordering bug triggered by the Testnet behavior of omitting a closed symbol row.

## 7. Emergency Guard Behavior

Current monitor price guard:

- For SHORT, breach is `current >= planned SL`.
- It has no tolerance/buffer.
- It runs during the hourly position-monitor cycle.
- It requires `entry_status=FILLED`, `exit_status=OPEN`, a current price, and `exit_orders_placed=true`.
- It submits a reduce-only BUY MARKET if those conditions hold.

The executor's recovery path also prioritizes emergency closure when it proves the SL is missing and the current price has breached the SL.

For DASH, the emergency guard should **not** act now because the exchange position is already closed. A duplicate reduce-only market order would be incorrect and should fail/no-op. The stale dashboard is not evidence of guard failure.

General hardening observation: an ACTIVE-but-demonstrably-breached SL should be re-queried and the actual live position checked rather than trusted indefinitely. That scenario did not occur here; DASH SL is FINISHED.

## 8. Runtime/Deployment Verification

Evidence:

- Local `main`, `origin/main`, and `HEAD` are `21b5c29`.
- `21b5c29` contains the prior idempotent protection commit `76f04f6` in its ancestry.
- GitHub Actions deploy run 19 for full SHA `21b5c29cbd252bea5594ce6981ffc73462065309` completed successfully on 2026-08-20. Its `Deploy via SSH`, container health check, and Supabase heartbeat health check steps all succeeded; rollback was skipped.
- The DASH row contains `research_snapshot_version=futures_pre_submit_v1`, decision/pre-submit fields, and client algo IDs introduced by the deployed patches.
- Runtime heartbeat was fresh at 2026-08-23 18:00:37 WIB.
- The same cycle updated DASH protection metadata to UNKNOWN and incremented its counter, proving the current protection/reconciliation code path is active.

Conclusion: `OLD_RUNTIME_DEPLOYED` is not supported. Commit `21b5c29` was successfully deployed and runtime behavior contains its features.

Limitation: the heartbeat schema does not store a commit hash, and no direct VM/container SSH target was available from repository configuration during this audit. Therefore the container filesystem hash was not independently read. The successful SSH deploy and health checks plus runtime feature evidence strongly support, but do not cryptographically prove, that the container is exactly `21b5c29`.

## 9. Root Cause

Primary classifications:

### `STALE_DATABASE`

Supabase row 104 remains OPEN with null exit fields even though exchange evidence proves a full SL close.

### `RECONCILIATION_BUG`

Terminal SL evidence is not processed when `exit_orders_placed=false`, and the executor returns early on an absent post-close position row before honoring the terminal algo state.

### `STALE_DASHBOARD_ONLY`

The dashboard is faithfully rendering stale Supabase data. Its displayed current price and calculated unrealized loss do not correspond to a live exchange position.

Explicitly rejected classifications:

- `SL_NOT_CREATED`: SL exists in exchange history.
- `SL_VERIFICATION_FALSE_POSITIVE`: it actually triggered and filled.
- `SL_CREATED_WRONG_TRIGGER`: trigger exactly matches 36.49.
- `SL_CREATED_WRONG_SEMANTICS`: side, position side, reduce-only, working type, and order type are correct.
- `SL_FAILED_TO_EXECUTE`: full fill is confirmed.
- `SL_CANCELED_OR_EXPIRED`: SL status is FINISHED, not canceled/expired.
- `PARTIAL_QTY_UNPROTECTED`: full 1.010 quantity closed.
- `EMERGENCY_GUARD_GAP`: no live position remained for an emergency close.
- `OLD_RUNTIME_DEPLOYED`: deployment/runtime evidence contradicts it.
- `TESTNET_EXCHANGE_ANOMALY`: omission of a zero-position row is a behavior the reconciliation must handle, but the exchange correctly executed the protection order.

## 10. Recommended Fix

Do not change strategy, ranking, sizing, leverage, SL/TP prices, ML, or research behavior.

Smallest safe lifecycle fix:

1. Always inspect persisted TP/SL IDs for terminal states before attempting protection recovery, regardless of `exit_orders_placed`.
2. In `place_exit_orders()`, prioritize a reconciled terminal leg before returning UNKNOWN for a missing position row.
3. When a terminal SL/TP exists, return `terminal_order_seen=true` so the monitor resolves the exchange fill instead of creating/recovering protection.
4. Treat “position row absent” as a post-close reconciliation case only after checking persisted algo IDs and account fills; do not blindly mutate.
5. Persist the actual terminal fill and canonical `exit_reason` immediately.

The most localized implementation options are:

- Move the terminal-state check above the `position_error` return in the executor and preserve `terminal_order_seen`; and/or
- Remove the `exit_orders_placed` gate from terminal-order status inspection in the monitor, using the presence of persisted TP/SL IDs as the gate.

The monitor-first option is stronger because exit resolution is lifecycle work, while `place_exit_orders()` should remain protection reconciliation/creation logic. Both paths must remain idempotent and must never create an order after a terminal leg is observed.

Separately, store runtime commit SHA in heartbeat/deployment metadata for future definitive version audits. This is observability hardening, not part of the DASH lifecycle fix.

## 11. Regression Tests Needed

1. `exit_orders_placed=false`, persisted SL algo ID is FINISHED, position endpoint returns no row: resolve `SL_HIT` with `EXCHANGE_SL`.
2. Same scenario for terminal TP: resolve `TP_HIT` with `EXCHANGE_TP`.
3. Missing position row plus terminal leg must not create any TP, SL, or market order.
4. Missing position row and no terminal evidence remains UNKNOWN/reconciliation-required; no blind mutation.
5. `exit_orders_placed=false` must not prevent querying persisted TP/SL IDs.
6. Full SL fill must persist exchange exit price/time and actual realized PnL/fill-derived PnL.
7. Repeated monitor cycles after resolution must be idempotent and emit no duplicate Telegram event.
8. Active fully matching protection remains unchanged.
9. ACTIVE SL beyond trigger plus live position still open forces a fresh status/position/fill reconciliation before any fail-safe action.
10. Quantity coverage tests use Decimal-safe normalization, including LTC `0.700/0.001` and DOT `41.3/0.1`; this is separate from the DASH root cause.

## Audit Safety Count

- Trades placed: 0
- Orders created: 0
- Orders canceled: 0
- Positions modified/closed: 0
- Database mutations: 0
- Production code changes: 0
- Commits/pushes: 0
