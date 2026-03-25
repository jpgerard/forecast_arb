"""
Patch 2 — Broker preflight and decision packet tests.

Covers:
- run_broker_preflight: SKIPPED / OK / BLOCKED / error tolerance
- build_decision_packet: schema, notes, top candidates
- run_daily_core importability
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _make_positions_json(path: Path, positions: list) -> None:
    _write_json(path, positions)


# ---------------------------------------------------------------------------
# run_broker_preflight
# ---------------------------------------------------------------------------

from forecast_arb.ops.preflight import run_broker_preflight


def test_preflight_skipped_when_no_csv(tmp_path):
    """ibkr_csv_path=None → status=SKIPPED, drift=None."""
    positions_path = tmp_path / "positions.json"
    _make_positions_json(positions_path, [])

    result = run_broker_preflight(
        positions_path=positions_path,
        fills_ledger_path=None,
        ibkr_csv_path=None,
        trade_outcomes_path=None,
    )

    assert result["status"] == "SKIPPED"
    assert result["drift"] is None
    assert "ts_utc" in result


def test_preflight_skipped_when_csv_absent(tmp_path):
    """ibkr_csv_path points to non-existent file → still SKIPPED."""
    result = run_broker_preflight(
        positions_path=tmp_path / "positions.json",
        fills_ledger_path=None,
        ibkr_csv_path=tmp_path / "nonexistent.csv",
        trade_outcomes_path=None,
    )

    assert result["status"] == "SKIPPED"
    assert result["drift"] is None


def test_preflight_ok_when_in_sync(tmp_path):
    """Mock diff returning in_sync=True → status=OK."""
    positions_path = tmp_path / "positions.json"
    _make_positions_json(positions_path, [])
    ibkr_csv_path = tmp_path / "ibkr.csv"
    ibkr_csv_path.write_text("Symbol,Quantity\n")  # minimal valid file

    mock_diff_result = {
        "ok": True,
        "in_sync": True,
        "ccc_count": 1,
        "ibkr_count": 1,
        "only_in_ccc": [],
        "only_in_ibkr": [],
        "qty_mismatches": [],
        "headline": "CCC and IBKR positions match",
        "errors": [],
    }

    _bd = "forecast_arb.allocator.broker_drift"
    with patch(f"{_bd}.load_ccc_positions", return_value=[]), \
         patch(f"{_bd}.load_ibkr_positions_from_csv", return_value=[]), \
         patch(f"{_bd}.normalize_ccc_spread_positions", return_value=[]), \
         patch(f"{_bd}.normalize_ibkr_spread_positions", return_value=[]), \
         patch(f"{_bd}.diff_ccc_vs_ibkr", return_value=mock_diff_result):
        result = run_broker_preflight(
            positions_path=positions_path,
            fills_ledger_path=None,
            ibkr_csv_path=ibkr_csv_path,
            trade_outcomes_path=None,
        )

    assert result["status"] == "OK"
    assert result["drift"] is not None
    assert result["drift"]["in_sync"] is True


def test_preflight_blocked_on_ibkr_only(tmp_path):
    """only_in_ibkr non-empty → status=BLOCKED."""
    positions_path = tmp_path / "positions.json"
    _make_positions_json(positions_path, [])
    ibkr_csv_path = tmp_path / "ibkr.csv"
    ibkr_csv_path.write_text("Symbol,Quantity\n")

    mock_diff_result = {
        "ok": True,
        "in_sync": False,
        "ccc_count": 0,
        "ibkr_count": 1,
        "only_in_ccc": [],
        "only_in_ibkr": [{"symbol": "SPY", "expiry": "20260402"}],
        "qty_mismatches": [],
        "headline": "1 position(s) in IBKR not in CCC",
        "errors": [],
    }

    _bd = "forecast_arb.allocator.broker_drift"
    with patch(f"{_bd}.load_ccc_positions", return_value=[]), \
         patch(f"{_bd}.load_ibkr_positions_from_csv", return_value=[]), \
         patch(f"{_bd}.normalize_ccc_spread_positions", return_value=[]), \
         patch(f"{_bd}.normalize_ibkr_spread_positions", return_value=[]), \
         patch(f"{_bd}.diff_ccc_vs_ibkr", return_value=mock_diff_result):
        result = run_broker_preflight(
            positions_path=positions_path,
            fills_ledger_path=None,
            ibkr_csv_path=ibkr_csv_path,
            trade_outcomes_path=None,
        )

    assert result["status"] == "BLOCKED"
    assert "IBKR not in CCC" in result["reason"] or "drift" in result["reason"].lower()


def test_preflight_errors_do_not_propagate(tmp_path):
    """
    Malformed positions.json and missing fills ledger must not raise.
    errors list should be populated, status should not be an exception.
    """
    positions_path = tmp_path / "positions.json"
    positions_path.write_text("{invalid json")  # deliberately malformed

    # Should not raise
    result = run_broker_preflight(
        positions_path=positions_path,
        fills_ledger_path=tmp_path / "no_fills.jsonl",
        ibkr_csv_path=None,
        trade_outcomes_path=None,
    )

    assert result["status"] == "SKIPPED"  # no CSV → SKIPPED regardless
    assert isinstance(result["errors"], list)
    # At least the inventory read should have logged an error
    assert len(result["errors"]) >= 1


def test_preflight_inventory_counts_open_positions(tmp_path):
    """Inventory counts crash_open and selloff_open from positions.json."""
    positions_path = tmp_path / "positions.json"
    _make_positions_json(positions_path, [
        {"regime": "crash", "qty_open": 1},
        {"regime": "crash", "qty_open": 1},
        {"regime": "selloff", "qty_open": 1},
        {"regime": "crash", "qty_open": 0},   # closed — should not count
    ])

    result = run_broker_preflight(
        positions_path=positions_path,
        fills_ledger_path=None,
        ibkr_csv_path=None,
        trade_outcomes_path=None,
    )

    assert result["inventory"]["crash_open"] == 2
    assert result["inventory"]["selloff_open"] == 1


# ---------------------------------------------------------------------------
# run_daily_core importability
# ---------------------------------------------------------------------------

def test_run_daily_core_importable():
    """run_daily_core can be imported and is callable (no sys.exit on import)."""
    # Ensure scripts/ is on the path for import
    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from run_daily_v2 import run_daily_core  # noqa: F401
    assert callable(run_daily_core)


# ---------------------------------------------------------------------------
# build_decision_packet
# ---------------------------------------------------------------------------

from forecast_arb.core.decision_packet import build_decision_packet


_REQUIRED_KEYS = {
    "schema_version", "ts_utc", "run", "broker_preflight",
    "top_candidates", "signals", "notes",
}
_REQUIRED_RUN_KEYS = {
    "run_id", "timestamp", "mode", "decision", "reason",
    "edge", "p_external", "p_implied", "confidence",
    "num_tickets", "submit_requested", "submit_executed",
}
_REQUIRED_SIGNALS_KEYS = {
    "p_external", "p_implied", "edge", "confidence", "gate_decision",
}


def test_decision_packet_schema_keys():
    """build_decision_packet returns all required top-level keys."""
    packet = build_decision_packet(run_dir=None, preflight=None)

    assert _REQUIRED_KEYS <= set(packet.keys())
    assert _REQUIRED_RUN_KEYS <= set(packet["run"].keys())
    assert _REQUIRED_SIGNALS_KEYS <= set(packet["signals"].keys())
    assert packet["schema_version"] == "2.0"
    assert isinstance(packet["notes"], list)
    assert isinstance(packet["top_candidates"], list)


def test_decision_packet_no_run_dir_gives_empty_run(tmp_path):
    """run_dir=None → run fields are None/defaults, top_candidates=[]."""
    packet = build_decision_packet(run_dir=None, preflight=None)

    assert packet["run"]["run_id"] is None
    assert packet["top_candidates"] == []
    assert packet["broker_preflight"] is None


def test_decision_packet_notes_broker_blocked():
    """preflight status=BLOCKED → BROKER_DRIFT_BLOCKED in notes."""
    preflight = {
        "status": "BLOCKED",
        "reason": "Broker drift detected: 1 spread(s) in IBKR not in CCC",
        "drift": None,
        "inventory": {},
        "pending": {},
        "positions_view": {},
        "errors": [],
        "ts_utc": "2026-03-25T10:00:00+00:00",
    }
    packet = build_decision_packet(run_dir=None, preflight=preflight)

    assert "BROKER_DRIFT_BLOCKED" in packet["notes"]


def test_decision_packet_notes_no_trade(tmp_path):
    """decision=NO_TRADE in run artifacts → NO_TRADE in notes."""
    run_dir = tmp_path / "run_001"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    _write_json(artifacts_dir / "final_decision.json", {
        "decision": "NO_TRADE",
        "reason": "LOW_EDGE",
        "submit_requested": False,
        "submit_executed": False,
    })

    packet = build_decision_packet(run_dir=run_dir, preflight=None)

    assert "NO_TRADE" in packet["notes"]
    assert packet["run"]["decision"] == "NO_TRADE"


def test_decision_packet_top_candidates_from_review_candidates(tmp_path):
    """review_candidates.json present → top candidates extracted correctly."""
    run_dir = tmp_path / "run_002"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    review_candidates = {
        "regimes": {
            "crash": {
                "candidates": [
                    {
                        "rank": 1,
                        "expiry": "20260402",
                        "strikes": {"long_put": 585.0, "short_put": 565.0},
                        "ev_per_dollar": 31.2,
                        "debit_per_contract": 37.0,
                        "candidate_id": "abc123",
                    },
                    {
                        "rank": 2,
                        "expiry": "20260402",
                        "strikes": {"long_put": 580.0, "short_put": 560.0},
                        "ev_per_dollar": 28.5,
                        "debit_per_contract": 40.0,
                        "candidate_id": "def456",
                    },
                ]
            }
        }
    }
    _write_json(artifacts_dir / "review_candidates.json", review_candidates)

    packet = build_decision_packet(run_dir=run_dir, preflight=None, max_candidates_per_regime=3)

    assert len(packet["top_candidates"]) == 2
    c1 = packet["top_candidates"][0]
    assert c1["regime"] == "crash"
    assert c1["rank"] == 1
    assert c1["expiry"] == "20260402"
    assert c1["long_strike"] == 585.0
    assert c1["short_strike"] == 565.0
    assert c1["ev_per_dollar"] == 31.2
    assert c1["candidate_id"] == "abc123"


def test_decision_packet_max_candidates_respected(tmp_path):
    """max_candidates_per_regime=1 limits to one candidate per regime."""
    run_dir = tmp_path / "run_003"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    review_candidates = {
        "regimes": {
            "crash": {
                "candidates": [
                    {"rank": 1, "expiry": "20260402",
                     "strikes": {"long_put": 585.0, "short_put": 565.0},
                     "ev_per_dollar": 31.2, "debit_per_contract": 37.0,
                     "candidate_id": "c1"},
                    {"rank": 2, "expiry": "20260402",
                     "strikes": {"long_put": 580.0, "short_put": 560.0},
                     "ev_per_dollar": 28.5, "debit_per_contract": 40.0,
                     "candidate_id": "c2"},
                ]
            }
        }
    }
    _write_json(artifacts_dir / "review_candidates.json", review_candidates)

    packet = build_decision_packet(run_dir=run_dir, preflight=None, max_candidates_per_regime=1)

    assert len(packet["top_candidates"]) == 1
    assert packet["top_candidates"][0]["rank"] == 1


def test_decision_packet_missing_artifacts_returns_empty_candidates(tmp_path):
    """No review_candidates.json or tickets.json → top_candidates=[]."""
    run_dir = tmp_path / "run_empty"
    (run_dir / "artifacts").mkdir(parents=True)

    packet = build_decision_packet(run_dir=run_dir, preflight=None)

    assert packet["top_candidates"] == []
