"""
Unit tests for Phase 4 probability conditioning layer.

Tests:
- Multiplier bounds enforcement
- Missing signal safety
- High stress regime adjustment
- Low volatility regime adjustment
- Confidence scoring
"""

import pytest
from forecast_arb.probability.conditioning import (
    adjust_crash_probability,
    ConditioningConfig,
    compute_vol_multiplier,
    compute_skew_multiplier,
    compute_credit_multiplier,
    compute_confidence_score
)


class TestMultiplierBounds:
    """Test that multipliers respect hard bounds."""
    
    def test_min_multiplier_bound(self):
        """Ensure p_adj >= 0.25 * base_p"""
        config = ConditioningConfig()
        
        # Set all multipliers to cause extreme downward adjustment
        config.vol_low_mult = 0.1  # Extreme low
        config.skew_low_mult = 0.1
        config.credit_low_mult = 0.1
        
        result = adjust_crash_probability(
            base_p=0.10,
            vix_pct=0.05,  # Low VIX
            skew_pct=0.05,  # Low skew
            credit_pct=0.05,  # Low credit
            config=config
        )
        
        # Combined would be 0.1 * 0.1 * 0.1 = 0.001
        # But should be bounded to 0.25 * base_p = 0.025
        assert result["p_adjusted"] >= 0.10 * config.min_multiplier
        assert result["p_adjusted"] >= 0.025
    
    def test_max_multiplier_bound(self):
        """Ensure p_adj <= 3.0 * base_p"""
        config = ConditioningConfig()
        
        # Set all multipliers to cause extreme upward adjustment
        config.vol_high_mult = 2.0  # Extreme high
        config.skew_high_mult = 2.0
        config.credit_high_mult = 2.0
        
        result = adjust_crash_probability(
            base_p=0.05,
            vix_pct=0.95,  # High VIX
            skew_pct=0.95,  # High skew
            credit_pct=0.95,  # High credit
            config=config
        )
        
        # Combined would be 2.0 * 2.0 * 2.0 = 8.0x
        # But should be bounded to 3.0 * base_p = 0.15
        assert result["p_adjusted"] <= 0.05 * config.max_multiplier
        assert result["p_adjusted"] <= 0.15
    
    def test_absolute_cap(self):
        """Ensure p_adj never exceeds absolute cap (0.35)"""
        config = ConditioningConfig()
        
        # Start with high base prob and apply upward multipliers
        result = adjust_crash_probability(
            base_p=0.20,
            vix_pct=0.95,
            skew_pct=0.95,
            credit_pct=0.95,
            config=config
        )
        
        assert result["p_adjusted"] <= config.absolute_cap
        assert result["p_adjusted"] <= 0.35


class TestMissingSignalsSafety:
    """Test that None inputs don't crash and default to neutral."""
    
    def test_all_signals_none(self):
        """All None signals should return base_p unchanged."""
        result = adjust_crash_probability(
            base_p=0.08,
            vix_pct=None,
            skew_pct=None,
            credit_pct=None
        )
        
        assert result["p_adjusted"] == 0.08
        assert result["multipliers"]["vol"] == 1.0
        assert result["multipliers"]["skew"] == 1.0
        assert result["multipliers"]["credit"] == 1.0
        assert result["confidence_score"] == 0.0
        assert result["p_source"] == "base"
    
    def test_partial_signals(self):
        """Partial signals should apply available multipliers only."""
        result = adjust_crash_probability(
            base_p=0.08,
            vix_pct=0.85,  # Available - high
            skew_pct=None,  # Missing
            credit_pct=None  # Missing
        )
        
        assert result["multipliers"]["vol"] == 1.20  # Applied
        assert result["multipliers"]["skew"] == 1.0  # Neutral
        assert result["multipliers"]["credit"] == 1.0  # Neutral
        assert result["confidence_score"] == 0.33  # 1/3 available
        assert result["p_source"] == "conditioned"  # Because vol != 1.0


class TestHighStressRegime:
    """Test upward adjustment in high stress regime."""
    
    def test_high_vix_high_credit(self):
        """High VIX + high credit stress should increase p_adj."""
        result = adjust_crash_probability(
            base_p=0.05,
            vix_pct=0.90,  # High stress
            skew_pct=None,
            credit_pct=0.90  # High stress
        )
        
        # vol=1.20, skew=1.0, credit=1.25 → combined=1.5
        assert result["p_adjusted"] > 0.05
        assert result["multipliers"]["vol"] == 1.20
        assert result["multipliers"]["credit"] == 1.25
        assert result["multipliers"]["combined"] == 1.20 * 1.25
        assert result["p_source"] == "conditioned"
    
    def test_extreme_stress_all_signals(self):
        """All signals showing stress should compound."""
        result = adjust_crash_probability(
            base_p=0.06,
            vix_pct=0.95,
            skew_pct=0.95,
            credit_pct=0.95
        )
        
        # vol=1.20, skew=1.15, credit=1.25 → combined=1.725
        assert result["p_adjusted"] > 0.06
        expected_mult = 1.20 * 1.15 * 1.25
        assert abs(result["multipliers"]["combined"] - expected_mult) < 0.01
        assert result["confidence_score"] == 0.99  # All 3 signals


