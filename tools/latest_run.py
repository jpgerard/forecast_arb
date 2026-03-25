"""
Latest Run Helper

Prints the path to the latest run and key artifacts for quick access.
Makes daily workflow smoother by providing direct paths to review.
"""

import json
import sys
from pathlib import Path


def main():
    """Print latest run information."""
    runs_root = Path("runs")
    latest_file = runs_root / "LATEST.json"
    
    if not latest_file.exists():
        print("❌ No LATEST.json found")
        print(f"   Expected at: {latest_file.absolute()}")
        sys.exit(1)
    
    # Load latest run info
    with open(latest_file, "r") as f:
        latest = json.load(f)
    
    run_dir = Path(latest.get("run_dir", ""))
    
    if not run_dir.exists():
        print(f"❌ Latest run directory not found: {run_dir}")
        sys.exit(1)
    
    # Print info
    print("=" * 80)
    print("LATEST RUN")
    print("=" * 80)
    print(f"Run ID:       {latest.get('run_id', 'N/A')}")
    print(f"Decision:     {latest.get('decision', 'N/A')}")
    print(f"Reason:       {latest.get('reason', 'N/A')}")
    print(f"Timestamp:    {latest.get('timestamp', 'N/A')}")
    print(f"Run Dir:      {run_dir.absolute()}")
    print("")
    
    # List artifacts
    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.exists():
        print("ARTIFACTS:")
        print("-" * 80)
        
        artifact_files = [
            "review_pack.md",
            "review_candidates.json",
            "decision_template.md",
            "final_decision.json",
            "gate_decision.json",
            "p_event_implied.json",
            "external_source_policy.json",
            "tickets.json",
            "review.txt"
        ]
        
        for artifact_name in artifact_files:
            artifact_path = artifacts_dir / artifact_name
            if artifact_path.exists():
                # Calculate size
                size = artifact_path.stat().st_size
                size_kb = size / 1024
                
                print(f"  ✓ {artifact_name:<30} ({size_kb:.1f} KB)")
                print(f"    {artifact_path.absolute()}")
            else:
                print(f"  - {artifact_name:<30} (not present)")
        
        print("")
    
    # Quick commands
    print("QUICK COMMANDS:")
    print("-" * 80)
    
    review_pack = artifacts_dir / "review_pack.md"
    if review_pack.exists():
        print(f"View review pack:")
        print(f"  code {review_pack.absolute()}")
        print("")
    
    candidates_json = artifacts_dir / "review_candidates.json"
    if candidates_json.exists():
        print(f"View candidates JSON:")
        print(f"  code {candidates_json.absolute()}")
        print("")
    
    final_decision = artifacts_dir / "final_decision.json"
    if final_decision.exists():
        print(f"View final decision:")
        print(f"  type {final_decision.absolute()}")
        print("")
    
    print("=" * 80)


if __name__ == "__main__":
    main()
