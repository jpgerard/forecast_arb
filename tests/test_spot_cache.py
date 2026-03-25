"""
Test spot cache functionality.
"""

import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from forecast_arb.ibkr.spot_cache import (
    make_cache_key,
    load_cached_spot,
    save_cached_spot,
    _get_cache_path
)


def test_make_cache_key():
    """Test cache key generation."""
    key = make_cache_key("SPY", "ARCA", "USD")
    assert key == "SPY|ARCA|USD"
    
    key2 = make_cache_key("QQQ", "NASDAQ", "USD")
    assert key2 == "QQQ|NASDAQ|USD"


def test_save_and_load_cache(tmp_path):
    """Test save and load roundtrip."""
    # Mock cache path to use temp directory
    cache_file = tmp_path / "test_cache.json"
    
    with patch("forecast_arb.ibkr.spot_cache._get_cache_path", return_value=cache_file):
        # Save spot
        key = make_cache_key("SPY", "ARCA", "USD")
        save_cached_spot(key, 450.50, 12345, "last")
        
        # Load it back
        cached = load_cached_spot(key, ttl_seconds=60)
        
        assert cached is not None
        assert cached["spot"] == 450.50
        assert cached["conId"] == 12345
        assert cached["source"] == "last"
        assert "timestamp" in cached


def test_cache_expiration(tmp_path):
    """Test that expired cache entries return None."""
    cache_file = tmp_path / "test_cache.json"
    
    with patch("forecast_arb.ibkr.spot_cache._get_cache_path", return_value=cache_file):
        key = make_cache_key("SPY", "ARCA", "USD")
        
        # Save spot
        save_cached_spot(key, 450.50, 12345, "last")
        
        # Load with very short TTL (0 seconds = immediate expiration)
        # But since we *just* saved it, need to manipulate the timestamp
        # Load the cache and modify timestamp
        with open(cache_file, 'r') as f:
            cache_data = json.load(f)
        
        # Set timestamp to 2 days ago
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        cache_data[key]["timestamp"] = old_time.isoformat()
        
        with open(cache_file, 'w') as f:
            json.dump(cache_data, f)
        
        # Now try to load with 1 day TTL - should be expired
        cached = load_cached_spot(key, ttl_seconds=24*60*60)
        assert cached is None


def test_cache_miss(tmp_path):
    """Test cache miss returns None."""
    cache_file = tmp_path / "test_cache.json"
    
    with patch("forecast_arb.ibkr.spot_cache._get_cache_path", return_value=cache_file):
        key = make_cache_key("MISSING", "NASDAQ", "USD")
        cached = load_cached_spot(key, ttl_seconds=60)
        assert cached is None


def test_invalid_spot_not_cached(tmp_path):
    """Test that invalid spots are not cached."""
    cache_file = tmp_path / "test_cache.json"
    
    with patch("forecast_arb.ibkr.spot_cache._get_cache_path", return_value=cache_file):
        key = make_cache_key("SPY", "ARCA", "USD")
        
        # Try to save None
        save_cached_spot(key, None, 12345, "last")
        cached = load_cached_spot(key, ttl_seconds=60)
        assert cached is None
        
        # Try to save zero
        save_cached_spot(key, 0, 12345, "last")
        cached = load_cached_spot(key, ttl_seconds=60)
        assert cached is None
        
        # Try to save negative
        save_cached_spot(key, -100, 12345, "last")
        cached = load_cached_spot(key, ttl_seconds=60)
        assert cached is None


def test_multiple_symbols(tmp_path):
    """Test caching multiple symbols."""
    cache_file = tmp_path / "test_cache.json"
    
    with patch("forecast_arb.ibkr.spot_cache._get_cache_path", return_value=cache_file):
        # Save multiple symbols
        key1 = make_cache_key("SPY", "ARCA", "USD")
        key2 = make_cache_key("QQQ", "NASDAQ", "USD")
        key3 = make_cache_key("IWM", "ARCA", "USD")
        
        save_cached_spot(key1, 450.50, 12345, "last")
        save_cached_spot(key2, 380.25, 23456, "midpoint")
        save_cached_spot(key3, 195.75, 34567, "last")
        
        # Load them back
        cached1 = load_cached_spot(key1, ttl_seconds=60)
        cached2 = load_cached_spot(key2, ttl_seconds=60)
        cached3 = load_cached_spot(key3, ttl_seconds=60)
        
        assert cached1["spot"] == 450.50
        assert cached2["spot"] == 380.25
        assert cached3["spot"] == 195.75
        
        assert cached1["source"] == "last"
        assert cached2["source"] == "midpoint"
        assert cached3["source"] == "last"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
