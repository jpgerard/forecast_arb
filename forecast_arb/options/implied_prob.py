"""
Options-implied probability using Black-Scholes framework.

Computes risk-neutral tail probabilities P(S_T <= K) from option parameters.
"""

import math
import logging
from typing import Dict, List, Optional, Tuple
from scipy.stats import norm

from ..structuring.quotes import price_buy, price_sell
from ..structuring.snapshot_io import get_strikes_for_expiry, get_puts_for_expiry, get_option_by_strike


logger = logging.getLogger(__name__)


def options_implied_p_event(
    spot: float,
    strike: float,
    iv: float,
    dte: int,
    r: float = 0.0
) -> float:
    """
    Compute risk-neutral probability P(S_T <= K) using Black-Scholes.
    
    Uses the standard Black-Scholes formula for the probability that
    the underlying will be at or below the strike at expiration.
    
    Args:
        spot: Current spot price (S0)
        strike: Strike price (K)
        iv: Implied volatility (annualized, e.g., 0.15 for 15%)
        dte: Days to expiration
        r: Risk-free rate (annualized, default 0.0)
        
    Returns:
        Probability P(S_T <= K) clipped to (1e-6, 1-1e-6)
        
    Raises:
        ValueError: If any input is invalid (non-positive spot/strike/iv/T)
        
    Formula:
        T = dte / 365.0
        d2 = [ln(S/K) + (r - 0.5*σ²)*T] / (σ*√T)
        P(S_T <= K) = N(-d2)
        
    where N is the standard normal CDF.
    """
    # Validate inputs
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    if iv <= 0:
        raise ValueError(f"iv must be positive, got {iv}")
    if dte <= 0:
        raise ValueError(f"dte must be positive, got {dte}")
    
    # Time to expiration in years
    T = dte / 365.0
    
    # Black-Scholes d2 parameter
    # d2 = [ln(S/K) + (r - 0.5*σ²)*T] / (σ*√T)
    d2 = (math.log(spot / strike) + (r - 0.5 * iv**2) * T) / (iv * math.sqrt(T))
    
    # Risk-neutral probability P(S_T <= K) = N(-d2)
    p = norm.cdf(-d2)
    
    # Clip to avoid numerical issues
    p = max(1e-6, min(1 - 1e-6, p))
    
    return float(p)


