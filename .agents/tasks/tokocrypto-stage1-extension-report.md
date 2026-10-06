# Tokocrypto Stage 1 Extension — Final Report

**Date:** 2026-06-05  
**Stage:** 1 (Read-Only / Production Preparation)  
**Baseline tests:** 411 total (51 tokocrypto)  
**Final tests:** 426 total (66 tokocrypto)

---

## SAFETY DECLARATIONS

- ✅ **No order was placed.** No `POST /open/v1/orders` call exists anywhere.
- ✅ **Binance adapter was not modified.** Changes are isolated to `core/clients/tokocrypto_client.py`.
- ✅ **STP=0 was not hardcoded.** `default_stp_mode` is `None` when the field is absent from the API response.
- ✅ **executionRules ≠ Binance PERCENT_PRICE_BY_SIDE.** Documented explicitly in code, docstrings, and comment blocks.
- ✅ **No blind retry logic added.** `TokocryptoUnknownOrderStatus` carries no retry logic.

---

## IMPLEMENTED

### Dataclasses / Models

**`ExecutionRulesRule`** — single rule entry from `GET /api/v3/executionRules`:
- `rule_type: str` — e.g. `"PRICE_RANGE"`
- `bid_limit_mult_up: float | None`
- `bid_limit_mult_down: float | None`
- `ask_limit_mult_up: float | None`
- `ask_limit_mult_down: float | None`
- `raw: dict` — full rule entry for forward compatibility

**`ExecutionRules`** — parsed execution rules for one symbol:
- `symbol: str`
- `rules: list[ExecutionRulesRule]` — empty list if no rules configured (not an error)
- `raw: dict`

**`ExchangeSymbol.default_stp_mode: str | None`**  
New field added 2026-06-05. Populated from top-level `defaultSelfTradePreventionMode`. `None` when field absent — not `0`.

**`ExchangeSymbol.allowed_stp_modes: list[str]`**  
New field added 2026-06-05. Populated from `allowedSelfTradePreventionModes`. Empty list when absent.

**`TokocryptoUnknownOrderStatus(TokocryptoError)`**  
Typed error for HTTP 5XX on any request. Fields: `status_code: int`, `body: dict`. No retry logic. Semantics: order state is UNKNOWN — must reconcile via order query before classifying outcome.

### Methods Added

**`get_execution_rules(symbol: str) -> ExecutionRules`**  
Maps to `GET /api/v3/executionRules` on `https://www.tokocrypto.site`.  
- Strips underscore from symbol (`BTC_USDT` → `BTCUSDT`)  
- Returns empty `ExecutionRules` if symbol not in response (not an error)  
- Raises `ValueError` on empty/None symbol  
- Raises `TokocryptoMalformedResponseError` if `symbolRules` key absent  

**`_parse_execution_rules(raw: dict) -> ExecutionRules`** (static)  
Parses one `symbolRules` entry. Absent multiplier fields → `None` (never defaults to `1.0` or any other value). Does not assume semantics equal to Binance `PERCENT_PRICE_BY_SIDE`.

**`get_reference_price(symbol: str) -> float | None`**  
Maps to `GET /api/v3/referencePrice` on `https://www.tokocrypto.site`.  
Distinct from `get_ticker()` (last traded price). Returns `None` when exchange returns `null` referencePrice, which means the Price Range execution rule is not enforced.

### Methods Updated

**`_handle_response`** — added HTTP 5XX branch (positioned after 429/418 and 401/403 checks, before JSON parse). Any `status_code >= 500` raises `TokocryptoUnknownOrderStatus`. Does not attempt retry.

**`_parse_symbol`** — now reads `defaultSelfTradePreventionMode` and `allowedSelfTradePreventionModes` from top-level symbol dict. Both fields degrade safely when absent (backward compatible).

### Architecture / Design Docs Added (as code comments)

- **User Data Stream TODO** — migration path from deprecated `POST /open/v1/user-data-stream` to `POST /open/v1/user-listen-token`; deferred to Stage 2.
- **Order Preflight Design** — interface sketch showing the full preflight pipeline (symbol filters → executionRules → referencePrice → order submission), ready to be implemented in Stage 2.
- **OCO Audit** — documented current knowledge gaps; `ocoEnable` flag available but endpoint contract not confirmed.

---

## NOT IMPLEMENTED (with reason)

