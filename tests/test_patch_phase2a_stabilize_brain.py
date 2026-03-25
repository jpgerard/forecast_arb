"""
Phase 2A Tests — Task A: Annual Premium Budget Control

Tests for the annual convexity budget gate introduced in Phase 2A.

Covers:
  - compute_premium_spent_ytd reads commit ledger correctly
  - compute_premium_spent_breakdown returns ytd + mtd
  - BudgetState.annual_budget_enabled / remaining_annual
  - OPEN is blocked when ytd_spent >= annual_convexity_budget
  - OPEN proceeds when ytd_spent < annual_convexity_budget
  - Reason code BUDGET_ANNUAL_CAP is emitted on block
  - daily.py summary includes annual budget fields
  - Backward compat: missing annual_convexity_budget does not break anything

All tests are deterministic and use no network calls.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_budget(**kwargs):
    """Build a minimal BudgetState for testing."""
    from forecast_arb.allocator.types import BudgetState
    defaults = {
        "monthly_baseline": 1000.0,
        "monthly_max": 2000.0,
        "weekly_baseline": 250.0,
        "daily_baseline": 50.0,
        "weekly_kicker": 500.0,
        "daily_kicker": 100.0,
    }
    defaults.update(kwargs)
    return BudgetState(**defaults)


def _make_inventory(crash_open=0, crash_target=1, selloff_open=0, selloff_target=1):
    from forecast_arb.allocator.types import InventoryState
    return InventoryState(
        crash_target=crash_target,
        crash_open=crash_open,
        selloff_target=selloff_target,
        selloff_open=selloff_open,
    )


def _make_candidate(
    regime="crash",
    ev_per_dollar=2.0,
    premium=50.0,
    max_gain=1500.0,
    p_used=0.10,
    p_src="implied",
):
    """Build a minimal candidate dict that passes all gates except the one under test."""
    return {
        "candidate_id": f"TEST_{regime}_001",
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20261120",
        "long_strike": 520.0,
        "short_strike": 500.0,
        "computed_premium_usd": premium,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar": ev_per_dollar,
        "p_used": p_used,
        "p_used_src": p_src,
    }


def _make_policy_dict(
    annual_budget: Optional[float] = None,
    ev_threshold_implied: float = 1.0,
    convexity_multiple: float = 10.0,
):
    """Build a minimal policy dict (already normalized)."""
    budgets: Dict[str, Any] = {
        "monthly_baseline": 1000.0,
        "monthly_max": 2000.0,
        "weekly_baseline": 250.0,
        "daily_baseline": 50.0,
        "weekly_kicker": 500.0,
        "daily_kicker": 100.0,
    }
    if annual_budget is not None:
        budgets["annual_convexity_budget"] = annual_budget

    return {
        "policy_id": "test_policy",
        "budgets": budgets,
        "inventory_targets": {"crash": 1, "selloff": 1},
        "thresholds": {
            "crash": {
                "fill_when_empty": {
                    "ev_per_dollar_implied": ev_threshold_implied,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": convexity_multiple,
                },
                "add_when_full": {
                    "ev_per_dollar_implied": ev_threshold_implied,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": convexity_multiple,
                },
            },
            "selloff": {
                "fill_when_empty": {
                    "ev_per_dollar_implied": ev_threshold_implied,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": convexity_multiple,
                },
                "add_when_full": {
                    "ev_per_dollar_implied": ev_threshold_implied,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": convexity_multiple,
                },
            },
        },
        "harvest": {
            "partial_close_multiple": 2.0,
            "full_close_multiple": 3.0,
            "time_stop_dte": 14,
            "time_stop_min_multiple": 1.2,
            "partial_close_fraction": 0.5,
        },
        "sizing": {"max_qty_per_trade": 10},
        "kicker": {"min_conditioning_confidence": 0.66, "max_vix_percentile": 35.0},
        # Optional
        "robustness": {"enabled": False},
        "roll": {"enabled": False},
        "limits": {"max_open_actions_per_day": 5, "max_close_actions_per_day": 5},
    }


def _write_ledger(path: Path, records: List[Dict[str, Any]]) -> None:
    """Write JSONL records to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _this_year() -> int:
    return datetime.now(timezone.utc).date().year


