"""
scripts/review_reflection_proposals.py
=======================================
CLI for reviewing managed reflection proposals.

Human-in-the-loop only — no autonomous approval. No config mutation. No execution.

Subcommands
-----------
    list     Display proposals (optionally filtered by --status or --type).
    approve  Approve a proposal to a specific target status.
    reject   Reject a proposal.

Default paths
-------------
    --proposals    runs/proposals/weekly_reflection_proposals.json
    --decisions-log  runs/proposals/proposal_decisions.jsonl

Type-gated approval targets
----------------------------
    parameter proposals:  APPROVED_FOR_REPLAY | APPROVED_FOR_PAPER | APPROVED_FOR_RESEARCH
    strategy proposals:   APPROVED_FOR_PAPER | APPROVED_FOR_RESEARCH
                          (APPROVED_FOR_REPLAY is blocked for strategy proposals)

Reviewed snapshot
-----------------
    After each approve/reject action, a snapshot of all reviewed (non-PENDING)
    proposals is written to:
        <proposals_dir>/weekly_reflection_proposals_reviewed.json

Usage
-----
    python scripts/review_reflection_proposals.py list [--proposals PATH]
        [--status STATUS] [--type TYPE]

    python scripts/review_reflection_proposals.py approve ID TARGET_STATUS
        [--proposals PATH] [--decisions-log PATH]
        [--note TEXT] [--operator TEXT]

    python scripts/review_reflection_proposals.py reject ID
        [--proposals PATH] [--decisions-log PATH]
        [--note TEXT] [--operator TEXT]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from forecast_arb.ops.proposals import (
    VALID_STATUSES,
    append_decision_event,
    load_proposals,
    save_proposals,
    update_proposal_status,
    validate_approval_target,
)

_DEFAULT_PROPOSALS = Path("runs/proposals/weekly_reflection_proposals.json")
_DEFAULT_DECISIONS_LOG = Path("runs/proposals/proposal_decisions.jsonl")


# ---------------------------------------------------------------------------
# Reviewed snapshot helper
# ---------------------------------------------------------------------------


def _write_reviewed_snapshot(proposals_path: Path, container: dict) -> None:
    """Write a snapshot of all non-PENDING proposals as a sibling file."""
    reviewed = [p for p in container.get("proposals", []) if p.get("status") != "PENDING"]
    snapshot = {
        "schema_version": container.get("schema_version", "1.0"),
        "ts_created": container.get("ts_created", ""),
        "ts_updated": datetime.now(timezone.utc).isoformat(),
        "proposals": reviewed,
    }
    snapshot_path = proposals_path.parent / "weekly_reflection_proposals_reviewed.json"
    with open(snapshot_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)


# ---------------------------------------------------------------------------
# Internal callables (testable without subprocess)
# ---------------------------------------------------------------------------


def _do_list(
    proposals_path: Path,
    status_filter: str = "all",
    type_filter: str = "all",
) -> None:
    """Print proposals to stdout. No file writes."""
    proposals_path = Path(proposals_path)
    container = load_proposals(proposals_path)
    proposals = container.get("proposals", [])

    # Apply filters
    if status_filter != "all":
        proposals = [p for p in proposals if p.get("status") == status_filter]
    if type_filter != "all":
        proposals = [p for p in proposals if p.get("type") == type_filter]

    if not proposals:
        print(f"[review] No proposals found (status={status_filter!r} type={type_filter!r}).")
        return

    # Header
    print(f"\n{'ID':<10} {'TYPE':<12} {'STATUS':<22} {'OVERFIT':<8} {'CONF':<6}  DESCRIPTION")
    print("-" * 100)
    for p in proposals:
        pid = p.get("id", "?")[:10]
        ptype = p.get("type", "?")[:12]
        status = p.get("status", "?")[:22]
        overfit = p.get("overfit_risk", "?")[:8]
        conf = p.get("confidence")
        conf_str = f"{conf:.2f}" if conf is not None else "N/A"
        if ptype.strip() == "parameter":
            desc = f"{p.get('parameter', '?')}  ({p.get('current_value')} → {p.get('suggested_value')})"
        else:
            desc = (p.get("hypothesis") or "")[:70]
        print(f"{pid:<10} {ptype:<12} {status:<22} {overfit:<8} {conf_str:<6}  {desc}")
    print()


def _do_approve(
    proposals_path: Path,
    decisions_log: Path,
    proposal_id: str,
    target_status: str,
    note: str = "",
    operator: str = "operator",
) -> int:
    """
    Approve a proposal to target_status.

    Returns:
        0 on success, 1 on error (proposal not found, type gate blocked, unknown status).
    """
    proposals_path = Path(proposals_path)
    decisions_log = Path(decisions_log)

    # Load
    container = load_proposals(proposals_path)

    # Find proposal to check type before validation
    proposal = next(
        (p for p in container.get("proposals", []) if p.get("id") == proposal_id),
        None,
    )
    if proposal is None:
        print(f"[review] ERROR: proposal id={proposal_id!r} not found.", file=sys.stderr)
        _print_available_ids(container)
        return 1

    # Type-gated validation
    try:
        validate_approval_target(proposal["type"], target_status)
    except ValueError as exc:
        print(f"[review] ERROR: {exc}", file=sys.stderr)
        return 1

    # Update status
    ts_utc = datetime.now(timezone.utc).isoformat()
    update_proposal_status(container, proposal_id, target_status, review_reason=note)
    save_proposals(proposals_path, container)
    _write_reviewed_snapshot(proposals_path, container)
    append_decision_event(
        jsonl_path=decisions_log,
        proposal_id=proposal_id,
        action="approve",
        new_status=target_status,
        reason=note,
        operator=operator,
        ts_utc=ts_utc,
    )

    param_desc = (
        f"parameter={proposal.get('parameter')!r}"
        if proposal.get("type") == "parameter"
        else f"hypothesis={str(proposal.get('hypothesis', ''))[:60]!r}"
    )
    print(f"[review] Approved id={proposal_id} → {target_status}  ({param_desc})")
    return 0


def _do_reject(
    proposals_path: Path,
    decisions_log: Path,
    proposal_id: str,
    note: str = "",
    operator: str = "operator",
) -> int:
    """
    Reject a proposal.

    Returns:
        0 on success, 1 if proposal not found.
    """
    proposals_path = Path(proposals_path)
    decisions_log = Path(decisions_log)

    container = load_proposals(proposals_path)

    proposal = next(
        (p for p in container.get("proposals", []) if p.get("id") == proposal_id),
        None,
    )
    if proposal is None:
        print(f"[review] ERROR: proposal id={proposal_id!r} not found.", file=sys.stderr)
        _print_available_ids(container)
        return 1

    ts_utc = datetime.now(timezone.utc).isoformat()
    update_proposal_status(container, proposal_id, "REJECTED", review_reason=note)
    save_proposals(proposals_path, container)
    _write_reviewed_snapshot(proposals_path, container)
    append_decision_event(
        jsonl_path=decisions_log,
        proposal_id=proposal_id,
        action="reject",
        new_status="REJECTED",
        reason=note,
        operator=operator,
        ts_utc=ts_utc,
    )

    print(f"[review] Rejected id={proposal_id}  (note: {note!r})")
    return 0


def _print_available_ids(container: dict) -> None:
    ids = [p.get("id", "?") for p in container.get("proposals", [])]
    if ids:
        print(f"[review] Available IDs: {', '.join(ids)}", file=sys.stderr)
    else:
        print("[review] No proposals in store.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Review managed reflection proposals. "
            "Human-in-the-loop only — no autonomous approval."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # list
    ls = sub.add_parser("list", help="Display proposals.")
    ls.add_argument("--proposals", type=Path, default=_DEFAULT_PROPOSALS,
                    help="Path to managed proposals JSON.")
    ls.add_argument(
        "--status", default="all",
        choices=["all"] + sorted(VALID_STATUSES),
        help="Filter by status.",
    )
    ls.add_argument(
        "--type", dest="type_filter", default="all",
        choices=["all", "parameter", "strategy"],
        help="Filter by proposal type.",
    )

    # approve
    ap = sub.add_parser("approve", help="Approve a proposal.")
    ap.add_argument("proposal_id", help="8-char proposal ID.")
    ap.add_argument(
        "target_status",
        choices=sorted(VALID_STATUSES - {"PENDING", "REJECTED"}),
        help="Approval target status.",
    )
    ap.add_argument("--proposals", type=Path, default=_DEFAULT_PROPOSALS)
    ap.add_argument("--decisions-log", type=Path, default=_DEFAULT_DECISIONS_LOG)
    ap.add_argument("--note", default="", help="Optional operator comment.")
    ap.add_argument("--operator", default="operator", help="Operator identifier.")

    # reject
    rj = sub.add_parser("reject", help="Reject a proposal.")
    rj.add_argument("proposal_id", help="8-char proposal ID.")
    rj.add_argument("--proposals", type=Path, default=_DEFAULT_PROPOSALS)
    rj.add_argument("--decisions-log", type=Path, default=_DEFAULT_DECISIONS_LOG)
    rj.add_argument("--note", default="", help="Reason for rejection.")
    rj.add_argument("--operator", default="operator", help="Operator identifier.")

    return p


def main() -> None:
    args = _build_parser().parse_args()

    if args.command == "list":
        _do_list(
            proposals_path=args.proposals,
            status_filter=args.status,
            type_filter=args.type_filter,
        )

    elif args.command == "approve":
        rc = _do_approve(
            proposals_path=args.proposals,
            decisions_log=args.decisions_log,
            proposal_id=args.proposal_id,
            target_status=args.target_status,
            note=args.note,
            operator=args.operator,
        )
        sys.exit(rc)

    elif args.command == "reject":
        rc = _do_reject(
            proposals_path=args.proposals,
            decisions_log=args.decisions_log,
            proposal_id=args.proposal_id,
            note=args.note,
            operator=args.operator,
        )
        sys.exit(rc)


if __name__ == "__main__":
    main()
