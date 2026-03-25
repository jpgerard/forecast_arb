"""
CCC v1.6 "Always-On but Disciplined" Patch Pack — Test Suite
Task D: Deterministic tests for inventory-aware thresholds.

Tests:
  1. Crash fill (empty inventory) uses relaxed fill_when_empty thresholds → OPEN allowed
  2. Crash add (full inventory) uses strict add_when_full thresholds → same candidate REJECTED
  3. Selloff fill uses relaxed fill_when_empty thresholds → OPEN allowed
  4. Selloff add uses strict add_when_full thresholds → same candidate REJECTED
  5. Backward compat: legacy flat-key policy treated as add_when_full
  6. Trace completeness: HOLD with candidates → candidates_evaluated non-empty + tier + thresholds_used
"""
from __future__ import annotations

import io
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from forecast_arb.allocator.open_plan import _evaluate_candidate, generate_open_actions
from forecast_arb.allocator.policy import (
    PolicyError,
    _normalize_thresholds,
    get_effective_thresholds,
    load_policy,
)
from forecast_arb.allocator.types import BudgetState, InventoryState


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

def _make_policy_new_structure() -> Dict[str, Any]:
    """Policy dict with the v1.6 nested fill_when_empty/add_when_full structure."""
    return {
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
                "fill_when_empty": {
                    "ev_per_dollar_implied": 1.45,
                    "ev_per_dollar_external": 0.45,
                    "convexity_multiple": 20.0,
                },
                "add_when_full": {
                    "ev_per_dollar_implied": 1.60,
                    "ev_per_dollar_external": 0.50,
                    "convexity_multiple": 25.0,
                },
            },
            "selloff": {
                "fill_when_empty": {
                    "ev_per_dollar_implied": 1.20,
                    "ev_per_dollar_external": 0.30,
                    "convexity_multiple": 12.0,
                },
                "add_when_full": {
                    "ev_per_dollar_implied": 1.30,
                    "ev_per_dollar_external": 0.30,
                    "convexity_multiple": 15.0,
                },
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
        "limits": {"max_open_actions_per_day": 99, "max_close_actions_per_day": 99},
        "close_liquidity_guard": {"max_width_pct": 1.0},
    }


def _make_policy_legacy_structure() -> Dict[str, Any]:
    """Policy dict with the legacy flat threshold keys (backward compat)."""
    p = _make_policy_new_structure()
    # Replace nested thresholds with flat legacy keys
    p["thresholds"] = {
        "crash": {
            "ev_per_dollar_implied": 1.60,
            "ev_per_dollar_external": 0.50,
            "convexity_multiple": 25.0,
        },
        "selloff": {
            "ev_per_dollar_implied": 1.30,
            "ev_per_dollar_external": 0.30,
            "convexity_multiple": 15.0,
        },
    }
    return p


def _make_generous_budget() -> BudgetState:
    """A budget that never blocks trades."""
    return BudgetState(
        monthly_baseline=1000.0,
        monthly_max=2000.0,
        weekly_baseline=250.0,
        daily_baseline=50.0,
        weekly_kicker=500.0,
        daily_kicker=100.0,
        spent_month=0.0,
        spent_week=0.0,
        spent_today=0.0,
        kicker_enabled=False,
    )


def _make_crash_candidate(
    ev_per_dollar: float = 1.46,
    convexity_factor: float = 20.1,   # multiple = max_gain/premium = factor
    premium: float = 10.0,
) -> Dict[str, Any]:
    """
    Candidate that passes fill_when_empty (1.45/20x) but fails add_when_full (1.60/25x).
    convexity_factor is the max_gain/premium ratio (= convexity multiple).
    """
    return {
        "candidate_id": "crash_test_cand",
        "regime": "crash",
        "underlier": "SPY",
        "expiry": "20260320",
        "long_strike": 580.0,
        "short_strike": 560.0,
        "debit_per_contract": premium,
        "max_gain_per_contract": premium * convexity_factor,
        "ev_per_dollar": ev_per_dollar,
        "p_used_src": "implied",
    }


