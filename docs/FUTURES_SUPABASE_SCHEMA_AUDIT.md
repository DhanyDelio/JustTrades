# Futures Supabase Schema Audit

Audit date: 2026-08-20 (Asia/Jakarta)
Scope: actual Supabase PostgREST/OpenAPI metadata, current repository SQL/mappers, and the uncommitted Phase 1B diff.
Safety: read-only metadata and row access only. No SQL was executed, no schema/data mutation occurred, and no production code was edited during this audit.

## 1. Executive Summary

The pre-migration `public.trades_futures` table exposed **57 columns**. After the approved migration, a second read-only OpenAPI check confirmed **69 columns**, including all 12 Phase 1B fields. Existing columns for final entry/SL, TP, planned R, risk, ATR%, zones, volatility and `exit_reason` remain canonical.

The final Phase 1B mapping writes `exit_reason` and all 12 research fields as first-class Supabase values. A separate `exit_provenance` column is unnecessary. `raw_entry_order.research_snapshot` remains debug/backward-compatible redundancy.

The minimal required-now migration is 12 nullable columns:

1. `research_snapshot_version`
2. `decision_time`
3. `pre_submit_time`
4. `initial_entry_price`
5. `initial_sl`
6. `initial_risk_pct`
7. `final_rr`
8. `delta_rr`
9. `atr_at_entry`
10. `analysis_candle_open_time`
11. `analysis_candle_close_time`
12. `analysis_candle_closed`

Both candle timestamps and the boolean are independently required. All proposed columns should remain nullable for historical compatibility. Existing structured columns cover every other Phase 1B value.

The runtime mapping was subsequently adjusted to populate these columns on initial insert while retaining the JSON snapshot for debugging/backward compatibility.

## 2. Schema Sources

| Source | Role | Authority/currentness |
|---|---|---|
| Supabase `/rest/v1/` OpenAPI metadata | Actual exposed production table properties, PostgreSQL formats, required/default metadata | **Current primary schema evidence**; queried read-only during this audit |
| `services/supabase_client.py` | Runtime table names and generic select/upsert/update operations | **Current runtime I/O**, but no typed schema declaration |
| `core/repositories/futures_trade_repository.py` | Current initial Futures insert payload | **Current application write contract** |
| `core/executors/futures_position_monitor.py` | Current lifecycle update/eager-terminal payload | **Current application update contract** |
| `core/scanners/futures_candidate_scanner.py` | Source variables for candidate and Phase 1B nested snapshot | **Current/uncommitted application source** |
| `core/executors/futures_order_executor.py` | Pre-submit timestamp and emergency provenance source | **Current/uncommitted application source** |
| `scripts/migration_to_supabase.py::map_futures_record` | One-time local JSON-to-Supabase mapper | **Legacy migration utility**; missing current `exit_reason` and Phase 1B fields, not runtime authority |
| `data/json/trade_futures.json*` | Local historical/fallback records | **Legacy/read fallback**, not authoritative research storage; demonstrably stale in prior audit |
| `docs/migrations/001_add_oco_reconciliation_status.sql` | Adds Spot-only OCO reconciliation field | Current migration artifact but **not relevant to Futures table** |
| `docs/migrations/002_add_spot_exit_reason.sql` | Adds Spot-only exit reason | **Not the source** of the existing Futures `exit_reason` |
| `tests/order_executor_base.py` | Legacy/parallel test helper with its own payload shapes | Test support, **not production schema/runtime authority** |
| `docs/FUTURES_SYSTEM_AUDIT.md`, Phase 1/1B docs | Prior findings | Documentation; useful context, never schema authority |

No repository SQL file creates or alters `trades_futures`. Therefore old mapper field lists cannot establish the complete production schema. The actual OpenAPI metadata is the strongest available evidence short of direct `information_schema` SQL access.

## 3. Actual `trades_futures` Columns

Nullability below is inferred from the OpenAPI `required` list. OpenAPI reports PostgreSQL type formats but does not expose numeric precision/scale, every constraint, identity sequence details, or index definitions. For example, runtime inserts omit `id`, so the database evidently supplies it even though the OpenAPI property default is not descriptive.

