# Kalshi Multi-Series Adapter with Proxy Support

## Overview

The Kalshi adapter has been enhanced to search multiple series and provide proxy probabilities when exact matches are not available. This expansion maintains full backward compatibility with existing interfaces.

##Series Supported

- **KXINX**: Daily S&P 500 close levels (exact match)
- **KXINXY**: Yearly S&P 500 close levels (exact match)
- **KXINXMINY**: Yearly S&P 500 minimum levels (proxy via hazard scaling)
- **KXINXMAXY**: Yearly S&P 500 maximum levels (reserved for future use)

## Feature: Proxy Probabilities

### What is a Proxy?

A **proxy probability** is an estimated event probability derived from related Kalshi markets when no exact match exists. Proxies are:

- **Explicitly labeled** - Never silently treated as exact matches
- **Low confidence** - Fixed at ≤0.40 confidence score
- **Heavily warned** - Include multiple warnings (PROXY_USED, LOW_CONFIDENCE_PROXY, HORIZON_MISMATCH)
- **Informational only** - Do NOT bypass BLOCKED_FALLBACK policy by default

### Proxy Method: Yearly Min Hazard Scaling

When no exact match exists, the adapter can compute a proxy using yearly minimum data:

1. Find KXINXMINY market for "yearly min < barrier"
2. Extract `p_1y_breach` from Kalshi mid-price
3. Scale to horizon T days using hazard rate: `p_T = 1 - (1 - p_1y)^(T/365)`
4. Return with low confidence (0.35) and warnings

**Example:**
- Event: SPX below 5100 in 45 days
- Kalshi KXINXMINY: 30% chance yearly min < 5100
- Proxy: ~3.8% chance in 45 days (via hazard scaling)

## Hard Constraints (Non-Breaking)

✅ **Maintained:**
- PEventResult structure unchanged (proxy in metadata only)
- Default behavior: proxy disabled (`allow_proxy=False`)
- If no exact match and proxy disabled → returns None (existing fallback behavior)
- Proxy values never override `p_event` field
- Proxy cannot silently bypass BLOCKED_FALLBACK policy

## Usage

### Enable Proxy Support

```python
from forecast_arb.oracle.p_event_source import KalshiPEventSource, create_p_event_source

# Option 1: Direct instantiation
source = KalshiPEventSource(client, allow_proxy=True)

# Option 2: Factory (must modify factory to pass allow_proxy)
source = create_p_event_source(
    mode="kalshi",
    kalshi_client=client
    # Note: Factory currently doesn't expose allow_proxy parameter
)
```

### Event Definition Format

```python
event_def = {
    "type": "index_drawdown",
    "index": "SPX",
    "threshold_pct": -0.15,  # 15% drawdown
    "expiry": date(2026, 3, 20)
}

result = source.get_p_event(
    event_def,
    spot_spx=6000.0,
    horizon_days=45
)
```

### Interpreting Results

**Exact Match:**
```python
{
    "p_event": 0.18,  # Actual probability
    "source": "kalshi",
    "confidence": 0.70,
    "metadata": {
        "market_ticker": "KXINX-26MAR20-B5100",
        "source_series": "KXINX"
    },
    "warnings": []
}
```

**Proxy (when enabled):**
```python
{
    "p_event": None,  # NO exact match
    "source": "kalshi",
    "confidence": 0.0,  # Zero because no exact match
    "metadata": {
        "p_external_proxy": 0.038,  # PROXY value
        "proxy_method": "yearly_min_hazard_scale",
        "proxy_series": "KXINXMINY",
        "proxy_confidence": 0.35,  # LOW
        "proxy_horizon_days": 45,
        "proxy_market_ticker": "KXINXMINY-26DEC31-B5100"
    },
    "warnings": [
        "PROXY_USED",
        "LOW_CONFIDENCE_PROXY",
        "HORIZON_MISMATCH",
        "ASSUMPTION_HAZARD_RATE"
    ]
}
```

**No Match (proxy disabled or unavailable):**
```python
{
    "p_event": None,
    "source": "kalshi",
    "confidence": 0.0,
    "metadata": {},
    "warnings": ["NO_MARKET_MATCH"]
}
```

## Review Pack Integration

When proxy data isavailable, the review pack displays it prominently:

```markdown
### External Probability (p_external)
- **Value:** N/A
- **Source:** kalshi
- **Confidence:** 0.00

#### ⚠️ Proxy Probability Available (NOT exact match)
- **Proxy Value:** 0.0380 (3.80%)
- **Method:** yearly_min_hazard_scale
- **Series:** KXINXMINY
- **Horizon:** 45 days
- **Market Ticker:** KXINXMINY-26DEC31-B5100
- **Confidence:** 0.35 (LOW)

**⚠️ IMPORTANT:** This is a proxy probability, NOT an exact Kalshi market match.
It is estimated using yearly minimum data with hazard rate scaling.
Use with extreme caution and heavy discounting. Not recommended for automated trading.
```