def _make_selloff_candidate(
    ev_per_dollar: float = 1.21,
    convexity_factor: float = 12.1,
    premium: float = 10.0,
) -> Dict[str, Any]:
    """
    Candidate that passes fill_when_empty (1.20/12x) but fails add_when_full (1.30/15x).
    """
    return {
        "candidate_id": "selloff_test_cand",
        "regime": "selloff",
        "underlier": "SPY",
        "expiry": "20260320",
        "long_strike": 580.0,
        "short_strike": 560.0,
        "debit_per_contract": premium,
        "max_gain_per_contract": premium * convexity_factor,
        "ev_per_dollar": ev_per_dollar,
        "p_used_src": "implied",
    }


# ---------------------------------------------------------------------------
# Test 1: Crash fill uses relaxed thresholds → OPEN allowed
# ---------------------------------------------------------------------------

class TestCrashFillUsesRelaxedThresholds:
    """crash_open=0 < crash_target=1  → tier=fill_when_empty (ev=1.45, conv=20x)"""

    def test_open_allowed_with_fill_thresholds(self):
        """EV 1.46 ≥ 1.45 and conv 20.1x ≥ 20.0x → should OPEN."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=0,
            selloff_target=1, selloff_open=1,  # selloff is full
        )
        budget = _make_generous_budget()
        candidate = _make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)

        eff = get_effective_thresholds(policy, "crash", inv)
        assert eff["tier"] == "fill_when_empty", f"Expected fill_when_empty, got {eff['tier']}"
        assert eff["ev_implied"] == pytest.approx(1.45)
        assert eff["convexity_multiple"] == pytest.approx(20.0)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is not None, f"Expected OPEN action but got REJECTED: {reason}"
        assert action.type == "OPEN"

    def test_tier_is_fill_when_empty_when_inventory_zero(self):
        """Tier selection is purely based on crash_open < crash_target."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=0,
            selloff_target=1, selloff_open=0,
        )
        eff = get_effective_thresholds(policy, "crash", inv)
        assert eff["tier"] == "fill_when_empty"

    def test_tier_is_add_when_full_when_inventory_met(self):
        """When crash_open == crash_target, tier = add_when_full."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,
            selloff_target=1, selloff_open=0,
        )
        eff = get_effective_thresholds(policy, "crash", inv)
        assert eff["tier"] == "add_when_full"
        assert eff["ev_implied"] == pytest.approx(1.60)
        assert eff["convexity_multiple"] == pytest.approx(25.0)

    def test_generate_open_actions_opens_with_fill_thresholds(self):
        """generate_open_actions produces an OPEN when crash inventory is empty."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=0,
            selloff_target=1, selloff_open=1,  # selloff at target
        )
        budget = _make_generous_budget()
        candidates_data = {"selected": [_make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inv,
        )

        assert len(actions) == 1, f"Expected 1 OPEN action, got {len(actions)}: {actions}"
        assert actions[0].type == "OPEN"
        assert actions[0].regime == "crash"


# ---------------------------------------------------------------------------
# Test 2: Crash add uses strict thresholds → same candidate REJECTED
# ---------------------------------------------------------------------------

class TestCrashAddUsesStrictThresholds:
    """crash_open=1 == crash_target=1  → tier=add_when_full (ev=1.60, conv=25x)"""

    def test_open_rejected_with_add_thresholds(self):
        """EV 1.46 < 1.60 → should be REJECTED under add_when_full."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,
            selloff_target=1, selloff_open=1,
        )
        budget = _make_generous_budget()
        candidate = _make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)

        eff = get_effective_thresholds(policy, "crash", inv)
        assert eff["tier"] == "add_when_full"

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is None, "Expected REJECTED but got OPEN"
        assert "EV_BELOW_THRESHOLD" in reason or "CONVEXITY_TOO_LOW" in reason

    def test_generate_open_no_open_when_inventory_full(self):
        """generate_open_actions produces no OPEN when crash inventory is at target."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,
            selloff_target=1, selloff_open=1,
        )
        budget = _make_generous_budget()
        candidates_data = {"selected": [_make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inv,
        )
        # Inventory is at target for both regimes — no OPEN expected
        assert len(actions) == 0, f"Expected 0 OPEN actions, got {len(actions)}"

    def test_high_ev_candidate_passes_add_thresholds(self):
        """A genuinely good candidate (EV 1.65, conv 26x) still passes add_when_full."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,
            selloff_target=1, selloff_open=1,
        )
        # We can't trigger it because inventory is full for both regimes;
        # so test the threshold values directly via _evaluate_candidate
        budget = _make_generous_budget()
        candidate = _make_crash_candidate(ev_per_dollar=1.65, convexity_factor=26.0)
        eff = get_effective_thresholds(policy, "crash", inv)

        # Even add_when_full should pass for a genuinely good trade
        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is not None, f"Genuinely good candidate should pass add_when_full, got: {reason}"


# ---------------------------------------------------------------------------
# Test 3: Selloff fill vs add
# ---------------------------------------------------------------------------

class TestSelloffFillVsAdd:
    """Mirrors crash tests for selloff regime (fill=1.20/12x vs add=1.30/15x)."""

    def test_selloff_fill_allows_marginal_candidate(self):
        """EV 1.21 ≥ 1.20 and conv 12.1x ≥ 12.0x → passes fill_when_empty."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,   # crash at target
            selloff_target=1, selloff_open=0,   # selloff empty
        )
        budget = _make_generous_budget()
        candidate = _make_selloff_candidate(ev_per_dollar=1.21, convexity_factor=12.1)

        eff = get_effective_thresholds(policy, "selloff", inv)
        assert eff["tier"] == "fill_when_empty"
        assert eff["ev_implied"] == pytest.approx(1.20)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="selloff",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is not None, f"Expected OPEN but got REJECTED: {reason}"

    def test_selloff_add_rejects_marginal_candidate(self):
        """EV 1.21 < 1.30 → fails add_when_full for selloff."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,
            selloff_target=1, selloff_open=1,   # selloff at target
        )
        budget = _make_generous_budget()
        candidate = _make_selloff_candidate(ev_per_dollar=1.21, convexity_factor=12.1)

        eff = get_effective_thresholds(policy, "selloff", inv)
        assert eff["tier"] == "add_when_full"
        assert eff["ev_implied"] == pytest.approx(1.30)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="selloff",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is None, f"Expected REJECTED but got OPEN"
        assert "EV_BELOW_THRESHOLD" in reason

    def test_selloff_generate_open_uses_fill_thresholds(self):
        """generate_open_actions for empty selloff inventory uses fill thresholds."""
        policy = _make_policy_new_structure()
        inv = InventoryState(
            crash_target=1, crash_open=1,
            selloff_target=1, selloff_open=0,
        )
        budget = _make_generous_budget()
        # EV 1.21 would fail add (1.30) but pass fill (1.20)
        candidates_data = {"selected": [_make_selloff_candidate(ev_per_dollar=1.21, convexity_factor=12.1)]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inv,
        )
        assert len(actions) == 1, f"Expected 1 OPEN, got {len(actions)}"
        assert actions[0].regime == "selloff"

    def test_selloff_conv_boundary(self):
        """Convexity exactly at threshold (12.0x) passes fill_when_empty."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=1, selloff_target=1, selloff_open=0)
        budget = _make_generous_budget()
        # conv = 12.0x exactly — should pass (>=)
        candidate = _make_selloff_candidate(ev_per_dollar=1.25, convexity_factor=12.0)
        eff = get_effective_thresholds(policy, "selloff", inv)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="selloff",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is not None, f"Boundary case conv=12.0x should pass fill: {reason}"


