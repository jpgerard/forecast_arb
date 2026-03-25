# Active Context — forecast_arb

## Current Version: CCC v2.2 "Broker-State Drift Check" — COMPLETE (2026-03-23)

---

## What Was Done (CCC v1.9 — Max Value / Low Complexity)

### Task A: Worst-case Debit Gating

**New file: `forecast_arb/allocator/pricing.py`**
- `compute_debit_mid(long_mid, short_mid)` → per-share mid debit
- `compute_debit_worstcase(long_ask, short_bid)` → per-share WC debit
- `compute_premium_per_contract(debit_share)` → $/contract
- `compute_pricing_detail(candidate)` → full pricing dict with all quotes

**Updated: `forecast_arb/allocator/open_plan.py`**
- `_resolve_premium_for_gating()`: selects WC → MID → CAMPAIGN premium in order
- EV/$ **recomputed from p_used × max_gain** ONLY when premium_src != CAMPAIGN
  (backward compat: stored ev_per_dollar used when no quotes present)
- Reason codes: `PREMIUM_USED:WC`, `PREMIUM_USED:MID`, or `PREMIUM_USED:CAMPAIGN`
- Both `PREMIUM_WC_PER_CONTRACT:xxx` and `PREMIUM_MID_PER_CONTRACT:xxx` emitted when both available

**Updated: `forecast_arb/allocator/types.py`**
- `AllocatorAction.pricing` (optional Dict): `{premium_used, premium_used_source, premium_wc, premium_mid, debit_wc_share, debit_mid_share}`
- `AllocatorAction.layer` (optional str): `"A"` | `"B"` | `None`
- `AllocatorAction.fragile` (optional bool): `True` | `False` | `None`
- All three serialized to `allocator_actions.json` via `to_dict()`

---

### Task B: Crash Ladder (A/B Layers)

**Updated: `configs/allocator_ccc_v1.yaml`**
```yaml
thresholds:
  crash:
    ladder:
      layer_a: {moneyness_min_pct: 5.0, moneyness_max_pct: 9.0}
      layer_b: {moneyness_min_pct: 10.0, moneyness_max_pct: 16.0}
```

**Updated: `forecast_arb/allocator/open_plan.py`**
- `_classify_ladder_layer()`: computes OTM% = (spot - long_strike) / spot * 100
- `_layer_sort_key()`: Layer A priority=0, B=1, None=2 when inv=0 and regime=crash
- `action.layer` populated on OPEN actions

**Updated: `forecast_arb/allocator/policy.py`**
- `get_ladder_params(policy, regime)` → returns None if section absent (backward compat)

---

### Task C: Roll-Forward Discipline

**Updated: `configs/allocator_ccc_v1.yaml`**
```yaml
roll:
  enabled: true
  dte_max_for_roll: 21
  min_multiple_to_hold: 1.10
  min_convexity_multiple_to_hold: 8.0
```

**Updated: `forecast_arb/allocator/harvest.py`**
- NEW `generate_roll_discipline_actions(positions, policy, skip_trade_ids)`:
  - Triggers when DTE ≤ dte_max_for_roll AND (multiple < 1.10 OR convexity < 8.0)
  - Reason codes: `ROLL_DTE`, `ROLL_MULTIPLE`, `ROLL_CONVEXITY`, `ROLL_MISSING_MULTIPLE`
  - Close-liquidity guard applies → HOLD with `WIDE_MARKET_NO_CLOSE` if spread too wide

**Updated: `forecast_arb/allocator/plan.py`**
- Step 6b: calls `generate_roll_discipline_actions()` with `skip_trade_ids` from harvest
- Step 7b: ROLL_CLOSE slot-freeing logic
  - Counts roll closes by regime
  - Subtracts them from pending inflation → `adj_crash_reserved = max(0, pending - roll_closes)`
  - Replacement OPEN allowed even when pending intents exist

**Updated: `forecast_arb/allocator/policy.py`**
- `get_roll_params(policy)` → defaults `enabled=False` when section absent

---

### Task D: Effective Inventory (already implemented in v1.8, named explicitly)

The existing v1.8 implementation already correctly implements:
- `actual_filled`         = positions.json count
- `committed_not_filled`  = commit_ledger − fills_ledger (called "pending" in code)
- `inv_effective`         = actual + committed_not_filled

Console shows all three components. No new code needed beyond what was already in v1.8.
`compute_pending_from_ledgers()` in inventory.py is the explicit helper.

