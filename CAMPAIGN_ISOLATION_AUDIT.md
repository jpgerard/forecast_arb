# Campaign Grid Isolation Audit

**Issue:** Multi-underlier campaigns (SPY, QQQ) must maintain proper isolation to prevent:
1. Reusing snapshot for wrong underlier
2. Stale price feeds between cells
3. State leakage between underlier runs

**Date:** 2026-02-24

---

## Isolation Requirements

### 1. Snapshot Isolation

**Requirement:** Each underlier MUST have its own snapshot with its own price data.

**Validation:**

```python
# In run_campaign_mode() (scripts/daily.py):
snapshots = {}
for underlier in underliers:
    # Each underlier gets its OWN snapshot
    if snapshot_path and len(underliers) == 1:
        snapshots[underlier] = snapshot_path  # Only if single underlier
    else:
        # Fetch NEW snapshot for THIS underlier
        output_path = f"snapshots/{underlier}_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        exporter.export_snapshot(
            underlier=underlier,  # ← Correct underlier
            ...
        )
        snapshots[underlier] = output_path
```

✅ **Status:** Each underlier fetches its own snapshot

**Key Check:**
```python
# snapshots dict structure:
{
  "SPY": "snapshots/SPY_snapshot_20260224_143000.json",
  "QQQ": "snapshots/QQQ_snapshot_20260224_143010.json"  # Different file!
}
```

---

### 2. Grid Runner Snapshot Resolution

**Validation:**

```python
# In run_campaign_grid() (forecast_arb/campaign/grid_runner.py):
for underlier in underliers:
    # Check snapshot availability
    if underlier not in snapshots:
        logger.warning(f"⚠️  No snapshot for {underlier}, skipping")
        continue
    
    # Load THIS underlier's snapshot
    snapshot_path = snapshots[underlier]  # ← Gets correct path
    snapshot = load_snapshot(snapshot_path)  # ← Loads correct data
    metadata = get_snapshot_metadata(snapshot)
    
    logger.info(f"Processing {underlier}: spot=${metadata['current_price']:.2f}")
```

✅ **Status:** Each underlier loads its own snapshot from the `snapshots` dict

**Validation Check:**
- SPY processes with SPY snapshot → SPY spot price
- QQQ processes with QQQ snapshot → QQQ spot price
- No cross-contamination

---

### 3. run_regime() Call Isolation

**Potential Risk:** `run_regime()` from `scripts.run_daily_v2` might have global state.

**Validation:**

```python
# In grid_runner.py:
regime_result = run_regime(
    regime=regime_name,
    config=structuring_config,
    snapshot=snapshot,           # ← Correct snapshot for THIS underlier
    snapshot_path=snapshot_path, # ← Correct path for THIS underlier
    p_external=p_external,       # ← Can be underlier-specific
    min_debit_per_contract=min_debit_per_contract,
    run_id=cell_run_id  # ← Unique per cell: campaign_v1_abc123_SPY_crash
)
```

**State Analysis:**
```python
# run_regime() parameters are ALL passed in:
- Takes snapshot as argument (no global snapshot cache)
- Takes config as argument (no global config)
- Returns RegimeResult dataclass (stateless)
- No mutable global variables modified
```

✅ **Status:** `run_regime()` is functional (stateless)

---

### 4. Metadata Isolation

**Critical Check:** Ensure metadata comes from correct snapshot.

```python
# For SPY:
metadata = get_snapshot_metadata(spy_snapshot)
# metadata['underlier'] == 'SPY'
# metadata['current_price'] == SPY spot price

# For QQQ:
metadata = get_snapshot_metadata(qqq_snapshot)
# metadata['underlier'] == 'QQQ'
# metadata['current_price'] == QQQ spot price
```

✅ **Status:** Metadata extracted from correct snapshot per underlier

---

### 5. Cell ID Uniqueness

**Validation:**

```python
# Cell IDs are unique per (underlier × regime × bucket):
cell_id = f"{underlier}_{regime_name}_{bucket_name}"

# Examples:
# - SPY_crash_dte_30_60
# - QQQ_crash_dte_30_60
# - SPY_selloff_dte_30_60
# - QQQ_selloff_dte_30_60
```

✅ **Status:** Cell IDs prevent cross-contamination

---

### 6. Run ID Isolation

**Validation:**

```python
# Each cell gets unique run_id:
cell_run_id = f"{campaign_run_id}_{underlier}_{regime_name}"

# Examples:
# - campaign_v1_abc123_20260224T143000_SPY_crash
# - campaign_v1_abc123_20260224T143000_QQQ_crash
```

