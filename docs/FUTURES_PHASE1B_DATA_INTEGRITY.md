# Futures Phase 1B Data-Integrity Audit

Audit date: 2026-08-20 (Asia/Jakarta)
Scope: current JustTrades Futures code, tests, repository-accessible logs, and a read-only Supabase snapshot.
R hypothesis classification: **2 — UNCERTAIN: SAFE BUT HEAVILY CONFOUNDED**.

This task made prospective logging/provenance changes only. It did not change trade selection, ranking, sizing, leverage, entry/SL/TP prices, order types, protection behavior, deployment, or historical database rows.

## 1. Executive Summary

Futures entry and SL re-anchoring occur synchronously inside candidate selection, before sizing, before order submission, and therefore before fill or monitoring. Re-anchor uses the same chart-analysis result already attached to the candidate plus current exchange precision constraints. It does not fetch a new candle, ticker, order, fill, or position. Consequently:

- `final_R = abs(TP-final_entry)/abs(final_entry-final_SL)` is structurally available before submission and is safe for **prospective** pre-entry ML.
- `delta_R = final_R-initial_planned_R` is also prospectively safe.
- Neither is post-entry leakage.
- Historical reconstruction is less trustworthy than prospective capture because old rows retain final entry/SL/TP but not the initial contract, decision timestamp, candle cutoff, or proof that the latest analysis candle was closed.

The R finding remains interesting but heavily confounded. The 2–3R bucket contains 18 trades (10 TP, 8 SL; 55.6%), while 3+ contains 51 (7 TP, 44 SL; 13.7%). The 3+ bucket also has a larger target-distance median (6.96% vs 4.96%) and larger median re-anchor delta (+1.045R vs +0.418R). The result may be target reachability/re-anchor composition, not an independent R effect.

Historical SL provenance cannot be recovered safely. All 53 SL rows have null `exit_reason`; 52 lack stored protection state, repository logs contain no exit events, and algo IDs/price proximity cannot prove which writer closed the position. Counts are therefore: proven exchange SL 0, proven unprotected emergency 0, proven price guard 0, ambiguous 53.

Prospective fixes implemented:

1. Existing `exit_reason` now records `EXCHANGE_TP`, `EXCHANGE_SL`, `EMERGENCY_UNPROTECTED`, `EMERGENCY_PRICE_GUARD`, or `RECONCILED_EXCHANGE_CLOSE` while preserving existing `exit_status` semantics.
2. Twelve structured Supabase columns are the authoritative immutable research interface for the initial contract, final R/delta R, ATR, timestamps, and candle provenance.
3. A deep-copied `raw_entry_order.research_snapshot` remains only as debug/backward-compatible redundancy and is not modified by lifecycle updates.

## 2. Re-anchor Code Path

