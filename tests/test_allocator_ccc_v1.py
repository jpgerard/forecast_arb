"""CCC v1 Allocator tests - deterministic, no live IBKR/Kalshi."""
import json
import math
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

POLICY_DICT = {
    "policy_id": "ccc_v1",
    "budgets": {
        "monthly_baseline": 1000.0,
        "monthly_max": 2000.0,
        "weekly_baseline": 250.0,
        "daily_baseline": 50.0,
        "weekly_kicker": 500.0,
        "daily_kicker": 100.0,
    },
    "inventory_targets": {"crash": 1, "selloff": 1},
    "thresholds": {
        "crash": {
            "ev_per_dollar_implied": 1.6,
            "ev_per_dollar_external": 0.5,
            "convexity_multiple": 25.0,
        },
        "selloff": {
            "ev_per_dollar_implied": 1.3,
            "ev_per_dollar_external": 0.3,
            "convexity_multiple": 15.0,
        },
    },
    "harvest": {
        "partial_close_multiple": 2.0,
        "full_close_multiple": 3.0,
        "partial_close_fraction": 0.5,
        "time_stop_dte": 14,
        "time_stop_min_multiple": 1.2,
    },
    "sizing": {"max_qty_per_trade": 10},
    "kicker": {
        "min_conditioning_confidence": 0.66,
        "max_vix_percentile": 35.0,
    },
    "ledger_dir": "runs/allocator",
    "output_dir": "runs/allocator",
}

POLICY_YAML = """policy_id: ccc_v1
budgets:
  monthly_baseline: 1000.0
  monthly_max: 2000.0
  weekly_baseline: 250.0
  daily_baseline: 50.0
  weekly_kicker: 500.0
  daily_kicker: 100.0
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    ev_per_dollar_implied: 1.6
    ev_per_dollar_external: 0.5
    convexity_multiple: 25.0
  selloff:
    ev_per_dollar_implied: 1.3
    ev_per_dollar_external: 0.3
    convexity_multiple: 15.0
harvest:
  partial_close_multiple: 2.0
  full_close_multiple: 3.0
  partial_close_fraction: 0.5
  time_stop_dte: 14
  time_stop_min_multiple: 1.2
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
ledger_dir: runs/allocator
output_dir: runs/allocator
"""


@pytest.fixture
def policy_file(tmp_path):
    p = tmp_path / "allocator_ccc_v1.yaml"
    p.write_text(POLICY_YAML)
    return str(p)


@pytest.fixture
def policy(policy_file):
    from forecast_arb.allocator.policy import load_policy
    return load_policy(policy_file)


@pytest.fixture
def empty_ledger(tmp_path):
    return tmp_path / "allocator_ledger.jsonl"


# ===========================================================================
# Budget tests
# ===========================================================================

class TestBudgetState:
    def test_baseline_daily_cap(self, policy, empty_ledger):
        from forecast_arb.allocator.budget import compute_budget_state
        b = compute_budget_state(policy, empty_ledger)
        assert b.daily_soft_cap == 50.0
        assert b.weekly_soft_cap == 250.0
        assert b.monthly_soft_cap == 1000.0

    def test_zero_spend_on_empty_ledger(self, policy, empty_ledger):
        from forecast_arb.allocator.budget import compute_budget_state
        b = compute_budget_state(policy, empty_ledger)
        assert b.spent_today == 0.0
        assert b.spent_week == 0.0
        assert b.spent_month == 0.0
        assert b.remaining_today == 50.0

    def test_spend_accumulates_from_ledger(self, policy, tmp_path):
        from forecast_arb.allocator.budget import compute_budget_state, append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN", "premium_spent": 49.0
        })
        b = compute_budget_state(policy, ledger)
        assert b.spent_today == 49.0
        assert b.remaining_today == 1.0  # 50 - 49

    def test_no_kicker_without_signals(self, policy, empty_ledger):
        from forecast_arb.allocator.budget import compute_budget_state
        b = compute_budget_state(policy, empty_ledger, signals={})
        assert b.kicker_enabled is False
        assert "NO_CONDITIONING_CONFIDENCE_SIGNAL" in b.kicker_reasons

    def test_kicker_enabled_with_all_signals(self, policy, empty_ledger):
        from forecast_arb.allocator.budget import compute_budget_state
        signals = {
            "conditioning_confidence": 0.75,
            "vix_percentile": 20.0,
            "credit_stress_elevated": False,
        }
        b = compute_budget_state(policy, empty_ledger, signals=signals)
        assert b.kicker_enabled is True
        assert b.daily_soft_cap == 100.0  # kicker daily cap

    def test_kicker_blocked_vix_too_high(self, policy, empty_ledger):
        from forecast_arb.allocator.budget import compute_budget_state
        signals = {
            "conditioning_confidence": 0.75,
            "vix_percentile": 40.0,
            "credit_stress_elevated": False,
        }
        b = compute_budget_state(policy, empty_ledger, signals=signals)
        assert b.kicker_enabled is False
        assert any("VIX_TOO_HIGH" in r for r in b.kicker_reasons)

    def test_overspend_prevention(self, policy, tmp_path):
        from forecast_arb.allocator.budget import compute_budget_state, append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        # Spend exactly the daily cap
        append_ledger_record(ledger, {"date": today, "action": "OPEN", "premium_spent": 50.0})
        b = compute_budget_state(policy, ledger)
        assert b.remaining_today == 0.0
        assert not b.can_spend(1.0)

    def test_monthly_cap_tracked(self, policy, tmp_path):
        from forecast_arb.allocator.budget import compute_budget_state, append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {"date": today, "action": "OPEN", "premium_spent": 500.0})
        b = compute_budget_state(policy, ledger)
        assert b.spent_month == 500.0
        assert b.remaining_month == 500.0