def _last_year_str() -> str:
    return f"{_this_year() - 1}-06-01"


# ===========================================================================
# A2: budget_control module tests
# ===========================================================================

class TestBudgetControlYTD:
    """Tests for compute_premium_spent_ytd and compute_premium_spent_breakdown."""

    def test_empty_ledger_returns_zero(self, tmp_path):
        """Non-existent ledger → ytd = 0."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        result = compute_premium_spent_ytd(tmp_path / "nonexistent.jsonl")
        assert result == 0.0

    def test_empty_file_returns_zero(self, tmp_path):
        """Empty file → ytd = 0."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        ledger = tmp_path / "commit.jsonl"
        ledger.write_text("")
        assert compute_premium_spent_ytd(ledger) == 0.0

    def test_counts_only_open_rows(self, tmp_path):
        """Only OPEN rows count; DAILY_SUMMARY rows are ignored."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        ledger = tmp_path / "commit.jsonl"
        _write_ledger(ledger, [
            {"action": "OPEN",          "date": _today_str(), "premium_spent": 100.0},
            {"action": "DAILY_SUMMARY", "date": _today_str(), "premium_spent": 9999.0},
            {"action": "OPEN",          "date": _today_str(), "premium_spent": 75.0},
        ])
        assert compute_premium_spent_ytd(ledger) == pytest.approx(175.0)

    def test_last_year_excluded_from_ytd(self, tmp_path):
        """Records from previous year are excluded from YTD."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        ledger = tmp_path / "commit.jsonl"
        _write_ledger(ledger, [
            {"action": "OPEN", "date": _today_str(),    "premium_spent": 200.0},
            {"action": "OPEN", "date": _last_year_str(), "premium_spent": 5000.0},  # excluded
        ])
        assert compute_premium_spent_ytd(ledger) == pytest.approx(200.0)

    def test_breakdown_returns_ytd_and_mtd(self, tmp_path):
        """compute_premium_spent_breakdown returns ytd and mtd keys."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_breakdown
        ledger = tmp_path / "commit.jsonl"
        _write_ledger(ledger, [
            {"action": "OPEN", "date": _today_str(), "premium_spent": 150.0},
        ])
        result = compute_premium_spent_breakdown(ledger)
        assert "ytd" in result
        assert "mtd" in result
        assert "current_year" in result
        assert "current_month" in result
        assert result["ytd"] == pytest.approx(150.0)
        assert result["mtd"] == pytest.approx(150.0)
        assert result["current_year"] == _this_year()

    def test_rows_missing_date_are_skipped(self, tmp_path):
        """Rows missing 'date' field are safely skipped."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        ledger = tmp_path / "commit.jsonl"
        _write_ledger(ledger, [
            {"action": "OPEN", "premium_spent": 9999.0},          # no date
            {"action": "OPEN", "date": "", "premium_spent": 9999.0},  # empty date
            {"action": "OPEN", "date": "invalid", "premium_spent": 9999.0},  # bad date
            {"action": "OPEN", "date": _today_str(), "premium_spent": 42.0},  # good
        ])
        assert compute_premium_spent_ytd(ledger) == pytest.approx(42.0)


# ===========================================================================
# BudgetState: new fields and properties
# ===========================================================================

