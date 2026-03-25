"""
forecast_arb.adapter — Trading Adapter v1 (Thin, Read-Mostly)

Thin invocation + summarization layer that wraps the CCC workflow for use
by the JP Life Command Center or any agent/LLM caller.

CCC remains the sole authority for all trade logic, gating, staging, and
execution discipline. This adapter only translates, invokes, and summarizes.

Public surface (v1):
    AdapterResult    — output schema (machine-readable)
    TradingAdapter   — main class:
                         .status_snapshot()      — Task A
                         .preview_daily_cycle()  — Task B
                         .report_snapshot()      — Task C
                         .summarize_latest()     — Task D

Usage:
    from forecast_arb.adapter import TradingAdapter, AdapterResult

    adapter = TradingAdapter()
    result = adapter.status_snapshot()
    print(result.headline)
    print(result.to_dict())
"""

from .trading_adapter import AdapterResult, TradingAdapter

__all__ = [
    "AdapterResult",
    "TradingAdapter",
]
