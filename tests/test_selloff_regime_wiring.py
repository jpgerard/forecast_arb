"""
Test Selloff Regime Wiring (Regression Test)

Reproduces and tests fix for the bug where selloff regime was generating
candidates using crash moneyness (-0.15) instead of selloff moneyness (-0.09).

Bug Symptoms:
- Selloff event_spec has correct moneyness (-0.09) and threshold (~627)
- But selloff candidates have strikes near ~585 (crash strikes) and 
  moneyness_target=-0.15 (crash moneyness)

Root Cause:
- run_daily_v2.py was passing base config's moneyness_targets list to
  generate_candidates_from_snapshot, which wasn't regime-specific
- Candidates were missing "regime" field

Fix:
- Pass regime-specific moneyness ([event_moneyness]) instead of config list
- Add "regime" field to candidate dict
- Add validation guardrails to catch mismatches
"""

import pytest
import json
from pathlib import Path
from unittest.mock import Mock, patch


def test_selloff_uses_correct_moneyness():
    """
    Test that selloff regime uses selloff moneyness (-0.09), not crash (-0.15).
    
    This is a regression test for the bug where selloff generated candidates
    with crash strikes.
    """
    from forecast_arb.engine.crash_venture_v1_snapshot import generate_candidates_from_snapshot
    
    # Create a minimal synthetic snapshot with spot=689 (similar to real bug)
    spot = 689.0
    expiry = "20260320"
    
    # Create strike grid covering both crash and selloff regions
    # Crash threshold (spot * 0.85) = 585.65 → strikes near 585
    # Selloff threshold (spot * 0.91) = 627.19 → strikes near 627
    strikes = list(range(565, 701, 5))  # 565, 570, 575, ..., 695, 700
    
    snapshot = {
        "snapshot_metadata": {
            "underlier": "SPY",
            "current_price": spot,
            "snapshot_time": "2026-02-06T15:00:00Z"
        },
        expiry: []
    }
    
    # Add put options for each strike with synthetic pricing
    for strike in strikes:
        moneyness = (strike - spot) / spot
        # Rough IV estimate based on moneyness
        iv = 0.20 + abs(moneyness) * 0.5
        
        # Simplified pricing (not accurate, just for structure)
        otm_amount = max(0, spot - strike)
        time_value = 1.0
        bid = max(0.01, otm_amount + time_value - 0.10)
        ask = otm_amount + time_value + 0.10
        
        snapshot[expiry].append({
            "strike": strike,
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "implied_vol": iv,
            "delta": -0.05 * (1 + abs(moneyness))
        })
    
    # Test crash regime (-0.15 moneyness)
    crash_candidates, crash_filtered = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.15],
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="crash"
    )
    
    # Test selloff regime (-0.09 moneyness)
    selloff_candidates, selloff_filtered = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.09],
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="selloff"
    )
    
    # Assertions: Crash should use strikes near 585
    assert len(crash_candidates) > 0, "Crash should generate candidates"
    crash_long_strike = crash_candidates[0]["strikes"]["long_put"]
    crash_moneyness = crash_candidates[0]["moneyness_target"]
    crash_regime = crash_candidates[0]["regime"]
    
    # Crash long strike should be near 585 (spot * 0.85 = 585.65)
    assert 580 <= crash_long_strike <= 590, f"Crash long strike {crash_long_strike} should be near 585"
    assert crash_moneyness == -0.15, f"Crash moneyness should be -0.15, got {crash_moneyness}"
    assert crash_regime == "crash", f"Crash regime field should be 'crash', got {crash_regime}"
    
    # Assertions: Selloff should use strikes near 627 (NOT 585!)
    assert len(selloff_candidates) > 0, "Selloff should generate candidates"
    selloff_long_strike = selloff_candidates[0]["strikes"]["long_put"]
    selloff_moneyness = selloff_candidates[0]["moneyness_target"]
    selloff_regime = selloff_candidates[0]["regime"]
    
    # THIS IS THE KEY TEST: Selloff long strike should be near 627, NOT 585
    assert 620 <= selloff_long_strike <= 635, (
        f"Selloff long strike {selloff_long_strike} should be near 627 (spot * 0.91), "
        f"NOT near 585 (crash strikes). This indicates the bug is still present!"
    )
    assert selloff_moneyness == -0.09, f"Selloff moneyness should be -0.09, got {selloff_moneyness}"
    assert selloff_regime == "selloff", f"Selloff regime field should be 'selloff', got {selloff_regime}"
    
    # Verify strikes are different between regimes
    assert abs(crash_long_strike - selloff_long_strike) > 30, (
        f"Crash ({crash_long_strike}) and selloff ({selloff_long_strike}) "
        f"should use different strikes (>$30 apart)"
    )


