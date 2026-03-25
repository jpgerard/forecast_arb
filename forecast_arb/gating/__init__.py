"""
Gating module for edge and confidence-based trade decisions.
"""

from .edge_gate import gate, GateDecision

__all__ = [
    "gate",
    "GateDecision",
]
