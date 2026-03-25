"""
OrderTicket Schema

Canonical schema for IBKR order tickets.
No IBKR objects - pure JSON-serializable dataclasses.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any


@dataclass
class OrderLeg:
    """Single leg of an option order."""
    
    action: str  # "BUY" or "SELL"
    right: str  # "P" (put) or "C" (call)
    strike: float
    expiry: str  # YYYYMMDD format
    quantity: int
    exchange: str = "SMART"
    
    def __post_init__(self):
        """Validate fields."""
        if self.action not in ("BUY", "SELL"):
            raise ValueError(f"action must be 'BUY' or 'SELL', got '{self.action}'")
        
        if self.right not in ("P", "C"):
            raise ValueError(f"right must be 'P' or 'C', got '{self.right}'")
        
        if self.strike <= 0:
            raise ValueError(f"strike must be >0, got {self.strike}")
        
        if self.quantity <= 0:
            raise ValueError(f"quantity must be >0, got {self.quantity}")
        
        # Validate expiry format (basic check)
        if not (len(self.expiry) == 8 and self.expiry.isdigit()):
            raise ValueError(f"expiry must be YYYYMMDD format, got '{self.expiry}'")


@dataclass
class OrderTicket:
    """
    Canonical IBKR order ticket.
    
    Represents a combo order (e.g., vertical spread) with multiple legs.
    All prices in USD.
    """
    
    symbol: str  # Underlier (e.g., "SPY")
    expiry: str  # YYYYMMDD format
    combo_type: str  # e.g., "VERTICAL_SPREAD"
    legs: List[OrderLeg]
    limit_price: float  # Debit per spread in dollars (e.g., 12.50)
    quantity: int  # Number of spreads
    tif: str = "DAY"  # Time in force
    account: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate fields."""
        if not self.symbol:
            raise ValueError("symbol cannot be empty")
        
        if not self.legs:
            raise ValueError("legs cannot be empty")
        
        if self.limit_price <= 0:
            raise ValueError(f"limit_price must be >0, got {self.limit_price}")
        
        if self.quantity <= 0:
            raise ValueError(f"quantity must be >0, got {self.quantity}")
        
        if self.tif not in ("DAY", "GTC", "IOC", "GTD"):
            raise ValueError(f"tif must be DAY/GTC/IOC/GTD, got '{self.tif}'")
        
        # Validate all legs have same expiry
        for leg in self.legs:
            if leg.expiry != self.expiry:
                raise ValueError(
                    f"All legs must have same expiry as ticket. "
                    f"Ticket expiry={self.expiry}, leg expiry={leg.expiry}"
                )
    
    def total_debit(self) -> float:
        """Calculate total debit for this order (limit_price * quantity * 100)."""
        return self.limit_price * self.quantity * 100
    
    def max_loss(self) -> float:
        """Calculate maximum loss (same as total debit for debit spreads)."""
        return self.total_debit()
    
    def max_gain(self) -> float:
        """
        Calculate maximum gain for vertical spread.
        Assumes 2 legs with same quantity.
        """
        if len(self.legs) != 2:
            raise ValueError(f"max_gain calculation requires 2 legs, got {len(self.legs)}")
        
        # For put spread: max_gain = (K_long - K_short) * 100 * quantity - total_debit
        strikes = sorted([leg.strike for leg in self.legs], reverse=True)
        strike_width = strikes[0] - strikes[1]
        
        max_gain_gross = strike_width * 100 * self.quantity
        max_gain_net = max_gain_gross - self.total_debit()
        
        return max_gain_net


def to_dict(ticket: OrderTicket) -> Dict[str, Any]:
    """
    Convert OrderTicket to JSON-serializable dict.
    
    Args:
        ticket: OrderTicket instance
        
    Returns:
        Dict representation
    """
    return asdict(ticket)


def from_candidate(
    candidate: Dict[str, Any],
    quantity: int = 1,
    account: Optional[str] = None
) -> OrderTicket:
    """
    Convert a candidate structure to an OrderTicket.
    
    Args:
        candidate: Candidate structure dict from engine output
        quantity: Number of spreads to order (default: 1)
        account: IBKR account ID (optional)
        
    Returns:
        OrderTicket instance
    """
    # Extract basic info
    symbol = candidate["underlier"]
    expiry = candidate["expiry"]
    
    # Extract strikes
    strikes = candidate.get("strikes", {})
    long_put_strike = strikes.get("long_put")
    short_put_strike = strikes.get("short_put")
    
    if long_put_strike is None or short_put_strike is None:
        raise ValueError(f"Missing strikes in candidate: {strikes}")
    
    # Create legs for put spread
    # Long put = BUY
    # Short put = SELL
    legs = [
        OrderLeg(
            action="BUY",
            right="P",
            strike=long_put_strike,
            expiry=expiry,
            quantity=quantity
        ),
        OrderLeg(
            action="SELL",
            right="P",
            strike=short_put_strike,
            expiry=expiry,
            quantity=quantity
        )
    ]
    
    # Get debit per contract (this is the limit price per spread)
    # Debit is stored in per-contract units (e.g., $1250 for a spread)
    # But limit_price should be per-spread-unit (divide by 100)
    debit_per_contract = candidate.get("debit_per_contract")
    if debit_per_contract is None or debit_per_contract <= 0:
        raise ValueError(f"Invalid debit_per_contract: {debit_per_contract}")
    
    # Convert to limit price (per spread, in dollars)
    # debit_per_contract is in cents (e.g., 1250 = $12.50)
    limit_price = debit_per_contract / 100.0
    
    # Extract metadata for order ticket
    metadata = {
        "rank": candidate.get("rank"),
        "ev_per_contract": candidate.get("ev_per_contract"),
        "ev_per_dollar": candidate.get("ev_per_dollar"),
        "max_loss_per_contract": candidate.get("max_loss_per_contract"),
        "max_gain_per_contract": candidate.get("max_gain_per_contract"),
        "prob_profit": candidate.get("prob_profit"),
        "spread_width": candidate.get("spread_width"),
        "moneyness_target": candidate.get("moneyness_target"),
        "reason_selected": candidate.get("reason_selected"),
    }
    
    # Remove None values
    metadata = {k: v for k, v in metadata.items() if v is not None}
    
    ticket = OrderTicket(
        symbol=symbol,
        expiry=expiry,
        combo_type="VERTICAL_SPREAD",
        legs=legs,
        limit_price=limit_price,
        quantity=quantity,
        tif="DAY",
        account=account,
        metadata=metadata
    )
    
    return ticket
