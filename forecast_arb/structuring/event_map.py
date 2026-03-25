"""
Event mapping: Convert Kalshi market events to option underliers.

Maps binary prediction markets to their corresponding option underliers.
"""

from typing import Dict, Optional, List
import re


# Market pattern to underlier mapping
EVENT_MAP = {
    # Equity indices
    r"INX-\d{2}[A-Z]{3}\d{2}": {
        "underlier": "SPY",  # Map S&P 500 markets to SPY ETF
        "type": "index",
        "description": "S&P 500 Index"
    },
    r"NASDAQ|NDX": {
        "underlier": "QQQ",
        "type": "index",
        "description": "NASDAQ 100 Index"
    },
    r"DJI|DOW": {
        "underlier": "DIA",
        "type": "index",
        "description": "Dow Jones Industrial Average"
    },
    r"RUT": {
        "underlier": "IWM",
        "type": "index",
        "description": "Russell 2000 Index"
    },
    
    # Volatility
    r"VIX": {
        "underlier": "VIX",
        "type": "volatility",
        "description": "CBOE Volatility Index"
    },
    
    # Single stocks
    r"AAPL": {
        "underlier": "AAPL",
        "type": "stock",
        "description": "Apple Inc."
    },
    r"TSLA": {
        "underlier": "TSLA",
        "type": "stock",
        "description": "Tesla Inc."
    },
    r"MSFT": {
        "underlier": "MSFT",
        "type": "stock",
        "description": "Microsoft Corporation"
    },
    r"NVDA": {
        "underlier": "NVDA",
        "type": "stock",
        "description": "NVIDIA Corporation"
    },
    
    # Commodities
    r"GOLD|GLD": {
        "underlier": "GLD",
        "type": "commodity",
        "description": "Gold"
    },
    r"OIL|USO": {
        "underlier": "USO",
        "type": "commodity",
        "description": "Crude Oil"
    },
    r"SILVER|SLV": {
        "underlier": "SLV",
        "type": "commodity",
        "description": "Silver"
    },
    
    # Cryptocurrencies
    r"BTC|BITCOIN": {
        "underlier": "BTC-USD",
        "type": "crypto",
        "description": "Bitcoin"
    },
    r"ETH|ETHEREUM": {
        "underlier": "ETH-USD",
        "type": "crypto",
        "description": "Ethereum"
    }
}


def map_market_to_underlier(
    market: Dict,
    default_underlier: str = "SPY"
) -> Dict:
    """
    Map Kalshi market to option underlier.
    
    Args:
        market: Kalshi market dict
        default_underlier: Default underlier if no match found
        
    Returns:
        Dict with underlier, type, description, confidence
    """
    market_id = market.get("ticker", market.get("id", ""))
    title = market.get("title", "")
    market_text = f"{market_id} {title}".upper()
    
    # Try to match patterns
    for pattern, mapping in EVENT_MAP.items():
        if re.search(pattern, market_text):
            return {
                "underlier": mapping["underlier"],
                "type": mapping["type"],
                "description": mapping["description"],
                "confidence": "high",
                "matched_pattern": pattern
            }
    
    # No match found - use default
    return {
        "underlier": default_underlier,
        "type": "unknown",
        "description": "Unknown - using default",
        "confidence": "low",
        "matched_pattern": None
    }


def extract_expiry_from_market(market: Dict) -> Optional[str]:
    """
    Extract expiration date from Kalshi market.
    
    Args:
        market: Kalshi market dict
        
    Returns:
        Expiry string in YYYY-MM-DD format, or None
    """
    # Try to get from close_time or expiration_time
    close_time = market.get("close_time", "")
    if close_time:
        # Parse ISO format: "2026-03-15T20:00:00Z" -> "2026-03-15"
        return close_time.split("T")[0]
    
    # Try to extract from market_id (e.g., "INX-26MAR15")
    market_id = market.get("ticker", market.get("id", ""))
    date_match = re.search(r"(\d{2})([A-Z]{3})(\d{2})", market_id)
    if date_match:
        year = int("20" + date_match.group(1))
        month_abbr = date_match.group(2)
        day = int(date_match.group(3))
        
        # Convert month abbreviation to number
        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
            "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
            "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
        }
        month = month_map.get(month_abbr, 1)
        
        return f"{year:04d}-{month:02d}-{day:02d}"
    
    return None


def enrich_oracle_data_with_mapping(
    oracle_data: List[Dict],
    required_underlier: Optional[str] = None,
    fail_on_ambiguity: bool = True
) -> List[Dict]:
    """
    Enrich oracle data with underlier and expiry mappings.
    
    Args:
        oracle_data: List of oracle data dicts
        required_underlier: If set, enforce that ALL events map to this underlier
        fail_on_ambiguity: If True, raise error on low-confidence or missing mapping
        
    Returns:
        Enriched oracle data with underlier and expiry fields
        
    Raises:
        ValueError: If mapping is ambiguous and fail_on_ambiguity=True
    """
    import logging
    logger = logging.getLogger(__name__)
    
    enriched = []
    
    for data in oracle_data:
        raw_market = data.get("raw_market", {})
        market_id = raw_market.get("ticker", raw_market.get("id", "UNKNOWN"))
        
        # Map to underlier (no default if required_underlier is set)
        default = required_underlier if required_underlier else "SPY"
        mapping = map_market_to_underlier(raw_market, default)
        
        # FAIL FAST: Check for required underlier constraint
        if required_underlier and mapping["underlier"] != required_underlier:
            error_msg = (
                f"Market {market_id} maps to '{mapping['underlier']}' "
                f"but required underlier is '{required_underlier}'. "
                f"Confidence: {mapping['confidence']}"
            )
            logger.error(error_msg)
            if fail_on_ambiguity:
                raise ValueError(error_msg)
        
        # FAIL FAST: Check for low-confidence mapping
        if fail_on_ambiguity and mapping["confidence"] == "low":
            error_msg = (
                f"Market {market_id} has low-confidence mapping to "
                f"'{mapping['underlier']}'. Pattern not found in EVENT_MAP. "
                f"Cannot proceed with ambiguous mapping."
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Extract expiry
        expiry = extract_expiry_from_market(raw_market)
        
        # FAIL FAST: Check for missing expiry
        if fail_on_ambiguity and not expiry:
            error_msg = f"Market {market_id} has no extractable expiry date"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Add to data
        enriched_data = {
            **data,
            "underlier": mapping["underlier"],
            "underlier_type": mapping["type"],
            "underlier_description": mapping["description"],
            "mapping_confidence": mapping["confidence"],
            "expiry": expiry
        }
        
        enriched.append(enriched_data)
    
    logger.info(f"Enriched {len(enriched)} oracle events with underlier mappings")
    
    return enriched


def get_supported_underliers() -> List[str]:
    """
    Get list of supported underliers.
    
    Returns:
        List of underlier symbols
    """
    underliers = set()
    for mapping in EVENT_MAP.values():
        underliers.add(mapping["underlier"])
    return sorted(list(underliers))


def validate_mapping(market: Dict, expected_underlier: str) -> bool:
    """
    Validate that a market maps to expected underlier.
    
    Args:
        market: Kalshi market dict
        expected_underlier: Expected underlier symbol
        
    Returns:
        True if mapping matches
    """
    mapping = map_market_to_underlier(market)
    return mapping["underlier"] == expected_underlier