| Stage | File / symbol | Current behavior |
|---|---|---|
| Candle snapshot | `services/chart_analyzer.py::analyze_symbol` | Fetches 540 × 4h OHLCV; uses latest returned close; calculates ATR, S/R, Fib and both side setups. |
| Initial SL | `suggest_sl_tp` | LONG: nearest support low − 0.5 ATR. SHORT: nearest resistance high + 0.5 ATR. |
| TP | `suggest_sl_tp` | Selects T1 zone or T2 Fib target within 4 ATR that clears minimum 1.5 R. |
| Initial R | `suggest_sl_tp::_run_selection` | `abs(TP-current_price)/abs(current_price-initial_SL)`. Stored in candidate as `rr`. |
| Initial entry | `FuturesCandidateScanner.gather_candidates` | Candidate `entry_price=current_price`. |
| Pool ranking | `gather_candidates` | Combined LONG/SHORT sort by `(initial risk_pct ASC, initial rr DESC)`. |
| Re-anchor | `FuturesCandidateScanner.pick_best_candidate` | After fetching symbol tick/step constraints, selects support/resistance entry zone and rewrites `entry_price`, `sl`, and `risk_pct`. |
| LONG final contract | same | `entry=tick_round(zone_center*1.0015)`; `SL=tick_round(zone_low−0.5ATR)`. |
| SHORT final contract | same | `entry=tick_round(zone_center*0.9985)`; `SL=tick_round(zone_high+0.5ATR)`. |
| R after re-anchor, previously | same | `rr` was not recomputed or used for a new gate/rank. |
| R after re-anchor, now logged | same | Computes research-only `final_pre_submit_r` and `delta_r`; trading code does not read them. |
| Validation/sizing | same | Direction geometry, size, exchange minimum, and liquidation checks use final entry/SL. |
| Pre-submit stamp | `FuturesOrderExecutor.execute` | Adds research-only `pre_submit_time` immediately before existing leverage/margin setup and entry submission. |
| Entry submission | `FuturesOrderExecutor.execute` | Existing LIMIT/GTC order remains unchanged. |
| Persistence | `FuturesTradeRepository.log_futures_trade` | Final contract and Phase 1B snapshot are written to first-class columns; a debug copy remains in JSON. |
| Fill/monitor | `FuturesPositionMonitor.check_positions` | Records fill and manages exits; does not re-anchor entry, SL, TP, or R. |

There is one re-anchor mechanism only: zone anchoring in `pick_best_candidate`. No entry/SL re-anchor was found in fill handling or position monitoring.

## 3. Re-anchor Timeline

```text
mainnet public chart fetch
  -> analysis uses latest returned 4h candle
  -> initial entry = current close
  -> initial SL from nearest opposite zone edge ± 0.5 ATR
  -> TP from zone/Fib
  -> initial planned R calculated
  -> candidates ranked using initial risk and initial R
  -> exchange tick/step constraints fetched
  -> zone re-anchor (same cached candidate zones/ATR/current price)
  -> final entry, final SL, final risk %
  -> final R becomes fully knowable
  -> geometry, sizing, min-order and liquidation validation
  -> volatility-regime and funding enrichment
  -> pre-submit timestamp
  -> leverage/margin request
  -> LIMIT entry submission
  -> DB insert
  -> later fill
  -> later position monitoring and exits
```

Explicit answers:

| Question | Answer |
|---|---|
| A. Before order submission? | **Yes.** |
| B. Before entry fill? | **Yes.** LIMIT entry does not yet exist when re-anchor occurs. |
| C. After entry fill? | **No.** |
| D. During monitoring? | **No.** |
| E. Depends on later market movement? | **No.** It uses the original candidate's current price, ATR and zones. |
| F. Newly fetched data after original analysis? | Exchange symbol constraints are newly fetched for rounding/validation; no new candle, ticker, order, fill or position data is used. Volatility/funding enrichment is fetched after re-anchor but does not change final entry/SL/TP/R. |

The separate concern is that the original analysis itself may include an unfinished latest 4h candle. The patch records its open/close timestamps and closed status without changing strategy behavior.

## 4. R Feature Safety Matrix

