"""
tests/test_trading_adapter_v1.py
=================================
Test suite for Trading Adapter v1 (Tasks A–D + CLI).

Design rules:
  - All tests are deterministic (no live I/O, no network, no subprocess without mock).
  - Subprocess calls are mocked via unittest.mock.patch.
  - File reads in status_snapshot() are mocked via tmp files or patched loaders.
  - Tests cover required output contract keys, edge cases, and failure surfaces.
  - No live execution paths are tested (v1 has none).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from forecast_arb.adapter.trading_adapter import (
    AdapterResult,
    TradingAdapter,
    _combine_actionability,
)
from forecast_arb.adapter.parsers import (
    parse_preview_output,
    parse_report_output,
    build_status_headline,
    build_preview_headline,
    build_summarize_headline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(**kwargs) -> TradingAdapter:
    """Return an adapter with test-scoped config paths."""
    return TradingAdapter(
        policy_path=Path("configs/allocator_ccc_v1.yaml"),
        campaign_path=Path("configs/campaign_v1.yaml"),
        timeout_secs=30,
        **kwargs,
    )


def _required_keys() -> set:
    """Required top-level keys in every AdapterResult.to_dict()."""
    return {"ok", "actionability", "headline", "details", "raw_output", "errors"}


def _valid_actionability() -> set:
    return {
        "NO_ACTION",
        "REVIEW_ONLY",
        "CANDIDATE_AVAILABLE",
        "PAPER_ACTION_AVAILABLE",
        "ERROR",
    }


# ---------------------------------------------------------------------------
# Shared sample output strings (mimic real script stdout)
# ---------------------------------------------------------------------------

_REPORT_STDOUT_POSITIONS = textwrap.dedent("""\

    ════════════════════════════════════════════════════════════════════════
      CCC PORTFOLIO REPORT  —  2026-03-17 15:00 UTC
      Positions : runs/allocator/positions.json
      Ledger    : runs/allocator/allocator_commit_ledger.jsonl
    ════════════════════════════════════════════════════════════════════════

    ════════════════════════════════════════════════════════════════════════
      SECTION A — OPEN POSITIONS
    ────────────────────────────────────────────────────────────────────────
      UNDERLIER  REGIME   EXPIRY     STRIKES         QTY    DEBIT     MARK
      ────────────────────────────────────────────────────────────────────
      SPY        crash    20260417   585/565            3   $41.00    $52.00   2.50x   ...

      Total: 3 open position(s)
    ════════════════════════════════════════════════════════════════════════

    ════════════════════════════════════════════════════════════════════════
      SECTION B — PORTFOLIO SUMMARY
    ────────────────────────────────────────────────────────────────────────
      Crash open positions:           3  (soft target=1, hard cap=5)
      Selloff open positions:         0  (soft target=1, hard cap=3)
      Pending (committed-not-filled): 1  (crash=1, selloff=0)
      Crash premium at risk:          $123.00 / $500.00  (25%)
      Selloff premium at risk:        $0.00 / $300.00  (0%)
      Total premium at risk:          $123.00 / $750.00  (16%)
      YTD premium spent:              $245.00
      Annual convexity budget:        $30000.00
      Annual remaining budget:        $29755.00
    ════════════════════════════════════════════════════════════════════════

    ════════════════════════════════════════════════════════════════════════
      SECTION C — LATEST PLAN SUMMARY
    ────────────────────────────────────────────────────────────────────────
      Plan timestamp:                 2026-03-17 14:55:01
      Planned opens:                  0
      Planned closes:                 0
      Holds:                          3
      Gate reason:                    EV_BELOW_THRESHOLD
    ════════════════════════════════════════════════════════════════════════

      Report complete. All data read-only — no files were modified.
    ════════════════════════════════════════════════════════════════════════
""")

_REPORT_STDOUT_EMPTY = textwrap.dedent("""\

    ════════════════════════════════════════════════════════════════════════
      CCC PORTFOLIO REPORT  —  2026-03-17 15:00 UTC
    ════════════════════════════════════════════════════════════════════════

    ════════════════════════════════════════════════════════════════════════
      SECTION A — OPEN POSITIONS
    ────────────────────────────────────────────────────────────────────────
      (no open positions)
    ════════════════════════════════════════════════════════════════════════

    ════════════════════════════════════════════════════════════════════════
      SECTION B — PORTFOLIO SUMMARY
    ────────────────────────────────────────────────────────────────────────
      Crash open positions:           0
      Selloff open positions:         0
      Pending (committed-not-filled): 0  (crash=0, selloff=0)
      Premium at risk:                $0.00
      YTD premium spent:              $0.00
      Annual convexity budget:        N/A (not configured)
      Annual remaining budget:        N/A
    ════════════════════════════════════════════════════════════════════════

      Report complete.
