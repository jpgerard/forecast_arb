"""
CCC v1 Allocator - Convexity scoring (Phase 2A Task B).

Provides compute_convexity_score() for ranking candidates by payoff quality.

Purpose:
    This score is used ONLY for candidate ranking; it does NOT replace or
    modify existing policy pass/fail gates (EV/$ threshold, convexity_multiple
    threshold, annual budget cap, etc.).  After all gates are applied,
    convexity_score orders the remaining candidates so that better payoff
    geometry — not just better EV/$ — floats to the top.

Formula:
    payoff_multiple = max_gain_per_contract / premium_per_contract
    score           = ev_per_dollar × payoff_multiple × p_used

    Optional liquidity penalty (only when spread_width is available):
        score *= exp(−spread_width / premium_per_contract)
    A tighter spread → penalty closer to 1.0 → higher score.
    A wider spread   → penalty approaches 0.0 → lower score.

Fallback behaviour:
    If any required field (ev_per_dollar, max_gain_per_contract,
    premium_per_contract, p_used) is absent or non-positive, returns 0.0.
    This ensures ranking degrades gracefully to ev_per_dollar tiebreaking
    when the scoring inputs are unavailable (e.g. campaign-only candidates
    that lack live quote data).

No external dependencies; no side effects; deterministic.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_convexity_score(candidate: Dict[str, Any]) -> float:
    """
    Compute the convexity score for a single candidate.

    A higher score indicates better expected payoff geometry, accounting for
    probability, EV quality, and payoff multiple.  When both score and EV/$
    are available, use score as the primary ranking signal within a regime.

    Args:
        candidate: Dict from campaign output (recommended.json / candidates_flat.json).
                   Required fields: ev_per_dollar, max_gain_per_contract,
                   computed_premium_usd or debit_per_contract, p_used (or alias).
                   Optional: spread_width (for liquidity penalty).

    Returns:
        Non-negative float.  0.0 when any required input is missing or
        non-positive (graceful fallback to ev_per_dollar tie-breaking).
    """
    # --- Required inputs ---
    ev = _safe_positive_float(candidate, "ev_per_dollar")
    if ev is None:
        return 0.0

    premium = _extract_premium(candidate)
    if premium is None:
        return 0.0

    max_gain = _safe_positive_float(candidate, "max_gain_per_contract")
    if max_gain is None:
        return 0.0

    p_used = _extract_p_used(candidate)
    if p_used is None:
        return 0.0

    # --- Core formula ---
    payoff_multiple = max_gain / premium
    score = ev * payoff_multiple * p_used

    # --- Optional liquidity penalty ---
    spread_width = _safe_positive_float(candidate, "spread_width")
    if spread_width is not None and premium > 0:
        try:
            score *= math.exp(-spread_width / premium)
        except (OverflowError, ValueError):
            # If math.exp overflows (astronomically wide spread), skip penalty.
            pass

    return max(0.0, score)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_positive_float(candidate: Dict[str, Any], key: str) -> Optional[float]:
    """
    Extract a positive float from candidate[key].

    Returns None if the key is absent, the value is non-numeric, zero, or
    negative — any of which would make the score undefined.
    """
    val = candidate.get(key)
    if val is None:
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    return f if f > 0 else None


def _extract_premium(candidate: Dict[str, Any]) -> Optional[float]:
    """
    Extract premium-per-contract from candidate, trying multiple field names.

    Field priority (same as open_plan._get_premium):
      1. computed_premium_usd
      2. debit_per_contract
    """
    for key in ("computed_premium_usd", "debit_per_contract"):
        result = _safe_positive_float(candidate, key)
        if result is not None:
            return result
    return None


def _extract_p_used(candidate: Dict[str, Any]) -> Optional[float]:
    """
    Extract probability used, trying multiple field aliases.

    Returns None if no valid probability (0 < p <= 1) is found.
    Field priority (same as open_plan._get_p_used):
      p_used → p_event_used → assumed_p_event → p_used_value
    """
    for key in ("p_used", "p_event_used", "assumed_p_event", "p_used_value"):
        val = candidate.get(key)
        if val is None:
            continue
        try:
            f = float(val)
        except (ValueError, TypeError):
            continue
        if 0 < f <= 1:
            return f
    return None
