"""
Regime Decision Ledger - Append-Only JSONL Logging

Provides append-only logging for regime-level decisions.
Each run writes one entry per regime, capturing what the system
decided even if no orders are placed.

This is the foundation for decision quality analysis and PM review.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional


def append_jsonl(path: Path, obj: dict) -> None:
    """
    Append a JSON object to a JSONL file.
    
    Args:
        path: Path to JSONL file
        obj: Dictionary to append as JSON line
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write as compact JSON with sorted keys for consistency
    line = json.dumps(obj, separators=(",", ":"), sort_keys=False)
    
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_regime_ledger_entry(
    run_dir: Path,
    entry: dict,
    also_global: bool = True
) -> None:
    """
    Write a regime decision ledger entry.
    
    Writes to both per-run and global ledgers:
    - Per-run: <run_dir>/artifacts/regime_ledger.jsonl
    - Global: runs/regime_ledger.jsonl
    
    Args:
        run_dir: Run directory path
        entry: Ledger entry dictionary
        also_global: Whether to also write to global ledger
    """
    # Validate required fields
    required_fields = [
        "ts_utc", "run_id", "regime", "mode", "decision", "reasons",
        "event_hash", "expiry", "moneyness", "spot", "threshold",
        "p_implied", "representable"
    ]
    
    for field in required_fields:
        if field not in entry:
            raise ValueError(f"Missing required field in ledger entry: {field}")
    
    # Ensure ts_utc is present and valid
    if not entry["ts_utc"]:
        entry["ts_utc"] = datetime.now(timezone.utc).isoformat()
    
    # Write to per-run ledger
    run_ledger_path = run_dir / "artifacts" / "regime_ledger.jsonl"
    append_jsonl(run_ledger_path, entry)
    
    # Write to global ledger if requested
    if also_global:
        # Use runs/ as the stable location
        global_ledger_path = Path("runs") / "regime_ledger.jsonl"
        append_jsonl(global_ledger_path, entry)


def create_regime_ledger_entry(
    run_id: str,
    regime: str,
    mode: str,
    decision: str,
    reasons: list,
    event_hash: str,
    expiry: str,
    moneyness: float,
    spot: float,
    threshold: float,
    p_implied: Optional[float],
    p_external: Optional[float],
    representable: bool,
    candidate_id: Optional[str] = None,
    debit: Optional[float] = None,
    max_loss: Optional[float] = None,
    ts_utc: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a regime ledger entry dictionary.
    
    This is a helper to construct the entry with proper schema.
    
    Args:
        run_id: Run identifier
        regime: Regime name ("crash" or "selloff")
        mode: Mode (CRASH_ONLY, SELLOFF_ONLY, BOTH, STAND_DOWN)
        decision: Decision (TRADE, NO_TRADE, STAND_DOWN)
        reasons: List of reason strings
        event_hash: Event hash for this regime
        expiry: Expiry date (YYYYMMDD)
        moneyness: Event moneyness
        spot: Spot price
        threshold: Event threshold
        p_implied: Options-implied probability
        p_external: External probability (Kalshi, etc.)
        representable: Whether event is representable
        candidate_id: Candidate ID if trade selected
        debit: Debit per contract if trade selected
        max_loss: Max loss per contract if trade selected
        ts_utc: Timestamp (defaults to now)
        
    Returns:
        Dictionary conforming to ledger schema
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).isoformat()
    
    # Ensure reasons is a list
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    
    entry = {
        "ts_utc": ts_utc,
        "run_id": run_id,
        "regime": regime,
        "mode": mode,
        "decision": decision,
        "reasons": reasons,
        "event_hash": event_hash,
        "expiry": expiry,
        "moneyness": moneyness,
        "spot": spot,
        "threshold": threshold,
        "p_implied": p_implied,
        "p_external": p_external,
        "representable": representable,
        "candidate_id": candidate_id,
        "debit": debit,
        "max_loss": max_loss
    }
    
    return entry
