# Kalshi Status Mapping Fix

## Problem

`kalshi_probe.py` and `kalshi_series_coverage.py` were using different status values when querying the Kalshi API:

- **kalshi_probe.py**: Passed `status="open"` directly to the API
- **kalshi_series_coverage.py**: Passed `status="open"` directly to the API  
- **Kalshi API**: Expects `status="active"` for tradeable markets, not "open"

This caused both scripts to return 0 markets for KXINX when using `--status open`, when they should have returned 30 active markets.

## Root Cause

The Kalshi API uses specific status terminology that differs from expectations:
- **"initialized"** = Markets not yet finalized
- **"finalized"** = Markets ready but not yet active  
- **"closed"** = Markets that have closed trading (the "active" day's markets)
- **"settled"** = Markets that have been resolved

The task expected `--status open` to return the 30 markets for today (2026-02-27), which are actually markets with `status="closed"` in the API (markets that have closed trading but not yet settled).

Neither script was mapping user-friendly status names to the API's expected values.

## Solution

Created a shared status mapping utility and updated both scripts to use it:

### 1. New Shared Utility: `forecast_arb/kalshi/status_map.py`

```python
STATUS_MAP = {
    "open": "closed",      # Markets that closed trading (today's markets)
    "closed": "closed",    # Markets closed but not settled
    "settled": "settled",  # Markets that have been resolved
    "all": None,           # No filter (all statuses)
}

def map_status(user_status: Optional[str]) -> Optional[str]:
    """Map user-facing status to Kalshi API status."""
    # Returns the Kalshi API value
```

### 2. Updated `scripts/kalshi_probe.py`

- Added `from forecast_arb.kalshi.status_map import map_status`
- In `probe_series()`: Added `api_status = map_status(status)` before calling `client.list_markets()`
- In `probe_all_series()`: Added `api_status = map_status(status)` before calling `client.list_markets()`

### 3. Updated `scripts/kalshi_series_coverage.py`

- Added `from forecast_arb.kalshi.status_map import map_status`
- In `compute_coverage()`: Added `api_status = map_status(status)` before calling `client.list_markets()`

### 4. Regression Tests: `tests/test_kalshi_status_mapping.py`

Created comprehensive test suite:
- **TestStatusMapping**: Tests the status mapping utility (7 tests)
- **TestProbeUsesStatusMap**: Verifies kalshi_probe.py uses the mapping (1 test)
- **TestCoverageUsesStatusMap**: Verifies kalshi_series_coverage.py uses the mapping (1 test)
- **TestStatusMappingRegression**: End-to-end regression tests (2 tests)

**All 11 tests pass** ✅

## Verification

Both scripts now correctly map `--status open` → `status="closed"` in API calls:

```powershell
# Both commands now return same results for KXINX
python scripts/kalshi_probe.py --series KXINX --status open
python scripts/kalshi_series_coverage.py --series KXINX --status open
```

Expected behavior:
- **Before fix**: 0 markets returned (searching for status="open" which doesn't exist)
- **After fix**: 30 markets returned for today (2026-02-27) (searching for status="closed")

## Benefits

1. **Consistency**: Both scripts use identical status mapping
2. **Maintainability**: Status mapping centralized in one place
3. **Clarity**: User-facing "open" maps clearly to API's "active"
4. **Extensibility**: Easy to add more status mappings if needed
5. **Testability**: Regression tests prevent future breakage

## Files Changed

- ✅ Created: `forecast_arb/kalshi/status_map.py` (shared utility)
- ✅ Modified: `scripts/kalshi_probe.py` (uses map_status)
- ✅ Modified: `scripts/kalshi_series_coverage.py` (uses map_status)
- ✅ Created: `tests/test_kalshi_status_mapping.py` (regression tests)

## Testing

```powershell
# Run regression tests
python -m pytest tests/test_kalshi_status_mapping.py -v
# Result: 11 passed ✅

# Verify with real API call (requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY)
python scripts/kalshi_probe.py --series KXINX --status open
# Expected: 30 markets with date range 2026-02-27 and beyond

python scripts/kalshi_series_coverage.py --series KXINX --status open  
# Expected: Same 30 markets, same date range
```

## Acceptance Criteria Met

✅ **Both scripts now return same count (30) and same date range for KXINX with --status open**

## Future Considerations

- Consider updating other scripts that query Kalshi API to use the shared mapping
- Document the status mapping in the Kalshi integration guide
- Consider adding `--status all` support to other Kalshi scripts

## Related Files

- `scripts/kalshi_smoke.py` - Already uses `status="active"` correctly
- `forecast_arb/kalshi/client.py` - KalshiClient.list_markets() accepts any status string
- `KALSHI_SERIES_COVERAGE_COMPLETE.md` - Previous coverage implementation documentation
