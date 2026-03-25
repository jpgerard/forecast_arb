"""
Unit tests for calibrator module.
"""

import pytest
import numpy as np
from forecast_arb.structuring.calibrator import (
    calibrate_drift,
    lognormal_cdf,
    implied_drift_from_price_target
)


def test_calibrate_drift_basic():
    """Test basic drift calibration."""
    # Given 50% probability of crossing barrier
    p_event = 0.5
    S0 = 100.0
    K_barrier = 105.0
    T = 30/365
    sigma = 0.15
    
    mu, p_achieved = calibrate_drift(
        p_event=p_event,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=5000,
        seed=42
    )
    
    # Check that achieved probability is close to target
    assert abs(p_achieved - p_event) < 0.05, f"p_achieved={p_achieved:.3f} far from target {p_event}"
    
    # Drift should be positive for upward move
    assert mu > 0, "Expected positive drift for upward move"


def test_calibrate_drift_high_probability():
    """Test calibration with high event probability."""
    p_event = 0.8
    S0 = 100.0
    K_barrier = 102.0
    T = 30/365
    sigma = 0.15
    
    mu, p_achieved = calibrate_drift(
        p_event=p_event,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=5000,
        seed=42
    )
    
    assert p_achieved > 0.75, f"High probability event should achieve {p_event:.0%}"
    assert mu > 0, "Expected positive drift"


def test_calibrate_drift_low_probability():
    """Test calibration with low event probability."""
    p_event = 0.2
    S0 = 100.0
    K_barrier = 110.0
    T = 30/365
    sigma = 0.15
    
    mu, p_achieved = calibrate_drift(
        p_event=p_event,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=5000,
        seed=42
    )
    
    assert p_achieved < 0.3, f"Low probability event should achieve ~{p_event:.0%}"


def test_calibrate_drift_deterministic():
    """Test that calibration is deterministic with same seed."""
    params = {
        "p_event": 0.6,
        "S0": 100.0,
        "K_barrier": 105.0,
        "T": 30/365,
        "sigma": 0.15,
        "n_samples": 5000,
        "seed": 42
    }
    
    mu1, p1 = calibrate_drift(**params)
    mu2, p2 = calibrate_drift(**params)
    
    assert mu1 == mu2, "Drift should be deterministic"
    assert p1 == p2, "Probability should be deterministic"


def test_lognormal_cdf():
    """Test lognormal CDF calculation."""
    # At mean, CDF should be ~0.5
    S0 = 100
    K = 100
    mu = 0
    sigma = 0.15
    T = 0.25
    
    prob = lognormal_cdf(K, S0, mu, sigma, T)
    
    # Should be around 0.5 (slightly less due to lognormal skew)
    assert 0.4 < prob < 0.6, f"CDF at mean should be ~0.5, got {prob:.3f}"


def test_lognormal_cdf_far_otm():
    """Test CDF for far out-of-the-money scenario."""
    S0 = 100
    K = 150  # 50% above current
    mu = 0
    sigma = 0.15
    T = 30/365
    
    prob = lognormal_cdf(K, S0, mu, sigma, T)
    
    # Probability of reaching 150 should be low
    assert prob < 0.1, f"Probability of large move should be low, got {prob:.3f}"


def test_implied_drift_from_price_target():
    """Test implied drift calculation from price target."""
    S0 = 100
    S_target = 105
    T = 30/365
    sigma = 0.15
    
    mu = implied_drift_from_price_target(S0, S_target, T, sigma)
    
    # Positive drift expected for upward target
    assert mu > 0, "Expected positive drift for upward target"
    
    # Verify by forward simulation
    expected_ST = S0 * np.exp(mu * T)
    assert abs(expected_ST - S_target) < 1.0, "Forward price should match target"


def test_calibrate_drift_boundary_conditions():
    """Test calibration at boundary conditions."""
    S0 = 100.0
    K_barrier = 100.0  # At current price
    T = 30/365
    sigma = 0.15
    
    # 50% chance of being above current price
    mu, p = calibrate_drift(
        p_event=0.5,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=5000,
        seed=42
    )
    
    # Drift should be approximately zero for 50% at current price
    assert abs(mu) < 0.5, f"Drift should be near zero for 50% at current, got {mu:.3f}"


def test_calibrate_drift_validation():
    """Test input validation."""
    with pytest.raises(AssertionError):
        # Invalid probability
        calibrate_drift(
            p_event=1.5,  # > 1
            S0=100,
            K_barrier=105,
            T=30/365,
            sigma=0.15
        )
    
    with pytest.raises(AssertionError):
        # Negative probability
        calibrate_drift(
            p_event=-0.1,
            S0=100,
            K_barrier=105,
            T=30/365,
            sigma=0.15
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
