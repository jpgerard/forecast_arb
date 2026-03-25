"""
Live Quote Snapshot Utility (Read-Only, Diagnostic)

Fetches live bid/ask/mid for individual legs and BAG combo spreads.
NO orders, NO staging, NO transmit.

This is for decision-time diagnostics only.
"""

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def fetch_live_quotes(
    underlier: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
    right: str = "P",
    qty: int = 1,
    snapshot: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Fetch live quotes for a vertical spread (read-only diagnostic).
    
    Args:
        underlier: Ticker symbol (e.g., "SPY")
        expiry: Expiry date in YYYYMMDD format
        long_strike: Strike price for long leg
        short_strike: Strike price for short leg
        right: Option right ("P" or "C")
        qty: Quantity (default 1)
        snapshot: IBKR snapshot to extract quotes from (if available)
        
    Returns:
        Dict with live quotes:
        {
            "timestamp": "...",
            "long_leg": {"bid": x, "ask": y, "mid": z, "source": "snapshot"},
            "short_leg": {"bid": x, "ask": y, "mid": z, "source": "snapshot"},
            "spread_synthetic": {"bid": x, "ask": y, "mid": z},
            "spread_combo": {"bid": x, "ask": y, "mid": z} | null,
            "warnings": []
        }
    """
    warnings = []
    timestamp = datetime.now(timezone.utc).isoformat()
    
    # Extract quotes from snapshot if available
    long_leg_quote = None
    short_leg_quote = None
    
    if snapshot:
        try:
            long_leg_quote = _extract_quote_from_snapshot(
                snapshot, expiry, long_strike, right
            )
            short_leg_quote = _extract_quote_from_snapshot(
                snapshot, expiry, short_strike, right
            )
        except Exception as e:
            logger.warning(f"Failed to extract quotes from snapshot: {e}")
            warnings.append(f"SNAPSHOT_EXTRACTION_FAILED: {str(e)}")
    
    # Build result
    result = {
        "timestamp": timestamp,
        "long_leg": long_leg_quote or {
            "bid": None,
            "ask": None,
            "mid": None,
            "source": "unavailable"
        },
        "short_leg": short_leg_quote or {
            "bid": None,
            "ask": None,
            "mid": None,
            "source": "unavailable"
        },
        "spread_synthetic": None,
        "spread_combo": None,  # Always null for now (combo quotes not implemented)
        "warnings": warnings
    }
    
    # Compute synthetic spread if both legs available
    if long_leg_quote and short_leg_quote:
        long_bid = long_leg_quote.get("bid")
        long_ask = long_leg_quote.get("ask")
        long_mid = long_leg_quote.get("mid")
        
        short_bid = short_leg_quote.get("bid")
        short_ask = short_leg_quote.get("ask")
        short_mid = short_leg_quote.get("mid")
        
        # Synthetic spread: debit = (long_ask - short_bid) for natural debit
        # For debit spread: we BUY long leg (pay ask), SELL short leg (receive bid)
        synthetic_bid = None
        synthetic_ask = None
        synthetic_mid = None
        
        if long_ask is not None and short_bid is not None:
            # Natural debit (what we'd pay in worst case)
            synthetic_ask = long_ask - short_bid
        
        if long_bid is not None and short_ask is not None:
            # Best case debit (if we could get filled at best prices)
            synthetic_bid = long_bid - short_ask
        
        if long_mid is not None and short_mid is not None:
            # Mid-price debit
            synthetic_mid = long_mid - short_mid
        
        result["spread_synthetic"] = {
            "bid": synthetic_bid,
            "ask": synthetic_ask,
            "mid": synthetic_mid
        }
        
        # Check for executable quotes
        if long_bid is None or long_ask is None:
            warnings.append("LONG_LEG_NOT_EXECUTABLE")
        if short_bid is None or short_ask is None:
            warnings.append("SHORT_LEG_NOT_EXECUTABLE")
        
        # Check spread width
        if synthetic_bid is not None and synthetic_ask is not None:
            spread_width = synthetic_ask - synthetic_bid
            if spread_width > synthetic_mid * 0.20 if synthetic_mid else False:
                warnings.append(f"WIDE_SPREAD: {spread_width:.4f}")
    else:
        warnings.append("INCOMPLETE_LEG_QUOTES")
    
    result["warnings"] = warnings
    
    return result


def _extract_quote_from_snapshot(
    snapshot: Dict[str, Any],
    expiry: str,
    strike: float,
    right: str
) -> Optional[Dict[str, Any]]:
    """
    Extract bid/ask/mid for a single option from snapshot.
    
    Args:
        snapshot: IBKR snapshot dict
        expiry: Expiry in YYYYMMDD format
        strike: Strike price
        right: Option right ("P" or "C")
        
    Returns:
        Dict with {"bid": x, "ask": y, "mid": z, "source": "snapshot"} or None
    """
    # Navigate snapshot structure
    contracts = snapshot.get("data", {}).get("contracts", {})
    
    # Find matching contract
    for contract_key, contract_data in contracts.items():
        if (contract_data.get("expiry") == expiry and
            contract_data.get("strike") == strike and
            contract_data.get("right") == right):
            
            # Extract pricing
            bid = contract_data.get("bid")
            ask = contract_data.get("ask")
            
            # Compute mid
            mid = None
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2
            
            return {
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "source": "snapshot",
                "last": contract_data.get("last"),
                "volume": contract_data.get("volume"),
                "open_interest": contract_data.get("open_interest")
            }
    
    return None


def fetch_quotes_for_candidates(
    candidates: List[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]] = None,
    max_candidates: int = 3
) -> Dict[int, Dict[str, Any]]:
    """
    Fetch live quotes for multiple candidates.
    
    Args:
        candidates: List of candidate structures
        snapshot: IBKR snapshot to extract quotes from
        max_candidates: Maximum number of candidates to fetch quotes for
        
    Returns:
        Dict mapping rank -> quote data
    """
    results = {}
    
    for candidate in candidates[:max_candidates]:
        rank = candidate.get("rank", 0)
        expiry = candidate.get("expiry")
        strikes = candidate.get("strikes", {})
        long_strike = strikes.get("long_put")
        short_strike = strikes.get("short_put")
        
        if not all([expiry, long_strike, short_strike]):
            logger.warning(f"Candidate rank {rank} missing required fields")
            results[rank] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "warnings": ["MISSING_CANDIDATE_FIELDS"]
            }
            continue
        
        try:
            quote = fetch_live_quotes(
                underlier="SPY",  # Hardcoded for now
                expiry=expiry,
                long_strike=long_strike,
                short_strike=short_strike,
                right="P",
                snapshot=snapshot
            )
            results[rank] = quote
        except Exception as e:
            logger.error(f"Failed to fetch quotes for rank {rank}: {e}")
            results[rank] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "warnings": [f"FETCH_FAILED: {str(e)}"]
            }
    
    return results
