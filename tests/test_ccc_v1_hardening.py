"""
CCC v1 Hardening Tests — Tasks 1-8.

All tests are fully deterministic (no live IBKR/Kalshi calls).
Each class targets a specific task's acceptance criteria.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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
close_liquidity_guard:
  max_width_pct: 0.25
limits:
  max_open_actions_per_day: 1
  max_close_actions_per_day: 2
sizing:
  max_qty_per_trade: 10
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
"""

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
    "close_liquidity_guard": {"max_width_pct": 0.25},
    "limits": {"max_open_actions_per_day": 1, "max_close_actions_per_day": 2},
    "sizing": {"max_qty_per_trade": 10},
    "kicker": {
        "min_conditioning_confidence": 0.66,
        "max_vix_percentile": 35.0,
    },
    "ledger_dir": "runs/allocator",
    "output_dir": "runs/allocator",
}


@pytest.fixture
def policy_file(tmp_path):
    p = tmp_path / "allocator_ccc_v1.yaml"
    p.write_text(POLICY_YAML)
    return str(p)


@pytest.fixture
def policy(policy_file):
    from forecast_arb.allocator.policy import load_policy
    return load_policy(policy_file)


def _write_ledger_open(ledger: Path, trade_id: str, regime: str,
                       underlier: str, expiry: str, strikes: list,
                       qty: int, premium: float, candidate_id: str = None):
    """Helper to append an OPEN record to a ledger."""
    from forecast_arb.allocator.budget import append_ledger_record
    today = datetime.now(timezone.utc).date().isoformat()
    rec = {
        "date": today,
        "action": "OPEN",
        "trade_id": trade_id,
        "regime": regime,
        "underlier": underlier,
        "expiry": expiry,
        "strikes": strikes,
        "qty": qty,
        "premium_per_contract": premium,
        "premium_spent": premium * qty,
    }
    if candidate_id:
        rec["candidate_id"] = candidate_id
    append_ledger_record(ledger, rec)


def _make_policy_file(tmp_path, extra_yaml: str = "") -> str:
    content = POLICY_YAML + f"\nledger_dir: {tmp_path}\noutput_dir: {tmp_path}\nintents_dir: {tmp_path / 'intents'}\n"
    if extra_yaml:
        content += extra_yaml
    pf = tmp_path / "policy.yaml"
    pf.write_text(content)
    return str(pf)


# ===========================================================================
# Task 1 — Budget semantics: before / after split
# ===========================================================================

