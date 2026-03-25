"""
Standalone test for intent emission (no pytest required)
"""

import json
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from forecast_arb.execution.intent_builder import build_order_intent


def test_build_order_intent_crash():
    """Test crash regime intent"""
    print("Test 1: Build intent for crash regime...")
    
    candidate = {
        "rank": 1,
        "expiry": "20260320",
        "strikes": {
            "long_put": 580.0,
            "short_put": 560.0
        },
        "symbol": "SPY",
        "event_spec_hash": "abc123",
        "candidate_id": "20260320_580_560",
        "moneyness_target": -0.15,
        "metrics": {
            "ev_per_dollar": 0.25
        },
        "structure": {
            "max_loss": 2000,
            "max_gain": 500
        }
    }
    
    intent = build_order_intent(
        candidate=candidate,
        regime="crash",
        qty=1,
        limit_start=2.50,
        limit_max=2.75
    )
    
    # Validate
    assert intent["strategy"] == "crash_venture_v2", f"Expected strategy=crash_venture_v2, got {intent['strategy']}"
    assert intent["regime"] == "crash", f"Expected regime=crash, got {intent['regime']}"
    assert intent["symbol"] == "SPY"
    assert intent["expiry"] == "20260320"
    assert intent["qty"] == 1
    assert intent["limit"]["start"] == 2.50
    assert intent["limit"]["max"] == 2.75
    assert intent["transmit"] is False, "CRITICAL: transmit must be False!"
    assert len(intent["legs"]) == 2
    assert intent["legs"][0]["strike"] == 580.0
    assert intent["legs"][1]["strike"] == 560.0
    
    print("  ✓ Crash intent valid")


def test_build_order_intent_selloff():
    """Test selloff regime intent"""
    print("Test 2: Build intent for selloff regime...")
    
    candidate = {
        "rank": 1,
        "expiry": "20260320",
        "strikes": {
            "long_put": 600.0,
            "short_put": 585.0
        },
        "symbol": "SPY",
        "event_spec_hash": "def456",
        "candidate_id": "20260320_600_585",
        "moneyness_target": -0.09,
        "metrics": {
            "ev_per_dollar": 0.18
        },
        "structure": {
            "max_loss": 1500,
            "max_gain": 350
        }
    }
    
    intent = build_order_intent(
        candidate=candidate,
        regime="selloff",
        qty=2,
        limit_start=1.75,
        limit_max=2.00
    )
    
    # Validate
    assert intent["regime"] == "selloff"
    assert intent["qty"] == 2
    assert intent["metadata"]["moneyness_target"] == -0.09
    assert intent["legs"][0]["strike"] == 600.0
    assert intent["legs"][1]["strike"] == 585.0
    assert intent["transmit"] is False
    
    print("  ✓ Selloff intent valid")


def test_candidate_selection_by_rank():
    """Test selecting candidate by rank"""
    print("Test 3: Candidate selection by rank...")
    
    candidates = [
        {"rank": 1, "expiry": "20260320", "name": "c1"},
        {"rank": 2, "expiry": "20260320", "name": "c2"},
        {"rank": 3, "expiry": "20260320", "name": "c3"},
    ]
    
    def select_candidate_by_rank(candidates_list, rank):
        for c in candidates_list:
            if c.get("rank") == rank:
                return c
        return None
    
    # Find rank 2
    candidate = select_candidate_by_rank(candidates, 2)
    assert candidate is not None
    assert candidate["rank"] == 2
    assert candidate["name"] == "c2"
    
    # Rank not found
    candidate = select_candidate_by_rank(candidates, 99)
    assert candidate is None
    
    print("  ✓ Candidate selection works")


def test_intent_validation():
    """Test CLI validation logic"""
    print("Test 4: Intent mode validation...")
    
    def validate_intent_args(emit_intent, regime, pick_rank, limit_start, limit_max, intent_out):
        if emit_intent:
            required = [regime, pick_rank, limit_start, limit_max, intent_out]
            if any(v is None for v in required):
                raise SystemExit("❌ --emit-intent requires all parameters")
            
            if regime not in ("crash", "selloff"):
                raise SystemExit("❌ --emit-intent requires --regime crash|selloff (not auto/both)")
    
    # Valid: crash
    try:
        validate_intent_args(True, "crash", 1, 2.50, 2.75, "intent.json")
        print("  ✓ Crash regime accepted")
    except SystemExit:
        raise AssertionError("Crash regime should be valid")
    
    # Valid: selloff
    try:
        validate_intent_args(True, "selloff", 1, 1.75, 2.00, "intent.json")
        print("  ✓ Selloff regime accepted")
    except SystemExit:
        raise AssertionError("Selloff regime should be valid")
    
    # Invalid: auto
    try:
        validate_intent_args(True, "auto", 1, 2.50, 2.75, "intent.json")
        raise AssertionError("Auto regime should be rejected")
    except SystemExit:
        print("  ✓ Auto regime rejected")
    
    # Invalid: both
    try:
        validate_intent_args(True, "both", 1, 2.50, 2.75, "intent.json")
        raise AssertionError("Both regime should be rejected")
    except SystemExit:
        print("  ✓ Both regime rejected")
    
    # Invalid: missing rank
    try:
        validate_intent_args(True, "crash", None, 2.50, 2.75, "intent.json")
        raise AssertionError("Missing rank should be rejected")
    except SystemExit:
        print("  ✓ Missing parameters rejected")


def test_intent_file_writing():
    """Test writing intent to file"""
    print("Test 5: Intent file writing...")
    
    candidate = {
        "rank": 1,
        "expiry": "20260320",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "symbol": "SPY",
        "event_spec_hash": "test",
        "candidate_id": "test_id",
        "metrics": {"ev_per_dollar": 0.25},
        "structure": {"max_loss": 2000, "max_gain": 500}
    }
    
    intent = build_order_intent(
        candidate=candidate,
        regime="crash",
        qty=1,
        limit_start=2.50,
        limit_max=2.75
    )
    
    # Write to temp file
    temp_file = Path("test_intent_temp.json")
    with open(temp_file, "w") as f:
        json.dump(intent, f, indent=2)
    
    # Read back and validate
    with open(temp_file, "r") as f:
        loaded = json.load(f)
    
    assert loaded["strategy"] == "crash_venture_v2"
    assert loaded["regime"] == "crash"
    assert loaded["qty"] == 1
    assert loaded["transmit"] is False
    
    # Cleanup
    temp_file.unlink()
    
    print("  ✓ Intent file I/O works")


def main():
    print("=" * 60)
    print("Intent Emission Tests (Standalone)")
    print("=" * 60)
    print()
    
    try:
        test_build_order_intent_crash()
        test_build_order_intent_selloff()
        test_candidate_selection_by_rank()
        test_intent_validation()
        test_intent_file_writing()
        
        print()
        print("=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        return 0
        
    except AssertionError as e:
        print()
        print("=" * 60)
        print(f"❌ TEST FAILED: {e}")
        print("=" * 60)
        return 1
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ UNEXPECTED ERROR: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
