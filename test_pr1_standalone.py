"""
PR1 Acceptance Test - Standalone (no pytest required)

Tests regime config overlay system works and maintains backward compatibility.
"""

import sys
from forecast_arb.core.regime import apply_regime_overrides, get_regime_config


def test_backward_compatibility_no_regimes_section():
    """Config without regimes section returns unchanged."""
    print("Testing backward compatibility (no regimes section)...", end=" ")
    base_config = {
        "campaign_name": "crash_venture_v1",
        "edge_gating": {
            "event_moneyness": -0.15,
            "min_edge": 0.05
        }
    }
    
    result = apply_regime_overrides(base_config, "crash")
    
    assert result["edge_gating"]["event_moneyness"] == -0.15
    assert result == base_config
    print("✓ PASS")


def test_backward_compatibility_no_mutation():
    """Applying override doesn't mutate original config."""
    print("Testing no mutation of original config...", end=" ")
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {
            "crash": {"moneyness": -0.15},
            "selloff": {"moneyness": -0.09}
        }
    }
    
    original_moneyness = base_config["edge_gating"]["event_moneyness"]
    result = apply_regime_overrides(base_config, "selloff")
    
    assert base_config["edge_gating"]["event_moneyness"] == original_moneyness
    assert base_config["edge_gating"]["event_moneyness"] == -0.15
    assert result["edge_gating"]["event_moneyness"] == -0.09
    print("✓ PASS")


def test_crash_regime_overlay():
    """Crash regime overlay applies moneyness correctly."""
    print("Testing crash regime overlay...", end=" ")
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.10
        },
        "regimes": {
            "crash": {
                "moneyness": -0.15,
                "min_otm_boundary": -0.13
            }
        }
    }
    
    result = apply_regime_overrides(base_config, "crash")
    
    assert result["edge_gating"]["event_moneyness"] == -0.15
    assert result["edge_gating"]["min_otm_boundary"] == -0.13
    print("✓ PASS")


def test_selloff_regime_overlay():
    """Selloff regime overlay applies moneyness correctly."""
    print("Testing selloff regime overlay...", end=" ")
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {
            "selloff": {
                "moneyness": -0.09,
               "otm_bounds": [-0.07, -0.12]
            }
        }
    }
    
    result = apply_regime_overrides(base_config, "selloff")
    
    assert result["edge_gating"]["event_moneyness"] == -0.09
    assert result["edge_gating"]["otm_bounds"] == [-0.07, -0.12]
    print("✓ PASS")


def test_get_regime_config_defaults():
    """get_regime_config returns sensible defaults."""
    print("Testing regime config defaults...", end=" ")
    crash_defaults = get_regime_config("crash")
    assert crash_defaults["moneyness"] == -0.15
    assert crash_defaults["min_otm_boundary"] == -0.13
    assert crash_defaults["selector_p_threshold"] == 0.015
    
    selloff_defaults = get_regime_config("selloff")
    assert selloff_defaults["moneyness"] == -0.09
    assert selloff_defaults["otm_bounds"] == [-0.07, -0.12]
    assert selloff_defaults["selector_p_min"] == 0.08
    assert selloff_defaults["selector_p_max"] == 0.25
    print("✓ PASS")


def test_multiple_overrides():
    """Multiple overrides can be applied in sequence."""
    print("Testing multiple overrides...", end=" ")
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {
            "crash": {"moneyness": -0.15},
            "selloff": {"moneyness": -0.09}
        }
    }
    
    crash_cfg = apply_regime_overrides(base_config, "crash")
    assert crash_cfg["edge_gating"]["event_moneyness"] == -0.15
    
    selloff_cfg = apply_regime_overrides(base_config, "selloff")
    assert selloff_cfg["edge_gating"]["event_moneyness"] == -0.09
    
    assert crash_cfg["edge_gating"]["event_moneyness"] == -0.15
    print("✓ PASS")


def main():
    """Run all PR1 acceptance tests."""
    print("=" * 80)
    print("PR1 ACCEPTANCE TESTS - Regime Config Overlay")
    print("=" * 80)
    print()
    
    tests = [
        test_backward_compatibility_no_regimes_section,
        test_backward_compatibility_no_mutation,
        test_crash_regime_overlay,
        test_selloff_regime_overlay,
        test_get_regime_config_defaults,
        test_multiple_overrides
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
        print("\n❌ PR1 ACCEPTANCE TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ PR1 ACCEPTANCE TESTS PASSED")
        print("\nNext: PR2 - Wire regime through EventSpec + event_hash")
        sys.exit(0)


if __name__ == "__main__":
    main()