# ===========================================================================
# Auto-size tests
# ===========================================================================

class TestAutosize:
    def _make_budget(self, daily_cap=50.0, spent_today=0.0, weekly_cap=250.0, spent_week=0.0,
                     monthly_cap=1000.0, spent_month=0.0, kicker=False):
        from forecast_arb.allocator.types import BudgetState
        b = BudgetState(
            monthly_baseline=monthly_cap,
            monthly_max=2000.0,
            weekly_baseline=weekly_cap,
            daily_baseline=daily_cap,
            weekly_kicker=500.0,
            daily_kicker=100.0,
            spent_today=spent_today,
            spent_week=spent_week,
            spent_month=spent_month,
            kicker_enabled=kicker,
        )
        return b

    def test_basic_qty_floor_div(self):
        from forecast_arb.allocator.open_plan import _autosize_qty
        budget = self._make_budget(daily_cap=50.0)
        qty, reason = _autosize_qty(premium=25.0, budget=budget, max_qty=10)
        assert qty == 2  # floor(50/25)
        assert reason == ""

    def test_qty_clamped_to_max(self):
        from forecast_arb.allocator.open_plan import _autosize_qty
        budget = self._make_budget(daily_cap=500.0)
        qty, reason = _autosize_qty(premium=10.0, budget=budget, max_qty=10)
        assert qty == 10  # floor(500/10)=50 but clamped to 10

    def test_premium_exceeds_daily_falls_back_to_weekly(self):
        from forecast_arb.allocator.open_plan import _autosize_qty
        # premium=80 > daily_cap=50 but weekly_remaining=250
        budget = self._make_budget(daily_cap=50.0, weekly_cap=250.0)
        qty, reason = _autosize_qty(premium=80.0, budget=budget, max_qty=10)
        assert qty == 1
        assert reason == ""

    def test_premium_exceeds_both_daily_and_weekly(self):
        from forecast_arb.allocator.open_plan import _autosize_qty
        # premium=300 > both daily=50 and weekly remaining=200
        budget = self._make_budget(daily_cap=50.0, weekly_cap=250.0, spent_week=100.0)
        qty, reason = _autosize_qty(premium=300.0, budget=budget, max_qty=10)
        assert qty is None
        assert "PREMIUM_300" in reason or "WEEKLY_INSUFFICIENT" in reason

    def test_qty_1_when_premium_equals_daily(self):
        from forecast_arb.allocator.open_plan import _autosize_qty
        budget = self._make_budget(daily_cap=49.0)
        qty, reason = _autosize_qty(premium=49.0, budget=budget, max_qty=10)
        assert qty == 1

    def test_daily_budget_exhausted_returns_none(self):
        from forecast_arb.allocator.open_plan import _autosize_qty
        budget = self._make_budget(daily_cap=50.0, spent_today=50.0)
        qty, reason = _autosize_qty(premium=10.0, budget=budget, max_qty=10)
        assert qty is None
        assert "DAILY_BUDGET_EXHAUSTED" in reason


# ===========================================================================
# Reconciliation tests
# ===========================================================================

