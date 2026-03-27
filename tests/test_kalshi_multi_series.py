"""
Unit tests for Kalshi multi-series adapter.

Covers:
- INDEX_FAMILY_SERIES Nasdaq refresh (stale names removed)
- compute_yearly_min_proxy() with KXINXMINY decimal/final-segment parsing
  (second live production path)
"""

import pytest
from unittest.mock import patch

from forecast_arb.kalshi.multi_series_adapter import (
    INDEX_FAMILY_SERIES,
    DEFAULT_KALSHI_SERIES,
    compute_yearly_min_proxy,
    ProxyProbability,
)
from forecast_arb.kalshi.market_mapper import parse_market_level


# ---------------------------------------------------------------------------
# INDEX_FAMILY_SERIES Nasdaq family refresh
# ---------------------------------------------------------------------------


class TestIndexFamilySeries:
    """Verify INDEX_FAMILY_SERIES contains current candidate names."""

    def test_spx_series_unchanged(self):
        assert "KXINX" in INDEX_FAMILY_SERIES["SPX"]
        assert "KXINXY" in INDEX_FAMILY_SERIES["SPX"]
        assert "KXINXMINY" in INDEX_FAMILY_SERIES["SPX"]
        assert "KXINXMAXY" in INDEX_FAMILY_SERIES["SPX"]

    def test_spy_series_unchanged(self):
        assert INDEX_FAMILY_SERIES["SPY"] == INDEX_FAMILY_SERIES["SPX"]

    def test_qqq_stale_names_removed(self):
        """KXNDX / KXNDXY / NASDAQ100 / NDX must not be in QQQ candidate list."""
        stale = {"KXNDX", "KXNDXY", "NASDAQ100", "NDX"}
        assert not stale.intersection(INDEX_FAMILY_SERIES["QQQ"]), (
            f"Stale QQQ series found: {stale & set(INDEX_FAMILY_SERIES['QQQ'])}"
        )

    def test_ndx_stale_names_removed(self):
        stale = {"KXNDX", "KXNDXY", "NASDAQ100", "NDX"}
        assert not stale.intersection(INDEX_FAMILY_SERIES["NDX"])

    def test_qqq_current_candidates_present(self):
        """Current Nasdaq-100 candidate family names are in the list."""
        assert "KXNASDAQ100" in INDEX_FAMILY_SERIES["QQQ"]
        assert "KXNASDAQ100Y" in INDEX_FAMILY_SERIES["QQQ"]

    def test_ndx_current_candidates_present(self):
        assert "KXNASDAQ100" in INDEX_FAMILY_SERIES["NDX"]
        assert "KXNASDAQ100Y" in INDEX_FAMILY_SERIES["NDX"]

    def test_qqq_ndx_share_same_candidates(self):
        """QQQ and NDX should map to the same candidate series set."""
        assert set(INDEX_FAMILY_SERIES["QQQ"]) == set(INDEX_FAMILY_SERIES["NDX"])

    def test_no_empty_family(self):
        for underlier, series in INDEX_FAMILY_SERIES.items():
            assert series, f"INDEX_FAMILY_SERIES[{underlier!r}] is empty"


# ---------------------------------------------------------------------------
# parse_market_level with KXINXMINY decimal ticker
# (unit-level sanity — no mocking required)
# ---------------------------------------------------------------------------


class TestKxinxminyDecimalParsing:
    """Direct unit tests for the KXINXMINY decimal final-segment path."""

    def test_integer_threshold(self):
        """KXINXMINY-01JAN2027-6600 → level 6600.0."""
        result = parse_market_level("KXINXMINY-01JAN2027-6600", "")
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == pytest.approx(6600.0)

    def test_decimal_threshold(self):
        """KXINXMINY-01JAN2027-6600.01 → level 6600.01 (the key regression)."""
        result = parse_market_level("KXINXMINY-01JAN2027-6600.01", "")
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == pytest.approx(6600.01)

    def test_high_decimal_precision(self):
        """KXINXMINY-01JAN2027-5875.50 → level 5875.50."""
        result = parse_market_level("KXINXMINY-01JAN2027-5875.50", "")
        assert result is not None
        assert result["level"] == pytest.approx(5875.50)

    def test_kxinxmaxy_decimal(self):
        """KXINXMAXY-01JAN2027-7500.25 → level 7500.25."""
        result = parse_market_level("KXINXMAXY-01JAN2027-7500.25", "")
        assert result is not None
        assert result["market_type"] == "level"
        assert result["level"] == pytest.approx(7500.25)


