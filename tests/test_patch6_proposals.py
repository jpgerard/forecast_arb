"""
Patch 6 — Human-reviewed proposal workflow tests.

Covers:
- proposals.normalize_proposals: parameter proposals extracted with all fields
- proposals.normalize_proposals: strategy_hypotheses extracted
- proposals.normalize_proposals: optimization_opportunities extracted as strategy
- proposals.normalize_proposals: IDs are deterministic (same input → same IDs)
- proposals.normalize_proposals: IDs are stable across two independent calls
- proposals.normalize_proposals: all proposals start with status=PENDING
- proposals.normalize_proposals: empty/absent sources → empty list
- proposals.normalize_proposals: source metadata fields populated
- proposals.load_proposals: nonexistent file → valid empty container
- proposals.save_proposals / load_proposals: roundtrip preserves all data
- proposals.update_proposal_status: known id → True, fields updated
- proposals.update_proposal_status: unknown id → False, no mutation
- proposals.validate_approval_target: parameter proposals accept all three targets
- proposals.validate_approval_target: strategy + APPROVED_FOR_REPLAY → ValueError
- proposals.validate_approval_target: strategy + APPROVED_FOR_PAPER → ok
- proposals.append_decision_event: creates JSONL, second call appends
- review_reflection_proposals._do_list: no proposals → no crash
- review_reflection_proposals._do_approve: updates status, writes reviewed snapshot,
  appends to decisions JSONL
- review_reflection_proposals._do_approve: strategy + APPROVED_FOR_REPLAY → exit 1
- review_reflection_proposals._do_reject: sets REJECTED, records reason
- materialize_parameter_overlay._run: no APPROVED_FOR_REPLAY → exit 0, no file
- materialize_parameter_overlay._run: writes YAML with correct key/value
- materialize_parameter_overlay._run: dotted parameter name → nested YAML
- materialize_parameter_overlay._run: --dry-run → no file written
- materialize_parameter_overlay._run: non-APPROVED_FOR_REPLAY parameter excluded
- materialize_parameter_overlay._run: duplicate parameter — higher confidence wins
- reflection.run_weekly_reflection: new schema fields present in ok result
- reflection.run_weekly_reflection: new schema fields present in empty/error result
- agent_weekly_reflection.run_agent_weekly_reflection: --proposals-out writes managed store
- agent_weekly_reflection.run_agent_weekly_reflection: --proposals-out writes archive snapshot
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from forecast_arb.ops.proposals import (
    PARAMETER_APPROVAL_TARGETS,
    STRATEGY_APPROVAL_TARGETS,
    append_decision_event,
    load_proposals,
    normalize_proposals,
    save_proposals,
    update_proposal_status,
    validate_approval_target,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _param_suggestion(**overrides) -> dict:
    base = {
        "parameter": "dte_min",
        "current_value": 30,
        "suggested_value": 25,
        "reasoning": "Edge concentration at shorter DTE",
        "expected_effect": "More candidates captured",
        "overfit_risk": "MEDIUM",
        "confidence": 0.45,
        "promotion_path": "Run paper for 3 weeks",
    }
    base.update(overrides)
    return base


def _strategy_hypothesis(**overrides) -> dict:
    base = {
        "hypothesis": "Reducing daily run frequency would lower operational cost.",
        "rationale": "Most runs produce NO_TRADE",
        "confidence": 0.30,
        "expected_outcome": "Lower compute cost, same EV",
        "overfit_risk": "LOW",
    }
    base.update(overrides)
    return base


def _opt_opportunity(**overrides) -> dict:
    base = {
        "opportunity": "Simplify sleeve cadence",
        "description": "Run weekly instead of daily",
        "expected_improvement": "Lower ops overhead",
        "overfit_risk": "LOW",
        "confidence": 0.25,
    }
    base.update(overrides)
    return base


def _period(since: str = "2026-03-16", until: str = "2026-03-22") -> dict:
    return {"since": since, "until": until}


def _report_with(param=None, hypothesis=None, opportunity=None, ts="2026-03-23T10:00:00+00:00") -> dict:
    r: dict = {"ts_utc": ts}
    if param is not None:
        r["parameter_suggestions"] = param if isinstance(param, list) else [param]
    if hypothesis is not None:
        r["strategy_hypotheses"] = hypothesis if isinstance(hypothesis, list) else [hypothesis]
    if opportunity is not None:
        r["optimization_opportunities"] = opportunity if isinstance(opportunity, list) else [opportunity]
    return r


# ===========================================================================
# normalize_proposals tests
# ===========================================================================


def test_normalize_parameter_proposals_extracts_all_fields():
    """Parameter suggestions → type=parameter, all payload fields present."""
    report = _report_with(param=_param_suggestion())
    proposals = normalize_proposals(report, _period())

    assert len(proposals) == 1
    p = proposals[0]
    assert p["type"] == "parameter"
    assert p["parameter"] == "dte_min"
    assert p["current_value"] == 30
    assert p["suggested_value"] == 25
    assert p["overfit_risk"] == "MEDIUM"
    assert p["confidence"] == 0.45
    assert p["promotion_path"] == "Run paper for 3 weeks"
    assert p["overlay_path"] is None


def test_normalize_strategy_hypotheses_extracted():
    """strategy_hypotheses → type=strategy proposals."""
    report = _report_with(hypothesis=_strategy_hypothesis())
    proposals = normalize_proposals(report, _period())

    assert len(proposals) == 1
    p = proposals[0]
    assert p["type"] == "strategy"
    assert p["source_kind"] == "strategy_hypothesis"
    assert "Reducing daily run frequency" in p["hypothesis"]
    assert p["expected_outcome"] == "Lower compute cost, same EV"
    assert p["overfit_risk"] == "LOW"
    # strategy proposals have no overlay_path, parameter, etc.
    assert "overlay_path" not in p
    assert "parameter" not in p


def test_normalize_optimization_opportunities_extracted_as_strategy():
    """optimization_opportunities → type=strategy, source_kind=optimization_opportunity."""
    report = _report_with(opportunity=_opt_opportunity())
    proposals = normalize_proposals(report, _period())

    assert len(proposals) == 1
    p = proposals[0]
    assert p["type"] == "strategy"
    assert p["source_kind"] == "optimization_opportunity"
    assert p["hypothesis"] == "Simplify sleeve cadence"
    assert p["rationale"] == "Run weekly instead of daily"
    assert p["expected_outcome"] == "Lower ops overhead"


def test_normalize_mixed_sources():
    """All three sources → three proposals of correct types."""
    report = _report_with(
        param=_param_suggestion(),
        hypothesis=_strategy_hypothesis(),
        opportunity=_opt_opportunity(),
    )
    proposals = normalize_proposals(report, _period())

    assert len(proposals) == 3
    types = [p["type"] for p in proposals]
    assert types.count("parameter") == 1
    assert types.count("strategy") == 2


def test_normalize_ids_are_deterministic():
    """Same report + period → same IDs on two independent calls."""
    report = _report_with(param=_param_suggestion(), hypothesis=_strategy_hypothesis())
    period = _period()

    ids_a = [p["id"] for p in normalize_proposals(report, period)]
    ids_b = [p["id"] for p in normalize_proposals(report, period)]

    assert ids_a == ids_b


def test_normalize_ids_differ_across_periods():
    """Same report content, different period → different IDs."""
    report = _report_with(param=_param_suggestion())
    ids_p1 = [p["id"] for p in normalize_proposals(report, _period("2026-03-16", "2026-03-22"))]
    ids_p2 = [p["id"] for p in normalize_proposals(report, _period("2026-03-23", "2026-03-29"))]

    assert ids_p1 != ids_p2


def test_normalize_all_start_pending():
    """All normalized proposals have status=PENDING."""
    report = _report_with(
        param=_param_suggestion(),
        hypothesis=_strategy_hypothesis(),
        opportunity=_opt_opportunity(),
    )
    proposals = normalize_proposals(report, _period())
    assert all(p["status"] == "PENDING" for p in proposals)


def test_normalize_empty_report_returns_empty():
    """Empty report → empty list."""
    proposals = normalize_proposals({}, _period())
    assert proposals == []


def test_normalize_source_metadata_present():
    """source_kind, source_period, source_ts_utc, source_report_path are all set."""
    ts = "2026-03-23T10:00:00+00:00"
    report = _report_with(param=_param_suggestion(), ts=ts)
    proposals = normalize_proposals(report, _period(), source_report_path="/path/to/report.json")

    p = proposals[0]
    assert p["source_kind"] == "parameter_suggestion"
    assert p["source_period"] == _period()
    assert p["source_ts_utc"] == ts
    assert p["source_report_path"] == "/path/to/report.json"
    assert p["reviewed_ts_utc"] is None
    assert p["review_reason"] is None


def test_normalize_duplicate_parameter_collapses_to_first():
    """Two suggestions for same parameter → only first one kept (same ID)."""
    report = _report_with(param=[
        _param_suggestion(parameter="dte_min", confidence=0.4),
        _param_suggestion(parameter="dte_min", confidence=0.6),  # duplicate
    ])
    proposals = normalize_proposals(report, _period())

    # Only one proposal for dte_min
    param_proposals = [p for p in proposals if p.get("parameter") == "dte_min"]
    assert len(param_proposals) == 1
    # IDs are unique
    ids = [p["id"] for p in proposals]
    assert len(ids) == len(set(ids))


# ===========================================================================
# load_proposals / save_proposals tests
# ===========================================================================


def test_load_proposals_nonexistent_returns_empty_container(tmp_path):
    """Missing file → valid empty container with required keys."""
    container = load_proposals(tmp_path / "nonexistent.json")
    assert container["schema_version"] == "1.0"
    assert container["proposals"] == []
    assert "ts_created" in container
    assert "ts_updated" in container


def test_save_load_roundtrip(tmp_path):
    """Save then load → proposals list preserved exactly."""
    report = _report_with(param=_param_suggestion(), hypothesis=_strategy_hypothesis())
    proposals = normalize_proposals(report, _period())

    proposals_path = tmp_path / "proposals.json"
    container = load_proposals(proposals_path)
    container["proposals"] = proposals
    save_proposals(proposals_path, container)

    loaded = load_proposals(proposals_path)
    assert len(loaded["proposals"]) == 2
    assert loaded["proposals"][0]["id"] == proposals[0]["id"]
    assert loaded["proposals"][1]["type"] == "strategy"


# ===========================================================================
# update_proposal_status tests
# ===========================================================================


def test_update_proposal_status_success():
    """Known id → True, status updated, reviewed_ts_utc set."""
    report = _report_with(param=_param_suggestion())
    proposals = normalize_proposals(report, _period())
    container = {"proposals": proposals}

    pid = proposals[0]["id"]
    result = update_proposal_status(container, pid, "APPROVED_FOR_REPLAY", review_reason="looks good")

    assert result is True
    updated = container["proposals"][0]
    assert updated["status"] == "APPROVED_FOR_REPLAY"
    assert updated["review_reason"] == "looks good"
    assert updated["reviewed_ts_utc"] is not None


def test_update_proposal_status_missing_id():
    """Unknown id → False, container unchanged."""
    container = {"proposals": [{"id": "aaaaaaaa", "status": "PENDING"}]}
    result = update_proposal_status(container, "zzzzzzzz", "APPROVED_FOR_REPLAY")

    assert result is False
    assert container["proposals"][0]["status"] == "PENDING"


# ===========================================================================
# validate_approval_target tests
# ===========================================================================


def test_validate_parameter_all_approval_targets_ok():
    """Parameter proposals accept APPROVED_FOR_REPLAY/PAPER/RESEARCH."""
    for target in PARAMETER_APPROVAL_TARGETS:
        validate_approval_target("parameter", target)  # should not raise


def test_validate_strategy_replay_raises():
    """strategy + APPROVED_FOR_REPLAY → ValueError."""
    with pytest.raises(ValueError, match="APPROVED_FOR_REPLAY"):
        validate_approval_target("strategy", "APPROVED_FOR_REPLAY")


def test_validate_strategy_paper_ok():
    """strategy + APPROVED_FOR_PAPER → no raise."""
    validate_approval_target("strategy", "APPROVED_FOR_PAPER")


def test_validate_strategy_research_ok():
    """strategy + APPROVED_FOR_RESEARCH → no raise."""
    validate_approval_target("strategy", "APPROVED_FOR_RESEARCH")


def test_validate_unknown_status_raises():
    """Unknown status → ValueError for any type."""
    with pytest.raises(ValueError, match="Unknown status"):
        validate_approval_target("parameter", "APPROVED_FOR_PRODUCTION")


# ===========================================================================
# append_decision_event tests
# ===========================================================================


def test_append_decision_event_creates_jsonl(tmp_path):
    """First call creates the file with a valid JSON line."""
    log_path = tmp_path / "decisions.jsonl"
    ts = "2026-03-23T10:00:00+00:00"
    append_decision_event(log_path, "a1b2c3d4", "approve", "APPROVED_FOR_REPLAY",
                          "good candidate", "operator1", ts)

    assert log_path.exists()
    line = log_path.read_text().strip()
    record = json.loads(line)
    assert record["proposal_id"] == "a1b2c3d4"
    assert record["action"] == "approve"
    assert record["new_status"] == "APPROVED_FOR_REPLAY"
    assert record["operator"] == "operator1"
    assert record["ts_utc"] == ts


def test_append_decision_event_appends_second_line(tmp_path):
    """Second call appends a new line; first line is preserved."""
    log_path = tmp_path / "decisions.jsonl"
    ts = "2026-03-23T10:00:00+00:00"
    append_decision_event(log_path, "aaaaaaaa", "approve", "APPROVED_FOR_REPLAY", "", "op", ts)
    append_decision_event(log_path, "bbbbbbbb", "reject", "REJECTED", "not compelling", "op", ts)

    lines = [l for l in log_path.read_text().strip().splitlines() if l]
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["proposal_id"] == "aaaaaaaa"
    assert second["proposal_id"] == "bbbbbbbb"
    assert second["action"] == "reject"


# ===========================================================================
# review_reflection_proposals tests
# ===========================================================================

import review_reflection_proposals as rrp


def _make_store_with_proposals(path: Path, proposals: list) -> None:
    container = load_proposals(path)
    container["proposals"] = proposals
    save_proposals(path, container)


def _make_one_param_proposal(period=None) -> dict:
    period = period or _period()
    report = _report_with(param=_param_suggestion())
    return normalize_proposals(report, period)[0]


def _make_one_strategy_proposal(period=None) -> dict:
    period = period or _period()
    report = _report_with(hypothesis=_strategy_hypothesis())
    return normalize_proposals(report, period)[0]


def test_review_list_empty_no_crash(tmp_path, capsys):
    """list with no proposals prints a message and does not crash."""
    proposals_path = tmp_path / "proposals.json"
    rrp._do_list(proposals_path)
    out = capsys.readouterr().out
    assert "No proposals" in out


def test_review_approve_updates_status_and_writes_jsonl(tmp_path):
    """approve sets status to target, saves proposals, appends decisions JSONL."""
    proposals_path = tmp_path / "proposals.json"
    decisions_path = tmp_path / "decisions.jsonl"
    proposal = _make_one_param_proposal()
    _make_store_with_proposals(proposals_path, [proposal])

    rc = rrp._do_approve(
        proposals_path=proposals_path,
        decisions_log=decisions_path,
        proposal_id=proposal["id"],
        target_status="APPROVED_FOR_REPLAY",
        note="approved for replay",
        operator="test_op",
    )

    assert rc == 0
    container = load_proposals(proposals_path)
    updated = container["proposals"][0]
    assert updated["status"] == "APPROVED_FOR_REPLAY"
    assert updated["review_reason"] == "approved for replay"

    assert decisions_path.exists()
    record = json.loads(decisions_path.read_text().strip())
    assert record["proposal_id"] == proposal["id"]
    assert record["action"] == "approve"
    assert record["new_status"] == "APPROVED_FOR_REPLAY"
    assert record["operator"] == "test_op"


def test_review_approve_writes_reviewed_snapshot(tmp_path):
    """After approve, a _reviewed.json snapshot is written with non-PENDING proposals."""
    proposals_path = tmp_path / "proposals.json"
    decisions_path = tmp_path / "decisions.jsonl"
    proposal = _make_one_param_proposal()
    _make_store_with_proposals(proposals_path, [proposal])

    rrp._do_approve(
        proposals_path=proposals_path,
        decisions_log=decisions_path,
        proposal_id=proposal["id"],
        target_status="APPROVED_FOR_PAPER",
    )

    snapshot_path = proposals_path.parent / "weekly_reflection_proposals_reviewed.json"
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text())
    assert len(snapshot["proposals"]) == 1
    assert snapshot["proposals"][0]["status"] == "APPROVED_FOR_PAPER"


def test_review_approve_strategy_replay_returns_exit_1(tmp_path):
    """strategy proposal + APPROVED_FOR_REPLAY → returns exit code 1."""
    proposals_path = tmp_path / "proposals.json"
    decisions_path = tmp_path / "decisions.jsonl"
    proposal = _make_one_strategy_proposal()
    _make_store_with_proposals(proposals_path, [proposal])

    rc = rrp._do_approve(
        proposals_path=proposals_path,
        decisions_log=decisions_path,
        proposal_id=proposal["id"],
        target_status="APPROVED_FOR_REPLAY",
    )

    assert rc == 1
    # Status must remain PENDING — no mutation on failure
    container = load_proposals(proposals_path)
    assert container["proposals"][0]["status"] == "PENDING"
    # Decisions JSONL must NOT have been written
    assert not decisions_path.exists()


def test_review_reject_updates_status_and_records_reason(tmp_path):
    """reject → REJECTED, review_reason stored, decisions JSONL written."""
    proposals_path = tmp_path / "proposals.json"
    decisions_path = tmp_path / "decisions.jsonl"
    proposal = _make_one_param_proposal()
    _make_store_with_proposals(proposals_path, [proposal])

    rc = rrp._do_reject(
        proposals_path=proposals_path,
        decisions_log=decisions_path,
        proposal_id=proposal["id"],
        note="not convinced",
        operator="op",
    )

    assert rc == 0
    container = load_proposals(proposals_path)
    updated = container["proposals"][0]
    assert updated["status"] == "REJECTED"
    assert updated["review_reason"] == "not convinced"

    record = json.loads(decisions_path.read_text().strip())
    assert record["action"] == "reject"
    assert record["new_status"] == "REJECTED"


# ===========================================================================
# materialize_parameter_overlay tests
# ===========================================================================

import materialize_parameter_overlay as mat


def _make_approved_replay_proposal(
    parameter: str = "dte_min",
    suggested_value: int = 25,
    confidence: float = 0.50,
    period=None,
) -> dict:
    period = period or _period()
    report = _report_with(param=_param_suggestion(
        parameter=parameter,
        suggested_value=suggested_value,
        confidence=confidence,
    ))
    proposal = normalize_proposals(report, period)[0]
    proposal["status"] = "APPROVED_FOR_REPLAY"
    return proposal


def test_materialize_no_approved_returns_zero_no_file(tmp_path):
    """No APPROVED_FOR_REPLAY parameter proposals → exit 0, no overlay written."""
    proposals_path = tmp_path / "proposals.json"
    out_dir = tmp_path / "overlays"
    proposal = _make_one_param_proposal()  # stays PENDING
    _make_store_with_proposals(proposals_path, [proposal])

    rc = mat._run(proposals_path=proposals_path, out_dir=out_dir, dry_run=False)
    assert rc == 0
    assert not out_dir.exists() or not list(out_dir.glob("*.yaml"))


def test_materialize_writes_yaml_with_correct_key_value(tmp_path):
    """One approved parameter proposal → YAML file with correct key/value."""
    proposals_path = tmp_path / "proposals.json"
    out_dir = tmp_path / "overlays"
    proposal = _make_approved_replay_proposal(parameter="dte_min", suggested_value=25)
    _make_store_with_proposals(proposals_path, [proposal])

    rc = mat._run(proposals_path=proposals_path, out_dir=out_dir, dry_run=False)
    assert rc == 0

    yaml_files = list(out_dir.glob("*_reflection_test.yaml"))
    assert len(yaml_files) == 1

    import yaml as _yaml
    content = _yaml.safe_load(yaml_files[0].read_text())
    assert content["dte_min"] == 25


def test_materialize_dotted_parameter_renders_nested_yaml(tmp_path):
    """Dotted parameter name renders as nested YAML structure."""
    proposals_path = tmp_path / "proposals.json"
    out_dir = tmp_path / "overlays"
    proposal = _make_approved_replay_proposal(parameter="structuring.dte_min", suggested_value=28)
    _make_store_with_proposals(proposals_path, [proposal])

    rc = mat._run(proposals_path=proposals_path, out_dir=out_dir, dry_run=False)
    assert rc == 0

    import yaml as _yaml
    yaml_files = list(out_dir.glob("*_reflection_test.yaml"))
    content = _yaml.safe_load(yaml_files[0].read_text())
    assert isinstance(content.get("structuring"), dict)
    assert content["structuring"]["dte_min"] == 28


def test_materialize_dry_run_no_file_written(tmp_path, capsys):
    """--dry-run → YAML printed to stdout, no file written, no proposals updated."""
    proposals_path = tmp_path / "proposals.json"
    out_dir = tmp_path / "overlays"
    proposal = _make_approved_replay_proposal()
    _make_store_with_proposals(proposals_path, [proposal])

    rc = mat._run(proposals_path=proposals_path, out_dir=out_dir, dry_run=True)
    assert rc == 0
    assert not out_dir.exists() or not list(out_dir.glob("*.yaml"))

    # YAML content should be in stdout
    captured = capsys.readouterr()
    assert "dte_min" in captured.out

    # overlay_path must NOT have been set in proposals store
    container = load_proposals(proposals_path)
    assert container["proposals"][0].get("overlay_path") is None


def test_materialize_skips_paper_approved_and_strategy_proposals(tmp_path):
    """APPROVED_FOR_PAPER parameter + APPROVED_FOR_RESEARCH strategy → nothing materialized."""
    proposals_path = tmp_path / "proposals.json"
    out_dir = tmp_path / "overlays"

    param_paper = _make_approved_replay_proposal()
    param_paper["status"] = "APPROVED_FOR_PAPER"  # not REPLAY

    strategy = _make_one_strategy_proposal()
    strategy["status"] = "APPROVED_FOR_RESEARCH"

    _make_store_with_proposals(proposals_path, [param_paper, strategy])

    rc = mat._run(proposals_path=proposals_path, out_dir=out_dir, dry_run=False)
    assert rc == 0
    assert not out_dir.exists() or not list(out_dir.glob("*.yaml"))


def test_materialize_duplicate_parameter_higher_confidence_wins(tmp_path, capsys):
    """Two approved proposals for same parameter: higher confidence is kept, warning emitted."""
    proposals_path = tmp_path / "proposals.json"
    out_dir = tmp_path / "overlays"
    period = _period()

    # Two proposals for dte_min with different IDs (different periods, same name)
    p_low = _make_approved_replay_proposal(
        parameter="dte_min", suggested_value=20, confidence=0.30,
        period={"since": "2026-03-09", "until": "2026-03-15"},
    )
    p_high = _make_approved_replay_proposal(
        parameter="dte_min", suggested_value=27, confidence=0.60,
        period={"since": "2026-03-16", "until": "2026-03-22"},
    )
    # Both APPROVED_FOR_REPLAY
    _make_store_with_proposals(proposals_path, [p_low, p_high])

    rc = mat._run(proposals_path=proposals_path, out_dir=out_dir, dry_run=False)
    assert rc == 0

    import yaml as _yaml
    yaml_files = list(out_dir.glob("*_reflection_test.yaml"))
    assert len(yaml_files) == 1
    content = _yaml.safe_load(yaml_files[0].read_text())
    # Higher confidence (p_high, suggested_value=27) must win
    assert content["dte_min"] == 27

    # Warning must have been emitted (to stderr)
    captured = capsys.readouterr()
    assert "duplicate" in captured.err.lower() or "WARNING" in captured.err


# ===========================================================================
# reflection.run_weekly_reflection — new schema fields tests
# ===========================================================================

from forecast_arb.ops.reflection import run_weekly_reflection


def _valid_reflection_packet() -> dict:
    return {
        "schema_version": "1.0",
        "period": {"since": "2026-03-18", "until": "2026-03-25"},
        "ts_utc": "2026-03-25T12:00:00+00:00",
        "runs_scanned": 5,
        "runs_skipped_no_timestamp": 0,
        "run_dirs_included": [],
        "timestamp_strategy_used": "artifact_ts",
        "trade_summary": {
            "total_runs": 5, "runs_with_trade": 0, "no_trade_runs": 5,
            "submit_executed_count": 0, "outcomes": [],
        },
        "rejection_summary": {
            "total_no_trade": 5,
            "reasons": {"NO_CANDIDATES_GENERATED": 5},
            "preflight_blocked": 0, "gate_rejections": 0, "notes_frequency": {},
        },
        "quote_activity": {
            "QUOTE_OK": 0, "QUOTE_BLOCKED": 0, "STAGED_PAPER": 0,
            "top_block_reasons": [], "data_available": False,
        },
        "signal_stats": {
            "p_implied": {"mean": 0.03, "min": 0.01, "max": 0.05, "n": 5},
            "p_external": {"mean": 0.05, "min": 0.03, "max": 0.07, "n": 5},
            "edge": {"mean": None, "min": None, "max": None, "n": 0},
            "confidence": {"mean": None, "min": None, "max": None, "n": 0},
        },
        "regime_summary": {"regimes_run": {"crash": 5}, "gate_decisions": {}},
        "active_parameters": {
            "structuring": {"dte_range_days": {"min": 30, "max": 60}},
        },
        "config_paths_used": ["configs/structuring_crash_venture_v2.yaml"],
    }


def _mock_openai_response(content: str):
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def test_reflection_new_fields_present_in_ok_result(monkeypatch):
    """When OpenAI returns new fields, they appear correctly in the result."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    packet = _valid_reflection_packet()

    api_json = json.dumps({
        "summary": {
            "headline": "Stable week.",
            "overall_assessment": "MIXED",
            "evidence_strength": "WEAK",
            "n_runs_assessed": 5,
            "n_trades_assessed": 0,
        },
        "what_worked": [], "what_failed": [],
        "calibration_assessment": {"overall": "UNCLEAR", "edge_vs_outcome_narrative": "",
                                    "rejection_pattern_narrative": "", "confidence": 0.2, "caveats": []},
        "market_regime_assessment": {"inferred_regime": "UNCLEAR", "supporting_evidence": "",
                                      "strategy_fit_narrative": "", "confidence": 0.2},
        "parameter_suggestions": [],
        "open_questions": [],
        "weak_evidence_flags": [],
        "strategy_effectiveness_assessment": {
            "verdict": "MARGINAL",
            "rationale": "No trades executed despite available edge.",
            "confidence": 0.35,
            "caveats": ["Small sample"],
        },
        "optimization_opportunities": [
            {
                "opportunity": "Reduce run frequency",
                "description": "Most runs produce NO_TRADE",
                "expected_improvement": "Lower compute cost",
                "overfit_risk": "LOW",
                "confidence": 0.4,
            }
        ],
        "strategy_hypotheses": [
            {
                "hypothesis": "Switching to weekly runs would preserve edge capture.",
                "rationale": "Daily variability is noise",
                "confidence": 0.3,
                "expected_outcome": "Same EV, lower cost",
                "overfit_risk": "LOW",
            }
        ],
    })

    mock_openai = MagicMock()
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_openai_response(api_json)
    mock_openai.OpenAI.return_value = mock_client

    with patch.dict("sys.modules", {"openai": mock_openai}):
        result = run_weekly_reflection(packet)

    assert result["status"] == "ok"

    sea = result["strategy_effectiveness_assessment"]
    assert sea["verdict"] == "MARGINAL"
    assert sea["confidence"] == pytest.approx(0.35)
    assert "Small sample" in sea["caveats"]

    opps = result["optimization_opportunities"]
    assert len(opps) == 1
    assert opps[0]["opportunity"] == "Reduce run frequency"
    assert opps[0]["overfit_risk"] == "LOW"

    hyps = result["strategy_hypotheses"]
    assert len(hyps) == 1
    assert "weekly runs" in hyps[0]["hypothesis"]


