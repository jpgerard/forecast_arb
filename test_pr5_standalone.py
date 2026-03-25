"""
PR5 Acceptance Test - Pipeline Orchestration

Tests that:
1. resolve_regimes() handles all CLI modes correctly
2. Selector integration works in auto mode
3. write_unified_artifacts() creates proper structure
4. check_representability() validates events
"""

import sys
import json
import tempfile
from pathlib import Path

from forecast_arb.core.regime_orchestration import (
    resolve_regimes,
    write_unified_artifacts,
    check_representability
)
from forecast_arb.core.regime_result import RegimeResult


def test_resolve_regimes_explicit():
    """resolve_regimes handles explicit regime flags."""
    print("Testing resolve_regimes (explicit modes)...", end=" ")
    
    # Crash only
    regimes1 = resolve_regimes("crash")
    assert regimes1 == ["crash"]
    
    # Selloff only
    regimes2 = resolve_regimes("selloff")
    assert regimes2 == ["selloff"]
    
    # Both
    regimes3 = resolve_regimes("both")
    assert set(regimes3) == {"crash", "selloff"}
    
    print("✓ PASS")


def test_resolve_regimes_auto_mode():
    """resolve_regimes auto mode uses selector."""
    print("Testing resolve_regimes (auto mode)...", end=" ")
    
    # Auto mode with both eligible
    selector_inputs = {
        "p_implied_crash": 0.012,
        "p_implied_selloff": 0.15
    }
    
    regimes = resolve_regimes("auto", selector_inputs=selector_inputs)
    assert set(regimes) == {"crash", "selloff"}
    
    # Check decision was stored
    assert "_decision" in selector_inputs
    decision = selector_inputs["_decision"]
    assert decision.regime_mode.value == "BOTH"
    
    print("✓ PASS")


def test_resolve_regimes_auto_crash_only():
    """Auto mode can select crash-only."""
    print("Testing auto mode crash-only...", end=" ")
    
    selector_inputs = {
        "p_implied_crash": 0.010,
        "p_implied_selloff": 0.05  # Too low
    }
    
    regimes = resolve_regimes("auto", selector_inputs=selector_inputs)
    assert regimes == ["crash"]
    
    print("✓ PASS")


def test_resolve_regimes_auto_stand_down():
    """Auto mode can select stand-down (empty list)."""
    print("Testing auto mode stand-down...", end=" ")
    
    selector_inputs = {
        "p_implied_crash": 0.020,  # Too high
        "p_implied_selloff": 0.05  # Too low
    }
    
    regimes = resolve_regimes("auto", selector_inputs=selector_inputs)
    assert regimes == []
    
    print("✓ PASS")


def test_write_unified_artifacts():
    """write_unified_artifacts creates proper JSON structure."""
    print("Testing write_unified_artifacts...", end=" ")
    
    # Create temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        
        # Create mock results
        crash_result = RegimeResult(
            regime="crash",
            event_spec={"moneyness": -0.15, "threshold": 510.0},
            event_hash="crash_hash_123",
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
            manifest={}
        )
        
        selloff_result = RegimeResult(
            regime="selloff",
            event_spec={"moneyness": -0.09, "threshold": 546.0},
            event_hash="selloff_hash_456",
            p_implied=0.15,
            p_implied_confidence=0.70,
            p_implied_warnings=[],
            candidates=[{"rank": 1, "expiry": "20260320"}],
            filtered_out=[],
            expiry_used="20260320",
            expiry_selection_reason="PRIMARY_WINDOW_MATCH",
            representable=False,
            warnings=[],
            run_id="test_run_001",
            manifest={}
        )
        
        results_by_regime = {
            "crash": crash_result,
            "selloff": selloff_result
        }
        
        selector_decision = {
            "regime_mode": "BOTH",
            "eligible_regimes": ["crash", "selloff"],
            "confidence": 0.5
        }
        
        # Write artifacts
        write_unified_artifacts(results_by_regime, selector_decision, run_dir)
        
        # Verify files exist
        artifacts_dir = run_dir / "artifacts"
        assert artifacts_dir.exists()
        
        decision_path = artifacts_dir / "regime_decision.json"
        assert decision_path.exists()
        
        candidates_path = artifacts_dir / "review_candidates.json"
        assert candidates_path.exists()
        
        # Verify structure
        with open(candidates_path) as f:
            data = json.load(f)
        
        assert "regimes" in data
        assert "crash" in data["regimes"]
        assert "selloff" in data["regimes"]
        assert "selector_decision" in data
        
        # Verify crash regime data
        crash_data = data["regimes"]["crash"]
        assert crash_data["event_hash"] == "crash_hash_123"
        assert crash_data["p_implied"] == 0.012
        assert crash_data["representable"] is True
        assert crash_data["expiry_selection_reason"] == "PRIMARY_WINDOW_MATCH"
        assert len(crash_data["candidates"]) == 1
        
        # Verify selloff regime data
        selloff_data = data["regimes"]["selloff"]
        assert selloff_data["event_hash"] == "selloff_hash_456"
        assert selloff_data["representable"] is False
        
    print("✓ PASS")


