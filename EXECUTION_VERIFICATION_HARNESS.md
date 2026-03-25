# Execution Verification Harness

**Status:** ✅ Complete  
**Date:** 2026-02-03  
**Goal:** Prove the OrderIntent → execute_trade refactor is safe and reliable under real market-data conditions

## Overview

This harness adds verification, safety hardening, logging, and smoke tests for the execution flow **without changing architecture, adding auto-trading, or modifying strategy logic**.

## Features Added

### 1. Quote-Only Mode (`--quote-only`)

**Purpose:** Fetch live quotes, run all guards, and print diagnostics WITHOUT placing any order.

**Usage:**
```powershell
python -m forecast_arb.execution.execute_trade `
  --intent path/to/order_intent.json `
  --paper `
  --quote-only
```

**Behavior:**
- Connects to IBKR
- Qualifies contracts
- Fetches live bid/ask quotes for each leg
- Computes synthetic spread bid/ask/mid
- Runs ALL guards
- Prints ticket summary
- **Exits without placing order** (even staged)

**Output Example:**
```
================================================================================
TICKET SUMMARY
================================================================================
INTENT: SPY 20260320 P570/P590 x1  LIMIT start=45.00 max=47.25  transmit=false
LEGS: 590P bid/ask/mid=51.00/52.00/51.50 | 570P bid/ask/mid=6.00/7.00/6.50
SPREAD(synth): bid/ask/mid=44.00/46.00/45.00
SPREAD(combo): bid/ask/mid=N/A (combo quotes not implemented)
GUARDS: max_debit=PASS | min_dte=PASS | max_spread_width=PASS
DECISION: OK_TO_STAGE (quote-only mode, not placing order)
================================================================================
```

### 2. Enhanced Diagnostics

**Human-Usable Output:**
- Intent summary (symbol, expiry, strikes, qty, limits)
- Quote summary (leg bid/ask/mid, synthetic spread bid/ask/mid)
- Guard checks (PASS/FAIL per guard with explicit reasons)
- Final recommended action (OK_TO_STAGE / ABORT: <reason>)

**Guard Status:**
Each guard prints individual PASS/FAIL status:
- `executable_legs=PASS` - All legs have bid/ask quotes
- `max_spread_width=PASS` - Spread width within limits
- `max_debit=PASS` - Debit within max allowed
- `min_dte=PASS` - Days to expiry meets minimum

### 3. Safety Hardening

**Hard Safety Improvements:**

1. **Transmit requires explicit confirm:**
   ```powershell
   # This will ABORT:
   python -m forecast_arb.execution.execute_trade --intent X --live --transmit
   
   # This is required:
   python -m forecast_arb.execution.execute_trade --intent X --live --transmit --confirm SEND
   ```

2. **Intent transmit=true ignored unless CLI flag set:**
   - If intent JSON has `"transmit": true` but CLI `--transmit` is missing → treated as false
   - Prevents accidental transmission from intent files

3. **Live mode + transmit requires confirmation:**
   - `--live --transmit` without `--confirm SEND` → hard abort
   - No silent fallbacks

### 4. Failure-Mode Smoke Tests

**Location:** `tests/test_execution_guards.py`

**Purpose:** Validate guard validation failures produce deterministic error messages (no IBKR needed).

**Test Coverage:**
- ✅ Missing required fields (strategy, symbol, legs, etc.)
- ✅ Empty legs
- ✅ Leg missing action/right/strike
- ✅ Invalid expiry format
- ✅ Limit missing start/max
- ✅ Guard violation: debit too high
- ✅ Guard violation: DTE too low
- ✅ Guard violation: missing executable legs
- ✅ Guard violation: spread width too wide
- ✅ Valid intent passes all validations

**Run Tests:**
```powershell
python -m pytest tests/test_execution_guards.py -v
```

**Result:** 15/15 tests passing ✅

### 5. End-to-End Integration Script

**Location:** `tools/smoke_intent_flow.py`

**Purpose:** Automated end-to-end test from review_candidates.json → quote-only execution.

**Usage:**
```powershell
python -m tools.smoke_intent_flow `
  --run-dir runs/crash_venture_v1_1/crash_venture_v1_1_XXX `
  --rank 1 `
  --paper
```

**What It Does:**
1. Loads `review_candidates.json` from run directory
2. Emits `order_intent.json` for specified rank
3. Runs `execute_trade.py --quote-only --paper`
4. Prints summary ticket
5. **No orders placed**

**Example Output:**
```
================================================================================
SMOKE TEST: INTENT FLOW
================================================================================
Run Dir: runs/crash_venture_v1_1/crash_venture_v1_1_XXX
Rank: 1
Mode: PAPER
================================================================================

STEP 1: Emitting OrderIntent from review_candidates.json
--------------------------------------------------------------------------------
✓ Emitted OrderIntent for rank 1: runs/.../artifacts/order_intent.json
  Symbol: SPY
  Expiry: 20260320
  Strikes: 590/570
  Limit: $45.00 (max $47.25)

STEP 2: Running execute_trade.py in quote-only mode
--------------------------------------------------------------------------------
Command: python -m forecast_arb.execution.execute_trade --intent ... --quote-only --paper

[... execute_trade output ...]

================================================================================
✅ SMOKE TEST PASSED
================================================================================

Summary:
  ✓ Intent emitted from rank 1
  ✓ Quote-only mode executed successfully
  ✓ No orders placed (quote-only)
```

