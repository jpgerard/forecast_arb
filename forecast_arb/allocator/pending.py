"""
CCC v1.8 -- Pending Exposure Helpers.

"Pending" = committed intents (in commit ledger, mode paper/live) that have NOT yet
appeared in the fills ledger as POSITION_OPENED.

This is the authoritative definition used by inventory gating.  It DOES NOT depend on
filesystem timestamps, file modification times, or OPEN_*.json scans.

Public API
----------
load_commit_intent_ids(commit_ledger_path, date=None) -> set[str]
    Return set of intent_ids from the commit ledger, optionally filtered to a single date.

load_filled_intent_ids(fills_ledger_path) -> set[str]
    Return set of intent_ids that have a POSITION_OPENED row in the fills ledger.
    ORDER_STAGED rows are deliberately excluded (staged != filled).

compute_pending_intent_ids(...) -> set[str]
    committed minus filled  (for the given date, or all-time if date is None).

pending_counts_by_regime(commit_ledger_rows, pending_intent_ids) -> dict[regime,int]
    {regime: count} for each intent_id that is still pending.
    Rows that lack a regime field (e.g. older ledger entries) are counted under "unknown".
    "unknown" rows are NOT propagated to crash/selloff caps (but still list in warnings).

load_pending_counts(commit_ledger_path, fills_ledger_path, date=None) -> dict[str, int]
    Convenience: load + compute all in one call.
    Returns {"crash": N, "selloff": N} (skips "unknown").
    Warnings are printed for any "unknown" regime rows.
"""
from __future__ import annotations

