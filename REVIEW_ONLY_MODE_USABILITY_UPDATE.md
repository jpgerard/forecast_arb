# Review-Only Mode Usability Update

**Date:** 2026-01-30  
**Goal:** Make review-only mode actually usable day to day

## Summary

This update implements unified expiry selection and ensures review pack generation always happens in review-only mode, even when there are zero candidate structures. The changes make review-only mode reliable and practical for daily manual review workflows.

## Changes Implemented

### 1. Unified Expiry Selection (`forecast_arb/structuring/expiry_selection.py`)

**NEW FILE** - Centralized expiry selection logic based on coverage score.

#### Function: `select_best_expiry(snapshot, target_dte, dte_min, dte_max)`

**Strategy:**
1. Filter expiries by DTE range (if specified)
2. Compute coverage score for each expiry based on:
   - Number of puts/calls with executable bid/ask (50% weight)
   - Number of puts/calls with IV data (30% weight)
   - Average spread quality (20% weight)
3. Select expiry with best coverage, preferring closest DTE to target

**Returns:**
- `(selected_expiry, diagnostics)` tuple
- `selected_expiry`: Best expiry string (YYYYMMDD) or None
- `diagnostics`: Dict with selection details and per-expiry scores

**Coverage Score Components:**
```python
total_score = (
    0.50 * executable_ratio +
    0.30 * iv_ratio +
    0.20 * avg_spread_quality
)
```

### 2. Updated `run_daily.py` - Expiry Selection

**Location:** Step 2.5 (Options-Implied Probability & Edge Gating)

**Before:**
```python
# Pick the expiry to use for p_implied calculation
expiries = get_expiries(snapshot)
target_expiry = None

for exp in sorted(expiries):
    T = compute_time_to_expiry(metadata['snapshot_time'], exp)
    dte = int(T * 365)
    if args.dte_min <= dte <= args.dte_max:
        target_expiry = exp
        break
```

**After:**
```python
from forecast_arb.structuring.expiry_selection import select_best_expiry

target_dte_midpoint = (args.dte_min + args.dte_max) // 2

target_expiry, expiry_diagnostics = select_best_expiry(
    snapshot=snapshot,
    target_dte=target_dte_midpoint,
    dte_min=args.dte_min,
    dte_max=args.dte_max
)
```

**Benefits:**
- Uses expiry with best quote coverage
- Ensures p_implied uses the same expiry as structuring
- Provides diagnostics for selection reasoning

### 3. Updated `crash_venture_v1_snapshot.py` - Expiry Selection

**Before:**
```python
# Select expiry (use first available in snapshot)
expiries = get_expiries(snapshot)
if not expiries:
    raise ValueError("No expiries in snapshot")

expiry = expiries[0]  # Use first expiry
```

**After:**
```python
from ..structuring.expiry_selection import select_best_expiry

dte_min = struct_config["dte_range_days"]["min"]
dte_max = struct_config["dte_range_days"]["max"]
target_dte_midpoint = (dte_min + dte_max) // 2

expiry, expiry_diagnostics = select_best_expiry(
    snapshot=snapshot,
    target_dte=target_dte_midpoint,
    dte_min=dte_min,
    dte_max=dte_max
)
```

**Benefits:**
- Structuring engine uses best expiry (not just first)
- Coverage score logged for debugging
- Consistent with p_implied calculation

### 4. Review Pack Generation - Always Happens

**Location:** `run_daily.py` - Step 5

**Before:**
```python
# If review-only mode and we have structures, generate review artifacts
if args.review_only_structuring and result['top_structures']:
    logger.info("⚠️  REVIEW-ONLY MODE: Generating review artifacts")
```

**After:**
```python
# ALWAYS generate review artifacts in review-only mode (even with 0 candidates)
if args.review_only_structuring:
    logger.info("⚠️  REVIEW-ONLY MODE: Generating review artifacts")
    logger.info(f"   Candidates found: {len(result.get('top_structures', []))}")
```

**Impact:**
- Review pack generated even when structuring produces 0 candidates
- Always writes:
  - `artifacts/review_pack.md`
  - `artifacts/review_candidates.json` (empty list if 0 candidates)
  - `artifacts/decision_template.md`
- Ensures operator has complete context for nil decisions

### 5. Filter Diagnostics - Already Present

**Status:** ✅ Already implemented in `crash_venture_v1_snapshot.py`

Filter diagnostics automatically written when candidates are filtered:
- Location: `run_dir/filter_diagnostics.json`
- Contains:  
  - Moneyness targets
  - Requested vs effective widths
  - Strike details
  - **Filter reasons** (e.g., `NO_EXECUTABLE_PRICE_LONG`, `Debit per contract $X < min $Y`)

