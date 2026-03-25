"""
Regression test for ranking order bug.

Ensures structures are ranked by ev_per_dollar when objective="max_ev_per_dollar".
"""

import pytest
from forecast_arb.structuring.router import choose_best_structure


def test_ranking_by_ev_per_dollar():
    """Test that structures are correctly ranked by ev_per_dollar."""
    
    # Create two mock structures with different ev_per_dollar values
    # Structure A: Higher EV/$ (should rank #1)
    struct_a = {
        "expiry": "20260227",
        "debit": 0.50,  # per share
        "debit_per_contract": 50.0,
        "max_loss": 0.50,
        "max_loss_per_contract": 50.0,
        "max_gain": 4.50,
        "max_gain_per_contract": 450.0,
        "ev": 1.20,  # per share
        "std": 2.0,
        "prob_profit": 0.25,
        "ev_per_dollar": 2.40,  # Higher EV/$
        "breakeven": 495.0
    }
    
    # Structure B: Lower EV/$ (should rank #2)
    struct_b = {
        "expiry": "20260227",
        "debit": 1.00,  # per share
        "debit_per_contract": 100.0,
        "max_loss": 1.00,
        "max_loss_per_contract": 100.0,
        "max_gain": 9.00,
        "max_gain_per_contract": 900.0,
        "ev": 2.00,  # per share (higher absolute EV)
        "std": 3.0,
        "prob_profit": 0.30,
        "ev_per_dollar": 2.00,  # Lower EV/$
        "breakeven": 490.0
    }
    
    # Test both orderings to ensure deterministic ranking
    for structures in [[struct_a, struct_b], [struct_b, struct_a]]:
        constraints = {
            "max_loss_usd_per_trade": 500.0,
            "min_prob_profit": 0.0,
            "min_ev": 0.0
        }
        
        # Sort by ev_per_dollar
        sorted_structures = choose_best_structure(
            structures,
            constraints=constraints,
            objective="max_ev_per_dollar"
        )
        
        # Assert correct order: A should be first (higher EV/$)
        assert len(sorted_structures) == 2
        assert sorted_structures[0]["ev_per_dollar"] == 2.40
        assert sorted_structures[1]["ev_per_dollar"] == 2.00
        
        # Verify it's struct_a first
        assert sorted_structures[0]["ev"] == 1.20
        assert sorted_structures[1]["ev"] == 2.00


def test_ev_per_dollar_is_float():
    """Test that ev_per_dollar is a float, not a string."""
    
    struct = {
        "expiry": "20260227",
        "debit": 0.50,
        "debit_per_contract": 50.0,
        "max_loss": 0.50,
        "max_loss_per_contract": 50.0,
        "max_gain": 4.50,
        "max_gain_per_contract": 450.0,
        "ev": 1.20,
        "std": 2.0,
        "prob_profit": 0.25,
        "ev_per_dollar": 2.40,  # float
        "breakeven": 495.0
    }
    
    constraints = {
        "max_loss_usd_per_trade": 500.0,
        "min_prob_profit": 0.0,
        "min_ev": 0.0
    }
    
    sorted_structures = choose_best_structure(
        [struct],
        constraints=constraints,
        objective="max_ev_per_dollar"
    )
    
    assert len(sorted_structures) == 1
    ev_per_dollar = sorted_structures[0]["ev_per_dollar"]
    assert isinstance(ev_per_dollar, (int, float)), f"ev_per_dollar must be numeric, got {type(ev_per_dollar)}"


def test_ev_per_dollar_string_raises_error():
    """Test that ev_per_dollar as string raises TypeError."""
    
    struct = {
        "expiry": "20260227",
        "debit": 0.50,
        "debit_per_contract": 50.0,
        "max_loss": 0.50,
        "max_loss_per_contract": 50.0,
        "max_gain": 4.50,
        "max_gain_per_contract": 450.0,
        "ev": 1.20,
        "std": 2.0,
        "prob_profit": 0.25,
        "ev_per_dollar": "2.40",  # STRING - should fail
        "breakeven": 495.0
    }
    
    constraints = {
        "max_loss_usd_per_trade": 500.0,
        "min_prob_profit": 0.0,
        "min_ev": 0.0
    }
    
    # Should raise TypeError
    with pytest.raises(TypeError, match="ev_per_dollar must be float"):
        choose_best_structure(
            [struct],
            constraints=constraints,
            objective="max_ev_per_dollar"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