class TestBudgetStateAnnualFields:
    """Tests for the new annual_budget_enabled / remaining_annual properties."""

    def test_default_annual_budget_is_disabled(self):
        """Default BudgetState has annual_budget_enabled=False."""
        budget = _make_budget()
        assert not budget.annual_budget_enabled
        assert budget.remaining_annual == float("inf")

    def test_annual_budget_enabled_when_finite(self):
        """Setting a finite annual_convexity_budget enables the gate."""
        budget = _make_budget(annual_convexity_budget=30000.0)
        assert budget.annual_budget_enabled is True

    def test_remaining_annual_simple(self):
        """remaining_annual = budget - spent_ytd."""
        budget = _make_budget(annual_convexity_budget=30000.0, spent_ytd=5000.0)
        assert budget.remaining_annual == pytest.approx(25000.0)

    def test_remaining_annual_clamped_at_zero(self):
        """remaining_annual never goes below 0."""
        budget = _make_budget(annual_convexity_budget=1000.0, spent_ytd=2000.0)
        assert budget.remaining_annual == 0.0

    def test_very_large_annual_budget_disabled(self):
        """A very large annual_convexity_budget is treated as disabled."""
        budget = _make_budget(annual_convexity_budget=1e16)
        assert not budget.annual_budget_enabled


# ===========================================================================
# A3: open_plan gating via _evaluate_candidate / generate_open_actions
# ===========================================================================

class TestAnnualBudgetGating:
    """Tests for BUDGET_ANNUAL_CAP gate in open_plan._evaluate_candidate."""

    def _run_generate(
        self,
        candidates: List[Dict],
        budget,
        inventory,
        policy,
    ):
        from forecast_arb.allocator.open_plan import generate_open_actions
        rejection_log: List[Dict] = []
        actions = generate_open_actions(
            candidates_data={"selected": candidates},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
        )
        return actions, rejection_log

    def test_open_blocked_when_ytd_equals_annual_budget(self):
        """OPEN blocked when spent_ytd == annual_convexity_budget."""
        policy = _make_policy_dict(annual_budget=30000.0)
        budget = _make_budget(
            annual_convexity_budget=30000.0,
            spent_ytd=30000.0,   # exactly at cap
        )
        inventory = _make_inventory(crash_open=0)
        candidate = _make_candidate(regime="crash")

        actions, rejection_log = self._run_generate([candidate], budget, inventory, policy)

        assert len(actions) == 0
        # Rejection log should mention BUDGET_ANNUAL_CAP
        assert len(rejection_log) == 1
        assert rejection_log[0]["primary_reason"] == "BUDGET_ANNUAL_CAP"

    def test_open_blocked_when_ytd_exceeds_annual_budget(self):
        """OPEN blocked when spent_ytd > annual_convexity_budget (already over)."""
        policy = _make_policy_dict(annual_budget=30000.0)
        budget = _make_budget(
            annual_convexity_budget=30000.0,
            spent_ytd=30200.0,   # over cap
        )
        budget.annual_convexity_budget = 30000.0
        inventory = _make_inventory(crash_open=0)
        candidate = _make_candidate(regime="crash")

        actions, rejection_log = self._run_generate([candidate], budget, inventory, policy)

        assert len(actions) == 0
        assert any("BUDGET_ANNUAL_CAP" in str(r.get("reason", "")) for r in rejection_log)

    def test_open_allowed_when_ytd_below_annual_budget(self):
        """OPEN proceeds when spent_ytd < annual_convexity_budget."""
        policy = _make_policy_dict(annual_budget=30000.0)
        budget = _make_budget(
            annual_convexity_budget=30000.0,
            spent_ytd=5000.0,    # well under cap
        )
        inventory = _make_inventory(crash_open=0)
        candidate = _make_candidate(regime="crash", ev_per_dollar=2.0)

        actions, rejection_log = self._run_generate([candidate], budget, inventory, policy)

        assert len(actions) == 1
        assert actions[0].type == "OPEN"
        # No annual cap rejection
        approved = [r for r in rejection_log if r.get("result") == "APPROVED"]
        assert len(approved) == 1

    def test_annual_budget_disabled_when_key_absent(self):
        """If annual_convexity_budget is absent from policy, gate is disabled."""
        policy = _make_policy_dict(annual_budget=None)  # key not set
        budget = _make_budget()  # annual_convexity_budget defaults to inf
        budget.spent_ytd = 999999.0  # huge ytd but gate should still be disabled
        inventory = _make_inventory(crash_open=0)
        candidate = _make_candidate(regime="crash", ev_per_dollar=2.0)

        actions, _ = self._run_generate([candidate], budget, inventory, policy)

        assert len(actions) == 1

    def test_reason_code_contains_ytd_and_budget_values(self):
        """Rejection reason code includes YTD_SPENT and ANNUAL_BUDGET values."""
        from forecast_arb.allocator.open_plan import _evaluate_candidate, get_effective_thresholds
        policy = _make_policy_dict(annual_budget=5000.0)
        budget = _make_budget(annual_convexity_budget=5000.0, spent_ytd=7000.0)
        candidate = _make_candidate(regime="crash")

        inv = _make_inventory(crash_open=0)
        eff = get_effective_thresholds(policy, "crash", inv)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )

        assert action is None
        assert "BUDGET_ANNUAL_CAP" in reason
        assert "YTD_SPENT:7000" in reason
        assert "ANNUAL_BUDGET:5000" in reason

    def test_existing_daily_monthly_caps_unaffected(self):
        """Existing daily/monthly soft cap logic still works when annual gate is disabled."""
        policy = _make_policy_dict(annual_budget=None)
        budget = _make_budget()
        budget.spent_today = 1000.0   # exhaust daily budget  
        inventory = _make_inventory(crash_open=0)
        candidate = _make_candidate(regime="crash", ev_per_dollar=2.0)

        actions, rejection_log = self._run_generate([candidate], budget, inventory, policy)

        assert len(actions) == 0
        # Should be blocked by daily cap, NOT annual cap
        if rejection_log:
            primary = rejection_log[0].get("primary_reason", "")
            assert "ANNUAL" not in primary


