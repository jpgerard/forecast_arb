"""
Unit normalization and formatting for financial metrics.

Provides helpers for normalizing EV/Dollar and other per-contract metrics
to ensure consistent precision and formatting across the system.
"""


def to_contract_dollars(x: float) -> float:
    """
    Normalize to per-contract dollars.
    
    This is a "tripwire" function that consolidates unit conversions.
    Currently passes through the value unchanged, but serves as a central
    location for future unit conversion logic.
    
    Args:
        x: Value in dollars
        
    Returns:
        Normalized value in per-contract dollars (float)
    """
    return float(x)


def format_ev_per_dollar(ev_per_dollar: float) -> str:
    """
    Format EV/Dollar for display with 6 decimal precision.
    
    Args:
        ev_per_dollar: EV per dollar value
        
    Returns:
        Formatted string with 6 decimals (e.g., "0.123456")
    """
    return f"{ev_per_dollar:.6f}"
