"""
Phase 3 Campaign EV Provenance Tests

Tests to ensure:
1. Selector uses ONLY canonical EV fields (never raw)
2. Probability labels are consistent (P(event) = p_used)
3. Robustness penalty is applied correctly
4. Missing canonical fields raise errors (no silent defaults)
"""

import pytest
from forecast_arb.campaign.selector import select_candidates, compute_robustness_score


def create_mock_positions_view():
    """Create empty positions view for testing."""
    return {
        "open_positions": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set(),
        "timestamp_utc": "2026-02-26T12:00:00Z"
    }


def create_mock_governors():
    """Create permissive governors for testing."""
    return {
        "daily_premium_cap_usd": 10000.0,
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {"crash": 10, "selloff": 10},
        "premium_at_risk_caps_usd": {
            "crash": 10000.0,
            "selloff": 10000.0,
            "total": 20000.0
        },
        "max_trades_per_day": 10
    }


def test_selector_uses_canonical_ev_not_raw():
    """
    Test that selector ranks by canonical EV, not raw EV.
    
    Setup:
    - Candidate A: ev_per_dollar_raw=100, ev_per_dollar=1
    - Candidate B: ev_per_dollar_raw=2, ev_per_dollar=3
    
    Expected: Candidate B ranks higher (canonical EV is higher)
    """
    candidates = [
        {
            "candidate_id": "A",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            # RAW fields (from generator)
            "ev_per_dollar_raw": 100.0,  # HIGH raw value
            "prob_profit_raw": 0.95,
            "ev_usd_raw": 10000.0,
            # CANONICAL fields (recomputed by campaign)
            "ev_per_dollar": 1.0,  # LOW canonical value
            "ev_usd": 100.0,
            "p_profit": 0.05,
            "p_used": 0.05,
            "p_used_src": "external",
            "p_impl": 0.04,
            "p_ext": 0.05,
            "p_ext_status": "OK",
            "p_ext_reason": "Authoritative",
            "representable": True,
            "rank": 1,
        },
        {
            "candidate_id": "B",
            "underlier": "QQQ",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "TECH",
            "cell_id": "QQQ_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 480.0,
            "short_strike": 460.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            # RAW fields (from generator)
            "ev_per_dollar_raw": 2.0,  # LOW raw value
            "prob_profit_raw": 0.10,
            "ev_usd_raw": 200.0,
            # CANONICAL fields (recomputed by campaign)
            "ev_per_dollar": 3.0,  # HIGH canonical value
            "ev_usd": 300.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",
            "p_impl": 0.08,
            "p_ext": 0.10,
            "p_ext_status": "OK",
            "p_ext_reason": "Authoritative",
            "representable": True,
            "rank": 1,
        }
    ]
    
    governors = create_mock_governors()
    positions_view = create_mock_positions_view()
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Assert: Candidate B selected (higher canonical EV)
    assert len(result.selected) == 2  # Both should be selected with permissive governors
    # First selected should be B (higher canonical EV)
    assert result.selected[0]["candidate_id"] == "B", \
        "Selector should rank B first (canonical ev_per_dollar=3.0 > 1.0)"
    assert result.selected[1]["candidate_id"] == "A"


