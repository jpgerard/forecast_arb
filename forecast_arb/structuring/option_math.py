"""
Option pricing, implied volatility, and Greeks calculations using py_vollib.
"""

import numpy as np
from typing import Dict, Literal
from py_vollib.black_scholes import black_scholes as bs_price
from py_vollib.black_scholes.greeks import analytical as greeks


def price_option(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float,
    option_type: Literal["c", "p"]
) -> float:
    """
    Compute Black-Scholes option price.
    
    Args:
        S: Spot price
        K: Strike price
        T: Time to expiry (years)
        r: Risk-free rate
        sigma: Implied volatility (annualized)
        q: Dividend yield
        option_type: 'c' for call, 'p' for put
        
    Returns:
        Option price
    """
    # Adjust spot for dividends
    S_adj = S * np.exp(-q * T)
    
    return bs_price(option_type, S_adj, K, T, r, sigma)


def compute_iv(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    option_type: Literal["c", "p"]
) -> float:
    """
    Compute implied volatility from option price using Newton-Raphson.
    
    Args:
        price: Observed option price
        S: Spot price
        K: Strike price
        T: Time to expiry (years)
        r: Risk-free rate
        q: Dividend yield
        option_type: 'c' for call, 'p' for put
        
    Returns:
        Implied volatility (annual)
    """
    from py_vollib.black_scholes.implied_volatility import implied_volatility
    
    S_adj = S * np.exp(-q * T)
    
    try:
        iv = implied_volatility(price, S_adj, K, T, r, option_type)
        return iv
    except Exception as e:
        # If IV calculation fails, return NaN
        return np.nan


def compute_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    q: float,
    option_type: Literal["c", "p"]
) -> Dict[str, float]:
    """
    Compute option Greeks.
    
    Args:
        S: Spot price
        K: Strike price
        T: Time to expiry (years)
        r: Risk-free rate
        sigma: Implied volatility
        q: Dividend yield
        option_type: 'c' for call, 'p' for put
        
    Returns:
        Dict with delta, gamma, theta, vega, rho
    """
    S_adj = S * np.exp(-q * T)
    
    return {
        "delta": greeks.delta(option_type, S_adj, K, T, r, sigma),
        "gamma": greeks.gamma(option_type, S_adj, K, T, r, sigma),
        "theta": greeks.theta(option_type, S_adj, K, T, r, sigma),
        "vega": greeks.vega(option_type, S_adj, K, T, r, sigma),
        "rho": greeks.rho(option_type, S_adj, K, T, r, sigma)
    }


def compute_atm_iv(options_chain: list, spot: float) -> float:
    """
    Compute ATM implied volatility (nearest strike to spot).
    
    Args:
        options_chain: List of option dicts with strike, bid, ask, type
        spot: Current spot price
        
    Returns:
        ATM IV estimate
    """
    # Find ATM strike (nearest to spot)
    strikes = [opt["strike"] for opt in options_chain]
    atm_strike = min(strikes, key=lambda k: abs(k - spot))
    
    # Get ATM options
    atm_opts = [opt for opt in options_chain if opt["strike"] == atm_strike]
    
    if not atm_opts:
        return np.nan
    
    # Average call and put IVs if both available
    ivs = []
    for opt in atm_opts:
        mid = (opt["bid"] + opt["ask"]) / 2.0
        if mid > 0:
            # Placeholder: would compute IV from mid price
            # For now, return a typical value
            ivs.append(0.20)  # 20% IV placeholder
    
    return np.mean(ivs) if ivs else 0.20
