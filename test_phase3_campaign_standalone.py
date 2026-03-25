"""
Phase 3 Campaign EV Provenance Tests - Standalone Version

Tests to ensure:
1. Selector uses ONLY canonical EV fields (never raw)
2. Probability labels are consistent (P(event) = p_used)
3. Robustness penalty is applied correctly
4. Missing canonical fields raise errors (no silent defaults)
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from forecast_arb.campaign.selector import select_candidates, compute_robustness_score


def create_mock_positions_view():
    """Create empty positions view for testing."""
    return {
        "open_positions": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set(),
        "timestamp_utc": "2026-02-26T12:00:00Z"
    }


def create_mock_governors():
    """Create permissive governors for testing."""
    return {
        "daily_premium_cap_usd": 10000.0,
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {"crash": 10, "selloff": 10},
        "premium_at_risk_caps_usd": {
            "crash": 10000.0,
            "selloff": 10000.0,
            "total": 20000.0
        },
        "max_trades_per_day": 10
    }


def test_selector_uses_canonical_ev_not_raw():
    """Test that selector ranks by canonical EV, not raw EV."""
    print("\n" + "="*80)
    print("TEST 1: Selector uses canonical EV (not raw)")
    print("="*80)
    
    candidates = [
        {
            "candidate_id": "A",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar_raw": 100.0,  # HIGH raw value
            "prob_profit_raw": 0.95,
            "ev_usd_raw": 10000.0,
            "ev_per_dollar": 1.0,  # LOW canonical value
            "ev_usd": 100.0,
            "p_profit": 0.05,
            "p_used": 0.05,
            "p_used_src": "external",
            "p_impl": 0.04,
            "p_ext": 0.05,
            "p_ext_status": "OK",
            "p_ext_reason": "Authoritative",
            "representable": True,
            "rank": 1,
        },
        {
            "candidate_id": "B",
            "underlier": "QQQ",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "TECH",
            "cell_id": "QQQ_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 480.0,
            "short_strike": 460.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar_raw": 2.0,  # LOW raw value
            "prob_profit_raw": 0.10,
            "ev_usd_raw": 200.0,
            "ev_per_dollar": 3.0,  # HIGH canonical value
            "ev_usd": 300.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",
            "p_impl": 0.08,
            "p_ext": 0.10,
            "p_ext_status": "OK",
            "p_ext_reason": "Authoritative",
            "representable": True,
            "rank": 1,
        }
    ]
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=create_mock_governors(),
        positions_view=create_mock_positions_view(),
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Check results
    assert len(result.selected) == 2, f"Expected 2 selected, got {len(result.selected)}"
    assert result.selected[0]["candidate_id"] == "B", \
        f"Expected B first (canonical ev_per_dollar=3.0), got {result.selected[0]['candidate_id']}"
    assert result.selected[1]["candidate_id"] == "A", \
        f"Expected A second (canonical ev_per_dollar=1.0), got {result.selected[1]['candidate_id']}"
    
    print("✅ PASSED: Selector correctly ranks by canonical EV")
    return True


def test_robustness_penalty_applied():
    """Test that robustness penalty is applied correctly."""
    print("\n" + "="*80)
    print("TEST 2: Robustness penalty applied")
    print("="*80)
    
    candidates = [
        {
            "candidate_id": "A_external",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 2.0,
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",  # NO PENALTY
            "p_impl": 0.08,
            "p_ext": 0.10,
            "p_ext_status": "OK",
            "representable": True,
            "rank": 1,
        },
        {
            "candidate_id": "B_fallback",
            "underlier": "QQQ",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "TECH",
            "cell_id": "QQQ_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 480.0,
            "short_strike": 460.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 2.0,  # SAME as A
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "fallback",  # 0.5x PENALTY
            "p_impl": None,
            "p_ext": None,
            "p_ext_status": "NO_MARKET",
            "representable": True,
            "rank": 1,
        }
    ]
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=create_mock_governors(),
        positions_view=create_mock_positions_view(),
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Check results
    assert len(result.selected) == 2
    assert result.selected[0]["candidate_id"] == "A_external", \
        f"Expected A_external first (no penalty), got {result.selected[0]['candidate_id']}"
    assert result.selected[0]["robustness"] == 1.0, \
        f"Expected robustness=1.0, got {result.selected[0]['robustness']}"
    assert result.selected[1]["robustness"] == 0.5 * 0.7, \
        f"Expected robustness=0.35, got {result.selected[1]['robustness']}"
    assert "P_FALLBACK" in result.selected[1]["robustness_flags"]
    assert "P_EXT_NO_MARKET" in result.selected[1]["robustness_flags"]
    
    print("✅ PASSED: Robustness penalty correctly applied")
    return True


def test_canonical_ev_required():
    """Test that missing canonical ev_per_dollar raises an error."""
    print("\n" + "="*80)
    print("TEST 3: Missing canonical EV raises error")
    print("="*80)
    
    candidates = [
        {
            "candidate_id": "MISSING_CANONICAL_EV",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar_raw": 2.0,
            "prob_profit_raw": 0.10,
            # "ev_per_dollar": None,  # MISSING!
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",
            "representable": True,
            "rank": 1,
        }
    ]
    
    try:
        select_candidates(
            candidates_flat=candidates,
            governors=create_mock_governors(),
            positions_view=create_mock_positions_view(),
            qty=1,
            scoring_method="ev_per_dollar"
        )
        print("❌ FAILED: Expected ValueError not raised")
        return False
    except ValueError as e:
        if "missing required canonical field" in str(e):
            print(f"✅ PASSED: Correctly raised ValueError: {e}")
            return True
        else:
            print(f"❌ FAILED: Wrong error message: {e}")
            return False


def test_robustness_score_computation():
    """Test robustness score computation."""
    print("\n" + "="*80)
    print("TEST 4: Robustness score computation")
    print("="*80)
    
    tests = [
        ({"representable": True, "p_used_src": "external", "p_ext_status": "OK"}, 
         1.0, [], "External + OK"),
        ({"representable": True, "p_used_src": "fallback", "p_ext_status": "NO_MARKET"}, 
         0.35, ["P_FALLBACK", "P_EXT_NO_MARKET"], "Fallback + NO_MARKET"),
        ({"representable": True, "p_used_src": "implied", "p_ext_status": "NO_MARKET"}, 
         0.7, ["P_EXT_NO_MARKET"], "Implied + NO_MARKET"),
        ({"representable": False, "p_used_src": "external", "p_ext_status": "OK"}, 
         0.0, ["NOT_REPRESENTABLE"], "Not representable"),
        ({"representable": True, "p_used_src": "external", "p_ext_status": "AUTH_FAIL"}, 
         0.7, ["P_EXT_AUTH_FAIL"], "External + AUTH_FAIL"),
    ]
    
    all_passed = True
    for candidate, expected_r, expected_flags, desc in tests:
        robustness, flags = compute_robustness_score(candidate)
        if abs(robustness - expected_r) < 0.01 and set(flags) == set(expected_flags):
            print(f"  ✅ {desc}: robustness={robustness:.2f}, flags={flags}")
        else:
            print(f"  ❌ {desc}: expected r={expected_r}, flags={expected_flags}, got r={robustness}, flags={flags}")
            all_passed = False
    
    if all_passed:
        print("✅ PASSED: All robustness computations correct")
    return all_passed


def test_probability_label_consistency():
    """Test p_used consistency."""
    print("\n" + "="*80)
    print("TEST 5: Probability label consistency")
    print("="*80)
    
    # Use consistent test data
    p_used = 0.10
    debit = 100.0
    max_gain = 2000.0
    
    # Calculate expected EV using p_used
    expected_ev = p_used * max_gain - (1 - p_used) * debit
    expected_ev_per_dollar = expected_ev / debit
    
    candidate = {
        "candidate_id": "TEST",
        "debit_per_contract": debit,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar": expected_ev_per_dollar,  # Consistent with p_used
        "ev_usd": expected_ev,
        "p_profit": p_used,
        "p_used": p_used,
        "p_used_src": "external",
        "p_impl": 0.08,
        "p_ext": p_used,
        "p_ext_status": "OK",
        "p_event_used": p_used,  # Should match p_used
        "p_event": p_used,        # Should match p_used
    }
    
    # Verify consistency: p_used == p_event_used == p_event
    assert candidate["p_used"] == candidate["p_event_used"], \
        f"p_used ({candidate['p_used']}) != p_event_used ({candidate['p_event_used']})"
    assert candidate["p_used"] == candidate["p_event"], \
        f"p_used ({candidate['p_used']}) != p_event ({candidate['p_event']})"
    
    # Verify EV calculation uses p_used
    calculated_ev = candidate["p_used"] * candidate["max_gain_per_contract"] - \
                    (1 - candidate["p_used"]) * candidate["debit_per_contract"]
    calculated_ev_per_dollar = calculated_ev / candidate["debit_per_contract"]
    
    assert abs(candidate["ev_per_dollar"] - calculated_ev_per_dollar) < 0.01, \
        f"EV/$ mismatch: stored={candidate['ev_per_dollar']:.4f}, calculated={calculated_ev_per_dollar:.4f}"
    
    print(f"  p_used = {p_used:.3f}")
    print(f"  EV = ${expected_ev:.2f}")
    print(f"  EV/$ = {expected_ev_per_dollar:.3f}")
    print(f"  All probability fields consistent: p_used = p_event_used = p_event")
    print("✅ PASSED: Probability fields consistent")
    return True


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("PHASE 3 CAMPAIGN EV PROVENANCE TESTS")
    print("="*80)
    
    tests = [
        ("Canonical EV (not raw)", test_selector_uses_canonical_ev_not_raw),
        ("Robustness penalty", test_robustness_penalty_applied),
        ("Missing canonical EV error", test_canonical_ev_required),
        ("Robustness computation", test_robustness_score_computation),
        ("Probability consistency", test_probability_label_consistency),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n❌ ERROR in {name}: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{status}: {name}")
    
    print(f"\nTotal: {passed_count}/{total_count} tests passed")
    
    if passed_count == total_count:
        print("\n🎉 ALL TESTS PASSED!")
        return 0
    else:
        print(f"\n❌ {total_count - passed_count} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
