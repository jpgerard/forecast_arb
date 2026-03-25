"""
CCC v1.3 Patch Pack Tests

Tests for the v1.3 patch pack:

Task A — build_order_intent_from_candidate() produces valid OrderIntent
Task B — AllocatorAction carries underlier/regime/expiry/strikes/cluster metadata
Task C — Intent file is pure executable OrderIntent (no allocator action fields)
Task C — _simulate_inventory_after_actions() uses action.regime directly
Task D — validate_order_intent() passes on generated intents
         planned inventory increments when OPEN action present
         mock quote-only execution on generated intent

All tests are deterministic (no live IBKR, no network calls).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _make_crash_candidate(candidate_id: str = "SPY_20260402_580_560_crash") -> Dict[str, Any]:
    """Return a qualifying crash candidate matching candidates_flat.json schema."""
    return {
        "candidate_id": candidate_id,
        "underlier": "SPY",
        "regime": "crash",
        "expiry": "20260402",
        "long_strike": 580.0,
        "short_strike": 560.0,
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "computed_premium_usd": 20.0,
        "debit_per_contract": 20.0,
        "max_gain_per_contract": 2000.0,
        "ev_per_dollar": 2.0,
        "p_used_src": "implied",
        "representable": True,
        "rank": 1,
        "cluster_id": "SPY_crash_near",
    }


def _minimal_policy(policy_id: str = "ccc_v1") -> Dict[str, Any]:
    """Return a minimal policy dict matching what build_order_intent_from_candidate expects."""
    return {
        "policy_id": policy_id,
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
    }


# ---------------------------------------------------------------------------
# Task A — build_order_intent_from_candidate() produces valid OrderIntent
# ---------------------------------------------------------------------------

class TestBuildOrderIntentFromCandidate:
    """validate_order_intent() must PASS on output of build_order_intent_from_candidate()."""

    def test_basic_crash_candidate_passes_validation(self):
        """Core happy path: crash candidate → valid OrderIntent."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate
        from forecast_arb.execution.execute_trade import validate_order_intent

        candidate = _make_crash_candidate()
        policy = _minimal_policy()

        intent = build_order_intent_from_candidate(candidate, qty=1, policy=policy)

        # Must not raise
        validate_order_intent(intent)

    def test_required_fields_present(self):
        """All validate_order_intent required fields must be in output."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = _make_crash_candidate()
        policy = _minimal_policy()
        intent = build_order_intent_from_candidate(candidate, qty=2, policy=policy)

        for field in ("strategy", "symbol", "expiry", "type", "legs",
                      "qty", "limit", "tif", "guards", "intent_id"):
            assert field in intent, f"Required field '{field}' missing from OrderIntent"

    def test_intent_id_is_non_empty_string(self):
        """intent_id must be a non-empty string (SHA1 hex)."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        intent = build_order_intent_from_candidate(
            _make_crash_candidate(), qty=1, policy=_minimal_policy()
        )
        assert isinstance(intent["intent_id"], str)
        assert len(intent["intent_id"]) == 40  # SHA1 hexdigest

    def test_intent_id_is_deterministic(self):
        """Same inputs → same intent_id (no random state)."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = _make_crash_candidate()
        policy = _minimal_policy()
        intent_a = build_order_intent_from_candidate(candidate, qty=1, policy=policy)
        intent_b = build_order_intent_from_candidate(candidate, qty=1, policy=policy)
        assert intent_a["intent_id"] == intent_b["intent_id"]

    def test_qty_in_intent_matches_arg(self):
        """qty in intent must equal the qty argument passed to the builder."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        intent = build_order_intent_from_candidate(
            _make_crash_candidate(), qty=3, policy=_minimal_policy()
        )
        assert intent["qty"] == 3

    def test_legs_correct_structure(self):
        """BUY long_strike put / SELL short_strike put structure."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = _make_crash_candidate()
        intent = build_order_intent_from_candidate(candidate, qty=1, policy=_minimal_policy())

        assert len(intent["legs"]) == 2
        buy_legs = [l for l in intent["legs"] if l["action"] == "BUY"]
        sell_legs = [l for l in intent["legs"] if l["action"] == "SELL"]
        assert len(buy_legs) == 1
        assert len(sell_legs) == 1

        # BUY leg = long (higher) strike
        assert buy_legs[0]["strike"] == 580.0
        assert buy_legs[0]["right"] == "P"

        # SELL leg = short (lower) strike
        assert sell_legs[0]["strike"] == 560.0
        assert sell_legs[0]["right"] == "P"

    def test_limit_start_equals_debit(self):
        """limit.start must equal debit_per_contract from candidate."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = _make_crash_candidate()  # debit_per_contract = 20.0
        intent = build_order_intent_from_candidate(candidate, qty=1, policy=_minimal_policy())

        # Per-share convention: debit_per_contract=20.0 → limit.start=0.20/share (IBKR lmtPrice)
        assert intent["limit"]["start"] == pytest.approx(0.20, abs=0.001)
        assert intent["limit"]["max"] == pytest.approx(round(0.20 * 1.02, 4), abs=0.001)
        # Per-contract metadata preserved for reporting
        assert intent["limit"]["limit_per_contract_start"] == pytest.approx(20.0)
        assert intent["limit"]["limit_per_contract_max"] == pytest.approx(round(20.0 * 1.02, 2))

    def test_strategy_from_policy_id(self):
        """strategy field must match policy.policy_id."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        policy = _minimal_policy("my_custom_policy")
        intent = build_order_intent_from_candidate(_make_crash_candidate(), qty=1, policy=policy)
        assert intent["strategy"] == "my_custom_policy"

    def test_strikes_dict_schema_works_too(self):
        """Candidate using nested strikes dict (not flat long_strike/short_strike)."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate
        from forecast_arb.execution.execute_trade import validate_order_intent

        candidate = {
            "candidate_id": "SPY_20260402_590_570_crash",
            "underlier": "SPY",
            "regime": "crash",
            "expiry": "20260402",
            # No flat long_strike/short_strike — use nested dict
            "strikes": {"long_put": 590.0, "short_put": 570.0},
            "debit_per_contract": 15.0,
        }
        intent = build_order_intent_from_candidate(
            candidate, qty=1, policy=_minimal_policy()
        )
        # Must pass validation even with nested schema
        validate_order_intent(intent)

        buy_legs = [l for l in intent["legs"] if l["action"] == "BUY"]
        assert buy_legs[0]["strike"] == 590.0


