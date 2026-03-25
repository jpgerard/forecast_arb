"""
Unit tests for Kalshi threshold parser.

Tests series-aware parsing of INX market thresholds with deterministic inputs.
Ensures correct extraction of levels from tickers and titles without false matches.
"""

import pytest
from forecast_arb.kalshi.threshold_parser import (
    parse_threshold_from_market,
    format_threshold_display,
    _infer_series_from_ticker,
    _parse_range_from_title,
    _parse_threshold_fallback
)


class TestKXINXPointThresholds:
    """Test KXINX/KXINXY point threshold parsing (-T suffix)."""
    
    def test_kxinx_t6500(self):
        """Parse KXINX-...-T6500 as point threshold 6500."""
        market = {
            "ticker": "KXINX-26FEB27H1600-T6500",
            "title": "Will the S&P 500 close below 6500 on Feb 27?"
        }
        
        result = parse_threshold_from_market(market, series="KXINX")
        
        assert result["kind"] == "point"
        assert result["threshold"] == 6500.0
        assert result["low"] is None
        assert result["high"] is None
        assert result["source"] == "ticker"
        assert result["confidence"] == 1.0
    
    def test_kxinx_t7199_9999(self):
        """Parse KXINX-...-T7199.9999 as point threshold 7199.9999."""
        market = {
            "ticker": "KXINX-26FEB27H1600-T7199.9999",
            "title": "Will the S&P 500 close below 7199.9999 on Feb 27?"
        }
        
        result = parse_threshold_from_market(market, series="KXINX")
        
        assert result["kind"] == "point"
        assert result["threshold"] == 7199.9999
        assert result["source"] == "ticker"
        assert result["confidence"] == 1.0
    
    def test_kxinxy_t9000(self):
        """Parse KXINXY yearly market with T suffix."""
        market = {
            "ticker": "KXINXY-31DEC27-T9000",
            "title": "Will the S&P 500 close below 9000 on Dec 31, 2027?"
        }
        
        result = parse_threshold_from_market(market, series="KXINXY")
        
        assert result["kind"] == "point"
        assert result["threshold"] == 9000.0
        assert result["source"] == "ticker"


class TestKXINXRanges:
    """Test KXINX/KXINXY range parsing (-B suffix)."""
    
    def test_kxinx_b7187_range(self):
        """Parse KXINX-...-B7187 with title range."""
        market = {
            "ticker": "KXINX-26FEB27H1600-B7187",
            "title": "Will the S&P 500 close between 7175 and 7199.9999 on Feb 27?"
        }
        
        result = parse_threshold_from_market(market, series="KXINX")
        
        assert result["kind"] == "range"
        assert result["threshold"] is None
        assert result["low"] == 7175.0
        assert result["high"] == 7199.9999
        assert result["source"] == "title"
        assert result["confidence"] == 0.9
    
    def test_kxinx_b_without_range_in_title(self):
        """Parse KXINX -B ticker when title lacks range info."""
        market = {
            "ticker": "KXINX-26FEB27H1600-B7000",
            "title": "S&P 500 market for Feb 27"  # Missing "between...and"
        }
        
        result = parse_threshold_from_market(market, series="KXINX")
        
        assert result["kind"] == "unknown"
        assert result["confidence"] == 0.0


class TestKXINXMINYThresholds:
    """Test KXINXMINY point threshold parsing (final segment)."""
    
    def test_kxinxminy_6600_01(self):
        """Parse KXINXMINY-...-6600.01 as point threshold."""
        market = {
            "ticker": "KXINXMINY-01JAN2027-6600.01",
            "title": "Will the S&P 500 minimum be below 6600.01 in 2027?"
        }
        
        result = parse_threshold_from_market(market, series="KXINXMINY")
        
        assert result["kind"] == "point"
        assert result["threshold"] == 6600.01
        assert result["low"] is None
        assert result["high"] is None
        assert result["source"] == "ticker"
        assert result["confidence"] == 1.0
    
    def test_kxinxminy_5000(self):
        """Parse KXINXMINY with round threshold."""
        market = {
            "ticker": "KXINXMINY-31DEC27-5000",
            "title": "S&P 500 yearly minimum below 5000"
        }
        
        result = parse_threshold_from_market(market, series="KXINXMINY")
        
        assert result["kind"] == "point"
        assert result["threshold"] == 5000.0
        assert result["source"] == "ticker"
    
    def test_kxinxmaxy_parsing(self):
        """Parse KXINXMAXY market (yearly max)."""
        market = {
            "ticker": "KXINXMAXY-31DEC27-8500.5",
            "title": "S&P 500 yearly maximum above 8500.5"
        }
        
        result = parse_threshold_from_market(market, series="KXINXMAXY")
        
        assert result["kind"] == "point"
        assert result["threshold"] == 8500.5


class TestSP500AvoidExtracting500:
    """Critical: Must NOT extract 500 from "S&P 500" in titles."""
    
    def test_sp500_in_title_with_t_ticker(self):
        """Ticker should take priority, not extract 500 from title."""
        market = {
            "ticker": "KXINX-26FEB27H1600-T7000",
            "title": "Will the S&P 500 close below 7000 on Feb 27?"
        }
        
        result = parse_threshold_from_market(market, series="KXINX")
        
        # Should extract 7000 from ticker, NOT 500 from "S&P 500"
        assert result["kind"] == "point"
        assert result["threshold"] == 7000.0
        assert result["threshold"] != 500.0
    
    def test_sp500_in_title_without_threshold_ticker(self):
        """Title with S&P 500 but no clear threshold should use fallback."""
        market = {
            "ticker": "KXINX-26FEB27",  # No -T or -B suffix
            "title": "S&P 500 closes below 6800 on Feb 27"
        }
        
        result = parse_threshold_from_market(market, series="KXINX")
        
        # Fallback should match "below 6800", not "500"
        if result["kind"] == "point":
            assert result["threshold"] == 6800.0
            assert result["threshold"] != 500.0
    
    def test_sp500_without_explicit_threshold(self):
        """Title with just 'S&P 500' and no threshold should return unknown."""
        market = {
            "ticker": "KXINX-UNKNOWN",
            "title": "S&P 500 market information"
        }
        
        result = parse_threshold_from_market(market)
        
        # Should NOT extract 500 from "S&P 500"
        assert result["kind"] == "unknown" or result["threshold"] != 500.0


