"""
Campaign Risk Summary

Scans open positions/intents/receipts to compute campaign-wide risk metrics.
Informational only - no enforcement or gating.
"""

import logging
from typing import Dict, Any, Optional
from pathlib import Path
import json

logger = logging.getLogger(__name__)


def get_campaign_risk_summary(
    runs_root: Path = Path("runs"),
    campaign_cap: Optional[float] = None
) -> Dict[str, Any]:
    """
    Compute campaign risk summary by scanning run artifacts.
    
    This is a simple scan of recent runs to estimate open risk.
    For now, it returns zeros since we don't have persistent position tracking.
    
    Args:
        runs_root: Root directory for run artifacts
        campaign_cap: Campaign max loss cap (from config)
        
    Returns:
        Dict with:
        {
            "open_positions": 0,
            "open_max_loss": 0.0,
            "campaign_cap": float | None,
            "remaining_capacity": float | None,
            "warnings": []
        }
    """
    warnings = []
    
    # TODO: Implement actual position tracking
    # For now, return zeros (no persistent position state)
    
    # In a full implementation, this would:
    # 1. Scan runs directory for executed trades
    # 2. Check execution receipts for open positions
    # 3. Sum max_loss across open positions
    # 4. Compare to campaign cap
    
    open_positions = 0
    open_max_loss = 0.0
    
    # Try to estimate from recent successful runs (if any)
    # This is a simple heuristic - not authoritative
    try:
        if runs_root.exists():
            # Look for LATEST.json to find most recent run
            latest_path = runs_root / "LATEST.json"
            if latest_path.exists():
                with open(latest_path, "r") as f:
                    latest = json.load(f)
                    
                # Check if it was a successful trade
                if latest.get("decision") == "TRADE":
                    warnings.append("LATEST_RUN_WAS_TRADE")
                    # Note: We can't definitively say it's "open" without position tracking
    except Exception as e:
        logger.warning(f"Failed to scan runs for risk estimation: {e}")
        warnings.append(f"SCAN_FAILED: {str(e)}")
    
    # Compute remaining capacity
    remaining_capacity = None
    if campaign_cap is not None:
        remaining_capacity = campaign_cap - open_max_loss
    
    return {
        "open_positions": open_positions,
        "open_max_loss": open_max_loss,
        "campaign_cap": campaign_cap,
        "remaining_capacity": remaining_capacity,
        "warnings": warnings,
        "note": "Position tracking not implemented - values are estimates only"
    }


def format_campaign_summary(summary: Dict[str, Any]) -> str:
    """
    Format campaign summary for display.
    
    Args:
        summary: Summary dict from get_campaign_risk_summary
        
    Returns:
        Formatted string
    """
    lines = []
    
    lines.append("Campaign Risk Summary")
    lines.append("-" * 40)
    lines.append(f"Open positions: {summary['open_positions']}")
    lines.append(f"Open max loss: ${summary['open_max_loss']:.2f}")
    
    if summary['campaign_cap'] is not None:
        lines.append(f"Campaign cap: ${summary['campaign_cap']:.2f}")
        lines.append(f"Remaining capacity: ${summary['remaining_capacity']:.2f}")
        
        # Compute utilization percentage
        if summary['campaign_cap'] > 0:
            utilization = (summary['open_max_loss'] / summary['campaign_cap']) * 100
            lines.append(f"Utilization: {utilization:.1f}%")
    else:
        lines.append("Campaign cap: Not configured")
    
    if summary.get('warnings'):
        lines.append("")
        lines.append("Warnings:")
        for warning in summary['warnings']:
            lines.append(f"  - {warning}")
    
    if summary.get('note'):
        lines.append("")
        lines.append(f"Note: {summary['note']}")
    
    return "\n".join(lines)