| # | Column | PostgreSQL/OpenAPI type | Nullable | Default exposed | Current purpose | Written by runtime? |
|---:|---|---|---:|---|---|---:|
| 1 | `id` | bigint | No | metadata null | Primary key | DB-generated |
| 2 | `symbol` | text | No | — | Instrument | Insert |
| 3 | `position_side` | text | No | — | LONG/SHORT | Insert |
| 4 | `direction` | text | No | — | lower-case direction | Insert |
| 5 | `margin_budget` | numeric | Yes | — | Fixed per-trade margin budget | Insert |
| 6 | `leverage` | smallint | Yes | — | Leverage | Insert |
| 7 | `margin_mode` | text | Yes | — | isolated/cross descriptor | Insert |
| 8 | `rule_version` | text | Yes | — | Strategy rule version | Insert |
| 9 | `correlation_cluster_id` | text | Yes | — | Batch/effective-N group | Insert |
| 10 | `entry_order_id` | bigint | No | — | Exchange entry identity/upsert key | Insert |
| 11 | `entry_client_id` | text | Yes | — | Exchange client order ID | Insert |
| 12 | `entry_status` | text | No | `NEW` | Entry lifecycle | Insert/update |
| 13 | `entry_price` | numeric | Yes | — | Final pre-submit LIMIT entry | Insert |
| 14 | `entry_fill_price` | numeric | Yes | — | Actual fill price | Fill update |
| 15 | `entry_fill_time` | bigint | Yes | — | Fill epoch milliseconds | Fill update |
| 16 | `entry_qty` | numeric | Yes | — | Planned/filled quantity | Insert/fill update |
| 17 | `entry_notional` | numeric | Yes | — | Planned notional | Insert |
| 18 | `margin_used` | numeric | Yes | — | Planned margin | Insert |
| 19 | `open_time` | timestamptz | Yes | — | Insert/log timestamp after order response | Insert |
| 20 | `tp_order_id` | bigint | Yes | — | TP order identity | Monitor update |
| 21 | `sl_order_id` | bigint | Yes | — | SL order identity | Monitor update |
| 22 | `tp_algo_id` | bigint | Yes | — | TP conditional algo identity | Monitor update |
| 23 | `sl_algo_id` | bigint | Yes | — | SL conditional algo identity | Monitor update |
| 24 | `exit_orders_placed` | boolean | No | `false` | Full exit-protection/terminal gate | Insert/update |
| 25 | `sl` | numeric | Yes | — | Final pre-submit SL | Insert |
| 26 | `tp1` | numeric | Yes | — | Executed TP contract | Insert |
| 27 | `tp2` | numeric | Yes | — | Secondary target metadata | Insert |
| 28 | `entry_zone_center` | numeric | Yes | — | Selected final entry-zone center | Insert |
| 29 | `entry_zone_touches` | smallint | Yes | — | Selected entry-zone touches | Insert |
| 30 | `liquidation_price` | numeric | Yes | — | Approximate planned liquidation | Insert |
| 31 | `distance_to_liquidation_pct` | numeric | Yes | — | Approximate liquidation distance | Insert |
| 32 | `planned_rr` | numeric | Yes | — | Initial/pre-anchor planned R | Insert |
| 33 | `risk_pct` | numeric | Yes | — | Final post-anchor/pre-submit risk % | Insert |
| 34 | `max_loss_usd` | numeric | Yes | — | Planned maximum loss | Insert |
| 35 | `zone_type` | text | Yes | — | T1/T2 tier | Insert |
| 36 | `zone_touches` | smallint | Yes | — | Selected zone touches (duplicates entry-zone meaning) | Insert |
| 37 | `atr_pct_at_entry` | numeric | Yes | — | ATR as percent of analysis price | Insert |
| 38 | `volatility_regime_at_entry` | text | Yes | — | low/medium/high/unknown | Insert |
| 39 | `funding_rate_at_entry` | numeric | Yes | — | Funding snapshot | Insert |
| 40 | `funding_rate_paid` | numeric | Yes | — | Accumulated signed funding estimate | Insert/update |
| 41 | `funding_rate_history` | jsonb | No | metadata null | Funding events | Insert/update |
| 42 | `last_funding_check_time` | bigint | Yes | — | Funding cursor epoch ms | Update |
| 43 | `fee_usd_roundtrip` | numeric | Yes | — | Estimated roundtrip fee | Insert |
| 44 | `slippage_pct` | numeric | Yes | — | Entry fill-vs-plan movement | Fill update |
| 45 | `time_in_position_sec` | integer | Yes | — | Holding duration | Exit update |
| 46 | `exit_status` | text | No | `OPEN` | Lifecycle/outcome status | Insert/update |
| 47 | `exit_price` | numeric | Yes | — | Resolved exit price | Exit update |
| 48 | `exit_time` | bigint | Yes | — | Exit epoch milliseconds | Exit update |
| 49 | `realized_pnl_usd` | numeric | Yes | — | Computed realized PnL | Exit update |
| 50 | `realized_pnl_pct` | numeric | Yes | — | Computed realized PnL % | Exit update |
| 51 | `max_adverse_excursion_pct` | numeric | Yes | — | Post-exit reconstructed MAE | Exit update |
| 52 | `max_favorable_excursion_pct` | numeric | Yes | — | Post-exit reconstructed MFE | Exit update |
| 53 | `distance_to_liquidation_pct_min` | numeric | Yes | — | Post-exit closest liquidation distance | Exit update |
| 54 | `raw_entry_order` | jsonb | Yes | — | Raw entry response plus mutable nested metadata | Insert/update |
| 55 | `created_at` | timestamptz | No | `now()` | DB creation timestamp | DB-managed |
| 56 | `updated_at` | timestamptz | No | `now()` | DB update timestamp/default; trigger behavior unverified | DB-managed/unknown |
| 57 | `exit_reason` | text | Yes | — | Canonical exit provenance | Phase 1B insert/update |
| 58 | `research_snapshot_version` | text | Yes | — | Snapshot mapping version | Insert |
| 59 | `decision_time` | timestamptz | Yes | — | Analysis/trade-decision timestamp | Insert |
| 60 | `pre_submit_time` | timestamptz | Yes | — | Immediate pre-submission timestamp | Insert |
| 61 | `initial_entry_price` | numeric | Yes | — | Pre-anchor entry | Insert |
| 62 | `initial_sl` | numeric | Yes | — | Pre-anchor SL | Insert |
| 63 | `initial_risk_pct` | numeric | Yes | — | Pre-anchor/ranking risk | Insert |
| 64 | `final_rr` | numeric | Yes | — | Final pre-submit R:R | Insert |
| 65 | `delta_rr` | numeric | Yes | — | Final minus planned R:R | Insert |
| 66 | `atr_at_entry` | numeric | Yes | — | Absolute decision-time ATR | Insert |
| 67 | `analysis_candle_open_time` | timestamptz | Yes | — | Latest analyzed candle open | Insert |
| 68 | `analysis_candle_close_time` | timestamptz | Yes | — | Latest analyzed candle close | Insert |
| 69 | `analysis_candle_closed` | boolean | Yes | — | Closed-candle provenance flag | Insert |