### 6. Step 5 Summary Correctness

**Status:** ✅ Already correct

The Step 5 logging accurately reflects:
- `structuring_ran`: True/False based on `skip_structuring` variable
- `review_only`: True/False from `args.review_only_structuring`  
- Candidate count displayed in all cases

## Key Benefits

### For Daily Operations

1. **Unified Expiry Logic:**
   - p_implied and structuring always use the same expiry
   - Selection based on quote quality rather than arbitrary ordering
   - Diagnostic output explains selection

2. **Zero-Candidate Scenario:**
   - Review pack still generated with full context
   - Operator can see why no candidates (via filter_diagnostics.json)
   - Decision checklist available even with empty slate

3. **Coverage Transparency:**
   - Coverage score (0.0-1.0) indicates quote quality
   - Breakdown by executable count, IV count, spread quality
   - Helps operator assess data reliability

### For Manual Review

**Every review-only run now produces:**
```
artifacts/
├── review_pack.md          # Human-readable summary
├── review_candidates.json  # Structured data (even if [])
├── decision_template.md     # Manual decision capture
├── filter_diagnostics.json  # Why candidates filtered (if any)
├── gate_decision.json       # Edge gate result
└── p_event_implied.json     # Options-implied probability
```

**Review pack includes:**
- Snapshot summary (spot, time, expiry, DTE)
- Event definition with moneyness
- Probabilities (p_external, p_implied, edge)
- Gate decision & reasoning
- External source policy status
- Candidate table (or "0 candidates" with diagnostics)
- JP decision checklist
- Manual operator section with order entry template

## Testing Recommendations

### Acceptance Test 1: Review-Only During Market Hours

```powershell
python scripts/run_daily.py `
  --review-only-structuring `
  --snapshot snapshots/SPY_snapshot_LATEST.json `
  --fallback-p 0.30
```

**Verify:**
- ✅ review_pack.md exists
- ✅ Selected expiry in pack matches expiry used for p_implied
- ✅ Coverage score logged in console output
- ✅ All artifacts written

### Acceptance Test 2: Zero Candidates Scenario

```powershell
python scripts/run_daily.py `
  --review-only-structuring `
  --snapshot snapshots/SPY_snapshot_LATEST.json `
  --fallback-p 0.30 `
  --min-debit-per-contract 500.0
```

**Verify:**
- ✅ review_pack.md exists (even with 0 candidates)
- ✅ review_candidates.json contains `[]`
- ✅ filter_diagnostics.json explains why candidates filtered
- ✅ review_pack shows "0 candidates" with diagnostics

### Acceptance Test 3: Expiry Consistency

Run any test and confirm:
- ✅ Console log shows same expiry for p_implied calculation
- ✅ Structuring uses same expiry
- ✅ review_pack.md "Expiry Used" matches both

## Files Modified

1. **NEW:** `forecast_arb/structuring/expiry_selection.py` (233 lines)
2. **MODIFIED:** `scripts/run_daily.py` (unified expiry selection, always-generate review pack)
3. **MODIFIED:** `forecast_arb/engine/crash_venture_v1_snapshot.py` (unified expiry selection)

## Files Unchanged (Already Correct)

- `forecast_arb/review/review_pack.py` - Already handles zero candidates gracefully
- `forecast_arb/engine/crash_venture_v1_snapshot.py` - Already writes filter_diagnostics.json
- Step 5 summary logging - Already accurate

## Optional Enhancement (Not Implemented)

**Reduce IBKR Noise for Error 200:**

Contract qualifier errors (error code 200) could be logged at DEBUG level with summary count only. This would reduce noise in logs without losing information.

**Implementation location:** `forecast_arb/ibkr/snapshot.py`

**Current:** Each error 200 logged at WARNING  
**Proposed:** Debug log per error, summary log at end (e.g., "Attempted: 150, Qualified: 148, Unknown: 2")

**Status:** Deferred - not critical for review-only mode usability

## Conclusion

Review-only mode is now production-ready for daily manual reviews:

✅ **Always produces paste-ready review_pack.md** (even with 0 candidates)  
✅ **Unified expiry selection** ensures p_implied and structuring agree  
✅ **Coverage-based selection** uses expiry with best quote quality  
✅ **Filter diagnostics** explain why candidates were rejected  
✅ **Step 5 summary** correctly reflects what actually happened

The operator can now run `--review-only-structuring` confidently every day and receive complete context for manual trading decisions.
