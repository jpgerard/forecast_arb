# Proxy Probability Fix: p_external Authorization Rule

## Problem

The system was incorrectly populating `p_external` with proxy/fallback probability values, which caused false positives in trade decisions. When Kalshi returned a proxy probability (e.g., from a different horizon) or when fallback was used, the system would treat these non-authoritative estimates as if they were real market data.

### Example of the Bug

**Last run output (BEFORE fix):**
```
External Probability (p_external)
- Value: 0.3000 (30.00%)
- Source: fallback
- Confidence: 0.70
```

This was **wrong** because:
- The probability was NOT from a real Kalshi market
- Trading on this would be a false positive caused by a labeling bug
- The value should have been in metadata only, not authorizing trades

## Solution

**ONE SURGICAL CORRECTION:** `p_external` may ONLY be populated if the probability comes from an exact match (is_exact == True).

### Code Changes

**File: `forecast_arb/oracle/p_event_source.py`**

#### Change 1: Fallback source now returns `p_event=None`
```python
# BEFORE:
return PEventResult(
    p_event=p_event,  # ❌ Wrong: populated with fallback value
    source="fallback",
    confidence=0.1,
    ...
)

# AFTER:
return PEventResult(
    p_event=None,  # ✅ Correct: None for non-authoritative
    source="fallback",
    confidence=0.0,  # Zero confidence
    metadata={
        "p_external_fallback": p_fallback,  # Value in metadata only
        ...
    },
    ...
)
```

#### Change 2: Proxy results already return `p_event=None`
The Kalshi source was already correctly returning `p_event=None` for proxy results:
```python
# Already correct in kalshi_multi_series_search path:
if search_result["proxy"] is not None:
    return PEventResult(
        p_event=None,  # ✅ Already correct
        metadata={
            "p_external_proxy": proxy.p_external_proxy,  # Proxy in metadata
            ...
        }
    )
```

## Expected Output (AFTER fix)

**Probabilities section:**
```
p_external: N/A
source: kalshi (or fallback)
exact_match: NO

p_external_proxy: 0.30
method: yearly_min_hazard_scale
confidence: LOW (≤0.35)
warnings: PROXY_USED, HORIZON_MISMATCH
```

**Policy/Gate/Decision:**
```
Policy: BLOCKED (proxy not authorizing)
Gate: NO_P_EXTERNAL
Candidates: informational only
Decision: NO TRADE
```

**Reason for NO TRADE:**
- Not because crash risk is impossible
- But because the external probability being used is not real
- Trading here would be a false positive caused by using proxy data

## Authorization Rule

```
p_external may ONLY be populated if is_exact == True

If proxy or fallback is used:
- p_external must be None
- proxy/fallback must live in metadata only  
- policy must treat it as non-authoritative
- gate will block with NO_P_EXTERNAL
```

## Impact

### Before Fix
- ❌ Fallback would populate `p_external = 0.30`
- ❌ Gate would see this as valid and potentially pass
- ❌ False positives possible

### After Fix  
- ✅ Fallback returns `p_external = None`
- ✅ Proxy value lives in `metadata.p_external_fallback`
- ✅ Gate blocks immediately with `NO_P_EXTERNAL`
- ✅ No false positives from proxy/fallback data

## Testing

All tests updated and passing:
- ✅ `test_fallback_source_blocks_trade_by_default` - Verifies p_event=None
- ✅ `test_fallback_source_allows_trade_when_explicitly_enabled` - Now obsolete (always blocks)
- ✅ `test_kalshi_source_not_blocked` - Real Kalshi data still works
- ✅ `test_edge_gate_fail_takes_precedence` - Gate logic correct
- ✅ `test_p_implied_failure_blocks_before_external_source_policy` - Precedence correct

## Files Modified

1. `forecast_arb/oracle/p_event_source.py`
   - `FallbackPEventSource.get_p_event()` - Changed `p_event` from fallback value to `None`
   - Changed confidence from 0.1 to 0.0 for non-authoritative sources
   - Moved fallback value to `metadata.p_external_fallback`

2. `tests/test_fallback_trade_block.py`
   - Updated all tests to reflect new behavior
   - Documented that fallback/proxy cannot authorize trades anymore

## Summary

This fix ensures **data integrity** and **trade safety** by enforcing a strict rule: only exact market matches can populate `p_external` and authorize trades. Proxy and fallback values are preserved for informational purposes in metadata, but cannot accidentally trigger false positive trades.