class TestReconcile:
    def _write_open_record(self, ledger_path, trade_id, regime, underlier,
                           expiry, strikes, qty, premium=49.0):
        from forecast_arb.allocator.budget import append_ledger_record
        append_ledger_record(ledger_path, {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "action": "OPEN",
            "trade_id": trade_id,
            "regime": regime,
            "underlier": underlier,
            "expiry": expiry,
            "strikes": strikes,
            "qty": qty,
            "premium_per_contract": premium,
            "premium_spent": premium * qty,
        })

    def test_reconcile_single_open_position(self, tmp_path):
        from forecast_arb.allocator.reconcile import reconcile_positions
        ledger = tmp_path / "ledger.jsonl"
        self._write_open_record(
            ledger, "trade_abc", "crash", "SPY",
            "20260402", [580.0, 560.0], 2, 49.0
        )
        positions = reconcile_positions(ledger)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.trade_id == "trade_abc"
        assert pos.regime == "crash"
        assert pos.underlier == "SPY"
        assert pos.qty_open == 2
        assert pos.entry_debit == 49.0
        assert pos.strikes[0] == 580.0  # long strike highest

    def test_harvest_close_removes_from_open(self, tmp_path):
        from forecast_arb.allocator.reconcile import reconcile_positions
        from forecast_arb.allocator.budget import append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        self._write_open_record(ledger, "trade_xyz", "crash", "SPY",
                                "20260402", [580.0, 560.0], 1)
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {
            "date": today, "action": "HARVEST_CLOSE", "trade_id": "trade_xyz"
        })
        positions = reconcile_positions(ledger)
        assert len(positions) == 0

    def test_missing_trade_id_skipped(self, tmp_path):
        from forecast_arb.allocator.reconcile import reconcile_positions
        from forecast_arb.allocator.budget import append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        # Record without trade_id
        append_ledger_record(ledger, {
            "date": "2026-02-28", "action": "OPEN",
            "regime": "crash", "underlier": "SPY",
            "expiry": "20260402", "strikes": [580.0, 560.0], "qty": 1,
        })
        positions = reconcile_positions(ledger)
        assert len(positions) == 0

    def test_ibkr_stubs_ambiguous_produces_no_position(self, tmp_path):
        from forecast_arb.allocator.reconcile import reconcile_from_ibkr_stubs
        ledger = tmp_path / "ledger.jsonl"
        # Two long legs, no short → UNRECONCILED
        ibkr_positions = [
            {"symbol": "SPY", "right": "P", "strike": 580.0, "expiry": "20260402",
             "position": 1},
            {"symbol": "SPY", "right": "P", "strike": 570.0, "expiry": "20260402",
             "position": 1},  # both long = ambiguous
        ]
        positions = reconcile_from_ibkr_stubs(ibkr_positions, ledger)
        assert len(positions) == 0

    def test_ibkr_stubs_valid_spread_grouped(self, tmp_path):
        from forecast_arb.allocator.reconcile import reconcile_from_ibkr_stubs
        ledger = tmp_path / "ledger.jsonl"
        ibkr_positions = [
            {"symbol": "SPY", "right": "P", "strike": 580.0, "expiry": "20260402",
             "position": 2},   # long
            {"symbol": "SPY", "right": "P", "strike": 560.0, "expiry": "20260402",
             "position": -2},  # short
        ]
        positions = reconcile_from_ibkr_stubs(ibkr_positions, ledger)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.strikes[0] == 580.0  # long strike
        assert pos.strikes[1] == 560.0  # short strike
        assert pos.qty_open == 2
        assert pos.entry_debit is None  # not in ledger


# ===========================================================================
# Harvest tests
# ===========================================================================

