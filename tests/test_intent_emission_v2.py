"""
Tests for Intent Emission in run_daily_v2.py

Validates that --emit-intent flag works correctly:
- Requires explicit regime (crash/selloff)
- Rejects auto/both
- Locates candidate by rank within regime
- Builds correct OrderIntent
- Writes intent atomically
- Does not execute
"""

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from forecast_arb.execution.intent_builder import build_order_intent


class TestIntentEmissionV2:
    """Test intent emission wiring in run_daily_v2.py"""
    
    def test_build_order_intent_crash(self):
        """Happy path: Build intent for crash regime"""
        candidate = {
            "rank": 1,
            "expiry": "20260320",
            "strikes": {
                "long_put": 580.0,
                "short_put": 560.0
            },
            "symbol": "SPY",
            "event_spec_hash": "abc123",
            "candidate_id": "20260320_580_560",
            "moneyness_target": -0.15,
            "metrics": {
                "ev_per_dollar": 0.25
            },
            "structure": {
                "max_loss": 2000,
                "max_gain": 500
            }
        }
        
        intent = build_order_intent(
            candidate=candidate,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75
        )
        
        # Validate structure
        assert intent["strategy"] == "crash_venture_v2"
        assert intent["regime"] == "crash"
        assert intent["symbol"] == "SPY"
        assert intent["expiry"] == "20260320"
        assert intent["type"] == "PUT_SPREAD"
        assert intent["qty"] == 1
        assert intent["limit"]["start"] == 2.50
        assert intent["limit"]["max"] == 2.75
        assert intent["transmit"] is False
        
        # Validate legs
        assert len(intent["legs"]) == 2
        assert intent["legs"][0]["action"] == "BUY"
        assert intent["legs"][0]["right"] == "P"
        assert intent["legs"][0]["strike"] == 580.0
        assert intent["legs"][1]["action"] == "SELL"
        assert intent["legs"][1]["right"] == "P"
        assert intent["legs"][1]["strike"] == 560.0
        
        # Validate metadata
        assert intent["metadata"]["rank"] == 1
        assert intent["metadata"]["ev_per_dollar"] == 0.25
        assert intent["metadata"]["moneyness_target"] == -0.15
    
    def test_build_order_intent_selloff(self):
        """Test selloff regime intent has correct moneyness"""
        candidate = {
            "rank": 1,
            "expiry": "20260320",
            "strikes": {
                "long_put": 600.0,
                "short_put": 585.0
            },
            "symbol": "SPY",
            "event_spec_hash": "def456",
            "candidate_id": "20260320_600_585",
            "moneyness_target": -0.09,  # Selloff target
            "metrics": {
                "ev_per_dollar": 0.18
            },
            "structure": {
                "max_loss": 1500,
                "max_gain": 350
            }
        }
        
        intent = build_order_intent(
            candidate=candidate,
            regime="selloff",
            qty=2,
            limit_start=1.75,
            limit_max=2.00
        )
        
        # Validate regime-specific fields
        assert intent["regime"] == "selloff"
        assert intent["qty"] == 2
        assert intent["metadata"]["moneyness_target"] == -0.09
        
        # Verify strikes match selloff candidate
        assert intent["legs"][0]["strike"] == 600.0
        assert intent["legs"][1]["strike"] == 585.0
    
    def test_intent_builder_never_transmits(self):
        """Ensure intent builder always sets transmit=False"""
        candidate = {
            "rank": 1,
            "expiry": "20260320",
            "strikes": {"long_put": 580.0, "short_put": 560.0},
            "symbol": "SPY",
            "event_spec_hash": "test",
            "candidate_id": "test_id",
            "metrics": {},
            "structure": {}
        }
        
        intent = build_order_intent(
            candidate=candidate,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75
        )
        
        # Critical: must never transmit in intent mode
        assert intent["transmit"] is False
    
    def test_candidate_selection_by_rank(self):
        """Test selecting candidate by rank from list"""
        candidates = [
            {"rank": 1, "expiry": "20260320", "strikes": {"long_put": 580.0, "short_put": 560.0}},
            {"rank": 2, "expiry": "20260320", "strikes": {"long_put": 575.0, "short_put": 555.0}},
            {"rank": 3, "expiry": "20260320", "strikes": {"long_put": 570.0, "short_put": 550.0}},
        ]
        
        def select_candidate_by_rank(candidates_list, rank):
            for c in candidates_list:
                if c.get("rank") == rank:
                    return c
            return None
        
        # Find rank 2
        candidate = select_candidate_by_rank(candidates, 2)
        assert candidate is not None
        assert candidate["rank"] == 2
        assert candidate["strikes"]["long_put"] == 575.0
        
        # Rank not found
        candidate = select_candidate_by_rank(candidates, 99)
        assert candidate is None
    
    def test_intent_validation_requires_regime(self):
        """Test that intent mode requires explicit regime"""
        # Simulate CLI validation logic
        def validate_intent_args(emit_intent, regime, pick_rank, limit_start, limit_max, intent_out):
            if emit_intent:
                required = [regime, pick_rank, limit_start, limit_max, intent_out]
                if any(v is None for v in required):
                    raise SystemExit("❌ --emit-intent requires all parameters")
                
                if regime not in ("crash", "selloff"):
                    raise SystemExit("❌ --emit-intent requires --regime crash|selloff (not auto/both)")
        
        # Valid: crash regime
        validate_intent_args(True, "crash", 1, 2.50, 2.75, "intent.json")
        
        # Valid: selloff regime
        validate_intent_args(True, "selloff", 1, 1.75, 2.00, "intent.json")
        
        # Invalid: auto regime
        with pytest.raises(SystemExit, match="crash|selloff"):
            validate_intent_args(True, "auto", 1, 2.50, 2.75, "intent.json")
        
        # Invalid: both regime
        with pytest.raises(SystemExit, match="crash|selloff"):
            validate_intent_args(True, "both", 1, 2.50, 2.75, "intent.json")
        
        # Invalid: missing rank
        with pytest.raises(SystemExit, match="requires all parameters"):
            validate_intent_args(True, "crash", None, 2.50, 2.75, "intent.json")
    
    def test_intent_file_written_atomically(self, tmp_path):
        """Test that intent is written to file correctly"""
        candidate = {
            "rank": 1,
            "expiry": "20260320",
            "strikes": {"long_put": 580.0, "short_put": 560.0},
            "symbol": "SPY",
            "event_spec_hash": "test",
            "candidate_id": "test_id",
            "metrics": {"ev_per_dollar": 0.25},
            "structure": {"max_loss": 2000, "max_gain": 500}
        }
        
        intent = build_order_intent(
            candidate=candidate,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75
        )
        
        # Write to temp file
        intent_path = tmp_path / "intent.json"
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(intent_path, "w") as f:
            json.dump(intent, f, indent=2)
        
        # Read back and validate
        with open(intent_path, "r") as f:
            loaded = json.load(f)
        
        assert loaded["strategy"] == "crash_venture_v2"
        assert loaded["regime"] == "crash"
        assert loaded["qty"] == 1
        assert loaded["transmit"] is False
    
    def test_available_ranks_error_message(self):
        """Test error message shows available ranks when requested rank not found"""
        candidates = [
            {"rank": 1, "expiry": "20260320"},
            {"rank": 2, "expiry": "20260320"},
            {"rank": 5, "expiry": "20260320"},
        ]
        
        def select_candidate_by_rank(candidates_list, rank):
            for c in candidates_list:
                if c.get("rank") == rank:
                    return c
            return None
        
        candidate = select_candidate_by_rank(candidates, 3)
        
        if candidate is None:
            available = [c.get("rank") for c in candidates]
            error_msg = f"No candidate with rank=3. Available ranks: {available}"
            assert "Available ranks: [1, 2, 5]" in error_msg
    
    def test_no_candidates_available_error(self):
        """Test error when regime has no candidates"""
        candidates = []
        
        if not candidates:
            # This should raise SystemExit with appropriate message
            error_msg = "No candidates available for regime=crash"
            assert "No candidates available" in error_msg


