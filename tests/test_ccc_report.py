"""
Tests for scripts/ccc_report.py

Tests are deterministic and read from tmp_path fixtures only.
No real ledger files are touched.

Coverage:
  - load_positions: list format, dict format, missing file, bad JSON
  - compute_ytd_spent: fixtures with current-year and other-year entries
  - compute_pending_count: commit + fills ledger logic
  - compute_premium_at_risk: entry_debit × qty_open, net vs gross
  - _fmt_strikes: list format, dict format, edge cases
  - load_annual_budget: present, absent, disabled (inf)
  - load_actions: present HOLD plan, present OPEN plan, missing file
  - run_report end-to-end: no crash, key sections in output
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Import helpers from the report script
# ---------------------------------------------------------------------------

# Ensure the scripts/ directory is importable by inserting project root
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ccc_report import (
    _fmt_multiple,
    _fmt_strikes,
    compute_pending_count,
    compute_premium_at_risk,
    compute_ytd_spent,
    load_actions,
    load_annual_budget,
    load_positions,
    run_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _current_year() -> int:
    return datetime.now(timezone.utc).year


# ===========================================================================
# load_positions
# ===========================================================================

class TestLoadPositions:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_positions(tmp_path / "nonexistent.json") == []

    def test_list_format(self, tmp_path):
        p = tmp_path / "positions.json"
        positions = [
            {"trade_id": "T1", "underlier": "SPY", "regime": "crash", "qty_open": 1},
        ]
        _write_json(p, positions)
        result = load_positions(p)
        assert len(result) == 1
        assert result[0]["trade_id"] == "T1"

    def test_dict_with_positions_key(self, tmp_path):
        p = tmp_path / "positions.json"
        _write_json(p, {"positions": [{"trade_id": "T2"}], "meta": "x"})
        result = load_positions(p)
        assert len(result) == 1
        assert result[0]["trade_id"] == "T2"

    def test_bad_json_returns_empty(self, tmp_path):
        p = tmp_path / "positions.json"
        p.write_text("NOT VALID JSON", encoding="utf-8")
        assert load_positions(p) == []

    def test_empty_list(self, tmp_path):
        p = tmp_path / "positions.json"
        _write_json(p, [])
        assert load_positions(p) == []


# ===========================================================================
# compute_ytd_spent
# ===========================================================================

class TestComputeYtdSpent:
    def test_empty_ledger_returns_zero(self, tmp_path):
        p = tmp_path / "commit.jsonl"
        p.write_text("", encoding="utf-8")
        assert compute_ytd_spent(p) == 0.0

    def test_missing_ledger_returns_zero(self, tmp_path):
        assert compute_ytd_spent(tmp_path / "nonexistent.jsonl") == 0.0

    def test_sums_current_year_opens_only(self, tmp_path):
        cyr = _current_year()
        p = tmp_path / "commit.jsonl"
        _write_jsonl(p, [
            {"action": "OPEN",  "date": f"{cyr}-01-15", "premium_spent": 65.0},
            {"action": "OPEN",  "date": f"{cyr}-02-10", "premium_spent": 80.0},
            {"action": "OPEN",  "date": f"{cyr-1}-12-01", "premium_spent": 999.0},  # last year
            {"action": "DAILY_SUMMARY", "date": f"{cyr}-03-01", "premium_spent": 0.0},
        ])
        result = compute_ytd_spent(p)
        assert result == pytest.approx(145.0, abs=0.01)

    def test_excludes_previous_year(self, tmp_path):
        cyr = _current_year()
        p = tmp_path / "commit.jsonl"
        _write_jsonl(p, [
            {"action": "OPEN", "date": f"{cyr-1}-11-01", "premium_spent": 1000.0},
        ])
        assert compute_ytd_spent(p) == 0.0


# ===========================================================================
# compute_pending_count
# ===========================================================================

class TestComputePendingCount:
    def test_empty_both_returns_zero(self, tmp_path):
        c = tmp_path / "commit.jsonl"
        f = tmp_path / "fills.jsonl"
        c.write_text("", encoding="utf-8")
        f.write_text("", encoding="utf-8")
        result = compute_pending_count(c, f)
        assert result == {"crash": 0, "selloff": 0, "total": 0}

    def test_missing_files_return_zero(self, tmp_path):
        result = compute_pending_count(
            tmp_path / "nope.jsonl",
            tmp_path / "nope2.jsonl",
        )
        assert result["total"] == 0

    def test_all_pending_when_no_fills(self, tmp_path):
        c = tmp_path / "commit.jsonl"
        f = tmp_path / "fills.jsonl"
        _write_jsonl(c, [
            {"action": "OPEN", "intent_id": "ID1", "regime": "crash"},
            {"action": "OPEN", "intent_id": "ID2", "regime": "selloff"},
        ])
        f.write_text("", encoding="utf-8")
        result = compute_pending_count(c, f)
        assert result["crash"] == 1
        assert result["selloff"] == 1
        assert result["total"] == 2

    def test_filled_intents_not_counted(self, tmp_path):
        c = tmp_path / "commit.jsonl"
        f2 = tmp_path / "fills.jsonl"
        _write_jsonl(c, [
            {"action": "OPEN", "intent_id": "ID1", "regime": "crash"},
            {"action": "OPEN", "intent_id": "ID2", "regime": "crash"},
        ])
        _write_jsonl(f2, [
            {"event_type": "POSITION_OPENED", "intent_id": "ID1"},
        ])
        result = compute_pending_count(c, f2)
        assert result["crash"] == 1   # ID2 still pending
        assert result["total"] == 1

    def test_all_filled_returns_zero_pending(self, tmp_path):
        c = tmp_path / "commit.jsonl"
        f2 = tmp_path / "fills.jsonl"
        _write_jsonl(c, [
            {"action": "OPEN", "intent_id": "ID1", "regime": "crash"},
        ])
        _write_jsonl(f2, [
            {"event_type": "POSITION_OPENED", "intent_id": "ID1"},
        ])
        result = compute_pending_count(c, f2)
        assert result["total"] == 0


# ===========================================================================
# compute_premium_at_risk
# ===========================================================================

class TestComputePremiumAtRisk:
    def test_empty_positions_returns_zero(self):
        assert compute_premium_at_risk([]) == 0.0

    def test_single_position(self):
        pos = [{"entry_debit": 65.0, "qty_open": 1}]
        assert compute_premium_at_risk(pos) == pytest.approx(65.0)

    def test_multiple_positions(self):
        positions = [
            {"entry_debit": 65.0, "qty_open": 1},
            {"entry_debit": 80.0, "qty_open": 2},
        ]
        # 65*1 + 80*2 = 225
        assert compute_premium_at_risk(positions) == pytest.approx(225.0)

    def test_prefers_entry_debit_net(self):
        pos = [{"entry_debit": 100.0, "entry_debit_net": 70.0, "qty_open": 1}]
        assert compute_premium_at_risk(pos) == pytest.approx(70.0)

    def test_skips_positions_with_no_debit(self):
        positions = [
            {"qty_open": 1},                         # no debit field
            {"entry_debit": 50.0, "qty_open": 1},
        ]
        assert compute_premium_at_risk(positions) == pytest.approx(50.0)

    def test_skips_zero_qty(self):
        pos = [{"entry_debit": 100.0, "qty_open": 0}]
        assert compute_premium_at_risk(pos) == 0.0


# ===========================================================================
# Helper functions
# ===========================================================================

class TestFmtStrikes:
    def test_list_format(self):
        assert _fmt_strikes([540.0, 520.0]) == "540/520"

    def test_dict_long_short_put(self):
        result = _fmt_strikes({"long_put": 540.0, "short_put": 520.0})
        assert result == "540/520"

    def test_dict_long_short(self):
        result = _fmt_strikes({"long": 540.0, "short": 520.0})
        assert result == "540/520"

    def test_empty_list_returns_str(self):
        result = _fmt_strikes([])
        assert isinstance(result, str)

    def test_none_returns_str(self):
        result = _fmt_strikes(None)
        assert isinstance(result, str)


class TestFmtMultiple:
    def test_computes_multiple(self):
        pos = {"entry_debit": 65.0, "mark_mid": 130.0}
        result = _fmt_multiple(pos)
        assert "2.00x" in result

    def test_prefers_net_debit(self):
        pos = {"entry_debit": 100.0, "entry_debit_net": 65.0, "mark_mid": 130.0}
        result = _fmt_multiple(pos)
        assert "2.00x" in result

    def test_missing_mark_mid_returns_na(self):
        pos = {"entry_debit": 65.0}
        assert _fmt_multiple(pos) == "N/A"

    def test_zero_debit_returns_na(self):
        pos = {"entry_debit": 0.0, "mark_mid": 130.0}
        assert _fmt_multiple(pos) == "N/A"


# ===========================================================================
# load_annual_budget
# ===========================================================================

class TestLoadAnnualBudget:
    def test_missing_file_returns_disabled(self, tmp_path):
        result = load_annual_budget(tmp_path / "nofile.yaml")
        assert result["enabled"] is False
        assert result["budget"] is None

    def test_reads_budget_from_yaml(self, tmp_path):
        p = tmp_path / "policy.yaml"
        p.write_text(
            "policy_id: test\nbudgets:\n  annual_convexity_budget: 30000.0\n",
            encoding="utf-8",
        )
        result = load_annual_budget(p)
        assert result["enabled"] is True
        assert result["budget"] == pytest.approx(30000.0)

    def test_absent_key_returns_disabled(self, tmp_path):
        p = tmp_path / "policy.yaml"
        p.write_text("policy_id: test\nbudgets:\n  monthly_baseline: 1000.0\n",
                     encoding="utf-8")
        result = load_annual_budget(p)
        assert result["enabled"] is False


# ===========================================================================
# load_actions
# ===========================================================================

class TestLoadActions:
    def test_missing_returns_none(self, tmp_path):
        assert load_actions(tmp_path / "nope.json") is None

    def test_loads_actions_json(self, tmp_path):
        p = tmp_path / "actions.json"
        data = {"timestamp_utc": "2026-03-07T12:00:00Z", "actions": [{"type": "HOLD"}]}
        _write_json(p, data)
        result = load_actions(p)
        assert result is not None
        assert result["timestamp_utc"] == "2026-03-07T12:00:00Z"

    def test_bad_json_returns_none(self, tmp_path):
        p = tmp_path / "actions.json"
        p.write_text("INVALID", encoding="utf-8")
        assert load_actions(p) is None


# ===========================================================================
# End-to-end: run_report prints expected sections
# ===========================================================================

class TestRunReportEndToEnd:
    def _capture_report(self, **kwargs) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_report(**kwargs)
        return buf.getvalue()

    def test_runs_with_all_missing_files(self, tmp_path):
        """Report should complete without error when all source files are absent."""
        out = self._capture_report(
            positions_path=tmp_path / "positions.json",
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        assert "SECTION A" in out
        assert "SECTION B" in out
        assert "SECTION C" in out

    def test_positions_shown_in_section_a(self, tmp_path):
        positions = [
            {
                "trade_id": "T1", "underlier": "SPY", "regime": "crash",
                "expiry": "20261120", "strikes": [540.0, 520.0],
                "qty_open": 1, "entry_debit": 65.0, "mark_mid": 90.0,
            }
        ]
        pos_path = tmp_path / "positions.json"
        _write_json(pos_path, positions)

        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        assert "SPY" in out
        assert "crash" in out
        assert "540/520" in out

    def test_ytd_and_budget_in_section_b(self, tmp_path):
        cyr = _current_year()
        commit_path = tmp_path / "commit.jsonl"
        _write_jsonl(commit_path, [
            {"action": "OPEN", "date": f"{cyr}-02-01", "premium_spent": 120.0,
             "regime": "crash", "intent_id": "IX1"},
        ])
        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text(
            "policy_id: test\nbudgets:\n  annual_convexity_budget: 30000.0\n",
            encoding="utf-8",
        )
        out = self._capture_report(
            positions_path=tmp_path / "positions.json",
            commit_ledger_path=commit_path,
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=policy_path,
        )
        assert "YTD premium spent:" in out
        assert "$120.00" in out
        assert "Annual convexity budget:" in out
        assert "$30,000.00" in out

    def test_plan_section_c_with_hold(self, tmp_path):
        actions_path = tmp_path / "actions.json"
        _write_json(actions_path, {
            "timestamp_utc": "2026-03-07T12:00:00Z",
            "actions": [{"type": "HOLD", "reason_codes": ["EV_BELOW_THRESHOLD"]}],
            "open_gate_trace": {
                "reason": "ALL_REJECTED:EV_BELOW_THRESHOLD",
                "rejection_reasons_top": {"EV_BELOW_THRESHOLD": 3},
                "budget_blocked": False,
            },
        })
        out = self._capture_report(
            positions_path=tmp_path / "positions.json",
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=actions_path,
            policy_path=tmp_path / "policy.yaml",
        )
        assert "SECTION C" in out
        assert "ALL_REJECTED:EV_BELOW_THRESHOLD" in out
        assert "Holds:" in out

    def test_no_plan_suppresses_section_c(self, tmp_path):
        out = self._capture_report(
            positions_path=tmp_path / "positions.json",
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
            show_plan=False,
        )
        assert "SECTION A" in out
        assert "SECTION B" in out
        assert "SECTION C" not in out

    def test_report_marks_read_only(self, tmp_path):
        """Output must contain the read-only confirmation line."""
        out = self._capture_report(
            positions_path=tmp_path / "positions.json",
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        assert "read-only" in out.lower()

    def test_premium_at_risk_computed(self, tmp_path):
        positions = [
            {"underlier": "SPY", "regime": "crash", "expiry": "20261120",
             "strikes": [540.0, 520.0], "qty_open": 2, "entry_debit": 65.0,
             "mark_mid": 80.0},
        ]
        pos_path = tmp_path / "positions.json"
        _write_json(pos_path, positions)
        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        # 65 * 2 = 130
        assert "$130.00" in out

    def test_does_not_mutate_any_file(self, tmp_path):
        """No new files should be created in tmp_path by run_report."""
        before = set(tmp_path.iterdir())
        self._capture_report(
            positions_path=tmp_path / "positions.json",
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        after = set(tmp_path.iterdir())
        assert after == before, "run_report must not create any files"

    def test_multiple_positions_table_rows(self, tmp_path):
        positions = [
            {"underlier": "SPY", "regime": "crash",   "expiry": "20261120",
             "strikes": [540.0, 520.0], "qty_open": 1, "entry_debit": 65.0, "mark_mid": 90.0},
            {"underlier": "SPY", "regime": "selloff",  "expiry": "20261120",
             "strikes": [560.0, 545.0], "qty_open": 1, "entry_debit": 45.0, "mark_mid": 55.0},
        ]
        pos_path = tmp_path / "positions.json"
        _write_json(pos_path, positions)
        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        assert "crash" in out
        assert "selloff" in out
        assert "2 open position(s)" in out
        # Section B crash / selloff counts
        assert "Crash open positions:" in out
        assert "Selloff open positions:" in out