def test_candidate_has_regime_field():
    """Test that all candidates include the 'regime' field."""
    from forecast_arb.engine.crash_venture_v1_snapshot import generate_candidates_from_snapshot
    
    spot = 689.0
    expiry = "20260320"
    strikes = list(range(580, 631, 5))
    
    snapshot = {
        "snapshot_metadata": {
            "underlier": "SPY",
            "current_price": spot,
            "snapshot_time": "2026-02-06T15:00:00Z"
        },
        expiry: []
    }
    
    for strike in strikes:
        snapshot[expiry].append({
            "strike": strike,
            "bid": 1.0,
            "ask": 1.2,
            "implied_vol": 0.20,
            "delta": -0.05
        })
    
    candidates, _ = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.09],
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="selloff"
    )
    
    assert len(candidates) > 0, "Should generate at least one candidate"
    
    for candidate in candidates:
        assert "regime" in candidate, "Candidate must have 'regime' field"
        assert candidate["regime"] == "selloff", f"Expected regime='selloff', got {candidate['regime']}"
        assert "moneyness_target" in candidate, "Candidate must have 'moneyness_target' field"


def test_candidate_validation_catches_mismatch():
    """Test that candidate validation catches regime/moneyness mismatch."""
    from forecast_arb.structuring.candidate_validator import (
        validate_candidate_regime,
        CandidateRegimeMismatchError
    )
    
    # Valid candidate
    valid_candidate = {
        "regime": "selloff",
        "moneyness_target": -0.09,
        "candidate_id": "test123"
    }
    
    # Should not raise
    validate_candidate_regime(
        candidate=valid_candidate,
        regime="selloff",
        expected_moneyness=-0.09,
        tolerance=0.001
    )
    
    # Invalid: Wrong regime
    wrong_regime_candidate = {
        "regime": "crash",  # Wrong!
        "moneyness_target": -0.09,
        "candidate_id": "test456"
    }
    
    with pytest.raises(CandidateRegimeMismatchError, match="does not match expected 'selloff'"):
        validate_candidate_regime(
            candidate=wrong_regime_candidate,
            regime="selloff",
            expected_moneyness=-0.09,
            tolerance=0.001
        )
    
    # Invalid: Wrong moneyness (the actual bug case!)
    wrong_moneyness_candidate = {
        "regime": "selloff",
        "moneyness_target": -0.15,  # This is crash moneyness, not selloff!
        "candidate_id": "test789"
    }
    
    with pytest.raises(CandidateRegimeMismatchError, match="does not match expected"):
        validate_candidate_regime(
            candidate=wrong_moneyness_candidate,
            regime="selloff",
            expected_moneyness=-0.09,
            tolerance=0.001
        )


