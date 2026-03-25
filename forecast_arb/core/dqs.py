"""
Decision Quality Score (DQS) - Scaffolding and Storage

Provides structure for recording decision quality scores post-hoc.
DQS is a manual or semi-automated assessment of decision quality
across multiple dimensions:
- Regime selection quality
- Pricing quality
- Structure quality
- Execution quality
- Governance quality

This enables learning from both winning and losing trades.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional


def create_dqs_entry(
    candidate_id: str,
    run_id: str,
    regime: str,
    dqs_total: int,
    breakdown: Dict[str, int],
    notes: str = "",
    ts_utc: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a DQS entry dictionary.
    
    Args:
        candidate_id: Candidate identifier
        run_id: Run identifier
        regime: Regime name ("crash" or "selloff")
        dqs_total: Total DQS score (0-10)
        breakdown: Score breakdown by dimension
        notes: Free-form notes
        ts_utc: Timestamp (defaults to now)
        
    Returns:
        Dictionary conforming to DQS schema
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).isoformat()
    
    # Validate breakdown has expected keys
    expected_dimensions = ["regime", "pricing", "structure", "execution", "governance"]
    for dim in expected_dimensions:
        if dim not in breakdown:
            raise ValueError(f"Missing dimension in breakdown: {dim}")
    
    entry = {
        "ts_utc": ts_utc,
        "candidate_id": candidate_id,
        "run_id": run_id,
        "regime": regime,
        "dqs_total": dqs_total,
        "breakdown": breakdown,
        "notes": notes
    }
    
    return entry


def append_dqs_entry(
    run_dir: Path,
    entry: Dict[str, Any],
    also_global: bool = True
) -> None:
    """
    Append a DQS entry to ledgers.
    
    Writes to both per-run and global ledgers:
    - Per-run: <run_dir>/artifacts/dqs.jsonl
    - Global: runs/dqs.jsonl
    
    Args:
        run_dir: Run directory path
        entry: DQS entry dictionary
        also_global: Whether to also write to global ledger
    """
    # Validate required fields
    required_fields = ["ts_utc", "candidate_id", "run_id", "regime", "dqs_total", "breakdown"]
    
    for field in required_fields:
        if field not in entry:
            raise ValueError(f"Missing required field in DQS entry: {field}")
    
    # Write to per-run ledger
    run_ledger_path = run_dir / "artifacts" / "dqs.jsonl"
    _append_jsonl(run_ledger_path, entry)
    
    # Write to global ledger if requested
    if also_global:
        global_ledger_path = Path("runs") / "dqs.jsonl"
        _append_jsonl(global_ledger_path, entry)


def _append_jsonl(path: Path, obj: dict) -> None:
    """
    Internal helper to append a JSON object to a JSONL file.
    
    Args:
        path: Path to JSONL file
        obj: Dictionary to append as JSON line
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write as compact JSON
    line = json.dumps(obj, separators=(",", ":"), sort_keys=False)
    
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_dqs_entries(ledger_path: Path) -> list[Dict[str, Any]]:
    """
    Read DQS entries from a ledger.
    
    Args:
        ledger_path: Path to dqs.jsonl
        
    Returns:
        List of DQS entries
    """
    if not ledger_path.exists():
        return []
    
    entries = []
    
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            
            entry = json.loads(line)
            entries.append(entry)
    
    return entries


def compute_dqs_summary(entries: list[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute summary statistics from DQS entries.
    
    Args:
        entries: List of DQS entries
        
    Returns:
        Summary dictionary with avg, min, max, count
    """
    if not entries:
        return {
            "count": 0,
            "avg_total": None,
            "min_total": None,
            "max_total": None,
            "by_regime": {}
        }
    
    totals = [e["dqs_total"] for e in entries]
    
    # By regime breakdown
    by_regime = {}
    for entry in entries:
        regime = entry["regime"]
        if regime not in by_regime:
            by_regime[regime] = []
        by_regime[regime].append(entry["dqs_total"])
    
    regime_summaries = {}
    for regime, scores in by_regime.items():
        regime_summaries[regime] = {
            "count": len(scores),
            "avg": sum(scores) / len(scores),
            "min": min(scores),
            "max": max(scores)
        }
    
    return {
        "count": len(entries),
        "avg_total": sum(totals) / len(totals),
        "min_total": min(totals),
        "max_total": max(totals),
        "by_regime": regime_summaries
    }
