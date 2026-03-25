"""
CCC v2.0 Allocator - Premium-at-risk computation helpers.

Shared between:
  - allocator (open_plan.py)  — for primary gate evaluation
  - reporting (ccc_report.py) — for Section B display

Semantic consistency invariant:
  The premium-at-risk shown in the report and used for gating must be
  computed from the same logic.  All callers go through this module.

Priority for debit basis (per spec §Task B):
  1. entry_debit_net   (net of commissions — most accurate)
  2. entry_debit       (legacy gross, from plan ledger)
  3. entry_debit_gross (from positions.json / reconcile snapshot)

Works with both SleevePosition objects and raw dict positions (positions.json).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Single-position helper
# ---------------------------------------------------------------------------

def _get_debit_basis(position: Any) -> Optional[float]:
    """
    Extract the debit basis from a position (SleevePosition or dict).

    Priority: entry_debit_net → entry_debit → entry_debit_gross
    Returns None if all fields are absent or zero/negative.
    """
    # Try as object (SleevePosition) first, then as dict
    if hasattr(position, "entry_debit_net"):
        # SleevePosition dataclass
        val = (
            getattr(position, "entry_debit_net", None)
            or getattr(position, "entry_debit", None)
        )
    else:
        # Raw dict (from positions.json or test fixtures)
        val = (
            position.get("entry_debit_net")
            or position.get("entry_debit")
            or position.get("entry_debit_gross")
        )

    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _get_qty_open(position: Any) -> int:
    """Extract qty_open from a position (SleevePosition or dict)."""
    if hasattr(position, "qty_open"):
        qty = getattr(position, "qty_open", 0)
    else:
        qty = position.get("qty_open", 0)
    try:
        return max(0, int(qty or 0))
    except (TypeError, ValueError):
        return 0


def _get_regime(position: Any) -> str:
    """Extract regime string from a position, lowercased."""
    if hasattr(position, "regime"):
        return str(getattr(position, "regime", "") or "").lower()
    return str(position.get("regime", "") or "").lower()


def compute_position_premium_at_risk(position: Any) -> float:
    """
    Compute premium-at-risk for a single position.

    PAR = debit_basis * qty_open

    Rules:
      - Use entry_debit_net if present
      - else entry_debit
      - else entry_debit_gross
      - multiply by qty_open (must be positive; zero/negative → 0.0)
      - missing debit → returns 0.0 (safe; never raises)

    Args:
        position: SleevePosition dataclass OR dict (positions.json entry)

    Returns:
        float: premium-at-risk in dollars (0.0 when data is missing)
    """
    qty = _get_qty_open(position)
    if qty <= 0:
        return 0.0

    basis = _get_debit_basis(position)
    if basis is None:
        return 0.0

    return round(basis * qty, 4)


# ---------------------------------------------------------------------------
# Portfolio-level helper
# ---------------------------------------------------------------------------

def compute_portfolio_premium_at_risk(positions: List[Any]) -> Dict[str, float]:
    """
    Compute premium-at-risk by regime across all open positions.

    Returns:
        {
            "crash":   float,   # total PAR for crash positions
            "selloff": float,   # total PAR for selloff positions
            "total":   float,   # grand total PAR
        }

    Notes:
      - Uses compute_position_premium_at_risk() per position (no debit → 0.0)
      - Deterministic: result is the same on repeated calls with same input
      - Handles empty positions list gracefully
    """
    result: Dict[str, float] = {"crash": 0.0, "selloff": 0.0, "total": 0.0}

    for pos in positions:
        par = compute_position_premium_at_risk(pos)
        if par <= 0.0:
            continue

        regime = _get_regime(pos)
        if regime in ("crash", "selloff"):
            result[regime] = round(result[regime] + par, 4)
        result["total"] = round(result["total"] + par, 4)

    return result
