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
        """
        B-tickers without a range title now return None.

        Old behaviour: extracted level=4200 from the ticker digit sequence.
        New behaviour (via threshold_parser shim): B-tickers are range
        boundaries, not point levels.  Without a "between X and Y" title,
        the result is None.  This is the correct interpretation.
        """
        ticker = "INX-26FEB27-B4200"
        title = "Some unclear description"

        result = parse_market_level(ticker, title)

        assert result is None
    
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


class TestParseMarketLevelRouting:
    """
    Tests documenting the routing behaviour of the parse_market_level shim.

    These tests cover cases that the old parser did not handle (T-ticker
    decimals, KXINXMINY final-segment, comma-separated numbers) as well as
    the explicit B-ticker behavioural change.
    """

    def test_kxinx_t_ticker_decimal(self):
        """KXINX T-ticker with decimal is parsed as a point level."""
        ticker = "KXINX-26FEB27H1600-T7199.9999"
        title = "Will S&P 500 close below 7199.9999 on Feb 27?"
        result = parse_market_level(ticker, title)
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == pytest.approx(7199.9999)

    def test_kxinxminy_final_segment_decimal(self):
        """KXINXMINY ticker with decimal final segment is parsed as a point level."""
        ticker = "KXINXMINY-01JAN2027-6600.01"
        title = "S&P 500 yearly minimum below 6600.01"
        result = parse_market_level(ticker, title)
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == pytest.approx(6600.01)

    def test_kxinx_b_ticker_with_range_title(self):
        """KXINX B-ticker WITH a range title returns a range result."""
        ticker = "KXINX-26FEB27H1600-B7187"
        title = "Will the S&P 500 close between 7175 and 7199.9999 on Feb 27?"
        result = parse_market_level(ticker, title)
        assert result is not None
        assert result["market_type"] == "range"
        assert result["low"] == pytest.approx(7175.0)
        assert result["high"] == pytest.approx(7199.9999)
        assert result["mid"] == pytest.approx((7175.0 + 7199.9999) / 2)

    def test_b_ticker_without_range_title_returns_none(self):
        """B-ticker WITHOUT a range title returns None (documents the shim change)."""
        ticker = "KXINX-26FEB27H1600-B7187"
        title = "Some market"
        result = parse_market_level(ticker, title)
        assert result is None

    def test_title_with_comma_separated_number(self):
        """Comma-separated threshold in title is parsed correctly."""
        ticker = "KXINX-26DEC27H1600-T7000"
        title = "S&P 500 closes below 7,000 on Dec 27"
        # threshold_parser fallback handles comma in \b(?:below|...)\s+([\d,]+...)
        result = parse_market_level(ticker, title)
        assert result is not None
        assert result["market_type"] == "level"
        # Both title fallback (7000) and T-ticker (7000) return same value
        assert result["level"] == pytest.approx(7000.0)

    def test_no_false_match_sp500_number(self):
        """Parsing must NOT extract '500' from 'S&P 500' in titles."""
        ticker = "KXINX-26FEB27H1600-T7200"
        title = "Will the S&P 500 close above 7200 on Feb 27?"
        result = parse_market_level(ticker, title)
        # Should find 7200 from the T-ticker, not 500
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == pytest.approx(7200.0)

    def test_range_from_title_non_kxinx_series(self):
        """Non-KXINX tickers with 'between X and Y' title still return a range."""
        ticker = "INX-26FEB27-R40004200"
        title = "SPX between 4,000 and 4,200 on Feb 27"
        result = parse_market_level(ticker, title)
        assert result is not None
        assert result["market_type"] == "range"
        assert result["low"] == pytest.approx(4000.0)
        assert result["high"] == pytest.approx(4200.0)


