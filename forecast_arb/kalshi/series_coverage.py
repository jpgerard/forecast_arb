"""
Kalshi Series Coverage Manager

Provides coverage metadata (date ranges, threshold ranges) for Kalshi series
to enable fast prechecks in the mapper without querying the API.

Features:
- On-demand coverage computation
- 1-hour TTL caching
- Expiry range validation
"""

import json
import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.kalshi.threshold_parser import parse_threshold_from_market
from forecast_arb.kalshi.status_map import map_status


logger = logging.getLogger(__name__)


CACHE_FILE = "runs/.kalshi_series_coverage_cache.json"
CACHE_TTL_SECONDS = 3600  # 1 hour


def parse_event_date(market: Dict[str, Any]) -> Optional[date]:
    """Parse event date from market."""
    # Try close_time first
    close_time_str = market.get("close_time")
    if close_time_str:
        try:
            dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            return dt.date()
        except (ValueError, AttributeError):
            pass
    
    # Try event_date
    event_date_str = market.get("event_date")
    if event_date_str:
        try:
            if "T" in event_date_str:
                dt = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                return dt.date()
            else:
                return datetime.fromisoformat(event_date_str).date()
        except (ValueError, AttributeError):
            pass
    
    return None


class SeriesCoverageManager:
    """Manages coverage metadata for Kalshi series with caching."""
    
    def __init__(
        self,
        cache_file: str = CACHE_FILE,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS
    ):
        """
        Initialize coverage manager.
        
        Args:
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL in seconds
        """
        self.cache_file = cache_file
        self.cache_ttl_seconds = cache_ttl_seconds
        self._coverage_cache: Optional[Dict[str, Any]] = None
    
    def get_coverage(
        self,
        series_list: List[str],
        status: str = "open",
        limit: int = 500,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Get coverage metadata for series (with caching).
        
        Args:
            series_list: List of series tickers
            status: Market status filter
            limit: Max markets per series
            force_refresh: If True, bypass cache
        
        Returns:
            Coverage dict per series
        """
        # Check cache first
        if not force_refresh:
            cached = self._load_cache()
            if cached:
                # Validate cache covers requested series
                cached_series = set(cached.get("coverage", {}).keys())
                requested_series = set(series_list)
                if requested_series.issubset(cached_series):
                    logger.info("Using cached coverage data")
                    return cached["coverage"]
        
        # Compute fresh coverage
        logger.info(f"Computing fresh coverage for series: {series_list}")
        coverage = self._compute_coverage(series_list, status, limit)
        
        # Save to cache
        self._save_cache(coverage, series_list)
        
        return coverage
    
    def check_expiry_coverage(
        self,
        series: str,
        target_expiry: date,
        coverage: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Check if a series covers a target expiry date.
        
        Args:
            series: Series ticker
            target_expiry: Target expiry date
            coverage: Pre-computed coverage dict (if None, will compute)
        
        Returns:
            Dict with:
            {
                "covers": bool,
                "reason": str,
                "series_min_date": str or None,
                "series_max_date": str or None,
                "target_expiry": str
            }
        """
        # Get coverage if not provided
        if coverage is None:
            coverage = self.get_coverage([series])
        
        # Extract series metrics
        series_metrics = coverage.get(series, {})
        
        if "error" in series_metrics:
            return {
                "covers": False,
                "reason": f"SERIES_ERROR: {series_metrics['error']}",
                "series_min_date": None,
                "series_max_date": None,
                "target_expiry": str(target_expiry)
            }
        
        min_date_str = series_metrics.get("min_date")
        max_date_str = series_metrics.get("max_date")
        
        if not min_date_str or not max_date_str:
            return {
                "covers": False,
                "reason": "NO_DATES_AVAILABLE",
                "series_min_date": None,
                "series_max_date": None,
                "target_expiry": str(target_expiry)
            }
        
        # Parse dates
        try:
            min_date = datetime.fromisoformat(min_date_str).date()
            max_date = datetime.fromisoformat(max_date_str).date()
        except ValueError:
            return {
                "covers": False,
                "reason": "INVALID_DATE_FORMAT",
                "series_min_date": min_date_str,
                "series_max_date": max_date_str,
                "target_expiry": str(target_expiry)
            }
        
        # Check if target is in range
        if target_expiry < min_date or target_expiry > max_date:
            return {
                "covers": False,
                "reason": "EXPIRY_OUT_OF_RANGE",
                "series_min_date": min_date_str,
                "series_max_date": max_date_str,
                "target_expiry": str(target_expiry)
            }
        
        # Covered!
        return {
            "covers": True,
            "reason": "OK",
            "series_min_date": min_date_str,
            "series_max_date": max_date_str,
            "target_expiry": str(target_expiry)
        }
    
    def _compute_coverage(
        self,
        series_list: List[str],
        status: str,
        limit: int
    ) -> Dict[str, Any]:
        """Compute coverage for series list."""
        client = KalshiClient()
        results = {}
        
        for series in series_list:
            logger.info(f"Fetching markets for series: {series}")
            
            try:
                # Map user-facing status to API status(es)
                api_status = map_status(status)
                markets = client.list_markets(
                    series=[series],
                    status=api_status,
                    limit=limit
                )
                
                logger.info(f"  Fetched {len(markets)} markets")
                
                # Parse markets
                event_dates = set()
                kind_counts = {"point": 0, "range": 0, "unknown": 0}
                point_thresholds = []
                range_lows = []
                range_highs = []
                
                for market in markets:
                    # Parse date
                    event_date = parse_event_date(market)
                    if event_date:
                        event_dates.add(event_date)
                    
                    # Parse threshold
                    parsed = parse_threshold_from_market(market, series=series)
                    kind = parsed.get("kind", "unknown")
                    kind_counts[kind] += 1
                    
                    if kind == "point" and parsed.get("threshold") is not None:
                        point_thresholds.append(parsed["threshold"])
                    elif kind == "range":
                        if parsed.get("low") is not None:
                            range_lows.append(parsed["low"])
                        if parsed.get("high") is not None:
                            range_highs.append(parsed["high"])
                
                # Compute aggregates
                sorted_dates = sorted(event_dates) if event_dates else []
                
                results[series] = {
                    "status_filter": status,
                    "markets_fetched": len(markets),
                    "unique_event_dates": len(event_dates),
                    "min_date": str(sorted_dates[0]) if sorted_dates else None,
                    "max_date": str(sorted_dates[-1]) if sorted_dates else None,
                    "kind_counts": kind_counts,
                    "threshold_min": min(point_thresholds) if point_thresholds else None,
                    "threshold_max": max(point_thresholds) if point_thresholds else None,
                    "range_low_min": min(range_lows) if range_lows else None,
                    "range_high_max": max(range_highs) if range_highs else None
                }
                
            except Exception as e:
                logger.error(f"Failed to fetch markets for {series}: {e}")
                results[series] = {
                    "status_filter": status,
                    "error": str(e),
                    "markets_fetched": 0
                }
        
        return results
    
    def _load_cache(self) -> Optional[Dict[str, Any]]:
        """Load coverage from cache file if valid."""
        if not os.path.exists(self.cache_file):
            logger.debug("Cache file not found")
            return None
        
        try:
            with open(self.cache_file, 'r') as f:
                cached = json.load(f)
            
            # Check TTL
            timestamp_str = cached.get("timestamp")
            if not timestamp_str:
                logger.debug("Cache missing timestamp")
                return None
            
            timestamp = datetime.fromisoformat(timestamp_str)
            age_seconds = (datetime.now() - timestamp).total_seconds()
            
            if age_seconds > self.cache_ttl_seconds:
                logger.info(f"Cache expired (age={age_seconds:.0f}s, ttl={self.cache_ttl_seconds}s)")
                return None
            
            logger.info(f"Cache valid (age={age_seconds:.0f}s)")
            return cached
            
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            return None
    
    def _save_cache(self, coverage: Dict[str, Any], series_list: List[str]) -> None:
        """Save coverage to cache file."""
        try:
            # Ensure directory exists
            Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
            
            cache_data = {
                "timestamp": datetime.now().isoformat(),
                "series_list": series_list,
                "coverage": coverage
            }
            
            with open(self.cache_file, 'w') as f:
                json.dump(cache_data, f, indent=2)
            
            logger.info(f"Coverage cached to {self.cache_file}")
            
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")


# Global singleton instance
_coverage_manager: Optional[SeriesCoverageManager] = None


def get_coverage_manager() -> SeriesCoverageManager:
    """Get global coverage manager instance."""
    global _coverage_manager
    if _coverage_manager is None:
        _coverage_manager = SeriesCoverageManager()
    return _coverage_manager