def test_reflection_new_fields_present_in_error_result(monkeypatch):
    """Error result (no API key) still includes new fields with safe defaults."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = run_weekly_reflection(_valid_reflection_packet())

    assert result["status"] == "error"
    assert "strategy_effectiveness_assessment" in result
    assert result["strategy_effectiveness_assessment"]["verdict"] == "UNCLEAR"
    assert result["optimization_opportunities"] == []
    assert result["strategy_hypotheses"] == []


# ===========================================================================
# agent_weekly_reflection — proposals_out tests
# ===========================================================================

from agent_weekly_reflection import run_agent_weekly_reflection


def _make_mock_report_with_strategy() -> dict:
    """Mock report including parameter_suggestions and strategy_hypotheses."""
    return {
        "status": "ok",
        "period": {"since": "2026-03-18", "until": "2026-03-25"},
        "summary": {
            "headline": "Low activity week.",
            "overall_assessment": "INSUFFICIENT_DATA",
            "evidence_strength": "WEAK",
            "n_runs_assessed": 2,
            "n_trades_assessed": 0,
        },
        "what_worked": [], "what_failed": [],
        "calibration_assessment": {"overall": "UNCLEAR", "edge_vs_outcome_narrative": "",
                                    "rejection_pattern_narrative": "", "confidence": 0.1, "caveats": []},
        "market_regime_assessment": {"inferred_regime": "UNCLEAR", "supporting_evidence": "",
                                      "strategy_fit_narrative": "", "confidence": 0.1},
        "parameter_suggestions": [
            {"parameter": "crash_p_threshold", "current_value": 0.015, "suggested_value": 0.02,
             "reasoning": "x", "expected_effect": "y", "overfit_risk": "MEDIUM",
             "confidence": 0.25, "promotion_path": "test 3 weeks"}
        ],
        "strategy_hypotheses": [
            {"hypothesis": "Switch to weekly runs.", "rationale": "Noise reduction.",
             "confidence": 0.30, "expected_outcome": "Same EV, lower cost", "overfit_risk": "LOW"}
        ],
        "optimization_opportunities": [],
        "strategy_effectiveness_assessment": {"verdict": "UNCLEAR", "rationale": "",
                                               "confidence": 0.0, "caveats": []},
        "open_questions": [],
        "weak_evidence_flags": ["Only 2 runs"],
        "raw_response": "{}", "error": None,
        "ts_utc": "2026-03-25T12:00:01+00:00",
    }


def test_agent_weekly_proposals_out_writes_managed_store(tmp_path):
    """--proposals-out causes managed proposals store to be written."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    out_dir = tmp_path / "out"
    proposals_out = tmp_path / "proposals" / "weekly_reflection_proposals.json"

    with patch("agent_weekly_reflection.run_weekly_reflection",
               return_value=_make_mock_report_with_strategy()):
        run_agent_weekly_reflection(
            runs_root=runs_root,
            since="2026-03-18",
            until="2026-03-25",
            out_dir=out_dir,
            reflect=True,
            proposals_out=proposals_out,
        )

    assert proposals_out.exists()
    container = load_proposals(proposals_out)
    assert len(container["proposals"]) == 2  # 1 parameter + 1 strategy

    types = {p["type"] for p in container["proposals"]}
    assert "parameter" in types
    assert "strategy" in types
    assert all(p["status"] == "PENDING" for p in container["proposals"])