def test_robustness_penalty_applied():
    """
    Test that robustness penalty is applied to fallback probability source.
    
    Setup:
    - Candidate A: ev_per_dollar=2.0, p_used_src="external" (no penalty)
    - Candidate B: ev_per_dollar=2.0, p_used_src="fallback" (0.5x penalty)
    
    Expected: Candidate A ranks higher (same base EV, but no robustness penalty)
    """
    candidates = [
        {
            "candidate_id": "A_external",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 2.0,
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",  # NO PENALTY
            "p_impl": 0.08,
            "p_ext": 0.10,
            "p_ext_status": "OK",
            "p_ext_reason": "Authoritative",
            "representable": True,
            "rank": 1,
        },
        {
            "candidate_id": "B_fallback",
            "underlier": "QQQ",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "TECH",
            "cell_id": "QQQ_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 480.0,
            "short_strike": 460.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 2.0,  # SAME as candidate A
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "fallback",  # 0.5x PENALTY
            "p_impl": None,
            "p_ext": None,
            "p_ext_status": "NO_MARKET",
            "p_ext_reason": "No Kalshi market found",
            "representable": True,
            "rank": 1,
        }
    ]
    
    governors = create_mock_governors()
    positions_view = create_mock_positions_view()
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Assert: Candidate A selected first (no robustness penalty)
    assert len(result.selected) == 2
    assert result.selected[0]["candidate_id"] == "A_external", \
        "Selector should rank external source higher (no robustness penalty)"
    assert result.selected[1]["candidate_id"] == "B_fallback"
    
    # Check robustness fields are persisted
    assert result.selected[0]["robustness"] == 1.0, "External source should have robustness=1.0"
    assert result.selected[1]["robustness"] == 0.5 * 0.7, "Fallback + NO_MARKET should have robustness=0.35"
    assert "P_FALLBACK" in result.selected[1]["robustness_flags"]
    assert "P_EXT_NO_MARKET" in result.selected[1]["robustness_flags"]


def test_canonical_ev_required_no_silent_default():
    """
    Test that missing canonical ev_per_dollar raises an error (no silent default to 0).
    
    Setup: Candidate missing canonical ev_per_dollar field
    Expected: ValueError raised
    """
    candidates = [
        {
            "candidate_id": "MISSING_CANONICAL_EV",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            # RAW fields present
            "ev_per_dollar_raw": 2.0,
            "prob_profit_raw": 0.10,
            # CANONICAL ev_per_dollar MISSING
            # "ev_per_dollar": None,  # <-- MISSING!
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",
            "representable": True,
            "rank": 1,
        }
    ]
    
    governors = create_mock_governors()
    positions_view = create_mock_positions_view()
    
    # Assert: ValueError raised
    with pytest.raises(ValueError, match="missing required canonical field 'ev_per_dollar'"):
        select_candidates(
            candidates_flat=candidates,
            governors=governors,
            positions_view=positions_view,
            qty=1,
            scoring_method="ev_per_dollar"
        )


def test_robustness_score_computation():
    """Test robustness score computation for various scenarios."""
    
    # Scenario 1: External source, OK status -> robustness=1.0
    candidate1 = {
        "representable": True,
        "p_used_src": "external",
        "p_ext_status": "OK"
    }
    robustness1, flags1 = compute_robustness_score(candidate1)
    assert robustness1 == 1.0
    assert flags1 == []
    
    # Scenario 2: Fallback source -> robustness=0.5
    candidate2 = {
        "representable": True,
        "p_used_src": "fallback",
        "p_ext_status": "NO_MARKET"
    }
    robustness2, flags2 = compute_robustness_score(candidate2)
    assert robustness2 == 0.5 * 0.7  # fallback * no_market
    assert "P_FALLBACK" in flags2
    assert "P_EXT_NO_MARKET" in flags2
    
    # Scenario 3: Implied source, NO_MARKET -> robustness=0.7
    candidate3 = {
        "representable": True,
        "p_used_src": "implied",
        "p_ext_status": "NO_MARKET"
    }
    robustness3, flags3 = compute_robustness_score(candidate3)
    assert robustness3 == 0.7
    assert flags3 == ["P_EXT_NO_MARKET"]
    
    # Scenario 4: Not representable -> robustness=0.0
    candidate4 = {
        "representable": False,
        "p_used_src": "external",
        "p_ext_status": "OK"
    }
    robustness4, flags4 = compute_robustness_score(candidate4)
    assert robustness4 == 0.0
    assert flags4 == ["NOT_REPRESENTABLE"]
    
    # Scenario 5: External, AUTH_FAIL -> robustness=0.7
    candidate5 = {
        "representable": True,
        "p_used_src": "external",
        "p_ext_status": "AUTH_FAIL"
    }
    robustness5, flags5 = compute_robustness_score(candidate5)
    assert robustness5 == 0.7
    assert flags5 == ["P_EXT_AUTH_FAIL"]


