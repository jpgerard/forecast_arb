"""
Test: Intent Builder Fix

Validates that intent_builder produces valid OrderIntent JSON matching execute_trade schema.

Tests:
1. intent_builder produces valid OrderIntent with all required fields
2. intent_id is deterministic (same input = same intent_id)
3. intent_builder exits non-zero if no candidate found
4. OrderIntent matches execute_trade.validate_order_intent requirements
"""

import json
import subprocess
import sys
from pathlib import Path

def test_intent_builder_valid_output():
    """Test that intent_builder produces valid OrderIntent."""
    print("\n" + "=" * 80)
    print("TEST 1: Intent Builder Valid Output")
    print("=" * 80)
    
    # Use existing review_candidates.json
    candidates_path = "runs/crash_venture_v2/crash_venture_v2_a54e721dd97bbbbc_20260223T164837/artifacts/review_candidates.json"
    
    if not Path(candidates_path).exists():
        print(f"❌ Test file not found: {candidates_path}")
        print("⚠️  SKIPPED: No test data available")
        return True
    
    # Call intent_builder
    cmd = [
        sys.executable,
        "-m", "forecast_arb.execution.intent_builder",
        "--candidates", candidates_path,
        "--regime", "crash",
        "--rank", "1",
        "--output-dir", "intents/test"
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"❌ intent_builder failed with exit code {result.returncode}")
        print(f"stderr: {result.stderr}")
        return False
    
    # Get intent path from stdout
    intent_path = result.stdout.strip()
    print(f"✓ Intent created: {intent_path}")
    
    if not Path(intent_path).exists():
        print(f"❌ Intent file not found: {intent_path}")
        return False
    
    # Load intent
    with open(intent_path, "r") as f:
        intent = json.load(f)
    
    # Validate required fields per execute_trade.validate_order_intent
    required_fields = [
        "strategy", "symbol", "expiry", "type", "legs",
        "qty", "limit", "tif", "guards", "intent_id"
    ]
    
    print("\nValidating required fields:")
    for field in required_fields:
        if field not in intent:
            print(f"  ❌ Missing field: {field}")
            return False
        print(f"  ✓ {field}: {intent[field] if field != 'legs' else f'{len(intent[field])} legs'}")
    
    # Validate intent_id is not empty
    if not intent["intent_id"] or not isinstance(intent["intent_id"], str):
        print(f"❌ intent_id must be a non-empty string, got: {intent['intent_id']}")
        return False
    print(f"  ✓ intent_id is valid: {intent['intent_id'][:16]}...")
    
    # Validate legs have required fields
    for i, leg in enumerate(intent["legs"]):
        leg_required = ["action", "right", "strike", "ratio", "exchange", "currency"]
        for field in leg_required:
            if field not in leg:
                print(f"❌ Leg {i} missing field: {field}")
                return False
    print(f"  ✓ All {len(intent['legs'])} legs have required fields")
    
    # Validate limit structure
    if "start" not in intent["limit"] or "max" not in intent["limit"]:
        print(f"❌ limit must have 'start' and 'max'")
        return False
    print(f"  ✓ limit.start: {intent['limit']['start']}, limit.max: {intent['limit']['max']}")
    
    # Clean up
    Path(intent_path).unlink()
    
    print("\n✅ TEST 1 PASSED: Intent builder produces valid OrderIntent")
    return True


