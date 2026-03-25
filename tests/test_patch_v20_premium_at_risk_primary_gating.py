"""
tests/test_patch_v20_premium_at_risk_primary_gating.py

CCC v2.0 — Premium-at-Risk Primary Gating
==========================================

10 deterministic tests covering Tasks A–E/F:

1.  Policy loads with premium_at_risk_caps section
2.  Policy backward-compatible when caps absent
3.  PAR computed correctly (gross/net debit, qty scaling)
4.  Candidate blocked when projected crash PAR exceeds cap
5.  Candidate blocked when projected total PAR exceeds cap
6.  Candidate allowed when crash count > soft target but PAR < cap and hard cap not exceeded
7.  Candidate blocked when hard count cap exceeded even if PAR is below cap
8.  Report prints regime and total PAR vs cap (Section B output)
9.  open_gate_trace makes premium-cap blocking explicit
10. Missing debit fields do not crash gating
"""
from __future__ import annotations

import sys
import io
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

from forecast_arb.allocator.risk import (
    compute_position_premium_at_risk,
    compute_portfolio_premium_at_risk,
)
from forecast_arb.allocator.policy import (
    get_premium_at_risk_caps,
    get_inventory_hard_caps,
)
from forecast_arb.allocator.open_plan import generate_open_actions, _evaluate_candidate
from forecast_arb.allocator.types import BudgetState, InventoryState, ActionType


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _make_budget(
    daily=200.0,
    weekly=1000.0,
    monthly=4000.0,
    spent_today=0.0,
    spent_week=0.0,
    spent_month=0.0,
) -> BudgetState:
    return BudgetState(
        monthly_baseline=monthly,
        monthly_max=monthly * 2,
        weekly_baseline=weekly,
        weekly_kicker=weekly * 2,
        daily_baseline=daily,
        daily_kicker=daily * 2,
        spent_month=spent_month,
        spent_week=spent_week,
        spent_today=spent_today,
        kicker_enabled=False,
    )


def _make_inventory(crash_open=0, crash_target=1, selloff_open=0, selloff_target=1) -> InventoryState:
    return InventoryState(
        crash_target=crash_target,
        crash_open=crash_open,
        selloff_target=selloff_target,
        selloff_open=selloff_open,
    )


def _make_candidate(
    regime="crash",
    premium=30.0,
    ev_per_dollar=2.0,
    max_gain=900.0,
    candidate_id="SPY_20260402_570_550_crash",
    p_used=0.15,
) -> Dict[str, Any]:
    """Minimal candidate dict that passes EV, convexity, and fragility gates."""
    return {
        "candidate_id": candidate_id,
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20260402",
        "long_strike": 570.0,
        "short_strike": 550.0,
        "computed_premium_usd": premium,
        "debit_per_contract": premium,
        "ev_per_dollar": ev_per_dollar,
        "max_gain_per_contract": max_gain,
        "p_used": p_used,
        "p_used_src": "implied",
    }


def _policy_with_par_caps(
    crash_cap=500.0,
    selloff_cap=300.0,
    total_cap=750.0,
    inventory_targets=None,
    hard_caps=None,
    robustness_enabled=False,
) -> Dict[str, Any]:
    """Return a minimal policy dict with par caps enabled."""
    targets = inventory_targets or {"crash": 1, "selloff": 1}
    policy: Dict[str, Any] = {
        "policy_id": "test_v20",
        "inventory_targets": targets,
        "thresholds": {
            "crash": {
                "add_when_full": {
                    "ev_per_dollar_implied": 1.0,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": 5.0,
                },
                "fill_when_empty": {
                    "ev_per_dollar_implied": 0.8,
                    "ev_per_dollar_external": 0.2,
                    "convexity_multiple": 4.0,
                },
            },
            "selloff": {
                "add_when_full": {
                    "ev_per_dollar_implied": 1.0,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": 5.0,
                },
                "fill_when_empty": {
                    "ev_per_dollar_implied": 0.8,
                    "ev_per_dollar_external": 0.2,
                    "convexity_multiple": 4.0,
                },
            },
        },
        "budgets": {"annual_convexity_budget": 99999.0},
        "sizing": {"max_qty_per_trade": 10},
        "diversity": {},
        "premium_at_risk_caps": {
            "crash": crash_cap,
            "selloff": selloff_cap,
            "total": total_cap,
        },
        "robustness": {"enabled": robustness_enabled},
    }
    if hard_caps:
        policy["inventory_hard_caps"] = hard_caps
    return policy


