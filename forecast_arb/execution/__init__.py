"""
Execution Module

Handles order ticket generation, human review formatting, and IBKR submission.
Safe-by-default: never submits unless explicitly requested.
"""

from .tickets import OrderLeg, OrderTicket, to_dict, from_candidate
from .review import format_review
from .ibkr_submit import submit_tickets, ExecutionResult

__all__ = [
    "OrderLeg",
    "OrderTicket",
    "to_dict",
    "from_candidate",
    "format_review",
    "submit_tickets",
    "ExecutionResult",
]
