"""
forecast_arb.ops.proposals
===========================
Managed proposal lifecycle for weekly reflection outputs.

Proposal types
--------------
    "parameter"  — suggested change to a named config parameter.
                   May be materialized into a YAML overlay for replay/paper testing.
    "strategy"   — broader structural suggestion (sleeve cadence, regime mix,
                   objective redesign, etc.).
                   Advisory/research only. Cannot reach APPROVED_FOR_REPLAY.

Proposal statuses
-----------------
    PENDING              — awaiting operator review
    APPROVED_FOR_REPLAY  — approved for config overlay + deterministic replay
    APPROVED_FOR_PAPER   — approved for paper trading evaluation
    APPROVED_FOR_RESEARCH — approved for offline research / backtesting
    REJECTED             — declined; kept for audit trail

Public API
----------
    normalize_proposals(reflection_report, source_period, source_report_path=None) -> list[dict]
    load_proposals(path: Path) -> dict
    save_proposals(path: Path, container: dict) -> None
    update_proposal_status(container, proposal_id, new_status, review_reason="") -> bool
    validate_approval_target(proposal_type, target_status) -> None
    append_decision_event(jsonl_path, proposal_id, action, new_status,
                          reason, operator, ts_utc) -> None

Container format
----------------
    {
        "schema_version": "1.0",
        "ts_created": str,
        "ts_updated": str,
        "proposals": [...]
    }

Proposal format — parameter
----------------------------
    {
        "id":                  str  (8-char hex, deterministic),
        "type":                "parameter",
        "status":              str,
        "source_kind":         "parameter_suggestion",
        "source_period":       {"since": str, "until": str},
        "source_ts_utc":       str,
        "source_report_path":  str | null,
        "created_ts_utc":      str,
        "reviewed_ts_utc":     str | null,
        "review_reason":       str | null,
        "parameter":           str,
        "current_value":       any,
        "suggested_value":     any,
        "reasoning":           str,
        "expected_effect":     str,
        "overfit_risk":        "HIGH" | "MEDIUM" | "LOW",
        "confidence":          float | null,
        "promotion_path":      str,
        "overlay_path":        str | null,
    }

Proposal format — strategy
---------------------------
    {
        "id":                  str,
        "type":                "strategy",
        "status":              str,
        "source_kind":         "strategy_hypothesis" | "optimization_opportunity",
        "source_period":       {"since": str, "until": str},
        "source_ts_utc":       str,
        "source_report_path":  str | null,
        "created_ts_utc":      str,
        "reviewed_ts_utc":     str | null,
        "review_reason":       str | null,
        "hypothesis":          str,
        "rationale":           str,
        "confidence":          float | null,
        "expected_outcome":    str,
        "overfit_risk":        "HIGH" | "MEDIUM" | "LOW",
    }
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

log = logging.getLogger(__name__)

PROPOSAL_SCHEMA_VERSION = "1.0"

VALID_STATUSES: FrozenSet[str] = frozenset({
    "PENDING",
    "APPROVED_FOR_REPLAY",
    "APPROVED_FOR_PAPER",
    "APPROVED_FOR_RESEARCH",
    "REJECTED",
})
VALID_TYPES: FrozenSet[str] = frozenset({"parameter", "strategy"})

# Allowed approval targets per proposal type
PARAMETER_APPROVAL_TARGETS: FrozenSet[str] = frozenset({
    "APPROVED_FOR_REPLAY",
    "APPROVED_FOR_PAPER",
    "APPROVED_FOR_RESEARCH",
})
STRATEGY_APPROVAL_TARGETS: FrozenSet[str] = frozenset({
    "APPROVED_FOR_PAPER",
    "APPROVED_FOR_RESEARCH",
    # APPROVED_FOR_REPLAY is deliberately excluded for strategy proposals
})


# ---------------------------------------------------------------------------
# Deterministic ID derivation
# ---------------------------------------------------------------------------


def _derive_id(proposal_type: str, source_period: dict, key_content: str) -> str:
    """
    Derive a stable 8-character hex proposal ID.

    Same (type, period, key_content) always produces the same ID.
    Used to deduplicate proposals across reflection runs within the same period.
    """
    since = source_period.get("since", "")
    until = source_period.get("until", "")
    canonical = f"{proposal_type}|{since}|{until}|{key_content}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Internal builder
# ---------------------------------------------------------------------------


def _base_fields(
    proposal_type: str,
    source_kind: str,
    source_period: dict,
    source_ts_utc: str,
    source_report_path: Optional[str],
    proposal_id: str,
) -> Dict[str, Any]:
    return {
        "id": proposal_id,
        "type": proposal_type,
        "status": "PENDING",
        "source_kind": source_kind,
        "source_period": source_period,
        "source_ts_utc": source_ts_utc,
        "source_report_path": source_report_path,
        "created_ts_utc": datetime.now(timezone.utc).isoformat(),
        "reviewed_ts_utc": None,
        "review_reason": None,
    }


# ---------------------------------------------------------------------------
# Public: normalize
# ---------------------------------------------------------------------------


def normalize_proposals(
    reflection_report: dict,
    source_period: dict,
    source_report_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Normalize a reflection report's outputs into a typed, status-tracked proposal list.

    Sources consumed:
        reflection_report["parameter_suggestions"]      → type="parameter"
        reflection_report["strategy_hypotheses"]         → type="strategy"
        reflection_report["optimization_opportunities"]  → type="strategy"

    ID stability:
        IDs are derived from (type, period, key_content).
        For parameter proposals the key is the parameter name; same parameter
        name within the same period always gets the same ID.  Duplicate
        parameter names within one call are collapsed to the first occurrence.
        For strategy items the key is the first 120 chars of the hypothesis/
        opportunity text (prefixed "opt:" for optimization_opportunities).

    Args:
        reflection_report:   Result dict from run_weekly_reflection().
                             Missing or empty source lists produce no proposals.
        source_period:       {"since": "YYYY-MM-DD", "until": "YYYY-MM-DD"}
        source_report_path:  Absolute path to the report file, if available.

    Returns:
        List of proposal dicts, all with status="PENDING".
        Never raises.
    """
    source_ts_utc = reflection_report.get("ts_utc", "")
    proposals: List[Dict[str, Any]] = []
    seen_ids: set = set()

    # ------------------------------------------------------------------
    # Parameter suggestions  →  type="parameter"
    # ------------------------------------------------------------------
    for item in (reflection_report.get("parameter_suggestions") or []):
        if not isinstance(item, dict):
            continue
        param = item.get("parameter")
        if not param:
            continue
        pid = _derive_id("parameter", source_period, str(param))
        if pid in seen_ids:
            log.debug(
                "normalize_proposals: duplicate parameter proposal id=%s param=%s; skipping",
                pid, param,
            )
            continue
        seen_ids.add(pid)
        base = _base_fields(
            "parameter", "parameter_suggestion",
            source_period, source_ts_utc, source_report_path, pid,
        )
        base.update({
            "parameter": str(param),
            "current_value": item.get("current_value"),
            "suggested_value": item.get("suggested_value"),
            "reasoning": str(item.get("reasoning") or ""),
            "expected_effect": str(item.get("expected_effect") or ""),
            "overfit_risk": str(item.get("overfit_risk") or "HIGH"),
            "confidence": item.get("confidence"),
            "promotion_path": str(item.get("promotion_path") or ""),
            "overlay_path": None,
        })
        proposals.append(base)

    # ------------------------------------------------------------------
    # Strategy hypotheses  →  type="strategy"
    # ------------------------------------------------------------------
    for item in (reflection_report.get("strategy_hypotheses") or []):
        if not isinstance(item, dict):
            continue
        hypothesis = str(item.get("hypothesis") or "").strip()
        if not hypothesis:
            continue
        pid = _derive_id("strategy", source_period, hypothesis[:120])
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        base = _base_fields(
            "strategy", "strategy_hypothesis",
            source_period, source_ts_utc, source_report_path, pid,
        )
        base.update({
            "hypothesis": hypothesis,
            "rationale": str(item.get("rationale") or ""),
            "confidence": item.get("confidence"),
            "expected_outcome": str(item.get("expected_outcome") or ""),
            "overfit_risk": str(item.get("overfit_risk") or "HIGH"),
        })
        proposals.append(base)

    # ------------------------------------------------------------------
    # Optimization opportunities  →  type="strategy"
    # ------------------------------------------------------------------
    for item in (reflection_report.get("optimization_opportunities") or []):
        if not isinstance(item, dict):
            continue
        opportunity = str(item.get("opportunity") or "").strip()
        if not opportunity:
            continue
        pid = _derive_id("strategy", source_period, f"opt:{opportunity[:120]}")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        base = _base_fields(
            "strategy", "optimization_opportunity",
            source_period, source_ts_utc, source_report_path, pid,
        )
        base.update({
            "hypothesis": opportunity,
            "rationale": str(item.get("description") or ""),
            "confidence": item.get("confidence"),
            "expected_outcome": str(item.get("expected_improvement") or ""),
            "overfit_risk": str(item.get("overfit_risk") or "HIGH"),
        })
        proposals.append(base)

    return proposals


