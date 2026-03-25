"""
Regression test for ev_per_dollar calculation.

Ensures:
1. ev_per_dollar is nonzero for synthetic cases
2. ev_per_dollar = ev_per_contract / debit_per_contract
3. debit_per_contract == max_loss_per_contract for debit spreads
4. All values are positive
"""

import pytest
from forecast_arb.structuring.output_formatter import format_structure_output


def test_ev_per_dollar_nonzero_synthetic():
    """Test that ev_per_dollar is nonzero for a known synthetic case."""
    # Synthetic structure with known values
    structure = {
        "underlier": "SPY",
        "template_name": "put_debit_spread",
        "expiry": "20260228",
        "legs": [
            {"type": "put", "side": "long", "strike": 550, "bid": 10.0, "ask": 10.5},
            {"type": "put", "side": "short", "strike": 545, "bid": 8.0, "ask": 8.5}
        ],
        "debit": 2.25,  # Per share (long ask - short bid)
        "max_loss": 2.25,  # Same as debit for debit spread
        "max_gain": 2.75,  # Width - debit = (550-545) - 2.25
        "ev": 0.45,  # Positive EV per share
        "std": 1.5,
        "breakeven": 547.75,
        "premium": 2.25,
        "multiplier": 100
    }
    
    # Format output
    output = format_structure_output(structure)
    
    # Assertions
    assert output["debit_per_contract"] > 0, "Debit must be positive"
    assert output["max_loss_per_contract"] > 0, "Max loss must be positive"
    assert output["max_gain_per_contract"] > 0, "Max gain must be positive"
    
    # For debit spreads: debit == max_loss
    assert abs(output["debit_per_contract"] - output["max_loss_per_contract"]) < 0.01, \
        f"Debit {output['debit_per_contract']:.2f} must equal max_loss {output['max_loss_per_contract']:.2f}"
    
    # ev_per_dollar must be nonzero
    assert output["ev_per_dollar"] != 0, "ev_per_dollar must be nonzero for positive EV structure"
    assert output["ev_per_dollar"] > 0, "ev_per_dollar must be positive for positive EV structure"
    
    # Verify calculation: ev_per_dollar = ev_per_contract / debit_per_contract
    expected_ev_per_dollar = output["ev_per_contract"] / output["debit_per_contract"]
    assert abs(output["ev_per_dollar"] - expected_ev_per_dollar) < 0.001, \
        f"ev_per_dollar {output['ev_per_dollar']:.3f} != ev/debit {expected_ev_per_dollar:.3f}"
    
    # Check specific values
    assert output["debit_per_contract"] == 225.0, f"Expected 225, got {output['debit_per_contract']}"
    assert output["max_loss_per_contract"] == 225.0, f"Expected 225, got {output['max_loss_per_contract']}"
    assert output["ev_per_contract"] == 45.0, f"Expected 45, got {output['ev_per_contract']}"
    assert abs(output["ev_per_dollar"] - 0.2) < 0.001, f"Expected 0.2, got {output['ev_per_dollar']}"


def test_ev_per_dollar_negative_ev():
    """Test that ev_per_dollar can be negative for negative EV."""
    structure = {
        "underlier": "SPY",
        "template_name": "put_debit_spread",
        "expiry": "20260228",
        "legs": [
            {"type": "put", "side": "long", "strike": 550, "bid": 10.0, "ask": 10.5},
            {"type": "put", "side": "short", "strike": 545, "bid": 8.0, "ask": 8.5}
        ],
        "debit": 3.0,
        "max_loss": 3.0,
        "max_gain": 2.0,
        "ev": -0.5,  # Negative EV
        "std": 1.5,
        "breakeven": 547.0,
        "premium": 3.0,
        "multiplier": 100
    }
    
    output = format_structure_output(structure)
    
    # ev_per_dollar should be negative
    assert output["ev_per_dollar"] < 0, "ev_per_dollar should be negative for negative EV"
    assert output["ev_per_contract"] == -50.0, f"Expected -50, got {output['ev_per_contract']}"
    assert abs(output["ev_per_dollar"] - (-50.0 / 300.0)) < 0.001


