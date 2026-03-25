"""
Expiry Selection Logic

Provides unified expiry selection based on coverage score and DTE targeting.
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def select_best_expiry(
    snapshot: Dict,
    target_dte: Optional[int] = None,
    dte_min: Optional[int] = None,
    dte_max: Optional[int] = None,
    event_threshold: Optional[float] = None,
    threshold_tolerance: float = 5.0
) -> Tuple[Optional[str], Dict]:
    """
    Select best expiry from snapshot based on coverage and DTE.
    
    REPRESENTABILITY FIX: Now filters out expiries where event threshold strike doesn't exist.
    
    Strategy:
    1. Filter expiries by DTE range (if specified)
    2. Filter out non-representable expiries (if event_threshold specified)
    3. For each expiry, compute coverage score based on:
       - Number of puts/calls with executable bid/ask
       - Number of puts/calls with IV data
       - Spread quality
    4. Select expiry with best coverage, preferring closest to target_dte
    
    Args:
        snapshot: IBKR snapshot dict
        target_dte: Target days to expiry (optional, prefers closest if specified)
        dte_min: Minimum DTE (optional filter)
        dte_max: Maximum DTE (optional filter)
        event_threshold: Event threshold price for representability check (optional)
        threshold_tolerance: Distance tolerance for representability (default: $5)
        
    Returns:
        (selected_expiry, diagnostics) where:
        - selected_expiry: Best expiry string (YYYYMMDD) or None if none available
        - diagnostics: Dict with selection details and per-expiry scores
    """
    from .snapshot_io import (
        get_expiries,
        get_snapshot_metadata,
        compute_time_to_expiry,
        get_puts_for_expiry,
        get_calls_for_expiry
    )
    
    metadata = get_snapshot_metadata(snapshot)
    snapshot_time = metadata["snapshot_time"]
    expiries = get_expiries(snapshot)
    
    if not expiries:
        return None, {"error": "NO_EXPIRIES_IN_SNAPSHOT", "expiries_checked": 0}
    
    # Score each expiry
    expiry_scores = []
    non_representable_expiries = []
    
    for expiry in expiries:
        # Compute DTE
        T = compute_time_to_expiry(snapshot_time, expiry)
        dte = int(T * 365)
        
        # Apply DTE filters
        if dte_min is not None and dte < dte_min:
            continue
        if dte_max is not None and dte > dte_max:
            continue
        
        # Get options for this expiry
        puts = get_puts_for_expiry(snapshot, expiry)
        calls = get_calls_for_expiry(snapshot, expiry)
        
        # REPRESENTABILITY CHECK: Filter out expiries where threshold strike doesn't exist
        if event_threshold is not None:
            representable, repr_reason = _check_expiry_representability(
                puts, event_threshold, threshold_tolerance
            )
            
            if not representable:
                non_representable_expiries.append({
                    "expiry": expiry,
                    "dte": dte,
                    "reason": repr_reason
                })
                logger.debug(f"Skipping non-representable expiry {expiry}: {repr_reason}")
                continue
        
        # Compute coverage score
        coverage = _compute_coverage_score(puts, calls)
        
        # Compute DTE distance if target specified
        dte_distance = abs(dte - target_dte) if target_dte is not None else 0
        
        expiry_scores.append({
            "expiry": expiry,
            "dte": dte,
            "coverage_score": coverage["total_score"],
            "dte_distance": dte_distance,
            "coverage_details": coverage,
            "representable": True
        })
    
    if not expiry_scores:
        error_reason = "NO_REPRESENTABLE_EXPIRIES" if non_representable_expiries else "NO_EXPIRIES_AFTER_DTE_FILTER"
        diagnostics = {
            "error": error_reason,
            "dte_min": dte_min,
            "dte_max": dte_max,
            "event_threshold": event_threshold,
            "total_expiries": len(expiries),
            "expiries_checked": len(expiries),
            "non_representable_expiries": non_representable_expiries
        }
        return None, diagnostics
    
    # Sort by: 1) coverage score (desc), 2) DTE distance (asc)
    expiry_scores.sort(key=lambda x: (-x["coverage_score"], x["dte_distance"]))
    
    best = expiry_scores[0]
    
    logger.info(
        f"Selected expiry {best['expiry']} (DTE={best['dte']}) "
        f"with coverage score {best['coverage_score']:.2f}"
    )
    
    diagnostics = {
        "selected_expiry": best["expiry"],
        "selected_dte": best["dte"],
        "selected_coverage_score": best["coverage_score"],
        "selection_reason": "BEST_COVERAGE_AND_REPRESENTABLE",
        "all_scores": expiry_scores,
        "non_representable_count": len(non_representable_expiries),
        "non_representable_expiries": non_representable_expiries,
        "target_dte": target_dte,
        "dte_range": {"min": dte_min, "max": dte_max},
        "event_threshold": event_threshold
    }
    
    return best["expiry"], diagnostics


def _check_expiry_representability(
    puts: List[Dict],
    threshold: float,
    tolerance: float = 5.0
) -> Tuple[bool, str]:
    """
    Check if expiry has strikes near threshold with valid quotes.
    
    Args:
        puts: List of put options for expiry
        threshold: Event threshold price
        tolerance: Distance tolerance (default: $5)
        
    Returns:
        (is_representable, reason) tuple
    """
    if not puts:
        return False, "NO_PUTS"
    
    # Get all strikes
    strikes = [p["strike"] for p in puts]
    
    # Find nearest strike
    nearest = min(strikes, key=lambda s: abs(s - threshold))
    distance = abs(nearest - threshold)
    
    if distance > tolerance:
        return False, f"NEAREST_STRIKE_TOO_FAR: ${nearest:.0f} is ${distance:.2f} from threshold ${threshold:.2f} (tolerance=${tolerance})"
    
    # Find option at nearest strike
    opt = next((p for p in puts if p["strike"] == nearest), None)
    
    if not opt:
        return False, f"NO_OPTION_AT_STRIKE: ${nearest:.0f}"
    
    # Check for valid quotes
    bid = opt.get("bid")
    ask = opt.get("ask")
    
    bid_ok = bid is not None and bid > 0
    ask_ok = ask is not None and ask > 0
    
    if not (bid_ok and ask_ok):
        return False, f"INVALID_QUOTES: bid={bid}, ask={ask} at strike ${nearest:.0f}"
    
    return True, "OK"


def _compute_coverage_score(puts: List[Dict], calls: List[Dict]) -> Dict:
    """
    Compute coverage quality score for an expiry.
    
    Factors:
    - Number of options with executable quotes (bid > 0 and ask > bid)
    - Number of options with IV data
    - Average spread quality
    
    Returns:
        Dict with score components and total score
    """
    total_options = len(puts) + len(calls)
    
    if total_options == 0:
        return {
            "total_score": 0.0,
            "executable_count": 0,
            "iv_count": 0,
            "avg_spread_quality": 0.0,
            "total_options": 0
        }
    
    executable_count = 0
    iv_count = 0
    spread_qualities = []
    
    for option in puts + calls:
        bid = option.get("bid")
        ask = option.get("ask")
        iv = option.get("implied_vol")
        
        # Check executable quotes
        if bid is not None and ask is not None and bid > 0 and ask > bid:
            executable_count += 1
            
            # Compute spread quality
            mid = (bid + ask) / 2.0
            if mid > 0:
                spread_pct = (ask - bid) / mid
                # Convert to quality score (1.0 = tight, 0.0 = wide)
                if spread_pct < 0.05:
                    quality = 1.0
                elif spread_pct < 0.10:
                    quality = 0.85
                elif spread_pct < 0.20:
                    quality = 0.65
                elif spread_pct < 0.50:
                    quality = 0.35
                else:
                    quality = 0.15
                spread_qualities.append(quality)
        
        # Check IV
        if iv is not None and 0.01 <= iv <= 2.0:
            iv_count += 1
    
    # Compute average spread quality
    avg_spread_quality = sum(spread_qualities) / len(spread_qualities) if spread_qualities else 0.0
    
    # Compute total score (weighted combination)
    # 50% weight on executable quotes, 30% on IV coverage, 20% on spread quality
    executable_ratio = executable_count / total_options
    iv_ratio = iv_count / total_options
    
    total_score = (
        0.50 * executable_ratio +
        0.30 * iv_ratio +
        0.20 * avg_spread_quality
    )
    
    return {
        "total_score": total_score,
        "executable_count": executable_count,
        "iv_count": iv_count,
        "avg_spread_quality": avg_spread_quality,
        "total_options": total_options,
        "executable_ratio": executable_ratio,
        "iv_ratio": iv_ratio
    }
