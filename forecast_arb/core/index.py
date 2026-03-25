"""
Run index management.

Maintains a rolling index of runs with one-line summaries,
enabling quick audits and historical lookups.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any


def load_index(runs_root: Path) -> Dict:
    """
    Load run index from disk.
    
    Args:
        runs_root: Root runs directory (e.g., "runs")
        
    Returns:
        Index dict with metadata and runs list
    """
    runs_root = Path(runs_root)
    index_path = runs_root / "index.json"
    
    if not index_path.exists():
        # Return empty index structure
        return {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "runs": []
        }
    
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            index = json.load(f)
        
        # Ensure required fields exist
        if "version" not in index:
            index["version"] = 1
        if "runs" not in index:
            index["runs"] = []
        
        return index
    except (json.JSONDecodeError, IOError) as e:
        # If corrupted, start fresh (but log warning)
        print(f"Warning: Could not load index from {index_path}: {e}")
        return {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "runs": []
        }


def append_run(
    index: Dict,
    summary: Dict,
    max_entries: int = 500
) -> Dict:
    """
    Append run summary to index, applying truncation if needed.
    
    Args:
        index: Current index dict
        summary: Run summary dict to append
        max_entries: Maximum number of entries to keep (default: 500)
        
    Returns:
        Updated index dict
    """
    # Add the new run to the front (most recent first)
    runs = index.get("runs", [])
    runs.insert(0, summary)
    
    # Truncate to max_entries (keeping most recent)
    if len(runs) > max_entries:
        runs = runs[:max_entries]
    
    index["runs"] = runs
    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    return index


def write_index(runs_root: Path, index: Dict) -> None:
    """
    Write index to disk.
    
    Args:
        runs_root: Root runs directory (e.g., "runs")
        index: Index dict to write
    """
    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    
    index_path = runs_root / "index.json"
    
    # Update timestamp
    index["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)


def find_run_by_id(index: Dict, run_id: str) -> Optional[Dict]:
    """
    Find a run in the index by run_id.
    
    Args:
        index: Index dict
        run_id: Run ID to search for
        
    Returns:
        Run summary dict or None if not found
    """
    runs = index.get("runs", [])
    
    for run in runs:
        if run.get("run_id") == run_id:
            return run
    
    return None


def get_recent_runs(index: Dict, n: int = 10) -> List[Dict]:
    """
    Get the n most recent runs from index.
    
    Args:
        index: Index dict
        n: Number of runs to return (default: 10)
        
    Returns:
        List of run summary dicts (most recent first)
    """
    runs = index.get("runs", [])
    return runs[:n]