class TestHarvest:
    def _make_position(self, entry_debit, mark_mid, dte, qty=1, trade_id="trade_1"):
        from forecast_arb.allocator.types import SleevePosition
        return SleevePosition(
            trade_id=trade_id,
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],
            qty_open=qty,
            regime="crash",
            entry_debit=entry_debit,
            mark_mid=mark_mid,
            dte=dte,
        )

    def test_partial_close_at_2x(self):
        from forecast_arb.allocator.harvest import generate_harvest_actions
        pos = self._make_position(entry_debit=49.0, mark_mid=102.0, dte=30, qty=4)
        # multiple = 102/49 ≈ 2.08x → partial close
        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        a = actions[0]
        assert a.type == "HARVEST_CLOSE"
        assert a.qty == 2  # ceil(4 * 0.5) = 2
        assert any("PARTIAL_CLOSE" in rc for rc in a.reason_codes)

    def test_full_close_at_3x(self):
        from forecast_arb.allocator.harvest import generate_harvest_actions
        pos = self._make_position(entry_debit=49.0, mark_mid=151.0, dte=30, qty=2)
        # multiple = 151/49 ≈ 3.08x → full close
        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        a = actions[0]
        assert a.type == "HARVEST_CLOSE"
        assert a.qty == 2  # all remaining
        assert any("FULL_CLOSE" in rc for rc in a.reason_codes)

    def test_time_stop_at_low_dte(self):
        from forecast_arb.allocator.harvest import generate_harvest_actions
        pos = self._make_position(entry_debit=49.0, mark_mid=50.0, dte=10, qty=1)
        # multiple = 50/49 ≈ 1.02x < 1.2x → time stop triggers
        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        a = actions[0]
        assert a.type == "ROLL_CLOSE"
        assert any("TIME_STOP" in rc for rc in a.reason_codes)

    def test_no_action_below_2x_and_high_dte(self):
        from forecast_arb.allocator.harvest import generate_harvest_actions
        pos = self._make_position(entry_debit=49.0, mark_mid=85.0, dte=30)
        # multiple = 85/49 ≈ 1.73x < 2.0x; DTE > 14 → no action
        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 0

    def test_missing_entry_debit_time_stop_only(self):
        from forecast_arb.allocator.harvest import generate_harvest_actions
        from forecast_arb.allocator.types import SleevePosition
        pos = SleevePosition(
            trade_id="t1", underlier="SPY", expiry="20260402",
            strikes=[580.0, 560.0], qty_open=1, regime="crash",
            entry_debit=None, mark_mid=None, dte=10
        )
        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        assert actions[0].type == "ROLL_CLOSE"
        assert any("MISSING_ENTRY_DEBIT" in rc for rc in actions[0].reason_codes)

    def test_partial_close_qty_ceiling(self):
        from forecast_arb.allocator.harvest import _partial_close_qty
        assert _partial_close_qty(1, 0.5) == 1   # ceil(0.5) = 1
        assert _partial_close_qty(3, 0.5) == 2   # ceil(1.5) = 2
        assert _partial_close_qty(4, 0.5) == 2   # ceil(2.0) = 2
        assert _partial_close_qty(5, 0.5) == 3   # ceil(2.5) = 3


# ===========================================================================
# Output / schema tests
# ===========================================================================

