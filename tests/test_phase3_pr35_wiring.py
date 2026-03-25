"""
Tests for Phase 3 PR3.5 - Wiring into Run Daily

Tests that ledger writing integration works correctly with regime orchestration.
"""

import tempfile
from pathlib import Path
from datetime import datetime, timezone

from forecast_arb.core.ledger import (
    create_regime_ledger_entry,
    write_regime_ledger_entry
)
from forecast_arb.core.regime_result import RegimeResult


def test_regime_ledger_entry_from_regime_result():
    """Test creating ledger entry from RegimeResult."""
    # Create a mock RegimeResult
    regime_result = RegimeResult(
        regime="crash",
        event_spec={
            "underlier": "SPY",
            "expiry": "20260320",
            "spot": 684.98,
            "moneyness": -0.15,
            "threshold": 582.23
        },
        event_hash="evt_crash_20260320_m015",
        p_implied=0.0651,
        p_implied_confidence=0.85,
        p_implied_warnings=[],
        candidates=[
            {
                "rank": 1,
                "candidate_id": "cand_crash_20260320_580_560",
                "debit_per_contract": 115.0,
                "max_loss_per_contract": 115.0
            }
        ],
        filtered_out=[],
        expiry_used="20260320",
        expiry_selection_reason="BEST_DTE_MATCH",
        representable=True,
        warnings=[],
        run_id="crash_venture_v2_abc123",
        manifest={}
    )
    
    # Extract data for ledger entry
    top_candidate = regime_result.get_top_candidate()
    
    # Create ledger entry
    entry = create_regime_ledger_entry(
        run_id=regime_result.run_id,
        regime=regime_result.regime,
        mode="CRASH_ONLY",
        decision="TRADE" if top_candidate else "NO_TRADE",
        reasons=["CANDIDATES_AVAILABLE"] if top_candidate else ["NO_CANDIDATES"],
        event_hash=regime_result.event_hash,
        expiry=regime_result.expiry_used,
        moneyness=regime_result.event_spec["moneyness"],
        spot=regime_result.event_spec["spot"],
        threshold=regime_result.event_spec["threshold"],
        p_implied=regime_result.p_implied,
        p_external=None,
        representable=regime_result.representable,
        candidate_id=top_candidate["candidate_id"] if top_candidate else None,
        debit=top_candidate["debit_per_contract"] if top_candidate else None,
        max_loss=top_candidate["max_loss_per_contract"] if top_candidate else None
    )
    
    # Verify entry structure
    assert entry["run_id"] == "crash_venture_v2_abc123"
    assert entry["regime"] == "crash"
    assert entry["decision"] == "TRADE"
    assert entry["event_hash"] == "evt_crash_20260320_m015"
    assert entry["candidate_id"] == "cand_crash_20260320_580_560"
    assert entry["debit"] == 115.0


def test_regime_ledger_entry_no_candidates():
    """Test creating ledger entry when no candidates available."""
    # Create RegimeResult with no candidates
    regime_result = RegimeResult(
        regime="selloff",
        event_spec={
            "underlier": "SPY",
            "expiry": "20260320",
            "spot": 684.98,
            "moneyness": -0.10,
            "threshold": 616.48
        },
        event_hash="evt_selloff_20260320_m010",
        p_implied=0.1234,
        p_implied_confidence=0.75,
        p_implied_warnings=[],
        candidates=[],  # No candidates
        filtered_out=[{"reason": "MIN_DEBIT_FILTER", "count": 5}],
        expiry_used="20260320",
        expiry_selection_reason="BEST_DTE_MATCH",
        representable=False,
        warnings=["NOT_REPRESENTABLE_ON_KALSHI"],
        run_id="crash_venture_v2_def456",
        manifest={}
    )
    
    # Create ledger entry for NO_TRADE
    entry = create_regime_ledger_entry(
        run_id=regime_result.run_id,
        regime=regime_result.regime,
        mode="SELLOFF_ONLY",
        decision="NO_TRADE",
        reasons=["NO_CANDIDATES_SURVIVED_FILTERS"],
        event_hash=regime_result.event_hash,
        expiry=regime_result.expiry_used,
        moneyness=regime_result.event_spec["moneyness"],
        spot=regime_result.event_spec["spot"],
        threshold=regime_result.event_spec["threshold"],
        p_implied=regime_result.p_implied,
        p_external=None,
        representable=regime_result.representable,
        candidate_id=None,
        debit=None,
        max_loss=None
    )
    
    # Verify NO_TRADE entry
    assert entry["decision"] == "NO_TRADE"
    assert entry["candidate_id"] is None
    assert entry["debit"] is None
    assert entry["representable"] is False