def implied_prob_terminal_below(
    snapshot: Dict,
    expiry: str,
    threshold: float,
    r: float = 0.0
) -> Tuple[Optional[float], float, List[str]]:
    """
    Compute options-implied probability P(S_T < threshold) using put vertical spreads.
    
    Approach:
    - Find strikes K1 (just above threshold) and K2 (just below threshold)
    - Price put vertical spread using executable sides (BUY=ask, SELL=bid)
    - Approximate P(S_T < threshold) from spread price
    - Return confidence based on quote quality
    
    Args:
        snapshot: IBKR snapshot dict
        expiry: Target expiry (YYYYMMDD)
        threshold: Strike-like threshold (event boundary)
        r: Risk-free rate (annualized, default 0.0)
        
    Returns:
        (p_implied, confidence, warnings) where:
        - p_implied: Implied probability or None if insufficient data
        - confidence: Quality score 0.0-1.0
        - warnings: List of warning messages
        
    Confidence factors:
    - 1.0: Both legs have tight bid/ask, strikes bracket threshold well
    - 0.6-0.9: Some quotes available, moderate spreads
    - <0.6: Wide spreads or far strikes
    - 0.0: Insufficient quotes
    """
    from ..structuring.snapshot_io import get_snapshot_metadata
    
    metadata = get_snapshot_metadata(snapshot)
    spot = metadata["current_price"]
    
    warnings = []
    
    # Get available strikes and puts
    try:
        available_strikes = get_strikes_for_expiry(snapshot, expiry)
        puts = get_puts_for_expiry(snapshot, expiry)
    except Exception as e:
        warnings.append(f"Failed to load strikes/puts: {e}")
        return None, 0.0, warnings
    
    if not available_strikes or not puts:
        warnings.append("No strikes or puts available")
        return None, 0.0, warnings
    
    # Find strikes bracketing threshold
    strikes_above = [k for k in available_strikes if k > threshold]
    strikes_below = [k for k in available_strikes if k <= threshold]
    
    if not strikes_above or not strikes_below:
        warnings.append(f"Cannot bracket threshold ${threshold:.2f}: strikes_above={len(strikes_above)}, strikes_below={len(strikes_below)}")
        return None, 0.0, warnings
    
    # Pick nearest strikes
    K1 = min(strikes_above)  # Just above threshold
    K2 = max(strikes_below)  # Just below threshold
    
    logger.info(f"Bracketing threshold ${threshold:.2f} with K1=${K1:.2f} (above), K2=${K2:.2f} (below)")
    
    # Get put options
    put_K1 = get_option_by_strike(puts, K1)
    put_K2 = get_option_by_strike(puts, K2)
    
    if put_K1 is None or put_K2 is None:
        warnings.append(f"Missing put options: K1={K1} exists={put_K1 is not None}, K2={K2} exists={put_K2 is not None}")
        return None, 0.0, warnings
    
    # Price the legs using executable sides
    # To estimate P(S_T < threshold), we buy a put spread:
    # BUY put@K1 (higher strike) - SELL put@K2 (lower strike)
    price_K1, source_K1 = price_buy(put_K1)  # Leg we BUY
    price_K2, source_K2 = price_sell(put_K2)  # Leg we SELL
    
    if price_K1 is None or price_K2 is None:
        warnings.append(f"NO_EXECUTABLE_PRICE: K1 source={source_K1}, K2 source={source_K2}")
        warnings.append("PRIMARY_VERTICAL_NO_EXECUTABLE_QUOTES")
        
        # Fallback: try Black-Scholes with ATM IV
        logger.warning(f"Primary bracket method failed, attempting BS fallback")
        return _bs_fallback(snapshot, expiry, threshold, spot, metadata, warnings, r)
    
    # Debit for put spread (per share)
    spread_debit = price_K1 - price_K2
    spread_width = K1 - K2
    
    if spread_debit <= 0:
        warnings.append(f"Invalid spread: debit={spread_debit:.4f} <= 0 (K1={K1}, K2={K2})")
        return None, 0.0, warnings
    
    if spread_width <= 0:
        warnings.append(f"Invalid width: {spread_width:.2f} (K1={K1}, K2={K2})")
        return None, 0.0, warnings
    
    # Approximate digital probability
    # P(S_T in [K2, K1]) ≈ spread_price / (spread_width * discount_factor)
    # For P(S_T < threshold), we need to adjust based on where threshold sits
    
    # Simple approximation: assume uniform distribution within [K2, K1]
    # P(S_T < threshold) ≈ P(S_T < K2) + P(S_T in [K2, threshold])
    
    # Discount factor
    from ..structuring.snapshot_io import compute_time_to_expiry
    T = compute_time_to_expiry(metadata["snapshot_time"], expiry)
    discount = math.exp(-r * T)
    
    # Discounted spread price
    spread_value_discounted = spread_debit / discount
    
    # Raw probability estimate from spread
    # This is P(S_T in [K2, K1])
    p_spread = spread_value_discounted / spread_width
    
    # Clip to valid range
    p_spread = max(0.0, min(1.0, p_spread))
    
    # Interpolate within bracket
    # If threshold is at K2, p_below = p_at_K2
    # If threshold is at K1, p_below = p_at_K2 + p_spread
    # Linear interpolation
    threshold_position = (threshold - K2) / spread_width if spread_width > 0 else 0.5
    threshold_position = max(0.0, min(1.0, threshold_position))
    
    # This is a very rough approximation
    # p_below_threshold ≈ p_below_K2 + p_spread * threshold_position
    # We don't know p_below_K2 directly, so we make a simplifying assumption
    # For deep OTM, p_below_K2 is small, so p_below_threshold ≈ p_spread * threshold_position
    
    # Better approach: use put prices directly with Black-Scholes
    # Estimate IV from put prices
    iv_K1 = put_K1.get("implied_vol")
    iv_K2 = put_K2.get("implied_vol")
    
    if iv_K1 and iv_K1 > 0 and iv_K2 and iv_K2 > 0:
        # Use average IV for threshold
        iv_avg = (iv_K1 + iv_K2) / 2.0
        dte = int(T * 365)
        
        try:
            p_implied = options_implied_p_event(
                spot=spot,
                strike=threshold,
                iv=iv_avg,
                dte=dte,
                r=r
            )
            
            logger.info(f"P(S_T < ${threshold:.2f}) = {p_implied:.3f} (IV={iv_avg:.3f})")
        except Exception as e:
            warnings.append(f"Black-Scholes calculation failed: {e}")
            # Fall back to spread-based estimate
            p_implied = p_spread * threshold_position
            logger.warning(f"Using spread-based estimate: {p_implied:.3f}")
    else:
        #IV not available, use spread-based estimate
        warnings.append(f"IV not available: K1={iv_K1}, K2={iv_K2}")
        p_implied = p_spread * threshold_position
        logger.warning(f"Using spread-based estimate: {p_implied:.3f}")
    
    # Clip final result
    p_implied = max(0.001, min(0.999, p_implied))
    
    # Compute confidence
    confidence = _compute_confidence(
        put_K1=put_K1,
        put_K2=put_K2,
        price_K1=price_K1,
        price_K2=price_K2,
        source_K1=source_K1,
        source_K2=source_K2,
        threshold=threshold,
        K1=K1,
        K2=K2,
        spot=spot
    )
    
    return p_implied, confidence, warnings