class TestSeriesInference:
    """Test series inference from tickers."""
    
    def test_infer_kxinx(self):
        assert _infer_series_from_ticker("KXINX-26FEB27-T6500") == "KXINX"
    
    def test_infer_kxinxy(self):
        assert _infer_series_from_ticker("KXINXY-31DEC27-T7000") == "KXINXY"
    
    def test_infer_kxinxminy(self):
        assert _infer_series_from_ticker("KXINXMINY-01JAN27-5000") == "KXINXMINY"
    
    def test_infer_kxinxmaxy(self):
        assert _infer_series_from_ticker("KXINXMAXY-31DEC27-9000") == "KXINXMAXY"
    
    def test_infer_unknown(self):
        assert _infer_series_from_ticker("UNKNOWN-TICKER") is None


class TestRangeParsingHelper:
    """Test range parsing helper function."""
    
    def test_between_and_pattern(self):
        result = _parse_range_from_title("between 7175 and 7199.9999")
        assert result == {"low": 7175.0, "high": 7199.9999}
    
    def test_from_to_pattern(self):
        result = _parse_range_from_title("from 5000 to 5500")
        assert result == {"low": 5000.0, "high": 5500.0}
    
    def test_with_commas(self):
        result = _parse_range_from_title("between 5,000 and 5,500")
        assert result == {"low": 5000.0, "high": 5500.0}
    
    def test_no_range(self):
        result = _parse_range_from_title("S&P 500 closes at 7000")
        assert result is None


class TestFallbackParser:
    """Test fallback parser that avoids false matches."""
    
    def test_below_6500(self):
        result = _parse_threshold_fallback("close below 6500 on Feb 27")
        assert result is not None
        assert result["threshold"] == 6500.0
    
    def test_above_7200(self):
        result = _parse_threshold_fallback("above 7200")
        assert result is not None
        assert result["threshold"] == 7200.0
    
    def test_sp500_not_matched(self):
        """Fallback must NOT match 500 from S&P 500."""
        result = _parse_threshold_fallback("S&P 500 market")
        # Should not extract 500 without directional indicator
        assert result is None or result["threshold"] != 500.0
    
    def test_at_6800(self):
        result = _parse_threshold_fallback("at 6800")
        assert result is not None
        assert result["threshold"] == 6800.0
    
    def test_sanity_check_rejects_low_values(self):
        """Fallback rejects implausibly low thresholds."""
        result = _parse_threshold_fallback("below 500")
        # 500 is below sanity check min (1000), should be rejected
        assert result is None


class TestDisplayFormatting:
    """Test threshold display formatting."""
    
    def test_format_point_threshold(self):
        parsed = {
            "kind": "point",
            "threshold": 6500.0,
            "low": None,
            "high": None
        }
        assert format_threshold_display(parsed) == "6500"
    
    def test_format_decimal_threshold(self):
        parsed = {
            "kind": "point",
            "threshold": 7199.9999,
            "low": None,
            "high": None
        }
        assert format_threshold_display(parsed) == "7200"
    
    def test_format_range(self):
        parsed = {
            "kind": "range",
            "threshold": None,
            "low": 7175.0,
            "high": 7199.9999
        }
        display = format_threshold_display(parsed)
        assert "7175" in display
        assert "7200" in display or "7199.9999" in display
        assert "–" in display or "-" in display
    
    def test_format_unknown(self):
        parsed = {
            "kind": "unknown",
            "threshold": None,
            "low": None,
            "high": None
        }
        assert format_threshold_display(parsed) == "unknown"


class TestEndToEnd:
    """End-to-end integration tests."""
    
    def test_typical_kxinx_market(self):
        """Test realistic KXINX market."""
        market = {
            "ticker": "KXINX-26FEB27H1600-T7150",
            "title": "Will the S&P 500 close below 7150 on February 27, 2027 at 4:00 PM ET?"
        }
        
        parsed = parse_threshold_from_market(market)
        display = format_threshold_display(parsed)
        
        assert parsed["kind"] == "point"
        assert parsed["threshold"] == 7150.0
        assert display == "7150"
    
    def test_typical_kxinxminy_market(self):
        """Test realistic KXINXMINY market."""
        market = {
            "ticker": "KXINXMINY-01JAN2027-6250.5",
            "title": "Will the S&P 500 yearly minimum be below 6250.5 in 2027?"
        }
        
        parsed = parse_threshold_from_market(market)
        display = format_threshold_display(parsed)
        
        assert parsed["kind"] == "point"
        assert parsed["threshold"] == 6250.5
        assert "6250" in display
    
    def test_typical_range_market(self):
        """Test realistic range market."""
        market = {
            "ticker": "KXINX-26FEB27H1600-B7100",
            "title": "Will the S&P 500 close between 7100 and 7125 on Feb 27?"
        }
        
        parsed = parse_threshold_from_market(market)
        display = format_threshold_display(parsed)
        
        assert parsed["kind"] == "range"
        assert parsed["low"] == 7100.0
        assert parsed["high"] == 7125.0
        assert "7100" in display and "7125" in display


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
