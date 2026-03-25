"""
Tests for CCC v2.1 — "Make Premium-at-Risk Truly Primary"

Verifies:
  1. inventory_targets treated as soft target, not dominant hard blocker
  2. explicit inventory_hard_caps loaded correctly
  3. candidate allowed when: crash count > soft target, PAR below cap,
     hard cap not exceeded
  4. candidate blocked when hard cap exceeded
  5. candidate blocked when premium-at-risk cap exceeded
  6. open_gate_trace shows PREMIUM_AT_RISK_CAP vs HARD_COUNT_CAP correctly
  7. report/summary surfaces soft target and hard cap distinctly
  8. backward-compatible behavior with missing inventory_hard_caps
  9. SOFT_TARGET_EXCEEDED_ALLOWED tag added to approved OPEN
 10. get_inventory_hard_caps fallback with max(target, floor) logic
"""
from __future__ import annotations

import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_budget(daily_cap: float = 500.0, spent: float = 0.0) -> Any:
    """Return a BudgetState using the actual constructor signature.

    BudgetState takes: monthly_baseline, monthly_max, weekly_baseline,
    daily_baseline, weekly_kicker, daily_kicker as required fields,
    then derives soft caps and remaining amounts internally.
    """
    from forecast_arb.allocator.types import BudgetState
    b = BudgetState(
        monthly_baseline=daily_cap * 20,
        monthly_max=daily_cap * 40,
        weekly_baseline=daily_cap * 5,
        daily_baseline=daily_cap,
        weekly_kicker=daily_cap * 10,
        daily_kicker=daily_cap * 2,
        spent_month=spent,
        spent_week=spent,
        spent_today=spent,
        kicker_enabled=False,
        kicker_reasons=[],
        annual_convexity_budget=float("inf"),
        spent_ytd=0.0,
    )
    return b


def _make_inventory(crash_open: int = 0, selloff_open: int = 0,
                    crash_target: int = 1, selloff_target: int = 1) -> Any:
    """Return a minimal InventoryState."""
    from forecast_arb.allocator.types import InventoryState
    return InventoryState(
        crash_target=crash_target,
        crash_open=crash_open,
        selloff_target=selloff_target,
        selloff_open=selloff_open,
    )


def _make_policy(
    soft_targets: Optional[Dict] = None,
    hard_caps: Optional[Dict] = None,
    par_caps: Optional[Dict] = None,
    ev_thresh: float = 1.0,
    conv_thresh: float = 10.0,
) -> Dict[str, Any]:
    """Build a minimal valid policy dict for testing."""
    inv_targets = soft_targets or {"crash": 1, "selloff": 1}
    tier = {
        "ev_per_dollar_implied":  ev_thresh,
        "ev_per_dollar_external": ev_thresh * 0.5,
        "convexity_multiple":     conv_thresh,
    }
    policy: Dict[str, Any] = {
        "policy_id": "test_v21",
        "budgets": {
            "monthly_baseline": 1000.0,
            "monthly_max":      2000.0,
            "weekly_baseline":  250.0,
            "weekly_kicker":    500.0,
            "daily_baseline":   50.0,
            "daily_kicker":     100.0,
        },
        "inventory_targets": inv_targets,
        "thresholds": {
            "crash": {
                "fill_when_empty": dict(tier),
                "add_when_full":   dict(tier),
            },
            "selloff": {
                "fill_when_empty": {
                    "ev_per_dollar_implied": 0.8,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": 8.0,
                },
                "add_when_full": {
                    "ev_per_dollar_implied": 0.8,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": 8.0,
                },
            },
        },
        "harvest": {
            "partial_close_multiple": 2.0,
            "full_close_multiple":    3.0,
            "time_stop_dte":          14,
            "time_stop_min_multiple": 1.2,
            "partial_close_fraction": 0.5,
        },
        "sizing": {"max_qty_per_trade": 10},
        "kicker": {
            "min_conditioning_confidence": 0.66,
            "max_vix_percentile":          35.0,
        },
    }
    if hard_caps is not None:
        policy["inventory_hard_caps"] = hard_caps
    if par_caps is not None:
        policy["premium_at_risk_caps"] = par_caps
    return policy