# ---------------------------------------------------------------------------
# Public: load / save container
# ---------------------------------------------------------------------------


def _empty_container() -> Dict[str, Any]:
    ts = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "ts_created": ts,
        "ts_updated": ts,
        "proposals": [],
    }


def load_proposals(path: Path) -> Dict[str, Any]:
    """
    Load the managed proposals container from disk.

    Returns an empty, valid container if the file does not exist.
    Propagates JSON parse errors on corrupt files.
    """
    path = Path(path)
    if not path.exists():
        return _empty_container()
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_proposals(path: Path, container: Dict[str, Any]) -> None:
    """
    Write the managed proposals container to disk.

    Creates parent directories if needed. Bumps ts_updated before writing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    container["ts_updated"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(container, fh, indent=2)


# ---------------------------------------------------------------------------
# Public: status management
# ---------------------------------------------------------------------------


def update_proposal_status(
    container: Dict[str, Any],
    proposal_id: str,
    new_status: str,
    review_reason: str = "",
) -> bool:
    """
    Update a proposal's status in-place within the container.

    Sets reviewed_ts_utc to now and stores review_reason.

    Returns:
        True if the proposal was found and updated; False if id not found.
    """
    for proposal in container.get("proposals", []):
        if proposal.get("id") == proposal_id:
            proposal["status"] = new_status
            proposal["reviewed_ts_utc"] = datetime.now(timezone.utc).isoformat()
            proposal["review_reason"] = review_reason if review_reason else None
            return True
    return False


def validate_approval_target(proposal_type: str, target_status: str) -> None:
    """
    Assert that target_status is a valid approval target for proposal_type.

    Raises:
        ValueError: if target_status is unknown, or if a strategy proposal
                    is being approved for APPROVED_FOR_REPLAY.
    """
    if target_status not in VALID_STATUSES:
        raise ValueError(
            f"Unknown status {target_status!r}. "
            f"Valid statuses: {sorted(VALID_STATUSES)}"
        )
    if proposal_type == "strategy" and target_status == "APPROVED_FOR_REPLAY":
        raise ValueError(
            "Strategy proposals cannot be APPROVED_FOR_REPLAY — they cannot be "
            "materialized into config overlays. Use APPROVED_FOR_PAPER or "
            f"APPROVED_FOR_RESEARCH instead. "
            f"Valid targets for strategy: {sorted(STRATEGY_APPROVAL_TARGETS)}"
        )


# ---------------------------------------------------------------------------
# Public: audit log
# ---------------------------------------------------------------------------


def append_decision_event(
    jsonl_path: Path,
    proposal_id: str,
    action: str,
    new_status: str,
    reason: str,
    operator: str,
    ts_utc: str,
) -> None:
    """
    Append one decision event to the proposal_decisions.jsonl audit log.

    Creates the file and parent directories if needed.

    Args:
        jsonl_path:   Path to the JSONL audit log file.
        proposal_id:  8-char hex proposal ID.
        action:       Human-readable action name ("approve" | "reject").
        new_status:   The new status string assigned.
        reason:       Operator note / reason text.
        operator:     Operator identifier string.
        ts_utc:       ISO 8601 UTC timestamp of the decision.
    """
    jsonl_path = Path(jsonl_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_utc": ts_utc,
        "proposal_id": proposal_id,
        "action": action,
        "new_status": new_status,
        "reason": reason or "",
        "operator": operator or "",
    }
    with open(jsonl_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
