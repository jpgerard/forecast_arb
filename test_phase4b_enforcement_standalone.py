"""
Standalone Test for Phase 4b Execution Enforcement

Tests PR-EXEC-1 through PR-EXEC-5 without pytest dependency.
"""

import sys
from forecast_arb.execution.execute_trade import (
    enforce_intent_immutability,
    apply_price_band_clamping,
    enforce_mode_invariants,
)
from forecast_arb.execution.execution_result import (
    create_execution_result,
    validate_execution_result,
)


def test_pr_exec_1_immutability():
    """Test PR-EXEC-1: Intent immutability."""
    print("\n🔒 Testing PR-EXEC-1: Intent Immutability...")
    
    intent = {
        "expiry": "20260327",
        "legs": [
            {"strike": 590.0, "right": "P", "action": "BUY"},
            {"strike": 570.0, "right": "P", "action": "SELL"},
        ]
    }
    
    # Test pass case
    try:
        enforce_intent_immutability(intent, "20260327", [590.0, 570.0])
        print("  ✓ Immutability check passes when fields match")
    except AssertionError as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    # Test fail case - expiry mismatch
    try:
        enforce_intent_immutability(intent, "20260320", [590.0, 570.0])
        print("  ✗ FAILED: Should have raised expiry mismatch error")
        return False
    except AssertionError as e:
        if "expiry" in str(e):
            print("  ✓ Correctly blocks expiry mismatch")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    # Test fail case - strikes mismatch
    try:
        enforce_intent_immutability(intent, "20260327", [585.0, 565.0])
        print("  ✗ FAILED: Should have raised strikes mismatch error")
        return False
    except AssertionError as e:
        if "strikes" in str(e):
            print("  ✓ Correctly blocks strikes mismatch")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    return True


def test_pr_exec_2_price_clamping():
    """Test PR-EXEC-2: Price band clamping."""
    print("\n💰 Testing PR-EXEC-2: Price Band Clamping...")
    
    intent = {
        "limit": {
            "start": 0.40,
            "max": 0.50
        }
    }
    
    # Test valid range
    try:
        exec_low, exec_high = apply_price_band_clamping(intent, 0.45)
        if exec_low == 0.45 and exec_high == 0.45:
            print("  ✓ Price clamping works for valid range")
        else:
            print(f"  ✗ FAILED: Expected (0.45, 0.45), got ({exec_low}, {exec_high})")
            return False
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    # Test tightening (never loosening)
    try:
        exec_low, exec_high = apply_price_band_clamping(intent, 0.48)
        if exec_low >= 0.40 and exec_high <= 0.50:
            print("  ✓ Price clamping tightens but never loosens")
        else:
            print(f"  ✗ FAILED: Limits loosened: ({exec_low}, {exec_high})")
            return False
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    # Test blocked drift
    intent_narrow = {
        "limit": {
            "start": 0.40,
            "max": 0.42
        }
    }
    try:
        apply_price_band_clamping(intent_narrow, 0.50)
        print("  ✗ FAILED: Should have blocked price drift")
        return False
    except ValueError as e:
        if "BLOCKED_PRICE_DRIFT" in str(e):
            print("  ✓ Correctly blocks price drift")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    return True


def test_pr_exec_3_execution_result():
    """Test PR-EXEC-3: ExecutionResult v2 schema."""
    print("\n📋 Testing PR-EXEC-3: ExecutionResult v2 Schema...")
    
    # Test valid result
    try:
        result = create_execution_result(
            intent_id="test_123",
            mode="quote-only",
            verdict="OK_TO_STAGE",
            reason="Guards passed",
            quotes={
                "long": {"bid": 3.50, "ask": 3.60},
                "short": {"bid": 1.20, "ask": 1.30},
                "combo_mid": 2.25
            },
            limits={
                "intent": [0.40, 0.50],
                "effective": [0.45, 0.45]
            },
            guards={
                "max_debit": "PASS",
                "min_dte": "PASS"
            }
        )
        
        validate_execution_result(result)
        print("  ✓ ExecutionResult v2 schema creates and validates")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    # Test invalid mode
    try:
        result_invalid = create_execution_result(
            intent_id="test",
            mode="invalid",
            verdict="OK_TO_STAGE",
            reason="Test",
            quotes={"long": {}, "short": {}, "combo_mid": 0.0},
            limits={"intent": [], "effective": []},
            guards={}
        )
        validate_execution_result(result_invalid)
        print("  ✗ FAILED: Should have rejected invalid mode")
        return False
    except ValueError as e:
        if "Invalid mode" in str(e):
            print("  ✓ Correctly rejects invalid mode")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    # Test invalid verdict
    try:
        result_invalid = create_execution_result(
            intent_id="test",
            mode="quote-only",
            verdict="INVALID",
            reason="Test",
            quotes={"long": {}, "short": {}, "combo_mid": 0.0},
            limits={"intent": [], "effective": []},
            guards={}
        )
        validate_execution_result(result_invalid)
        print("  ✗ FAILED: Should have rejected invalid verdict")
        return False
    except ValueError as e:
        if "Invalid verdict" in str(e):
            print("  ✓ Correctly rejects invalid verdict")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    return True


def test_pr_exec_4_mode_invariants():
    """Test PR-EXEC-4: Mode invariants."""
    print("\n🛡️ Testing PR-EXEC-4: Mode Invariants...")
    
    # Test quote-only cannot transmit
    try:
        enforce_mode_invariants("paper", True, False, None)
        print("  ✓ Quote-only with transmit=False passes")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    try:
        enforce_mode_invariants("paper", True, True, "SEND")
        print("  ✗ FAILED: Quote-only should not allow transmit")
        return False
    except AssertionError as e:
        if "quote-only" in str(e):
            print("  ✓ Quote-only correctly blocks transmit")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    # Test paper cannot transmit
    try:
        enforce_mode_invariants("paper", False, False, None)
        print("  ✓ Paper mode with transmit=False passes")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    try:
        enforce_mode_invariants("paper", False, True, "SEND")
        print("  ✗ FAILED: Paper mode should not allow transmit")
        return False
    except AssertionError as e:
        if "paper" in str(e):
            print("  ✓ Paper mode correctly blocks transmit")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    # Test live requires confirmation
    try:
        enforce_mode_invariants("live", False, True, "SEND")
        print("  ✓ Live mode with correct confirm passes")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        return False
    
    try:
        enforce_mode_invariants("live", False, True, "wrong")
        print("  ✗ FAILED: Live mode should require correct confirmation")
        return False
    except AssertionError as e:
        if "confirm" in str(e):
            print("  ✓ Live mode correctly requires confirmation")
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False
    
    return True


def main():
    """Run all tests."""
    print("=" * 80)
    print("PHASE 4B EXECUTION ENFORCEMENT TESTS")
    print("=" * 80)
    
    tests = [
        ("PR-EXEC-1: Intent Immutability", test_pr_exec_1_immutability),
        ("PR-EXEC-2: Price Band Clamping", test_pr_exec_2_price_clamping),
        ("PR-EXEC-3: ExecutionResult v2", test_pr_exec_3_execution_result),
        ("PR-EXEC-4: Mode Invariants", test_pr_exec_4_mode_invariants),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n  ✗ {name} CRASHED: {e}")
            results.append((name, False))
    
    # Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
    
    print("=" * 80)
    print(f"TOTAL: {passed_count}/{total_count} tests passed")
    print("=" * 80)
    
    if passed_count == total_count:
        print("\n✅ All enforcement tests passed!")
        return 0
    else:
        print(f"\n❌ {total_count - passed_count} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
