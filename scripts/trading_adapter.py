#!/usr/bin/env python3
"""
scripts/trading_adapter.py — JP Life Command Center Trading Adapter CLI

Thin CLI wrapper around forecast_arb.adapter.TradingAdapter.

Usage
-----
    python scripts/trading_adapter.py status
    python scripts/trading_adapter.py status --json

    python scripts/trading_adapter.py preview
    python scripts/trading_adapter.py preview --json

    python scripts/trading_adapter.py report
    python scripts/trading_adapter.py report --json

    python scripts/trading_adapter.py summarize
    python scripts/trading_adapter.py summarize --json
    python scripts/trading_adapter.py summarize --no-preview

Commands
--------
    status    — Task A: current sleeve state (read-only, no subprocess)
    preview   — Task B: run daily preview via subprocess (paper + quote-only)
    report    — Task C: run ccc_report.py via subprocess
    summarize — Task D: combine status + report + preview into one object

Flags
-----
    --json            Output machine-readable JSON instead of human text
    --policy PATH     Override policy YAML path
    --campaign PATH   Override campaign YAML path (preview / summarize only)
    --no-preview      For summarize: skip live preview, use cached artifacts
    --timeout N       Subprocess timeout in seconds (default 120)
    --broker-csv PATH (CCC v2.2) Path to IBKR exported positions CSV.
                      When supplied, runs a broker-state drift check and
                      warns if CCC internal state differs from broker truth.
                      Compatible with status and summarize commands.

Exit codes
----------
    0  — ok=True and actionability != ERROR
    1  — ok=False or actionability == ERROR
    2  — unknown command / argument error

V1 INVARIANTS:
    No action/execute commands.
    No live execution wrappers.
    No approval/cancel pending trade commands.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path
from typing import Optional

# Ensure project root is importable regardless of cwd
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from forecast_arb.adapter.trading_adapter import AdapterResult, TradingAdapter


# ---------------------------------------------------------------------------
# Human-readable printer
# ---------------------------------------------------------------------------

def _print_human(result: AdapterResult, command: str) -> None:
    """Print a human-friendly summary of an AdapterResult."""
    ok_str     = "✓ OK" if result.ok else "✗ FAILED"
    act_str    = result.actionability
    print("")
    print("=" * 72)
    print(f"  Trading Adapter — {command.upper()}")
    print(f"  Status        : {ok_str}  [{act_str}]")
    print(f"  Headline      : {result.headline}")
    print("=" * 72)

    if result.errors:
        print("")
        print("  ERRORS:")
        for err in result.errors:
            for line in err.splitlines():
                print(f"    {line}")

    details = result.details
    if details:
        print("")
        print("  KEY DETAILS:")
        _print_details_human(details, command)

    if result.raw_output and command in ("report",):
        # For report, the raw output *is* the report — skip reprinting.
        pass

    print("=" * 72)
    print("")


def _print_details_human(details: dict, command: str) -> None:
    """Print key details in a readable two-column layout."""

    if command == "status":
        _kv("Crash open",    details.get("crash_open", 0))
        _kv("Selloff open",  details.get("selloff_open", 0))
        _kv("Pending total", details.get("pending_total", 0))
        par_crash   = details.get("par_crash", 0.0)
        par_selloff = details.get("par_selloff", 0.0)
        par_total   = details.get("par_total", 0.0)
        cap_crash   = details.get("par_crash_cap")
        cap_total   = details.get("par_total_cap")
        crash_str = f"${par_crash:.2f}" + (f" / ${cap_crash:.2f} cap" if cap_crash else "")
        total_str = f"${par_total:.2f}" + (f" / ${cap_total:.2f} cap" if cap_total else "")
        _kv("PAR crash",    crash_str)
        _kv("PAR selloff",  f"${par_selloff:.2f}")
        _kv("PAR total",    total_str)
        _kv("YTD spent",    f"${details.get('ytd_spent', 0):.2f}")
        ann_rem = details.get("annual_remaining")
        if ann_rem is not None:
            _kv("Annual remaining", f"${ann_rem:.2f}")
        lp_ts = details.get("latest_plan_ts")
        if lp_ts:
            _kv("Latest plan",
                f"opens={details.get('latest_plan_opens',0)}  "
                f"closes={details.get('latest_plan_closes',0)}  "
                f"holds={details.get('latest_plan_holds',0)}")
            _kv("Plan timestamp", str(lp_ts)[:19])
        gate = details.get("latest_plan_gate_reason")
        if gate:
            _kv("Gate reason", gate)

    elif command == "preview":
        _kv("Planned opens",  details.get("planned_opens", 0))
        _kv("Planned closes", details.get("planned_closes", 0))
        _kv("Holds",          details.get("holds", 0))
        _kv("Quote-only validated", details.get("quote_only_validated", 0))
        gate = details.get("gate_reason")
        if gate:
            _kv("Gate reason", gate)
        _kv("Summary box found", details.get("summary_box_found", False))

    elif command == "report":
        _kv("Crash open",    details.get("crash_open", 0))
        _kv("Selloff open",  details.get("selloff_open", 0))
        _kv("Pending total", details.get("pending_total", 0))
        par_total = details.get("par_total")
        par_crash = details.get("par_crash")
        if par_crash is not None:
            _kv("PAR crash",   f"${par_crash:.2f}")
        if par_total is not None:
            _kv("PAR total",   f"${par_total:.2f}")
        ytd = details.get("ytd_spent")
        if ytd is not None:
            _kv("YTD spent",   f"${ytd:.2f}")
        _kv("Planned opens",   details.get("planned_opens", 0))
        _kv("Holds",           details.get("holds", 0))
        gate = details.get("gate_reason")
        if gate:
            _kv("Gate reason", gate)

    elif command == "summarize":
        # Three sub-objects — print a compact summary of each
        status  = details.get("status", {})
        preview = details.get("preview", {})
        report  = details.get("report", {})

        print(f"    [STATUS]")
        _kv("  Crash open",   status.get("crash_open", 0), indent=4)
        _kv("  PAR total",    f"${status.get('par_total', 0.0):.2f}", indent=4)
        _kv("  YTD spent",    f"${status.get('ytd_spent', 0.0):.2f}", indent=4)

        print(f"    [PREVIEW]")
        _kv("  Planned opens", preview.get("planned_opens", 0), indent=4)
        _kv("  Validated",     preview.get("quote_only_validated", 0), indent=4)
        gate = preview.get("gate_reason")
        if gate:
            _kv("  Gate reason",   gate, indent=4)

        print(f"    [REPORT]")
        par_total_r = report.get("par_total")
        _kv("  PAR total",    f"${par_total_r:.2f}" if par_total_r is not None else "N/A", indent=4)
        _kv("  Planned opens", report.get("planned_opens", 0), indent=4)


def _kv(label: str, value, indent: int = 4) -> None:
    prefix = " " * indent
    print(f"{prefix}{label:<26}  {value}")


def _print_broker_drift_section(result: AdapterResult, command: str) -> None:
    """
    Print the broker drift section to stdout for human-readable output.
    Only prints if broker_drift data is present in details.
    """
    details = result.details
    # For summarize, drift is nested under "status"
    if command == "summarize":
        drift = details.get("status", {}).get("broker_drift")
    else:
        drift = details.get("broker_drift")

    if not drift:
        return  # No drift check was run — skip silently

    print("")
    print("─" * 72)
    print("  BROKER DRIFT CHECK (CCC v2.2)")
    print("─" * 72)

    in_sync = drift.get("in_sync", True)
    ccc_cnt  = drift.get("ccc_count", 0)
    ibkr_cnt = drift.get("ibkr_count", 0)
    sync_str = "✓ IN SYNC" if in_sync else "⚠ DRIFT DETECTED"
    print(f"    Status          : {sync_str}")
    print(f"    CCC positions   : {ccc_cnt}")
    print(f"    IBKR positions  : {ibkr_cnt}")

    only_in_ccc  = drift.get("only_in_ccc", [])
    only_in_ibkr = drift.get("only_in_ibkr", [])
    qty_mm       = drift.get("qty_mismatches", [])

    if only_in_ccc:
        print(f"    Only in CCC     : {len(only_in_ccc)} spread(s)")
        for rec in only_in_ccc:
            print(f"      - {rec.get('key', rec)}")

    if only_in_ibkr:
        print(f"    Only in IBKR    : {len(only_in_ibkr)} spread(s)")
        for rec in only_in_ibkr:
            print(f"      - {rec.get('key', rec)}")

    if qty_mm:
        print(f"    Qty mismatches  : {len(qty_mm)}")
        for mm in qty_mm:
            print(f"      - {mm.get('key')}: CCC qty={mm.get('ccc_qty')}, IBKR qty={mm.get('ibkr_qty')}")

    drift_headline = drift.get("headline", "")
    if drift_headline:
        print(f"    Headline        : {drift_headline}")

    print("─" * 72)
    print("")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="trading_adapter.py",
        description=textwrap.dedent("""\
            JP Life Command Center — Trading Adapter v1 CLI

            Read-only adapter for the CCC workflow.
            No action/execute commands in v1.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=["status", "preview", "report", "summarize"],
        help="Command to run",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json_output",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--policy",
        default=None,
        metavar="PATH",
        help="Override policy YAML path (default: configs/allocator_ccc_v1.yaml)",
    )
    parser.add_argument(
        "--campaign",
        default=None,
        metavar="PATH",
        help="Override campaign YAML path (default: configs/campaign_v1.yaml)",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        default=False,
        dest="no_preview",
        help="For 'summarize': skip live preview; use cached artifacts only",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        metavar="N",
        help="Subprocess timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--broker-csv",
        default=None,
        metavar="PATH",
        dest="broker_csv",
        help=(
            "(CCC v2.2) Path to IBKR exported positions CSV. "
            "Runs broker-state drift check; warns if CCC differs from broker. "
            "Applies to 'status' and 'summarize' commands."
        ),
    )

    args = parser.parse_args()

    policy_path    = Path(args.policy)     if args.policy     else None
    campaign_path  = Path(args.campaign)   if args.campaign   else None
    broker_csv_path = Path(args.broker_csv) if args.broker_csv else None

    adapter = TradingAdapter(
        policy_path=policy_path,
        campaign_path=campaign_path,
        timeout_secs=args.timeout,
    )

    command = args.command

    if command == "status":
        result = adapter.status_snapshot(broker_csv_path=broker_csv_path)
    elif command == "preview":
        result = adapter.preview_daily_cycle(
            campaign_path=campaign_path,
            policy_path=policy_path,
        )
    elif command == "report":
        result = adapter.report_snapshot(policy_path=policy_path)
    elif command == "summarize":
        result = adapter.summarize_latest(
            run_preview=not args.no_preview,
            campaign_path=campaign_path,
            policy_path=policy_path,
            broker_csv_path=broker_csv_path,
        )
    else:
        # argparse already validates choices — should not reach here
        parser.error(f"Unknown command: {command}")
        return 2

    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        _print_human(result, command)
        # For status/summarize: print broker drift section if present
        if command in ("status", "summarize") and not args.json_output:
            _print_broker_drift_section(result, command)
        if command == "report" and result.raw_output:
            print(result.raw_output)

    return 0 if (result.ok and result.actionability != "ERROR") else 1


if __name__ == "__main__":
    sys.exit(main())
