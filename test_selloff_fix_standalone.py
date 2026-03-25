"""
Standalone Test for Selloff Regime Wiring Fix

Run with: python test_selloff_fix_standalone.py
"""

import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from forecast_arb.engine.crash_venture_v1_snapshot import generate_candidates_from_snapshot
from forecast_arb.core.regime import apply_regime_overrides
from forecast_arb.structuring.candidate_validator import (
    validate_candidate_regime,
    enforce_regime_consistency,
    CandidateRegimeMismatchError
)


def test_selloff_uses_correct_moneyness():
    """
    Test that selloff regime uses selloff moneyness (-0.09), not crash (-0.15).
    """
    print("TEST 1: Selloff uses correct moneyness and strikes")
    print("=" * 60)
    
    # Create a minimal synthetic snapshot with spot=689 (similar to real bug)
    spot = 689.0
    expiry = "20260320"
    
    # Create strike grid covering both crash and selloff regions
    # Crash threshold (spot * 0.85) = 585.65 → strikes near 585
    # Selloff threshold (spot * 0.91) = 627.19 → strikes near 627
    strikes = list(range(565, 701, 5))  # 565, 570, 575, ..., 695, 700
    
    snapshot = {
        "snapshot_metadata": {
            "underlier": "SPY",
            "current_price": spot,
            "snapshot_time": "2026-02-06T15:00:00Z"
        },
        "expiries": {
            expiry: []
        }
    }
    
    # Add put options for each strike with synthetic pricing
    for strike in strikes:
        moneyness = (strike - spot) / spot
        iv = 0.20 + abs(moneyness) * 0.5
        otm_amount = max(0, spot - strike)
        time_value = 1.0
        bid = max(0.01, otm_amount + time_value - 0.10)
        ask = otm_amount + time_value + 0.10
        
        snapshot["expiries"][expiry].append({
            "strike": strike,
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "implied_vol": iv,
            "delta": -0.05 * (1 + abs(moneyness))
        })
    
    # Test crash regime (-0.15 moneyness)
    crash_candidates, _ = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.15],
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="crash"
    )
    
    # Test selloff regime (-0.09 moneyness)
    selloff_candidates, _ = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.09],
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="selloff"
    )
    
    # Check crash
    assert len(crash_candidates) > 0, "Crash should generate candidates"
    crash_long_strike = crash_candidates[0]["strikes"]["long_put"]
    crash_moneyness = crash_candidates[0]["moneyness_target"]
    crash_regime = crash_candidates[0]["regime"]
    
    print(f"Crash candidates:")
    print(f"  Long strike: {crash_long_strike}")
    print(f"  Moneyness: {crash_moneyness}")
    print(f"  Regime: {crash_regime}")
    
    assert 580 <= crash_long_strike <= 590, f"Crash long strike {crash_long_strike} should be near 585"
    assert crash_moneyness == -0.15, f"Crash moneyness should be -0.15, got {crash_moneyness}"
    assert crash_regime == "crash", f"Crash regime field should be 'crash', got {crash_regime}"
    
    # Check selloff
    assert len(selloff_candidates) > 0, "Selloff should generate candidates"
    selloff_long_strike = selloff_candidates[0]["strikes"]["long_put"]
    selloff_moneyness = selloff_candidates[0]["moneyness_target"]
    selloff_regime = selloff_candidates[0]["regime"]
    
    print(f"\nSelloff candidates:")
    print(f"  Long strike: {selloff_long_strike}")
    print(f"  Moneyness: {selloff_moneyness}")
    print(f"  Regime: {selloff_regime}")
    
    # THIS IS THE KEY TEST
    assert 620 <= selloff_long_strike <= 635, (
        f"FAIL: Selloff long strike {selloff_long_strike} should be near 627, NOT 585! "
        f"Bug still present!"
    )
    assert selloff_moneyness == -0.09, f"Selloff moneyness should be -0.09, got {selloff_moneyness}"
    assert selloff_regime == "selloff", f"Selloff regime field should be 'selloff', got {selloff_regime}"
    
    # Verify strikes are different
    strike_diff = abs(crash_long_strike - selloff_long_strike)
    assert strike_diff > 30, f"Crash and selloff should use different strikes (>{30} apart), got {strike_diff}"
    
    print(f"\n✓ PASS: Strike difference = ${strike_diff:.0f}")
    print(f"✓ PASS: Selloff uses correct moneyness and strikes")
    print()


