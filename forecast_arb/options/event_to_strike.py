"""
Event-to-strike adapter for options-implied probability.

Maps event definitions to specific strikes available in the option chain.
"""


def pick_implied_strike_for_event(
    event_def: dict,
    spot: float,
    available_strikes: list[float]
) -> dict:
    """
    Pick the nearest available strike for an event definition.
    
    For an index_drawdown event with threshold_pct, computes the target level
    and finds the closest strike in the available options chain.
    
    Args:
        event_def: Event definition dict with fields:
            - type: "index_drawdown"
            - index: "SPX"
            - threshold_pct: float (e.g., -0.15 for 15% drawdown)
            - expiry: date
        spot: Current spot price of the underlying
        available_strikes: List of available strike prices
        
    Returns:
        Dict with:
            - picked_strike: Selected strike (float)
            - target_level: Target level from event definition (float)
            - strike_error_pct: Abs error as fraction of target (float)
            
    Raises:
        ValueError: If event_def is invalid or no strikes available
    """
    if not available_strikes:
        raise ValueError("available_strikes cannot be empty")
    
    # Validate event definition
    event_type = event_def.get("type")
    if event_type != "index_drawdown":
        raise ValueError(f"Unsupported event type: {event_type}")
    
    index = event_def.get("index")
    if index != "SPX":
        raise ValueError(f"Unsupported index: {index}")
    
    threshold_pct = event_def.get("threshold_pct")
    if threshold_pct is None:
        raise ValueError("threshold_pct required for index_drawdown event")
    
    # Compute target level
    # threshold_pct is typically negative (e.g., -0.15 for 15% down)
    target_level = spot * (1 + threshold_pct)
    
    # Find nearest strike
    available_strikes_sorted = sorted(available_strikes)
    
    # Binary search or simple min distance
    picked_strike = min(
        available_strikes_sorted,
        key=lambda k: abs(k - target_level)
    )
    
    # Compute error
    strike_error_pct = abs(picked_strike - target_level) / target_level
    
    return {
        "picked_strike": float(picked_strike),
        "target_level": float(target_level),
        "strike_error_pct": float(strike_error_pct)
    }