import json
import logging
from datetime import date as date_type
from pathlib import Path
from typing import Dict, List, Optional, Set

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Commit ledger helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> List[Dict]:
    """Read a JSONL file into a list of dicts.  Returns [] if missing."""
    if not path.exists():
        return []
    rows: List[Dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_commit_ledger_rows(
    commit_ledger_path: Path,
    date: Optional[str] = None,
) -> List[Dict]:
    """
    Return all rows from the commit ledger (optionally filtered to a single date).

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl
        date:               Optional YYYY-MM-DD string. If provided, only rows whose
                            "date" field matches are returned.

    Returns:
        List of row dicts (may be empty).
    """
    rows = _read_jsonl(commit_ledger_path)
    if date is not None:
        rows = [r for r in rows if r.get("date") == date]
    return rows


def load_commit_intent_ids(
    commit_ledger_path: Path,
    date: Optional[str] = None,
) -> Set[str]:
    """
    Return set of intent_ids from the commit ledger — OPEN action rows only.

    Only rows with action="OPEN" (or missing/null action for backward compat)
    are included.  OPEN_CANCELED, OPEN_EXPIRED rows are explicitly excluded so
    that canceled intents are not treated as pending.

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl
        date:               Optional YYYY-MM-DD string filter.

    Returns:
        Set of intent_id strings (non-empty, non-null only).
    """
    rows = load_commit_ledger_rows(commit_ledger_path, date=date)
    ids: Set[str] = set()
    _OPEN_ONLY = {"OPEN", "", None}
    for row in rows:
        action = row.get("action")
        if action not in _OPEN_ONLY:
            continue  # skip OPEN_CANCELED, OPEN_EXPIRED, etc.
        iid = row.get("intent_id")
        if iid and isinstance(iid, str) and iid.strip():
            ids.add(iid.strip())
    return ids


def load_canceled_intent_ids(commit_ledger_path: Path) -> Set[str]:
    """
    Return set of intent_ids that have an OPEN_CANCELED or OPEN_EXPIRED row
    in the commit ledger.

    Used by callers who need to know which intents the operator explicitly
    canceled (or expired), e.g. for status display or audit.

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl

    Returns:
        Set of intent_id strings.
    """
    _CANCEL_ACTIONS = {"OPEN_CANCELED", "OPEN_EXPIRED"}
    rows = _read_jsonl(commit_ledger_path)
    ids: Set[str] = set()
    for row in rows:
        if row.get("action") not in _CANCEL_ACTIONS:
            continue
        iid = row.get("intent_id")
        if iid and isinstance(iid, str) and iid.strip():
            ids.add(iid.strip())
    return ids


# ---------------------------------------------------------------------------
# Fills ledger helpers
# ---------------------------------------------------------------------------


def load_filled_intent_ids(fills_ledger_path: Path) -> Set[str]:
    """
    Return set of intent_ids that have a POSITION_OPENED row in the fills ledger.

    ORDER_STAGED rows are deliberately excluded:
    - ORDER_STAGED = order placed but not yet confirmed filled.
    - POSITION_OPENED = order definitely filled; position exists.

    Only POSITION_OPENED rows "remove" an intent_id from the pending set.

    Args:
        fills_ledger_path: Path to allocator_fills_ledger.jsonl

    Returns:
        Set of intent_id strings.
    """
    rows = _read_jsonl(fills_ledger_path)
    ids: Set[str] = set()
    for row in rows:
        if row.get("action") != "POSITION_OPENED":
            continue
        iid = row.get("intent_id")
        if iid and isinstance(iid, str) and iid.strip():
            ids.add(iid.strip())
    return ids


def load_staged_intent_ids(fills_ledger_path: Path) -> Set[str]:
    """
    Return set of intent_ids that have an ORDER_STAGED row in the fills ledger.

    Used by callers who want to treat staged orders as pending (see spec §E).

    Args:
        fills_ledger_path: Path to allocator_fills_ledger.jsonl

    Returns:
        Set of intent_id strings.
    """
    rows = _read_jsonl(fills_ledger_path)
    ids: Set[str] = set()
    for row in rows:
        if row.get("action") != "ORDER_STAGED":
            continue
        iid = row.get("intent_id")
        if iid and isinstance(iid, str) and iid.strip():
            ids.add(iid.strip())
    return ids


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_pending_intent_ids(
    commit_ledger_path: Path,
    fills_ledger_path: Path,
    date: Optional[str] = None,
) -> Set[str]:
    """
    Compute pending intent_ids = committed − filled − canceled.

    An intent is "pending" if:
      - It appears in the commit ledger with action="OPEN" (optionally on the given
        date), AND
      - It does NOT appear in the fills ledger as POSITION_OPENED, AND
      - It does NOT appear in the commit ledger as OPEN_CANCELED or OPEN_EXPIRED.

    ORDER_STAGED rows in the fills ledger do NOT remove from pending set.
    POSITION_OPENED rows DO remove from pending set.
    OPEN_CANCELED / OPEN_EXPIRED rows in the commit ledger DO remove from pending set.

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        date:               Optional YYYY-MM-DD string. If given, only committed
                            intents for that date are considered.

    Returns:
        Set of pending intent_id strings.
    """
    committed = load_commit_intent_ids(commit_ledger_path, date=date)
    filled = load_filled_intent_ids(fills_ledger_path)
    canceled = load_canceled_intent_ids(commit_ledger_path)
    return committed - filled - canceled


def pending_counts_by_regime(
    commit_ledger_rows: List[Dict],
    pending_intent_ids: Set[str],
) -> Dict[str, int]:
    """
    Count pending intents by regime.

    Iterates commit_ledger_rows; for each row whose intent_id is in
    pending_intent_ids, increments the count for its regime.

    Rows with an unknown/missing regime are counted under "unknown".
    Callers should warn when "unknown" > 0 but MUST NOT count "unknown" toward
    crash/selloff caps.

    Args:
        commit_ledger_rows:  List of commit ledger row dicts (all dates or filtered).
        pending_intent_ids:  Set of intent_ids still pending.

    Returns:
        Dict mapping regime_str → count.  Always contains at least {} (empty dict).
    """
    counts: Dict[str, int] = {}
    for row in commit_ledger_rows:
        iid = row.get("intent_id", "")
        if not iid or iid not in pending_intent_ids:
            continue
        regime = str(row.get("regime") or "").strip().lower()
        if not regime:
            regime = "unknown"
        counts[regime] = counts.get(regime, 0) + 1
    return counts


def load_pending_counts(
    commit_ledger_path: Path,
    fills_ledger_path: Path,
    date: Optional[str] = None,
) -> Dict[str, int]:
    """
    Convenience: load commit rows + fills, return {regime: pending_count}.

    Only "crash" and "selloff" are returned (skips "unknown").
    Prints a WARNING log for any "unknown" regime rows (older ledger entries
    that lack regime field).

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        date:               Optional YYYY-MM-DD filter.

    Returns:
        Dict like {"crash": 1, "selloff": 0}.  Missing regimes default to 0.
    """
    rows = load_commit_ledger_rows(commit_ledger_path, date=date)
    pending_ids = compute_pending_intent_ids(
        commit_ledger_path, fills_ledger_path, date=date
    )
    all_counts = pending_counts_by_regime(rows, pending_ids)

    unknown = all_counts.get("unknown", 0)
    if unknown > 0:
        log.warning(
            f"[pending] {unknown} committed intent(s) have no 'regime' field in commit ledger. "
            "These are listed under 'unknown' and NOT counted toward crash/selloff caps. "
            "Inspect older ledger rows and add 'regime' manually if needed."
        )

    return {
        "crash": all_counts.get("crash", 0),
        "selloff": all_counts.get("selloff", 0),
    }


# ---------------------------------------------------------------------------
# Stale pending helpers (v1.9 Operator Hygiene)
# ---------------------------------------------------------------------------


def load_pending_rows_with_age(
    commit_ledger_path: Path,
    fills_ledger_path: Path,
    today: Optional[str] = None,
) -> List[Dict]:
    """
    Return list of pending OPEN rows enriched with 'age_days'.

    Each returned dict is a copy of the original commit ledger row for a
    still-pending intent, with two extra keys added:
      - "age_days": int  — days since the commit date  (0 if date field missing)
      - "intent_exists": bool — whether intent_path file exists on disk

    Args:
        commit_ledger_path: Path to allocator_commit_ledger.jsonl
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        today:              YYYY-MM-DD string (default: local date at call time)

    Returns:
        List of enriched dicts, one per still-pending intent_id.
        Empty list if no commit ledger or no pending intents.
    """
    import os

    if today is None:
        today = date_type.today().isoformat()

    pending_ids = compute_pending_intent_ids(commit_ledger_path, fills_ledger_path)
    rows = load_commit_ledger_rows(commit_ledger_path)

    _OPEN_ONLY = {"OPEN", "", None}
    result: List[Dict] = []
    seen: Set[str] = set()  # dedup: only first OPEN row per intent_id

    for row in rows:
        if row.get("action") not in _OPEN_ONLY:
            continue
        iid = str(row.get("intent_id", "")).strip()
        if not iid or iid not in pending_ids or iid in seen:
            continue
        seen.add(iid)

        # Compute age
        date_str = row.get("date", "")
        age_days = 0
        if date_str:
            try:
                commit_date = date_type.fromisoformat(date_str)
                today_date = date_type.fromisoformat(today)
                age_days = max(0, (today_date - commit_date).days)
            except (ValueError, TypeError):
                age_days = 0

        # Check intent_path existence
        intent_path_str = row.get("intent_path", "")
        intent_exists = bool(
            intent_path_str and Path(intent_path_str).exists()
        )

        enriched = dict(row)
        enriched["age_days"] = age_days
        enriched["intent_exists"] = intent_exists
        result.append(enriched)

    return result
