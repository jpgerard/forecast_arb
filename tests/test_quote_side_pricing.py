"""
Test quote-side pricing for debit spreads.

Ensures BUY legs use ask and SELL legs use bid for executable pricing.
"""

import pytest
from forecast_arb.structuring.quotes import price_buy, price_sell


class TestPriceBuy:
    """Test price_buy function for BUY legs (long positions in debit spreads)."""
    
    def test_ask_available(self):
        """BUY leg should prefer ask when available."""
        quote = {"bid": 1.50, "ask": 1.55, "last": 1.52}
        price, source = price_buy(quote)
        assert price == 1.55
        assert source == "ask"
    
    def test_ask_only_bid_none(self):
        """BUY leg should use ask when bid is None (deep OTM case)."""
        quote = {"bid": None, "ask": 0.05, "last": None}
        price, source = price_buy(quote)
        assert price == 0.05
        assert source == "ask"
    
    def test_bid_fallback(self):
        """BUY leg should fallback to bid if ask missing."""
        quote = {"bid": 1.50, "ask": None, "last": 1.52}
        price, source = price_buy(quote)
        assert price == 1.50
        assert source == "bid_fallback"
    
    def test_last_fallback(self):
        """BUY leg should fallback to last if bid and ask missing."""
        quote = {"bid": None, "ask": None, "last": 1.52}
        price, source = price_buy(quote)
        assert price == 1.52
        assert source == "last_fallback"
    
    def test_no_price_available(self):
        """BUY leg should return None if no prices available."""
        quote = {"bid": None, "ask": None, "last": None}
        price, source = price_buy(quote)
        assert price is None
        assert source == "no_price"
    
    def test_zero_ask_rejected(self):
        """BUY leg should reject ask=0."""
        quote = {"bid": None, "ask": 0, "last": None}
        price, source = price_buy(quote)
        assert price is None
        assert source == "no_price"
    
    def test_negative_ask_rejected(self):
        """BUY leg should reject negative ask."""
        quote = {"bid": 1.0, "ask": -0.5, "last": 1.0}
        price, source = price_buy(quote)
        assert price == 1.0  # Should fallback to bid
        assert source == "bid_fallback"


class TestPriceSell:
    """Test price_sell function for SELL legs (short positions in debit spreads)."""
    
    def test_bid_available(self):
        """SELL leg should prefer bid when available."""
        quote = {"bid": 0.50, "ask": 0.55, "last": 0.52}
        price, source = price_sell(quote)
        assert price == 0.50
        assert source == "bid"
    
    def test_bid_only_ask_none(self):
        """SELL leg should use bid when ask is None."""
        quote = {"bid": 0.10, "ask": None, "last": None}
        price, source = price_sell(quote)
        assert price == 0.10
        assert source == "bid"
    
    def test_ask_fallback(self):
        """SELL leg should fallback to ask if bid missing."""
        quote = {"bid": None, "ask": 0.55, "last": 0.52}
        price, source = price_sell(quote)
        assert price == 0.55
        assert source == "ask_fallback"
    
    def test_last_fallback(self):
        """SELL leg should fallback to last if bid and ask missing."""
        quote = {"bid": None, "ask": None, "last": 0.52}
        price, source = price_sell(quote)
        assert price == 0.52
        assert source == "last_fallback"
    
    def test_no_price_available(self):
        """SELL leg should return None if no prices available."""
        quote = {"bid": None, "ask": None, "last": None}
        price, source = price_sell(quote)
        assert price is None
        assert source == "no_price"
    
    def test_zero_bid_rejected(self):
        """SELL leg should reject bid=0."""
        quote = {"bid": 0, "ask": 0.55, "last": None}
        price, source = price_sell(quote)
        assert price == 0.55  # Should fallback to ask
        assert source == "ask_fallback"
    
    def test_negative_bid_rejected(self):
        """SELL leg should reject negative bid."""
        quote = {"bid": -0.5, "ask": 0.55, "last": 0.5}
        price, source = price_sell(quote)
        assert price == 0.55  # Should fallback to ask
        assert source == "ask_fallback"