| Value | Formula | Computed/available when | Data timestamp | Decision availability | Classification |
|---|---|---|---|---|---|
| Stored `planned_rr` | `abs(TP-current)/abs(current-initial_SL)` | Chart setup, before ranking | Latest analysis response | Yes | `SAFE_PRE_ENTRY`, but represents pre-anchor contract |
| Prospective `final_pre_submit_r` | `abs(TP-final_entry)/abs(final_entry-final_SL)` | Immediately after re-anchor | Same analysis zones/ATR plus current exchange tick | Yes, before validation/submission | `SAFE_PRE_SUBMISSION` |
| Historical reconstructed final R | Same, from stored final fields | Reconstructed today | Original final contract values, but missing immutable decision snapshot | Structurally yes; audit proof incomplete | `UNKNOWN` for strict historical feature provenance; acceptable exploratory reconstruction |
| `delta_r` | `final_R-planned_rr` | After re-anchor | Same pre-submit inputs | Yes | `SAFE_PRE_SUBMISSION` prospectively |
| TP distance % | `abs(TP-final_entry)/final_entry*100` | After re-anchor | Same pre-submit inputs | Yes | `SAFE_PRE_SUBMISSION` |
| SL distance % | `abs(final_entry-final_SL)/final_entry*100` | After re-anchor | Same pre-submit inputs | Yes | `SAFE_PRE_SUBMISSION`; equals final risk geometry |
| ATR-normalized TP distance | `abs(TP-final_entry)/ATR` | Derivable after re-anchor | ATR from analysis | Yes | `SAFE_PRE_SUBMISSION`, conditional on candle provenance |
| ATR-normalized SL distance | `abs(final_entry-final_SL)/ATR` | Derivable after re-anchor | ATR from analysis | Yes | `SAFE_PRE_SUBMISSION`, conditional on candle provenance |
| Fill-based R | Any formula substituting `entry_fill_price` | After fill | Exchange fill | No at decision | `POST_FILL_ONLY`; entry-model leakage |
| Realized R | `realized_pnl_usd/max_loss_usd` | Exit | Full trade path | No | `POST_ENTRY_LEAKAGE` for entry model; valid outcome metric |

Final R is not made unsafe merely because ranking used initial R. It is a valid pre-submit descriptor of the submitted contract. It must not be confused with fill-adjusted or realized R.

## 5. R Bucket Composition

The exact Phase 1 bucket definition is `<2`, `2–3`, and `3+`, with boundaries `[2,3)`.

| Final R | N | TP | SL | WR | LONG / SHORT | Tier | Volatility | Median risk % | Median TP dist % | Median SL dist % | Median delta R |
|---|---:|---:|---:|---:|---|---|---|---:|---:|---:|---:|
| <2 | 3 | 2 | 1 | 66.7% | 2 / 1 | T1 3 | high 3 | 2.615 | 5.170 | 2.615 | −0.084 |
| 2–3 | 18 | 10 | 8 | 55.6% | 11 / 7 | T1 16, T2 2 | low 10, medium 7, high 1 | 1.863 | 4.962 | 1.863 | +0.418 |
| 3+ | 51 | 7 | 44 | 13.7% | 27 / 24 | T1 51 | low 30, medium 15, high 6 | 1.852 | 6.960 | 1.852 | +1.045 |

The <2 cell is unusably small. Risk and side composition are similar between 2–3 and 3+, but 3+ has:

- larger target distance;
- larger re-anchor delta;
- no T2 examples;
- somewhat different volatility composition;
- most of the historical sample and likely substantial time/cluster effects.

The association is not obviously explained by risk percentage, but is plausibly explained by reachability, re-anchor mechanics, regime/time clustering, or ambiguous negative labels. No causal inference is justified.

## 6. SL_HIT Write Paths

| Path | Trigger and exchange evidence | Persisted fields before patch | Prospective reason | Retrospective distinguishable? |
|---|---|---|---|---|
| Normal exchange SL | Stored SL algo query returns FILLED/EXECUTED/COMPLETED/FINISHED; response supplies status and possible fill/time | `SL_HIT`, price/time/PnL, IDs; raw terminal response not stored | `EXCHANGE_SL` | Old rows: no, because IDs alone do not prove this writer won |
| Reconciler emergency | Live SL confirmed missing; current price already breaches contract; reduce-only MARKET response exists in executor | `SL_HIT`, ticker-derived price/PnL; no response/provenance persisted | `EMERGENCY_UNPROTECTED` | Old rows: no |
| Monitor price guard | Entry FILLED, DB says exits placed, algo status not terminal, ticker crosses SL; reduce-only MARKET response parsed | `SL_HIT`, market fill/fallback price/time/PnL | `EMERGENCY_PRICE_GUARD` | Old rows: no |
| Reconciliation inference | Live position is zero with no terminal TP/SL observed | `MANUALLY_CLOSED`, no price/PnL | `RECONCILED_EXCHANGE_CLOSE` | Status distinguishable, exact cause not known |
| Manual intervention | No direct manual-write path found in inspected Futures monitor | Unknown external process/DB action | Not changed | Unknown |
| Fallback fill price | Normal algo path falls back to trigger; price guard falls back to ticker; reconciler uses ticker | Same status despite differing evidence quality | Reason now distinguishes writer, not fill-price quality | Old rows: no |

