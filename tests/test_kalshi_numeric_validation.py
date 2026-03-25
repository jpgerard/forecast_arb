"""
Tests for Kalshi numeric validation and proxy computation.

Ensures that invalid numeric values (complex numbers, invalid probabilities)
are caught and handled gracefully without crashing.
"""

import pytest
from datetime import date, datetime, timezone
from unittest.mock import Mock, MagicMock, patch
from forecast_arb.kalshi.numeric import (
    as_float,
    as_probability,
    safe_hazard_scale
)
from forecast_arb.kalshi.multi_series_adapter import (
    compute_yearly_min_proxy,
    kalshi_multi_series_search,
    ProxyProbability
)


class TestNumericHygiene:
    """Test numeric conversion and validation utilities."""
    
    def test_as_float_valid_types(self):
        """Test as_float with valid numeric types."""
        assert as_float(5, "test") == 5.0
        assert as_float(3.14, "test") == 3.14
        assert as_float(True, "test") == 1.0
        assert as_float(False, "test") == 0.0
    
    def test_as_float_none_allowed(self):
        """Test as_float with None when allowed."""
        assert as_float(None, "test", allow_none=True) is None
    
    def test_as_float_none_not_allowed(self):
        """Test as_float raises when None not allowed."""
        with pytest.raises(ValueError, match="is None"):
            as_float(None, "test", allow_none=False)
    
    def test_as_float_complex_negligible_imag(self):
        """Test as_float with complex number with negligible imaginary part."""
        # Should succeed and use real part
        result = as_float(3.14 + 1e-12j, "test")
        assert abs(result - 3.14) < 1e-6
    
    def test_as_float_complex_significant_imag(self):
        """Test as_float raises on complex number with significant imaginary part."""
        with pytest.raises(ValueError, match="is complex"):
            as_float(3.14 + 2.5j, "test")
    
    def test_as_float_string_convertible(self):
        """Test as_float with string that can be converted."""
        assert as_float("3.14", "test") == 3.14
    
    def test_as_float_invalid_string(self):
        """Test as_float raises on invalid string."""
        with pytest.raises(ValueError, match="Cannot convert"):
            as_float("not_a_number", "test")
    
    def test_as_probability_valid_range(self):
        """Test as_probability with valid probabilities."""
        assert as_probability(0.0, "test") == 0.0
        assert as_probability(0.5, "test") == 0.5
        assert as_probability(1.0, "test") == 1.0
    
    def test_as_probability_below_zero(self):
        """Test as_probability raises on negative value."""
        with pytest.raises(ValueError, match="not in valid probability range"):
            as_probability(-0.1, "test")
    
    def test_as_probability_above_one(self):
        """Test as_probability raises on value > 1."""
        with pytest.raises(ValueError, match="not in valid probability range"):
            as_probability(1.5, "test")
    
    def test_safe_hazard_scale_valid(self):
        """Test safe_hazard_scale with valid inputs."""
        # p_annual = 0.25, horizon = 45 days
        result = safe_hazard_scale(0.25, 45)
        assert 0.0 < result < 0.25  # Should be less than annual
        assert isinstance(result, float)
    
    def test_safe_hazard_scale_edge_cases(self):
        """Test safe_hazard_scale edge cases."""
        assert safe_hazard_scale(0.0, 45) == 0.0
        assert safe_hazard_scale(1.0, 45) == 1.0
    
    def test_safe_hazard_scale_invalid_probability(self):
        """Test safe_hazard_scale raises on invalid probability."""
        with pytest.raises(ValueError, match="not in valid probability range"):
            safe_hazard_scale(1.5, 45)  # > 1 would create negative base
    
    def test_safe_hazard_scale_invalid_horizon(self):
        """Test safe_hazard_scale raises on invalid horizon."""
        with pytest.raises(ValueError, match="must be positive"):
            safe_hazard_scale(0.25, -10)