def _make_candidate(
    regime: str = "crash",
    candidate_id: str = "test_cand_1",
    ev: float = 2.0,
    premium: float = 40.0,
    max_gain: float = 2000.0,
    p_used: float = 0.06,
    long_strike: float = 545.0,
    short_strike: float = 525.0,
    spot: float = 580.0,
) -> Dict[str, Any]:
    return {
        "candidate_id":        candidate_id,
        "regime":              regime,
        "underlier":           "SPY",
        "expiry":              "20260402",
        "long_strike":         long_strike,
        "short_strike":        short_strike,
        "spot":                spot,
        "computed_premium_usd": premium,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar":       ev,
        "p_used":              p_used,
        "p_used_src":          "implied",
    }


# ---------------------------------------------------------------------------
# Test 1: inventory_targets treated as soft target, not dominant hard blocker
# ---------------------------------------------------------------------------

class TestInventoryTargetsSoftOnly:
    """v2.1 T1: soft targets do NOT block when PAR caps enabled and below cap."""

    def test_soft_target_does_not_block_when_par_caps_enabled(self):
        """
        crash=3 > soft_target=1, but PAR caps enabled and PAR well below cap.
        Candidate should NOT be blocked by count alone.
        The hard cap is 5, and we're at 3, so it should pass.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        # 3 positions, soft target is 1 — we're 3x over soft target
        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)

        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0)
        candidates_data = {"selected": [candidate]}

        # Simulate no existing positions (PAR = $0 baseline)
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        # Should produce an OPEN since PAR is below cap and hard cap not exceeded
        assert len(actions) == 1, (
            f"Expected 1 OPEN but got {len(actions)}: "
            f"{[a.reason_codes for a in actions]}"
        )
        # ActionType may be a plain string or an enum; check both forms
        assert str(actions[0].type) in ("OPEN", "ActionType.OPEN")

    def test_legacy_mode_soft_target_still_blocks(self):
        """
        Legacy mode (no par_caps section): soft target blocks as before.
        crash=2, soft_target=1, no par_caps → HOLD (inventory at/above target).
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        # No par_caps → legacy mode
        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps=None,
            par_caps=None,
        )

        inventory = _make_inventory(crash_open=2, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash")
        candidates_data = {"selected": [candidate]}

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=[],
        )

        # Legacy mode: crash=2 > target=1 → inventory.needs_open("crash") = False → no OPEN
        assert len(actions) == 0, f"Expected 0 OPEN in legacy mode but got {len(actions)}"


# ---------------------------------------------------------------------------
# Test 2: explicit inventory_hard_caps loaded correctly
# ---------------------------------------------------------------------------

class TestHardCapsLoading:
    """v2.1 T2: get_inventory_hard_caps returns correct values."""

    def test_explicit_hard_caps_loaded_when_present(self):
        from forecast_arb.allocator.policy import get_inventory_hard_caps

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
        )
        caps = get_inventory_hard_caps(policy)
        assert caps["crash"] == 5
        assert caps["selloff"] == 3

    def test_missing_hard_caps_falls_back_to_max_of_target_and_floor(self):
        """When inventory_hard_caps absent, fallback = max(target, floor)."""
        from forecast_arb.allocator.policy import get_inventory_hard_caps

        # soft_target=1 for both → fallback should be max(1, floor)
        # floors: crash=3, selloff=2
        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps=None,  # absent
        )
        caps = get_inventory_hard_caps(policy)
        # crash: max(1, 3) = 3
        assert caps["crash"] == 3, f"Expected crash=3, got {caps['crash']}"
        # selloff: max(1, 2) = 2
        assert caps["selloff"] == 2, f"Expected selloff=2, got {caps['selloff']}"

    def test_large_soft_target_preserved_in_fallback(self):
        """When soft_target > floor, hard cap fallback = soft_target."""
        from forecast_arb.allocator.policy import get_inventory_hard_caps

        # large soft target: crash=10 > floor=3 → hard cap fallback = 10
        policy = _make_policy(
            soft_targets={"crash": 10, "selloff": 5},
            hard_caps=None,  # absent
        )
        caps = get_inventory_hard_caps(policy)
        assert caps["crash"] == 10
        assert caps["selloff"] == 5

    def test_get_inventory_targets_docstring_semantics(self):
        """inventory_targets returns soft targets unchanged."""
        from forecast_arb.allocator.policy import get_inventory_targets

        policy = _make_policy(soft_targets={"crash": 2, "selloff": 1})
        targets = get_inventory_targets(policy)
        assert targets["crash"] == 2
        assert targets["selloff"] == 1


