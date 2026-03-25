# Probability Consistency Fix - Campaign Mode

## Critical Bug Identified

**Symptom:** Campaign mode was showing conflicting probability values:
- Table showed: `P_USED = 0.052` with `EV/$ = 49.54`
- Sensitivity showed: `Base case (P=0.30): EV/$ = 15.67`

These cannot both be true. The sensitivity was using a hardcoded default instead of the actual probability used in EV calculation.

## Root Cause

The `print_ev_sensitivity()` function had a hardcoded default:
```python
base_p = candidate.get("assumed_p_event", 0.30)  # BAD: hardcoded fallback
```

This meant:
1. Campaign candidates use `p_event_used` field (not `assumed_p_event`)
2. If that field was missing, it fell back to 0.30
3. The table showed one probability, sensitivity showed another

## Fix Applied

### 1. EV Sensitivity Now Uses Actual p_event_used

Updated `print_ev_sensitivity()` in `scripts/daily.py`:

```python
# BEFORE (BUG):
base_p = candidate.get("assumed_p_event", 0.30)  # Hardcoded default!

# AFTER (FIXED):
base_p = candidate.get("p_event_used") or candidate.get("assumed_p_event")
if base_p is None:
    print("⚠️  Cannot compute sensitivity: p_event_used not available")
    return
```

**Contract:**
- Check `p_event_used` first (campaign mode)
- Fall back to `assumed_p_event` (single-regime mode)
- If both missing, display error and skip sensitivity (don't invent numbers)

### 2. Single Source of Truth

**All displays now use the same probability:**
- Table `P_USED` column → `p_event_used`
- Detail block `P_used=X` → `p_event_used`  
- Sensitivity `Base case (P=X)` → `p_event_used`
- EV calculation → used `p_event_used` when candidate was created

### 3. Verified Data Flow

**Campaign Mode:**
```
grid_runner.flatten_candidate()
  → extracts candidate.get("assumed_p_event")
  → stores as flat_candidate["p_event_used"]
  →selector preserves in recommended.json
  → daily.py displays in table
  → daily.py uses in sensitivity
```

**Single-Regime Mode:**
```
candidate has "assumed_p_event"
  → daily.py checks p_event_used OR assumed_p_event
  → uses whichever exists
  → displays consistently
```

## Expected Output After Fix

When you re-run campaign mode with the same data, you should see:

```
P_USED = 0.052  (matches what was used in EV calculation)

... [later in output] ...

EV SENSITIVITY ANALYSIS
Base case (P=0.052):    EV = $XXXX | EV/$ = 49.54  (matches table!)
Lower bound (P=0.002):  EV = $YYYY | EV/$ = ...
Upper bound (P=0.102):  EV = $ZZZZ | EV/$ = ...
```

The base case EV/$ should now **match** the table EV/$.

## Testing Required

1. **Re-run the exact same snapshot:**
   ```powershell
   python scripts/daily.py --campaign configs/campaign_v1.yaml --snapshot <same_snapshot>
   ```

2. **Verify consistency:**
   - Table EV/$ = Sensitivity base case EV/$
   - Table P_USED = Sensitivity base case P value
   - No more "0.30" appearing when P_USED shows 0.052

3. **Check both modes:**
   - Campaign mode: Uses `p_event_used`
   - Single-regime mode: Uses `assumed_p_event`

## Additional Safeguards

- If `p_event_used`/`assumed_p_event` both missing → Skip sensitivity, don't crash
- Never use hardcoded probability defaults in display code
- All financial calculations should use same probability source

## Files Modified

1. `scripts/daily.py` - Fixed `print_ev_sensitivity()` to use actual `p_event_used`

## Status

✅ **CRITICAL FIX APPLIED** - No more probability mismatch  
⚠️  **REQUIRES TESTING** - Re-run campaign mode to verify consistency  
🔒 **NO LIVE TRADES** - Until consistency verified with test run  

---

**Fixed:** 2026-02-26  
**Bug Severity:** Critical (data inconsistency)  
**Breaking Change:** None (only fixes display consistency)  
**User Action:** Re-test campaign mode before live trading
