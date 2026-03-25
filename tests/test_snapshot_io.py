"""
Unit tests for snapshot I/O module.
"""

import pytest
import json
import tempfile
from forecast_arb.structuring.snapshot_io import (
    load_snapshot,
    validate_snapshot,
    get_snapshot_metadata,
    get_expiries,
    get_strikes_for_expiry,
    get_calls_for_expiry,
    get_puts_for_expiry,
    get_option_by_strike,
    compute_time_to_expiry,
    extract_structure_inputs_from_snapshot,
    find_nearest_expiry,
    find_atm_strike,
    snapshot_summary
)


# Mock snapshot data
MOCK_SNAPSHOT = {
    "snapshot_metadata": {
        "underlier": "SPY",
        "snapshot_time": "2026-01-27T22:00:00Z",
        "current_price": 500.0,
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "dte_min": 20,
        "dte_max": 40
    },
    "expiries": {
        "20260228": {
            "expiry_date": "20260228",
            "calls": [
                {
                    "strike": 490.0,
                    "bid": 15.0,
                    "ask": 15.5,
                    "last": 15.25,
                    "volume": 100,
                    "open_interest": 500,
                    "implied_vol": 0.15
                },
                {
                    "strike": 500.0,
                    "bid": 10.0,
                    "ask": 10.5,
                    "last": 10.25,
                    "volume": 200,
                    "open_interest": 1000,
                    "implied_vol": 0.14
                },
                {
                    "strike": 510.0,
                    "bid": 5.5,
                    "ask": 6.0,
                    "last": 5.75,
                    "volume": 150,
                    "open_interest": 750,
                    "implied_vol": 0.16
                }
            ],
            "puts": [
                {
                    "strike": 490.0,
                    "bid": 4.0,
                    "ask": 4.5,
                    "last": 4.25,
                    "volume": 120,
                    "open_interest": 600,
                    "implied_vol": 0.15
                },
                {
                    "strike": 500.0,
                    "bid": 7.0,
                    "ask": 7.5,
                    "last": 7.25,
                    "volume": 180,
                    "open_interest": 900,
                    "implied_vol": 0.14
                },
                {
                    "strike": 510.0,
                    "bid": 11.5,
                    "ask": 12.0,
                    "last": 11.75,
                    "volume": 90,
                    "open_interest": 450,
                    "implied_vol": 0.16
                }
            ]
        }
    }
}


def test_validate_snapshot():
    """Test snapshot validation."""
    # Valid snapshot
    assert validate_snapshot(MOCK_SNAPSHOT) is True
    
    # Missing metadata
    with pytest.raises(ValueError, match="Missing snapshot_metadata"):
        validate_snapshot({"expiries": {}})
    
    # Missing expiries
    with pytest.raises(ValueError, match="Missing expiries"):
        validate_snapshot({"snapshot_metadata": {}})
    
    # Invalid expiries structure
    invalid = MOCK_SNAPSHOT.copy()
    invalid["expiries"] = {"20260228": {"calls": []}}  # Missing puts
    with pytest.raises(ValueError, match="missing calls or puts"):
        validate_snapshot(invalid)


def test_get_snapshot_metadata():
    """Test metadata extraction."""
    meta = get_snapshot_metadata(MOCK_SNAPSHOT)
    
    assert meta["underlier"] == "SPY"
    assert meta["current_price"] == 500.0
    assert meta["risk_free_rate"] == 0.05
    assert meta["dte_min"] == 20
    assert meta["dte_max"] == 40


def test_get_expiries():
    """Test expiry extraction."""
    expiries = get_expiries(MOCK_SNAPSHOT)
    
    assert len(expiries) == 1
    assert expiries[0] == "20260228"


def test_get_strikes_for_expiry():
    """Test strike extraction."""
    strikes = get_strikes_for_expiry(MOCK_SNAPSHOT, "20260228")
    
    assert len(strikes) == 3
    assert strikes == [490.0, 500.0, 510.0]
    
    # Non-existent expiry
    strikes_empty = get_strikes_for_expiry(MOCK_SNAPSHOT, "20261231")
    assert strikes_empty == []


