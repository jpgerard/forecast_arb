# Phase 3 Campaign EV Provenance Fix - COMPLETE

**Date**: 2026-02-26  
**Status**: ✅ COMPLETE  
**Tests**: 5/5 PASSING

## Problem Statement

In campaign mode, we observed:
```
EV/$ RECALCULATED: stored=55.95, recalc=1.88 (using p=0.046)
```

This indicated that upstream candidate EV fields were not computed with the same probability policy used by the campaign selector, resulting in inconsistent and non-auditable selection.

## Hard Constraints (Maintained)

- ✅ Did NOT change strategy math or structuring generation logic
- ✅ Did NOT change execute_trade behavior (remains sole ledger writer)
- ✅ Campaign remains an additive layer
- ✅ Selection is deterministic

## Changes Implemented

### 1. Canonicalized Campaign Fields (Raw vs Canonical)

**File**: `forecast_arb/campaign/grid_runner.py`

Extended `flatten_candidate()` to store BOTH:

**Raw fields** (from generator, may use different probability):
- `ev_per_dollar_raw`
- `prob_profit_raw`
- `ev_usd_raw`

**Canonical fields** (recomputed by campaign using p_used):
- `ev_per_dollar` (CANONICAL - used for scoring)
- `ev_usd`
- `p_profit`
- `p_used` (the probability used for EV calculation)
- `p_used_src` ("external" | "implied" | "fallback")
- `p_impl` (options-implied probability)
- `p_ext` (external/Kalshi probability)
- `p_ext_status` ("OK" | "AUTH_FAIL" | "NO_MARKET")
- `p_ext_reason`

**Key Logic**:
```python
# Determine p_used with priority logic
if p_ext is not None and regime_p_external.get("authoritative"):
    p_used = p_ext
    p_used_src = "external"
elif p_impl is not None:
    p_used = p_impl
    p_used_src = "implied"
else:
    p_used = fallback
    p_used_src = "fallback"

# Compute canonical EV using p_used
ev_usd = p_used * max_gain - (1 - p_used) * debit
ev_per_dollar = ev_usd / debit
```

**Warnings**: If raw EV differs from canonical by > 1 cent, log warning but continue.

### 2. Updated Selector to Use Only Canonical Fields

**File**: `forecast_arb/campaign/selector.py`

**Changes**:
- Added `compute_robustness_score()` function
- Selector ONLY uses `ev_per_dollar` (canonical) for scoring
- **Validates** canonical field exists - raises `ValueError` if missing (no silent defaults)
- Sorting: `adjusted_score = ev_per_dollar * robustness`

**Example validation**:
```python
canonical_ev = candidate.get(scoring_method)
if canonical_ev is None:
    raise ValueError(
        f"Candidate {candidate_id} missing required canonical field '{scoring_method}'"
    )
```

### 3. Added Robustness Penalty

**Logic**:
```python
robustness = 1.0
if p_used_src == "fallback": robustness *= 0.5
if p_ext_status in ["NO_MARKET", "BLOCKED", "AUTH_FAIL"]: robustness *= 0.7
if not representable: robustness = 0.0
```

**Flags persisted**:
- `robustness` (float)
- `robustness_flags` (list[str]), e.g. `["P_FALLBACK", "P_EXT_NO_MARKET"]`

**Impact**: Candidates with fallback probability or no market data are down-weighted in selection, biasing toward authoritative external sources.

### 4. Fixed Console Labeling

**File**: `scripts/daily.py`

**Changes**:
- Replaced table column `P(Win)` with `P(event)`
- Display shows `p_used` (same as `p_event`)
- Clarified that P(event) is the probability used for EV calculation

**Before**:
```
P(Win) 95.1%
```

**After**:
```
P(event) 4.6%
```

This eliminates confusion between "probability of profit" and "probability of event" used in EV calculation.

### 5. Added Comprehensive Tests

**File**: `test_phase3_campaign_standalone.py`

