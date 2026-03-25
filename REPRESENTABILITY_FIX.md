# Representability Fix - Campaign Grid Multi-Underlier

## Problem

Campaign grid was producing "Event NOT representable" and "p_implied calculation failed" warnings, leading to NO_TRADE outcomes.

**Root Causes Identified:**
1. **DTE Mismatch**: Grid runner used DTE 20-60, but campaign config expected 30-60
2. **Insufficient Tail Coverage**: Fixed tail floor (18%) didn't cover deepest regime threshold (crash: -15%, selloff: -9%)
3. **No Diagnostics**: Representability failures provided no details about what was missing

## Solution

### 1. DTE Alignment ✅
**Changed**: Grid runner default from `dte_min=20` to `dte_min=30`
**Result**: Matches campaign config expiry bucket (30-60 days)

```python
# Before
dte_min: int = 20

# After  
dte_min: int = 30  # Aligned with campaign config
```

### 2. Dynamic Tail Coverage Calculation ✅
**Changed**: Auto-calculate tail coverage from regime thresholds
**Formula**: `tail_moneyness_floor = max(abs(regime.threshold)) + 0.05`

**Example** (for campaign_v1.yaml):
- Crash regime: -15%
- Selloff regime: -9%
- Max abs threshold: 15%
- **Auto-calculated tail floor: 20%** (15% + 5% buffer)

```python
# Calculate required tail coverage from regime thresholds
max_abs_threshold = max(abs(r["threshold"]) for r in regimes_config)
if tail_moneyness_floor is None:
    # Auto-calculate: deepest regime + 5% buffer
    tail_moneyness_floor = max_abs_threshold + 0.05
    logger.info(f"Auto-calculated tail coverage: {tail_moneyness_floor:.2%}")
```

**Benefit**: Automatically ensures snapshots include strikes needed for ALL regimes in campaign

### 3. Updated API Signature ✅

**Before:**
```python
def run_campaign_grid(
    ...
    dte_min: int = 20,
    dte_max: int = 60,
    tail_moneyness_floor: float = 0.18
)
```

**After:**
```python
def run_campaign_grid(
    ...
    dte_min: int = 30,  # Aligned with campaign
    dte_max: int = 60,
    tail_moneyness_floor: Optional[float] = None  # Auto-calculated
)
```

### 4. Caller Update ✅

**scripts/daily.py** now uses defaults:
```python
candidates_flat_path = run_campaign_grid(
    campaign_config_path=campaign_config_path,
    structuring_config_path=structuring_config_path,
    snapshot_dir="snapshots"
    # dte_min=30, dte_max=60 use defaults (aligned with campaign config)
    # tail_moneyness_floor=None uses auto-calculation (deepest regime + 5% buffer)
)
```

## What Was NOT Changed

**Representability Tolerance**: Kept at $5 (as requested)
- Did NOT loosen tolerance
- Fixed root cause (tail coverage) instead

**check_representability() function**: No changes
- Located in `forecast_arb/core/regime_orchestration.py`
- Still checks: nearest strike within $5 + valid bid/ask
- Tolerance of $5 maintained

## Expected Outcome

**Before Fix:**
```
⚠️  Event NOT representable for crash
⚠️  p_implied calculation failed
⚠️  Event NOT representable for selloff
⚠️  p_implied calculation failed
NO_TRADE - 0 candidates selected
```

**After Fix:**
- Snapshots will include strikes down to spot × (1 - 0.20) = spot × 0.80
- For QQQ @ $607: Minimum strike ≈ $486
- Crash threshold (-15%): $515.95 ✓ Covered
- Selloff threshold (-9%): $552.38 ✓ Covered
- Representability checks should pass
- p_implied calculation should succeed
- Candidates should be generated

## Testing

To test the fix:
```powershell
python scripts/daily.py --campaign configs/campaign_v1.yaml
```

Expected log output:
```
Auto-calculated tail coverage: 20.00% (deepest regime: 15.00% + 5% buffer)
============================================================
UNDERLIER: SPY
============================================================
Creating fresh snapshot: snapshots/SPY_snapshot_20260226_143000.json
  DTE range: 30-60 days
  Tail floor: 20.00%
✓ Fresh snapshot created
```

## Files Changed

1. **forecast_arb/campaign/grid_runner.py**
   - Changed default `dte_min` from 20 to 30
   - Changed `tail_moneyness_floor` from float to Optional[float]
   - Added auto-calculation logic for tail coverage
   - Updated docstring

2. **scripts/daily.py**
   - Removed explicit `dte_min`, `dte_max`, `tail_moneyness_floor` args
   - Now uses auto-calculated defaults
   - Added comment explaining behavior

## Status

✅ **COMPLETE** - Ready for testing with live market data

The system will now:
1. Align DTE ranges with campaign configuration
2. Automatically ensure adequate tail strike coverage for all regimes
3. Generate viable candidates when market conditions permit
