"""
Unit tests for structure evaluator (Monte Carlo).
"""

import pytest
import numpy as np
from forecast_arb.structuring.evaluator import (
    evaluate_structure,
    simulate_paths,
    compute_statistics
)
from forecast_arb.structuring.templates import generate_put_spread, generate_call_spread


def test_simulate_paths():
    """Test path simulation."""
    S0 = 100.0
    mu = 0.1  # 10% drift
    sigma = 0.15
    T = 30/365
    n_paths = 1000
    seed = 42
    
    paths = simulate_paths(S0, mu, sigma, T, n_paths, seed)
    
    assert len(paths) == n_paths
    assert all(p > 0 for p in paths), "All paths should be positive"
    
    # Mean should be approximately S0 * exp(mu * T)
    expected_mean = S0 * np.exp(mu * T)
    actual_mean = np.mean(paths)
    
    # Within 10% of expected (stochastic)
    assert abs(actual_mean - expected_mean) / expected_mean < 0.1


def test_simulate_paths_deterministic():
    """Test that simulation is deterministic with seed."""
    params = {
        "S0": 100.0,
        "mu": 0.1,
        "sigma": 0.15,
        "T": 30/365,
        "n_paths": 1000,
        "seed": 42
    }
    
    paths1 = simulate_paths(**params)
    paths2 = simulate_paths(**params)
    
    assert np.allclose(paths1, paths2), "Same seed should give identical paths"


def test_compute_statistics():
    """Test statistics computation."""
    payoffs = np.array([-2, -1, 0, 1, 2, 3, 5, 10])
    
    stats = compute_statistics(payoffs)
    
    assert "ev" in stats
    assert "std" in stats
    assert "prob_profit" in stats
    assert "percentiles" in stats
    
    # EV = mean
    assert abs(stats["ev"] - np.mean(payoffs)) < 0.01
    
    # Std
    assert abs(stats["std"] - np.std(payoffs)) < 0.01
    
    # Prob profit = fraction > 0
    assert abs(stats["prob_profit"] - 0.625) < 0.01  # 5/8
    
    # Percentiles
    assert "p05" in stats["percentiles"]
    assert "p50" in stats["percentiles"]
    assert "p95" in stats["percentiles"]


def test_evaluate_structure_put_spread():
    """Test evaluation of put spread."""
    # Generate put spread
    structure = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=490.0,
        K_short=480.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    # Evaluate with neutral drift
    evaluation = evaluate_structure(
        structure=structure,
        mu=0.0,
        sigma=0.15,
        S0=500.0,
        T=30/365,
        n_paths=5000,
        seed=42
    )
    
    # Should have all evaluation fields
    assert "ev" in evaluation
    assert "std" in evaluation
    assert "prob_profit" in evaluation
    assert "percentiles" in evaluation
    assert "max_loss" in evaluation
    assert "max_gain" in evaluation
    
    # Max loss should be negative (limited risk)
    assert evaluation["max_loss"] < 0
    
    # Max gain should be positive
    assert evaluation["max_gain"] > 0
    
    # Prob profit should be between 0 and 1
    assert 0 <= evaluation["prob_profit"] <= 1


def test_evaluate_structure_call_spread():
    """Test evaluation of call spread."""
    structure = generate_call_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=510.0,
        K_short=520.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    # Evaluate with bullish drift
    evaluation = evaluate_structure(
        structure=structure,
        mu=0.2,  # Strong bullish
        sigma=0.15,
        S0=500.0,
        T=30/365,
        n_paths=5000,
        seed=42
    )
    
    # With bullish drift, EV should be positive
    assert evaluation["ev"] > 0, "Call spread should have positive EV with bullish drift"
    
    # Prob profit should be reasonably high
    assert evaluation["prob_profit"] > 0.3


def test_evaluate_structure_bearish_scenario():
    """Test put spread in bearish scenario."""
    structure = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=495.0,
        K_short=485.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    # Evaluate with bearish drift
    evaluation = evaluate_structure(
        structure=structure,
        mu=-0.2,  # Strong bearish
        sigma=0.15,
        S0=500.0,
        T=30/365,
        n_paths=5000,
        seed=42
    )
    
    # With bearish drift, put spread should have positive EV
    assert evaluation["ev"] > 0, "Put spread should profit in bearish scenario"


def test_evaluate_structure_deterministic():
    """Test that evaluation is deterministic."""
    structure = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=490.0,
        K_short=480.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    params = {
        "structure": structure,
        "mu": 0.1,
        "sigma": 0.15,
        "S0": 500.0,
        "T": 30/365,
        "n_paths": 1000,
        "seed": 42
    }
    
    eval1 = evaluate_structure(**params)
    eval2 = evaluate_structure(**params)
    
    assert eval1["ev"] == eval2["ev"], "EV should be deterministic"
    assert eval1["std"] == eval2["std"], "Std should be deterministic"
    assert eval1["prob_profit"] == eval2["prob_profit"], "Prob profit should be deterministic"


def test_evaluate_structure_convergence():
    """Test that more paths give more stable results."""
    structure = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=490.0,
        K_short=480.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    # Low path count
    eval_low = evaluate_structure(
        structure=structure,
        mu=0.1,
        sigma=0.15,
        S0=500.0,
        T=30/365,
        n_paths=500,
        seed=42
    )
    
    # High path count
    eval_high = evaluate_structure(
        structure=structure,
        mu=0.1,
        sigma=0.15,
        S0=500.0,
        T=30/365,
        n_paths=10000,
        seed=42
    )
    
    # Results should be similar (within 20%)
    assert abs(eval_low["ev"] - eval_high["ev"]) / abs(eval_high["ev"]) < 0.2


def test_evaluate_structure_preserves_metadata():
    """Test that evaluation preserves structure metadata."""
    structure = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=490.0,
        K_short=480.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    evaluation = evaluate_structure(
        structure=structure,
        mu=0.1,
        sigma=0.15,
        S0=500.0,
        T=30/365,
        n_paths=1000,
        seed=42
    )
    
    # Original structure fields should be preserved
    assert evaluation["template_name"] == "put_spread"
    assert evaluation["underlier"] == "SPY"
    assert evaluation["expiry"] == "2026-03-15"
    assert "legs" in evaluation


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
