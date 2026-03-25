"""
Test tail strike filtering for IBKR snapshot exporter.
"""

import pytest
from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter


def test_tail_strike_filtering_with_moneyness_floor():
    """Test tail strike filtering using moneyness floor."""
    exporter = IBKRSnapshotExporter()
    
    # Mock available strikes from IBKR
    all_strikes = [float(i) for i in range(400, 620, 5)]  # 400, 405, 410, ..., 615
    spot = 600.0
    tail_moneyness_floor = 0.18
    
    # Filter with tail mode
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=tail_moneyness_floor
    )
    
    # Expected tail floor: 600 * (1 - 0.18) = 492, rounded down to 490
    assert metadata["tail_floor_strike"] == 490.0
    assert metadata["tail_moneyness_floor"] == 0.18
    assert metadata["tail_floor_source"] == "computed_from_moneyness"
    
    # Should include strikes from 490 up to spot (600)
    assert min(strikes) == 490.0
    assert max(strikes) >= spot  # Should include some above spot too
    
    # Should not be incomplete (we have strikes down to the requested floor)
    assert metadata["incomplete"] is False
    assert metadata["actual_floor"] == 490.0
    
    # Verify all strikes in tail band are included
    tail_band = [s for s in all_strikes if 490.0 <= s < spot]
    for s in tail_band:
        assert s in strikes


def test_tail_strike_filtering_with_explicit_min_strike():
    """Test tail strike filtering using explicit minimum strike."""
    exporter = IBKRSnapshotExporter()
    
    all_strikes = [float(i) for i in range(400, 620, 5)]
    spot = 600.0
    min_strike = 480.0
    
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        min_strike=min_strike
    )
    
    # Should use explicit min_strike
    assert metadata["tail_floor_strike"] == 480.0
    assert metadata["tail_floor_source"] == "explicit_min_strike"
    
    assert min(strikes) == 480.0
    assert metadata["incomplete"] is False
    assert metadata["actual_floor"] == 480.0


def test_tail_strike_incomplete_coverage():
    """Test when requested tail floor is not available."""
    exporter = IBKRSnapshotExporter()
    
    # Limited strikes - only from 550 up
    all_strikes = [float(i) for i in range(550, 620, 5)]
    spot = 600.0
    tail_moneyness_floor = 0.18  # Would request 490
    
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=tail_moneyness_floor
    )
    
    # Requested 490, but lowest available is 550
    assert metadata["tail_floor_strike"] == 490.0
    assert metadata["incomplete"] is True
    assert metadata["requested_floor"] == 490.0
    assert metadata["actual_floor"] == 550.0
    
    assert min(strikes) == 550.0


def test_legacy_mode_filtering():
    """Test legacy mode strike filtering (backward compatibility)."""
    exporter = IBKRSnapshotExporter()
    
    all_strikes = [float(i) for i in range(400, 650, 5)]  # Extended to 650
    spot = 600.0
    strikes_below = 10
    strikes_above = 5
    
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        strikes_below=strikes_below,
        strikes_above=strikes_above
    )
    
    # Should have 10 strikes below + 5 above = 15 total
    assert len(strikes) == 15
    
    # No tail metadata in legacy mode
    assert metadata == {}
    
    # Verify strikes are around spot
    below = [s for s in strikes if s < spot]
    above = [s for s in strikes if s >= spot]
    assert len(below) == 10
    assert len(above) == 5


def test_rounding_logic_below_100():
    """Test that strikes below 100 round to nearest $5."""
    exporter = IBKRSnapshotExporter()
    
    all_strikes = [float(i) for i in range(60, 110, 5)]
    spot = 95.0
    tail_moneyness_floor = 0.18  # 95 * (1 - 0.18) = 77.9
    
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=tail_moneyness_floor
    )
    
    # Should round down to nearest $5: 77.9 -> 75
    assert metadata["tail_floor_strike"] == 75.0
    assert metadata["tail_floor_raw"] == 77.9


def test_rounding_logic_above_100():
    """Test that strikes above 100 round to nearest $10."""
    exporter = IBKRSnapshotExporter()
    
    all_strikes = [float(i) for i in range(400, 620, 10)]
    spot = 600.0
    tail_moneyness_floor = 0.18  # 600 * (1 - 0.18) = 492
    
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=tail_moneyness_floor
    )
    
    # Should round down to nearest $10: 492 -> 490
    assert metadata["tail_floor_strike"] == 490.0
    assert abs(metadata["tail_floor_raw"] - 492.0) < 0.01  # Floating point tolerance


def test_above_spot_band_included():
    """Test that strikes above spot are included for completeness."""
    exporter = IBKRSnapshotExporter()
    
    all_strikes = [float(i) for i in range(400, 650, 5)]
    spot = 600.0
    tail_moneyness_floor = 0.18
    
    strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=tail_moneyness_floor
    )
    
    # Should include some strikes above spot (default: 5)
    above_spot = [s for s in strikes if s >= spot]
    assert len(above_spot) >= 5
    
    # For SPY at 600, typical crash venture usage
    # Should have tail band (490-600) + small above band
    assert 490.0 in strikes  # tail floor
    assert 595.0 in strikes  # just below spot
    assert 600.0 in strikes  # at spot
    assert 605.0 in strikes  # just above spot


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
