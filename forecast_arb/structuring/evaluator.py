"""
Structure evaluator: Compute EV and statistics via Monte Carlo simulation.
"""

import numpy as np
from typing import Dict, List
from forecast_arb.structuring.templates import compute_payoff


def simulate_paths(
    S0: float,
    mu: float,
    sigma: float,
    T: float,
    n_paths: int,
    seed: int = None
) -> np.ndarray:
    """
    Simulate terminal stock prices under lognormal dynamics.
    
    S_T = S_0 * exp((mu - 0.5*sigma^2)*T + sigma*sqrt(T)*Z)
    
    Args:
        S0: Current spot price
        mu: Drift parameter (annualized)
        sigma: Volatility (annualized)
        T: Time to expiry (years)
        n_paths: Number of Monte Carlo paths
        seed: Random seed for reproducibility
        
    Returns:
        Array of simulated terminal prices
    """
    if seed is not None:
        np.random.seed(seed)
    
    # Generate random normal draws
    Z = np.random.normal(0, 1, n_paths)
    
    # Lognormal formula
    log_return = (mu - 0.5 * sigma**2) * T + sigma * np.sqrt(T) * Z
    S_T = S0 * np.exp(log_return)
    
    return S_T


def compute_statistics(payoffs: np.ndarray) -> Dict:
    """
    Compute statistics from payoff distribution.
    
    Args:
        payoffs: Array of payoffs from Monte Carlo simulation
        
    Returns:
        Dict with ev, std, prob_profit, percentiles
    """
    return {
        "ev": float(np.mean(payoffs)),
        "std": float(np.std(payoffs)),
        "prob_profit": float(np.mean(payoffs > 0)),
        "percentiles": {
            "p05": float(np.percentile(payoffs, 5)),
            "p25": float(np.percentile(payoffs, 25)),
            "p50": float(np.percentile(payoffs, 50)),
            "p75": float(np.percentile(payoffs, 75)),
            "p95": float(np.percentile(payoffs, 95))
        }
    }


def evaluate_structure(
    structure: Dict,
    mu: float,
    sigma: float,
    S0: float,
    T: float,
    n_paths: int = 10000,
    seed: int = None
) -> Dict:
    """
    Evaluate option structure via Monte Carlo simulation.
    
    Args:
        structure: Option structure dict (from generate_* functions)
        mu: Calibrated drift parameter
        sigma: Volatility (annualized)
        S0: Current spot price
        T: Time to expiry (years)
        n_paths: Number of MC paths
        seed: Random seed for reproducibility
        
    Returns:
        Dict with all original structure fields plus evaluation statistics
    """
    # Simulate terminal prices
    S_T = simulate_paths(S0, mu, sigma, T, n_paths, seed)
    
    # Compute payoffs for all terminal prices
    payoffs = np.array([compute_payoff(structure, s_t) for s_t in S_T])
    
    # Compute statistics
    stats = compute_statistics(payoffs)
    
    # Add max loss and max gain from simulation
    stats["max_loss"] = float(np.min(payoffs))
    stats["max_gain"] = float(np.max(payoffs))
    
    # Preserve all original structure fields and add evaluation stats
    result = {**structure, **stats}
    
    return result


def evaluate_multiple_structures(
    structures: List[Dict],
    mu: float,
    sigma: float,
    S0: float,
    T: float,
    n_paths: int = 10000,
    seed: int = None
) -> List[Dict]:
    """
    Evaluate multiple structures in batch.
    
    Args:
        structures: List of option structure dicts
        mu: Drift parameter
        sigma: Volatility
        S0: Spot price
        T: Time to expiry
        n_paths: MC paths
        seed: Base seed (incremented for each structure if provided)
        
    Returns:
        List of evaluation dicts
    """
    results = []
    
    for i, structure in enumerate(structures):
        # Use different seed for each structure if seed provided
        structure_seed = (seed + i) if seed is not None else None
        
        eval_result = evaluate_structure(
            structure, mu, sigma, S0, T, n_paths, structure_seed
        )
        
        results.append(eval_result)
    
    return results