def _bs_fallback(
    snapshot: Dict,
    expiry: str,
    threshold: float,
    spot: float,
    metadata: Dict,
    warnings: List[str],
    r: float
) -> Tuple[Optional[float], float, List[str]]:
    """
    Fallback method using Black-Scholes with ATM IV when bracket method fails.
    
    Returns:
        (p_implied, confidence, warnings) where confidence is reduced (0.35-0.5)
    """
    from ..structuring.snapshot_io import compute_time_to_expiry
    from .iv_source import get_atm_iv
    
    warnings.append("FALLBACK_MODEL_USED")
    
    # Use standardized IV sourcing helper
    iv, iv_source, iv_warnings = get_atm_iv(snapshot, expiry, spot)
    warnings.extend(iv_warnings)
    
    if iv is None:
        warnings.append("No valid IV source for BS fallback")
        return None, 0.0, warnings
    
    # Compute BS probability
    T = compute_time_to_expiry(metadata["snapshot_time"], expiry)
    dte = int(T * 365)
    
    try:
        p_implied = options_implied_p_event(
            spot=spot,
            strike=threshold,
            iv=iv,
            dte=dte,
            r=r
        )
        
        # Compute confidence based on IV source
        # Base confidence: 0.45 (higher than before since we're more reliable now)
        base_confidence = 0.45
        
        # Adjust based on IV source quality
        if iv_source == "snapshot_atm_iv":
            # Direct snapshot ATM IV (best quality)
            confidence = 0.50
        elif iv_source == "expiry_atm_iv":
            # Per-expiry ATM IV (very good quality)
            confidence = 0.48
        elif iv_source == "iv_inferred_atm":
            # Inferred from near-ATM option (good quality)
            confidence = 0.35
        else:
            # Unknown source or fallback
            confidence = 0.20
        
        # Reduce confidence for far OTM
        threshold_moneyness = abs(threshold - spot) / spot
        if threshold_moneyness > 0.30:
            confidence *= 0.85
        if threshold_moneyness > 0.40:
            confidence *= 0.80
        
        logger.info(
            f"BS fallback: P(S_T < ${threshold:.2f}) = {p_implied:.3f}, "
            f"IV={iv:.4f} ({iv_source}), confidence={confidence:.2f}"
        )
        
        return p_implied, confidence, warnings
        
    except Exception as e:
        warnings.append(f"BS fallback calculation failed: {e}")
        return None, 0.0, warnings


def _compute_confidence(
    put_K1: Dict,
    put_K2: Dict,
    price_K1: float,
    price_K2: float,
    source_K1: str,
    source_K2: str,
    threshold: float,
    K1: float,
    K2: float,
    spot: float
) -> float:
    """
    Compute confidence score for implied probability estimate.
    
    Factors:
    - Quote quality (bid/ask presence and spreads)
    - Strike proximity to threshold
    - Distance from spot (more reliable near ATM)
    
    Returns:
        Confidence score 0.0-1.0
    """
    confidence = 1.0
    
    # Factor 1: Quote source quality
    # Prefer primary quotes (ask/bid) over fallbacks
    if source_K1 != "ask":
        confidence *= 0.9
    if source_K2 != "bid":
        confidence *= 0.9
    
    # Factor 2: Bid/ask spread quality
    bid_K1 = put_K1.get("bid")
    ask_K1 = put_K1.get("ask")
    bid_K2 = put_K2.get("bid")
    ask_K2 = put_K2.get("ask")
    
    if ask_K1 and bid_K1 and bid_K1 > 0:
        spread_pct_K1 = (ask_K1 - bid_K1) / bid_K1
        if spread_pct_K1 > 0.20:  # >20% spread
            confidence *= 0.8
        elif spread_pct_K1 > 0.50:  # >50% spread
            confidence *= 0.6
    
    if bid_K2 and ask_K2 and bid_K2 > 0:
        spread_pct_K2 = (ask_K2 - bid_K2) / bid_K2
        if spread_pct_K2 > 0.20:
            confidence *= 0.8
        elif spread_pct_K2 > 0.50:
            confidence *= 0.6
    
    # Factor 3: Strike bracketing quality
    # How well do K1 and K2 bracket the threshold?
    bracket_width = K1 - K2
    if bracket_width > spot * 0.10:  # Very wide bracket (>10% of spot)
        confidence *= 0.7
    
    # Factor 4: Distance from ATM
    # Strikes far from spot are less reliable
    threshold_moneyness = abs(threshold - spot) / spot
    if threshold_moneyness > 0.25:  # >25% OTM
        confidence *= 0.8
    if threshold_moneyness > 0.35:  # >35% OTM
        confidence *= 0.7
    
    return max(0.0, min(1.0, confidence))