---

### Task E: Fragility Gating

**Updated: `configs/allocator_ccc_v1.yaml`**
```yaml
robustness:
  enabled: true
  p_downshift_pp: 3.0
  debit_upshift_pct: 10.0
  require_positive_ev_under_shocks: true
  allow_if_inventory_empty: true
```

**Updated: `forecast_arb/allocator/open_plan.py`**
- `_compute_ev_shock()`: computes EV under p_shock and premium_shock
- Gate: if `ev_shock ≤ 0` and `inv > 0` → HOLD with `EV_FRAGILE_UNDER_SHOCKS`
- Gate: if `ev_shock ≤ 0` and `inv = 0` and `allow_if_inventory_empty` → ALLOW with `FRAGILE_ALLOWED_EMPTY`
- `action.fragile = True/False` (None if p_used/max_gain unavailable)

**Updated: `forecast_arb/allocator/policy.py`**
- `get_robustness_params(policy)` → defaults `enabled=False` when section absent

---

### Task F: Console Output Improvements

**Updated: `forecast_arb/allocator/plan.py` `_print_pm_summary()`**
- OPEN action line now shows: `$65/c[WC]  EV=1.80  conv=28.5x layer=A robust`
  - `[WC]`/`[MID]`/`[CAMPAIGN]` = premium source
  - `layer=A`/`layer=B` = crash ladder layer
  - `robust`/`FRAGILE` = fragility status

**Updated: `scripts/daily.py` `_run_allocator()`**
- Returns `open_details` list with `{candidate_id, regime, premium, premium_src, layer, fragile, qty}`
- Returns `committed_not_filled_by_regime` (explicit alias for pending)

---

### New Tests: `tests/test_patch_v19_max_value_low_complexity.py`

**29 tests, all passing (0.42s)**:

| Test Class | Tests |
|-----------|-------|
| `TestWorstCasePricingGating` | WC premium flips OPEN→HOLD |
| `TestPremiumUsedReasonCodes` | PREMIUM_USED:WC, CAMPAIGN tags; pricing dict on action |
| `TestLadderLayerPreference` | Layer A selected over B when inv=0 |
| `TestRollCloseEnablesReplacementOpen` | ROLL_CLOSE triggers + enables replacement OPEN |
| `TestLiquidityGuardBlocksCloseAndOpen` | Wide market → HOLD → no replacement OPEN |
| `TestEffectiveInventoryGating` | committed-not-filled blocks open; clears after fill |
| `TestFragilityGating` | Blocks when inv>0; allows when inv=0 with flag |
| `TestAllocatorActionsJsonContainsNewFields` | pricing+layer+fragile in to_dict() |
| `TestPricingUtilities` | Unit tests for pricing.py functions |
| `TestPolicyHelpers` | get_robustness_params/roll_params/ladder_params defaults |
| `TestRollDisciplineEdgeCases` | Roll edge cases (DTE, convexity, disabled) |

---

## Key Semantic: EV/$ Recomputation

```
# ONLY when using WC or MID premium (different from campaign assumption):
ev_per_dollar = (p_used * max_gain - (1 - p_used) * premium_used) / premium_used

# When using CAMPAIGN premium (same as campaign's computation):
ev_per_dollar = candidate["ev_per_dollar"]  # authoritative; backward-compat
```

This ensures existing tests that deliberately set `ev_per_dollar=0.5` to fail a gate continue to work correctly.

---

## Key File Index (current)

| File | Version | Purpose |
|------|---------|---------|
| `forecast_arb/allocator/pricing.py` | **v1.9 NEW** | Worst-case debit pricing utilities |
| `forecast_arb/allocator/open_plan.py` | **v1.9 updated** | WC gating, ladder layer, fragility |
| `forecast_arb/allocator/types.py` | **v1.9 updated** | pricing/layer/fragile on AllocatorAction |
| `forecast_arb/allocator/harvest.py` | **v1.9 updated** | generate_roll_discipline_actions() |
| `forecast_arb/allocator/plan.py` | **v1.9 updated** | Roll discipline integration + slot-freeing |
| `forecast_arb/allocator/policy.py` | **v1.9 updated** | get_robustness_params/roll_params/ladder_params |
| `configs/allocator_ccc_v1.yaml` | **v1.9 updated** | ladder + roll + robustness sections |
| `scripts/daily.py` | **v1.9 updated** | open_details in _run_allocator return |
| `DAILY_WORKFLOW_GUIDE.md` | **v1.9 updated** | Tasks A/B/C/E documentation |
| `tests/test_patch_v19_max_value_low_complexity.py` | **v1.9 NEW** | 29 tests, all passing |

