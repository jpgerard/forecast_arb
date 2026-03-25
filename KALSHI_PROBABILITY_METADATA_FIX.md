# Kalshi Probability Metadata Fix

## Summary

Fixed the metadata flow issue where Kalshi probability data (`p_external`) was being fetched successfully but not preserved in `review_candidates.json`. The system now properly tracks Kalshi probability provenance with full metadata at both regime and candidate levels.

## Problem Statement

The daily workflow was calling Kalshi APIs correctly and receiving probability data, but this information was being lost before reaching `review_candidates.json`. Operators had no visibility into:
- Whether Kalshi returned an exact match or fallback
- Which Kalshi market was used
- What the authoritative p_external value was
- Whether the data was trustworthy

## Solution Overview

Implemented a **p_event_external** schema with:
- **Regime-level authoritative block** (source of truth)
- **Candidate-level reference pointers** (for flattening)
- **Full provenance metadata** (market ticker, match quality, timestamps)

## Files Modified

### 1. `scripts/run_daily_v2.py`
**Changes:**
- Enhanced `fetch_p_external()` to return full p_event_external block instead of just a float
- Updated `run_regime()` signature to accept `p_event_external: Dict` instead of `p_external: float`
- Added console logging for Kalshi market details
- Passed p_event_external to all `create_regime_result()` calls

**Key functions updated:**
```python
def fetch_p_external(...) -> Dict:  # Was: tuple[float, str, Dict]
    # Now returns structured p_event_external block

def run_regime(..., p_event_external: Dict, ...):  # Was: p_external: float
    # Extracts value for calibration, passes full block to result
```

### 2. `forecast_arb/core/regime_result.py`
**Changes:**
- Added `p_event_external: Optional[Dict[str, Any]]` field to `RegimeResult` dataclass
- Updated `to_dict()` to serialize p_event_external and enrich candidates with references
- Updated `create_regime_result()` to accept and pass through p_event_external parameter

**Candidate enrichment:**
```python
# Each candidate now gets:
candidate["p_event_external_ref"] = {
    "regime": "crash",
    "asof_ts_utc": "2026-02-26T14:01:53Z",
    "source": "kalshi",
    "authoritative": true
}
candidate["p_event_external_p"] = 0.072  # Convenience copy
```

## Schema Design

### Regime-Level (Authoritative)
```json
{
  "regimes": {
    "crash": {
      "p_event_external": {
        "p": 0.072,
        "source": "kalshi",
        "authoritative": true,
        "asof_ts_utc": "2026-02-26T14:01:53Z",
        "market": {
          "ticker": "KXINX-26APR02-B5000",
          "market_id": "KXINX-26APR02-B5000",
          "title": null
        },
        "match": {
          "exact_match": true,
          "proxy_used": false,
          "match_reason": "exact_match",
          "mapping_confidence": 0.7
        },
        "quality": {
          "liquidity_ok": true,
          "staleness_ok": true,
          "spread_ok": true,
          "warnings": []
        }
      }
    }
  }
}
```

### Candidate-Level (Reference)
```json
{
  "candidates": [
    {
      "rank": 1,
      "strikes": {...},
      "p_event_external_ref": {
        "regime": "crash",
        "asof_ts_utc": "2026-02-26T14:01:53Z",
        "source": "kalshi",
        "authoritative": true
      },
      "p_event_external_p": 0.072
    }
  ]
}
```

## Testing

### To verify the fix works:

1. **Run a daily workflow:**
```powershell
python scripts/daily.py --snapshot snapshots/SPY_snapshot_latest.json
```

2. **Check console output for Kalshi details:**
```
Step 3: External Probability
--------------------------------------------------------------------------------
p_external: 0.072 (source: kalshi)
  Market: KXINX-26APR02-B5000
  Exact match: True
```

3. **Inspect review_candidates.json:**
```powershell
cat runs/crash_venture_v2/*/artifacts/review_candidates.json | jq '.regimes.crash.p_event_external'
```

Expected output:
```json
{
  "p": 0.072,
  "source": "kalshi",
  "authoritative": true,
  "market": {
    "ticker": "KXINX-26APR02-B5000",
    ...
  },
  ...
}
```

4. **Check candidate-level references:**
```powershell
cat runs/crash_venture_v2/*/artifacts/review_candidates.json | jq '.regimes.crash.candidates[0].p_event_external_ref'
```

## Backward Compatibility

- ✅ Existing fields (`p_implied`, `p_implied_confidence`, `p_implied_warnings`) unchanged
- ✅ RegimeResult remains serializable/deserializable
- ✅ Old runs without p_event_external will have `null` for this field
- ✅ Candidate flattening logic still works (p_event_external_p is a convenience copy)

## Future Enhancements

### Optional (not implemented):
1. **Standalone diagnostic script** - `diagnose_kalshi_probability.py` for isolated testing
2. **Enhanced quality checks** - Populate `quality.liquidity_ok`, `quality.spread_ok` from actual market data
3. **PEventResult enrichment** - Add bid/ask/volume_24h to metadata for better quality assessment
4. **Troubleshooting guide** - `KALSHI_TROUBLESHOOTING.md` with common failure modes

## Architecture Notes

### Data Flow
```
fetch_p_external() 
  → p_event_external Dict (full block)
  → run_regime(p_event_external=block)
  → create_regime_result(p_event_external=block)
  → RegimeResult.to_dict()
    → regime-level: full block
    → candidate-level: enriched with references
  → review_candidates.json
```

### Key Design Decisions
1. **Regime-level as source of truth** - All candidates in a regime share the same event probability
2. **Candidate-level references** - Allows flat

tening without losing provenance
3. **Authoritative flag** - Policy decision, not just "where it came from"
4. **Null-safe** - p_event_external can be None without breaking anything

## Questions Answered

✅ **What probability did we use?** - `p_event_external.p`  
✅ **Where did it come from?** - `p_event_external.source`  
✅ **Was it authoritative?** - `p_event_external.authoritative`  
✅ **If not, why not?** - `p_event_external.quality.warnings`  
✅ **What market was mapped?** - `p_event_external.market.ticker`  

## Deployment

No configuration changes required. The fix is transparent to operators:
- Next run will automatically include p_event_external in review_candidates.json
- Console output will show Kalshi market details during Step 3
- All provenance metadata flows through without manual intervention

---

**Implemented:** 2026-02-26  
**Files Changed:** 2 (run_daily_v2.py, regime_result.py)  
**Lines Added:** ~150  
**Breaking Changes:** None
