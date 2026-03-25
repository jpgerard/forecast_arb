"""
Decision Quality Score (DQS) CLI Tool

Allows operators to manually record decision quality scores for trades/decisions.

Usage:
    python scripts/score_decision.py --candidate-id X --run-id Y --regime crash --dqs 8 --regime 2 --pricing 2 --structure 2 --execution 1 --governance 1 --notes "Good edge but execution could improve"
"""

import argparse
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.core.dqs import create_dqs_entry, append_dqs_entry


def main():
    """CLI entrypoint for recording DQS scores."""
    parser = argparse.ArgumentParser(
        description="Record Decision Quality Score (DQS) for a trade or decision"
    )
    
    # Identification
    parser.add_argument(
        "--candidate-id",
        type=str,
        required=True,
        help="Candidate identifier (e.g., cand_crash_20260320_580_560)"
    )
    parser.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Run identifier"
    )
    parser.add_argument(
        "--regime",
        type=str,
        required=True,
        choices=["crash", "selloff"],
        help="Regime name"
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Run directory (if not provided, only global ledger is written)"
    )
    
    # Scores
    parser.add_argument(
        "--dqs",
        type=int,
        required=True,
        help="Total DQS score (0-10)"
    )
    parser.add_argument(
        "--regime-score",
        type=int,
        required=True,
        help="Regime dimension score (0-2)"
    )
    parser.add_argument(
        "--pricing",
        type=int,
        required=True,
        help="Pricing dimension score (0-2)"
    )
    parser.add_argument(
        "--structure",
        type=int,
        required=True,
        help="Structure dimension score (0-2)"
    )
    parser.add_argument(
        "--execution",
        type=int,
        required=True,
        help="Execution dimension score (0-2)"
    )
    parser.add_argument(
        "--governance",
        type=int,
        required=True,
        help="Governance dimension score (0-2)"
    )
    
    # Notes
    parser.add_argument(
        "--notes",
        type=str,
        default="",
        help="Free-form notes about the decision quality"
    )
    
    args = parser.parse_args()
    
    # Validate score ranges
    if not (0 <= args.dqs <= 10):
        print(f"ERROR: --dqs must be between 0 and 10, got {args.dqs}", file=sys.stderr)
        sys.exit(1)
    
    for score_name, score_value in [
        ("regime-score", args.regime_score),
        ("pricing", args.pricing),
        ("structure", args.structure),
        ("execution", args.execution),
        ("governance", args.governance)
    ]:
        if not (0 <= score_value <= 2):
            print(f"ERROR: --{score_name} must be between 0 and 2, got {score_value}", file=sys.stderr)
            sys.exit(1)
    
    # Create breakdown
    breakdown = {
        "regime": args.regime_score,
        "pricing": args.pricing,
        "structure": args.structure,
        "execution": args.execution,
        "governance": args.governance
    }
    
    # Validate sum
    breakdown_sum = sum(breakdown.values())
    if breakdown_sum != args.dqs:
        print(f"WARNING: Breakdown sum ({breakdown_sum}) != total DQS ({args.dqs})", file=sys.stderr)
    
    # Create DQS entry
    try:
        entry = create_dqs_entry(
            candidate_id=args.candidate_id,
            run_id=args.run_id,
            regime=args.regime,
            dqs_total=args.dqs,
            breakdown=breakdown,
            notes=args.notes
        )
        
        print(f"Created DQS entry for candidate {args.candidate_id}")
        print(f"  Total: {args.dqs}/10")
        print(f"  Breakdown: {breakdown}")
        print(f"  Notes: {args.notes or '(none)'}")
        print()
        
    except Exception as e:
        print(f"ERROR: Failed to create DQS entry: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Determine run_dir
    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.exists():
            print(f"ERROR: Run directory does not exist: {run_dir}", file=sys.stderr)
            sys.exit(1)
        
        try:
            append_dqs_entry(
                run_dir=run_dir,
                entry=entry,
                also_global=True
            )
            
            print(f"✓ DQS entry written:")
            print(f"  Per-run: {run_dir / 'artifacts' / 'dqs.jsonl'}")
            print(f"  Global: runs/dqs.jsonl")
            
        except Exception as e:
            print(f"ERROR: Failed to write DQS entry: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # No run_dir - write only to global
        print("WARNING: No --run-dir provided, writing only to global ledger")
        
        try:
            # Use dummy run_dir and disable per-run write
            dummy_run_dir = Path("runs") / "temp"
            dummy_run_dir.mkdir(parents=True, exist_ok=True)
            
            # Write only to global
            global_ledger_path = Path("runs") / "dqs.jsonl"
            from forecast_arb.core.dqs import _append_jsonl
            _append_jsonl(global_ledger_path, entry)
            
            print(f"✓ DQS entry written to global ledger: {global_ledger_path}")
            
        except Exception as e:
            print(f"ERROR: Failed to write DQS entry: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