class TestDiagnosticSummaryBlock:
    """
    Tests for the structured diagnostic summary emitted by map_event_to_markets.

    Each test verifies the diag_code logged, using caplog to capture INFO-level
    entries from the market_mapper logger.
    """

    BASE_EVENT = {
        "type": "index_drawdown",
        "index": "SPX",
        "threshold_pct": -0.15,
        "expiry": date(2026, 2, 27),
    }
    SPOT = 5000.0

    def _get_diag_code(self, caplog, candidates, markets):
        """Helper: run mapping with disabled coverage precheck, return captured diag code."""
        import logging
        with caplog.at_level(logging.INFO, logger="forecast_arb.kalshi.market_mapper"):
            map_event_to_markets(
                self.BASE_EVENT, self.SPOT, markets, enable_coverage_precheck=False
            )
        for record in caplog.records:
            if "mapping pass summary" in record.message:
                # parse diag_code from the dict representation in the message
                import ast
                # The dict is embedded as repr; extract it
                text = record.message[record.message.index("{"):]
                d = ast.literal_eval(text)
                return d.get("diag_code")
        return "NOT_FOUND"

    def test_diag_no_markets_returned(self, caplog):
        """Empty input → DIAG_NO_MARKETS_RETURNED."""
        code = self._get_diag_code(caplog, [], [])
        assert code == "NO_MARKETS_RETURNED"

    def test_diag_no_representable_markets(self, caplog):
        """Non-SPX markets only → DIAG_NO_REPRESENTABLE_MARKETS."""
        markets = [
            {
                "ticker": "NASDAQ-26FEB27-B15000",
                "title": "NASDAQ below 15000 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
            }
        ]
        code = self._get_diag_code(caplog, [], markets)
        assert code == "NO_REPRESENTABLE_MARKETS"

    def test_diag_date_mismatch(self, caplog):
        """SPX market with wrong expiry → DIAG_DATE_MISMATCH."""
        markets = [
            {
                "ticker": "INX-26MAR27-B4250",
                "title": "S&P 500 closes below 4250 on 2026-03-27",
                "close_time": "2026-03-27T23:59:59Z",
                "market_type": "binary",
            }
        ]
        code = self._get_diag_code(caplog, [], markets)
        assert code == "DATE_MISMATCH"

    def test_diag_parse_failure(self, caplog):
        """SPX market on correct date but unparseable title → DIAG_PARSE_FAILURE."""
        markets = [
            {
                "ticker": "INX-26FEB27-XYZ",
                "title": "S&P 500 something unknown on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
            }
        ]
        code = self._get_diag_code(caplog, [], markets)
        assert code == "PARSE_FAILURE"

    def test_diag_threshold_mismatch(self, caplog):
        """Level parsed but too far from target → DIAG_THRESHOLD_MISMATCH."""
        markets = [
            {
                "ticker": "INX-26FEB27-B3000",
                "title": "S&P 500 closes below 3000 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
            }
        ]
        code = self._get_diag_code(caplog, [], markets)
        assert code == "THRESHOLD_MISMATCH"

    def test_diag_exact_match_found(self, caplog):
        """Exact level match → diag_code is None."""
        target_level = 5000.0 * 0.85  # 4250
        markets = [
            {
                "ticker": "INX-26FEB27-B4250",
                "title": "S&P 500 closes below 4250 on 2026-02-27",
                "close_time": "2026-02-27T23:59:59Z",
                "market_type": "binary",
                "volume_24h": 500,
            }
        ]
        import logging
        with caplog.at_level(logging.INFO, logger="forecast_arb.kalshi.market_mapper"):
            map_event_to_markets(
                self.BASE_EVENT, self.SPOT, markets, enable_coverage_precheck=False
            )
        for record in caplog.records:
            if "mapping pass summary" in record.message:
                import ast
                text = record.message[record.message.index("{"):]
                d = ast.literal_eval(text)
                assert d.get("diag_code") is None
                assert d.get("n_candidates") == 1
                return
        pytest.fail("No mapping pass summary found in logs")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
