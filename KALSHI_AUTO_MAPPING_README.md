# Kalshi Auto-Mapping Feature

## Overview

The Kalshi auto-mapping feature automatically finds the right Kalshi prediction market based on your event parameters (moneyness, expiry, spot price). No more manually searching for tickers!

## How It Works

1. **Reads your event parameters** from the config (moneyness, expiry)
2. **Converts SPY to SPX** (SPX ≈ SPY × 10)
3. **Queries Kalshi API** for all open SPX markets
4. **Maps to best match** using the `market_mapper.py` algorithm
5. **Fetches p_event** from the mapped market
6. **Logs audit trail** for full transparency

## Usage

### Auto-Mapping Mode (Recommended)

```bash
python scripts/run_daily.py \
  --p-event-source kalshi-auto \
  --review-only-structuring
```

**That's it!** The system will automatically:
- Calculate your event threshold (e.g., SPY @ $691 with -15% moneyness → SPX ~$5,870)
- Search Kalshi for matching SPX markets
- Pick the best match (lowest mapping error, highest liquidity)
- Fetch the p_event

### Manual Mode (Legacy)

If you know the exact Kalshi ticker you want to use:

```bash
python scripts/run_daily.py \
  --p-event-source kalshi \
  --kalshi-ticker INXD-26MAR20-B5870 \
  --review-only-structuring
```

### Fallback Mode (Default)

```bash
python scripts/run_daily.py \
  --p-event-source fallback \
  --fallback-p 0.30
```

## Requirements

### API Credentials

Set these environment variables:

```bash
export KALSHI_API_KEY="your_api_key"
export KALSHI_API_SECRET="your_api_secret"
```

Or in your `.env` file:
```
KALSHI_API_KEY=your_api_key
KALSHI_API_SECRET=your_api_secret
```

Without credentials, auto-mapping will gracefully fall back to the configured `--fallback-p` value.

## Example Output

```
================================================================================
KALSHI AUTO-MAPPING MODE
================================================================================
Finding Kalshi market automatically based on event parameters...

Event Parameters:
  SPY Spot: $691.40
  SPX Spot (estimated): $6914.00
  Moneyness: -15.00%
  Expiry: 20260320

Fetching Kalshi markets...
Found 87 open SPX markets

✓ Found matching Kalshi market:
  Ticker: INXD-26MAR20-B5870
  Title: S&P 500 closes below $5,870 on March 20, 2026
  Level: $5870.00
  Mapping Error: 0.24%
  Liquidity Score: 0.85
  Rationale: Level-based market at 5870 vs target 5875 (0.24% error)

✓ Auto-mapped Kalshi p_event: 0.342
```

## Audit Trail

All auto-mapping details are saved to `final_decision.json`:

```json
{
  "inputs": {
    "p_event": 0.342,
    "p_event_source": "kalshi-auto",
    "kalshi_auto_mapping": {
      "ticker": "INXD-26MAR20-B5870",
      "title": "S&P 500 closes below $5,870 on March 20, 2026",
      "implied_level": 5870.0,
      "mapping_error": 0.0024,
      "liquidity_score": 0.85,
      "rationale": "Level-based market at 5870 vs target 5875 (0.24% error)",
      "alternatives_found": 3
    }
  }
}
```

## Mapping Algorithm

The `market_mapper.py` algorithm:

1. **Filters** for SPX markets with matching expiry (exact date)
2. **Parses** market levels from ticker/title (supports both "below X" and "between X-Y" formats)
3. **Calculates** mapping error: `|market_level - target_level| / target_level`
4. **Ranks** by mapping error (ascending) then liquidity (descending)
5. **Accepts** only markets within `max_mapping_error` threshold (default: 10%)

## Troubleshooting

### No Matching Markets Found

```
⚠️  No matching Kalshi markets found
   Try widening max_mapping_error or check if markets exist
```

**Solutions:**
- Markets might not exist for your exact expiry/threshold combination
- Check Kalshi.com to see if SPX markets exist for your date
- Use manual mode with a different ticker
- Use fallback mode

### Kalshi API Credentials Not Found

```
⚠️  Kalshi API credentials not found - cannot auto-map
```

**Solution:**
Set `KALSHI_API_KEY` and `KALSHI_API_SECRET` environment variables

### Auto-Mapping Failed - Exception

Check the logs and `kalshi_auto_mapping.error` field in `final_decision.json` for details.

## SPY to SPX Conversion

**Important:** Your trade uses SPY options, but Kalshi markets track SPX (S&P 500 Index).

- SPY is an ETF trading at ~$691
- SPX is the index trading at ~$6,910 (roughly 10× SPY)

The auto-mapper handles this conversion automatically:
```python
spot_spx = spot_spy * 10  # Approximate conversion
```

This means:
- SPY threshold @ $587 → SPX threshold @ $5,870
- The system finds Kalshi markets near $5,870 for SPX

## Configuration

Mapping parameters can be tuned in the code (advanced):

```python
# In run_daily.py, kalshi-auto section:
mapped_markets = map_event_to_markets(
    event_def=event_def_for_mapping,
    spot_spx=spot_spx,
    kalshi_markets=all_markets,
    max_mapping_error=0.10  # Allow 10% error (adjustable)
)
```

## Comparison of Modes

| Mode | Pros | Cons | Use When |
|------|------|------|----------|
| **kalshi-auto** | Fully automated, auditable, no manual lookup | Requires API credentials | You want automated workflow |
| **kalshi** (manual) | Full control, can override | Must find ticker manually | You have specific market in mind |
| **fallback** | Always works, no API needed | Not market-based | Testing or when Kalshi unavailable |

## Next Steps

1. **Set up Kalshi API credentials** (see Requirements above)
2. **Run with `--p-event-source kalshi-auto`**
3. **Check the audit trail** in `final_decision.json`
4. **Review the mapping** in logs to ensure accuracy
5. **Trade with confidence** knowing the p_event is from the right market!

---

**Note:** This feature is designed to work seamlessly with `--review-only-structuring` mode for manual review workflows.
