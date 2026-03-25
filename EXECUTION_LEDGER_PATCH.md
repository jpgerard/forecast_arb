# Execution Ledger Patch - Trade Event System

## Summary

Minimal patch to `execute_trade.py` and `outcome_ledger.py` to replace dangerous write_ledger_hook() with safer event-based logging system. This enforces cleaner semantics: only execute_trade writes to the ledger, daily.py only prints receipts.

## Changes Made

### 1. Added `append_trade_event()` to outcome_ledger.py

**Location**: `forecast_arb/execution/outcome_ledger.py`

**New Function**: `append_trade_event()`

Replaces the old `append_trade_open()` with a cleaner event-based system:

**Event Types**:
- `QUOTE_OK` - Quote-only check passed all guards
- `QUOTE_BLOCKED` - Quote-only check failed guards  
- `STAGED_PAPER` - Paper order staged (not transmitted)
- `SUBMITTED_LIVE` - Live order submitted to exchange
- `FILLED_OPEN` - Order filled, position now open

**Key Features**:
- Only `FILLED_OPEN` events represent actual open positions
- `FILLED_OPEN` requires full position data (expiry, strikes, qty, entry_price)
- Other events only need basic metadata (intent_id, order_id, timestamp)
- Writes to global `runs/trade_outcomes.jsonl` ledger

### 2. Removed/Disabled `write_ledger_hook()` in execute_trade.py

**Location**: `forecast_arb/execution/execute_trade.py`

**Status**: ~~Removed~~ Replaced with comment

```python
# write_ledger_hook() has been removed - dangerous and replaced by append_trade_event()
# Use append_trade_event() with appropriate event types:
# - QUOTE_OK / QUOTE_BLOCKED for quote-only
# - STAGED_PAPER for paper staging
# - SUBMITTED_LIVE for live transmission
# - FILLED_OPEN for confirmed fills
```

**Why Dangerous**: The old hook was writing to ledger even in quote-only mode with incorrect semantics.

### 3. Replaced `append_trade_open()` with `append_trade_event()`

**Location**: `forecast_arb/execution/execute_trade.py` - `execute_order_intent()` function

**Old Behavior**:
- Called `append_trade_open()` after order placement
- Wrote full OPEN record to ledger

**New Behavior**:
```python
# Live transmission with transmit=True
if transmit:
    if order_status == "Filled":
        append_trade_event(event="FILLED_OPEN", ...)  # Full position data
    elif order_status in ["Submitted", "PreSubmitted"]:
        append_trade_event(event="SUBMITTED_LIVE", ...)  # Basic data only
        
# Paper staging without transmit
elif not transmit and not quote_only:
    append_trade_event(event="STAGED_PAPER", ...)  # Basic data only
```

**Benefits**:
- Clear semantic distinction between event types
- Only `FILLED_OPEN` creates actual positions for tracking
- Intermediate events (SUBMITTED, STAGED) are logged but don't create positions
- Execute_trade is the **only** writer to outcome ledger

### 4. Added intent_id Validation

**Location**: `forecast_arb/execution/execute_trade.py` - `validate_order_intent()` function

**Change**:
```python
required_fields = [
    "strategy", "symbol", "expiry", "type", "legs",
    "qty", "limit", "tif", "guards", "intent_id"  # intent_id now mandatory
]

# Validate intent_id is not empty
if not intent["intent_id"] or not isinstance(intent["intent_id"], str):
    raise ValueError("OrderIntent intent_id must be a non-empty string")
```

**Enforcement**: If `intent_id` is missing from OrderIntent JSON → execution **BLOCKS** with error.

### 5. Updated daily.py - Print Only, No Writes

**Location**: `scripts/daily.py`

**Changes**:
```python
# OLD:
from forecast_arb.execution.outcome_ledger import append_trade_open

# NEW:
# NOTE: daily.py DOES NOT WRITE outcomes - only execute_trade.py writes events
# daily.py only prints receipts and paths for manual inspection
```

**Behavior**:
- daily.py no longer imports any ledger writing functions
- Only prints receipt with paths to ledger files
- execute_trade.py handles all ledger writing
- Ledger paths shown in receipt for manual inspection

## Usage Patterns

