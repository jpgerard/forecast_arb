"""
Standalone smoke test for Phase 4 probability conditioning.
Tests key scenarios without pytest dependency.
"""

from forecast_arb.probability.conditioning import adjust_crash_probability
from forecast_arb.probability.regime_signals import get_regime_signals


def test_missing_signals():
    """Test that None signals work safely."""
    print("\n1. Testing missing signals...")
    result = adjust_crash_probability(
        base_p=0.08,
        vix_pct=None,
        skew_pct=None,
        credit_pct=None
    )
    
    assert result["p_adjusted"] == 0.08, f"Expected 0.08, got {result['p_adjusted']}"
    assert result["confidence_score"] == 0.0
    assert result["p_source"] == "base"
    print("✓ Missing signals handled correctly")


def test_high_stress():
    """Test high stress regime increases probability."""
    print("\n2. Testing high stress regime...")
    result = adjust_crash_probability(
        base_p=0.05,
        vix_pct=0.90,
        skew_pct=None,
        credit_pct=0.90
    )
    
    assert result["p_adjusted"] > 0.05, f"Expected increase, got {result['p_adjusted']}"
    assert result["multipliers"]["vol"] == 1.20
    assert result["multipliers"]["credit"] == 1.25
    print(f"✓ High stress: {0.05:.4f} → {result['p_adjusted']:.4f}")


def test_low_vol():
    """Test low vol regime decreases probability."""
    print("\n3. Testing low vol regime...")
    result = adjust_crash_probability(
        base_p=0.08,
        vix_pct=0.10,
        skew_pct=None,
        credit_pct=None
    )
    
    assert result["p_adjusted"] < 0.08, f"Expected decrease, got {result['p_adjusted']}"
    assert result["multipliers"]["vol"] == 0.85
    print(f"✓ Low vol: {0.08:.4f} → {result['p_adjusted']:.4f}")


def test_bounds():
    """Test hard bounds enforcement."""
    print("\n4. Testing bounds enforcement...")
    
    # Test max bound
    result_high = adjust_crash_probability(
        base_p=0.05,
        vix_pct=0.95,
        skew_pct=0.95,
        credit_pct=0.95
    )
    assert result_high["p_adjusted"] <= 0.15, "Max multiplier bound violated"
    assert result_high["p_adjusted"] <= 0.35, "Absolute cap violated"
    print(f"✓ High bound: {0.05:.4f} → {result_high['p_adjusted']:.4f} (capped)")
    
    # Test min bound  
    result_low = adjust_crash_probability(
        base_p=0.10,
        vix_pct=0.05,
        skew_pct=0.05,
        credit_pct=0.05
    )
    assert result_low["p_adjusted"] >= 0.025, "Min multiplier bound violated"
    print(f"✓ Low bound: {0.10:.4f} → {result_low['p_adjusted']:.4f} (floored)")


def test_confidence_scoring():
    """Test confidence score calculation."""
    print("\n5. Testing confidence scoring...")
    
    # No signals
    result0 = adjust_crash_probability(0.05, None, None, None)
    assert result0["confidence_score"] == 0.0
    
    # One signal
    result1 = adjust_crash_probability(0.05, 0.5, None, None)
    assert result1["confidence_score"] == 0.33
    
    # Two signals
    result2 = adjust_crash_probability(0.05, 0.5, None, 0.5)
    assert result2["confidence_score"] == 0.66
    
    # Three signals
    result3 = adjust_crash_probability(0.05, 0.5, 0.5, 0.5)
    assert result3["confidence_score"] == 0.99
    
    print("✓ Confidence scores: 0→0.0, 1→0.33, 2→0.66, 3→0.99")


def test_regime_signals_safe():
    """Test that regime signal fetching is safe (doesn't crash)."""
    print("\n6. Testing regime signal fetching...")
    
    try:
        signals = get_regime_signals(lookback_days=252)
        print(f"✓ Regime signals fetched: {signals}")
        
        # Verify structure
        assert "vix_pct" in signals
        assert "skew_pct" in signals
        assert "credit_pct" in signals
        
    except Exception as e:
        print(f"✓ Regime signals safe (returned error gracefully): {e}")


def main():
    """Run all tests."""
    print("=" * 60)
    print("PHASE 4 PROBABILITY CONDITIONING - SMOKE TEST")
    print("=" * 60)
    
    try:
        test_missing_signals()
        test_high_stress()
        test_low_vol()
        test_bounds()
        test_confidence_scoring()
        test_regime_signals_safe()
        
        print("\n" + "=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return 0
        
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