OpenAPI still marks the original 11 properties required; all 12 Phase 1B columns are nullable.

## 4. Runtime Write Mapping

| Runtime path | Method | Columns/payload responsibility |
|---|---|---|
| Initial insert | `FuturesTradeRepository.log_futures_trade` → `upsert_futures` | Identity, final contract, initial planned R, risk/sizing, ATR%, zones, volatility/funding, OPEN lifecycle, raw JSON |
| Conflict target | `upsert_futures(... on_conflict="entry_order_id")` | Operationally implies a unique/exclusion constraint on `entry_order_id`; exact index definition is not exposed by OpenAPI |
| Fill detection | `FuturesPositionMonitor.check_positions` | `entry_status`, fill price/time/qty, slippage, funding cursor |
| Protection reconciliation | same | TP/SL IDs, `exit_orders_placed`, mutable `raw_entry_order.exit_protection` |
| Funding | same | funding paid/history/check time |
| Terminal eager write | `_eager_commit_futures` | status, reason, exit price/time, PnL and duration |
| Terminal/end-cycle write | `check_positions` update list | Same lifecycle fields plus excursions/protection/funding |
| Read | `fetch_all_futures` | `select(*) order(id)`; no projection/schema validation |
| Dashboard | `load_futures_data` | Read-only consumer; uses a subset of columns |
| Local fallback | `FuturesTradeRepository.load_futures_log` | Reads stale JSON only when Supabase fails; inappropriate as research authority |

