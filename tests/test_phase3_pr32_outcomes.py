"""
Tests for Phase 3 PR3.2 - Trade Outcome Ledger

Tests append-only logging for trade outcomes (open/close events).
"""

import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from forecast_arb.execution.outcome_ledger import (
    append_trade_open,
    append_trade_close,
    read_trade_outcomes
)


def test_append_trade_open():
    """Test basic trade OPEN event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        append_trade_open(
            run_dir=run_dir,
            candidate_id="cand_crash_20260320_580_560",
            run_id="crash_venture_v2_abc123",
            regime="crash",
            entry_ts_utc="2026-02-06T14:30:00Z",
            entry_price=0.40,
            qty=1,
            expiry="20260320",
            long_strike=580.0,
            short_strike=560.0,
            also_global=False
        )
        
        # Verify file exists
        ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
        assert ledger_path.exists()
        
        # Read and verify
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 1
        
        entry = json.loads(lines[0])
        assert entry["candidate_id"] == "cand_crash_20260320_580_560"
        assert entry["regime"] == "crash"
        assert entry["entry_price"] == 0.40
        assert entry["qty"] == 1
        assert entry["status"] == "OPEN"
        assert entry["exit_ts_utc"] is None
        assert entry["pnl"] is None


def test_append_trade_close():
    """Test basic trade CLOSE event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # First write OPEN
        append_trade_open(
            run_dir=run_dir,
            candidate_id="cand_crash_20260320_580_560",
            run_id="crash_venture_v2_abc123",
            regime="crash",
            entry_ts_utc="2026-02-06T14:30:00Z",
            entry_price=0.40,
            qty=1,
            expiry="20260320",
            long_strike=580.0,
            short_strike=560.0,
            also_global=False
        )
        
        # Then write CLOSE
        append_trade_close(
            run_dir=run_dir,
            candidate_id="cand_crash_20260320_580_560",
            exit_ts_utc="2026-02-07T10:00:00Z",
            exit_price=1.10,
            exit_reason="TAKE_PROFIT",
            pnl=70.0,
            mfe=80.0,
            mae=-10.0,
            also_global=False
        )
        
        # Verify file has 2 lines (append-only)
        ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 2
        
        # Verify OPEN event
        open_entry = json.loads(lines[0])
        assert open_entry["status"] == "OPEN"
        assert open_entry["entry_price"] == 0.40
        
        # Verify CLOSE event
        close_entry = json.loads(lines[1])
        assert close_entry["status"] == "CLOSED"
        assert close_entry["candidate_id"] == "cand_crash_20260320_580_560"
        assert close_entry["exit_price"] == 1.10
        assert close_entry["exit_reason"] == "TAKE_PROFIT"
        assert close_entry["pnl"] == 70.0


def test_append_trade_open_with_global():
    """Test that global ledger is written when also_global=True."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "runs" / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Change to tmpdir for global ledger
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            
            append_trade_open(
                run_dir=run_dir,
                candidate_id="cand_test",
                run_id="test_run",
                regime="crash",
                entry_ts_utc="2026-02-06T14:30:00Z",
                entry_price=0.40,
                qty=1,
                expiry="20260320",
                long_strike=580.0,
                short_strike=560.0,
                also_global=True
            )
            
            # Verify per-run ledger
            run_ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
            assert run_ledger_path.exists()
            
            # Verify global ledger
            global_ledger_path = Path(tmpdir) / "runs" / "trade_outcomes.jsonl"
            assert global_ledger_path.exists()
            
            # Both should have same content
            run_lines = run_ledger_path.read_text().strip().split("\n")
            global_lines = global_ledger_path.read_text().strip().split("\n")
            assert len(run_lines) == 1
            assert len(global_lines) == 1
            
            run_entry = json.loads(run_lines[0])
            global_entry = json.loads(global_lines[0])
            assert run_entry["candidate_id"] == global_entry["candidate_id"]
            
        finally:
            os.chdir(old_cwd)


def test_read_trade_outcomes():
    """Test reading and reconstructing trade outcomes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Write OPEN for trade 1
        append_trade_open(
            run_dir=run_dir,
            candidate_id="cand_1",
            run_id="test_run",
            regime="crash",
            entry_ts_utc="2026-02-06T14:30:00Z",
            entry_price=0.40,
            qty=1,
            expiry="20260320",
            long_strike=580.0,
            short_strike=560.0,
            also_global=False
        )
        
        # Write OPEN for trade 2
        append_trade_open(
            run_dir=run_dir,
            candidate_id="cand_2",
            run_id="test_run",
            regime="selloff",
            entry_ts_utc="2026-02-06T14:35:00Z",
            entry_price=0.50,
            qty=2,
            expiry="20260320",
            long_strike=620.0,
            short_strike=610.0,
            also_global=False
        )
        
        # Write CLOSE for trade 1
        append_trade_close(
            run_dir=run_dir,
            candidate_id="cand_1",
            exit_ts_utc="2026-02-07T10:00:00Z",
            exit_price=1.10,
            exit_reason="TAKE_PROFIT",
            pnl=70.0,
            also_global=False
        )
        
        # Read and reconstruct
        ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
        trades = read_trade_outcomes(ledger_path)
        
        # Verify we have 2 trades
        assert len(trades) == 2
        
        # Verify trade 1 (closed)
        assert "cand_1" in trades
        trade1 = trades["cand_1"]
        assert trade1["status"] == "CLOSED"
        assert trade1["entry_price"] == 0.40
        assert trade1["exit_price"] == 1.10
        assert trade1["pnl"] == 70.0
        
        # Verify trade 2 (still open)
        assert "cand_2" in trades
        trade2 = trades["cand_2"]
        assert trade2["status"] == "OPEN"
        assert trade2["entry_price"] == 0.50
        assert trade2["exit_price"] is None


def test_read_trade_outcomes_empty_file():
    """Test reading from non-existent ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "nonexistent.jsonl"
        
        trades = read_trade_outcomes(ledger_path)
        assert trades == {}


def test_multiple_trades_same_ledger():
    """Test multiple trades written to same ledger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Write 3 trades
        for i in range(1, 4):
            append_trade_open(
                run_dir=run_dir,
                candidate_id=f"cand_{i}",
                run_id="test_run",
                regime="crash",
                entry_ts_utc=f"2026-02-06T14:{30+i}:00Z",
                entry_price=0.40 + (i * 0.1),
                qty=i,
                expiry="20260320",
                long_strike=580.0,
                short_strike=560.0,
                also_global=False
            )
        
        # Verify ledger has 3 entries
        ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 3
        
        # Read and verify
        trades = read_trade_outcomes(ledger_path)
        assert len(trades) == 3
        
        assert trades["cand_1"]["qty"] == 1
        assert trades["cand_2"]["qty"] == 2
        assert trades["cand_3"]["qty"] == 3