# ---------------------------------------------------------------------------
# Task B — AllocatorAction carries metadata, inventory simulation uses it
# ---------------------------------------------------------------------------

class TestInventorySimulationUsesActionRegime:
    """_simulate_inventory_after_actions() must use action.regime directly."""

    def test_crash_open_increments_single_action(self):
        """One OPEN action with regime='crash' → crash_open increases by 1."""
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        from forecast_arb.allocator.types import (
            ActionType, AllocatorAction, InventoryState,
        )

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)
        open_action = AllocatorAction(
            type=ActionType.OPEN,
            candidate_id="SPY_20260402_580_560_crash",
            regime="crash",  # v1.3 explicit regime field
            qty=1,
            premium=20.0,
        )

        result = _simulate_inventory_after_actions(inv=inv, actions=[open_action], positions=[])

        assert result.crash_open == 1, (
            f"Expected crash_open=1 after one OPEN with regime='crash', got {result.crash_open}"
        )
        assert result.selloff_open == 0

    def test_selloff_open_increments_single_action(self):
        """One OPEN action with regime='selloff' → selloff_open increases by 1."""
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        from forecast_arb.allocator.types import (
            ActionType, AllocatorAction, InventoryState,
        )

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)
        open_action = AllocatorAction(
            type=ActionType.OPEN,
            candidate_id="SPY_20260402_580_560_selloff",
            regime="selloff",  # v1.3 explicit regime field
            qty=1,
            premium=10.0,
        )

        result = _simulate_inventory_after_actions(inv=inv, actions=[open_action], positions=[])

        assert result.selloff_open == 1
        assert result.crash_open == 0

    def test_regime_field_overrides_heuristic_extraction(self):
        """regime field beats heuristic even if candidate_id doesn't contain regime name."""
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        from forecast_arb.allocator.types import (
            ActionType, AllocatorAction, InventoryState,
        )

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)
        # candidate_id looks like selloff but regime field says crash
        open_action = AllocatorAction(
            type=ActionType.OPEN,
            candidate_id="some_generic_id_without_regime_in_name",
            regime="crash",
            qty=1,
            premium=20.0,
        )

        result = _simulate_inventory_after_actions(inv=inv, actions=[open_action], positions=[])
        assert result.crash_open == 1, "regime='crash' field should have been used, not heuristic"

    def test_planned_inventory_in_allocator_actions_json(self, tmp_path):
        """
        End-to-end: when OPEN is planned, inventory.planned.crash.open must be > actual.
        """
        import yaml
        from unittest.mock import patch
        from forecast_arb.allocator.plan import run_allocator_plan

        # Dirs
        intents_dir = tmp_path / "intents" / "allocator"
        output_dir = tmp_path / "runs" / "allocator"
        ledger_dir = tmp_path / "runs" / "allocator"

        # Build policy
        policy_dict = _minimal_policy()
        policy_dict.update({
            "intents_dir": str(intents_dir),
            "output_dir": str(output_dir),
            "ledger_dir": str(ledger_dir),
        })
        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text(yaml.dump(policy_dict))

        # Candidates file
        candidates_path = tmp_path / "recommended.json"
        candidates_path.write_text(json.dumps({"selected": [_make_crash_candidate()]}))

        with patch("forecast_arb.allocator.reconcile.reconcile_positions", return_value=[]), \
             patch("forecast_arb.allocator.budget.compute_budget_state") as mb, \
             patch("forecast_arb.allocator.inventory.compute_inventory_state") as mi:

            from forecast_arb.allocator.types import BudgetState, InventoryState
            mb.return_value = BudgetState(
                monthly_baseline=1000.0, monthly_max=2000.0,
                weekly_baseline=250.0, daily_baseline=50.0,
                weekly_kicker=500.0, daily_kicker=100.0,
                kicker_enabled=True,
            )
            mi.return_value = InventoryState(crash_target=1, crash_open=0,
                                             selloff_target=1, selloff_open=0)

            plan = run_allocator_plan(
                policy_path=str(policy_path),
                candidates_path=str(candidates_path),
                dry_run=False,
            )

        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        if open_actions:
            # If OPEN was produced, planned inventory must be 1 ahead of actual
            assert plan.inventory_after is not None
            assert plan.inventory_after.crash_open > plan.inventory.crash_open, (
                "planned crash open should be > actual crash open when OPEN action produced"
            )


