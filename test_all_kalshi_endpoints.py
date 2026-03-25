"""
Comprehensive test of all Kalshi API endpoints per documentation.

Tests:
- GET /markets (with various filters)
- GET /events (collection of related markets)
- GET /series (if available)
- Market search strategies for SPX/financial markets
"""

import os
import json
from forecast_arb.kalshi.client import KalshiClient


def test_markets_endpoint(client: KalshiClient):
    """Test /markets endpoint with various filters."""
    print("\n" + "="*80)
    print("TEST 1: /markets endpoint - basic query")
    print("="*80)
    
    markets = client.list_markets(limit=200, status="open")
    print(f"Total open markets returned: {len(markets)}")
    
    if markets:
        print("\nFirst 3 markets:")
        for m in markets[:3]:
            print(f"  Ticker: {m.get('ticker')}")
            print(f"  Title: {m.get('title')}")
            print(f"  Series: {m.get('series_ticker', 'N/A')}")
            print(f"  Category: {m.get('category', 'N/A')}")
            print(f"  Tags: {m.get('tags', [])}")
            print()
    
    # Check all unique categories/series
    series = set()
    categories = set()
    tags_all = set()
    
    for m in markets:
        if m.get('series_ticker'):
            series.add(m['series_ticker'])
        if m.get('category'):
            categories.add(m['category'])
        for tag in m.get('tags', []):
            tags_all.add(tag)
    
    print(f"\nUnique series tickers found: {len(series)}")
    print(f"Series: {sorted(series)[:10]}...")  # Show first 10
    
    print(f"\nUnique categories found: {len(categories)}")
    print(f"Categories: {sorted(categories)}")
    
    print(f"\nUnique tags found: {len(tags_all)}")
    print(f"Tags: {sorted(tags_all)[:20]}...")  # Show first 20


def test_events_endpoint(client: KalshiClient):
    """Test /events endpoint (collection of related markets)."""
    print("\n" + "="*80)
    print("TEST 2: /events endpoint")
    print("="*80)
    
    try:
        response = client._get("/events", params={"limit": 100, "status": "open"})
        events = response.get("events", [])
        print(f"Total events returned: {len(events)}")
        
        if events:
            print("\nFirst 5 events:")
            for e in events[:5]:
                print(f"  Event ticker: {e.get('event_ticker')}")
                print(f"  Title: {e.get('title')}")
                print(f"  Category: {e.get('category', 'N/A')}")
                print(f"  Series: {e.get('series_ticker', 'N/A')}")
                print(f"  Markets count: {len(e.get('markets', []))}")
                print()
            
            # Check categories in events
            event_categories = set(e.get('category') for e in events if e.get('category'))
            print(f"Event categories: {sorted(event_categories)}")
            
    except Exception as e:
        print(f"Events endpoint error: {e}")


def test_series_endpoint(client: KalshiClient):
    """Test /series endpoint if it exists."""
    print("\n" + "="*80)
    print("TEST 3: /series endpoint")
    print("="*80)
    
    try:
        response = client._get("/series", params={"limit": 100})
        series = response.get("series", [])
        print(f"Total series returned: {len(series)}")
        
        if series:
            print("\nFirst 10 series:")
            for s in series[:10]:
                print(f"  Series ticker: {s.get('ticker')}")
                print(f"  Title: {s.get('title')}")
                print(f"  Category: {s.get('category', 'N/A')}")
                print()
            
            # Look for financial-related series
            financial_keywords = ['spx', 'sp500', 's&p', 'index', 'stock', 'equity', 'dow', 'nasdaq', 'finance', 'economy']
            financial_series = []
            for s in series:
                title = s.get('title', '').lower()
                ticker = s.get('ticker', '').lower()
                if any(kw in title or kw in ticker for kw in financial_keywords):
                    financial_series.append(s)
            
            if financial_series:
                print(f"\n🎯 Found {len(financial_series)} potentially financial series:")
                for s in financial_series:
                    print(f"  {s.get('ticker')}: {s.get('title')}")
            else:
                print("\n❌ No financial series found with keywords")
                
    except Exception as e:
        print(f"Series endpoint error: {e}")