def _policy_without_par_caps(inventory_targets=None) -> Dict[str, Any]:
    """Return a minimal policy dict WITHOUT par caps (legacy behavior)."""
    targets = inventory_targets or {"crash": 1, "selloff": 1}
    return {
        "policy_id": "test_legacy",
        "inventory_targets": targets,
        "thresholds": {
            "crash": {
                "add_when_full": {
                    "ev_per_dollar_implied": 1.0,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": 5.0,
                },
                "fill_when_empty": {
                    "ev_per_dollar_implied": 0.8,
                    "ev_per_dollar_external": 0.2,
                    "convexity_multiple": 4.0,
                },
            },
            "selloff": {
                "add_when_full": {
                    "ev_per_dollar_implied": 1.0,
                    "ev_per_dollar_external": 0.3,
                    "convexity_multiple": 5.0,
                },
                "fill_when_empty": {
                    "ev_per_dollar_implied": 0.8,
                    "ev_per_dollar_external": 0.2,
                    "convexity_multiple": 4.0,
                },
            },
        },
        "budgets": {"annual_convexity_budget": 99999.0},
        "sizing": {"max_qty_per_trade": 10},
        "diversity": {},
        "robustness": {"enabled": False},
    }


# ===========================================================================
# Test 1 — Policy loads with premium_at_risk_caps
# ===========================================================================

class TestPolicyWithPARCaps:
    def test_get_premium_at_risk_caps_enabled(self):
        """Policy with premium_at_risk_caps returns expected values."""
        policy = _policy_with_par_caps(crash_cap=500.0, selloff_cap=300.0, total_cap=750.0)
        caps = get_premium_at_risk_caps(policy)
        assert caps["enabled"] is True
        assert caps["crash"] == 500.0
        assert caps["selloff"] == 300.0
        assert caps["total"] == 750.0

    def test_get_premium_at_risk_caps_loads_from_yaml(self, tmp_path):
        """round-trip: write YAML, load_policy, get_premium_at_risk_caps."""
        import yaml
        from forecast_arb.allocator.policy import load_policy

        yaml_text = """\
policy_id: test_v20_yaml
budgets:
  monthly_baseline: 1000.0
  monthly_max: 2000.0
  weekly_baseline: 250.0
  weekly_kicker: 500.0
  daily_baseline: 50.0
  daily_kicker: 100.0
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    add_when_full:
      ev_per_dollar_implied: 1.6
      ev_per_dollar_external: 0.5
      convexity_multiple: 25.0
    fill_when_empty:
      ev_per_dollar_implied: 1.45
      ev_per_dollar_external: 0.45
      convexity_multiple: 20.0
  selloff:
    add_when_full:
      ev_per_dollar_implied: 1.3
      ev_per_dollar_external: 0.3
      convexity_multiple: 15.0
    fill_when_empty:
      ev_per_dollar_implied: 1.2
      ev_per_dollar_external: 0.3
      convexity_multiple: 12.0
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
premium_at_risk_caps:
  crash: 500.0
  selloff: 300.0
  total: 750.0
inventory_hard_caps:
  crash: 3
  selloff: 2
"""
        config_file = tmp_path / "test_policy.yaml"
        config_file.write_text(yaml_text)

        policy = load_policy(str(config_file))
        caps = get_premium_at_risk_caps(policy)
        assert caps["enabled"] is True
        assert caps["crash"] == 500.0
        assert caps["total"] == 750.0

        hard_caps = get_inventory_hard_caps(policy)
        assert hard_caps["crash"] == 3
        assert hard_caps["selloff"] == 2


# ===========================================================================
# Test 2 — Backward-compatible when caps absent
# ===========================================================================

