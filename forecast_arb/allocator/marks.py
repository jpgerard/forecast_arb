"""
CCC v1 Allocator - Spread mark computation.

Computes spread mid price from leg-level bid/ask quotes.
When live IBKR data is unavailable, marks remain None.

spread_mid = long_leg_mid - short_leg_mid
where leg_mid = (bid + ask) / 2

All values are in dollars-per-contract (same convention as debit_per_contract).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .types import SleevePosition


def _leg_mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Compute mid from bid/ask.  Returns None if both are absent or invalid."""
    if bid is None and ask is None:
        return None
    if bid is None:
        return float(ask) if ask is not None else None
    if ask is None:
        return float(bid)
    return (float(bid) + float(ask)) / 2.0


def compute_spread_mid(
    long_bid: Optional[float],
    long_ask: Optional[float],
    short_bid: Optional[float],
    short_ask: Optional[float],
) -> Optional[float]:
    """
    Compute put-debit spread mid price.

    For a put debit spread (long high-strike put, short low-strike put):
        spread_mid = long_mid - short_mid

    Returns None if either leg mid cannot be computed.
    Negative values are clamped to 0 (intrinsic floor for spread).
    """
    long_mid = _leg_mid(long_bid, long_ask)
    short_mid = _leg_mid(short_bid, short_ask)

    if long_mid is None or short_mid is None:
        return None

    # spread_mid is long - short; clamp at 0 (debit can't be negative)
    return max(0.0, long_mid - short_mid)


def populate_marks_from_leg_quotes(
    positions: List[SleevePosition],
    leg_quotes: Dict[str, Dict[str, Any]],
) -> List[SleevePosition]:
    """
    Populate mark_mid on each position from a leg-quote lookup.

    Args:
        positions:   List of SleevePosition objects
        leg_quotes:  Lookup keyed by (underlier, expiry, strike, right) → {bid, ask}
                     e.g. ("SPY", "20260402", 580.0, "P") → {"bid": 1.53, "ask": 1.56}
                     Values are per-share (multiply by 100 for $/contract).

    Returns:
        Same positions list with mark_mid populated where possible.
        Positions without leg quotes keep mark_mid = None.
    """
    for pos in positions:
        if len(pos.strikes) < 2:
            continue

        long_strike = pos.strikes[0]   # highest strike (long put)
        short_strike = pos.strikes[1]  # lower strike (short put)

        long_key = (pos.underlier, pos.expiry, long_strike, "P")
        short_key = (pos.underlier, pos.expiry, short_strike, "P")

        long_q = leg_quotes.get(long_key, {})
        short_q = leg_quotes.get(short_key, {})

        long_bid = long_q.get("bid")
        long_ask = long_q.get("ask")
        short_bid = short_q.get("bid")
        short_ask = short_q.get("ask")

        per_share_mid = compute_spread_mid(long_bid, long_ask, short_bid, short_ask)

        if per_share_mid is not None:
            # Convert per-share to per-contract (100 shares per standard equity option)
            pos.mark_mid = per_share_mid * 100.0

    return positions


def populate_marks_from_candidates(
    positions: List[SleevePosition],
    candidates: List[Dict[str, Any]],
) -> List[SleevePosition]:
    """
    Populate mark_mid from candidate leg arrays (best-effort when live quotes absent).

    Uses the 'legs' field in candidate dicts (bid/ask per leg).
    This is a fallback: candidates reflect structuring-time prices, not current marks.

    Args:
        positions:  SleevePosition list
        candidates: Candidate dicts with 'legs' arrays

    Returns:
        Positions with mark_mid set where candidate data matches.
    """
    # Build lookup: (underlier, expiry, long_strike, short_strike) → spread_mid
    candidate_marks: Dict[Tuple, float] = {}

    for c in candidates:
        underlier = str(c.get("underlier", "")).upper()
        expiry = str(c.get("expiry", ""))
        strikes = c.get("strikes", {})
        long_strike = float(strikes.get("long_put", 0))
        short_strike = float(strikes.get("short_put", 0))

        if not (underlier and expiry and long_strike and short_strike):
            continue

        # Use debit_per_contract as current mark proxy (structuring-time premium)
        debit = c.get("debit_per_contract")
        if debit is not None:
            key = (underlier, expiry, long_strike, short_strike)
            candidate_marks[key] = float(debit)

    for pos in positions:
        if pos.mark_mid is not None:
            continue  # already populated
        if len(pos.strikes) < 2:
            continue

        long_strike = pos.strikes[0]
        short_strike = pos.strikes[1]
        key = (pos.underlier, pos.expiry, long_strike, short_strike)
        mark = candidate_marks.get(key)
        if mark is not None:
            pos.mark_mid = mark

    return positions
