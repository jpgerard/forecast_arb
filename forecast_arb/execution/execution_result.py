"""
ExecutionResult v2 Schema

Structured schema for execution results with clear verdicts and tracking.
"""

from typing import Dict, Any, Optional, Literal
from datetime import datetime


ExecutionVerdict = Literal["OK_TO_STAGE", "BLOCKED", "TRANSMITTED"]
ExecutionMode = Literal["quote-only", "paper", "live"]


def create_execution_result(
    intent_id: str,
    mode: ExecutionMode,
    verdict: ExecutionVerdict,
    reason: str,
    quotes: Dict[str, Any],
    limits: Dict[str, Any],
    guards: Dict[str, Any],
    order_id: Optional[str] = None,
    timestamp_utc: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create ExecutionResult v2 schema.
    
    Args:
        intent_id: Intent identifier (e.g., candidate_id)
        mode: Execution mode (quote-only, paper, live)
        verdict: Execution verdict (OK_TO_STAGE, BLOCKED, TRANSMITTED)
        reason: Human-readable reason for verdict
        quotes: Quote data including long, short, combo_mid
        limits: Limit data including intent and effective ranges
        guards: Guard results and violations
        order_id: Order ID if placed
        timestamp_utc: Timestamp (defaults to now)
        
    Returns:
        ExecutionResult dict
    """
    if timestamp_utc is None:
        timestamp_utc = datetime.utcnow().isoformat() + "Z"
    
    result = {
        "intent_id": intent_id,
        "mode": mode,
        "execution_verdict": verdict,
        "reason": reason,
        "quotes": quotes,
        "limits": limits,
        "guards": guards,
        "timestamp_utc": timestamp_utc
    }
    
    if order_id is not None:
        result["order_id"] = order_id
    
    return result


def validate_execution_result(result: Dict[str, Any]) -> None:
    """
    Validate ExecutionResult v2 schema.
    
    Args:
        result: ExecutionResult dict
        
    Raises:
        ValueError: If validation fails
    """
    required_fields = [
        "intent_id", "mode", "execution_verdict", "reason",
        "quotes", "limits", "guards", "timestamp_utc"
    ]
    
    for field in required_fields:
        if field not in result:
            raise ValueError(f"ExecutionResult missing required field: {field}")
    
    # Validate mode
    valid_modes = ["quote-only", "paper", "live"]
    if result["mode"] not in valid_modes:
        raise ValueError(f"Invalid mode: {result['mode']}, must be one of {valid_modes}")
    
    # Validate verdict
    valid_verdicts = ["OK_TO_STAGE", "BLOCKED", "TRANSMITTED"]
    if result["execution_verdict"] not in valid_verdicts:
        raise ValueError(f"Invalid verdict: {result['execution_verdict']}, must be one of {valid_verdicts}")
    
    # Validate quotes structure
    if "long" not in result["quotes"] or "short" not in result["quotes"]:
        raise ValueError("ExecutionResult quotes must include 'long' and 'short'")
    
    # Validate limits structure
    if "intent" not in result["limits"] or "effective" not in result["limits"]:
        raise ValueError("ExecutionResult limits must include 'intent' and 'effective'")