def test_get_calls_for_expiry():
    """Test call extraction."""
    calls = get_calls_for_expiry(MOCK_SNAPSHOT, "20260228")
    
    assert len(calls) == 3
    assert calls[0]["strike"] == 490.0
    assert calls[1]["strike"] == 500.0
    assert calls[2]["strike"] == 510.0


def test_get_puts_for_expiry():
    """Test put extraction."""
    puts = get_puts_for_expiry(MOCK_SNAPSHOT, "20260228")
    
    assert len(puts) == 3
    assert puts[0]["strike"] == 490.0
    assert puts[1]["strike"] == 500.0
    assert puts[2]["strike"] == 510.0


def test_get_option_by_strike():
    """Test finding option by strike."""
    calls = get_calls_for_expiry(MOCK_SNAPSHOT, "20260228")
    
    # Exact match
    opt = get_option_by_strike(calls, 500.0)
    assert opt is not None
    assert opt["strike"] == 500.0
    assert opt["bid"] == 10.0
    
    # No match
    opt_none = get_option_by_strike(calls, 550.0)
    assert opt_none is None
    
    # Within tolerance
    opt_close = get_option_by_strike(calls, 500.005, tolerance=0.01)
    assert opt_close is not None
    assert opt_close["strike"] == 500.0


def test_compute_time_to_expiry():
    """Test time to expiry calculation."""
    T = compute_time_to_expiry("2026-01-27T22:00:00Z", "20260228")
    
    # 32 days from Jan 27 to Feb 28
    expected = 32 / 365.0
    assert abs(T - expected) < 0.01


def test_extract_structure_inputs_from_snapshot():
    """Test structure input extraction."""
    inputs = extract_structure_inputs_from_snapshot(
        MOCK_SNAPSHOT,
        expiry="20260228",
        strikes=[490.0, 500.0],
        option_types=["put", "put"]
    )
    
    assert inputs["underlier"] == "SPY"
    assert inputs["S0"] == 500.0
    assert inputs["r"] == 0.05
    assert inputs["T"] > 0
    assert len(inputs["legs"]) == 2
    
    # Check first leg (490 put)
    leg1 = inputs["legs"][0]
    assert leg1["type"] == "put"
    assert leg1["strike"] == 490.0
    assert leg1["price"] == (4.0 + 4.5) / 2.0  # Midpoint


def test_find_nearest_expiry():
    """Test finding nearest expiry."""
    # Add another expiry for testing (use deepcopy to avoid mutating MOCK_SNAPSHOT)
    import copy
    snapshot = copy.deepcopy(MOCK_SNAPSHOT)
    snapshot["expiries"]["20260315"] = snapshot["expiries"]["20260228"].copy()
    
    # Target 32 DTE should match 20260228
    nearest = find_nearest_expiry(snapshot, 32)
    assert nearest == "20260228"


def test_find_atm_strike():
    """Test finding ATM strike."""
    atm = find_atm_strike(MOCK_SNAPSHOT, "20260228")
    
    # Spot is 500.0, so ATM should be 500.0
    assert atm == 500.0


def test_snapshot_summary():
    """Test summary generation."""
    summary = snapshot_summary(MOCK_SNAPSHOT)
    
    assert "SPY" in summary
    assert "500.00" in summary
    assert "20260228" in summary
    assert "DTE=" in summary


def test_load_snapshot_from_file():
    """Test loading snapshot from file."""
    # Create temp file
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        json.dump(MOCK_SNAPSHOT, f)
        temp_path = f.name
    
    try:
        # Load snapshot
        loaded = load_snapshot(temp_path)
        
        assert loaded["snapshot_metadata"]["underlier"] == "SPY"
        assert validate_snapshot(loaded)
    
    finally:
        # Cleanup
        import os
        os.unlink(temp_path)


def test_extract_structure_mixed_types():
    """Test extracting mixed call/put structure."""
    inputs = extract_structure_inputs_from_snapshot(
        MOCK_SNAPSHOT,
        expiry="20260228",
        strikes=[490.0, 510.0],
        option_types=["put", "call"]  # Strangle
    )
    
    assert len(inputs["legs"]) == 2
    assert inputs["legs"][0]["type"] == "put"
    assert inputs["legs"][0]["strike"] == 490.0
    assert inputs["legs"][1]["type"] == "call"
    assert inputs["legs"][1]["strike"] == 510.0


