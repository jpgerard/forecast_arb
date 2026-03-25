"""
Phase 2A Tests — Task C: Strike Diversity Guard

Tests for _check_strike_diversity(), get_diversity_params(), and the
diversity gate wired into generate_open_actions().

Covers:
  - candidate too close to existing open position → STRIKE_TOO_CLOSE
  - candidate far enough away → OPEN allowed
  - candidate too close to in-run approved OPEN → STRIKE_TOO_CLOSE
  - different underlier → no block
  - different regime → no block
  - missing diversity config → gate disabled, behavior unchanged
  - missing spot in candidate → graceful skip (no crash, no block)

All tests are deterministic and use no network calls.
Math:
  distance_fraction = abs(candidate.long_strike - existing.long_strike) / spot
  Block if: distance_fraction < min_strike_distance_pct / 100

With spot=580, existing_long=540, threshold=3.0%:
  min_distance_pts = 580 * 0.03 = 17.4
  too_close:   candidate_long=545 → | 5|/580 = 0.86% < 3% → BLOCKED
  far_enough:  candidate_long=520 → |20|/580 = 3.45% > 3% → ALLOWED
  at_boundary: candidate_long=522.6 → 17.4/580 = 3.0% = exactly threshold → ALLOWED
               (rule is strictly <, not <=)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_budget(**kwargs):
    from forecast_arb.allocator.types import BudgetState
    defaults = {
        "monthly_baseline": 5000.0, "monthly_max": 10000.0,
        "weekly_baseline": 1250.0,  "daily_baseline": 500.0,
        "weekly_kicker": 2500.0,    "daily_kicker": 1000.0,
    }
    defaults.update(kwargs)
    return BudgetState(**defaults)


def _make_inventory(crash_open=0, crash_target=1, selloff_open=0, selloff_target=1):
    from forecast_arb.allocator.types import InventoryState
    return InventoryState(
        crash_target=crash_target, crash_open=crash_open,
        selloff_target=selloff_target, selloff_open=selloff_open,
    )


def _make_sleeve_position(
    trade_id: str,
    underlier: str,
    regime: str,
    long_strike: float,
    short_strike: float = 0.0,
):
    """Build a minimal SleevePosition for diversity testing."""
    from forecast_arb.allocator.types import SleevePosition
    return SleevePosition(
        trade_id=trade_id,
        underlier=underlier,
        expiry="20261120",
        strikes=[long_strike, short_strike if short_strike > 0 else long_strike - 20],
        qty_open=1,
        regime=regime,
        entry_debit=65.0,
        mark_mid=65.0,
        dte=90,
    )


def _make_open_action(
    candidate_id: str,
    underlier: str,
    regime: str,
    long_strike: float,
):
    """Build a minimal OPEN AllocatorAction for in-run diversity testing."""
    from forecast_arb.allocator.types import ActionType, AllocatorAction
    return AllocatorAction(
        type=ActionType.OPEN,
        candidate_id=candidate_id,
        underlier=underlier,
        regime=regime,
        long_strike=long_strike,
        short_strike=long_strike - 20,
        expiry="20261120",
        qty=1,
        premium=65.0,
        reason_codes=["DUMMY"],
    )


def _make_policy(diversity_pct: Optional[float] = None, ev_threshold=1.0, conv_multiple=10.0):
    """Minimal policy dict for diversity testing."""
    thresholds: Dict[str, Any] = {
        "fill_when_empty": {
            "ev_per_dollar_implied": ev_threshold,
            "ev_per_dollar_external": 0.3,
            "convexity_multiple": conv_multiple,
        },
        "add_when_full": {
            "ev_per_dollar_implied": ev_threshold,
            "ev_per_dollar_external": 0.3,
            "convexity_multiple": conv_multiple,
        },
    }
    policy: Dict[str, Any] = {
        "policy_id": "test_diversity",
        "budgets": {
            "monthly_baseline": 5000.0, "monthly_max": 10000.0,
            "weekly_baseline": 1250.0,  "daily_baseline": 500.0,
            "weekly_kicker": 2500.0,    "daily_kicker": 1000.0,
        },
        "inventory_targets": {"crash": 1, "selloff": 1},
        "thresholds": {"crash": thresholds, "selloff": thresholds},
        "harvest": {
            "partial_close_multiple": 2.0, "full_close_multiple": 3.0,
            "time_stop_dte": 14, "time_stop_min_multiple": 1.2,
            "partial_close_fraction": 0.5,
        },
        "sizing": {"max_qty_per_trade": 10},
        "kicker": {"min_conditioning_confidence": 0.66, "max_vix_percentile": 35.0},
        "robustness": {"enabled": False},
        "roll": {"enabled": False},
        "limits": {"max_open_actions_per_day": 5, "max_close_actions_per_day": 5},
    }
    if diversity_pct is not None:
        policy["diversity"] = {"min_strike_distance_pct": diversity_pct}
    return policy


def _candidate(
    candidate_id="C001",
    underlier="SPY",
    regime="crash",
    long_strike=545.0,
    spot=580.0,
    ev_per_dollar=2.0,
    premium=60.0,
    max_gain=1800.0,
    p_used=0.10,
):
    """Build a minimal candidate dict for diversity testing."""
    c: Dict[str, Any] = {
        "candidate_id": candidate_id,
        "underlier": underlier,
        "regime": regime,
        "expiry": "20261120",
        "long_strike": long_strike,
        "short_strike": long_strike - 20,
        "computed_premium_usd": premium,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar": ev_per_dollar,
        "p_used": p_used,
        "p_used_src": "implied",
    }
    if spot is not None:
        c["spot"] = spot
    return c


# ===========================================================================
# Unit tests: _check_strike_diversity helper
# ===========================================================================

class TestCheckStrikeDiversityHelper:
    """Direct unit tests for _check_strike_diversity()."""

    def test_blocked_by_existing_position_too_close(self):
        """Candidate within 3% of existing position's long_strike → STRIKE_TOO_CLOSE."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        # spot=580, existing=540, candidate=545 → |545-540|/580 = 0.86% < 3%
        pos = _make_sleeve_position("TRADE_01", "SPY", "crash", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is not None
        assert "STRIKE_TOO_CLOSE" in result
        assert "MIN_STRIKE_DISTANCE_PCT:3.0" in result
        assert "EXISTING_LONG_STRIKE:540.0" in result
        assert "EXISTING_TRADE_ID:TRADE_01" in result

    def test_allowed_when_far_enough(self):
        """Candidate more than 3% from existing position → None (passes)."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        # spot=580, existing=540, candidate=520 → |520-540|/580 = 3.45% > 3%
        pos = _make_sleeve_position("TRADE_01", "SPY", "crash", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=520.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None

    def test_above_threshold_passes(self):
        """
        Distance just above the threshold (3.1%) → passes (rule is strictly <).
        spot=580, existing=540, candidate=522 → |522-540|/580 = 18/580 ≈ 3.10% > 3%.
        """
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        pos = _make_sleeve_position("TRADE_01", "SPY", "crash", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=522.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        # 18/580 ≈ 3.10% > 3.0% → should pass
        assert result is None, "3.10% distance should not be blocked by 3.0% threshold"

    def test_just_inside_threshold_blocked(self):
        """Just inside the threshold (2.93%) → BLOCKED."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        # |523.0-540.0|/580 = 17/580 = 2.93% < 3%
        pos = _make_sleeve_position("TRADE_01", "SPY", "crash", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=523.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is not None
        assert "STRIKE_TOO_CLOSE" in result

    def test_different_underlier_does_not_block(self):
        """QQQ crash position does not block SPY crash candidate."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        pos = _make_sleeve_position("TRADE_01", underlier="QQQ", regime="crash",
                                    long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None, "Different underlier must not block"

    def test_different_regime_does_not_block(self):
        """SPY selloff position does not block SPY crash candidate."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        pos = _make_sleeve_position("TRADE_01", underlier="SPY", regime="selloff",
                                    long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None, "Different regime must not block"

    def test_blocked_by_in_run_open_action(self):
        """Candidate too close to already-approved OPEN in same run → STRIKE_TOO_CLOSE."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        approved = _make_open_action("C_EXISTING", "SPY", "crash", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[], approved_opens=[approved],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is not None
        assert "STRIKE_TOO_CLOSE" in result
        assert "EXISTING_CANDIDATE_ID:C_EXISTING" in result

    def test_in_run_different_regime_does_not_block(self):
        """In-run OPEN with different regime does not block the candidate."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        approved = _make_open_action("C_SELLOFF", "SPY", "selloff", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[], approved_opens=[approved],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None, "In-run OPEN with different regime must not block"

    def test_in_run_different_underlier_does_not_block(self):
        """In-run OPEN with different underlier does not block the candidate."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        approved = _make_open_action("C_QQQ", "QQQ", "crash", long_strike=540.0)
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[], approved_opens=[approved],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None, "In-run OPEN with different underlier must not block"

    def test_position_with_empty_strikes_skipped(self):
        """Position with empty strikes list is gracefully skipped."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        from forecast_arb.allocator.types import SleevePosition
        pos_no_strikes = SleevePosition(
            trade_id="T_NOSTRIKE", underlier="SPY", regime="crash",
            expiry="20261120", strikes=[], qty_open=1,
            entry_debit=60.0, mark_mid=60.0, dte=90,
        )
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[pos_no_strikes], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None, "Position with empty strikes must be skipped gracefully"

    def test_empty_positions_and_actions_passes(self):
        """No existing positions and no in-run OPENs → always passes."""
        from forecast_arb.allocator.open_plan import _check_strike_diversity
        result = _check_strike_diversity(
            candidate_long_strike=545.0, spot=580.0,
            diversity_threshold_pct=3.0,
            existing_positions=[], approved_opens=[],
            cand_underlier="SPY", cand_regime="crash",
        )
        assert result is None


# ===========================================================================
# Integration: diversity gate via generate_open_actions
# ===========================================================================

class TestDiversityGatingIntegration:
    """Tests for diversity gate wired into generate_open_actions()."""

    def _run(self, candidates, policy, positions=None, inventory=None):
        from forecast_arb.allocator.open_plan import generate_open_actions
        budget = _make_budget()
        if inventory is None:
            inventory = _make_inventory(crash_open=0)
        rejection_log: List[Dict] = []
        actions = generate_open_actions(
            candidates_data={"selected": candidates},
            policy=policy,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
            positions=positions or [],
        )
        return actions, rejection_log

    def test_blocked_by_too_close_existing_position(self):
        """Candidate within 3% of existing position's long_strike → no OPEN."""
        policy = _make_policy(diversity_pct=3.0)
        # existing: SPY crash at 540.0
        # candidate: SPY crash at 545.0 → only 0.86% apart → blocked
        pos = _make_sleeve_position("T1", "SPY", "crash", long_strike=540.0)
        cand = _candidate(long_strike=545.0, spot=580.0)

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(actions) == 0
        assert len(rejection_log) == 1
        assert rejection_log[0]["primary_reason"] == "STRIKE_TOO_CLOSE"

    def test_allowed_when_far_enough_from_existing(self):
        """Candidate more than 3% from existing position → OPEN allowed."""
        policy = _make_policy(diversity_pct=3.0)
        # existing: SPY crash at 540.0
        # candidate: SPY crash at 520.0 → 3.45% apart → allowed
        pos = _make_sleeve_position("T1", "SPY", "crash", long_strike=540.0)
        cand = _candidate(long_strike=520.0, spot=580.0)

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(actions) == 1
        assert actions[0].type == "OPEN"

    def test_diversity_disabled_when_config_absent(self):
        """When diversity config is absent, gate is disabled — candidate always allowed."""
        policy = _make_policy(diversity_pct=None)  # no diversity section
        pos = _make_sleeve_position("T1", "SPY", "crash", long_strike=540.0)
        cand = _candidate(long_strike=545.0, spot=580.0)  # would be blocked if enabled

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(actions) == 1
        assert actions[0].type == "OPEN"

    def test_missing_spot_does_not_crash_or_block(self):
        """When candidate has no spot field, diversity check is skipped → OPEN allowed."""
        policy = _make_policy(diversity_pct=3.0)
        pos = _make_sleeve_position("T1", "SPY", "crash", long_strike=540.0)
        # candidate with no spot
        cand = _candidate(long_strike=545.0, spot=None)  # spot=None → skipped

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(actions) == 1
        assert actions[0].type == "OPEN"

    def test_different_underlier_not_blocked(self):
        """SPY candidate not blocked by QQQ position (different underlier)."""
        policy = _make_policy(diversity_pct=3.0)
        pos = _make_sleeve_position("T1", underlier="QQQ", regime="crash",
                                    long_strike=540.0)
        cand = _candidate(underlier="SPY", long_strike=545.0, spot=580.0)

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(actions) == 1

    def test_different_regime_not_blocked(self):
        """SPY crash candidate not blocked by SPY selloff position (different regime)."""
        policy = _make_policy(diversity_pct=3.0)
        pos = _make_sleeve_position("T1", underlier="SPY", regime="selloff",
                                    long_strike=540.0)
        cand = _candidate(underlier="SPY", regime="crash",
                          long_strike=545.0, spot=580.0)

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(actions) == 1

    def test_no_existing_positions_allows_open(self):
        """With no existing positions and diversity enabled, OPEN is allowed."""
        policy = _make_policy(diversity_pct=3.0)
        cand = _candidate(long_strike=545.0, spot=580.0)

        actions, rejection_log = self._run([cand], policy, positions=[])

        assert len(actions) == 1

    def test_reason_code_in_rejection_log(self):
        """Rejected diversity candidate has STRIKE_TOO_CLOSE in rejection_log with metadata."""
        policy = _make_policy(diversity_pct=3.0)
        pos = _make_sleeve_position("T_EXISTING", "SPY", "crash", long_strike=540.0)
        cand = _candidate(candidate_id="C_CLOSE", long_strike=543.0, spot=580.0)

        actions, rejection_log = self._run([cand], policy, positions=[pos])

        assert len(rejection_log) == 1
        entry = rejection_log[0]
        assert entry["primary_reason"] == "STRIKE_TOO_CLOSE"
        reason = entry["reason"]
        assert "MIN_STRIKE_DISTANCE_PCT:3.0" in reason
        assert "EXISTING_LONG_STRIKE:540.0" in reason
        assert "EXISTING_TRADE_ID:T_EXISTING" in reason

    def test_zero_diversity_pct_disables_check(self):
        """min_strike_distance_pct: 0.0 → gate disabled (enabled=False)."""
        policy = _make_policy(diversity_pct=0.0)
        pos = _make_sleeve_position("T1", "SPY", "crash", long_strike=540.0)
        cand = _candidate(long_strike=541.0, spot=580.0)  # extremely close

        actions, _ = self._run([cand], policy, positions=[pos])

        assert len(actions) == 1, "0.0 threshold disables the gate"


# ===========================================================================
# Policy helper: get_diversity_params
# ===========================================================================

class TestGetDiversityParams:
    """Unit tests for policy.get_diversity_params()."""

    def test_absent_section_returns_disabled(self):
        """When diversity section absent → enabled=False."""
        from forecast_arb.allocator.policy import get_diversity_params
        policy = {}
        result = get_diversity_params(policy)
        assert result["enabled"] is False
        assert result["min_strike_distance_pct"] == 0.0

    def test_present_positive_pct_enabled(self):
        """When min_strike_distance_pct > 0 → enabled=True."""
        from forecast_arb.allocator.policy import get_diversity_params
        policy = {"diversity": {"min_strike_distance_pct": 3.0}}
        result = get_diversity_params(policy)
        assert result["enabled"] is True
        assert result["min_strike_distance_pct"] == pytest.approx(3.0)

    def test_zero_pct_disabled(self):
        """When min_strike_distance_pct == 0 → enabled=False."""
        from forecast_arb.allocator.policy import get_diversity_params
        policy = {"diversity": {"min_strike_distance_pct": 0.0}}
        result = get_diversity_params(policy)
        assert result["enabled"] is False

    def test_empty_diversity_section_disabled(self):
        """Empty diversity dict → enabled=False."""
        from forecast_arb.allocator.policy import get_diversity_params
        policy = {"diversity": {}}
        result = get_diversity_params(policy)
        assert result["enabled"] is False
