"""
tests/test_patch7_evaluation.py
================================
Unit and integration tests for Patch 7 — overlay evaluation harness.

Coverage
--------
- flatten_config / deep_merge_configs
- classify_overlay_keys (4 buckets)
- apply_threshold_gate (all outcome paths)
- _collect_run_dirs
- compute_evaluation_metrics (coverage fields)
- compute_comparison (assessment paths, coverage downgrade)
- build_evaluation_report (success + error path)
- evaluate_parameter_overlay.run_evaluation (dry-run + file output + proposals update)
- _render_comparison_md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.ops.evaluation import (
    SIMULATION_DISCLAIMER,
    _collect_run_dirs,
    apply_threshold_gate,
    build_evaluation_report,
    classify_overlay_keys,
    compute_comparison,
    compute_evaluation_metrics,
    deep_merge_configs,
    flatten_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_cfg() -> dict:
    return {
        "regime_selector": {"crash_p_threshold": 0.015, "selloff_p_min": 0.05},
        "edge_gating": {"min_edge": 0.05, "min_confidence": 0.60},
        "min_debit_per_contract": 30.0,
        "structuring": {"dte_range_days": {"min": 30, "max": 60}},
    }


def _full_signals(
    p_implied=0.005,
    edge=0.08,
    confidence=0.72,
    gate_present=True,
    candidates_present=True,
    min_debit=35.0,
    decision="TRADE",
) -> dict:
    return {
        "run_id": "test_run",
        "decision": decision,
        "reason": "",
        "p_implied": p_implied,
        "p_external": 0.008,
        "edge": edge,
        "confidence": confidence,
        "num_tickets": 1,
        "gate_artifact_present": gate_present,
        "candidates_artifact_present": candidates_present,
        "min_debit_in_candidates": min_debit,
    }


# ---------------------------------------------------------------------------
# flatten_config
# ---------------------------------------------------------------------------


class TestFlattenConfig:
    def test_flat_passthrough(self):
        cfg = {"a": 1, "b": 2}
        assert flatten_config(cfg) == {"a": 1, "b": 2}

    def test_nested(self):
        cfg = {"x": {"y": {"z": 3}}}
        assert flatten_config(cfg) == {"x.y.z": 3}

    def test_mixed(self):
        cfg = {"a": 1, "b": {"c": 2}}
        result = flatten_config(cfg)
        assert result == {"a": 1, "b.c": 2}

    def test_empty(self):
        assert flatten_config({}) == {}


# ---------------------------------------------------------------------------
# deep_merge_configs
# ---------------------------------------------------------------------------


class TestDeepMergeConfigs:
    def test_overlay_wins(self):
        base = {"a": 1, "b": 2}
        over = {"b": 99}
        merged = deep_merge_configs(base, over)
        assert merged["b"] == 99
        assert merged["a"] == 1

    def test_baseline_keys_preserved(self):
        base = {"a": 1, "extra": "keep"}
        over = {"a": 2}
        merged = deep_merge_configs(base, over)
        assert merged["extra"] == "keep"

    def test_neither_input_mutated(self):
        base = {"a": 1}
        over = {"a": 2}
        deep_merge_configs(base, over)
        assert base["a"] == 1
        assert over["a"] == 2

    def test_dotted_overlay_key(self):
        base = {"edge_gating": {"min_edge": 0.05, "min_confidence": 0.60}}
        over = {"edge_gating.min_edge": 0.08}
        merged = deep_merge_configs(base, over)
        assert merged["edge_gating"]["min_edge"] == 0.08
        assert merged["edge_gating"]["min_confidence"] == 0.60

    def test_nested_overlay_dict(self):
        base = {"regime_selector": {"crash_p_threshold": 0.015}}
        over = {"regime_selector": {"crash_p_threshold": 0.020}}
        merged = deep_merge_configs(base, over)
        assert merged["regime_selector"]["crash_p_threshold"] == 0.020


# ---------------------------------------------------------------------------
# classify_overlay_keys
# ---------------------------------------------------------------------------


class TestClassifyOverlayKeys:
    def test_fully_evaluable(self):
        overlay = {"regime_selector.crash_p_threshold": 0.02, "edge_gating.min_edge": 0.06}
        result = classify_overlay_keys(overlay)
        assert "regime_selector.crash_p_threshold" in result["fully_evaluable"]
        assert "edge_gating.min_edge" in result["fully_evaluable"]
        assert result["partially_evaluable"] == []
        assert result["requires_rerun"] == []
        assert result["unknown"] == []

    def test_partially_evaluable(self):
        overlay = {"min_debit_per_contract": 35.0}
        result = classify_overlay_keys(overlay)
        assert "min_debit_per_contract" in result["partially_evaluable"]

    def test_requires_rerun(self):
        overlay = {"structuring.dte_min": 25, "regimes.selloff.enabled": True}
        result = classify_overlay_keys(overlay)
        assert any(k.startswith("structuring.") for k in result["requires_rerun"])
        assert any(k.startswith("regimes.") for k in result["requires_rerun"])

    def test_unknown_key(self):
        overlay = {"some_random_new_key": 42}
        result = classify_overlay_keys(overlay)
        assert "some_random_new_key" in result["unknown"]

    def test_mixed_overlay(self):
        overlay = {
            "edge_gating.min_edge": 0.07,
            "min_debit_per_contract": 32.0,
            "structuring.dte_min": 20,
            "mystery_key": 99,
        }
        result = classify_overlay_keys(overlay)
        assert len(result["fully_evaluable"]) == 1
        assert len(result["partially_evaluable"]) == 1
        assert len(result["requires_rerun"]) == 1
        assert len(result["unknown"]) == 1

    def test_empty_overlay(self):
        result = classify_overlay_keys({})
        for bucket in ("fully_evaluable", "partially_evaluable", "requires_rerun", "unknown"):
            assert result[bucket] == []


# ---------------------------------------------------------------------------
# apply_threshold_gate
# ---------------------------------------------------------------------------


class TestApplyThresholdGate:
    def test_pass_all(self):
        cfg = _base_cfg()
        sig = _full_signals()
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "PASS"
        assert result["simulated"] is True

    def test_fail_edge(self):
        cfg = {"edge_gating": {"min_edge": 0.10, "min_confidence": 0.60}}
        sig = _full_signals(edge=0.03)
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "FAIL_EDGE"
        assert result["simulated"] is True
        assert "FAIL_EDGE" in result["gate_reasons"]

    def test_fail_confidence(self):
        cfg = {"edge_gating": {"min_edge": 0.05, "min_confidence": 0.80}}
        sig = _full_signals(confidence=0.55)
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "FAIL_CONFIDENCE"
        assert result["simulated"] is True

    def test_fail_crash_threshold(self):
        cfg = {"regime_selector": {"crash_p_threshold": 0.010}}
        sig = _full_signals(p_implied=0.020)
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "FAIL_CRASH_THRESHOLD"
        assert result["simulated"] is True

    def test_fail_debit(self):
        cfg = {"min_debit_per_contract": 50.0}
        sig = _full_signals(min_debit=25.0)
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "FAIL_DEBIT"
        assert result["simulated"] is True

    def test_no_signals(self):
        cfg = _base_cfg()
        sig = {
            "p_implied": None, "edge": None, "confidence": None,
            "gate_artifact_present": False, "candidates_artifact_present": False,
            "min_debit_in_candidates": None,
        }
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "NO_SIGNALS"
        assert result["simulated"] is False

    def test_partial_signals_no_gate_artifact(self):
        # edge/confidence keys present in config but gate_decision.json absent
        cfg = {"edge_gating": {"min_edge": 0.05, "min_confidence": 0.60}}
        sig = _full_signals(gate_present=False)
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "PARTIAL_SIGNALS"
        assert result["simulated"] is False

    def test_partial_signals_missing_edge_in_full_simulation(self):
        # Gate present but edge signal None
        cfg = {"edge_gating": {"min_edge": 0.05, "min_confidence": 0.60}}
        sig = _full_signals(edge=None)
        result = apply_threshold_gate(sig, cfg)
        assert result["gate_outcome"] == "PARTIAL_SIGNALS"
        assert result["simulated"] is False

    def test_debit_skipped_when_no_candidates(self):
        # min_debit overlay but no candidates generated — should not fail
        cfg = {"min_debit_per_contract": 50.0, "edge_gating": {"min_edge": 0.05, "min_confidence": 0.60}}
        sig = _full_signals(candidates_present=False, min_debit=None)
        result = apply_threshold_gate(sig, cfg)
        # No debit check possible, but edge/confidence pass
        assert result["gate_outcome"] == "PASS"


# ---------------------------------------------------------------------------
# compute_evaluation_metrics
# ---------------------------------------------------------------------------


class TestComputeEvaluationMetrics:
    def test_empty_returns_defaults(self):
        result = compute_evaluation_metrics([], {})
        assert result["runs_total"] == 0
        assert result["coverage_rate"] == 0.0
        assert result["simulated_only"] is True

    def test_coverage_rate(self):
        cfg = {"edge_gating": {"min_edge": 0.05, "min_confidence": 0.60}}
        fully_simulated = _full_signals()  # gate present, all signals → simulated=True
        no_sig = {
            "p_implied": None, "edge": None, "confidence": None,
            "gate_artifact_present": False, "candidates_artifact_present": False,
            "min_debit_in_candidates": None,
        }
        result = compute_evaluation_metrics([fully_simulated, no_sig], cfg)
        assert result["runs_total"] == 2
        assert result["runs_fully_simulated"] == 1
        assert result["runs_without_signals"] == 1
        assert result["coverage_rate"] == pytest.approx(0.5)

    def test_gate_pass_rate_only_over_fully_simulated(self):
        cfg = {"edge_gating": {"min_edge": 0.05, "min_confidence": 0.60}}
        passing = _full_signals()
        failing = _full_signals(edge=0.01)  # FAIL_EDGE
        no_sig = {"p_implied": None, "edge": None, "confidence": None,
                  "gate_artifact_present": False, "candidates_artifact_present": False,
                  "min_debit_in_candidates": None}
        result = compute_evaluation_metrics([passing, failing, no_sig], cfg)
        assert result["runs_fully_simulated"] == 2
        assert result["gate_pass_rate"] == pytest.approx(0.5)

    def test_simulated_only_always_true(self):
        result = compute_evaluation_metrics([_full_signals()], {})
        assert result["simulated_only"] is True


# ---------------------------------------------------------------------------
# compute_comparison
# ---------------------------------------------------------------------------


class TestComputeComparison:
    def _classification(self, fully=None):
        return {
            "fully_evaluable": fully or ["edge_gating.min_edge"],
            "partially_evaluable": [],
            "requires_rerun": [],
            "unknown": [],
        }

    def test_always_has_disclaimer(self):
        result = compute_comparison({}, {}, self._classification())
        assert result["simulated_only"] is True
        assert result["simulation_disclaimer"] == SIMULATION_DISCLAIMER

    def test_no_change_small_delta(self):
        b = {"gate_pass_rate": 0.50, "no_trade_rate": 0.3, "runs_total": 10, "coverage_rate": 0.8}
        o = {"gate_pass_rate": 0.51, "no_trade_rate": 0.3, "runs_total": 10, "coverage_rate": 0.8}
        result = compute_comparison(b, o, self._classification())
        assert result["assessment"] == "NO_CHANGE"

    def test_keep_testing_low_coverage(self):
        # Delta is large but coverage is < 40%
        b = {"gate_pass_rate": 0.30, "no_trade_rate": 0.5, "runs_total": 10, "coverage_rate": 0.3}
        o = {"gate_pass_rate": 0.60, "no_trade_rate": 0.5, "runs_total": 10, "coverage_rate": 0.3}
        result = compute_comparison(b, o, self._classification())
        assert result["assessment"] == "KEEP_TESTING"
        # Should note coverage issue in caveats
        assert any("coverage" in c.lower() for c in result["assessment_caveats"])

    def test_keep_testing_insufficient_runs(self):
        # n=3 below _MIN_RUNS_FOR_PROMOTE=5
        b = {"gate_pass_rate": 0.20, "no_trade_rate": 0.5, "runs_total": 3, "coverage_rate": 0.9}
        o = {"gate_pass_rate": 0.50, "no_trade_rate": 0.5, "runs_total": 3, "coverage_rate": 0.9}
        result = compute_comparison(b, o, self._classification())
        assert result["assessment"] == "KEEP_TESTING"

    def test_promote_to_paper_review(self):
        b = {"gate_pass_rate": 0.30, "no_trade_rate": 0.5, "runs_total": 10, "coverage_rate": 0.8}
        o = {"gate_pass_rate": 0.55, "no_trade_rate": 0.5, "runs_total": 10, "coverage_rate": 0.8}
        result = compute_comparison(b, o, self._classification())
        assert result["assessment"] == "PROMOTE_TO_PAPER_REVIEW"

    def test_requires_rerun_adds_caveat(self):
        classification = {
            "fully_evaluable": ["edge_gating.min_edge"],
            "partially_evaluable": [],
            "requires_rerun": ["structuring.dte_min"],
            "unknown": [],
        }
        b = {"gate_pass_rate": 0.50, "no_trade_rate": 0.3, "runs_total": 10, "coverage_rate": 0.8}
        o = {"gate_pass_rate": 0.51, "no_trade_rate": 0.3, "runs_total": 10, "coverage_rate": 0.8}
        result = compute_comparison(b, o, classification)
        assert any("structural" in c.lower() or "rerun" in c.lower() or "not simulated" in c.lower()
                   for c in result["assessment_caveats"])

    def test_no_evaluable_params(self):
        classification = {
            "fully_evaluable": [],
            "partially_evaluable": [],
            "requires_rerun": ["structuring.dte_min"],
            "unknown": [],
        }
        b = {"gate_pass_rate": None, "no_trade_rate": None, "runs_total": 0, "coverage_rate": 0.0}
        o = {"gate_pass_rate": None, "no_trade_rate": None, "runs_total": 0, "coverage_rate": 0.0}
        result = compute_comparison(b, o, classification)
        assert result["assessment"] == "KEEP_TESTING"


# ---------------------------------------------------------------------------
# build_evaluation_report
# ---------------------------------------------------------------------------


class TestBuildEvaluationReport:
    def test_success_structure(self, tmp_path):
        report = build_evaluation_report(
            baseline_config=_base_cfg(),
            overlay_config=_base_cfg(),
            run_dirs=[],
            period={"since": "2024-01-01", "until": "2024-01-07"},
            ts_utc="2024-01-07T12:00:00+00:00",
        )
        assert report["simulated_only"] is True
        assert report["simulation_disclaimer"] == SIMULATION_DISCLAIMER
        assert "overlay_classification" in report
        assert "baseline" in report
        assert "overlay" in report
        assert "comparison" in report

    def test_overlay_classification_always_present(self, tmp_path):
        # Even when overlay == baseline (no change), classification should be present
        report = build_evaluation_report(
            baseline_config=_base_cfg(),
            overlay_config=_base_cfg(),
            run_dirs=[],
            period={"since": "2024-01-01", "until": "2024-01-07"},
            ts_utc="now",
        )
        cls = report["overlay_classification"]
        for bucket in ("fully_evaluable", "partially_evaluable", "requires_rerun", "unknown"):
            assert bucket in cls

    def test_error_path_still_returns_valid_schema(self):
        # Force an error by passing a non-dict config
        report = build_evaluation_report(
            baseline_config=None,   # will cause _recover_overlay to fail
            overlay_config=None,
            run_dirs=[],
            period={"since": "2024-01-01", "until": "2024-01-07"},
            ts_utc="now",
        )
        # Must still return a valid dict with required fields
        assert report["simulated_only"] is True
        assert "comparison" in report
        # Classification defaults to empty buckets on error
        assert isinstance(report["overlay_classification"], dict)


# ---------------------------------------------------------------------------
# _collect_run_dirs
# ---------------------------------------------------------------------------


class TestCollectRunDirs:
    def test_collects_matching_names(self, tmp_path):
        campaign = tmp_path / "campaign_v1"
        campaign.mkdir()
        run_a = campaign / "run_20240101"
        run_b = campaign / "run_20240102"
        run_a.mkdir()
        run_b.mkdir()

        result = _collect_run_dirs(tmp_path, ["run_20240101"])
        assert len(result) == 1
        assert result[0].name == "run_20240101"

    def test_skips_non_campaign_dirs(self, tmp_path):
        for skip_name in ("allocator", "weekly", "proposals"):
            d = tmp_path / skip_name
            d.mkdir()
            run = d / "run_xyz"
            run.mkdir()

        result = _collect_run_dirs(tmp_path, ["run_xyz"])
        assert result == []

    def test_empty_names_list(self, tmp_path):
        result = _collect_run_dirs(tmp_path, [])
        assert result == []

    def test_nonexistent_root(self, tmp_path):
        result = _collect_run_dirs(tmp_path / "nonexistent", ["anything"])
        assert result == []


# ---------------------------------------------------------------------------
# run_evaluation (integration — dry-run + file output)
# ---------------------------------------------------------------------------


def _make_overlay_yaml(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "test_overlay.yaml"
    with open(p, "w", encoding="utf-8") as fh:
        yaml.dump(content, fh)
    return p


def _make_baseline_yaml(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "baseline.yaml"
    with open(p, "w", encoding="utf-8") as fh:
        yaml.dump(content, fh)
    return p


class TestRunEvaluation:
    def _import(self):
        import importlib, sys
        # ensure fresh import
        mod_name = "evaluate_parameter_overlay"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        scripts_dir = str(Path(__file__).parent.parent / "scripts")
        sys.path.insert(0, scripts_dir)
        import evaluate_parameter_overlay as m
        return m

    def test_dry_run_prints_markdown(self, tmp_path, capsys):
        m = self._import()
        baseline = _make_baseline_yaml(tmp_path, _base_cfg())
        overlay = _make_overlay_yaml(tmp_path, {"edge_gating.min_edge": 0.07})
        out_dir = tmp_path / "eval_out"

        with patch("evaluate_parameter_overlay.build_reflection_packet") as mock_brp:
            mock_brp.return_value = {"run_dirs_included": []}
            rc = m.run_evaluation(
                baseline_path=baseline,
                overlay_path=overlay,
                since="2024-01-01",
                until="2024-01-07",
                runs_dir=tmp_path,
                out_dir=out_dir,
                dry_run=True,
            )

        assert rc == 0
        captured = capsys.readouterr()
        assert "Counterfactual" in captured.out
        assert not out_dir.exists()

    def test_writes_four_artifacts(self, tmp_path):
        m = self._import()
        baseline = _make_baseline_yaml(tmp_path, _base_cfg())
        overlay = _make_overlay_yaml(tmp_path, {"edge_gating.min_edge": 0.07})
        out_dir = tmp_path / "eval_out"

        with patch("evaluate_parameter_overlay.build_reflection_packet") as mock_brp:
            mock_brp.return_value = {"run_dirs_included": []}
            rc = m.run_evaluation(
                baseline_path=baseline,
                overlay_path=overlay,
                since="2024-01-01",
                until="2024-01-07",
                runs_dir=tmp_path,
                out_dir=out_dir,
                dry_run=False,
            )

        assert rc == 0
        assert (out_dir / "evaluation_baseline.json").exists()
        assert (out_dir / "evaluation_overlay.json").exists()
        assert (out_dir / "evaluation_comparison.json").exists()
        assert (out_dir / "evaluation_comparison.md").exists()

    def test_comparison_json_has_required_fields(self, tmp_path):
        m = self._import()
        baseline = _make_baseline_yaml(tmp_path, _base_cfg())
        overlay = _make_overlay_yaml(tmp_path, {"edge_gating.min_edge": 0.07})
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
            )

        with open(out_dir / "evaluation_comparison.json", encoding="utf-8") as fh:
            comp = json.load(fh)

        assert comp["simulated_only"] is True
        assert "assessment" in comp
        assert "delta" in comp

    def test_missing_baseline_returns_1(self, tmp_path):
        m = self._import()
        overlay = _make_overlay_yaml(tmp_path, {})
        rc = m.run_evaluation(
            baseline_path=tmp_path / "nonexistent.yaml",
            overlay_path=overlay,
            since="2024-01-01",
            until="2024-01-07",
            runs_dir=tmp_path,
            out_dir=tmp_path / "out",
        )
        assert rc == 1

    def test_missing_overlay_returns_1(self, tmp_path):
        m = self._import()
        baseline = _make_baseline_yaml(tmp_path, _base_cfg())
        rc = m.run_evaluation(
            baseline_path=baseline,
            overlay_path=tmp_path / "nonexistent.yaml",
            since="2024-01-01",
            until="2024-01-07",
            runs_dir=tmp_path,
            out_dir=tmp_path / "out",
        )
        assert rc == 1

    def test_proposals_updated_when_provided(self, tmp_path):
        from forecast_arb.ops.proposals import load_proposals, normalize_proposals, save_proposals

        m = self._import()
        baseline = _make_baseline_yaml(tmp_path, _base_cfg())
        overlay_path = _make_overlay_yaml(tmp_path, {"edge_gating.min_edge": 0.07})
        out_dir = tmp_path / "eval_out"
        proposals_path = tmp_path / "proposals.json"

        # Build a proposal with overlay_path set
        proposals_dir = tmp_path
        report = {
            "parameter_suggestions": [{
                "parameter": "edge_gating.min_edge",
                "current_value": 0.05,
                "suggested_value": 0.07,
                "reasoning": "test",
                "expected_effect": "more passes",
                "overfit_risk": "low",
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
            "ts_created": "2024-01-07T00:00:00+00:00",
            "ts_updated": "2024-01-07T00:00:00+00:00",
            "proposals": proposals,
        }
        # Set overlay_path on the proposal
        container["proposals"][0]["overlay_path"] = str(overlay_path.resolve())
        save_proposals(proposals_path, container)

        with patch("evaluate_parameter_overlay.build_reflection_packet") as mock_brp:
            mock_brp.return_value = {"run_dirs_included": []}
            m.run_evaluation(
                baseline_path=baseline,
                overlay_path=overlay_path,
                since="2024-01-01",
                until="2024-01-07",
                runs_dir=tmp_path,
                out_dir=out_dir,
                proposals_path=proposals_path,
            )

        updated = load_proposals(proposals_path)
        assert updated["proposals"][0].get("evaluation_path") == str(out_dir.resolve())


# ---------------------------------------------------------------------------
# _render_comparison_md
# ---------------------------------------------------------------------------


class TestRenderComparisonMd:
    def _import(self):
        scripts_dir = str(Path(__file__).parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import evaluate_parameter_overlay as m
        return m

    def test_contains_disclaimer(self):
        m = self._import()
        comparison = {
            "simulated_only": True,
            "assessment": "NO_CHANGE",
            "assessment_rationale": "Tiny delta.",
            "assessment_caveats": [],
            "fully_evaluable_parameters": ["edge_gating.min_edge"],
            "partially_evaluable_parameters": [],
            "requires_rerun_parameters": [],
            "unknown_parameters": [],
            "delta": {"gate_pass_rate": 0.01, "no_trade_rate": None, "runs_total": 5, "coverage_rate": 0.8},
        }
        md = m._render_comparison_md(
            comparison=comparison,
            baseline_metrics={"gate_pass_rate": 0.50, "no_trade_rate": 0.3,
                               "runs_total": 5, "coverage_rate": 0.8,
                               "runs_fully_simulated": 4, "runs_partial_signals": 1,
                               "runs_without_signals": 0, "gate_fail_reasons": {}},
            overlay_metrics={"gate_pass_rate": 0.51, "no_trade_rate": 0.3,
                              "runs_total": 5, "coverage_rate": 0.8,
                              "runs_fully_simulated": 4, "runs_partial_signals": 1,
                              "runs_without_signals": 0, "gate_fail_reasons": {}},
            period={"since": "2024-01-01", "until": "2024-01-07"},
            baseline_path=Path("configs/baseline.yaml"),
            overlay_path=Path("configs/overlay.yaml"),
            ts_utc="2024-01-07T12:00:00+00:00",
        )
        assert "COUNTERFACTUAL_ONLY" in md
        assert "NO_CHANGE" in md
        assert "edge_gating.min_edge" in md

    def test_caveats_rendered(self):
        m = self._import()
        comparison = {
            "simulated_only": True,
            "assessment": "KEEP_TESTING",
            "assessment_rationale": "Low coverage.",
            "assessment_caveats": ["Coverage 30% below minimum 40%."],
            "fully_evaluable_parameters": [],
            "partially_evaluable_parameters": [],
            "requires_rerun_parameters": [],
            "unknown_parameters": [],
            "delta": {"gate_pass_rate": None, "no_trade_rate": None, "runs_total": 3, "coverage_rate": 0.3},
        }
        md = m._render_comparison_md(
            comparison=comparison,
            baseline_metrics={"gate_pass_rate": None, "no_trade_rate": None,
                               "runs_total": 3, "coverage_rate": 0.3,
                               "runs_fully_simulated": 1, "runs_partial_signals": 1,
                               "runs_without_signals": 1, "gate_fail_reasons": {}},
            overlay_metrics={"gate_pass_rate": None, "no_trade_rate": None,
                              "runs_total": 3, "coverage_rate": 0.3,
                              "runs_fully_simulated": 1, "runs_partial_signals": 1,
                              "runs_without_signals": 1, "gate_fail_reasons": {}},
            period={"since": "2024-01-01", "until": "2024-01-07"},
            baseline_path=Path("configs/baseline.yaml"),
            overlay_path=Path("configs/overlay.yaml"),
            ts_utc="2024-01-07T12:00:00+00:00",
        )
        assert "Coverage 30%" in md
        assert "Caveats" in md