The one-time `map_futures_record` migration does not map `exit_reason` and should be treated as legacy. Re-running it against modern records could omit newer fields from its payload, although upsert behavior would only update supplied keys rather than necessarily nulling omitted columns.

## 5. Phase 1B Field Mapping

| Research field | Existing SQL column? | Existing name/equivalent | Currently persisted? | JSON-only now? | New column needed? |
|---|---:|---|---:|---:|---:|
| Snapshot/version identifier | No | — | Yes, nested | Yes | **Yes: `research_snapshot_version`** |
| Decision timestamp | No | `open_time` is too late; `created_at` is DB insert time | Nested `analysis_time`, but not a true named decision boundary | Yes | **Yes: `decision_time`** |
| Pre-submit timestamp | No | — | Nested `pre_submit_time` | Yes | **Yes: `pre_submit_time`** |
| Initial entry | No | `entry_price` is final after re-anchor | Nested | Yes | **Yes: `initial_entry_price`** |
| Final pre-submit entry | Yes | `entry_price` | Structured | Duplicated in JSON | No |
| Initial SL | No | `sl` is final after re-anchor | Nested | Yes | **Yes: `initial_sl`** |
| Final pre-submit SL | Yes | `sl` | Structured | Duplicated in JSON | No |
| TP | Yes | `tp1`, `tp2` | Structured | Duplicated in JSON | No |
| Stored/planned R | Yes | `planned_rr` | Structured | Duplicated as `initial_planned_r` | No |
| Initial risk % | No | `risk_pct` is final post-anchor | Nested | Yes | **Yes: `initial_risk_pct`** |
| Final risk % | Yes | `risk_pct` | Structured | Duplicated as `final_risk_pct` | No |
| Final R | No | — | Nested `final_pre_submit_r` | Yes | **Yes: `final_rr`** |
| Delta R | No | — | Nested `delta_r` | Yes | **Yes: `delta_rr`** |
| ATR absolute | No | — | Nested `atr` | Yes | **Yes: `atr_at_entry`** |
| ATR % | Yes | `atr_pct_at_entry` | Structured | Duplicated as `atr_pct` | No |
| Zone tier | Yes | `zone_type` | Structured | Duplicated as `zone_tier` | No |
| Zone touches | Yes | `entry_zone_touches` and `zone_touches` | Structured twice | Duplicated | No; consider semantics later, do not rename now |
| Candle open time | No | — | Nested epoch ms | Yes | **Yes: `analysis_candle_open_time`** |
| Candle close time | No | — | Nested epoch ms | Yes | **Yes: `analysis_candle_close_time`** |
| Candle closed status | No | — | Nested boolean | Yes | **Yes: `analysis_candle_closed`** |
| Exit provenance/reason | Yes | `exit_reason` | Structured in Phase 1B diff | No | No |
| Volatility regime | Yes | `volatility_regime_at_entry` | Structured | No | No |
| BTC trend/return | No | — | Not computed/persisted by Futures runtime | No | Optional later only |
| Symbol trend/return | No | — | Not computed/persisted | No | Optional later only |
| Directional alignment | No | — | Not computed/persisted | No | Optional later only |
| Candidate rank | No | — | Not persisted | No | Optional later only |
| Candidate pool size | No | — | Not persisted | No | Optional later only |

`decision_time` needs a precise runtime definition before mapping: recommended meaning is “candidate passed final validation and was selected for submission,” not merely chart analysis start. The current nested `analysis_time` should remain distinct debugging provenance or be deliberately mapped only if the team accepts that semantic definition.

## 6. Exit Provenance Mapping

The actual column is `exit_reason TEXT NULL`. It is sufficient as the single canonical provenance field.

| Runtime path in uncommitted Phase 1B code | `exit_status` | `exit_reason` | Supabase mapping status |
|---|---|---|---|
| Terminal TP algo result | `TP_HIT` | `EXCHANGE_TP` | Correct: eager and end-cycle structured update |
| Terminal SL algo result | `SL_HIT` | `EXCHANGE_SL` | Correct |
| Missing SL, breached contract, emergency MARKET | `SL_HIT` | `EMERGENCY_UNPROTECTED` | Correct |
| Monitor price-guard MARKET | `SL_HIT` | `EMERGENCY_PRICE_GUARD` | Correct |
| Position absent without recognized terminal leg | `MANUALLY_CLOSED` | `RECONCILED_EXCHANGE_CLOSE` | Correct |
| New/open record | `OPEN` | null | Correct initial insert |