### 6. Intent Builder Module

**Location:** `forecast_arb/execution/intent_builder.py`

**Purpose:** Build OrderIntent JSON from review_candidates.json

**Functions:**

**`build_order_intent_from_candidate(candidate, qty=1, tif="DAY", transmit=False, guards=None)`**
- Converts a candidate dict to OrderIntent
- Extracts strikes, expiry, pricing
- Applies default guards if not specified
- Returns OrderIntent dict

**`emit_intent_from_run_dir(run_dir, rank=1, output_path=None)`**
- Loads review_candidates.json from run directory
- Finds candidate with specified rank
- Builds and writes order_intent.json
- Returns path to intent file

**Example:**
```python
from forecast_arb.execution.intent_builder import emit_intent_from_run_dir

intent_path = emit_intent_from_run_dir(
    run_dir="runs/crash_venture_v1_1/crash_venture_v1_1_XXX",
    rank=1
)
```

## Usage Workflow

### Manual Quote-Only Test

1. **Get run directory with candidates:**
   ```powershell
   # Find latest run
   $runDir = (Get-Content runs/LATEST.json | ConvertFrom-Json).latest_run_dir
   ```

2. **Emit intent:**
   ```powershell
   python -c "from forecast_arb.execution.intent_builder import emit_intent_from_run_dir; emit_intent_from_run_dir('$runDir', rank=1)"
   ```

3. **Run quote-only:**
   ```powershell
   python -m forecast_arb.execution.execute_trade `
     --intent "$runDir/artifacts/order_intent.json" `
     --paper `
     --quote-only
   ```

### Automated Smoke Test

```powershell
python -m tools.smoke_intent_flow --run-dir <run_dir> --paper
```

### Stage Order (No Transmission)

```powershell
python -m forecast_arb.execution.execute_trade `
  --intent path/to/order_intent.json `
  --paper
```
*(Order staged but NOT transmitted)*

### Transmit Order (LIVE TRADING)

```powershell
python -m forecast_arb.execution.execute_trade `
  --intent path/to/order_intent.json `
  --live `
  --transmit `
  --confirm SEND
```
**⚠️ WARNING:** This will place a LIVE order on the exchange!

## Safety Checklist

Before using `--live --transmit`:

- [ ] Verified quotes in `--quote-only` mode
- [ ] All guards passing
- [ ] Strikes and expiry correct
- [ ] Limit prices reasonable
- [ ] Position size appropriate
- [ ] Account has sufficient capital
- [ ] TWS/Gateway connected to correct account
- [ ] Confirmed `--confirm SEND` is set

## Guard Configuration

Default guards in intent_builder:
```json
{
  "max_debit": <limit_max * 1.1>,
  "min_dte": 7,
  "max_spread_width": 0.20,
  "require_executable_legs": false
}
```

Custom guards can be provided:
```python
from forecast_arb.execution.intent_builder import build_order_intent_from_candidate

intent = build_order_intent_from_candidate(
    candidate=candidate,
    guards={
        "max_debit": 50.0,
        "min_dte": 14,
        "max_spread_width": 0.15,
        "require_executable_legs": True
    }
)
```

## File Structure

```
forecast_arb/
├── execution/
│   ├── execute_trade.py      # Main execution module (enhanced)
│   ├── intent_builder.py     # OrderIntent builder (new)
│   ├── tickets.py             # Ticket schemas
│   ├── review.py              # Review formatters
│   └── ibkr_submit.py         # IBKR submission logic
├── tests/
│   └── test_execution_guards.py  # Guard smoke tests (new)
└── tools/
    └── smoke_intent_flow.py   # E2E integration script (new)
```

## What Was NOT Changed

As per requirements:

- ❌ No repo restructure
- ❌ No automation for selecting trades
- ❌ No modification to strategy logic
- ❌ No modification to gating thresholds
- ❌ No auto-submission in run_daily
- ❌ No architecture changes

## Testing Summary

**Failure-Mode Smoke Tests:**
- 15/15 tests passing ✅
- No IBKR connectivity required
- Validates all guard failure modes
- Deterministic error messages verified

**Manual Testing Required:**
- End-to-end smoke test with live IBKR connection (paper account)
- Verify quote-only mode fetches real quotes
- Verify guard checks work with real market data

## Next Steps

1. **Test with paper account:**
   ```powershell
   python -m tools.smoke_intent_flow --run-dir <recent_run> --paper
   ```

2. **Verify quotes match market:**
   - Compare synthetic spread vs. real spread
   - Check if guards would pass with real data

3. **Consider adding:**
   - Combo quote fetching (currently shows N/A)
   - Historical comparison of quote quality
   - Alert if synthetic vs. combo spread diverges significantly

## Conclusion

This verification harness provides:
✅ **Quote-only mode** for safe testing  
✅ **Enhanced diagnostics** for human review  
✅ **Hard safety controls** to prevent accidental transmission  
✅ **Comprehensive smoke tests** (15/15 passing)  
✅ **End-to-end integration** script  
✅ **No architecture changes** - pure verification layer  

The refactor is now **testable, verifiable, and hardened** against accidental live trading.
