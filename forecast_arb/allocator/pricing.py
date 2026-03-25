"""
CCC v1.9 Allocator — Worst-case debit pricing utilities (Task A).

Computes mid and worst-case (ask/bid) debit prices from leg quotes.
Safe fallbacks when one or both sides are missing; callers decide what
to do with None values (e.g. fall back to campaign-computed premium).

Convention (matches IBKR):
  long leg  → we BUY   → pay the ASK (worst case)
  short leg → we SELL  → receive the BID (worst case)

All values are per-share (÷100 of per-contract).
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def compute_debit_mid(
    long_mid: Optional[float],
    short_mid: Optional[float],
) -> Optional[float]:
    """
    Mid debit per share = long_mid − short_mid.

    Returns None if either side is missing.
    Returns 0.0 if result is negative (trivial / near-zero spread).
    """
    if long_mid is None or short_mid is None:
        return None
    return max(0.0, long_mid - short_mid)


def compute_debit_worstcase(
    long_ask: Optional[float],
    short_bid: Optional[float],
) -> Optional[float]:
    """
    Worst-case debit per share = long_ask − short_bid.

    We pay the ask on the long leg and receive only the bid on the short leg.
    This is the maximum realistic execution cost of the spread.

    Returns None if either quote is missing.
    Returns 0.0 if result is negative (crossed market / bad data guard).
    """
    if long_ask is None or short_bid is None:
        return None
    return max(0.0, long_ask - short_bid)


def compute_premium_per_contract(debit_share: float) -> float:
    """
    Convert per-share debit to per-contract premium.

    Standard option contract = 100 shares.
    """
    return debit_share * 100.0


def extract_leg_quotes(candidate: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    Extract bid / ask / mid per-share quotes for both legs from a candidate dict.

    Expected candidate fields (all optional):
      long_bid, long_ask, long_mid
      short_bid, short_ask, short_mid

    Returns a dict with those six keys; any missing values are None.
    """
    def _safe(val: Any) -> Optional[float]:
        if val is None:
            return None
        try:
            f = float(val)
            return f if f >= 0 else None
        except (TypeError, ValueError):
            return None

    return {
        "long_bid":  _safe(candidate.get("long_bid")),
        "long_ask":  _safe(candidate.get("long_ask")),
        "long_mid":  _safe(candidate.get("long_mid")),
        "short_bid": _safe(candidate.get("short_bid")),
        "short_ask": _safe(candidate.get("short_ask")),
        "short_mid": _safe(candidate.get("short_mid")),
    }


def compute_pricing_detail(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute the full pricing detail dict for a candidate.

    Returns a dict suitable for storing in AllocatorAction.pricing:
      long_bid / long_ask / long_mid
      short_bid / short_ask / short_mid
      debit_mid_share    — per-share mid debit   (None if quotes missing)
      debit_wc_share     — per-share worst-case   (None if quotes missing)
      premium_mid        — per-contract mid        (None if quotes missing)
      premium_wc         — per-contract worst-case (None if quotes missing)
      has_quotes         — True if any leg quote is present
    """
    quotes = extract_leg_quotes(candidate)

    debit_mid_share = compute_debit_mid(quotes["long_mid"], quotes["short_mid"])
    debit_wc_share  = compute_debit_worstcase(quotes["long_ask"], quotes["short_bid"])

    premium_mid = (
        compute_premium_per_contract(debit_mid_share)
        if debit_mid_share is not None else None
    )
    premium_wc = (
        compute_premium_per_contract(debit_wc_share)
        if debit_wc_share is not None else None
    )

    has_quotes = any(v is not None for v in quotes.values())

    return {
        **quotes,
        "debit_mid_share": debit_mid_share,
        "debit_wc_share":  debit_wc_share,
        "premium_mid":     premium_mid,
        "premium_wc":      premium_wc,
        "has_quotes":      has_quotes,
    }