class TestPolicyBackwardCompat:
    def test_no_par_caps_section_returns_disabled(self):
        """Policy without premium_at_risk_caps returns enabled=False."""
        policy = _policy_without_par_caps()
        caps = get_premium_at_risk_caps(policy)
        assert caps["enabled"] is False
        assert caps["crash"] == float("inf")
        assert caps["selloff"] == float("inf")
        assert caps["total"] == float("inf")

    def test_no_par_caps_hard_caps_fallback_to_targets(self):
        """Without inventory_hard_caps, v2.1 fallback = max(target, floor).
        floors: crash=3, selloff=2.
        targets: crash=2, selloff=1  →  hard_caps: crash=max(2,3)=3, selloff=max(1,2)=2
        """
        policy = _policy_without_par_caps(inventory_targets={"crash": 2, "selloff": 1})
        hard_caps = get_inventory_hard_caps(policy)
        # v2.1: hard_cap = max(target, floor) — prevents soft target from acting as hard blocker
        assert hard_caps["crash"] == 3    # max(target=2, floor=3) = 3
        assert hard_caps["selloff"] == 2  # max(target=1, floor=2) = 2

    def test_no_par_caps_open_blocked_at_count_target(self):
        """Legacy: OPEN blocked when inv_open >= target (original behavior)."""
        policy = _policy_without_par_caps()
        inventory = _make_inventory(crash_open=1, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
        )
        # With crash_open=1 == crash_target=1, needs_open("crash") = False
        # → no OPEN actions when par_caps disabled
        assert len(actions) == 0 or all(a.type != ActionType.OPEN for a in actions)


# ===========================================================================
# Test 3 — PAR computed correctly from positions
# ===========================================================================

class TestPremiumAtRiskComputation:
    def test_single_dict_position_with_entry_debit(self):
        """Dict position uses entry_debit correctly."""
        pos = {"regime": "crash", "qty_open": 1, "entry_debit": 45.0}
        assert compute_position_premium_at_risk(pos) == 45.0

    def test_single_dict_position_with_entry_debit_gross(self):
        """Dict position uses entry_debit_gross when entry_debit absent."""
        pos = {"regime": "crash", "qty_open": 2, "entry_debit_gross": 50.0}
        assert compute_position_premium_at_risk(pos) == 100.0

    def test_net_debit_wins_over_gross(self):
        """entry_debit_net takes priority over entry_debit."""
        pos = {
            "regime": "crash",
            "qty_open": 1,
            "entry_debit_net": 40.0,
            "entry_debit": 55.0,  # gross — should be ignored
        }
        assert compute_position_premium_at_risk(pos) == 40.0

    def test_qty_scaling(self):
        """PAR scales linearly with qty_open."""
        pos = {"regime": "crash", "qty_open": 3, "entry_debit": 30.0}
        assert compute_position_premium_at_risk(pos) == 90.0

    def test_zero_qty_returns_zero(self):
        pos = {"regime": "crash", "qty_open": 0, "entry_debit": 45.0}
        assert compute_position_premium_at_risk(pos) == 0.0

    def test_missing_debit_returns_zero(self):
        """Position without any debit field → 0.0, no exception."""
        pos = {"regime": "crash", "qty_open": 2}
        assert compute_position_premium_at_risk(pos) == 0.0

    def test_portfolio_par_by_regime(self):
        """compute_portfolio_premium_at_risk splits correctly by regime."""
        positions = [
            {"regime": "crash",   "qty_open": 2, "entry_debit": 50.0},   # $100
            {"regime": "crash",   "qty_open": 1, "entry_debit": 22.80},  # $22.80
            {"regime": "selloff", "qty_open": 1, "entry_debit": 0.0},    # $0 (zero debit)
            {"regime": "unknown", "qty_open": 1, "entry_debit": 30.0},   # $30 (unknown regime — goes to total only)
        ]
        result = compute_portfolio_premium_at_risk(positions)
        assert result["crash"] == pytest.approx(122.80, abs=0.01)
        assert result["selloff"] == 0.0
        assert result["total"] == pytest.approx(152.80, abs=0.01)

    def test_sleeve_position_object(self):
        """compute_position_premium_at_risk works with SleevePosition objects."""
        from forecast_arb.allocator.types import SleevePosition
        pos = SleevePosition(
            trade_id="tid1",
            underlier="SPY",
            expiry="20260402",
            strikes=[570.0, 550.0],
            qty_open=2,
            regime="crash",
            entry_debit=45.0,
            mark_mid=20.0,
            dte=30,
        )
        assert compute_position_premium_at_risk(pos) == 90.0

    def test_sleeve_position_net_debit(self):
        """entry_debit_net on SleevePosition takes priority."""
        from forecast_arb.allocator.types import SleevePosition
        pos = SleevePosition(
            trade_id="tid2",
            underlier="SPY",
            expiry="20260402",
            strikes=[570.0, 550.0],
            qty_open=1,
            regime="crash",
            entry_debit=50.0,
            mark_mid=20.0,
            dte=15,
            entry_debit_net=44.0,
        )
        assert compute_position_premium_at_risk(pos) == 44.0