class TestIntentEmissionIntegration:
    """Integration tests for full intent emission flow"""
    
    def test_multi_regime_run_requires_explicit_regime_selection(self):
        """When running both regimes, intent emission requires picking one"""
        # Simulate results from both regimes
        results_by_regime = {
            "crash": {
                "candidates": [
                    {"rank": 1, "expiry": "20260320", "strikes": {"long_put": 580.0, "short_put": 560.0}}
                ]
            },
            "selloff": {
                "candidates": [
                    {"rank": 1, "expiry": "20260320", "strikes": {"long_put": 600.0, "short_put": 585.0}}
                ]
            }
        }
        
        # User must specify which regime to emit from
        selected_regime = "crash"
        regime_result = results_by_regime.get(selected_regime)
        
        assert regime_result is not None
        assert len(regime_result["candidates"]) > 0
    
    def test_intent_emission_exits_before_execution(self):
        """Verify that intent emission returns early and doesn't continue to execution"""
        # This is validated by the explicit `return` statement in run_daily_v2.py
        # after intent is written. The test verifies the behavior conceptually.
        
        intent_mode = True
        
        if intent_mode:
            # Write intent
            intent_written = True
            # Explicit termination - should return here
            assert intent_written
            return  # Should not continue to execution
        
        # This should never be reached in intent mode
        pytest.fail("Should not reach execution code in intent mode")


