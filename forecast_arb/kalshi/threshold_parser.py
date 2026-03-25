"""
Kalshi Threshold Parser: Series-aware market threshold extraction.

Correctly parses thresholds from Kalshi INX series markets, avoiding
false matches like extracting "500" from "S&P 500" in titles.

Design:
- Ticker patterns take priority over title parsing
- Series-specific rules (KXINX, KXINXY, KXINXMINY have different formats)
- Returns structured result with confidence and provenance
"""

import logging
import re
from typing import Dict, Any, Optional


logger = logging.getLogger(__name__)


def parse_threshold_from_market(
    market: Dict[str, Any],
    series: Optional[str] = None
) -> Dict[str, Any]:
    """
    Parse threshold or range from Kalshi market with series-aware rules.
    
    Parsing Priority:
    1. Ticker pattern (most reliable)
    2. Series-specific title patterns
    3. Generic fallback (low confidence)
    
    Args:
        market: Market dict with 'ticker' and 'title' keys
        series: Series ticker (e.g., 'KXINX', 'KXINXY', 'KXINXMINY')
                If None, attempts to infer from ticker
    
    Returns:
        Dict with structure:
        {
            "kind": "point" | "range" | "unknown",
            "threshold": float | None,  # For point thresholds
            "low": float | None,         # For ranges
            "high": float | None,        # For ranges
            "source": "ticker" | "title" | "fallback",
            "confidence": float  # 0-1 score
        }
    
    Examples:
        KXINX-26FEB27H1600-T7199.9999 -> point threshold 7199.9999
        KXINX-26FEB27H1600-T6500 -> point threshold 6500
        KXINX-26FEB27H1600-B7187 + title "between 7175 and 7199.9999" -> range
        KXINXMINY-01JAN2027-6600.01 -> point threshold 6600.01
    """
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    
    # Infer series from ticker if not provided
    if series is None:
        series = _infer_series_from_ticker(ticker)
    
    logger.debug(f"Parsing market: ticker={ticker}, series={series}")
    
    # Rule 1: KXINX / KXINXY "T" tickers (point threshold)
    if series in ("KXINX", "KXINXY"):
        # Pattern: KXINX-26FEB27H1600-T7199.9999
        t_match = re.search(r'-T([\d.]+)$', ticker)
        if t_match:
            try:
                threshold = float(t_match.group(1))
                return {
                    "kind": "point",
                    "threshold": threshold,
                    "low": None,
                    "high": None,
                    "source": "ticker",
                    "confidence": 1.0
                }
            except ValueError:
                logger.warning(f"Invalid threshold in ticker: {ticker}")
        
        # Rule 2: KXINX / KXINXY "B" tickers (range)
        # Pattern: KXINX-26FEB27H1600-B7187
        # Title: "Will the S&P 500 close between 7175 and 7199.9999..."
        b_match = re.search(r'-B\d+', ticker)
        if b_match:
            # Parse range from title
            range_result = _parse_range_from_title(title)
            if range_result:
                return {
                    "kind": "range",
                    "threshold": None,
                    "low": range_result["low"],
                    "high": range_result["high"],
                    "source": "title",
                    "confidence": 0.9  # High confidence for range parsing
                }
            else:
                # Failed to parse range - return unknown
                return {
                    "kind": "unknown",
                    "threshold": None,
                    "low": None,
                    "high": None,
                    "source": "ticker",
                    "confidence": 0.0
                }
    
    # Rule 3: KXINXMINY / KXINXMAXY tickers (point threshold)
    # Pattern: KXINXMINY-01JAN2027-6600.01
    if series in ("KXINXMINY", "KXINXMAXY"):
        # Extract final segment after last dash
        parts = ticker.split('-')
        if len(parts) >= 3:
            try:
                threshold = float(parts[-1])
                return {
                    "kind": "point",
                    "threshold": threshold,
                    "low": None,
                    "high": None,
                    "source": "ticker",
                    "confidence": 1.0
                }
            except ValueError:
                logger.warning(f"Invalid threshold in KXINXMINY ticker: {ticker}")
    
    # Fallback: Generic parsing (low confidence)
    # Avoid matching "500" from "S&P 500"!
    # Look for explicit threshold indicators in title
    fallback = _parse_threshold_fallback(title)
    if fallback:
        return fallback
    
    # Give up
    return {
        "kind": "unknown",
        "threshold": None,
        "low": None,
        "high": None,
        "source": "none",
        "confidence": 0.0
    }