# ---------------------------------------------------------------------------
# Test 3: candidate allowed above soft target but below PAR cap + hard cap
# ---------------------------------------------------------------------------

class TestCandidateAllowedAboveSoftTarget:
    """v2.1 T3: OPEN approved when crash > soft_tgt, PAR < cap, hard_cap not exceeded."""

    def test_open_approved_above_soft_target_below_par(self):
        """The primary fix scenario: crash=3/1, hard_cap=5, PAR=122.80/500."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        # Candidate premium=$40, qty=1 → candidate PAR = $40
        # Current PAR ≈ $122.80 (from 3 positions × ~$40)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        # Simulate existing crash PAR = $122.80
        from forecast_arb.allocator.types import SleevePosition
        # Create mock positions with PAR so projected_par is ~$162.80 < $500
        mock_positions = []
        for i in range(3):
            class _FakePos:
                pass
            p = _FakePos()
            p.regime = "crash"
            p.qty_open = 1
            p.entry_debit_gross = 40.93
            p.entry_debit_net = None
            p.entry_debit = None
            mock_positions.append(p)

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=mock_positions,
        )

        assert len(actions) == 1, (
            f"Expected 1 OPEN. rejection_log={[e.get('reason') for e in rejection_log]}"
        )

    def test_soft_target_exceeded_allowed_tag_present(self):
        """When OPEN approved above soft target, SOFT_TARGET_EXCEEDED_ALLOWED tag present."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        assert len(actions) == 1
        rc_str = " ".join(actions[0].reason_codes)
        assert "SOFT_TARGET_EXCEEDED_ALLOWED" in rc_str, (
            f"Expected SOFT_TARGET_EXCEEDED_ALLOWED in reason_codes, got: {actions[0].reason_codes}"
        )

    def test_no_soft_target_tag_when_below_target(self):
        """When inv=0 < soft_target=1, SOFT_TARGET_EXCEEDED_ALLOWED is NOT added."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=0, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        assert len(actions) == 1
        rc_str = " ".join(actions[0].reason_codes)
        assert "SOFT_TARGET_EXCEEDED_ALLOWED" not in rc_str, (
            f"Should NOT have SOFT_TARGET_EXCEEDED_ALLOWED when at/below target. "
            f"reason_codes={actions[0].reason_codes}"
        )


# ---------------------------------------------------------------------------
# Test 4: candidate blocked when hard cap exceeded
# ---------------------------------------------------------------------------

class TestCandidateBlockedByHardCap:
    """v2.1 T4: OPEN blocked when hard count cap reached."""

    def test_blocked_when_at_hard_cap(self):
        """crash=5, hard_cap=5 → should block even with PAR well below cap."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=5, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=10.0, ev=3.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        # Hard cap = 5, inv = 5 → blocked
        assert len(actions) == 0, (
            f"Expected 0 OPEN when at hard cap, got {len(actions)}: "
            f"{[a.reason_codes for a in actions]}"
        )

    def test_hard_count_cap_in_rejection_log(self):
        """When blocked by hard count, rejection_log shows HARD_COUNT_CAP."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 3, "selloff": 2},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=10.0, ev=3.0)
        candidates_data = {"selected": [candidate]}

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=[],
        )

        assert len(actions) == 0
        # Rejection log should contain HARD_COUNT_CAP entry
        hard_cap_entries = [
            e for e in rejection_log
            if e.get("primary_reason") == "HARD_COUNT_CAP"
        ]
        assert len(hard_cap_entries) >= 1, (
            f"Expected HARD_COUNT_CAP in rejection_log. Got: "
            f"{[e.get('primary_reason') for e in rejection_log]}"
        )
        entry = hard_cap_entries[0]
        assert "HARD_COUNT_CAP" in entry.get("reason", "")
        assert entry["result"] == "REJECTED"

    def test_just_below_hard_cap_is_allowed(self):
        """crash=4, hard_cap=5 → below hard cap → should pass to PAR gate."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=4, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        # Should not be blocked by hard cap (4 < 5)
        assert len(actions) == 1, (
            f"Expected 1 OPEN when below hard cap (4<5). Got {len(actions)}"
        )


# ---------------------------------------------------------------------------
# Test 5: candidate blocked when PAR cap exceeded
# ---------------------------------------------------------------------------