Both `_eager_commit_futures` and the full end-of-cycle field list include `exit_reason`, preventing the common failure where the in-memory reason exists but is omitted from persistence.

A separate `exit_provenance` would duplicate `exit_reason`, invite disagreement, require precedence rules, and add no current value. If richer evidence is later needed, add a raw immutable exit-event/evidence object or event table—not a second synonymous text column.

## 7. JSON / Raw Metadata Audit

Classification:

- **A:** keep JSON-only (raw/debug)
- **B:** promote to structured SQL and optionally retain JSON copy
- **C:** already duplicated in structured SQL
- **D:** legacy/unused

### `raw_entry_order.research_snapshot`

| JSON key | Class | Reason |
|---|---|---|
| `schema_version` | B | Required for authoritative dataset coverage/version filters |
| `analysis_time` | B/clarify | Valuable market-snapshot time, but not automatically equivalent to decision time |
| `pre_submit_time` | B | Required temporal boundary |
| `last_candle_open_time` | B | Core leakage/provenance control |
| `last_candle_close_time` | B | Core leakage/provenance control |
| `last_candle_was_closed` | B | Core research eligibility flag |
| `symbol` | C | Existing `symbol` |
| `side` | C | Existing `position_side` |
| `initial_entry` | B | Lost from first-class fields after re-anchor |
| `initial_sl` | B | Lost from first-class fields after re-anchor |
| `tp1`, `tp2` | C | Existing columns |
| `initial_planned_r` | C | Existing `planned_rr` |
| `initial_risk_pct` | B | Existing `risk_pct` is final, not initial |
| `atr` | B | Absolute ATR missing |
| `atr_pct` | C | Existing `atr_pct_at_entry` |
| `zone_tier` | C | Existing `zone_type` |
| `final_pre_submit_entry` | C | Existing `entry_price` |
| `final_pre_submit_sl` | C | Existing `sl` |
| `final_pre_submit_r` | B | Core Phase 1B hypothesis field missing structurally |
| `delta_r` | B | Core re-anchor integrity field missing structurally |
| `final_risk_pct` | C | Existing `risk_pct` |
| `entry_zone_center` | C | Existing column |
| `entry_zone_touches` | C | Existing column |

### Other JSON/raw fields

| JSON location | Class | Treatment |
|---|---|---|
| Original exchange entry response keys | A | Keep JSON-only; raw/debug/audit payload |
| `raw_entry_order.exit_protection.state/errors/quantities/mark_price/client IDs` | A | Operational diagnostics; not core pre-entry ML feature |
| `funding_rate_history` | A in dedicated JSONB column | Correctly has its own structured table column; event list is naturally JSON |
| Local `trade_futures.json*` raw payloads | D for research authority | Backward-compatible fallback/legacy only; do not design Phase 2 around them |

The nested snapshot can remain as a redundancy/debug copy, but queryable Phase 1B features should not depend on JSON paths.

## 8. Missing Structured Fields

### Required now

| Proposed column | Type | Why genuinely missing |
|---|---|---|
| `research_snapshot_version` | text | Version/coverage gate for prospective research rows |
| `decision_time` | timestamptz | Explicit selected-candidate decision boundary; `open_time` occurs after order response |
| `pre_submit_time` | timestamptz | Last boundary before exchange configuration/submission |
| `initial_entry_price` | numeric | Pre-anchor entry is otherwise lost |
| `initial_sl` | numeric | Pre-anchor SL is otherwise lost |
| `initial_risk_pct` | numeric | Initial rank input differs from final `risk_pct` |
| `final_rr` | numeric | Recomputed final contract R is core Phase 1B field |
| `delta_rr` | numeric | Quantifies re-anchor change |
| `atr_at_entry` | numeric | Absolute ATR required for normalized distances |
| `analysis_candle_open_time` | timestamptz | Reproducible candle identity/cutoff |
| `analysis_candle_close_time` | timestamptz | Closed-candle safety boundary |
| `analysis_candle_closed` | boolean | Direct eligibility/audit flag |

Naming follows existing conventions: snake_case; `_pct` for percentages; `_time` for timestamps; `numeric` for prices/ratios; nullable text/boolean/timestamptz for prospective fields. `final_rr` and `delta_rr` align with existing `planned_rr`; using `final_r` would introduce mixed terminology.