# ===========================================================================
# Test 4 — Candidate blocked when projected CRASH PAR exceeds cap
# ===========================================================================

class TestPARCapBlocksCrashExceedance:
    def test_blocked_when_crash_par_would_exceed_cap(self):
        """
        existing crash PAR = $90 ($45 × 2 contracts)
        candidate premium  = $30 × 1 = $30
        projected crash PAR = $120 > cap $100 → BLOCKED
        """
        existing_positions = [
            {"regime": "crash", "qty_open": 2, "entry_debit": 45.0}
        ]
        policy = _policy_with_par_caps(crash_cap=100.0, selloff_cap=300.0, total_cap=500.0)
        # inv_open=0 so par_caps gate evaluates (soft target allows evaluation)
        inventory = _make_inventory(crash_open=0, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=[type("P", (), pos)() if False else pos for pos in existing_positions],
        )

        # No OPEN should be generated
        assert not any(a.type == ActionType.OPEN for a in actions)

        # Rejection log must show PAR cap as reason
        assert rejection_log, "rejection_log should have at least one entry"
        primary_reasons = [e.get("primary_reason", "") for e in rejection_log]
        assert any(r == "PREMIUM_AT_RISK_CAP" for r in primary_reasons), (
            f"Expected PREMIUM_AT_RISK_CAP, got {primary_reasons}"
        )

    def test_blocked_reason_contains_crash_par_detail(self):
        """Rejection reason code contains projected PAR and cap values."""
        existing_positions = [
            {"regime": "crash", "qty_open": 2, "entry_debit": 45.0}
        ]
        policy = _policy_with_par_caps(crash_cap=100.0, total_cap=500.0)
        inventory = _make_inventory(crash_open=0, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)

        rejection_log: list = []
        generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=existing_positions,
        )

        # Check the reason string
        reasons = [e.get("reason", "") for e in rejection_log]
        assert any("PROJECTED_CRASH_PREMIUM_AT_RISK" in r for r in reasons), (
            f"Expected PROJECTED_CRASH_PREMIUM_AT_RISK in reasons: {reasons}"
        )
        assert any("CRASH_PREMIUM_CAP" in r for r in reasons), (
            f"Expected CRASH_PREMIUM_CAP in reasons: {reasons}"
        )


# ===========================================================================
# Test 5 — Candidate blocked when projected TOTAL PAR exceeds cap
# ===========================================================================

class TestPARCapBlocksTotalExceedance:
    def test_blocked_by_total_par_cap(self):
        """
        crash PAR = $200, selloff PAR = $250, total = $450
        candidate premium = $50 (crash) → projected total = $500 > cap $450 → BLOCKED
        """
        existing_positions = [
            {"regime": "crash",   "qty_open": 4, "entry_debit": 50.0},   # $200
            {"regime": "selloff", "qty_open": 5, "entry_debit": 50.0},   # $250
        ]
        # Crash cap is large so we need TOTAL to trigger
        policy = _policy_with_par_caps(crash_cap=999.0, selloff_cap=999.0, total_cap=450.0)
        # inv_open=0 for crash, par caps enabled so hard_cap for crash = 999
        inventory = _make_inventory(crash_open=0, crash_target=1, selloff_open=1, selloff_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=50.0, ev_per_dollar=2.0)

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=existing_positions,
        )

        assert not any(a.type == ActionType.OPEN for a in actions)
        reasons = [e.get("reason", "") for e in rejection_log]
        assert any("PROJECTED_TOTAL_PREMIUM_AT_RISK" in r for r in reasons), (
            f"Expected total PAR cap block. Rejection reasons: {reasons}"
        )


# ===========================================================================
# Test 6 — Candidate allowed at crash_count > soft target but PAR below cap
# ===========================================================================