""")

_PREVIEW_STDOUT_HOLD = textwrap.dedent("""\
    ════════════════════════════════════════════════════════════════════════
    OPERATOR CONSOLE: daily.py
    Mode: CAMPAIGN
    Config: configs/campaign_v1.yaml
    Execute: PAPER  QUOTE-ONLY (no commit)
    ════════════════════════════════════════════════════════════════════════

    ╔══════════════════════════════════════════════════════════════╗
    ║  DAILY RUN SUMMARY (CCC v1.8)                                ║
    ╠──────────────────────────────────────────────────────────────╣
    ║  CANDIDATES FILE: intents/allocator/recommended.json (seen 2)║
    ║  CCC PLAN: planned_opens=0 planned_closes=0 holds=3          ║
    ║  INVENTORY ACTUAL: crash=3 selloff=0                         ║
    ║  PENDING (committed-not-filled): crash=0 selloff=0           ║
    ║  CCC EXECUTE: mode=paper quote_only=true committed_new=0 committed_skipped=0 ║
    ╚══════════════════════════════════════════════════════════════╝
""")

_PREVIEW_STDOUT_OPEN = textwrap.dedent("""\
    ════════════════════════════════════════════════════════════════════════
    OPERATOR CONSOLE: daily.py
    Mode: CAMPAIGN
    ════════════════════════════════════════════════════════════════════════

    ╔══════════════════════════════════════════════════════════════╗
    ║  DAILY RUN SUMMARY (CCC v1.8)                                ║
    ╠──────────────────────────────────────────────────────────────╣
    ║  CANDIDATES FILE: intents/allocator/recommended.json (seen 3)║
    ║  CCC PLAN: planned_opens=1 planned_closes=0 holds=2          ║
    ║  INVENTORY ACTUAL: crash=2 selloff=0                         ║
    ║  PENDING (committed-not-filled): crash=0 selloff=0           ║
    ║  CCC EXECUTE: mode=paper quote_only=true committed_new=0 committed_skipped=0 ║
    ╚══════════════════════════════════════════════════════════════╝
      Quote-only preview: 1 intent(s) validated
