# High Priority Patch Summary

## Overview
This patch fixes critical IBKR spot price selection issues and output math integrity bugs without adding new features.

## Changes Made

### A) IBKR Spot Price Correctness (`forecast_arb/data/ibkr_snapshot.py`)

**Problem**: Contract resolution/qualification issue causing incorrect SPY spot prices.

**Fixes**:
1. **Corrected price selection priority order**:
   - Priority 1: `last` (most recent trade)
   - Priority 2: `(bid + ask) / 2` (midpoint)
   - Priority 3: `close` (mark as stale with warning)
   - Fail fast if no valid data

2. **Added SPY price range sanity check**:
   - Spot must be within [250, 800] for SPY
   - Configurable for other symbols
   - Aborts with clear error if violated

3. **Added ATM strike validation**:
   - ATM strike must exist within +/- $5 of spot
   - Aborts if no nearby strike found
   - Prevents incorrect contract qualification

4. **Enhanced contract qualification**:
   - SPY: Stock("SPY", "SMART", "USD", primaryExchange="ARCA")
   - Other symbols: Stock(symbol, "SMART", "USD")

5. **Complete audit trail**:
   - Returns dict with `spot`, `source`, `is_stale` flags
   - Stores raw fields: `raw_last`, `raw_bid`, `raw_ask`, `raw_close`, `raw_market_price`
   - All data preserved in snapshot metadata for traceability

### B) Output Math Integrity (`forecast_arb/structuring/output_formatter.py`)

**Problem**: Sign/unit inconsistencies in crash_venture_v1 output formatting.

**Fixes**:
1. **max_loss_per_contract always positive**:
   - Used `abs()` to ensure positive values everywhere
   - Prevents negative loss display

2. **Fixed ev_per_dollar calculation**:
   - Changed from: `ev / max_loss` (incorrect for debit spreads)
   - Changed to: `ev_per_contract / debit_per_contract` (correct)
   - For crash_venture_v1: `debit_per_contract == max_loss_per_contract`

3. **Added regression test** (`tests/test_ev_per_dollar_regression.py`):
   - Tests ev_per_dollar is nonzero for known synthetic cases
   - Validates: `ev_per_dollar = ev_per_contract / debit_per_contract`
   - Validates: `debit_per_contract == max_loss_per_contract` for debit spreads
   - Tests edge cases (negative EV, zero debit)
   - All tests passing ✓

### C) Spot Audit Trail (Snapshot Metadata)

**Added to snapshot metadata**:
```json
{
  "spot_source": "last|midpoint|close",
  "spot_is_stale": false,
  "spot_audit": {
    "raw_last": 580.45,
    "raw_bid": 580.40,
    "raw_ask": 580.50,
    "raw_close": 580.30,
    "raw_market_price": 580.45
  },
  "atm_strike": 580.0,
  "atm_distance": 0.45
}
```

### D) Messaging Improvements (`scripts/run_real_cycle.py`)

**Fixes**:
1. **Removed "real trade recommendations" language when using fallback p_event**
2. **Added clear "SMOKE TEST MODE" warning**:
   ```
   ================================================================================
   SMOKE TEST MODE: Using fallback p_event (no real Kalshi market data)
   Fallback p_event: 0.300
   ================================================================================
   ```
3. **Clear distinction between production and testing modes**

## Testing

All regression tests passing:
- ✅ `test_ev_per_dollar_regression.py` (4 tests)
- ✅ `test_units_regression.py` (2 tests)

## Impact

**IBKR Snapshot**:
- SPY spot prices will now be accurate and reliable
- Clear validation prevents bad data from entering system
- Full audit trail for debugging

**Output Formatting**:
- All monetary values display correctly (positive)
- EV/dollar ratio correctly calculated
- Consistent units throughout

**User Experience**:
- Clear warnings when running in test mode
- Better error messages for debugging
- Improved data quality assurance

## Files Modified

1. `forecast_arb/data/ibkr_snapshot.py` - Spot price logic + validation
2. `forecast_arb/structuring/output_formatter.py` - Math fixes
3. `scripts/run_real_cycle.py` - Messaging improvements
4. `tests/test_ev_per_dollar_regression.py` - New regression tests

## Backward Compatibility

- ✅ Existing snapshots remain valid (new fields added to metadata)
- ✅ All existing tests still pass
- ✅ No breaking changes to public APIs
- ✅ Enhanced error handling (fail fast with clear messages)
