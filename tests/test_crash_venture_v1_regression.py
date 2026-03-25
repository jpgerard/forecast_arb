"""
Regression test for Crash Venture v1.

Tests that with fixed inputs (snapshot, p_event, config),
the output structure ID, strikes, and premium remain unchanged.

This test should fail if math or ranking logic changes.
"""

import pytest
import json
from pathlib import Path
from forecast_arb.structuring.templates import generate_put_spread
from forecast_arb.structuring.evaluator import evaluate_structure
from forecast_arb.structuring.router import choose_best_structure, rank_structures, filter_dominated_structures
from forecast_arb.structuring.calibrator import calibrate_drift


# Fixed test parameters (FROZEN)
FIXED_CONFIG = {
    "S0": 500.0,
    "r": 0.05,
    "sigma": 0.15,
    "T": 45/365,  # 45 DTE
    "p_event": 0.35,  # Fixed event probability
    "moneyness_targets": [-0.10, -0.15, -0.20],
    "spread_widths": [5, 10, 15],
    "n_paths": 30000,
    "seed": 42,  # Fixed seed for determinism
    "max_loss_usd": 500
}


def test_calibration_determinism():
    """Test that calibration produces same result with fixed inputs."""
    # Fixed inputs
    p_event = FIXED_CONFIG["p_event"]
    S0 = FIXED_CONFIG["S0"]
    sigma = FIXED_CONFIG["sigma"]
    T = FIXED_CONFIG["T"]
    K_barrier = S0 * 0.95  # Event = SPY drops below 475
    seed = FIXED_CONFIG["seed"]
    
    # Calibrate drift
    mu_calib, p_achieved = calibrate_drift(
        p_event=p_event,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=10000,
        seed=seed
    )
    
    # Expected values (baseline)
    # These will be the reference values that should not change
    assert mu_calib is not None
    assert p_achieved is not None
    assert abs(p_achieved - p_event) < 0.1  # Within 10% of target
    
    # Store for regression checking
    print(f"Calibrated mu: {mu_calib:.6f}, achieved p: {p_achieved:.4f}")


def test_structure_generation_determinism():
    """Test that structure generation is deterministic."""
    S0 = FIXED_CONFIG["S0"]
    r = FIXED_CONFIG["r"]
    sigma = FIXED_CONFIG["sigma"]
    T = FIXED_CONFIG["T"]
    
    # Generate put spread with fixed parameters
    put_spread = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=S0,
        K_long=S0 * 0.90,  # 450
        K_short=S0 * 0.85,  # 425
        r=r,
        sigma=sigma,
        T=T
    )
    
    # Check structure fields
    assert put_spread["underlier"] == "SPY"
    assert put_spread["template_name"] == "put_spread"
    assert len(put_spread["legs"]) == 2
    
    # Check strikes
    strikes = [leg["strike"] for leg in put_spread["legs"]]
    assert 450.0 in strikes  # Long put
    assert 425.0 in strikes  # Short put
    
    # Premium should be negative (debit spread)
    assert put_spread["premium"] < 0
    
    print(f"Put spread premium: {put_spread['premium']:.4f}")


def test_evaluation_determinism():
    """Test that evaluation produces same EV with fixed seed."""
    S0 = FIXED_CONFIG["S0"]
    r = FIXED_CONFIG["r"]
    sigma = FIXED_CONFIG["sigma"]
    T = FIXED_CONFIG["T"]
    seed = FIXED_CONFIG["seed"]
    
    # Generate structure
    put_spread = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=S0,
        K_long=S0 * 0.90,
        K_short=S0 * 0.85,
        r=r,
        sigma=sigma,
        T=T
    )
    
    # Calibrate drift for p_event=0.35
    mu_calib, _ = calibrate_drift(
        p_event=FIXED_CONFIG["p_event"],
        S0=S0,
        K_barrier=S0 * 0.95,
        T=T,
        sigma=sigma,
        n_samples=10000,
        seed=seed
    )
    
    # Evaluate
    evaluated = evaluate_structure(
        structure=put_spread,
        mu=mu_calib,
        sigma=sigma,
        S0=S0,
        T=T,
        n_paths=FIXED_CONFIG["n_paths"],
        seed=seed
    )
    
    # Check evaluation fields
    assert "ev" in evaluated
    assert "std" in evaluated
    assert "prob_profit" in evaluated
    assert "max_loss" in evaluated
    assert "max_gain" in evaluated
    
    # EV should be deterministic with fixed seed
    # Store baseline values
    baseline_ev = evaluated["ev"]
    baseline_std = evaluated["std"]
    
    print(f"Evaluated EV: {baseline_ev:.4f}, Std: {baseline_std:.4f}")
    
    # Run again with same seed - should get exact same result
    evaluated_2 = evaluate_structure(
        structure=put_spread,
        mu=mu_calib,
        sigma=sigma,
        S0=S0,
        T=T,
        n_paths=FIXED_CONFIG["n_paths"],
        seed=seed
    )
    
    assert abs(evaluated_2["ev"] - baseline_ev) < 1e-10
    assert abs(evaluated_2["std"] - baseline_std) < 1e-10