def test_ev_per_dollar_zero_debit():
    """Test that ev_per_dollar handles zero debit gracefully."""
    structure = {
        "underlier": "SPY",
        "template_name": "put_debit_spread",
        "expiry": "20260228",
        "legs": [
            {"type": "put", "side": "long", "strike": 550, "bid": 10.0, "ask": 10.5},
            {"type": "put", "side": "short", "strike": 545, "bid": 8.0, "ask": 8.5}
        ],
        "debit": 0.0,  # Edge case
        "max_loss": 0.0,
        "max_gain": 5.0,
        "ev": 2.0,
        "std": 1.5,
        "breakeven": 550.0,
        "premium": 0.0,
        "multiplier": 100
    }
    
    output = format_structure_output(structure)
    
    # Should handle gracefully without division by zero - set to None for exclusion from ranking
    assert output["ev_per_dollar"] is None, "ev_per_dollar should be None for zero debit (invalid structure)"


def test_ev_per_dollar_exact_calculation():
    """
    Test exact ev_per_dollar calculation with specific values.
    
    Given: ev_per_contract=100, debit_per_contract=10
    Expected: ev_per_dollar=10.0
    """
    # Create structure with ev=1.0 per share (100 per contract with multiplier 100)
    # and debit=0.10 per share (10 per contract with multiplier 100)
    structure = {
        "underlier": "SPY",
        "template_name": "put_debit_spread",
        "expiry": "20260228",
        "legs": [
            {"type": "put", "side": "long", "strike": 500, "bid": 5.0, "ask": 5.5},
            {"type": "put", "side": "short", "strike": 495, "bid": 4.9, "ask": 5.4}
        ],
        "debit": 0.10,  # Per share -> 10 per contract
        "max_loss": 0.10,
        "max_gain": 4.90,
        "ev": 1.0,  # Per share -> 100 per contract
        "std": 2.0,
        "breakeven": 499.90,
        "premium": 0.10,
        "multiplier": 100
    }
    
    output = format_structure_output(structure)
    
    # Verify per-contract values
    assert output["ev_per_contract"] == 100.0, \
        f"Expected ev_per_contract=100.0, got {output['ev_per_contract']}"
    assert output["debit_per_contract"] == 10.0, \
        f"Expected debit_per_contract=10.0, got {output['debit_per_contract']}"
    
    # Verify calculation: ev_per_dollar = 100 / 10 = 10.0
    assert output["ev_per_dollar"] == 10.0, \
        f"Expected ev_per_dollar=10.0, got {output['ev_per_dollar']}"
    
    # Verify it matches printed/serialized value (3 decimals)
    formatted = f"{output['ev_per_dollar']:.3f}"
    assert formatted == "10.000", \
        f"Formatted value should be '10.000', got '{formatted}'"


def test_max_loss_always_positive():
    """Test that max_loss_per_contract is always positive even if input is negative."""
    structure = {
        "underlier": "SPY",
        "template_name": "put_debit_spread",
        "expiry": "20260228",
        "legs": [
            {"type": "put", "side": "long", "strike": 550, "bid": 10.0, "ask": 10.5},
            {"type": "put", "side": "short", "strike": 545, "bid": 8.0, "ask": 8.5}
        ],
        "debit": 2.25,
        "max_loss": -2.25,  # Negative (bug in some calculations)
        "max_gain": 2.75,
        "ev": 0.45,
        "std": 1.5,
        "breakeven": 547.75,
        "premium": 2.25,
        "multiplier": 100
    }
    
    output = format_structure_output(structure)
    
    # max_loss_per_contract should be positive
    assert output["max_loss_per_contract"] > 0, "max_loss_per_contract must be positive"
    assert output["max_loss_per_contract"] == 225.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
