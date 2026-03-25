#!/usr/bin/env python3
"""
CCC Broker-State Sync — Import existing live IBKR positions into CCC ledger.

USE CASE
--------
IBKR account holds bear put spreads that were NOT created through the CCC
execution pipeline (placed directly in TWS, or via a legacy system).  Running
this script makes those positions visible to:
  - runs/allocator/positions.json             (open position snapshot)
  - crash/selloff inventory counts             (ccc_report, plan.py gating)
  - harvest + roll discipline                  (marks, DTE, multiple computation)

SAFETY
------
  - Idempotent: re-running the same spread list is a strict no-op (dedup by
    stable intent_id = "ibkr_import_SPY_20260417_575_555")
  - Read-only by default until --confirm is passed, unless --dry-run is used
  - Does NOT touch strategy logic, allocator gating, or any existing ledger rows
  - All new fills-ledger rows are tagged source="ibkr_import" for auditability

USAGE
-----
# Inline spreads (can repeat --spread):
python scripts/ccc_import_ibkr_positions.py --mode live \\
    --spread SPY 20260417 575 555 1 \\
    --spread SPY 20260327 590 570 1 \\
    --spread SPY 20260320 590 570 1

# From JSON file:
python scripts/ccc_import_ibkr_positions.py --mode live \\
    --combos-json path/to/ibkr_combos.json

# Dry-run (prints what would happen, writes nothing):
python scripts/ccc_import_ibkr_positions.py --dry-run --mode live \\
    --spread SPY 20260417 575 555 1

# Diagnose-only (diff IBKR vs CCC, no import):
python scripts/ccc_import_ibkr_positions.py --diagnose --mode live \\
    --spread SPY 20260417 575 555 1

COMBO JSON FORMAT
-----------------
[
  {
    "symbol": "SPY",
    "expiry": "20260417",
    "long_strike": 575,
    "short_strike": 555,
    "qty": 1,
    "regime": "crash",
    "entry_debit": 65.00
  },
  ...
]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Default paths (same as fills.py defaults)
# ---------------------------------------------------------------------------

_DEFAULT_FILLS_LEDGER = Path("runs/allocator/allocator_fills_ledger.jsonl")
_DEFAULT_POSITIONS    = Path("runs/allocator/positions.json")

# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _parse_spread_arg(parts: List[str]) -> Dict[str, Any]:
    """
    Parse a --spread argument.

    Format: SYMBOL EXPIRY LONG_STRIKE SHORT_STRIKE [QTY [REGIME [ENTRY_DEBIT]]]
    Examples:
      SPY 20260417 575 555 1
      SPY 20260417 575 555 1 crash
      SPY 20260417 575 555 1 crash 65.00
    """
    if len(parts) < 4:
        raise argparse.ArgumentTypeError(
            f"--spread requires at least 4 values: SYMBOL EXPIRY LONG SHORT [QTY [REGIME [DEBIT]]]"
            f", got: {parts!r}"
        )
    combo: Dict[str, Any] = {
        "symbol":       parts[0].upper(),
        "expiry":       parts[1],
        "long_strike":  float(parts[2]),
        "short_strike": float(parts[3]),
    }
    if len(parts) >= 5:
        combo["qty"] = int(parts[4])
    if len(parts) >= 6:
        combo["regime"] = parts[5].lower()
    if len(parts) >= 7:
        combo["entry_debit"] = float(parts[6])
    return combo


def _print_section(title: str) -> None:
    sep = "─" * 72
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="CCC Broker-State Sync — import existing IBKR live positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ---- Spread sources ----
    parser.add_argument(
        "--spread",
        metavar="SYMBOL EXPIRY LONG SHORT [QTY [REGIME [DEBIT]]]",
        nargs="+",
        action="append",
        dest="spreads",
        default=[],
        help=(
            "Inline spread: SYMBOL EXPIRY LONG_STRIKE SHORT_STRIKE [QTY [REGIME [DEBIT]]]"
            "  (repeatable)"
        ),
    )
    parser.add_argument(
        "--combos-json",
        metavar="PATH",
        default=None,
        dest="combos_json",
        help="Path to JSON file containing an array of combo dicts",
    )

    # ---- Paths ----
    parser.add_argument(
        "--fills-ledger",
        default=str(_DEFAULT_FILLS_LEDGER),
        dest="fills_ledger",
        help=f"Path to allocator_fills_ledger.jsonl (default: {_DEFAULT_FILLS_LEDGER})",
    )
    parser.add_argument(
        "--positions",
        default=str(_DEFAULT_POSITIONS),
        dest="positions",
        help=f"Path to positions.json (default: {_DEFAULT_POSITIONS})",
    )

    # ---- Mode ----
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        required=True,
        help="Account mode: 'paper' or 'live'",
    )

    # ---- Regime override ----
    parser.add_argument(
        "--regime",
        default=None,
        choices=["crash", "selloff"],
        help="Override regime for all inline --spread args (default: 'crash')",
    )

    # ---- Run modes ----
    mutex = parser.add_mutually_exclusive_group()
    mutex.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Validate and preview but do NOT write any files",
    )
    mutex.add_argument(
        "--diagnose",
        action="store_true",
        default=False,
        dest="diagnose",
        help="Show diff between IBKR combos and current positions.json; do not import",
    )
    mutex.add_argument(
        "--rebuild-positions",
        action="store_true",
        default=False,
        dest="rebuild_positions",
        help=(
            "Force-rebuild positions.json from fills ledger applying contract dedup "
            "— use this to collapse pre-existing duplicate positions "
            "(e.g. from a prior ibkr_import before this fix was applied)"
        ),
    )

    args = parser.parse_args()

    # ---- Build combo list ----
    combos: List[Dict[str, Any]] = []

    # From --spread args
    for parts in (args.spreads or []):
        try:
            combo = _parse_spread_arg(parts)
        except (ValueError, argparse.ArgumentTypeError) as e:
            print(f"ERROR: invalid --spread {parts!r}: {e}", file=sys.stderr)
            return 1
        # Apply regime override if given
        if args.regime:
            combo["regime"] = args.regime
        combos.append(combo)

    # From --combos-json file
    if args.combos_json:
        combos_path = Path(args.combos_json)
        if not combos_path.exists():
            print(f"ERROR: --combos-json file not found: {combos_path}", file=sys.stderr)
            return 1
        try:
            with open(combos_path, "r", encoding="utf-8") as f:
                file_combos = json.load(f)
            if not isinstance(file_combos, list):
                print(f"ERROR: {combos_path} must contain a JSON array", file=sys.stderr)
                return 1
            combos.extend(file_combos)
        except (json.JSONDecodeError, OSError) as e:
            print(f"ERROR: could not read {combos_path}: {e}", file=sys.stderr)
            return 1

    if not combos:
        print("ERROR: no combos provided. Use --spread or --combos-json.", file=sys.stderr)
        return 1

    fills_ledger_path = Path(args.fills_ledger)
    positions_path    = Path(args.positions)
    mode              = args.mode

    # ---- Diagnose-only mode ----
    if args.diagnose:
        _print_section("IBKR vs CCC DIAGNOSTICS")
        from forecast_arb.allocator.broker_sync import diff_ibkr_vs_positions

        diff = diff_ibkr_vs_positions(combos, positions_path)
        print(f"  IBKR spreads:             {diff['ibkr_count']}")
        print(f"  CCC open positions:       {diff['ccc_count']}")
        print()

        if diff["matched"]:
            print(f"  ✓ MATCHED ({len(diff['matched'])}):")
            for k in diff["matched"]:
                print(f"    {k}")
        else:
            print("  ✓ MATCHED: (none)")

        if diff["missing_from_ccc"]:
            print(f"\n  ✗ MISSING FROM CCC — need import ({len(diff['missing_from_ccc'])}):")
            for k in diff["missing_from_ccc"]:
                print(f"    {k}")
            print(
                f"\n  → Run without --diagnose to import "
                f"{len(diff['missing_from_ccc'])} missing spread(s)."
            )
        else:
            print("\n  ✓ All IBKR spreads are already in CCC.  No import needed.")

        if diff["extra_in_ccc"]:
            print(f"\n  ⚠  EXTRA IN CCC (not in IBKR list) ({len(diff['extra_in_ccc'])}):")
            for k in diff["extra_in_ccc"]:
                print(f"    {k}")

        print()
        return 0

    # ---- Dry-run or live import ----
    from forecast_arb.allocator.broker_sync import sync_ibkr_positions

    _print_section(
        f"CCC BROKER-STATE SYNC  —  mode={mode.upper()}  "
        f"{'[DRY-RUN]' if args.dry_run else '[LIVE WRITE]'}"
    )
    print(f"  Fills ledger : {fills_ledger_path}")
    print(f"  Positions    : {positions_path}")
    print(f"  Combos       : {len(combos)}")
    print()

    # Print combo table
    print(
        f"  {'SYMBOL':<8} {'EXPIRY':<10} {'LONG':>6} {'SHORT':>6} "
        f"{'QTY':>4} {'REGIME':<8} {'ENTRY_DEBIT':>12}"
    )
    print("  " + "─" * 60)
    for c in combos:
        sym   = str(c.get("symbol") or c.get("underlier") or "?")[:8]
        exp   = str(c.get("expiry") or "?")[:10]
        long_ = c.get("long_strike", "?")
        short = c.get("short_strike", "?")
        qty   = c.get("qty", 1)
        reg   = str(c.get("regime") or "crash")[:8]
        dbt   = c.get("entry_debit")
        dbt_s = f"${float(dbt):.2f}" if dbt is not None else "N/A"
        print(
            f"  {sym:<8} {exp:<10} {float(long_) if long_ != '?' else '?':>6.0f} "
            f"{float(short) if short != '?' else '?':>6.0f} {qty:>4} {reg:<8} {dbt_s:>12}"
        )
    print()

    result = sync_ibkr_positions(
        combos=combos,
        fills_ledger_path=fills_ledger_path,
        positions_path=positions_path,
        mode=mode,
        dry_run=args.dry_run,
        force_rebuild=getattr(args, "rebuild_positions", False),
    )

    # ---- Results ----
    _print_section("RESULTS")
    if args.dry_run:
        print(f"  DRY-RUN: would import : {result['imported']}")
        print(f"  Already present       : {result['skipped_dedup']}")
        print(f"  Errors                : {len(result['errors'])}")
        if result.get("positions_preview"):
            preview = result["positions_preview"]
            crash_n  = sum(1 for p in preview if str(p.get("regime","")).lower() == "crash")
            selloff_n = sum(1 for p in preview if str(p.get("regime","")).lower() == "selloff")
            print(
                f"\n  positions.json preview: "
                f"{len(preview)} total  (crash={crash_n}, selloff={selloff_n})"
            )
    else:
        print(f"  Imported              : {result['imported']}")
        print(f"  Already present       : {result['skipped_dedup']}")
        print(f"  Errors                : {len(result['errors'])}")
        print(f"  Fills ledger written  : {result['fills_written']}")
        print(f"  positions.json written: {result['positions_written']}")

    if result["errors"]:
        print("\n  ERRORS:")
        for err in result["errors"]:
            print(f"    {err}")

    if not args.dry_run and result["imported"] > 0:
        print(
            f"\n  ✓ positions.json now reflects {result['imported']} newly-imported "
            f"IBKR spread(s).\n"
            f"  Run `python scripts/ccc_report.py` to verify inventory count."
        )
    elif not args.dry_run and result["imported"] == 0 and result["skipped_dedup"] > 0:
        print("\n  ✓ All specified spreads already present — no changes needed.")

    print()
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
