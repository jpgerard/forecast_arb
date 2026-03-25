"""
PR4 Acceptance Test - Regime Selector (Pure Function)

Tests that:
1. RegimeSelector makes conservative decisions
2. eligible_regimes list is populated correctly
3. Confidence scoring works as expected
4. Representable flags reduce confidence
5. STAND_DOWN on missing inputs
"""

import sys
from forecast_arb.oracle.regime_selector import RegimeSelector, RegimeMode


def test_crash_only_eligible():
    """Crash eligible, selloff not → CRASH_ONLY."""
    print("Testing CRASH_ONLY mode...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=0.010,  # Below threshold (1.5%)
        p_implied_selloff=0.05  # Below min (8%)
    )
    
    assert decision.regime_mode == RegimeMode.CRASH_ONLY
    assert decision.eligible_regimes == ["crash"]
    assert "crash" in decision.reasons
    assert "ELIGIBLE" in decision.reasons["crash"]
    assert decision.confidence == 1.0  # Full confidence (both representable by default)
    print("✓ PASS")


def test_selloff_only_eligible():
    """Selloff eligible, crash not → SELLOFF_ONLY."""
    print("Testing SELLOFF_ONLY mode...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=0.020,  # Above threshold (1.5%)
        p_implied_selloff=0.15  # In band [8%, 25%]
    )
    
    assert decision.regime_mode == RegimeMode.SELLOFF_ONLY
    assert decision.eligible_regimes == ["selloff"]
    assert "selloff" in decision.reasons
    assert "ELIGIBLE" in decision.reasons["selloff"]
    assert decision.confidence == 1.0
    print("✓ PASS")


def test_both_eligible():
    """Both crash and selloff eligible → BOTH."""
    print("Testing BOTH mode...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=0.012,  # Below threshold
        p_implied_selloff=0.15  # In band
    )
    
    assert decision.regime_mode == RegimeMode.BOTH
    assert set(decision.eligible_regimes) == {"crash", "selloff"}
    assert "crash" in decision.reasons
    assert "selloff" in decision.reasons
    assert "ELIGIBLE" in decision.reasons["crash"]
    assert "ELIGIBLE" in decision.reasons["selloff"]
    assert decision.confidence == 1.0
    print("✓ PASS")


def test_stand_down_neither_eligible():
    """Neither crash nor selloff eligible → STAND_DOWN."""
    print("Testing STAND_DOWN (neither eligible)...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=0.020,  # Above threshold
        p_implied_selloff=0.05  # Below min
    )
    
    assert decision.regime_mode == RegimeMode.STAND_DOWN
    assert decision.eligible_regimes == []
    assert "INELIGIBLE" in decision.reasons["crash"]
    assert "INELIGIBLE" in decision.reasons["selloff"]
    print("✓ PASS")


def test_stand_down_missing_inputs():
    """Missing inputs → STAND_DOWN (conservative)."""
    print("Testing STAND_DOWN (missing inputs)...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=None,
        p_implied_selloff=None
    )
    
    assert decision.regime_mode == RegimeMode.STAND_DOWN
    assert decision.eligible_regimes == []
    assert "MISSING_INPUTS" in decision.reasons.get("STAND_DOWN", "")
    assert decision.confidence == 0.0  # Zero confidence on missing inputs
    print("✓ PASS")


def test_confidence_reduced_not_representable():
    """Confidence reduced when not representable."""
    print("Testing confidence reduction...", end=" ")
    
    selector = RegimeSelector()
    
    # Both representable
    decision1 = selector.select_regime(
        p_implied_crash=0.012,
        p_implied_selloff=0.15,
        representable_crash=True,
        representable_selloff=True
    )
    assert decision1.confidence == 1.0
    
    # Crash not representable
    decision2 = selector.select_regime(
        p_implied_crash=0.012,
        p_implied_selloff=0.15,
        representable_crash=False,
        representable_selloff=True
    )
    assert decision2.confidence == 0.5  # 1.0 * 0.5
    assert "(NOT_REPRESENTABLE)" in decision2.reasons["crash"]
    
    # Both not representable
    decision3 = selector.select_regime(
        p_implied_crash=0.012,
        p_implied_selloff=0.15,
        representable_crash=False,
        representable_selloff=False
    )
    assert decision3.confidence == 0.25  # 1.0 * 0.5 * 0.5
    assert "(NOT_REPRESENTABLE)" in decision3.reasons["crash"]
    assert "(NOT_REPRESENTABLE)" in decision3.reasons["selloff"]
    
    print("✓ PASS")


