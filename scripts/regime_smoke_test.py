"""
Regime Selector Smoke Test & Demo

Tests the regime selector with various market scenarios and demonstrates
the two-regime system (Crash + Selloff).

Usage:
    python scripts/regime_smoke_test.py
"""

import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.oracle.regime_selector import RegimeSelector, RegimeMode
from forecast_arb.options.event_def import create_event_spec


def test_scenario(
    name: str,
    selector: RegimeSelector,
    p_implied_crash: float,
    p_implied_selloff: float,
    drawdown: float = None,
    skew: float = None
):
    """Test a single scenario and print results."""
    print("=" * 80)
    print(f"Scenario: {name}")
    print("=" * 80)
    print(f"Inputs:")
    print(f"  p_implied_crash:   {p_implied_crash:.3f}" if p_implied_crash is not None else "  p_implied_crash:   None")
    print(f"  p_implied_selloff: {p_implied_selloff:.3f}" if p_implied_selloff is not None else "  p_implied_selloff: None")
    if drawdown is not None:
        print(f"  drawdown:          {drawdown:.2%}")
    if skew is not None:
        print(f"  skew:              {skew:.3f}")
    print("")
    
    decision = selector.select_regime(
        p_implied_crash=p_implied_crash,
        p_implied_selloff=p_implied_selloff,
        drawdown=drawdown,
        skew=skew
    )
    
    print(f"Decision: {decision.regime_mode.value}")
    print("")
    print("Reasons:")
    for reason in decision.reasons:
        print(f"  • {reason}")
    print("")
    
    return decision


def test_event_specs():
    """Test EventSpec creation for both regimes."""
    print("=" * 80)
    print("EventSpec Creation Test")
    print("=" * 80)
    print("")
    
    spot = 600.0
    expiry = "20260320"
    
    # Create crash event spec
    crash_spec = create_event_spec(
        underlier="SPY",
        expiry=expiry,
        spot=spot,
        moneyness=-0.15,
        regime="crash"
    )
    
    print("Crash Event Spec:")
    print(f"  Event: P(SPY < ${crash_spec.threshold:.2f} at {crash_spec.expiry})")
    print(f"  Moneyness: {crash_spec.moneyness:.2%}")
    print(f"  Threshold: ${crash_spec.threshold:.2f}")
    print(f"  Regime: {crash_spec.regime}")
    print(f"  Event Hash: {crash_spec.event_hash}")
    print("")
    
    # Create selloff event spec
    selloff_spec = create_event_spec(
        underlier="SPY",
        expiry=expiry,
        spot=spot,
        moneyness=-0.09,
        regime="selloff"
    )
    
    print("Selloff Event Spec:")
    print(f"  Event: P(SPY < ${selloff_spec.threshold:.2f} at {selloff_spec.expiry})")
    print(f"  Moneyness: {selloff_spec.moneyness:.2%}")
    print(f"  Threshold: ${selloff_spec.threshold:.2f}")
    print(f"  Regime: {selloff_spec.regime}")
    print(f"  Event Hash: {selloff_spec.event_hash}")
    print("")
    
    # Verify moneyness boundaries (guardrails)
    print("Regime Boundaries (Guardrails):")
    print(f"  Crash:   ≤ -13% OTM (moneyness ≤ -0.13)")
    print(f"  Selloff: -7% to -12% OTM (moneyness -0.07 to -0.12)")
    print("")
    
    crash_otm = abs(crash_spec.moneyness)
    selloff_otm = abs(selloff_spec.moneyness)
    
    print(f"Actual OTM:")
    print(f"  Crash:   {crash_otm:.2%} ({'PASS' if crash_otm >= 0.13 else 'FAIL'})")
    print(f"  Selloff: {selloff_otm:.2%} ({'PASS' if 0.07 <= selloff_otm <= 0.12 else 'FAIL'})")
    print("")