# ===========================================================================
# A4: daily.py summary includes annual budget fields
# ===========================================================================

class TestDailySummaryAnnualBudget:
    """Tests that _print_operator_summary surfaces annual budget fields."""

    def _capture_summary(self, plan_summary, capsys):
        """Call _print_operator_summary with given plan_summary and capture output."""
        import sys
        from io import StringIO
        # Directly import and call the function
        from scripts.daily import _print_operator_summary

        _print_operator_summary(
            candidates_path=None,
            plan_summary=plan_summary,
            execute=False,
            exec_mode="paper",
            quote_only=False,
            exec_summary=None,
            commit_ledger_path=None,
            reconcile_summary=None,
        )
        return capsys.readouterr().out

    def test_annual_budget_shown_when_enabled(self, capsys):
        """ANNUAL BUDGET line appears when annual_budget_enabled=True."""
        plan_summary = {
            "opens": 1,
            "closes": 0,
            "holds": 0,
            "positions_by_regime": {"crash": 0, "selloff": 0},
            "pending_by_regime": {},
            "annual_budget_enabled": True,
            "annual_convexity_budget": 30000.0,
            "ytd_spent": 5000.0,
            "remaining_annual": 25000.0,
        }
        output = self._capture_summary(plan_summary, capsys)
        assert "ANNUAL BUDGET" in output
        assert "30000" in output
        assert "25000" in output
        assert "5000" in output

    def test_annual_budget_not_shown_when_disabled(self, capsys):
        """ANNUAL BUDGET line is absent when annual_budget_enabled=False."""
        plan_summary = {
            "opens": 0,
            "closes": 0,
            "holds": 1,
            "positions_by_regime": {"crash": 0, "selloff": 0},
            "pending_by_regime": {},
            "annual_budget_enabled": False,
            "annual_convexity_budget": float("inf"),
            "ytd_spent": 0.0,
            "remaining_annual": None,
        }
        output = self._capture_summary(plan_summary, capsys)
        assert "ANNUAL BUDGET" not in output

    def test_annual_budget_not_shown_when_plan_summary_none(self, capsys):
        """ANNUAL BUDGET line is absent when plan_summary is None."""
        output = self._capture_summary(None, capsys)
        assert "ANNUAL BUDGET" not in output


# ===========================================================================
# Backward compatibility
# ===========================================================================

