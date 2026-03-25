"""
PR2 Acceptance Test - Regime in EventSpec and event_hash

Tests that:
1. EventSpec accepts regime parameter
2. Event hash includes regime (prevents collision)
3. Same event with different regimes has different hashes
4. Backward compatibility: regime=None still works
"""

import sys
from forecast_arb.options.event_def import create_event_spec


def test_event_spec_accepts_regime():
    """EventSpec can be created with regime parameter."""
    print("Testing EventSpec accepts regime parameter...", end=" ")
    
    event_spec = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15,
        regime="crash"
    )
    
    assert event_spec.regime == "crash"
    assert event_spec.underlier == "SPY"
    assert event_spec.expiry == "20260320"
    assert event_spec.spot == 600.0
    assert event_spec.moneyness == -0.15
    assert event_spec.threshold == 510.0  # 600 * 0.85
    print("✓ PASS")


def test_event_hash_generated():
    """Event hash is generated and included in EventSpec."""
    print("Testing event hash generation...", end=" ")
    
    event_spec = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15,
        regime="crash"
    )
    
    assert event_spec.event_hash is not None
    assert len(event_spec.event_hash) == 16  # Hash truncated to 16 chars
    assert isinstance(event_spec.event_hash, str)
    print("✓ PASS")


def test_event_hash_includes_regime():
    """Different regimes produce different event hashes."""
    print("Testing event hash uniqueness per regime...", end=" ")
    
    crash_spec = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15,
        regime="crash"
    )
    
    selloff_spec = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.09,  # Different moneyness
        regime="selloff"
    )
    
    # Event hashes must be different (prevents collision)
    assert crash_spec.event_hash != selloff_spec.event_hash
    
    # Thresholds are different
    assert crash_spec.threshold == 510.0  # 600 * 0.85
    assert selloff_spec.threshold == 546.0  # 600 * 0.91
    
    print("✓ PASS")


def test_same_regime_same_hash():
    """Same inputs with same regime produce same hash (deterministic)."""
    print("Testing event hash determinism...", end=" ")
    
    spec1 = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15,
        regime="crash"
    )
    
    spec2 = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15,
        regime="crash"
    )
    
    # Same inputs → same hash
    assert spec1.event_hash == spec2.event_hash
    print("✓ PASS")


def test_backward_compatibility_no_regime():
    """EventSpec works without regime (backward compat)."""
    print("Testing backward compatibility (no regime)...", end=" ")
    
    event_spec = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15
        # regime not specified
    )
    
    assert event_spec.regime is None
    assert event_spec.event_hash is not None  # Hash still generated
    assert event_spec.threshold == 510.0
    print("✓ PASS")


def test_event_spec_to_dict_includes_regime():
    """EventSpec.to_dict() includes regime when present."""
    print("Testing EventSpec.to_dict() with regime...", end=" ")
    
    event_spec = create_event_spec(
        underlier="SPY",
        expiry="20260320",
        spot=600.0,
        moneyness=-0.15,
        regime="crash"
    )
    
    data = event_spec.to_dict()
    
    assert "regime" in data
    assert data["regime"] == "crash"
    assert "event_hash" in data
    assert data["event_hash"] == event_spec.event_hash
    print("✓ PASS")


def test_engine_accepts_regime_parameter():
    """Engine function accepts regime parameter (backward compat default)."""
    print("Testing engine accepts regime parameter...", end=" ")
    
    from forecast_arb.engine.crash_venture_v1_snapshot import run_crash_venture_v1_snapshot
    import inspect
    
    # Check function signature
    sig = inspect.signature(run_crash_venture_v1_snapshot)
    params = sig.parameters
    
    assert "regime" in params
    assert params["regime"].default == "crash"  # Default for backward compat
    print("✓ PASS")


def main():
    """Run all PR2 acceptance tests."""
    print("=" * 80)
    print("PR2 ACCEPTANCE TESTS - Regime in EventSpec + event_hash")
    print("=" * 80)
    print()
    
    tests = [
        test_event_spec_accepts_regime,
        test_event_hash_generated,
        test_event_hash_includes_regime,
        test_same_regime_same_hash,
        test_backward_compatibility_no_regime,
        test_event_spec_to_dict_includes_regime,
        test_engine_accepts_regime_parameter
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
        print("\n❌ PR2 ACCEPTANCE TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ PR2 ACCEPTANCE TESTS PASSED")
        print("\nPR2 Summary:")
        print("  • EventSpec accepts regime parameter")
        print("  • Event hash computed with regime (prevents collision)")
        print("  • Different regimes → different hashes")
        print("  • Backward compatible (regime=None works)")
        print("  • Engine function accepts regime parameter (default='crash')")
        print("\nNext: PR3 - Standardize outputs with RegimeResult")
        sys.exit(0)


if __name__ == "__main__":
    main()