def _infer_series_from_ticker(ticker: str) -> Optional[str]:
    """
    Infer series from ticker prefix.
    
    Args:
        ticker: Market ticker
    
    Returns:
        Series ticker or None
    """
    if ticker.startswith("KXINXMINY"):
        return "KXINXMINY"
    elif ticker.startswith("KXINXMAXY"):
        return "KXINXMAXY"
    elif ticker.startswith("KXINXY"):
        return "KXINXY"
    elif ticker.startswith("KXINX"):
        return "KXINX"
    else:
        return None


def _parse_range_from_title(title: str) -> Optional[Dict[str, float]]:
    """
    Parse range from title text.
    
    Pattern: "between X and Y" or "from X to Y"
    
    Args:
        title: Market title
    
    Returns:
        Dict with 'low' and 'high' keys, or None
    """
    # Pattern: "between 7175 and 7199.9999"
    # Must have explicit "between...and" or "from...to"
    pattern = r'\b(?:between|from)\s+([\d,]+(?:\.\d+)?)\s+(?:and|to)\s+([\d,]+(?:\.\d+)?)\b'
    match = re.search(pattern, title, re.IGNORECASE)
    
    if match:
        try:
            low_str = match.group(1).replace(',', '')
            high_str = match.group(2).replace(',', '')
            low = float(low_str)
            high = float(high_str)
            
            # Sanity check
            if low < high and low > 0:
                return {"low": low, "high": high}
        except ValueError:
            pass
    
    return None


def _parse_threshold_fallback(title: str) -> Optional[Dict[str, Any]]:
    """
    Fallback parser for titles without ticker hints.
    
    CRITICAL: Must NOT match "500" from "S&P 500"!
    
    Strategy: Only match numbers with explicit threshold indicators
    like "below", "above", "at", etc.
    
    Args:
        title: Market title
    
    Returns:
        Threshold dict or None
    """
    # Pattern: "below 6500" or "above 7200"
    # Must have explicit directional indicator
    pattern = r'\b(?:below|above|at|≤|≥|<|>)\s+([\d,]+(?:\.\d+)?)\b'
    match = re.search(pattern, title, re.IGNORECASE)
    
    if match:
        try:
            threshold_str = match.group(1).replace(',', '')
            threshold = float(threshold_str)
            
            # Sanity check: SPX thresholds are typically 1000-10000
            if 1000 <= threshold <= 20000:
                return {
                    "kind": "point",
                    "threshold": threshold,
                    "low": None,
                    "high": None,
                    "source": "fallback",
                    "confidence": 0.6  # Lower confidence for fallback
                }
        except ValueError:
            pass
    
    return None


def format_threshold_display(parsed: Dict[str, Any]) -> str:
    """
    Format parsed threshold for display.
    
    Args:
        parsed: Result from parse_threshold_from_market()
    
    Returns:
        Human-readable threshold string
    """
    kind = parsed.get("kind", "unknown")
    
    if kind == "point":
        threshold = parsed.get("threshold")
        if threshold is not None:
            return f"{threshold:.4g}"
        else:
            return "N/A"
    
    elif kind == "range":
        low = parsed.get("low")
        high = parsed.get("high")
        if low is not None and high is not None:
            return f"{low:.4g}–{high:.4g}"
        else:
            return "N/A"
    
    else:
        return "unknown"
