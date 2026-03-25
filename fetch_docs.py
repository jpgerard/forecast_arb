"""Fetch Kalshi API documentation for review."""
import requests

# Try to fetch the API documentation
try:
    print("Fetching Kalshi API documentation...")
    response = requests.get("https://docs.kalshi.com/welcome", timeout=10)
    print(f"Status: {response.status_code}")
    print("\n" + "="*60)
    print(response.text[:5000])  # Print first 5000 chars
    print("="*60)
    
    # Also check the API reference
    print("\n\nFetching API Authentication docs...")
    auth_response = requests.get("https://docs.kalshi.com/authentication", timeout=10)
    print(f"Status: {auth_response.status_code}")
    print("\n" + "="*60)
    print(auth_response.text[:5000])
    print("="*60)
    
except Exception as e:
    print(f"Error: {e}")

# Try their API base URL
print("\n\nTrying to get API info from trading-api.kalshi.com...")
try:
    # Try without authentication first
    response = requests.get("https://trading-api.kalshi.com/trade-api/v2/markets", 
                          params={"status": "open", "limit": 1}, 
                          timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")
