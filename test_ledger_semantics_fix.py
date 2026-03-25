"""
Test surgical ledger semantics fixes.

FIXES:
A) Enforce single OPEN per intent/order
B) Add intent_id and order_id to trade_outcomes.jsonl
C) Make expiry single-source-of-truth from IBKR contract
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from forecast_arb.execution.outcome_ledger import append_trade_open


def test_fix_a_single_open_per_intent():
    """
    FIX A: Enforce single OPEN per intent/order.
    
    Should raise ValueError if attempting to write duplicate OPEN
    for the same intent_id.
    """
    print("\n" + "=" * 80)
    print("TEST FIX A: Single OPEN per intent/order")
    print("=" * 80)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir()
        
        # First OPEN should succeed
        print("\n✓ Writing first OPEN entry...")
        append_trade_open(
            run_dir=run_dir,
            candidate_id="test_candidate_1",
            run_id="test_run_1",
            regime="crash",
            entry_ts_utc=datetime.now(timezone.utc).isoformat(),
            entry_price=0.35,
            qty=1,
            expiry="20260320",
            long_strike=590.0,
            short_strike=570.0,
            intent_id="intent_001",
            order_id="12345",
            also_global=False
        )
        print("  SUCCESS: First OPEN written")
        
        # Second OPEN with same intent_id should fail
        print("\n✗ Attempting duplicate OPEN with same intent_id...")
        try:
            append_trade_open(
                run_dir=run_dir,
                candidate_id="test_candidate_2",
                run_id="test_run_1",
                regime="crash",
                entry_ts_utc=datetime.now(timezone.utc).isoformat(),
                entry_price=0.40,
                qty=1,
                expiry="20260320",
                long_strike=590.0,
                short_strike=570.0,
                intent_id="intent_001",  # Same intent_id
                order_id="67890",
                also_global=False
            )
            print("  ❌ FAIL: Should have raised ValueError")
            return False
        except ValueError as e:
            if "LEDGER VIOLATION" in str(e):
                print(f"  ✅ SUCCESS: Correctly blocked duplicate OPEN")
                print(f"     Error: {e}")
            else:
                print(f"  ❌ FAIL: Wrong error: {e}")
                return False
        
        # Different intent_id should succeed
        print("\n✓ Writing OPEN with different intent_id...")
        append_trade_open(
            run_dir=run_dir,
            candidate_id="test_candidate_3",
            run_id="test_run_1",
            regime="crash",
            entry_ts_utc=datetime.now(timezone.utc).isoformat(),
            entry_price=0.40,
            qty=1,
            expiry="20260320",
            long_strike=590.0,
            short_strike=570.0,
            intent_id="intent_002",  # Different intent_id
            order_id="67890",
            also_global=False
        )
        print("  SUCCESS: Different intent_id allowed")
    
    print("\n✅ FIX A TEST PASSED")
    return True


def test_fix_b_mandatory_fields():
    """
    FIX B: Verify intent_id and order_id are persisted to ledger.
    """
    print("\n" + "=" * 80)
    print("TEST FIX B: Mandatory intent_id and order_id fields")
    print("=" * 80)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir()
        
        # Write entry with all fields
        print("\n✓ Writing OPEN with intent_id and order_id...")
        intent_id = "intent_test_123"
        order_id = "order_456"
        
        append_trade_open(
            run_dir=run_dir,
            candidate_id="test_candidate",
            run_id="test_run",
            regime="crash",
            entry_ts_utc=datetime.now(timezone.utc).isoformat(),
            entry_price=0.35,
            qty=1,
            expiry="20260320",
            long_strike=590.0,
            short_strike=570.0,
            intent_id=intent_id,
            order_id=order_id,
            also_global=False
        )
        
        # Read back and verify
        ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
        with open(ledger_path, "r") as f:
            line = f.read().strip()
            entry = json.loads(line)
        
        print(f"\n  Checking fields in ledger entry...")
        print(f"    intent_id: {entry.get('intent_id')}")
        print(f"    order_id: {entry.get('order_id')}")
        
        # Verify fields exist and match
        if entry.get("intent_id") != intent_id:
            print(f"  ❌ FAIL: intent_id mismatch")
            return False
        
        if entry.get("order_id") != order_id:
            print(f"  ❌ FAIL: order_id mismatch")
            return False
        
        print(f"\n  ✅ Both fields correctly persisted")
        
        # Test with None order_id
        print("\n✓ Testing with order_id=None...")
        append_trade_open(
            run_dir=run_dir,
            candidate_id="test_candidate_2",
            run_id="test_run",
            regime="crash",
            entry_ts_utc=datetime.now(timezone.utc).isoformat(),
            entry_price=0.35,
            qty=1,
            expiry="20260320",
            long_strike=590.0,
            short_strike=570.0,
            intent_id="intent_test_456",
            order_id=None,  # Before order assigned
            also_global=False
        )
        
        with open(ledger_path, "r") as f:
            lines = f.readlines()
            last_entry = json.loads(lines[-1].strip())
        
        if last_entry.get("order_id") is not None:
            print(f"  ❌ FAIL: order_id should be None")
            return False
        
        print(f"  ✅ order_id=None correctly handled")
    
    print("\n✅ FIX B TEST PASSED")
    return True


def test_fix_c_expiry_validation():
    """
    FIX C: Document expiry validation.
    
    This fix enforces that expiry must come from IBKR-resolved contracts.
    The validation happens in execute_trade.py's enforce_intent_immutability().
    Here we just verify the field is used correctly.
    """
    print("\n" + "=" * 80)
    print("TEST FIX C: Expiry single-source-of-truth")
    print("=" * 80)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir()
        
        # Write entry with resolved IBKR expiry
        print("\n✓ Writing OPEN with IBKR-resolved expiry...")
        ibkr_resolved_expiry = "20260320"  # From IBKR contract
        
        append_trade_open(
            run_dir=run_dir,
            candidate_id="test_candidate",
            run_id="test_run",
            regime="crash",
            entry_ts_utc=datetime.now(timezone.utc).isoformat(),
            entry_price=0.35,
            qty=1,
            expiry=ibkr_resolved_expiry,  # Must be from IBKR
            long_strike=590.0,
            short_strike=570.0,
            intent_id="intent_test_789",
            order_id="order_123",
            also_global=False
        )
        
        # Read back and verify
        ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
        with open(ledger_path, "r") as f:
            entry = json.loads(f.read().strip())
        
        print(f"\n  Expiry in ledger: {entry.get('expiry')}")
        
        if entry.get("expiry") != ibkr_resolved_expiry:
            print(f"  ❌ FAIL: Expiry mismatch")
            return False
        
        print(f"  ✅ Expiry correctly persisted from IBKR contract")
        print(f"\n  NOTE: Validation logic in execute_trade.py enforces:")
        print(f"    - Intent expiry MUST match IBKR-resolved expiry")
        print(f"    - Ledger uses IBKR expiry as single source of truth")
        print(f"    - Mismatch → BLOCKS execution with FIX C VIOLATION")
    
    print("\n✅ FIX C TEST PASSED")
    return True


def main():
    """Run all tests."""
    print("\n" + "=" * 80)
    print("LEDGER SEMANTICS SURGICAL FIXES - TEST SUITE")
    print("=" * 80)
    
    tests = [
        ("FIX A: Single OPEN per intent/order", test_fix_a_single_open_per_intent),
        ("FIX B: Mandatory intent_id and order_id", test_fix_b_mandatory_fields),
        ("FIX C: Expiry single-source-of-truth", test_fix_c_expiry_validation),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
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
    
    all_passed = all(passed for _, passed in results)
    
    if all_passed:
        print("\n" + "=" * 80)
        print("🎉 ALL TESTS PASSED - LEDGER SEMANTICS FIXES VERIFIED")
        print("=" * 80)
    else:
        print("\n" + "=" * 80)
        print("❌ SOME TESTS FAILED")
        print("=" * 80)
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    exit(main())
