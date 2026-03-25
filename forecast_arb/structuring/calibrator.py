"""
Distribution calibration: Map event probability to lognormal parameters.

Assumption: S_T ~ Lognormal(mu, sigma^2 * T)
Given: P(S_T <= K_event) = p_event and sigma (from ATM IV)
Solve for: mu such that constraint is satisfied
"""

import numpy as np
from scipy.stats import norm
from typing import Tuple


def calibrate_distribution(
    spot: float,
    K_event: float,
    p_event: float,
    sigma: float,
    T: float,
    r: float = 0.0,
    q: float = 0.0
) -> Tuple[float, float]:
    """
    Calibrate lognormal distribution parameters given event probability.
    
    Assume S_T ~ Lognormal, so log(S_T) ~ Normal(mu_log, sigma^2 * T)
    
    Under risk-neutral measure:
    mu_log = log(S_0) + (r - q - 0.5 * sigma^2) * T
    
    But we want to calibrate so that P(S_T <= K_event) = p_event
    This means: P(log(S_T) <= log(K_event)) = p_event
    Which gives: Phi((log(K_event) - mu_log) / (sigma * sqrt(T))) = p_event
    
    Solve for adjusted mu_log:
    mu_log = log(K_event) - sigma * sqrt(T) * Phi^(-1)(p_event)
    
    Args:
        spot: Current spot price
        K_event: Event strike/threshold
        p_event: Probability that S_T <= K_event (or >= depending on event type)
        sigma: Annualized volatility (ATM IV)
        T: Time to expiry (years)
        r: Risk-free rate
        q: Dividend yield
        
    Returns:
        Tuple of (mu, sigma) where:
        - mu: drift parameter for lognormal (annualized)
        - sigma: volatility parameter (annualized)
    """
    if p_event <= 0 or p_event >= 1:
        raise ValueError(f"p_event must be in (0, 1), got {p_event}")
    
    if T <= 0:
        raise ValueError(f"Time to expiry must be positive, got {T}")
    
    # Inverse CDF to get z-score for target probability
    z = norm.ppf(p_event)
    
    # Solve for mu_log such that P(log(S_T) <= log(K_event)) = p_event
    log_K = np.log(K_event)
    mu_log = log_K - sigma * np.sqrt(T) * z
    
    # Convert to annualized drift mu
    # S_T = S_0 * exp((mu - 0.5 * sigma^2) * T + sigma * sqrt(T) * Z)
    # So mu = (mu_log - log(S_0))/T + 0.5 * sigma^2
    log_S0 = np.log(spot)
    mu = (mu_log - log_S0) / T + 0.5 * sigma**2
    
    return mu, sigma


def validate_calibration(
    spot: float,
    K_event: float,
    p_event: float,
    mu: float,
    sigma: float,
    T: float
) -> float:
    """
    Validate calibration by computing implied probability.
    
    Args:
        spot: Current spot price
        K_event: Event threshold
        p_event: Target probability
        mu: Calibrated drift
        sigma: Volatility
        T: Time to expiry
        
    Returns:
        Implied probability P(S_T <= K_event) under calibrated distribution
    """
    # Under calibrated params: log(S_T) ~ N(log(S_0) + (mu - 0.5*sigma^2)*T, sigma^2*T)
    mu_log = np.log(spot) + (mu - 0.5 * sigma**2) * T
    std_log = sigma * np.sqrt(T)
    
    # P(S_T <= K_event) = P(log(S_T) <= log(K_event))
    z = (np.log(K_event) - mu_log) / std_log
    p_implied = norm.cdf(z)
    
    return p_implied