class TestProxyComputationRobustness:
    """Test proxy computation handles invalid data gracefully."""
    
    def test_compute_yearly_min_proxy_invalid_pricing_above_one(self):
        """Test that proxy rejects yes_bid/yes_ask > 100 cents."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 3, 15)
        }
        
        # Mock market with invalid pricing (> 100 cents = > 1.0 probability)
        mock_market = {
            "ticker": "KXINXMINY-TEST",
            "title": "SPX yearly min below 4000",
            "yes_bid": 120,  # INVALID: 120 cents > 100 = 1.2 probability
            "yes_ask": 150   # INVALID: 150 cents > 100 = 1.5 probability
        }
        
        markets_by_series = {
            "KXINXMINY": [mock_market]
        }
        
        # Should return None (reject invalid data)
        result = compute_yearly_min_proxy(
            event_definition=event_def,
            spot_spx=5000.0,
            markets_by_series=markets_by_series,
            horizon_days=45
        )
        
        assert result is None  # Should reject, not crash
    
    def test_compute_yearly_min_proxy_invalid_pricing_negative(self):
        """Test that proxy rejects negative yes_bid/yes_ask."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 3, 15)
        }
        
        mock_market = {
            "ticker": "KXINXMINY-TEST",
            "title": "SPX yearly min below 4000",
            "yes_bid": -0.1,  # INVALID: < 0
            "yes_ask": 0.5
        }
        
        markets_by_series = {
            "KXINXMINY": [mock_market]
        }
        
        result = compute_yearly_min_proxy(
            event_definition=event_def,
            spot_spx=5000.0,
            markets_by_series=markets_by_series,
            horizon_days=45
        )
        
        assert result is None  # Should reject, not crash
    
    def test_compute_yearly_min_proxy_complex_number_from_bad_math(self):
        """Test that proxy doesn't crash if hazard scaling somehow produces complex."""
        # This test ensures the numeric validation catches any edge case
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 3, 15)
        }
        
        # Edge case: probability exactly at 1.0 (100 cents)
        mock_market = {
            "ticker": "KXINXMINY-TEST",
            "title": "SPX yearly min below 4000",
            "yes_bid": 100,  # 100 cents = 1.0 probability
            "yes_ask": 100   # 100 cents = 1.0 probability
        }
        
        markets_by_series = {
            "KXINXMINY": [mock_market]
        }
        
        # Should handle gracefully (result is 1.0)
        result = compute_yearly_min_proxy(
            event_definition=event_def,
            spot_spx=5000.0,
            markets_by_series=markets_by_series,
            horizon_days=45
        )
        
        # Should succeed with p=1.0 (but clamped to 0.99 for proxy)
        assert result is not None
        assert result.p_external_proxy == 0.99  # Clamped max
    
    def test_compute_yearly_min_proxy_valid_case(self):
        """Test that proxy works correctly with valid data."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 3, 15)
        }
        
        # Valid market with reasonable pricing
        mock_market = {
            "ticker": "KXINXMINY-TEST",
            "title": "SPX yearly min below 4250",
            "yes_bid": 0.25,
            "yes_ask": 0.30
        }
        
        markets_by_series = {
            "KXINXMINY": [mock_market]
        }
        
        result = compute_yearly_min_proxy(
            event_definition=event_def,
            spot_spx=5000.0,  # Target = 5000 * 0.85 = 4250
            markets_by_series=markets_by_series,
            horizon_days=45
        )
        
        # Should succeed
        assert result is not None
        assert isinstance(result, ProxyProbability)
        assert 0.01 <= result.p_external_proxy <= 0.99
        assert result.proxy_method == "yearly_min_hazard_scale"
        assert result.proxy_series == "KXINXMINY"
        assert result.confidence == 0.35
        assert result.proxy_horizon_days == 45
    
    def test_compute_yearly_min_proxy_no_pricing_data(self):
        """Test that proxy returns None when pricing data missing."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 3, 15)
        }
        
        # Market without pricing data
        mock_market = {
            "ticker": "KXINXMINY-TEST",
            "title": "SPX yearly min below 4250",
            # Missing yes_bid and yes_ask
        }
        
        markets_by_series = {
            "KXINXMINY": [mock_market]
        }
        
        result = compute_yearly_min_proxy(
            event_definition=event_def,
            spot_spx=5000.0,
            markets_by_series=markets_by_series,
            horizon_days=45
        )
        
        assert result is None  # Should return None, not crash


class TestMultiSeriesSearchIntegration:
    """Test multi-series search handles errors gracefully."""
    
    def test_multi_series_search_no_crash_on_bad_data(self):
        """Test that multi-series search doesn't crash on invalid market data."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 3, 15)
        }
        
        # Mock client
        mock_client = Mock()
        
        # Mock list_markets to return markets with invalid data (>100 cents)
        mock_client.list_markets = Mock(return_value=[
            {
                "ticker": "KXINXMINY-TEST",
                "title": "SPX yearly min below 4250",
                "yes_bid": 150,  # INVALID:  150 cents > 100 = 1.5 probability
                "yes_ask": 200,  # INVALID: 200 cents > 100 = 2.0 probability
                "market_type": "binary",
                "close_time": "2026-03-15T00:00:00Z"
            }
        ])
        
        # Should not crash
        result = kalshi_multi_series_search(
            event_definition=event_def,
            client=mock_client,
            spot_spx=5000.0,
            horizon_days=45,
            allow_proxy=True,
            series_list=["KXINXMINY"]
        )
        
        # Should return graceful failure
        assert result["exact_match"] is False
        assert result["p_external"] is None
        assert result["proxy"] is None  # Proxy rejected due to invalid data
        assert "PROXY_CALCULATION_FAILED" in result["warnings"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
