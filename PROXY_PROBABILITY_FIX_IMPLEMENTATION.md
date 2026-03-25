# Proxy Probability Fix - Implementation Status

## ✅ IMPLEMENTATION COMPLETE

This document describes the implementation of the critical safety fix to prevent non-exact p_event promotion.

## Changes Made

### 1. scripts/run_daily.py - Critical Safety Guards

Added explicit safety checks in the Kalshi auto-mapping flow:

```python
# CRITICAL SAFETY CHECK: Classify response
has_exact = (p_event_result.p_event is not None)
has_proxy = ("p_external_proxy" in p_event_result.metadata)

# INVARIANT: p_external is set ONLY when is_exact == True
logger.info("P_EVENT_CLASSIFICATION")
logger.info(f"  exact_match: {'YES' if has_exact else 'NO'}")
logger.info(f"  proxy_present: {'YES' if has_proxy else 'NO'}")
logger.info(f"  p_external_authoritative: {'YES' if has_exact else 'NO'}")
```

**Key Enforcement Points:**

1. **Exact Match Path:**
   - Uses `p_event_result.p_event` as authoritative
   - Assertion: `assert p_event is not None`
   - Logs as `EXACT MATCH: p_external={value} (authoritative)`

2. **Proxy Path:**
   - NEVER uses proxy value as p_external
   - Falls back to `args.fallback_p`
   - Assertion: `assert p_event != proxy_value`
   - Logs clear WARNING about proxy not being used
   - Stores proxy in metadata for review only

3. **Safety Assertion:**
   ```python
   # SAFETY ASSERTION: Verify p_external is None or came from exact match
   if not has_exact and p_event_source_actual == "kalshi":
       assert p_event == args.fallback_p, \
           f"INVARIANT VIOLATION: No exact Kalshi match but p_event={p_event} != fallback={args.fallback_p}"
   ```

### 2. Enhanced Logging

**When proxy is detected:**
```
⚠️  PROXY PROBABILITY DETECTED (NOT EXACT MATCH)
  Proxy value: 0.420
  Proxy method: yearly_min_hazard_scale
  Proxy series: KXINXMINY
  Proxy confidence: 0.35 (LOW)
  
  ⚠️  POLICY: Proxy NOT used as authoritative p_external
  ⚠️  Falling back to fallback p_event for safety

Using fallback p_event: 0.300 (source: fallback, NOT proxy)
```

**Summary section:**
```
P_EVENT_SOURCE_SUMMARY
  Mode: kalshi-auto
  Source: fallback
  Exact Match: NO
  Proxy Present: YES
  p_external Value: 0.300 (authoritative: NO)
  Confidence: 0.00
  
  PROXY DETAILS (informational only, NOT authoritative):
    Proxy value: 0.420
    Proxy confidence: 0.35 (LOW)
    Proxy method: yearly_min_hazard_scale
    ⚠️  WARNING: Proxy NOT used for p_external
```

### 3. Review Pack Integration

The `forecast_arb/review/review_pack.py` already surfaces proxy information in the review pack when available:

```markdown
#### ⚠️ Proxy Probability Available (NOT exact match)
- **Proxy Value:** 0.4200 (42.00%)
- **Method:** yearly_min_hazard_scale
- **Series:** KXINXMINY
- **Horizon:** 45 days
- **Market Ticker:** KXINXMINY-26FEB-T4800
- **Confidence:** 0.35 (LOW)

**⚠️ IMPORTANT:** This is a proxy probability, NOT an exact Kalshi market match.
It is estimated using yearly minimum data with hazard rate scaling.
Use with extreme caution and heavy discounting. Not recommended for automated trading.
```

## Test Coverage

Created `tests/test_proxy_prevention.py` with comprehensive tests:

### Test 1: Proxy Never Becomes p_external
```python
def test_proxy_never_becomes_p_external():
    """
    CRITICAL SAFETY TEST: Proxy values must never be promoted to p_external.
    """
    # Creates a PEventResult with proxy
    # Verifies p_event is None (not proxy value)
    # Simulates run_daily.py logic
    # Asserts fallback is used instead of proxy
```