def lognormal_cdf(K: float, S0: float, mu: float, sigma: float, T: float) -> float:
    """
    Compute P(S_T >= K) under lognormal distribution.
    
    S_T = S0 * exp((mu - 0.5*sigma^2)*T + sigma*sqrt(T)*Z)
    where Z ~ N(0,1)
    
    Args:
        K: Barrier/strike level
        S0: Initial spot price
        mu: Drift parameter (annualized)
        sigma: Volatility parameter (annualized)
        T: Time to expiry (years)
        
    Returns:
        Probability P(S_T >= K)
    """
    if T <= 0:
        return 1.0 if S0 >= K else 0.0
    
    # log(S_T) ~ N(log(S0) + (mu - 0.5*sigma^2)*T, sigma^2*T)
    mu_log = np.log(S0) + (mu - 0.5 * sigma**2) * T
    std_log = sigma * np.sqrt(T)
    
    # P(S_T >= K) = P(log(S_T) >= log(K))
    z = (np.log(K) - mu_log) / std_log
    
    # P(Z >= z) = 1 - Phi(z)
    return 1.0 - norm.cdf(z)


def calibrate_drift(
    p_event: float,
    S0: float,
    K_barrier: float,
    T: float,
    sigma: float,
    n_samples: int = 10000,
    seed: int = None
) -> Tuple[float, float]:
    """
    Calibrate drift parameter to match event probability via Monte Carlo.
    
    Given P(S_T >= K_barrier) = p_event, find mu such that this holds.
    
    Args:
        p_event: Target probability (0, 1)
        S0: Initial spot price
        K_barrier: Barrier level
        T: Time to expiry (years)
        sigma: Volatility (annualized)
        n_samples: Number of Monte Carlo samples for validation
        seed: Random seed for reproducibility
        
    Returns:
        Tuple of (mu, p_achieved) where:
        - mu: Calibrated drift parameter
        - p_achieved: Actual probability achieved via Monte Carlo
    """
    assert 0 < p_event < 1, f"p_event must be in (0, 1), got {p_event}"
    assert T > 0, f"T must be positive, got {T}"
    assert sigma > 0, f"sigma must be positive, got {sigma}"
    
    # Use analytical solution: P(S_T >= K) = p_event
    # Find mu such that this holds
    # P(S_T >= K) = P(log(S_T) >= log(K))
    # = P((log(S0) + (mu - 0.5*sigma^2)*T + sigma*sqrt(T)*Z) >= log(K))
    # = P(Z >= (log(K) - log(S0) - (mu - 0.5*sigma^2)*T) / (sigma*sqrt(T)))
    # = 1 - Phi(z)
    # So: Phi(z) = 1 - p_event
    # z = Phi^(-1)(1 - p_event)
    
    z_target = norm.ppf(1 - p_event)
    
    # Solve for mu:
    # z = (log(K) - log(S0) - (mu - 0.5*sigma^2)*T) / (sigma*sqrt(T))
    # mu = (log(K) - log(S0) - z*sigma*sqrt(T)) / T + 0.5*sigma^2
    
    log_ratio = np.log(K_barrier / S0)
    mu = (log_ratio - z_target * sigma * np.sqrt(T)) / T + 0.5 * sigma**2
    
    # Validate with Monte Carlo
    if seed is not None:
        np.random.seed(seed)
    
    # Simulate paths: S_T = S0 * exp((mu - 0.5*sigma^2)*T + sigma*sqrt(T)*Z)
    Z = np.random.normal(0, 1, n_samples)
    S_T = S0 * np.exp((mu - 0.5 * sigma**2) * T + sigma * np.sqrt(T) * Z)
    
    # Count how many paths cross barrier
    p_achieved = np.mean(S_T >= K_barrier)
    
    return mu, p_achieved


def implied_drift_from_price_target(
    S0: float,
    S_target: float,
    T: float,
    sigma: float
) -> float:
    """
    Compute implied drift such that E[S_T] = S_target.
    
    For lognormal: E[S_T] = S0 * exp(mu * T)
    So: mu = log(S_target / S0) / T
    
    Args:
        S0: Initial spot price
        S_target: Target expected price at T
        T: Time to expiry (years)
        sigma: Volatility (not used for expectation, but included for consistency)
        
    Returns:
        Drift parameter mu
    """
    assert T > 0, f"T must be positive, got {T}"
    assert S0 > 0 and S_target > 0, "Prices must be positive"
    
    mu = np.log(S_target / S0) / T
    return mu
