"""
ccc_reconcile.py — Reconcile fills into positions after IBKR execution.

CCC v1.7 "Button Up Execution + Filled Trade Reconcile"

What this does:
  1. Reads intents/allocator/execution_result.json (or --from-execution-result PATH)
  2. Reads the corresponding OPEN_*.json intent for regime/candidate metadata
  3. Computes entry_debit_gross from leg quotes
  4. Appends a POSITION_OPENED row to runs/allocator/allocator_fills_ledger.jsonl
     (dedup by intent_id — safe to run multiple times)
  5. Rebuilds runs/allocator/positions.json snapshot
  6. Archives OPEN_*.json + execution_result.json into intents/allocator/_archive/YYYYMMDD/

If no execution_result.json is found, exits 0 with a "no fills" message.
No IBKR connection required (reads local files only).

Usage:
    # Paper mode (after a paper staged order)
    python scripts/ccc_reconcile.py --paper

    # Live mode (after a live transmitted order fills)
    python scripts/ccc_reconcile.py --live

    # Use a custom execution_result.json
    python scripts/ccc_reconcile.py --paper --from-execution-result path/to/execution_result.json

    # Dry run (validate but don't write)
    python scripts/ccc_reconcile.py --paper --dry-run

Output (always printed):
    fills_found=N  positions_opened=N  dedup_skipped=N  files_written=N

After a successful run, next daily.py will show:
    INVENTORY ACTUAL crash=1/1 (even if pending_intents=0)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup (allow running from any CWD)
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "CCC v1.7 Fill Reconciliation — reads execution_result.json, "
            "writes fills ledger + positions.json, archives intents."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Reconcile today's paper execution
  python scripts/ccc_reconcile.py --paper

  # Reconcile today's live execution
  python scripts/ccc_reconcile.py --live

  # Use a custom execution_result path
  python scripts/ccc_reconcile.py --paper --from-execution-result path/to/execution_result.json

  # Dry run (print what would happen, no writes)
  python scripts/ccc_reconcile.py --paper --dry-run
""",
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--paper",
        action="store_true",
        help="Paper mode (match paper-staged orders)",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Live mode (match live-transmitted orders)",
    )

    parser.add_argument(
        "--from-execution-result",
        type=str,
        default=None,
        dest="execution_result_path",
        metavar="PATH",
        help=(
            "Override path to execution_result.json. "
            "Default: intents/allocator/execution_result.json"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Validate and preview without writing any files.",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Enable debug logging.",
    )

    # Advanced overrides (mostly for tests / CI)
    parser.add_argument("--fills-ledger", type=str, default=None, dest="fills_ledger",
                        help="Override path to allocator_fills_ledger.jsonl")
    parser.add_argument("--positions-file", type=str, default=None, dest="positions_file",
                        help="Override path to positions.json")
    parser.add_argument("--intents-dir", type=str, default=None, dest="intents_dir",
                        help="Override intents/allocator directory")
    parser.add_argument("--archive-dir", type=str, default=None, dest="archive_dir",
                        help="Override archive base directory")
    parser.add_argument("--policy-id", type=str, default="ccc_v1", dest="policy_id",
                        help="Policy ID to record in fills ledger (default: ccc_v1)")

    args = parser.parse_args()
    _setup_logging(verbose=args.verbose)

    mode = "live" if args.live else "paper"

    # Resolve paths
    from forecast_arb.allocator.fills import (
        DEFAULT_ARCHIVE_BASE,
        DEFAULT_FILLS_LEDGER_PATH,
        DEFAULT_INTENTS_DIR,
        DEFAULT_POSITIONS_PATH,
        run_reconcile,
    )

    fills_ledger_path = Path(args.fills_ledger) if args.fills_ledger else DEFAULT_FILLS_LEDGER_PATH
    positions_path = Path(args.positions_file) if args.positions_file else DEFAULT_POSITIONS_PATH
    intents_dir = Path(args.intents_dir) if args.intents_dir else DEFAULT_INTENTS_DIR
    archive_base = Path(args.archive_dir) if args.archive_dir else DEFAULT_ARCHIVE_BASE
    exec_result_path = Path(args.execution_result_path) if args.execution_result_path else None

    # Print header
    print("")
    print("=" * 72)
    print(f"  CCC RECONCILE  —  mode={mode.upper()}")
    if args.dry_run:
        print("  *** DRY RUN — no files will be written ***")
    print(f"  fills_ledger : {fills_ledger_path}")
    print(f"  positions    : {positions_path}")
    print(f"  intents_dir  : {intents_dir}")
    if exec_result_path:
        print(f"  exec_result  : {exec_result_path}")
    print("=" * 72)

    # Run reconcile
    try:
        result = run_reconcile(
            mode=mode,
            execution_result_path=exec_result_path,
            fills_ledger_path=fills_ledger_path,
            positions_path=positions_path,
            intents_dir=intents_dir,
            archive_base_dir=archive_base,
            policy_id=args.policy_id,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        log.error(f"Reconcile failed with unexpected error: {exc}", exc_info=args.verbose)
        print(f"\n  ✗ RECONCILE ERROR: {exc}")
        sys.exit(1)

    # Print result summary
    fills_found = result.get("fills_found", 0)
    positions_opened = result.get("positions_opened", 0)
    dedup_skipped = result.get("dedup_skipped", 0)
    files_written = result.get("files_written", [])
    archived = result.get("archived", [])
    errors = result.get("errors", [])

    print("")

    if fills_found == 0:
        print("  ℹ️  NO FILLS FOUND — nothing to reconcile (no-op)")
        print("     Check: intents/allocator/execution_result.json must exist")
    else:
        if positions_opened > 0:
            print(f"  ✓ fills_found={fills_found}  positions_opened={positions_opened}  "
                  f"dedup_skipped={dedup_skipped}")
        elif dedup_skipped > 0:
            print(f"  ↩ DEDUP: fills_found={fills_found}  positions_opened=0  "
                  f"dedup_skipped={dedup_skipped}  (already recorded — no-op)")
        else:
            print(f"  ⚠  fills_found={fills_found}  positions_opened=0  "
                  f"(check errors below)")

    if files_written:
        print(f"\n  Files written ({len(files_written)}):")
        for p in files_written:
            print(f"    → {p}")

    if archived:
        print(f"\n  Archived ({len(archived)}):")
        for p in archived:
            print(f"    → {p}")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for e in errors:
            print(f"    ✗ {e}")

    if args.dry_run:
        print("\n  *** DRY RUN complete — no files written ***")

    print("")
    print("=" * 72)

    # Exit non-zero only on errors
    if errors and positions_opened == 0 and dedup_skipped == 0 and fills_found > 0:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