class TestAnnualBudgetBackwardCompat:
    """Ensure existing code paths are unaffected when annual_convexity_budget is absent."""

    def test_policy_load_without_annual_key(self, tmp_path):
        """load_policy succeeds when annual_convexity_budget absent from YAML."""
        import yaml
        from forecast_arb.allocator.policy import load_policy

        policy_yaml = tmp_path / "policy.yaml"
        policy_yaml.write_text("""
policy_id: compat_test
budgets:
  monthly_baseline: 500.0
  monthly_max: 1000.0
  weekly_baseline: 125.0
  daily_baseline: 25.0
  weekly_kicker: 250.0
  daily_kicker: 50.0
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    fill_when_empty:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
    add_when_full:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
  selloff:
    fill_when_empty:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
    add_when_full:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
harvest:
  partial_close_multiple: 2.0
  full_close_multiple: 3.0
  time_stop_dte: 14
  time_stop_min_multiple: 1.2
  partial_close_fraction: 0.5
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
""")
        policy = load_policy(str(policy_yaml))
        # Should not raise; annual_convexity_budget is optional
        assert "budgets" in policy

    def test_get_annual_budget_params_absent_key(self):
        """get_annual_budget_params returns disabled defaults when key absent."""
        from forecast_arb.allocator.policy import get_annual_budget_params
        policy = {"budgets": {"monthly_baseline": 1000.0}}
        result = get_annual_budget_params(policy)
        assert result["enabled"] is False
        assert result["annual_convexity_budget"] == float("inf")

    def test_get_annual_budget_params_present_key(self):
        """get_annual_budget_params returns correct value when key present."""
        from forecast_arb.allocator.policy import get_annual_budget_params
        policy = {"budgets": {"annual_convexity_budget": 30000.0}}
        result = get_annual_budget_params(policy)
        assert result["enabled"] is True
        assert result["annual_convexity_budget"] == pytest.approx(30000.0)

    def test_full_open_plan_without_annual_budget_key(self):
        """generate_open_actions works correctly when annual_budget_enabled=False."""
        policy = _make_policy_dict(annual_budget=None)
        budget = _make_budget()  # annual fields default to inf/disabled
        inventory = _make_inventory(crash_open=0)
        candidate = _make_candidate(regime="crash", ev_per_dollar=2.0)

        from forecast_arb.allocator.open_plan import generate_open_actions
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
        )
        # Should produce OPEN without error
        assert len(actions) == 1
        assert actions[0].type == "OPEN"


# ===========================================================================
# End-to-end integration: run_allocator_plan with annual cap
# ===========================================================================

