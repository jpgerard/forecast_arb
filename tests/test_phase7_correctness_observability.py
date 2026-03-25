"""
Phase 7: Correctness & Observability Tests

Tests for:
1. Kalshi series sanity check when returned_markets=0
2. Kalshi filter/retrieval separation in diagnostics
3. IBKR contract hygiene validation
4. Pre-governor ranking table output
5. End-to-end correctness invariants
"""

import json
import pytest
from unittest.mock import Mock, MagicMock, patch
from forecast_arb.kalshi.multi_series_adapter import fetch_all_series_markets, kalshi_multi_series_search
from forecast_arb.structuring.contract_validation import (
    extract_qualified_strikes_from_snapshot,
    validate_candidate_strikes,
    filter_candidates_by_contract_validity,
    InvalidContractError
)
from forecast_arb.campaign.selector import select_candidates, compute_robustness_score


class TestKalshiSeriesSanityCheck:
    """Test Task 1: Kalshi series sanity check when returned_markets=0."""
    
    def test_series_empty_triggers_diagnostic(self):
        """When filtered query returns 0 markets, should call unfiltered query."""
        mock_client = Mock()
        
        # First call (filtered): returns empty
        # Second call (unfiltered): returns 5 markets 
        mock_client.list_markets = Mock(side_effect=[
            [],  # Filtered query
            [  # Unfiltered diagnostic query
                {"ticker": "KXNDX-24MAR01-5800", "title": "S&P 500", "status": "closed", "close_time": "2024-03-01"},
                {"ticker": "KXNDX-24MAR02-5850", "title": "S&P 500", "status": "closed", "close_time": "2024-03-02"},
            ]
        ])
        
        result = fetch_all_series_markets(mock_client, series_list=["KXNDX"], status="open")
        
        # Should have called list_markets twice
        assert mock_client.list_markets.call_count == 2
        
        # Should have diagnostics
        assert "_diagnostics" in result
        assert "KXNDX" in result["_diagnostics"]
        
        diag = result["_diagnostics"]["KXNDX"]
        assert diag["series_exists"] == True
        assert diag["series_market_count_total"] == 2
        assert len(diag["series_sample_markets"]) == 2
        assert diag["failure_reason"] == "FILTER_SHAPE_MISMATCH"
    
    def test_series_does_not_exist(self):
        """When series truly doesn't exist, should mark as SERIES_EMPTY_OR_ACCESS."""
        mock_client = Mock()
        
        # Both calls return empty
        mock_client.list_markets = Mock(side_effect=[[], []])
        
        result = fetch_all_series_markets(mock_client, series_list=["INVALID"], status="open")
        
        assert "_diagnostics" in result
        assert "INVALID" in result["_diagnostics"]
        
        diag = result["_diagnostics"]["INVALID"]
        assert diag["series_exists"] == False
        assert diag["series_market_count_total"] == 0
        assert diag["failure_reason"] == "SERIES_EMPTY_OR_ACCESS"
    
    def test_diagnostic_query_fails(self):
        """When diagnostic query fails, should record error."""
        mock_client = Mock()
        
        # First call succeeds but empty, second call raises exception
        mock_client.list_markets = Mock(side_effect=[[], Exception("API Error")])
        
        result = fetch_all_series_markets(mock_client, series_list=["KXNDX"], status="open")
        
        assert "_diagnostics" in result
        diag = result["_diagnostics"]["KXNDX"]
        assert diag["failure_reason"] == "DIAGNOSTIC_QUERY_FAILED"
        assert "error" in diag


class TestKalshiDiagnosticsSeparation:
    """Test Task 2: Separate filters from retrieval in diagnostics."""
    
    def test_diagnostics_includes_filters_and_retrieval(self):
        """Diagnostics should separate filter params from retrieval results."""
        mock_client = Mock()
        mock_client.list_markets = Mock(return_value=[])
        
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.10,
            "date": "2024-04-15",
            "comparator": "below"
        }
        
        result = kalshi_multi_series_search(
            event_definition=event_def,
            client=mock_client,
            spot_spx=6000.0,
            horizon_days=45,
            allow_proxy=False,
            max_mapping_error=0.05
        )
        
        assert "diagnostics" in result
        diag = result["diagnostics"]
        
        # Check filters block
        assert "filters" in diag
        filters = diag["filters"]
        assert "target_level" in filters
        assert filters["target_level"] == pytest.approx(5400.0)  # 6000 * 0.90
        assert filters["comparator"] == "below"
        assert filters["max_mapping_error"] == 0.05
        assert filters["status_tried"] == "open"
        
        # Check retrieval block
        assert "retrieval" in diag
        retrieval = diag["retrieval"]
        assert "series_tried" in retrieval
        assert "returned_markets_filtered" in retrieval
        assert "markets_by_series" in retrieval


