"""
Tests for Phase 3 PR3.3 - Decision Quality Score (DQS)

Tests DQS entry creation, validation, and storage.
"""

import json
import tempfile
from pathlib import Path

from forecast_arb.core.dqs import (
    create_dqs_entry,
    append_dqs_entry,
    read_dqs_entries,
    compute_dqs_summary
)


def test_create_dqs_entry():
    """Test basic DQS entry creation."""
    entry = create_dqs_entry(
        candidate_id="cand_crash_20260320_580_560",
        run_id="crash_venture_v2_abc123",
        regime="crash",
        dqs_total=8,
        breakdown={
            "regime": 2,
            "pricing": 2,
            "structure": 2,
            "execution": 1,
            "governance": 1
        },
        notes="Good edge, execution could improve"
    )
    
    # Verify required fields
    assert entry["candidate_id"] == "cand_crash_20260320_580_560"
    assert entry["run_id"] == "crash_venture_v2_abc123"
    assert entry["regime"] == "crash"
    assert entry["dqs_total"] == 8
    assert entry["breakdown"]["regime"] == 2
    assert entry["breakdown"]["pricing"] == 2
    assert entry["notes"] == "Good edge, execution could improve"
    assert "ts_utc" in entry


def test_create_dqs_entry_missing_dimension():
    """Test that missing dimensions raise ValueError."""
    try:
        entry = create_dqs_entry(
            candidate_id="cand_test",
            run_id="test_run",
            regime="crash",
            dqs_total=8,
            breakdown={
                "regime": 2,
                "pricing": 2,
                # Missing: structure, execution, governance
            },
            notes=""
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "structure" in str(e).lower() or "dimension" in str(e).lower()


def test_append_dqs_entry():
    """Test appending DQS entry to ledgers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        entry = create_dqs_entry(
            candidate_id="cand_test",
            run_id="test_run",
            regime="crash",
            dqs_total=7,
            breakdown={
                "regime": 2,
                "pricing": 1,
                "structure": 2,
                "execution": 1,
                "governance": 1
            },
            notes="Test entry"
        )
        
        # Write without global
        append_dqs_entry(
            run_dir=run_dir,
            entry=entry,
            also_global=False
        )
        
        # Verify per-run ledger
        ledger_path = run_dir / "artifacts" / "dqs.jsonl"
        assert ledger_path.exists()
        
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 1
        
        parsed = json.loads(lines[0])
        assert parsed["dqs_total"] == 7


def test_append_dqs_entry_with_global():
    """Test that global ledger is written."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "runs" / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            
            entry = create_dqs_entry(
                candidate_id="cand_test",
                run_id="test_run",
                regime="crash",
                dqs_total=7,
                breakdown={
                    "regime": 2,
                    "pricing": 1,
                    "structure": 2,
                    "execution": 1,
                    "governance": 1
                }
            )
            
            append_dqs_entry(
                run_dir=run_dir,
                entry=entry,
                also_global=True
            )
            
            # Verify both ledgers
            run_ledger = run_dir / "artifacts" / "dqs.jsonl"
            global_ledger = Path(tmpdir) / "runs" / "dqs.jsonl"
            
            assert run_ledger.exists()
            assert global_ledger.exists()
            
        finally:
            os.chdir(old_cwd)


def test_read_dqs_entries():
    """Test reading DQS entries from ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Write multiple entries
        for i in range(3):
            entry = create_dqs_entry(
                candidate_id=f"cand_{i}",
                run_id="test_run",
                regime="crash",
                dqs_total=6 + i,
                breakdown={
                    "regime": 1,
                    "pricing": 1,
                    "structure": 2,
                    "execution": 1,
                    "governance": 1 + i
                }
            )
            append_dqs_entry(run_dir, entry, also_global=False)
        
        # Read entries
        ledger_path = run_dir / "artifacts" / "dqs.jsonl"
        entries = read_dqs_entries(ledger_path)
        
        assert len(entries) == 3
        assert entries[0]["dqs_total"] == 6
        assert entries[1]["dqs_total"] == 7
        assert entries[2]["dqs_total"] == 8


def test_read_dqs_entries_empty():
    """Test reading from non-existent ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "nonexistent.jsonl"
        entries = read_dqs_entries(ledger_path)
        assert entries == []


def test_compute_dqs_summary():
    """Test DQS summary statistics."""
    entries = [
        {"candidate_id": "c1", "regime": "crash", "dqs_total": 8},
        {"candidate_id": "c2", "regime": "crash", "dqs_total": 6},
        {"candidate_id": "c3", "regime": "selloff", "dqs_total": 9},
        {"candidate_id": "c4", "regime": "selloff", "dqs_total": 7},
    ]
    
    summary = compute_dqs_summary(entries)
    
    assert summary["count"] == 4
    assert summary["avg_total"] == 7.5
    assert summary["min_total"] == 6
    assert summary["max_total"] == 9
    
    # By regime
    assert summary["by_regime"]["crash"]["count"] == 2
    assert summary["by_regime"]["crash"]["avg"] == 7.0
    assert summary["by_regime"]["selloff"]["count"] == 2
    assert summary["by_regime"]["selloff"]["avg"] == 8.0


def test_compute_dqs_summary_empty():
    """Test DQS summary with no entries."""
    summary = compute_dqs_summary([])
    
    assert summary["count"] == 0
    assert summary["avg_total"] is None
    assert summary["min_total"] is None
    assert summary["max_total"] is None
    assert summary["by_regime"] == {}


def test_dqs_entry_missing_required_field():
    """Test that missing required fields raise ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Create incomplete entry
        incomplete_entry = {
            "ts_utc": "2026-02-06T10:00:00Z",
            "candidate_id": "cand_test",
            # Missing: run_id, regime, dqs_total, breakdown
        }
        
        try:
            append_dqs_entry(run_dir, incomplete_entry, also_global=False)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "required field" in str(e).lower()