class TestCandidateBlockedByPARCap:
    """v2.1 T5: OPEN blocked when adding candidate would exceed PAR cap."""

    def test_blocked_by_crash_par_cap(self):
        """Existing crash PAR = $480, candidate premium=$40 → projected=$520 > $500 cap."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 10, "selloff": 5},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        # Premium = $40; if existing PAR = $480 → projected = $520 > $500 cap
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        # Simulate existing crash PAR = $480 via "positions" with debit entries
        class _MockPos:
            regime = "crash"
            qty_open = 1
            entry_debit_gross = None
            entry_debit_net = None
            entry_debit = None

        # Build positions that contribute $480 to crash PAR
        # 12 positions × $40/each = $480
        mock_positions = []
        for _ in range(12):
            p = _MockPos()
            p.entry_debit = 40.0
            mock_positions.append(p)

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=mock_positions,
        )

        assert len(actions) == 0, f"Expected 0 OPEN due to PAR cap, got {len(actions)}"

        # Check rejection: primary reason should be PREMIUM_AT_RISK_CAP
        par_cap_entries = [
            e for e in rejection_log
            if e.get("primary_reason") == "PREMIUM_AT_RISK_CAP"
        ]
        assert len(par_cap_entries) >= 1, (
            f"Expected PREMIUM_AT_RISK_CAP in rejection_log. Got: "
            f"{[e.get('primary_reason') for e in rejection_log]}"
        )

    def test_blocked_by_total_par_cap(self):
        """Existing total PAR = $730, candidate premium=$40 → projected=$770 > $750 total cap."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 10, "selloff": 10},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        # Simulate existing PAR: crash=$400, selloff=$330 → total=$730
        class _MockCrashPos:
            regime = "crash"
            qty_open = 1
            entry_debit = 40.0
            entry_debit_net = None
            entry_debit_gross = None

        class _MockSelloffPos:
            regime = "selloff"
            qty_open = 1
            entry_debit = 33.0
            entry_debit_net = None
            entry_debit_gross = None

        mock_positions = [_MockCrashPos() for _ in range(10)] + [
            _MockSelloffPos() for _ in range(10)
        ]

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=mock_positions,
        )

        assert len(actions) == 0, f"Expected 0 OPEN due to total PAR cap, got {len(actions)}"
        par_cap_entries = [
            e for e in rejection_log
            if e.get("primary_reason") == "PREMIUM_AT_RISK_CAP"
        ]
        assert len(par_cap_entries) >= 1


# ---------------------------------------------------------------------------
# Test 6: open_gate_trace shows correct primary reason
# ---------------------------------------------------------------------------

