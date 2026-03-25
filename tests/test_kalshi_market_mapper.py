"""
Unit tests for Kalshi market mapper.

Tests deterministic, auditable mapping from event definitions to Kalshi markets.
"""

import pytest
from datetime import date, datetime
from forecast_arb.kalshi.market_mapper import (
    map_event_to_markets,
    validate_event_def,
    parse_market_level,
    parse_market_date,
    is_spx_market,
    calculate_liquidity_score,
    MappedKalshiMarket
)


class TestEventDefValidation:
    """Test event definition validation."""
    
    def test_valid_event_def(self):
        """Test that valid event_def passes validation."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        # Should not raise
        validate_event_def(event_def)
    
    def test_invalid_event_type(self):
        """Test that invalid event type raises ValueError."""
        event_def = {
            "type": "price_move",  # Unsupported
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        with pytest.raises(ValueError, match="Unsupported event type"):
            validate_event_def(event_def)
    
    def test_invalid_index(self):
        """Test that invalid index raises ValueError."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPY",  # Unsupported
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        with pytest.raises(ValueError, match="Unsupported index"):
            validate_event_def(event_def)
    
    def test_missing_threshold_pct(self):
        """Test that missing threshold_pct raises ValueError."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "expiry": date(2026, 2, 27)
        }
        with pytest.raises(ValueError, match="Missing required field"):
            validate_event_def(event_def)
    
    def test_missing_expiry(self):
        """Test that missing expiry raises ValueError."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15
        }
        with pytest.raises(ValueError, match="Missing required field"):
            validate_event_def(event_def)


class TestMarketLevelParsing:
    """Test market level and range parsing."""
    
    def test_parse_level_based_below(self):
        """Test parsing level-based market (below)."""
        ticker = "INX-26FEB27-B4200"
        title = "S&P 500 closes below 4200 on 2026-02-27"
        
        result = parse_market_level(ticker, title)
        
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == 4200.0
        assert result["direction"] == "below"
    
    def test_parse_level_based_above(self):
        """Test parsing level-based market (above)."""
        ticker = "INX-26MAR27-A5000"
        title = "S&P 500 closes above 5000 on 2026-03-27"
        
        result = parse_market_level(ticker, title)
        
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == 5000.0
        assert result["direction"] == "above"
    
    def test_parse_range_based(self):
        """Test parsing range-based market."""
        ticker = "INX-26FEB27-R40004200"
        title = "SPX between 4000 and 4200 on Feb 27"
        
        result = parse_market_level(ticker, title)
        
        assert result is not None
        assert result["market_type"] == "range"
        assert result["low"] == 4000.0
        assert result["high"] == 4200.0
        assert result["mid"] == 4100.0
    
    def test_parse_ticker_only(self):
        """Test parsing level from ticker when title is unclear."""
        ticker = "INX-26FEB27-B4200"
        title = "Some unclear description"
        
        result = parse_market_level(ticker, title)
        
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == 4200.0
    
    def test_parse_unparseable_market(self):
        """Test that unparseable market returns None."""
        ticker = "SOMEMARKET-26FEB27"
        title = "Some unrelated market"
        
        result = parse_market_level(ticker, title)
        
        assert result is None


class TestMarketDateParsing:
    """Test market date parsing."""
    
    def test_parse_iso_date(self):
        """Test parsing ISO format date."""
        close_time = "2026-02-27T23:59:59Z"
        
        result = parse_market_date(close_time)
        
        assert result == date(2026, 2, 27)
    
    def test_parse_invalid_date(self):
        """Test parsing invalid date returns None."""
        close_time = "invalid"
        
        result = parse_market_date(close_time)
        
        assert result is None


class TestSPXMarketDetection:
    """Test SPX market detection."""
    
    def test_detect_spx_ticker(self):
        """Test detection from ticker."""
        assert is_spx_market("INX-26FEB27-B4200", "Some title")
    
    def test_detect_spx_title(self):
        """Test detection from title."""
        assert is_spx_market("SOME-TICKER", "S&P 500 closes below 4200")
    
    def test_detect_non_spx(self):
        """Test non-SPX market."""
        assert not is_spx_market("NASDAQ-26FEB27-B15000", "NASDAQ below 15000")


class TestLiquidityScore:
    """Test liquidity score calculation."""
    
    def test_high_volume(self):
        """Test high volume market."""
        market = {"volume_24h": 1000, "open_interest": 500}
        
        score = calculate_liquidity_score(market)
        
        assert score >= 0.8
    
    def test_low_volume(self):
        """Test low volume market."""
        market = {"volume_24h": 50, "open_interest": 25}
        
        score = calculate_liquidity_score(market)
        
        assert 0.0 <= score <= 0.5
    
    def test_default_no_data(self):
        """Test default score when no volume data."""
        market = {}
        
        score = calculate_liquidity_score(market)
        
        assert score == 0.3