# ---------------------------------------------------------------------------
# Task C — Intent file is pure executable OrderIntent schema
# ---------------------------------------------------------------------------

class TestIntentFileIsExecutableSchema:
    """The intent JSON written to intents/allocator/ must pass validate_order_intent()."""

    def _write_open_intent_to_tmp(self, tmp_path) -> Path:
        """Write a single OPEN intent and return its path."""
        from forecast_arb.allocator.plan import _write_open_intents
        from forecast_arb.allocator.types import (
            AllocatorPlan, AllocatorAction, ActionType,
            BudgetState, InventoryState,
        )

        intents_dir = tmp_path / "intents" / "allocator"

        budget = BudgetState(
            monthly_baseline=1000.0, monthly_max=2000.0,
            weekly_baseline=250.0, daily_baseline=50.0,
            weekly_kicker=500.0, daily_kicker=100.0,
        )
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)

        candidate = _make_crash_candidate()
        open_action = AllocatorAction(
            type=ActionType.OPEN,
            candidate_id=candidate["candidate_id"],
            qty=1,
            premium=20.0,
            regime="crash",
            underlier="SPY",
            expiry="20260402",
            long_strike=580.0,
            short_strike=560.0,
            cluster_id="SPY_crash_near",
        )

        plan = AllocatorPlan(
            timestamp_utc="2026-03-02T10:00:00+00:00",
            policy_id="ccc_v1",
            budgets=budget,
            inventory=inv,
            inventory_after=inv,
            positions=[],
            actions=[open_action],
        )

        candidates_data = {"selected": [candidate]}
        policy = _minimal_policy()

        _write_open_intents(plan, intents_dir, candidates_data=candidates_data, policy=policy)
        return open_action.intent_path

    def test_intent_file_passes_validate_order_intent(self, tmp_path):
        """The written intent file must pass validate_order_intent()."""
        from forecast_arb.execution.execute_trade import validate_order_intent

        intent_path = self._write_open_intent_to_tmp(tmp_path)
        assert intent_path is not None, "intent_path must be set on action"

        intent = json.loads(Path(intent_path).read_text())
        # Must not raise
        validate_order_intent(intent)

    def test_intent_file_has_no_allocator_action_fields(self, tmp_path):
        """
        Intent file must NOT contain allocator-only fields like:
        intent_type, action_type, reason_codes, convexity, premium_per_contract.
        These belong only in allocator_actions.json.
        """
        intent_path = self._write_open_intent_to_tmp(tmp_path)
        intent = json.loads(Path(intent_path).read_text())

        forbidden_fields = ["intent_type", "action_type", "reason_codes",
                            "convexity", "premium_per_contract"]
        for field in forbidden_fields:
            assert field not in intent, (
                f"Allocator-only field '{field}' must NOT be in intent file (Task C). "
                f"Found in: {list(intent.keys())}"
            )

    def test_intent_file_has_executable_fields(self, tmp_path):
        """Intent file must have all fields needed to call execute_trade."""
        intent_path = self._write_open_intent_to_tmp(tmp_path)
        intent = json.loads(Path(intent_path).read_text())

        required = ["strategy", "symbol", "expiry", "type", "legs",
                    "qty", "limit", "tif", "guards", "intent_id"]
        for field in required:
            assert field in intent, f"Executable field '{field}' missing from intent file"

    def test_intent_file_legs_have_required_fields(self, tmp_path):
        """Each leg in intent file must have action, right, strike."""
        intent_path = self._write_open_intent_to_tmp(tmp_path)
        intent = json.loads(Path(intent_path).read_text())

        for i, leg in enumerate(intent["legs"]):
            for leg_field in ("action", "right", "strike"):
                assert leg_field in leg, (
                    f"Leg {i} missing required field '{leg_field}' in intent file"
                )

    def test_full_plan_intent_passes_validation(self, tmp_path):
        """
        End-to-end: OPEN intent written by run_allocator_plan() must pass
        validate_order_intent().
        """
        import yaml
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.execution.execute_trade import validate_order_intent

        intents_dir = tmp_path / "intents" / "allocator"
        output_dir = tmp_path / "runs" / "allocator"
        ledger_dir = tmp_path / "runs" / "allocator"

        policy_dict = _minimal_policy()
        policy_dict.update({
            "intents_dir": str(intents_dir),
            "output_dir": str(output_dir),
            "ledger_dir": str(ledger_dir),
        })
        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text(yaml.dump(policy_dict))

        candidates_path = tmp_path / "recommended.json"
        candidates_path.write_text(json.dumps({"selected": [_make_crash_candidate()]}))

        with patch("forecast_arb.allocator.reconcile.reconcile_positions", return_value=[]), \
             patch("forecast_arb.allocator.budget.compute_budget_state") as mb, \
             patch("forecast_arb.allocator.inventory.compute_inventory_state") as mi:

            from forecast_arb.allocator.types import BudgetState, InventoryState
            mb.return_value = BudgetState(
                monthly_baseline=1000.0, monthly_max=2000.0,
                weekly_baseline=250.0, daily_baseline=50.0,
                weekly_kicker=500.0, daily_kicker=100.0,
                kicker_enabled=True,
            )
            mi.return_value = InventoryState(crash_target=1, crash_open=0,
                                             selloff_target=1, selloff_open=0)

            plan = run_allocator_plan(
                policy_path=str(policy_path),
                candidates_path=str(candidates_path),
                dry_run=False,
            )

        open_actions = [a for a in plan.actions if a.type == "OPEN"]
        assert len(open_actions) >= 1, "Expected at least one OPEN action"

        for action in open_actions:
            assert action.intent_path is not None, "OPEN action must have intent_path set"
            intent_file = Path(action.intent_path)
            assert intent_file.exists(), f"Intent file not found: {intent_file}"

            intent = json.loads(intent_file.read_text())
            # This is the core assertion: validate_order_intent must not raise
            validate_order_intent(intent)