class TestLowVolRegime:
    """Test downward adjustment in low volatility regime."""
    
    def test_low_vix(self):
        """Low VIX should decrease p_adj."""
        result = adjust_crash_probability(
            base_p=0.08,
            vix_pct=0.10,  # Low VIX
            skew_pct=None,
            credit_pct=None
        )
        
        assert result["p_adjusted"] < 0.08
        assert result["multipliers"]["vol"] == 0.85
        assert result["p_source"] == "conditioned"
    
    def test_calm_regime_all_signals(self):
        """All signals showing calm should compound downward."""
        result = adjust_crash_probability(
            base_p=0.10,
            vix_pct=0.10,  # Calm
            skew_pct=0.10,  # Cheap skew
            credit_pct=0.10  # Calm credit
        )
        
        # vol=0.85, skew=0.90, credit=0.90 → combined≈0.689
        assert result["p_adjusted"] < 0.10
        expected_mult = 0.85 * 0.90 * 0.90
        assert abs(result["multipliers"]["combined"] - expected_mult) < 0.01


class TestConfidenceScoring:
    """Test confidence score computation."""
    
    def test_no_signals(self):
        """No signals = 0 confidence."""
        result = adjust_crash_probability(
            base_p=0.05,
            vix_pct=None,
            skew_pct=None,
            credit_pct=None
        )
        assert result["confidence_score"] == 0.0
    
    def test_one_signal(self):
        """One signal = 0.33 confidence."""
        result = adjust_crash_probability(
            base_p=0.05,
            vix_pct=0.50,
            skew_pct=None,
            credit_pct=None
        )
        assert result["confidence_score"] == 0.33
    
    def test_two_signals(self):
        """Two signals = 0.66 confidence."""
        result = adjust_crash_probability(
            base_p=0.05,
            vix_pct=0.50,
            skew_pct=None,
            credit_pct=0.50
        )
        assert result["confidence_score"] == 0.66
    
    def test_three_signals(self):
        """Three signals = 0.99 confidence (rounded)."""
        result = adjust_crash_probability(
            base_p=0.05,
            vix_pct=0.50,
            skew_pct=0.50,
            credit_pct=0.50
        )
        assert result["confidence_score"] == 0.99


class TestComponentMultipliers:
    """Test individual multiplier functions."""
    
    def test_vol_multiplier_ranges(self):
        """Test VIX percentile buckets."""
        config = ConditioningConfig()
        
        # Low VIX
        assert compute_vol_multiplier(0.10, config) == 0.85
        assert compute_vol_multiplier(0.19, config) == 0.85
        
        # Normal VIX
        assert compute_vol_multiplier(0.20, config) == 1.0
        assert compute_vol_multiplier(0.50, config) == 1.0
        assert compute_vol_multiplier(0.80, config) == 1.0
        
        # High VIX
        assert compute_vol_multiplier(0.81, config) == 1.20
        assert compute_vol_multiplier(0.95, config) == 1.20
        
        # None
        assert compute_vol_multiplier(None, config) == 1.0
    
    def test_skew_multiplier_ranges(self):
        """Test skew percentile buckets."""
        config = ConditioningConfig()
        
        # Low skew (cheap)
        assert compute_skew_multiplier(0.10, config) == 0.90
        
        # Normal skew
        assert compute_skew_multiplier(0.50, config) == 1.0
        
        # High skew (expensive)
        assert compute_skew_multiplier(0.85, config) == 1.15
        
        # None
        assert compute_skew_multiplier(None, config) == 1.0
    
    def test_credit_multiplier_ranges(self):
        """Test credit percentile buckets."""
        config = ConditioningConfig()
        
        # Low credit stress (calm)
        assert compute_credit_multiplier(0.10, config) == 0.90
        
        # Normal credit
        assert compute_credit_multiplier(0.50, config) == 1.0
        
        # High credit stress
        assert compute_credit_multiplier(0.85, config) == 1.25
        
        # None
        assert compute_credit_multiplier(None, config) == 1.0


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_invalid_base_p_too_low(self):
        """base_p <= 0 should raise ValueError."""
        with pytest.raises(ValueError, match="base_p must be in"):
            adjust_crash_probability(
                base_p=0.0,
                vix_pct=0.5,
                skew_pct=None,
                credit_pct=None
            )
    
    def test_invalid_base_p_too_high(self):
        """base_p >= 1 should raise ValueError."""
        with pytest.raises(ValueError, match="base_p must be in"):
            adjust_crash_probability(
                base_p=1.0,
                vix_pct=0.5,
                skew_pct=None,
                credit_pct=None
            )
    
    def test_valid_probability_range(self):
        """Adjusted prob should always be in (0, 1)."""
        # Test various extreme inputs
        test_cases = [
            (0.001, 0.95, 0.95, 0.95),  # Low base, high stress
            (0.30, 0.05, 0.05, 0.05),   # High base, low stress
            (0.15, 0.50, 0.50, 0.50),   # Mid base, mid stress
        ]
        
        for base_p, vix, skew, credit in test_cases:
            result = adjust_crash_probability(
                base_p=base_p,
                vix_pct=vix,
                skew_pct=skew,
                credit_pct=credit
            )
            assert 0 < result["p_adjusted"] < 1


class TestDeterminism:
    """Test that conditioning is deterministic."""
    
    def test_same_inputs_same_output(self):
        """Same inputs should always produce same output."""
        inputs = {
            "base_p": 0.075,
            "vix_pct": 0.65,
            "skew_pct": 0.45,
            "credit_pct": 0.70
        }
        
        result1 = adjust_crash_probability(**inputs)
        result2 = adjust_crash_probability(**inputs)
        
        assert result1["p_adjusted"] == result2["p_adjusted"]
        assert result1["multipliers"] == result2["multipliers"]
        assert result1["confidence_score"] == result2["confidence_score"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
