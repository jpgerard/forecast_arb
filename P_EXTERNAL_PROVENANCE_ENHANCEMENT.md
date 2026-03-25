# P_EXTERNAL Provenance Enhancement

## Summary

Enhanced the p_external (Kalshi) probability tracking system to provide full provenance metadata in artifacts and operator-facing displays. This enables trust/reject decisions during trade selection and facilitates weekly "why we weren't anchored" analysis.

## Motivation

The existing system showed `p_src = fallback` without explaining **why** Kalshi data wasn't used. Operators and weekly reviews needed:

1. **Transparency**: What Kalshi market was mapped? Was it an exact match?
2. **Trust signals**: Bid/ask spread, volume, timestamp, confidence score
3. **Diagnostic info**: Why did we fall back? (NO_MARKET, AUTH_FAIL, STALE, etc.)
4. **Weekly analysis**: Track Kalshi coverage and policy effectiveness over time

## Solution Overview

### Three-Layer Enhancement

1. **grid_runner.py** - Extracts and attaches full p_external metadata object from regime results
2. **selector.py** - Preserves p_external_metadata in candidates_flat.json and recommended.json
3. **daily.py** - Displays P_EXTERNAL summary line in candidate detail view

### New Fields Added

#### In `candidates_flat.json` and `recommended.json`:

```json
{
  "p_external_metadata": {
    "value": 0.072,
    "source": "kalshi",
    "asof_ts_utc": "2026-02-26T14:01:53Z",
    "authoritative": true,
    "market_ticker": "KXINX-26APR02-B5000",
    "market_id": "KXINX-26APR02-B5000",
    "market_title": null,
    "exact_match": true,
    "proxy_used": false,
    "match_reason": "exact_match",
    "mapping_confidence": 0.7,
    "liquidity_ok": true,
    "staleness_ok": true,
    "spread_ok": true,
    "warnings": []
  },
  "p_external_status": "OK",
  "p_external_reason": "Authoritative external source used"
}
```

#### Status Values:
- `OK` - Authoritative external source used
- `AUTH_FAIL` - Kalshi data present but policy blocked (not authoritative)
- `NO_MARKET` - No Kalshi market found or mapped for this event
- `UNKNOWN` - Probability source not available

## Output Examples

### Daily Console - Candidate Detail View

When Kalshi data **IS** authoritative:
```
Probability: P_used=0.300 | P_impl=0.063 | P_ext=0.072 | source=kalshi
P_EXT: 0.072 (kalshi) | market: KXINX-26APR02-B5000 | exact_match: True | ts: 2026-02-26T14:01:53 | conf: 0.7
```

When Kalshi data is **NOT** authoritative (policy blocked):
```
Probability: P_used=0.300 | P_impl=0.063 | P_ext=— | source=fallback
P_EXT: — | status: AUTH_FAIL | reason: Policy blocked: stale_quote, low_liquidity
```

When **NO** Kalshi market found:
```
Probability: P_used=0.300 | P_impl=0.063 | P_ext=— | source=fallback
P_EXT: — | status: NO_MARKET | reason: No Kalshi market found or mapped for this event
```

### JSON Artifacts

The p_external_metadata object is now preserved in:
- `runs/campaign/<run_id>/candidates_flat.json` (all candidates)
- `runs/campaign/<run_id>/recommended.json` (selected + rejected_top10)

This allows post-run analysis without re-fetching data.

## Files Modified

### 1. `forecast_arb/campaign/grid_runner.py`
**Changes:**
- Enhanced `flatten_candidate()` to extract full p_external_metadata from regime results
- Added p_external_status and p_external_reason fields
- Comprehensive mapping of market ticker, quality indicators, timestamps

**Key addition:**
```python
# p_external_metadata: Full provenance object (NEW - comprehensive Kalshi details)
p_external_metadata = {
    "value": regime_p_external.get("p"),
    "source": regime_p_external.get("source"),
    "market_ticker": market_data.get("ticker"),
    "exact_match": match_data.get("exact_match", False),
    "mapping_confidence": match_data.get("mapping_confidence"),
    "liquidity_ok": quality_data.get("liquidity_ok"),
    "staleness_ok": quality_data.get("staleness_ok"),
    "spread_ok": quality_data.get("spread_ok"),
    "warnings": quality_data.get("warnings", [])
}
```

### 2. `forecast_arb/campaign/selector.py`
**Changes:**
- Updated `run_selector()` to preserve p_external_metadata in selected candidates
- Added p_external_status and p_external_reason to rejected_top10 for diagnostics
- Ensures full provenance flows through to recommended.json

**Key additions:**
```python
"selected": [
    {
        **candidate,
        "p_external_metadata": candidate.get("p_external_metadata"),
        "p_external_status": candidate.get("p_external_status"),
        "p_external_reason": candidate.get("p_external_reason")
    }
]
```

### 3. `scripts/daily.py`
**Changes:**
- Added P_EXTERNAL summary line in campaign mode candidate detail view
- Shows market ticker, exact_match, timestamp, confidence when available
- Shows status + reason when Kalshi data not used
- Provides diagnostic info for NO_MARKET / AUTH_FAIL / STALE cases

