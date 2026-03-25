"""
Tests for options-implied probability calculations.
"""

import pytest
import math
from scipy.stats import norm

from forecast_arb.options.implied_prob import options_implied_p_event


class TestOptionsImpliedPEvent:
    """Test Black-Scholes implied probability calculations."""
    
    def test_basic_computation(self):
        """Test basic probability computation."""
        spot = 100.0
        strike = 95.0
        iv = 0.15
        dte = 30
        
        p = options_implied_p_event(spot, strike, iv, dte)
        
        # Should be between 0 and 1
        assert 0 < p < 1
        
        # For ATM put with 30 DTE, expect moderate probability
        assert 0.1 < p < 0.5
    
    def test_monotonic_strike(self):
        """Test that probability increases as strike increases."""
        spot = 100.0
        iv = 0.15
        dte = 30
        
        p_low = options_implied_p_event(spot, 90.0, iv, dte)
        p_mid = options_implied_p_event(spot, 95.0, iv, dte)
        p_high = options_implied_p_event(spot, 100.0, iv, dte)
        
        # Higher strikes should have higher probabilities
        assert p_low < p_mid < p_high
    
    def test_monotonic_iv(self):
        """Test that tail probability increases with IV."""
        spot = 100.0
        strike = 85.0  # 15% OTM put
        dte = 45
        
        p_low_iv = options_implied_p_event(spot, strike, 0.10, dte)
        p_mid_iv = options_implied_p_event(spot, strike, 0.20, dte)
        p_high_iv = options_implied_p_event(spot, strike, 0.30, dte)
        
        # Higher IV should mean higher tail probability
        assert p_low_iv < p_mid_iv < p_high_iv
    
    def test_monotonic_time(self):
        """Test that tail probability changes with time."""
        spot = 100.0
        strike = 85.0
        iv = 0.15
        
        p_short = options_implied_p_event(spot, strike, iv, 10)
        p_long = options_implied_p_event(spot, strike, iv, 90)
        
        # Longer time should allow more probability of reaching strike
        assert p_short < p_long
    
    def test_clipping(self):
        """Test that probabilities are clipped to valid range."""
        spot = 100.0
        strike = 1.0  # Very deep OTM
        iv = 0.01  # Very low vol
        dte = 1
        
        p = options_implied_p_event(spot, strike, iv, dte)
        
        # Should be clipped to minimum
        assert p >= 1e-6
        assert p <= 1 - 1e-6
    
    def test_invalid_inputs(self):
        """Test that invalid inputs raise ValueError."""
        with pytest.raises(ValueError, match="spot must be positive"):
            options_implied_p_event(0.0, 95.0, 0.15, 30)
        
        with pytest.raises(ValueError, match="strike must be positive"):
            options_implied_p_event(100.0, 0.0, 0.15, 30)
        
        with pytest.raises(ValueError, match="iv must be positive"):
            options_implied_p_event(100.0, 95.0, 0.0, 30)
        
        with pytest.raises(ValueError, match="dte must be positive"):
            options_implied_p_event(100.0, 95.0, 0.15, 0)
    
    def test_regression_known_values(self):
        """Test against known Black-Scholes values."""
        # Spot=100, Strike=90, IV=0.20, DTE=45, r=0
        spot = 100.0
        strike = 90.0
        iv = 0.20
        dte = 45
        r = 0.0
        
        T = dte / 365.0
        d2 = (math.log(spot / strike) + (r - 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        expected_p = norm.cdf(-d2)
        
        p = options_implied_p_event(spot, strike, iv, dte, r)
        
        # Should match within floating point precision
        assert abs(p - expected_p) < 1e-10
    
    def test_float_precision(self):
        """Test that output is float type."""
        p = options_implied_p_event(100.0, 95.0, 0.15, 30)
        
        assert isinstance(p, float)


class TestImpliedProbFallback:
    """Test BS fallback when bracket quotes are missing."""
    
    def create_minimal_snapshot_with_atm_iv(self, spot=600.0, atm_iv=0.15):
        """Create a minimal snapshot for testing BS fallback."""
        return {
            "snapshot_metadata": {
                "underlier": "SPY",
                "symbol": "SPY",
                "current_price": spot,
                "snapshot_time": "2026-01-30T14:30:00Z",
                "atm_iv": atm_iv
            },
            "options": {
                "20260227": {
                    "puts": [
                        {
                            "strike": 500.0,
                            "bid": None,  # No executable price
                            "ask": None,
                            "implied_vol": None
                        },
                        {
                            "strike": 490.0,
                            "bid": None,
                            "ask": None,
                            "implied_vol": None
                        }
                    ]
                }
            }
        }
    
    def create_snapshot_with_liquid_atm(self, spot=600.0):
        """Create snapshot with liquid ATM put for IV inference."""
        return {
            "snapshot_metadata": {
                "underlier": "SPY",
                "symbol": "SPY",
                "current_price": spot,
                "snapshot_time": "2026-01-30T14:30:00Z"
            },
            "options": {
                "20260227": {
                    "puts": [
                        {
                            "strike": 600.0,  # ATM
                            "bid": 15.0,
                            "ask": 15.5,
                            "implied_vol": 0.18  # Available IV
                        },
                        {
                            "strike": 500.0,  # Deep OTM, no quotes
                            "bid": None,
                            "ask": None,
                            "implied_vol": None
                        },
                        {
                            "strike": 490.0,
                            "bid": None,
                            "ask": None,
                            "implied_vol": None
                        }
                    ]
                }
            }
        }
    
    def test_bs_fallback_with_atm_iv_from_snapshot(self):
        """Test that BS fallback works when snapshot has ATM IV."""
        from forecast_arb.options.implied_prob import implied_prob_terminal_below
        
        snapshot = self.create_minimal_snapshot_with_atm_iv(spot=600.0, atm_iv=0.15)
        
        p_implied, confidence, warnings = implied_prob_terminal_below(
            snapshot=snapshot,
            expiry="20260227",
            threshold=495.0,
            r=0.0
        )
        
        # Should return a non-None probability using BS fallback
        assert p_implied is not None
        assert 0 < p_implied < 1
        
        # Confidence should be lowered (<0.6)
        assert confidence < 0.6
        assert confidence > 0.0
        
        # Should have fallback warnings
        assert "FALLBACK_MODEL_USED" in warnings
        assert "PRIMARY_VERTICAL_NO_EXECUTABLE_QUOTES" in warnings
    
    def test_bs_fallback_with_inferred_iv(self):
        """Test BS fallback with IV inferred from ATM put."""
        from forecast_arb.options.implied_prob import implied_prob_terminal_below
        
        snapshot = self.create_snapshot_with_liquid_atm(spot=600.0)
        
        p_implied, confidence, warnings = implied_prob_terminal_below(
            snapshot=snapshot,
            expiry="20260227",
            threshold=495.0,
            r=0.0
        )
        
        # Should successfully use ATM IV
        assert p_implied is not None
        assert 0 < p_implied < 1
        
        # Confidence should be present but lowered
        assert confidence < 0.6
        assert confidence > 0.0
        
        # Should have fallback warnings
        assert "FALLBACK_MODEL_USED" in warnings
    
    def test_bs_fallback_confidence_lower_than_primary(self):
        """Test that fallback confidence is lower than primary method."""
        from forecast_arb.options.implied_prob import implied_prob_terminal_below
        
        snapshot = self.create_minimal_snapshot_with_atm_iv(spot=600.0, atm_iv=0.15)
        
        p_implied, confidence, warnings = implied_prob_terminal_below(
            snapshot=snapshot,
            expiry="20260227",
            threshold=495.0,
            r=0.0
        )
        
        # Fallback confidence should be significantly lower
        # Per spec: 0.35-0.5 range
        assert 0.30 < confidence < 0.55
    
    def test_no_fallback_if_no_iv_source(self):
        """Test that fallback fails gracefully if no IV source available."""
        from forecast_arb.options.implied_prob import implied_prob_terminal_below
        
        snapshot = {
            "snapshot_metadata": {
                "underlier": "SPY",
                "symbol": "SPY",
                "current_price": 600.0,
                "snapshot_time": "2026-01-30T14:30:00Z"
                # No atm_iv
            },
            "options": {
                "20260227": {
                    "puts": [
                        {
                            "strike": 500.0,
                            "bid": None,
                            "ask": None,
                            "implied_vol": None  # No IV
                        },
                        {
                            "strike": 490.0,
                            "bid": None,
                            "ask": None,
                            "implied_vol": None
                        }
                    ]
                }
            }
        }
        
        p_implied, confidence, warnings = implied_prob_terminal_below(
            snapshot=snapshot,
            expiry="20260227",
            threshold=495.0,
            r=0.0
        )
        
        # Should still return None if no IV available
        assert p_implied is None
        assert confidence == 0.0
        assert "No valid IV source for BS fallback" in warnings
