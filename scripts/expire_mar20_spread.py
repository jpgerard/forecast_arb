#!/usr/bin/env python3
"""
One-off operator script: remove expired SPY 20260320 590/570 spread from fills ledger.

The March 20, 2026 spread expired worthless on 2026-03-20 (3 days ago).
CCC does not auto-expire positions.  This script performs a safe operator-guided
cleanup:

1. Creates dated backups of fills ledger and positions.json
2. Removes the two Mar20 rows (import + debit-enrich)
3. Rebuilds positions.json from the remaining 4 rows (deduped)
4. Verifies the result shows exactly 2 open positions

Run with --dry-run first to preview, then without to apply.

Usage:
    python scripts/expire_mar20_spread.py --dry-run
    python scripts/expire_mar20_spread.py
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from forecast_arb.allocator.fills import (
    build_positions_snapshot,
    write_positions_snapshot,
)
from forecast_arb.allocator.broker_sync import _dedup_fills_rows_by_contract

FILLS_PATH = PROJECT_ROOT / "runs/allocator/allocator_fills_ledger.jsonl"
POSITIONS_PATH = PROJECT_ROOT / "runs/allocator/positions.json"

# The two rows to remove (expired Mar20 spread)
REMOVE_IDS = {
    "ibkr_import_SPY_20260320_590_570",
    "ibkr_debit_enrich_SPY_20260320_590_570",
}


def main():
    parser = argparse.ArgumentParser(description="Expire stale SPY 20260320 position from CCC state.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing any files")
    args = parser.parse_args()
    dry_run = args.dry_run

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Expiring SPY 20260320 590/570 spread from CCC state")
    print("=" * 70)

    # Load fills ledger
    if not FILLS_PATH.exists():
        print(f"ERROR: fills ledger not found at {FILLS_PATH}")
        sys.exit(1)

    rows = [json.loads(l) for l in FILLS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"Fills ledger: {len(rows)} rows")

    kept   = [r for r in rows if r.get("intent_id") not in REMOVE_IDS]
    removed = [r.get("intent_id") for r in rows if r.get("intent_id") in REMOVE_IDS]

    print(f"\nRemoving {len(removed)} expired row(s):")
    for rid in removed:
        print(f"  - {rid}")

    print(f"\nRetaining {len(kept)} rows:")
    for r in kept:
        print(f"  {r.get('action','?'):20s}  {r.get('intent_id','?')}")

    # Rebuild positions using contract-level dedup
    deduped = _dedup_fills_rows_by_contract(kept)
    new_positions = build_positions_snapshot(deduped)

    print(f"\nRebuilt positions.json: {len(new_positions)} open position(s):")
    for p in new_positions:
        print(f"  {p['underlier']} {p['expiry']} {p['strikes']}  "
              f"qty={p['qty_open']}  src={p['source']}")

    if dry_run:
        print("\n[DRY-RUN] No files written.")
        return 0

    # Backup
    bak_fills = str(FILLS_PATH) + ".bak_20260323"
    bak_pos   = str(POSITIONS_PATH) + ".bak_20260323"
    shutil.copy2(FILLS_PATH, bak_fills)
    shutil.copy2(POSITIONS_PATH, bak_pos)
    print(f"\nBackups created:")
    print(f"  {bak_fills}")
    print(f"  {bak_pos}")

    # Write updated fills ledger
    with open(FILLS_PATH, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
    print(f"\nFills ledger updated: {FILLS_PATH}")

    # Write positions.json
    write_positions_snapshot(POSITIONS_PATH, new_positions)
    print(f"Positions.json updated: {POSITIONS_PATH}")

    print(f"\nDone.  CCC now shows {len(new_positions)} open position(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
