"""
tests/test_patch_d_policy.py
==============================
Patch D policy-surfacing tests.

Covers:
  1. EVIDENCE_POLICY_DESC — completeness, wording invariants
  2. get_policy_role() — None-safe accessor
  3. GateDecision — p_external_role + p_external_gate_semantics fields
  4. Reflection — by_role, informative_or_above_rate, terminal_or_above_rate
  5. Operator summary — annotated role row, used_for_gating qualifier, gate semantics
  6. Older artifact compatibility — missing Patch D fields handled gracefully
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from forecast_arb.oracle.evidence import (
    EVIDENCE_POLICY_DESC,
    EVIDENCE_ROLE,
    EvidenceClass,
    get_policy_role,
    is_authoritative_capable,
)
from forecast_arb.gating.edge_gate import GateDecision, gate


# ---------------------------------------------------------------------------
# 1. EVIDENCE_POLICY_DESC completeness + wording invariants
# ---------------------------------------------------------------------------


class TestEvidencePolicyDesc:
    def test_all_classes_have_description(self):
        for ec in EvidenceClass:
            assert ec in EVIDENCE_POLICY_DESC, f"{ec} missing from EVIDENCE_POLICY_DESC"

    def test_descriptions_are_non_empty_strings(self):
        for ec, desc in EVIDENCE_POLICY_DESC.items():
            assert isinstance(desc, str) and desc.strip(), f"Empty description for {ec}"

    def test_policy_desc_and_role_have_identical_key_sets(self):
        role_keys = set(EVIDENCE_ROLE.keys())
        desc_keys = set(EVIDENCE_POLICY_DESC.keys())
        assert role_keys == desc_keys

    def test_exact_terminal_description_is_present_tense_eligible(self):
        """EXACT_TERMINAL must say 'eligible for future authority, not currently determinative'."""
        desc = EVIDENCE_POLICY_DESC[EvidenceClass.EXACT_TERMINAL]
        assert "eligible" in desc.lower() or "future" in desc.lower(), (
            f"EXACT_TERMINAL description must reference future eligibility: {desc!r}"
        )
        assert "not currently" in desc.lower() or "not currently determinative" in desc.lower(), (
            f"EXACT_TERMINAL description must say 'not currently determinative': {desc!r}"
        )

    def test_non_authoritative_descriptions_do_not_contain_word_authoritative(self):
        """Guard against accidentally promoting non-authoritative classes in description text."""
        non_auth = [
            EvidenceClass.NEARBY_TERMINAL,
            EvidenceClass.PATHWISE_PROXY,
            EvidenceClass.COARSE_REGIME,
            EvidenceClass.UNUSABLE,
        ]
        for ec in non_auth:
            desc = EVIDENCE_POLICY_DESC[ec]
            assert "authoritative" not in desc.lower(), (
                f"{ec} description must not contain 'authoritative': {desc!r}"
            )

    def test_informative_classes_say_does_not_affect_gate(self):
        for ec in (EvidenceClass.NEARBY_TERMINAL, EvidenceClass.PATHWISE_PROXY):
            desc = EVIDENCE_POLICY_DESC[ec]
            assert "does not affect gate" in desc.lower() or "does not gate" in desc.lower(), (
                f"{ec} description should state it does not affect gate: {desc!r}"
            )

    def test_context_only_class_does_not_affect_gate(self):
        desc = EVIDENCE_POLICY_DESC[EvidenceClass.COARSE_REGIME]
        assert "does not affect gate" in desc.lower() or "does not gate" in desc.lower(), (
            f"COARSE_REGIME description should state it does not affect gate: {desc!r}"
        )

    def test_unusable_is_absence_of_evidence(self):
        desc = EVIDENCE_POLICY_DESC[EvidenceClass.UNUSABLE]
        assert "absence" in desc.lower() or "diagnostic" in desc.lower(), (
            f"UNUSABLE description should reference absence-of-evidence: {desc!r}"
        )

    def test_descriptions_are_immutable_to_callers(self):
        """Modifying the returned value from EVIDENCE_POLICY_DESC should not mutate the table."""
        desc = EVIDENCE_POLICY_DESC[EvidenceClass.EXACT_TERMINAL]
        original = str(desc)
        # strings are immutable in Python — this is a structural check
        assert EVIDENCE_POLICY_DESC[EvidenceClass.EXACT_TERMINAL] == original


# ---------------------------------------------------------------------------
# 2. get_policy_role()
# ---------------------------------------------------------------------------


class TestGetPolicyRole:
    def test_none_returns_unknown(self):
        assert get_policy_role(None) == "UNKNOWN"

    def test_exact_terminal_returns_authoritative_capable(self):
        assert get_policy_role(EvidenceClass.EXACT_TERMINAL) == "AUTHORITATIVE_CAPABLE"

    def test_nearby_terminal_returns_informative_only(self):
        assert get_policy_role(EvidenceClass.NEARBY_TERMINAL) == "INFORMATIVE_ONLY"

    def test_pathwise_proxy_returns_informative_only(self):
        assert get_policy_role(EvidenceClass.PATHWISE_PROXY) == "INFORMATIVE_ONLY"

    def test_coarse_regime_returns_context_only(self):
        assert get_policy_role(EvidenceClass.COARSE_REGIME) == "CONTEXT_ONLY"

    def test_unusable_returns_diagnostic_only(self):
        assert get_policy_role(EvidenceClass.UNUSABLE) == "DIAGNOSTIC_ONLY"

    def test_all_known_classes_return_non_unknown(self):
        for ec in EvidenceClass:
            role = get_policy_role(ec)
            assert role != "UNKNOWN", f"{ec} should have a known role"

    def test_string_value_works_for_enum_member(self):
        """EvidenceClass is str+Enum so string values should match too."""
        assert get_policy_role("EXACT_TERMINAL") == "AUTHORITATIVE_CAPABLE"

    def test_unrecognised_string_returns_unknown_not_error(self):
        result = get_policy_role("FUTURE_UNKNOWN_CLASS")  # type: ignore[arg-type]
        assert result == "UNKNOWN"

    def test_consistent_with_evidence_role_table(self):
        for ec in EvidenceClass:
            assert get_policy_role(ec) == EVIDENCE_ROLE[ec]


# ---------------------------------------------------------------------------
# 3. GateDecision — p_external_role + p_external_gate_semantics
# ---------------------------------------------------------------------------


def _make_p_event(p_event=0.60, confidence=0.75, evidence_class=None):
    obj = MagicMock()
    obj.p_event = p_event
    obj.confidence = confidence
    obj.metadata = {}
    obj.evidence_class = evidence_class
    return obj


class TestGateDecisionPatchDFields:
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

    def test_p_external_role_default_is_none(self):
        gd = self._make_gd()
        assert gd.p_external_role is None

    def test_p_external_gate_semantics_default(self):
        gd = self._make_gd()
        assert gd.p_external_gate_semantics == "consulted_not_determinative"

    def test_to_dict_always_emits_role(self):
        gd = self._make_gd()
        d = gd.to_dict()
        assert "p_external_role" in d

    def test_to_dict_always_emits_gate_semantics(self):
        gd = self._make_gd()
        d = gd.to_dict()
        assert "p_external_gate_semantics" in d

    def test_gate_semantics_value_in_to_dict(self):
        gd = self._make_gd()
        d = gd.to_dict()
        assert d["p_external_gate_semantics"] == "consulted_not_determinative"

    def test_role_none_in_to_dict_when_not_set(self):
        gd = self._make_gd()
        d = gd.to_dict()
        assert d["p_external_role"] is None

    def test_role_propagated_when_set(self):
        gd = self._make_gd(p_external_role="INFORMATIVE_ONLY")
        d = gd.to_dict()
        assert d["p_external_role"] == "INFORMATIVE_ONLY"

    def test_patch_c_fields_still_present(self):
        """Patch D must not remove Patch C fields from to_dict()."""
        gd = self._make_gd()
        d = gd.to_dict()
        assert "evidence_class" in d
        assert "p_external_authoritative_capable" in d

    def test_gate_populates_role_exact_terminal(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.EXACT_TERMINAL)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["p_external_role"] == "AUTHORITATIVE_CAPABLE"
        assert d["p_external_gate_semantics"] == "consulted_not_determinative"

    def test_gate_populates_role_nearby_terminal(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.NEARBY_TERMINAL)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        assert gd.to_dict()["p_external_role"] == "INFORMATIVE_ONLY"

    def test_gate_populates_role_coarse_regime(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.COARSE_REGIME)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        assert gd.to_dict()["p_external_role"] == "CONTEXT_ONLY"

    def test_gate_role_none_when_no_evidence_class(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=None)
        p_imp = _make_p_event(p_event=0.50)
        gd = gate(p_ext, p_imp)
        assert gd.to_dict()["p_external_role"] is None

    def test_gate_role_does_not_change_pass_fail(self):
        """p_external_role must never alter the gate outcome."""
        p_ext_auth = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.EXACT_TERMINAL)
        p_ext_diag = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.UNUSABLE)
        p_imp = _make_p_event(p_event=0.50)
        gd_auth = gate(p_ext_auth, p_imp)
        gd_diag = gate(p_ext_diag, p_imp)
        assert gd_auth.decision == gd_diag.decision
        assert gd_auth.reason == gd_diag.reason

    def test_no_trade_path_still_has_patch_d_fields(self):
        p_ext = _make_p_event(p_event=0.65, evidence_class=EvidenceClass.PATHWISE_PROXY)
        p_imp = _make_p_event(p_event=None)
        gd = gate(p_ext, p_imp)
        d = gd.to_dict()
        assert d["decision"] == "NO_TRADE"
        assert "p_external_role" in d
        assert "p_external_gate_semantics" in d
        assert d["p_external_role"] == "INFORMATIVE_ONLY"


# ---------------------------------------------------------------------------
# 4. Reflection — by_role, informative_or_above_rate, terminal_or_above_rate
# ---------------------------------------------------------------------------


class TestReflectionPatchDStats:
    def _call(self, run_dirs, runs_root):
        from forecast_arb.core.reflection_packet import _compute_evidence_class_stats
        return _compute_evidence_class_stats(run_dirs, runs_root)

    def _write_artifact(self, base_dir: Path, run_name: str, payload) -> None:
        rd = base_dir / run_name / "artifacts"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "p_event_external.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_empty_list_by_role_all_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = self._call([], Path(tmp))
        by_role = stats["by_role"]
        assert by_role["AUTHORITATIVE_CAPABLE"] == 0
        assert by_role["INFORMATIVE_ONLY"] == 0
        assert by_role["CONTEXT_ONLY"] == 0
        assert by_role["DIAGNOSTIC_ONLY"] == 0
        assert by_role["UNKNOWN"] == 0

    def test_by_role_always_has_all_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = self._call([], Path(tmp))
        for key in ("AUTHORITATIVE_CAPABLE", "INFORMATIVE_ONLY", "CONTEXT_ONLY",
                    "DIAGNOSTIC_ONLY", "UNKNOWN"):
            assert key in stats["by_role"], f"by_role missing key {key!r}"

    def test_exact_terminal_increments_authoritative_capable_in_by_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r1", {
                "evidence_class": "EXACT_TERMINAL", "authoritative_capable": True
            })
            stats = self._call(["r1"], root)
        assert stats["by_role"]["AUTHORITATIVE_CAPABLE"] == 1

    def test_nearby_terminal_increments_informative_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r1", {
                "evidence_class": "NEARBY_TERMINAL", "authoritative_capable": False
            })
            stats = self._call(["r1"], root)
        assert stats["by_role"]["INFORMATIVE_ONLY"] == 1

    def test_coarse_regime_increments_context_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r1", {
                "evidence_class": "COARSE_REGIME", "authoritative_capable": False
            })
            stats = self._call(["r1"], root)
        assert stats["by_role"]["CONTEXT_ONLY"] == 1

    def test_unusable_increments_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r1", {
                "evidence_class": "UNUSABLE", "authoritative_capable": False
            })
            stats = self._call(["r1"], root)
        assert stats["by_role"]["DIAGNOSTIC_ONLY"] == 1

    def test_unclassified_run_increments_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r1").mkdir()  # no artifacts dir
            stats = self._call(["r1"], root)
        assert stats["by_role"]["UNKNOWN"] == 1
        assert stats["unclassified"] == 1

    def test_by_role_values_sum_to_n_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r1", {"evidence_class": "EXACT_TERMINAL", "authoritative_capable": True})
            self._write_artifact(root, "r2", {"evidence_class": "NEARBY_TERMINAL", "authoritative_capable": False})
            self._write_artifact(root, "r3", {"evidence_class": "UNUSABLE", "authoritative_capable": False})
            (root / "r4").mkdir()  # unclassified
            stats = self._call(["r1", "r2", "r3", "r4"], root)
        assert sum(stats["by_role"].values()) == stats["n_total"]

    def test_informative_or_above_rate_all_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(3):
                self._write_artifact(root, f"r{i}", {
                    "evidence_class": "EXACT_TERMINAL", "authoritative_capable": True
                })
            stats = self._call(["r0", "r1", "r2"], root)
        assert stats["informative_or_above_rate"] == 1.0

    def test_informative_or_above_rate_all_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(3):
                self._write_artifact(root, f"r{i}", {
                    "evidence_class": "UNUSABLE", "authoritative_capable": False
                })
            stats = self._call(["r0", "r1", "r2"], root)
        assert stats["informative_or_above_rate"] == 0.0

    def test_terminal_or_above_rate_exact_and_nearby(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r0", {"evidence_class": "EXACT_TERMINAL", "authoritative_capable": True})
            self._write_artifact(root, "r1", {"evidence_class": "NEARBY_TERMINAL", "authoritative_capable": False})
            self._write_artifact(root, "r2", {"evidence_class": "PATHWISE_PROXY", "authoritative_capable": False})
            stats = self._call(["r0", "r1", "r2"], root)
        assert stats["terminal_or_above_rate"] == round(2 / 3, 4)
        assert stats["informative_or_above_rate"] == 1.0

    def test_terminal_or_above_rate_zero_when_only_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r0", {"evidence_class": "PATHWISE_PROXY", "authoritative_capable": False})
            stats = self._call(["r0"], root)
        assert stats["terminal_or_above_rate"] == 0.0
        assert stats["informative_or_above_rate"] == 1.0

    def test_rates_over_classified_only_not_n_total(self):
        """Unclassified runs must not dilute the informative rates."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, "r0", {"evidence_class": "EXACT_TERMINAL", "authoritative_capable": True})
            (root / "r1").mkdir()  # unclassified — must not be in denominator
            stats = self._call(["r0", "r1"], root)
        # 1 classified out of 1 classified → rate = 1.0 (not 0.5)
        assert stats["informative_or_above_rate"] == 1.0
        assert stats["terminal_or_above_rate"] == 1.0

    def test_rates_zero_when_no_classified_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r1").mkdir()
            stats = self._call(["r1"], root)
        assert stats["informative_or_above_rate"] == 0.0
        assert stats["terminal_or_above_rate"] == 0.0

    def test_existing_patch_c_keys_unchanged(self):
        """Patch D must not remove or rename any Patch C output keys."""
        with tempfile.TemporaryDirectory() as tmp:
            stats = self._call([], Path(tmp))
        for key in ("EXACT_TERMINAL", "NEARBY_TERMINAL", "PATHWISE_PROXY",
                    "COARSE_REGIME", "UNUSABLE", "unclassified",
                    "authoritative_capable_count", "authoritative_capable_rate", "n_total"):
            assert key in stats, f"Patch C key {key!r} missing from Patch D stats"


