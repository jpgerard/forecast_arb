"""
Tests for Phase 3 PR3.1 - Regime Decision Ledger

Tests append-only JSONL logging for regime-level decisions.
"""

import json
import tempfile
from pathlib import Path

from forecast_arb.core.ledger import (
    append_jsonl,
    write_regime_ledger_entry,
    create_regime_ledger_entry
)


def test_append_jsonl():
    """Test basic JSONL append functionality."""
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = Path(tmpdir) / "test.jsonl"
        
        # Append two objects
        obj1 = {"ts": "2026-02-06T10:00:00Z", "value": 42}
        obj2 = {"ts": "2026-02-06T10:01:00Z", "value": 43}
        
        append_jsonl(jsonl_path, obj1)
        append_jsonl(jsonl_path, obj2)
        
        # Read and verify
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2
        
        parsed1 = json.loads(lines[0])
        parsed2 = json.loads(lines[1])
        
        assert parsed1["value"] == 42
        assert parsed2["value"] == 43


def test_create_regime_ledger_entry():
    """Test ledger entry creation with required fields."""
    entry = create_regime_ledger_entry(
        run_id="crash_venture_v2_abc123_20260206T100000",
        regime="crash",
        mode="CRASH_ONLY",
        decision="TRADE",
        reasons=["EDGE_SUFFICIENT", "HIGH_CONFIDENCE"],
        event_hash="evt_crash_20260320_m015",
        expiry="20260320",
        moneyness=-0.15,
        spot=684.98,
        threshold=582.23,
        p_implied=0.0651,
        p_external=0.0800,
        representable=True,
        candidate_id="cand_crash_20260320_580_560",
        debit=115.0,
        max_loss=115.0
    )
    
    # Verify required fields exist
    required_fields = [
        "ts_utc", "run_id", "regime", "mode", "decision", "reasons",
        "event_hash", "expiry", "moneyness", "spot", "threshold",
        "p_implied", "representable"
    ]
    
    for field in required_fields:
        assert field in entry, f"Missing required field: {field}"
    
    # Verify values
    assert entry["run_id"] == "crash_venture_v2_abc123_20260206T100000"
    assert entry["regime"] == "crash"
    assert entry["mode"] == "CRASH_ONLY"
    assert entry["decision"] == "TRADE"
    assert len(entry["reasons"]) == 2
    assert entry["moneyness"] == -0.15
    assert entry["representable"] is True
    assert entry["candidate_id"] == "cand_crash_20260320_580_560"


def test_create_regime_ledger_entry_no_trade():
    """Test ledger entry for NO_TRADE decision."""
    entry = create_regime_ledger_entry(
        run_id="crash_venture_v2_abc123_20260206T100000",
        regime="selloff",
        mode="SELLOFF_ONLY",
        decision="NO_TRADE",
        reasons=["NO_CANDIDATES_SURVIVED_FILTERS"],
        event_hash="evt_selloff_20260320_m010",
        expiry="20260320",
        moneyness=-0.10,
        spot=684.98,
        threshold=616.48,
        p_implied=0.1234,
        p_external=None,
        representable=False,
        candidate_id=None,
        debit=None,
        max_loss=None
    )
    
    assert entry["decision"] == "NO_TRADE"
    assert entry["candidate_id"] is None
    assert entry["debit"] is None
    assert entry["max_loss"] is None
    assert entry["representable"] is False


def test_write_regime_ledger_entry():
    """Test writing ledger entry to both per-run and global ledgers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "runs" / "crash_venture_v2" / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Change to tmpdir for global ledger
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            
            entry = create_regime_ledger_entry(
                run_id="test_run",
                regime="crash",
                mode="CRASH_ONLY",
                decision="TRADE",
                reasons=["TEST"],
                event_hash="evt_test",
                expiry="20260320",
                moneyness=-0.15,
                spot=684.98,
                threshold=582.23,
                p_implied=0.0651,
                p_external=0.0800,
                representable=True
            )
            
            write_regime_ledger_entry(
                run_dir=run_dir,
                entry=entry,
                also_global=True
            )
            
            # Verify per-run ledger
            run_ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
            assert run_ledger_path.exists()
            
            lines = run_ledger_path.read_text().strip().split("\n")
            assert len(lines) == 1
            parsed = json.loads(lines[0])
            assert parsed["regime"] == "crash"
            
            # Verify global ledger
            global_ledger_path = Path(tmpdir) / "runs" / "regime_ledger.jsonl"
            assert global_ledger_path.exists()
            
            lines = global_ledger_path.read_text().strip().split("\n")
            assert len(lines) == 1
            parsed = json.loads(lines[0])
            assert parsed["regime"] == "crash"
            
        finally:
            os.chdir(old_cwd)


def test_write_regime_ledger_entry_no_global():
    """Test writing ledger entry only to per-run ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "runs" / "crash_venture_v2" / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        entry = create_regime_ledger_entry(
            run_id="test_run",
            regime="crash",
            mode="CRASH_ONLY",
            decision="TRADE",
            reasons=["TEST"],
            event_hash="evt_test",
            expiry="20260320",
            moneyness=-0.15,
            spot=684.98,
            threshold=582.23,
            p_implied=0.0651,
            p_external=0.0800,
            representable=True
        )
        
        write_regime_ledger_entry(
            run_dir=run_dir,
            entry=entry,
            also_global=False
        )
        
        # Verify per-run ledger exists
        run_ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
        assert run_ledger_path.exists()
        
        # Verify global ledger does NOT exist
        global_ledger_path = Path(tmpdir) / "runs" / "regime_ledger.jsonl"
        assert not global_ledger_path.exists()


def test_ledger_entry_missing_required_field():
    """Test that missing required fields raise ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Create incomplete entry (missing 'regime')
        incomplete_entry = {
            "ts_utc": "2026-02-06T10:00:00Z",
            "run_id": "test_run",
            # "regime": "crash",  # MISSING
            "mode": "CRASH_ONLY",
            "decision": "TRADE",
            "reasons": ["TEST"],
            "event_hash": "evt_test",
            "expiry": "20260320",
            "moneyness": -0.15,
            "spot": 684.98,
            "threshold": 582.23,
            "p_implied": 0.0651,
            "representable": True
        }
        
        try:
            write_regime_ledger_entry(
                run_dir=run_dir,
                entry=incomplete_entry,
                also_global=False
            )
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "regime" in str(e).lower()