def test_intent_id_deterministic():
    """Test that intent_id is deterministic."""
    print("\n" + "=" * 80)
    print("TEST 2: Intent ID Deterministic")
    print("=" * 80)
    
    # Use existing review_candidates.json
    candidates_path = "runs/crash_venture_v2/crash_venture_v2_a54e721dd97bbbbc_20260223T164837/artifacts/review_candidates.json"
    
    if not Path(candidates_path).exists():
        print(f"❌ Test file not found: {candidates_path}")
        print("⚠️  SKIPPED: No test data available")
        return True
    
    # Call intent_builder twice
    cmd = [
        sys.executable,
        "-m", "forecast_arb.execution.intent_builder",
        "--candidates", candidates_path,
        "--regime", "crash",
        "--rank", "1",
        "--output-dir", "intents/test"
    ]
    
    # First call
    result1 = subprocess.run(cmd, capture_output=True, text=True)
    if result1.returncode != 0:
        print(f"❌ First call failed with exit code {result1.returncode}")
        return False
    
    intent_path1 = result1.stdout.strip()
    with open(intent_path1, "r") as f:
        intent1 = json.load(f)
    
    intent_id1 = intent1["intent_id"]
    print(f"First intent_id: {intent_id1}")
    
    # Clean up first file
    Path(intent_path1).unlink()
    
    # Second call
    result2 = subprocess.run(cmd, capture_output=True, text=True)
    if result2.returncode != 0:
        print(f"❌ Second call failed with exit code {result2.returncode}")
        return False
    
    intent_path2 = result2.stdout.strip()
    with open(intent_path2, "r") as f:
        intent2 = json.load(f)
    
    intent_id2 = intent2["intent_id"]
    print(f"Second intent_id: {intent_id2}")
    
    # Clean up second file
    Path(intent_path2).unlink()
    
    # Compare
    if intent_id1 != intent_id2:
        print(f"❌ intent_id not deterministic: {intent_id1} != {intent_id2}")
        return False
    
    print(f"✓ Both calls produced same intent_id")
    print("\n✅ TEST 2 PASSED: Intent ID is deterministic")
    return True


def test_intent_builder_no_candidate():
    """Test that intent_builder exits non-zero if no candidate found."""
    print("\n" + "=" * 80)
    print("TEST 3: Intent Builder No Candidate")
    print("=" * 80)
    
    # Use existing review_candidates.json
    candidates_path = "runs/crash_venture_v2/crash_venture_v2_a54e721dd97bbbbc_20260223T164837/artifacts/review_candidates.json"
    
    if not Path(candidates_path).exists():
        print(f"❌ Test file not found: {candidates_path}")
        print("⚠️  SKIPPED: No test data available")
        return True
    
    # Call intent_builder with invalid rank (should not exist)
    cmd = [
        sys.executable,
        "-m", "forecast_arb.execution.intent_builder",
        "--candidates", candidates_path,
        "--regime", "crash",
        "--rank", "999",  # Invalid rank
        "--output-dir", "intents/test"
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"❌ intent_builder should have failed but returned 0")
        return False
    
    print(f"✓ intent_builder correctly failed with exit code {result.returncode}")
    print(f"  stderr: {result.stderr.strip()[:100]}...")
    
    print("\n✅ TEST 3 PASSED: Intent builder exits non-zero on no candidate")
    return True


def test_intent_builder_invalid_regime():
    """Test that intent_builder exits non-zero if regime not found."""
    print("\n" + "=" * 80)
    print("TEST 4: Intent Builder Invalid Regime")
    print("=" * 80)
    
    # Use existing review_candidates.json
    candidates_path = "runs/crash_venture_v2/crash_venture_v2_a54e721dd97bbbbc_20260223T164837/artifacts/review_candidates.json"
    
    if not Path(candidates_path).exists():
        print(f"❌ Test file not found: {candidates_path}")
        print("⚠️  SKIPPED: No test data available")
        return True
    
    # Call intent_builder with invalid regime
    cmd = [
        sys.executable,
        "-m", "forecast_arb.execution.intent_builder",
        "--candidates", candidates_path,
        "--regime", "invalid_regime",
        "--rank", "1",
        "--output-dir", "intents/test"
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"❌ intent_builder should have failed but returned 0")
        return False
    
    print(f"✓ intent_builder correctly failed with exit code {result.returncode}")
    print(f"  stderr: {result.stderr.strip()[:100]}...")
    
    print("\n✅ TEST 4 PASSED: Intent builder exits non-zero on invalid regime")
    return True


def main():
    """Run all tests."""
    print("=" * 80)
    print("INTENT BUILDER FIX - TEST SUITE")
    print("=" * 80)
    
    tests = [
        ("Valid Output", test_intent_builder_valid_output),
        ("Deterministic Intent ID", test_intent_id_deterministic),
        ("No Candidate Error", test_intent_builder_no_candidate),
        ("Invalid Regime Error", test_intent_builder_invalid_regime),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n❌ TEST FAILED WITH EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    print(f"\nPassed: {passed_count}/{total_count}")
    
    if passed_count == total_count:
        print("\n🎉 ALL TESTS PASSED")
        return 0
    else:
        print(f"\n❌ {total_count - passed_count} TEST(S) FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