No NOT NULL constraints or indexes are warranted now. Historical rows must remain valid, and N≈60 queries do not justify new indexes.

## 9. Required-Now Migration

Proposal only—**not executed**:

```sql
-- Futures Phase 1B prospective research fields.
-- All columns are nullable for backward compatibility with historical rows.

ALTER TABLE public.trades_futures
  ADD COLUMN IF NOT EXISTS research_snapshot_version TEXT,
  ADD COLUMN IF NOT EXISTS decision_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS pre_submit_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS initial_entry_price NUMERIC,
  ADD COLUMN IF NOT EXISTS initial_sl NUMERIC,
  ADD COLUMN IF NOT EXISTS initial_risk_pct NUMERIC,
  ADD COLUMN IF NOT EXISTS final_rr NUMERIC,
  ADD COLUMN IF NOT EXISTS delta_rr NUMERIC,
  ADD COLUMN IF NOT EXISTS atr_at_entry NUMERIC,
  ADD COLUMN IF NOT EXISTS analysis_candle_open_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS analysis_candle_close_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS analysis_candle_closed BOOLEAN;

COMMENT ON COLUMN public.trades_futures.research_snapshot_version IS
  'Version of immutable prospective Futures research snapshot mapping.';
COMMENT ON COLUMN public.trades_futures.decision_time IS
  'UTC time when the candidate passed final validation and was selected.';
COMMENT ON COLUMN public.trades_futures.pre_submit_time IS
  'UTC timestamp immediately before exchange configuration/order submission.';
COMMENT ON COLUMN public.trades_futures.initial_entry_price IS
  'Pre-anchor candidate entry from the original market snapshot.';
COMMENT ON COLUMN public.trades_futures.initial_sl IS
  'Pre-anchor stop loss from the original setup.';
COMMENT ON COLUMN public.trades_futures.initial_risk_pct IS
  'Pre-anchor risk percentage used in initial candidate ordering.';
COMMENT ON COLUMN public.trades_futures.final_rr IS
  'R:R recomputed from final pre-submit entry, SL, and TP1.';
COMMENT ON COLUMN public.trades_futures.delta_rr IS
  'final_rr minus planned_rr; measures zone re-anchor impact.';
COMMENT ON COLUMN public.trades_futures.atr_at_entry IS
  'Absolute ATR from the candidate analysis snapshot.';
COMMENT ON COLUMN public.trades_futures.analysis_candle_open_time IS
  'UTC open timestamp of the latest candle used by analysis.';
COMMENT ON COLUMN public.trades_futures.analysis_candle_close_time IS
  'UTC close timestamp of the latest candle used by analysis.';
COMMENT ON COLUMN public.trades_futures.analysis_candle_closed IS
  'Whether the latest analyzed candle was fully closed at analysis time.';
```

There is deliberately no backfill. Old `entry_price/sl/tp1` can reconstruct a numerical final R, but copying that into `final_rr` would falsely imply the full prospective snapshot/candle provenance existed.

## 10. Optional-Later Fields

Do not migrate these until runtime computes them using only fully closed pre-decision candles and a fixed documented definition:

```sql
-- OPTIONAL LATER — proposal only; do not add until safely computed.
ALTER TABLE public.trades_futures
  ADD COLUMN IF NOT EXISTS btc_return_24h NUMERIC,
  ADD COLUMN IF NOT EXISTS btc_trend TEXT,
  ADD COLUMN IF NOT EXISTS symbol_return_24h NUMERIC,
  ADD COLUMN IF NOT EXISTS symbol_trend TEXT,
  ADD COLUMN IF NOT EXISTS directional_alignment TEXT,
  ADD COLUMN IF NOT EXISTS candidate_rank INTEGER,
  ADD COLUMN IF NOT EXISTS candidate_pool_size INTEGER;
```

`volatility_regime_at_entry` already exists and must not be duplicated. BTC/symbol threshold or lookback metadata may eventually belong in a versioned feature schema rather than more columns if definitions multiply.

## 11. Repository Mapping Plan

No mapping was implemented in this audit.