class TestIBKRContractHygiene:
    """Test Task 3: IBKR contract validation prevents invalid strikes."""
    
    def test_extract_qualified_strikes(self):
        """Should extract strikes from snapshot expiry."""
        snapshot = {
            "expiries": {
                "20260320": {
                    "puts": [
                        {"strike": 570.0, "bid": 1.5, "ask": 1.7},
                        {"strike": 580.0, "bid": 2.0, "ask": 2.2},
                        {"strike": 590.0, "bid": 2.5, "ask": 2.7},
                    ]
                }
            }
        }
        
        strikes = extract_qualified_strikes_from_snapshot(snapshot, "20260320", "P")
        
        assert strikes == {570.0, 580.0, 590.0}
    
    def test_validate_candidate_with_valid_strikes(self):
        """Candidate with valid strikes should pass."""
        snapshot = {
            "expiries": {
                "20260320": {
                    "puts": [
                        {"strike": 570.0, "bid": 1.5, "ask": 1.7},
                        {"strike": 590.0, "bid": 2.5, "ask": 2.7},
                    ]
                }
            }
        }
        
        candidate = {
            "candidate_id": "test_cand",
            "underlier": "SPY",
            "expiry": "20260320",
            "long_strike": 590.0,
            "short_strike": 570.0
        }
        
        is_valid, warnings = validate_candidate_strikes(candidate, snapshot)
        
        assert is_valid == True
        assert len(warnings) == 0
    
    def test_validate_candidate_with_invalid_long_strike(self):
        """Candidate with invalid long strike should fail."""
        snapshot = {
            "expiries": {
                "20260320": {
                    "puts": [
                        {"strike": 570.0, "bid": 1.5, "ask": 1.7},
                        {"strike": 580.0, "bid": 2.0, "ask": 2.2},
                    ]
                }
            }
        }
        
        candidate = {
            "candidate_id": "test_cand",
            "underlier": "SPY",
            "expiry": "20260320",
            "long_strike": 609.78,  # Invalid strike not in snapshot
            "short_strike": 570.0
        }
        
        is_valid, warnings = validate_candidate_strikes(candidate, snapshot)
        
        assert is_valid == False
        assert len(warnings) > 0
        assert "609.78" in warnings[0]
    
    def test_filter_candidates_marks_invalid_as_non_representable(self):
        """Filter should mark invalid candidates as non-representable."""
        snapshot = {
            "expiries": {
                "20260320": {
                    "puts": [
                        {"strike": 570.0, "bid": 1.5, "ask": 1.7},
                        {"strike": 590.0, "bid": 2.5, "ask": 2.7},
                    ]
                }
            }
        }
        
        candidates = [
            {
                "candidate_id": "valid_cand",
                "expiry": "20260320",
                "long_strike": 590.0,
                "short_strike": 570.0
            },
            {
                "candidate_id": "invalid_cand",
                "expiry": "20260320",
                "long_strike": 609.78,  # Invalid
                "short_strike": 570.0
            }
        ]
        
        valid, invalid = filter_candidates_by_contract_validity(candidates, snapshot)
        
        assert len(valid) == 1
        assert valid[0]["candidate_id"] == "valid_cand"
        
        assert len(invalid) == 1
        assert invalid[0]["candidate_id"] == "invalid_cand"
        assert invalid[0]["representable"] == False
        assert invalid[0]["representability_reason"] == "INVALID_CONTRACT_STRIKES"