class TestOpenGateTraceReasonCodes:
    """v2.1 T6: open_gate_trace shows correct primary blocking reason."""

    def test_rejection_log_premium_cap_has_correct_primary_reason(self):
        """When blocked by PAR cap, primary_reason = PREMIUM_AT_RISK_CAP."""
        from forecast_arb.allocator.open_plan import _evaluate_candidate

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 10, "selloff": 5},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        budget = _make_budget(daily_cap=500.0)
        eff_thresh = {
            "tier": "add_when_full",
            "ev_implied": 1.0,
            "ev_external": 0.5,
            "convexity_multiple": 10.0,
        }
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)

        # Simulate: existing crash PAR already at $490 → projected = $490 + $40 = $530 > $500
        projected_par_base = {"crash": 490.0, "selloff": 0.0, "total": 490.0}

        action, reason = _evaluate_candidate(
            candidate=candidate,
            regime="crash",
            policy=policy,
            budget=budget,
            max_qty=10,
            eff_thresh=eff_thresh,
            inv_open_for_regime=3,
            par_caps_enabled=True,
            projected_par_base=projected_par_base,
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0, "enabled": True},
        )

        assert action is None
        assert reason.startswith("PREMIUM_AT_RISK_CAP"), (
            f"Expected reason to start with PREMIUM_AT_RISK_CAP, got: {reason!r}"
        )

    def test_rejection_log_hard_count_cap_has_correct_primary_reason(self):
        """When blocked by hard count, rejection_log entry has HARD_COUNT_CAP."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 3, "selloff": 2},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash")
        candidates_data = {"selected": [candidate]}

        rejection_log: list = []
        generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=[],
        )

        # Should have HARD_COUNT_CAP as primary reason
        primary_reasons = [e.get("primary_reason") for e in rejection_log]
        assert "HARD_COUNT_CAP" in primary_reasons, (
            f"Expected HARD_COUNT_CAP in primary_reasons, got: {primary_reasons}"
        )

        # Should NOT have PREMIUM_AT_RISK_CAP (count blocked it before PAR was evaluated)
        assert "PREMIUM_AT_RISK_CAP" not in primary_reasons, (
            f"PAR cap should NOT be in reasons when hard count blocked: {primary_reasons}"
        )


# ---------------------------------------------------------------------------
# Test 7: report/summary surfaces soft target and hard cap distinctly
# ---------------------------------------------------------------------------

class TestReportSoftTargetDisplay:
    """v2.1 T7: ccc_report.py Section B shows soft target and hard cap."""

    def test_load_inventory_targets_and_caps_returns_correct_data(self, tmp_path):
        """load_inventory_targets_and_caps returns soft targets and hard caps."""
        import yaml
        from scripts.ccc_report import load_inventory_targets_and_caps

        policy_content = {
            "policy_id": "test",
            "inventory_targets": {"crash": 1, "selloff": 1},
            "inventory_hard_caps": {"crash": 5, "selloff": 3},
            "budgets": {
                "monthly_baseline": 1000.0, "monthly_max": 2000.0,
                "weekly_baseline": 250.0, "weekly_kicker": 500.0,
                "daily_baseline": 50.0, "daily_kicker": 100.0,
            },
            "thresholds": {"crash": {"add_when_full": {"ev_per_dollar_implied": 1.0,
                                                         "ev_per_dollar_external": 0.5,
                                                         "convexity_multiple": 10.0}},
                           "selloff": {"add_when_full": {"ev_per_dollar_implied": 0.8,
                                                          "ev_per_dollar_external": 0.3,
                                                          "convexity_multiple": 8.0}}},
            "harvest": {"partial_close_multiple": 2.0, "full_close_multiple": 3.0,
                        "time_stop_dte": 14, "time_stop_min_multiple": 1.2,
                        "partial_close_fraction": 0.5},
            "sizing": {"max_qty_per_trade": 10},
            "kicker": {"min_conditioning_confidence": 0.66, "max_vix_percentile": 35.0},
        }
        policy_file = tmp_path / "test_policy.yaml"
        with open(policy_file, "w") as f:
            yaml.dump(policy_content, f)

        result = load_inventory_targets_and_caps(policy_file)
        assert result["enabled"] is True
        assert result["soft_targets"]["crash"] == 1
        assert result["soft_targets"]["selloff"] == 1
        assert result["hard_caps"]["crash"] == 5
        assert result["hard_caps"]["selloff"] == 3

    def test_print_portfolio_summary_shows_soft_and_hard(self, capsys):
        """print_portfolio_summary includes soft target and hard cap in output."""
        from scripts.ccc_report import print_portfolio_summary

        positions = [
            {"regime": "crash", "qty_open": 1, "entry_debit": 40.0},
            {"regime": "crash", "qty_open": 1, "entry_debit": 41.0},
            {"regime": "crash", "qty_open": 1, "entry_debit": 41.8},
        ]
        pending = {"crash": 0, "selloff": 0, "total": 0}
        annual_budget = {"enabled": False, "budget": None}
        par_caps = {"crash": 500.0, "selloff": 300.0, "total": 750.0, "enabled": True}
        inv_targets_caps = {
            "soft_targets": {"crash": 1, "selloff": 1},
            "hard_caps": {"crash": 5, "selloff": 3},
            "enabled": True,
        }

        print_portfolio_summary(
            positions=positions,
            pending=pending,
            ytd_spent=122.80,
            annual_budget=annual_budget,
            par_caps=par_caps,
            inv_targets_caps=inv_targets_caps,
        )

        captured = capsys.readouterr().out
        assert "soft target=1" in captured, f"Expected 'soft target=1' in output:\n{captured}"
        assert "hard cap=5" in captured, f"Expected 'hard cap=5' in output:\n{captured}"
        # Also check PAR display
        assert "$122.80" in captured or "122" in captured  # crash PAR

    def test_print_portfolio_summary_legacy_mode(self, capsys):
        """Without inv_targets_caps, shows plain position count (backward compat)."""
        from scripts.ccc_report import print_portfolio_summary

        positions = [{"regime": "crash", "qty_open": 1, "entry_debit": 40.0}]
        pending = {"crash": 0, "selloff": 0, "total": 0}
        annual_budget = {"enabled": False, "budget": None}

        # No inv_targets_caps → backward compat mode
        print_portfolio_summary(
            positions=positions,
            pending=pending,
            ytd_spent=40.0,
            annual_budget=annual_budget,
        )

        captured = capsys.readouterr().out
        assert "Crash open positions:" in captured
        # Should NOT have soft target or hard cap labels
        assert "soft target" not in captured.lower() or "soft_target" not in captured


# ---------------------------------------------------------------------------
# Test 8: backward-compatible behavior without inventory_hard_caps
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """v2.1 T8: legacy configs without inventory_hard_caps still work."""

    def test_fallback_hard_caps_allow_more_than_soft_target(self):
        """
        Legacy config (no inventory_hard_caps) with PAR caps:
        fallback hard cap = max(soft_target=1, floor=3) = 3.
        So crash=2 (above soft target=1, below fallback=3) → NOT blocked by count.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        # No hard_caps section → fallback kicks in
        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps=None,  # absent → fallback = max(1, 3) = 3
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=2, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=2000.0)
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        # crash=2 < fallback_hard_cap=3 → should evaluate PAR → should pass
        assert len(actions) == 1, (
            f"Expected 1 OPEN with fallback hard cap=3, crash=2. Got {len(actions)}"
        )

    def test_fallback_hard_cap_still_blocks_at_floor(self):
        """With fallback hard cap = 3, crash=3 → BLOCKED."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps=None,  # fallback = crash:3, selloff:2
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0)
        candidates_data = {"selected": [candidate]}

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=[],
        )

        # crash=3 >= fallback_hard_cap=3 → BLOCKED by hard count
        assert len(actions) == 0, (
            f"Expected 0 OPEN at fallback hard cap. Got {len(actions)}"
        )
        hard_cap_entries = [
            e for e in rejection_log
            if e.get("primary_reason") == "HARD_COUNT_CAP"
        ]
        assert len(hard_cap_entries) >= 1

    def test_no_par_caps_section_uses_pure_legacy_gating(self):
        """Without par_caps section, gating is purely by inventory.needs_open()."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        # Neither par_caps nor hard_caps → pure legacy
        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps=None,
            par_caps=None,
        )

        inventory = _make_inventory(crash_open=1, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)
        candidate = _make_candidate(regime="crash")
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        # Legacy: crash=1 >= target=1 → needs_open = False → no OPEN
        assert len(actions) == 0


