# Ledger Semantics Surgical Fixes

**Date:** February 10, 2026  
**Status:** ✅ Complete & Tested

## Summary

Three minimal, surgical fixes to tighten ledger semantics without requiring a refactor:

### A) Enforce Single OPEN per Intent/Order

**Rule:** One OPEN entry only when order is confirmed Filled (or at least Submitted).

**Before:**
- Multiple OPEN entries could be written for the same intent
- No deduplication logic
- Confusing ledger state

**After:**
- `append_trade_open()` checks for existing OPEN with same `intent_id`
- Raises `ValueError` with clear message if duplicate detected
- Suggests using `INTENT_STAGED` or `ORDER_SUBMITTED` states instead

**Implementation:** `forecast_arb/execution/outcome_ledger.py`
- Checks both run_dir and global ledgers before writing
- Blocks duplicate OPEN entries at write time

---

### B) Add Mandatory Fields to trade_outcomes.jsonl

**Rule:** Two new mandatory fields going forward:
- `intent_id` - Unique identifier for the OrderIntent
- `order_id` - IBKR order ID (can be None if not yet assigned)

**Before:**
```json
{
  "candidate_id": "16f487b8b24e",
  "run_id": "",
  "regime": "crash",
  "entry_ts_utc": "2026-02-10T15:24:47.547712+00:00",
  "entry_price": 0.34,
  "qty": 1,
  "expiry": "20260320",
  "long_strike": 590.0,
  "short_strike": 570.0,
  "status": "OPEN"
}
```

**After:**
```json
{
  "candidate_id": "16f487b8b24e",
  "run_id": "test_run",
  "regime": "crash",
  "entry_ts_utc": "2026-02-10T15:24:47.547712+00:00",
  "entry_price": 0.34,
  "qty": 1,
  "expiry": "20260320",
  "long_strike": 590.0,
  "short_strike": 570.0,
  "intent_id": "16f487b8b24e_order_intent",
  "order_id": "12345",
  "status": "OPEN"
}
```

**Implementation:**
- `forecast_arb/execution/outcome_ledger.py` - Added mandatory parameters
- `forecast_arb/execution/execute_trade.py` - Passes intent_id and order_id

**Intent ID Generation:**
- Format: `{candidate_id}_{intent_file_stem}`
- Example: `16f487b8b24e_order_intent`
- Unique per intent file

---

### C) Make Expiry Single-Source-of-Truth from IBKR Contract

**Rule:** Ledger must take expiry from resolved IBKR contract, not candidate file.

**Before:**
- Expiry could come from candidate file
- Potential mismatch with IBKR-resolved contract
- No validation

**After:**
- `execute_trade.py` extracts `resolved_expiry` from qualified IBKR contracts
- `enforce_intent_immutability()` validates intent expiry matches IBKR
- If mismatch → **BLOCKS** execution with clear error:
  ```
  ❌ FIX C VIOLATION: Expiry mismatch!
  Intent expiry 20260321 != IBKR resolved 20260320.
  Ledger must take expiry from resolved IBKR contract, not candidate file.
  BLOCKING EXECUTION.
  ```
- Ledger receives `resolved_expiry` from IBKR as single source of truth

**Implementation:** `forecast_arb/execution/execute_trade.py`
```python
# Extract resolved expiry from IBKR (single source of truth)
resolved_expiry = qualified_legs[0][0].lastTradeDateOrContractMonth
resolved_strikes = [leg[0].strike for leg in qualified_legs]

# Enforce expiry immutability
enforce_intent_immutability(intent, resolved_expiry, resolved_strikes)

# Pass IBKR-resolved expiry to ledger
append_trade_open(
    ...
    expiry=resolved_expiry,  # From IBKR, not candidate
    ...
)
```

---

## Files Modified

1. **forecast_arb/execution/outcome_ledger.py**
   - Added `intent_id` and `order_id` mandatory parameters
   - Added deduplication check for FIX A
   - Updated docstring with FIX A, B, C notes

