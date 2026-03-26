"""
Patch 4 — Optional analyst layer tests.

Covers:
- run_analyst: schema-version mismatch → error dict, no API call
- run_analyst: missing OPENAI_API_KEY → error dict, no raise
- run_analyst: successful structured output parsing
- run_analyst: recommendation constrained to valid values
- run_analyst: OpenAI API error → error dict, no raise
- run_analyst: malformed JSON response → error dict, no raise
- run_agent_daily: analyst=True writes analyst_recommendation.json
- run_agent_daily: operator_summary.json unchanged by analyst run
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from forecast_arb.ops.analyst import run_analyst, SUPPORTED_SCHEMA_VERSION


def _valid_packet(**overrides):
    base = {
        "schema_version": "2.0",
        "ts_utc": "2026-03-25T10:00:00+00:00",
        "run": {
            "run_id": "run_001",
            "decision": "NO_TRADE",
            "reason": "LOW_EDGE",
            "edge": 0.05,
            "p_external": 0.06,
            "p_implied": 0.04,
            "confidence": 0.55,
            "num_tickets": 0,
            "submit_requested": False,
            "submit_executed": False,
        },
        "broker_preflight": None,
        "top_candidates": [],
        "signals": {
            "p_external": 0.06,
            "p_implied": 0.04,
            "edge": 0.05,
            "confidence": 0.55,
            "gate_decision": "PASS",
        },
        "notes": [],
    }
    base.update(overrides)
    return base


_VALID_SUMMARY_MD = "# Daily Operator Summary\n\n**Decision:** NO_TRADE\n"


def _mock_openai_response(content: str):
    """Build a minimal fake openai response object."""
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


# ---------------------------------------------------------------------------
# run_analyst unit tests
# ---------------------------------------------------------------------------


def test_analyst_rejects_wrong_schema_version():
    """Wrong schema_version → status='error', no API call made."""
    bad_packet = _valid_packet(schema_version="1.0")
    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        result = run_analyst(bad_packet, _VALID_SUMMARY_MD)

    assert result["status"] == "error"
    assert "schema_version" in result["error"]
    assert result["recommendation"] is None
    mock_cls.assert_not_called()


def test_analyst_rejects_missing_schema_version():
    """Missing schema_version → status='error', no API call."""
    packet = _valid_packet()
    del packet["schema_version"]
    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        result = run_analyst(packet, _VALID_SUMMARY_MD)

    assert result["status"] == "error"
    mock_cls.assert_not_called()


def test_analyst_missing_api_key_returns_error(monkeypatch):
    """OPENAI_API_KEY not set → status='error', no raise."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        result = run_analyst(_valid_packet(), _VALID_SUMMARY_MD)

    assert result["status"] == "error"
    assert "OPENAI_API_KEY" in result["error"]
    assert result["recommendation"] is None
    mock_cls.assert_not_called()


def test_analyst_success_parses_structured_output(monkeypatch):
    """Mock OpenAI returns valid JSON → status='ok', fields extracted."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    api_json = json.dumps({
        "recommendation": "SKIP",
        "confidence": 0.72,
        "rationale": "Edge is below threshold; no compelling entry.",
        "flags": ["LOW_EDGE", "NO_CANDIDATES"],
    })
    mock_response = _mock_openai_response(api_json)

    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.return_value = mock_response
        mock_cls.return_value = mock_instance

        result = run_analyst(_valid_packet(), _VALID_SUMMARY_MD)

    assert result["status"] == "ok"
    assert result["recommendation"] == "SKIP"
    assert abs(result["confidence"] - 0.72) < 1e-6
    assert "Edge is below threshold" in result["rationale"]
    assert "LOW_EDGE" in result["flags"]
    assert result["error"] is None
    assert "ts_utc" in result


def test_analyst_recommendation_valid_values(monkeypatch):
    """Only EXECUTE/SKIP/REVIEW are accepted; anything else → None."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    for valid_rec in ("EXECUTE", "SKIP", "REVIEW"):
        api_json = json.dumps({"recommendation": valid_rec, "confidence": 0.5,
                               "rationale": "ok", "flags": []})
        mock_response = _mock_openai_response(api_json)

        with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.chat.completions.create.return_value = mock_response
            mock_cls.return_value = mock_instance
            result = run_analyst(_valid_packet(), _VALID_SUMMARY_MD)

        assert result["recommendation"] == valid_rec, f"Expected {valid_rec}"

    # Invalid value
    api_json = json.dumps({"recommendation": "MAYBE", "confidence": 0.5,
                           "rationale": "unsure", "flags": []})
    mock_response = _mock_openai_response(api_json)
    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.return_value = mock_response
        mock_cls.return_value = mock_instance
        result = run_analyst(_valid_packet(), _VALID_SUMMARY_MD)

    assert result["recommendation"] is None


