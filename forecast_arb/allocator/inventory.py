"""
CCC v1 Allocator - Inventory tracking.

Determines currently open positions by regime from the allocator ledger
OR from positions.json (fills ledger snapshot — authoritative in v1.7+).

v1.7 additions:
  - compute_inventory_state_from_positions(): reads positions.json snapshot
  - compute_inventory_state_with_positions(): prefers positions.json if exists

v1.8 additions:
  - compute_pending_from_ledgers(): uses pending.py (commit − filled) instead of
    filesystem timestamp scanning.  Returns {"crash": N, "selloff": N} pending counts.
  - compute_inventory_state_full(): returns actual + pending in one call.
    Replaces _scan_pending_open_intents() in plan.py.

Priority:
  1. positions.json (from fills ledger reconcile) — count crash/selloff open positions
  2. Plan ledger fallback (pre-v1.7 behaviour) — for backward compat

inventory.pending is computed from commit_ledger − fills_ledger (v1.8).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .budget import read_ledger_records
from .types import InventoryState


def compute_inventory_state(
    policy: Dict[str, Any],
    ledger_path: Path,
) -> InventoryState:
    """
    Compute InventoryState from policy targets + allocator ledger.

    Logic:
    - OPEN records add to open set
    - HARVEST_CLOSE or ROLL_CLOSE records remove from open set (by trade_id)
    - Remaining open set counts per regime

    Args:
        policy:       Validated policy dict
        ledger_path:  Path to allocator_ledger.jsonl

    Returns:
        InventoryState
    """
    inv_cfg = policy["inventory_targets"]
    crash_target = int(inv_cfg.get("crash", 1))
    selloff_target = int(inv_cfg.get("selloff", 1))

    records = read_ledger_records(ledger_path)

    # {trade_id: regime}
    open_trades: Dict[str, str] = {}
    closed_ids: Set[str] = set()

    for rec in records:
        action = rec.get("action", "")
        trade_id = rec.get("trade_id", "")

        if not trade_id:
            continue

        if action == "OPEN":
            if trade_id not in closed_ids:
                open_trades[trade_id] = rec.get("regime", "").lower()

        elif action in ("HARVEST_CLOSE", "ROLL_CLOSE"):
            # Mark as fully closed (simplistic: any close removes from open)
            closed_ids.add(trade_id)
            open_trades.pop(trade_id, None)

    # Count open by regime
    crash_open = sum(1 for r in open_trades.values() if r == "crash")
    selloff_open = sum(1 for r in open_trades.values() if r == "selloff")

    return InventoryState(
        crash_target=crash_target,
        crash_open=crash_open,
        selloff_target=selloff_target,
        selloff_open=selloff_open,
    )


def compute_inventory_state_from_positions(
    policy: Dict[str, Any],
    positions_path: Path,
) -> InventoryState:
    """
    Compute InventoryState from positions.json snapshot (v1.7 authoritative source).

    positions.json is written by ccc_reconcile after a fill is recorded.
    Each entry with qty_open > 0 counts toward open inventory.

    Args:
        policy:         Validated policy dict (for targets)
        positions_path: Path to positions.json (from fills ledger reconcile)

    Returns:
        InventoryState with crash_open/selloff_open derived from positions.json
    """
    inv_cfg = policy["inventory_targets"]
    crash_target = int(inv_cfg.get("crash", 1))
    selloff_target = int(inv_cfg.get("selloff", 1))

    # Lazy import to avoid circular dependency
    from .fills import read_positions_snapshot

    positions = read_positions_snapshot(positions_path)

    crash_open = sum(
        1 for p in positions
        if p.get("regime", "").lower() == "crash" and int(p.get("qty_open", 0)) > 0
    )
    selloff_open = sum(
        1 for p in positions
        if p.get("regime", "").lower() == "selloff" and int(p.get("qty_open", 0)) > 0
    )

    return InventoryState(
        crash_target=crash_target,
        crash_open=crash_open,
        selloff_target=selloff_target,
        selloff_open=selloff_open,
    )


def compute_inventory_state_with_positions(
    policy: Dict[str, Any],
    ledger_path: Path,
    positions_path: Optional[Path] = None,
) -> InventoryState:
    """
    Compute InventoryState, preferring positions.json if available.

    v1.7: If positions_path is provided and the file exists, uses the fills
    ledger snapshot (authoritative). Otherwise falls back to plan ledger.

    Args:
        policy:         Validated policy dict
        ledger_path:    Path to allocator_plan_ledger.jsonl (fallback)
        positions_path: Optional path to positions.json (authoritative if present)

    Returns:
        InventoryState
    """
    if positions_path is not None and positions_path.exists():
        return compute_inventory_state_from_positions(policy, positions_path)
    return compute_inventory_state(policy, ledger_path)


def compute_pending_from_ledgers(
    commit_ledger_path: Path,
    fills_ledger_path: Path,
) -> Dict[str, int]:
    """
    Compute pending intent counts by regime using durable ledger sources.

    v1.8: Replaces _scan_pending_open_intents() (filesystem timestamp scan).

    "Pending" = committed (in commit ledger) but NOT yet filled (not in fills ledger
    as POSITION_OPENED).  ORDER_STAGED rows in the fills ledger do NOT count as filled
    — they remain pending until POSITION_OPENED appears.

    Args:
        commit_ledger_path:  Path to allocator_commit_ledger.jsonl
        fills_ledger_path:   Path to allocator_fills_ledger.jsonl

    Returns:
        {"crash": N, "selloff": N}  — counts of pending intents by regime.
    """
    from .pending import load_pending_counts
    return load_pending_counts(
        commit_ledger_path=commit_ledger_path,
        fills_ledger_path=fills_ledger_path,
    )


def compute_inventory_state_full(
    policy: Dict[str, Any],
    ledger_path: Path,
    commit_ledger_path: Path,
    fills_ledger_path: Path,
    positions_path: Optional[Path] = None,
) -> tuple:
    """
    Compute actual + pending inventory in one call (v1.8 gating authority).

    Returns:
        (inv_actual: InventoryState, pending_by_regime: dict, inv_effective: InventoryState)

    Where:
        inv_actual        = crash/selloff open from positions.json (v1.7 authoritative)
        pending_by_regime = {"crash": N, "selloff": N} from commit − filled ledgers
        inv_effective     = actual + pending (used for OPEN gating decisions)

    The caller (plan.py) uses inv_effective to decide whether to plan an OPEN action.
    inventory.actual does NOT include pending in harvest/multiple logic.
    """
    # Step 1: actual from positions.json (or plan ledger fallback)
    inv_actual = compute_inventory_state_with_positions(
        policy=policy,
        ledger_path=ledger_path,
        positions_path=positions_path,
    )

    # Step 2: pending from commit_ledger − fills_ledger
    pending = compute_pending_from_ledgers(commit_ledger_path, fills_ledger_path)

    # Step 3: effective = actual + pending (per regime)
    inv_effective = InventoryState(
        crash_target=inv_actual.crash_target,
        crash_open=inv_actual.crash_open + pending.get("crash", 0),
        selloff_target=inv_actual.selloff_target,
        selloff_open=inv_actual.selloff_open + pending.get("selloff", 0),
    )

    return inv_actual, pending, inv_effective


def list_open_trades(ledger_path: Path) -> List[Dict[str, Any]]:
    """
    Return list of OPEN ledger records that have not been closed.

    Useful for reconcile to match trade_ids to positions.
    """
    records = read_ledger_records(ledger_path)

    open_by_id: Dict[str, Dict[str, Any]] = {}
    closed_ids: Set[str] = set()

    for rec in records:
        action = rec.get("action", "")
        trade_id = rec.get("trade_id", "")

        if not trade_id:
            continue

        if action == "OPEN":
            if trade_id not in closed_ids:
                open_by_id[trade_id] = rec

        elif action in ("HARVEST_CLOSE", "ROLL_CLOSE"):
            closed_ids.add(trade_id)
            open_by_id.pop(trade_id, None)

    return list(open_by_id.values())