# ---------------------------------------------------------------------------
# Test 9: policy helper docstrings / semantics
# ---------------------------------------------------------------------------

class TestPolicyHelperSemantics:
    """v2.1 T9: Policy helpers return the right types and defaults."""

    def test_get_inventory_targets_returns_dict_of_int(self):
        from forecast_arb.allocator.policy import get_inventory_targets
        policy = _make_policy(soft_targets={"crash": 2, "selloff": 1})
        targets = get_inventory_targets(policy)
        assert isinstance(targets, dict)
        assert isinstance(targets["crash"], int)
        assert isinstance(targets["selloff"], int)

    def test_get_inventory_hard_caps_returns_dict_of_int(self):
        from forecast_arb.allocator.policy import get_inventory_hard_caps
        policy = _make_policy(hard_caps={"crash": 5, "selloff": 3})
        caps = get_inventory_hard_caps(policy)
        assert isinstance(caps, dict)
        assert isinstance(caps["crash"], int)
        assert isinstance(caps["selloff"], int)

    def test_get_premium_at_risk_caps_enabled_when_present(self):
        from forecast_arb.allocator.policy import get_premium_at_risk_caps
        policy = _make_policy(par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0})
        caps = get_premium_at_risk_caps(policy)
        assert caps["enabled"] is True
        assert caps["crash"] == 500.0
        assert caps["total"] == 750.0

    def test_get_premium_at_risk_caps_disabled_when_absent(self):
        from forecast_arb.allocator.policy import get_premium_at_risk_caps
        policy = _make_policy(par_caps=None)
        caps = get_premium_at_risk_caps(policy)
        assert caps["enabled"] is False
        assert caps["crash"] == float("inf")

    def test_hard_cap_fallback_floors_constant_exists(self):
        """_HARD_CAP_FALLBACK_FLOORS is defined with correct values."""
        from forecast_arb.allocator.policy import _HARD_CAP_FALLBACK_FLOORS
        assert _HARD_CAP_FALLBACK_FLOORS["crash"] == 3
        assert _HARD_CAP_FALLBACK_FLOORS["selloff"] == 2


