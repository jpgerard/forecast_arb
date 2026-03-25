"""
Regime signal fetching and percentile computation.

This module fetches market regime indicators (VIX, skew, credit spreads) and computes
their percentile ranks vs historical lookback periods.

All functions are designed to be safe:
- Never crash on API failures
- Return None if data unavailable
- Log warnings for debugging
"""

import logging
import json
from typing import Optional, Dict
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

logger = logging.getLogger(__name__)

# Cache file for VIX data (session-level caching)
VIX_CACHE_FILE = Path("runs/.vix_cache.json")
VIX_CACHE_TTL_SECONDS = 3600  # 1 hour cache


def _load_vix_cache() -> Optional[Dict]:
    """Load VIX cache if valid."""
    if not VIX_CACHE_FILE.exists():
        return None
    
    try:
        with open(VIX_CACHE_FILE, "r") as f:
            cache = json.load(f)
        
        # Check if cache is still valid
        cached_time = datetime.fromisoformat(cache["timestamp"])
        age_seconds = (datetime.now() - cached_time).total_seconds()
        
        if age_seconds < VIX_CACHE_TTL_SECONDS:
            logger.info(f"Using cached VIX data (age: {age_seconds:.0f}s)")
            return cache
        else:
            logger.info(f"VIX cache expired (age: {age_seconds:.0f}s > {VIX_CACHE_TTL_SECONDS}s)")
            return None
    except Exception as e:
        logger.warning(f"Failed to load VIX cache: {e}")
        return None


def _save_vix_cache(vix_value: float, history: list):
    """Save VIX data to cache."""
    try:
        VIX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache = {
            "timestamp": datetime.now().isoformat(),
            "vix_value": vix_value,
            "history": history
        }
        with open(VIX_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"VIX data cached to {VIX_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save VIX cache: {e}")


def _fetch_vix_from_ibkr() -> Optional[tuple[float, list]]:
    """
    Fetch VIX from IBKR.
    
    Returns:
        Tuple of (current_vix, historical_values) or None if unavailable
    """
    try:
        from ib_insync import IB, Index
        
        # Connect to IBKR
        ib = IB()
        try:
            ib.connect("127.0.0.1", 7496, clientId=999, readonly=True, timeout=10)
            logger.info("Connected to IBKR for VIX fetch")
            
            # Create VIX index contract
            vix_contract = Index("VIX", "CBOE")
            
            # Qualify contract
            qualified = ib.qualifyContracts(vix_contract)
            if not qualified:
                logger.warning("Failed to qualify VIX contract")
                return None
            
            # Request current market data
            ticker = ib.reqMktData(qualified[0], "", snapshot=True)
            ib.sleep(2)
            
            # Get current VIX value
            current_vix = None
            if ticker.last and ticker.last > 0:
                current_vix = ticker.last
            elif ticker.close and ticker.close > 0:
                current_vix = ticker.close
            
            if current_vix is None:
                logger.warning("No valid VIX price from IBKR")
                ib.cancelMktData(qualified[0])
                return None
            
            logger.info(f"Current VIX from IBKR: {current_vix:.2f}")
            
            # Request historical data (daily bars, 1 year)
            bars = ib.reqHistoricalData(
                qualified[0],
                endDateTime='',
                durationStr='1 Y',
                barSizeSetting='1 day',
                whatToShow='TRADES',
                useRTH=True
            )
            
            ib.cancelMktData(qualified[0])
            
            if not bars or len(bars) < 20:
                logger.warning(f"Insufficient VIX historical data: {len(bars) if bars else 0} bars")
                return None
            
            # Extract close prices
            history = [bar.close for bar in bars]
            logger.info(f"Fetched {len(history)} days of VIX history from IBKR")
            
            return (current_vix, history)
            
        finally:
            if ib.isConnected():
                ib.disconnect()
                logger.info("Disconnected from IBKR")
    
    except ImportError:
        logger.warning("ib_insync not installed, cannot fetch VIX from IBKR")
        return None
    except Exception as e:
        logger.warning(f"Failed to fetch VIX from IBKR: {e}")
        return None