# ---------------------------------------------------------------------------
# compute_yearly_min_proxy — second live production path
# ---------------------------------------------------------------------------


class TestComputeYearlyMinProxy:
    """
    Integration tests for compute_yearly_min_proxy() with KXINXMINY decimal
    tickers.

    This is the second live production path: multi_series_adapter imports
    parse_market_level from market_mapper, which now routes through the
    threshold_parser shim.  These tests verify that decimal final-segment
    tickers (e.g. "KXINXMINY-01JAN2027-6600.01") are correctly parsed and
    yield a valid ProxyProbability.
    """

    BASE_EVENT = {
        "type": "index_drawdown",
        "index": "SPX",
        "threshold_pct": -0.15,
        "expiry": "2027-01-01",
    }
    SPOT_SPX = 7800.0  # target_level = 7800 * 0.85 = 6630

    def _make_miny_market(self, ticker: str, yes_bid: int = 25, yes_ask: int = 35) -> dict:
        return {
            "ticker": ticker,
            "title": f"KXINXMINY market {ticker}",
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
        }

    def test_decimal_ticker_produces_proxy(self):
        """
        KXINXMINY decimal ticker (6600.01) is close enough to target (6630)
        that compute_yearly_min_proxy returns a ProxyProbability, not None.
        """
        markets_by_series = {
            "KXINXMINY": [self._make_miny_market("KXINXMINY-01JAN2027-6600.01")]
        }
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is not None
        assert isinstance(result, ProxyProbability)
        assert result.proxy_series == "KXINXMINY"
        assert result.proxy_market_ticker == "KXINXMINY-01JAN2027-6600.01"
        assert 0.0 < result.p_external_proxy < 1.0

    def test_integer_ticker_produces_proxy(self):
        """KXINXMINY integer ticker (6600) also produces a proxy."""
        markets_by_series = {
            "KXINXMINY": [self._make_miny_market("KXINXMINY-01JAN2027-6600")]
        }
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is not None
        assert result.proxy_market_ticker == "KXINXMINY-01JAN2027-6600"

    def test_no_kxinxminy_series_returns_none(self):
        """Missing KXINXMINY series → None."""
        markets_by_series = {}
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is None

    def test_no_pricing_data_returns_none(self):
        """Market with no yes_bid/yes_ask → None."""
        market = {
            "ticker": "KXINXMINY-01JAN2027-6600.01",
            "title": "No pricing",
        }
        markets_by_series = {"KXINXMINY": [market]}
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is None

    def test_market_too_far_from_target_returns_none(self):
        """Market level more than 15% from target → None (error > proxy max)."""
        # target = 7800 * 0.85 = 6630; 5000 is ~24% away → rejected
        markets_by_series = {
            "KXINXMINY": [self._make_miny_market("KXINXMINY-01JAN2027-5000")]
        }
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is None

    def test_best_match_selected_by_proximity(self):
        """
        When multiple KXINXMINY markets are available, the one closest to
        target level is selected.
        """
        # target_level = 7800 * 0.85 = 6630
        # 6600.01 is closer (~0.5% error) than 6400 (~3.5% error)
        markets_by_series = {
            "KXINXMINY": [
                self._make_miny_market("KXINXMINY-01JAN2027-6400"),
                self._make_miny_market("KXINXMINY-01JAN2027-6600.01"),
            ]
        }
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is not None
        assert result.proxy_market_ticker == "KXINXMINY-01JAN2027-6600.01"

    def test_proxy_confidence_is_low(self):
        """Proxy confidence is fixed at 0.35 (low, as expected for a proxy)."""
        markets_by_series = {
            "KXINXMINY": [self._make_miny_market("KXINXMINY-01JAN2027-6600.01")]
        }
        result = compute_yearly_min_proxy(
            event_definition=self.BASE_EVENT,
            spot_spx=self.SPOT_SPX,
            markets_by_series=markets_by_series,
            horizon_days=45,
        )
        assert result is not None
        assert result.confidence == pytest.approx(0.35)
        assert result.proxy_method == "yearly_min_hazard_scale"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
