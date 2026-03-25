"""
Tests for Phase 3 Integration

Tests that ledger writing works with regime orchestration.
"""

import tempfile
from pathlib import Path
import json

from forecast_arb.core.regime_result import RegimeResult
from forecast_arb.core.regime_orchestration import write_regime_ledgers


def test_write_regime_ledgers_integration():
    """Test that write_regime_ledgers creates proper ledger entries."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Create mock regime results
        results_by_regime = {
            "crash": RegimeResult(
                regime="crash",
                event_spec={
                    "spot": 684.98,
                    "moneyness": -0.15,
                    "threshold": 582.23
                },
                event_hash="evt_crash_20260320",
                p_implied=0.0651,
                p_implied_confidence=0.85,
                p_implied_warnings=[],
                candidates=[
                    {
                        "rank": 1,
                        "candidate_id": "cand_crash_1",
                        "debit_per_contract": 115.0,
                        "max_loss_per_contract": 115.0
                    }
                ],
                filtered_out=[],
                expiry_used="20260320",
                expiry_selection_reason="BEST_DTE_MATCH",
                representable=True,
                warnings=[],
                run_id="test_run_123",
                manifest={}
            ),
            "selloff": RegimeResult(
                regime="selloff",
                event_spec={
                    "spot": 684.98,
                    "moneyness": -0.10,
                    "threshold": 616.48
                },
                event_hash="evt_selloff_20260320",
                p_implied=0.1234,
                p_implied_confidence=0.75,
                p_implied_warnings=[],
                candidates=[],  # No candidates
                filtered_out=[{"reason": "MIN_DEBIT_FILTER"}],
                expiry_used="20260320",
                expiry_selection_reason="BEST_DTE_MATCH",
                representable=False,
                warnings=["NOT_REPRESENTABLE"],
                run_id="test_run_123",
                manifest={}
            )
        }
        
        # Write ledgers
        write_regime_ledgers(
            results_by_regime=results_by_regime,
            regime_mode="BOTH",
            p_external_value=0.08,
            run_dir=run_dir
        )
        
        # Verify ledger was created
        ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
        assert ledger_path.exists(), "Ledger file not created"
        
        # Read and verify entries
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 2, f"Expected 2 ledger entries, got {len(lines)}"
        
        # Parse entries
        entries = [json.loads(line) for line in lines]
        
        # Find crash and selloff entries
        crash_entry = next(e for e in entries if e["regime"] == "crash")
        selloff_entry = next(e for e in entries if e["regime"] == "selloff")
        
        # Verify crash entry (has candidates)
        assert crash_entry["decision"] == "TRADE"
        assert crash_entry["candidate_id"] == "cand_crash_1"
        assert crash_entry["debit"] == 115.0
        assert crash_entry["representable"] is True
        assert crash_entry["mode"] == "BOTH"
        assert crash_entry["p_external"] == 0.08
        assert "CANDIDATES_AVAILABLE" in crash_entry["reasons"]
        
        # Verify selloff entry (no candidates)
        assert selloff_entry["decision"] == "NO_TRADE"
        assert selloff_entry["candidate_id"] is None
        assert selloff_entry["debit"] is None
        assert selloff_entry["representable"] is False
        assert "NO_CANDIDATES_SURVIVED_FILTERS" in selloff_entry["reasons"]
        assert "NOT_REPRESENTABLE" in selloff_entry["reasons"]


def test_write_regime_ledgers_global_ledger():
    """Test that global ledger is also written."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            
            run_dir = Path(tmpdir) / "runs" / "test_run"
            run_dir.mkdir(parents=True, exist_ok=True)
            
            results_by_regime = {
                "crash": RegimeResult(
                    regime="crash",
                    event_spec={"spot": 684.98, "moneyness": -0.15, "threshold": 582.23},
                    event_hash="evt_crash",
                    p_implied=0.0651,
                    p_implied_confidence=0.85,
                    p_implied_warnings=[],
                    candidates=[
                        {"rank": 1, "candidate_id": "cand_1", "debit_per_contract": 115.0, "max_loss_per_contract": 115.0}
                    ],
                    filtered_out=[],
                    expiry_used="20260320",
                    expiry_selection_reason="BEST_DTE",
                    representable=True,
                    warnings=[],
                    run_id="test_run",
                    manifest={}
                )
            }
            
            write_regime_ledgers(
                results_by_regime=results_by_regime,
                regime_mode="CRASH_ONLY",
                p_external_value=0.08,
                run_dir=run_dir
            )
            
            # Verify global ledger exists
            global_ledger = Path(tmpdir) / "runs" / "regime_ledger.jsonl"
            assert global_ledger.exists(), "Global ledger not created"
            
            # Verify entry
            lines = global_ledger.read_text().strip().split("\n")
            assert len(lines) == 1
            
            entry = json.loads(lines[0])
            assert entry["regime"] == "crash"
            assert entry["decision"] == "TRADE"
            
        finally:
            os.chdir(old_cwd)


def test_write_regime_ledgers_no_p_external():
    """Test ledger writing when p_external is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        results_by_regime = {
            "crash": RegimeResult(
                regime="crash",
                event_spec={"spot": 684.98, "moneyness": -0.15, "threshold": 582.23},
                event_hash="evt_crash",
                p_implied=0.0651,
                p_implied_confidence=0.85,
                p_implied_warnings=[],
                candidates=[],
                filtered_out=[],
                expiry_used="20260320",
                expiry_selection_reason="BEST_DTE",
                representable=True,
                warnings=[],
                run_id="test_run",
                manifest={}
            )
        }
        
        # Write with no p_external
        write_regime_ledgers(
            results_by_regime=results_by_regime,
            regime_mode="CRASH_ONLY",
            p_external_value=None,
            run_dir=run_dir
        )
        
        # Verify entry
        ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
        lines = ledger_path.read_text().strip().split("\n")
        entry = json.loads(lines[0])
        
        assert entry["p_external"] is None
        assert entry["p_implied"] == 0.0651