def get_vix_percentile(lookback_days: int = 252) -> Optional[float]:
    """
    Fetch VIX and compute percentile vs lookback period.
    
    Uses IBKR as primary source with session-level caching.
    
    Args:
        lookback_days: Historical lookback period (default 1 year = 252 trading days)
        
    Returns:
        Percentile rank [0, 1], or None if unavailable
    """
    # Try cache first
    cached = _load_vix_cache()
    if cached:
        current_vix = cached["vix_value"]
        history = cached["history"]
    else:
        # Fetch from IBKR
        result = _fetch_vix_from_ibkr()
        if result is None:
            logger.warning("VIX signal unavailable (IBKR fetch failed)")
            return None
        
        current_vix, history = result
        
        # Save to cache
        _save_vix_cache(current_vix, history)
    
    # Compute percentile
    lookback_values = np.array(history[-min(lookback_days, len(history)):])
    
    if len(lookback_values) < 20:
        logger.warning(f"Insufficient VIX lookback data: {len(lookback_values)} points")
        return None
    
    # Percentile rank: what fraction of historical values are <= current value
    percentile = np.mean(lookback_values <= current_vix)
    
    logger.info(f"VIX percentile: {percentile:.2f} (current={current_vix:.2f}, n={len(lookback_values)})")
    
    return float(percentile)


def get_skew_percentile(lookback_days: int = 252) -> Optional[float]:
    """
    Compute skew percentile if available.
    
    Skew measurement requires access to options data (25-delta put vs ATM vol).
    Without live options feed, we return None.
    
    Future enhancement: Derive from snapshot if available.
    
    Args:
        lookback_days: Historical lookback period
        
    Returns:
        Percentile rank [0, 1], or None if unavailable
    """
    # No implementation for now - requires specialized options data
    # System must tolerate None gracefully
    logger.info("Skew signal not implemented, returning None")
    return None


def get_credit_spread_percentile(lookback_days: int = 252) -> Optional[float]:
    """
    Fetch credit spread indicator and compute percentile.
    
    Uses HYG (high-yield bond ETF) as proxy for credit stress.
    Alternative: FRED API for HY OAS (option adjusted spread).
    
    Args:
        lookback_days: Historical lookback period
        
    Returns:
        Percentile rank [0, 1], or None if unavailable
    """
    try:
        # Use HYG yield as credit proxy
        import yfinance as yf
        
        # HYG = iShares High Yield Corporate Bond ETF
        hyg = yf.Ticker("HYG")
        end_date = datetime.now()
        start_date = end_date - timedelta(days=int(lookback_days * 1.4))
        
        hist = hyg.history(start=start_date, end=end_date)
        
        if hist.empty or len(hist) < 20:
            logger.warning(f"Insufficient HYG data: {len(hist)} points")
            return None
        
        # Use inverse of price as credit stress proxy
        # Lower HYG price = higher credit stress
        latest_price = hist['Close'].iloc[-1]
        lookback_values = hist['Close'].iloc[-min(lookback_days, len(hist)):]
        
        if len(lookback_values) < 20:
            logger.warning(f"Insufficient HYG lookback data: {len(lookback_values)} points")
            return None
        
        # Invert: high percentile = high stress (low price)
        # Percentile of inverse price
        percentile = np.mean(lookback_values >= latest_price)
        
        logger.info(f"Credit percentile: {percentile:.2f} (HYG price={latest_price:.2f}, n={len(lookback_values)})")
        
        return float(percentile)
        
    except ImportError:
        logger.warning("yfinance not installed, credit signal unavailable")
        return None
    except Exception as e:
        logger.warning(f"Failed to fetch credit spread: {e}")
        return None


def get_regime_signals(lookback_days: int = 252) -> Dict[str, Optional[float]]:
    """
    Fetch all regime signals in one call.
    
    Args:
        lookback_days: Historical lookback period
        
    Returns:
        Dict with keys: vix_pct, skew_pct, credit_pct
        Values are percentiles [0, 1] or None if unavailable
    """
    logger.info(f"Fetching regime signals (lookback={lookback_days} days)")
    
    signals = {
        "vix_pct": get_vix_percentile(lookback_days),
        "skew_pct": get_skew_percentile(lookback_days),
        "credit_pct": get_credit_spread_percentile(lookback_days)
    }
    
    # Log summary
    available = sum(1 for v in signals.values() if v is not None)
    logger.info(f"Regime signals: {available}/3 available")
    
    for name, value in signals.items():
        if value is not None:
            logger.info(f"  {name}: {value:.2f}")
        else:
            logger.info(f"  {name}: None")
    
    return signals