---

## Test Results

- Full allocator test suite: **340 passed, 2 skipped (4.5s)**
- New v1.9 tests: **29/29 passed (0.42s)**
- Pre-existing failures (unrelated): `test_phase4b_execution_enforcement.py`, `test_run_real_cycle.py` (pre-existing import errors from before this patch)

---

## Config Additions (all backward-compatible with defaults)

```yaml
# thresholds.crash.ladder — crash layer classification
# roll — roll-forward discipline  
# robustness — fragility (EV shock) gating
```

All new sections default to `enabled: false` when absent from YAML → zero behavioral change for existing deployments that omit these sections.

---

## IBKR Close-Spread Execution Utility (2026-03-10)

**New files:**

| File | Purpose |
|------|---------|
| `forecast_arb/ibkr/close_spread.py` | Core module — position verify, BAG build, quote fetch, liquidity guard, pricing ladder |
| `scripts/close_spy_spread.py` | CLI entrypoint — thin argparse wrapper |

### Entrypoint
```python
from forecast_arb.ibkr.close_spread import close_bear_put_spread
result = close_bear_put_spread(mode="paper")   # or mode="live"
```

### Execution flow
1. Connect to IBKR (paper 7497 / live 7496)
2. `reqPositions()` → verify +1 SPY 20260320 590P and -1 SPY 20260320 570P exist
3. `qualifyContracts()` on both legs → obtain conIds
4. Build BAG contract: `SELL 1 BAG` with legs `SELL 590P` + `BUY 570P`
5. `reqMktData(BAG)` → fetch live combo bid/ask; fall back to synthetic from legs
6. Liquidity guard: if `(ask−bid)/mid > 0.25` → return `WIDE_MARKET_NO_CLOSE`
7. Pricing ladder `[0.16, 0.15, 0.14]`: place `SELL 1 BAG LMT DAY` at each price;
   poll for fill (default 60 s/level); cancel + retry on timeout
8. Return `SpreadCloseResult` with status, fill price, order_id, perm_id, full log

### Status codes
| Code | Meaning |
|------|---------|
| `FILLED` | Filled at some ladder price |
| `STAGED` | transmit=False; order visible in TWS, not sent to exchange |
| `WIDE_MARKET_NO_CLOSE` | Combo width > 25% of mid; no order submitted |
| `POSITION_NOT_FOUND` | Legs not in IBKR account; no order submitted |
| `LADDER_EXHAUSTED` | Tried all prices (0.16→0.15→0.14), no fill |
| `ERROR` | Connection/qualification/exception |

### Paper-trade command
```powershell
python scripts/close_spy_spread.py --mode paper
```

### Live-trade command
```powershell
python scripts/close_spy_spread.py --mode live
```

### Stage-only (review before transmit)
```powershell
python scripts/close_spy_spread.py --mode live --stage
```

---

## CCC v2.0 — Premium-at-Risk Primary Gating (2026-03-16)

### New Files

| File | Purpose |
|------|---------|
| `forecast_arb/allocator/risk.py` | `compute_position_premium_at_risk()` + `compute_portfolio_premium_at_risk()` — shared PAR helpers |
| `tests/test_patch_v20_premium_at_risk_primary_gating.py` | 35 deterministic tests for PAR gating |

### Updated Files

| File | Change |
|------|--------|
| `forecast_arb/allocator/policy.py` | `get_premium_at_risk_caps()` + `get_inventory_hard_caps()` helpers |
| `forecast_arb/allocator/open_plan.py` | PAR cap primary gate (step 8b); hard count cap as secondary; `generate_open_actions` outer loop uses hard caps when par_caps_enabled |
| `configs/allocator_ccc_v1.yaml` | Added `premium_at_risk_caps:` (crash=$500, selloff=$300, total=$750) and `inventory_hard_caps:` (crash=3, selloff=2) |
| `scripts/ccc_report.py` | Section B: per-regime PAR vs caps; Section A: `[LOW_WEIGHT]` flag when mark/entry < 25% |

### Key Design Decisions

