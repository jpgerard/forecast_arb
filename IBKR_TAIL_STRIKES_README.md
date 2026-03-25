# IBKR Snapshot Exporter - Tail Strike Support for Crash Venture

## Overview

The IBKR snapshot exporter now supports tail strike inclusion for crash venture strategies. This allows you to capture deep out-of-the-money put options needed for tail risk analysis.

## New Features

### 1. Tail Moneyness Floor (`--tail-moneyness-floor`)

Specify a drawdown percentage to automatically compute the minimum strike:

```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --dte-min 20 \
    --dte-max 60 \
    --tail-moneyness-floor 0.18 \
    --out snapshot_crash_venture.json
```

**How it works:**
- For crash venture with 18% drawdown: `tail_floor = S0 * (1 - 0.18)`
- If SPY is at $600, tail floor = $492, rounded down to $490
- Includes all strikes from $490 up to spot, plus 5 strikes above for completeness

**Rounding rules:**
- Strikes < $100: Round down to nearest $5
- Strikes ≥ $100: Round down to nearest $10

### 2. Explicit Minimum Strike (`--min-strike`)

Directly specify the minimum strike price:

```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --dte-min 20 \
    --dte-max 60 \
    --min-strike 480 \
    --out snapshot_crash_venture.json
```

## Output Metadata

The snapshot JSON now includes tail strike metadata:

```json
{
  "snapshot_metadata": {
    "underlier": "SPY",
    "current_price": 600.50,
    "tail_metadata": {
      "tail_floor_strike": 490.0,
      "tail_moneyness_floor": 0.18,
      "tail_floor_raw": 492.41,
      "tail_floor_source": "computed_from_moneyness",
      "incomplete": false,
      "actual_floor": 490.0
    }
  }
}
```

### Incomplete Coverage Warning

If IBKR doesn't have contracts down to the requested tail floor:

```json
{
  "tail_metadata": {
    "incomplete": true,
    "requested_floor": 490.0,
    "actual_floor": 550.0
  }
}
```

You'll also see a warning in the logs:
```
⚠️  INCOMPLETE TAIL COVERAGE: Requested $490.00, got $550.00
```

## Backward Compatibility

Legacy mode still works with `--strikes-below` and `--strikes-above`:

```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --dte-min 20 \
    --dte-max 60 \
    --strikes-below 10 \
    --strikes-above 10 \
    --out snapshot_legacy.json
```

**Note:** You cannot mix legacy and tail modes - the command will error if you try.

## Usage Examples

### Crash Venture (Default: 18% drawdown)
```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --tail-moneyness-floor 0.18 \
    --dte-min 25 \
    --dte-max 35 \
    --out spy_crash_venture.json
```

### Conservative Tail (12% drawdown)
```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --tail-moneyness-floor 0.12 \
    --dte-min 25 \
    --dte-max 35 \
    --out spy_conservative_tail.json
```

### Aggressive Tail (25% drawdown)
```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --tail-moneyness-floor 0.25 \
    --dte-min 25 \
    --dte-max 35 \
    --out spy_aggressive_tail.json
```

### Explicit Strike Floor
```bash
python -m forecast_arb.data.ibkr_snapshot SPY \
    --min-strike 450 \
    --dte-min 25 \
    --dte-max 35 \
    --out spy_explicit_floor.json
```

## Strike Selection Logic

**Tail Mode:**
1. Compute tail_floor_strike (from moneyness or explicit)
2. Include all strikes: `tail_floor_strike <= K < spot`
3. Add 5 strikes above spot for completeness
4. Validate coverage and mark incomplete if needed

**Legacy Mode:**
1. Find N strikes below spot
2. Find M strikes above spot
3. No tail coverage guarantees

## Strike Coverage Guarantee

In tail mode, the exporter **guarantees** coverage of the tail band:
- ✅ All available strikes from floor to spot are included
- ✅ Incomplete flag set if requested floor not available
- ✅ Actual floor recorded in metadata
- ✅ Warning logged if incomplete

This ensures crash venture strategies have the deep OTM puts they need for tail risk modeling.

## Testing

Run the tail strike tests:

```bash
python -m pytest tests/test_ibkr_tail_strikes.py -v
```

All 7 tests verify:
- ✅ Tail strike filtering with moneyness floor
- ✅ Explicit minimum strike
- ✅ Incomplete coverage detection
- ✅ Legacy mode backward compatibility
- ✅ Rounding logic (< $100 and ≥ $100)
- ✅ Above-spot band inclusion

## Integration with Crash Venture

The tail strike feature is designed to work seamlessly with the crash venture engine:

1. Export snapshot with tail strikes
2. Load into crash venture strategy
3. Deep OTM puts are available for portfolio construction
4. Tail metadata helps validate coverage

Example workflow:
```bash
# 1. Export with tail strikes
python -m forecast_arb.data.ibkr_snapshot SPY \
    --tail-moneyness-floor 0.18 \
    --dte-min 28 \
    --dte-max 32 \
    --out data/spy_crash_venture.json

# 2. Run crash venture (uses the tail strikes automatically)
python -m forecast_arb.engine.run crash_venture_v1 \
    --snapshot data/spy_crash_venture.json \
    --config configs/structuring_crash_venture_v1.yaml
```