""")


# ===========================================================================
# PART 1: Output contract tests
# ===========================================================================

class TestAdapterResultContract(unittest.TestCase):
    """AdapterResult must always contain the required top-level keys."""

    def test_to_dict_has_required_keys(self):
        r = AdapterResult(ok=True, actionability="NO_ACTION", headline="test")
        d = r.to_dict()
        self.assertEqual(_required_keys(), set(d.keys()))

    def test_actionability_valid_state(self):
        for state in _valid_actionability():
            r = AdapterResult(ok=True, actionability=state, headline="x")
            self.assertIn(r.actionability, _valid_actionability())

    def test_error_result_factory(self):
        r = AdapterResult.error_result(["boom"])
        self.assertFalse(r.ok)
        self.assertEqual(r.actionability, "ERROR")
        self.assertIn("boom", r.errors)
        self.assertIn("errors", r.to_dict())

    def test_to_dict_json_serializable(self):
        r = AdapterResult(
            ok=True,
            actionability="REVIEW_ONLY",
            headline="headline",
            details={"x": 1},
            raw_output="raw",
            errors=["warn"],
        )
        serialized = json.dumps(r.to_dict())  # should not raise
        d = json.loads(serialized)
        self.assertTrue(d["ok"])
        self.assertEqual(d["actionability"], "REVIEW_ONLY")


# ===========================================================================
# PART 2: status_snapshot() — Task A
# ===========================================================================

class TestStatusSnapshot(unittest.TestCase):
    """Task A: status_snapshot() — read-only from artifact files."""

    def _mock_ccc_report_module(self, positions=None, pending=None, ytd=0.0,
                                annual_bud=None, par_caps=None, inv_tc=None,
                                portfolio_par=None, actions_data=None):
        """Build a mock of the dynamically imported ccc_report module."""
        mock = MagicMock()
        mock.load_positions.return_value = positions or []
        mock.compute_pending_count.return_value = pending or {"crash": 0, "selloff": 0, "total": 0}
        mock.compute_ytd_spent.return_value = ytd
        mock.load_annual_budget.return_value = annual_bud or {"budget": None, "enabled": False}
        mock.load_premium_at_risk_caps.return_value = par_caps or {
            "crash": None, "selloff": None, "total": None, "enabled": False
        }
        mock.load_inventory_targets_and_caps.return_value = inv_tc or {
            "soft_targets": {}, "hard_caps": {}, "enabled": False
        }
        mock._compute_par.return_value = portfolio_par or {"crash": 0.0, "selloff": 0.0, "total": 0.0}
        mock.load_actions.return_value = actions_data
        return mock

    def _run_with_mock(self, mock_module):
        """Run status_snapshot() with a patched importlib.util."""
        adapter = _make_adapter()
        with patch("importlib.util.spec_from_file_location") as mock_spec_fn, \
             patch("importlib.util.module_from_spec") as mock_mod_fn:
            mock_spec = MagicMock()
            mock_spec_fn.return_value = mock_spec
            mock_mod_fn.return_value = mock_module
            mock_spec.loader.exec_module = MagicMock()
            result = adapter.status_snapshot()
        return result

    def test_required_keys_present(self):
        mock = self._mock_ccc_report_module()
        result = self._run_with_mock(mock)
        d = result.to_dict()
        self.assertEqual(_required_keys(), set(d.keys()))

    def test_no_positions_no_action(self):
        mock = self._mock_ccc_report_module()
        result = self._run_with_mock(mock)
        self.assertTrue(result.ok)
        self.assertEqual(result.actionability, "NO_ACTION")

    def test_crash_positions_review_only(self):
        positions = [{"regime": "crash", "qty_open": 1, "entry_debit_net": 45.0}]
        par = {"crash": 45.0, "selloff": 0.0, "total": 45.0}
        mock = self._mock_ccc_report_module(positions=positions, portfolio_par=par)
        result = self._run_with_mock(mock)
        self.assertTrue(result.ok)
        self.assertEqual(result.actionability, "REVIEW_ONLY")

    def test_details_has_crash_open(self):
        # positions.json has one entry per spread position (qty_open = contracts in that position)
        # crash_open counts position *entries* with regime=="crash", not qty_open
        positions = [
            {"regime": "crash",   "qty_open": 1, "entry_debit_net": 45.0},
            {"regime": "crash",   "qty_open": 1, "entry_debit_net": 43.0},
            {"regime": "selloff", "qty_open": 1, "entry_debit_net": 30.0},
        ]
        par = {"crash": 88.0, "selloff": 30.0, "total": 118.0}
        mock = self._mock_ccc_report_module(positions=positions, portfolio_par=par)
        result = self._run_with_mock(mock)
        self.assertEqual(result.details["crash_open"], 2)
        self.assertEqual(result.details["selloff_open"], 1)

    def test_details_has_premium_at_risk(self):
        positions = [{"regime": "crash", "qty_open": 3, "entry_debit_net": 41.0}]
        par = {"crash": 123.0, "selloff": 0.0, "total": 123.0}
        par_caps = {"crash": 500.0, "selloff": 300.0, "total": 750.0, "enabled": True}
        mock = self._mock_ccc_report_module(positions=positions, portfolio_par=par, par_caps=par_caps)
        result = self._run_with_mock(mock)
        self.assertAlmostEqual(result.details["par_crash"], 123.0)
        self.assertAlmostEqual(result.details["par_total"], 123.0)
        self.assertEqual(result.details["par_crash_cap"], 500.0)
        self.assertEqual(result.details["par_total_cap"], 750.0)

    def test_details_pending_counts(self):
        pending = {"crash": 1, "selloff": 0, "total": 1}
        mock = self._mock_ccc_report_module(pending=pending)
        result = self._run_with_mock(mock)
        self.assertEqual(result.details["pending_total"], 1)
        self.assertEqual(result.details["pending_crash"], 1)
        # pending alone makes it REVIEW_ONLY
        self.assertEqual(result.actionability, "REVIEW_ONLY")

    def test_annual_budget_remaining(self):
        annual = {"budget": 30000.0, "enabled": True}
        mock = self._mock_ccc_report_module(annual_bud=annual, ytd=245.0)
        result = self._run_with_mock(mock)
        self.assertAlmostEqual(result.details["ytd_spent"], 245.0)
        self.assertAlmostEqual(result.details["annual_remaining"], 30000.0 - 245.0)

    def test_soft_targets_and_hard_caps_in_details(self):
        inv_tc = {
            "soft_targets": {"crash": 1, "selloff": 1},
            "hard_caps": {"crash": 5, "selloff": 3},
            "enabled": True,
        }
        mock = self._mock_ccc_report_module(inv_tc=inv_tc)
        result = self._run_with_mock(mock)
        self.assertEqual(result.details["crash_soft_target"], 1)
        self.assertEqual(result.details["crash_hard_cap"], 5)
        self.assertEqual(result.details["selloff_hard_cap"], 3)

    def test_latest_plan_from_actions_data(self):
        actions_data = {
            "timestamp_utc": "2026-03-17T14:55:01Z",
            "actions": [
                {"type": "HOLD"},
                {"type": "HOLD"},
                {"type": "HOLD"},
            ],
            "open_gate_trace": {"reason": "EV_BELOW_THRESHOLD"},
        }
        mock = self._mock_ccc_report_module(actions_data=actions_data)
        result = self._run_with_mock(mock)
        self.assertEqual(result.details["latest_plan_holds"], 3)
        self.assertEqual(result.details["latest_plan_gate_reason"], "EV_BELOW_THRESHOLD")

    def test_headline_mentions_crash_positions(self):
        positions = [{"regime": "crash", "qty_open": 3, "entry_debit_net": 41.0}]
        par = {"crash": 123.0, "selloff": 0.0, "total": 123.0}
        mock = self._mock_ccc_report_module(positions=positions, portfolio_par=par)
        result = self._run_with_mock(mock)
        self.assertIn("3", result.headline)
        self.assertIn("crash", result.headline.lower())

    def test_error_on_loader_failure(self):
        """When ccc_report helpers raise, status_snapshot returns ERROR."""
        adapter = _make_adapter()
        with patch("importlib.util.spec_from_file_location") as mock_spec_fn:
            mock_spec_fn.side_effect = RuntimeError("import failed")
            result = adapter.status_snapshot()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(len(result.errors) > 0)


# ===========================================================================
# PART 3: report_snapshot() — Task C
# ===========================================================================

class TestReportSnapshot(unittest.TestCase):
    """Task C: report_snapshot() — shells out to ccc_report.py."""

    def _run_report(self, stdout="", returncode=0):
        adapter = _make_adapter()
        mock_proc = MagicMock()
        mock_proc.stdout = stdout
        mock_proc.stderr = ""
        mock_proc.returncode = returncode
        with patch("subprocess.run", return_value=mock_proc), \
             patch("pathlib.Path.exists", return_value=True):
            result = adapter.report_snapshot()
        return result

    def test_required_keys_in_result(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertEqual(_required_keys(), set(result.to_dict().keys()))

    def test_premium_at_risk_parsed(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.details.get("par_total"))
        self.assertAlmostEqual(result.details["par_total"], 123.0)
        self.assertAlmostEqual(result.details["par_crash"], 123.0)

    def test_open_counts_parsed(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertEqual(result.details["crash_open"], 3)
        self.assertEqual(result.details["selloff_open"], 0)

    def test_pending_parsed(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertEqual(result.details["pending_total"], 1)
        self.assertEqual(result.details["pending_crash"], 1)
        self.assertEqual(result.details["pending_selloff"], 0)

    def test_sections_found(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        sections = result.details.get("sections_found", [])
        self.assertIn("A", sections)
        self.assertIn("B", sections)
        self.assertIn("C", sections)

    def test_empty_portfolio_no_action(self):
        result = self._run_report(stdout=_REPORT_STDOUT_EMPTY)
        self.assertTrue(result.ok)
        self.assertEqual(result.actionability, "NO_ACTION")
        self.assertEqual(result.details["crash_open"], 0)
        self.assertEqual(result.details["total_open"], 0)

    def test_positions_make_review_only(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertEqual(result.actionability, "REVIEW_ONLY")

    def test_gate_reason_in_details(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertEqual(result.details.get("gate_reason"), "EV_BELOW_THRESHOLD")

    def test_error_on_nonzero_returncode(self):
        result = self._run_report(stdout="", returncode=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(len(result.errors) > 0)

    def test_missing_script_returns_error(self):
        adapter = _make_adapter()
        with patch("pathlib.Path.exists", return_value=False):
            result = adapter.report_snapshot()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(any("not found" in e.lower() or "ccc_report" in e.lower()
                           for e in result.errors))

    def test_raw_output_captured(self):
        result = self._run_report(stdout=_REPORT_STDOUT_POSITIONS)
        self.assertEqual(result.raw_output, _REPORT_STDOUT_POSITIONS)


# ===========================================================================
# PART 4: preview_daily_cycle() — Task B
# ===========================================================================

class TestPreviewDailyCycle(unittest.TestCase):
    """Task B: preview_daily_cycle() — shells out to scripts/daily.py."""

    def _run_preview(self, stdout="", returncode=0, stderr=""):
        adapter = _make_adapter()
        mock_proc = MagicMock()
        mock_proc.stdout = stdout
        mock_proc.stderr = stderr
        mock_proc.returncode = returncode
        with patch("subprocess.run", return_value=mock_proc), \
             patch("pathlib.Path.exists", return_value=True):
            result = adapter.preview_daily_cycle()
        return result

    def test_required_keys_present(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_HOLD)
        self.assertEqual(_required_keys(), set(result.to_dict().keys()))

    def test_hold_case_no_action(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_HOLD)
        self.assertTrue(result.ok)
        self.assertEqual(result.actionability, "NO_ACTION")
        self.assertEqual(result.details["planned_opens"], 0)
        self.assertEqual(result.details["holds"], 3)

    def test_open_case_paper_action_available(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_OPEN)
        self.assertTrue(result.ok)
        self.assertEqual(result.actionability, "PAPER_ACTION_AVAILABLE")
        self.assertEqual(result.details["planned_opens"], 1)
        self.assertEqual(result.details["quote_only_validated"], 1)

    def test_open_no_validation_candidate_available(self):
        # OPEN=1 but no quote-only validation line
        stdout_no_quote = _PREVIEW_STDOUT_OPEN.replace(
            "Quote-only preview: 1 intent(s) validated", ""
        )
        result = self._run_preview(stdout=stdout_no_quote)
        self.assertTrue(result.ok)
        self.assertEqual(result.actionability, "CANDIDATE_AVAILABLE")
        self.assertEqual(result.details["planned_opens"], 1)
        self.assertEqual(result.details["quote_only_validated"], 0)

    def test_error_on_nonzero_returncode(self):
        result = self._run_preview(stdout="", returncode=1, stderr="Connection refused")
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(any("returncode" not in e or "1" in e or "Connection" in e
                           for e in result.errors))

    def test_missing_campaign_file_returns_error(self):
        adapter = _make_adapter()
        with patch("pathlib.Path.exists", return_value=False):
            result = adapter.preview_daily_cycle()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(any("not found" in e.lower() for e in result.errors))

    def test_summary_box_detected(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_HOLD)
        self.assertTrue(result.details.get("summary_box_found", False))

    def test_headline_hold_contains_no_new_trade(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_HOLD)
        self.assertIn("No new trade", result.headline)

    def test_headline_open_mentions_open_planned(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_OPEN)
        headline_lower = result.headline.lower()
        self.assertTrue(
            "open" in headline_lower or "1" in result.headline,
            f"Expected open/1 in headline: {result.headline!r}",
        )

    def test_timeout_returns_error(self):
        adapter = _make_adapter()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)), \
             patch("pathlib.Path.exists", return_value=True):
            result = adapter.preview_daily_cycle()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(any("timed out" in e.lower() for e in result.errors))

    def test_raw_output_captured_on_success(self):
        result = self._run_preview(stdout=_PREVIEW_STDOUT_HOLD)
        self.assertEqual(result.raw_output, _PREVIEW_STDOUT_HOLD)


# ===========================================================================
# PART 5: summarize_latest() — Task D
# ===========================================================================

class TestSummarizeLatest(unittest.TestCase):
    """Task D: summarize_latest() combines status + report + preview."""

    def _make_ok_status(self, crash_open=0, par_total=0.0):
        return AdapterResult(
            ok=True,
            actionability="NO_ACTION" if crash_open == 0 else "REVIEW_ONLY",
            headline="Status headline",
            details={
                "crash_open": crash_open,
                "selloff_open": 0,
                "total_open": crash_open,
                "pending_crash": 0,
                "pending_selloff": 0,
                "pending_total": 0,
                "par_crash": par_total,
                "par_selloff": 0.0,
                "par_total": par_total,
                "par_crash_cap": 500.0 if crash_open > 0 else None,
                "par_total_cap": 750.0 if crash_open > 0 else None,
                "par_selloff_cap": None,
                "crash_soft_target": 1,
                "crash_hard_cap": 5,
                "selloff_soft_target": 1,
                "selloff_hard_cap": 3,
                "ytd_spent": 245.0,
                "annual_budget": 30000.0,
                "annual_remaining": 29755.0,
                "latest_plan_ts": "2026-03-17T14:55:00Z",
                "latest_plan_opens": 0,
                "latest_plan_closes": 0,
                "latest_plan_holds": crash_open,
                "latest_plan_gate_reason": "EV_BELOW_THRESHOLD" if crash_open > 0 else None,
            },
        )

    def _make_ok_report(self, crash_open=0):
        return AdapterResult(
            ok=True,
            actionability="NO_ACTION" if crash_open == 0 else "REVIEW_ONLY",
            headline="Report headline",
            details={
                "crash_open": crash_open,
                "selloff_open": 0,
                "total_open": crash_open,
                "pending_total": 0,
                "pending_crash": 0,
                "pending_selloff": 0,
                "par_crash": 123.0 if crash_open > 0 else None,
                "par_selloff": 0.0,
                "par_total": 123.0 if crash_open > 0 else 0.0,
                "planned_opens": 0,
                "planned_closes": 0,
                "holds": crash_open,
                "gate_reason": "EV_BELOW_THRESHOLD" if crash_open > 0 else None,
                "sections_found": ["A", "B", "C"],
            },
            raw_output=_REPORT_STDOUT_POSITIONS,
        )

    def _make_ok_preview(self, opens=0, validated=0, gate=None):
        act = "NO_ACTION" if opens == 0 else (
            "PAPER_ACTION_AVAILABLE" if validated > 0 else "CANDIDATE_AVAILABLE"
        )
        return AdapterResult(
            ok=True,
            actionability=act,
            headline="Preview headline",
            details={
                "planned_opens": opens,
                "planned_closes": 0,
                "holds": 3,
                "gate_reason": gate,
                "quote_only_validated": validated,
                "summary_box_found": True,
            },
        )

    def test_required_keys_present(self):
        adapter = _make_adapter()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status(3, 123.0)), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report(3)), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview(0, gate="EV_BELOW_THRESHOLD")):
            result = adapter.summarize_latest()
        self.assertEqual(_required_keys(), set(result.to_dict().keys()))

    def test_combined_details_has_sub_keys(self):
        adapter = _make_adapter()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status(3, 123.0)), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report(3)), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview(0)):
            result = adapter.summarize_latest()
        self.assertIn("status", result.details)
        self.assertIn("report", result.details)
        self.assertIn("preview", result.details)

    def test_no_positions_no_action_headline(self):
        adapter = _make_adapter()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status(0)), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report(0)), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview(0)):
            result = adapter.summarize_latest()
        self.assertTrue(result.ok)
        self.assertIn("No new trade", result.headline)

    def test_positions_with_gate_in_headline(self):
        adapter = _make_adapter()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status(3, 123.0)), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report(3)), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview(0, gate="EV_BELOW_THRESHOLD")):
            result = adapter.summarize_latest()
        # Should mention no new trade + EV gate
        self.assertIn("gate", result.headline.lower())
        self.assertIn("EV_BELOW_THRESHOLD", result.headline)

    def test_open_action_reflected_in_actionability(self):
        adapter = _make_adapter()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status(2, 90.0)), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report(2)), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview(1, 1)):
            result = adapter.summarize_latest()
        self.assertEqual(result.actionability, "PAPER_ACTION_AVAILABLE")

    def test_no_preview_flag_skips_subprocess(self):
        adapter = _make_adapter()
        preview_spy = MagicMock()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status()), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report()), \
             patch.object(adapter, "preview_daily_cycle", side_effect=preview_spy):
            result = adapter.summarize_latest(run_preview=False)
        preview_spy.assert_not_called()
        self.assertIn("preview", result.details)

    def test_stable_headline_type(self):
        """Headline is always a non-empty string."""
        adapter = _make_adapter()
        with patch.object(adapter, "status_snapshot", return_value=self._make_ok_status(3, 122.80)), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report(3)), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview(0)):
            result = adapter.summarize_latest()
        self.assertIsInstance(result.headline, str)
        self.assertTrue(len(result.headline) > 5)

    def test_error_status_makes_combined_failed(self):
        adapter = _make_adapter()
        bad_status = AdapterResult.error_result(["status failed"])
        ok_report  = self._make_ok_report(0)
        ok_preview = self._make_ok_preview(0)
        with patch.object(adapter, "status_snapshot", return_value=bad_status), \
             patch.object(adapter, "report_snapshot", return_value=ok_report), \
             patch.object(adapter, "preview_daily_cycle", return_value=ok_preview):
            result = adapter.summarize_latest()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertIn("status failed", result.errors)


# ===========================================================================
# PART 6: CLI --json output
# ===========================================================================

class TestCLIJsonOutput(unittest.TestCase):
    """CLI --json output must parse as valid JSON with required contract keys."""

    def _run_cli_json(self, command: str, adapter_method_results: Dict[str, AdapterResult]) -> dict:
        """
        Run the CLI main() function with --json and capture output.
        Patches TradingAdapter methods to return given results.
        """
        import io
        from contextlib import redirect_stdout
        import sys

        sys.argv = ["trading_adapter.py", command, "--json"]

        captured = io.StringIO()
        import scripts.trading_adapter as cli_mod

        # Patch all four methods
        with patch.object(
            TradingAdapter,
            "status_snapshot",
            return_value=adapter_method_results.get(
                "status",
                AdapterResult(ok=True, actionability="NO_ACTION", headline="ok")
            ),
        ), patch.object(
            TradingAdapter,
            "preview_daily_cycle",
            return_value=adapter_method_results.get(
                "preview",
                AdapterResult(ok=True, actionability="NO_ACTION", headline="ok")
            ),
        ), patch.object(
            TradingAdapter,
            "report_snapshot",
            return_value=adapter_method_results.get(
                "report",
                AdapterResult(ok=True, actionability="NO_ACTION", headline="ok")
            ),
        ), patch.object(
            TradingAdapter,
            "summarize_latest",
            return_value=adapter_method_results.get(
                "summarize",
                AdapterResult(ok=True, actionability="NO_ACTION", headline="ok",
                              details={"status": {}, "report": {}, "preview": {}})
            ),
        ):
            with redirect_stdout(captured):
                try:
                    cli_mod.main()
                except SystemExit:
                    pass

        output = captured.getvalue().strip()
        return json.loads(output)

    def _default_ok(self) -> AdapterResult:
        return AdapterResult(ok=True, actionability="NO_ACTION", headline="All clear.")

    def test_status_json_valid(self):
        d = self._run_cli_json("status", {"status": self._default_ok()})
        self.assertEqual(_required_keys(), set(d.keys()))
        self.assertTrue(d["ok"])

    def test_preview_json_valid(self):
        d = self._run_cli_json("preview", {"preview": self._default_ok()})
        self.assertEqual(_required_keys(), set(d.keys()))

    def test_report_json_valid(self):
        d = self._run_cli_json("report", {"report": self._default_ok()})
        self.assertEqual(_required_keys(), set(d.keys()))

    def test_summarize_json_valid(self):
        summary = AdapterResult(
            ok=True,
            actionability="REVIEW_ONLY",
            headline="Summary ok.",
            details={"status": {}, "report": {}, "preview": {}},
        )
        d = self._run_cli_json("summarize", {"summarize": summary})
        self.assertEqual(_required_keys(), set(d.keys()))
        self.assertIn("actionability", d)

    def test_json_output_is_valid_json_string(self):
        d = self._run_cli_json("status", {"status": self._default_ok()})
        # Verify round-trip
        self.assertEqual(json.loads(json.dumps(d)), d)


# ===========================================================================
# PART 7: Parsers unit tests
# ===========================================================================

class TestParsers(unittest.TestCase):
    """Unit tests for parsers module (pure functions — no I/O)."""

    def test_parse_preview_output_hold(self):
        parsed = parse_preview_output(_PREVIEW_STDOUT_HOLD, "")
        self.assertEqual(parsed["planned_opens"], 0)
        self.assertEqual(parsed["holds"], 3)
        self.assertEqual(parsed["crash_open"], 3)
        self.assertTrue(parsed["summary_box_found"])

    def test_parse_preview_output_open(self):
        parsed = parse_preview_output(_PREVIEW_STDOUT_OPEN, "")
        self.assertEqual(parsed["planned_opens"], 1)
        self.assertEqual(parsed["quote_only_validated"], 1)
        self.assertEqual(parsed["crash_open"], 2)

    def test_parse_report_output_positions(self):
        parsed = parse_report_output(_REPORT_STDOUT_POSITIONS)
        self.assertEqual(parsed["crash_open"], 3)
        self.assertEqual(parsed["selloff_open"], 0)
        self.assertAlmostEqual(parsed["par_crash"], 123.0)
        self.assertAlmostEqual(parsed["par_total"], 123.0)
        self.assertAlmostEqual(parsed["ytd_spent"], 245.0)
        self.assertEqual(parsed["planned_opens"], 0)
        self.assertEqual(parsed["holds"], 3)
        self.assertEqual(parsed["gate_reason"], "EV_BELOW_THRESHOLD")

    def test_parse_report_output_empty(self):
        parsed = parse_report_output(_REPORT_STDOUT_EMPTY)
        self.assertEqual(parsed["crash_open"], 0)
        self.assertEqual(parsed["total_open"], 0)
        self.assertAlmostEqual(parsed["ytd_spent"], 0.0)

    def test_build_status_headline_crash_only(self):
        h = build_status_headline(
            crash_open=3, selloff_open=0,
            par_crash=122.80, par_selloff=None, par_total=122.80,
            pending_total=0,
        )
        self.assertIn("3", h)
        self.assertIn("crash", h.lower())
        self.assertIn("122.80", h)

    def test_build_status_headline_no_positions(self):
        h = build_status_headline(
            crash_open=0, selloff_open=0,
            par_crash=None, par_selloff=None, par_total=None,
            pending_total=0,
        )
        self.assertIn("No crash", h)

    def test_build_preview_headline_no_action(self):
        h = build_preview_headline({"planned_opens": 0, "holds": 3, "gate_reason": None}, "NO_ACTION")
        self.assertIn("No new trade", h)

    def test_build_preview_headline_paper_action(self):
        h = build_preview_headline(
            {"planned_opens": 1, "planned_closes": 0, "holds": 2,
             "gate_reason": None, "quote_only_validated": 1},
            "PAPER_ACTION_AVAILABLE",
        )
        self.assertIn("1", h)

    def test_build_preview_headline_error(self):
        h = build_preview_headline({}, "ERROR")
        self.assertIn("failed", h.lower())


# ===========================================================================
# PART 8: _combine_actionability helper
# ===========================================================================

class TestCombineActionability(unittest.TestCase):

    def test_error_dominates(self):
        self.assertEqual(_combine_actionability("NO_ACTION", "ERROR", "REVIEW_ONLY"), "ERROR")

    def test_paper_action_over_candidate(self):
        self.assertEqual(
            _combine_actionability("CANDIDATE_AVAILABLE", "PAPER_ACTION_AVAILABLE"),
            "PAPER_ACTION_AVAILABLE",
        )

    def test_review_only_over_no_action(self):
        self.assertEqual(_combine_actionability("NO_ACTION", "REVIEW_ONLY"), "REVIEW_ONLY")

    def test_same_state(self):
        self.assertEqual(_combine_actionability("NO_ACTION", "NO_ACTION"), "NO_ACTION")

    def test_single_state(self):
        self.assertEqual(_combine_actionability("CANDIDATE_AVAILABLE"), "CANDIDATE_AVAILABLE")


# ===========================================================================
# PART 9: Adapter failure surfaces cleanly when scripts/files are missing
# ===========================================================================

class TestFailureSurfaces(unittest.TestCase):

    def test_status_snapshot_file_missing_returns_error(self):
        """status_snapshot gracefully handles missing files via importlib error."""
        adapter = _make_adapter()
        with patch("importlib.util.spec_from_file_location", side_effect=FileNotFoundError("no file")):
            result = adapter.status_snapshot()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(len(result.errors) > 0)

    def test_report_snapshot_ccc_report_missing(self):
        adapter = _make_adapter()
        with patch("pathlib.Path.exists", return_value=False):
            result = adapter.report_snapshot()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")

    def test_preview_daily_cycle_script_missing(self):
        adapter = _make_adapter()
        with patch("pathlib.Path.exists", return_value=False):
            result = adapter.preview_daily_cycle()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")
        self.assertTrue(any("not found" in e.lower() for e in result.errors))

    def test_preview_subprocess_exception_returns_error(self):
        adapter = _make_adapter()
        with patch("subprocess.run", side_effect=OSError("cannot fork")), \
             patch("pathlib.Path.exists", return_value=True):
            result = adapter.preview_daily_cycle()
        self.assertFalse(result.ok)
        self.assertEqual(result.actionability, "ERROR")

    def test_errors_list_always_present(self):
        """errors key must always be a list, even on success."""
        r = AdapterResult(ok=True, actionability="NO_ACTION", headline="headline")
        self.assertIsInstance(r.errors, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
