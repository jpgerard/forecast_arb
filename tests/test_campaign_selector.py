"""
Tests for Campaign Selector - Portfolio-Aware Candidate Selection

Tests governor enforcement without requiring IBKR connection.
"""

import json
import pytest
from pathlib import Path
from forecast_arb.campaign.selector import select_candidates, compute_candidate_premium_usd
from forecast_arb.portfolio.positions_view import load_positions_view


def test_cluster_cap_enforcement():
    """Test that cluster cap prevents selecting >1 from same cluster per day."""
    
    # Two candidates from same cluster (US_INDEX)
    candidates = [
        {
            "candidate_id": "SPY_crash_1",
            "underlier": "SPY",
            "regime": "crash",
            "cluster_id": "US_INDEX",
            "expiry": "20260402",
            "long_strike": 580,
            "short_strike": 560,
            "debit_per_contract": 49.0,
            "ev_per_dollar": 25.0,
            "prob_profit": 0.70,
            "max_gain_per_contract": 1951.0,
            "representable": True,
            "rank": 1
        },
        {
            "candidate_id": "QQQ_crash_1",
            "underlier": "QQQ",
            "regime": "crash",
            "cluster_id": "US_INDEX",  # Same cluster!
            "expiry": "20260402",
            "long_strike": 420,
            "short_strike": 400,
            "debit_per_contract": 45.0,
            "ev_per_dollar": 24.0,  # Slightly lower EV/$
            "prob_profit": 0.68,
            "max_gain_per_contract": 1955.0,
            "representable": True,
            "rank": 1
        }
    ]
    
    governors = {
        "daily_premium_cap_usd": 5000.0,
        "cluster_cap_per_day": 1,  # Only 1 per cluster
        "max_open_positions_by_regime": {"crash": 5},
        "premium_at_risk_caps_usd": {"crash": 10000, "total": 10000},
        "max_trades_per_day": 2
    }
    
    positions_view = {
        "open_positions": [],
        "pending_orders": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set()
    }
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Should only select 1 (the higher EV/$ one = SPY)
    assert len(result.selected) == 1
    assert result.selected[0]["underlier"] == "SPY"
    assert result.selected[0]["ev_per_dollar"] == 25.0
    
    # Second should be rejected for cluster cap
    assert len(result.rejected) == 1
    assert result.rejected[0]["blocked_by"] == "cluster_cap"


def test_daily_premium_cap_enforcement():
    """Test that daily premium cap blocks selections."""
    
    candidates = [
        {
            "candidate_id": "candidate_1",
            "underlier": "SPY",
            "regime": "crash",
            "cluster_id": "US_INDEX",
            "expiry": "20260402",
            "long_strike": 580,
            "short_strike": 560,
            "debit_per_contract": 800.0,  # High premium
            "ev_per_dollar": 25.0,
            "prob_profit": 0.70,
            "max_gain_per_contract": 20000.0,
            "representable": True,
            "rank": 1
        },
        {
            "candidate_id": "candidate_2",
            "underlier": "QQQ",
            "regime": "crash",
            "cluster_id": "TECH",
            "expiry": "20260402",
            "long_strike": 420,
            "short_strike": 400,
            "debit_per_contract": 600.0,  # Would exceed cap
            "ev_per_dollar": 24.0,
            "prob_profit": 0.68,
            "max_gain_per_contract": 20000.0,
            "representable": True,
            "rank": 1
        }
    ]
    
    governors = {
        "daily_premium_cap_usd": 1250.0,  # Total cap
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {"crash": 5},
        "premium_at_risk_caps_usd": {"crash": 10000, "total": 10000},
        "max_trades_per_day": 2
    }
    
    positions_view = {
        "open_positions": [],
        "pending_orders": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set()
    }
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Should only select 1 (first one at $800)
    assert len(result.selected) == 1
    assert result.selected[0]["candidate_id"] == "candidate_1"
    
    # Second blocked by daily premium cap
    assert len(result.rejected) == 1
    assert result.rejected[0]["blocked_by"] == "daily_premium_cap"