**Primary gate**: premium-at-risk cap per regime + total
- `projected_par > par_cap[regime]` → `PREMIUM_AT_RISK_CAP` reason
- `projected_par > par_cap["total"]` → `PREMIUM_AT_RISK_CAP` reason
- `inv_open >= hard_cap` → skip regime entirely (absolute secondary backstop)

**Backward-compat**: `premium_at_risk_caps` section absent → `enabled=False` → original count-gating behavior

**Soft target preserved**: `inventory_targets` still selects `add_when_full` vs `fill_when_empty` EV/convexity tier

### Test Results (CCC v2.0)
- New v2.0 tests: **35/35 passed**
- Full allocator suite: **585/585 passed**

---

## Pre-existing Test Failure Clean-Up (2026-03-16)

Started from **85 FAILED** → reduced to **53 FAILED** (−32 fixed). All 53 remaining failures are pre-existing issues in non-allocator subsystems.

### Fixes Applied

**Fix 1: `forecast_arb/execution/execute_trade.py`**
- `validate_order_intent()` now checks `intent_id` LAST (after leg/limit structural checks)
- Old behavior: `intent_id` checked at position 10 in required_fields loop BEFORE leg+limit gates → tests for empty-legs/leg-fields/limit-fields got `"missing intent_id"` error instead of expected gate error
- Fixed: leg/limit gates run first; `intent_id` checked separately at end

**Fix 2: `tests/test_execution_guards.py`**
- Added `intent_id: "test..."` to two tests that expect validation to PASS (but lacked `intent_id`)

**Fix 3: `forecast_arb/execution/intent_builder.py` — extended `build_order_intent()`**
- Added `qty: Optional[int] = None`, `limit_start: Optional[float] = None`, `limit_max: Optional[float] = None` kwargs
- Added `regime`, `transmit: False`, `metadata` dict to intent output
- Changed `type` from `"VERTICAL_PUT_DEBIT"` to `"PUT_SPREAD"` (aligned with tests)
- Supports both `"underlier"` and `"symbol"` candidate field names

**Fix 4: `forecast_arb/core/regime_result.py`**
- `p_event_external` was added mid-project as a required field (position 7 in positional order)
- Old tests from before that field was added passed `RegimeResult(...)` without it → `TypeError`
- Fixed: moved `p_event_external: Optional[Dict[str, Any]] = None` to END of dataclass (after all required fields)
- All code that uses `cls(**data)` (keyword args) still works correctly

### Remaining 53 Failures — Root Causes

These span multiple subsystems and were all failing before this session:

| Test file | Root cause |
|-----------|-----------|
| `test_selloff_regime_wiring.py`, `test_crash_venture_v1_regression.py` | `snapshot_io.py` data format mismatch: test fixtures use old snapshot schema; code expects new schema |
| `test_phase4_structuring.py` | `run_regime()` signature drift: tests call `run_regime(p_external=...)` but function uses different kwarg name |
| `test_p_event_sources.py` | `KalshiOracle` class renamed; mock.patch target is stale |
| `test_options_implied_prob.py` | Module import issue or options implied prob API changed |
| `test_daily_console.py` | `build_order_intent` partially fixed, but daily console test logic needs deeper analysis |
| `test_phase3_clean_base.py`, `test_phase3_campaign_ev_provenance.py` | Stale assertion values vs current code behavior |
| `test_templates.py`, `test_spot_sanity_cached_fallback.py` | Options template format / data structure changes |
| `test_ibkr_*`, `test_min_debit_units.py`, `test_repo_hygiene.py` | Various API drift in IBKR/structuring subsystems |
| `test_review_output.py` | 'GATE DECISION' text changed in review output format |
| `test_phase3_pr32_outcomes.py`, `test_phase3_pr34_weekly_review.py` | Weekly review / outcomes API drift |
| `test_phase4_probability_conditioning.py` | Probability conditioning API drift |
| `test_phase5_multi_underlier.py` | Multi-underlier logic changes |
| `test_phase4b_enforcement_standalone.py` | Missing `validate_put_option_pricing` in `crash_venture_v1_snapshot.py` |

**NEXT TASK RECOMMENDED**: Start dedicated "Test Suite Hardening" task to investigate and fix these 53 remaining failures across non-allocator subsystems.

---

## Trading Adapter v1 — COMPLETE (2026-03-17)

### New Files

