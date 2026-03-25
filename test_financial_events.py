"""
Test /events endpoint with Financials category to find actual financial markets.
"""

import os
import json
from forecast_arb.kalshi.client import KalshiClient


def main():
    """Query Financials category via /events endpoint."""
    print("\n" + "="*80)
    print("QUERYING FINANCIALS CATEGORY VIA /EVENTS")
    print("="*80)
    
    client = KalshiClient()
    
    # Query events with Financials category
    try:
        response = client._get("/events", params={
            "limit": 1000,
            "status": "open",
            "category": "Financials"
        })
        events = response.get("events", [])
        print(f"\n🎯 Found {len(events)} Financial events (status=open)")
        
        if events:
            print("\nSample Financial Events:")
            for e in events[:10]:
                print(f"\n  Event: {e.get('event_ticker')}")
                print(f"  Title: {e.get('title')}")
                print(f"  Series: {e.get('series_ticker')}")
                print(f"  Category: {e.get('category')}")
                print(f"  Markets: {len(e.get('markets', []))}")
                
                # Check if markets are embedded
                markets = e.get('markets', [])
                if markets:
                    print(f"  First market ticker: {markets[0].get('ticker', 'N/A')}")
        else:
            print("❌ No financial events found with status=open")
            
    except Exception as e:
        print(f"Error: {e}")
    
    # Try without status filter
    print("\n" + "="*80)
    print("TRYING WITHOUT STATUS FILTER")
    print("="*80)
    
    try:
        response = client._get("/events", params={
            "limit": 1000,
            "category": "Financials"
        })
        events = response.get("events", [])
        print(f"\n🎯 Found {len(events)} Financial events (any status)")
        
        if events:
            print("\nSample Financial Events:")
            for e in events[:10]:
                print(f"\n  Event: {e.get('event_ticker')}")
                print(f"  Title: {e.get('title')}")
                print(f"  Series: {e.get('series_ticker')}")
                print(f"  Status: {e.get('status')}")
                print(f"  Markets: {len(e.get('markets', []))}")
                
    except Exception as e:
        print(f"Error: {e}")
    
    # Query specific S&P 500 series
    print("\n" + "="*80)
    print("QUERYING SPECIFIC SPX SERIES")
    print("="*80)
    
    spx_series = ['KXINXW', 'KXINX', 'INX', 'INXZ', 'INXM', 'INXU', 'INXW']
    
    for series_ticker in spx_series:
        try:
            response = client._get(f"/series/{series_ticker}")
            series_data = response.get("series", {})
            
            if series_data:
                print(f"\n🎯 Series: {series_ticker}")
                print(f"  Title: {series_data.get('title')}")
                print(f"  Category: {series_data.get('category')}")
                print(f"  Frequency: {series_data.get('frequency')}")
                
                # Try to get markets for this series
                markets_response = client.list_markets(series=[series_ticker], status="open", limit=100)
                print(f"  Open markets: {len(markets_response)}")
                
                if markets_response:
                    for m in markets_response[:3]:
                        print(f"    - {m.get('ticker')}: {m.get('title')}")
            else:
                print(f"❌ Series {series_ticker} not found")
                
        except Exception as e:
            print(f"❌ Series {series_ticker}: {e}")
    
    print("\n" + "="*80)
    print("COMPLETE")
    print("="*80)


if __name__ == "__main__":
    main()
