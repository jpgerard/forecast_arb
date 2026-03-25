"""
Test snapshot strike selection logic for deep OTM coverage.

Verifies that the strike filtering logic includes sufficient depth for
crash venture structures targeting -8% to -15% moneyness (and deeper).
"""

import pytest
from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter


def test_strike_filter_tail_mode_deep_coverage():
    """
    Test tail mode strike filtering provides deep OTM coverage.
    
    For crash venture targeting -8% to -15% moneyness, we need strikes
    extending down to at least spot * 0.85 (or lower).
    """
    # Create mock exporter (no connection needed for testing filter logic)
    exporter = IBKRSnapshotExporter()
    
    # Mock available strikes: comprehensive range from 550 to 750 in $5 increments
    spot = 690.0
    all_strikes = [float(s) for s in range(550, 751, 5)]
    
    # Test with tail_moneyness_floor=0.20 (20% depth, covers -15% targets)
    filtered_strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=0.20
    )
    
    # Verify deep coverage
    min_strike = min(filtered_strikes)
    max_strike = max(filtered_strikes)
    
    # For spot=690 with 20% floor:
    # Expected floor: 690 * 0.80 = 552 (rounded down to nearest $10 = 550)
    expected_floor_raw = spot * (1 - 0.20)  # 552
    expected_floor_rounded = (expected_floor_raw // 10) * 10  # 550
    
    assert min_strike <= expected_floor_rounded, \
        f"Min strike {min_strike} should be <= {expected_floor_rounded} for 20% tail"
    
    # Should include strikes up to spot
    assert max_strike >= spot, f"Max strike {max_strike} should be >= spot {spot}"
    
    # Check metadata
    assert metadata["tail_floor_source"] == "computed_from_moneyness"
    assert metadata["tail_moneyness_floor"] == 0.20
    assert metadata["incomplete"] is False  # All requested strikes available
    
    # Verify specific coverage for crash venture targets
    # -8% moneyness: 690 * 0.92 = 634.8
    # -15% moneyness: 690 * 0.85 = 586.5
    assert any(630 <= s <= 640 for s in filtered_strikes), "Should cover -8% target"
    assert any(580 <= s <= 590 for s in filtered_strikes), "Should cover -15% target"


def test_strike_filter_tail_mode_incomplete_coverage():
    """
    Test tail mode when requested strikes are not available (incomplete coverage).
    """
    exporter = IBKRSnapshotExporter()
    
    spot = 690.0
    # Limited strikes: only down to 650 (not deep enough for 20% tail)
    all_strikes = [float(s) for s in range(650, 751, 5)]
    
    # Request 20% tail (would need strikes down to ~552)
    filtered_strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=0.20
    )
    
    # Should still return what's available
    assert len(filtered_strikes) > 0
    min_strike = min(filtered_strikes)
    
    # But should flag as incomplete
    assert metadata["incomplete"] is True
    assert metadata["actual_floor"] == 650.0  # Actual min available
    assert metadata["requested_floor"] < 650.0  # Requested lower
    
    # Verify we got partial coverage
    assert 650.0 in filtered_strikes


def test_strike_filter_explicit_min_strike():
    """
    Test explicit min_strike parameter (alternative to moneyness_floor).
    """
    exporter = IBKRSnapshotExporter()
    
    spot = 690.0
    all_strikes = [float(s) for s in range(550, 751, 5)]
    
    # Use explicit min_strike = 600
    filtered_strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        min_strike=600.0
    )
    
    min_strike = min(filtered_strikes)
    assert min_strike <= 600.0
    assert 600.0 in filtered_strikes
    
    assert metadata["tail_floor_source"] == "explicit_min_strike"
    assert metadata["tail_floor_strike"] == 600.0


