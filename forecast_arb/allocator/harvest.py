"""
CCC v1 Allocator - Harvest / roll action generation.

Applies mechanical harvest rules to each SleevePosition and generates
HARVEST_CLOSE or ROLL_CLOSE AllocatorAction intents.

Rules (from policy harvest section):
  partial_close_multiple: e.g. 2.0  → if mark_mid >= 2.0x entry_debit → close 50%
  full_close_multiple:    e.g. 3.0  → if mark_mid >= 3.0x entry_debit → close remainder
  time_stop_dte:          e.g. 14   → if DTE <= 14 and...
  time_stop_min_multiple: e.g. 1.2  → ...and mark < 1.2x entry_debit → close (roll eligible)
  partial_close_fraction: e.g. 0.5  → fraction to close on partial trigger (ceil)

Close-liquidity guard (Task 5):
  Before issuing HARVEST_CLOSE or ROLL_CLOSE, check the spread bid/ask width.
  If (ask - bid) / mid > close_liquidity_guard.max_width_pct → emit HOLD instead
  with reason code WIDE_MARKET_NO_CLOSE. Guard only applies when spread_bid and
  spread_ask are populated on the SleevePosition (live IBKR quotes).

Output:
  HARVEST_CLOSE at 2× (partial)
  HARVEST_CLOSE at 3× (full)
  ROLL_CLOSE at time-stop (close; caller may schedule re-open)
  HOLD with WIDE_MARKET_NO_CLOSE when spread is too wide

If entry_debit is None → only time-stop (DTE-based) applies.
If mark_mid is None → no multiple-based rules, time-stop only applies if DTE known.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .types import ActionType, AllocatorAction, SleevePosition


def generate_harvest_actions(
    positions: List[SleevePosition],
    policy: Dict[str, Any],
) -> List[AllocatorAction]:
    """
    Given a list of reconciled positions (with mark_mid populated), apply harvest rules.

    Args:
        positions: SleevePosition list (mark_mid may be None)
        policy:    Validated policy dict

    Returns:
        List of AllocatorAction (HARVEST_CLOSE, ROLL_CLOSE, or HOLD for wide-market).
        Positions with no triggered rule produce no action here.
    """
    hp = policy["harvest"]
    partial_mult = float(hp["partial_close_multiple"])
    full_mult = float(hp["full_close_multiple"])
    ts_dte = int(hp["time_stop_dte"])
    ts_min_mult = float(hp["time_stop_min_multiple"])
    partial_frac = float(hp["partial_close_fraction"])

    # Task 5: close-liquidity guard
    guard_cfg = policy.get("close_liquidity_guard", {})
    max_width_pct = float(guard_cfg.get("max_width_pct", 1.0))  # default 100% = effectively off

    actions: List[AllocatorAction] = []

    for pos in positions:
        action = _evaluate_position(
            pos=pos,
            partial_mult=partial_mult,
            full_mult=full_mult,
            ts_dte=ts_dte,
            ts_min_mult=ts_min_mult,
            partial_frac=partial_frac,
            max_width_pct=max_width_pct,
        )
        if action is not None:
            actions.append(action)

    return actions


def _evaluate_position(
    pos: SleevePosition,
    partial_mult: float,
    full_mult: float,
    ts_dte: int,
    ts_min_mult: float,
    partial_frac: float,
    max_width_pct: float,
) -> AllocatorAction | None:
    """
    Evaluate a single position and return an action or None.

    Priority order (highest wins):
      0. Close-liquidity guard check (blocks all close types)
      1. Full close (≥ 3×)
      2. Partial close (≥ 2×)
      3. Time-stop close

    Returns the first triggered rule's action, or None if no rule triggers.
    HOLD is returned (not None) when the liquidity guard blocks a close.
    """
    multiple = pos.multiple  # None if entry_debit or mark_mid missing

    # ---- Determine if any close rule would trigger ----
    # We need to check the guard only if we're about to emit a close action.

    def _liquidity_guard_blocked() -> AllocatorAction | None:
        """
        Returns a HOLD action if spread is too wide, else None.
        Only fires when spread_bid and spread_ask are both present.
        """
        if pos.spread_bid is None or pos.spread_ask is None:
            return None  # No data → guard not applied
        if pos.mark_mid is None or pos.mark_mid <= 0:
            return None  # No mid → can't compute ratio

        spread_width = pos.spread_ask - pos.spread_bid
        if spread_width < 0:
            return None  # Bad data → skip guard
        width_pct = spread_width / pos.mark_mid
        if width_pct > max_width_pct:
            return AllocatorAction(
                type=ActionType.HOLD,
                trade_id=pos.trade_id,
                reason_codes=[
                    "WIDE_MARKET_NO_CLOSE",
                    f"SPREAD_WIDTH_PCT:{width_pct:.1%}>{max_width_pct:.1%}",
                    f"BID:{pos.spread_bid:.2f}_ASK:{pos.spread_ask:.2f}_MID:{pos.mark_mid:.2f}",
                ],
            )
        return None

    # ---- Multiple-based rules (require both entry_debit and mark_mid) ----
    if multiple is not None:
        # Full close (3×+)
        if multiple >= full_mult:
            blocked = _liquidity_guard_blocked()
            if blocked:
                return blocked
            return AllocatorAction(
                type=ActionType.HARVEST_CLOSE,
                trade_id=pos.trade_id,
                qty=pos.qty_open,  # close all remaining
                reason_codes=[
                    f"FULL_CLOSE_MULTIPLE:{multiple:.2f}x>={full_mult}x",
                    f"MARK_MID:{pos.mark_mid:.2f}" if pos.mark_mid else "MARK_MID:N/A",
                ],
                premium=pos.mark_mid,
            )

        # Partial close (2×+, but < 3×)
        if multiple >= partial_mult:
            blocked = _liquidity_guard_blocked()
            if blocked:
                return blocked
            close_qty = _partial_close_qty(pos.qty_open, partial_frac)
            return AllocatorAction(
                type=ActionType.HARVEST_CLOSE,
                trade_id=pos.trade_id,
                qty=close_qty,
                reason_codes=[
                    f"PARTIAL_CLOSE_MULTIPLE:{multiple:.2f}x>={partial_mult}x",
                    f"MARK_MID:{pos.mark_mid:.2f}" if pos.mark_mid else "MARK_MID:N/A",
                    f"CLOSE_QTY:{close_qty}_OF_{pos.qty_open}",
                ],
                premium=pos.mark_mid,
            )

    # ---- Time-stop rule (always check DTE) ----
    if pos.dte is not None and pos.dte <= ts_dte:
        # If entry_debit available, check multiple condition
        below_threshold = True
        if multiple is not None:
            below_threshold = multiple < ts_min_mult
        elif pos.entry_debit is None:
            # No entry_debit → apply time-stop flag regardless (conservative)
            below_threshold = True
        # If entry_debit exists but mark_mid is None → still flag (can't confirm recovery)
        else:
            below_threshold = True

        if below_threshold:
            blocked = _liquidity_guard_blocked()
            if blocked:
                return blocked

            reason_codes = [
                f"TIME_STOP_DTE:{pos.dte}<={ts_dte}",
            ]
            if multiple is not None:
                reason_codes.append(f"BELOW_MIN_MULTIPLE:{multiple:.2f}x<{ts_min_mult}x")
            if pos.entry_debit is None:
                reason_codes.append("MISSING_ENTRY_DEBIT")
            if pos.mark_mid is None:
                reason_codes.append("MARK_MID_UNAVAILABLE")

            return AllocatorAction(
                type=ActionType.ROLL_CLOSE,
                trade_id=pos.trade_id,
                qty=pos.qty_open,
                reason_codes=reason_codes,
                premium=pos.mark_mid,
            )

    return None


def _partial_close_qty(qty_open: int, fraction: float) -> int:
    """
    Compute quantity to close on a partial harvest.

    Uses math.ceil for deterministic rounding (always close at least 1).
    E.g. qty_open=3, fraction=0.5 → ceil(1.5) = 2
    """
    return max(1, math.ceil(qty_open * fraction))


# ---------------------------------------------------------------------------
# v1.9 Task C: Roll-forward discipline (separate from time-stop harvest)
# ---------------------------------------------------------------------------

def generate_roll_discipline_actions(
    positions: List[SleevePosition],
    policy: Dict[str, Any],
    skip_trade_ids: Optional[set] = None,
) -> List[AllocatorAction]:
    """
    Task C: Roll-forward discipline — close positions that have lost convexity value.

    Uses policy.roll section (separate from harvest rules):
      dte_max_for_roll:             21
      min_multiple_to_hold:         1.10   — close if mark_multiple < this
      min_convexity_multiple_to_hold: 8.0  — close if max_gain/mark < this

    A position is rolled when its DTE is within the window AND at least one of:
      - multiple < min_multiple_to_hold   (ROLL_MULTIPLE)
      - convexity_now < min_convexity_multiple_to_hold  (ROLL_CONVEXITY)

    Close-liquidity guard applies (WIDE_MARKET_NO_CLOSE → HOLD, no replace).

    Args:
        positions:       Open SleevePositions with marks populated.
        policy:          Validated policy dict.
        skip_trade_ids:  Trade IDs already actioned by generate_harvest_actions;
                         these are skipped to avoid double-action on one position.

    Returns:
        List of AllocatorAction (ROLL_CLOSE or HOLD).
    """
    _skip = set(skip_trade_ids or set())

    # Roll params with defaults (if roll section absent, disabled)
    roll_cfg = policy.get("roll", {})
    if not bool(roll_cfg.get("enabled", False)):
        return []

    dte_max  = int(roll_cfg.get("dte_max_for_roll", 21))
    min_mult = float(roll_cfg.get("min_multiple_to_hold", 1.10))
    min_conv = float(roll_cfg.get("min_convexity_multiple_to_hold", 8.0))

    # Close-liquidity guard
    guard_cfg = policy.get("close_liquidity_guard", {})
    max_width_pct = float(guard_cfg.get("max_width_pct", 1.0))

    actions: List[AllocatorAction] = []

    for pos in positions:
        if pos.trade_id in _skip:
            continue  # handled by harvest time-stop already

        # Roll window check
        if pos.dte is None or pos.dte > dte_max:
            continue

        # Determine which roll criteria are triggered
        roll_reasons: List[str] = [f"ROLL_DTE:{pos.dte}<={dte_max}"]
        needs_roll = False

        multiple = pos.multiple   # mark_mid / entry_debit  (None if missing data)

        if multiple is not None:
            if multiple < min_mult:
                roll_reasons.append(f"ROLL_MULTIPLE:{multiple:.2f}x<{min_mult:.2f}x")
                needs_roll = True
        else:
            # No multiple computable → conservative: roll (can't confirm recovery)
            roll_reasons.append("ROLL_MISSING_MULTIPLE")
            needs_roll = True

        # Convexity-now = max_gain_per_contract / mark_mid
        # (remaining payoff potential relative to current mark)
        max_gain_pc: Optional[float] = None
        if len(pos.strikes) >= 2 and pos.strikes[0] > pos.strikes[1]:
            max_gain_pc = (pos.strikes[0] - pos.strikes[1]) * 100.0

        if max_gain_pc is not None and pos.mark_mid is not None and pos.mark_mid > 0:
            convexity_now = max_gain_pc / pos.mark_mid
            if convexity_now < min_conv:
                roll_reasons.append(
                    f"ROLL_CONVEXITY:{convexity_now:.1f}x<{min_conv:.1f}x"
                )
                needs_roll = True

        if not needs_roll:
            continue

        # Close-liquidity guard
        if pos.spread_bid is not None and pos.spread_ask is not None:
            if pos.mark_mid is not None and pos.mark_mid > 0:
                spread_width = pos.spread_ask - pos.spread_bid
                if spread_width >= 0:
                    width_pct = spread_width / pos.mark_mid
                    if width_pct > max_width_pct:
                        actions.append(AllocatorAction(
                            type=ActionType.HOLD,
                            trade_id=pos.trade_id,
                            reason_codes=[
                                "WIDE_MARKET_NO_CLOSE",
                                f"SPREAD_WIDTH_PCT:{width_pct:.1%}>{max_width_pct:.1%}",
                                f"BID:{pos.spread_bid:.2f}_ASK:{pos.spread_ask:.2f}_MID:{pos.mark_mid:.2f}",
                            ] + roll_reasons,
                        ))
                        continue

        actions.append(AllocatorAction(
            type=ActionType.ROLL_CLOSE,
            trade_id=pos.trade_id,
            qty=pos.qty_open,
            reason_codes=roll_reasons,
            premium=pos.mark_mid,
        ))

    return actions