The patch does not rename `SL_HIT`, preserving lifecycle/dashboard semantics. `exit_reason` supplies the required provenance dimension.

## 7. Historical Exit-Provenance Audit

Read-only live snapshot:

| Historical classification | Count | Confidence |
|---|---:|---|
| `PROVEN_EXCHANGE_SL` | 0 | No persisted terminal response or reason proves exchange SL was the resolving writer |
| `PROVEN_EMERGENCY_CLOSE` | 0 | No persisted emergency flag/market response |
| `PROVEN_PRICE_GUARD_CLOSE` | 0 | Telegram/log source not retained in repository logs |
| `AMBIGUOUS` | 53 | Certain that DB says SL_HIT; cause is not recoverable from stored evidence |

Supporting observations:

- `exit_reason` is null for all 53 SL rows.
- 52/53 have no nested `exit_protection.state`; one says FULLY_PROTECTED, which still does not rule out price guard.
- 48 have an SL ID plus entry/exit timestamps, four have an SL ID but no exit timestamp, and one lacks an SL ID but has timestamps. None of these combinations proves which writer executed.
- Repository-accessible logs contained dashboard timing only and no historical Futures exit event trail.

## 8. Historical Data Salvageability

| Evidence source | What it establishes | Why it cannot safely reclassify old SL rows |
|---|---|---|
| `raw_entry_order` | Entry response; occasionally latest protection metadata | It is mutable and does not store exit market/algo response |
| Protection state | Latest known protection condition for one row | FULLY_PROTECTED does not exclude price guard; absence says nothing |
| SL/algo IDs | A protection ID existed or was remembered | Does not prove it executed rather than being canceled after market close |
| Exit timestamp/price | Resolution was recorded around a price | Trigger proximity is circumstantial and writer fallbacks can use planned SL/ticker |
| Binance history | Could theoretically establish historical orders | Not persisted in repo; availability/retention and exact writer matching were not proven |
| Runtime logs | Could contain “price-guard” notification | Relevant logs are not retained in the repository/runtime-accessible log set |
| Reconciliation errors | Could prove a missing leg at a prior cycle | Mostly absent; latest nested state is not an append-only event history |

No historical row was reclassified, and no database mutation was performed. Old 53 negative labels remain usable only under an explicitly permissive label definition.

## 9. Prospective Provenance Fix

The existing Supabase `exit_reason` field is used; no migration is needed.

| Existing `exit_status` | New `exit_reason` | Evidence source |
|---|---|---|
| `TP_HIT` | `EXCHANGE_TP` | TP algo endpoint reported terminal execution |
| `SL_HIT` | `EXCHANGE_SL` | SL algo endpoint reported terminal execution |
| `SL_HIT` | `EMERGENCY_UNPROTECTED` | Missing SL plus breached contract caused reconciler MARKET close |
| `SL_HIT` | `EMERGENCY_PRICE_GUARD` | Monitor ticker breach caused MARKET close |
| `MANUALLY_CLOSED` | `RECONCILED_EXCHANGE_CLOSE` | Exchange position absent without a recognized terminal leg |
| `OPEN` | null | No exit yet |

The reason is included in eager terminal persistence and the end-of-cycle update list. Existing records with null reason remain backward compatible.

Clean-label boundary:

