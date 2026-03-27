"""
tests/test_patch_c_provenance.py
==================================
Patch C provenance wiring tests.

Covers:
  1. EVIDENCE_ROLE table completeness and is_authoritative_capable()
  2. GateDecision evidence fields present and stable in to_dict()
  3. gate() propagates evidence_class + authoritative_capable from PEventResult
  4. p_external_used_for_gating logic in decision_packet
  5. ops/summary.py surfaces non-authoritative warning
  6. _compute_evidence_class_stats in reflection_packet
  7. run_summary Patch C fields read from artifact
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from forecast_arb.oracle.evidence import (
    EVIDENCE_ROLE,
    EvidenceClass,
    is_authoritative_capable,
)
from forecast_arb.gating.edge_gate import GateDecision, gate


# ---------------------------------------------------------------------------
# 1. EVIDENCE_ROLE table + is_authoritative_capable
# ---------------------------------------------------------------------------


class TestEvidenceRoleTable:
    def test_all_classes_present(self):
        for ec in EvidenceClass:
            assert ec in EVIDENCE_ROLE, f"{ec} missing from EVIDENCE_ROLE"

    def test_only_exact_terminal_is_authoritative_capable(self):
        assert is_authoritative_capable(EvidenceClass.EXACT_TERMINAL) is True
        for ec in EvidenceClass:
            if ec != EvidenceClass.EXACT_TERMINAL:
                assert is_authoritative_capable(ec) is False, f"{ec} should not be auth-capable"

    def test_none_returns_false(self):
        assert is_authoritative_capable(None) is False

    def test_role_strings_are_valid(self):
        valid = {"AUTHORITATIVE_CAPABLE", "INFORMATIVE_ONLY", "CONTEXT_ONLY", "DIAGNOSTIC_ONLY"}
        for ec, role in EVIDENCE_ROLE.items():
            assert role in valid, f"Unknown role {role!r} for {ec}"

    def test_exact_terminal_role_string(self):
        assert EVIDENCE_ROLE[EvidenceClass.EXACT_TERMINAL] == "AUTHORITATIVE_CAPABLE"

    def test_unusable_is_diagnostic(self):
        assert EVIDENCE_ROLE[EvidenceClass.UNUSABLE] == "DIAGNOSTIC_ONLY"

    def test_coarse_regime_is_context(self):
        assert EVIDENCE_ROLE[EvidenceClass.COARSE_REGIME] == "CONTEXT_ONLY"

    def test_informative_classes(self):
        for ec in (EvidenceClass.NEARBY_TERMINAL, EvidenceClass.PATHWISE_PROXY):
            assert EVIDENCE_ROLE[ec] == "INFORMATIVE_ONLY"


# ---------------------------------------------------------------------------
# 2. GateDecision evidence fields in to_dict()
# ---------------------------------------------------------------------------


class TestGateDecisionPatchCFields:
    def _make_gd(self, **kwargs) -> GateDecision:
        defaults = dict(
            decision="PASS",
            reason="PASSED_GATES",
            edge=0.10,
            p_external=0.65,
            p_implied=0.55,
            confidence=0.70,
            confidence_external=0.70,
            confidence_implied=0.72,
        )
        defaults.update(kwargs)
        return GateDecision(**defaults)

    def test_default_fields_present_in_to_dict(self):
        gd = self._make_gd()
        d = gd.to_dict()
        assert "evidence_class" in d
        assert "p_external_authoritative_capable" in d

    def test_default_none_false(self):
        gd = self._make_gd()
        d = gd.to_dict()
        assert d["evidence_class"] is None
        assert d["p_external_authoritative_capable"] is False

    def test_fields_emitted_even_when_none(self):
        """Stable artifact shape — fields always present, never KeyError."""
        gd = self._make_gd(evidence_class=None, p_external_authoritative_capable=False)
        d = gd.to_dict()
        assert "evidence_class" in d
        assert "p_external_authoritative_capable" in d

    def test_evidence_class_value_propagated(self):
        gd = self._make_gd(evidence_class="EXACT_TERMINAL", p_external_authoritative_capable=True)
        d = gd.to_dict()
        assert d["evidence_class"] == "EXACT_TERMINAL"
        assert d["p_external_authoritative_capable"] is True

    def test_no_trade_decision_has_fields(self):
        gd = self._make_gd(
            decision="NO_TRADE", reason="NO_P_EXTERNAL",
            edge=None, p_external=None, confidence=0.0,
            confidence_external=0.0, confidence_implied=None,
        )
        d = gd.to_dict()
        assert "evidence_class" in d
        assert "p_external_authoritative_capable" in d


# ---------------------------------------------------------------------------
# 3. gate() propagates evidence fields from PEventResult
# ---------------------------------------------------------------------------


def _make_p_event(p_event=0.60, confidence=0.75, evidence_class=None):
    obj = MagicMock()
    obj.p_event = p_event
    obj.confidence = confidence
    obj.metadata = {}
    obj.evidence_class = evidence_class
    return obj


class TestGatePatchCPropagation:
    def test_no_evidence_class_produces_none(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=None)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["evidence_class"] is None
        assert d["p_external_authoritative_capable"] is False

    def test_exact_terminal_propagates_auth_capable(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.EXACT_TERMINAL)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["evidence_class"] == "EXACT_TERMINAL"
        assert d["p_external_authoritative_capable"] is True

    def test_nearby_terminal_not_auth_capable(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.NEARBY_TERMINAL)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["evidence_class"] == "NEARBY_TERMINAL"
        assert d["p_external_authoritative_capable"] is False

    def test_coarse_regime_not_auth_capable(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.COARSE_REGIME)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["evidence_class"] == "COARSE_REGIME"
        assert d["p_external_authoritative_capable"] is False

    def test_no_trade_no_p_implied_still_has_fields(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.PATHWISE_PROXY)
        p_imp = _make_p_event(p_event=None)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["decision"] == "NO_TRADE"
        assert "evidence_class" in d
        assert d["evidence_class"] == "PATHWISE_PROXY"

    def test_gate_does_not_change_decision_logic(self):
        """Patch C fields must not alter pass/fail outcome."""
        p_ext_auth = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.EXACT_TERMINAL)
        p_ext_diag = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.UNUSABLE)
        p_imp = _make_p_event(p_event=0.50)
        gd_auth = gate(p_ext_auth, p_imp)
        gd_diag = gate(p_ext_diag, p_imp)
        # Same edge → same decision
        assert gd_auth.decision == gd_diag.decision
        assert gd_auth.reason == gd_diag.reason


# ---------------------------------------------------------------------------
# 4. p_external_used_for_gating in decision_packet
# ---------------------------------------------------------------------------


class TestPExternalUsedForGating:
    """Tests for _p_external_used_for_gating() logic in decision_packet."""

    def _call(self, gate_dict):
        from forecast_arb.core.decision_packet import _p_external_used_for_gating
        return _p_external_used_for_gating(gate_dict)

    def test_none_gate_dict_returns_false(self):
        assert self._call(None) is False

    def test_empty_gate_dict_returns_false(self):
        assert self._call({}) is False

    def test_none_p_external_returns_false(self):
        assert self._call({"p_external": None}) is False

    def test_float_p_external_returns_true(self):
        assert self._call({"p_external": 0.65}) is True

    def test_zero_p_external_returns_true(self):
        # 0.0 is a valid (non-None) value — gate was consulted
        assert self._call({"p_external": 0.0}) is True

    def test_extra_fields_do_not_affect_result(self):
        assert self._call({"p_external": 0.50, "decision": "PASS", "edge": 0.10}) is True


# ---------------------------------------------------------------------------
# 5. ops/summary.py Patch C rendering
# ---------------------------------------------------------------------------


class TestOperatorSummaryPatchC:
    def _base_packet(self, **overrides) -> Dict[str, Any]:
        signals = {
            "p_external": 0.60,
            "p_implied": 0.50,
            "edge": 0.10,
            "confidence": 0.70,
            "gate_decision": "PASS",
            "p_evidence_class": "EXACT_TERMINAL",
            "p_external_authoritative_capable": True,
            "p_external_used_for_gating": True,
            "p_external_role": "AUTHORITATIVE_CAPABLE",
            "p_baseline_source": "options_implied",
            "p_external_semantic_notes": [],
        }
        signals.update(overrides.pop("signals", {}))
        packet = {
            "schema_version": "2.0",
            "ts_utc": "2026-03-27T00:00:00+00:00",
            "run": {
                "run_id": "test-run",
                "timestamp": "2026-03-27T00:00:00+00:00",
                "mode": "review_only",
                "decision": "NO_TRADE",
                "reason": "EDGE_TOO_SMALL",
                "num_tickets": 0,
                "submit_requested": False,
                "submit_executed": False,
            },
            "broker_preflight": None,
            "top_candidates": [],
            "signals": signals,
            "notes": [],
        }
        packet.update(overrides)
        return packet

    def test_renders_without_error_auth_capable(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        assert "evidence_role" in md
        assert "auth_capable" in md
        assert "ext_used_for_gating" in md
        assert "baseline_source" in md

    def test_no_warning_when_auth_capable(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        assert "non-authoritative" not in md

    def test_warning_when_not_auth_capable(self):
        from forecast_arb.ops.summary import render_operator_summary
        overrides = {"signals": {
            "p_external_authoritative_capable": False,
            "p_external_role": "INFORMATIVE_ONLY",
            "p_evidence_class": "NEARBY_TERMINAL",
            "p_external_semantic_notes": ["nearby terminal; informative only"],
        }}
        md = render_operator_summary(self._base_packet(**overrides))
        assert "non-authoritative" in md
        assert "INFORMATIVE_ONLY" in md

    def test_warning_shows_first_semantic_note(self):
        from forecast_arb.ops.summary import render_operator_summary
        overrides = {"signals": {
            "p_external_authoritative_capable": False,
            "p_external_role": "CONTEXT_ONLY",
            "p_evidence_class": "COARSE_REGIME",
            "p_external_semantic_notes": ["first note", "second note"],
        }}
        md = render_operator_summary(self._base_packet(**overrides))
        assert "first note" in md
        assert "second note" not in md

    def test_warning_uses_evidence_class_when_no_notes(self):
        from forecast_arb.ops.summary import render_operator_summary
        overrides = {"signals": {
            "p_external_authoritative_capable": False,
            "p_external_role": "DIAGNOSTIC_ONLY",
            "p_evidence_class": "UNUSABLE",
            "p_external_semantic_notes": [],
        }}
        md = render_operator_summary(self._base_packet(**overrides))
        assert "evidence_class=UNUSABLE" in md

    def test_schema_mismatch_raises(self):
        from forecast_arb.ops.summary import render_operator_summary
        packet = self._base_packet()
        packet["schema_version"] = "1.0"
        with pytest.raises(ValueError, match="schema_version"):
            render_operator_summary(packet)


# ---------------------------------------------------------------------------
# 6. reflection_packet _compute_evidence_class_stats
# ---------------------------------------------------------------------------


class TestComputeEvidenceClassStats:
    """Unit-tests for _compute_evidence_class_stats (private helper)."""

    def _call(self, run_dirs, runs_root):
        from forecast_arb.core.reflection_packet import _compute_evidence_class_stats
        return _compute_evidence_class_stats(run_dirs, runs_root)

    def _write_artifact(self, base_dir: Path, run_name: str, payload) -> Path:
        rd = base_dir / run_name / "artifacts"
        rd.mkdir(parents=True, exist_ok=True)
        p = rd / "p_event_external.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return base_dir / run_name

    def test_empty_run_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = self._call([], Path(tmp))
        assert stats["n_total"] == 0
        assert stats["authoritative_capable_rate"] == 0.0

    def test_all_classes_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for ec in ("EXACT_TERMINAL", "NEARBY_TERMINAL", "PATHWISE_PROXY",
                       "COARSE_REGIME", "UNUSABLE"):
                auth = ec == "EXACT_TERMINAL"
                self._write_artifact(root, f"run_{ec}", {
                    "evidence_class": ec,
                    "authoritative_capable": auth,
                })
            run_dirs = [f"run_{ec}" for ec in
                        ("EXACT_TERMINAL", "NEARBY_TERMINAL", "PATHWISE_PROXY",
                         "COARSE_REGIME", "UNUSABLE")]
            stats = self._call(run_dirs, root)
        assert stats["EXACT_TERMINAL"] == 1
        assert stats["NEARBY_TERMINAL"] == 1
        assert stats["PATHWISE_PROXY"] == 1
        assert stats["COARSE_REGIME"] == 1
        assert stats["UNUSABLE"] == 1
        assert stats["unclassified"] == 0
        assert stats["n_total"] == 5
        assert stats["authoritative_capable_count"] == 1
        assert stats["authoritative_capable_rate"] == 0.2

    def test_missing_artifact_becomes_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_nofile").mkdir(parents=True, exist_ok=True)
            stats = self._call(["run_nofile"], root)
        assert stats["unclassified"] == 1
        assert stats["n_total"] == 1

    def test_plain_float_artifact_becomes_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rd = root / "run_legacy" / "artifacts"
            rd.mkdir(parents=True)
            (rd / "p_event_external.json").write_text("0.65", encoding="utf-8")
            stats = self._call(["run_legacy"], root)
        assert stats["unclassified"] == 1

    def test_unknown_evidence_class_becomes_unclassified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "run_unknown", {"evidence_class": "FUTURE_CLASS"})
            stats = self._call(["run_unknown"], root)
        assert stats["unclassified"] == 1

    def test_authoritative_capable_rate_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "run_a", {
                "evidence_class": "EXACT_TERMINAL", "authoritative_capable": True
            })
            self._write_artifact(root, "run_b", {
                "evidence_class": "EXACT_TERMINAL", "authoritative_capable": True
            })
            self._write_artifact(root, "run_c", {
                "evidence_class": "NEARBY_TERMINAL", "authoritative_capable": False
            })
            stats = self._call(["run_a", "run_b", "run_c"], root)
        assert stats["authoritative_capable_count"] == 2
        assert stats["authoritative_capable_rate"] == round(2 / 3, 4)

    def test_result_keyed_on_all_known_classes(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = self._call([], Path(tmp))
        for key in ("EXACT_TERMINAL", "NEARBY_TERMINAL", "PATHWISE_PROXY",
                    "COARSE_REGIME", "UNUSABLE", "unclassified",
                    "authoritative_capable_count", "authoritative_capable_rate", "n_total"):
            assert key in stats, f"Missing key {key!r} in stats"


# ---------------------------------------------------------------------------
# 7. run_summary Patch C fields read from artifact
# ---------------------------------------------------------------------------


class TestRunSummaryPatchCFields:
    def _make_run_dir(self, tmp: Path, artifact_data: dict) -> Path:
        run_dir = tmp / "run_test"
        arts = run_dir / "artifacts"
        arts.mkdir(parents=True)
        (arts / "p_event_external.json").write_text(
            json.dumps(artifact_data), encoding="utf-8"
        )
        return run_dir

    def test_authoritative_capable_read(self):
        from forecast_arb.core.run_summary import extract_summary
        with tempfile.TemporaryDirectory() as tmp:
            rd = self._make_run_dir(Path(tmp), {
                "value": 0.65,
                "evidence_class": "EXACT_TERMINAL",
                "authoritative_capable": True,
                "semantic_notes": ["exact match"],
                "p_external_role": "AUTHORITATIVE_CAPABLE",
            })
            s = extract_summary(rd)
        assert s["p_external_authoritative_capable"] is True
        assert s["p_external_role"] == "AUTHORITATIVE_CAPABLE"
        assert s["p_external_semantic_notes"] == ["exact match"]
        assert s["p_evidence_class"] == "EXACT_TERMINAL"

    def test_non_authoritative_defaults(self):
        from forecast_arb.core.run_summary import extract_summary
        with tempfile.TemporaryDirectory() as tmp:
            rd = self._make_run_dir(Path(tmp), {
                "value": None,
                "evidence_class": "UNUSABLE",
                "authoritative_capable": False,
                "semantic_notes": [],
                "p_external_role": "DIAGNOSTIC_ONLY",
            })
            s = extract_summary(rd)
        assert s["p_external_authoritative_capable"] is False
        assert s["p_external_role"] == "DIAGNOSTIC_ONLY"

    def test_legacy_float_artifact_safe_defaults(self):
        """Plain-float artifact — no Patch C fields — must not raise."""
        from forecast_arb.core.run_summary import extract_summary
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_legacy"
            arts = run_dir / "artifacts"
            arts.mkdir(parents=True)
            (arts / "p_event_external.json").write_text("0.55", encoding="utf-8")
            s = extract_summary(run_dir)
        assert s["p_external_authoritative_capable"] is False
        assert s["p_external_semantic_notes"] == []
        assert s["p_external_role"] is None

    def test_missing_artifact_safe_defaults(self):
        from forecast_arb.core.run_summary import extract_summary
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_nofile"
            run_dir.mkdir()
            (run_dir / "artifacts").mkdir()
            s = extract_summary(run_dir)
        assert s["p_external_authoritative_capable"] is False
        assert s["p_evidence_class"] is None
        assert s["p_baseline_source"] == "options_implied"

    def test_semantic_notes_full_list_preserved(self):
        """All notes stored (not truncated) — operator summary may truncate display."""
        from forecast_arb.core.run_summary import extract_summary
        notes = ["note one", "note two", "note three"]
        with tempfile.TemporaryDirectory() as tmp:
            rd = self._make_run_dir(Path(tmp), {
                "value": 0.40,
                "evidence_class": "PATHWISE_PROXY",
                "authoritative_capable": False,
                "semantic_notes": notes,
                "p_external_role": "INFORMATIVE_ONLY",
            })
            s = extract_summary(rd)
        assert s["p_external_semantic_notes"] == notes
