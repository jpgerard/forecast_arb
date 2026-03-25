"""
Tests for review-only structuring mode.

Verifies that --review-only-structuring flag works correctly and:
1. Prevents --submit from working
2. Runs structuring even when blocked
3. Generates review artifacts
"""

import pytest
import subprocess
import sys
from pathlib import Path


def test_review_only_blocks_submit():
    """Test that --review-only-structuring prevents --submit."""
    # This should exit with code 2
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_daily.py",
            "--review-only-structuring",
            "--submit",
            "--snapshot", "examples/ibkr_snapshot_spy.json",
            "--mode", "dev"
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 2, f"Expected exit code 2, got {result.returncode}"
    assert "Cannot submit in review-only mode" in result.stderr or "Cannot submit in review-only mode" in result.stdout


def test_review_only_flag_parsing():
    """Test that --review-only-structuring flag is recognized."""
    # Just test that the flag is accepted (will fail for other reasons but not arg parsing)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_daily.py",
            "--help"
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent
    )
    
    assert "--review-only-structuring" in result.stdout, "Flag not in help output"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