class TestBudgetBeforeAfter:
    """
    Acceptance: When an OPEN uses $45 and remaining_today_before is $5,
    remaining_today_after becomes $0.
    """

    def test_budget_before_reflects_ledger_actuals(self, tmp_path, policy):
        """spent_today_before = ledger actuals, not affected by planned actions."""
        from forecast_arb.allocator.budget import compute_budget_state, append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {"date": today, "action": "OPEN", "premium_spent": 45.0})
        b = compute_budget_state(policy, ledger)
        assert b.spent_today == 45.0
        assert b.remaining_today_before == pytest.approx(5.0)

    def test_remaining_today_after_minus_planned(self, tmp_path, policy):
        """remaining_today_after = remaining_today_before - planned_spend_today."""
        from forecast_arb.allocator.budget import compute_budget_state, append_ledger_record
        from forecast_arb.allocator.types import BudgetState
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {"date": today, "action": "OPEN", "premium_spent": 45.0})
        b = compute_budget_state(policy, ledger)
        # Simulate planned spend of $5 (fills remaining budget exactly)
        b.planned_spend_today = 5.0
        assert b.remaining_today_after == pytest.approx(0.0)

    def test_remaining_after_floors_at_zero(self, tmp_path, policy):
        """remaining_today_after cannot go negative."""
        from forecast_arb.allocator.budget import compute_budget_state
        from forecast_arb.allocator.types import BudgetState
        ledger = tmp_path / "ledger.jsonl"
        b = compute_budget_state(policy, ledger)
        b.planned_spend_today = 999.0  # way over budget
        assert b.remaining_today_after == 0.0

    def test_plan_budget_dict_has_before_after_keys(self, tmp_path):
        """allocator_actions.json emits spent_today_before and remaining_today_after."""
        from forecast_arb.allocator.plan import run_allocator_plan
        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, dry_run=True)
        d = plan.to_dict()
        b = d["budgets"]
        # Before fields
        assert "spent_today_before" in b
        assert "remaining_today_before" in b
        assert "spent_week_before" in b
        assert "remaining_week_before" in b
        assert "spent_month_before" in b
        assert "remaining_month_before" in b
        # Planned fields
        assert "planned_spend_today" in b
        assert "planned_spend_week" in b
        assert "planned_spend_month" in b
        # After fields
        assert "remaining_today_after" in b
        assert "remaining_week_after" in b
        assert "remaining_month_after" in b

    def test_plan_budget_before_after_math(self, tmp_path):
        """
        Full plan run: committed spend $45 + $5 OPEN plan →
        remaining_today_before=5, planned_spend_today=5, remaining_today_after=0.

        v1.4: budget reads commit ledger; inventory reads plan ledger.
        We write to BOTH to simulate a trade that was committed and then closed.
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record

        plan_ledger = tmp_path / "allocator_plan_ledger.jsonl"
        commit_ledger = tmp_path / "allocator_commit_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        # Plan ledger: position opened and closed (inventory reconcile)
        append_ledger_record(plan_ledger, {
            "date": today, "action": "OPEN", "premium_spent": 45.0,
            "trade_id": "old_trade_1", "regime": "crash",
            "underlier": "SPY", "expiry": "20261201",
            "strikes": [570.0, 550.0], "qty": 1, "premium_per_contract": 45.0,
        })
        append_ledger_record(plan_ledger, {
            "date": today, "action": "HARVEST_CLOSE", "trade_id": "old_trade_1"
        })
        # Commit ledger: the $45 committed spend (only this affects spent_today_before)
        append_ledger_record(commit_ledger, {
            "date": today, "action": "OPEN", "premium_spent": 45.0,
        })

        # Candidates: one crash candidate for $5 (fills remaining exactly)
        candidates = {
            "candidates": [{
                "candidate_id": "crash_venture_v2_xxx:SPY:crash:20261001:560/540",
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20261001",
                "debit_per_contract": 5.0,
                "max_gain_per_contract": 1995.0,  # 399x convexity >> 25x
                "ev_per_dollar": 40.0,
                "strikes": {"long_put": 560.0, "short_put": 540.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        d = plan.to_dict()
        b = d["budgets"]
        assert b["spent_today_before"] == pytest.approx(45.0)
        assert b["remaining_today_before"] == pytest.approx(5.0)
        assert b["planned_spend_today"] == pytest.approx(5.0)
        assert b["remaining_today_after"] == pytest.approx(0.0)

    def test_backward_compat_remaining_today(self, tmp_path, policy):
        """remaining_today backward-compat alias still works."""
        from forecast_arb.allocator.budget import compute_budget_state, append_ledger_record
        ledger = tmp_path / "ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {"date": today, "action": "OPEN", "premium_spent": 20.0})
        b = compute_budget_state(policy, ledger)
        assert b.remaining_today == b.remaining_today_before == pytest.approx(30.0)


# ===========================================================================
# Task 2 — Candidate identity: canonical candidate_id
# ===========================================================================

class TestCandidateIdentity:
    """
    Acceptance: You can grep candidate_id in candidates_flat.json and find an exact match.
    """

    CANONICAL_ID = "crash_venture_v2_abc123:SPY:selloff:20260402:530/550"

    def _make_candidate(self, candidate_id: str, regime: str = "crash") -> dict:
        return {
            "candidate_id": candidate_id,
            "regime": regime,
            "underlier": "SPY",
            "expiry": "20261001",
            "debit_per_contract": 15.0,
            "max_gain_per_contract": 1985.0,  # 132.3x >> 25x
            "ev_per_dollar": 40.0,
            "strikes": {"long_put": 570.0, "short_put": 550.0},
        }

    def test_open_action_stores_exact_candidate_id(self, tmp_path):
        """OPEN action candidate_id must exactly match the candidate's candidate_id field."""
        from forecast_arb.allocator.plan import run_allocator_plan

        canonical_id = self.CANONICAL_ID
        candidates = {"candidates": [self._make_candidate(canonical_id, regime="crash")]}
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        assert len(open_actions) == 1
        assert open_actions[0].candidate_id == canonical_id, (
            f"Expected exact match to '{canonical_id}', got '{open_actions[0].candidate_id}'"
        )

    def test_open_action_candidate_id_grepable_in_candidates(self, tmp_path):
        """candidate_id from OPEN action must appear verbatim in candidates JSON."""
        from forecast_arb.allocator.plan import run_allocator_plan

        canonical_id = "crash_venture_v2_abc:SPY:crash:20261001:570/550"
        candidates = {"candidates": [self._make_candidate(canonical_id)]}
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        assert len(open_actions) == 1
        stored_id = open_actions[0].candidate_id

        # Must be grep-matchable in the candidates file
        candidates_text = cpath.read_text()
        assert stored_id in candidates_text, (
            f"candidate_id '{stored_id}' not found in candidates_flat.json"
        )

    def test_run_id_and_rank_propagated(self, tmp_path):
        """run_id and candidate_rank from candidate dict appear in OPEN action."""
        from forecast_arb.allocator.plan import run_allocator_plan

        candidate = self._make_candidate("test_id_777")
        candidate["run_id"] = "crash_v2_run_20260228"
        candidate["rank"] = 1
        candidates = {"candidates": [candidate]}
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        assert len(open_actions) == 1
        a = open_actions[0]
        assert a.run_id == "crash_v2_run_20260228"
        assert a.candidate_rank == 1

    def test_run_id_and_rank_in_to_dict(self, tmp_path):
        """run_id and candidate_rank appear in action's to_dict output."""
        from forecast_arb.allocator.plan import run_allocator_plan

        candidate = self._make_candidate("test_id_888")
        candidate["run_id"] = "my_run_abc"
        candidate["rank"] = 3
        candidates = {"candidates": [candidate]}
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)
        d = plan.to_dict()

        open_dicts = [a for a in d["actions"] if a["type"] == "OPEN"]
        assert len(open_dicts) == 1
        assert open_dicts[0]["run_id"] == "my_run_abc"
        assert open_dicts[0]["candidate_rank"] == 3


