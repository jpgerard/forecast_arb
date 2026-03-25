"""
Patch 3 — Agent daily workflow tests.

Covers:
- render_operator_summary: schema mismatch raises ValueError
- render_operator_summary: valid packet renders all sections
- render_operator_summary: BLOCKED preflight includes drift details
- render_operator_summary: missing candidates renders placeholder
- render_operator_summary: run_dir=None renders artifact placeholder
- run_agent_daily: returns a valid schema_version "2.0" packet
- run_agent_daily: writes operator_summary.json and operator_summary.md
- run_agent_daily: writes agent_last_run.json with correct fields
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# render_operator_summary tests
# ---------------------------------------------------------------------------

from forecast_arb.ops.summary import render_operator_summary, SUPPORTED_SCHEMA_VERSION


def _minimal_packet(**overrides):
    """Build a minimal valid schema_version '2.0' packet."""
    base = {
        "schema_version": "2.0",
        "ts_utc": "2026-03-25T10:00:00+00:00",
        "run": {
            "run_id": "run_001",
            "timestamp": "2026-03-25T10:00:00+00:00",
            "mode": "live",
            "decision": "NO_TRADE",
            "reason": "LOW_EDGE",
            "edge": 0.12,
            "p_external": 0.08,
            "p_implied": 0.05,
            "confidence": 0.6,
            "num_tickets": 0,
            "submit_requested": False,
            "submit_executed": False,
        },
        "broker_preflight": None,
        "top_candidates": [],
        "signals": {
            "p_external": 0.08,
            "p_implied": 0.05,
            "edge": 0.12,
            "confidence": 0.6,
            "gate_decision": "PASS",
        },
        "notes": [],
    }
    base.update(overrides)
    return base


def test_render_summary_raises_on_schema_mismatch():
    """render_operator_summary raises ValueError for wrong schema_version."""
    bad_packet = _minimal_packet(schema_version="1.0")
    with pytest.raises(ValueError, match="unsupported schema_version"):
        render_operator_summary(bad_packet)


def test_render_summary_raises_on_missing_schema_version():
    """render_operator_summary raises ValueError when schema_version is absent."""
    packet = _minimal_packet()
    del packet["schema_version"]
    with pytest.raises(ValueError, match="unsupported schema_version"):
        render_operator_summary(packet)


def test_render_summary_valid_packet_contains_sections():
    """Valid packet renders all expected section headers."""
    packet = _minimal_packet()
    md = render_operator_summary(packet)

    assert "# Daily Operator Summary" in md
    assert "## Broker Preflight" in md
    assert "## Signals" in md
    assert "## Top Candidates" in md
    assert "## Run Status" in md
    assert "## Notes" in md
    assert "## Artifacts" in md


def test_render_summary_blocked_preflight_shows_drift():
    """BLOCKED preflight with only_in_ibkr shows drift table rows."""
    packet = _minimal_packet(
        broker_preflight={
            "status": "BLOCKED",
            "reason": "Broker drift detected: 1 spread(s) in IBKR not in CCC",
            "drift": {
                "in_sync": False,
                "only_in_ibkr": [{"symbol": "SPY", "expiry": "20260402", "qty": 1}],
                "qty_mismatches": [],
            },
            "inventory": {"crash_open": 1, "selloff_open": 0},
            "positions_view": {},
            "errors": [],
            "ts_utc": "2026-03-25T10:00:00+00:00",
        }
    )
    md = render_operator_summary(packet)

    assert "BLOCKED" in md
    assert "In IBKR, not in CCC" in md
    assert "SPY" in md
    assert "20260402" in md


def test_render_summary_no_candidates_shows_placeholder():
    """Empty top_candidates renders the 'No candidates.' placeholder."""
    packet = _minimal_packet(top_candidates=[])
    md = render_operator_summary(packet)
    assert "*No candidates.*" in md


def test_render_summary_no_run_dir_shows_artifact_placeholder():
    """run_dir=None renders the 'Run dir not available.' placeholder."""
    packet = _minimal_packet()
    md = render_operator_summary(packet, run_dir=None)
    assert "*Run dir not available.*" in md


def test_render_summary_with_candidates_renders_table():
    """Top candidates list renders a markdown table row."""
    packet = _minimal_packet(
        top_candidates=[
            {
                "regime": "crash",
                "rank": 1,
                "expiry": "20260402",
                "long_strike": 585.0,
                "short_strike": 565.0,
                "ev_per_dollar": 31.2,
                "debit_per_contract": 37.0,
            }
        ]
    )
    md = render_operator_summary(packet)
    assert "crash" in md
    assert "585.0" in md
    assert "565.0" in md
    assert "$37.00" in md


# ---------------------------------------------------------------------------
# run_agent_daily tests
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from agent_daily import run_agent_daily


def _make_mock_run_result(run_dir: Path, run_id: str = "run_test_001") -> dict:
    """Build a fake run_daily_core() return value."""
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    # Write minimal final_decision.json so build_decision_packet can extract it
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


def test_run_agent_daily_returns_packet(tmp_path):
    """run_agent_daily returns a valid schema_version '2.0' decision packet."""
    run_dir = tmp_path / "runs" / "crash_venture_v2" / "run_test_001"
    mock_result = _make_mock_run_result(run_dir)

    with patch("run_daily_v2.run_daily_core", return_value=mock_result):
        packet = run_agent_daily(runs_root=tmp_path / "runs")

    assert packet["schema_version"] == "2.0"
    assert "run" in packet
    assert "signals" in packet
    assert "top_candidates" in packet
    assert isinstance(packet["notes"], list)


def test_run_agent_daily_writes_summary_artifacts(tmp_path):
    """run_agent_daily writes operator_summary.json and operator_summary.md."""
    run_dir = tmp_path / "runs" / "crash_venture_v2" / "run_test_002"
    mock_result = _make_mock_run_result(run_dir, run_id="run_test_002")

    with patch("run_daily_v2.run_daily_core", return_value=mock_result):
        run_agent_daily(runs_root=tmp_path / "runs")

    artifacts_dir = run_dir / "artifacts"
    assert (artifacts_dir / "operator_summary.json").exists()
    assert (artifacts_dir / "operator_summary.md").exists()

    # JSON must be the packet
    with open(artifacts_dir / "operator_summary.json", encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["schema_version"] == "2.0"

    # Markdown must contain section headers
    md = (artifacts_dir / "operator_summary.md").read_text()
    assert "# Daily Operator Summary" in md


def test_run_agent_daily_writes_agent_last_run(tmp_path):
    """run_agent_daily writes runs_root/agent_last_run.json with required fields."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "crash_venture_v2" / "run_test_003"
    mock_result = _make_mock_run_result(run_dir, run_id="run_test_003")

    with patch("run_daily_v2.run_daily_core", return_value=mock_result):
        run_agent_daily(runs_root=runs_root)

    last_run_path = runs_root / "agent_last_run.json"
    assert last_run_path.exists()

    with open(last_run_path, encoding="utf-8") as fh:
        record = json.load(fh)

    assert record["run_id"] == "run_test_003"
    assert Path(record["run_dir"]).is_absolute()
    assert "ts_utc" in record
    assert "preflight_status" in record
    assert "decision" in record
    assert "num_candidates" in record


def test_run_agent_daily_preflight_status_propagated(tmp_path):
    """Preflight SKIPPED status is propagated to agent_last_run.json."""
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "crash_venture_v2" / "run_test_004"
    mock_result = _make_mock_run_result(run_dir, run_id="run_test_004")

    with patch("run_daily_v2.run_daily_core", return_value=mock_result):
        # No ibkr_csv_path → preflight will be SKIPPED
        packet = run_agent_daily(
            runs_root=runs_root,
            ibkr_csv_path=None,
        )

    last_run_path = runs_root / "agent_last_run.json"
    with open(last_run_path, encoding="utf-8") as fh:
        record = json.load(fh)

    assert record["preflight_status"] == "SKIPPED"
