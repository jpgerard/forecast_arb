"""
Quote-side pricing helpers for debit/credit spreads.

For executable pricing:
- BUY legs (debit): prefer ask, fallback to bid, then last
- SELL legs (credit): prefer bid, fallback to ask, then last
"""

from typing import Dict, Optional, Tuple


def price_buy(quote: Dict) -> Tuple[Optional[float], str]:
    """
    Get executable price for buying an option (paying premium).
    
    For BUY legs in a debit spread, we pay the ask (or higher).
    
    Args:
        quote: Option quote dict with bid/ask/last fields
        
    Returns:
        (price, source) where source is "ask", "bid_fallback", "last_fallback", or "no_price"
    """
    ask = quote.get("ask")
    bid = quote.get("bid")
    last = quote.get("last")
    
    if ask is not None and ask > 0:
        return ask, "ask"
    elif bid is not None and bid > 0:
        return bid, "bid_fallback"
    elif last is not None and last > 0:
        return last, "last_fallback"
    else:
        return None, "no_price"


def price_sell(quote: Dict) -> Tuple[Optional[float], str]:
    """
    Get executable price for selling an option (receiving premium).
    
    For SELL legs in a debit spread, we receive the bid (or lower).
    
    Args:
        quote: Option quote dict with bid/ask/last fields
        
    Returns:
        (price, source) where source is "bid", "ask_fallback", "last_fallback", or "no_price"
    """
    bid = quote.get("bid")
    ask = quote.get("ask")
    last = quote.get("last")
    
    if bid is not None and bid > 0:
        return bid, "bid"
    elif ask is not None and ask > 0:
        return ask, "ask_fallback"
    elif last is not None and last > 0:
        return last, "last_fallback"
    else:
        return None, "no_price"
