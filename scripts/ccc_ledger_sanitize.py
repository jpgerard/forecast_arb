"""
ccc_ledger_sanitize.py — One-time helper to clean legacy allocator_plan_ledger.jsonl.

v1.5 Task 3:
  Reads allocator_plan_ledger.jsonl and drops OPEN rows that are missing required
  metadata fields needed for reconcile, harvest, and budget purposes.

  Writes output to allocator_plan_ledger.sanitized.jsonl in the SAME directory.
  The operator can manually replace the original if desired:

      copy runs\\allocator\\allocator_plan_ledger.sanitized.jsonl ^
           runs\\allocator\\allocator_plan_ledger.jsonl

  This script only READS and FILTERS. It does NOT modify any runtime state.
  All runtime logic (allocator, budget, reconcile) continues to work with
  the original file until the operator chooses to replace it.

Usage:
    python scripts/ccc_ledger_sanitize.py
    python scripts/ccc_ledger_sanitize.py --ledger runs/allocator/allocator_plan_ledger.jsonl
    python scripts/ccc_ledger_sanitize.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Fields required on an OPEN record in allocator_plan_ledger.jsonl for it to be
# usable by reconcile / harvest / inventory discovery logic.
_OPEN_REQUIRED_FIELDS: List[str] = [
    "underlier",
    "expiry",
    "strikes",    # must be non-None; list or dict acceptable in plan ledger
    "regime",
]


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a JSONL file, returning list of dicts.  Skips blank lines."""
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning(f"Line {lineno}: JSON decode error ({e}) — DROPPED")
    return records


def _is_open_missing_required(rec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Return (True, [missing_fields]) if this OPEN record is missing required metadata.

    Returns (False, []) if the record is acceptable.
    """
    missing = []
    for field in _OPEN_REQUIRED_FIELDS:
        val = rec.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(field)
    return (bool(missing), missing)


def sanitize_plan_ledger(
    ledger_path: Path,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Read allocator_plan_ledger.jsonl, drop incomplete OPEN rows, write sanitized output.

    Non-OPEN rows (HARVEST_CLOSE, ROLL_CLOSE, DAILY_SUMMARY, etc.) are kept as-is.

    Args:
        ledger_path: Path to allocator_plan_ledger.jsonl
        dry_run:     If True, only report what would be dropped — do NOT write.

    Returns:
        Dict with stats: kept, dropped_open, total
    """
    if not ledger_path.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_path}")

    records = _read_jsonl(ledger_path)
    total = len(records)

    kept: List[Dict[str, Any]] = []
    dropped_open = 0
    kept_open = 0

    for rec in records:
        action = rec.get("action", "")

        if action == "OPEN":
            is_bad, missing = _is_open_missing_required(rec)
            if is_bad:
                log.info(
                    f"  DROP  OPEN  candidate_id={rec.get('candidate_id','?')!r}"
                    f"  date={rec.get('date','?')!r}"
                    f"  missing={missing}"
                )
                dropped_open += 1
                continue
            kept_open += 1

        kept.append(rec)

    stats = {
        "total": total,
        "kept": len(kept),
        "dropped_open": dropped_open,
        "kept_open": kept_open,
    }

    if dry_run:
        log.info(
            f"DRY-RUN complete: {total} rows read, "
            f"{dropped_open} incomplete OPEN rows would be dropped, "
            f"{len(kept)} rows would be kept."
        )
        return stats

    # Write sanitized output
    out_path = ledger_path.with_suffix(".sanitized.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in kept:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    log.info(
        f"Sanitized ledger written to: {out_path}"
    )
    log.info(
        f"  Input : {total} rows"
    )
    log.info(
        f"  Output: {len(kept)} rows  ({dropped_open} incomplete OPEN rows dropped)"
    )
    if dropped_open > 0:
        log.info(
            f"\nTo apply, run:\n"
            f"  copy {out_path} {ledger_path}\n"
            f"(Windows) or:\n"
            f"  cp {out_path} {ledger_path}\n"
            f"(Linux/Mac)"
        )
    else:
        log.info("No rows dropped — ledger is already clean.")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "ccc_ledger_sanitize — Remove incomplete OPEN rows from allocator_plan_ledger.jsonl. "
            "Writes allocator_plan_ledger.sanitized.jsonl.  Does NOT modify the original."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/ccc_ledger_sanitize.py
  python scripts/ccc_ledger_sanitize.py --ledger runs/allocator/allocator_plan_ledger.jsonl
  python scripts/ccc_ledger_sanitize.py --dry-run
""",
    )
    parser.add_argument(
        "--ledger",
        type=str,
        default="runs/allocator/allocator_plan_ledger.jsonl",
        help="Path to allocator_plan_ledger.jsonl (default: runs/allocator/allocator_plan_ledger.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Report what would be dropped without writing any files.",
    )

    args = parser.parse_args()
    _setup_logging()

    ledger_path = Path(args.ledger)

    log.info(f"ccc_ledger_sanitize: reading {ledger_path}")
    log.info(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}")

    try:
        stats = sanitize_plan_ledger(ledger_path, dry_run=args.dry_run)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    if stats["dropped_open"] > 0 and not args.dry_run:
        log.info(
            f"\n✓ Done.  Review allocator_plan_ledger.sanitized.jsonl before replacing."
        )
    elif stats["dropped_open"] == 0:
        log.info(f"\n✓ Ledger already clean — no changes needed.")


if __name__ == "__main__":
    main()
