"""
Latest run pointer for Windows (no symlinks).

Maintains a JSON file pointing to the most recent run, enabling
quick access without scanning directory trees.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union


def set_latest_run(
    runs_root: Path,
    run_dir: Path,
    decision: str,
    reason: str,
    run_id: Optional[str] = None
) -> None:
    """
    Write latest run pointer.
    
    Args:
        runs_root: Root runs directory (e.g., "runs")
        run_dir: Path to the run directory
        decision: Run decision (TRADE/NO_TRADE)
        reason: Decision reason
        run_id: Run ID (extracted from run_dir if not provided)
    """
    runs_root = Path(runs_root)
    run_dir = Path(run_dir)
    
    # Compute relative path from runs_root to run_dir
    try:
        rel_path = run_dir.relative_to(runs_root)
    except ValueError:
        # If run_dir is absolute, make it relative
        rel_path = run_dir.name if run_dir.parent == runs_root else run_dir
    
    # Extract run_id from directory name if not provided
    if run_id is None:
        run_id = run_dir.name
    
    latest_data = {
        "run_dir": str(rel_path).replace("\\", "/"),  # Use forward slashes for consistency
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "reason": reason
    }
    
    latest_path = runs_root / "LATEST.json"
    
    # Ensure runs_root exists
    runs_root.mkdir(parents=True, exist_ok=True)
    
    with open(latest_path, 'w', encoding='utf-8') as f:
        json.dump(latest_data, f, indent=2)


def get_latest_run(runs_root: Path) -> Optional[Dict]:
    """
    Read latest run pointer.
    
    Args:
        runs_root: Root runs directory (e.g., "runs")
        
    Returns:
        Dict with run info or None if not found
    """
    runs_root = Path(runs_root)
    latest_path = runs_root / "LATEST.json"
    
    if not latest_path.exists():
        return None
    
    try:
        with open(latest_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Convert relative path back to absolute
        run_dir_rel = data.get("run_dir", "")
        data["run_dir_abs"] = str(runs_root / run_dir_rel)
        
        return data
    except (json.JSONDecodeError, IOError):
        return None
