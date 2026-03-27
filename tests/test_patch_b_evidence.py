"""
tests/test_patch_b_evidence.py
================================
Patch B: evidence-class refactor.

Covers:
- EvidenceClass enum and threshold constants
- PEventResult backward compat + new fields
- PExternalClassification backward compat + new fields
- classify_external() evidence_class propagation
- Evidence class assignment logic inside KalshiPEventSource.get_p_event()
- verify_invariants() extended checks (Patch B invariants 4-6)
- decision_packet signals include p_evidence_class
- render_operator_summary evidence_class row
- run_summary reads evidence_class from p_event_external.json
- Backward-compatibility: older artifacts without evidence_class work cleanly
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from forecast_arb.oracle.evidence import (
    EvidenceClass,
    EXACT_MATCH_THRESHOLD_PCT,
    EXACT_TERMINAL_SERIES,
    YEARLY_SERIES,
    COARSE_REGIME_MAX_ERROR_PCT,
)
from forecast_arb.oracle.p_event_source import PEventResult
from forecast_arb.oracle.p_event_policy import (
    PExternalClassification,
    classify_external,
    verify_invariants,
)
from forecast_arb.core.decision_packet import build_decision_packet
from forecast_arb.ops.summary import render_operator_summary


# ---------------------------------------------------------------------------
# 1. EvidenceClass enum
# ---------------------------------------------------------------------------


class TestEvidenceClassEnum:
    def test_all_five_values_exist(self):
        assert EvidenceClass.EXACT_TERMINAL
        assert EvidenceClass.NEARBY_TERMINAL
        assert EvidenceClass.PATHWISE_PROXY
        assert EvidenceClass.COARSE_REGIME
        assert EvidenceClass.UNUSABLE

    def test_string_serialisation(self):
        assert EvidenceClass.EXACT_TERMINAL == "EXACT_TERMINAL"
        assert EvidenceClass.PATHWISE_PROXY == "PATHWISE_PROXY"

    def test_round_trip_from_string(self):
        assert EvidenceClass("NEARBY_TERMINAL") == EvidenceClass.NEARBY_TERMINAL
        assert EvidenceClass("COARSE_REGIME") == EvidenceClass.COARSE_REGIME

    def test_exact_match_threshold(self):
        assert EXACT_MATCH_THRESHOLD_PCT == pytest.approx(0.1)

    def test_exact_terminal_series_contains_kxinx_only(self):
        assert "KXINX" in EXACT_TERMINAL_SERIES
        assert "KXINXY" not in EXACT_TERMINAL_SERIES

    def test_yearly_series_contains_kxinxy(self):
        assert "KXINXY" in YEARLY_SERIES
        assert "KXINX" not in YEARLY_SERIES

    def test_coarse_regime_max_error(self):
        assert COARSE_REGIME_MAX_ERROR_PCT == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# 2. PEventResult backward compat + new fields
# ---------------------------------------------------------------------------


def _make_result(**kwargs) -> PEventResult:
    defaults = dict(
        p_event=0.25,
        source="kalshi",
        confidence=0.70,
        timestamp="2026-03-27T00:00:00+00:00",
        metadata={"market_ticker": "KXINX-27MAR26-T4250"},
    )
    defaults.update(kwargs)
    return PEventResult(**defaults)


class TestPEventResultEvidenceClass:
    def test_evidence_class_none_by_default(self):
        r = _make_result()
        assert r.evidence_class is None

    def test_semantic_notes_empty_list_by_default(self):
        r = _make_result()
        assert r.semantic_notes == []
        assert isinstance(r.semantic_notes, list)

    def test_can_set_evidence_class(self):
        r = _make_result(evidence_class=EvidenceClass.EXACT_TERMINAL)
        assert r.evidence_class == EvidenceClass.EXACT_TERMINAL

    def test_semantic_notes_as_list(self):
        r = _make_result(semantic_notes=["note one", "note two"])
        assert r.semantic_notes == ["note one", "note two"]

    def test_to_dict_includes_evidence_class_string(self):
        r = _make_result(evidence_class=EvidenceClass.PATHWISE_PROXY)
        d = r.to_dict()
        assert d["evidence_class"] == "PATHWISE_PROXY"

    def test_to_dict_evidence_class_none_when_unset(self):
        r = _make_result()
        d = r.to_dict()
        assert d["evidence_class"] is None

    def test_to_dict_semantic_notes_serialised_as_list(self):
        r = _make_result(semantic_notes=["a", "b"])
        d = r.to_dict()
        assert d["semantic_notes"] == ["a", "b"]

    def test_to_dict_semantic_notes_empty_list_when_default(self):
        r = _make_result()
        d = r.to_dict()
        assert d["semantic_notes"] == []

    def test_existing_fields_unchanged(self):
        r = _make_result()
        d = r.to_dict()
        assert "p_event" in d
        assert "source" in d
        assert "confidence" in d
        assert "warnings" in d


# ---------------------------------------------------------------------------
# 3. PExternalClassification backward compat + new fields
# ---------------------------------------------------------------------------


def _make_classification(**kwargs) -> PExternalClassification:
    defaults = dict(
        p_external_value=0.25,
        p_external_confidence=0.70,
        p_external_source="kalshi",
        p_external_is_authoritative=True,
        p_external_metadata={"source": "kalshi"},
    )
    defaults.update(kwargs)
    return PExternalClassification(**defaults)


class TestPExternalClassificationEvidenceClass:
    def test_default_evidence_class_is_none(self):
        """Default is None (unclassified). classify_external() always sets it explicitly."""
        c = _make_classification()
        assert c.evidence_class is None

    def test_default_semantic_notes_is_empty_list(self):
        c = _make_classification()
        assert c.semantic_notes == []

    def test_can_set_evidence_class(self):
        c = _make_classification(evidence_class=EvidenceClass.EXACT_TERMINAL)
        assert c.evidence_class == EvidenceClass.EXACT_TERMINAL

    def test_to_dict_includes_evidence_class(self):
        c = _make_classification(evidence_class=EvidenceClass.NEARBY_TERMINAL)
        d = c.to_dict()
        assert d["evidence_class"] == "NEARBY_TERMINAL"

    def test_to_dict_includes_semantic_notes(self):
        c = _make_classification(semantic_notes=["proximity match"])
        d = c.to_dict()
        assert d["semantic_notes"] == ["proximity match"]

    def test_to_dict_includes_all_original_fields(self):
        c = _make_classification()
        d = c.to_dict()
        for key in [
            "p_external_value", "p_external_confidence",
            "p_external_source", "p_external_is_authoritative",
            "p_external_metadata",
        ]:
            assert key in d


# ---------------------------------------------------------------------------
# 4. classify_external() evidence_class propagation
# ---------------------------------------------------------------------------


class TestClassifyExternalPropagates:
    def _kalshi_result(self, p_event, ec=None, notes=None):
        return _make_result(p_event=p_event, evidence_class=ec, semantic_notes=notes or [])

    def test_exact_terminal_propagated(self):
        r = self._kalshi_result(0.25, ec=EvidenceClass.EXACT_TERMINAL, notes=["exact"])
        c = classify_external(r, mode="kalshi")
        assert c.evidence_class == EvidenceClass.EXACT_TERMINAL
        assert c.semantic_notes == ["exact"]

    def test_nearby_terminal_propagated(self):
        r = self._kalshi_result(0.30, ec=EvidenceClass.NEARBY_TERMINAL)
        c = classify_external(r, mode="kalshi")
        assert c.evidence_class == EvidenceClass.NEARBY_TERMINAL

    def test_pathwise_proxy_propagated(self):
        # proxy results have p_event=None
        r = _make_result(
            p_event=None,
            confidence=0.0,
            metadata={"p_external_proxy": 0.15},
            evidence_class=EvidenceClass.PATHWISE_PROXY,
        )
        c = classify_external(r, mode="kalshi")
        assert c.evidence_class == EvidenceClass.PATHWISE_PROXY
        assert c.p_external_is_authoritative is False

    def test_coarse_regime_propagated(self):
        r = _make_result(
            p_event=None,
            confidence=0.0,
            metadata={},
            evidence_class=EvidenceClass.COARSE_REGIME,
        )
        c = classify_external(r, mode="kalshi")
        assert c.evidence_class == EvidenceClass.COARSE_REGIME
        assert c.p_external_is_authoritative is False

    def test_unusable_propagated(self):
        r = _make_result(
            p_event=None,
            confidence=0.0,
            metadata={},
            evidence_class=EvidenceClass.UNUSABLE,
        )
        c = classify_external(r, mode="kalshi")
        assert c.evidence_class == EvidenceClass.UNUSABLE

    def test_none_evidence_class_resolved_to_unusable_by_classify_external(self):
        """classify_external() always resolves None evidence_class → UNUSABLE."""
        r = _make_result(p_event=None, confidence=0.0, metadata={})
        assert r.evidence_class is None  # PEventResult default
        c = classify_external(r, mode="kalshi")
        assert c.evidence_class == EvidenceClass.UNUSABLE

    def test_evidence_class_in_metadata_after_classify(self):
        r = self._kalshi_result(0.25, ec=EvidenceClass.NEARBY_TERMINAL)
        c = classify_external(r, mode="kalshi")
        assert c.p_external_metadata.get("evidence_class") == "NEARBY_TERMINAL"

    def test_fallback_source_gets_unusable(self):
        r = PEventResult(
            p_event=None,
            source="fallback",
            confidence=0.0,
            timestamp="2026-03-27T00:00:00+00:00",
            metadata={"p_external_fallback": 0.30},
            fallback_used=True,
        )
        c = classify_external(r, mode="fallback")
        assert c.evidence_class == EvidenceClass.UNUSABLE


# ---------------------------------------------------------------------------
# 5. Evidence class assignment inside KalshiPEventSource.get_p_event()
# ---------------------------------------------------------------------------


def _mock_search_result(
    exact_match=True,
    p_external=0.25,
    source_series="KXINX",
    mapping_error_pct=0.0,
    proxy=None,
    warnings=None,
    closest_series=None,
    closest_error_pct=None,
):
    closest = None
    if closest_series is not None:
        closest = {
            "series": closest_series,
            "ticker": f"{closest_series}-TEST",
            "mapping_error_pct": closest_error_pct or 0.0,
        }
    return {
        "exact_match": exact_match,
        "p_external": p_external,
        "market_ticker": "KXINX-27MAR26-T4250",
        "source_series": source_series,
        "proxy": proxy,
        "warnings": warnings or [],
        "diagnostics": {
            "closest_match": closest or (
                {
                    "series": source_series,
                    "ticker": "KXINX-27MAR26-T4250",
                    "mapping_error_pct": mapping_error_pct,
                }
                if exact_match
                else None
            ),
        },
    }


def _mock_proxy():
    p = MagicMock()
    p.proxy_market_ticker = "KXINXMINY-01JAN2027-6600.01"
    p.p_external_proxy = 0.18
    p.proxy_method = "yearly_min_hazard_scale"
    p.proxy_series = "KXINXMINY"
    p.proxy_transform = "hazard_scale"
    p.proxy_horizon_days = 45
    p.proxy_source_url = "https://trading-api.kalshi.com/markets/KXINXMINY-01JAN2027-6600.01"
    p.confidence = 0.35
    return p


class TestEvidenceClassAssignment:
    """Test that KalshiPEventSource.get_p_event() assigns the correct evidence class."""

    EVENT_DEF = {
        "type": "index_drawdown",
        "index": "SPX",
        "threshold_pct": -0.15,
        "expiry": "2026-03-27",
    }

    def _get_result(self, search_result_override):
        """Call get_p_event with a mocked kalshi_multi_series_search."""
        from forecast_arb.oracle.p_event_source import KalshiPEventSource

        mock_client = MagicMock()
        source = KalshiPEventSource(mock_client, allow_proxy=True)

        with patch(
            "forecast_arb.kalshi.multi_series_adapter.kalshi_multi_series_search",
            return_value=search_result_override,
        ):
            return source.get_p_event(self.EVENT_DEF, spot_spx=5000.0, horizon_days=45)

    def test_kxinx_zero_error_is_exact_terminal(self):
        sr = _mock_search_result(exact_match=True, source_series="KXINX", mapping_error_pct=0.0)
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.EXACT_TERMINAL

    def test_kxinx_below_threshold_is_exact_terminal(self):
        sr = _mock_search_result(exact_match=True, source_series="KXINX", mapping_error_pct=0.09)
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.EXACT_TERMINAL

    def test_kxinx_at_threshold_is_exact_terminal(self):
        sr = _mock_search_result(exact_match=True, source_series="KXINX", mapping_error_pct=0.1)
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.EXACT_TERMINAL

    def test_kxinx_above_threshold_is_nearby_terminal(self):
        sr = _mock_search_result(exact_match=True, source_series="KXINX", mapping_error_pct=0.11)
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.NEARBY_TERMINAL

    def test_kxinxy_tiny_error_is_nearby_terminal_not_exact(self):
        """KXINXY is yearly series — exact_terminal requires terminal semantics, not just error."""
        sr = _mock_search_result(exact_match=True, source_series="KXINXY", mapping_error_pct=0.0)
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.NEARBY_TERMINAL

    def test_proxy_result_is_pathwise_proxy(self):
        sr = _mock_search_result(
            exact_match=False, p_external=None, proxy=_mock_proxy(), closest_series=None
        )
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.PATHWISE_PROXY
        assert r.p_event is None

    def test_no_match_kxinxy_closest_is_coarse_regime(self):
        sr = _mock_search_result(
            exact_match=False,
            p_external=None,
            proxy=None,
            closest_series="KXINXY",
            closest_error_pct=8.0,  # within 15% bound
        )
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.COARSE_REGIME

    def test_no_match_kxinx_closest_is_unusable(self):
        """KXINX closest with high error → UNUSABLE (not yearly series)."""
        sr = _mock_search_result(
            exact_match=False,
            p_external=None,
            proxy=None,
            closest_series="KXINX",
            closest_error_pct=10.0,
        )
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.UNUSABLE

    def test_no_match_no_closest_is_unusable(self):
        sr = {
            "exact_match": False,
            "p_external": None,
            "market_ticker": None,
            "source_series": None,
            "proxy": None,
            "warnings": ["NO_MARKET_MATCH"],
            "diagnostics": {"closest_match": None},
        }
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.UNUSABLE

    def test_evidence_class_in_metadata_exact(self):
        sr = _mock_search_result(exact_match=True, source_series="KXINX", mapping_error_pct=0.0)
        r = self._get_result(sr)
        assert r.metadata.get("evidence_class") == "EXACT_TERMINAL"

    def test_evidence_class_in_metadata_proxy(self):
        sr = _mock_search_result(exact_match=False, p_external=None, proxy=_mock_proxy())
        r = self._get_result(sr)
        assert r.metadata.get("evidence_class") == "PATHWISE_PROXY"

    def test_semantic_notes_nonempty_on_exact(self):
        sr = _mock_search_result(exact_match=True, source_series="KXINX", mapping_error_pct=0.0)
        r = self._get_result(sr)
        assert len(r.semantic_notes) >= 1

    def test_coarse_regime_kxinxy_beyond_max_error_is_unusable(self):
        """KXINXY market beyond COARSE_REGIME_MAX_ERROR_PCT → UNUSABLE."""
        sr = _mock_search_result(
            exact_match=False,
            p_external=None,
            proxy=None,
            closest_series="KXINXY",
            closest_error_pct=20.0,  # beyond 15% cap
        )
        r = self._get_result(sr)
        assert r.evidence_class == EvidenceClass.UNUSABLE


# ---------------------------------------------------------------------------
# 6. verify_invariants() extended checks (Patch B invariants 4-6)
# ---------------------------------------------------------------------------


class TestVerifyInvariantsExtended:
    def _make(self, **kwargs):
        defaults = dict(
            p_external_value=None,
            p_external_confidence=0.0,
            p_external_source="kalshi",
            p_external_is_authoritative=False,
            p_external_metadata={},
            evidence_class=EvidenceClass.UNUSABLE,
            semantic_notes=[],
        )
        defaults.update(kwargs)
        return PExternalClassification(**defaults)

    def test_pathwise_proxy_not_authoritative_passes(self):
        c = self._make(evidence_class=EvidenceClass.PATHWISE_PROXY, p_external_value=None)
        verify_invariants(c)  # must not raise

    def test_pathwise_proxy_authoritative_raises(self):
        c = self._make(
            evidence_class=EvidenceClass.PATHWISE_PROXY,
            p_external_value=0.20,
            p_external_is_authoritative=True,
        )
        with pytest.raises(AssertionError, match="PATHWISE_PROXY"):
            verify_invariants(c)

    def test_coarse_regime_not_authoritative_passes(self):
        c = self._make(evidence_class=EvidenceClass.COARSE_REGIME, p_external_value=None)
        verify_invariants(c)

    def test_coarse_regime_authoritative_raises(self):
        c = self._make(
            evidence_class=EvidenceClass.COARSE_REGIME,
            p_external_value=0.15,
            p_external_is_authoritative=True,
        )
        with pytest.raises(AssertionError, match="COARSE_REGIME"):
            verify_invariants(c)

    def test_unusable_value_none_passes(self):
        c = self._make(evidence_class=EvidenceClass.UNUSABLE, p_external_value=None)
        verify_invariants(c)

    def test_unusable_with_value_raises(self):
        # Must set is_authoritative=True to bypass invariant 2 first,
        # source=kalshi to allow kalshi+value, then test invariant 6
        c = PExternalClassification(
            p_external_value=0.30,
            p_external_confidence=0.70,
            p_external_source="kalshi",
            p_external_is_authoritative=True,
            p_external_metadata={},
            evidence_class=EvidenceClass.UNUSABLE,
        )
        with pytest.raises(AssertionError, match="UNUSABLE"):
            verify_invariants(c)

    def test_exact_terminal_authoritative_passes(self):
        c = self._make(
            evidence_class=EvidenceClass.EXACT_TERMINAL,
            p_external_value=0.25,
            p_external_is_authoritative=True,
            p_external_source="kalshi",
        )
        verify_invariants(c)  # must not raise

    def test_none_evidence_class_no_crash(self):
        """Pre-Patch-B classifications with no evidence_class must not fail verify."""
        c = self._make(evidence_class=EvidenceClass.UNUSABLE, p_external_value=None)
        # Simulate absence of attribute (pre-Patch-B object)
        object.__setattr__(c, "evidence_class", None)
        verify_invariants(c)


# ---------------------------------------------------------------------------
# 7. decision_packet signals include p_evidence_class
# ---------------------------------------------------------------------------


class TestDecisionPacketEvidenceClass:
    def test_evidence_class_in_signals_when_no_run_dir(self):
        packet = build_decision_packet(run_dir=None)
        assert "p_evidence_class" in packet["signals"]
        assert packet["signals"]["p_evidence_class"] is None

    def test_evidence_class_in_run_block_when_no_run_dir(self):
        packet = build_decision_packet(run_dir=None)
        assert "p_evidence_class" in packet["run"]
        assert packet["run"]["p_evidence_class"] is None

    def test_evidence_class_in_signals_from_artifact(self, tmp_path):
        """When p_event_external.json contains evidence_class, it appears in signals."""
        run_dir = tmp_path / "run_20260327_120000"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)

        (artifacts / "p_event_external.json").write_text(
            json.dumps({"value": 0.22, "evidence_class": "EXACT_TERMINAL"}),
            encoding="utf-8",
        )

        packet = build_decision_packet(run_dir=run_dir)
        assert packet["signals"]["p_evidence_class"] == "EXACT_TERMINAL"
        assert packet["run"]["p_evidence_class"] == "EXACT_TERMINAL"

    def test_evidence_class_none_from_plain_float_artifact(self, tmp_path):
        """Older artifact that is a plain float → p_evidence_class=None."""
        run_dir = tmp_path / "run_20260327_120001"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "p_event_external.json").write_text("0.28", encoding="utf-8")

        packet = build_decision_packet(run_dir=run_dir)
        assert packet["signals"]["p_evidence_class"] is None


# ---------------------------------------------------------------------------
# 8. render_operator_summary evidence_class row
# ---------------------------------------------------------------------------


def _minimal_packet(evidence_class=None):
    return {
        "schema_version": "2.0",
        "ts_utc": "2026-03-27T12:00:00+00:00",
        "run": {
            "run_id": "run_test",
            "timestamp": "2026-03-27",
            "mode": "paper",
            "decision": "NO_TRADE",
            "reason": "LOW_CONFIDENCE",
            "num_tickets": 0,
            "submit_requested": False,
            "submit_executed": False,
        },
        "broker_preflight": None,
        "top_candidates": [],
        "signals": {
            "p_external": 0.22,
            "p_implied": 0.18,
            "edge": 0.04,
            "confidence": 0.70,
            "gate_decision": "NO_TRADE",
            "p_evidence_class": evidence_class,
        },
        "notes": [],
    }


class TestOperatorSummaryEvidenceClass:
    def test_evidence_class_row_present(self):
        md = render_operator_summary(_minimal_packet("EXACT_TERMINAL"))
        assert "evidence_class" in md

    def test_evidence_class_value_shown(self):
        md = render_operator_summary(_minimal_packet("PATHWISE_PROXY"))
        assert "PATHWISE_PROXY" in md

    def test_evidence_class_na_when_none(self):
        md = render_operator_summary(_minimal_packet(None))
        assert "N/A" in md

    def test_no_crash_when_key_absent(self):
        """Signals dict missing p_evidence_class entirely — row shows N/A."""
        packet = _minimal_packet()
        del packet["signals"]["p_evidence_class"]
        md = render_operator_summary(packet)
        assert "evidence_class" in md


# ---------------------------------------------------------------------------
# 9. Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_old_artifact_no_evidence_class_gives_none(self, tmp_path):
        """p_event_external.json without evidence_class → p_evidence_class=None."""
        from forecast_arb.core.run_summary import extract_summary

        run_dir = tmp_path / "run_legacy"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)

        # Old format: dict with value but no evidence_class
        (artifacts / "p_event_external.json").write_text(
            json.dumps({"value": 0.25, "p_event": 0.25}),
            encoding="utf-8",
        )

        summary = extract_summary(run_dir)
        assert summary["p_evidence_class"] is None
        assert summary["p_external"] == pytest.approx(0.25)

    def test_packet_with_no_evidence_class_in_signals_renders_cleanly(self):
        """Older packet missing p_evidence_class in signals → summary renders, shows N/A."""
        packet = {
            "schema_version": "2.0",
            "ts_utc": "2026-03-27T12:00:00+00:00",
            "run": {
                "run_id": "run_old",
                "timestamp": "2026-03-27",
                "mode": "paper",
                "decision": "NO_TRADE",
                "reason": "test",
                "num_tickets": 0,
                "submit_requested": False,
                "submit_executed": False,
            },
            "broker_preflight": None,
            "top_candidates": [],
            # Simulate old signals without p_evidence_class
            "signals": {
                "p_external": 0.20,
                "p_implied": 0.18,
                "edge": 0.02,
                "confidence": 0.65,
                "gate_decision": "NO_TRADE",
            },
            "notes": [],
        }
        md = render_operator_summary(packet)
        # Must render without exception and show N/A for evidence_class
        assert "evidence_class" in md
        assert "N/A" in md

    def test_build_packet_from_run_dir_without_p_event_artifact(self, tmp_path):
        """Run dir with no p_event_external.json → p_evidence_class=None, no crash."""
        run_dir = tmp_path / "run_nopext"
        run_dir.mkdir()
        (run_dir / "artifacts").mkdir()

        packet = build_decision_packet(run_dir=run_dir)
        assert packet["signals"]["p_evidence_class"] is None
        assert packet["run"]["p_evidence_class"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
