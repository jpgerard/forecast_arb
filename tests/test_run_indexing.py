"""
Tests for run indexing system (latest pointer, index, summary extraction).
"""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import shutil

from forecast_arb.core.latest import set_latest_run, get_latest_run
from forecast_arb.core.index import load_index, append_run, write_index, find_run_by_id, get_recent_runs
from forecast_arb.core.run_summary import extract_summary, extract_summary_safe


@pytest.fixture
def temp_runs_dir():
    """Create a temporary runs directory."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def sample_run_dir(temp_runs_dir):
    """Create a sample run directory with minimal artifacts."""
    run_id = "test_run_001_20260129T120000"
    run_dir = temp_runs_dir / "daily" / run_id
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    
    # Create manifest.json
    manifest = {
        "run_id": run_id,
        "run_time_utc": "2026-01-29T12:00:00+00:00",
        "mode": "dev",
        "inputs": {
            "p_event": 0.35
        }
    }
    with open(run_dir / "manifest.json", 'w') as f:
        json.dump(manifest, f)
    
    # Create final_decision.json
    final_decision = {
        "run_id": run_id,
        "decision": "TRADE",
        "reason": "Structures generated",
        "submit_requested": False,
        "submit_executed": False,
        "mode": "dev",
        "timestamp_utc": "2026-01-29T12:05:00+00:00"
    }
    with open(artifacts_dir / "final_decision.json", 'w') as f:
        json.dump(final_decision, f)
    
    # Create tickets.json
    tickets = [{"ticket_id": 1}, {"ticket_id": 2}]
    with open(artifacts_dir / "tickets.json", 'w') as f:
        json.dump(tickets, f)
    
    return run_dir


def test_latest_pointer_read_write(temp_runs_dir, sample_run_dir):
    """Test latest run pointer read/write."""
    # Set latest pointer
    set_latest_run(
        runs_root=temp_runs_dir,
        run_dir=sample_run_dir,
        decision="TRADE",
        reason="Test run",
        run_id=sample_run_dir.name
    )
    
    # Check that LATEST.json was created
    latest_path = temp_runs_dir / "LATEST.json"
    assert latest_path.exists()
    
    # Read back the pointer
    latest = get_latest_run(temp_runs_dir)
    assert latest is not None
    assert latest["run_id"] == sample_run_dir.name
    assert latest["decision"] == "TRADE"
    assert latest["reason"] == "Test run"
    assert "run_dir" in latest
    assert "run_dir_abs" in latest


def test_latest_pointer_nonexistent(temp_runs_dir):
    """Test reading latest pointer when it doesn't exist."""
    latest = get_latest_run(temp_runs_dir)
    assert latest is None


def test_index_load_empty(temp_runs_dir):
    """Test loading index when it doesn't exist."""
    index = load_index(temp_runs_dir)
    assert index is not None
    assert index["version"] == 1
    assert index["runs"] == []


def test_index_append_and_write(temp_runs_dir):
    """Test appending to index and writing."""
    index = load_index(temp_runs_dir)
    
    # Append a run summary
    summary = {
        "run_id": "test_001",
        "timestamp": "2026-01-29T12:00:00+00:00",
        "decision": "TRADE",
        "mode": "dev"
    }
    
    index = append_run(index, summary)
    assert len(index["runs"]) == 1
    assert index["runs"][0]["run_id"] == "test_001"
    
    # Write index
    write_index(temp_runs_dir, index)
    
    # Read back
    index2 = load_index(temp_runs_dir)
    assert len(index2["runs"]) == 1
    assert index2["runs"][0]["run_id"] == "test_001"


def test_index_append_truncation(temp_runs_dir):
    """Test that index truncates when exceeding max_entries."""
    index = load_index(temp_runs_dir)
    
    # Add more than max_entries
    for i in range(15):
        summary = {
            "run_id": f"test_{i:03d}",
            "timestamp": f"2026-01-29T12:{i:02d}:00+00:00"
        }
        index = append_run(index, summary, max_entries=10)
    
    # Should keep only 10 most recent
    assert len(index["runs"]) == 10
    
    # Most recent should be first
    assert index["runs"][0]["run_id"] == "test_014"
    assert index["runs"][-1]["run_id"] == "test_005"