# ---------------------------------------------------------------------------
# 5. Operator summary — annotated role, used_for_gating qualifier, gate semantics
# ---------------------------------------------------------------------------


class TestOperatorSummaryPatchD:
    def _base_packet(self, signals_overrides=None) -> Dict[str, Any]:
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
            "p_external_gate_semantics": "consulted_not_determinative",
        }
        if signals_overrides:
            signals.update(signals_overrides)
        return {
            "schema_version": "2.0",
            "ts_utc": "2026-03-27T00:00:00+00:00",
            "run": {
                "run_id": "test",
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

    def test_role_row_contains_policy_description_for_exact_terminal(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        # The annotated role row should include text from EVIDENCE_POLICY_DESC
        assert "eligible" in md.lower() or "future" in md.lower() or "not currently" in md.lower()

    def test_role_row_contains_role_string(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        assert "AUTHORITATIVE_CAPABLE" in md

    def test_used_for_gating_row_contains_not_determinative(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        assert "not determinative" in md.lower() or "consulted_not_determinative" in md.lower()

    def test_non_authoritative_warning_contains_no_class_affects_gate(self):
        from forecast_arb.ops.summary import render_operator_summary
        packet = self._base_packet({
            "p_external_authoritative_capable": False,
            "p_external_role": "INFORMATIVE_ONLY",
            "p_evidence_class": "NEARBY_TERMINAL",
            "p_external_semantic_notes": [],
        })
        md = render_operator_summary(packet)
        assert "no evidenceclass currently affects gate" in md.lower() or \
               "no evidence" in md.lower()

    def test_non_authoritative_warning_mentions_exact_terminal_future_only(self):
        from forecast_arb.ops.summary import render_operator_summary
        packet = self._base_packet({
            "p_external_authoritative_capable": False,
            "p_external_role": "DIAGNOSTIC_ONLY",
            "p_evidence_class": "UNUSABLE",
            "p_external_semantic_notes": [],
        })
        md = render_operator_summary(packet)
        assert "exact_terminal" in md.lower() or "exact terminal" in md.lower()
        assert "future" in md.lower()

    def test_authoritative_case_no_warning_block(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        # No non-authoritative warning when auth_capable=True
        assert "non-authoritative" not in md.lower()

    def test_informative_role_row_annotated_correctly(self):
        from forecast_arb.ops.summary import render_operator_summary
        packet = self._base_packet({
            "p_external_authoritative_capable": False,
            "p_external_role": "INFORMATIVE_ONLY",
            "p_evidence_class": "NEARBY_TERMINAL",
            "p_external_semantic_notes": [],
        })
        md = render_operator_summary(packet)
        assert "INFORMATIVE_ONLY" in md
        # Policy desc for NEARBY should mention "does not affect gate"
        assert "does not affect gate" in md.lower()

    def test_gate_semantics_field_surfaced(self):
        from forecast_arb.ops.summary import render_operator_summary
        md = render_operator_summary(self._base_packet())
        assert "consulted_not_determinative" in md

    def test_missing_gate_semantics_graceful(self):
        """Pre-Patch-D packet without p_external_gate_semantics must not raise."""
        from forecast_arb.ops.summary import render_operator_summary
        packet = self._base_packet()
        del packet["signals"]["p_external_gate_semantics"]
        md = render_operator_summary(packet)
        assert md  # did not raise; rendered something


# ---------------------------------------------------------------------------
# 6. Older artifact compatibility
# ---------------------------------------------------------------------------


class TestOlderArtifactCompat:
    """Verify Patch D additions degrade gracefully for pre-Patch-D artifacts."""

    def test_gate_decision_json_missing_p_external_role_handled(self):
        """decision_packet should not raise when gate_decision.json lacks Patch D fields."""
        import tempfile, json as _json
        from forecast_arb.core.decision_packet import build_decision_packet
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_old"
            arts = run_dir / "artifacts"
            arts.mkdir(parents=True)
            # Write a pre-Patch-D gate_decision.json (Patch C fields only)
            (arts / "gate_decision.json").write_text(_json.dumps({
                "decision": "NO_TRADE",
                "reason": "NO_P_EXTERNAL",
                "edge": None,
                "p_external": None,
                "p_implied": 0.55,
                "confidence_gate": 0.70,
                "confidence_external": 0.0,
                "confidence_implied": 0.72,
                "metadata": {},
                "evidence_class": "NEARBY_TERMINAL",
                "p_external_authoritative_capable": False,
                # p_external_role and p_external_gate_semantics absent (pre-Patch-D)
            }), encoding="utf-8")
            packet = build_decision_packet(run_dir=run_dir)

        signals = packet["signals"]
        # p_external_gate_semantics should be None (missing from old artifact)
        assert signals.get("p_external_gate_semantics") is None
        # p_external_used_for_gating: p_external was None in artifact → False
        assert signals["p_external_used_for_gating"] is False

    def test_gate_decision_without_role_does_not_break_summary(self):
        """render_operator_summary must render cleanly when role is None."""
        from forecast_arb.ops.summary import render_operator_summary
        packet = {
            "schema_version": "2.0",
            "ts_utc": "2026-03-27T00:00:00+00:00",
            "run": {
                "run_id": "old-run",
                "timestamp": None,
                "mode": "review_only",
                "decision": "NO_TRADE",
                "reason": "NO_P_EXTERNAL",
                "num_tickets": 0,
                "submit_requested": False,
                "submit_executed": False,
            },
            "broker_preflight": None,
            "top_candidates": [],
            "signals": {
                "p_external": None,
                "p_implied": 0.55,
                "edge": None,
                "confidence": 0.70,
                "gate_decision": "NO_TRADE",
                "p_evidence_class": None,
                "p_external_authoritative_capable": False,
                "p_external_used_for_gating": False,
                "p_external_role": None,          # pre-Patch-D: absent / None
                "p_baseline_source": "options_implied",
                "p_external_semantic_notes": [],
                # p_external_gate_semantics intentionally absent
            },
            "notes": [],
        }
        md = render_operator_summary(packet)
        assert "# Daily Operator Summary" in md

    def test_reflection_stats_on_pre_patch_d_artifact_still_work(self):
        """p_event_external.json without Patch D fields should be bucketed correctly."""
        from forecast_arb.core.reflection_packet import _compute_evidence_class_stats
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rd = root / "old_run" / "artifacts"
            rd.mkdir(parents=True)
            # Patch C artifact: has evidence_class but no by_role key
            (rd / "p_event_external.json").write_text(json.dumps({
                "value": 0.60,
                "evidence_class": "NEARBY_TERMINAL",
                "authoritative_capable": False,
                # No extra Patch D fields
            }), encoding="utf-8")
            stats = _compute_evidence_class_stats(["old_run"], root)
        assert stats["NEARBY_TERMINAL"] == 1
        assert stats["by_role"]["INFORMATIVE_ONLY"] == 1
        assert stats["informative_or_above_rate"] == 1.0
        assert stats["terminal_or_above_rate"] == 1.0

    def test_gate_decision_object_without_role_field_is_valid(self):
        """A GateDecision constructed without p_external_role must still to_dict() cleanly."""
        gd = GateDecision(
            decision="NO_TRADE",
            reason="NO_P_EXTERNAL",
            edge=None,
            p_external=None,
            p_implied=0.55,
            confidence=0.0,
            confidence_external=0.0,
            confidence_implied=0.72,
        )
        d = gd.to_dict()
        assert d["p_external_role"] is None
        assert d["p_external_gate_semantics"] == "consulted_not_determinative"
