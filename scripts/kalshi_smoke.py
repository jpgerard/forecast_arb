"""
Kalshi Smoke Test - Deterministic validation of Kalshi Trade API connectivity.

Behavior:
1. Authenticate with Kalshi API
2. Fetch active markets (GET /markets?limit=20)
3. Print market metadata (ticker, title, close_time)
4. Select first active binary market
5. Fetch orderbook and compute midpoint probability
6. Emit structured JSON result with assertions

Hard Assertions:
- 0 < p_event < 1
- confidence > 0
- Fails explicitly if no markets or no orderbook

This is connectivity + correctness validation ONLY.
Market mapping (SPY ↔ SPX) is explicitly out of scope.
"""

import sys
import json
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from datetime import datetime, timezone

from forecast_arb.kalshi.client import KalshiClient, BASE_URL


def setup_logging():
    """Configure logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )


def main():
    """Run Kalshi smoke test."""
    setup_logging()
    logger = logging.getLogger(__name__)
    
    print("=" * 80)
    print("KALSHI SMOKE TEST - Deterministic Validation")
    print("=" * 80)
    print()
    print(f"Base URL: {BASE_URL}")
    print()
    
    # Step 1: Initialize client
    print("Step 1: Authenticating with Kalshi API...")
    try:
        client = KalshiClient()
        print(f"  ✓ Base URL: {client.base_url}")
        print(f"  ✓ API Key: ***{client.api_key[-4:]}")
        print(f"  ✓ Private Key: Loaded")
        print(f"  ✓ Authentication configured")
    except ValueError as e:
        print(f"  ✗ Configuration error: {e}")
        print()
        print("REQUIRED:")
        print("  1. Set KALSHI_API_KEY_ID in .env")
        print("  2. Set KALSHI_PRIVATE_KEY_PATH in .env pointing to your RSA private key")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"  ✗ Private key file not found: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Failed to initialize client: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print()
    
    # Step 2: Fetch active markets
    print("Step 2: Fetching active markets (GET /markets?status=active&limit=20)...")
    try:
        markets = client.list_markets(status="active", limit=20)
        print(f"  ✓ Retrieved {len(markets)} active markets")
        
        if not markets:
            print("  ✗ No active markets available")
            print()
            print("ERROR: Cannot proceed without active markets.")
            print("This may indicate:")
            print("  - Authentication failure")
            print("  - API endpoint issue")
            print("  - No markets currently open")
            sys.exit(1)
        
        print()
        print("  Markets retrieved:")
        for i, market in enumerate(markets[:5]):
            ticker = market.get("ticker", "N/A")
            title = market.get("title", "N/A")[:70]
            close_time = market.get("close_time", "N/A")
            volume = market.get("volume_24h", 0)
            print(f"    {i+1}. {ticker}")
            print(f"       {title}")
            print(f"       Close: {close_time}")
            print(f"       Volume: {volume:,}")
        
        if len(markets) > 5:
            print(f"    ... and {len(markets) - 5} more")
            
    except RuntimeError as e:
        print(f"  ✗ Kalshi API error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print()
    
    # Step 3: Find first active binary market with orderbook data
    print("Step 3: Finding market with active orderbook...")
    
    test_market = None
    market_ticker = None
    market_title = None
    orderbook = None
    yes_bid = None
    yes_ask = None
    
    for i, market in enumerate(markets):
        ticker = market.get("ticker")
        print(f"  Checking market {i+1}/{len(markets)}: {ticker}...")
        
        try:
            ob = client.get_orderbook(ticker)
            bid = ob.get("yes", {}).get("bid")
            ask = ob.get("yes", {}).get("ask")
            
            if bid is not None and ask is not None:
                # Found a market with orderbook data!
                test_market = market
                market_ticker = ticker
                market_title = market.get("title", "N/A")
                orderbook = ob
                yes_bid = bid
                yes_ask = ask
                print(f"  ✓ Found market with active orderbook: {ticker}")
                break
            else:
                print(f"    No bid/ask data (inactive)")
        except Exception as e:
            print(f"    Error fetching orderbook: {e}")
    
    if test_market is None:
        print()
        print("  ✗ No markets with active orderbook data found")
        print()
        print("ERROR: Cannot proceed without a market with bid/ask data.")
        print("This may indicate:")
        print("  - All markets currently have low liquidity")
        print("  - Market close times approaching")
        print("  - Try again later when markets are more active")
        sys.exit(1)
    
    print()
    print(f"  Selected: {market_ticker}")
    print(f"  Title: {market_title[:80]}")
    print()
    
    print("Step 4: Computing probability from orderbook...")
    
    try:
        # Compute midpoint probability
        p_yes = (yes_bid + yes_ask) / 2.0
        spread_cents = (yes_ask - yes_bid) * 100
        
        print("  Orderbook Data:")
        print(f"    YES Bid: {yes_bid:.4f}")
        print(f"    YES Ask: {yes_ask:.4f}")
        print(f"    Midpoint: {p_yes:.4f}")
        print(f"    Spread: {spread_cents:.2f} cents")
        
    except RuntimeError as e:
        print(f"  ✗ API error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    print()
    
    # Step 5: Compute confidence and emit structured result
    print("Step 5: Computing confidence and validating assertions...")
    
    # Compute confidence based on bid/ask sizes
    yes_bid_size = orderbook.get("yes", {}).get("bid_size", 0)
    yes_ask_size = orderbook.get("yes", {}).get("ask_size", 0)
    
    warnings = []
    
    if yes_bid_size > 0 or yes_ask_size > 0:
        # confidence = min(1.0, (bid_size + ask_size) / 100.0)
        confidence = min(1.0, (yes_bid_size + yes_ask_size) / 100.0)
        print(f"  Confidence: {confidence:.2f} (based on bid_size={yes_bid_size} + ask_size={yes_ask_size})")
    else:
        # Size data unavailable, default to 0.5
        confidence = 0.5
        warnings.append("Bid/ask size data unavailable, using default confidence=0.5")
        print(f"  Confidence: {confidence:.2f} (default - size data unavailable)")
    print()
    
    # Hard assertions
    print("  Running hard assertions...")
    try:
        assert 0 < p_yes < 1, f"Assertion failed: 0 < p_event < 1 (got {p_yes})"
        print(f"    ✓ 0 < p_event < 1: {p_yes:.4f}")
        
        assert confidence > 0, f"Assertion failed: confidence > 0 (got {confidence})"
        print(f"    ✓ confidence > 0: {confidence:.2f}")
        
    except AssertionError as e:
        print(f"  ✗ {e}")
        sys.exit(1)
    
    print()
    
    # Emit structured JSON result
    timestamp = datetime.now(timezone.utc).isoformat()
    
    result = {
        "source": "kalshi",
        "p_event": p_yes,
        "confidence": confidence,
        "market_ticker": market_ticker,
        "timestamp": timestamp
    }
    
    # Add warnings if any
    if warnings:
        print()
        print("  Warnings:")
        for warning in warnings:
            print(f"    - {warning}")
    
    print("Step 6: Structured Result (JSON):")
    print()
    print(json.dumps(result, indent=2))
    print()
    
    # Final validation
    print("=" * 80)
    print("✅ KALSHI SMOKE TEST PASSED")
    print("=" * 80)
    print()
    print("Summary:")
    print(f"  ✓ Base URL: {BASE_URL}")
    print(f"  ✓ Authentication: RSA-PSS SHA256 with UNIX timestamp")
    print(f"  ✓ Markets retrieved: {len(markets)}")
    print(f"  ✓ Orderbook fetched: {market_ticker}")
    print(f"  ✓ Probability computed: {p_yes:.2%}")
    print(f"  ✓ Confidence: {confidence:.2f}")
    print(f"  ✓ All assertions passed")
    print()
    print("Next steps:")
    print("  - Wire into p_event_source system")
    print("  - Add market mapping (SPY ↔ SPX, etc.)")
    print("  - Implement event_def translation")
    print()


if __name__ == "__main__":
    main()