def test_selloff_priced_in_warning():
    """Selloff still eligible but with warning when above max."""
    print("Testing selloff priced-in warning...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=0.020,  # Not eligible
        p_implied_selloff=0.30  # Above max (25%)
    )
    
    assert decision.regime_mode == RegimeMode.SELLOFF_ONLY
    assert "selloff" in decision.eligible_regimes
    assert "PRICED_IN_WARNING" in decision.reasons["selloff"]
    print("✓ PASS")


def test_partial_missing_inputs():
    """One input missing → still makes decision with available data."""
    print("Testing partial missing inputs...", end=" ")
    
    selector = RegimeSelector()
    
    # Only crash available
    decision1 = selector.select_regime(
        p_implied_crash=0.012,
        p_implied_selloff=None
    )
    assert decision1.regime_mode == RegimeMode.CRASH_ONLY
    assert "SKIP" in decision1.reasons["selloff"]
    
    # Only selloff available
    decision2 = selector.select_regime(
        p_implied_crash=None,
        p_implied_selloff=0.15
    )
    assert decision2.regime_mode == RegimeMode.SELLOFF_ONLY
    assert "SKIP" in decision2.reasons["crash"]
    
    print("✓ PASS")


def test_to_dict_serialization():
    """RegimeDecision to_dict() includes all new fields."""
    print("Testing RegimeDecision.to_dict()...", end=" ")
    
    selector = RegimeSelector()
    decision = selector.select_regime(
        p_implied_crash=0.012,
        p_implied_selloff=0.15,
        representable_crash=False
    )
    
    data = decision.to_dict()
    
    assert "regime_mode" in data
    assert "eligible_regimes" in data
    assert "reasons" in data
    assert "metrics" in data
    assert "confidence" in data
    assert "timestamp_utc" in data
    
    assert data["regime_mode"] == "BOTH"
    assert set(data["eligible_regimes"]) == {"crash", "selloff"}
    assert isinstance(data["reasons"], dict)
    assert data["confidence"] == 0.5
    
    print("✓ PASS")


def test_create_regime_selector_factory():
    """create_regime_selector factory function works."""
    print("Testing create_regime_selector factory...", end=" ")
    
    from forecast_arb.oracle.regime_selector import create_regime_selector
    
    # Default
    selector1 = create_regime_selector()
    assert selector1.crash_p_threshold == 0.015
    assert selector1.selloff_p_min == 0.08
    assert selector1.selloff_p_max == 0.25
    
    # With config
    config = {
        "regime_selector": {
            "crash_p_threshold": 0.020,
            "selloff_p_min": 0.10,
            "selloff_p_max": 0.30
        }
    }
    selector2 = create_regime_selector(config)
    assert selector2.crash_p_threshold == 0.020
    assert selector2.selloff_p_min == 0.10
    assert selector2.selloff_p_max == 0.30
    
    print("✓ PASS")


def main():
    """Run all PR4 acceptance tests."""
    print("=" * 80)
    print("PR4 ACCEPTANCE TESTS - Regime Selector (Pure Function)")
    print("=" * 80)
    print()
    
    tests = [
        test_crash_only_eligible,
        test_selloff_only_eligible,
        test_both_eligible,
        test_stand_down_neither_eligible,
        test_stand_down_missing_inputs,
        test_confidence_reduced_not_representable,
        test_selloff_priced_in_warning,
        test_partial_missing_inputs,
        test_to_dict_serialization,
        test_create_regime_selector_factory
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR: {e}")
            failed += 1
    
    print()
    print("=" * 80)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 80)
    
    if failed > 0:
        print("\n❌ PR4 ACCEPTANCE TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ PR4 ACCEPTANCE TESTS PASSED")
        print("\nPR4 Summary:")
        print("  • RegimeSelector enhanced with representability")
        print("  • eligible_regimes list populated correctly")
        print("  • Confidence scoring based on data quality")
        print("  • Conservative: STAND_DOWN on missing inputs")
        print("  • Reasons per-regime (dict structure)")
        print("  • Pure function, fully unit testable")
        print("\nNext: PR5 - Pipeline Orchestration (run_daily.py)")
        sys.exit(0)


if __name__ == "__main__":
    main()