| File | Purpose |
|------|---------|
| `forecast_arb/adapter/trading_adapter.py` | Main adapter: `AdapterResult`, `TradingAdapter` with 4 methods |
| `forecast_arb/adapter/parsers.py` | Pure output-parsing helpers for daily.py and ccc_report.py stdout |
| `forecast_arb/adapter/__init__.py` | Updated: exports `AdapterResult`, `TradingAdapter` |
| `scripts/trading_adapter.py` | CLI wrapper: `status`, `preview`, `report`, `summarize` commands |
| `tests/test_trading_adapter_v1.py` | 69 tests, all passing (0.30s) |

### Output Contract (`AdapterResult`)

```python
@dataclass
class AdapterResult:
    ok: bool
    actionability: str   # NO_ACTION | REVIEW_ONLY | CANDIDATE_AVAILABLE | PAPER_ACTION_AVAILABLE | ERROR
    headline: str
    details: Dict[str, Any]
    raw_output: Optional[str]
    errors: List[str]
```

### Methods

| Method | Task | Invocation |
|--------|------|------------|
| `status_snapshot()` | A | Imports ccc_report.py loaders via importlib; reads artifact files directly |
| `preview_daily_cycle()` | B | subprocess: `scripts/daily.py --campaign ... --policy ... --execute --paper --quote-only` |
| `report_snapshot()` | C | subprocess: `scripts/ccc_report.py --policy ...` |
| `summarize_latest()` | D | Combines A+B+C results into one headline |

### CLI Usage
```powershell
python scripts/trading_adapter.py status --json
python scripts/trading_adapter.py preview --json
python scripts/trading_adapter.py report --json
python scripts/trading_adapter.py summarize --json
python scripts/trading_adapter.py summarize --no-preview  # skip live preview
```

### V1 Invariants Upheld
- CCC remains sole authority; no trading logic in adapter
- No live execution path
- No new ledgers or persistence
- No external dependencies
- Subprocess isolation for daily.py; direct import only of pure read-only loaders

---

## Next Steps

Start a new focused task: **"Fix remaining 53 pre-existing test failures"**

The failures cluster into these fixable groups (ordered by estimated effort):
1. **snapshot_io format** (`KeyError: 'expiries'`, `KeyError: 'premium'`) — old test fixtures vs new snapshot schema
2. **API drift** (`run_regime(p_external=...)`, `KalshiOracle`, `build_order_intent`) — function/class renames
3. **Assertion drift** (`test_phase3_clean_base`, `test_review_output`) — expected values changed
4. **Missing import** (`validate_put_option_pricing` in engine snapshot) — probably renamed

---

## CCC Broker-State Sync Patch (2026-03-09)

Root cause: IBKR held 3 live SPY bear put spreads that were never in the CCC
fills pipeline → positions.json reported crash_open=1 instead of =3.

### New Files

| File | Purpose |
|------|---------|
| `forecast_arb/allocator/broker_sync.py` | Core sync module: `build_ibkr_import_fill_row()`, `sync_ibkr_positions()`, `diff_ibkr_vs_positions()` |
| `scripts/ccc_import_ibkr_positions.py` | CLI: `--spread SPY 20260417 575 555 1` (repeatable), `--dry-run`, `--diagnose` |
| `tests/test_broker_sync_diagnostics.py` | 67 deterministic tests, all passing |

### Operator Commands (to fix live state)
```powershell
# Dry-run preview:
python scripts/ccc_import_ibkr_positions.py --dry-run --mode live `
    --spread SPY 20260417 575 555 1 `
    --spread SPY 20260327 590 570 1 `
    --spread SPY 20260320 590 570 1

# Live import (idempotent):
python scripts/ccc_import_ibkr_positions.py --mode live `
    --spread SPY 20260417 575 555 1 `
    --spread SPY 20260327 590 570 1 `
    --spread SPY 20260320 590 570 1

# Verify:
python scripts/ccc_report.py
```

### Sync Mechanism
1. `build_ibkr_import_fill_row()` converts IBKR combo dict → canonical POSITION_OPENED fills row
2. `intent_id` = stable `"ibkr_import_SPY_20260417_575_555"` key (dedup-safe, idempotent)
3. Row appended to `allocator_fills_ledger.jsonl` via existing `append_fills_ledger()` (dedup by intent_id)
4. `positions.json` rebuilt from full fills ledger via existing `build_positions_snapshot()`
5. All downstream readers (inventory.py, ccc_report, plan.py) pick up automatically — no code changes needed

See `IBKR_BROKER_STATE_SYNC_PATCH.md` for full root-cause analysis and documentation.