# ---------------------------------------------------------------------------
# Test 4: Backward compatibility — legacy flat-key policy
# ---------------------------------------------------------------------------

class TestBackwardCompatLegacyThresholds:
    """Legacy flat keys must be treated as add_when_full; fill_when_empty = same."""

    def test_normalize_thresholds_creates_nested_structure(self):
        """_normalize_thresholds converts flat keys to fill/add_when_full."""
        policy = _make_policy_legacy_structure()
        _normalize_thresholds(policy)

        crash_t = policy["thresholds"]["crash"]
        assert "fill_when_empty" in crash_t, "fill_when_empty missing after normalization"
        assert "add_when_full" in crash_t, "add_when_full missing after normalization"
        # Both tiers should be equal (conservative default)
        assert crash_t["fill_when_empty"] == crash_t["add_when_full"]
        assert crash_t["add_when_full"]["ev_per_dollar_implied"] == pytest.approx(1.60)

    def test_legacy_policy_inv_empty_still_uses_add_when_full_values(self):
        """With legacy flat keys, fill_when_empty == add_when_full (same strict thresholds)."""
        policy = _make_policy_legacy_structure()
        _normalize_thresholds(policy)  # simulate what load_policy does

        inv_empty = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        eff = get_effective_thresholds(policy, "crash", inv_empty)

        # fill_when_empty tier selected, but values = add_when_full (both same after normalization)
        assert eff["tier"] == "fill_when_empty"
        assert eff["ev_implied"] == pytest.approx(1.60)   # same as add_when_full
        assert eff["convexity_multiple"] == pytest.approx(25.0)

    def test_legacy_policy_marginal_candidate_rejected(self):
        """With legacy policy, EV 1.46 < 1.60 → still rejected (no relaxed tier)."""
        policy = _make_policy_legacy_structure()
        _normalize_thresholds(policy)

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        budget = _make_generous_budget()
        candidate = _make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)
        eff = get_effective_thresholds(policy, "crash", inv)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        # legacy policy = strict everywhere — same candidate should be REJECTED
        assert action is None, f"Expected REJECTED under legacy policy, got OPEN"

    def test_legacy_policy_good_candidate_passes(self):
        """With legacy policy, EV 1.65 / conv 26x passes the strict add_when_full thresholds."""
        policy = _make_policy_legacy_structure()
        _normalize_thresholds(policy)

        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        budget = _make_generous_budget()
        candidate = _make_crash_candidate(ev_per_dollar=1.65, convexity_factor=26.0)
        eff = get_effective_thresholds(policy, "crash", inv)

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff,
        )
        assert action is not None, f"Good candidate should pass legacy policy: {reason}"


