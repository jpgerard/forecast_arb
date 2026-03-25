"""
CCC v1 Allocator package.

Cost-Controlled Convexity Allocator - manages budget discipline,
inventory targets, harvest rules, and auto-sized opens.
"""
from .plan import run_allocator_plan

__all__ = ["run_allocator_plan"]