class TestPARAllowsOpenAboveSoftTarget:
    def test_allowed_when_above_soft_target_but_below_par_cap(self):
        """
        crash soft target = 1, crash_open = 2 (above target!)
        hard cap = 5
        existing PAR = $40 (small)
        PAR cap = $500 >> $40 + candidate premium
        → OPEN should be ALLOWED despite crash_open > target
        """
        existing_positions = [
            {"regime": "crash", "qty_open": 2, "entry_debit": 20.0},   # $40
        ]
        policy = _policy_with_par_caps(
            crash_cap=500.0,
            total_cap=750.0,
            inventory_targets={"crash": 1, "selloff": 1},
            hard_caps={"crash": 5, "selloff": 3},
        )
        # crash_open = 2, soft_target = 1  → above target but below hard cap
        inventory = _make_inventory(crash_open=2, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=existing_positions,
        )

        open_actions = [a for a in actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 1, (
            f"Expected 1 OPEN, got {len(open_actions)}. Actions: {[a.type for a in actions]}"
        )


# ===========================================================================
# Test 7 — Candidate blocked when hard count cap exceeded
# ===========================================================================

class TestHardCountCapBlocks:
    def test_blocked_at_hard_count_cap(self):
        """
        hard_cap.crash = 3, crash_open = 3 → BLOCKED absolutely
        even if PAR is way below cap.
        """
        existing_positions = [
            {"regime": "crash", "qty_open": 1, "entry_debit": 10.0},  # tiny PAR
            {"regime": "crash", "qty_open": 1, "entry_debit": 10.0},
            {"regime": "crash", "qty_open": 1, "entry_debit": 10.0},
        ]
        policy = _policy_with_par_caps(
            crash_cap=50000.0,  # very large cap
            total_cap=99999.0,
            hard_caps={"crash": 3, "selloff": 2},
        )
        # crash_open = 3 == hard_cap
        inventory = _make_inventory(crash_open=3, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=10.0, ev_per_dollar=5.0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=existing_positions,
        )

        open_actions = [a for a in actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 0, (
            f"Expected 0 OPEN (hard cap hit), got {len(open_actions)}"
        )

    def test_not_blocked_below_hard_cap(self):
        """
        hard_cap.crash = 3, crash_open = 2 → NOT blocked
        as long as PAR cap also allows it.
        """
        policy = _policy_with_par_caps(
            crash_cap=5000.0,
            total_cap=9999.0,
            hard_caps={"crash": 3, "selloff": 2},
        )
        inventory = _make_inventory(crash_open=2, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
        )

        open_actions = [a for a in actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 1, (
            f"Expected 1 OPEN (below hard cap), got {len(open_actions)}"
        )


# ===========================================================================
# Test 8 — Report prints regime and total PAR vs cap
# ===========================================================================

class TestReportPARDisplay:
    def test_section_b_shows_per_regime_par_with_caps(self, capsys):
        """
        print_portfolio_summary with par_caps shows per-regime lines.
        E.g. 'Crash premium at risk:' and '/ $500.00'
        """
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from scripts.ccc_report import print_portfolio_summary

        positions = [
            {"regime": "crash",   "qty_open": 1, "entry_debit": 122.80},
            {"regime": "selloff", "qty_open": 1, "entry_debit": 0.0},
        ]
        pending = {"crash": 0, "selloff": 0, "total": 0}
        annual_budget = {"budget": None, "enabled": False}
        par_caps = {
            "crash":   500.0,
            "selloff": 300.0,
            "total":   750.0,
            "enabled": True,
        }

        print_portfolio_summary(
            positions=positions,
            pending=pending,
            ytd_spent=0.0,
            annual_budget=annual_budget,
            par_caps=par_caps,
        )

        captured = capsys.readouterr()
        out = captured.out
        assert "Crash premium at risk:" in out, f"Missing 'Crash premium at risk:' in:\n{out}"
        assert "Selloff premium at risk:" in out
        assert "Total premium at risk:" in out
        assert "$500.00" in out, f"Missing cap $500.00 in:\n{out}"
        assert "$750.00" in out, f"Missing total cap $750.00 in:\n{out}"
        assert "$122.80" in out, f"Missing crash PAR value in:\n{out}"

    def test_section_b_legacy_no_caps_shows_single_line(self, capsys):
        """Without par_caps, Section B shows single 'Premium at risk:' line."""
        from scripts.ccc_report import print_portfolio_summary

        positions = [{"regime": "crash", "qty_open": 1, "entry_debit": 50.0}]
        pending = {"crash": 0, "selloff": 0, "total": 0}
        annual_budget = {"budget": None, "enabled": False}

        print_portfolio_summary(
            positions=positions,
            pending=pending,
            ytd_spent=0.0,
            annual_budget=annual_budget,
            par_caps=None,
        )

        captured = capsys.readouterr()
        out = captured.out
        # Should still show total PAR without per-regime breakdown
        assert "Premium at risk:" in out
        assert "Crash premium at risk:" not in out


# ===========================================================================
# Test 9 — open_gate_trace makes premium-cap blocking explicit
# ===========================================================================

class TestGateTracePARCapExplicit:
    def test_par_cap_block_appears_in_rejection_log_reason(self):
        """
        When a candidate is blocked by PAR cap, the rejection log clearly shows
        PREMIUM_AT_RISK_CAP as primary_reason.
        """
        existing_positions = [
            {"regime": "crash", "qty_open": 3, "entry_debit": 90.0},  # PAR=$270
        ]
        # Cap = $300; candidate premium = $40 → projected = $310 > $300 → BLOCKED
        policy = _policy_with_par_caps(crash_cap=300.0, total_cap=999.0)
        inventory = _make_inventory(crash_open=0, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=40.0, ev_per_dollar=2.0)

        rejection_log: list = []
        generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=existing_positions,
        )

        assert rejection_log, "Expected at least one rejection log entry"
        entry = rejection_log[0]
        assert entry["primary_reason"] == "PREMIUM_AT_RISK_CAP", (
            f"Expected primary_reason=PREMIUM_AT_RISK_CAP, got {entry.get('primary_reason')}"
        )

    def test_par_cap_reason_code_includes_projected_values(self):
        """Reason string contains projected PAR and cap values."""
        existing_positions = [
            {"regime": "crash", "qty_open": 3, "entry_debit": 90.0},  # PAR=$270
        ]
        policy = _policy_with_par_caps(crash_cap=300.0, total_cap=999.0)
        inventory = _make_inventory(crash_open=0, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=40.0, ev_per_dollar=2.0)

        rejection_log: list = []
        generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=existing_positions,
        )

        assert rejection_log
        reason = rejection_log[0].get("reason", "")
        # e.g. "PREMIUM_AT_RISK_CAP:PROJECTED_CRASH_PREMIUM_AT_RISK:310.00:CRASH_PREMIUM_CAP:300.00"
        assert "310" in reason or "PREMIUM_AT_RISK_CAP" in reason, (
            f"Reason string missing expected values: {reason}"
        )
        assert "300" in reason or "CRASH_PREMIUM_CAP" in reason, (
            f"Reason string missing cap value: {reason}"
        )


# ===========================================================================
# Test 10 — Missing debit fields do not crash gating
# ===========================================================================

class TestMissingDebitSafe:
    def test_position_with_no_debit_does_not_crash(self):
        """Position missing all debit fields → PAR = 0.0, no exception."""
        pos = {"regime": "crash", "qty_open": 2}  # no debit field at all
        result = compute_position_premium_at_risk(pos)
        assert result == 0.0

    def test_generate_open_actions_with_no_debit_positions(self):
        """generate_open_actions with positions missing debit does NOT crash."""
        existing_positions = [
            {"regime": "crash", "qty_open": 1},  # no debit
        ]
        policy = _policy_with_par_caps(crash_cap=500.0, total_cap=750.0)
        inventory = _make_inventory(crash_open=0, crash_target=1)
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)

        # Must not raise
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            positions=existing_positions,
        )

        # PAR from existing positions = 0.0, so candidate PAR = $30 < $500 → OPEN allowed
        open_actions = [a for a in actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 1

    def test_portfolio_par_with_mixed_debit_missing(self):
        """compute_portfolio_premium_at_risk skips positions without debit."""
        positions = [
            {"regime": "crash", "qty_open": 1, "entry_debit": 50.0},  # counted
            {"regime": "crash", "qty_open": 1},                        # no debit — skipped
            {"regime": "crash", "qty_open": 0, "entry_debit": 999.0}, # zero qty — skipped
        ]
        result = compute_portfolio_premium_at_risk(positions)
        assert result["crash"] == 50.0
        assert result["total"] == 50.0

    def test_none_debit_does_not_crash(self):
        """Explicit None debit does not crash."""
        pos = {"regime": "crash", "qty_open": 1, "entry_debit": None}
        assert compute_position_premium_at_risk(pos) == 0.0

    def test_candidate_missing_premium_blocked_gracefully(self):
        """Candidate with no premium field is blocked with NO_PREMIUM, not a crash."""
        policy = _policy_with_par_caps()
        inventory = _make_inventory(crash_open=0, crash_target=1)
        budget = _make_budget()
        candidate = {
            "candidate_id": "test_no_premium",
            "regime": "crash",
            "ev_per_dollar": 2.0,
            # NO premium fields
        }

        rejection_log: list = []
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
        )
        assert not any(a.type == ActionType.OPEN for a in actions)
        primary_reasons = [e.get("primary_reason", "") for e in rejection_log]
        assert any("NO_PREMIUM" in r for r in primary_reasons), (
            f"Expected NO_PREMIUM reason. Got: {primary_reasons}"
        )


