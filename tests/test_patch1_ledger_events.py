"""
Patch 1 — Ledger event visibility tests.

Covers:
- read_trade_events() returns event-type entries
- read_trade_events() does not return OPEN/CLOSED status entries
- weekly_pm_review generate_weekly_review() includes Quote Activity section
"""

import json
import os
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from forecast_arb.execution.outcome_ledger import (
    append_trade_event,
    _append_jsonl,
    read_trade_events,
    read_trade_outcomes,
)
from scripts.weekly_pm_review import generate_weekly_review


# ---------------------------------------------------------------------------
# read_trade_events
# ---------------------------------------------------------------------------

def test_read_trade_events_returns_event_entries(tmp_path):
    """read_trade_events returns entries that have an 'event' field."""
    ledger = tmp_path / "runs" / "trade_outcomes.jsonl"
    ledger.parent.mkdir(parents=True)

    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        append_trade_event(
            event="QUOTE_OK",
            intent_id="iid1",
            candidate_id="cid1",
            run_id="run1",
            regime="crash",
            timestamp_utc="2026-03-25T10:00:00+00:00",
            also_global=True,
        )
        append_trade_event(
            event="QUOTE_BLOCKED",
            intent_id="iid2",
            candidate_id="cid2",
            run_id="run1",
            regime="crash",
            timestamp_utc="2026-03-25T10:01:00+00:00",
            also_global=True,
        )
        append_trade_event(
            event="STAGED_PAPER",
            intent_id="iid3",
            candidate_id="cid3",
            run_id="run1",
            regime="crash",
            timestamp_utc="2026-03-25T10:02:00+00:00",
            also_global=True,
        )
    finally:
        os.chdir(original_cwd)

    events = read_trade_events(ledger)
    assert len(events) == 3
    assert events[0]["event"] == "QUOTE_OK"
    assert events[1]["event"] == "QUOTE_BLOCKED"
    assert events[2]["event"] == "STAGED_PAPER"


def test_read_trade_events_skips_status_entries(tmp_path):
    """read_trade_events ignores OPEN/CLOSED entries (no 'event' field)."""
    ledger = tmp_path / "trade_outcomes.jsonl"

    # Write a status-style OPEN entry directly
    _append_jsonl(ledger, {
        "candidate_id": "cid_open",
        "run_id": "run1",
        "regime": "crash",
        "status": "OPEN",
        "intent_id": "iid_open",
        "entry_ts_utc": "2026-03-25T09:00:00+00:00",
        "entry_price": 2.50,
        "qty": 1,
        "expiry": "20260320",
        "long_strike": 580.0,
        "short_strike": 560.0,
    })
    # Write an event entry
    _append_jsonl(ledger, {
        "event": "QUOTE_OK",
        "intent_id": "iid_q",
        "candidate_id": "cid_q",
        "run_id": "run1",
        "regime": "crash",
        "timestamp_utc": "2026-03-25T10:00:00+00:00",
    })

    events = read_trade_events(ledger)
    assert len(events) == 1
    assert events[0]["event"] == "QUOTE_OK"

    # read_trade_outcomes should still work and return the OPEN entry
    outcomes = read_trade_outcomes(ledger)
    assert "cid_open" in outcomes


def test_read_trade_events_empty_on_missing_file(tmp_path):
    """read_trade_events returns [] when file does not exist."""
    result = read_trade_events(tmp_path / "nonexistent.jsonl")
    assert result == []


# ---------------------------------------------------------------------------
# weekly_pm_review — Quote Activity section
# ---------------------------------------------------------------------------

def _seed_ledger(ledger_path: Path, events: list[dict]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    for entry in events:
        _append_jsonl(ledger_path, entry)


def test_weekly_review_includes_quote_activity_section(tmp_path):
    """generate_weekly_review() includes a Quote Activity section."""
    since = datetime(2026, 3, 18, tzinfo=timezone.utc)
    until = datetime(2026, 3, 25, tzinfo=timezone.utc)

    outcomes_path = tmp_path / "trade_outcomes.jsonl"
    _seed_ledger(outcomes_path, [
        {"event": "QUOTE_OK",      "intent_id": "i1", "candidate_id": "c1",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-20T10:00:00+00:00"},
        {"event": "QUOTE_OK",      "intent_id": "i2", "candidate_id": "c2",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-21T10:00:00+00:00"},
        {"event": "QUOTE_BLOCKED", "intent_id": "i3", "candidate_id": "c3",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-22T10:00:00+00:00"},
    ])

    md = generate_weekly_review(
        regime_ledger_path=tmp_path / "regime_ledger.jsonl",
        trade_outcomes_path=outcomes_path,
        dqs_ledger_path=tmp_path / "dqs.jsonl",
        since=since,
        until=until,
    )

    assert "## Quote Activity" in md
    assert "QUOTE_OK" in md
    assert "QUOTE_BLOCKED" in md


def test_weekly_review_quote_counts_are_correct(tmp_path):
    """Quote Activity counts match the seeded event entries."""
    since = datetime(2026, 3, 18, tzinfo=timezone.utc)
    until = datetime(2026, 3, 25, tzinfo=timezone.utc)

    outcomes_path = tmp_path / "trade_outcomes.jsonl"
    _seed_ledger(outcomes_path, [
        {"event": "QUOTE_OK",      "intent_id": "i1", "candidate_id": "c1",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-20T10:00:00+00:00"},
        {"event": "QUOTE_OK",      "intent_id": "i2", "candidate_id": "c2",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-21T10:00:00+00:00"},
        {"event": "QUOTE_BLOCKED", "intent_id": "i3", "candidate_id": "c3",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-22T10:00:00+00:00"},
        {"event": "STAGED_PAPER",  "intent_id": "i4", "candidate_id": "c4",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-23T10:00:00+00:00"},
        # This one is outside the date range — should be excluded
        {"event": "QUOTE_OK",      "intent_id": "i5", "candidate_id": "c5",
         "run_id": "r1", "regime": "crash",
         "timestamp_utc": "2026-03-10T10:00:00+00:00"},
    ])

    md = generate_weekly_review(
        regime_ledger_path=tmp_path / "regime_ledger.jsonl",
        trade_outcomes_path=outcomes_path,
        dqs_ledger_path=tmp_path / "dqs.jsonl",
        since=since,
        until=until,
    )

    # Counts: 2× QUOTE_OK, 1× QUOTE_BLOCKED, 1× STAGED_PAPER (out-of-range excluded)
    lines = md.splitlines()
    quote_ok_line = next(l for l in lines if "QUOTE_OK" in l and "|" in l)
    quote_blocked_line = next(l for l in lines if "QUOTE_BLOCKED" in l and "|" in l)
    staged_line = next(l for l in lines if "STAGED_PAPER" in l and "|" in l)

    assert "| 2 |" in quote_ok_line
    assert "| 1 |" in quote_blocked_line
    assert "| 1 |" in staged_line
    assert "**Total Quote Events:** 4" in md


def test_weekly_review_no_events_shows_empty_message(tmp_path):
    """Quote Activity section shows empty message when no events in range."""
    since = datetime(2026, 3, 18, tzinfo=timezone.utc)
    until = datetime(2026, 3, 25, tzinfo=timezone.utc)

    md = generate_weekly_review(
        regime_ledger_path=tmp_path / "regime_ledger.jsonl",
        trade_outcomes_path=tmp_path / "trade_outcomes.jsonl",
        dqs_ledger_path=tmp_path / "dqs.jsonl",
        since=since,
        until=until,
    )

    assert "## Quote Activity" in md
    assert "No quote events recorded" in md
