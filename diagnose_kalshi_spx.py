"""
Comprehensive diagnostic script to find S&P 500 markets on Kalshi.
Tests different query methods and parameter combinations.
"""

from forecast_arb.kalshi.client import KalshiClient
import json

client = KalshiClient()

print("=" * 80)
print("KALSHI SPX MARKET DIAGNOSTIC")
print("=" * 80)
print()

# Test 1: Get a large sample of all markets
print("Test 1: Fetching 100 open markets (no filters)")
print("-" * 80)
all_markets = client.list_markets(limit=100, status="open")
print(f"Total markets fetched: {len(all_markets)}")
print()

# Analyze series_ticker distribution
series_distribution = {}
for m in all_markets:
    series = m.get("series_ticker", "N/A")
    series_distribution[series] = series_distribution.get(series, 0) + 1

print("Series ticker distribution:")
for series, count in sorted(series_distribution.items(), key=lambda x: -x[1])[:10]:
    print(f"  {series}: {count} markets")
print()

# Test 2: Look for any index-related markets
print("Test 2: Searching for index/SPX-related markets")
print("-" * 80)
search_terms = ["SPX", "S&P", "500", "index", "INX", "INXD"]
found_any = False

for term in search_terms:
    matches = [m for m in all_markets if term.lower() in str(m).lower()]
    if matches:
        print(f"\nFound {len(matches)} markets containing '{term}':")
        for m in matches[:3]:
            print(f"  Ticker: {m.get('ticker', 'N/A')[:60]}")
            print(f"  Title: {m.get('title', 'N/A')[:60]}")
            print(f"  Series: {m.get('series_ticker', 'N/A')}")
        found_any = True

if not found_any:
    print("No markets found containing index/SPX search terms")
print()

# Test 3: Check event_ticker field (if different from ticker)
print("Test 3: Checking event_ticker vs ticker")
print("-" * 80)
sample = all_markets[0] if all_markets else {}
print("Sample market fields:")
for key in sorted(sample.keys()):
    value = str(sample[key])
    if len(value) > 60:
        value = value[:60] + "..."
    print(f"  {key}: {value}")
print()

# Test 4: Try querying with different parameter name combinations
print("Test 4: Testing different series filter parameters")
print("-" * 80)

# Try the parameter we're currently using
try:
    print("Trying: series=['INX']")
    result1 = client.list_markets(series=["INX"], status="open", limit=10)
    print(f"  Result: {len(result1)} markets")
except Exception as e:
    print(f"  Error: {e}")

# Try without the list
try:
    print("Trying: series=['INXD']")
    result2 = client.list_markets(series=["INXD"], status="open", limit=10)
    print(f"  Result: {len(result2)} markets")
except Exception as e:
    print(f"  Error: {e}")

print()

# Test 5: Check if there are ANY non-N/A series tickers
print("Test 5: Finding markets with actual series_ticker values")
print("-" * 80)
markets_with_series = [m for m in all_markets if m.get("series_ticker") and m.get("series_ticker") != "N/A"]
print(f"Markets with series_ticker != 'N/A': {len(markets_with_series)}")
if markets_with_series:
    print("\nExamples:")
    for m in markets_with_series[:5]:
        print(f"  {m.get('ticker', 'N/A')[:50]} - Series: {m.get('series_ticker', 'N/A')}")
print()

# Test 6: Look at market categories/tags
print("Test 6: Analyzing market tags and categories")
print("-" * 80)
all_tags = set()
for m in all_markets:
    tags = m.get("tags", []) or []
    all_tags.update(tags)

print(f"Unique tags found: {len(all_tags)}")
if all_tags:
    print("Sample tags:")
    for tag in sorted(all_tags)[:20]:
        print(f"  {tag}")
print()

# Test 7: Check raw API response for one market
print("Test 7: Raw market object (first market)")
print("-" * 80)
if all_markets:
    print(json.dumps(all_markets[0], indent=2))
print()

print("=" * 80)
print("DIAGNOSTIC COMPLETE")
print("=" * 80)
