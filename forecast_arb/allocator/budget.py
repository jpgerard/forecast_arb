"""
CCC v1 Allocator - Budget tracking and kicker eligibility.

v1.5 (Patch Pack v1.5 Task 2):
  - Legacy row handling: rows missing 'date' or 'action' are skipped with a warning counter.
  - BudgetState.legacy_unusable_count tracks how many rows were ignored (visible in ops console).
  - Spend is derived ONLY from commit-ledger rows with action=="OPEN" and a parseable date.

v1.4: Reads allocator_commit_ledger.jsonl (commit ledger) to compute running spend totals.
Only intents staged / transmitted via ccc_execute.py write to the commit ledger.
Running the allocator planner multiple times in a day does NOT affect spent_today_before.
All arithmetic is deterministic; no randomness.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import BudgetState


# ---------------------------------------------------------------------------
# Ledger reading helpers
# ---------------------------------------------------------------------------

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _iso_week_key(d: date) -> str:
    """Return 'YYYY-Www' string for ISO week."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def read_ledger_records(ledger_path: Path) -> List[Dict[str, Any]]:
    """
    Read all records from the allocator ledger JSONL.

    Returns empty list if file does not exist.
    """
    if not ledger_path.exists():
        return []

    records = []
    with open(ledger_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Corrupt allocator ledger at line {lineno}: {exc}"
                ) from exc
    return records


# ---------------------------------------------------------------------------
# Budget computation
# ---------------------------------------------------------------------------

def compute_budget_state(
    policy: Dict[str, Any],
    ledger_path: Path,
    signals: Optional[Dict[str, Any]] = None,
) -> BudgetState:
    """
    Compute current BudgetState from policy + commit ledger + conditioning signals.

    IMPORTANT (v1.5): This function reads the COMMIT ledger, not the plan ledger.
    Only rows with action=="OPEN" and a parseable "date" field are counted.

    Legacy rows (missing 'action' or missing/invalid 'date') are silently skipped
    with a warning counter written to BudgetState.legacy_unusable_count.
    This ensures backward-compatibility without crashing.

    Args:
        policy:       Validated policy dict from policy.py
        ledger_path:  Path to allocator_commit_ledger.jsonl
        signals:      Optional dict with conditioning signals:
                      - conditioning_confidence: float (0-1)
                      - vix_percentile: float (0-100) or None
                      - credit_stress_elevated: bool or None

    Returns:
        BudgetState with current spend totals and kicker eligibility
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    bp = policy["budgets"]
    kicker_cfg = policy["kicker"]

    budget = BudgetState(
        monthly_baseline=float(bp["monthly_baseline"]),
        monthly_max=float(bp["monthly_max"]),
        weekly_baseline=float(bp["weekly_baseline"]),
        daily_baseline=float(bp["daily_baseline"]),
        weekly_kicker=float(bp["weekly_kicker"]),
        daily_kicker=float(bp["daily_kicker"]),
    )

    today = _today_utc()
    today_key = today.isoformat()
    week_key = _iso_week_key(today)
    month_key = _month_key(today)

    records = read_ledger_records(ledger_path)

    legacy_unusable = 0
    missing_action_count = 0
    missing_date_count = 0

    for rec in records:
        # v1.5: Track legacy rows missing 'action' (old commit records before v1.5)
        if "action" not in rec:
            missing_action_count += 1
            legacy_unusable += 1
            continue

        # Only OPEN actions consume premium budget
        if rec.get("action") != "OPEN":
            continue

        rec_date = rec.get("date", "")

        # v1.5: Track rows with missing/invalid date
        if not rec_date:
            missing_date_count += 1
            legacy_unusable += 1
            continue

        try:
            rec_d = date.fromisoformat(rec_date)
        except (ValueError, TypeError):
            missing_date_count += 1
            legacy_unusable += 1
            continue

        premium_spent = float(rec.get("premium_spent", 0.0))

        if _month_key(rec_d) == month_key:
            budget.spent_month += premium_spent
        if _iso_week_key(rec_d) == week_key:
            budget.spent_week += premium_spent
        if rec_date == today_key:
            budget.spent_today += premium_spent

    # Report legacy row counts
    budget.legacy_unusable_count = legacy_unusable
    if missing_action_count > 0:
        _log.warning(
            f"commit ledger {ledger_path}: {missing_action_count} legacy row(s) missing "
            f"'action' field — skipped (upgrade to v1.5 commit schema to fix). "
            f"These rows DO NOT count toward budget spend."
        )
    if missing_date_count > 0:
        _log.warning(
            f"commit ledger {ledger_path}: {missing_date_count} row(s) with "
            f"missing/invalid 'date' field — skipped."
        )

    # Determine kicker eligibility
    kicker_enabled, kicker_reasons = _check_kicker_eligibility(kicker_cfg, signals or {})
    budget.kicker_enabled = kicker_enabled
    budget.kicker_reasons = kicker_reasons

    return budget


def _check_kicker_eligibility(
    kicker_cfg: Dict[str, Any],
    signals: Dict[str, Any],
) -> tuple[bool, List[str]]:
    """
    Determine whether kicker spending is allowed today.

    All conditions must be TRUE for kicker to be enabled.
    If any required signal is missing → no kicker (conservative default).

    Returns:
        (enabled: bool, reason_codes: list[str])
    """
    reasons: List[str] = []

    min_conf = float(kicker_cfg.get("min_conditioning_confidence", 0.66))
    max_vix_pct = float(kicker_cfg.get("max_vix_percentile", 35.0))

    # 1. Conditioning confidence
    conf = signals.get("conditioning_confidence")
    if conf is None:
        reasons.append("NO_CONDITIONING_CONFIDENCE_SIGNAL")
        return False, reasons
    if float(conf) < min_conf:
        reasons.append(f"CONFIDENCE_TOO_LOW:{conf:.2f}<{min_conf}")
        return False, reasons
    reasons.append(f"CONFIDENCE_OK:{conf:.2f}>={min_conf}")

    # 2. VIX percentile (if available)
    vix_pct = signals.get("vix_percentile")
    if vix_pct is None:
        reasons.append("NO_VIX_PERCENTILE_SIGNAL")
        return False, reasons
    if float(vix_pct) >= max_vix_pct:
        reasons.append(f"VIX_TOO_HIGH:{vix_pct:.1f}>={max_vix_pct}")
        return False, reasons
    reasons.append(f"VIX_OK:{vix_pct:.1f}<{max_vix_pct}")

    # 3. Credit stress (if available)
    credit_elevated = signals.get("credit_stress_elevated")
    if credit_elevated is None:
        reasons.append("NO_CREDIT_STRESS_SIGNAL")
        return False, reasons
    if credit_elevated:
        reasons.append("CREDIT_STRESS_ELEVATED")
        return False, reasons
    reasons.append("CREDIT_STRESS_OK")

    return True, reasons


# ---------------------------------------------------------------------------
# Ledger append
# ---------------------------------------------------------------------------

def append_ledger_record(ledger_path: Path, record: Dict[str, Any]) -> None:
    """
    Append a single record to the allocator ledger JSONL (thread-unsafe, single-process fine).

    Creates parent directories if needed.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