class TestPreGovernorRankingTable:
    """Test Task 4: Pre-governor ranking table is displayed."""
    
    def test_robustness_score_fallback_penalty(self):
        """Fallback p_used_src should get 0.5x multiplier."""
        candidate = {
            "representable": True,
            "p_used_src": "fallback",
            "p_ext_status": "OK"
        }
        
        robustness, flags = compute_robustness_score(candidate)
        
        assert robustness == 0.5
        assert "P_FALLBACK" in flags
    
    def test_robustness_score_no_market_penalty(self):
        """NO_MARKET p_ext_status should get 0.7x multiplier."""
        candidate = {
            "representable": True,
            "p_used_src": "external",
            "p_ext_status": "NO_MARKET"
        }
        
        robustness, flags = compute_robustness_score(candidate)
        
        assert robustness == 0.7
        assert "P_EXT_NO_MARKET" in flags
    
    def test_robustness_score_combined_penalties(self):
        """Fallback + NO_MARKET should multiply: 0.5 * 0.7 = 0.35."""
        candidate = {
            "representable": True,
            "p_used_src": "fallback",
            "p_ext_status": "NO_MARKET"
        }
        
        robustness, flags = compute_robustness_score(candidate)
        
        assert robustness == pytest.approx(0.35)
        assert "P_FALLBACK" in flags
        assert "P_EXT_NO_MARKET" in flags
    
    def test_select_candidates_computes_robustness(self, caplog):
        """Selection should compute robustness and display pre-governor ranking."""
        candidates = [
            {
                "candidate_id": "spy_1",
                "representable": True,
                "underlier": "SPY",
                "regime": "crash",
                "expiry": "20260320",
                "long_strike": 590,
                "short_strike": 570,
                "cluster_id": "SPY_20260320_crash",
                "debit_per_contract": 10.0,
                "ev_per_dollar": 0.25,
                "p_used": 0.15,
                "p_used_src": "external",
                "p_ext_status": "OK",
                "p_profit": 0.10
            },
            {
                "candidate_id": "qqq_1",
                "representable": True,
                "underlier": "QQQ",
                "regime": "crash",
                "expiry": "20260320",
                "long_strike": 490,
                "short_strike": 470,
                "cluster_id": "QQQ_20260320_crash",
                "debit_per_contract": 8.0,
                "ev_per_dollar": 0.30,
                "p_used": 0.12,
                "p_used_src": "fallback",  # Penalized
                "p_ext_status": "NO_MARKET",  # Penalized
                "p_profit": 0.08
            }
        ]
        
        governors = {
            "daily_premium_cap_usd": 1000.0,
            "cluster_cap_per_day": 1,
            "max_trades_per_day": 2
        }
        
        positions_view = {
            "open_positions": [],
            "open_premium_by_regime": {},
            "open_premium_total": 0.0,
            "open_count_by_regime": {},
            "open_clusters": set()
        }
        
        import logging
        caplog.set_level(logging.INFO)
        
        result = select_candidates(
            candidates_flat=candidates,
            governors=governors,
            positions_view=positions_view,
            qty=1,
            scoring_method="ev_per_dollar"
        )
        
        # Check that pre-governor ranking table was logged
        log_text = caplog.text
        assert "PRE-GOVERNOR RANKING" in log_text
        assert "SPY" in log_text
        assert "QQQ" in log_text
        
        # Check robustness was computed
        assert result.selected[0].get("robustness") is not None
        
        # SPY should rank higher (0.25 * 1.0 = 0.25) than QQQ (0.30 * 0.35 = 0.105)
        # So SPY should be selected first
        assert result.selected[0]["underlier"] == "SPY"


class TestEndToEndInvariants:
    """Test end-to-end correctness invariants."""
    
    def test_no_candidate_without_canonical_ev(self):
        """Selector must fail if candidate missing canonical EV field."""
        candidates = [
            {
                "candidate_id": "bad_cand",
                "representable": True,
                "regime": "crash",
                "cluster_id": "test",
                "debit_per_contract": 10.0,
                # Missing ev_per_dollar!
            }
        ]
        
        governors = {"daily_premium_cap_usd": 1000.0}
        positions_view = {
            "open_positions": [],
            "open_premium_by_regime": {},
            "open_premium_total": 0.0,
            "open_count_by_regime": {},
            "open_clusters": set()
        }
        
        with pytest.raises(ValueError, match="missing required canonical field"):
            select_candidates(
                candidates_flat=candidates,
                governors=governors,
                positions_view=positions_view,
                scoring_method="ev_per_dollar"
            )
    
    def test_kalshi_diagnostics_always_present_when_no_match(self):
        """When Kalshi returns no match, diagnostics must be present."""
        mock_client = Mock()
        mock_client.list_markets = Mock(return_value=[])
        
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.10
        }
        
        result = kalshi_multi_series_search(
            event_definition=event_def,
            client=mock_client,
            spot_spx=6000.0,
            allow_proxy=False
        )
        
        assert result["exact_match"] == False
        assert result["p_external"] is None
        assert "diagnostics" in result
        assert "filters" in result["diagnostics"]
        assert "retrieval" in result["diagnostics"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
