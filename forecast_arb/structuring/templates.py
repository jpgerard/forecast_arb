"""
Option structure templates (put spreads, call spreads, strangles).
"""

from typing import Dict, List
from forecast_arb.structuring.option_math import price_option


def generate_put_spread(
    underlier: str,
    expiry: str,
    S0: float,
    K_long: float,
    K_short: float,
    r: float,
    sigma: float,
    T: float,
    q: float = 0.0
) -> Dict:
    """
    Generate a bear put spread (long higher strike, short lower strike).
    
    Profit if underlier drops below K_long.
    Max profit = (K_long - K_short) - debit
    Max loss = debit
    Breakeven = K_long - debit
    
    Args:
        underlier: Ticker symbol
        expiry: Expiration date (YYYY-MM-DD)
        S0: Current spot price
        K_long: Strike of long put (higher)
        K_short: Strike of short put (lower)
        r: Risk-free rate (annual)
        sigma: Implied volatility (annual)
        T: Time to expiry (years)
        q: Dividend yield (annual)
        
    Returns:
        Dict with structure details (per-share values)
    """
    if K_long <= K_short:
        raise ValueError("Put spread: K_long must be > K_short")
    
    # Price options using Black-Scholes (per-share)
    price_long = price_option(S0, K_long, T, r, sigma, q, "p")
    price_short = price_option(S0, K_short, T, r, sigma, q, "p")
    
    # Debit paid (per-share, positive number)
    debit = price_long - price_short
    
    # Max loss = debit (per-share, positive)
    max_loss = debit
    
    # Max gain = spread width - debit (per-share, positive)
    max_gain = (K_long - K_short) - debit
    
    # Breakeven = K_long - debit
    breakeven = K_long - debit
    
    return {
        "template_name": "put_spread",
        "underlier": underlier,
        "expiry": expiry,
        "legs": [
            {
                "type": "put",
                "strike": K_long,
                "side": "long",
                "price": price_long,
                "quantity": 1
            },
            {
                "type": "put",
                "strike": K_short,
                "side": "short",
                "price": price_short,
                "quantity": 1
            }
        ],
        "debit": debit,           # Per-share, positive
        "max_loss": max_loss,     # Per-share, positive
        "max_gain": max_gain,     # Per-share, positive
        "breakeven": breakeven,   # Price level
        "multiplier": 100         # Shares per contract
    }


def generate_call_spread(
    underlier: str,
    expiry: str,
    S0: float,
    K_long: float,
    K_short: float,
    r: float,
    sigma: float,
    T: float,
    q: float = 0.0
) -> Dict:
    """
    Generate a bull call spread (long lower strike, short higher strike).
    
    Profit if underlier rises above K_long.
    Max profit = (K_short - K_long) - net_debit
    Max loss = net_debit
    
    Args:
        underlier: Ticker symbol
        expiry: Expiration date (YYYY-MM-DD)
        S0: Current spot price
        K_long: Strike of long call (lower)
        K_short: Strike of short call (higher)
        r: Risk-free rate (annual)
        sigma: Implied volatility (annual)
        T: Time to expiry (years)
        q: Dividend yield (annual)
        
    Returns:
        Dict with structure details
    """
    if K_long >= K_short:
        raise ValueError("Call spread: K_long must be < K_short")
    
    # Price options using Black-Scholes
    price_long = price_option(S0, K_long, T, r, sigma, q, "c")
    price_short = price_option(S0, K_short, T, r, sigma, q, "c")
    
    # Net premium (negative = debit)
    net_premium = -price_long + price_short
    
    # Max loss = premium paid
    max_loss = net_premium
    
    # Max gain = spread width - premium paid
    max_gain = (K_short - K_long) + net_premium
    
    return {
        "template_name": "call_spread",
        "underlier": underlier,
        "expiry": expiry,
        "legs": [
            {
                "type": "call",
                "strike": K_long,
                "side": "long",
                "price": price_long,
                "quantity": 1
            },
            {
                "type": "call",
                "strike": K_short,
                "side": "short",
                "price": price_short,
                "quantity": 1
            }
        ],
        "premium": net_premium,
        "max_loss": max_loss,
        "max_gain": max_gain
    }


def generate_strangle(
    underlier: str,
    expiry: str,
    S0: float,
    K_put: float,
    K_call: float,
    r: float,
    sigma: float,
    T: float,
    q: float = 0.0,
    long: bool = True
) -> Dict:
    """
    Generate a long or short strangle.
    
    Long strangle: Profit from large move in either direction
    Short strangle: Profit if underlier stays between strikes
    
    Args:
        underlier: Ticker symbol
        expiry: Expiration date (YYYY-MM-DD)
        S0: Current spot price
        K_put: Strike of put (lower)
        K_call: Strike of call (higher)
        r: Risk-free rate (annual)
        sigma: Implied volatility (annual)
        T: Time to expiry (years)
        q: Dividend yield (annual)
        long: True for long strangle, False for short
        
    Returns:
        Dict with structure details
    """
    if K_put >= K_call:
        raise ValueError("Strangle: K_put must be < K_call")
    
    # Price options using Black-Scholes
    price_put = price_option(S0, K_put, T, r, sigma, q, "p")
    price_call = price_option(S0, K_call, T, r, sigma, q, "c")
    
    # Net premium
    if long:
        net_premium = -(price_put + price_call)
        max_loss = net_premium
        max_gain = float('inf')
    else:
        net_premium = price_put + price_call
        max_loss = float('-inf')
        max_gain = net_premium
    
    side = "long" if long else "short"
    
    return {
        "template_name": "strangle",
        "underlier": underlier,
        "expiry": expiry,
        "legs": [
            {
                "type": "put",
                "strike": K_put,
                "side": side,
                "price": price_put,
                "quantity": 1
            },
            {
                "type": "call",
                "strike": K_call,
                "side": side,
                "price": price_call,
                "quantity": 1
            }
        ],
        "premium": net_premium,
        "max_loss": max_loss,
        "max_gain": max_gain
    }


def compute_payoff(structure: Dict, S_T: float) -> float:
    """
    Compute payoff of an option structure at expiration (per-share).
    
    Args:
        structure: Option structure dict with legs and debit
        S_T: Terminal spot price
        
    Returns:
        Total payoff per-share (positive = profit, negative = loss)
    """
    payoff = 0.0
    
    for leg in structure["legs"]:
        option_type = leg["type"]
        strike = leg["strike"]
        side = leg["side"]
        quantity = leg["quantity"]
        
        # Compute intrinsic value at expiration
        if option_type == "call":
            intrinsic = max(0, S_T - strike)
        elif option_type == "put":
            intrinsic = max(0, strike - S_T)
        else:
            raise ValueError(f"Unknown option type: {option_type}")
        
        # Apply position (long = +1, short = -1)
        multiplier = 1 if side == "long" else -1
        
        payoff += multiplier * intrinsic * quantity
    
    # Subtract debit paid (debit is positive, so we subtract it)
    debit = structure.get("debit", 0)
    payoff -= debit
    
    return payoff


# Keep old functions for backward compatibility
def create_put_spread(*args, **kwargs):
    """Deprecated: Use generate_put_spread instead."""
    raise DeprecationWarning("Use generate_put_spread instead")


def create_call_spread(*args, **kwargs):
    """Deprecated: Use generate_call_spread instead."""
    raise DeprecationWarning("Use generate_call_spread instead")


def create_strangle(*args, **kwargs):
    """Deprecated: Use generate_strangle instead."""
    raise DeprecationWarning("Use generate_strangle instead")