def test_open_premium_caps_by_regime():
    """Test that open premium caps by regime are enforced."""
    
    candidates = [
        {
            "candidate_id": "candidate_1",
            "underlier": "SPY",
            "regime": "crash",
            "cluster_id": "US_INDEX",
            "expiry": "20260402",
            "long_strike": 580,
            "short_strike": 560,
            "debit_per_contract": 500.0,
            "ev_per_dollar": 25.0,
            "prob_profit": 0.70,
            "max_gain_per_contract": 10000.0,
            "representable": True,
            "rank": 1
        }
    ]
    
    governors = {
        "daily_premium_cap_usd": 5000.0,
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {"crash": 5},
        "premium_at_risk_caps_usd": {"crash": 3000, "total": 10000},  # Crash cap is $3000
        "max_trades_per_day": 2
    }
    
    # Simulate existing positions with $2600 in crash regime
    positions_view = {
        "open_positions": [
            {"regime": "crash", "entry_price": 800},
            {"regime": "crash", "entry_price": 900},
            {"regime": "crash", "entry_price": 900},
        ],
        "pending_orders": [],
        "open_premium_by_regime": {"crash": 2600.0},  # Already at $2600
        "open_premium_total": 2600.0,
        "open_count_by_regime": {"crash": 3},
        "open_clusters": {"US_INDEX"}
    }
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Should reject - would push crash over $3000 cap
    assert len(result.selected) == 0
    assert len(result.rejected) == 1
    assert result.rejected[0]["blocked_by"] == "regime_premium_cap"


def test_deterministic_selection_ordering():
    """Test that selection is deterministic (same input = same output)."""
    
    candidates = [
        {
            "candidate_id": "candidate_B",
            "underlier": "QQQ",
            "regime": "crash",
            "cluster_id": "TECH",
            "expiry": "20260402",
            "long_strike": 420,
            "short_strike": 400,
            "debit_per_contract": 45.0,
            "ev_per_dollar": 25.0,  # Same EV/$
            "prob_profit": 0.68,  # Lower prob_profit (tiebreaker)
            "max_gain_per_contract": 1955.0,
            "representable": True,
            "rank": 1
        },
        {
            "candidate_id": "candidate_A",
            "underlier": "SPY",
            "regime": "crash",
            "cluster_id": "US_INDEX",
           "expiry": "20260402",
            "long_strike": 580,
            "short_strike": 560,
            "debit_per_contract": 49.0,
            "ev_per_dollar": 25.0,  # Same EV/$
            "prob_profit": 0.70,  # Higher prob_profit (wins tiebreaker)
            "max_gain_per_contract": 1951.0,
            "representable": True,
            "rank": 1
        }
    ]
    
    governors = {
        "daily_premium_cap_usd": 5000.0,
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {"crash": 5},
        "premium_at_risk_caps_usd": {"crash": 10000, "total": 10000},
        "max_trades_per_day": 1  # Only select 1
    }
    
    positions_view = {
        "open_positions": [],
        "pending_orders": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set()
    }
    
    # Run selection twice with same inputs
    result1 = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    result2 = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Should be deterministic
    assert len(result1.selected) == 1
    assert len(result2.selected) == 1
    assert result1.selected[0]["candidate_id"] == result2.selected[0]["candidate_id"]
    
    # Should pick candidate_A (higher prob_profit as tiebreaker)
    assert result1.selected[0]["candidate_id"] == "candidate_A"


def test_no_representable_candidates():
    """Test behavior when no representable candidates available."""
    
    candidates = [
        {
            "candidate_id": "candidate_1",
            "underlier": "SPY",
            "regime": "crash",
            "cluster_id": "US_INDEX",
            "expiry": "20260402",
            "long_strike": 580,
            "short_strike": 560,
            "debit_per_contract": 49.0,
            "ev_per_dollar": 25.0,
            "prob_profit": 0.70,
            "max_gain_per_contract": 1951.0,
            "representable": False,  # NOT representable
            "rank": 1
        }
    ]
    
    governors = {
        "daily_premium_cap_usd": 5000.0,
        "cluster_cap_per_day": 10,
        "max_open_positions_by_regime": {"crash": 5},
        "premium_at_risk_caps_usd": {"crash": 10000, "total": 10000},
        "max_trades_per_day": 2
    }
    
    positions_view = {
        "open_positions": [],
        "pending_orders": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set()
    }
    
    result = select_candidates(
        candidates_flat=candidates,
        governors=governors,
        positions_view=positions_view,
        qty=1,
        scoring_method="ev_per_dollar"
    )
    
    # Should select nothing
    assert len(result.selected) == 0
    assert result.reasons.get("no_representable_candidates") == True


def test_premium_usd_convention():
    """Test that premium_usd convention is correct (no ×100 multiplier)."""
    
    candidate = {
        "debit_per_contract": 49.50
    }
    
    # Convention: premium_usd = debit_per_contract * qty (no ×100)
    premium = compute_candidate_premium_usd(candidate, qty=1)
    assert premium == 49.50  # Not 4950
    
    premium_qty2 = compute_candidate_premium_usd(candidate, qty=2)
    assert premium_qty2 == 99.00  # 49.50 * 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