✅ **Status:** Run IDs are unique per cell

---

## Potential Failure Modes

### ❌ FAILURE MODE 1: Reusing Single Snapshot

**If this happened:**
```python
# WRONG:
snapshot = load_snapshot("SPY_snapshot.json")
for underlier in ["SPY", "QQQ"]:
    # Both use SPY snapshot!
    regime_result = run_regime(snapshot=snapshot, ...)  # ❌
```

**Why it won't happen:**
- `snapshots` dict is keyed by underlier
- Each underlier loads its own snapshot from dict
- Grid runner explicitly checks `if underlier not in snapshots`

---

### ❌ FAILURE MODE 2: Stale Price Feeds

**If this happened:**
```python
# WRONG:
current_price = 600.00  # Global variable
for underlier in ["SPY", "QQQ"]:
    # Both use stale price!
    event_spec = create_event_spec(spot=current_price)  # ❌
```

**Why it won't happen:**
- No global price variables
- `metadata = get_snapshot_metadata(snapshot)` extracts price from snapshot
- Metadata is local variable per underlier loop iteration

---

### ❌ FAILURE MODE 3: Strike Leakage

**If this happened:**
```python
# WRONG:
strikes_cache = {}  # Global
for underlier in ["SPY", "QQQ"]:
    strikes = strikes_cache.get(underlier, compute_strikes())
    # QQQ might get SPY strikes!
```

**Why it won't happen:**
- `run_regime()` calls structuring engine fresh each time
- Snapshot data includes strikes per underlier
- No global strike cache

---

## Test Validation

### Recommended Test: Multi-Underlier Isolation

```python
def test_multi_underlier_isolation():
    """Test that SPY and QQQ don't cross-contaminate."""
    
    # Mock snapshots with different spots
    spy_snapshot = {"metadata": {"current_price": 600.0, "underlier": "SPY"}, ...}
    qqq_snapshot = {"metadata": {"current_price": 450.0, "underlier": "QQQ"}, ...}
    
    snapshots = {
        "SPY": "spy_snapshot.json",
        "QQQ": "qqq_snapshot.json"
    }
    
    # Run grid
    candidates_flat = run_campaign_grid(...)
    
    # Validate SPY candidates use SPY prices
    spy_candidates = [c for c in candidates_flat if c["underlier"] == "SPY"]
    assert all(580 < c["long_strike"] < 620 for c in spy_candidates)  # SPY range
    
    # Validate QQQ candidates use QQQ prices
    qqq_candidates = [c for c in candidates_flat if c["underlier"] == "QQQ"]
    assert all(420 < c["long_strike"] < 480 for c in qqq_candidates)  # QQQ range
    
    # No overlap
    assert len(spy_candidates) > 0
    assert len(qqq_candidates) > 0
```

**Status:** ⚠️ Test not yet implemented (recommended for live validation)

---

## Runtime Validation Checks

### Add to grid_runner.py:

```python
# After loading snapshot for each underlier:
loaded_underlier = metadata.get('underlier')
if loaded_underlier != underlier:
    raise ValueError(
        f"ISOLATION VIOLATION: Expected {underlier} snapshot, "
        f"but loaded {loaded_underlier} snapshot! "
        f"Snapshot path: {snapshot_path}"
    )

logger.info(f"✓ Snapshot validation: {underlier} spot=${metadata['current_price']:.2f}")
```

**Status:** ⚠️ Not yet added (recommended runtime guard)

---

## Conclusion

### Current Status: ✅ ISOLATED

1. **Snapshot Isolation:** ✅ Each underlier fetches/loads own snapshot
2. **Price Isolation:** ✅ Metadata extracted per-snapshot
3. **Strike Isolation:** ✅ Structuring runs fresh per cell
4. **State Isolation:** ✅ `run_regime()` is stateless
5. **Cell ID Uniqueness:** ✅ Prevents mixups

### Recommended Enhancements:

1. **Add runtime validation** to grid_runner (check snapshot underlier matches expected)
2. **Add integration test** with SPY+QQQ to validate isolation
3. **Log snapshot metadata** per underlier to audit trail

### No Remediation Needed

The current implementation is properly isolated. The concerns are valid but already addressed by:
- Per-underlier snapshot fetching
- Dict-based snapshot routing
- Stateless `run_regime()` calls
- Local variable scoping

✅ **Campaign grid is safe for multi-underlier operation.**
