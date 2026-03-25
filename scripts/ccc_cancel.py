"""
ccc_cancel.py — Cancel a pending open intent in the commit ledger.

CCC v1.9 "Operator Hygiene"

Appends an OPEN_CANCELED row to runs/allocator/allocator_commit_ledger.jsonl
so that the canceled intent is no longer counted as pending exposure.

Usage:
    python scripts/ccc_cancel.py --intent-id <id> --reason "explanation" [--paper|--live]

Arguments:
    --intent-id   (required) The intent_id to cancel
    --reason      (required) Human-readable explanation (e.g. "expiry passed, market closed")
    --paper       Paper mode label (default)
    --live        Live mode label

Idempotent:
    - If intent_id already has OPEN_CANCELED or OPEN_EXPIRED in commit ledger → no-op.
    - If intent_id appears in fills ledger as POSITION_OPENED → refuse with clear message.

Cancel row schema (appended to allocator_commit_ledger.jsonl):
    {
      "date": "YYYY-MM-DD",
      "timestamp_utc": "ISO-8601",
      "action": "OPEN_CANCELED",
      "mode": "paper" | "live",
      "intent_id": "...",
      "reason": "..."
    }

Public API (importable):
    from scripts.ccc_cancel import run_cancel
    result = run_cancel(intent_id, reason, mode, commit_ledger_path, fills_ledger_path)
    # result: {"action": "CANCELED"|"NO_OP"|"REFUSED", "message": str, ...}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Default paths (relative to project root)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_DEFAULT_COMMIT_LEDGER = Path("runs/allocator/allocator_commit_ledger.jsonl")
_DEFAULT_FILLS_LEDGER = Path("runs/allocator/allocator_fills_ledger.jsonl")

# Actions that mean the intent is already non-pending (idempotency guard)
_TERMINAL_ACTIONS = {"OPEN_CANCELED", "OPEN_EXPIRED"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_local_date() -> str:
    """Return today's date as YYYY-MM-DD (America/New_York, fallback to UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        pass
    try:
        import pytz  # type: ignore[import]
        return datetime.now(pytz.timezone("America/New_York")).date().isoformat()
    except Exception:
        pass
    return datetime.now(timezone.utc).date().isoformat()


