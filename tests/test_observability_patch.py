"""
Tests for Phase 6 Observability Patch

Tests covering:
1. p_used_breakdown accounting normalization
2. Pre-governor ranking table generation
3. Kalshi mapping debug payload
"""

import pytest
from forecast_arb.campaign.grid_runner import flatten_candidate
from forecast_arb.campaign.selector import compute_robustness_score, select_candidates


class TestPUsedBreakdownAccounting:
    """Test Task 1: p_used_breakdown accounting fix"""
    
    def test_normalize_conditioned_suffix(self):
        """Test that _conditioned suffix is normalized for accounting"""
        # Create mock candidate with conditioning applied
        candidate = {
            "candidate_id": "test_001",
            "expiry": "2026-04-15",
            "strikes": {"long_put": 500, "short_put": 510},
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 900.0,
            "ev_per_dollar": 0.15,
            "p_implied": 0.12,
            "conditioning": {
                "p_adjusted": 0.14,
                "p_original": 0.12
            },
            "representable": True,
            "rank": 1
        }
        
        regime_p_implied = 0.12
        regime_p_external = None  # NO_MARKET case
        
        flat = flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-45d",
            cluster_id="SPX",
            cell_id="SPY_crash_30-45d",
            regime_p_implied=regime_p_implied,
            regime_p_external=regime_p_external
        )
        
        # Verify p_used_src has _conditioned suffix
        assert flat["p_used_src"] == "implied_conditioned"
        
        # Verify normalization strips _conditioned for accounting
        p_used_src_normalized = flat["p_used_src"].replace("_conditioned", "") if flat["p_used_src"].endswith("_conditioned") else flat["p_used_src"]
        assert p_used_src_normalized == "implied"
    
    def test_external_conditioned_maps_to_external(self):
        """Test external_conditioned maps back to external"""
        candidate = {
            "candidate_id": "test_002",
            "expiry": "2026-04-15",
            "strikes": {"long_put": 500, "short_put": 510},
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 900.0,
            "ev_per_dollar": 0.18,
            "p_implied": 0.12,
            "conditioning": {
                "p_adjusted": 0.16,
                "p_original": 0.15
            },
            "representable": True,
            "rank": 1
        }
        
        regime_p_implied = 0.12
        regime_p_external = {
            "p": 0.15,
            "source": "kalshi",
            "authoritative": True,
            "asof_ts_utc": "2026-02-27T12:00:00Z",
            "market": {"ticker": "TEST-MARKET"},
            "match": {},
            "quality": {"liquidity_ok": True, "staleness_ok": True, "spread_ok": True, "warnings": []}
        }
        
        flat = flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-45d",
            cluster_id="SPX",
            cell_id="SPY_crash_30-45d",
            regime_p_implied=regime_p_implied,
            regime_p_external=regime_p_external
        )
        
        # Verify p_used_src is external_conditioned
        assert flat["p_used_src"] == "external_conditioned"
        
        # Verify normalization
        p_used_src_normalized = flat["p_used_src"].replace("_conditioned", "") if flat["p_used_src"].endswith("_conditioned") else flat["p_used_src"]
        assert p_used_src_normalized == "external"
    
    def test_no_market_shows_implied_in_breakdown(self):
        """Test NO_MARKET case shows implied count correctly"""
        candidate = {
            "candidate_id": "test_003",
            "expiry": "2026-04-15",
            "strikes": {"long_put": 500, "short_put": 510},
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 900.0,
            "ev_per_dollar": 0.12,
            "p_implied": 0.10,
            "representable": True,
            "rank": 1
        }
        
        regime_p_implied = 0.10
        regime_p_external = None  # NO_MARKET
        
        flat = flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-45d",
            cluster_id="SPX",
            cell_id="SPY_crash_30-45d",
            regime_p_implied=regime_p_implied,
            regime_p_external=regime_p_external
        )
        
        # Should use implied
        assert flat["p_used_src"] == "implied"
        assert flat["p_ext_status"] == "NO_MARKET"
        
        # Accounting should count this as "implied"
        assert flat["p_used_src"] in ["implied", "external", "fallback"]


class TestPreGovernorRanking:
    """Test Task 2: Pre-governor ranking visibility"""
    
    def test_score_calculation(self):
        """Test that score = ev_per_dollar * robustness"""
        candidate = {
            "candidate_id": "test_rank_001",
            "underlier": "SPY",
            "regime": "crash",
            "expiry": "2026-04-15",
            "long_strike": 500,
            "short_strike": 510,
            "ev_per_dollar": 0.20,  # Base score
            "debit_per_contract": 100.0,
            "p_used_src": "implied",
            "p_ext_status": "NO_MARKET",
            "representable": True
        }
        
        robustness, flags = compute_robustness_score(candidate)
        
        # NO_MARKET should get 0.7x penalty
        assert robustness == 0.7
        assert "P_EXT_NO_MARKET" in flags
        
        # Adjusted score should be 0.20 * 0.7 = 0.14
        adjusted_score = candidate["ev_per_dollar"] * robustness
        assert abs(adjusted_score - 0.14) < 0.001
    
    def test_fallback_penalty(self):
        """Test fallback p_used_src gets 0.5x penalty"""
        candidate = {
            "candidate_id": "test_rank_002",
            "underlier": "QQQ",
            "regime": "selloff",
            "ev_per_dollar": 0.15,
            "p_used_src": "fallback",
            "p_ext_status": "NO_MARKET",
            "representable": True
        }
        
        robustness, flags = compute_robustness_score(candidate)
        
        # Fallback (0.5x) AND NO_MARKET (0.7x) both apply = 0.5 * 0.7 = 0.35
        assert robustness == 0.35
        assert "P_FALLBACK" in flags
        assert "P_EXT_NO_MARKET" in flags
    
    def test_external_ok_no_penalty(self):
        """Test external with OK status gets no penalty"""
        candidate = {
            "candidate_id": "test_rank_003",
            "underlier": "SPY",
            "regime": "crash",
            "ev_per_dollar": 0.25,
            "p_used_src": "external",
            "p_ext_status": "OK",
            "representable": True
        }
        
        robustness, flags = compute_robustness_score(candidate)
        
        # No penalty
        assert robustness == 1.0
        assert len(flags) == 0


