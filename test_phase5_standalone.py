"""
Phase 5: Standalone Test Runner

Runs Phase 5 multi-underlier integration test for manual verification.
"""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

if __name__ == "__main__":
    import pytest
    
    print("=" * 80)
    print("PHASE 5: KALSHI + QQQ VERIFICATION - STANDALONE TEST")
    print("=" * 80)
    print()
    
    # Run the test with verbose output
    exit_code = pytest.main([
        "tests/test_phase5_multi_underlier.py",
        "-v",
        "-s",
        "--tb=short"
    ])
    
    print()
    print("=" * 80)
    if exit_code == 0:
        print("✅ ALL TESTS PASSED")
    else:
        print("❌ TESTS FAILED")
    print("=" * 80)
    
    sys.exit(exit_code)