| SQL column | Source variable / required adjustment | File / function | Insert/update timing |
|---|---|---|---|
| `research_snapshot_version` | `research_snapshot.schema_version` | `FuturesTradeRepository.log_futures_trade` | Initial insert only |
| `decision_time` | Add timestamp after candidate passes all final validation, before return/selection | `FuturesCandidateScanner.pick_best_candidate` | Initial insert only |
| `pre_submit_time` | `research_snapshot.pre_submit_time` | `FuturesOrderExecutor.execute` → repository | Initial insert only |
| `initial_entry_price` | `research_snapshot.initial_entry` | Repository | Initial insert only |
| `initial_sl` | `research_snapshot.initial_sl` | Repository | Initial insert only |
| `initial_risk_pct` | `research_snapshot.initial_risk_pct` | Repository | Initial insert only |
| `final_rr` | `research_snapshot.final_pre_submit_r` | Repository | Initial insert only |
| `delta_rr` | `research_snapshot.delta_r` | Repository | Initial insert only |
| `atr_at_entry` | `research_snapshot.atr` / `cand["atr"]` | Repository | Initial insert only |
| `analysis_candle_open_time` | Convert snapshot epoch ms to UTC ISO/timestamptz | Repository or scanner normalization | Initial insert only |
| `analysis_candle_close_time` | Convert snapshot epoch ms to UTC ISO/timestamptz | Repository or scanner normalization | Initial insert only |
| `analysis_candle_closed` | `research_snapshot.last_candle_was_closed` | Repository | Initial insert only |

The position monitor should not update any of these immutable columns. It should continue updating only lifecycle fields and `exit_reason`. Tests should assert initial insert mapping and prove subsequent monitor updates omit all snapshot columns.

The legacy `scripts/migration_to_supabase.py` should either remain clearly historical or later be extended to preserve these fields when present. It must not invent/backfill missing values.

## 12. Risks / Duplicates / Legacy Fields

1. `entry_zone_touches` and `zone_touches` currently receive the same selected-zone count. Their distinct intended semantics are unclear; do not add another touches column or rename before a compatibility audit.
2. `planned_rr` is initial/pre-anchor R despite its generic name. Documentation and new `final_rr` must preserve that distinction.
3. `entry_price` and `sl` already represent final pre-submit values. Columns named `final_entry_price`/`final_sl` would be duplicates and risk desynchronization.
4. `open_time`, `created_at`, analysis time, decision time, and pre-submit time are different events. Reusing one as another would corrupt latency/research interpretation.
5. The Phase 1B JSON snapshot sits inside a parent JSON object that later mutates. Tests protect nested content by convention, but structured immutable columns are safer research authority.
6. OpenAPI does not reveal exact numeric precision, check constraints, RLS, triggers, or all indexes. The proposed generic `NUMERIC` matches existing price/ratio fields.
7. Upsert on `entry_order_id` indicates a usable conflict constraint, but its exact definition was not independently inspected.
8. `updated_at` has default `now()`, but automatic update-trigger behavior was not verified.
9. `scripts/migration_to_supabase.py` lacks modern fields and points to script-relative JSON paths that differ from the current repository fallback location; it is legacy.
10. Prior docs describing `exit_reason` as unused were accurate before the uncommitted Phase 1B diff; they will become historical after that code is committed/deployed.
11. JSON and structured copies can drift if built independently. Repository mapping should derive both from one immutable snapshot dictionary, with equality tests.
12. Direct callers bypassing the normal scanner can currently create an empty research snapshot. Structured mapping should accept nulls rather than fabricate values.

## 13. Recommended Next Action

Completed before committing/pushing the Phase 1B runtime changes:

1. The 12 nullable columns were approved and applied outside application runtime.
2. Repository initial inserts now populate the structured columns from one snapshot source.
3. JSON remains debug/backward compatibility; structured columns are authoritative.
4. Tests cover every required mapping and ensure lifecycle updates omit immutable fields.
5. Read-only OpenAPI metadata confirms all 12 columns exist.

Still recommended: record the first deployed order ID carrying both `research_snapshot_version` and clean `exit_reason`, and do not add optional directional fields until safely computed.

### Current Phase 1B diff decision

- Exit provenance is aligned with structured `exit_reason`.
- Research snapshot now populates explicit structured columns on initial insert.
- Nested JSON is retained only as a compatibility/debug copy.
- No manual deployment was performed by this work.