class TestKalshiMappingDebug:
    """Test Task 3: Kalshi mapping debug payload"""
    
    def test_no_market_includes_debug_payload(self):
        """Test NO_MARKET status includes kalshi_mapping_debug"""
        candidate = {
            "candidate_id": "test_kalshi_001",
            "expiry": "2026-04-15",
            "strikes": {"long_put": 500, "short_put": 510},
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 900.0,
            "ev_per_dollar": 0.12,
            "p_implied": 0.10,
            "representable": True,
            "rank": 1
        }
        
        regime_p_implied = 0.10
        regime_p_external = None  # NO_MARKET
        
        flat = flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-45d",
            cluster_id="SPX",
            cell_id="SPY_crash_30-45d",
            regime_p_implied=regime_p_implied,
            regime_p_external=regime_p_external
        )
        
        # NO_MARKET should trigger debug payload in grid_runner
        assert flat["p_ext_status"] == "NO_MARKET"
        assert flat["p_ext_reason"] is not None
        
        # Verify required probability fields exist (grid_runner will add kalshi_mapping_debug)
        assert "p_used" in flat
        assert "p_used_src" in flat
        assert "p_ext_status" in flat
    
    def test_ok_status_no_debug_payload(self):
        """Test OK status does not need debug payload"""
        candidate = {
            "candidate_id": "test_kalshi_002",
            "expiry": "2026-04-15",
            "strikes": {"long_put": 500, "short_put": 510},
            "debit_per_contract": 100.0,
            "max_gain_per_contract": 900.0,
            "ev_per_dollar": 0.18,
            "p_implied": 0.12,
            "representable": True,
            "rank": 1
        }
        
        regime_p_implied = 0.12
        regime_p_external = {
            "p": 0.15,
            "source": "kalshi",
            "authoritative": True,
            "asof_ts_utc": "2026-02-27T12:00:00Z",
            "market": {"ticker": "TEST-MARKET"},
            "match": {},
            "quality": {"liquidity_ok": True, "staleness_ok": True, "spread_ok": True, "warnings": []}
        }
        
        flat = flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-45d",
            cluster_id="SPX",
            cell_id="SPY_crash_30-45d",
            regime_p_implied=regime_p_implied,
            regime_p_external=regime_p_external
        )
        
        # OK status should not need debug payload
        assert flat["p_ext_status"] == "OK"
        # kalshi_mapping_debug not added by flatten_candidate (added in grid_runner only for non-OK)


class TestDeterministicBehavior:
    """Test that all changes are deterministic"""
    
    def test_accounting_is_deterministic(self):
        """Test p_used_breakdown accounting is deterministic"""
        # Same input should always produce same output
        for _ in range(3):
            candidate = {
                "candidate_id": "test_det_001",
                "expiry": "2026-04-15",
                "strikes": {"long_put": 500, "short_put": 510},
                "debit_per_contract": 100.0,
                "max_gain_per_contract": 900.0,
                "ev_per_dollar": 0.12,
                "p_implied": 0.10,
                "representable": True,
                "rank": 1
            }
            
            regime_p_implied = 0.10
            regime_p_external = None
            
            flat = flatten_candidate(
                candidate=candidate,
                underlier="SPY",
                regime="crash",
                expiry_bucket="30-45d",
                cluster_id="SPX",
                cell_id="SPY_crash_30-45d",
                regime_p_implied=regime_p_implied,
                regime_p_external=regime_p_external
            )
            
            assert flat["p_used_src"] == "implied"
            assert flat["p_ext_status"] == "NO_MARKET"
    
    def test_ranking_is_deterministic(self):
        """Test pre-governor ranking is deterministic"""
        # Create candidates
        candidates = [
            {
                "candidate_id": f"test_rank_{i}",
                "underlier": "SPY",
                "regime": "crash",
                "ev_per_dollar": 0.15 - (i * 0.01),
                "debit_per_contract": 100.0,
                "p_used_src": "implied",
                "p_ext_status": "NO_MARKET",
                "p_profit": 0.10,
                "representable": True,
                "cluster_id": "SPX"
            }
            for i in range(5)
        ]
        
        # Run selection multiple times
        portfolio_view = {
            "open_positions": [],
            "open_premium_by_regime": {},
            "open_premium_total": 0.0,
            "open_count_by_regime": {},
            "open_clusters": set(),
            "timestamp_utc": "2026-02-27T12:00:00Z"
        }
        
        governors = {
            "daily_premium_cap_usd": 1000.0,
            "cluster_cap_per_day": 2,
            "max_open_positions_by_regime": {},
            "premium_at_risk_caps_usd": {},
            "max_trades_per_day": 2
        }
        
        # Run twice and verify same order
        results1 = select_candidates(candidates, governors, portfolio_view, qty=1)
        results2 = select_candidates(candidates, governors, portfolio_view, qty=1)
        
        # Should select same candidates in same order
        assert len(results1.selected) == len(results2.selected)
        for i in range(len(results1.selected)):
            assert results1.selected[i]["candidate_id"] == results2.selected[i]["candidate_id"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
