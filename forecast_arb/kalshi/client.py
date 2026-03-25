"""
Minimal REST client for Kalshi API.

Implements RSA-PSS SHA256 signature authentication as required by Kalshi API.
"""

import os
import time
import hashlib
import base64
from typing import Any, Dict, List, Optional
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# Official Kalshi Trade API base URL - PRODUCTION endpoint (serves all available markets)
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    """Minimal REST client for Kalshi prediction markets API."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key_path: Optional[str] = None,
        private_key_str: Optional[str] = None,
        rate_limit_per_second: int = 10
    ):
        """
        Initialize Kalshi client with RSA-PSS authentication.
        
        Args:
            api_key: API key ID (defaults to KALSHI_API_KEY_ID env var)
            private_key_path: Path to RSA private key file (defaults to KALSHI_PRIVATE_KEY_PATH env var)
            private_key_str: RSA private key as string (defaults to KALSHI_PRIVATE_KEY env var)
            rate_limit_per_second: Rate limit for API calls
        """
        # Use official Kalshi Trade API endpoint
        self.base_url = BASE_URL
        
        # Use environment variables if not explicitly provided
        self.api_key = api_key or os.getenv("KALSHI_API_KEY_ID")
        self.private_key_path = private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH")
        self.private_key_str = private_key_str or os.getenv("KALSHI_PRIVATE_KEY")
        self.rate_limit = rate_limit_per_second
        self.last_request_time = 0.0
        self.private_key = None
        
        # Validate credentials
        if not self.api_key:
            raise ValueError("KALSHI_API_KEY_ID must be set in environment or provided as argument")
        
        # Either private key path or private key string must be provided
        if not self.private_key_path and not self.private_key_str:
            raise ValueError(
                "Either KALSHI_PRIVATE_KEY_PATH (file path) or KALSHI_PRIVATE_KEY (key string) "
                "must be set in environment or provided as argument"
            )
        
        # Load private key (required for authentication)
        self._load_private_key()
        
    def _load_private_key(self):
        """Load RSA private key from file or string."""
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend
            
            # Try loading from string first (if provided)
            if self.private_key_str:
                # Handle potential quotes around the key string
                key_str = self.private_key_str.strip().strip('"').strip("'")
                
                # Ensure proper line breaks if they were escaped
                if "\\n" in key_str:
                    key_str = key_str.replace("\\n", "\n")
                
                key_bytes = key_str.encode('utf-8')
                self.private_key = serialization.load_pem_private_key(
                    key_bytes,
                    password=None,
                    backend=default_backend()
                )
            # Otherwise load from file
            elif self.private_key_path:
                if not os.path.exists(self.private_key_path):
                    raise FileNotFoundError(f"Private key file not found: {self.private_key_path}")
                
                with open(self.private_key_path, 'rb') as f:
                    self.private_key = serialization.load_pem_private_key(
                        f.read(),
                        password=None,
                        backend=default_backend()
                    )
            else:
                raise ValueError("No private key source provided")
                
        except ImportError:
            raise ImportError(
                "cryptography package required for authenticated Kalshi requests. "
                "Install with: pip install cryptography"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to load private key: {e}")
    
    def _sign_request(self, timestamp_str: str, method: str, path: str) -> str:
        """
        Generate RSA-PSS SHA256 signature for request.
        
        Args:
            timestamp_str: Request timestamp as string (UNIX seconds)
            method: HTTP method (GET, POST, etc.)
            path: Request path (without query string)
            
        Returns:
            Base64-encoded signature
        """
        if self.private_key is None:
            raise RuntimeError("Private key not loaded - cannot sign request")
        
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        
        # Create message: timestamp + method + path (no query string)
        message = f"{timestamp_str}{method}{path}"
        message_bytes = message.encode('utf-8')
        
        # Sign with RSA-PSS
        signature = self.private_key.sign(
            message_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        
        # Return base64-encoded signature
        return base64.b64encode(signature).decode('utf-8')
    
    def _rate_limit_wait(self):
        """Enforce rate limiting."""
        if self.rate_limit > 0:
            min_interval = 1.0 / self.rate_limit
            elapsed = time.time() - self.last_request_time
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        self.last_request_time = time.time()
    
    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Make authenticated GET request to Kalshi API.
        
        Args:
            endpoint: API endpoint path (e.g., "/trade-api/v2/markets")
            params: Query parameters
            
        Returns:
            JSON response as dict
        """
        self._rate_limit_wait()
        
        # Normalize endpoint - remove leading /trade-api/v2/ if present (since it's in BASE_URL)
        if endpoint.startswith("/trade-api/v2/"):
            endpoint = "/" + endpoint.split("/trade-api/v2/", 1)[1]
        elif not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        
        url = f"{self.base_url}{endpoint}"
        
        # Authentication is mandatory - build headers
        timestamp_str = str(int(time.time()))
        
        # Sign request (path only, no query string)
        signature = self._sign_request(timestamp_str, "GET", endpoint)
        
        headers = {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_str,
        }
        
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Kalshi API request failed: {e}. URL: {url}") from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Kalshi API network error: {e}") from e
    
    def list_markets(
        self,
        series: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        tickers: Optional[List[str]] = None,
        status: Optional[List[str]] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        List markets matching filters.
        
        NOTE: Kalshi API does not support server-side status filtering.
        All markets are fetched and filtered client-side by status.
        
        Args:
            series: Filter by series IDs
            tags: Filter by tags
            tickers: Filter by ticker symbols
            status: List of market statuses to filter by (e.g., ["active"]) or None for all
            limit: Maximum number of markets to return
            
        Returns:
            List of market dicts filtered by status
        """
        # Build query parameters (no status parameter - API doesn't support it)
        params: Dict[str, Any] = {"limit": limit}
        
        if series:
            params["series_ticker"] = ",".join(series)
        if tags:
            params["tags"] = ",".join(tags)
        if tickers:
            params["ticker"] = ",".join(tickers)
        
        # Fetch all markets
        response = self._get("/markets", params)
        markets = response.get("markets", [])
        
        # Client-side filtering by status
        if status is None:
            # Return all markets
            return markets
        
        # Filter by status list
        if isinstance(status, list):
            # Filter markets to only those with matching status
            filtered_markets = [
                market for market in markets
                if market.get("status") in status
            ]
            return filtered_markets
        
        # Legacy: if status is a string, treat as single-element list
        if isinstance(status, str):
            filtered_markets = [
                market for market in markets
                if market.get("status") == status
            ]
            return filtered_markets
        
        raise ValueError(f"Invalid status type: {type(status)}. Expected list, str, or None.")
    
    def get_market(self, market_id: str) -> Dict[str, Any]:
        """
        Get detailed information for a specific market.
        
        Args:
            market_id: Market identifier (ticker)
            
        Returns:
            Market details dict
        """
        response = self._get(f"/markets/{market_id}")
        return response.get("market", {})
    
    def get_orderbook(self, market_id: str) -> Dict[str, Any]:
        """
        Get current orderbook for a market.
        
        Args:
            market_id: Market identifier (ticker)
            
        Returns:
            Dict with best bid/ask for YES and NO:
            {
                "yes": {"bid": float, "ask": float, "bid_size": int, "ask_size": int},
                "no": {"bid": float, "ask": float, "bid_size": int, "ask_size": int}
            }
        """
        response = self._get(f"/markets/{market_id}/orderbook")
        orderbook = response.get("orderbook", {})
        
        # Parse orderbook to get best bid/ask
        yes_bids = orderbook.get("yes", [])
        no_bids = orderbook.get("no", [])
        
        result = {
            "yes": {
                "bid": yes_bids[0][0] / 100.0 if yes_bids else None,
                "ask": None,  # Derive from NO side
                "bid_size": yes_bids[0][1] if yes_bids else 0,
                "ask_size": 0
            },
            "no": {
                "bid": no_bids[0][0] / 100.0 if no_bids else None,
                "ask": None,
                "bid_size": no_bids[0][1] if no_bids else 0,
                "ask_size": 0
            }
        }
        
        # YES ask = 1 - NO bid (if available)
        if result["no"]["bid"] is not None:
            result["yes"]["ask"] = 1.0 - result["no"]["bid"]
            
        # NO ask = 1 - YES bid (if available)
        if result["yes"]["bid"] is not None:
            result["no"]["ask"] = 1.0 - result["yes"]["bid"]
            
        return result
    
    def get_trades(
        self,
        market_id: str,
        since: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get recent trades for a market.
        
        Args:
            market_id: Market identifier (ticker)
            since: ISO8601 timestamp to get trades since
            limit: Maximum number of trades to return
            
        Returns:
            List of trade dicts
        """
        params: Dict[str, Any] = {"limit": limit}
        if since:
            params["min_ts"] = since
        
        response = self._get(f"/markets/{market_id}/trades", params)
        return response.get("trades", [])
    
    def get_market_volume_24h(self, market_id: str) -> int:
        """
        Get 24-hour trading volume for a market.
        
        Args:
            market_id: Market identifier (ticker)
            
        Returns:
            24-hour volume (number of contracts)
        """
        market = self.get_market(market_id)
        return market.get("volume_24h", 0)
