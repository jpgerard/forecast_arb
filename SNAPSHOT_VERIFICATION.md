# Snapshot Strike Coverage Verification

## Current State (BEFORE Fix)

**Snapshot**: `SPY_snapshot_20260129_152322.json`
**Spot**: $691.75
**Mode**: Legacy (strikes_below=30, strikes_above=30)

### Coverage Analysis:
| Expiry | Min Strike | Min Moneyness | Status |
|--------|-----------|---------------|---------|
| 20260306 | $662.00 | 95.70% | ❌ TOO SHALLOW |
| 20260313 | $681.00 | 98.45% | ❌ TOO SHALLOW |
| 20260320 | $662.00 | 95.70% | ❌ TOO SHALLOW |
| 20260331 | $662.00 | 95.70% | ❌ TOO SHALLOW |

**Problem**: Min strikes only go down to ~96% of spot (4% below), but we need ~75% (25% below) to support -20% moneyness targets.

## Expected State (AFTER Fix)

**Code Change**: Updated `scripts/run_daily.py` to use `tail_moneyness_floor=0.25`

**Next snapshot should show**:
- Mode: `tail` (not `legacy`)
- `tail_metadata.tail_moneyness_floor`: 0.25
- `tail_metadata.tail_floor_strike`: ~$520 (for spot=$691.75, floor = 691.75 * 0.75 = 518.81)
- Min strikes for ALL expiries: ~$520-$550 range
- Min moneyness: ≤75% for all expiries

### Target Coverage for spot=$691.75:
- **-10% target**: $622.58 ✅ (should be included)
- **-15% target**: $588.00 ✅ (should be included)
- **-20% target**: $553.40 ✅ (should be included)
- **Floor (25%)**: $518.81 ✅ (should be at boundary)

## Verification Steps

### Step 1: Create New Snapshot
```powershell
# With IBKR connected, run:
python -m scripts.run_daily --mode dev --p-event-source fallback
```

### Step 2: Check Coverage
```powershell
python check_snapshot_coverage.py snapshots/SPY_snapshot_YYYYMMDD_HHMMSS.json
```

### Step 3: Verify Metadata
```powershell
python -c "import json; snap=json.load(open('snapshots/SPY_snapshot_LATEST.json')); tm=snap['snapshot_metadata']['tail_metadata']; print(f\"Mode: {tm.get('mode', 'N/A')}\"); print(f\"Tail floor: {tm.get('tail_floor_strike', 'N/A')}\"); print(f\"Moneyness floor: {tm.get('tail_moneyness_floor', 'N/A')}\"); print(f\"Incomplete: {tm.get('incomplete', 'N/A')}\")"
```

### Step 4: Engine Test
Run the engine with the new snapshot and verify no "No strikes below K_long=..." errors:

```powershell
python -m scripts.run_daily --snapshot snapshots/SPY_snapshot_LATEST.json --mode dev
```

**Success criteria**:
- ✅ Engine selects an expiry (e.g., 20260306)
- ✅ For that expiry, min strike ≤ spot * 0.85 (to cover  -15% target)
- ✅ No "No strikes below" filter messages
-  ✅ Candidates generated for -10%, -15%, -20% moneyness targets

## Quick Verification Script

Use the provided `check_snapshot_coverage.py`:

```powershell
# Check all recent snapshots
python check_snapshot_coverage.py snapshots/SPY_snapshot_20260129_152322.json
python check_snapshot_coverage.py snapshots/SPY_snapshot_NEXT.json
```

**Look for**:
- Tail metadata shows `mode`: `tail` (not `legacy`)
- All expiries show min moneyness ≤ 75%
- All expiries show "✅ GOOD" coverage status

## Troubleshooting

### If snapshot still shows TOO SHALLOW:
1. Check that `run_daily.py` was modified correctly
2. Verify you're creating a NEW snapshot (not reusing old one with --snapshot flag)
3. Check IBKR connection has access to deep OTM strikes

### If tail_metadata missing:
- Snapshot is still using legacy mode
- Check that `tail_moneyness_floor=0.25` is being passed to `export_snapshot()`

### If only some expiries have good coverage:
- This would indicate a bug in per-expiry strike selection
- File an issue - all expiries should use same tail_moneyness_floor parameter

---

**Last Updated**: 2026-01-29
**Related**: STRIKE_COVERAGE_ENHANCEMENT.md