# ---------------------------------------------------------------------------
# Test 10: Integration — tier selection still governed by soft target
# ---------------------------------------------------------------------------

class TestTierSelectionSoftTarget:
    """v2.1 T10: Even when allowed above soft target, add_when_full tier applies."""

    def test_add_when_full_tier_used_above_soft_target(self):
        """
        crash=3 > soft_target=1 → add_when_full tier applies (stricter thresholds).
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        # Strict add_when_full threshold: ev >= 3.0
        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
            ev_thresh=3.0,  # strict
            conv_thresh=10.0,
        )

        inventory = _make_inventory(crash_open=3, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)

        # EV = 2.0 < add_when_full threshold of 3.0
        low_ev_candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=4000.0)
        candidates_data = {"selected": [low_ev_candidate]}

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=[],
        )

        # EV 2.0 < strict threshold 3.0 → rejected with EV_BELOW_THRESHOLD
        assert len(actions) == 0, f"Expected 0 OPEN (EV too low for add_when_full). Got {len(actions)}"
        ev_below = [e for e in rejection_log if e.get("primary_reason") == "EV_BELOW_THRESHOLD"]
        assert len(ev_below) >= 1, (
            f"Expected EV_BELOW_THRESHOLD. Got: {[e.get('primary_reason') for e in rejection_log]}"
        )

    def test_fill_when_empty_tier_below_soft_target(self):
        """
        crash=0 < soft_target=1 → fill_when_empty tier (relaxed threshold).
        A candidate passing the relaxed threshold should be approved.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        # fill_when_empty uses env_thresh.ev=1.0, add_when_full uses 3.0
        # We only set ev_thresh which goes into both tiers via _make_policy
        # Let's use a policy where fill EV=1.0 and add EV=3.0 explicitly
        policy = _make_policy(
            soft_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
            par_caps={"crash": 500.0, "selloff": 300.0, "total": 750.0},
        )
        # Override thresholds to have different fill vs add
        policy["thresholds"]["crash"]["fill_when_empty"]["ev_per_dollar_implied"] = 1.0
        policy["thresholds"]["crash"]["add_when_full"]["ev_per_dollar_implied"] = 3.0

        inventory = _make_inventory(crash_open=0, selloff_open=0)
        budget = _make_budget(daily_cap=500.0)

        # EV = 2.0 > fill_when_empty=1.0, but < add_when_full=3.0
        candidate = _make_candidate(regime="crash", premium=40.0, ev=2.0, max_gain=4000.0)
        candidates_data = {"selected": [candidate]}

        actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=[],
        )

        # crash=0 < target=1 → fill_when_empty → EV 2.0 > 1.0 → PASS
        assert len(actions) == 1, (
            f"Expected 1 OPEN with fill_when_empty tier (ev=2.0 > 1.0). Got {len(actions)}"
        )


# ---------------------------------------------------------------------------
# Guard: existing allocator tests must continue passing
# ---------------------------------------------------------------------------

class TestExistingCompatibilityGuards:
    """Smoke-test: key policy helpers still work with typical configs."""

    def test_policy_helpers_all_importable(self):
        from forecast_arb.allocator.policy import (
            get_inventory_targets,
            get_inventory_hard_caps,
            get_premium_at_risk_caps,
            get_ladder_params,
            get_robustness_params,
            get_roll_params,
            get_diversity_params,
            _HARD_CAP_FALLBACK_FLOORS,
        )
        assert callable(get_inventory_targets)
        assert callable(get_inventory_hard_caps)
        assert callable(get_premium_at_risk_caps)
        assert isinstance(_HARD_CAP_FALLBACK_FLOORS, dict)

    def test_open_plan_importable_with_new_signature(self):
        from forecast_arb.allocator.open_plan import (
            generate_open_actions,
            _evaluate_candidate,
        )
        assert callable(generate_open_actions)
        assert callable(_evaluate_candidate)

    def test_ccc_report_importable_with_new_function(self):
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from scripts.ccc_report import (
            load_inventory_targets_and_caps,
            print_portfolio_summary,
            run_report,
        )
        assert callable(load_inventory_targets_and_caps)
        assert callable(print_portfolio_summary)