def test_probability_label_consistency():
    """
    Test that p_used (P(event)) is consistently used throughout the system.
    
    This test verifies that the canonical probability field used for EV calculation
    matches what would be displayed as P(event) in the console.
    """
    candidate = {
        "candidate_id": "TEST",
        "underlier": "SPY",
        "regime": "crash",
        "expiry_bucket": "30-60d",
        "cluster_id": "EQUITY",
        "cell_id": "SPY_crash_30-60d",
        "expiry": "2026-03-20",
        "long_strike": 580.0,
        "short_strike": 560.0,
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "ev_per_dollar": 2.0,
        "ev_usd": 200.0,
        "p_profit": 0.046,
        "p_used": 0.046,  # CANONICAL probability
        "p_used_src": "external",
        "p_impl": 0.040,
        "p_ext": 0.046,
        "p_ext_status": "OK",
        # Legacy fields (should match canonical for backwards compatibility)
        "p_event_used": 0.046,
        "p_event": 0.046,
        "representable": True,
        "rank": 1,
    }
    
    # Verify consistency: p_used == p_event_used == p_event
    assert candidate["p_used"] == candidate["p_event_used"]
    assert candidate["p_used"] == candidate["p_event"]
    
    # Verify EV calculation uses p_used
    # EV = p_used * max_gain - (1 - p_used) * debit
    expected_ev = candidate["p_used"] * candidate["max_gain_per_contract"] - \
                  (1 - candidate["p_used"]) * candidate["debit_per_contract"]
    expected_ev_per_dollar = expected_ev / candidate["debit_per_contract"]
    
    # Allow small floating point difference
    assert abs(candidate["ev_per_dollar"] - expected_ev_per_dollar) < 0.01


def test_robustness_stats_in_selection_summary():
    """Test that robustness stats are included in selection summary."""
    candidates = [
        {
            "candidate_id": "A",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 580.0,
            "short_strike": 560.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 2.0,
            "ev_usd": 200.0,
            "p_profit": 0.10,
            "p_used": 0.10,
            "p_used_src": "external",
            "p_impl": 0.08,
            "p_ext": 0.10,
            "p_ext_status": "OK",
            "representable": True,
            "rank": 1,
        },
        {
            "candidate_id": "B",
            "underlier": "QQQ",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "TECH",
            "cell_id": "QQQ_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 480.0,
            "short_strike": 460.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 1.5,
            "ev_usd": 150.0,
            "p_profit": 0.08,
            "p_used": 0.08,
            "p_used_src": "fallback",
            "p_impl": None,
            "p_ext": None,
            "p_ext_status": "NO_MARKET",
            "representable": True,
            "rank": 1,
        },
        {
            "candidate_id": "C",
            "underlier": "IWM",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "SMALL_CAP",
            "cell_id": "IWM_crash_30-60d",
            "expiry": "2026-03-20",
            "long_strike": 220.0,
            "short_strike": 200.0,
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 2000.0,
            "ev_per_dollar": 1.2,
            "ev_usd": 120.0,
            "p_profit": 0.06,
            "p_used": 0.06,
            "p_used_src": "implied",
            "p_impl": 0.06,
            "p_ext": None,
            "p_ext_status": "NO_MARKET",
            "representable": True,
            "rank": 1,
        }
    ]
    
    governors = create_mock_governors()
    positions_view = create_mock_positions_view()
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Check robustness stats in reasons
    assert "robustness_stats" in result.reasons
    stats = result.reasons["robustness_stats"]
    
    assert stats["external_count"] == 1
    assert stats["implied_count"] == 1
    assert stats["fallback_count"] == 1
    assert stats["no_market_count"] == 2  # B and C


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