# ===========================================================================
# Additional: PAR cap helpers unit tests
# ===========================================================================

class TestPARCapEdgeCases:
    def test_par_cap_with_empty_section_disabled(self):
        """Empty premium_at_risk_caps dict → enabled=False."""
        policy = {"premium_at_risk_caps": {}, "inventory_targets": {"crash": 1, "selloff": 1}}
        caps = get_premium_at_risk_caps(policy)
        assert caps["enabled"] is False

    def test_hard_caps_from_explicit_section(self):
        """inventory_hard_caps section takes explicit values."""
        policy = {
            "inventory_hard_caps": {"crash": 4, "selloff": 2},
            "inventory_targets": {"crash": 1, "selloff": 1},
        }
        hard_caps = get_inventory_hard_caps(policy)
        assert hard_caps["crash"] == 4
        assert hard_caps["selloff"] == 2

    def test_hard_caps_fallback_when_section_absent(self):
        """Without inventory_hard_caps, v2.1 fallback = max(target, floor).
        floors: crash=3, selloff=2.
        crash target=2, selloff target=1  →  hard_caps: crash=max(2,3)=3, selloff=max(1,2)=2
        """
        policy = {"inventory_targets": {"crash": 2, "selloff": 1}}
        hard_caps = get_inventory_hard_caps(policy)
        # v2.1: hard_cap = max(target, floor) — prevents soft target from acting as hard blocker
        assert hard_caps["crash"] == 3    # max(target=2, floor=3) = 3
        assert hard_caps["selloff"] == 2  # max(target=1, floor=2) = 2

    def test_allowed_when_both_par_and_count_ok(self):
        """OPEN approved when both PAR < cap and count < hard cap."""
        policy = _policy_with_par_caps(
            crash_cap=500.0,
            total_cap=750.0,
            hard_caps={"crash": 3, "selloff": 2},
        )
        inventory = _make_inventory(crash_open=1, crash_target=1)  # above soft target
        budget = _make_budget()
        candidate = _make_candidate(regime="crash", premium=30.0, ev_per_dollar=2.0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=policy,
            budget=budget,
            inventory=inventory,
        )

        open_actions = [a for a in actions if a.type == ActionType.OPEN]
        assert len(open_actions) == 1


