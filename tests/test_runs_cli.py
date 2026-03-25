"""
Tests for runs.py CLI.
"""

import json
import pytest
import subprocess
import sys
from pathlib import Path
import tempfile
import shutil

from forecast_arb.core.index import load_index, append_run, write_index
from forecast_arb.core.latest import set_latest_run


@pytest.fixture
def temp_runs_dir():
    """Create a temporary runs directory with sample data."""
    temp_dir = Path(tempfile.mkdtemp())
    
    # Create sample index with a few runs
    index = {
        "version": 1,
        "updated_at": "2026-01-29T12:00:00+00:00",
        "runs": [
            {
                "run_id": "test_003",
                "timestamp": "2026-01-29T12:30:00+00:00",
                "mode": "dev",
                "decision": "TRADE",
                "reason": "Good edge",
                "edge": 0.15,
                "num_tickets": 3,
                "submit_executed": False,
                "outdir": str(temp_dir / "test_003")
            },
            {
                "run_id": "test_002",
                "timestamp": "2026-01-29T12:20:00+00:00",
                "mode": "dev",
                "decision": "NO_TRADE",
                "reason": "No structures",
                "edge": None,
                "num_tickets": 0,
                "submit_executed": False,
                "outdir": str(temp_dir / "test_002")
            },
            {
                "run_id": "test_001",
                "timestamp": "2026-01-29T12:10:00+00:00",
                "mode": "prod",
                "decision": "TRADE",
                "reason": "Submitted",
                "edge": 0.20,
                "num_tickets": 2,
                "submit_executed": True,
                "outdir": str(temp_dir / "test_001")
            }
        ]
    }
    
    with open(temp_dir / "index.json", 'w') as f:
        json.dump(index, f)
    
    # Create latest pointer
    latest = {
        "run_dir": "test_003",
        "run_id": "test_003",
        "timestamp": "2026-01-29T12:30:00+00:00",
        "decision": "TRADE",
        "reason": "Good edge"
    }
    
    with open(temp_dir / "LATEST.json", 'w') as f:
        json.dump(latest, f)
    
    # Create a sample review file
    run_dir = temp_dir / "test_003"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    
    review_text = """
=== RUN DECISION ===
Decision: TRADE
Reason: Good edge
Run ID: test_003
===  END  ===
"""
    
    with open(artifacts_dir / "review.txt", 'w') as f:
        f.write(review_text)
    
    yield temp_dir
    shutil.rmtree(temp_dir)


def run_cli(*args):
    """Helper to run the CLI and capture output."""
    cmd = [sys.executable, "scripts/runs.py"] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent
    )
    return result


def test_cli_recent(temp_runs_dir):
    """Test recent command."""
    result = run_cli("--runs-root", str(temp_runs_dir), "recent", "--n", "3")
    
    assert result.returncode == 0
    # Check for key content (timestamps, decisions, reasons)
    assert "2026-01-29" in result.stdout
    assert "TRADE" in result.stdout
    assert "NO_TRADE" in result.stdout
    assert "Good edge" in result.stdout
    assert "No structures" in result.stdout


def test_cli_recent_default_count(temp_runs_dir):
    """Test recent command with default count."""
    result = run_cli("--runs-root", str(temp_runs_dir), "recent")
    
    assert result.returncode == 0
    # Should show all 3 runs (default is 10)
    # Count number of lines with timestamps (one per run)
    assert result.stdout.count("2026-01-29") == 3


def test_cli_latest(temp_runs_dir):
    """Test latest command."""
    result = run_cli("--runs-root", str(temp_runs_dir), "latest")
    
    assert result.returncode == 0
    assert "test_003" in result.stdout
    assert "TRADE" in result.stdout
    assert "Good edge" in result.stdout
    # Should include review text
    assert "RUN DECISION" in result.stdout


def test_cli_show_existing(temp_runs_dir):
    """Test show command with existing run."""
    result = run_cli("--runs-root", str(temp_runs_dir), "show", "test_002")
    
    assert result.returncode == 0
    assert "test_002" in result.stdout
    assert "NO_TRADE" in result.stdout
    assert "No structures" in result.stdout


def test_cli_show_nonexistent(temp_runs_dir):
    """Test show command with non-existent run."""
    result = run_cli("--runs-root", str(temp_runs_dir), "show", "nonexistent")
    
    assert result.returncode == 1
    assert "not found" in result.stdout.lower()


def test_cli_nonexistent_runs_root():
    """Test CLI with non-existent runs root."""
    result = run_cli("--runs-root", "/nonexistent/path", "recent")
    
    assert result.returncode == 1
    assert "not found" in result.stdout.lower()


def test_cli_no_command():
    """Test CLI with no command."""
    result = run_cli()
    
    # Should print help and have non-zero return code
    assert result.returncode == 1


def test_cli_output_format_recent(temp_runs_dir):
    """Test that recent output has expected format."""
    result = run_cli("--runs-root", str(temp_runs_dir), "recent")
    
    assert result.returncode == 0
    output = result.stdout
    
    # Should have headers
    assert "Timestamp" in output
    assert "Mode" in output
    assert "Decision" in output
    assert "Reason" in output
    assert "Edge" in output
    assert "Tickets" in output
    assert "Submit" in output
    
    # Should have separator lines
    assert "=" in output
    assert "-" in output
    
    # Should show total count
    assert "Total:" in output or "run(s)" in output


def test_cli_output_format_latest(temp_runs_dir):
    """Test that latest output has expected format."""
    result = run_cli("--runs-root", str(temp_runs_dir), "latest")
    
    assert result.returncode == 0
    output = result.stdout
    
    # Should have LATEST RUN header
    assert "LATEST RUN" in output
    
    # Should have key fields
    assert "Run ID:" in output
    assert "Directory:" in output
    assert "Decision:" in output
    assert "Reason:" in output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
