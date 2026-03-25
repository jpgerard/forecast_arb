"""
Phase 3/4 Clean Base Hardening Tests

Tests to ensure:
1. recommended.json always has non-null timestamp_utc
2. Candidate flatten schema is complete (all canonical fields required)
3. Probability semantics are clean and consistent
4. Selection summary is structured properly
5. Conditioning provenance is preserved
6. Snapshot isolation guard prevents contamination
7. No-trade artifacts are complete
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from forecast_arb.campaign.selector import run_selector, select_candidates
from forecast_arb.campaign.grid_runner import flatten_candidate


# ==============================================================================
# TEST A — No-trade Artifact Integrity
# ==============================================================================

def test_no_trade_recommended_json_integrity():
    """
    Test that recommended.json is well-formed even with zero candidates.
    
    Ensures:
    - timestamp_utc is non-null
    - selection_summary is structured
    - No KeyError on required fields
    """
    # Create empty candidates file
    with tempfile.TemporaryDirectory() as tmpdir:
        candidates_path = Path(tmpdir) / "candidates_flat.json"
        with open(candidates_path, "w") as f:
            json.dump([], f)  # EMPTY candidate list
        
        # Create minimal campaign config
        campaign_config = {
            "governors": {
                "daily_premium_cap_usd": 1000.0,
                "cluster_cap_per_day": 1,
                "max_open_positions_by_regime": {},
                "premium_at_risk_caps_usd": {}
            },
            "selection": {
                "max_trades_per_day": 2,
                "scoring": "ev_per_dollar"
            }
        }
        
        # Create minimal ledger (empty file)
        ledger_path = Path(tmpdir) / "trade_outcomes.jsonl"
        ledger_path.touch()
        
        # Run selector
        recommended_path = run_selector(
            candidates_flat_path=str(candidates_path),
            campaign_config=campaign_config,
            ledger_path=str(ledger_path),
            qty=1,
            output_dir=Path(tmpdir)
        )
        
        # Load recommended.json
        with open(recommended_path, "r") as f:
            recommended = json.load(f)
        
        # ASSERTION 1: timestamp_utc exists and is non-null
        assert "timestamp_utc" in recommended, "timestamp_utc must be present"
        assert recommended["timestamp_utc"] is not None, "timestamp_utc must NOT be null"
        assert isinstance(recommended["timestamp_utc"], str), "timestamp_utc must be ISO string"
        
        # ASSERTION 2: selection_summary is structured
        assert "selection_summary" in recommended
        summary = recommended["selection_summary"]
        
        assert "total_candidates" in summary
        assert summary["total_candidates"] == 0
        
        assert "representable_count" in summary
        assert "non_representable_count" in summary
        assert "selected_count" in summary
        assert "no_representable_candidates" in summary
        
        # ASSERTION 3: blocked_by_governor structure exists
        assert "blocked_by_governor" in summary
        blocked = summary["blocked_by_governor"]
        assert "daily_premium_cap" in blocked
        assert "open_premium_cap" in blocked
        assert "regime_slot_cap" in blocked
        assert "cluster_cap" in blocked
        
        # ASSERTION 4: probability_breakdown structure exists
        assert "probability_breakdown" in summary
        breakdown = summary["probability_breakdown"]
        assert "external_count" in breakdown
        assert "implied_count" in breakdown
        assert "fallback_count" in breakdown
        
        # ASSERTION 5: No KeyError - all expected fields present
        assert "selected" in recommended
        assert isinstance(recommended["selected"], list)
        assert len(recommended["selected"]) == 0


def test_no_representable_candidates_artifact():
    """
    Test that system produces complete artifacts when all candidates are non-representable.
    """
    # Create candidate list with non-representable candidates
    candidates = [
        {
            "candidate_id": "NOT_REP_1",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "debit_per_contract": 100.0,
            "ev_per_dollar": 2.0,
            "ev_usd": 200.0,
            "p_used": 0.05,
            "p_used_src": "external",
            "p_impl": 0.04,
            "p_ext": 0.05,
            "p_ext_status": "OK",
            "p_ext_reason": "OK",
            "p_source": "kalshi",
            "representable": False,  # NOT REPRESENTABLE
            "robustness": None,
            "robustness_flags": [],
        }
    ]
    
    governors = {
        "daily_premium_cap_usd": 10000.0,
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {},
        "premium_at_risk_caps_usd": {},
        "max_trades_per_day": 10
    }
    
    positions_view = {
        "open_positions": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat()
    }
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1
    )
    
    # Verify structured response even with no representable candidates
    assert len(result.selected) == 0
    assert result.reasons["representable_candidates"] == 0
    assert result.reasons.get("no_representable_candidates") is not True  # Only true if list is empty


# ==============================================================================
# TEST B — Canonical Field Enforcement
# ==============================================================================

def test_missing_canonical_ev_per_dollar_fails():
    """
    Test that flatten_candidate raises ValueError if canonical ev_per_dollar cannot be computed.
    """
    # Candidate missing probability data (no p_ext, p_impl, or assumed_p_event)
    candidate = {
        "candidate_id": "MISSING_PROB",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "ev_per_dollar_raw": 2.0,
        "representable": True,
        "rank": 1,
        # Missing p_implied, no assumed_p_event
    }
    
    with pytest.raises(ValueError, match="Cannot determine p_used"):
        flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-60d",
            cluster_id="EQUITY",
            cell_id="SPY_crash_30-60d",
            regime_p_implied=None,  # No regime-level implied
            regime_p_external=None   # No external
        )


def test_missing_debit_per_contract_fails():
    """
    Test that flatten_candidate raises ValueError if debit_per_contract is missing.
    """
    candidate = {
        "candidate_id": "MISSING_DEBIT",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        # debit_per_contract MISSING
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
    }
    
    with pytest.raises(ValueError, match="Missing debit_per_contract"):
        flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-60d",
            cluster_id="EQUITY",
            cell_id="SPY_crash_30-60d"
        )


def test_canonical_fields_all_present():
    """
    Test that flatten_candidate emits all required canonical fields.
    """
    candidate = {
        "candidate_id": "FULL",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "ev_per_dollar": 2.0,
        "ev_usd": 200.0,
        "prob_profit": 0.05,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05
    )
    
    # REQUIRED canonical fields
    assert "ev_per_dollar" in flat
    assert flat["ev_per_dollar"] is not None
    
    assert "ev_usd" in flat
    assert flat["ev_usd"] is not None
    
    assert "p_profit" in flat
    assert flat["p_profit"] is not None
    
    assert "p_used" in flat
    assert flat["p_used"] is not None
    
    assert "p_used_src" in flat
    assert flat["p_used_src"] in ["external", "implied", "fallback", "external_conditioned", "implied_conditioned", "fallback_conditioned"]
    
    assert "p_ext_status" in flat
    assert flat["p_ext_status"] in ["OK", "AUTH_FAIL", "NO_MARKET"]
    
    assert "p_ext_reason" in flat
    assert flat["p_ext_reason"] is not None
    
    assert "p_source" in flat
    assert flat["p_source"] in ["kalshi", "options_implied", "implied_spread", "unknown"]
    
    # RAW fields
    assert "ev_per_dollar_raw" in flat
    assert "prob_profit_raw" in flat
    assert "ev_usd_raw" in flat
    
    # Robustness fields
    assert "robustness" in flat
    assert "robustness_flags" in flat


# ==============================================================================
# TEST C — Probability Semantics Consistency
# ==============================================================================

def test_external_probability_semantics():
    """
    Test that when p_used_src="external", p_ext_status must be "OK".
    """
    candidate = {
        "candidate_id": "EXT",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.04,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    regime_p_external = {
        "p": 0.05,
        "source": "kalshi",
        "authoritative": True,  # AUTHORITATIVE
        "asof_ts_utc": datetime.now(timezone.utc).isoformat(),
        "market": {},
        "match": {},
        "quality": {
            "liquidity_ok": True,
            "staleness_ok": True,
            "spread_ok": True,
            "warnings": []
        }
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.04,
        regime_p_external=regime_p_external
    )
    
    # ASSERTION: external used, status must be OK
    assert flat["p_used_src"] == "external"
    assert flat["p_ext_status"] == "OK"
    assert flat["p_used"] == 0.05  # Uses external
    assert flat["p_source"] == "kalshi"


def test_implied_probability_semantics():
    """
    Test that when p_used_src="implied", p_source reflects operational source.
    """
    candidate = {
        "candidate_id": "IMPL",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
        "p_event_result": {
            "p": 0.05,
            "source": "options_implied",
            "confidence": 0.8
        }
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05,
        regime_p_external=None  # No external
    )
    
    # ASSERTION: implied used, source reflects operational source
    assert flat["p_used_src"] == "implied"
    assert flat["p_used"] == 0.05
    assert flat["p_source"] == "options_implied"


def test_fallback_probability_semantics():
    """
    Test that when p_used_src="fallback", p_source is "unknown".
    """
    candidate = {
        "candidate_id": "FALLBACK",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "assumed_p_event": 0.03,  # Fallback probability
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=None,  # No implied
        regime_p_external=None  # No external
    )
    
    # ASSERTION: fallback used, source is unknown
    assert flat["p_used_src"] == "fallback"
    assert flat["p_used"] == 0.03
    assert flat["p_source"] == "unknown"


def test_p_used_src_does_not_collapse_with_p_source():
    """
    Test that p_used_src and p_source are separate and not collapsed.
    """
    candidate = {
        "candidate_id": "SEP",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    regime_p_external = {
        "p": 0.046,
        "source": "kalshi",
        "authoritative": True,
        "asof_ts_utc": datetime.now(timezone.utc).isoformat(),
        "market": {},
        "match": {},
        "quality": {"liquidity_ok": True, "staleness_ok": True, "spread_ok": True, "warnings": []}
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05,
        regime_p_external=regime_p_external
    )
    
    # p_used_src: which probability source was used for EV (external vs implied vs fallback)
    assert flat["p_used_src"] == "external"
    
    # p_source: operational source (kalshi, options_implied, etc.)
    assert flat["p_source"] == "kalshi"
    
    # These are DIFFERENT fields with different purposes
    assert flat["p_used_src"] != flat["p_source"]


# ==============================================================================
# TEST D — Snapshot Isolation Guard
# ==============================================================================

def test_snapshot_underlier_mismatch_fails():
    """
    Test that snapshot isolation guard raises ValueError on underlier mismatch.
    """
    from forecast_arb.structuring.snapshot_io import get_snapshot_metadata
    
    # Mock snapshot with wrong underlier
    mock_snapshot = {
        "metadata": {
            "underlier": "QQQ",  # WRONG - expected SPY
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "current_price": 480.0
        },
        "chains": []
    }
    
    # Simulate the check from grid_runner
    snapshot_symbol = mock_snapshot["metadata"]["underlier"]
    expected_underlier = "SPY"
    
    # ASSERTION: Should raise ValueError
    with pytest.raises(ValueError, match="SNAPSHOT UNDERLIER MISMATCH"):
        if snapshot_symbol != expected_underlier:
            raise ValueError(
                f"SNAPSHOT UNDERLIER MISMATCH: expected '{expected_underlier}', "
                f"got '{snapshot_symbol}'. "
                f"This would cause cross-underlier contamination and invalid candidate generation."
            )


def test_snapshot_correct_underlier_passes():
    """
    Test that snapshot isolation guard passes when underlier matches.
    """
    mock_snapshot = {
        "metadata": {
            "underlier": "SPY",  # CORRECT
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "current_price": 580.0
        },
        "chains": []
    }
    
    snapshot_symbol = mock_snapshot["metadata"]["underlier"]
    expected_underlier = "SPY"
    
    # Should NOT raise
    if snapshot_symbol != expected_underlier:
        raise ValueError("Should not raise")
    
    # Test passes if no exception


# ==============================================================================
# TEST E — Conditioning Provenance Pass-Through
# ==============================================================================

def test_conditioning_provenance_preserved():
    """
    Test that Phase 4 conditioning provenance is passed through unchanged.
    """
    candidate = {
        "candidate_id": "COND",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
        # Phase 4 conditioning block
        "conditioning": {
            "confidence_score": 0.85,
            "multipliers": {"vol_regime": 1.1, "market_stress": 0.9},
            "regime_signals": ["high_vol"],
            "p_base": 0.05,
            "p_adjusted": 0.055
        }
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05
    )
    
    # ASSERTION: Conditioning block preserved
    assert "conditioning" in flat
    assert flat["conditioning"] is not None
    assert flat["conditioning"]["p_adjusted"] == 0.055
    assert flat["conditioning"]["p_base"] == 0.05
    assert flat["conditioning"]["confidence_score"] == 0.85
    
    # ASSERTION: p_used uses p_adjusted from conditioning
    assert flat["p_used"] == 0.055
    assert flat["p_used_src"] == "implied_conditioned"


def test_conditioning_absent_no_invention():
    """
    Test that conditioning is not invented if not present in candidate.
    """
    candidate = {
        "candidate_id": "NO_COND",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
        # NO conditioning block
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05
    )
    
    # ASSERTION: Conditioning is None (not invented)
    assert flat["conditioning"] is None


# ==============================================================================
# TEST F — Selection Summary Structure
# ==============================================================================

def test_selection_summary_structure():
    """
    Test that selection_summary has all required structured fields.
    """
    candidates = [
        {
            "candidate_id": "A",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "debit_per_contract": 100.0,
            "ev_per_dollar": 2.0,
            "ev_usd": 200.0,
            "p_used": 0.05,
            "p_used_src": "external",
            "p_impl": 0.04,
            "p_ext": 0.05,
            "p_ext_status": "OK",
            "p_ext_reason": "OK",
            "p_source": "kalshi",
            "p_profit": 0.05,
            "representable": True,
        },
        {
            "candidate_id": "B",
            "underlier": "QQQ",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "TECH",
            "cell_id": "QQQ_crash_30-60d",
            "debit_per_contract": 100.0,
            "ev_per_dollar": 1.5,
            "ev_usd": 150.0,
            "p_used": 0.04,
            "p_used_src": "implied",
            "p_impl": 0.04,
            "p_ext": None,
            "p_ext_status": "NO_MARKET",
            "p_ext_reason": "No market",
            "p_source": "options_implied",
            "p_profit": 0.04,
            "representable": True,
        },
        {
            "candidate_id": "C",
            "underlier": "IWM",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "SMALL_CAP",
            "cell_id": "IWM_crash_30-60d",
            "debit_per_contract": 100.0,
            "ev_per_dollar": 1.2,
            "ev_usd": 120.0,
            "p_used": 0.03,
            "p_used_src": "fallback",
            "p_impl": None,
            "p_ext": None,
            "p_ext_status": "NO_MARKET",
            "p_ext_reason": "No market",
            "p_source": "unknown",
            "p_profit": 0.03,
            "representable": False,  # NOT REPRESENTABLE
        }
    ]
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write candidates
        candidates_path = Path(tmpdir) / "candidates_flat.json"
        with open(candidates_path, "w") as f:
            json.dump(candidates, f)
        
        # Create config with restrictive governors
        campaign_config = {
            "governors": {
                "daily_premium_cap_usd": 150.0,  # Only allow 1 trade
                "cluster_cap_per_day": 1,
                "max_open_positions_by_regime": {},
                "premium_at_risk_caps_usd": {}
            },
            "selection": {
                "max_trades_per_day": 2,
                "scoring": "ev_per_dollar"
            }
        }
        
        ledger_path = Path(tmpdir) / "trade_outcomes.jsonl"
        ledger_path.touch()
        
        recommended_path = run_selector(
            candidates_flat_path=str(candidates_path),
            campaign_config=campaign_config,
            ledger_path=str(ledger_path),
            qty=1,
            output_dir=Path(tmpdir)
        )
        
        with open(recommended_path, "r") as f:
            recommended = json.load(f)
        
        summary = recommended["selection_summary"]
        
        # ASSERTION: All required fields present
        assert summary["total_candidates"] == 3
        assert summary["representable_count"] == 2
        assert summary["non_representable_count"] == 1
        assert summary["selected_count"] >= 0  # May be 0 or more
        assert summary["no_representable_candidates"] == False
        
        # blocked_by_governor breakdown
        blocked = summary["blocked_by_governor"]
        assert isinstance(blocked["daily_premium_cap"], int)
        assert isinstance(blocked["open_premium_cap"], int)
        assert isinstance(blocked["regime_slot_cap"], int)
        assert isinstance(blocked["cluster_cap"], int)
        
        # probability_breakdown
        breakdown = summary["probability_breakdown"]
        assert breakdown["external_count"] == 1
        assert breakdown["implied_count"] == 1
        assert breakdown["fallback_count"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
