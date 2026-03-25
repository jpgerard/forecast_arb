# Intent Creation Pipeline Fix - Complete

**Date**: 2026-02-24  
**Status**: ✅ COMPLETE

## Summary

Successfully repaired the intent creation flow with no strategy changes. The pipeline now has a clean separation of concerns: `intent_builder` creates valid OrderIntent JSON files, and `daily.py` orchestrates the workflow by calling it via subprocess.

## Changes Made

### 1. ✅ intent_builder.py - Complete Rewrite

**Location**: `forecast_arb/execution/intent_builder.py`

**New Capabilities**:
- CLI interface with argparse (`--candidates`, `--regime`, `--rank`, `--output-dir`)
- Loads nested review_candidates schema: `regimes -> <regime> -> candidates -> [...]`
- Selects candidate by regime + rank
- Constructs OrderIntent with exact schema required by `execute_trade.validate_order_intent`
- Computes deterministic `intent_id` as SHA1 of sorted JSON (excluding intent_id itself)
- Writes file to `intents/` directory with readable filename format
- Prints file path to stdout for capture by caller
- Exits with code 1 if no candidate found or file write fails
- No silent success - always explicit output or error

**Schema Contract Met**:
```python
{
  "strategy": "crash_venture_v2",
  "symbol": "SPY",
  "expiry": "20260402",
  "type": "VERTICAL_PUT_DEBIT",
  "qty": 1,
  "limit": {"start": 49.0, "max": 49.98},
  "tif": "DAY",
  "guards": {
    "max_debit": 49.98,
    "max_spread_width": 0.20,
    "min_dte": 7
  },
  "legs": [
    {
      "action": "BUY",
      "right": "P",
      "strike": 580.0,
      "ratio": 1,
      "exchange": "SMART",
      "currency": "USD"
    },
    {
      "action": "SELL",
      "right": "P",
      "strike": 560.0,
      "ratio": 1,
      "exchange": "SMART",
      "currency": "USD"
    }
  ],
  "intent_id": "78db56f86fb9657e210f1d51804ed82d8a50f62d"
}
```

### 2. ✅ daily.py - Refactored to Use intent_builder

**Location**: `scripts/daily.py`

**Changes**:
- **Removed**: Manual OrderIntent construction logic
- **Removed**: `compute_intent_id()` function (now in intent_builder)
- **Removed**: `build_order_intent()` import from intent_builder module
- **Added**: `find_latest_run_dir()` - determines latest run_dir by directory timestamp
- **Updated**: `run_daily_orchestration()` - uses directory timestamps instead of stdout parsing
- **Updated**: `perform_quote_only()` - now calls intent_builder CLI via subprocess
- **Updated**: `offer_execution_options()` - accepts pre-created intent_path instead of building intents

**New Flow**:
1. Run orchestration → get run_dir by timestamp
2. Call `intent_builder` CLI to create OrderIntent
3. Capture intent file path from stdout
4. Pass intent file path to `execute_trade --quote-only`
5. If guards pass, reuse the already-created intent for paper/live execution

### 3. ✅ Test Suite

**Location**: `test_intent_builder_fix.py`

**Tests**:
- ✅ Test 1: Valid Output - intent_builder produces valid OrderIntent with all required fields
- ✅ Test 2: Deterministic Intent ID - same input produces same intent_id
- ✅ Test 3: No Candidate Error - exits 1 when rank not found
- ✅ Test 4: Invalid Regime Error - exits 1 when regime not found

**Results**: 4/4 tests passed

## Verification

### Schema Validation
```
✓ strategy: crash_venture_v2
✓ symbol: SPY
✓ expiry: 20260402
✓ type: VERTICAL_PUT_DEBIT
✓ legs: 2 legs (with action, right, strike, ratio, exchange, currency)
✓ qty: 1
✓ limit: {start: 49.0, max: 49.98}
✓ tif: DAY
✓ guards: {max_debit, max_spread_width, min_dte}
✓ intent_id: 78db56f86fb9657e210f1d51804ed82d8a50f62d (deterministic)
```

### Exit Code Validation
```
✓ Exits 0 on success
✓ Exits 1 if candidate not found (rank 999)
✓ Exits 1 if regime not found (invalid_regime)
✓ Prints file path to stdout on success
✓ Prints error to stderr on failure
```

### Integration Points
```
✓ daily.py calls intent_builder via subprocess
✓ Captures stdout to get intent file path
✓ Passes intent file to execute_trade
✓ No manual intent construction in daily.py
✓ Never writes to ledger (only execute_trade writes ledger)
```

## Hard Requirements - All Met

✅ **Did not modify strategy math**
- No changes to candidate selection logic
- No changes to structuring logic
- No changes to pricing calculations

✅ **Did not modify execute_trade guard logic**
- No changes to guard enforcement
- No changes to validation logic
- No changes to execution flow

✅ **Did not modify ledger system**
- daily.py never writes to ledger
- Only execute_trade writes to trade_outcomes.jsonl
- Ledger semantics unchanged

✅ **Did not change candidate schema**
- Reads existing review_candidates.json format
- No changes to candidate structure
- No changes to regime data structure

## Usage

### CLI Usage - intent_builder

```bash
# Create intent from review_candidates.json
python -m forecast_arb.execution.intent_builder \
  --candidates runs/crash_venture_v2/.../artifacts/review_candidates.json \
  --regime crash \
  --rank 1 \
  --output-dir intents

# Output: intents/SPY_20260402_580_560_crash_78db56f8.json
```

### Programmatic Usage - daily.py

The daily.py workflow now automatically calls intent_builder:

```bash
python scripts/daily.py --regime crash
```

This will:
1. Run orchestration
2. Call intent_builder automatically to create OrderIntent
3. Perform quote-only guard checks
4. Offer execution options (using the already-created intent)

## Files Modified

1. `forecast_arb/execution/intent_builder.py` - Complete rewrite with CLI
2. `scripts/daily.py` - Refactored to call intent_builder subprocess
3. `test_intent_builder_fix.py` - New test suite (4 tests, all passing)

## Files NOT Modified (As Required)

- `forecast_arb/execution/execute_trade.py` - Guard logic unchanged
- `forecast_arb/execution/outcome_ledger.py` - Ledger system unchanged
- `forecast_arb/structuring/` - Candidate generation unchanged
- `forecast_arb/engine/` - Strategy math unchanged

## Testing

Run test suite:
```bash
python test_intent_builder_fix.py
```

Expected output:
```
🎉 ALL TESTS PASSED
Passed: 4/4
```

## Conclusion

The intent creation pipeline has been successfully repaired with:
- ✅ Clean separation of concerns
- ✅ Deterministic intent_id generation
- ✅ Proper error handling with non-zero exit codes
- ✅ No silent failures
- ✅ Schema contract compliance with execute_trade
- ✅ No strategy or guard logic changes
- ✅ No ledger system modifications

The pipeline is now production-ready and follows best practices for subprocess orchestration.
