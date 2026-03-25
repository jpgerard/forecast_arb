"""
Kalshi Market Status Mapping

Maps user-facing status names to Kalshi API status values.

The Kalshi API uses these status values:
- "active" - Markets that are open for trading (tradable)
- "finalized" - Markets that have been resolved/settled
- Other statuses exist but are not commonly used

This module provides consistent mapping across all scripts.
"""

from typing import Optional, List


# User-facing status → Kalshi API status(es)
# Can map to a list of statuses or None for no filter
STATUS_MAP = {
    "open": ["active"],       # Tradable markets (open for trading)
    "closed": ["finalized"],  # Resolved/settled markets
    "all": None,              # No filter (all statuses)
}


def map_status(user_status: Optional[str]) -> Optional[List[str]]:
    """
    Map user-facing status to Kalshi API status(es).
    
    Args:
        user_status: User-facing status ("open", "closed", "all", or None)
    
    Returns:
        List of Kalshi API status values, or None for no filter
        
    Raises:
        ValueError: If status is not recognized
    
    Examples:
        >>> map_status("open")
        ['active']
        >>> map_status("closed")
        ['finalized']
        >>> map_status("all")
        None
        >>> map_status(None)
        None
    """
    if user_status is None:
        return None
    
    if user_status not in STATUS_MAP:
        valid_statuses = ", ".join(STATUS_MAP.keys())
        raise ValueError(
            f"Invalid status: '{user_status}'. "
            f"Must be one of: {valid_statuses}"
        )
    
    return STATUS_MAP[user_status]


def get_valid_statuses() -> List[str]:
    """
    Get list of valid user-facing status values.
    
    Returns:
        List of status strings (excluding None)
    """
    return [k for k in STATUS_MAP.keys() if k is not None]


def get_debug_description(user_status: Optional[str]) -> str:
    """
    Return a human-readable debug string describing the status mapping.

    Used by kalshi_probe.py and kalshi_series_coverage.py to emit the
    canonical mapping so the operator can see which API statuses are queried.

    Examples:
        >>> get_debug_description("open")
        "requested_status=open -> api_statuses=['active']"
        >>> get_debug_description("all")
        "requested_status=all -> api_statuses=None (all statuses)"
        >>> get_debug_description(None)
        "requested_status=None -> api_statuses=None (all statuses)"

    Args:
        user_status: User-facing status string or None

    Returns:
        Debug description string
    """
    if user_status is None:
        return "requested_status=None -> api_statuses=None (all statuses)"

    if user_status not in STATUS_MAP:
        return f"requested_status={user_status!r} -> INVALID (not in {list(STATUS_MAP.keys())})"

    api = STATUS_MAP[user_status]
    if api is None:
        return f"requested_status={user_status} -> api_statuses=None (all statuses)"

    return f"requested_status={user_status} -> api_statuses={api!r}"
