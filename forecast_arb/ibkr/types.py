"""
IBKR Data Types

Structured results for spot price fetching and snapshot creation.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SpotResult:
    """
    Result of spot price fetch operation.
    
    Attributes:
        ok: Whether spot fetch succeeded
        spot: Spot price (None if failed)
        source: Source of spot ("last", "midpoint", "close", "cached")
        is_stale: Whether spot is stale (e.g., using previous close)
        warnings: List of warning messages
        reason: Reason for failure (None if ok=True)
        audit: Audit trail with raw price fields and cache info
    """
    ok: bool
    spot: Optional[float]
    source: Optional[str]
    is_stale: bool
    warnings: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    audit: dict = field(default_factory=dict)


@dataclass
class SnapshotResult:
    """
    Result of snapshot creation operation.
    
    Attributes:
        ok: Whether snapshot creation succeeded
        snapshot: Snapshot dict (None if failed)
        reason: Reason for failure (None if ok=True)
        warnings: List of warning messages
        spot_result: SpotResult from spot price fetch
    """
    ok: bool
    snapshot: Optional[dict]
    reason: Optional[str]
    warnings: list[str] = field(default_factory=list)
    spot_result: Optional[SpotResult] = None
