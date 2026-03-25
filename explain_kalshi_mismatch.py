"""
Explain why Kalshi auto-mapping didn't find a match.
Shows what Kalshi offers vs what the strategy needs.
"""

import os
from datetime import datetime, date
from forecast_arb.kalshi.client import KalshiClient

# What the strategy needed (from last run)
NEEDED_EXPIRY = date(2026, 3, 20)  # March 20, 2026
NEEDED_SPX_LEVEL = 5919.23  # SPX @ 6963.80 * (1 - 0.15) = 5919.23
NEEDED_EVENT = f"P(SPX < ${NEEDED_SPX_LEVEL:.2f} by {NEEDED_EXPIRY})"

print("=" * 80)
print("KALSHI AUTO-MAPPING MISMATCH ANALYSIS")
print("=" * 80)
print()
print("WHAT THE STRATEGY NEEDS:")
print(f"  Event: {NEEDED_EVENT}")
print(f"  Expiry: {NEEDED_EXPIRY} (45 days from now)")
print(f"  Target Level: ${NEEDED_SPX_LEVEL:.2f}")
print()

# Get what Kalshi actually has
try:
    client = KalshiClient()
    markets = client.list_markets(series=["KXINX"], status="open", limit=200)
    
    print("=" * 80)
    print(f"WHAT KALSHI OFFERS: {len(markets)} markets in KXINX series")
    print("=" * 80)
    
    if not markets:
        print("❌ No markets found!")
    else:
        # Group by date
        by_date = {}
        for m in markets:
            close_time = m.get('close_time', '')
            if close_time:
                # Parse date from close_time
                try:
                    dt = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
                    market_date = dt.date()
                    if market_date not in by_date:
                        by_date[market_date] = []
                    by_date[market_date].append(m)
                except:
                    pass
        
        print(f"\nMarkets available by expiry date:")
        for expiry_date in sorted(by_date.keys()):
            count = len(by_date[expiry_date])
            days_from_now = (expiry_date - date.today()).days
            
            # Show if this matches what we need
            match_indicator = "✅ MATCH!" if expiry_date == NEEDED_EXPIRY else f"  ({days_from_now} days from now)"
            
            print(f"\n  {expiry_date}: {count} markets {match_indicator}")
            
            # Show sample strikes for this date
            sample_markets = by_date[expiry_date][:5]
            strikes = []
            for m in sample_markets:
                title = m.get('title', '')
                # Try to extract level from title
                import re
                # Look for numbers like "7274.9999" or "6575"
                numbers = re.findall(r'\b(\d{4,5}(?:\.\d+)?)\b', title)
                if numbers:
                    for num_str in numbers:
                        try:
                            level = float(num_str)
                            if 5000 < level < 9000:  # Plausible SPX level
                                strikes.append(level)
                                break
                        except:
                            pass
            
            if strikes:
                print(f"    Sample strikes: {', '.join(f'${s:.0f}' for s in sorted(set(strikes))[:5])}")
                
                # Check if our target is in range
                if strikes:
                    min_strike = min(strikes)
                    max_strike = max(strikes)
                    if expiry_date == NEEDED_EXPIRY:
                        if min_strike <= NEEDED_SPX_LEVEL <= max_strike:
                            print(f"    ✅ Target ${NEEDED_SPX_LEVEL:.2f} IS in range [${min_strike:.0f}, ${max_strike:.0f}]")
                        else:
                            print(f"    ❌ Target ${NEEDED_SPX_LEVEL:.2f} NOT in range [${min_strike:.0f}, ${max_strike:.0f}]")
    
    print()
    print("=" * 80)
    print("CONCLUSION")
    print("=" * 80)
    
    if NEEDED_EXPIRY not in by_date:
        print(f"❌ NO MATCH: Kalshi doesn't have markets expiring on {NEEDED_EXPIRY}")
        print(f"\n   Available dates: {', '.join(str(d) for d in sorted(by_date.keys())[:5])}")
        print(f"\n   Kalshi appears to offer same-day or very short-term markets only.")
        print(f"   Your strategy needs {(NEEDED_EXPIRY - date.today()).days}-day markets, but Kalshi has {(min(by_date.keys()) - date.today()).days}-day markets.")
    else:
        print(f"✅ DATE MATCH: Kalshi has markets for {NEEDED_EXPIRY}")
        print(f"   But check if strikes match target ${NEEDED_SPX_LEVEL:.2f}")

except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