# ---------------------------------------------------------------------------
# Test 5: Trace completeness
# ---------------------------------------------------------------------------

class TestTraceCompleteness:
    """On HOLD with candidates present, rejection log is non-empty and contains v1.6 fields."""

    def _run_generate_with_rejection_log(
        self,
        policy: Dict[str, Any],
        inv: InventoryState,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Helper: call generate_open_actions and return the rejection log."""
        budget = _make_generous_budget()
        rejection_log: List[Dict[str, Any]] = []
        candidates_data = {"selected": candidates}
        generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inv,
            rejection_log=rejection_log,
        )
        return rejection_log

    def test_rejection_log_non_empty_when_candidates_rejected(self):
        """Rejection log contains at least one entry when candidates are evaluated."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        # Candidate that fails add_when_full and is below fill_when_empty EV
        candidates = [_make_crash_candidate(ev_per_dollar=1.10, convexity_factor=15.0)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        assert len(log) >= 1, "rejection_log must be non-empty when candidates evaluated"

    def test_rejection_log_has_tier_field(self):
        """Each rejection log entry must include 'tier'."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        candidates = [_make_crash_candidate(ev_per_dollar=1.10, convexity_factor=15.0)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        for entry in log:
            assert "tier" in entry, f"'tier' missing in rejection log entry: {entry}"
            assert entry["tier"] in ("fill_when_empty", "add_when_full"), \
                f"Unexpected tier value: {entry['tier']}"

    def test_rejection_log_has_delta_fields(self):
        """Each rejection log entry must include delta_ev, delta_conv, passes_ev, passes_convexity."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        candidates = [_make_crash_candidate(ev_per_dollar=1.44, convexity_factor=19.2)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        for entry in log:
            assert "delta_ev" in entry, f"'delta_ev' missing: {entry}"
            assert "delta_conv" in entry, f"'delta_conv' missing: {entry}"
            assert "passes_ev" in entry, f"'passes_ev' missing: {entry}"
            assert "passes_convexity" in entry, f"'passes_convexity' missing: {entry}"

    def test_delta_ev_computed_correctly(self):
        """delta_ev = ev_per_dollar - ev_threshold_used (negative when failing)."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        # EV 1.44, fill threshold = 1.45  → delta = 1.44 - 1.45 = -0.01
        candidates = [_make_crash_candidate(ev_per_dollar=1.44, convexity_factor=19.2)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        assert len(log) == 1
        entry = log[0]

        expected_delta_ev = round(1.44 - 1.45, 4)  # -0.01
        assert entry["delta_ev"] == pytest.approx(expected_delta_ev, abs=0.001)
        assert entry["passes_ev"] is False
        assert entry["tier"] == "fill_when_empty"

    def test_rejection_log_has_thresholds_used(self):
        """Each entry must include ev_threshold_used and conv_threshold_used."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        candidates = [_make_crash_candidate(ev_per_dollar=1.44, convexity_factor=19.2)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        entry = log[0]
        assert "ev_threshold_used" in entry, f"'ev_threshold_used' missing: {entry}"
        assert "conv_threshold_used" in entry, f"'conv_threshold_used' missing: {entry}"
        # fill_when_empty thresholds for crash
        assert entry["ev_threshold_used"] == pytest.approx(1.45)
        assert entry["conv_threshold_used"] == pytest.approx(20.0)

    def test_decision_field_present(self):
        """Each entry must have a 'decision' field of APPROVE or REJECT."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        candidates = [_make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        for entry in log:
            assert "decision" in entry, f"'decision' missing: {entry}"
            assert entry["decision"] in ("APPROVE", "REJECT"), \
                f"Unexpected decision: {entry['decision']}"

    def test_approved_entry_has_decision_approve(self):
        """Passing candidate has decision=APPROVE in the log."""
        policy = _make_policy_new_structure()
        inv = InventoryState(crash_target=1, crash_open=0, selloff_target=1, selloff_open=1)
        candidates = [_make_crash_candidate(ev_per_dollar=1.46, convexity_factor=20.1)]

        log = self._run_generate_with_rejection_log(policy, inv, candidates)
        assert len(log) == 1
        assert log[0]["decision"] == "APPROVE"
        assert log[0]["passes_ev"] is True
        assert log[0]["passes_convexity"] is True


# ---------------------------------------------------------------------------
# Test 6: Policy loading validates new nested structure
# ---------------------------------------------------------------------------

class TestPolicyLoadValidation:
    """load_policy() must handle both new nested and legacy flat threshold structures."""

    def test_load_policy_accepts_new_nested_yaml(self, tmp_path):
        """Policy with nested fill/add structure loads without error."""
        import yaml
        policy_data = {
            "policy_id": "test_v16",
            "budgets": {
                "monthly_baseline": 1000.0, "monthly_max": 2000.0,
                "weekly_baseline": 250.0, "daily_baseline": 50.0,
                "weekly_kicker": 500.0, "daily_kicker": 100.0,
            },
            "inventory_targets": {"crash": 1, "selloff": 1},
            "thresholds": {
                "crash": {
                    "fill_when_empty": {
                        "ev_per_dollar_implied": 1.45,
                        "ev_per_dollar_external": 0.45,
                        "convexity_multiple": 20.0,
                    },
                    "add_when_full": {
                        "ev_per_dollar_implied": 1.60,
                        "ev_per_dollar_external": 0.50,
                        "convexity_multiple": 25.0,
                    },
                },
                "selloff": {
                    "fill_when_empty": {
                        "ev_per_dollar_implied": 1.20,
                        "ev_per_dollar_external": 0.30,
                        "convexity_multiple": 12.0,
                    },
                    "add_when_full": {
                        "ev_per_dollar_implied": 1.30,
                        "ev_per_dollar_external": 0.30,
                        "convexity_multiple": 15.0,
                    },
                },
            },
            "harvest": {
                "partial_close_multiple": 2.0, "full_close_multiple": 3.0,
                "partial_close_fraction": 0.5, "time_stop_dte": 14,
                "time_stop_min_multiple": 1.2,
            },
            "sizing": {"max_qty_per_trade": 10},
            "kicker": {"min_conditioning_confidence": 0.66, "max_vix_percentile": 35.0},
        }
        yaml_path = tmp_path / "policy.yaml"
        yaml_path.write_text(yaml.dump(policy_data))
        policy = load_policy(str(yaml_path))
        # Policy loaded and thresholds normalized
        assert "fill_when_empty" in policy["thresholds"]["crash"]
        assert "add_when_full" in policy["thresholds"]["crash"]

    def test_load_policy_accepts_legacy_flat_yaml(self, tmp_path):
        """Policy with legacy flat keys loads without error and is normalized."""
        import yaml
        policy_data = {
            "policy_id": "test_legacy",
            "budgets": {
                "monthly_baseline": 1000.0, "monthly_max": 2000.0,
                "weekly_baseline": 250.0, "daily_baseline": 50.0,
                "weekly_kicker": 500.0, "daily_kicker": 100.0,
            },
            "inventory_targets": {"crash": 1, "selloff": 1},
            "thresholds": {
                "crash": {
                    "ev_per_dollar_implied": 1.60,
                    "ev_per_dollar_external": 0.50,
                    "convexity_multiple": 25.0,
                },
                "selloff": {
                    "ev_per_dollar_implied": 1.30,
                    "ev_per_dollar_external": 0.30,
                    "convexity_multiple": 15.0,
                },
            },
            "harvest": {
                "partial_close_multiple": 2.0, "full_close_multiple": 3.0,
                "partial_close_fraction": 0.5, "time_stop_dte": 14,
                "time_stop_min_multiple": 1.2,
            },
            "sizing": {"max_qty_per_trade": 10},
            "kicker": {"min_conditioning_confidence": 0.66, "max_vix_percentile": 35.0},
        }
        yaml_path = tmp_path / "policy_legacy.yaml"
        yaml_path.write_text(yaml.dump(policy_data))
        policy = load_policy(str(yaml_path))
        # After load, thresholds should be normalized
        crash_t = policy["thresholds"]["crash"]
        assert "add_when_full" in crash_t
        assert crash_t["add_when_full"]["ev_per_dollar_implied"] == pytest.approx(1.60)

    def test_load_policy_rejects_nested_missing_add_when_full(self, tmp_path):
        """Nested structure without add_when_full raises PolicyError."""
        import yaml
        policy_data = {
            "policy_id": "bad",
            "budgets": {
                "monthly_baseline": 1000.0, "monthly_max": 2000.0,
                "weekly_baseline": 250.0, "daily_baseline": 50.0,
                "weekly_kicker": 500.0, "daily_kicker": 100.0,
            },
            "inventory_targets": {"crash": 1, "selloff": 1},
            "thresholds": {
                "crash": {
                    # fill_when_empty present but add_when_full MISSING — should fail
                    "fill_when_empty": {
                        "ev_per_dollar_implied": 1.45,
                        "ev_per_dollar_external": 0.45,
                        "convexity_multiple": 20.0,
                    },
                },
                "selloff": {
                    "fill_when_empty": {
                        "ev_per_dollar_implied": 1.20,
                        "ev_per_dollar_external": 0.30,
                        "convexity_multiple": 12.0,
                    },
                    "add_when_full": {
                        "ev_per_dollar_implied": 1.30,
                        "ev_per_dollar_external": 0.30,
                        "convexity_multiple": 15.0,
                    },
                },
            },
            "harvest": {
                "partial_close_multiple": 2.0, "full_close_multiple": 3.0,
                "partial_close_fraction": 0.5, "time_stop_dte": 14,
                "time_stop_min_multiple": 1.2,
            },
            "sizing": {"max_qty_per_trade": 10},
            "kicker": {"min_conditioning_confidence": 0.66, "max_vix_percentile": 35.0},
        }
        yaml_path = tmp_path / "bad_policy.yaml"
        yaml_path.write_text(yaml.dump(policy_data))
        with pytest.raises(PolicyError, match="add_when_full"):
            load_policy(str(yaml_path))


# ---------------------------------------------------------------------------
# Test 7: The real YAML config (allocator_ccc_v1.yaml) loads correctly
# ---------------------------------------------------------------------------

class TestRealConfigLoads:
    """Smoke test that the actual production YAML loads and has correct values."""

    def test_production_yaml_loads_without_error(self):
        """configs/allocator_ccc_v1.yaml loads successfully with new nested structure."""
        policy = load_policy("configs/allocator_ccc_v1.yaml")
        assert policy["policy_id"] == "ccc_v1"
        crash_t = policy["thresholds"]["crash"]
        assert "fill_when_empty" in crash_t
        assert "add_when_full" in crash_t

    def test_production_yaml_fill_thresholds_are_relaxed(self):
        """fill_when_empty thresholds are strictly less than add_when_full."""
        policy = load_policy("configs/allocator_ccc_v1.yaml")

        for regime in ("crash", "selloff"):
            fill = policy["thresholds"][regime]["fill_when_empty"]
            add = policy["thresholds"][regime]["add_when_full"]
            assert fill["ev_per_dollar_implied"] <= add["ev_per_dollar_implied"], \
                f"{regime}: fill EV should be <= add EV"
            assert fill["convexity_multiple"] <= add["convexity_multiple"], \
                f"{regime}: fill convexity should be <= add convexity"

    def test_production_yaml_crash_fill_values(self):
        """Verify crash fill_when_empty matches the spec: 1.45/0.45/20.0."""
        policy = load_policy("configs/allocator_ccc_v1.yaml")
        fill = policy["thresholds"]["crash"]["fill_when_empty"]
        assert fill["ev_per_dollar_implied"] == pytest.approx(1.45)
        assert fill["ev_per_dollar_external"] == pytest.approx(0.45)
        assert fill["convexity_multiple"] == pytest.approx(20.0)

    def test_production_yaml_crash_add_values(self):
        """Verify crash add_when_full matches the spec: 1.60/0.50/25.0."""
        policy = load_policy("configs/allocator_ccc_v1.yaml")
        add = policy["thresholds"]["crash"]["add_when_full"]
        assert add["ev_per_dollar_implied"] == pytest.approx(1.60)
        assert add["ev_per_dollar_external"] == pytest.approx(0.50)
        assert add["convexity_multiple"] == pytest.approx(25.0)

    def test_production_yaml_selloff_fill_values(self):
        """Verify selloff fill_when_empty matches the spec: 1.20/0.30/12.0."""
        policy = load_policy("configs/allocator_ccc_v1.yaml")
        fill = policy["thresholds"]["selloff"]["fill_when_empty"]
        assert fill["ev_per_dollar_implied"] == pytest.approx(1.20)
        assert fill["ev_per_dollar_external"] == pytest.approx(0.30)
        assert fill["convexity_multiple"] == pytest.approx(12.0)

    def test_production_yaml_selloff_add_values(self):
        """Verify selloff add_when_full matches the spec: 1.30/0.30/15.0."""
        policy = load_policy("configs/allocator_ccc_v1.yaml")
        add = policy["thresholds"]["selloff"]["add_when_full"]
        assert add["ev_per_dollar_implied"] == pytest.approx(1.30)
        assert add["ev_per_dollar_external"] == pytest.approx(0.30)
        assert add["convexity_multiple"] == pytest.approx(15.0)