**Key addition:**
```python
# P_EXTERNAL SUMMARY (NEW - comprehensive Kalshi details)
if p_ext_metadata and isinstance(p_ext_metadata, dict):
    ticker = p_ext_metadata.get("market_ticker") or "N/A"
    value = p_ext_metadata.get("value")
    exact_match = p_ext_metadata.get("exact_match", False)
    confidence = p_ext_metadata.get("mapping_confidence", 0)
    
    print(f"P_EXT: {value:.3f} ({source}) | market: {ticker} | "
          f"exact_match: {exact_match} | ts: {timestamp[:19]} | conf: {confidence:.1f}")
elif p_ext_status:
    print(f"P_EXT: — | status: {p_ext_status} | reason: {p_ext_reason or 'N/A'}")
```

## Use Cases

### Operator Trust Decision (Pre-Trade)

When reviewing a candidate before execution:
```
P_EXT: 0.072 (kalshi) | market: KXINX-26APR02-B5000 | exact_match: True | ts: 2026-02-26T14:01:53 | conf: 0.7
```

**Question:** Do I trust this price?
- ✓ Exact match (not proxy)
- ✓ Recent timestamp (< 1 hour old)
- ✓ High confidence (0.7)
- → **Trust signal: STRONG**

vs.

```
P_EXT: — | status: AUTH_FAIL | reason: Policy blocked: stale_quote, low_liquidity
```

**Question:** Why are we using fallback?
- Policy identified stale quote OR low liquidity
- → **Action: Review policy thresholds or accept fallback**

### Weekly Review Analysis

Query all recommended.json files from the week:

```powershell
# Count how often we used Kalshi vs fallback
Get-ChildItem -Recurse -Filter recommended.json | 
  ForEach-Object { Get-Content $_ | ConvertFrom-Json } |
  Select -ExpandProperty selected |
  Group-Object p_external_status |
  Select Name, Count
```

Output:
```
Name        Count
----        -----
OK          12
AUTH_FAIL   5
NO_MARKET   3
```

**Insight:** 60% Kalshi coverage, 25% policy blocked, 15% no market found

### Post-Mortem: "Why did we bet 4x against market?"

Load the artifact:
```powershell
$candidate = (Get-Content recommended.json | ConvertFrom-Json).selected[0]
$candidate.p_external_metadata
```

See full details:
- What Kalshi market was mapped
- Was it exact or proxy?
- What was the timestamp/freshness?
- Why did policy block it (if applicable)?

## Testing

Run campaign mode to verify:
```powershell
python scripts/daily.py --campaign configs/campaign_v1.yaml
```

**Expected output:**
1. Candidate detail view shows P_EXT summary line
2. candidates_flat.json contains p_external_metadata objects
3. recommended.json preserves full provenance
4. Console shows diagnostic info when fallback used

## Backward Compatibility

✅ **Fully backward compatible:**
- Old fields (`p_event_used`, `p_implied`, `p_external`, `p_source`) unchanged
- New fields added as optional (default to None if not available)
- Single-regime mode (non-campaign) unaffected
- Old runs without p_external_metadata will gracefully display "NO_DATA"

## Future Enhancements

### Optional (not implemented):
1. **Bid/ask/volume enrichment** - Add to p_external_metadata from Kalshi API
2. **Timeline view** - Track p_external values over multiple runs for same event
3. **Alert thresholds** - Warn if exact_match=False or confidence<0.5
4. **Policy tuner** - Suggest policy adjustments based on AUTH_FAIL patterns

## Architecture Notes

### Data Flow
```
RegimeResult.p_event_external (regime-level, authoritative)
  ↓
flatten_candidate() in grid_runner.py
  → extracts market ticker, quality, timestamp
  → determines status (OK / AUTH_FAIL / NO_MARKET)
  → attaches full metadata object
  ↓
candidates_flat.json (archived)
  ↓
selector.run_selector()
  → preserves p_external_metadata
  ↓
recommended.json (selected + rejected_top10)
  ↓
daily.py (display)
  → parses p_external_metadata
  → shows P_EXT summary line
```

### Why This Design?

1. **Regime-level as source of truth** - All candidates in a regime share the same event, thus same p_external
2. **Metadata at candidate level** - Enables flat file storage and per-candidate provenance
3. **Status + reason fields** - Quick filtering/grouping without parsing nested objects
4. **Full metadata preservation** - Enables retroactive analysis without API re-fetch

## Questions Answered

✅ **What Kalshi market did we map to?** - `p_external_metadata.market_ticker`  
✅ **Was it an exact match or proxy?** - `p_external_metadata.exact_match`  
✅ **Why did we fall back?** - `p_external_reason`  
✅ **Can I trust this price?** - Check `exact_match`, `staleness_ok`, `liquidity_ok`, `confidence`  
✅ **What's our Kalshi coverage?** - Aggregate `p_external_status` across runs  

---

**Implemented:** 2026-02-26  
**Files Changed:** 3 (grid_runner.py, selector.py, daily.py)  
**Lines Added:** ~100  
**Breaking Changes:** None  
**Test Coverage:** Manual verification via campaign mode