- Any still-OPEN trade resolved by the patched monitor will receive clean **writer provenance**, even if it predates the patch.
- The first new trade inserted after the patched code begins running will additionally receive the full versioned pre-submit research snapshot.
- Already-terminal historical trades remain ambiguous unless independent immutable exchange evidence is later obtained.

No deployment was performed in this task, so the actual first clean production order ID is not yet known.

## 10. Immutable Pre-entry Snapshot Audit

### Before the patch

| Data | Status |
|---|---|
| Final entry, SL, TP, risk %, planned R, ATR %, tier/touches, regime | First-class fields normally immutable after insert |
| Initial entry and initial SL | Lost when candidate dict was re-anchored |
| Final recomputed R/delta R | Not stored |
| Absolute ATR | Not stored |
| Analysis/decision timestamp | `open_time` only, created after exchange submission response |
| Candle cutoff/closed status | Not stored |
| Raw candidate snapshot | Not stored |
| `raw_entry_order` | Mutable; monitor appends/replaces `exit_protection` metadata |

### Prospective snapshot

Structured columns use snapshot version `futures_pre_submit_v1` and store:

- analysis timestamp;
- last analyzed candle open/close timestamp and whether it was closed at analysis;
- pre-submit timestamp;
- symbol and side;
- initial entry, initial SL, TP1/TP2, initial planned R and risk %;
- final pre-submit entry, SL, R, delta R and risk %;
- absolute ATR and ATR %;
- zone tier, selected entry-zone center and touches.

The repository maps these values at initial insert only and deep-copies a redundant JSON snapshot. Monitor lifecycle payloads omit all 12 immutable columns. Regression tests verify both the structured mapping and that later protection/exit updates cannot overwrite it.

Candidate rank/pool size, BTC/own trend, and a strategy-safe closed-candle-only feature set are intentionally not added here. They require a separate design and might affect or be confused with strategy behavior.

## 11. Implemented Data-Integrity Changes

| File | Logging-only change |
|---|---|
| `services/chart_analyzer.py` | Emits analysis time and last-candle provenance; does not filter/change candles. |
| `core/scanners/futures_candidate_scanner.py` | Captures initial and final candidate contracts and calculates research-only final R/delta R. |
| `core/executors/futures_order_executor.py` | Adds pre-submit timestamp and emergency-unprotected provenance result. |
| `core/repositories/futures_trade_repository.py` | Initializes `exit_reason`, writes all structured snapshot columns, and retains a deep-copied debug JSON. |
| `core/executors/futures_position_monitor.py` | Writes reason per terminal path and persists it eagerly/end-of-cycle. |
| `tests/test_futures_data_provenance.py` | Covers exchange TP/SL, both emergency paths, reconciled manual close, and snapshot immutability. |

Not implemented:

- historical backfill;
- application-executed schema migration (the migration was applied separately before this mapping);
- append-only exit event log;
- exchange response storage for terminal exits;
- strategy change to exclude unfinished candles;
- recomputing R for ranking or entry validation;
- any ML feature/model/scoring behavior.

## 12. Expectancy Sanity Check

Realized R is defined descriptively as:

```text
realized_R = realized_pnl_usd / max_loss_usd
```

This uses stored gross computed PnL and planned max loss. It differs from planned R and final contract R; it is post-entry outcome data. All 72 permissive rows had the necessary values.

| Group | N | WR | Avg winner R | Median winner R | Avg loser R | Median loser R | Expectancy R/trade | Avg realized PnL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 72 | 26.39% | +2.814 | +2.850 | −1.058 | −1.000 | −0.036 | +$0.0094 |
| LONG | 40 | 30.00% | +2.671 | +2.629 | −1.049 | −1.000 | +0.067 | +$0.0392 |
| SHORT | 32 | 21.88% | +3.060 | +2.942 | −1.067 | −1.000 | −0.164 | −$0.0279 |

