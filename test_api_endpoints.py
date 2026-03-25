"""
Test different Kalshi API endpoints to find financials markets.
"""

from forecast_arb.kalshi.client import KalshiClient
import requests
import time
import os

# Current endpoint in use
print("=" * 80)
print("TESTING KALSHI API ENDPOINTS")
print("=" * 80)
print()

client = KalshiClient()
print(f"Current BASE_URL: {client.base_url}")
print()

# Test 1: Try fetching with different status filters
print("Test 1: Different status filters on current endpoint")
print("-" * 80)
for status in ["open", "closed", "settled", "active"]:
    try:
        markets = client.list_markets(status=status, limit=10)
        print(f"  status='{status}': {len(markets)} markets")
        if markets:
            sample_ticker = markets[0].get('ticker', 'N/A')
            print(f"    Sample: {sample_ticker[:60]}")
    except Exception as e:
        print(f"  status='{status}': ERROR - {e}")
print()

# Test 2: Try without status filter
print("Test 2: No status filter")
print("-" * 80)
try:
    # Manually construct request to test without status
    api_key = os.getenv("KALSHI_API_KEY_ID")
    timestamp_str = str(int(time.time()))
    
    headers = {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": client._sign_request(timestamp_str, "GET", "/markets"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
    }
    
    # Try without status parameter
    url = f"{client.base_url}/markets"
    response = requests.get(url, params={"limit": 10}, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    markets = data.get("markets", [])
    print(f"  No status filter: {len(markets)} markets")
    if markets:
        for i, m in enumerate(markets[:3]):
            print(f"    {i+1}. {m.get('ticker', 'N/A')[:50]}")
except Exception as e:
    print(f"  ERROR: {e}")
print()

# Test 3: Check if there's a different endpoint for financials
print("Test 3: Looking for financial market indicators in sample")
print("-" * 80)
all_markets = client.list_markets(limit=200, status="open")
print(f"Fetched {len(all_markets)} markets total")

# Check for any financial-related keywords
financial_keywords = ["financial", "index", "stock", "nasdaq", "dow", "btc", "eth", "crypto", "rate", "yield"]
for keyword in financial_keywords:
    matches = [m for m in all_markets if keyword.lower() in str(m).lower()]
    if matches:
        print(f"\n  Found {len(matches)} markets mentioning '{keyword}':")
        for m in matches[:2]:
            print(f"    {m.get('ticker', 'N/A')[:60]}")

# Test 4: Check event_ticker structure
print("\n\nTest 4: Event ticker prefixes")
print("-" * 80)
prefixes = {}
for m in all_markets:
    event_ticker = m.get("event_ticker", "")
    if event_ticker:
        prefix = event_ticker.split("-")[0] if "-" in event_ticker else event_ticker
        prefixes[prefix] = prefixes.get(prefix, 0) + 1

print("Event ticker prefixes (top 10):")
for prefix, count in sorted(prefixes.items(), key=lambda x: -x[1])[:10]:
    print(f"  {prefix}: {count}")

print("\n" + "=" * 80)
print("If no financial markets found, the issue is likely:")
print("1. Different API endpoint needed")
print("2. Different authentication/permissions")
print("3. Financial markets in different status category")
print("=" * 80)