def test_index_find_by_id(temp_runs_dir):
    """Test finding run by ID in index."""
    index = load_index(temp_runs_dir)
    
    # Add some runs
    for i in range(5):
        summary = {
            "run_id": f"test_{i:03d}",
            "decision": "TRADE"
        }
        index = append_run(index, summary)
    
    # Find existing run
    run = find_run_by_id(index, "test_002")
    assert run is not None
    assert run["run_id"] == "test_002"
    
    # Try non-existent run
    run = find_run_by_id(index, "nonexistent")
    assert run is None


def test_index_get_recent(temp_runs_dir):
    """Test getting recent runs from index."""
    index = load_index(temp_runs_dir)
    
    # Add some runs
    for i in range(20):
        summary = {
            "run_id": f"test_{i:03d}",
            "timestamp": f"2026-01-29T12:{i:02d}:00+00:00"
        }
        index = append_run(index, summary)
    
    # Get recent 5
    recent = get_recent_runs(index, n=5)
    assert len(recent) == 5
    assert recent[0]["run_id"] == "test_019"  # Most recent first
    assert recent[4]["run_id"] == "test_015"


def test_extract_summary_complete(sample_run_dir):
    """Test extracting summary from complete run."""
    summary = extract_summary(sample_run_dir)
    
    assert summary["run_id"] == sample_run_dir.name
    assert summary["decision"] == "TRADE"
    assert summary["reason"] == "Structures generated"
    assert summary["mode"] == "dev"
    assert summary["p_external"] == 0.35
    assert summary["num_tickets"] == 2
    assert summary["submit_requested"] is False
    assert summary["submit_executed"] is False


def test_extract_summary_incomplete(temp_runs_dir):
    """Test extracting summary from incomplete run."""
    # Create run with only manifest
    run_id = "incomplete_run"
    run_dir = temp_runs_dir / run_id
    run_dir.mkdir(parents=True)
    
    manifest = {
        "run_id": run_id,
        "run_time_utc": "2026-01-29T12:00:00+00:00",
        "mode": "dev"
    }
    with open(run_dir / "manifest.json", 'w') as f:
        json.dump(manifest, f)
    
    summary = extract_summary(run_dir)
    
    # Should have defaults for missing data
    assert summary["run_id"] == run_id
    assert summary["decision"] == "UNKNOWN"
    assert summary["reason"] == "INCOMPLETE_RUN"
    assert summary["num_tickets"] == 0


def test_extract_summary_safe_exception(temp_runs_dir):
    """Test that extract_summary_safe handles exceptions."""
    # Non-existent directory
    run_dir = temp_runs_dir / "nonexistent"
    
    summary = extract_summary_safe(run_dir)
    
    # Should return minimal summary (may be INCOMPLETE_RUN or EXCEPTION depending on error)
    assert summary["run_id"] == "nonexistent"
    assert summary["decision"] in ["NO_TRADE", "UNKNOWN"]
    assert summary["num_tickets"] == 0
    # Reason should indicate problem (either INCOMPLETE or EXCEPTION)
    assert any(keyword in summary["reason"] for keyword in ["EXCEPTION", "INCOMPLETE"])


def test_index_deterministic_ordering(temp_runs_dir):
    """Test that index maintains deterministic ordering."""
    index = load_index(temp_runs_dir)
    
    # Add runs in specific order
    for i in [3, 1, 4, 1, 5, 9, 2, 6]:
        summary = {
            "run_id": f"test_{i}",
            "timestamp": f"2026-01-29T12:00:{i:02d}+00:00"
        }
        index = append_run(index, summary)
    
    # Most recent should be first (last appended)
    assert index["runs"][0]["run_id"] == "test_6"
    assert index["runs"][1]["run_id"] == "test_2"
    assert index["runs"][-1]["run_id"] == "test_3"


def test_windows_path_handling(temp_runs_dir, sample_run_dir):
    """Test that Windows paths are handled correctly (forward slashes in JSON)."""
    set_latest_run(
        runs_root=temp_runs_dir,
        run_dir=sample_run_dir,
        decision="TRADE",
        reason="Test",
        run_id=sample_run_dir.name
    )
    
    # Read raw JSON and check path format
    latest_path = temp_runs_dir / "LATEST.json"
    with open(latest_path, 'r') as f:
        data = json.load(f)
    
    # Should use forward slashes for cross-platform consistency
    assert "/" in data["run_dir"] or "\\" not in data["run_dir"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
