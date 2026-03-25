"""
CCC v1 Allocator - Annual premium budget control (Phase 2A Task A).

Provides YTD / MTD premium spend computation from the commit ledger.
Used by open_plan.py to gate OPEN actions against the annual convexity budget.

Design:
  - Standalone module; no circular imports with budget.py.
  - Reads the COMMIT ledger (same source as budget.compute_budget_state).
  - Only OPEN rows with action=="OPEN" and a parseable date are counted.
  - Year / month are based on UTC calendar.
  - Returns 0.0 safely when ledger does not exist (new deployment).

Backward-compat guarantee:
  - If annual_convexity_budget is absent from policy YAML,
    BudgetState.annual_convexity_budget defaults to float('inf') and
    BudgetState.annual_budget_enabled returns False.
    All OPEN gating is skipped in that case.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .budget import read_ledger_records


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_premium_spent_ytd(commit_ledger_path: Path) -> float:
    """
    Compute total premium spent year-to-date (UTC) from the commit ledger.

    Only rows with action=="OPEN" and a parseable date in the current UTC
    calendar year are included.

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl.

    Returns:
        YTD premium spent in dollars.  Returns 0.0 if:
          - ledger does not exist (new deployment)
          - ledger contains no usable OPEN rows for the current year
    """
    return compute_premium_spent_breakdown(commit_ledger_path)["ytd"]


def compute_premium_spent_breakdown(commit_ledger_path: Path) -> Dict[str, Any]:
    """
    Compute YTD and MTD premium spent from the commit ledger.

    Reads all OPEN rows and partitions by year / month.  Rows that are missing
    the "date" field or have an un-parseable date are silently skipped (same
    conservative treatment as budget.py).

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl.

    Returns:
        dict with:
            ytd   (float) — year-to-date premium spent
            mtd   (float) — month-to-date premium spent
            current_year  (int)  — UTC calendar year
            current_month (str)  — "YYYY-MM"
    """
    today = _today_utc()
    current_year = today.year
    current_month = f"{today.year}-{today.month:02d}"

    ytd: float = 0.0
    mtd: float = 0.0

    try:
        records = read_ledger_records(commit_ledger_path)
    except (FileNotFoundError, ValueError, OSError):
        records = []

    for rec in records:
        # Only OPEN rows consume premium budget
        if rec.get("action") != "OPEN":
            continue

        rec_date_str = rec.get("date", "")
        if not rec_date_str:
            continue

        try:
            rec_d = date.fromisoformat(str(rec_date_str))
        except (ValueError, TypeError):
            continue

        premium_spent = float(rec.get("premium_spent", 0.0))

        if rec_d.year == current_year:
            ytd += premium_spent
            month_key = f"{rec_d.year}-{rec_d.month:02d}"
            if month_key == current_month:
                mtd += premium_spent

    return {
        "ytd": round(ytd, 2),
        "mtd": round(mtd, 2),
        "current_year": current_year,
        "current_month": current_month,
    }
