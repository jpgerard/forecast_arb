"""
tests/test_patch8_promotion.py
================================
Unit and integration tests for Patch 8 — promotion workflow + lineage tracking.

Coverage
--------
- forecast_arb.ops.promotion.build_promotion_decision (all decision paths)
- forecast_arb.core.lineage (append, load, find, get_latest)
- scripts/review_overlay_promotion.run_promotion (dry-run, artifacts, lineage)
- lineage hooks in evaluate_parameter_overlay.run_evaluation
- lineage hooks in materialize_parameter_overlay._run
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from forecast_arb.core.lineage import (
    append_lineage_event,
    find_lineage_by_overlay,
    find_lineage_by_period,
    get_latest_event_by_overlay,
    load_lineage,
)
from forecast_arb.ops.promotion import (
    DO_NOT_PROMOTE,
    PROMOTE,
    build_promotion_decision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = "2024-03-15T12:00:00+00:00"


def _comparison(
    assessment: str = PROMOTE,
    coverage: float = 0.80,
    gate_delta: float = 0.25,
    n_runs: int = 10,
    caveats: list | None = None,
    requires_rerun: list | None = None,
    unknown: list | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "simulated_only": True,
        "assessment": assessment,
        "assessment_rationale": "test rationale",
        "assessment_caveats": caveats or [],
        "requires_rerun_parameters": requires_rerun or [],
        "unknown_parameters": unknown or [],
        "fully_evaluable_parameters": ["edge_gating.min_edge"],
        "partially_evaluable_parameters": [],
        "delta": {
            "gate_pass_rate": gate_delta,
            "no_trade_rate": -0.05,
            "runs_total": n_runs,
            "coverage_rate": coverage,
        },
    }


def _param_proposal(
    pid: str = "abc12345",
    overfit: str = "LOW",
    confidence: float = 0.75,
    source_kind: str = "parameter_suggestion",
    source_period: dict | None = None,
) -> dict:
    return {
        "id": pid,
        "type": "parameter",
        "status": "APPROVED_FOR_REPLAY",
        "source_kind": source_kind,
        "source_period": source_period or {"since": "2024-03-01", "until": "2024-03-15"},
        "parameter": "edge_gating.min_edge",
        "current_value": 0.05,
        "suggested_value": 0.07,
        "overfit_risk": overfit,
        "confidence": confidence,
    }


def _strategy_proposal(pid: str = "strat001") -> dict:
    return {
        "id": pid,
        "type": "strategy",
        "status": "APPROVED_FOR_RESEARCH",
        "source_kind": "strategy_hypothesis",
        "source_period": {"since": "2024-03-01", "until": "2024-03-15"},
        "hypothesis": "Test hypothesis",
        "confidence": 0.60,
        "overfit_risk": "LOW",
    }


# ---------------------------------------------------------------------------
# build_promotion_decision — happy path
# ---------------------------------------------------------------------------


class TestBuildPromotionDecisionHappyPath:
    def test_promote_when_all_conditions_met(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal()],
            overlay_path="/path/overlay.yaml",
            evaluation_path="/path/eval/",
            ts_utc=_TS,
        )
        assert result["decision"] == PROMOTE
        assert result["status"] == "ok"

    def test_schema_fields_present(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal()],
            overlay_path="/path/overlay.yaml",
            evaluation_path="/path/eval/",
            ts_utc=_TS,
        )
        for field in (
            "schema_version", "status", "simulated_only", "simulation_disclaimer",
            "overlay_path", "evaluation_path", "proposal_ids", "source_kind_counts",
            "decision", "reasoning", "confidence", "confidence_note", "warnings",
            "blockers", "ts_utc",
        ):
            assert field in result, f"missing field: {field}"

    def test_simulated_only_always_true(self):
        result = build_promotion_decision(
            comparison=_comparison(),
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["simulated_only"] is True

    def test_proposal_ids_captured(self):
        p1 = _param_proposal("id001")
        p2 = _param_proposal("id002")
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[p1, p2],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert set(result["proposal_ids"]) == {"id001", "id002"}

    def test_source_kind_counts(self):
        p1 = _param_proposal("id001", source_kind="parameter_suggestion")
        p2 = _param_proposal("id002", source_kind="optimization_opportunity")
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[p1, p2],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        skc = result["source_kind_counts"]
        assert skc["parameter_suggestion"] == 1
        assert skc["optimization_opportunity"] == 1

    def test_confidence_is_base_times_coverage(self):
        proposal = _param_proposal(confidence=0.80)
        comp = _comparison(assessment=PROMOTE, coverage=0.75)
        result = build_promotion_decision(
            comparison=comp, proposals=[proposal],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        expected = round(0.80 * 0.75, 4)
        assert result["confidence"] == pytest.approx(expected)

    def test_confidence_uses_minimum_proposal_confidence(self):
        p1 = _param_proposal("id1", confidence=0.90)
        p2 = _param_proposal("id2", confidence=0.50)
        comp = _comparison(assessment=PROMOTE, coverage=1.0)
        result = build_promotion_decision(
            comparison=comp, proposals=[p1, p2],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        expected = round(0.50 * 1.0, 4)
        assert result["confidence"] == pytest.approx(expected)

    def test_confidence_zero_when_no_proposals(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["confidence"] == 0.0

    def test_confidence_note_present_and_non_empty(self):
        result = build_promotion_decision(
            comparison=_comparison(),
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        note = result.get("confidence_note", "")
        assert "coverage" in note.lower() or "confidence" in note.lower()
        assert len(note) > 10


# ---------------------------------------------------------------------------
# build_promotion_decision — DO_NOT_PROMOTE paths
# ---------------------------------------------------------------------------


class TestBuildPromotionDecisionBlockers:
    def test_do_not_promote_assessment_keep_testing(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment="KEEP_TESTING"),
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["decision"] == DO_NOT_PROMOTE

    def test_do_not_promote_assessment_no_change(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment="NO_CHANGE"),
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["decision"] == DO_NOT_PROMOTE

    def test_do_not_promote_high_overfit(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal(overfit="HIGH")],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["decision"] == DO_NOT_PROMOTE

    def test_high_overfit_blocker_in_output(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal(overfit="HIGH")],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert any("overfit" in b.lower() for b in result["blockers"])

    def test_do_not_promote_strategy_proposal(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_strategy_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["decision"] == DO_NOT_PROMOTE

    def test_strategy_proposal_reasoning_mentions_research_or_paper(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_strategy_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        blockers_text = " ".join(result.get("blockers", []))
        assert "research" in blockers_text.lower() or "paper review" in blockers_text.lower()

    def test_mixed_strategy_and_param_still_blocked(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal(), _strategy_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["decision"] == DO_NOT_PROMOTE

    def test_error_status_on_none_comparison(self):
        result = build_promotion_decision(
            comparison=None,
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["status"] == "error"
        assert result["decision"] is None

    def test_error_status_on_empty_comparison(self):
        result = build_promotion_decision(
            comparison={},
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["status"] == "error"

    def test_blockers_empty_when_promoting(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert result["blockers"] == []


# ---------------------------------------------------------------------------
# build_promotion_decision — warnings
# ---------------------------------------------------------------------------


class TestBuildPromotionDecisionWarnings:
    def test_caveats_propagated_as_warnings(self):
        comp = _comparison(
            assessment=PROMOTE,
            caveats=["Test caveat from evaluation."],
        )
        result = build_promotion_decision(
            comparison=comp, proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert any("caveat" in w.lower() or "Test caveat" in w for w in result["warnings"])

    def test_requires_rerun_produces_warning(self):
        comp = _comparison(
            assessment=PROMOTE,
            requires_rerun=["structuring.dte_min"],
        )
        result = build_promotion_decision(
            comparison=comp, proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert any("structural" in w.lower() or "rerun" in w.lower() or "re-execution" in w.lower()
                   for w in result["warnings"])

    def test_low_coverage_produces_warning(self):
        comp = _comparison(assessment=PROMOTE, coverage=0.30)
        result = build_promotion_decision(
            comparison=comp, proposals=[_param_proposal()],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert any("coverage" in w.lower() for w in result["warnings"])

    def test_high_overfit_appears_in_warnings(self):
        result = build_promotion_decision(
            comparison=_comparison(assessment=PROMOTE),
            proposals=[_param_proposal(overfit="HIGH")],
            overlay_path="/p", evaluation_path="/e", ts_utc=_TS,
        )
        assert any("overfit" in w.lower() for w in result["warnings"])


# ---------------------------------------------------------------------------
# forecast_arb.core.lineage
# ---------------------------------------------------------------------------


class TestLineage:
    def test_append_creates_file_and_parents(self, tmp_path):
        p = tmp_path / "sub" / "deep" / "lineage.jsonl"
        append_lineage_event(p, {"event_type": "EVALUATION_RUN", "ts_utc": _TS})
        assert p.exists()

    def test_append_produces_valid_jsonl(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        append_lineage_event(p, {"event_type": "EVALUATION_RUN", "ts_utc": _TS, "x": 1})
        lines = p.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["event_type"] == "EVALUATION_RUN"
        assert obj["x"] == 1

    def test_load_returns_all_events(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        for i in range(3):
            append_lineage_event(p, {"event_type": "EVALUATION_RUN", "ts_utc": _TS, "i": i})
        events = load_lineage(p)
        assert len(events) == 3
        assert [e["i"] for e in events] == [0, 1, 2]

    def test_load_returns_empty_if_missing(self, tmp_path):
        assert load_lineage(tmp_path / "nonexistent.jsonl") == []

    def test_load_skips_corrupt_lines(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        p.write_text('{"event_type": "EVALUATION_RUN", "ts_utc": "t1"}\nNOT JSON\n{"event_type": "PROMOTION_DECIDED", "ts_utc": "t2"}\n')
        events = load_lineage(p)
        assert len(events) == 2
        assert events[0]["event_type"] == "EVALUATION_RUN"
        assert events[1]["event_type"] == "PROMOTION_DECIDED"

    def test_find_by_overlay_filters(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        append_lineage_event(p, {"event_type": "EVALUATION_RUN", "ts_utc": "t1",
                                  "overlay_path": "/path/a.yaml"})
        append_lineage_event(p, {"event_type": "EVALUATION_RUN", "ts_utc": "t2",
                                  "overlay_path": "/path/b.yaml"})
        result = find_lineage_by_overlay(p, "/path/a.yaml")
        assert len(result) == 1
        assert result[0]["overlay_path"] == "/path/a.yaml"

    def test_find_by_period_filters(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        append_lineage_event(p, {"event_type": "PROPOSALS_NORMALIZED", "ts_utc": "t1",
                                  "source_period": {"since": "2024-03-01", "until": "2024-03-07"}})
        append_lineage_event(p, {"event_type": "PROPOSALS_NORMALIZED", "ts_utc": "t2",
                                  "source_period": {"since": "2024-03-08", "until": "2024-03-14"}})
        result = find_lineage_by_period(p, "2024-03-01", "2024-03-07")
        assert len(result) == 1
        assert result[0]["ts_utc"] == "t1"

    def test_get_latest_by_overlay_returns_most_recent(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        append_lineage_event(p, {"event_type": "EVALUATION_RUN",
                                  "ts_utc": "2024-03-01T10:00:00+00:00",
                                  "overlay_path": "/path/x.yaml"})
        append_lineage_event(p, {"event_type": "PROMOTION_DECIDED",
                                  "ts_utc": "2024-03-15T10:00:00+00:00",
                                  "overlay_path": "/path/x.yaml"})
        latest = get_latest_event_by_overlay(p, "/path/x.yaml")
        assert latest is not None
        assert latest["event_type"] == "PROMOTION_DECIDED"

    def test_get_latest_returns_none_if_no_match(self, tmp_path):
        p = tmp_path / "lineage.jsonl"
        append_lineage_event(p, {"event_type": "EVALUATION_RUN",
                                  "ts_utc": "t1", "overlay_path": "/other.yaml"})
        result = get_latest_event_by_overlay(p, "/nonexistent.yaml")
        assert result is None

    def test_get_latest_returns_none_if_file_missing(self, tmp_path):
        result = get_latest_event_by_overlay(tmp_path / "missing.jsonl", "/path.yaml")
        assert result is None


# ---------------------------------------------------------------------------
# scripts/review_overlay_promotion.run_promotion
# ---------------------------------------------------------------------------


def _make_proposals_store(tmp_path: Path, proposals: list, overlay_str: str = "") -> Path:
    from forecast_arb.ops.proposals import save_proposals
    path = tmp_path / "proposals.json"
    for p in proposals:
        if overlay_str:
            p["overlay_path"] = overlay_str
    container = {
        "schema_version": "1.0",
        "ts_created": _TS,
        "ts_updated": _TS,
        "proposals": proposals,
    }
    save_proposals(path, container)
    return path


def _make_eval_json(tmp_path: Path, assessment: str = PROMOTE) -> Path:
    p = tmp_path / "evaluation_comparison.json"
    with open(p, "w") as fh:
        json.dump(_comparison(assessment=assessment), fh)
    return p


def _make_overlay_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "overlay.yaml"
    with open(p, "w") as fh:
        yaml.dump({"edge_gating": {"min_edge": 0.07}}, fh)
    return p


class TestRunPromotion:
    def _import(self):
        import importlib
        scripts_dir = str(Path(__file__).parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import review_overlay_promotion as m
        return m

    def test_writes_json_and_md(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal()], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path)
        out_dir = tmp_path / "promotions"

        rc = m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
        )
        assert rc == 0
        assert (out_dir / "promotion_decision.json").exists()
        assert (out_dir / "promotion_decision.md").exists()

    def test_json_schema_valid(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal()], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path, assessment=PROMOTE)
        out_dir = tmp_path / "promotions"

        m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
        )

        with open(out_dir / "promotion_decision.json") as fh:
            d = json.load(fh)

        for field in ("decision", "confidence", "confidence_note", "warnings",
                      "proposal_ids", "source_kind_counts", "reasoning", "ts_utc",
                      "simulated_only"):
            assert field in d, f"missing: {field}"
        assert d["simulated_only"] is True

    def test_dry_run_no_files_written(self, tmp_path, capsys):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal()], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path)
        out_dir = tmp_path / "promotions"

        rc = m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
            dry_run=True,
        )
        assert rc == 0
        assert not out_dir.exists()
        captured = capsys.readouterr()
        assert "Overlay Promotion Decision" in captured.out

    def test_missing_evaluation_json_returns_1(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        proposals_path = _make_proposals_store(tmp_path, [_param_proposal()])
        rc = m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=tmp_path / "nonexistent.json",
            out_dir=tmp_path / "out",
        )
        assert rc == 1

    def test_no_matching_proposals_still_produces_decision(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        # proposals have no overlay_path set — no match expected
        proposals_path = _make_proposals_store(tmp_path, [_param_proposal()])
        eval_json = _make_eval_json(tmp_path)
        out_dir = tmp_path / "promotions"

        rc = m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
        )
        assert rc == 0
        with open(out_dir / "promotion_decision.json") as fh:
            d = json.load(fh)
        # Conservative — no proposals means empty list → confidence=0
        assert d["confidence"] == 0.0

    def test_md_contains_decision_string(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal(overfit="HIGH")], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path, assessment=PROMOTE)
        out_dir = tmp_path / "promotions"

        m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
        )
        md = (out_dir / "promotion_decision.md").read_text()
        assert DO_NOT_PROMOTE in md

    def test_md_has_blockers_section_when_do_not_promote(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal(overfit="HIGH")], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path, assessment=PROMOTE)
        out_dir = tmp_path / "promotions"

        m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
        )
        md = (out_dir / "promotion_decision.md").read_text()
        assert "Blockers to Promotion" in md

    def test_md_no_blockers_section_when_promoting(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal(overfit="LOW")], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path, assessment=PROMOTE)
        out_dir = tmp_path / "promotions"

        m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
        )
        md = (out_dir / "promotion_decision.md").read_text()
        assert "Blockers to Promotion" not in md

    def test_writes_lineage_event(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path)
        overlay_str = str(overlay.resolve())
        proposals_path = _make_proposals_store(
            tmp_path, [_param_proposal()], overlay_str=overlay_str
        )
        eval_json = _make_eval_json(tmp_path)
        out_dir = tmp_path / "promotions"
        lineage_path = tmp_path / "lineage.jsonl"

        m.run_promotion(
            proposals_path=proposals_path,
            overlay_path=overlay,
            evaluation_json=eval_json,
            out_dir=out_dir,
            lineage_path=lineage_path,
        )
        events = load_lineage(lineage_path)
        assert len(events) == 1
        assert events[0]["event_type"] == "PROMOTION_DECIDED"
        assert events[0]["overlay_path"] == overlay_str


# ---------------------------------------------------------------------------
# Lineage hooks in existing scripts
# ---------------------------------------------------------------------------


class TestEvaluateOverlayLineageHook:
    def test_appends_evaluation_run_event(self, tmp_path):
        import evaluate_parameter_overlay as m

        baseline = tmp_path / "baseline.yaml"
        overlay = tmp_path / "overlay.yaml"
        with open(baseline, "w") as fh:
            yaml.dump({"edge_gating": {"min_edge": 0.05}}, fh)
        with open(overlay, "w") as fh:
            yaml.dump({"edge_gating": {"min_edge": 0.07}}, fh)

        out_dir = tmp_path / "eval_out"
        lineage_path = tmp_path / "lineage.jsonl"

        with patch("evaluate_parameter_overlay.build_reflection_packet") as mock_brp:
            mock_brp.return_value = {"run_dirs_included": []}
            rc = m.run_evaluation(
                baseline_path=baseline,
                overlay_path=overlay,
                since="2024-01-01",
                until="2024-01-07",
                runs_dir=tmp_path,
                out_dir=out_dir,
                lineage_path=lineage_path,
            )

        assert rc == 0
        events = load_lineage(lineage_path)
        assert len(events) == 1
        assert events[0]["event_type"] == "EVALUATION_RUN"
        assert events[0]["source_period"] == {"since": "2024-01-01", "until": "2024-01-07"}

    def test_no_lineage_when_path_not_provided(self, tmp_path):
        import evaluate_parameter_overlay as m

        baseline = tmp_path / "baseline.yaml"
        overlay = tmp_path / "overlay.yaml"
        with open(baseline, "w") as fh:
            yaml.dump({"edge_gating": {"min_edge": 0.05}}, fh)
        with open(overlay, "w") as fh:
            yaml.dump({"edge_gating": {"min_edge": 0.07}}, fh)

        out_dir = tmp_path / "eval_out"

        with patch("evaluate_parameter_overlay.build_reflection_packet") as mock_brp:
            mock_brp.return_value = {"run_dirs_included": []}
            m.run_evaluation(
                baseline_path=baseline,
                overlay_path=overlay,
                since="2024-01-01",
                until="2024-01-07",
                runs_dir=tmp_path,
                out_dir=out_dir,
                # lineage_path not provided
            )
        # No lineage file written
        assert not (tmp_path / "lineage.jsonl").exists()


class TestMaterializeLineageHook:
    def test_appends_overlay_materialized_event(self, tmp_path):
        import materialize_parameter_overlay as m
        from forecast_arb.ops.proposals import normalize_proposals, save_proposals

        proposals_path = tmp_path / "proposals.json"
        out_dir = tmp_path / "overlays"
        lineage_path = tmp_path / "lineage.jsonl"

        report = {
            "parameter_suggestions": [{
                "parameter": "edge_gating.min_edge",
                "current_value": 0.05,
                "suggested_value": 0.07,
                "reasoning": "test",
                "expected_effect": "more passes",
                "overfit_risk": "LOW",
                "confidence": 0.75,
                "promotion_path": "APPROVED_FOR_REPLAY",
            }],
            "strategy_hypotheses": [],
            "optimization_opportunities": [],
        }
        proposals = normalize_proposals(
            report,
            source_period={"since": "2024-01-01", "until": "2024-01-07"},
        )
        container = {
            "schema_version": "1.0",
            "ts_created": _TS,
            "ts_updated": _TS,
            "proposals": proposals,
        }
        # Set status to APPROVED_FOR_REPLAY
        for p in container["proposals"]:
            if p.get("type") == "parameter":
                p["status"] = "APPROVED_FOR_REPLAY"
        save_proposals(proposals_path, container)

        rc = m._run(
            proposals_path=proposals_path,
            out_dir=out_dir,
            dry_run=False,
            lineage_path=lineage_path,
        )
        assert rc == 0
        events = load_lineage(lineage_path)
        assert len(events) == 1
        assert events[0]["event_type"] == "OVERLAY_MATERIALIZED"
        assert events[0]["overlay_path"] is not None

    def test_no_lineage_on_dry_run(self, tmp_path):
        import materialize_parameter_overlay as m
        from forecast_arb.ops.proposals import normalize_proposals, save_proposals

        proposals_path = tmp_path / "proposals.json"
        out_dir = tmp_path / "overlays"
        lineage_path = tmp_path / "lineage.jsonl"

        report = {
            "parameter_suggestions": [{
                "parameter": "edge_gating.min_edge",
                "current_value": 0.05,
                "suggested_value": 0.07,
                "reasoning": "test",
                "expected_effect": "more passes",
                "overfit_risk": "LOW",
                "confidence": 0.75,
                "promotion_path": "APPROVED_FOR_REPLAY",
            }],
            "strategy_hypotheses": [],
            "optimization_opportunities": [],
        }
        proposals = normalize_proposals(
            report,
            source_period={"since": "2024-01-01", "until": "2024-01-07"},
        )
        container = {
            "schema_version": "1.0",
            "ts_created": _TS, "ts_updated": _TS,
            "proposals": proposals,
        }
        for p in container["proposals"]:
            if p.get("type") == "parameter":
                p["status"] = "APPROVED_FOR_REPLAY"
        save_proposals(proposals_path, container)

        m._run(
            proposals_path=proposals_path,
            out_dir=out_dir,
            dry_run=True,
            lineage_path=lineage_path,
        )
        # dry_run returns before writing → lineage event should NOT be appended
        assert not lineage_path.exists()
