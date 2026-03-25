"""
Snapshot I/O: Parse and validate IBKR option chain snapshots.

Handles JSON snapshots exported by ibkr_snapshot.py exporter.
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
import logging


logger = logging.getLogger(__name__)


def load_snapshot(path: str) -> Dict:
    """
    Load snapshot JSON from file.
    
    Args:
        path: Path to snapshot JSON file
        
    Returns:
        Parsed snapshot dict
    """
    with open(path, "r") as f:
        return json.load(f)


def validate_snapshot(snapshot: Dict) -> bool:
    """
    Validate snapshot structure.
    
    Args:
        snapshot: Snapshot dict
        
    Returns:
        True if valid
        
    Raises:
        ValueError if invalid
    """
    # Check required top-level keys
    if "snapshot_metadata" not in snapshot:
        raise ValueError("Missing snapshot_metadata")
    
    if "expiries" not in snapshot:
        raise ValueError("Missing expiries")
    
    metadata = snapshot["snapshot_metadata"]
    required_meta = ["underlier", "snapshot_time", "current_price"]
    
    for key in required_meta:
        if key not in metadata:
            raise ValueError(f"Missing metadata field: {key}")
    
    # Validate expiries
    if not isinstance(snapshot["expiries"], dict):
        raise ValueError("expiries must be a dict")
    
    for expiry, data in snapshot["expiries"].items():
        if "calls" not in data or "puts" not in data:
            raise ValueError(f"Expiry {expiry} missing calls or puts")
    
    return True


def get_snapshot_metadata(snapshot: Dict) -> Dict:
    """
    Extract snapshot metadata.
    
    Args:
        snapshot: Snapshot dict
        
    Returns:
        Metadata dict with parsed fields
    """
    meta = snapshot["snapshot_metadata"]
    
    return {
        "underlier": meta["underlier"],
        "snapshot_time": meta["snapshot_time"],
        "current_price": meta["current_price"],
        "risk_free_rate": meta.get("risk_free_rate", 0.0),
        "dividend_yield": meta.get("dividend_yield", 0.0),
        "dte_min": meta.get("dte_min"),
        "dte_max": meta.get("dte_max")
    }


def get_expiries(snapshot: Dict) -> List[str]:
    """
    Get list of expiry dates from snapshot.
    
    Args:
        snapshot: Snapshot dict
        
    Returns:
        Sorted list of expiry strings (YYYYMMDD)
    """
    return sorted(snapshot["expiries"].keys())


def get_strikes_for_expiry(snapshot: Dict, expiry: str) -> List[float]:
    """
    Get list of strikes for a specific expiry.
    
    Args:
        snapshot: Snapshot dict
        expiry: Expiry date (YYYYMMDD)
        
    Returns:
        Sorted list of strikes
    """
    if expiry not in snapshot["expiries"]:
        return []
    
    data = snapshot["expiries"][expiry]
    
    # Get strikes from both calls and puts
    strikes = set()
    for opt in data["calls"]:
        strikes.add(opt["strike"])
    for opt in data["puts"]:
        strikes.add(opt["strike"])
    
    return sorted(strikes)


def get_calls_for_expiry(snapshot: Dict, expiry: str) -> List[Dict]:
    """
    Get call options for a specific expiry.
    
    Args:
        snapshot: Snapshot dict
        expiry: Expiry date (YYYYMMDD)
        
    Returns:
        List of call option dicts
    """
    if expiry not in snapshot["expiries"]:
        return []
    
    return snapshot["expiries"][expiry]["calls"]


def get_puts_for_expiry(snapshot: Dict, expiry: str) -> List[Dict]:
    """
    Get put options for a specific expiry.
    
    Args:
        snapshot: Snapshot dict
        expiry: Expiry date (YYYYMMDD)
        
    Returns:
        List of put option dicts
    """
    if expiry not in snapshot["expiries"]:
        return []
    
    return snapshot["expiries"][expiry]["puts"]


def get_option_by_strike(
    options: List[Dict],
    strike: float,
    tolerance: float = 0.01
) -> Optional[Dict]:
    """
    Find option with specific strike.
    
    Args:
        options: List of option dicts
        strike: Target strike
        tolerance: Strike matching tolerance
        
    Returns:
        Option dict or None
    """
    for opt in options:
        if abs(opt["strike"] - strike) < tolerance:
            return opt
    
    return None


def compute_time_to_expiry(
    snapshot_time: str,
    expiry: str
) -> float:
    """
    Compute time to expiry in years.
    
    Args:
        snapshot_time: Snapshot timestamp (ISO format)
        expiry: Expiry date (YYYYMMDD)
        
    Returns:
        Time to expiry in years
    """
    snapshot_dt = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
    expiry_dt = datetime.strptime(expiry, "%Y%m%d").replace(tzinfo=timezone.utc)
    
    days = (expiry_dt - snapshot_dt).days
    return days / 365.0


def extract_structure_inputs_from_snapshot(
    snapshot: Dict,
    expiry: str,
    strikes: List[float],
    option_types: List[str]  # ["call", "put"]
) -> Dict:
    """
    Extract inputs for option structure from snapshot.
    
    Args:
        snapshot: Snapshot dict
        expiry: Target expiry (YYYYMMDD)
        strikes: List of strikes to use
        option_types: List of option types ("call" or "put")
        
    Returns:
        Dict with structure inputs (S0, r, T, options data)
    """
    metadata = get_snapshot_metadata(snapshot)
    
    S0 = metadata["current_price"]
    r = metadata["risk_free_rate"]
    T = compute_time_to_expiry(metadata["snapshot_time"], expiry)
    
    calls = get_calls_for_expiry(snapshot, expiry)
    puts = get_puts_for_expiry(snapshot, expiry)
    
    legs = []
    for i, (strike, opt_type) in enumerate(zip(strikes, option_types)):
        if opt_type == "call":
            opt = get_option_by_strike(calls, strike)
        elif opt_type == "put":
            opt = get_option_by_strike(puts, strike)
        else:
            raise ValueError(f"Unknown option type: {opt_type}")
        
        if opt is None:
            logger.warning(f"Option not found: {opt_type} {strike}")
            continue
        
        # Use midpoint or last price
        if opt["bid"] and opt["ask"]:
            price = (opt["bid"] + opt["ask"]) / 2.0
        elif opt["last"]:
            price = opt["last"]
        else:
            logger.warning(f"No price for {opt_type} {strike}")
            continue
        
        legs.append({
            "type": opt_type,
            "strike": strike,
            "price": price,
            "bid": opt["bid"],
            "ask": opt["ask"],
            "implied_vol": opt.get("implied_vol"),
            "delta": opt.get("delta"),
            "gamma": opt.get("gamma"),
            "vega": opt.get("vega"),
            "theta": opt.get("theta")
        })
    
    return {
        "underlier": metadata["underlier"],
        "expiry": expiry,
        "S0": S0,
        "r": r,
        "T": T,
        "legs": legs
    }


def find_nearest_expiry(
    snapshot: Dict,
    target_dte: int
) -> Optional[str]:
    """
    Find expiry closest to target DTE.
    
    Args:
        snapshot: Snapshot dict
        target_dte: Target days to expiry
        
    Returns:
        Expiry string (YYYYMMDD) or None
    """
    metadata = get_snapshot_metadata(snapshot)
    snapshot_time = metadata["snapshot_time"]
    
    expiries = get_expiries(snapshot)
    
    best_expiry = None
    best_diff = float('inf')
    
    for expiry in expiries:
        T_years = compute_time_to_expiry(snapshot_time, expiry)
        dte = T_years * 365
        
        diff = abs(dte - target_dte)
        if diff < best_diff:
            best_diff = diff
            best_expiry = expiry
    
    return best_expiry


def find_atm_strike(snapshot: Dict, expiry: str) -> Optional[float]:
    """
    Find at-the-money strike for expiry.
    
    Args:
        snapshot: Snapshot dict
        expiry: Expiry date (YYYYMMDD)
        
    Returns:
        ATM strike or None
    """
    metadata = get_snapshot_metadata(snapshot)
    spot = metadata["current_price"]
    
    strikes = get_strikes_for_expiry(snapshot, expiry)
    
    if not strikes:
        return None
    
    # Find closest strike to spot
    return min(strikes, key=lambda k: abs(k - spot))


def snapshot_summary(snapshot: Dict) -> str:
    """
    Generate human-readable summary of snapshot.
    
    Args:
        snapshot: Snapshot dict
        
    Returns:
        Markdown-formatted summary
    """
    metadata = get_snapshot_metadata(snapshot)
    expiries = get_expiries(snapshot)
    
    lines = ["# Option Chain Snapshot\n"]
    lines.append(f"**Underlier**: {metadata['underlier']}\n")
    lines.append(f"**Spot Price**: ${metadata['current_price']:.2f}\n")
    lines.append(f"**Snapshot Time**: {metadata['snapshot_time']}\n")
    lines.append(f"**Expiries**: {len(expiries)}\n")
    lines.append("\n## Expiries\n")
    
    for expiry in expiries:
        T = compute_time_to_expiry(metadata["snapshot_time"], expiry)
        dte = int(T * 365)
        
        strikes = get_strikes_for_expiry(snapshot, expiry)
        calls = get_calls_for_expiry(snapshot, expiry)
        puts = get_puts_for_expiry(snapshot, expiry)
        
        lines.append(f"\n### {expiry} (DTE={dte})\n")
        lines.append(f"- Strikes: {len(strikes)}\n")
        lines.append(f"- Calls: {len(calls)}\n")
        lines.append(f"- Puts: {len(puts)}\n")
    
    return "".join(lines)
