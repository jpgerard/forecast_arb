"""
Test script to verify Kalshi API connection and credentials.
"""

from forecast_arb.kalshi.client import KalshiClient
import sys


def test_kalshi_connection():
    """Test Kalshi API connection."""
    print("=" * 60)
    print("Testing Kalshi API Connection")
    print("=" * 60)
    
    try:
        # Initialize client (will auto-load from .env)
        print("\n1. Initializing KalshiClient...")
        client = KalshiClient()
        
        # Check if credentials were loaded
        if client.api_key:
            print(f"   ✅ API Key loaded: {client.api_key[:16]}...")
        else:
            print("   ⚠️  No API key found")
        
        if client.private_key:
            print(f"   ✅ Private Key loaded successfully")
        else:
            print("   ⚠️  No private key found")
        
        # Test API call - list markets
        print("\n2. Testing API call: list_markets()...")
        markets = client.list_markets(limit=5, status="open")
        
        print(f"   ✅ Successfully fetched {len(markets)} markets")
        
        # Display sample market data
        if markets:
            print("\n3. Sample Market Data:")
            print("-" * 60)
            for i, market in enumerate(markets[:3], 1):
                ticker = market.get("ticker", "N/A")
                title = market.get("title", "N/A")
                volume = market.get("volume_24h", 0)
                print(f"   Market {i}:")
                print(f"     Ticker: {ticker}")
                print(f"     Title: {title[:60]}...")
                print(f"     24h Volume: {volume:,} contracts")
                print()
        
        # Test orderbook fetch for first market
        if markets:
            print("4. Testing orderbook fetch...")
            test_ticker = markets[0].get("ticker")
            print(f"   Fetching orderbook for {test_ticker}...")
            
            try:
                orderbook = client.get_orderbook(test_ticker)
                yes_bid = orderbook["yes"]["bid"]
                yes_ask = orderbook["yes"]["ask"]
                
                if yes_bid is not None and yes_ask is not None:
                    mid_price = (yes_bid + yes_ask) / 2.0
                    print(f"   ✅ Yes Bid: {yes_bid:.2f}, Ask: {yes_ask:.2f}")
                    print(f"   ✅ Market probability: {mid_price:.1%}")
                else:
                    print("   ⚠️  Orderbook data incomplete")
            except Exception as e:
                print(f"   ⚠️  Orderbook fetch failed: {e}")
        
        print("\n" + "=" * 60)
        print("✅ Kalshi API Connection Test PASSED!")
        print("=" * 60)
        print("\nYour credentials are working correctly.")
        print("You can now run:")
        print("  python -m forecast_arb.engine.run --config configs/campaign_b.yaml --mode oracle")
        
        return True
        
    except Exception as e:
        print("\n" + "=" * 60)
        print("❌ Kalshi API Connection Test FAILED!")
        print("=" * 60)
        print(f"\nError: {e}")
        print("\nTroubleshooting:")
        print("1. Check your .env file has correct credentials:")
        print("   - KALSHI_API_KEY_ID=your_key_here")
        print("   - KALSHI_PRIVATE_KEY=your_private_key_here")
        print("2. Verify credentials at: https://kalshi.com/settings/api")
        print("3. Ensure python-dotenv is installed: pip install python-dotenv")
        
        return False


if __name__ == "__main__":
    success = test_kalshi_connection()
    sys.exit(0 if success else 1)