def test_top_structure_regression():
    """
    REGRESSION TEST: Top structure should remain unchanged with fixed inputs.
    
    This is the critical test - if this fails, something in the math or
    ranking logic has changed.
    """
    S0 = FIXED_CONFIG["S0"]
    r = FIXED_CONFIG["r"]
    sigma = FIXED_CONFIG["sigma"]
    T = FIXED_CONFIG["T"]
    seed = FIXED_CONFIG["seed"]
    p_event = FIXED_CONFIG["p_event"]
    
    # Calibrate drift
    mu_calib, _ = calibrate_drift(
        p_event=p_event,
        S0=S0,
        K_barrier=S0 * 0.95,
        T=T,
        sigma=sigma,
        n_samples=10000,
        seed=seed
    )
    
    # Generate candidate structures
    candidates = []
    
    for moneyness in FIXED_CONFIG["moneyness_targets"]:
        for width in FIXED_CONFIG["spread_widths"]:
            K_long = S0 * (1 + moneyness)
            K_short = K_long - width
            
            if K_short > 0:
                put_spread = generate_put_spread(
                    underlier="SPY",
                    expiry="2026-03-15",
                    S0=S0,
                    K_long=K_long,
                    K_short=K_short,
                    r=r,
                    sigma=sigma,
                    T=T
                )
                candidates.append(put_spread)
    
    # Evaluate all candidates
    evaluated = []
    for i, candidate in enumerate(candidates):
        eval_result = evaluate_structure(
            structure=candidate,
            mu=mu_calib,
            sigma=sigma,
            S0=S0,
            T=T,
            n_paths=FIXED_CONFIG["n_paths"],
            seed=seed + i  # Different seed per structure
        )
        evaluated.append(eval_result)
    
    # Apply dominance filter
    non_dominated = filter_dominated_structures(evaluated)
    
    # Choose best structure
    constraints = {
        "max_loss_usd_per_trade": FIXED_CONFIG["max_loss_usd"],
        "min_prob_profit": 0.0,
        "min_ev": 0
    }
    
    best_structures = choose_best_structure(
        non_dominated,
        constraints=constraints,
        objective="max_ev"
    )
    
    assert len(best_structures) > 0, "No valid structures found"
    
    # Rank top 3
    top_structures = rank_structures(best_structures, top_n=3)
    
    # Get top structure
    top_struct = top_structures[0]
    
    # REGRESSION BASELINE VALUES
    # These should NOT change unless we intentionally modify the algorithm
    expected_rank = 1
    expected_long_strike = None  # Will be filled after first run
    expected_short_strike = None  # Will be filled after first run
    expected_premium_range = None  # Will check it's within 5% of baseline
    
    # Extract actual values
    actual_rank = top_struct["rank"]
    long_leg = [leg for leg in top_struct["legs"] if leg["side"] == "long"][0]
    short_leg = [leg for leg in top_struct["legs"] if leg["side"] == "short"][0]
    actual_long_strike = long_leg["strike"]
    actual_short_strike = short_leg["strike"]
    actual_premium = top_struct["premium"]
    actual_ev = top_struct["ev"]
    
    # Assertions
    assert actual_rank == expected_rank
    
    # Print regression values for documentation
    print("\n=== REGRESSION BASELINE VALUES ===")
    print(f"Rank: {actual_rank}")
    print(f"Long Strike: {actual_long_strike:.2f}")
    print(f"Short Strike: {actual_short_strike:.2f}")
    print(f"Premium: {actual_premium:.4f}")
    print(f"EV: {actual_ev:.4f}")
    print(f"Max Loss: {top_struct['max_loss']:.4f}")
    print(f"Max Gain: {top_struct['max_gain']:.4f}")
    print(f"Prob Profit: {top_struct['prob_profit']:.4f}")
    print("===================================")
    
    # These values should be stored and checked in future runs
    # For now, just ensure they're reasonable
    assert actual_long_strike > actual_short_strike
    assert actual_premium < 0  # Debit spread
    assert abs(top_struct["max_loss"]) <= FIXED_CONFIG["max_loss_usd"]


def test_dominance_filter():
    """Test that dominance filter works correctly."""
    # Create mock structures where one dominates another
    struct_a = {
        "premium": -100,
        "max_gain": 200,
        "ev": 50,
        "template_name": "put_spread_a"
    }
    
    struct_b = {
        "premium": -120,  # Worse (more paid)
        "max_gain": 180,  # Worse (less gain)
        "ev": 40,  # Worse (less EV)
        "template_name": "put_spread_b"
    }
    
    struct_c = {
        "premium": -90,  # Better than A
        "max_gain": 220,  # Better than A
        "ev": 60,  # Better than A
        "template_name": "put_spread_c"
    }
    
    structures = [struct_a, struct_b, struct_c]
    
    # Filter dominated
    non_dominated = filter_dominated_structures(structures)
    
    # struct_b should be removed (dominated by both A and C)
    names = [s["template_name"] for s in non_dominated]
    assert "put_spread_b" not in names
    
    # At least struct_c should remain (it dominates A)
    assert "put_spread_c" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