def _read_jsonl(path: Path) -> List[Dict]:
    """Read JSONL file → list of dicts.  Returns [] if file is missing."""
    if not path.exists():
        return []
    rows: List[Dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _append_row(path: Path, row: Dict[str, Any]) -> None:
    """Append a single JSON line to a JSONL file (mkdir -p as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Core cancel logic (public API)
# ---------------------------------------------------------------------------


def run_cancel(
    intent_id: str,
    reason: str,
    mode: str = "paper",
    commit_ledger_path: Path = _DEFAULT_COMMIT_LEDGER,
    fills_ledger_path: Path = _DEFAULT_FILLS_LEDGER,
) -> Dict[str, Any]:
    """
    Cancel a pending open intent by appending OPEN_CANCELED to the commit ledger.

    This is the authoritative operator mechanism to resolve stale pending intents.
    After cancellation, the intent_id is removed from the pending set
    (pending = OPEN rows − POSITION_OPENED fills − OPEN_CANCELED rows).

    Args:
        intent_id:           The intent_id to cancel.
        reason:              Human-readable reason (required, non-empty).
        mode:                "paper" or "live" — recorded in the cancel row.
        commit_ledger_path:  Path to allocator_commit_ledger.jsonl.
        fills_ledger_path:   Path to allocator_fills_ledger.jsonl.

    Returns:
        Dict with keys:
          "action":  "CANCELED" | "NO_OP" | "REFUSED"
          "message": Human-readable summary.
          "row":     The appended row dict (only when action="CANCELED").

    Raises:
        ValueError: if intent_id or reason is empty.
    """
    intent_id = intent_id.strip()
    if not intent_id:
        raise ValueError("intent_id must not be empty")
    reason = reason.strip()
    if not reason:
        raise ValueError("reason must not be empty")
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")

    # -----------------------------------------------------------------------
    # Guard 1: Refuse if already filled (POSITION_OPENED in fills ledger).
    # Cannot cancel a filled position — use harvest flow instead.
    # -----------------------------------------------------------------------
    fills_rows = _read_jsonl(fills_ledger_path)
    for row in fills_rows:
        if row.get("action") == "POSITION_OPENED" and row.get("intent_id") == intent_id:
            msg = (
                f"CANCEL REFUSED: intent_id={intent_id} has a POSITION_OPENED row "
                f"in the fills ledger (date={row.get('date', '?')}).\n"
                f"  This intent has already been filled — a position exists.\n"
                f"  ccc_cancel.py cannot cancel a filled position.\n"
                f"  To close the position, use harvest / ccc_reconcile.py logic."
            )
            return {"action": "REFUSED", "message": msg}

    # -----------------------------------------------------------------------
    # Guard 2: Idempotent — no-op if already OPEN_CANCELED or OPEN_EXPIRED.
    # -----------------------------------------------------------------------
    commit_rows = _read_jsonl(commit_ledger_path)
    for row in commit_rows:
        if (
            row.get("intent_id") == intent_id
            and row.get("action") in _TERMINAL_ACTIONS
        ):
            existing = row.get("action", "OPEN_CANCELED")
            msg = (
                f"NO_OP: intent_id={intent_id} already has {existing} "
                f"in commit ledger (date={row.get('date', '?')}). "
                f"Nothing written."
            )
            return {"action": "NO_OP", "message": msg}

    # -----------------------------------------------------------------------
    # Write OPEN_CANCELED row.
    # -----------------------------------------------------------------------
    date_str = _get_local_date()
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    cancel_row: Dict[str, Any] = {
        "date": date_str,
        "timestamp_utc": timestamp_utc,
        "action": "OPEN_CANCELED",
        "mode": mode,
        "intent_id": intent_id,
        "reason": reason,
    }
    _append_row(commit_ledger_path, cancel_row)

    msg = (
        f"CANCELED: intent_id={intent_id}\n"
        f"  OPEN_CANCELED row written to {commit_ledger_path}\n"
        f"  date={date_str}  mode={mode}  reason={reason!r}\n"
        f"  The intent is no longer counted in pending exposure."
    )
    return {"action": "CANCELED", "message": msg, "row": cancel_row}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cancel a pending open intent (CCC v1.9 Operator Hygiene)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Cancel a stale pending commit (paper mode)
  python scripts/ccc_cancel.py --intent-id abc123 --reason "expiry passed" --paper

  # Cancel a live-mode commit
  python scripts/ccc_cancel.py --intent-id abc123 --reason "market closed" --live

  # Idempotent: running twice is safe — second call is a no-op
  python scripts/ccc_cancel.py --intent-id abc123 --reason "stale" --paper

Notes:
  - Refuses if the intent has already been filled (POSITION_OPENED in fills ledger).
  - Idempotent: no-op if already OPEN_CANCELED or OPEN_EXPIRED.
  - After cancel, the intent no longer appears in ccc_status.py pending list.
""",
    )
    parser.add_argument(
        "--intent-id",
        required=True,
        dest="intent_id",
        help="The intent_id to cancel (required).",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable reason for cancellation (required).",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--paper",
        action="store_true",
        default=True,
        help="Paper mode (default). Label stored in cancel row.",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Live mode. Label stored in cancel row.",
    )

    parser.add_argument(
        "--commit-ledger",
        default=str(_DEFAULT_COMMIT_LEDGER),
        dest="commit_ledger",
        help=f"Path to commit ledger (default: {_DEFAULT_COMMIT_LEDGER})",
    )
    parser.add_argument(
        "--fills-ledger",
        default=str(_DEFAULT_FILLS_LEDGER),
        dest="fills_ledger",
        help=f"Path to fills ledger (default: {_DEFAULT_FILLS_LEDGER})",
    )

    args = parser.parse_args()
    _setup_logging()

    mode = "live" if args.live else "paper"

    try:
        result = run_cancel(
            intent_id=args.intent_id,
            reason=args.reason,
            mode=mode,
            commit_ledger_path=Path(args.commit_ledger),
            fills_ledger_path=Path(args.fills_ledger),
        )
    except ValueError as exc:
        print(f"\n  ✗ ERROR: {exc}\n")
        sys.exit(2)

    print()
    for line in result["message"].splitlines():
        print(f"  {line}")
    print()

    if result["action"] == "REFUSED":
        sys.exit(1)
    elif result["action"] == "NO_OP":
        sys.exit(0)
    else:  # CANCELED
        sys.exit(0)


if __name__ == "__main__":
    main()