def test_agent_weekly_proposals_out_writes_archive_snapshot(tmp_path):
    """--proposals-out also writes period-scoped archive snapshot."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    out_dir = tmp_path / "out"
    proposals_out = tmp_path / "proposals" / "weekly_reflection_proposals.json"

    with patch("agent_weekly_reflection.run_weekly_reflection",
               return_value=_make_mock_report_with_strategy()):
        run_agent_weekly_reflection(
            runs_root=runs_root,
            since="2026-03-18",
            until="2026-03-25",
            out_dir=out_dir,
            reflect=True,
            proposals_out=proposals_out,
        )

    archive_path = proposals_out.parent / "archive" / "2026-03-18_2026-03-25_weekly_reflection_proposals.json"
    assert archive_path.exists()

    archive = json.loads(archive_path.read_text())
    assert archive["schema_version"] == "1.0"
    assert len(archive["proposals"]) == 2


def test_agent_weekly_proposals_out_skips_if_reviewed_proposals_exist(tmp_path):
    """proposals-out guard: if reviewed proposals exist, write is skipped."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    out_dir = tmp_path / "out"
    proposals_out = tmp_path / "proposals" / "weekly_reflection_proposals.json"

    # Pre-populate with a reviewed proposal
    existing_proposal = _make_one_param_proposal()
    existing_proposal["status"] = "APPROVED_FOR_REPLAY"
    container = load_proposals(proposals_out)
    container["proposals"] = [existing_proposal]
    save_proposals(proposals_out, container)

    with patch("agent_weekly_reflection.run_weekly_reflection",
               return_value=_make_mock_report_with_strategy()):
        run_agent_weekly_reflection(
            runs_root=runs_root,
            since="2026-03-18",
            until="2026-03-25",
            out_dir=out_dir,
            reflect=True,
            proposals_out=proposals_out,
        )

    # Store should still contain the original reviewed proposal (not overwritten)
    container_after = load_proposals(proposals_out)
    assert container_after["proposals"][0]["status"] == "APPROVED_FOR_REPLAY"