class TestDebitSpreadPricing:
    """Test debit spread pricing with correct quote sides."""
    
    def test_normal_case_ask_minus_bid(self):
        """Debit spread should use ask for long, bid for short."""
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            compute_debit_from_put_spread,
            validate_and_price_buy_leg,
            validate_and_price_sell_leg
        )
        
        # Long put (BUY): bid=2.00, ask=2.10
        long_put = {"bid": 2.00, "ask": 2.10, "last": 2.05}
        long_price, long_source = validate_and_price_buy_leg(long_put, 400.0)
        assert long_price == 2.10
        assert long_source == "ask"
        
        # Short put (SELL): bid=0.50, ask=0.55
        short_put = {"bid": 0.50, "ask": 0.55, "last": 0.52}
        short_price, short_source = validate_and_price_sell_leg(short_put, 390.0)
        assert short_price == 0.50
        assert short_source == "bid"
        
        # Debit = what we pay - what we receive
        debit = compute_debit_from_put_spread(long_price, short_price)
        assert debit == 2.10 - 0.50
        assert debit == 1.60
    
    def test_deep_otm_long_bid_none(self):
        """Deep OTM long put with bid=None should use ask."""
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            validate_and_price_buy_leg
        )
        
        # Deep OTM long put: bid=None (no market), ask=0.05
        long_put = {"bid": None, "ask": 0.05, "last": None}
        long_price, long_source = validate_and_price_buy_leg(long_put, 350.0)
        
        assert long_price == 0.05
        assert long_source == "ask"
        # This is the key fix: long leg should NOT be rejected when bid=None
    
    def test_no_executable_price_long(self):
        """Long leg with no prices should be rejected."""
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            validate_and_price_buy_leg
        )
        
        long_put = {"bid": None, "ask": None, "last": None}
        long_price, long_source = validate_and_price_buy_leg(long_put, 400.0)
        
        assert long_price is None
        assert long_source == "NO_EXECUTABLE_PRICE_LONG"
    
    def test_no_executable_price_short(self):
        """Short leg with no prices should be rejected."""
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            validate_and_price_sell_leg
        )
        
        short_put = {"bid": None, "ask": None, "last": None}
        short_price, short_source = validate_and_price_sell_leg(short_put, 390.0)
        
        assert short_price is None
        assert short_source == "NO_EXECUTABLE_PRICE_SHORT"
    
    def test_debit_positive(self):
        """Debit should be positive (we pay net premium)."""
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            compute_debit_from_put_spread
        )
        
        # Long put more expensive than short put
        long_price = 3.50
        short_price = 1.20
        debit = compute_debit_from_put_spread(long_price, short_price)
        
        assert debit > 0
        assert debit == 2.30


class TestRegressionBidNone:
    """Regression test for the specific bid=None issue."""
    
    def test_long_put_bid_none_ask_exists_accepted(self):
        """
        REGRESSION: Long put with bid=None but ask>0 should NOT be filtered.
        
        This was the original bug - candidates were rejected with:
        "Long put: Strike XXX: bid=None invalid (must be >0)"
        
        Now with correct quote-side logic, ask is used for BUY legs.
        """
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            validate_and_price_buy_leg
        )
        
        # Deep OTM long put scenario
        long_put = {
            "bid": None,  # No bid (too far OTM)
            "ask": 0.10,   # But ask exists
            "last": None,
            "strike": 350.0
        }
        
        price, source = validate_and_price_buy_leg(long_put, 350.0)
        
        # Should be ACCEPTED with ask price
        assert price is not None, "Long put should not be rejected when ask exists"
        assert price == 0.10
        assert source == "ask"
    
    def test_short_put_ask_none_bid_exists_accepted(self):
        """
        Short put with ask=None but bid>0 should be accepted.
        
        For SELL legs, we use bid price.
        """
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            validate_and_price_sell_leg
        )
        
        short_put = {
            "bid": 0.25,   # Bid exists
            "ask": None,   # No ask
            "last": None,
            "strike": 390.0
        }
        
        price, source = validate_and_price_sell_leg(short_put, 390.0)
        
        # Should be ACCEPTED with bid price
        assert price is not None, "Short put should not be rejected when bid exists"
        assert price == 0.25
        assert source == "bid"
    
    def test_full_spread_with_mixed_quotes(self):
        """
        Full debit spread with long.bid=None, short.ask=None should work.
        """
        from forecast_arb.engine.crash_venture_v1_snapshot import (
            validate_and_price_buy_leg,
            validate_and_price_sell_leg,
            compute_debit_from_put_spread
        )
        
        # Long put (deep OTM): bid=None, ask=0.15
        long_put = {"bid": None, "ask": 0.15, "last": None}
        long_price, long_source = validate_and_price_buy_leg(long_put, 360.0)
        assert long_price == 0.15
        assert long_source == "ask"
        
        # Short put: bid=0.05, ask=None
        short_put = {"bid": 0.05, "ask": None, "last": None}
        short_price, short_source = validate_and_price_sell_leg(short_put, 350.0)
        assert short_price == 0.05
        assert short_source == "bid"
        
        # Compute debit
        debit = compute_debit_from_put_spread(long_price, short_price)
        assert debit == pytest.approx(0.10)
        assert debit > 0


class TestPriceProvenance:
    """Test that price provenance is tracked correctly."""
    
    def test_provenance_ask_source(self):
        """Verify ask source is tracked."""
        quote = {"bid": 1.00, "ask": 1.05, "last": 1.02}
        price, source = price_buy(quote)
        assert source == "ask"
    
    def test_provenance_bid_fallback(self):
        """Verify bid_fallback source is tracked."""
        quote = {"bid": 1.00, "ask": None, "last": 1.02}
        price, source = price_buy(quote)
        assert source == "bid_fallback"
    
    def test_provenance_last_fallback(self):
        """Verify last_fallback source is tracked."""
        quote = {"bid": None, "ask": None, "last": 1.02}
        price, source = price_buy(quote)
        assert source == "last_fallback"
    
    def test_provenance_no_price(self):
        """Verify no_price source is tracked."""
        quote = {"bid": None, "ask": None, "last": None}
        price, source = price_buy(quote)
        assert source == "no_price"
