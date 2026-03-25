"""
CCC v1 Allocator - Position reconciliation.

Maps allocator ledger open records → SleevePosition objects.

Entry debit source of truth:
  1. Read allocator_ledger.jsonl for OPEN records (preferred - written at time of open)
  2. If debit absent → entry_debit = None → only time-stop rules apply

Does NOT read IBKR directly (no execution engine dependency).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .inventory import list_open_trades
from .types import SleevePosition


def _compute_dte(expiry_str: str) -> Optional[int]:
    """
    Compute calendar days to expiry from today (UTC).

    Args:
        expiry_str: 'YYYYMMDD' or 'YYYY-MM-DD'

    Returns:
        DTE integer, or None if unparseable
    """
    today = datetime.now(timezone.utc).date()
    try:
        if len(expiry_str) == 8:
            exp = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
        else:
            exp = date.fromisoformat(expiry_str)
        return (exp - today).days
    except (ValueError, TypeError):
        return None


def reconcile_positions(ledger_path: Path) -> List[SleevePosition]:
    """
    Build SleevePosition list from allocator ledger open records.

    Rules:
    - An OPEN record must have: trade_id, regime, underlier, expiry, strikes (list), qty
    - entry_debit is taken from the OPEN record's 'premium_per_contract' field
    - If missing → entry_debit = None (time-stop only)
    - mark_mid is None at this stage (filled later by marks.py)

    Args:
        ledger_path: Path to allocator_ledger.jsonl

    Returns:
        List of SleevePosition (one per open trade)
    """
    open_records = list_open_trades(ledger_path)
    positions: List[SleevePosition] = []
    warnings: List[str] = []

    for rec in open_records:
        trade_id = rec.get("trade_id", "")
        regime = rec.get("regime", "").lower()
        underlier = rec.get("underlier", "")
        expiry = rec.get("expiry", "")
        strikes_raw = rec.get("strikes")
        qty = int(rec.get("qty", 1))
        candidate_id = rec.get("candidate_id")

        # Validate required fields - fail loud on canonical field issues
        missing = []
        if not trade_id:
            missing.append("trade_id")
        if not regime:
            missing.append("regime")
        if not underlier:
            missing.append("underlier")
        if not expiry:
            missing.append("expiry")
        if not strikes_raw:
            missing.append("strikes")
        if qty <= 0:
            missing.append("qty>0")

        if missing:
            warnings.append(
                f"OPEN record trade_id={trade_id!r} missing required fields: {missing} - skipping"
            )
            continue

        # Normalise strikes to sorted-descending list (long strike > short for put debit)
        try:
            strikes = sorted([float(s) for s in strikes_raw], reverse=True)
        except (TypeError, ValueError):
            warnings.append(f"trade_id={trade_id}: invalid strikes {strikes_raw!r} - skipping")
            continue

        if len(strikes) < 2:
            warnings.append(f"trade_id={trade_id}: need at least 2 strikes, got {strikes} - skipping")
            continue

        # Entry debit from ledger record
        raw_debit = rec.get("premium_per_contract") or rec.get("entry_debit")
        if raw_debit is not None:
            try:
                entry_debit = float(raw_debit)
                if entry_debit <= 0:
                    entry_debit = None
            except (ValueError, TypeError):
                entry_debit = None
        else:
            entry_debit = None

        dte = _compute_dte(expiry)

        positions.append(
            SleevePosition(
                trade_id=trade_id,
                underlier=underlier,
                expiry=expiry,
                strikes=strikes,
                qty_open=qty,
                regime=regime,
                entry_debit=entry_debit,
                mark_mid=None,
                dte=dte,
                candidate_id=candidate_id,
            )
        )

    # Log any warnings
    for w in warnings:
        import logging
        logging.getLogger(__name__).warning(w)

    return positions


def reconcile_from_ibkr_stubs(
    ibkr_positions: List[Dict[str, Any]],
    ledger_path: Path,
) -> List[SleevePosition]:
    """
    Group raw IBKR option positions into spreads and match with ledger entry debits.

    Grouping rules (put debit spreads):
    - Same underlier + expiry
    - Same right (P)
    - Exactly two legs with opposite-sign qty
    - Long strike > short strike
    - If grouping is ambiguous → mark as UNRECONCILED (skip, no action)

    Args:
        ibkr_positions: List of IBKR option position dicts (from snapshot)
        ledger_path:    Allocator ledger for entry debit lookup

    Returns:
        List of SleevePosition (UNRECONCILED ones excluded)
    """
    import logging
    log = logging.getLogger(__name__)

    # Build a lookup of known entry debits from ledger (by strikes + expiry + underlier)
    open_records = list_open_trades(ledger_path)
    # Key: (underlier, expiry, long_strike, short_strike)
    debit_by_key: Dict[tuple, Dict[str, Any]] = {}
    for rec in open_records:
        underlier = rec.get("underlier", "").upper()
        expiry = rec.get("expiry", "")
        strikes = rec.get("strikes", [])
        if len(strikes) >= 2:
            key = (underlier, expiry, float(strikes[0]), float(strikes[1]))
            debit_by_key[key] = rec

    # Filter to put options only
    put_positions = [p for p in ibkr_positions if str(p.get("right", "")).upper() == "P"]

    # Group by (underlier, expiry)
    from collections import defaultdict
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for pos in put_positions:
        underlier = str(pos.get("symbol", pos.get("underlier", ""))).upper()
        expiry = str(pos.get("expiry", pos.get("lastTradeDateOrContractMonth", "")))
        groups[(underlier, expiry)].append(pos)

    positions: List[SleevePosition] = []

    for (underlier, expiry), legs in groups.items():
        # Find pairs with opposite-sign qty
        long_legs = [l for l in legs if float(l.get("position", l.get("qty", 0))) > 0]
        short_legs = [l for l in legs if float(l.get("position", l.get("qty", 0))) < 0]

        if len(long_legs) != 1 or len(short_legs) != 1:
            log.warning(
                f"UNRECONCILED: {underlier} {expiry} - "
                f"expected 1 long+1 short leg, got {len(long_legs)}L+{len(short_legs)}S"
            )
            continue

        long_leg = long_legs[0]
        short_leg = short_legs[0]

        long_strike = float(long_leg.get("strike", 0))
        short_strike = float(short_leg.get("strike", 0))

        if long_strike <= short_strike:
            log.warning(
                f"UNRECONCILED: {underlier} {expiry} - "
                f"long_strike ({long_strike}) not > short_strike ({short_strike})"
            )
            continue

        qty = abs(int(float(long_leg.get("position", long_leg.get("qty", 1)))))

        # Look up entry debit
        key = (underlier, expiry, long_strike, short_strike)
        rec = debit_by_key.get(key)
        entry_debit = None
        trade_id = f"ibkr_{underlier}_{expiry}_{long_strike:.0f}_{short_strike:.0f}"
        regime = "unknown"
        candidate_id = None

        if rec:
            raw_debit = rec.get("premium_per_contract") or rec.get("entry_debit")
            if raw_debit:
                try:
                    entry_debit = float(raw_debit)
                except (ValueError, TypeError):
                    entry_debit = None
            trade_id = rec.get("trade_id", trade_id)
            regime = rec.get("regime", "unknown").lower()
            candidate_id = rec.get("candidate_id")
        else:
            log.warning(
                f"MISSING_ENTRY_DEBIT: {underlier} {expiry} {long_strike}/{short_strike} "
                f"not found in allocator ledger - time-stop only"
            )

        dte = _compute_dte(expiry)

        positions.append(
            SleevePosition(
                trade_id=trade_id,
                underlier=underlier,
                expiry=expiry,
                strikes=[long_strike, short_strike],
                qty_open=qty,
                regime=regime,
                entry_debit=entry_debit,
                mark_mid=None,
                dte=dte,
                candidate_id=candidate_id,
            )
        )

    return positions
