"""
Regression test for Reason text bug.

Ensures reason_selected uses EV per contract, not per share.
"""

import pytest
from forecast_arb.structuring.output_formatter import get_reason_selected


def test_reason_uses_ev_per_contract():
    """Test that reason_selected uses EV per contract (not per share)."""
    
    # Structure with ev=2.05 per share
    # Should display as $205.00 per contract (2.05 * 100)
    struct = {
        "ev": 2.05,  # per share
        "ev_per_dollar": 2.40,
        "prob_profit": 0.27,
        "multiplier": 100
    }
    
    # Rank 1 with max_ev_per_dollar objective
    reason = get_reason_selected(struct, rank=1, objective="max_ev_per_dollar")
    
    # Should contain $205.00, not $2.05
    assert "$205.00" in reason, f"Reason should contain '$205.00' (per contract), not '$2.05'. Got: {reason}"
    assert "$2.05" not in reason, f"Reason should NOT contain '$2.05' (per share). Got: {reason}"
    
    # Should contain "per contract"
    assert "per contract" in reason.lower(), f"Reason should specify 'per contract'. Got: {reason}"


def test_reason_rank1_max_ev():
    """Test reason text for rank 1 with max_ev objective."""
    
    struct = {
        "ev": 1.50,  # per share -> $150 per contract
        "ev_per_dollar": 1.80,
        "prob_profit": 0.25,
        "multiplier": 100
    }
    
    reason = get_reason_selected(struct, rank=1, objective="max_ev")
    
    # Should show $150.00 per contract
    assert "$150.00" in reason
    assert "per contract" in reason.lower()
    assert "25.0%" in reason or "25%" in reason  # prob_profit formatting


def test_reason_rank2():
    """Test reason text for rank 2."""
    
    struct = {
        "ev": 1.20,  # per share -> $120 per contract
        "ev_per_dollar": 1.50,
        "prob_profit": 0.20,
        "multiplier": 100
    }
    
    reason = get_reason_selected(struct, rank=2, objective="max_ev_per_dollar")
    
    # Should show $120.00 per contract
    assert "$120.00" in reason
    assert "per contract" in reason.lower()
    assert "20.0%" in reason or "20%" in reason


def test_reason_rank3():
    """Test reason text for rank 3."""
    
    struct = {
        "ev": 0.95,  # per share -> $95 per contract
        "ev_per_dollar": 1.20,
        "prob_profit": 0.18,
        "multiplier": 100
    }
    
    reason = get_reason_selected(struct, rank=3, objective="max_ev_per_dollar")
    
    # Should show $95.00 per contract
    assert "$95.00" in reason
    assert "per contract" in reason.lower()


def test_reason_with_small_ev():
    """Test reason text with very small EV per share."""
    
    struct = {
        "ev": 0.02,  # per share -> $2.00 per contract
        "ev_per_dollar": 0.50,
        "prob_profit": 0.10,
        "multiplier": 100
    }
    
    reason = get_reason_selected(struct, rank=1, objective="max_ev_per_dollar")
    
    # Should show $2.00 per contract, NOT $0.02
    assert "$2.00" in reason
    assert "$0.02" not in reason
    assert "per contract" in reason.lower()


def test_reason_with_large_ev():
    """Test reason text with large EV per share."""
    
    struct = {
        "ev": 10.50,  # per share -> $1050.00 per contract
        "ev_per_dollar": 3.50,
        "prob_profit": 0.35,
        "multiplier": 100
    }
    
    reason = get_reason_selected(struct, rank=1, objective="max_ev_per_dollar")
    
    # Should show $1050.00 per contract
    assert "$1050.00" in reason or "$1,050.00" in reason  # Either format is OK
    assert "$10.50" not in reason
    assert "per contract" in reason.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
