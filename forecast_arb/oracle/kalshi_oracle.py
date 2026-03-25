"""
Kalshi Oracle: Treat Kalshi market probabilities as ground truth.

No LLM probability updates - just collect market-implied probabilities.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..kalshi.client import KalshiClient


logger = logging.getLogger(__name__)


class KalshiOracle:
    """
    Oracle that treats Kalshi market probabilities as ground truth.
    
    For YES/NO markets: p_event = midprice of YES contract
    For range markets: p_event = midprice of specific range contract
    """
    
    def __init__(self, client: KalshiClient):
        """
        Initialize oracle.
        
        Args:
            client: Kalshi REST client
        """
        self.client = client
    
    def get_event_probability(
        self,
        market: Dict,
        asof_utc: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Get event probability from Kalshi market.
        
        Args:
            market: Market data dict
            asof_utc: Snapshot timestamp
            
        Returns:
            Dict with p_event, bid, ask, spread_cents, volume_24h, raw market data
        """
        if asof_utc is None:
            asof_utc = datetime.now(timezone.utc).isoformat()
        
        market_id = market.get("ticker", market.get("id"))
        
        try:
            # Get orderbook
            orderbook = self.client.get_orderbook(market_id)
            
            # Get YES side (for binary YES/NO markets)
            yes_bid = orderbook.get("yes", {}).get("bid")
            yes_ask = orderbook.get("yes", {}).get("ask")
            
            if yes_bid is None or yes_ask is None:
                logger.warning(f"Missing orderbook data for {market_id}")
                return None
            
            # Compute midpoint as event probability
            p_event = (yes_bid + yes_ask) / 2.0
            spread_cents = (yes_ask - yes_bid) * 100
            
            # Get volume
            volume_24h = self.client.get_market_volume_24h(market_id)
            
            return {
                "market_id": market_id,
                "p_event": p_event,
                "bid": yes_bid,
                "ask": yes_ask,
                "spread_cents": spread_cents,
                "volume_24h": volume_24h,
                "asof_utc": asof_utc,
                "raw_market": market
            }
            
        except Exception as e:
            logger.error(f"Failed to get probability for {market_id}: {e}")
            return None


def collect_oracle_data(
    client: KalshiClient,
    markets: List[Dict],
    asof_utc: Optional[str] = None
) -> List[Dict]:
    """
    Collect oracle data for multiple markets.
    
    Args:
        client: Kalshi client
        markets: List of market dicts
        asof_utc: Snapshot timestamp
        
    Returns:
        List of oracle data dicts
    """
    oracle = KalshiOracle(client)
    oracle_data = []
    
    for market in markets:
        market_id = market.get("ticker", market.get("id"))
        logger.info(f"Collecting oracle data for {market_id}")
        
        data = oracle.get_event_probability(market, asof_utc)
        if data:
            oracle_data.append(data)
    
    logger.info(f"Collected oracle data for {len(oracle_data)}/{len(markets)} markets")
    return oracle_data