The low win rate is structurally compatible with winners near +2.8R and losers near −1R: breakeven before costs would be roughly 26%. The observed permissive expectancy is near zero overall. However:

- old SL provenance is ambiguous;
- PnL calculation quality differs among exit writers;
- fee estimate and funding are not subtracted in this realized-R calculation;
- correlated batches reduce independence;
- this is descriptive, not evidence of statistical profitability.

## 13. Phase 1B Verdict

| Question | Verdict |
|---|---|
| A. Is final R safe for prospective ML? | **Yes**, as `SAFE_PRE_SUBMISSION`, using the immutable prospective snapshot and excluding records whose analyzed candle was not closed if the research design requires closed candles. |
| B. Is delta R safe? | **Yes**, under the same conditions. Both components exist before submission. |
| C. Is 2–3 vs 3+ worth monitoring? | **Yes**, prospectively and without threshold tuning. |
| D. Is it invalid due to post-entry leakage? | **No.** It is not post-entry leakage; it is heavily confounded and historically under-audited. |
| E. How many historical SL labels are clean? | **0 proven clean; 53 ambiguous.** |
| F. From which trade are labels clean? | Exit provenance is clean for any OPEN trade resolved by the patched runtime. Full label + immutable feature snapshot starts with the first new trade inserted after deployment; no order ID yet because no deployment occurred. |
| G. What carries to Phase 2? | Final R/delta R, target/stop distance, side, zones, volatility and closed-candle provenance, grouped by cluster/time and filtered by explicit exit reason. |

**R hypothesis: 2 — UNCERTAIN: SAFE BUT HEAVILY CONFOUNDED.**

It is ALIVE as a monitoring hypothesis but does not meet the stronger “ALIVE—monitor as an apparently direct relationship” classification because target distance and re-anchor delta differ materially by bucket.

## 14. What to Carry to Effective N≈60

1. Freeze the current R buckets `<2`, `2–3`, `3+`; do not optimize boundaries against outcomes.
2. Use only prospectively reasoned labels: `EXCHANGE_TP`/`EXCHANGE_SL` for the cleanest strategy-outcome set. Analyze emergency reasons separately.
3. Require structured `research_snapshot_version='futures_pre_submit_v1'`.
4. For candle-derived research, require structured `analysis_candle_closed=true`; do not silently mix unsafe snapshots.
5. Compare final R jointly with TP distance %, SL distance %, ATR-normalized distances, side, zone touches, volatility, and batch cluster.
6. Report raw N and cluster-adjusted Effective N.
7. Use time/group-aware descriptive splits before any Phase 2 model.
8. Keep realized R strictly as a label/outcome metric, never a pre-entry feature.
9. Track snapshot coverage and exit-reason coverage from the first deployed order ID.
10. Re-run directional Phase 1 unchanged when side-specific Effective N approaches 60 or when key cells reach usable counts.

## 15. Remaining Risks / Open Questions

1. The strategy still analyzes the latest returned 4h candle even if unfinished; this task logs the condition but does not change behavior.
2. `exit_reason` identifies the software writer/evidence path, not necessarily every exchange race. A price guard and native SL could execute close together.
3. Terminal exchange responses are not persisted, limiting forensic proof beyond the reason assigned at runtime.
4. Emergency-unprotected persistence still derives price/PnL from ticker fallback rather than storing the market-close response.
5. Existing OPEN trades gain clean exit reason but lack the new initial/final research snapshot.
6. `raw_entry_order` remains mutable debug metadata and must not be used as the authoritative research interface.
7. Direct callers that bypass the scanner may persist an empty research snapshot; normal CLI path populates it.
8. No production deployment means clean-data collection has not started yet.
9. The exact Supabase constraints/JSON size limits remain undocumented in repository DDL.
10. A future append-only lifecycle event table could strengthen provenance but would require schema design and was intentionally excluded.