def test_multiple_regime_ledger_writes():
    """Test writing multiple regime ledger entries in one run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Simulate writing entries for both regimes
        regimes = ["crash", "selloff"]
        
        for regime in regimes:
            entry = create_regime_ledger_entry(
                run_id="crash_venture_v2_multi",
                regime=regime,
                mode="BOTH",
                decision="NO_TRADE",
                reasons=["SIMULATION_TEST"],
                event_hash=f"evt_{regime}_test",
                expiry="20260320",
                moneyness=-0.15 if regime == "crash" else -0.10,
                spot=684.98,
                threshold=582.23 if regime == "crash" else 616.48,
                p_implied=0.0651,
                p_external=None,
                representable=True
            )
            
            write_regime_ledger_entry(run_dir, entry, also_global=False)
        
        # Verify both entries were written
        ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
        assert ledger_path.exists()
        
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 2
        
        import json
        entry1 = json.loads(lines[0])
        entry2 = json.loads(lines[1])
        
        assert entry1["regime"] == "crash"
        assert entry2["regime"] == "selloff"


def test_ledger_entry_with_discretionary_override():
    """Test ledger entry with discretionary override context."""
    entry = create_regime_ledger_entry(
        run_id="crash_venture_v2_discretionary",
        regime="crash",
        mode="CRASH_ONLY",
        decision="TRADE",
        reasons=["DISCRETIONARY_OVERRIDE", "OPERATOR_JUDGMENT"],
        event_hash="evt_crash_20260320_m015",
        expiry="20260320",
        moneyness=-0.15,
        spot=684.98,
        threshold=582.23,
        p_implied=0.0651,
        p_external=0.0800,
        representable=True,
        candidate_id="cand_manual_selected",
        debit=120.0,
        max_loss=120.0
    )
    
    # Verify discretionary context is captured in reasons
    assert "DISCRETIONARY_OVERRIDE" in entry["reasons"]
    assert entry["candidate_id"] == "cand_manual_selected"


def test_ledger_orchestration_flow():
    """Test complete orchestration flow with ledger writing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "test_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        
        # Simulate orchestration results
        results_by_regime = {
            "crash": RegimeResult(
                regime="crash",
                event_spec={"spot": 684.98, "moneyness": -0.15, "threshold": 582.23},
                event_hash="evt_crash",
                p_implied=0.0651,
                p_implied_confidence=0.85,
                p_implied_warnings=[],
                candidates=[{"rank": 1, "candidate_id": "cand_1", "debit_per_contract": 115.0, "max_loss_per_contract": 115.0}],
                filtered_out=[],
                expiry_used="20260320",
                expiry_selection_reason="BEST_DTE",
                representable=True,
                warnings=[],
                run_id="test_run",
                manifest={}
            ),
            "selloff": RegimeResult(
                regime="selloff",
                event_spec={"spot": 684.98, "moneyness": -0.10, "threshold": 616.48},
                event_hash="evt_selloff",
                p_implied=0.1234,
                p_implied_confidence=0.75,
                p_implied_warnings=[],
                candidates=[],
                filtered_out=[],
                expiry_used="20260320",
                expiry_selection_reason="BEST_DTE",
                representable=False,
                warnings=[],
                run_id="test_run",
                manifest={}
            )
        }
        
        # Write ledger entries for each regime
        for regime_name, result in results_by_regime.items():
            top_cand = result.get_top_candidate()
            
            entry = create_regime_ledger_entry(
                run_id=result.run_id,
                regime=result.regime,
                mode="BOTH",
                decision="TRADE" if top_cand else "NO_TRADE",
                reasons=["HAS_CANDIDATES"] if top_cand else ["NO_CANDIDATES"],
                event_hash=result.event_hash,
                expiry=result.expiry_used,
                moneyness=result.event_spec["moneyness"],
                spot=result.event_spec["spot"],
                threshold=result.event_spec["threshold"],
                p_implied=result.p_implied,
                p_external=None,
                representable=result.representable,
                candidate_id=top_cand["candidate_id"] if top_cand else None,
                debit=top_cand["debit_per_contract"] if top_cand else None,
                max_loss=top_cand["max_loss_per_contract"] if top_cand else None
            )
            
            write_regime_ledger_entry(run_dir, entry, also_global=False)
        
        # Verify ledger written correctly
        ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
        assert ledger_path.exists()
        
        lines = ledger_path.read_text().strip().split("\n")
        assert len(lines) == 2
        
        import json
        entries = [json.loads(line) for line in lines]
        
        # Crash should have TRADE decision
        crash_entry = [e for e in entries if e["regime"] == "crash"][0]
        assert crash_entry["decision"] == "TRADE"
        assert crash_entry["candidate_id"] is not None
        
        # Selloff should have NO_TRADE decision
        selloff_entry = [e for e in entries if e["regime"] == "selloff"][0]
        assert selloff_entry["decision"] == "NO_TRADE"
        assert selloff_entry["candidate_id"] is None
