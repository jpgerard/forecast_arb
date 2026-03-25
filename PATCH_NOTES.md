# Patch Notes: Real-Cycle Output Fixes

## Summary
Fixed 3 critical issues in real-cycle output to ensure correct unit handling, width integrity, and min-debit filtering.

## Changes Made

### 1. Fixed Reporting Layer (Issue #1)
**File**: `scripts/run_real_cycle.py`

**Problem**: Script was printing debit/max_loss values without clarifying units (per-share vs per-contract), causing confusion.

**Solution**:
- Updated printout to display both per-contract and per-share values explicitly
- Use fields directly from structures.json without recomputation
- Format:
  ```
  Debit (per contract): $235.00
  Debit (per share): $2.3500
  Max Loss (per contract): $235.00
  Max Loss (per share): $2.3500
  ```

### 2. Enforced Width Integrity (Issue #2)
**File**: `forecast_arb/engine/crash_venture_v1_snapshot.py`

**Problem**: Requested spread widths could collapse into identical structures when actual strikes didn't match targets exactly.

**Solution**:
- Added width deviation check in `generate_candidates_from_snapshot()`
- Enforce: `|effective_width - requested_width| ≤ $2.50`
- Filter out candidates that exceed this tolerance
- Log both requested_width and effective_width in diagnostics

**Code Addition**:
```python
# ENFORCE WIDTH INTEGRITY: Check effective width vs requested width
effective_width = K_long - K_short
width_deviation = abs(effective_width - width)

if width_deviation > 2.50:
    reason = f"Width deviation too large: requested=${width}, effective=${effective_width:.2f}, deviation=${width_deviation:.2f} > $2.50"
    logger.warning(reason)
    filtered_out.append({...})
    continue
```

### 3. Replaced min_debit Filter (Issue #3)
**Files**: 
- `forecast_arb/engine/crash_venture_v1_snapshot.py`
- `forecast_arb/structuring/output_formatter.py`

**Problem**: 
- Was using min_debit_per_share (ambiguous units)
- EV_per_dollar calculated incorrectly using max_loss_per_share instead of debit_per_contract

**Solution**:
- Removed min_debit_per_share flag
- Added `min_debit_per_contract` parameter (default $30)
- Updated EV_per_dollar calculation:
  ```python
  # OLD (incorrect):
  ev_per_dollar = ev / max_loss_per_share
  
  # NEW (correct):
  debit_per_contract = eval_result.get("debit_per_contract", 0)
  assert debit_per_contract > 0
  ev_per_contract = eval_result["ev"] * 100
  ev_per_dollar = ev_per_contract / debit_per_contract
  ```

### 4. Updated Output Formatter
**File**: `forecast_arb/structuring/output_formatter.py`

**Problem**: Formatter was recomputing per-contract values from per-share, overwriting correct values from snapshot mode.

**Solution**:
- Check if `debit_per_contract` already exists in structure
- If yes, use existing values (from snapshot mode)
- If no, compute from per-share values (legacy mode)
- Preserve ev_per_dollar if already calculated

## Tests Added

### Test 1: Width Integrity (`test_width_integrity`)
- Ensures requested widths don't collapse into duplicate structures
- Verifies width deviation ≤ $2.50 tolerance
- Checks diagnostics include width deviation info

### Test 2: Max Loss Validation (`test_max_loss_never_zero_with_positive_debit`)
- Ensures max_loss > 0 when debit > 0
- Validates max_loss_per_contract == debit_per_contract for put spreads
- Verifies EV_per_dollar calculation correctness

## Test Results
All 11 tests passing:
```
✓ test_load_ibkr_snapshot_spy
✓ test_strikes_are_in_snapshot  
✓ test_find_nearest_strike
✓ test_validate_put_option_pricing
✓ test_compute_debit_from_put_spread
✓ test_generate_candidates_from_snapshot
✓ test_run_with_ibkr_snapshot
✓ test_min_debit_filter
✓ test_min_debit_per_contract_units (NEW REGRESSION TEST)
✓ test_width_integrity (NEW)
✓ test_max_loss_never_zero_with_positive_debit (NEW)
```

## Files Modified
1. `scripts/run_real_cycle.py` - Updated reporting
2. `forecast_arb/engine/crash_venture_v1_snapshot.py` - Width integrity + EV calculation
3. `forecast_arb/structuring/output_formatter.py` - Preserve per-contract fields
4. `tests/test_run_real_cycle.py` - Added 2 new tests
5. `configs/test_structuring_crash_venture_v1.yaml` - Test config (NEW)

## Backward Compatibility
- All changes are backward compatible
- Legacy mode (computing from per-share) still works
- New snapshot mode uses per-contract fields directly
- Default min_debit_per_contract = $30 (reasonable for SPY puts)

## Validation
- Width integrity prevents duplicate structures
- Unit clarity prevents trading errors
- EV_per_dollar now correctly measures return on capital risked
- All sanity checks assert debit/max_loss/max_gain > 0