# ---------------------------------------------------------------------------
# Task D — Mock quote-only execution on generated intent
# ---------------------------------------------------------------------------

class TestMockQuoteOnlyExecution:
    """
    validate_order_intent() path in execute_trade, invoked with a generated intent.
    Uses mocked IBKR connection (no real network).
    """

    def _make_intent_file(self, tmp_path) -> str:
        """Build and write a real OrderIntent file, return its path."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = _make_crash_candidate()
        policy = _minimal_policy()
        intent = build_order_intent_from_candidate(candidate, qty=1, policy=policy)

        intent_path = tmp_path / "OPEN_crash_test.json"
        intent_path.write_text(json.dumps(intent, indent=2))
        return str(intent_path)

    def test_validate_order_intent_does_not_raise(self, tmp_path):
        """
        validate_order_intent() called on a file produced by
        build_order_intent_from_candidate() must not raise.
        """
        from forecast_arb.execution.execute_trade import (
            load_order_intent,
            validate_order_intent,
        )

        intent_path = self._make_intent_file(tmp_path)
        intent = load_order_intent(intent_path)
        validate_order_intent(intent)  # must not raise

    def test_intent_limits_are_correct(self, tmp_path):
        """limit.start == debit_per_contract, limit.max == debit * 1.02."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = _make_crash_candidate()  # debit = 20.0
        intent = build_order_intent_from_candidate(candidate, qty=1, policy=_minimal_policy())

        # Per-share convention: debit_per_contract=20.0 → limit.start=0.20/share (IBKR lmtPrice)
        assert intent["limit"]["start"] == pytest.approx(0.20, abs=0.001)
        assert abs(intent["limit"]["max"] - round(0.20 * 1.02, 4)) < 0.001
        # Per-contract metadata preserved for reporting
        assert intent["limit"].get("limit_per_contract_start") == pytest.approx(20.0)

    def test_intent_guards_are_present(self, tmp_path):
        """guards dict must have max_debit, max_spread_width, min_dte."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        intent = build_order_intent_from_candidate(
            _make_crash_candidate(), qty=1, policy=_minimal_policy()
        )
        guards = intent["guards"]
        assert "max_debit" in guards
        assert "max_spread_width" in guards
        assert "min_dte" in guards
        assert guards["min_dte"] == 7

    def test_execute_trade_validate_path_mocked(self, tmp_path):
        """
        Mocked IBKR path: load intent + validate works up to IBKR connection.
        This verifies the execute_trade validation layer accepts our intent format.
        """
        from forecast_arb.execution.execute_trade import load_order_intent, validate_order_intent

        intent_path = self._make_intent_file(tmp_path)

        # This mirrors what execute_order_intent does at the start
        intent = load_order_intent(intent_path)

        # validate_order_intent is the gate before any IBKR connection
        # If this passes, the intent is executable (connection aside)
        try:
            validate_order_intent(intent)
            validation_passed = True
        except ValueError as e:
            validation_passed = False
            pytest.fail(f"validate_order_intent failed for generated intent: {e}")

        assert validation_passed, "Generated intent must pass validate_order_intent()"


# ---------------------------------------------------------------------------
# Task B — AllocatorAction carries correct metadata fields
# ---------------------------------------------------------------------------

class TestAllocatorActionMetadataFields:
    """AllocatorAction must carry underlier, regime, expiry, strikes, cluster_id."""

    def test_open_action_has_regime_field(self):
        """OPEN action generated by open_plan has regime set explicitly."""
        from forecast_arb.allocator.open_plan import generate_open_actions
        from forecast_arb.allocator.types import BudgetState, InventoryState

        budget = BudgetState(
            monthly_baseline=1000.0, monthly_max=2000.0,
            weekly_baseline=250.0, daily_baseline=50.0,
            weekly_kicker=500.0, daily_kicker=100.0,
            kicker_enabled=True,
        )
        inventory = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)

        policy = _minimal_policy()
        candidates_data = {"selected": [_make_crash_candidate()]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1, "Expected one OPEN action"
        action = actions[0]
        assert action.regime == "crash", f"Expected regime='crash', got {action.regime!r}"

    def test_open_action_has_underlier_expiry_strikes(self):
        """OPEN action must carry underlier, expiry, long_strike, short_strike."""
        from forecast_arb.allocator.open_plan import generate_open_actions
        from forecast_arb.allocator.types import BudgetState, InventoryState

        budget = BudgetState(
            monthly_baseline=1000.0, monthly_max=2000.0,
            weekly_baseline=250.0, daily_baseline=50.0,
            weekly_kicker=500.0, daily_kicker=100.0,
            kicker_enabled=True,
        )
        inventory = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=0)

        policy = _minimal_policy()
        candidate = _make_crash_candidate()
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1
        action = actions[0]

        assert action.underlier == "SPY", f"underlier mismatch: {action.underlier!r}"
        assert action.expiry == "20260402", f"expiry mismatch: {action.expiry!r}"
        assert action.long_strike == 580.0, f"long_strike mismatch: {action.long_strike}"
        assert action.short_strike == 560.0, f"short_strike mismatch: {action.short_strike}"
        assert action.cluster_id == "SPY_crash_near", f"cluster_id mismatch: {action.cluster_id!r}"