class TestMapEventToMarkets:
    """Test complete event-to-market mapping."""
    
    def test_exact_level_match(self):
        """Test mapping to exact level match."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        target_level = 5000 * 0.85  # 4250
        
        kalshi_markets = [
            {
                "ticker": "INX-26FEB27-B4250",
                "title": "S&P 500 closes below 4250 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 1
        assert candidates[0].ticker == "INX-26FEB27-B4250"
        assert candidates[0].implied_level == 4250.0
        assert candidates[0].mapping_error == 0.0  # Exact match
    
    def test_range_match(self):
        """Test mapping to range-based market."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        target_level = 5000 * 0.85  # 4250
        
        kalshi_markets = [
            {
                "ticker": "INX-26FEB27-R42004300",
                "title": "SPX between 4200 and 4300 on Feb 27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 300
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 1
        assert candidates[0].ticker == "INX-26FEB27-R42004300"
        assert candidates[0].implied_level == 4250.0  # Midpoint
        assert candidates[0].mapping_error == 0.0  # Exact match to midpoint
    
    def test_no_match_wrong_date(self):
        """Test no match when date doesn't match."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        
        kalshi_markets = [
            {
                "ticker": "INX-26MAR27-B4250",
                "title": "S&P 500 closes below 4250 on 2026-03-27",
                "close_time": "2026-03-27T23:59:59Z",  # Wrong date
                "market_type": "binary",
                "volume_24h": 500
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 0
    
    def test_no_match_wrong_index(self):
        """Test no match when index doesn't match."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        
        kalshi_markets = [
            {
                "ticker": "NASDAQ-26FEB27-B15000",
                "title": "NASDAQ closes below 15000 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 0
    
    def test_no_match_large_mapping_error(self):
        """Test no match when mapping error exceeds threshold."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        target_level = 5000 * 0.85  # 4250
        
        kalshi_markets = [
            {
                "ticker": "INX-26FEB27-B3800",  # Far from target
                "title": "S&P 500 closes below 3800 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            }
        ]
        
        candidates = map_event_to_markets(
            event_def, spot_spx, kalshi_markets, max_mapping_error=0.05
        )
        
        assert len(candidates) == 0
    
    def test_ranking_by_mapping_error(self):
        """Test that candidates are ranked by mapping error."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        target_level = 5000 * 0.85  # 4250
        
        kalshi_markets = [
            {
                "ticker": "INX-26FEB27-B4350",
                "title": "S&P 500 closes below 4350 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            },
            {
                "ticker": "INX-26FEB27-B4250",
                "title": "S&P 500 closes below 4250 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            },
            {
                "ticker": "INX-26FEB27-B4280",
                "title": "S&P 500 closes below 4280 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 3
        # Best match should be first
        assert candidates[0].ticker == "INX-26FEB27-B4250"
        assert candidates[0].mapping_error == 0.0
        # Others should be in order of increasing error
        assert candidates[1].mapping_error < candidates[2].mapping_error
    
    def test_ranking_by_liquidity(self):
        """Test that candidates with equal mapping error are ranked by liquidity."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        
        kalshi_markets = [
            {
                "ticker": "INX-26FEB27-B4250-A",
                "title": "S&P 500 closes below 4250 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 100  # Lower volume
            },
            {
                "ticker": "INX-26FEB27-B4250-B",
                "title": "S&P 500 closes below 4250 on 2026-02-27 (liquid)",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 1000  # Higher volume
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 2
        # Higher liquidity should be ranked first
        assert candidates[0].ticker == "INX-26FEB27-B4250-B"
        assert candidates[0].liquidity_score > candidates[1].liquidity_score
    
    def test_bad_event_def_hard_failure(self):
        """Test that bad event_def raises ValueError."""
        bad_event_def = {
            "type": "price_move",  # Invalid
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        kalshi_markets = []
        
        with pytest.raises(ValueError, match="Unsupported event type"):
            map_event_to_markets(bad_event_def, spot_spx, kalshi_markets)
    
    def test_empty_market_list(self):
        """Test mapping with empty market list returns empty."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": date(2026, 2, 27)
        }
        spot_spx = 5000.0
        kalshi_markets = []
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 0
    
    def test_expiry_date_string_conversion(self):
        """Test that expiry as string is converted to date."""
        event_def = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": -0.15,
            "expiry": "2026-02-27"  # String instead of date
        }
        spot_spx = 5000.0
        
        kalshi_markets = [
            {
                "ticker": "INX-26FEB27-B4250",
                "title": "S&P 500 closes below 4250 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500
            }
        ]
        
        candidates = map_event_to_markets(event_def, spot_spx, kalshi_markets)
        
        assert len(candidates) == 1


class TestMappedKalshiMarket:
    """Test MappedKalshiMarket dataclass."""
    
    def test_create_mapped_market(self):
        """Test creating a MappedKalshiMarket."""
        mapped = MappedKalshiMarket(
            ticker="INX-26FEB27-B4250",
            title="S&P 500 closes below 4250 on 2026-02-27",
            close_time=datetime(2026, 2, 27, 23, 59, 59),
            implied_level=4250.0,
            mapping_error=0.0,
            liquidity_score=0.8,
            rationale="Exact level match"
        )
        
        assert mapped.ticker == "INX-26FEB27-B4250"
        assert mapped.implied_level == 4250.0
        assert mapped.mapping_error == 0.0
        assert mapped.liquidity_score == 0.8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