## Testing

### Run Multi-Series Tests

```bash
python -m pytest tests/test_kalshi_multi_series.py -v
```

### Test Coverage

- ✅ Exact match scenarios (KXINX, KXINXY)
- ✅ Proxy scenarios (KXINXMINY with hazard scaling)
- ✅ Feature flag (proxy disabled/enabled)
- ✅ Pagination and market fetching
- ✅ Non-breaking behavior (p_event stays None when only proxy)
- ✅ Hazard scaling sanity checks

## Implementation Details

### Files Modified

1. **`forecast_arb/kalshi/multi_series_adapter.py`** (NEW)
   - Multi-series search logic
   - Proxy computation
   - Market fetching with pagination

2. **`forecast_arb/oracle/p_event_source.py`** (MODIFIED)
   - KalshiPEventSource: Added `allow_proxy` parameter
   - Integrated multi-series search
   - Proxy metadata handling

3. **`forecast_arb/review/review_pack.py`** (MODIFIED)
   - Display proxy information when available
   - Heavy warnings for proxy use

4. **`tests/test_kalshi_multi_series.py`** (NEW)
   - Comprehensive unit tests

### Architecture

```
┌─────────────────────────────────────┐
│  KalshiPEventSource                 │
│  (allow_proxy param)                │
└───────────────┬─────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│  kalshi_multi_series_search()       │
│  - Fetches all series               │
│  - Tries exact match first          │
│  - Falls back to proxy if enabled   │
└───────────────┬─────────────────────┘
                │
      ┌─────────┴──────────┐
      ▼                    ▼
┌──────────────┐    ┌─────────────────┐
│ find_exact   │    │ compute_proxy   │
│ _match()     │    │ _yearly_min()   │
└──────────────┘    └─────────────────┘
```

## Future Enhancements

### Potential Proxy Methods

1. **Daily Close Aggregate** (Method 2 from spec)
   - Use multiple daily KXINX markets
   - Aggregate: `p_T = 1 - ∏(1 - p_d)`
   - Requires dense market coverage

2. **Yearly Max as "No Breach" Proxy**
   - Use KXINXMAXY
   - Invert logic: `p_breach = 1 - p_max_above`

3. **Range-to-Level Mapping**
   - Use range markets as approximations
   - Interpolate between bucket boundaries

### Configuration Enhancement

Add config file support for:
- Custom series lists
- Proxy enable/disable per campaign
- Confidence thresholds for proxy acceptance

## Warnings and Guardrails

### Proxy Should NOT:
- ❌ Silently override `p_event` field
- ❌ Bypass BLOCKED_FALLBACK policy
- ❌ Be used for automated trading (without explicit review)
- ❌ Claim high confidence

### Proxy MUST:
- ✅ Be explicitly labeled in metadata
- ✅ Include multiple warnings
- ✅ Have confidence ≤ 0.40
- ✅ Be clearly distinguished in review packs
- ✅ Default to OFF

## Migration Guide

### For Existing Code

**No changes required!** The enhancement is backward compatible:

- Default behavior: `allow_proxy=False` → same as before
- PEventResult structure unchanged
- Existing tests may need minor updates for error message wording
- Review pack extended but maintains existing sections

### To Enable Proxy

1. Pass `allow_proxy=True` when creating KalshiPEventSource
2. Update review workflow to handle proxy probabilities
3. Train operators on proxy interpretation
4. Consider policy: should proxy trigger manual review or auto-block?

## Edge Cases Handled

- **No markets in any series**: Returns "NO_MARKET_MATCH" warning
- **Proxy calculation fails**: Falls back to "no match" behavior
- **Market has no pricing**: Proxy returns None
- **Mapping error too large**: Market rejected from candidacy (>15% for proxy)
- **Event type unsupported**: Gracefully falls back to legacy path

## Performance Considerations

- Fetches up to 200 markets per series (API max)
- Searches 4 series by default (KXINX, KXINXY, KXINXMINY, KXINXMAXY)
- Total API calls: ~4 (one per series)
- Pagination: Handled within 200-limit (Kalshi v2 doesn't support cursor)

## Questions?

See also:
- `P_EVENT_SYSTEM_README.md` - Core p_event architecture
- `KALSHI_AUTO_MAPPING_README.md` - Market mapping system
- `tests/test_kalshi_multi_series.py` - Usage examples
