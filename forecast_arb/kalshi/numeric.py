"""
Numeric hygiene utilities for Kalshi probability computations.

Ensures all numeric values used in comparisons, sorting, and probability
calculations are valid floats, preventing complex numbers from propagating.
"""

from typing import Any, Optional
import logging


logger = logging.getLogger(__name__)


def as_float(x: Any, name: str, allow_none: bool = False) -> Optional[float]:
    """
    Convert value to float with strict validation.
    
    Args:
        x: Value to convert
        name: Descriptive name for error messages
        allow_none: Whether None is acceptable
        
    Returns:
        Float value or None (if allow_none=True)
        
    Raises:
        ValueError: If conversion fails or value is complex
    """
    if x is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is None but None not allowed")
    
    # Handle boolean (convert False->0.0, True->1.0)
    if isinstance(x, bool):
        return float(x)
    
    # Handle numeric types
    if isinstance(x, (int, float)):
        result = float(x)
        # Check for NaN or inf
        if not (-1e308 < result < 1e308):  # Basic bounds check
            raise ValueError(f"{name}={result} is infinite or out of range")
        return result
    
    # Handle complex - only allow if imaginary part is negligible
    if isinstance(x, complex):
        if abs(x.imag) < 1e-9:
            logger.warning(f"{name} is complex with negligible imaginary part: {x}, using real part")
            return float(x.real)
        raise ValueError(f"{name} is complex: {x}")
    
    # Try generic conversion
    try:
        result = float(x)
        if not (-1e308 < result < 1e308):
            raise ValueError(f"{name}={result} is infinite or out of range")
        return result
    except Exception as e:
        raise ValueError(f"Cannot convert {name}={x} to float") from e


def as_probability(p: Any, name: str, allow_none: bool = False) -> Optional[float]:
    """
    Convert and validate probability value.
    
    Args:
        p: Probability value to validate
        name: Descriptive name for error messages
        allow_none: Whether None is acceptable
        
    Returns:
        Probability in [0, 1] or None (if allow_none=True)
        
    Raises:
        ValueError: If not a valid probability
    """
    if p is None:
        if allow_none:
            return None
        raise ValueError(f"{name} is None but None not allowed")
    
    # Convert to float first
    p_float = as_float(p, name, allow_none=False)
    
    # Validate range
    if not (0.0 <= p_float <= 1.0):
        raise ValueError(f"{name}={p_float} is not in valid probability range [0, 1]")
    
    return p_float


def safe_hazard_scale(p_annual: float, horizon_days: int, name: str = "p_annual") -> float:
    """
    Safely apply hazard rate scaling to convert annual probability to shorter horizon.
    
    Formula: p_T = 1 - (1 - p_annual)^(T/365)
    
    Guards against:
    - p_annual > 1 (would create negative base and complex result)
    - p_annual < 0 (invalid probability)
    - horizon_days <= 0 (invalid horizon)
    
    Args:
        p_annual: Annual probability [0, 1]
        horizon_days: Target horizon in days (> 0)
        name: Descriptive name for error messages
        
    Returns:
        Scaled probability [0, 1]
        
    Raises:
        ValueError: If inputs are invalid
    """
    # Validate annual probability
    p_annual = as_probability(p_annual, name, allow_none=False)
    
    # Validate horizon
    if horizon_days <= 0:
        raise ValueError(f"horizon_days={horizon_days} must be positive")
    
    # Handle edge cases
    if p_annual == 0.0:
        return 0.0
    if p_annual == 1.0:
        return 1.0
    
    # Calculate scaling factor
    T_years = horizon_days / 365.0
    
    # Compute scaled probability
    # p_T = 1 - (1 - p_annual)^(T/365)
    base = 1.0 - p_annual  # Always in [0, 1] if p_annual in [0, 1]
    
    try:
        p_T = 1.0 - (base ** T_years)
    except Exception as e:
        raise ValueError(
            f"Failed to compute hazard scale: p_annual={p_annual}, "
            f"horizon_days={horizon_days}, base={base}"
        ) from e
    
    # Ensure result is in valid range
    p_T = max(0.0, min(1.0, p_T))
    
    return p_T