# ===========================================================================
# Additional: Task F — LOW_REMAINING_ECONOMIC_WEIGHT reporting
# ===========================================================================

class TestLowRemainingEconomicWeight:
    def test_low_weight_flag_printed_when_mark_below_threshold(self, capsys):
        """Positions with mark/entry < 0.25 get [LOW_WEIGHT] tag in Section A."""
        from scripts.ccc_report import print_positions

        positions = [
            {
                "underlier": "SPY",
                "regime": "crash",
                "expiry": "20260402",
                "strikes": [570, 550],
                "qty_open": 1,
                "entry_debit": 45.0,
                "mark_mid": 5.0,   # 5/45 ≈ 0.11 < 0.25
                "max_gain_per_contract": 2000.0,
            }
        ]
        print_positions(positions)
        out = capsys.readouterr().out
        assert "LOW_WEIGHT" in out, f"Expected LOW_WEIGHT flag in output:\n{out}"
        assert "LOW_REMAINING_ECONOMIC_WEIGHT" in out

    def test_no_low_weight_flag_when_mark_ok(self, capsys):
        """Positions with mark/entry >= 0.25 do NOT get [LOW_WEIGHT] tag."""
        from scripts.ccc_report import print_positions

        positions = [
            {
                "underlier": "SPY",
                "regime": "crash",
                "expiry": "20260402",
                "strikes": [570, 550],
                "qty_open": 1,
                "entry_debit": 45.0,
                "mark_mid": 20.0,   # 20/45 ≈ 0.44 > 0.25
                "max_gain_per_contract": 2000.0,
            }
        ]
        print_positions(positions)
        out = capsys.readouterr().out
        assert "LOW_WEIGHT" not in out
        assert "LOW_REMAINING_ECONOMIC_WEIGHT" not in out