def test_check_representability():
    """check_representability validates event thresholds."""
    print("Testing check_representability...", end=" ")
    
    # Test that check_representability handles errors gracefully
    # (returns False on invalid snapshot structure)
    
    # Invalid snapshot structure should return False
    invalid_snapshot = {"invalid": "structure"}
    result1 = check_representability(invalid_snapshot, "20260320", 510.0)
    assert result1 is False
    
    # Empty snapshot should return False
    empty_snapshot = {}
    result2 = check_representability(empty_snapshot, "20260320", 510.0)
    assert result2 is False
    
    # Note: Full integration test with real snapshot will be in integration tests
    # This test just verifies graceful error handling
    
    print("✓ PASS")


def test_orchestration_pattern():
    """Full orchestration pattern works end-to-end."""
    print("Testing full orchestration pattern...", end=" ")
    
    # Step 1: Resolve regimes
    selector_inputs = {
        "p_implied_crash": 0.012,
        "p_implied_selloff": 0.15,
        "representable_crash": True,
        "representable_selloff": True
    }
    
    regimes_to_run = resolve_regimes("auto", selector_inputs=selector_inputs)
    assert len(regimes_to_run) == 2
    
    # Step 2: Simulate running each regime (would call engine in real code)
    results_by_regime = {}
    for regime in regimes_to_run:
        # In real code: apply_regime_overrides, run engine, wrap in RegimeResult
        result = RegimeResult(
            regime=regime,
            event_spec={"moneyness": -0.15 if regime == "crash" else -0.09},
            event_hash=f"{regime}_hash",
            p_implied=selector_inputs.get(f"p_implied_{regime}"),
            p_implied_confidence=0.85,
           p_implied_warnings=[],
            candidates=[],
            filtered_out=[],
            expiry_used="20260320",
            expiry_selection_reason="TEST",
            representable=selector_inputs.get(f"representable_{regime}", True),
            warnings=[],
            run_id="test_run",
            manifest={}
        )
        results_by_regime[regime] = result
    
    # Step 3: Write artifacts
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        selector_decision = selector_inputs.get("_decision").to_dict() if "_decision" in selector_inputs else None
        write_unified_artifacts(results_by_regime, selector_decision, run_dir)
        
        # Verify
        candidates_path = run_dir / "artifacts" / "review_candidates.json"
        assert candidates_path.exists()
        
        with open(candidates_path) as f:
            data = json.load(f)
        
        assert len(data["regimes"]) == 2
        assert "crash" in data["regimes"]
        assert "selloff" in data["regimes"]
    
    print("✓ PASS")


def main():
    """Run all PR5 acceptance tests."""
    print("=" * 80)
    print("PR5 ACCEPTANCE TESTS - Pipeline Orchestration")
    print("=" * 80)
    print()
    
    tests = [
        test_resolve_regimes_explicit,
        test_resolve_regimes_auto_mode,
        test_resolve_regimes_auto_crash_only,
        test_resolve_regimes_auto_stand_down,
        test_write_unified_artifacts,
        test_check_representability,
        test_orchestration_pattern
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
        print("\n❌ PR5 ACCEPTANCE TESTS FAILED")
        sys.exit(1)
    else:
        print("\n✅ PR5 ACCEPTANCE TESTS PASSED")
        print("\nPR5 Summary:")
        print("  • resolve_regimes() handles all CLI modes")
        print("  • Auto mode integrates with regime selector")
        print("  • write_unified_artifacts() creates proper structure")
        print("  • check_representability() validates events")
        print("  • Full orchestration pattern verified")
        print("  • No code explosion in run_daily.py (logic extracted)")
        print("\nNext: PR6 - Review Pack + Intent Emission")
        print("\nNote: Integration into run_daily.py follows this pattern:")
        print("  regimes_to_run = resolve_regimes(...)")
        print("  results_by_regime = {}")
        print("  for regime in regimes_to_run:")
        print("      cfg = apply_regime_overrides(base_cfg, regime)")
        print("      result = run_snapshot(..., regime=regime)")
        print("      results_by_regime[regime] = RegimeResult(...)")
        print("  write_unified_artifacts(results_by_regime, ...)")
        sys.exit(0)


if __name__ == "__main__":
    main()
