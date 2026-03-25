# IBKR Snapshot Strike Coverage Enhancement

## Overview
Enhanced IBKR snapshot acquisition to include deeper OTM strikes for crash venture structures, eliminating "No strikes below K_long=..." failures for normal SPY levels.

## Problem
Previous snapshot acquisition used limited strike coverage (30 strikes below/above spot), which was insufficient for crash venture strategies targeting deep OTM puts at -15% to -20% moneyness. This caused NO_TRADE failures with the reason "INSUFFICIENT_STRIKE_COVERAGE" or "No strikes below K_long=..." messages.

## Solution
Updated `scripts/run_daily.py` to use `tail_moneyness_floor=0.25` parameter when creating snapshots, ensuring coverage down to 25% below spot price. This provides adequate strike availability for:
- Current config: -10%, -15%, -20% moneyness targets  
- Future expansion: up to -25% moneyness if needed

## Changes Made

### 1. Updated scripts/run_daily.py
Changed snapshot acquisition from:
```python
exporter.export_snapshot(
    underlier=underlier,
    snapshot_time_utc=snapshot_time,
    dte_min=dte_min,
    dte_max=dte_max,
    strikes_below=30,  # OLD: Limited coverage
    strikes_above=30,
    out_path=output_path
)
```

To:
```python
exporter.export_snapshot(
    underlier=underlier,
    snapshot_time_utc=snapshot_time,
    dte_min=dte_min,
    dte_max=dte_max,
    tail_moneyness_floor=0.25,  # NEW: 25% below spot for deep OTM coverage
    out_path=output_path
)
```

### 2. Existing Infrastructure (Already in Place)
The snapshot exporter (`forecast_arb/ibkr/snapshot.py`) already supports tail mode strike selection with:
- `tail_moneyness_floor`: Coverage parameter (e.g., 0.25 = strikes down to 75% of spot)
- Metadata tracking: Logs incomplete coverage warnings
- Validation: Ensures ATM strikes exist within $5 of spot

## Strike Coverage Details

### For SPY ~$690:
- **Old coverage (strikes_below=30)**: ~$540-$690 range (150 points, ~22% below)
- **New coverage (tail_moneyness_floor=0.25)**: ~$517-$690 range (173 points, 25% below)

### Moneyness Coverage:
| Target Moneyness | Strike at SPY=$690 | Coverage Status |
|------------------|-------------------|-----------------|
| -10% | $621 | ✅ Fully covered |
| -15% | $586.50 | ✅ Fully covered |
| -20% | $552 | ✅ Fully covered |
| -25% | $517.50 | ✅ At boundary |

## Testing

### Existing Test Suite
`tests/test_snapshot_strike_depth.py` verifies:
- ✅ Tail mode provides deep OTM coverage (-20% and deeper)
- ✅ Incomplete coverage is properly flagged
- ✅ Explicit min_strike parameter works
- ✅ Legacy mode improved defaults (60 strikes below)
- ✅ Coverage for all crash venture moneyness targets

### Run Tests:
```powershell
python -m pytest tests/test_snapshot_strike_depth.py -v
```

## Acceptance Criteria

### ✅ 1. Snapshot includes sufficiently deep OTM strikes
- New snapshots include strikes down to spot * 0.75 (25% below)
- Covers all moneyness targets: -10%, -15%, -20%

### ✅ 2. No "No strikes below K_long=..." failures for normal SPY levels  
- For SPY in range $600-$800, all moneyness targets have adequate strikes
- Snapshots include strikes at least down to SPY * 0.85 = -15% target requirement

### ✅ 3. Deterministic and testable
- Strike selection is deterministic (sorted arrays)
- Test suite verifies coverage for various scenarios
- Metadata tracks coverage completeness

### ✅ 4. Runtime reasonable with guardrails
- Tail mode filters strikes in range [floor, spot+buffer]
- Limits total strikes fetched (typically <100 strikes with buffer)
- Logging tracks strike counts and coverage

### ✅ 5. Metadata includes strike coverage info
- Snapshot JSON includes `tail_metadata` with:
  - `tail_floor_strike`: Computed minimum strike
  - `tail_moneyness_floor`: Coverage parameter (0.25)
  - `incomplete`: Boolean flag for coverage validation
  - `actual_floor`: Actual minimum strike fetched

## Configuration

### Current Campaign Config
`configs/structuring_crash_venture_v1.yaml`:
```yaml
moneyness_targets:
  - -0.10  # 10% OTM
  - -0.15  # 15% OTM
  - -0.20  # 20% OTM
```

### Snapshot Parameters (in run_daily.py)
```python
tail_moneyness_floor=0.25  # 25% below spot
# This ensures coverage for -20% targets with buffer
```

## Impact

### Before Enhancement
- ❌ NO_TRADE with "No strikes below K_long=586.50" for -15% target
- ❌ Limited to ~22% below spot (strikes_below=30)
- ❌ Frequent coverage gaps for deep OTM structures

### After Enhancement
- ✅ Full coverage for all config targets (-10%, -15%, -20%)
- ✅ 25% below spot coverage (tail_moneyness_floor=0.25)
- ✅ Robust strike availability for crash venture strategies
- ✅ Clean metadata tracking and logging

## Best Practices

1. **Default to tail mode**: Use `tail_moneyness_floor` instead of `strikes_below/above` for crash strategies
2. **Monitor metadata**: Check `snapshot_metadata.tail_metadata.incomplete` flag
3. **Adjust if needed**: Can increase to 0.30 (30% below) for even deeper coverage
4. **Validate snapshots**: Always check min/max strikes in snapshot metadata

## Related Files
- `forecast_arb/ibkr/snapshot.py` - Strike filtering logic
- `scripts/run_daily.py` - Snapshot acquisition (UPDATED)
- `tests/test_snapshot_strike_depth.py` - Test coverage
- `forecast_arb/engine/crash_venture_v1_snapshot.py` - Uses filtered strikes

## Future Enhancements
If needed, could add config-driven strike parameters:
```yaml
# Future config option
strike_coverage:
  min_put_moneyness: 0.75  # Strikes down to 75% of spot
  max_call_moneyness: 1.10  # Strikes up to 110% of spot
  min_strikes_each_side: 120
  max_total_strikes: 400
```

This would enable per-campaign customization of strike coverage without changing code.

---

**Status**: ✅ COMPLETE - Strike coverage enhanced for crash venture v1
**Date**: 2026-01-29
**Impact**: Eliminates NO_TRADE failures due to insufficient strike coverage
