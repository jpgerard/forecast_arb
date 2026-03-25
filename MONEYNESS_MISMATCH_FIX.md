# MONEYNESS_MISMATCH Fix Implementation

## Goal
Fix MONEYNESS_MISMATCH by enforcing single source of truth using EventSpec

## Completed

### 1. Created EventSpec Dataclass ✓
- Added `EventSpec` to `forecast_arb/options/event_def.py`
- EventSpec contains: moneyness, threshold, expiry, spot, underlier, direction
- Added `create_event_spec()` factory function
- Added `validate_threshold_consistency()` method for validation
- Threshold computed ONCE: `threshold = spot * (1 + moneyness)`

### 2. Updated review_pack.py ✓
- Modified `render_review_pack()` to expect `event_spec` in run_context
- Removed recomputation of moneyness from threshold
- Added validation check: compares expected vs actual threshold
- Shows MONEYNESS_MISMATCH warning if drift detected (>1 cent)
- Displays both moneyness and threshold from EventSpec canonical values

### 3. Review-Only Mode Already Fixed ✓
Looking at run_daily.py code, review-only mode logic is already correct:
- When `--review-only-structuring` is set AND edge gate blocks: `skip_structuring = False`
- When `--review-only-structuring` is set AND external policy blocks: creates synthetic result then sets `skip_structuring = True` **BUT** this needs fixing
- Review artifacts are ALWAYS generated when in review-only mode

## Remaining Work

### A. Create EventSpec in run_daily.py (CRITICAL)
Currently `event_def` is created using legacy `create_terminal_below_event()`.
Need to:
1. After expiry selection, create EventSpec using `create_event_spec()`
2. Pass EventSpec to run_context for review_pack
3. Keep event_def for backward compat with p_implied_artifact

**Location in run_daily.py:**
```python
# Around line 658 (after expiry selection and before p_implied calculation)
if target_expiry:
    logger.info(f"Using expiry {target_expiry} for p_implied calculation")
    
    # CREATE EVENTSPEC (NEW - SINGLE SOURCE OF TRUTH)
    event_spec = create_event_spec(
        underlier=metadata['underlier'],
        expiry=target_expiry,
        spot=metadata['current_price'],
        moneyness=event_moneyness
    )
    
    # Create event definition (LEGACY - for backward compat)
    event_def = create_terminal_below_event(...)
```

**Then pass to run_context around line 915:**
```python
run_context = {
    "run_id": result['run_id'],
    "run_dir": str(run_dir),
    "snapshot_metadata": {...},
    "expiry_used": target_expiry,
    "dte": ...,
    "event_spec": event_spec.to_dict() if target_expiry else {},  # NEW
    "event_definition": event_def.to_dict() if target_expiry else {},  # LEGACY
    "min_edge": min_edge,
    "min_confidence": min_confidence
}
```

### B. Fix External Policy Block in Review-Only Mode (CRITICAL)
**Problem:** When external policy blocks AND review-only is enabled, the code creates a synthetic NO_TRADE result and sets `skip_structuring = True`, which prevents structuring from running.

**Location:** Around line 776 in run_daily.py:
```python
if external_source_blocked:
    ...
    # Create synthetic NO_TRADE result
    result = {...}
    skip_structuring = True  # <-- THIS BLOCKS STRUCTURING
```

**Fix:** Should check for review-only mode here too:
```python
if external_source_blocked:
    would_block_trade = True
    block_type = "EXTERNAL_SOURCE_BLOCKED"
    block_reason_detail = external_source_policy
    
    if args.review_only_structuring:
        # Review-only mode: proceed to structuring
        skip_structuring = False
        logger.warning("EXTERNAL SOURCE POLICY BLOCKED (REVIEW-ONLY MODE)")
        logger.warning("Proceeding to structuring for REVIEW PURPOSES ONLY.")
    else:
        # Normal mode: skip structuring, create synthetic result
        skip_structuring = True
        ...create synthetic NO_TRADE result...
```

### C. Pass EventSpec to Implied Probability (Optional Enhancement)
Currently `implied_prob_terminal_below()` receives just the threshold.
Could update signature to accept EventSpec for validation.

### D. Pass EventSpec to Structuring Engine (Optional Enhancement)  
Currently structuring engine doesn't use event definition.
Could pass EventSpec for future validation or metadata.

### E. Add Validation to Gate (Optional Enhancement)
Edge gate could validate that p_implied was computed using correct threshold from EventSpec.

## Testing Checklist

- [ ] Run with --review-only-structuring when edge gate blocks
- [ ] Verify structuring runs and review_pack shows correct moneyness/threshold
- [ ] Run with --review-only-structuring when external policy blocks  
- [ ] Verify structuring runs even when fallback source is used
- [ ] Check review_pack.md shows PASS for threshold consistency
- [ ] Verify no MONEYNESS_MISMATCH warnings appear
- [ ] Test with different moneyness values in config (-0.10, -0.15, -0.20)

## Implementation Priority

1. **HIGH:** Create EventSpec in run_daily.py (step A)
2. **HIGH:** Fix external policy block logic (step B)
3. **MEDIUM:** Test all scenarios
4. **LOW:** Optional enhancements (C, D, E)