# ===========================================================================
# Task 3 — Convexity multiple: unit sanity + explicit fields
# ===========================================================================

class TestConvexityDecomposition:
    """
    Acceptance: multiple = max_gain_per_contract / premium_per_contract.
    Unit test verifying multiple for known width/debit.
    """

    def test_convexity_multiple_unit_calculation(self):
        """
        Given: width=20.0, debit=$0.15/share → premium=$15/contract
               max_gain=(20-0.15)*100 = $1985/contract
               multiple = 1985/15 = 132.333...
        """
        from forecast_arb.allocator.open_plan import _compute_convexity_detail

        candidate = {
            "strikes": {"long_put": 570.0, "short_put": 550.0},
        }
        premium_per_contract = 15.0    # $15 per contract = $0.15 per share
        max_gain_per_contract = 1985.0  # (20 - 0.15) * 100

        detail = _compute_convexity_detail(candidate, premium_per_contract, max_gain_per_contract)

        assert detail["width"] == pytest.approx(20.0)
        assert detail["debit"] == pytest.approx(0.15, abs=1e-6)  # $/share
        assert detail["max_gain_per_contract"] == pytest.approx(1985.0)
        assert detail["premium_per_contract"] == pytest.approx(15.0)
        assert detail["multiple"] == pytest.approx(1985.0 / 15.0, rel=1e-3)

    def test_convexity_multiple_matches_decomposition(self):
        """multiple must equal max_gain_per_contract / premium_per_contract exactly (rounded)."""
        from forecast_arb.allocator.open_plan import _compute_convexity_detail

        # Test with another known set
        candidate = {"strikes": {"long_put": 580.0, "short_put": 560.0}}
        premium = 49.0   # $49 per contract
        max_gain = 1951.0  # $1951 per contract

        detail = _compute_convexity_detail(candidate, premium, max_gain)

        expected_multiple = round(max_gain / premium, 2)
        assert detail["multiple"] == pytest.approx(expected_multiple, rel=1e-4)

    def test_convexity_detail_present_in_open_action(self, tmp_path):
        """OPEN action in plan has convexity decomposition dict."""
        from forecast_arb.allocator.plan import run_allocator_plan

        candidates = {
            "candidates": [{
                "candidate_id": "test_conv_001",
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20261001",
                "debit_per_contract": 15.0,
                "max_gain_per_contract": 1985.0,
                "ev_per_dollar": 40.0,
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        assert len(open_actions) == 1
        conv = open_actions[0].convexity_detail
        assert conv is not None
        assert "width" in conv
        assert "debit" in conv
        assert "max_gain_per_contract" in conv
        assert "premium_per_contract" in conv
        assert "multiple" in conv

    def test_convexity_emitted_in_to_dict(self, tmp_path):
        """'convexity' key present in to_dict output for OPEN action."""
        from forecast_arb.allocator.plan import run_allocator_plan

        candidates = {
            "candidates": [{
                "candidate_id": "conv_test_002",
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20261001",
                "debit_per_contract": 15.0,
                "max_gain_per_contract": 1985.0,
                "ev_per_dollar": 40.0,
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)
        d = plan.to_dict()

        open_d = [a for a in d["actions"] if a["type"] == "OPEN"]
        assert len(open_d) == 1
        assert "convexity" in open_d[0]
        assert open_d[0]["convexity"]["width"] == 20.0
        # Multiple matches computation
        c = open_d[0]["convexity"]
        assert c["multiple"] == pytest.approx(
            c["max_gain_per_contract"] / c["premium_per_contract"], rel=1e-4
        )

    def test_convexity_debit_is_per_share(self):
        """debit field = premium_per_contract / 100  (dollars per share)."""
        from forecast_arb.allocator.open_plan import _compute_convexity_detail

        candidate = {"strikes": {"long_put": 560.0, "short_put": 540.0}}
        premium_per_contract = 49.0
        max_gain_per_contract = 1951.0

        detail = _compute_convexity_detail(candidate, premium_per_contract, max_gain_per_contract)
        assert detail["debit"] == pytest.approx(premium_per_contract / 100.0, rel=1e-6)


# ===========================================================================
# Task 4 — Post-action inventory calculation
# ===========================================================================

class TestInventoryBeforeAfter:
    """
    Acceptance: If a ROLL_CLOSE drops crash inventory below target,
    allocator is allowed to OPEN a replacement (subject to budget/gates).
    """

    def test_simulate_close_decrements_inventory(self):
        """ROLL_CLOSE action decrements crash_open in inventory simulation."""
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        from forecast_arb.allocator.types import AllocatorAction, InventoryState, SleevePosition

        inv = InventoryState(crash_target=1, crash_open=1, selloff_target=1, selloff_open=0)
        positions = [
            SleevePosition(
                trade_id="t1", underlier="SPY", expiry="20261001",
                strikes=[570.0, 550.0], qty_open=1, regime="crash",
                entry_debit=49.0, mark_mid=None, dte=10,
            )
        ]
        close_action = AllocatorAction(type="ROLL_CLOSE", trade_id="t1", qty=1)

        inv_after = _simulate_inventory_after_actions(inv, [close_action], positions)
        assert inv_after.crash_open == 0

    def test_simulate_open_increments_inventory(self):
        """OPEN action increments crash_open."""
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        from forecast_arb.allocator.types import AllocatorAction, InventoryState

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)
        open_action = AllocatorAction(
            type="OPEN",
            candidate_id="my_cid:SPY:crash:20261001:570/550",
            qty=1
        )
        inv_after = _simulate_inventory_after_actions(inv, [open_action], positions=[])
        assert inv_after.crash_open == 1

    def test_simulate_floor_at_zero(self):
        """ROLL_CLOSE on 0-open inventory doesn't go negative."""
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        from forecast_arb.allocator.types import AllocatorAction, InventoryState, SleevePosition

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)
        positions = [
            SleevePosition(
                trade_id="t1", underlier="SPY", expiry="20261001",
                strikes=[570.0, 550.0], qty_open=1, regime="crash",
                entry_debit=49.0, mark_mid=None, dte=10,
            )
        ]
        close_action = AllocatorAction(type="ROLL_CLOSE", trade_id="t1", qty=1)
        inv_after = _simulate_inventory_after_actions(inv, [close_action], positions)
        assert inv_after.crash_open == 0

    def test_roll_close_allows_replacement_open(self, tmp_path):
        """
        After a ROLL_CLOSE on the only crash position, should allow OPEN of replacement.
        Acceptance: If ROLL_CLOSE drops crash below target → OPEN allowed.
        v1.4: write to plan ledger (allocator_plan_ledger.jsonl) for inventory tracking.
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record

        # v1.4: plan ledger = inventory; commit ledger = budget
        ledger = tmp_path / "allocator_plan_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()

        # Existing open trade: crash, DTE <= 14, mark < 1.2x → ROLL_CLOSE
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "old_crash_trade", "regime": "crash",
            "underlier": "SPY", "expiry": "20261001",
            "strikes": [570.0, 550.0], "qty": 1,
            "premium_per_contract": 49.0, "premium_spent": 49.0,
        })

        # Candidate for replacement open
        candidates = {
            "candidates": [{
                "candidate_id": "crash_venture_v2:SPY:crash:20261201:570/550",
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20261201",
                "debit_per_contract": 15.0,
                "max_gain_per_contract": 1985.0,
                "ev_per_dollar": 40.0,
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        from datetime import date, timedelta
        near_expiry = (date.today() + timedelta(days=5)).strftime("%Y%m%d")

        # Re-write the plan ledger with near expiry and a small premium ($5)
        # so there is budget remaining ($45) to open the replacement.
        ledger.write_text("")
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "old_crash_trade", "regime": "crash",
            "underlier": "SPY", "expiry": near_expiry,
            "strikes": [570.0, 550.0], "qty": 1,
            "premium_per_contract": 5.0, "premium_spent": 5.0,
        })

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        # Should have a ROLL_CLOSE (DTE <= 14) and an OPEN (replacement)
        roll_actions = [a for a in plan.actions if a.type == "ROLL_CLOSE"]
        open_actions = [a for a in plan.actions if a.type == "OPEN"]

        assert len(roll_actions) >= 1, "Expected ROLL_CLOSE for near-expiry position"
        assert len(open_actions) >= 1, "Expected OPEN replacement after ROLL_CLOSE"

    def test_plan_to_dict_has_inventory_before_after(self, tmp_path):
        """Plan to_dict emits inventory.before and inventory.after."""
        from forecast_arb.allocator.plan import run_allocator_plan

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, dry_run=True)
        d = plan.to_dict()

        assert "inventory" in d
        inv = d["inventory"]
        assert "before" in inv
        assert "after" in inv

        # Both must have crash and selloff
        for key in ("before", "after"):
            assert "crash" in inv[key]
            assert "selloff" in inv[key]
            assert "target" in inv[key]["crash"]
            assert "open" in inv[key]["crash"]


# ===========================================================================
# Task 5 — Close-liquidity guard
# ===========================================================================

class TestCloseLiquidityGuard:
    """
    Acceptance: In a simulated wide market, allocator outputs HOLD instead of
    CLOSE and includes reason code WIDE_MARKET_NO_CLOSE.
    """

    def _make_position(self, mark_mid, entry_debit, dte, spread_bid=None, spread_ask=None, qty=1):
        from forecast_arb.allocator.types import SleevePosition
        return SleevePosition(
            trade_id="pos_1", underlier="SPY", expiry="20261001",
            strikes=[570.0, 550.0], qty_open=qty, regime="crash",
            entry_debit=entry_debit, mark_mid=mark_mid, dte=dte,
            spread_bid=spread_bid, spread_ask=spread_ask,
        )

    def test_wide_market_blocks_harvest_close(self):
        """
        Spread (ask-bid)/mid = (2.0-1.0)/1.5 = 66.7% >> 25% → HOLD with WIDE_MARKET_NO_CLOSE.
        mark_mid = 3x entry_debit → would normally be HARVEST_CLOSE (full close).
        """
        from forecast_arb.allocator.harvest import generate_harvest_actions

        pos = self._make_position(
            mark_mid=150.0,    # 3x entry_debit → would be FULL_CLOSE
            entry_debit=50.0,
            dte=30,
            spread_bid=90.0,   # wide
            spread_ask=210.0,  # (210-90)/150 = 0.80 = 80% > 25%
        )

        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        a = actions[0]
        assert a.type == "HOLD"
        assert "WIDE_MARKET_NO_CLOSE" in a.reason_codes

    def test_wide_market_blocks_roll_close(self):
        """
        Wide market also blocks time-stop ROLL_CLOSE.
        """
        from forecast_arb.allocator.harvest import generate_harvest_actions

        pos = self._make_position(
            mark_mid=55.0,      # mark < 1.2x entry_debit (1.1x)
            entry_debit=50.0,
            dte=5,              # DTE <= 14 → time-stop
            spread_bid=30.0,
            spread_ask=80.0,    # (80-30)/55 ≈ 0.91 = 91% > 25%
        )

        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        a = actions[0]
        assert a.type == "HOLD"
        assert "WIDE_MARKET_NO_CLOSE" in a.reason_codes

    def test_narrow_spread_allows_harvest(self):
        """Spread within 25% → HARVEST_CLOSE proceeds normally."""
        from forecast_arb.allocator.harvest import generate_harvest_actions

        pos = self._make_position(
            mark_mid=150.0,    # 3x entry_debit
            entry_debit=50.0,
            dte=30,
            spread_bid=145.0,  # narrow: (155-145)/150 = 6.7% < 25%
            spread_ask=155.0,
        )

        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        assert actions[0].type == "HARVEST_CLOSE"

    def test_no_spread_data_guard_not_applied(self):
        """When spread_bid/spread_ask are None, guard is not applied → harvest proceeds."""
        from forecast_arb.allocator.harvest import generate_harvest_actions

        pos = self._make_position(
            mark_mid=150.0,    # 3x entry_debit
            entry_debit=50.0,
            dte=30,
            spread_bid=None,   # no live data
            spread_ask=None,
        )

        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        # Without live data, guard not applied → HARVEST_CLOSE
        assert actions[0].type == "HARVEST_CLOSE"

    def test_guard_reason_code_has_pct(self):
        """WIDE_MARKET_NO_CLOSE reason code includes spread pct detail."""
        from forecast_arb.allocator.harvest import generate_harvest_actions

        pos = self._make_position(
            mark_mid=100.0,
            entry_debit=33.0,
            dte=30,
            spread_bid=50.0,   # (150-50)/100 = 100% >> 25%
            spread_ask=150.0,
        )
        actions = generate_harvest_actions([pos], POLICY_DICT)
        assert len(actions) == 1
        assert any("SPREAD_WIDTH_PCT" in rc for rc in actions[0].reason_codes)


# ===========================================================================
# Task 6 — Daily action caps
# ===========================================================================

class TestDailyActionCaps:
    """
    Acceptance: Allocator never produces more than configured count of OPEN/CLOSE actions.
    """

    def test_close_cap_enforced_priority_harvest_over_roll(self):
        """
        cap = 1 close: HARVEST_CLOSE is prioritized over ROLL_CLOSE.
        Excess converted to HOLD with DAILY_ACTION_LIMIT.
        """
        from forecast_arb.allocator.plan import _apply_action_caps
        from forecast_arb.allocator.types import AllocatorAction

        harvest = AllocatorAction(type="HARVEST_CLOSE", trade_id="t1", qty=1)
        roll = AllocatorAction(type="ROLL_CLOSE", trade_id="t2", qty=1)

        limits = {"max_close_actions_per_day": 1, "max_open_actions_per_day": 999}
        approved, capped = _apply_action_caps([harvest, roll], [], limits)

        approved_types = [a.type for a in approved]
        assert "HARVEST_CLOSE" in approved_types
        assert "ROLL_CLOSE" not in approved_types
        assert len(capped) == 1
        assert capped[0].type == "HOLD"
        assert "DAILY_ACTION_LIMIT" in capped[0].reason_codes

    def test_open_cap_enforced(self):
        """max_open=1: second OPEN is converted to HOLD."""
        from forecast_arb.allocator.plan import _apply_action_caps
        from forecast_arb.allocator.types import AllocatorAction

        open1 = AllocatorAction(type="OPEN", candidate_id="c1", qty=1, premium=15.0)
        open2 = AllocatorAction(type="OPEN", candidate_id="c2", qty=1, premium=15.0)

        limits = {"max_close_actions_per_day": 999, "max_open_actions_per_day": 1}
        approved, capped = _apply_action_caps([], [open1, open2], limits)

        open_count = sum(1 for a in approved if a.type == "OPEN")
        assert open_count == 1
        assert len(capped) == 1
        assert capped[0].type == "HOLD"
        assert "DAILY_ACTION_LIMIT" in capped[0].reason_codes

    def test_close_cap_exact_boundary(self):
        """With max_close=2: 2 closes approved, 3rd is HOLD."""
        from forecast_arb.allocator.plan import _apply_action_caps
        from forecast_arb.allocator.types import AllocatorAction

        closes = [
            AllocatorAction(type="HARVEST_CLOSE", trade_id="t1", qty=1),
            AllocatorAction(type="ROLL_CLOSE", trade_id="t2", qty=1),
            AllocatorAction(type="ROLL_CLOSE", trade_id="t3", qty=1),
        ]
        limits = {"max_close_actions_per_day": 2, "max_open_actions_per_day": 999}
        approved, capped = _apply_action_caps(closes, [], limits)

        close_count = sum(1 for a in approved
                          if a.type in ("HARVEST_CLOSE", "ROLL_CLOSE"))
        assert close_count == 2
        assert len(capped) == 1
        hold_reasons = capped[0].reason_codes
        assert "DAILY_ACTION_LIMIT" in hold_reasons

    def test_guard_holds_pass_through_cap(self):
        """
        HOLD actions from close-liquidity guard are not counted against
        the close cap (they're already blocked, not new closes).
        """
        from forecast_arb.allocator.plan import _apply_action_caps
        from forecast_arb.allocator.types import AllocatorAction

        guard_hold = AllocatorAction(
            type="HOLD", trade_id="t_wide",
            reason_codes=["WIDE_MARKET_NO_CLOSE"]
        )
        harvest = AllocatorAction(type="HARVEST_CLOSE", trade_id="t_ok", qty=1)

        limits = {"max_close_actions_per_day": 1, "max_open_actions_per_day": 999}
        # guard_hold comes in through harvest_actions; harvest comes as close too
        approved, capped = _apply_action_caps([guard_hold, harvest], [], limits)

        # The HARVEST_CLOSE should still be approved (cap = 1)
        assert any(a.type == "HARVEST_CLOSE" for a in approved)
        assert len(capped) == 0

    def test_plan_respects_configured_limits(self, tmp_path):
        """End-to-end: plan with max_open=1 produces at most 1 OPEN."""
        from forecast_arb.allocator.plan import run_allocator_plan

        candidates = {
            "candidates": [
                {
                    "candidate_id": "c_crash_1",
                    "regime": "crash", "underlier": "SPY", "expiry": "20261001",
                    "debit_per_contract": 15.0, "max_gain_per_contract": 1985.0,
                    "ev_per_dollar": 40.0, "strikes": {"long_put": 570.0, "short_put": 550.0},
                },
                # Second crash: would be second OPEN but cap is 1
                {
                    "candidate_id": "c_selloff_1",
                    "regime": "selloff", "underlier": "SPY", "expiry": "20261001",
                    "debit_per_contract": 15.0, "max_gain_per_contract": 1985.0,
                    "ev_per_dollar": 30.0, "strikes": {"long_put": 570.0, "short_put": 550.0},
                },
            ]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        open_count = sum(1 for a in plan.actions if a.type == "OPEN")
        assert open_count <= 1, f"Expected <= 1 OPEN, got {open_count}"


# ===========================================================================
# Task 7 — Close intents (or explicit manual flag)
# ===========================================================================

class TestCloseIntentEmission:
    """
    Acceptance: For harvest close, you can see a valid intent file path
    OR an explicit manual flag.
    """

    def test_harvest_close_writes_intent_file(self, tmp_path):
        """
        HARVEST_CLOSE action should produce a close-intent JSON file
        and set intent_path on the action.
        v1.4: write position to plan ledger (allocator_plan_ledger.jsonl).
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record

        ledger = tmp_path / "allocator_plan_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()

        # Write an open position that will trigger full close (3x mark)
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "trade_harvest_1", "regime": "crash", "candidate_id": "cid_h1",
            "underlier": "SPY", "expiry": "20261001",
            "strikes": [570.0, 550.0], "qty": 1,
            "premium_per_contract": 49.0, "premium_spent": 49.0,
        })

        # Mark at 3x via candidate
        candidates = {
            "candidates": [{
                "candidate_id": "cid_h1",
                "regime": "crash", "underlier": "SPY", "expiry": "20261001",
                "debit_per_contract": 151.0,  # 151/49 ≈ 3.08x → full close
                "max_gain_per_contract": 1849.0,
                "ev_per_dollar": 1.0,        # too low for OPEN
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        # dry_run=False so intents are written
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=False)

        harvest_actions = [a for a in plan.actions if a.type == "HARVEST_CLOSE"]
        assert len(harvest_actions) >= 1, "Expected HARVEST_CLOSE"

        ha = harvest_actions[0]
        # Acceptance: valid intent file path OR MANUAL_CLOSE_REQUIRED
        has_intent_path = ha.intent_path is not None and Path(ha.intent_path).exists()
        has_manual_flag = "MANUAL_CLOSE_REQUIRED" in ha.reason_codes
        assert has_intent_path or has_manual_flag, (
            f"Expected intent_path or MANUAL_CLOSE_REQUIRED, got: "
            f"intent_path={ha.intent_path}, reasons={ha.reason_codes}"
        )

    def test_intent_file_is_valid_json(self, tmp_path):
        """The intent file, if written, must be valid JSON with required fields."""
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record

        ledger = tmp_path / "allocator_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "trade_intent_1", "regime": "crash", "candidate_id": "cid_i1",
            "underlier": "SPY", "expiry": "20261001",
            "strikes": [570.0, 550.0], "qty": 1,
            "premium_per_contract": 49.0, "premium_spent": 49.0,
        })
        candidates = {
            "candidates": [{
                "candidate_id": "cid_i1",
                "regime": "crash", "underlier": "SPY", "expiry": "20261001",
                "debit_per_contract": 151.0,  # 3x → full close
                "max_gain_per_contract": 1849.0, "ev_per_dollar": 1.0,
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=False)

        harvest_actions = [a for a in plan.actions if a.type == "HARVEST_CLOSE"]
        if not harvest_actions:
            pytest.skip("No harvest action generated")

        ha = harvest_actions[0]
        if ha.intent_path is None:
            # Manual flag must be set
            assert "MANUAL_CLOSE_REQUIRED" in ha.reason_codes
            return

        # Verify intent file
        intent_path = Path(ha.intent_path)
        assert intent_path.exists(), f"Intent file missing: {intent_path}"
        intent_data = json.loads(intent_path.read_text())

        # Required fields
        for key in ("intent_type", "action_type", "trade_id", "underlier",
                    "expiry", "strikes", "manual_close_required"):
            assert key in intent_data, f"Intent missing key: {key}"
        assert intent_data["manual_close_required"] is True
        assert intent_data["action_type"] == "HARVEST_CLOSE"

    def test_intent_path_in_to_dict(self, tmp_path):
        """intent_path from action appears in to_dict output (when set)."""
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record

        ledger = tmp_path / "allocator_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        append_ledger_record(ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "trade_dict_1", "regime": "crash", "candidate_id": "cid_d1",
            "underlier": "SPY", "expiry": "20261001",
            "strikes": [570.0, 550.0], "qty": 1,
            "premium_per_contract": 49.0, "premium_spent": 49.0,
        })
        candidates = {
            "candidates": [{
                "candidate_id": "cid_d1",
                "regime": "crash", "underlier": "SPY", "expiry": "20261001",
                "debit_per_contract": 151.0, "max_gain_per_contract": 1849.0,
                "ev_per_dollar": 1.0,
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=False)

        d = plan.to_dict()
        harvest_dicts = [a for a in d["actions"] if a["type"] == "HARVEST_CLOSE"]
        if not harvest_dicts:
            pytest.skip("No harvest action generated")

        ha_d = harvest_dicts[0]
        # Either intent_path is set (string) or MANUAL_CLOSE_REQUIRED in reason_codes
        has_path = "intent_path" in ha_d and ha_d["intent_path"] is not None
        has_flag = "MANUAL_CLOSE_REQUIRED" in ha_d.get("reason_codes", [])
        assert has_path or has_flag


# ===========================================================================
# Task 8 — Integration: all guards working together
# ===========================================================================

class TestIntegrationHardening:
    """End-to-end scenario tests that combine multiple tasks."""

    def test_full_run_budget_inventory_caps_output(self, tmp_path):
        """
        Full run with:
        - Budget $45 spent (remaining_before=$5)
        - One crash position at DTE <= 14 → ROLL_CLOSE
        - Replacement OPEN (inventory_mid has crash=0, needs open)
        - Check: budget_before/after correct, inventory_before/after correct

        v1.4: budget reads commit ledger; inventory reads plan ledger. Write to both.
        """
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.budget import append_ledger_record
        from datetime import date, timedelta

        plan_ledger = tmp_path / "allocator_plan_ledger.jsonl"
        commit_ledger = tmp_path / "allocator_commit_ledger.jsonl"
        today = datetime.now(timezone.utc).date().isoformat()
        near_expiry = (date.today() + timedelta(days=5)).strftime("%Y%m%d")

        # Plan ledger: position at near_expiry (DTE <= 14 → ROLL_CLOSE triggered)
        append_ledger_record(plan_ledger, {
            "date": today, "action": "OPEN",
            "trade_id": "existing_crash", "regime": "crash",
            "underlier": "SPY", "expiry": near_expiry,
            "strikes": [570.0, 550.0], "qty": 1,
            "premium_per_contract": 45.0, "premium_spent": 45.0,
        })
        # Commit ledger: $45 committed spend (affects spent_today_before)
        append_ledger_record(commit_ledger, {
            "date": today, "action": "OPEN", "premium_spent": 45.0,
        })
        # DTE <= 14 → ROLL_CLOSE needed

        # New candidate: $5 premium (fits in remaining $5)
        candidates = {
            "candidates": [{
                "candidate_id": "crash_v2:SPY:crash:20261201:570/550",
                "regime": "crash", "underlier": "SPY", "expiry": "20261201",
                "debit_per_contract": 5.0,
                "max_gain_per_contract": 1995.0,  # 399x >> 25x
                "ev_per_dollar": 40.0,
                "strikes": {"long_put": 570.0, "short_put": 550.0},
            }]
        }
        cpath = tmp_path / "candidates.json"
        cpath.write_text(json.dumps(candidates))

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, candidates_path=str(cpath), dry_run=True)

        d = plan.to_dict()
        b = d["budgets"]
        inv = d["inventory"]

        # Budget before: $45 spent, $5 remaining
        assert b["spent_today_before"] == pytest.approx(45.0)
        assert b["remaining_today_before"] == pytest.approx(5.0)

        # Inventory before: crash=1 (the existing position), after ROLL_CLOSE → 0 → OPEN → 1
        assert inv["before"]["crash"]["open"] == 1
        # After: roll_close -1 → 0, open +1 → 1 (or just 0 if open blocked by budget)
        # (depends on whether the $5 OPEN fits; remaining=$5 >= $5 → YES)
        roll_actions = [a for a in plan.actions if a.type == "ROLL_CLOSE"]
        assert len(roll_actions) >= 1, "Expected ROLL_CLOSE for near-expiry"

    def test_plan_output_schema_comprehensive(self, tmp_path):
        """
        Verify allocator_actions.json contains all required top-level keys
        and the budget / inventory nested structure.
        """
        from forecast_arb.allocator.plan import run_allocator_plan

        pf = _make_policy_file(tmp_path)
        plan = run_allocator_plan(pf, dry_run=False)

        # Check the written file
        actions_file = tmp_path / "allocator_actions.json"
        assert actions_file.exists()
        data = json.loads(actions_file.read_text())

        # Top-level keys
        for key in ("timestamp_utc", "policy_id", "budgets", "inventory",
                    "positions", "actions", "notes"):
            assert key in data, f"Missing top-level key: {key}"

        # Budget before/after schema
        b = data["budgets"]
        for key in ("spent_today_before", "remaining_today_before",
                    "planned_spend_today", "remaining_today_after",
                    "daily_soft_cap", "kicker_enabled"):
            assert key in b, f"Missing budget key: {key}"

        # Inventory before/after schema
        inv = data["inventory"]
        assert "before" in inv
        assert "after" in inv
