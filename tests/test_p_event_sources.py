"""
Tests for p_event source subsystem.

Tests all modes: kalshi, kalshi_or_fallback, fallback_only, options_implied.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from forecast_arb.oracle.p_event_source import (
    PEventResult,
    PEventSource,
    KalshiPEventSource,
    FallbackPEventSource,
    OptionsImpliedPEventSource,
    KalshiOrFallbackPEventSource,
    create_p_event_source,
    PEventSourceType
)


class TestPEventResult:
    """Test PEventResult dataclass."""
    
    def test_basic_result(self):
        """Test basic result creation."""
        result = PEventResult(
            p_event=0.25,
            source="kalshi",
            confidence=0.8,
            timestamp="2026-01-29T12:00:00Z",
            metadata={"market_id": "TEST-MARKET"},
            fallback_used=False
        )
        
        assert result.p_event == 0.25
        assert result.source == "kalshi"
        assert result.confidence == 0.8
        assert result.fallback_used is False
        assert result.warnings == []
    
    def test_to_dict(self):
        """Test serialization to dict."""
        result = PEventResult(
            p_event=0.30,
            source="fallback",
            confidence=0.1,
            timestamp="2026-01-29T12:00:00Z",
            metadata={"reason": "test"},
            fallback_used=True,
            warnings=["Test warning"]
        )
        
        d = result.to_dict()
        assert d["p_event"] == 0.30
        assert d["source"] == "fallback"
        assert d["fallback_used"] is True
        assert len(d["warnings"]) == 1


class TestFallbackPEventSource:
    """Test fallback source (simplest case)."""
    
    def test_fallback_basic(self):
        """Test basic fallback functionality."""
        source = FallbackPEventSource(default_p_event=0.30)
        
        event_def = {
            "type": "price_move",
            "underlying": "SPY",
            "direction": "below"
        }
        
        result = source.get_p_event(event_def)
        
        assert result.p_event == 0.30
        assert result.source == "fallback"
        assert result.confidence == 0.1
        assert result.fallback_used is True
        assert len(result.warnings) > 0
    
    def test_fallback_custom_value(self):
        """Test fallback with custom override."""
        source = FallbackPEventSource(default_p_event=0.30)
        
        event_def = {"type": "test"}
        result = source.get_p_event(event_def, fallback_value=0.40)
        
        assert result.p_event == 0.40


class TestOptionsImpliedPEventSource:
    """Test options-implied probability source."""
    
    def test_options_implied_basic(self):
        """Test basic options-implied probability."""
        options_data = {"spot_price": 580.0}
        source = OptionsImpliedPEventSource(options_data)
        
        event_def = {
            "type": "price_move",
            "underlying": "SPY",
            "threshold": 550.0,  # ~5% below spot
            "direction": "below"
        }
        
        result = source.get_p_event(
            event_def,
            spot_price=580.0,
            atm_iv=0.15,
            days_to_expiry=45
        )
        
        assert 0 < result.p_event < 1
        assert result.source == "options_implied"
        assert result.confidence == 0.7
        assert not result.fallback_used
        assert result.metadata["method"] == "otm_put_prices"
    
    def test_options_implied_no_threshold(self):
        """Test options-implied with auto threshold (default 15% move)."""
        options_data = {"spot_price": 580.0}
        source = OptionsImpliedPEventSource(options_data)
        
        event_def = {
            "type": "price_move",
            "underlying": "SPY",
            "direction": "below"
        }
        
        result = source.get_p_event(
            event_def,
            spot_price=580.0,
            atm_iv=0.15,
            days_to_expiry=45
        )
        
        # Should default to threshold = 0.85 * spot
        expected_threshold = 580.0 * 0.85
        assert result.metadata["threshold"] == expected_threshold
    
    def test_options_implied_no_data(self):
        """Test options-implied fails without options data."""
        source = OptionsImpliedPEventSource(options_data=None)
        
        event_def = {"type": "test"}
        
        with pytest.raises(ValueError, match="Options data required"):
            source.get_p_event(event_def)


class TestKalshiPEventSource:
    """Test Kalshi source."""
    
    def test_kalshi_success(self):
        """Test successful Kalshi probability fetch."""
        # Mock client
        mock_client = Mock()
        mock_client.list_markets.return_value = [
            {
                "ticker": "SPX-26FEB27-YES",
                "title": "Will S&P 500 be below 5000 on Feb 27?",
                "volume_24h": 5000,
                "close_time": "2026-02-27T20:00:00Z"
            }
        ]
        
        source = KalshiPEventSource(mock_client)
        
        # Mock oracle response
        with patch('forecast_arb.oracle.p_event_source.KalshiOracle') as MockOracle:
            mock_oracle = MockOracle.return_value
            mock_oracle.get_event_probability.return_value = {
                "market_id": "SPX-26FEB27-YES",
                "p_event": 0.28,
                "bid": 0.27,
                "ask": 0.29,
                "spread_cents": 2.0,
                "volume_24h": 5000
            }
            
            event_def = {
                "type": "index_level",
                "underlying": "SPX",
                "date": "2026-02-27"
            }
            
            result = source.get_p_event(event_def)
            
            assert result.p_event == 0.28
            assert result.source == "kalshi"
            assert not result.fallback_used
            assert result.metadata["market_id"] == "SPX-26FEB27-YES"
    
    def test_kalshi_no_market_found(self):
        """Test Kalshi hard fails when no market found."""
        mock_client = Mock()
        mock_client.list_markets.return_value = []  # No markets
        
        source = KalshiPEventSource(mock_client)
        
        event_def = {
            "type": "index_level",
            "underlying": "SPY",
            "date": "2026-02-27"
        }
        
        with pytest.raises(RuntimeError, match="No Kalshi market found"):
            source.get_p_event(event_def)
    
    def test_confidence_assessment(self):
        """Test confidence scoring based on spread and volume."""
        source = KalshiPEventSource(Mock())
        
        # Tight spread, high volume -> high confidence
        oracle_data1 = {"spread_cents": 1.0, "volume_24h": 10000}
        conf1 = source._assess_confidence(oracle_data1)
        
        # Wide spread, low volume -> low confidence
        oracle_data2 = {"spread_cents": 50.0, "volume_24h": 10}
        conf2 = source._assess_confidence(oracle_data2)
        
        assert conf1 > conf2
        assert 0.3 <= conf1 <= 1.0
        assert 0.3 <= conf2 <= 1.0


class TestKalshiOrFallbackPEventSource:
    """Test Kalshi-or-fallback composite source."""
    
    def test_kalshi_succeeds(self):
        """Test that Kalshi is tried first and used if successful."""
        mock_client = Mock()
        mock_client.list_markets.return_value = [
            {
                "ticker": "TEST-MARKET",
                "title": "Test Market",
                "volume_24h": 1000,
                "close_time": "2026-02-27T20:00:00Z"
            }
        ]
        
        source = KalshiOrFallbackPEventSource(mock_client, fallback_p_event=0.25)
        
        with patch('forecast_arb.oracle.p_event_source.KalshiOracle') as MockOracle:
            mock_oracle = MockOracle.return_value
            mock_oracle.get_event_probability.return_value = {
                "market_id": "TEST-MARKET",
                "p_event": 0.35,
                "bid": 0.34,
                "ask": 0.36,
                "spread_cents": 2.0,
                "volume_24h": 1000
            }
            
            event_def = {"type": "test", "underlying": "SPY"}
            result = source.get_p_event(event_def)
            
            # Should use Kalshi, not fallback
            assert result.source == "kalshi"
            assert result.p_event == 0.35
            assert not result.fallback_used
    
    def test_kalshi_fails_uses_fallback(self):
        """Test that fallback is used when Kalshi fails."""
        mock_client = Mock()
        mock_client.list_markets.return_value = []  # No markets -> Kalshi fails
        
        source = KalshiOrFallbackPEventSource(mock_client, fallback_p_event=0.25)
        
        event_def = {"type": "test", "underlying": "SPY"}
        result = source.get_p_event(event_def)
        
        # Should fall back
        assert result.source == "fallback"
        assert result.p_event == 0.25
        assert result.fallback_used is True
        assert len(result.warnings) > 0
        assert "Kalshi unavailable" in result.warnings[0]


class TestCreatePEventSource:
    """Test factory function."""
    
    def test_create_fallback(self):
        """Test creating fallback source."""
        source = create_p_event_source("fallback_only", fallback_p_event=0.35)
        assert isinstance(source, FallbackPEventSource)
        assert source.default_p_event == 0.35
    
    def test_create_kalshi(self):
        """Test creating Kalshi source."""
        mock_client = Mock()
        source = create_p_event_source("kalshi", kalshi_client=mock_client)
        assert isinstance(source, KalshiPEventSource)
    
    def test_create_kalshi_or_fallback(self):
        """Test creating Kalshi-or-fallback source."""
        mock_client = Mock()
        source = create_p_event_source(
            "kalshi_or_fallback",
            kalshi_client=mock_client,
            fallback_p_event=0.30
        )
        assert isinstance(source, KalshiOrFallbackPEventSource)
    
    def test_create_options_implied(self):
        """Test creating options-implied source."""
        options_data = {"spot_price": 580.0}
        source = create_p_event_source("options_implied", options_data=options_data)
        assert isinstance(source, OptionsImpliedPEventSource)
    
    def test_create_invalid_mode(self):
        """Test that invalid mode raises error."""
        with pytest.raises(ValueError, match="Unknown"):
            create_p_event_source("invalid_mode")
    
    def test_create_kalshi_without_client(self):
        """Test that Kalshi mode requires client."""
        with pytest.raises(ValueError, match="kalshi_client required"):
            create_p_event_source("kalshi")
    
    def test_ensemble_not_implemented(self):
        """Test that ensemble mode is not yet implemented."""
        with pytest.raises(NotImplementedError, match="Ensemble"):
            create_p_event_source("ensemble")


class TestEventDefinitionStructure:
    """Test that event definitions work as expected."""
    
    def test_event_definition_formats(self):
        """Test various event definition formats."""
        # Index level event
        event1 = {
            "type": "index_level",
            "underlying": "SPX",
            "date": "2026-02-27",
            "threshold": 5000,
            "direction": "below"
        }
        
        # Price move event (relative)
        event2 = {
            "type": "price_move",
            "underlying": "SPY",
            "date": "2026-02-27",
            "direction": "below",
            "percent_move": -0.15  # 15% down
        }
        
        # Volatility event
        event3 = {
            "type": "volatility_spike",
            "underlying": "VIX",
            "threshold": 30,
            "direction": "above"
        }
        
        # All should be dict-like structures that can be passed to sources
        for event_def in [event1, event2, event3]:
            assert isinstance(event_def, dict)
            assert "type" in event_def
            assert "underlying" in event_def or "direction" in event_def
