# Expiry Selection Representability Fix

## Root Cause: Expiry Selection Ignored Representability

### The Bug

**Diagnostic Evidence** (QQQ @ $608.85, crash threshold $517.52):
```
20260331 (DTE=32): ✓ REPRESENTABLE (nearest=$520, distance=$2.48)
20260402 (DTE=34): ✓ REPRESENTABLE (nearest=$520, distance=$2.48)
20260410 (DTE=42): ✗ NOT REPRESENTABLE (nearest=$602, distance=$84.48)  ← SELECTED!
20260417 (DTE=49): ✓ REPRESENTABLE (nearest=$520, distance=$2.48)
```

**Problem**: `select_best_expiry()` was selecting **20260410** because:
- DTE midpoint = 45 days
- Closest expiry = 20260410 (DTE=42)
- High coverage score (good ATM quotes)
- **BUT** missing tail strikes for crash threshold

**Result**: "Event NOT representable" warnings for both crash and selloff regimes

## The Fix

### 1. Enhanced `select_best_expiry()` ✅

Added representability filter to expiry selection logic:

```python
def select_best_expiry(
    snapshot: Dict,
    target_dte: Optional[int] = None,
    dte_min: Optional[int] = None,
    dte_max: Optional[int] = None,
    event_threshold: Optional[float] = None,  # NEW
    threshold_tolerance: float = 5.0           # NEW
) -> Tuple[Optional[str], Dict]:
```

**New Logic**:
1. Filter by DTE range (existing)
2. **Filter out non-representable expiries** (NEW)
   - Check if threshold strike exists within $5
   - Check if bid>0 and ask>0
   - Skip expiries that fail
3. Score by coverage (existing)
4. Select best representable expiry

### 2. New Helper Function ✅

```python
def _check_expiry_representability(
    puts: List[Dict],
    threshold: float,
    tolerance: float = 5.0
) -> Tuple[bool, str]:
    """Check if expiry has strikes near threshold with valid quotes."""
```

**Checks**:
- Nearest strike within tolerance ($5)
- Valid bid/ask quotes (bid > 0, ask > 0)
- Returns (is_representable, diagnostic_reason)

### 3. Updated Callers ✅

**scripts/run_daily_v2.py** (2 locations updated):

```python
# Calculate event threshold for representability check
spot = metadata["current_price"]
event_threshold = spot * (1 + event_moneyness)

logger.info(f"Event threshold for representability: ${event_threshold:.2f}")

target_expiry, expiry_diagnostics = select_best_expiry(
    snapshot=snapshot,
    target_dte=target_dte_midpoint,
    dte_min=dte_min,
    dte_max=dte_max,
    event_threshold=event_threshold  # NEW
)
```

## Expected Behavior After Fix

**Before**:
```
Selected expiry 20260410 (DTE=42) with coverage score 0.85
⚠️  Event NOT representable for crash
⚠️  p_implied calculation failed
No candidates generated for crash
```

**After**:
```
Skipping non-representable expiry 20260410: NEAREST_STRIKE_TOO_FAR
Selected expiry 20260331 (DTE=32) with coverage score 0.82
Event threshold for representability: $517.52
Selected expiry: 20260331
✓ Event IS representable
p_implied: 0.025 (confidence: 0.75)
Generated 15 candidates for crash
```

## Files Changed

1. **forecast_arb/structuring/expiry_selection.py**
   - Added `event_threshold` and `threshold_tolerance` parameters
   - Added `_check_expiry_representability()` helper function
   - Filter expiries before scoring
   - Enhanced diagnostics with non-representable expiries list

2. **scripts/run_daily_v2.py**
   - Calculate `event_threshold` from spot and moneyness
   - Pass to `select_best_expiry()` in both locations (line ~326 and ~969)
   - Added logging for threshold calculation

3. **diagnose_representability.py** (NEW)
   - Diagnostic tool to analyze snapshot representability
   - Shows threshold calculation, strike coverage, quote   validity
   - Usage: `python diagnose_representability.py <snapshot> --regime-threshold -0.15`

## Testing

Run campaign mode and verify:
```powershell
python scripts/daily.py --campaign configs/campaign_v1.yaml
```

Expected output should show:
1. Expiry selection skips non-representable expiries
2. Selected expiry has strikes near threshold
3. Both crash and selloff regimes generate candidates
4. No "Event NOT representable" warnings

## Status

✅ **COMPLETE** - Expiry selection now filters for representability

The fix ensures only expiries with adequate tail strike coverage are selected, preventing systematic representability failures.