### Test 2: Exact Match Becomes p_external  
```python
def test_exact_match_becomes_p_external():
    """
    Test that exact matches ARE used as p_external (positive case).
    """
    # Creates a PEventResult with exact match
    # Verifies p_event is set correctly
    # No proxy in metadata
```

### Test 3: Classification Logging
```python
def test_classification_logging():
    """
    Test that the classification logic produces correct flags.
    """
    # Tests exact match: has_exact=True, has_proxy=False
    # Tests proxy only: has_exact=False, has_proxy=True
    # Tests no match: has_exact=False, has_proxy=False
```

## Expected Behavior

### Scenario: Kalshi Returns Proxy (No Exact Match)

**Input:**
- Event: SPX < 5800 at 2026-02-28
- Kalshi search: No exact match
- Proxy available: KXINXMINY-26FEB-T5800 (yearly min) → 0.42

**Output:**
```json
{
  "p_external": null,
  "source": "fallback",
  "exact_match": false,
  "proxy_metadata": {
    "p_external_proxy": 0.42,
    "proxy_method": "yearly_min_hazard_scale",
    "proxy_series": "KXINXMINY",
    "proxy_confidence": 0.35
  }
}
```

**Policy Decision:**
- External source policy: `BLOCKED_FALLBACK` (unless `--allow-fallback-trade`)
- Edge gate: May also block due to low confidence
- Final decision: `NO_TRADE`
- Reason: `EXTERNAL_SOURCE_BLOCKED:BLOCKED_FALLBACK`

**Review Pack:**
- Proxy value shown for informational purposes
- Clear warnings that it's NOT authoritative
- Candidates available for manual review if desired

## Key Invariants

The fix enforces these invariants at multiple layers:

1. **Source Layer** (`p_event_source.py`):
   - Returns `p_event=None` when proxy
   - Proxy value in `metadata.p_external_proxy`

2. **Integration Layer** (`run_daily.py`):
   - Explicit classification: exact vs proxy vs none
   - Assertions prevent proxy from becoming p_external
   - Falls back when no exact match

3 **Policy Layer**:
   - Fallback source triggers `BLOCKED_FALLBACK` policy
   - Unless explicitly allowed with `--allow-fallback-trade`

4. **Gate Layer**:
   - Low confidence (0.0) fails edge gate
   - min_confidence threshold typically 0.60

5. **Review Layer**:
   - Proxy surfaced as informational only
   - Clear warnings in review pack

## Defense in Depth

Multiple independent barriers prevent proxy from affecting automated trading:

```
┌─────────────────────────────────────────┐
│ Layer 1: p_event_source.py             │
│ → Returns p_event=None for proxy       │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ Layer 2: run_daily.py                   │
│ → Assertions enforce invariants         │
│ → Falls back to fallback p_event        │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ Layer 3: External Source Policy         │
│ → BLOCKED_FALLBACK (unless explicit OK) │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ Layer 4: Edge Gate                      │
│ → Low confidence (0.0) fails gate       │
└─────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────┐
│ Result: NO_TRADE                        │
│ → Proxy preserved for human review only │
└─────────────────────────────────────────┘
```

## Files Modified

1. **scripts/run_daily.py**
   - Added P_EVENT_CLASSIFICATION logging
   - Added safety assertions
   - Enhanced proxy detection and handling
   - Explicit fallback when proxy detected

2. **tests/test_proxy_prevention.py** (NEW)
   - Comprehensive test coverage
   - Critical safety tests

3. **PROXY_PROBABILITY_FIX_IMPLEMENTATION.md** (THIS FILE)
   - Documentation of implementation

## Running Tests

```powershell
# Run the proxy prevention test
pytest tests/test_proxy_prevention.py -v

# Expected output:
# tests/test_proxy_prevention.py::test_proxy_never_becomes_p_external PASSED
# tests/test_proxy_prevention.py::test_exact_match_becomes_p_external PASSED
# tests/test_proxy_prevention.py::test_classification_logging PASSED
```

## Summary

✅ **Fix implemented and tested**  
✅ **Multiple safety layers enforced**  
✅ **Proxy values never become authoritative**  
✅ **Clear logging and warnings**  
✅ **Preserved for review purposes only**  

The system now has **defense in depth** to prevent non-exact p_event promotion, ensuring that only exact Kalshi market matches can authorize automated trading.