def test_strike_filter_legacy_mode_improved_defaults():
    """
    Test legacy mode with improved defaults for deeper coverage.
    """
    exporter = IBKRSnapshotExporter()
    
    spot = 690.0
    all_strikes = [float(s) for s in range(550, 800, 5)]
    
    # Legacy mode with None (should use improved defaults)
    filtered_strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        strikes_below=None,  # Will default to 60
        strikes_above=None   # Will default to 10
    )
    
    # Should have gotten 60 strikes below + 10 above = ~70 total
    strikes_below_spot = [s for s in filtered_strikes if s < spot]
    strikes_above_spot = [s for s in filtered_strikes if s >= spot]
    
    assert len(strikes_below_spot) <= 60  # May be fewer if not enough available
    assert len(strikes_above_spot) <= 10
    
    # Check metadata
    assert metadata["mode"] == "legacy"
    assert metadata["strikes_below_requested"] == 60
    assert metadata["strikes_above_requested"] == 10


def test_strike_filter_moneyness_coverage_for_crash_venture():
    """
    End-to-end test: verify tail mode provides coverage for typical crash venture configs.
    
    Config targets: -8%, -10%, -12%, -15% moneyness
    Spread widths: $10, $20
    """
    exporter = IBKRSnapshotExporter()
    
    spot = 690.0
    all_strikes = [float(s) for s in range(500, 750, 1)]  # Dense grid
    
    # Use 20% tail floor (covers down to -20% moneyness)
    filtered_strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=0.20
    )
    
    # Calculate target strikes for each moneyness
    targets = {
        "-8%": spot * 0.92,   # 634.8
        "-10%": spot * 0.90,  # 621.0
        "-12%": spot * 0.88,  # 607.2
        "-15%": spot * 0.85   # 586.5
    }
    
    for label, target_strike in targets.items():
        # Find nearest available strike
        nearest = min(filtered_strikes, key=lambda s: abs(s - target_strike))
        distance = abs(nearest - target_strike)
        
        # Should be within $5 (reasonable for $1 grid)
        assert distance <= 5.0, \
            f"{label} target {target_strike:.1f}: nearest strike {nearest:.1f} too far (distance={distance:.1f})"
        
        # For $10 and $20 spread widths, need strikes both above and below target
        strikes_around_target = [
            s for s in filtered_strikes 
            if target_strike - 25 <= s <= target_strike + 5
        ]
        assert len(strikes_around_target) >= 25, \
            f"{label}: insufficient strikes around target for spreads"


def test_strike_count_sufficient():
    """
    Test that strike counts are sufficient for candidate generation.
    
    For 4 moneyness targets × 2 spread widths = 8 combinations,
    we need adequate strike availability.
    """
    exporter = IBKRSnapshotExporter()
    
    spot = 690.0
    # Realistic IBKR strike availability: $5 increments below $100, varies above
    strikes_below_100 = [float(s) for s in range(50, 100, 5)]
    strikes_100_to_spot_region = [float(s) for s in range(500, int(spot), 10)]
    strikes_near_atm = [float(s) for s in range(int(spot) - 50, int(spot) + 50, 5)]
    strikes_above_atm = [float(s) for s in range(int(spot) + 50, 800, 10)]
    
    all_strikes = sorted(set(strikes_below_100 + strikes_100_to_spot_region + 
                              strikes_near_atm + strikes_above_atm))
    
    # Use tail mode
    filtered_strikes, metadata = exporter.filter_strikes(
        all_strikes=all_strikes,
        spot=spot,
        tail_moneyness_floor=0.20
    )
    
    # Should have substantial coverage (tail mode filters [floor, spot+buffer])
    # Realistic: at least 20 strikes for adequate coverage
    assert len(filtered_strikes) >= 20, \
        f"Expected >= 20 strikes for deep coverage, got {len(filtered_strikes)}"
    
    # Check distribution - should have strikes in OTM range
    strikes_otm = [s for s in filtered_strikes if s < spot * 0.95]
    assert len(strikes_otm) >= 12, "Need >= 12 OTM strikes for deep targets"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