def test_extract_structure_missing_option():
    """Test handling missing option."""
    inputs = extract_structure_inputs_from_snapshot(
        MOCK_SNAPSHOT,
        expiry="20260228",
        strikes=[490.0, 550.0],  # 550 doesn't exist
        option_types=["put", "put"]
    )
    
    # Should only return leg for 490
    assert len(inputs["legs"]) == 1
    assert inputs["legs"][0]["strike"] == 490.0


def test_tail_strike_coverage_with_empty_calls():
    """
    Test that all puts are included in strikes even when calls list is empty.
    This is critical for crash venture which uses deep OTM puts.
    """
    # Snapshot with tail puts only (no calls)
    tail_snapshot = {
        "snapshot_metadata": {
            "underlier": "SPY",
            "snapshot_time": "2026-01-28T00:00:00Z",
            "current_price": 600.0,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "tail_metadata": {
                "tail_floor_strike": 490.0,
                "tail_moneyness_floor": 0.18,
                "incomplete": False,
                "actual_floor": 400.0
            }
        },
        "expiries": {
            "20260228": {
                "expiry_date": "20260228",
                "calls": [],  # Empty calls list
                "puts": [
                    {"strike": 400.0, "bid": 0.05, "ask": 0.10, "last": 0.08, "volume": 10, "open_interest": 50, "implied_vol": 0.45},
                    {"strike": 410.0, "bid": 0.08, "ask": 0.12, "last": 0.10, "volume": 15, "open_interest": 75, "implied_vol": 0.43},
                    {"strike": 420.0, "bid": 0.10, "ask": 0.15, "last": 0.12, "volume": 20, "open_interest": 100, "implied_vol": 0.41},
                    {"strike": 430.0, "bid": 0.15, "ask": 0.20, "last": 0.17, "volume": 25, "open_interest": 125, "implied_vol": 0.39},
                    {"strike": 440.0, "bid": 0.20, "ask": 0.25, "last": 0.22, "volume": 30, "open_interest": 150, "implied_vol": 0.37},
                    {"strike": 450.0, "bid": 0.30, "ask": 0.35, "last": 0.32, "volume": 40, "open_interest": 200, "implied_vol": 0.35},
                    {"strike": 490.0, "bid": 2.0, "ask": 2.2, "last": 2.1, "volume": 100, "open_interest": 500, "implied_vol": 0.25},
                    {"strike": 500.0, "bid": 3.5, "ask": 3.8, "last": 3.6, "volume": 150, "open_interest": 750, "implied_vol": 0.22},
                    {"strike": 510.0, "bid": 5.5, "ask": 5.8, "last": 5.6, "volume": 120, "open_interest": 600, "implied_vol": 0.20},
                    {"strike": 520.0, "bid": 8.0, "ask": 8.5, "last": 8.2, "volume": 90, "open_interest": 450, "implied_vol": 0.19},
                    {"strike": 530.0, "bid": 11.0, "ask": 11.5, "last": 11.2, "volume": 70, "open_interest": 350, "implied_vol": 0.18},
                ]
            }
        }
    }
    
    # Validate snapshot (should NOT fail due to empty calls)
    assert validate_snapshot(tail_snapshot) is True
    
    # Get all strikes for the expiry
    strikes = get_strikes_for_expiry(tail_snapshot, "20260228")
    
    # CRITICAL: All puts must be included, including deep OTM (400, 450, etc.)
    assert 400.0 in strikes, "Deep OTM put strike 400 must be included"
    assert 450.0 in strikes, "Tail put strike 450 must be included"
    assert 490.0 in strikes, "Tail floor strike 490 must be included"
    assert 530.0 in strikes, "Above spot strike 530 must be included"
    
    # Verify we got ALL 11 strikes from puts
    assert len(strikes) == 11, f"Expected 11 strikes, got {len(strikes)}"
    
    # Verify strikes are sorted
    assert strikes == sorted(strikes)
    
    # Verify puts are accessible
    puts = get_puts_for_expiry(tail_snapshot, "20260228")
    assert len(puts) == 11
    
    # Verify calls are empty but don't cause errors
    calls = get_calls_for_expiry(tail_snapshot, "20260228")
    assert len(calls) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
