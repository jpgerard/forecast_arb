"""
PR3 Acceptance Test - RegimeResult Standardized Output

Tests that:
1. RegimeResult can be created and serialized
2. Helper methods work correctly
3. create_regime_result wraps engine output
4. No branching required downstream (uniform structure)
"""

import sys
from forecast_arb.core.regime_result import RegimeResult, create_regime_result


def test_regime_result_creation():
    """RegimeResult can be created with all fields."""
    print("Testing RegimeResult creation...", end=" ")
    
    result = RegimeResult(
        regime="crash",
        event_spec={"moneyness": -0.15, "threshold": 510.0},
        event_hash="abc123def456",
        p_implied=0.012,
        p_implied_confidence=0.85,
        p_implied_warnings=[],
        candidates=[{"rank": 1, "expiry": "20260320"}],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="PRIMARY_WINDOW_MATCH",
        representable=True,
        warnings=[],
        run_id="test_run_001",
        manifest={"version": "v1"}
    )
    
    assert result.regime == "crash"
    assert result.event_hash == "abc123def456"
    assert result.p_implied == 0.012
    assert result.representable is True
    print("✓ PASS")


def test_has_candidates():
    """has_candidates() returns correct boolean."""
    print("Testing has_candidates()...", end=" ")
    
    with_candidates = RegimeResult(
        regime="crash",
        event_spec={},
        event_hash="hash1",
        p_implied=None,
        p_implied_confidence=0,
        p_implied_warnings=[],
        candidates=[{"rank": 1}],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="TEST",
        representable=True,
        warnings=[],
        run_id="test1",
        manifest={}
    )
    
    without_candidates = RegimeResult(
        regime="selloff",
        event_spec={},
        event_hash="hash2",
        p_implied=None,
        p_implied_confidence=0,
        p_implied_warnings=[],
        candidates=[],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="TEST",
        representable=False,
        warnings=[],
        run_id="test2",
        manifest={}
    )
    
    assert with_candidates.has_candidates() is True
    assert without_candidates.has_candidates() is False
    print("✓ PASS")


def test_get_top_candidate():
    """get_top_candidate() returns rank 1 candidate."""
    print("Testing get_top_candidate()...", end=" ")
    
    result = RegimeResult(
        regime="crash",
        event_spec={},
        event_hash="hash1",
        p_implied=None,
        p_implied_confidence=0,
        p_implied_warnings=[],
        candidates=[
            {"rank": 2, "strike": 500},
            {"rank": 1, "strike": 510},
            {"rank": 3, "strike": 490}
        ],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="TEST",
        representable=True,
        warnings=[],
        run_id="test1",
        manifest={}
    )
    
    top = result.get_top_candidate()
    assert top is not None
    assert top["rank"] == 1
    assert top["strike"] == 510
    print("✓ PASS")


def test_get_candidate_by_rank():
    """get_candidate_by_rank() finds correct candidate."""
    print("Testing get_candidate_by_rank()...", end=" ")
    
    result = RegimeResult(
        regime="crash",
        event_spec={},
        event_hash="hash1",
        p_implied=None,
        p_implied_confidence=0,
        p_implied_warnings=[],
        candidates=[
            {"rank": 1, "strike": 510},
            {"rank": 2, "strike": 500},
            {"rank": 3, "strike": 490}
        ],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="TEST",
        representable=True,
        warnings=[],
        run_id="test1",
        manifest={}
    )
    
    rank2 = result.get_candidate_by_rank(2)
    assert rank2 is not None
    assert rank2["rank"] == 2
    assert rank2["strike"] == 500
    
    not_found = result.get_candidate_by_rank(99)
    assert not_found is None
    print("✓ PASS")


def test_to_dict_serialization():
    """to_dict() produces serializable dict."""
    print("Testing to_dict() serialization...", end=" ")
    
    result = RegimeResult(
        regime="crash",
        event_spec={"moneyness": -0.15},
        event_hash="hash1",
        p_implied=0.012,
        p_implied_confidence=0.85,
        p_implied_warnings=["test_warning"],
        candidates=[{"rank": 1}],
        filtered_out=[{"reason": "too_cheap"}],
        expiry_used="20260320",
        expiry_selection_reason="PRIMARY_WINDOW_MATCH",
        representable=True,
        warnings=["general_warning"],
        run_id="test1",
        manifest={"version": "v1"}
    )
    
    data = result.to_dict()
    
    assert data["regime"] == "crash"
    assert data["event_hash"] == "hash1"
    assert data["p_implied"] == 0.012
    assert data["p_implied_confidence"] == 0.85
    assert data["p_implied_warnings"] == ["test_warning"]
    assert len(data["candidates"]) == 1
    assert data["expiry_selection_reason"] == "PRIMARY_WINDOW_MATCH"
    assert data["representable"] is True
    print("✓ PASS")