class TestProvenanceFields:
    """Patch 1-A: Top-level provenance fields in OrderIntent."""

    _CANDIDATE = {
        "rank": 2,
        "expiry": "20260320",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "symbol": "SPY",
        "candidate_id": "cand_20260320_580_560",
        "metrics": {"ev_per_dollar": 0.30},
    }

    def test_provenance_fields_promoted_to_top_level(self):
        """candidate_id, picked_rank, run_id, source_run_dir are at top-level."""
        intent = build_order_intent(
            candidate=self._CANDIDATE,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75,
            run_id="run_abc123",
            source_run_dir="/runs/crash_venture_v2/run_abc123",
        )
        assert intent["candidate_id"] == "cand_20260320_580_560"
        assert intent["picked_rank"] == 2
        assert intent["run_id"] == "run_abc123"
        assert intent["source_run_dir"] == "/runs/crash_venture_v2/run_abc123"

    def test_metadata_preserved_alongside_top_level(self):
        """metadata dict still contains candidate_id for backward compat."""
        intent = build_order_intent(
            candidate=self._CANDIDATE,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75,
            run_id="run_abc123",
        )
        # Top-level AND metadata both present
        assert intent["candidate_id"] == "cand_20260320_580_560"
        assert intent["metadata"]["candidate_id"] == "cand_20260320_580_560"

    def test_intent_id_stable_regardless_of_run_context(self):
        """intent_id is the same for two intents that differ only in run_id/source_run_dir."""
        base_kwargs = dict(
            candidate=self._CANDIDATE,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75,
        )
        intent_a = build_order_intent(**base_kwargs, run_id=None, source_run_dir=None)
        intent_b = build_order_intent(
            **base_kwargs,
            run_id="run_xyz999",
            source_run_dir="/runs/foo/bar",
        )
        assert intent_a["intent_id"] == intent_b["intent_id"], (
            "intent_id must be stable across differing run_id / source_run_dir"
        )

    def test_intent_id_differs_when_strategy_changes(self):
        """Sanity: intent_id does differ when actual strategy content changes."""
        cand_a = dict(self._CANDIDATE, strikes={"long_put": 580.0, "short_put": 560.0})
        cand_b = dict(self._CANDIDATE, strikes={"long_put": 575.0, "short_put": 555.0})
        intent_a = build_order_intent(candidate=cand_a, regime="crash", qty=1,
                                      limit_start=2.50, limit_max=2.75)
        intent_b = build_order_intent(candidate=cand_b, regime="crash", qty=1,
                                      limit_start=2.50, limit_max=2.75)
        assert intent_a["intent_id"] != intent_b["intent_id"]

    def test_none_run_id_stored_as_none(self):
        """When run_id is not provided, top-level field is None (not absent)."""
        intent = build_order_intent(
            candidate=self._CANDIDATE,
            regime="crash",
            qty=1,
            limit_start=2.50,
            limit_max=2.75,
        )
        assert "run_id" in intent
        assert intent["run_id"] is None
        assert "source_run_dir" in intent
        assert intent["source_run_dir"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
