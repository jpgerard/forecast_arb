"""
Smoke Test: Intent Flow

End-to-end integration test for OrderIntent → execute_trade flow.
Takes a run directory, emits an intent, and runs quote-only mode.

Usage:
    python -m tools.smoke_intent_flow --run-dir runs/crash_venture_v1_1/crash_venture_v1_1_XXX [--rank 1] [--paper|--live]
"""

import argparse
import sys
import subprocess
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.execution.intent_builder import emit_intent_from_run_dir


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Smoke test: Emit intent and run execute_trade in quote-only mode"
    )
    
    parser.add_argument(
        "--run-dir",
        type=str,
        required=True,
        help="Path to run directory containing review_candidates.json"
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=1,
        help="Rank of candidate to test (default: 1)"
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Use IBKR paper trading account"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use IBKR live account (⚠️ use with caution)"
    )
    
    args = parser.parse_args()
    
    # Validate mode
    if not args.paper and not args.live:
        print("❌ Must specify either --paper or --live")
        sys.exit(1)
    
    if args.paper and args.live:
        print("❌ Cannot specify both --paper and --live")
        sys.exit(1)
    
    mode = "live" if args.live else "paper"
    run_dir = Path(args.run_dir)
    
    if not run_dir.exists():
        print(f"❌ Run directory not found: {run_dir}")
        sys.exit(1)
    
    print("=" * 80)
    print("SMOKE TEST: INTENT FLOW")
    print("=" * 80)
    print(f"Run Dir: {run_dir}")
    print(f"Rank: {args.rank}")
    print(f"Mode: {mode.upper()}")
    print("=" * 80)
    print("")
    
    # Step 1: Emit intent from review_candidates.json
    print("STEP 1: Emitting OrderIntent from review_candidates.json")
    print("-" * 80)
    
    try:
        intent_path = emit_intent_from_run_dir(
            run_dir=str(run_dir),
            rank=args.rank
        )
        print(f"✓ Intent emitted: {intent_path}")
    except Exception as e:
        print(f"❌ Failed to emit intent: {e}")
        sys.exit(1)
    
    print("")
    
    # Step 2: Run execute_trade.py in quote-only mode
    print("STEP 2: Running execute_trade.py in quote-only mode")
    print("-" * 80)
    
    cmd = [
        "python",
        "-m",
        "forecast_arb.execution.execute_trade",
        "--intent", intent_path,
        "--quote-only"
    ]
    
    if args.paper:
        cmd.append("--paper")
    else:
        cmd.append("--live")
    
    print(f"Command: {' '.join(cmd)}")
    print("")
    
    try:
        result = subprocess.run(cmd, check=True)
        print("")
        print("=" * 80)
        print("✅ SMOKE TEST PASSED")
        print("=" * 80)
        print("")
        print("Summary:")
        print(f"  ✓ Intent emitted from rank {args.rank}")
        print(f"  ✓ Quote-only mode executed successfully")
        print(f"  ✓ No orders placed (quote-only)")
        print("")
        
    except subprocess.CalledProcessError as e:
        print("")
        print("=" * 80)
        print("❌ SMOKE TEST FAILED")
        print("=" * 80)
        print(f"execute_trade.py failed with exit code {e.returncode}")
        sys.exit(1)
    except FileNotFoundError:
        print("")
        print("=" * 80)
        print("❌ SMOKE TEST FAILED")
        print("=" * 80)
        print("Python executable not found. Ensure Python is in PATH.")
        sys.exit(1)


if __name__ == "__main__":
    main()
