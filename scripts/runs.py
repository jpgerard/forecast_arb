"""
Run operations CLI.

Provides quick access to run summaries, latest run, and historical lookups.
No external dependencies required.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.core.index import load_index, get_recent_runs, find_run_by_id
from forecast_arb.core.latest import get_latest_run


def format_value(val, none_str="-"):
    """Format a value for display."""
    if val is None:
        return none_str
    if isinstance(val, float):
        return f"{val:.3f}"
    if isinstance(val, bool):
        return "Y" if val else "N"
    return str(val)


def cmd_recent(args):
    """Print recent run summaries."""
    runs_root = Path(args.runs_root)
    
    if not runs_root.exists():
        print(f"Error: Runs root not found: {runs_root}")
        return 1
    
    index = load_index(runs_root)
    recent = get_recent_runs(index, n=args.n)
    
    if not recent:
        print("No runs found in index.")
        return 0
    
    # Print header
    print()
    print("Recent Runs:")
    print("=" * 120)
    print(f"{'Timestamp':<20} {'Mode':<10} {'Decision':<10} {'Reason':<25} {'Edge':<8} {'Tickets':<8} {'Submit':<6}")
    print("-" * 120)
    
    # Print rows
    for run in recent:
        timestamp = run.get('timestamp', '-')
        if timestamp and 'T' in timestamp:
            # Strip microseconds and timezone for readability
            timestamp = timestamp.split('.')[0].replace('T', ' ')
        
        mode = run.get('mode', '-')
        if mode and len(mode) > 10:
            mode = mode[:9] + '…'
        
        decision = run.get('decision', '-')
        reason = run.get('reason', '-')
        if reason and len(reason) > 25:
            reason = reason[:24] + '…'
        
        edge = format_value(run.get('edge'))
        num_tickets = format_value(run.get('num_tickets'), '0')
        submit = 'Y' if run.get('submit_executed') else 'N'
        
        print(f"{timestamp:<20} {mode:<10} {decision:<10} {reason:<25} {edge:<8} {num_tickets:<8} {submit:<6}")
    
    print("=" * 120)
    print(f"Total: {len(recent)} run(s)")
    print()
    
    return 0


def cmd_latest(args):
    """Print latest run info and review."""
    runs_root = Path(args.runs_root)
    
    if not runs_root.exists():
        print(f"Error: Runs root not found: {runs_root}")
        return 1
    
    latest = get_latest_run(runs_root)
    
    if not latest:
        print("No latest run pointer found.")
        return 1
    
    print()
    print("=" * 80)
    print("LATEST RUN")
    print("=" * 80)
    print(f"Run ID:     {latest.get('run_id', 'unknown')}")
    print(f"Directory:  {latest.get('run_dir_abs', 'unknown')}")
    print(f"Decision:   {latest.get('decision', 'unknown')}")
    print(f"Reason:     {latest.get('reason', 'unknown')}")
    print(f"Timestamp:  {latest.get('timestamp', 'unknown')}")
    print("=" * 80)
    print()
    
    # Try to read review.txt
    run_dir = Path(latest.get('run_dir_abs', ''))
    review_path = run_dir / "artifacts" / "review.txt"
    
    if review_path.exists():
        print("Review:")
        print("-" * 80)
        with open(review_path, 'r', encoding='utf-8') as f:
            print(f.read())
        print("-" * 80)
    else:
        print(f"No review found at: {review_path}")
    
    print()
    return 0


def cmd_show(args):
    """Show specific run by ID."""
    runs_root = Path(args.runs_root)
    
    if not runs_root.exists():
        print(f"Error: Runs root not found: {runs_root}")
        return 1
    
    index = load_index(runs_root)
    run = find_run_by_id(index, args.run_id)
    
    if not run:
        print(f"Run not found in index: {args.run_id}")
        return 1
    
    print()
    print("=" * 80)
    print(f"RUN: {run.get('run_id')}")
    print("=" * 80)
    print(f"Timestamp:       {run.get('timestamp', 'unknown')}")
    print(f"Mode:            {run.get('mode', 'unknown')}")
    print(f"Decision:        {run.get('decision', 'unknown')}")
    print(f"Reason:          {run.get('reason', 'unknown')}")
    print(f"Edge:            {format_value(run.get('edge'))}")
    print(f"P External:      {format_value(run.get('p_external'))}")
    print(f"P Implied:       {format_value(run.get('p_implied'))}")
    print(f"Confidence:      {format_value(run.get('confidence'))}")
    print(f"Tickets:         {run.get('num_tickets', 0)}")
    print(f"Submit Requested: {format_value(run.get('submit_requested'))}")
    print(f"Submit Executed: {format_value(run.get('submit_executed'))}")
    print(f"Directory:       {run.get('outdir', 'unknown')}")
    print("=" * 80)
    print()
    
    # Try to find and print review
    outdir = run.get('outdir')
    if outdir:
        review_path = Path(outdir) / "artifacts" / "review.txt"
        if review_path.exists():
            print("Review:")
            print("-" * 80)
            with open(review_path, 'r', encoding='utf-8') as f:
                print(f.read())
            print("-" * 80)
        else:
            print(f"Review path: {review_path}")
    
    print()
    return 0


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Run operations CLI - view run summaries and history"
    )
    
    parser.add_argument(
        "--runs-root",
        type=str,
        default="runs",
        help="Root runs directory (default: runs)"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # recent command
    parser_recent = subparsers.add_parser(
        "recent",
        help="Print recent run summaries"
    )
    parser_recent.add_argument(
        "--n",
        type=int,
        default=10,
        help="Number of recent runs to show (default: 10)"
    )
    
    # latest command
    parser_latest = subparsers.add_parser(
        "latest",
        help="Print latest run info and review"
    )
    
    # show command
    parser_show = subparsers.add_parser(
        "show",
        help="Show specific run by ID"
    )
    parser_show.add_argument(
        "run_id",
        type=str,
        help="Run ID to show"
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Dispatch to command handler
    if args.command == "recent":
        return cmd_recent(args)
    elif args.command == "latest":
        return cmd_latest(args)
    elif args.command == "show":
        return cmd_show(args)
    else:
        print(f"Unknown command: {args.command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
