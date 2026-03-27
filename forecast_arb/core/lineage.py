"""
forecast_arb.core.lineage
==========================
Lightweight append-only JSONL lineage tracker for the operator improvement loop.

Tracks artifact-level links among:
    PROPOSALS_NORMALIZED  — reflection report → managed proposals store
    OVERLAY_MATERIALIZED  — proposals → overlay YAML
    EVALUATION_RUN        — overlay + baseline → evaluation artifacts
    PROMOTION_DECIDED     — evaluation + proposals → promotion decision artifact

This is an audit trail, NOT a state store.  Multiple events for the same overlay
are valid (reruns, updates).  Callers needing "latest" should sort by ts_utc or
use ``get_latest_event_by_overlay()``.

Default path: runs/indexes/improvement_lineage.jsonl

Public API
----------
    append_lineage_event(lineage_path, event) -> None
    load_lineage(lineage_path) -> list[dict]
    find_lineage_by_overlay(lineage_path, overlay_path) -> list[dict]
    find_lineage_by_period(lineage_path, since, until) -> list[dict]
    get_latest_event_by_overlay(lineage_path, overlay_path) -> dict | None
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

log = logging.getLogger(__name__)

LINEAGE_EVENT_TYPES = frozenset({
    "PROPOSALS_NORMALIZED",
    "OVERLAY_MATERIALIZED",
    "EVALUATION_RUN",
    "PROMOTION_DECIDED",
})

_DEFAULT_LINEAGE_PATH = Path("runs/indexes/improvement_lineage.jsonl")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def append_lineage_event(lineage_path: Path, event: dict) -> None:
    """
    Append one lineage event as a JSON line.

    Creates parent directories if needed.  Never raises — failures are
    logged as warnings.

    Required fields in event:
        event_type  (one of LINEAGE_EVENT_TYPES)
        ts_utc      (ISO 8601 string)

    Optional fields:
        source_period   dict {"since": str, "until": str}
        proposal_ids    list[str]
        overlay_path    str
        evaluation_path str
        promotion_path  str
        notes           str
    """
    lineage_path = Path(lineage_path)
    try:
        lineage_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, default=str, ensure_ascii=False)
        with open(lineage_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        log.warning("append_lineage_event(%s): %s", lineage_path, exc)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def load_lineage(lineage_path: Path) -> List[dict]:
    """
    Load all lineage events from the JSONL file.

    Returns an empty list if the file does not exist.
    Corrupt lines are skipped with a warning; the rest are returned.
    """
    lineage_path = Path(lineage_path)
    if not lineage_path.exists():
        return []
    events: List[dict] = []
    try:
        with open(lineage_path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError as exc:
                    log.warning(
                        "load_lineage(%s): skipping corrupt line %d: %s",
                        lineage_path, lineno, exc,
                    )
    except Exception as exc:
        log.warning("load_lineage(%s): %s", lineage_path, exc)
    return events


def find_lineage_by_overlay(lineage_path: Path, overlay_path: str) -> List[dict]:
    """
    Return all lineage events where ``event["overlay_path"] == overlay_path``.

    Returns an empty list if the file does not exist or no events match.
    """
    return [
        e for e in load_lineage(lineage_path)
        if e.get("overlay_path") == overlay_path
    ]


def find_lineage_by_period(lineage_path: Path, since: str, until: str) -> List[dict]:
    """
    Return all lineage events where
    ``event["source_period"] == {"since": since, "until": until}``.

    Returns an empty list if no events match.
    """
    target = {"since": since, "until": until}
    return [
        e for e in load_lineage(lineage_path)
        if e.get("source_period") == target
    ]


def get_latest_event_by_overlay(
    lineage_path: Path,
    overlay_path: str,
) -> Optional[dict]:
    """
    Return the most recent lineage event for the given overlay_path.

    "Most recent" is determined by ``event["ts_utc"]`` (lexicographic sort,
    valid because ISO 8601 strings sort chronologically).

    Returns None if the file does not exist or no events match.
    Does NOT raise.
    """
    events = find_lineage_by_overlay(lineage_path, overlay_path)
    if not events:
        return None
    try:
        return max(events, key=lambda e: e.get("ts_utc", ""))
    except Exception as exc:
        log.warning("get_latest_event_by_overlay: %s", exc)
        return events[-1]  # last appended as fallback
