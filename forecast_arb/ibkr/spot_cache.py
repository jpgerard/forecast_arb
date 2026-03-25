"""
Spot Cache - Last Known Good Spot Price Cache

Disk-backed JSON cache for storing last-known-good spot prices.
Used as fallback when live quotes are unavailable or fail sanity checks.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _get_cache_path() -> Path:
    """
    Get the cache file path.
    
    Uses ~/.forecast_arb/cache/spot_cache.json
    
    Returns:
        Path to cache file
    """
    home = Path.home()
    cache_dir = home / ".forecast_arb" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "spot_cache.json"


def _load_cache_file() -> dict:
    """
    Load the entire cache file.
    
    Returns:
        Cache dict (empty if file doesn't exist or is invalid)
    """
    cache_path = _get_cache_path()
    
    if not cache_path.exists():
        return {}
    
    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("Cache file is not a dict, ignoring")
                return {}
            return data
    except Exception as e:
        logger.warning(f"Failed to load cache file: {e}")
        return {}


def _save_cache_file(cache: dict):
    """
    Save the entire cache file.
    
    Args:
        cache: Cache dict to save
    """
    cache_path = _get_cache_path()
    
    try:
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save cache file: {e}")


def make_cache_key(symbol: str, primary_exchange: str, currency: str) -> str:
    """
    Create a cache key from symbol, exchange, and currency.
    
    Args:
        symbol: Stock symbol (e.g., "SPY")
        primary_exchange: Primary exchange (e.g., "ARCA")
        currency: Currency (e.g., "USD")
        
    Returns:
        Cache key string (e.g., "SPY|ARCA|USD")
    """
    return f"{symbol}|{primary_exchange}|{currency}"


def load_cached_spot(cache_key: str, ttl_seconds: int) -> Optional[dict]:
    """
    Load a cached spot price if it exists and is not expired.
    
    Args:
        cache_key: Cache key from make_cache_key()
        ttl_seconds: Time-to-live in seconds (default: 2 trading days = 48 hours)
        
    Returns:
        Dict with spot data if valid, None if not found or expired:
        {
            "spot": float,
            "timestamp": str (ISO8601),
            "source": str,
            "conId": int
        }
    """
    cache = _load_cache_file()
    
    if cache_key not in cache:
        logger.debug(f"Cache miss: {cache_key}")
        return None
    
    entry = cache[cache_key]
    
    # Validate entry structure
    required_fields = ["spot", "timestamp", "source", "conId"]
    if not all(field in entry for field in required_fields):
        logger.warning(f"Cache entry missing required fields: {cache_key}")
        return None
    
    # Check TTL
    try:
        cached_time = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_seconds = (now - cached_time).total_seconds()
        
        if age_seconds > ttl_seconds:
            logger.debug(f"Cache expired: {cache_key} (age: {age_seconds:.0f}s, ttl: {ttl_seconds}s)")
            return None
        
        logger.info(f"Cache hit: {cache_key} (age: {age_seconds:.0f}s, spot: ${entry['spot']:.2f})")
        return entry
        
    except Exception as e:
        logger.warning(f"Failed to parse cache timestamp: {e}")
        return None


def save_cached_spot(
    cache_key: str,
    spot: float,
    conId: int,
    source: str
):
    """
    Save a spot price to the cache.
    
    Args:
        cache_key: Cache key from make_cache_key()
        spot: Spot price
        conId: IBKR contract ID
        source: Source of spot (e.g., "last", "midpoint")
    """
    if spot is None or spot <= 0:
        logger.warning(f"Refusing to cache invalid spot: {spot}")
        return
    
    cache = _load_cache_file()
    
    entry = {
        "spot": spot,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "conId": conId
    }
    
    cache[cache_key] = entry
    _save_cache_file(cache)
    
    logger.info(f"Cached spot: {cache_key} -> ${spot:.2f} (source: {source})")