def test_analyst_api_error_returns_error_dict(monkeypatch):
    """OpenAI API raises → status='error', no raise."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.side_effect = Exception("connection refused")
        mock_cls.return_value = mock_instance

        result = run_analyst(_valid_packet(), _VALID_SUMMARY_MD)

    assert result["status"] == "error"
    assert "connection refused" in result["error"]
    assert result["recommendation"] is None


def test_analyst_malformed_json_returns_error(monkeypatch):
    """Non-JSON API response → status='error', no raise."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = _mock_openai_response("not valid json {{{")

    with patch("forecast_arb.ops.analyst.openai.OpenAI") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.chat.completions.create.return_value = mock_response
        mock_cls.return_value = mock_instance

        result = run_analyst(_valid_packet(), _VALID_SUMMARY_MD)

    assert result["status"] == "error"
    assert "JSON parse error" in result["error"]
    assert result["recommendation"] is None


# ---------------------------------------------------------------------------
# run_agent_daily with analyst=True
# ---------------------------------------------------------------------------

from agent_daily import run_agent_daily


def _make_mock_run_result(run_dir: Path, run_id: str = "run_test_a01") -> dict:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "final_decision.json").write_text(json.dumps({
        "decision": "NO_TRADE",
        "reason": "LOW_EDGE",
        "submit_requested": False,
        "submit_executed": False,
    }))
    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "regimes_run": ["crash"],
        "regime_results": {},
        "snapshot_path": None,
        "config_checksum": "abc123",
        "ts_utc": "2026-03-25T10:00:00+00:00",
    }


def _mock_analyst_result(recommendation: str = "SKIP") -> dict:
    return {
        "status": "ok",
        "recommendation": recommendation,
        "confidence": 0.65,
        "rationale": "Edge below threshold.",
        "flags": ["LOW_EDGE"],
        "raw_response": "{}",
        "error": None,
        "ts_utc": "2026-03-25T10:00:01+00:00",
    }


def test_run_agent_daily_analyst_flag_writes_artifact(tmp_path):
    """analyst=True → analyst_recommendation.json is written in artifacts dir."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "crash_venture_v2" / "run_test_a01"
    mock_run = _make_mock_run_result(run_dir)

    with patch("run_daily_v2.run_daily_core", return_value=mock_run), \
         patch("agent_daily.run_analyst", return_value=_mock_analyst_result()) as mock_ra:

        packet = run_agent_daily(runs_root=runs_root, analyst=True)

    analyst_path = run_dir / "artifacts" / "analyst_recommendation.json"
    assert analyst_path.exists(), "analyst_recommendation.json not written"

    with open(analyst_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["recommendation"] == "SKIP"
    assert saved["status"] == "ok"

    # Also accessible on returned packet
    assert packet["_analyst"]["recommendation"] == "SKIP"


def test_run_agent_daily_operator_summary_json_unchanged(tmp_path):
    """operator_summary.json must not contain analyst keys after analyst run."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "crash_venture_v2" / "run_test_a02"
    mock_run = _make_mock_run_result(run_dir, run_id="run_test_a02")

    with patch("run_daily_v2.run_daily_core", return_value=mock_run), \
         patch("agent_daily.run_analyst", return_value=_mock_analyst_result()):

        run_agent_daily(runs_root=runs_root, analyst=True)

    op_summary_path = run_dir / "artifacts" / "operator_summary.json"
    with open(op_summary_path, encoding="utf-8") as fh:
        saved = json.load(fh)

    assert saved["schema_version"] == "2.0"
    assert "_analyst" not in saved
    assert "analyst_result" not in saved
    assert "recommendation" not in saved


def test_run_agent_daily_analyst_false_no_artifact(tmp_path):
    """analyst=False (default) → analyst_recommendation.json is NOT written."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "crash_venture_v2" / "run_test_a03"
    mock_run = _make_mock_run_result(run_dir, run_id="run_test_a03")

    with patch("run_daily_v2.run_daily_core", return_value=mock_run):
        packet = run_agent_daily(runs_root=runs_root, analyst=False)

    analyst_path = run_dir / "artifacts" / "analyst_recommendation.json"
    assert not analyst_path.exists()
    assert "_analyst" not in packet


def test_run_agent_daily_analyst_failure_is_nonfatal(tmp_path):
    """If run_analyst returns error status, run_agent_daily still completes."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "crash_venture_v2" / "run_test_a04"
    mock_run = _make_mock_run_result(run_dir, run_id="run_test_a04")

    error_result = {
        "status": "error",
        "recommendation": None,
        "confidence": None,
        "rationale": "",
        "flags": [],
        "raw_response": "",
        "error": "OPENAI_API_KEY environment variable not set",
        "ts_utc": "2026-03-25T10:00:01+00:00",
    }

    with patch("run_daily_v2.run_daily_core", return_value=mock_run), \
         patch("agent_daily.run_analyst", return_value=error_result):

        # Must not raise even when analyst errors
        packet = run_agent_daily(runs_root=runs_root, analyst=True)

    assert packet["schema_version"] == "2.0"
    assert packet["_analyst"]["status"] == "error"
    # Artifact is still written (error result is persisted)
    assert (run_dir / "artifacts" / "analyst_recommendation.json").exists()
