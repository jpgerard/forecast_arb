from forecast_arb.kalshi.client import KalshiClient

client = KalshiClient()

# Get some markets
print("Fetching all open markets...")
all_markets = client.list_markets(limit=20, status="open")
print(f"Total markets fetched: {len(all_markets)}")
print()

# Show first 10
print("First 10 markets:")
for i, m in enumerate(all_markets[:10]):
    ticker = m.get("ticker", "N/A")
    series = m.get("series_ticker", "N/A")
    title = m.get("title", "N/A")[:60]
    print(f"{i+1}. {ticker}")
    print(f"   Series: {series}")
    print(f"   Title: {title}...")
    print()

# Try filtering by series
print("\nTrying to fetch markets with series=['INX']...")
inx_markets = client.list_markets(series=["INX"], status="open", limit=20)
print(f"INX markets found: {len(inx_markets)}")

# Show unique series tickers
series_tickers = set(m.get("series_ticker", "N/A") for m in all_markets)
print(f"\nUnique series tickers in sample: {sorted(series_tickers)}")
