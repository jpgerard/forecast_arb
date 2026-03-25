"""
Event definitions for crash venture strategies.

Defines canonical event types used for computing options-implied probabilities
and comparing against external forecasts.
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class EventSpec:
    """
    Single Source of Truth for Event Parameters.
    
    Created once in run_daily.py and passed everywhere to prevent
    moneyness/threshold recomputation inconsistencies.
    
    Attributes:
        moneyness: Event moneyness from config (e.g., -0.15 for 15% below spot)
        threshold: Computed threshold = spot * (1 + moneyness)
        expiry: Selected expiry date (YYYYMMDD format)
        spot: Spot price at time of EventSpec creation
        underlier: Ticker symbol (e.g., "SPY")
        direction: Event direction ("below" or "above")
        regime: Regime type ("crash" or "selloff") for Crash Venture v2
        event_hash: Unique hash for this event spec
    """
    moneyness: float
    threshold: float
    expiry: str
    spot: float
    underlier: str
    direction: str = "below"
    regime: Optional[str] = None
    event_hash: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "moneyness": self.moneyness,
            "threshold": self.threshold,
            "expiry": self.expiry,
            "spot": self.spot,
            "underlier": self.underlier,
            "direction": self.direction
        }
        if self.regime is not None:
            result["regime"] = self.regime
        if self.event_hash is not None:
            result["event_hash"] = self.event_hash
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventSpec":
        """Create from dictionary."""
        return cls(**data)
    
    def validate_threshold_consistency(self, recomputed_threshold: float, tolerance: float = 0.01) -> None:
        """
        Validate that a recomputed threshold matches the canonical threshold.
        
        Args:
            recomputed_threshold: Threshold computed elsewhere
            tolerance: Allowable difference in dollars
            
        Raises:
            ValueError: If threshold mismatch detected
        """
        diff = abs(self.threshold - recomputed_threshold)
        if diff > tolerance:
            raise ValueError(
                f"MONEYNESS_MISMATCH: Canonical threshold ${self.threshold:.2f} "
                f"vs recomputed ${recomputed_threshold:.2f} (diff ${diff:.2f}). "
                f"This indicates threshold was recomputed instead of using EventSpec."
            )


@dataclass
class EventDef:
    """
    Canonical event definition (legacy - use EventSpec for new code).
    
    Attributes:
        event_type: Type of event ("terminal_below", "terminal_above", etc.)
        underlier: Ticker symbol (e.g., "SPY")
        expiry: Expiry date (YYYYMMDD format)
        threshold: Strike-like threshold level
        direction: Direction ("below" or "above")
    """
    event_type: str
    underlier: str
    expiry: str
    threshold: float
    direction: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_type": self.event_type,
            "underlier": self.underlier,
            "expiry": self.expiry,
            "threshold": self.threshold,
            "direction": self.direction
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EventDef":
        """Create from dictionary."""
        return cls(**data)
    
    def to_event_spec(self) -> EventSpec:
        """Convert EventDef to EventSpec (requires spot to compute moneyness)."""
        # This is a compatibility helper - ideally use EventSpec directly
        raise NotImplementedError(
            "Cannot convert EventDef to EventSpec without spot price. "
            "Create EventSpec directly instead."
        )


def create_event_spec(
    underlier: str,
    expiry: str,
    spot: float,
    moneyness: float,
    regime: Optional[str] = None
) -> EventSpec:
    """
    Create EventSpec - Single Source of Truth for event parameters.
    
    This should be called ONCE in run_daily.py and the EventSpec
    passed to all downstream components.
    
    Args:
        underlier: Ticker symbol
        expiry: Expiry date (YYYYMMDD)
        spot: Current spot price
        moneyness: Event moneyness from config (e.g., -0.15 for 15% below spot)
        regime: Regime type ("crash" or "selloff") for Crash Venture v2
        
    Returns:
        EventSpec with canonical threshold computed
        
    Example:
        If SPY is at $600 and moneyness=-0.15:
        threshold = 600 * (1 + (-0.15)) = 600 * 0.85 = $510
        Event = P(SPY < $510 at expiry)
    """
    import hashlib
    
    if moneyness >= 0:
        raise ValueError(
            f"moneyness must be negative for terminal_below events, got {moneyness}"
        )
    
    # SINGLE COMPUTATION - this is the canonical threshold
    threshold = spot * (1 + moneyness)
    
    # Compute event hash for uniqueness
    hash_input = f"{underlier}_{expiry}_{moneyness:.6f}_{spot:.2f}_{regime or 'default'}"
    event_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    
    return EventSpec(
        moneyness=moneyness,
        threshold=threshold,
        expiry=expiry,
        spot=spot,
        underlier=underlier,
        direction="below",
        regime=regime,
        event_hash=event_hash
    )


def create_terminal_below_event(
    underlier: str,
    expiry: str,
    spot: float,
    event_moneyness: float = -0.15
) -> EventDef:
    """
    Create a terminal_below event definition (legacy - use create_event_spec for new code).
    
    Event: P(S_T < threshold at expiry)
    
    Args:
        underlier: Ticker symbol
        expiry: Expiry date (YYYYMMDD)
        spot: Current spot price
        event_moneyness: Moneyness for threshold (default -0.15 = 15% below spot)
        
    Returns:
        EventDef for terminal_below event
        
    Example:
        If SPY is at $600 and event_moneyness=-0.15:
        threshold = 600 * (1 + (-0.15)) = 600 * 0.85 = $510
        Event = P(SPY < $510 at expiry)
    """
    if event_moneyness >= 0:
        raise ValueError(
            f"event_moneyness must be negative for terminal_below, got {event_moneyness}"
        )
    
    threshold = spot * (1 + event_moneyness)
    
    return EventDef(
        event_type="terminal_below",
        underlier=underlier,
        expiry=expiry,
        threshold=threshold,
        direction="below"
    )