def test_from_dict_deserialization():
    """from_dict() reconstructs RegimeResult."""
    print("Testing from_dict() deserialization...", end=" ")
    
    data = {
        "regime": "selloff",
        "event_spec": {"moneyness": -0.09},
        "event_hash": "hash2",
        "p_implied": 0.15,
        "p_implied_confidence": 0.70,
        "p_implied_warnings": [],
        "candidates": [],
        "filtered_out": [],
        "expiry_used": "20260320",
        "expiry_selection_reason": "ROLLED_FORWARD_NO_STRIKES",
        "representable": False,
        "warnings": [],
        "run_id": "test2",
        "manifest": {}
    }
    
    result = RegimeResult.from_dict(data)
    
    assert result.regime == "selloff"
    assert result.event_hash == "hash2"
    assert result.p_implied == 0.15
    assert result.expiry_selection_reason == "ROLLED_FORWARD_NO_STRIKES"
    assert result.representable is False
    print("✓ PASS")


def test_summary_line():
    """summary_line() produces expected output."""
    print("Testing summary_line()...", end=" ")
    
    # With candidates
    with_cands = RegimeResult(
        regime="crash",
        event_spec={},
        event_hash="hash1",
        p_implied=0.012,
        p_implied_confidence=0.85,
        p_implied_warnings=[],
        candidates=[{"rank": 1}, {"rank": 2}],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="PRIMARY_WINDOW_MATCH",
        representable=True,
        warnings=[],
        run_id="test1",
        manifest={}
    )
    
    summary1 = with_cands.summary_line()
    assert "crash" in summary1
    assert "2 candidates" in summary1
    assert "p_implied=0.012" in summary1
    assert "REPRESENTABLE" in summary1
    assert "PRIMARY_WINDOW_MATCH" in summary1
    
    # Without candidates
    without_cands = RegimeResult(
        regime="selloff",
        event_spec={},
        event_hash="hash2",
        p_implied=None,
        p_implied_confidence=0,
        p_implied_warnings=[],
        candidates=[],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="NO_STRIKES",
        representable=False,
        warnings=[],
        run_id="test2",
        manifest={}
    )
    
    summary2 = without_cands.summary_line()
    assert "selloff" in summary2
    assert "NO_CANDIDATES" in summary2
    assert "NOT_REPRESENTABLE" in summary2
    assert "NO_STRIKES" in summary2
    print("✓ PASS")


def test_create_regime_result_helper():
    """create_regime_result() wraps engine output correctly."""
    print("Testing create_regime_result() helper...", end=" ")
    
    # Simulate engine output
    engine_output = {
        "ok": True,
        "decision": "TRADE",
        "event_spec": {"moneyness": -0.15, "threshold": 510.0},
        "event_hash": "abc123",
        "top_structures": [{"rank": 1, "strike": 510}],
        "filtered_out": [{"reason": "too_cheap"}],
        "expiry_used": "20260320",
        "warnings": ["test_warning"],
        "run_id": "test_run_001",
        "manifest": {"version": "v1"}
    }
    
    result = create_regime_result(
        regime="crash",
        engine_output=engine_output,
        expiry_selection_reason="PRIMARY_WINDOW_MATCH",
        representable=True,
        p_implied=0.012,
        p_implied_confidence=0.85,
        p_implied_warnings=["p_warning"]
    )
    
    assert result.regime == "crash"
    assert result.event_hash == "abc123"
    assert result.p_implied == 0.012
    assert result.p_implied_confidence == 0.85
    assert result.p_implied_warnings == ["p_warning"]
    assert len(result.candidates) == 1
    assert len(result.filtered_out) == 1
    assert result.expiry_used == "20260320"
    assert result.expiry_selection_reason == "PRIMARY_WINDOW_MATCH"
    assert result.representable is True
    assert result.run_id == "test_run_001"
    print("✓ PASS")


def main():
    """Run all PR3 acceptance tests."""
    print("=" * 80)
    print("PR3 ACCEPTANCE TESTS - RegimeResult Standardized Output")
    print("=" * 80)
    print()
    
    tests = [
        test_regime_result_creation,
        test_has_candidates,
        test_get_top_candidate,
        test_get_candidate_by_rank,
        test_to_dict_serialization,
        test_from_dict_deserialization,
        test_summary_line,
        test_create_regime_result_helper
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
        print("\n❌ PR3 ACCEPTANCE TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ PR3 ACCEPTANCE TESTS PASSED")
        print("\nPR3 Summary:")
        print("  • RegimeResult dataclass created")
        print("  • Helper methods work (has_candidates, get_top_candidate, etc.)")
        print("  • Serialization/deserialization (to_dict/from_dict)")
        print("  • create_regime_result() wrapper for engine output")
        print("  • summary_line() for debugging")
        print("  • Prevents downstream branching (uniform structure)")
        print("\nNext: PR4 - Regime Selector (pure function)")
        sys.exit(0)


if __name__ == "__main__":
    main()
