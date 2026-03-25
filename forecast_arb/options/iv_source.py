"""
Implied volatility sourcing for Black-Scholes fallback.

Provides standardized methods to obtain ATM IV from snapshots,
either from snapshot-level metadata or inferred from liquid near-ATM options.
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_atm_iv(
    snapshot: Dict,
    expiry: str,
    spot: float
) -> Tuple[Optional[float], str, List[str]]:
    """
    Get ATM implied volatility for Black-Scholes fallback.
    
    Strategy:
    1. Try snapshot-level ATM IV (preferred, but not in current schema)
    2. Try per-expiry ATM IV (preferred, but not in current schema)
    3. Derive IV from near-ATM option with best executable pricing
    4. Return None if no valid IV source
    
    Args:
        snapshot: IBKR snapshot dict
        expiry: Target expiry (YYYYMMDD)
        spot: Current spot price
        
    Returns:
        (iv, source, warnings) where:
        - iv: Implied volatility (e.g., 0.15 for 15%) or None
        - source: Description of IV source
        - warnings: List of warning messages
        
    Sources (in priority order):
        - "snapshot_atm_iv": Snapshot-level ATM IV (not currently available)
        - "expiry_atm_iv": Per-expiry ATM IV (not currently available)
        - "iv_inferred_atm": Derived from near-ATM option with quotes
        - "NO_IV_SOURCE": No valid IV available
    """
    from ..structuring.snapshot_io import (
        get_calls_for_expiry,
        get_puts_for_expiry
    )
    
    warnings = []
    
    # Get metadata directly (don't use get_snapshot_metadata to avoid unnecessary dependencies)
    snapshot_meta = snapshot.get("snapshot_metadata", {})
    
    # Method 1: Check snapshot-level ATM IV
    # (Not in current schema, but check anyway for future compatibility)
    if "atm_iv" in snapshot_meta and snapshot_meta.get("atm_iv"):
        iv = snapshot_meta["atm_iv"]
        if _is_valid_iv(iv):
            logger.info(f"Using snapshot-level ATM IV: {iv:.4f}")
            return iv, "snapshot_atm_iv", warnings
    
    # Method 2: Check per-expiry ATM IV
    # (Not in current schema, but check anyway for future compatibility)
    expiries_data = snapshot.get("expiries", {})
    expiry_data = expiries_data.get(expiry, {})
    if "atm_iv" in expiry_data and expiry_data.get("atm_iv"):
        iv = expiry_data["atm_iv"]
        if _is_valid_iv(iv):
            logger.info(f"Using expiry-level ATM IV for {expiry}: {iv:.4f}")
            return iv, "expiry_atm_iv", warnings
    
    # Method 3: Infer IV from near-ATM option
    # Find the strike closest to spot with valid IV and executable quotes
    try:
        # Try both calls and puts, choose best
        calls = get_calls_for_expiry(snapshot, expiry)
        puts = get_puts_for_expiry(snapshot, expiry)
        
        candidates = []
        
        # Evaluate calls
        for call in calls:
            strike = call.get("strike")
            if strike is None:
                continue
            
            distance = abs(strike - spot)
            iv = call.get("implied_vol")
            bid = call.get("bid")
            ask = call.get("ask")
            
            if not _is_valid_iv(iv):
                continue
            
            # Check for executable quotes
            has_quotes = (bid is not None and bid > 0) or (ask is not None and ask > 0)
            if not has_quotes:
                continue
            
            # Prefer tight spreads
            spread_quality = _compute_spread_quality(bid, ask)
            
            candidates.append({
                "iv": iv,
                "strike": strike,
                "distance": distance,
                "spread_quality": spread_quality,
                "option_type": "call"
            })
        
        # Evaluate puts
        for put in puts:
            strike = put.get("strike")
            if strike is None:
                continue
            
            distance = abs(strike - spot)
            iv = put.get("implied_vol")
            bid = put.get("bid")
            ask = put.get("ask")
            
            if not _is_valid_iv(iv):
                continue
            
            # Check for executable quotes
            has_quotes = (bid is not None and bid > 0) or (ask is not None and ask > 0)
            if not has_quotes:
                continue
            
            # Prefer tight spreads
            spread_quality = _compute_spread_quality(bid, ask)
            
            candidates.append({
                "iv": iv,
                "strike": strike,
                "distance": distance,
                "spread_quality": spread_quality,
                "option_type": "put"
            })
        
        if not candidates:
            warnings.append("NO_IV_SOURCE: No options with valid IV and quotes")
            logger.warning(f"No ATM options found with valid IV for expiry {expiry}")
            return None, "NO_IV_SOURCE", warnings
        
        # Sort by: 1) distance from spot, 2) spread quality
        # Prefer options close to ATM with tight spreads
        candidates.sort(key=lambda c: (c["distance"], -c["spread_quality"]))
        
        best = candidates[0]
        iv = best["iv"]
        
        logger.info(
            f"Inferred ATM IV from {best['option_type']} @ strike {best['strike']:.2f} "
            f"(distance from spot: ${best['distance']:.2f}): IV={iv:.4f}"
        )
        
        return iv, "iv_inferred_atm", warnings
        
    except Exception as e:
        warnings.append(f"Failed to infer IV from options: {e}")
        logger.error(f"Error inferring ATM IV: {e}", exc_info=True)
        return None, "NO_IV_SOURCE", warnings


def _is_valid_iv(iv: Optional[float]) -> bool:
    """Check if IV is valid (between 1% and 200%)."""
    if iv is None:
        return False
    return 0.01 <= iv <= 2.0


def _compute_spread_quality(bid: Optional[float], ask: Optional[float]) -> float:
    """
    Compute spread quality score (higher is better).
    
    Returns:
        Score 0.0-1.0:
        - 1.0: Bid and ask both available with tight spread (<5%)
        - 0.5-0.9: Moderate spread (5-20%)
        - 0.1-0.5: Wide spread (>20%)
        - 0.0: No bid/ask available
    """
    if bid is None or ask is None:
        return 0.0
    
    if bid <= 0 or ask <= 0:
        return 0.0
    
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 0.0
    
    spread_pct = (ask - bid) / mid
    
    if spread_pct < 0.05:  # <5% spread
        return 1.0
    elif spread_pct < 0.10:  # <10% spread
        return 0.85
    elif spread_pct < 0.20:  # <20% spread
        return 0.65
    elif spread_pct < 0.50:  # <50% spread
        return 0.35
    else:
        return 0.15