def test_search_by_keyword(client: KalshiClient):
    """Search markets by SPX/financial keywords."""
    print("\n" + "="*80)
    print("TEST 4: Search markets for financial keywords")
    print("="*80)
    
    # Get all open markets
    all_markets = client.list_markets(limit=1000, status="open")
    
    keywords = ['spx', 'sp500', 's&p', 'index', 'stock', 'dow', 'nasdaq', 'financial']
    
    for keyword in keywords:
        matches = [
            m for m in all_markets 
            if keyword in m.get('title', '').lower() 
            or keyword in m.get('ticker', '').lower()
            or keyword in str(m.get('tags', [])).lower()
        ]
        
        if matches:
            print(f"\n🎯 Keyword '{keyword}' found {len(matches)} matches:")
            for m in matches[:5]:  # Show first 5
                print(f"  {m.get('ticker')}: {m.get('title')}")
        else:
            print(f"\n❌ Keyword '{keyword}' - no matches")


def test_category_filter(client: KalshiClient):
    """Test if we can filter by category."""
    print("\n" + "="*80)
    print("TEST 5: Filter by category")
    print("="*80)
    
    # Try common financial category names
    categories_to_try = [
        'financials', 'finance', 'economics', 'economy', 
        'markets', 'indices', 'stocks', 'equities'
    ]
    
    for cat in categories_to_try:
        try:
            response = client._get("/markets", params={
                "limit": 100,
                "status": "open",
                "category": cat
            })
            markets = response.get("markets", [])
            
            if markets:
                print(f"\n🎯 Category '{cat}' returned {len(markets)} markets:")
                for m in markets[:3]:
                    print(f"  {m.get('ticker')}: {m.get('title')}")
            else:
                print(f"❌ Category '{cat}' - no markets")
                
        except Exception as e:
            print(f"❌ Category '{cat}' - error: {e}")


def test_closed_markets(client: KalshiClient):
    """Check settled/closed markets for financial examples."""
    print("\n" + "="*80)
    print("TEST 6: Check settled markets for SPX/financial")
    print("="*80)
    
    for status in ["closed", "settled"]:
        try:
            markets = client.list_markets(limit=200, status=status)
            print(f"\n{status.upper()} markets: {len(markets)} found")
            
            # Look for SPX in closed markets
            spx_markets = [
                m for m in markets
                if 'spx' in m.get('title', '').lower() 
                or 'sp500' in m.get('title', '').lower()
                or 's&p' in m.get('title', '').lower()
            ]
            
            if spx_markets:
                print(f"🎯 Found {len(spx_markets)} SPX-related {status} markets:")
                for m in spx_markets[:5]:
                    print(f"  {m.get('ticker')}: {m.get('title')}")
            else:
                print(f"❌ No SPX markets in {status} status")
                
        except Exception as e:
            print(f"Error checking {status} markets: {e}")


def main():
    """Run all endpoint tests."""
    print("\n" + "="*80)
    print("KALSHI API COMPREHENSIVE ENDPOINT TEST")
    print("="*80)
    print(f"Base URL: {os.getenv('KALSHI_API_BASE_URL', 'https://api.elections.kalshi.com/trade-api/v2')}")
    print(f"API Key ID: {os.getenv('KALSHI_API_KEY_ID', 'Not set')[:20]}...")
    
    try:
        client = KalshiClient()
        
        # Run all tests
        test_markets_endpoint(client)
        test_events_endpoint(client)
        test_series_endpoint(client)
        test_search_by_keyword(client)
        test_category_filter(client)
        test_closed_markets(client)
        
        print("\n" + "="*80)
        print("ALL TESTS COMPLETE")
        print("="*80)
        
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
