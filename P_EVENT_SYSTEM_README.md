# P-Event System: Pluggable Event Probability Architecture

## Overview

The p_event system provides a first-class, pluggable architecture for obtaining event probabilities with explicit control over failure modes, fallback behavior, and data sources.

**Goal**: Ensure the engine **never mysteriously aborts** due to missing p_event data. Instead, it either:
- Returns a valid p_event with full provenance metadata, OR
- Exits with a clear, actionable error message (in hard-fail modes)

## Architecture

### Core Components

1. **PEventSource (Base Class)**: Abstract base for all probability sources
2. **PEventResult**: Dataclass containing probability + full provenance metadata
3. **Concrete Sources**:
   - `KalshiPEventSource`: Hard fail if Kalshi unavailable
   - `FallbackPEventSource`: Conservative estimates for smoke tests
   - `OptionsImpliedPEventSource`: Derive from option surface
   - `KalshiOrFallbackPEventSource`: Try Kalshi, fallback gracefully
   - `EnsemblePEventSource` (future): Blend multiple sources

### PEventResult Structure

```python
@dataclass
class PEventResult:
    p_event: float              # Event probability [0, 1]
    source: str                 # Source identifier
    confidence: float           # Confidence score [0, 1]
    timestamp: str              # ISO8601 timestamp
    metadata: Dict              # Source-specific data
    fallback_used: bool         # Whether fallback was used
    warnings: List[str]         # Any warnings encountered
```

## Available Modes

### 1. `kalshi` - Hard Fail Mode

**Use when**: Production runs requiring real market data

**Behavior**:
- Searches Kalshi for matching markets based on event definition
- **Hard fails** with `RuntimeError` if no market found or API unavailable
- Returns high-confidence probability with bid/ask/volume metadata

**Example**:
```python
from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.oracle.p_event_source import create_p_event_source

client = KalshiClient(demo_mode=False)
source = create_p_event_source("kalshi", kalshi_client=client)

event_def = {
    "type": "index_level",
    "underlying": "SPX",
    "date": "2026-02-27",
    "threshold": 5000,
    "direction": "below"
}

result = source.get_p_event(event_def)
# RuntimeError raised if no market found
```

### 2. `kalshi_or_fallback` - Graceful Degradation

**Use when**: Development/testing where you want real data but can't guarantee availability

**Behavior**:
- Tries Kalshi first
- Falls back to conservative estimate (default 30%) if Kalshi fails
- Logs warnings but does NOT abort
- `result.fallback_used` indicates if fallback was used

**Example**:
```python
source = create_p_event_source(
    "kalshi_or_fallback",
    kalshi_client=client,
    fallback_p_event=0.30
)

result = source.get_p_event(event_def)
# Never fails - always returns a result
if result.fallback_used:
    print(f"Warning: Using fallback probability {result.p_event}")
```

### 3. `fallback_only` - Smoke Tests

**Use when**: Unit tests, smoke tests, or when no real data needed

**Behavior**:
- Returns hardcoded probability (default 30%)
- Low confidence score (0.1)
- Useful for testing engine mechanics without data dependencies

**Example**:
```python
source = create_p_event_source("fallback_only", fallback_p_event=0.25)

result = source.get_p_event(event_def)
# Always returns fallback value
assert result.p_event == 0.25
assert result.confidence == 0.1
```

### 4. `options_implied` - Derive from Options Market

**Use when**: Kalshi has no relevant market, but you have options data

**Behavior**:
- Calculates tail probability from option prices
- Uses Black-Scholes + implied volatility
- Can enhance with Breeden-Litzenberger for actual chain data
- Moderate-high confidence (0.7)

**Example**:
```python
source = create_p_event_source(
    "options_implied",
    options_data={"spot_price": 580.0}
)

event_def = {
    "type": "price_move",
    "underlying": "SPY",
    "threshold": 550.0,  # 5% below spot
    "direction": "below"
}

result = source.get_p_event(
    event_def,
    spot_price=580.0,
    atm_iv=0.15,
    days_to_expiry=45
)
# Uses option math to estimate tail probability
```

### 5. `ensemble` - Blend Multiple Sources (Future)

**Planned feature**: Combine Kalshi + options-implied + your own priors with configurable weights.

## Event Definition Format

All sources accept an event definition dictionary:

```python
{
    "type": "index_level" | "price_move" | "volatility_spike",
    "underlying": "SPY" | "SPX" | "VIX" | etc.,
    "date": "2026-02-27",           # Optional: target date
    "threshold": 5000,              # Price/level threshold
    "direction": "below" | "above", # Direction of move
    "percent_move": -0.15           # Alternative to threshold
}
```

## Kalshi Authentication Updates

The Kalshi client now uses **RSA-PSS SHA256 signature authentication** as required by their API:

### Setup

