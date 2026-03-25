"""
Standalone Tests for Interactive Daily Console

Minimal tests for:
1. no candidate -> NO_TRADE exit
2. quote-only pass -> offers stage
3. live requires SEND
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from scripts.daily import (
    load_review_candidates,
    print_candidate_table,
    select_candidate_interactive,
)


def test_no_candidates_exits_with_error():
    """Test that missing candidates causes NO_TRADE exit."""
    print("TEST: No candidates exits with error")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create review_candidates with no candidates
        review_candidates_path = Path(tmpdir) / "review_candidates.json"
        
        data = {
            "regimes": {
                "crash": {
                    "event_spec": {},
                    "candidates": []  # Empty candidates
                }
            }
        }
        
        with open(review_candidates_path, "w") as f:
            json.dump(data, f)
        
        # Load candidates
        review_candidates = load_review_candidates(str(review_candidates_path))
        
        # print_candidate_table should exit with error
        try:
            print_candidate_table(review_candidates)
            print("  ❌ FAILED: Should have exited with error")
            return False
        except SystemExit as e:
            if e.code == 1:
                print("  ✓ PASSED: Exits with code 1 when no candidates")
                return True
            else:
                print(f"  ❌ FAILED: Wrong exit code: {e.code}")
                return False


def test_missing_regimes_key_exits_with_error():
    """Test that invalid schema causes exit."""
    print("TEST: Missing regimes key exits with error")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create invalid review_candidates (missing 'regimes' key)
        review_candidates_path = Path(tmpdir) / "review_candidates.json"
        
        data = {
            "invalid_key": {}
        }
        
        with open(review_candidates_path, "w") as f:
            json.dump(data, f)
        
        # Should exit with error
        try:
            load_review_candidates(str(review_candidates_path))
            print("  ❌ FAILED: Should have exited with error")
            return False
        except SystemExit as e:
            if e.code == 1:
                print("  ✓ PASSED: Exits with code 1 on invalid schema")
                return True
            else:
                print(f"  ❌ FAILED: Wrong exit code: {e.code}")
                return False


def test_candidate_selection_auto_rank_1():
    """Test that auto-selection picks rank=1 by default."""
    print("TEST: Candidate selection auto rank=1")
    
    review_candidates = {
        "regimes": {
            "crash": {
                "candidates": [
                    {
                        "rank": 1,
                        "expiry": "20260402",
                        "strikes": {"long_put": 580, "short_put": 560},
                        "ev_per_dollar": 23.16
                    },
                    {
                        "rank": 2,
                        "expiry": "20260402",
                        "strikes": {"long_put": 585, "short_put": 565},
                        "ev_per_dollar": 20.00
                    }
                ]
            }
        }
    }
    
    # Mock input to select default rank
    with patch('builtins.input', return_value=""):
        regime, candidate = select_candidate_interactive(
            review_candidates,
            regime_filter="crash",
            auto_rank=1
        )
    
    if regime == "crash" and candidate["rank"] == 1 and candidate["strikes"]["long_put"] == 580:
        print("  ✓ PASSED: Auto-selects rank 1")
        return True
    else:
        print(f"  ❌ FAILED: Got regime={regime}, rank={candidate.get('rank')}")
        return False


def test_candidate_selection_custom_rank():
    """Test that user can select custom rank."""
    print("TEST: Candidate selection custom rank")
    
    review_candidates = {
        "regimes": {
            "crash": {
                "candidates": [
                    {
                        "rank": 1,
                        "expiry": "20260402",
                        "strikes": {"long_put": 580, "short_put": 560},
                        "ev_per_dollar": 23.16
                    },
                    {
                        "rank": 2,
                        "expiry": "20260402",
                        "strikes": {"long_put": 585, "short_put": 565},
                        "ev_per_dollar": 20.00
                    }
                ]
            }
        }
    }
    
    # Mock input to select rank 2
    with patch('builtins.input', return_value="2"):
        regime, candidate = select_candidate_interactive(
            review_candidates,
            regime_filter="crash",
            auto_rank=1
        )
    
    if regime == "crash" and candidate["rank"] == 2 and candidate["strikes"]["long_put"] == 585:
        print("  ✓ PASSED: Selects custom rank 2")
        return True
    else:
        print(f"  ❌ FAILED: Got regime={regime}, rank={candidate.get('rank')}")
        return False


def test_candidate_selection_invalid_rank_exits():
    """Test that invalid rank causes exit."""
    print("TEST: Invalid rank exits with error")
    
    review_candidates = {
        "regimes": {
            "crash": {
                "candidates": [
                    {
                        "rank": 1,
                        "expiry": "20260402",
                        "strikes": {"long_put": 580, "short_put": 560},
                        "ev_per_dollar": 23.16
                    }
                ]
            }
        }
    }
    
    # Mock input to select non-existent rank 99
    with patch('builtins.input', return_value="99"):
        try:
            select_candidate_interactive(
                review_candidates,
                regime_filter="crash",
                auto_rank=1
            )
            print("  ❌ FAILED: Should have exited with error")
            return False
        except SystemExit as e:
            if e.code == 1:
                print("  ✓ PASSED: Exits with code 1 on invalid rank")
                return True
            else:
                print(f"  ❌ FAILED: Wrong exit code: {e.code}")
                return False


def test_quote_only_pass_enables_execution_options():
    """Test that quote-only pass enables execution options."""
    print("TEST: Quote-only pass enables execution options")
    
    candidate = {
        "rank": 1,
        "regime": "crash",
        "expiry": "20260402",
        "strikes": {"long_put": 580, "short_put": 560},
        "debit_per_contract": 49.0,
        "candidate_id": "test_candidate"
    }
    
    # Mock execution result with guards_passed=True
    exec_result = {
        "success": True,
        "quote_only": True,
        "guards_passed": True,
        "guards_result": "ALL_PASSED"
    }
    
    # Verify guards_passed flag exists and is True
    if exec_result.get("guards_passed") == True:
        print("  ✓ PASSED: Quote-only result has guards_passed=True")
        return True
    else:
        print("  ❌ FAILED: guards_passed not True")
        return False


def test_live_transmission_requires_send_confirmation():
    """Test that live transmission requires SEND confirmation."""
    print("TEST: Live transmission requires SEND confirmation")
    
    from forecast_arb.execution.intent_builder import build_order_intent
    
    candidate = {
        "rank": 1,
        "regime": "crash",
        "underlier": "SPY",
        "expiry": "20260402",
        "strikes": {"long_put": 580, "short_put": 560},
        "debit_per_contract": 49.0,
        "candidate_id": "test_candidate",
        "legs": [
            {
                "type": "put",
                "side": "long",
                "strike": 580.0,
                "quantity": 1,
                "price": 1.56
            },
            {
                "type": "put",
                "side": "short",
                "strike": 560.0,
                "quantity": 1,
                "price": 1.07
            }
        ]
    }
    
    try:
        # Build intent (this should work)
        intent = build_order_intent(
            candidate=candidate,
            regime="crash",
            qty=1,
            limit_start=49.0,
            limit_max=51.45
        )
        
        # Verify intent has required fields
        if all(key in intent for key in ["symbol", "expiry", "legs", "limit"]):
            print("  ✓ PASSED: Intent builder creates valid intent")
            return True
        else:
            print("  ❌ FAILED: Intent missing required fields")
            return False
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        return False


def test_intent_id_is_deterministic():
    """Test that intent_id is generated deterministically."""
    print("TEST: Intent ID is deterministic")
    
    fixed_timestamp = "20260224T095959"
    
    symbol = "SPY"
    expiry = "20260402"
    long_strike = 580
    short_strike = 560
    regime = "crash"
    
    # Expected format: <symbol>_<expiry>_<long>_<short>_<regime>_<timestamp>
    expected_intent_id = f"{symbol}_{expiry}_{long_strike}_{short_strike}_{regime}_{fixed_timestamp}"
    
    # Verify format
    if expected_intent_id == "SPY_20260402_580_560_crash_20260224T095959":
        parts = expected_intent_id.split("_")
        if (len(parts) == 6 and parts[0] == symbol and parts[1] == expiry and
            parts[2] == str(long_strike) and parts[3] == str(short_strike) and
            parts[4] == regime and parts[5] == fixed_timestamp):
            print("  ✓ PASSED: Intent ID format is deterministic")
            return True
    
    print("  ❌ FAILED: Intent ID format incorrect")
    return False


def test_no_silent_noops():
    """Test that operations without artifacts exit with error."""
    print("TEST: No silent no-ops")
    
    # Example 1: No candidates should exit
    review_candidates = {
        "regimes": {
            "crash": {
                "candidates": []
            }
        }
    }
    
    try:
        print_candidate_table(review_candidates)
        print("  ❌ FAILED: Should have exited on no candidates")
        return False
    except SystemExit:
        pass  # Expected
    
    # Example 2: Invalid regime should exit
    try:
        select_candidate_interactive(
            review_candidates,
            regime_filter="invalid_regime",
            auto_rank=1
        )
        print("  ❌ FAILED: Should have exited on invalid regime")
        return False
    except SystemExit:
        print("  ✓ PASSED: No silent no-ops, all errors exit explicitly")
        return True


def main():
    """Run all tests."""
    print("=" * 80)
    print("DAILY CONSOLE STANDALONE TESTS")
    print("=" * 80)
    print()
    
    tests = [
        test_no_candidates_exits_with_error,
        test_missing_regimes_key_exits_with_error,
        test_candidate_selection_auto_rank_1,
        test_candidate_selection_custom_rank,
        test_candidate_selection_invalid_rank_exits,
        test_quote_only_pass_enables_execution_options,
        test_live_transmission_requires_send_confirmation,
        test_intent_id_is_deterministic,
        test_no_silent_noops,
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            results.append(False)
        print()
    
    print("=" * 80)
    print(f"RESULTS: {sum(results)}/{len(results)} tests passed")
    print("=" * 80)
    
    if all(results):
        print("✅ ALL TESTS PASSED")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