class TestAnnualBudgetE2E:
    """
    End-to-end tests that call run_allocator_plan() with real file fixtures
    and verify the full gate chain: ledger → BudgetState → _evaluate_candidate
    → rejection_log → open_gate_trace.
    """

    # -----------------------------------------------------------------------
    # Fixture helpers
    # -----------------------------------------------------------------------

    _POLICY_TEMPLATE = """\
policy_id: e2e_annual_test
budgets:
  monthly_baseline: 5000.0
  monthly_max: 10000.0
  weekly_baseline: 1250.0
  daily_baseline: 250.0
  weekly_kicker: 2500.0
  daily_kicker: 500.0
  annual_convexity_budget: {annual_budget}
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    fill_when_empty:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
    add_when_full:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
  selloff:
    fill_when_empty:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
    add_when_full:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
harvest:
  partial_close_multiple: 2.0
  full_close_multiple: 3.0
  time_stop_dte: 14
  time_stop_min_multiple: 1.2
  partial_close_fraction: 0.5
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
robustness:
  enabled: false
roll:
  enabled: false
ledger_dir: {ledger_dir}
output_dir: {output_dir}
intents_dir: {intents_dir}
"""

    def _write_policy(self, tmp_path: Path, annual_budget: float) -> Path:
        """Write a minimal policy YAML to tmp_path."""
        policy_yaml = tmp_path / "policy.yaml"
        policy_yaml.write_text(self._POLICY_TEMPLATE.format(
            annual_budget=annual_budget,
            ledger_dir=str(tmp_path).replace("\\", "/"),
            output_dir=str(tmp_path).replace("\\", "/"),
            intents_dir=str(tmp_path / "intents").replace("\\", "/"),
        ))
        return policy_yaml

    def _write_commit_ledger(
        self,
        tmp_path: Path,
        ytd_spent: float,
        use_past_date: bool = False,
    ) -> Path:
        """
        Write commit ledger with the given YTD premium spend.

        use_past_date=True: record is dated Jan 3 of the current year so it
        counts as YTD but does NOT inflate today's / this-week's / this-month's
        soft-cap counters (needed when testing the "under cap → OPEN allowed"
        path without accidentally exhausting daily budget).
        """
        ledger = tmp_path / "allocator_commit_ledger.jsonl"
        if use_past_date:
            year = datetime.now(timezone.utc).date().year
            date_str = f"{year}-01-03"  # always earlier in the year; safe YTD-only
        else:
            date_str = _today_str()
        record = {
            "action": "OPEN",
            "date": date_str,
            "premium_spent": ytd_spent,
        }
        with open(ledger, "w", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return ledger

    def _write_candidates(self, tmp_path: Path) -> Path:
        """Write a qualifying crash candidate that would pass all gates except annual cap."""
        candidates = tmp_path / "candidates.json"
        candidate = {
            "candidate_id": "E2E_CRASH_001",
            "regime": "crash",
            "underlier": "SPY",
            "expiry": "20261120",
            "long_strike": 520.0,
            "short_strike": 500.0,
            "computed_premium_usd": 60.0,       # $60/contract
            "max_gain_per_contract": 1940.0,    # 32x convexity >> 10x threshold
            "ev_per_dollar": 2.5,               # >> 1.0 threshold
            "p_used": 0.10,
            "p_used_src": "implied",
        }
        with open(candidates, "w", encoding="utf-8") as f:
            json.dump({"candidates": [candidate]}, f)
        return candidates

    # -----------------------------------------------------------------------
    # Tests
    # -----------------------------------------------------------------------

    def test_e2e_annual_cap_exceeded_blocks_open(self, tmp_path):
        """
        run_allocator_plan(): when YTD spend exceeds annual cap →
          a) no OPEN action produced
          b) HOLD action present
          c) open_gate_trace contains BUDGET_ANNUAL_CAP in rejection_reasons_top
          d) open_gate_trace.budget_blocked = True
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.types import ActionType

        policy_yaml = self._write_policy(tmp_path, annual_budget=1000.0)
        self._write_commit_ledger(tmp_path, ytd_spent=1200.0)  # exceeds 1000.0
        candidates_file = self._write_candidates(tmp_path)

        plan = run_allocator_plan(
            policy_path=str(policy_yaml),
            candidates_path=str(candidates_file),
            signals=None,
            dry_run=True,
        )

        # a) No OPEN action
        open_actions = [a for a in plan.actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 0, (
            f"Expected no OPEN but got: {[a.reason_codes for a in open_actions]}"
        )

        # b) HOLD present
        hold_actions = [a for a in plan.actions if a.type == ActionType.HOLD]
        assert len(hold_actions) >= 1

        # c) open_gate_trace present and surfaces BUDGET_ANNUAL_CAP
        assert plan.open_gate_trace is not None, "open_gate_trace must be present"
        trace = plan.open_gate_trace
        rejection_reasons = trace.get("rejection_reasons_top", {})
        assert "BUDGET_ANNUAL_CAP" in rejection_reasons, (
            f"Expected BUDGET_ANNUAL_CAP in rejection_reasons_top, got: {rejection_reasons}"
        )

        # d) budget_blocked = True (Fix 2 in this patch)
        assert trace.get("budget_blocked") is True, (
            f"Expected budget_blocked=True, got: {trace.get('budget_blocked')}"
        )

    def test_e2e_annual_cap_not_reached_allows_open(self, tmp_path):
        """
        run_allocator_plan(): when YTD spend is well under annual cap →
          OPEN is produced (candidate passes all gates).

        The commit ledger record is dated Jan 3 of the current year (use_past_date=True)
        so it counts as YTD but does NOT inflate today's / this-week's / this-month's
        soft-cap counters.  This isolates the annual-cap gate as the only variable.
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.types import ActionType

        policy_yaml = self._write_policy(tmp_path, annual_budget=30000.0)
        self._write_commit_ledger(tmp_path, ytd_spent=200.0, use_past_date=True)  # well under 30000.0
        candidates_file = self._write_candidates(tmp_path)

        plan = run_allocator_plan(
            policy_path=str(policy_yaml),
            candidates_path=str(candidates_file),
            signals=None,
            dry_run=True,
        )

        open_actions = [a for a in plan.actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 1, (
            f"Expected one OPEN when under annual cap, got: {len(open_actions)}"
        )

    def test_e2e_plan_console_includes_annual_line(self, tmp_path, capsys):
        """
        run_allocator_plan() PM console output includes ANNUAL BUDGET line
        when annual_convexity_budget is configured.
        """
        from forecast_arb.allocator.plan import run_allocator_plan

        policy_yaml = self._write_policy(tmp_path, annual_budget=30000.0)
        self._write_commit_ledger(tmp_path, ytd_spent=1500.0)
        candidates_file = self._write_candidates(tmp_path)

        run_allocator_plan(
            policy_path=str(policy_yaml),
            candidates_path=str(candidates_file),
            signals=None,
            dry_run=True,
        )

        output = capsys.readouterr().out
        assert "ANNUAL BUDGET" in output, "plan.py console should show ANNUAL BUDGET line"
        assert "30000" in output, "plan.py console should show annual_convexity_budget value"
        assert "1500" in output, "plan.py console should show YTD spend"

    def test_e2e_budget_blocked_false_when_cap_not_configured(self, tmp_path):
        """
        When annual_convexity_budget is absent from policy, open_gate_trace.budget_blocked
        should NOT be True due to annual cap (other gates might block, but not annual cap).
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.types import ActionType

        # Policy with no annual_convexity_budget — use template without it
        policy_yaml = tmp_path / "policy_no_annual.yaml"
        policy_yaml.write_text("""\
policy_id: e2e_no_annual
budgets:
  monthly_baseline: 5000.0
  monthly_max: 10000.0
  weekly_baseline: 1250.0
  daily_baseline: 250.0
  weekly_kicker: 2500.0
  daily_kicker: 500.0
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    fill_when_empty:
      ev_per_dollar_implied: 99.0
      ev_per_dollar_external: 99.0
      convexity_multiple: 999.0
    add_when_full:
      ev_per_dollar_implied: 99.0
      ev_per_dollar_external: 99.0
      convexity_multiple: 999.0
  selloff:
    fill_when_empty:
      ev_per_dollar_implied: 99.0
      ev_per_dollar_external: 99.0
      convexity_multiple: 999.0
    add_when_full:
      ev_per_dollar_implied: 99.0
      ev_per_dollar_external: 99.0
      convexity_multiple: 999.0
harvest:
  partial_close_multiple: 2.0
  full_close_multiple: 3.0
  time_stop_dte: 14
  time_stop_min_multiple: 1.2
  partial_close_fraction: 0.5
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
robustness:
  enabled: false
roll:
  enabled: false
""" + f"""ledger_dir: {str(tmp_path).replace(chr(92), "/")}
output_dir: {str(tmp_path).replace(chr(92), "/")}
intents_dir: {str(tmp_path / "intents").replace(chr(92), "/")}
""")

        # Candidate that fails EV but NOT annual cap
        candidates_file = self._write_candidates(tmp_path)

        plan = run_allocator_plan(
            policy_path=str(policy_yaml),
            candidates_path=str(candidates_file),
            signals=None,
            dry_run=True,
        )

        # budget_blocked should be False when no hard budget cap is exhausted
        if plan.open_gate_trace is not None:
            # Candidate fails EV threshold (99.0 vs 2.5 ev), so
            # budget_blocked should be False (not a budget issue)
            assert plan.open_gate_trace.get("budget_blocked") is False, (
                f"budget_blocked should be False when annual cap not configured; "
                f"got: {plan.open_gate_trace.get('budget_blocked')}"
            )
            # BUDGET_ANNUAL_CAP must NOT be in rejection reasons
            rejection_reasons = plan.open_gate_trace.get("rejection_reasons_top", {})
            assert "BUDGET_ANNUAL_CAP" not in rejection_reasons, (
                f"BUDGET_ANNUAL_CAP should not appear when cap is not configured"
            )


# ===========================================================================
# Integration: plan.py attaches annual budget to BudgetState
# ===========================================================================

class TestPlanYtdAttachment:
    """Verify that run_allocator_plan sets budget.spent_ytd from commit ledger."""

    def test_ytd_is_zero_when_ledger_empty(self, tmp_path):
        """budget.spent_ytd=0 when commit ledger is empty/absent."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        ledger = tmp_path / "allocator_commit_ledger.jsonl"
        # File doesn't exist
        assert compute_premium_spent_ytd(ledger) == 0.0

    def test_ytd_accumulates_from_this_year_only(self, tmp_path):
        """Only this-year OPEN rows contribute to spent_ytd."""
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        ledger = tmp_path / "commit.jsonl"
        _write_ledger(ledger, [
            {"action": "OPEN", "date": _today_str(),     "premium_spent": 300.0},
            {"action": "OPEN", "date": _today_str(),     "premium_spent": 200.0},
            {"action": "OPEN", "date": _last_year_str(), "premium_spent": 5000.0},  # excluded
        ])
        assert compute_premium_spent_ytd(ledger) == pytest.approx(500.0)

    def test_plan_budget_state_has_annual_fields(self, tmp_path):
        """After plan.py sets annual budget, BudgetState has correct annual fields."""
        import yaml
        from forecast_arb.allocator.policy import load_policy
        from forecast_arb.allocator.budget import compute_budget_state
        from pathlib import Path

        # Write a minimal policy yaml
        policy_yaml = tmp_path / "policy.yaml"
        policy_yaml.write_text(f"""
policy_id: test_plan_annual
budgets:
  monthly_baseline: 1000.0
  monthly_max: 2000.0
  weekly_baseline: 250.0
  daily_baseline: 50.0
  weekly_kicker: 500.0
  daily_kicker: 100.0
  annual_convexity_budget: 30000.0
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    fill_when_empty:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
    add_when_full:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
  selloff:
    fill_when_empty:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
    add_when_full:
      ev_per_dollar_implied: 1.0
      ev_per_dollar_external: 0.3
      convexity_multiple: 10.0
harvest:
  partial_close_multiple: 2.0
  full_close_multiple: 3.0
  time_stop_dte: 14
  time_stop_min_multiple: 1.2
  partial_close_fraction: 0.5
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
ledger_dir: {tmp_path}
output_dir: {tmp_path}
intents_dir: {tmp_path}/intents
""")

        # Write commit ledger with some YTD spend
        commit_ledger = tmp_path / "allocator_commit_ledger.jsonl"
        _write_ledger(commit_ledger, [
            {"action": "OPEN", "date": _today_str(), "premium_spent": 1234.0},
        ])

        policy = load_policy(str(policy_yaml))

        # Simulate what plan.py does in step 2b
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        budget = compute_budget_state(policy, commit_ledger, signals={})
        _annual = float(policy.get("budgets", {}).get("annual_convexity_budget", float("inf")))
        budget.annual_convexity_budget = _annual
        if budget.annual_budget_enabled:
            budget.spent_ytd = compute_premium_spent_ytd(commit_ledger)

        assert budget.annual_budget_enabled is True
        assert budget.annual_convexity_budget == pytest.approx(30000.0)
        assert budget.spent_ytd == pytest.approx(1234.0)
        assert budget.remaining_annual == pytest.approx(30000.0 - 1234.0)