1. **Get API credentials** from [Kalshi Dashboard](https://kalshi.com/settings/api)
2. **Download private key** and save as PEM file
3. **Configure environment**:

```bash
# .env
KALSHI_API_KEY_ID=your_key_id
KALSHI_PRIVATE_KEY_PATH=/path/to/private_key.pem
KALSHI_DEMO_MODE=false  # true for demo API
```

### API Endpoints

- **Production**: `https://api.kalshi.com/trade-api/v2/`
- **Demo**: `https://demo-api.kalshi.co/trade-api/v2/`

All requests are signed with:
- `KALSHI-ACCESS-KEY` header
- `KALSHI-ACCESS-SIGNATURE` header (base64 RSA-PSS signature)
- `KALSHI-ACCESS-TIMESTAMP` header (milliseconds)

## Integration Example

### In Engine Code

```python
from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.oracle.p_event_source import create_p_event_source

# Configuration-driven source selection
p_event_config = config.get("p_event_source", {})
mode = p_event_config.get("mode", "kalshi_or_fallback")

# Create appropriate source
if mode in ["kalshi", "kalshi_or_fallback"]:
    client = KalshiClient(demo_mode=p_event_config.get("demo_mode", False))
    source = create_p_event_source(
        mode,
        kalshi_client=client,
        fallback_p_event=p_event_config.get("fallback_value", 0.30)
    )
else:
    source = create_p_event_source(mode)

# Define event based on strategy
event_definition = {
    "type": "price_move",
    "underlying": "SPY",
    "date": expiry_date,
    "direction": "below",
    "percent_move": -0.15  # 15% crash scenario
}

# Get probability with full provenance
result = source.get_p_event(
    event_definition,
    spot_price=spot_price,
    atm_iv=atm_iv,
    days_to_expiry=dte
)

# Log provenance
logger.info(f"p_event = {result.p_event:.2%} from {result.source}")
logger.info(f"Confidence: {result.confidence:.2f}")
if result.warnings:
    for warning in result.warnings:
        logger.warning(warning)

# Use in engine
run_engine(p_event=result.p_event, ...)

# Save metadata to manifest
manifest["p_event_provenance"] = result.to_dict()
```

### Configuration YAML

```yaml
campaign_name: crash_venture_v1
config_version: 2.0

# P-Event source configuration
p_event_source:
  mode: kalshi_or_fallback  # kalshi | kalshi_or_fallback | fallback_only | options_implied
  demo_mode: false          # Use Kalshi demo API
  fallback_value: 0.30      # Conservative estimate if fallback needed
  
  # Event definition
  event:
    type: price_move
    underlying: SPY
    direction: below
    percent_move: -0.15     # 15% downside move
```

## Testing

Run the test suite:

```bash
pytest tests/test_p_event_sources.py -v
```

Tests cover:
- All source modes (kalshi, fallback, options_implied, kalshi_or_fallback)
- Failure scenarios (no markets, API errors)
- Confidence assessment
- Fallback behavior
- Event definition formats

## Migration Guide

### Old Code (examples/run_real_cycle.py)

```python
# OLD: Hardcoded search, unclear failure modes
market = find_spy_market_on_kalshi(client)
if market:
    p_event = oracle.get_event_probability(market)
else:
    if not allow_fallback:
        sys.exit(1)  # Mysterious abort
    p_event = 0.30   # No metadata
```

### New Code

```python
# NEW: Explicit mode, clear failure behavior, full provenance
source = create_p_event_source(
    "kalshi_or_fallback",
    kalshi_client=client,
    fallback_p_event=0.30
)

event_def = {
    "type": "price_move",
    "underlying": "SPY",
    "direction": "below"
}

result = source.get_p_event(event_def, spot_price=S0, atm_iv=iv, days_to_expiry=dte)

# Full transparency
logger.info(f"p_event={result.p_event:.2%} from {result.source} "
           f"(confidence={result.confidence:.2f})")
if result.fallback_used:
    logger.warning("Kalshi unavailable - using fallback")

p_event = result.p_event
```

## Benefits

1. **No Mysterious Aborts**: Every mode has explicit failure behavior
2. **Full Provenance**: Know exactly where p_event came from and how confident to be
3. **Testable**: Easy to test with fallback_only mode
4. **Flexible**: Swap sources without changing engine code
5. **Production-Ready**: Hard-fail mode ensures data quality
6. **Extensible**: Easy to add new sources (ensemble, ML models, etc.)

## Troubleshooting

### "No Kalshi market found"

**Cause**: Event definition doesn't match any open markets

**Solutions**:
1. Check event definition format
2. Use `kalshi_or_fallback` mode instead of `kalshi`
3. Use `options_implied` mode if no Kalshi market exists
4. Adjust event definition to match available markets

### "Private key not loaded"

**Cause**: `KALSHI_PRIVATE_KEY_PATH` not set or file doesn't exist

**Solutions**:
1. Set environment variable: `KALSHI_PRIVATE_KEY_PATH=/path/to/key.pem`
2. Ensure private key file exists and is readable
3. Verify PEM format (starts with `-----BEGIN RSA PRIVATE KEY-----`)

### "cryptography package required"

**Cause**: Missing dependency for RSA-PSS authentication

**Solution**:
```bash
pip install cryptography>=41.0.0
```

## Next Steps

1. **Implement ensemble mode**: Blend Kalshi + options + priors
2. **Enhance options_implied**: Use actual option chain data (Breeden-Litzenberger)
3. **Add confidence thresholds**: Warn or fail if confidence too low
4. **Historical tracking**: Store p_event provenance for post-mortem analysis
5. **Auto-calibration**: Track realized vs predicted to adjust confidence scores

## See Also

- `forecast_arb/oracle/p_event_source.py` - Source implementations
- `forecast_arb/kalshi/client.py` - Kalshi API client
- `tests/test_p_event_sources.py` - Test suite
- `.env.example` - Configuration template
