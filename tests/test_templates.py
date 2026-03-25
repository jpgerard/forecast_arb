"""
Unit tests for option structure templates.
"""

import pytest
from forecast_arb.structuring.templates import (
    generate_put_spread,
    generate_call_spread,
    generate_strangle,
    compute_payoff
)


def test_generate_put_spread():
    """Test put spread generation."""
    underlier = "SPY"
    expiry = "2026-03-15"
    S0 = 500.0
    K_long = 490.0
    K_short = 480.0
    r = 0.05
    sigma = 0.15
    T = 30/365
    
    spread = generate_put_spread(
        underlier, expiry, S0, K_long, K_short, r, sigma, T
    )
    
    assert spread["template_name"] == "put_spread"
    assert spread["underlier"] == underlier
    assert spread["expiry"] == expiry
    assert len(spread["legs"]) == 2
    
    # Long put should be more expensive (higher strike)
    leg_long = spread["legs"][0]
    leg_short = spread["legs"][1]
    
    assert leg_long["strike"] == K_long
    assert leg_short["strike"] == K_short
    assert leg_long["side"] == "long"
    assert leg_short["side"] == "short"
    
    # Net premium should be negative (pay to enter)
    assert spread["premium"] < 0
    
    # Max loss = premium paid
    assert spread["max_loss"] == spread["premium"]
    
    # Max gain = spread width - premium
    expected_max_gain = (K_long - K_short) - abs(spread["premium"])
    assert abs(spread["max_gain"] - expected_max_gain) < 0.01


def test_generate_call_spread():
    """Test call spread generation."""
    underlier = "SPY"
    expiry = "2026-03-15"
    S0 = 500.0
    K_long = 510.0
    K_short = 520.0
    r = 0.05
    sigma = 0.15
    T = 30/365
    
    spread = generate_call_spread(
        underlier, expiry, S0, K_long, K_short, r, sigma, T
    )
    
    assert spread["template_name"] == "call_spread"
    assert len(spread["legs"]) == 2
    
    # Long call should be more expensive (lower strike)
    leg_long = spread["legs"][0]
    leg_short = spread["legs"][1]
    
    assert leg_long["strike"] == K_long
    assert leg_short["strike"] == K_short
    assert leg_long["side"] == "long"
    assert leg_short["side"] == "short"
    
    # Net premium should be negative (pay to enter)
    assert spread["premium"] < 0


def test_generate_strangle():
    """Test strangle generation."""
    underlier = "SPY"
    expiry = "2026-03-15"
    S0 = 500.0
    K_put = 480.0
    K_call = 520.0
    r = 0.05
    sigma = 0.15
    T = 30/365
    
    strangle = generate_strangle(
        underlier, expiry, S0, K_put, K_call, r, sigma, T
    )
    
    assert strangle["template_name"] == "strangle"
    assert len(strangle["legs"]) == 2
    
    leg_put = strangle["legs"][0]
    leg_call = strangle["legs"][1]
    
    assert leg_put["type"] == "put"
    assert leg_call["type"] == "call"
    assert leg_put["side"] == "long"
    assert leg_call["side"] == "long"
    
    # Premium should be negative (pay for both options)
    assert strangle["premium"] < 0
    
    # Max loss = premium paid
    assert strangle["max_loss"] == strangle["premium"]


def test_compute_payoff_put_spread():
    """Test payoff computation for put spread."""
    # Long 490 put, short 480 put
    structure = {
        "legs": [
            {"type": "put", "strike": 490, "side": "long", "price": 5.0, "quantity": 1},
            {"type": "put", "strike": 480, "side": "short", "price": 2.0, "quantity": 1}
        ],
        "premium": -3.0
    }
    
    # Below both strikes (max profit)
    S_T = 470
    payoff = compute_payoff(structure, S_T)
    expected = (490 - 470) - (480 - 470) - 3.0  # Long put - short put - premium
    assert abs(payoff - expected) < 0.01
    assert abs(payoff - 7.0) < 0.01  # 10 (spread) - 3 (premium)
    
    # Between strikes
    S_T = 485
    payoff = compute_payoff(structure, S_T)
    expected = (490 - 485) - 0 - 3.0  # Only long put ITM
    assert abs(payoff - expected) < 0.01
    assert abs(payoff - 2.0) < 0.01
    
    # Above both strikes (max loss = premium)
    S_T = 500
    payoff = compute_payoff(structure, S_T)
    assert abs(payoff - (-3.0)) < 0.01


def test_compute_payoff_call_spread():
    """Test payoff computation for call spread."""
    # Long 510 call, short 520 call
    structure = {
        "legs": [
            {"type": "call", "strike": 510, "side": "long", "price": 4.0, "quantity": 1},
            {"type": "call", "strike": 520, "side": "short", "price": 2.0, "quantity": 1}
        ],
        "premium": -2.0
    }
    
    # Above both strikes (max profit)
    S_T = 530
    payoff = compute_payoff(structure, S_T)
    expected = (530 - 510) - (530 - 520) - 2.0  # Long call - short call - premium
    assert abs(payoff - expected) < 0.01
    assert abs(payoff - 8.0) < 0.01
    
    # Between strikes
    S_T = 515
    payoff = compute_payoff(structure, S_T)
    expected = (515 - 510) - 0 - 2.0  # Only long call ITM
    assert abs(payoff - expected) < 0.01
    assert abs(payoff - 3.0) < 0.01
    
    # Below both strikes (max loss = premium)
    S_T = 500
    payoff = compute_payoff(structure, S_T)
    assert abs(payoff - (-2.0)) < 0.01


def test_compute_payoff_strangle():
    """Test payoff computation for strangle."""
    # Long 480 put, long 520 call
    structure = {
        "legs": [
            {"type": "put", "strike": 480, "side": "long", "price": 3.0, "quantity": 1},
            {"type": "call", "strike": 520, "side": "long", "price": 3.0, "quantity": 1}
        ],
        "premium": -6.0
    }
    
    # Far below (put ITM)
    S_T = 450
    payoff = compute_payoff(structure, S_T)
    expected = (480 - 450) - 6.0
    assert abs(payoff - expected) < 0.01
    assert abs(payoff - 24.0) < 0.01
    
    # Far above (call ITM)
    S_T = 550
    payoff = compute_payoff(structure, S_T)
    expected = (550 - 520) - 6.0
    assert abs(payoff - expected) < 0.01
    assert abs(payoff - 24.0) < 0.01
    
    # Between strikes (both OTM, max loss)
    S_T = 500
    payoff = compute_payoff(structure, S_T)
    assert abs(payoff - (-6.0)) < 0.01


def test_structure_validation():
    """Test that generated structures have required fields."""
    spread = generate_put_spread(
        underlier="SPY",
        expiry="2026-03-15",
        S0=500.0,
        K_long=490.0,
        K_short=480.0,
        r=0.05,
        sigma=0.15,
        T=30/365
    )
    
    # Required fields
    assert "template_name" in spread
    assert "underlier" in spread
    assert "expiry" in spread
    assert "legs" in spread
    assert "premium" in spread
    assert "max_loss" in spread
    assert "max_gain" in spread
    
    # Each leg should have required fields
    for leg in spread["legs"]:
        assert "type" in leg
        assert "strike" in leg
        assert "side" in leg
        assert "price" in leg
        assert "quantity" in leg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