### Quote-Only Mode
```bash
python -m forecast_arb.execution.execute_trade \
  --intent intents/spy_20260320_590_570_crash.json \
  --paper \
  --quote-only
```
**Result**: No ledger events written (quote-only doesn't create events)

### Paper Staging
```bash
python -m forecast_arb.execution.execute_trade \
  --intent intents/spy_20260320_590_570_crash.json \
  --paper
```
**Result**: `STAGED_PAPER` event written to ledger

### Live Transmission
```bash
python -m forecast_arb.execution.execute_trade \
  --intent intents/spy_20260320_590_570_crash.json \
  --live \
  --transmit \
  --confirm SEND
```
**Result**: 
- `SUBMITTED_LIVE` event written immediately
- `FILLED_OPEN` event written when order fills (manual step, not automated yet)

## Ledger Semantics

### Only Open Positions from FILLED_OPEN

**Rule**: Only `FILLED_OPEN` events represent actual open positions in the ledger.

**Query Pattern**:
```python
# Read ledger
with open("runs/trade_outcomes.jsonl", "r") as f:
    for line in f:
        event = json.loads(line)
        
        # Only FILLED_OPEN events are actual positions
        if event["event"] == "FILLED_OPEN":
            print(f"Open position: {event['intent_id']}")
```

### Event Progression
```
1. [Optional] QUOTE_OK/QUOTE_BLOCKED during testing
2. STAGED_PAPER when paper order created
3. SUBMITTED_LIVE when live order sent to exchange  
4. FILLED_OPEN when order fills ← ONLY THIS CREATES POSITION
```

## Files Modified

1. **forecast_arb/execution/outcome_ledger.py**
   - Added `append_trade_event()` function
   - Validates event types
   - Enforces required fields for FILLED_OPEN

2. **forecast_arb/execution/execute_trade.py**
   - Removed `write_ledger_hook()` function
   - Replaced `append_trade_open()` with `append_trade_event()` calls
   - Added `intent_id` validation
   - Uses correct event types based on order status

3. **scripts/daily.py**
   - Removed import of `append_trade_open`
   - Added comment explaining daily.py doesn't write to ledger
   - Only prints receipt with ledger paths

## Testing Recommendations

```bash
# 1. Test intent_id validation (should block)
python -m forecast_arb.execution.execute_trade \
  --intent intents/intent_without_id.json \
  --paper \
  --quote-only
# Expected: ValueError: OrderIntent missing required field: intent_id

# 2. Test paper staging writes STAGED_PAPER event
python -m forecast_arb.execution.execute_trade \
  --intent intents/spy_20260320_590_570_crash.json \
  --paper
# Check: runs/trade_outcomes.jsonl contains event="STAGED_PAPER"

# 3. Verify daily.py doesn't write to ledger
python scripts/daily.py --auto-regime crash --auto-rank 1
# Check: Only execute_trade.py writes to ledger, daily.py only prints
```

## Inspection Commands

```bash
# View all trade events
cat runs/trade_outcomes.jsonl | jq '.'

# View only FILLED_OPEN events (actual positions)
cat runs/trade_outcomes.jsonl | jq 'select(.event == "FILLED_OPEN")'

# Count events by type
cat runs/trade_outcomes.jsonl | jq -r '.event' | sort | uniq -c

# Find events for specific intent_id
cat runs/trade_outcomes.jsonl | jq 'select(.intent_id == "abc123...")'
```

## Migration Notes

**Breaking Change**: Old code that calls `append_trade_open()` directly will need updates.

**Fix**: Replace with `append_trade_event()`:
```python
# OLD:
append_trade_open(
    run_dir=run_dir,
    candidate_id="abc",
    run_id="run123",
    regime="crash",
    entry_ts_utc=ts,
    entry_price=50.0,
    qty=1,
    expiry="20260320",
    long_strike=590,
    short_strike=570,
    intent_id="intent123",
    order_id="12345",
    also_global=True
)

# NEW:
append_trade_event(
    event="FILLED_OPEN",  # Specify event type
    intent_id="intent123",
    candidate_id="abc",
    run_id="run123",
    regime="crash",
    timestamp_utc=ts,
    order_id="12345",
    expiry="20260320",
    long_strike=590,
    short_strike=570,
    qty=1,
    entry_price=50.0,
    also_global=True
)
```

## Completion Checklist

- [x] Added `append_trade_event()` function to outcome_ledger.py
- [x] Removed/disabled `write_ledger_hook()` in execute_trade.py
- [x] Replaced `append_trade_open()` with `append_trade_event()` calls
- [x] Added mandatory `intent_id` validation in execute_trade.py
- [x] Updated daily.py to only print, not write to ledger
- [x] Created documentation

## Summary

Execute_trade.py is now the **single writer** to the trade outcomes ledger. It uses semantic event types (QUOTE_OK, STAGED_PAPER, SUBMITTED_LIVE, FILLED_OPEN) instead of the old dangerous write_ledger_hook(). Only FILLED_OPEN events represent actual open positions. Daily.py prints receipts showing where to inspect the ledger but never writes to it.