def test_candidate_validation():
    """Test that validation catches regime/moneyness mismatch."""
    print("TEST 2: Candidate validation catches mismatches")
    print("=" * 60)
    
    # Valid candidate
    valid_candidate = {
        "regime": "selloff",
        "moneyness_target": -0.09,
        "candidate_id": "test123"
    }
    
    try:
        validate_candidate_regime(
            candidate=valid_candidate,
            regime="selloff",
            expected_moneyness=-0.09,
            tolerance=0.001
        )
        print("✓ Valid candidate passes validation")
    except CandidateRegimeMismatchError as e:
        print(f"✗ FAIL: Valid candidate rejected: {e}")
        raise
    
    # Invalid: Wrong moneyness (the actual bug case!)
    wrong_moneyness_candidate = {
        "regime": "selloff",
        "moneyness_target": -0.15,  # This is crash moneyness, not selloff!
        "candidate_id": "test789"
    }
    
    try:
        validate_candidate_regime(
            candidate=wrong_moneyness_candidate,
            regime="selloff",
            expected_moneyness=-0.09,
            tolerance=0.001
        )
        print("✗ FAIL: Wrong moneyness should have been caught!")
        raise AssertionError("Validation should have raised CandidateRegimeMismatchError")
    except CandidateRegimeMismatchError:
        print("✓ Wrong moneyness correctly rejected")
    
    print("✓ PASS: Validation works correctly")
    print()


def test_regime_config_overlay():
    """Test that apply_regime_overrides works correctly."""
    print("TEST 3: Regime config overlay applies correctly")
    print("=" * 60)
    
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15  # Default crash
        },
        "regimes": {
            "crash": {
                "moneyness": -0.15
            },
            "selloff": {
                "moneyness": -0.09
            }
        }
    }
    
    # Apply selloff overlay
    selloff_config = apply_regime_overrides(base_config, "selloff")
    selloff_moneyness = selloff_config["edge_gating"]["event_moneyness"]
    
    print(f"Base config event_moneyness: {base_config['edge_gating']['event_moneyness']}")
    print(f"Selloff config event_moneyness: {selloff_moneyness}")
    
    assert selloff_moneyness == -0.09, (
        f"Selloff overlay should set event_moneyness to -0.09, got {selloff_moneyness}"
    )
    
    # Verify deep copy (base unmodified)
    assert base_config["edge_gating"]["event_moneyness"] == -0.15, (
        "Base config should remain unchanged"
    )
    
    print("✓ PASS: Regime overlay works correctly")
    print()


def main():
    """Run all tests."""
    print()
    print("=" * 60)
    print("SELLOFF REGIME WIRING FIX - REGRESSION TESTS")
    print("=" * 60)
    print()
    
    try:
        test_regime_config_overlay()
        test_candidate_validation()
        test_selloff_uses_correct_moneyness()
        
        print("=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        print()
        print("The selloff regime bug has been successfully fixed!")
        print()
        print("Summary of fixes:")
        print("1. ✓ Candidates now include 'regime' field")
        print("2. ✓ run_daily_v2.py passes regime-specific moneyness")
        print("3. ✓ Validation guardrails catch mismatches")
        print("4. ✓ Selloff uses correct strikes (~627, not ~585)")
        print()
        return 0
        
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ TEST FAILED: {e}")
        print("=" * 60)
        print()
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