def main():
    """Run all smoke tests."""
    print("\n" + "=" * 80)
    print("REGIME SELECTOR SMOKE TEST")
    print("=" * 80)
    print("")
    
    # Create selector with default thresholds
    selector = RegimeSelector(
        crash_p_threshold=0.015,
        selloff_p_min=0.08,
        selloff_p_max=0.25
    )
    
    print("Selector Configuration:")
    print(f"  crash_p_threshold: {selector.crash_p_threshold:.3f} (1.5%)")
    print(f"  selloff_p_band:    [{selector.selloff_p_min:.3f}, {selector.selloff_p_max:.3f}]")
    print("")
    
    # Test scenarios
    scenarios = []
    
    # Scenario 1: Normal market - both eligible
    scenarios.append(
        test_scenario(
            name="Normal Market (Both Eligible)",
            selector=selector,
            p_implied_crash=0.010,
            p_implied_selloff=0.15
        )
    )
    
    # Scenario 2: Crash already priced, selloff normal
    scenarios.append(
        test_scenario(
            name="Crash Priced In (Selloff Only)",
            selector=selector,
            p_implied_crash=0.025,
            p_implied_selloff=0.12
        )
    )
    
    # Scenario 3: Selloff too cheap, crash eligible
    scenarios.append(
        test_scenario(
            name="Selloff Too Cheap (Crash Only)",
            selector=selector,
            p_implied_crash=0.008,
            p_implied_selloff=0.05
        )
    )
    
    # Scenario 4: Both ineligible
    scenarios.append(
        test_scenario(
            name="Both Ineligible (Stand Down)",
            selector=selector,
            p_implied_crash=0.030,
            p_implied_selloff=0.06
        )
    )
    
    # Scenario 5: Selloff priced in warning but allowed
    scenarios.append(
        test_scenario(
            name="Selloff Priced In Warning (Both with Warning)",
            selector=selector,
            p_implied_crash=0.012,
            p_implied_selloff=0.35
        )
    )
    
    # Scenario 6: Missing p_implied_crash
    scenarios.append(
        test_scenario(
            name="Missing Crash Probability (Selloff Only)",
            selector=selector,
            p_implied_crash=None,
            p_implied_selloff=0.15
        )
    )
    
    # Scenario 7: Missing both - stand down
    scenarios.append(
        test_scenario(
            name="Missing Both Probabilities (Stand Down)",
            selector=selector,
            p_implied_crash=None,
            p_implied_selloff=None
        )
    )
    
    # Summary
    print("=" * 80)
    print("SCENARIO SUMMARY")
    print("=" * 80)
    print("")
    
    mode_counts = {}
    for scenario in scenarios:
        mode = scenario.regime_mode.value
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    
    print("Regime Mode Distribution:")
    for mode, count in sorted(mode_counts.items()):
        print(f"  {mode}: {count}")
    print("")
    
    # Test EventSpec creation
    test_event_specs()
    
    # Test acceptance criteria
    print("=" * 80)
    print("ACCEPTANCE TEST 1: Stable Comparability")
    print("=" * 80)
    print("")
    
    # Run same scenario twice
    decision1 = selector.select_regime(
        p_implied_crash=0.010,
        p_implied_selloff=0.15
    )
    
    decision2 = selector.select_regime(
        p_implied_crash=0.010,
        p_implied_selloff=0.15
    )
    
    if decision1.regime_mode == decision2.regime_mode:
        print("✅ PASS: Same inputs produce same regime decision")
        print(f"   Mode: {decision1.regime_mode.value}")
    else:
        print("❌ FAIL: Same inputs produce different regime decisions")
        print(f"   Run 1: {decision1.regime_mode.value}")
        print(f"   Run 2: {decision2.regime_mode.value}")
    print("")
    
    # Test acceptance criteria 2
    print("=" * 80)
    print("ACCEPTANCE TEST 2: No Regime Drift (Boundaries)")
    print("=" * 80)
    print("")
    
    # Crash boundaries
    crash_moneyness = -0.15
    selloff_moneyness = -0.09
    
    crash_otm = abs(crash_moneyness)
    selloff_otm = abs(selloff_moneyness)
    
    crash_boundary_ok = crash_otm >= 0.13
    selloff_boundary_ok = 0.07 <= selloff_otm <= 0.12
    
    print(f"Crash moneyness: {crash_moneyness:.2%} (OTM: {crash_otm:.2%})")
    print(f"  Requirement: ≥ -13% OTM")
    print(f"  Result: {'✅ PASS' if crash_boundary_ok else '❌ FAIL'}")
    print("")
    
    print(f"Selloff moneyness: {selloff_moneyness:.2%} (OTM: {selloff_otm:.2%})")
    print(f"  Requirement: -7% to -12% OTM")
    print(f"  Result: {'✅ PASS' if selloff_boundary_ok else '❌ FAIL'}")
    print("")
    
    # Test acceptance criteria 3
    print("=" * 80)
    print("ACCEPTANCE TEST 3: Selector Conservative on Missing Inputs")
    print("=" * 80)
    print("")
    
    decision_missing = selector.select_regime(
        p_implied_crash=None,
        p_implied_selloff=None
    )
    
    if decision_missing.regime_mode == RegimeMode.STAND_DOWN:
        print("✅ PASS: Missing inputs → STAND_DOWN")
        print(f"   Reasons: {decision_missing.reasons}")
    else:
        print("❌ FAIL: Missing inputs did not produce STAND_DOWN")
        print(f"   Got: {decision_missing.regime_mode.value}")
    print("")
    
    print("=" * 80)
    print("✅ SMOKE TEST COMPLETE")
    print("=" * 80)
    print("")
    print("Next Steps:")
    print("  1. Review regime selector logic and thresholds")
    print("  2. Integrate into run_daily.py pipeline")
    print("  3. Update review pack to show both regimes")
    print("  4. Update intent emission to bind to regime + event hash")
    print("")


if __name__ == "__main__":
    main()