class TestOutputSchema:
    def test_allocator_actions_always_written(self, policy_file, tmp_path):
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.policy import load_policy, get_actions_path
        import yaml

        # Override output paths to tmp_path
        p = load_policy(policy_file)
        policy_content = open(policy_file).read()
        new_policy_file = tmp_path / "policy.yaml"
        intents_dir_tmp = str(tmp_path / "intents" / "allocator")
        new_policy_file.write_text(
            policy_content
            + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\nintents_dir: {intents_dir_tmp}\n"
        )
        plan = run_allocator_plan(
            policy_path=str(new_policy_file),
            candidates_path=None,
            dry_run=False,
        )
        actions_path = tmp_path / "allocator_actions.json"
        assert actions_path.exists()
        data = json.loads(actions_path.read_text())
        # Required top-level keys
        for key in ("timestamp_utc", "policy_id", "budgets", "inventory",
                    "positions", "actions", "notes"):
            assert key in data, f"Missing key: {key}"

    def test_hold_when_no_candidates(self, policy_file, tmp_path):
        from forecast_arb.allocator.plan import run_allocator_plan
        policy_content = open(policy_file).read()
        new_policy_file = tmp_path / "policy.yaml"
        new_policy_file.write_text(
            policy_content
            + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\n"
        )
        plan = run_allocator_plan(
            policy_path=str(new_policy_file),
            candidates_path=None,
            dry_run=True,
        )
        types = [a.type for a in plan.actions]
        assert "HOLD" in types

    def test_open_action_for_qualifying_candidate(self, policy_file, tmp_path):
        from forecast_arb.allocator.plan import run_allocator_plan
        # Write a candidates flat file with qualifying candidate
        candidates = {
            "candidates": [{
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20260402",
                "candidate_id": "test_crash_001",
                "debit_per_contract": 49.0,
                "max_gain_per_contract": 1951.0,
                "ev_per_dollar": 23.0,  # >> 1.6 threshold
                "strikes": {"long_put": 580.0, "short_put": 560.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        policy_content = open(policy_file).read()
        new_policy_file = tmp_path / "policy.yaml"
        intents_dir_tmp = str(tmp_path / "intents" / "allocator")
        new_policy_file.write_text(
            policy_content
            + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\nintents_dir: {intents_dir_tmp}\n"
        )
        plan = run_allocator_plan(
            policy_path=str(new_policy_file),
            candidates_path=str(cpath),
            dry_run=True,
        )
        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        assert len(open_actions) == 1
        oa = open_actions[0]
        assert oa.qty == 1  # floor(50/49) = 1
        assert oa.premium == 49.0
        assert "test_crash_001" in (oa.candidate_id or "")

    def test_harvest_action_in_output(self, policy_file, tmp_path):
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record
        # v1.4: plan ledger = inventory tracking
        ledger = tmp_path / "allocator_plan_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        # Write an open trade that should trigger 2x harvest
        # entry_debit=49, mark needs to be set via marks
        # We'll rely on no-candidates path (no mark populated) so only inventory check
        policy_content = open(policy_file).read()
        new_policy_file = tmp_path / "policy.yaml"
        new_policy_file.write_text(
            policy_content
            + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\n"
        )
        # Write open trade in ledger
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "trade_h1", "regime": "crash",
            "underlier": "SPY", "expiry": "20260402",
            "strikes": [580.0, 560.0], "qty": 2,
            "premium_per_contract": 49.0, "premium_spent": 98.0,
        })
        # Write a candidates file that also has this position's mark at 2.1x
        candidates = {
            "candidates": [{
                "regime": "crash", "underlier": "SPY",
                "expiry": "20260402",
                "candidate_id": "same_pos",
                "debit_per_contract": 103.0,  # 103/49 = 2.1x
                "max_gain_per_contract": 1897.0,
                "ev_per_dollar": 18.0,
                "strikes": {"long_put": 580.0, "short_put": 560.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        plan = run_allocator_plan(
            policy_path=str(new_policy_file),
            candidates_path=str(cpath),
            dry_run=True,
        )
        harvest_actions = [a for a in plan.actions if a.type == "HARVEST_CLOSE"]
        assert len(harvest_actions) == 1
        assert harvest_actions[0].trade_id == "trade_h1"

    def test_plan_to_dict_has_all_keys(self, policy_file, tmp_path):
        from forecast_arb.allocator.plan import run_allocator_plan
        policy_content = open(policy_file).read()
        new_policy_file = tmp_path / "policy.yaml"
        new_policy_file.write_text(
            policy_content
            + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\n"
        )
        plan = run_allocator_plan(str(new_policy_file), dry_run=True)
        d = plan.to_dict()
        for key in ("timestamp_utc", "policy_id", "budgets", "inventory",
                    "positions", "actions", "notes"):
            assert key in d
        assert isinstance(d["actions"], list)
        assert len(d["actions"]) >= 1  # at least HOLD

    def test_ledger_appended_on_run(self, policy_file, tmp_path):
        from forecast_arb.allocator.plan import run_allocator_plan
        policy_content = open(policy_file).read()
        new_policy_file = tmp_path / "policy.yaml"
        new_policy_file.write_text(
            policy_content
            + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\n"
        )
        # v1.4: plan ledger is now allocator_plan_ledger.jsonl
        ledger = tmp_path / "allocator_plan_ledger.jsonl"
        assert not ledger.exists()
        run_allocator_plan(str(new_policy_file), dry_run=False)
        assert ledger.exists()
        lines = ledger.read_text().strip().split("\n")
        # At minimum a DAILY_SUMMARY record
        records = [json.loads(l) for l in lines if l.strip()]
        actions = [r["action"] for r in records]
        assert "DAILY_SUMMARY" in actions


# ===========================================================================
# Policy validation tests
# ===========================================================================

class TestPolicyValidation:
    def test_missing_section_raises(self, tmp_path):
        from forecast_arb.allocator.policy import load_policy, PolicyError
        bad_yaml = "policy_id: test\nbudgets:\n  monthly_baseline: 1000\n"
        p = tmp_path / "bad.yaml"
        p.write_text(bad_yaml)
        with pytest.raises(PolicyError, match="missing required section"):
            load_policy(str(p))

    def test_valid_policy_loads(self, policy_file):
        from forecast_arb.allocator.policy import load_policy
        p = load_policy(policy_file)
        assert p["policy_id"] == "ccc_v1"
        assert p["budgets"]["monthly_baseline"] == 1000.0