def test_validation_filters_mismatched_candidates():
    """Test that enforce_regime_consistency filters out bad candidates."""
    from forecast_arb.structuring.candidate_validator import enforce_regime_consistency
    
    candidates = [
        # Valid
        {"regime": "selloff", "moneyness_target": -0.09, "candidate_id": "valid1"},
        # Invalid: wrong moneyness
        {"regime": "selloff", "moneyness_target": -0.15, "candidate_id": "invalid1"},
        # Valid
        {"regime": "selloff", "moneyness_target": -0.09, "candidate_id": "valid2"},
        # Invalid: wrong regime
        {"regime": "crash", "moneyness_target": -0.09, "candidate_id": "invalid2"},
    ]
    
    # In permissive mode (fail_fast=False), should filter out invalid
    valid_only = enforce_regime_consistency(
        candidates=candidates,
        regime="selloff",
        expected_moneyness=-0.09,
        tolerance=0.001,
        fail_fast=False
    )
    
    assert len(valid_only) == 2, f"Should filter to 2 valid candidates, got {len(valid_only)}"
    assert all(c["candidate_id"].startswith("valid") for c in valid_only)
    
    # In strict mode (fail_fast=True), should raise on first mismatch
    from forecast_arb.structuring.candidate_validator import CandidateRegimeMismatchError
    
    with pytest.raises(CandidateRegimeMismatchError):
        enforce_regime_consistency(
            candidates=candidates,
            regime="selloff",
            expected_moneyness=-0.09,
            tolerance=0.001,
            fail_fast=True
        )


def test_regime_config_overlay_applies_correctly():
    """Test that apply_regime_overrides properly applies selloff moneyness."""
    from forecast_arb.core.regime import apply_regime_overrides
    
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15  # Default crash
        },
        "regimes": {
            "crash": {
                "moneyness": -0.15
            },
            "selloff": {
                "moneyness": -0.09
            }
        }
    }
    
    # Apply crash overlay
    crash_config = apply_regime_overrides(base_config, "crash")
    assert crash_config["edge_gating"]["event_moneyness"] == -0.15
    
    # Apply selloff overlay
    selloff_config = apply_regime_overrides(base_config, "selloff")
    assert selloff_config["edge_gating"]["event_moneyness"] == -0.09, (
        "Selloff overlay should set event_moneyness to -0.09"
    )
    
    # Verify deep copy (base unmodified)
    assert base_config["edge_gating"]["event_moneyness"] == -0.15, (
        "Base config should remain unchanged"
    )


def test_candidate_id_includes_regime():
    """Test that candidate_id includes regime for uniqueness across regimes."""
    from forecast_arb.engine.crash_venture_v1_snapshot import generate_candidates_from_snapshot
    
    spot = 689.0
    expiry = "20260320"
    strikes = list(range(620, 641, 5))
    
    snapshot = {
        "snapshot_metadata": {
            "underlier": "SPY",
            "current_price": spot,
            "snapshot_time": "2026-02-06T15:00:00Z"
        },
        expiry: []
    }
    
    for strike in strikes:
        snapshot[expiry].append({
            "strike": strike,
            "bid": 1.0,
            "ask": 1.2,
            "implied_vol": 0.20,
            "delta": -0.05
        })
    
    # Generate same structure in both regimes
    crash_cands, _ = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.09],  # Intentionally same for this test
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="crash"
    )
    
    selloff_cands, _ = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=spot,
        moneyness_targets=[-0.09],
        spread_widths=[20],
        min_debit_per_contract=10.0,
        max_candidates=10,
        regime="selloff"
    )
    
    # Candidate IDs should differ because they include regime
    if crash_cands and selloff_cands:
        # Find candidates with same strikes
        for c_cand in crash_cands:
            for s_cand in selloff_cands:
                if (c_cand["strikes"]["long_put"] == s_cand["strikes"]["long_put"] and
                    c_cand["strikes"]["short_put"] == s_cand["strikes"]["short_put"]):
                    # Same strikes but different regime -> IDs must differ
                    assert c_cand["candidate_id"] != s_cand["candidate_id"], (
                        f"Candidates with same strikes but different regimes must have "
                        f"different IDs. Got crash={c_cand['candidate_id']}, "
                        f"selloff={s_cand['candidate_id']}"
                    )
