"""
Run summary extractor.

Reads artifacts from a run directory and builds a compact summary
for indexing and display.
"""

import json
from pathlib import Path
from typing import Dict, Optional, Any


def extract_summary(run_dir: Path) -> Dict:
    """
    Extract summary from run directory artifacts.
    
    Reads multiple artifact files and consolidates into a single
    summary dict matching the index schema.
    
    Args:
        run_dir: Path to run directory
        
    Returns:
        Summary dict with run info
    """
    run_dir = Path(run_dir)
    artifacts_dir = run_dir / "artifacts"
    
    # Initialize summary with defaults
    summary = {
        "run_id": run_dir.name,
        "timestamp": None,
        "mode": None,
        "outdir": str(run_dir).replace("\\", "/"),
        "decision": "UNKNOWN",
        "reason": "INCOMPLETE_RUN",
        "edge": None,
        "p_external": None,
        "p_implied": None,
        "confidence": None,
        "num_tickets": 0,
        "submit_requested": False,
        "submit_executed": False
    }
    
    # Try to load manifest for basic info
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            
            summary["run_id"] = manifest.get("run_id", run_dir.name)
            summary["timestamp"] = manifest.get("run_time_utc")
            summary["mode"] = manifest.get("mode", "unknown")
            
            # Check for p_event in inputs
            inputs = manifest.get("inputs", {})
            if "p_event" in inputs:
                summary["p_external"] = inputs["p_event"]
                
        except (json.JSONDecodeError, IOError):
            pass
    
    # Try to load final_decision.json (primary source)
    final_decision_path = artifacts_dir / "final_decision.json"
    if final_decision_path.exists():
        try:
            with open(final_decision_path, 'r', encoding='utf-8') as f:
                decision = json.load(f)
            
            summary["decision"] = decision.get("decision", "UNKNOWN")
            summary["reason"] = decision.get("reason", "")
            summary["submit_requested"] = decision.get("submit_requested", False)
            summary["submit_executed"] = decision.get("submit_executed", False)
            
            # Override timestamp if available
            if "timestamp_utc" in decision:
                summary["timestamp"] = decision["timestamp_utc"]
            
            # Get mode from decision if available
            if "mode" in decision:
                summary["mode"] = decision["mode"]
                
        except (json.JSONDecodeError, IOError):
            pass
    
    # Try to load gate_decision.json for edge/confidence
    gate_decision_path = artifacts_dir / "gate_decision.json"
    if gate_decision_path.exists():
        try:
            with open(gate_decision_path, 'r', encoding='utf-8') as f:
                gate = json.load(f)
            
            summary["edge"] = gate.get("edge")
            summary["confidence"] = gate.get("confidence")
            
        except (json.JSONDecodeError, IOError):
            pass
    
    # Try to load p_event_external.json
    p_external_path = artifacts_dir / "p_event_external.json"
    if p_external_path.exists():
        try:
            with open(p_external_path, 'r', encoding='utf-8') as f:
                p_ext = json.load(f)
            
            # Could be a simple value or a dict with 'value' key
            if isinstance(p_ext, dict):
                summary["p_external"] = p_ext.get("value", p_ext.get("p_event"))
            else:
                summary["p_external"] = p_ext
                
        except (json.JSONDecodeError, IOError):
            pass
    
    # Try to load p_event_implied.json
    p_implied_path = artifacts_dir / "p_event_implied.json"
    if p_implied_path.exists():
        try:
            with open(p_implied_path, 'r', encoding='utf-8') as f:
                p_imp = json.load(f)
            
            # Could be a simple value or a dict with 'value' key
            if isinstance(p_imp, dict):
                summary["p_implied"] = p_imp.get("value", p_imp.get("p_event"))
            else:
                summary["p_implied"] = p_imp
                
        except (json.JSONDecodeError, IOError):
            pass
    
    # Try to load tickets.json
    tickets_path = artifacts_dir / "tickets.json"
    if tickets_path.exists():
        try:
            with open(tickets_path, 'r', encoding='utf-8') as f:
                tickets = json.load(f)
            
            # Could be a list or a dict with 'tickets' key
            if isinstance(tickets, list):
                summary["num_tickets"] = len(tickets)
            elif isinstance(tickets, dict) and "tickets" in tickets:
                summary["num_tickets"] = len(tickets["tickets"])
            else:
                summary["num_tickets"] = 0
                
        except (json.JSONDecodeError, IOError):
            pass
    
    return summary


def extract_summary_safe(run_dir: Path) -> Dict:
    """
    Extract summary with exception handling.
    
    If extraction fails completely, returns a minimal summary
    with exception marker.
    
    Args:
        run_dir: Path to run directory
        
    Returns:
        Summary dict (never raises)
    """
    try:
        return extract_summary(run_dir)
    except Exception as e:
        # Return minimal summary on catastrophic failure
        return {
            "run_id": Path(run_dir).name,
            "timestamp": None,
            "mode": "unknown",
            "outdir": str(run_dir).replace("\\", "/"),
            "decision": "NO_TRADE",
            "reason": f"EXCEPTION:{type(e).__name__}",
            "edge": None,
            "p_external": None,
            "p_implied": None,
            "confidence": None,
            "num_tickets": 0,
            "submit_requested": False,
            "submit_executed": False
        }
