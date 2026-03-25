"""
Edge and confidence gating for trade decisions.

Implements multi-layer gating logic that produces either TRADE or NO_TRADE
decisions based on edge (p_external - p_implied) and confidence thresholds.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass
class GateDecision:
    """
    Gate decision with full provenance.
    
    Attributes:
        decision: "PASS" or "NO_TRADE"
        reason: Reason code (e.g., "EDGE_TOO_SMALL", "LOW_CONFIDENCE", "NO_P_IMPLIED", "PASSED_GATES")
        edge: Edge value (p_external - p_implied) or None
        p_external: External probability or None
        p_implied: Options-implied probability or None
        confidence: Gate confidence (conservative minimum of external and implied)
        confidence_external: Confidence from external source
        confidence_implied: Confidence from implied probability calculation
        metadata: Combined metadata from both sources
    """
    decision: str
    reason: str
    edge: Optional[float]
    p_external: Optional[float]
    p_implied: Optional[float]
    confidence: float
    confidence_external: float
    confidence_implied: Optional[float]
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "edge": self.edge,
            "p_external": self.p_external,
            "p_implied": self.p_implied,
            "confidence_gate": self.confidence,
            "confidence_external": self.confidence_external,
            "confidence_implied": self.confidence_implied,
            "metadata": self.metadata
        }


def gate(
    p_external,  # PEventResult
    p_implied,   # PEventResult
    min_edge: float = 0.05,
    min_confidence: float = 0.60
) -> GateDecision:
    """
    Apply edge and confidence gating to determine if trade should proceed.
    
    Gate rules (evaluated in order):
    1. If p_implied.p_event is None → NO_TRADE: NO_P_IMPLIED
    2. If p_external.p_event is None → NO_TRADE: NO_P_EXTERNAL
    3. edge = p_external.p_event - p_implied.p_event
    4. If p_external.confidence < min_confidence → NO_TRADE: LOW_CONFIDENCE
    5. If edge < min_edge → NO_TRADE: EDGE_TOO_SMALL
    6. Else → TRADE
    
    Args:
        p_external: PEventResult from external source (e.g., Kalshi)
        p_implied: PEventResult from options-implied calculation
        min_edge: Minimum edge required to trade (default 0.05 = 5%)
        min_confidence: Minimum confidence required (default 0.60)
        
    Returns:
        GateDecision with decision, reason, and full provenance
    """
    # Combine metadata from both sources
    combined_metadata = {
        "p_external_metadata": p_external.metadata if p_external else {},
        "p_implied_metadata": p_implied.metadata if p_implied else {},
        "min_edge_threshold": min_edge,
        "min_confidence_threshold": min_confidence
    }
    
    # Extract confidence values
    external_conf = p_external.confidence if p_external else 0.0
    # Only use implied confidence if p_implied exists AND has a valid p_event
    implied_conf = p_implied.confidence if (p_implied and p_implied.p_event is not None) else None
    
    # Compute gate confidence: conservative minimum
    # If implied is available, use min(external, implied), otherwise use 0.0
    gate_confidence = min(external_conf, implied_conf) if implied_conf is not None else 0.0
    
    # Rule 1: Check if p_implied is available
    if p_implied is None or p_implied.p_event is None:
        return GateDecision(
            decision="NO_TRADE",
            reason="NO_P_IMPLIED",
            edge=None,
            p_external=p_external.p_event if p_external else None,
            p_implied=None,
            confidence=gate_confidence,
            confidence_external=external_conf,
            confidence_implied=implied_conf,
            metadata={
                **combined_metadata,
                "confidence_source": "implied",
                "implied_available": False
            }
        )
    
    # Rule 2: Check if p_external is available
    if p_external is None or p_external.p_event is None:
        return GateDecision(
            decision="NO_TRADE",
            reason="NO_P_EXTERNAL",
            edge=None,
            p_external=None,
            p_implied=p_implied.p_event,
            confidence=gate_confidence,
            confidence_external=external_conf,
            confidence_implied=implied_conf,
            metadata=combined_metadata
        )
    
    # Rule 3: Compute edge
    edge = p_external.p_event - p_implied.p_event
    
    # Rule 4: Check confidence (use gate_confidence for threshold check)
    if gate_confidence < min_confidence:
        return GateDecision(
            decision="NO_TRADE",
            reason="LOW_CONFIDENCE",
            edge=edge,
            p_external=p_external.p_event,
            p_implied=p_implied.p_event,
            confidence=gate_confidence,
            confidence_external=external_conf,
            confidence_implied=implied_conf,
            metadata=combined_metadata
        )
    
    # Rule 5: Check edge threshold
    if edge < min_edge:
        return GateDecision(
            decision="NO_TRADE",
            reason="INSUFFICIENT_EDGE",
            edge=edge,
            p_external=p_external.p_event,
            p_implied=p_implied.p_event,
            confidence=gate_confidence,
            confidence_external=external_conf,
            confidence_implied=implied_conf,
            metadata=combined_metadata
        )
    
    # Rule 6: Pass all gates
    return GateDecision(
        decision="PASS",
        reason="PASSED_GATES",
        edge=edge,
        p_external=p_external.p_event,
        p_implied=p_implied.p_event,
        confidence=gate_confidence,
        confidence_external=external_conf,
        confidence_implied=implied_conf,
        metadata=combined_metadata
    )