**Tests (all passing)**:
1. ✅ **Canonical EV (not raw)**: Verifies selector ranks by canonical EV, not raw
2. ✅ **Robustness penalty**: Verifies fallback sources are penalized  
3. ✅ **Missing canonical EV error**: Verifies no silent defaults to 0
4. ✅ **Robustness computation**: Tests all penalty scenarios
5. ✅ **Probability consistency**: Verifies p_used = p_event_used = p_event

## Test Results

```
🎉 ALL TESTS PASSED!
Total: 5/5 tests passed

✅ PASSED: Canonical EV (not raw)
✅ PASSED: Robustness penalty
✅ PASSED: Missing canonical EV error
✅ PASSED: Robustness computation
✅ PASSED: Probability consistency
```

## Artifacts Updated

### candidates_flat.json Schema

Now includes both raw and canonical fields:

```json
{
  "candidate_id": "...",
  
  "ev_per_dollar_raw": 55.95,
  "prob_profit_raw": 0.95,
  "ev_usd_raw": 5595.0,
  
  "ev_per_dollar": 1.88,
  "ev_usd": 188.0,
  "p_profit": 0.046,
  "p_used": 0.046,
  "p_used_src": "external",
  "p_impl": 0.040,
  "p_ext": 0.046,
  "p_ext_status": "OK",
  "p_ext_reason": "Authoritative external source used"
}
```

### recommended.json Schema

Includes canonical fields + robustness:

```json
{
  "selected": [{
    "ev_per_dollar": 1.88,
    "ev_per_dollar_raw": 55.95,
    "p_used": 0.046,
    "p_used_src": "external",
    "robustness": 1.0,
    "robustness_flags": [],
    "p_ext_status": "OK"
  }],
  "selection_summary": {
    "robustness_stats": {
      "external_count": 2,
      "implied_count": 0,
      "fallback_count": 1,
      "no_market_count": 1
    }
  }
}
```

## Acceptance Criteria Met

✅ **candidates_flat.json includes raw + canonical fields**  
✅ **recommended.json includes raw + canonical + robustness**  
✅ **Selector ranking uses canonical EV with robustness penalty**  
✅ **Console labels probability correctly ("P(event)" = p_used)**  
✅ **Tests added and passing; no silent defaults to 0**

## Backwards Compatibility

Legacy fields preserved for backwards compatibility:
- `p_event_used` → maps to `p_used`
- `p_event` → maps to `p_used`
- `p_implied` → maps to `p_impl`
- `p_external` → maps to `p_ext`
- `p_source` → maps to `p_used_src`
- `prob_profit` → maps to `p_profit`

## Example Warning Output

When raw and canonical differ:

```
⚠️  EV/$ MISMATCH in SPY_crash_30-60d_rank1: 
    raw=55.95, canonical=1.88 (using p_used=0.046 from external)
```

Selection continues using canonical value (consistent, auditable).

## Next Steps

1. ✅ Run campaign mode to verify field population
2. ✅ Verify console displays P(event) correctly
3. ✅ Check recommended.json includes robustness stats
4. ✅ Confirm selection is deterministic across runs

## Files Modified

1. `forecast_arb/campaign/grid_runner.py` - Extended schema, canonical EV computation
2. `forecast_arb/campaign/selector.py` - Canonical-only scoring, robustness penalty
3. `scripts/daily.py` - Fixed P(Win) → P(event) labeling
4. `tests/test_phase3_campaign_ev_provenance.py` - Comprehensive tests (pytest)
5. `test_phase3_campaign_standalone.py` - Standalone test runner (no pytest dependency)

## Summary

The campaign mode now correctly separates raw metrics (from generator) and canonical metrics (recomputed by campaign). Selection is based exclusively on canonical EV computed with a consistent probability policy (p_used). Robustness penalties bias selection toward candidates with authoritative external probability sources. All changes are auditable through expanded field sets in candidates_flat.json and recommended.json.

**Result**: Selection is now consistent, deterministic, and auditable. No more "EV/$ RECALCULATED" confusion.