| Item | Reason |
|------|--------|
| `POST /open/v1/user-listen-token` (User Data Stream WebSocket) | Stage 1 read-only; no WebSocket layer exists; TODO comment added with full migration notes |
| OCO order placement | Stage 1 read-only; OCO endpoint path, required parameters, and price relationship rules not fully documented in available API contract |
| Order placement of any kind (`POST /open/v1/orders`) | Stage 1 constraint — no real order placement |
| Order reconciliation flow after HTTP 5XX | Stage 2; `TokocryptoUnknownOrderStatus` is the signal; query logic is not implemented |
| PRICE_RANGE preflight validation | Requires order placement to be meaningful |
| STP mode selection logic | Metadata is exposed; selection + validation deferred to Stage 2 |

---

## BLOCKED (needs API verification before Stage 2)

| Item | Blocker |
|------|---------|
| OCO endpoint parameters | Endpoint path (possibly `POST /open/v1/oco-orders`) and required parameter set not confirmed in available docs |
| OCO price relationship constraints | Whether PRICE_RANGE execution rules apply to OCO legs is unconfirmed |
| OCO STP mode applicability | Unconfirmed whether STP applies per-leg or per-OCO order |
| `executionRules` full field semantics | Only `PRICE_RANGE` ruleType observed; other rule types unknown; semantics at execution time vs order time need live verification |
| `GET /api/v3/referencePrice` availability | Endpoint confirmed from docs; live behavior (null vs non-null scenarios) not yet verified against live exchange |
| Testnet order flow | All order-related code must be validated on Tokocrypto testnet before production |

---

## TESTS

### Tests Added

**`TestSTPSymbolParsing`** (3 tests):
- `test_symbol_parser_with_stp_fields` — Test 52
- `test_symbol_parser_without_stp_fields` — Test 53
- `test_symbol_parser_stp_default_not_zero` — Test 54

**`TestGetExecutionRules`** (5 tests):
- `test_get_execution_rules_success` — Test 55
- `test_get_execution_rules_symbol_not_in_response` — Test 56
- `test_get_execution_rules_malformed_response` — Test 57
- `test_get_execution_rules_symbol_required` — Test 58
- `test_get_execution_rules_multiplier_partially_absent` — Test 59

**`TestGetReferencePrice`** (2 tests):
- `test_get_reference_price_success` — Test 60
- `test_get_reference_price_null` — Test 61

**`TestHTTP5XXSemantics`** (5 tests):
- `test_http_5xx_raises_unknown_order_status` — Test 62
- `test_http_502_raises_unknown_order_status` — Test 63
- `test_unknown_order_status_not_raised_for_4xx` — Test 64
- `test_unknown_order_status_not_raised_for_429` — Test 65
- `test_no_blind_retry_attribute` — Test 66

**Total new tests:** 15

### Test Results (final run)

```
Ran 426 tests in 11.264s
OK
```

- **Total test count:** 426
- **Result:** PASS
- **Regressions:** NONE — all 411 pre-existing tests continue to pass

---

## REMAINING BLOCKERS BEFORE PRODUCTION TRADING

1. **Order placement not implemented (Stage 2).** `POST /open/v1/orders` with all required parameters must be implemented, signed, and tested on Tokocrypto testnet before any real-money trading.

2. **HTTP 5XX reconciliation not implemented (Stage 2).** `TokocryptoUnknownOrderStatus` is the signal. Stage 2 must add: query order by `orderId`/`clientOrderId` → classify as `FILLED / NEW / CANCELED / NOT_FOUND`. No blind retry.

3. **STP mode selection logic (Stage 2).** Order placement must read `allowed_stp_modes` from symbol metadata, validate the user-configured preference, and pass a valid mode to the order request. Do not default to mode `0`.

4. **Order preflight validation (Stage 2).** All symbol filters (PRICE_FILTER, LOT_SIZE, NOTIONAL) plus PRICE_RANGE execution rule check (using `get_execution_rules` + `get_reference_price`) must be validated before order submission.

5. **OCO not verified.** The `ocoEnable` flag exists on symbol metadata but the OCO endpoint path, required parameters, and price relationship rules are not confirmed. OCO is Stage 3 at earliest.

6. **User data stream not implemented (Stage 2).** `POST /open/v1/user-listen-token` needed for real-time order fill and balance update events. The old `POST /open/v1/user-data-stream` was decommissioned 2026-04-30; do not implement it.

7. **executionRules must be integrated into order preflight (Stage 2).** `get_execution_rules()` and `get_reference_price()` are available as read-only methods; they must be called and validated in the preflight path before submitting any order.

8. **Testnet validation required.** All order-related code must be fully validated against Tokocrypto's testnet environment before enabling production trading with real funds.