2. **forecast_arb/execution/execute_trade.py**
   - Extract `resolved_expiry` from IBKR qualified contracts
   - Validate expiry match in `enforce_intent_immutability()`
   - Generate `intent_id` from candidate_id + intent filename
   - Pass `intent_id` and `order_id` to `append_trade_open()`
   - Use `resolved_expiry` (not intent expiry) for ledger

---

## Testing

**Test File:** `test_ledger_semantics_fix.py`

**Results:**
```
✅ PASS: FIX A: Single OPEN per intent/order
✅ PASS: FIX B: Mandatory intent_id and order_id
✅ PASS: FIX C: Expiry single-source-of-truth

🎉 ALL TESTS PASSED - LEDGER SEMANTICS FIXES VERIFIED
```

**Test Coverage:**
- FIX A: Verifies duplicate OPEN is blocked, different intent_id allowed
- FIX B: Verifies intent_id and order_id persisted correctly
- FIX C: Verifies expiry from IBKR contract is used

---

## Migration Notes

**Existing Data:**
- Old entries in `runs/trade_outcomes.jsonl` lack `intent_id` and `order_id`
- These will have `null` values for backward compatibility
- Code handles gracefully via `.get()` accessors

**Forward Compatibility:**
- New entries MUST include `intent_id` and `order_id`
- `append_trade_open()` enforces this via required parameters

**No Breaking Changes:**
- Existing code reading ledger uses `.get("intent_id")` - returns None for old entries
- New code benefits from guaranteed presence of these fields

---

## Benefits

1. **Clean Ledger State**
   - No duplicate OPEN entries
   - One-to-one mapping: intent → OPEN → order
   - Clear audit trail

2. **Traceability**
   - `intent_id` links back to OrderIntent file
   - `order_id` links to IBKR order
   - Full lineage: candidate → intent → order → fill

3. **Data Integrity**
   - Expiry guaranteed from IBKR (source of truth)
   - No silent mismatches
   - Fail-fast validation

4. **Minimal Changes**
   - Surgical fixes, no refactor needed
   - Backward compatible
   - Tested and verified

---

## Usage Example

```python
from forecast_arb.execution.outcome_ledger import append_trade_open

# Write OPEN entry with new mandatory fields
append_trade_open(
    run_dir=Path("runs/crash_venture_v2/run_123"),
    candidate_id="abc123",
    run_id="run_123",
    regime="crash",
    entry_ts_utc="2026-02-10T15:00:00Z",
    entry_price=0.35,
    qty=1,
    expiry="20260320",  # From IBKR contract
    long_strike=590.0,
    short_strike=570.0,
    intent_id="abc123_order_intent",  # NEW: Mandatory
    order_id="12345",                  # NEW: Mandatory (or None)
    also_global=True
)
```

**Deduplication:**
```python
# First call succeeds
append_trade_open(..., intent_id="intent_001", ...)

# Second call with same intent_id raises ValueError
append_trade_open(..., intent_id="intent_001", ...)
# ValueError: LEDGER VIOLATION: OPEN entry already exists for intent_id=intent_001
```

**Expiry Validation:**
```python
# In execute_trade.py
resolved_expiry = qualified_legs[0][0].lastTradeDateOrContractMonth

# This blocks if intent expiry != IBKR expiry
enforce_intent_immutability(intent, resolved_expiry, resolved_strikes)
```

---

## Next Steps

1. ✅ Code changes complete
2. ✅ Tests passing
3. ⏭️ Monitor first live execution with new semantics
4. ⏭️ Consider adding `INTENT_STAGED` and `ORDER_SUBMITTED` states for richer workflow tracking

---

## Reference

**Task:** Minimal surgical fixes to tighten ledger semantics  
**Approach:** No refactor, just enforce stricter rules  
**Result:** Clean, traceable, validated ledger entries
