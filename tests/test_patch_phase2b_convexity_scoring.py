"""
Phase 2A Tests — Task B: Convexity Scoring

Tests for compute_convexity_score() and the updated candidate ranking
in open_plan._layer_sort_key().

Covers:
  - formula: ev × (max_gain/premium) × p_used
  - optional liquidity penalty: score × exp(-spread_width/premium)
  - graceful fallback when required inputs are missing → score=0.0
  - higher-payoff candidate ranks above lower-payoff when both pass gates
  - ladder preference still dominates (Layer A beats Layer B when crash, inv=0)
  - convexity_score appears in rejection_log trace
  - existing EV/convexity_multiple gates are untouched

All tests are deterministic and use no network calls.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_budget(**kwargs):
    from forecast_arb.allocator.types import BudgetState
    defaults = {
        "monthly_baseline": 5000.0,
        "monthly_max": 10000.0,
        "weekly_baseline": 1250.0,
        "daily_baseline": 500.0,
        "weekly_kicker": 2500.0,
        "daily_kicker": 1000.0,
    }
    defaults.update(kwargs)
    return BudgetState(**defaults)


def _make_inventory(crash_open=0, crash_target=1, selloff_open=0, selloff_target=1):
    from forecast_arb.allocator.types import InventoryState
    return InventoryState(
        crash_target=crash_target, crash_open=crash_open,
        selloff_target=selloff_target, selloff_open=selloff_open,
    )


def _make_policy(ev_threshold=1.0, convexity_multiple=10.0, with_ladder=False):
    """Minimal policy dict for testing."""
    thresholds: Dict[str, Any] = {
        "fill_when_empty": {
            "ev_per_dollar_implied": ev_threshold,
            "ev_per_dollar_external": 0.3,
            "convexity_multiple": convexity_multiple,
        },
        "add_when_full": {
            "ev_per_dollar_implied": ev_threshold,
            "ev_per_dollar_external": 0.3,
            "convexity_multiple": convexity_multiple,
        },
    }
    if with_ladder:
        thresholds["ladder"] = {
            "layer_a": {"moneyness_min_pct": 5.0, "moneyness_max_pct": 9.0},
            "layer_b": {"moneyness_min_pct": 10.0, "moneyness_max_pct": 16.0},
        }
    return {
        "policy_id": "test_scoring",
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


def _candidate(
    candidate_id="C001",
    regime="crash",
    ev_per_dollar=2.0,
    premium=60.0,
    max_gain=1800.0,
    p_used=0.10,
    spread_width=None,
    spot=None,
    long_strike=None,
    short_strike=None,
):
    """Build a minimal candidate dict."""
    c: Dict[str, Any] = {
        "candidate_id": candidate_id,
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20261120",
        "computed_premium_usd": premium,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar": ev_per_dollar,
        "p_used": p_used,
        "p_used_src": "implied",
    }
    if spread_width is not None:
        c["spread_width"] = spread_width
    if spot is not None:
        c["spot"] = spot
    if long_strike is not None:
        c["long_strike"] = long_strike
    if short_strike is not None:
        c["short_strike"] = short_strike
    return c


# ===========================================================================
# Unit tests: compute_convexity_score
# ===========================================================================

class TestComputeConvexityScore:
    """Direct unit tests for scoring.compute_convexity_score()."""

    def test_basic_formula(self):
        """score = ev_per_dollar × (max_gain/premium) × p_used"""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10)
        # payoff_multiple = 1800/60 = 30
        # score = 2.0 * 30 * 0.10 = 6.0
        score = compute_convexity_score(c)
        assert score == pytest.approx(6.0, rel=1e-6)

    def test_higher_payoff_multiple_gives_higher_score(self):
        """Same EV/$ and p, but higher max_gain → higher score."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c_high = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=3000.0, p_used=0.10)
        c_low  = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=1200.0, p_used=0.10)
        assert compute_convexity_score(c_high) > compute_convexity_score(c_low)

    def test_liquidity_penalty_applied_when_spread_width_present(self):
        """Score is reduced by exp(-spread_width/premium) when spread_width is given."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        base = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10)
        with_spread = _candidate(
            ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10,
            spread_width=6.0,  # spread_width/premium = 0.1
        )
        base_score = compute_convexity_score(base)
        penalised_score = compute_convexity_score(with_spread)
        # penalty = exp(-6/60) = exp(-0.1) ≈ 0.9048
        assert penalised_score == pytest.approx(base_score * math.exp(-6.0 / 60.0), rel=1e-6)
        assert penalised_score < base_score

    def test_wider_spread_gives_lower_score(self):
        """Wider spread_width reduces score more than narrower spread."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        narrow = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=1800.0,
                            p_used=0.10, spread_width=3.0)
        wide   = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=1800.0,
                            p_used=0.10, spread_width=12.0)
        assert compute_convexity_score(narrow) > compute_convexity_score(wide)

    def test_no_spread_width_no_penalty(self):
        """When spread_width is absent, penalty is identity (×1.0)."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = _candidate(ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10)
        expected = 2.0 * (1800.0 / 60.0) * 0.10  # = 6.0
        assert compute_convexity_score(c) == pytest.approx(expected)

    def test_missing_ev_per_dollar_returns_zero(self):
        """Returns 0.0 gracefully when ev_per_dollar is absent."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = {"computed_premium_usd": 60.0, "max_gain_per_contract": 1800.0, "p_used": 0.10}
        assert compute_convexity_score(c) == 0.0

    def test_missing_premium_returns_zero(self):
        """Returns 0.0 gracefully when premium is absent."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = {"ev_per_dollar": 2.0, "max_gain_per_contract": 1800.0, "p_used": 0.10}
        assert compute_convexity_score(c) == 0.0

    def test_missing_max_gain_returns_zero(self):
        """Returns 0.0 gracefully when max_gain_per_contract is absent."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = {"ev_per_dollar": 2.0, "computed_premium_usd": 60.0, "p_used": 0.10}
        assert compute_convexity_score(c) == 0.0

    def test_missing_p_used_returns_zero(self):
        """Returns 0.0 gracefully when p_used (and all aliases) are absent."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = {"ev_per_dollar": 2.0, "computed_premium_usd": 60.0, "max_gain_per_contract": 1800.0}
        assert compute_convexity_score(c) == 0.0

    def test_p_used_alias_p_event_used(self):
        """score is computed correctly when p_event_used is used as the alias."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = {
            "ev_per_dollar": 2.0,
            "computed_premium_usd": 60.0,
            "max_gain_per_contract": 1800.0,
            "p_event_used": 0.10,
        }
        assert compute_convexity_score(c) == pytest.approx(6.0, rel=1e-6)

    def test_debit_per_contract_used_as_premium_fallback(self):
        """debit_per_contract is used when computed_premium_usd is absent."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = {
            "ev_per_dollar": 2.0,
            "debit_per_contract": 60.0,
            "max_gain_per_contract": 1800.0,
            "p_used": 0.10,
        }
        assert compute_convexity_score(c) == pytest.approx(6.0, rel=1e-6)

    def test_returns_nonnegative(self):
        """Score is always >= 0.0 even with negative ev_per_dollar."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = _candidate(ev_per_dollar=-1.0, premium=60.0, max_gain=1800.0, p_used=0.10)
        assert compute_convexity_score(c) == 0.0

    def test_empty_candidate_returns_zero(self):
        """Empty dict returns 0.0 without error."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        assert compute_convexity_score({}) == 0.0


# ===========================================================================
# Ranking: higher-payoff outranks lower-payoff when both pass thresholds
# ===========================================================================

class TestConvexityScoreRanking:
    """Tests that candidate ordering reflects convexity_score as middle rank."""

    def _run_generate(self, candidates, policy, budget=None, inventory=None):
        from forecast_arb.allocator.open_plan import generate_open_actions
        if budget is None:
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
        )
        return actions, rejection_log

    def test_high_payoff_ranks_first_same_ev(self):
        """
        Two candidates with similar EV/$ but different max_gain:
        high-payoff candidate should be selected (ranked first).
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0)

        # Both pass gates (EV=2.0 > 1.0; convexity >> 10x)
        # Same EV/$, but C_HIGH has much larger payoff multiple
        c_low  = _candidate("C_LOW",  ev_per_dollar=2.0, premium=60.0, max_gain=1200.0, p_used=0.10)
        c_high = _candidate("C_HIGH", ev_per_dollar=2.0, premium=60.0, max_gain=3000.0, p_used=0.10)

        # Present in order [low, high] — score should flip the order
        actions, rejection_log = self._run_generate([c_low, c_high], policy)

        assert len(actions) == 1
        # The high-payoff candidate should have been selected
        assert actions[0].candidate_id == "C_HIGH", (
            f"Expected C_HIGH to win (higher score), got: {actions[0].candidate_id}. "
            f"Scores: C_LOW={[r['convexity_score'] for r in rejection_log if r['candidate_id']=='C_LOW']}, "
            f"C_HIGH={[r['convexity_score'] for r in rejection_log if r['candidate_id']=='C_HIGH']}"
        )

    def test_low_payoff_candidate_is_in_rejection_log_as_rejected(self):
        """
        When high-payoff wins, the low-payoff candidate appears in rejection_log
        because the loop breaks after the first APPROVED action.
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0)
        c_low  = _candidate("C_LOW",  ev_per_dollar=2.0, premium=60.0, max_gain=1200.0, p_used=0.10)
        c_high = _candidate("C_HIGH", ev_per_dollar=2.0, premium=60.0, max_gain=3000.0, p_used=0.10)

        actions, rejection_log = self._run_generate([c_low, c_high], policy)

        # Only one action generated
        assert len(actions) == 1
        # We should see at least one entry in the trace
        trace_ids = [r["candidate_id"] for r in rejection_log]
        assert "C_HIGH" in trace_ids

    def test_convexity_score_appears_in_trace(self):
        """Each rejection_log entry should include convexity_score."""
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0)
        c = _candidate("C_TRACE", ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10)

        _, rejection_log = self._run_generate([c], policy)

        assert len(rejection_log) >= 1
        entry = rejection_log[0]
        assert "convexity_score" in entry, "convexity_score must appear in trace"
        # score = 2.0 × (1800/60) × 0.10 = 6.0
        assert entry["convexity_score"] == pytest.approx(6.0, rel=1e-4)

    def test_ev_threshold_gate_still_blocks_low_ev(self):
        """
        Even if a candidate has an excellent convexity_score, EV/$ gate still blocks it.
        Score is ONLY for ranking, not for pass/fail.
        """
        policy = _make_policy(ev_threshold=3.0, convexity_multiple=10.0)  # high EV bar
        # Both candidates have great payoff but EV=1.5 < 3.0 threshold
        c = _candidate("C_BLOCKED", ev_per_dollar=1.5, premium=60.0, max_gain=3000.0, p_used=0.10)

        actions, rejection_log = self._run_generate([c], policy)

        assert len(actions) == 0
        assert len(rejection_log) == 1
        assert rejection_log[0]["primary_reason"] == "EV_BELOW_THRESHOLD"

    def test_convexity_multiple_gate_still_blocks_low_multiple(self):
        """
        Even if convexity_score is computed, the convexity MULTIPLE gate still blocks.
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=30.0)  # needs 30x
        # max_gain/premium = 1800/60 = 30x — exactly at threshold
        # 30 >= 30 → passes
        c_at  = _candidate("C_AT",    ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10)
        # max_gain/premium = 1740/60 = 29x < 30x → blocked
        c_low = _candidate("C_BELOW", ev_per_dollar=2.0, premium=60.0, max_gain=1740.0, p_used=0.10)

        actions_at, _ = self._run_generate([c_at], policy)
        actions_low, log_low = self._run_generate([c_low], policy)

        assert len(actions_at) == 1   # passes
        assert len(actions_low) == 0  # blocked
        assert log_low[0]["primary_reason"] == "CONVEXITY_TOO_LOW"

    def test_missing_scoring_inputs_falls_back_to_ev_ordering(self):
        """
        When scoring inputs are missing (score=0.0 for all), ev_per_dollar is
        the tiebreaker — same behaviour as before Task B.
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0)

        # Candidates with no p_used → score=0.0 for both
        c_high_ev = {
            "candidate_id": "C_HIGH_EV", "regime": "crash", "underlier": "SPY",
            "expiry": "20261120", "computed_premium_usd": 60.0,
            "max_gain_per_contract": 1800.0, "ev_per_dollar": 2.5,
            "p_used_src": "implied",
            # p_used intentionally absent → score=0.0
        }
        c_low_ev = {
            "candidate_id": "C_LOW_EV", "regime": "crash", "underlier": "SPY",
            "expiry": "20261120", "computed_premium_usd": 60.0,
            "max_gain_per_contract": 1800.0, "ev_per_dollar": 2.0,
            "p_used_src": "implied",
        }

        actions, _ = self._run_generate([c_low_ev, c_high_ev], policy)

        # With score=0.0 for both, higher ev_per_dollar wins
        assert len(actions) == 1
        assert actions[0].candidate_id == "C_HIGH_EV"


# ===========================================================================
# Ladder preference with convexity scoring
# ===========================================================================

class TestLadderPriorityPreservedWithScoring:
    """
    Ladder (Layer A before Layer B when crash, inv=0) must still dominate even
    when the Layer B candidate has a HIGHER convexity_score.
    """

    def _run_generate(self, candidates, policy, inv_crash_open=0):
        from forecast_arb.allocator.open_plan import generate_open_actions
        budget = _make_budget()
        inventory = _make_inventory(crash_open=inv_crash_open)
        rejection_log: List[Dict] = []
        actions = generate_open_actions(
            candidates_data={"selected": candidates},
            policy=policy, budget=budget, inventory=inventory,
            rejection_log=rejection_log,
        )
        return actions, rejection_log

    def test_layer_a_beats_layer_b_even_with_lower_score_when_inv_zero(self):
        """
        Layer A candidate with lower convexity_score beats Layer B with higher
        score when crash inventory is 0 (ladder preference dominates).
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0, with_ladder=True)

        # Layer A: spot=580, long_strike=540 → moneyness=(580-540)/580*100 ≈ 6.9% → Layer A
        # Layer B: spot=580, long_strike=515 → moneyness=(580-515)/580*100 ≈ 11.2% → Layer B
        c_layer_a = _candidate(
            "LAYER_A", ev_per_dollar=2.0, premium=60.0, max_gain=1200.0, p_used=0.10,
            spot=580.0, long_strike=540.0, short_strike=520.0,
        )
        c_layer_b = _candidate(
            "LAYER_B", ev_per_dollar=2.0, premium=60.0, max_gain=3000.0, p_used=0.10,
            spot=580.0, long_strike=515.0, short_strike=495.0,
        )
        # LAYER_B has higher score (50x vs 20x payoff) but should lose to LAYER_A
        # because ladder preference (priority 0) < (priority 1)

        actions, _ = self._run_generate([c_layer_b, c_layer_a], policy, inv_crash_open=0)

        assert len(actions) == 1
        assert actions[0].candidate_id == "LAYER_A", (
            "Layer A must win over Layer B when crash inv=0, regardless of score"
        )

    def test_layer_b_wins_with_higher_score_when_inv_nonzero(self):
        """
        When crash inventory > 0 (no ladder preference), the higher-scoring
        Layer B candidate wins over lower-scoring Layer A.
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0, with_ladder=True)
        # Same candidates as above but inv=1
        c_layer_a = _candidate(
            "LAYER_A", ev_per_dollar=2.0, premium=60.0, max_gain=1200.0, p_used=0.10,
            spot=580.0, long_strike=540.0, short_strike=520.0,
        )
        c_layer_b = _candidate(
            "LAYER_B", ev_per_dollar=2.0, premium=60.0, max_gain=3000.0, p_used=0.10,
            spot=580.0, long_strike=515.0, short_strike=495.0,
        )

        # With inv=1, crash target=1, so needs_open=False → no OPEN generated at all.
        # Reuse crash_target=2 so inv_open=1 still needs one more.
        from forecast_arb.allocator.types import InventoryState
        from forecast_arb.allocator.open_plan import generate_open_actions
        budget = _make_budget()
        inventory = InventoryState(crash_target=2, crash_open=1, selloff_target=1, selloff_open=0)
        actions = generate_open_actions(
            candidates_data={"selected": [c_layer_a, c_layer_b]},
            policy=policy, budget=budget, inventory=inventory,
        )

        assert len(actions) == 1
        assert actions[0].candidate_id == "LAYER_B", (
            "With inv>0 and no ladder preference, Layer B wins via higher convexity_score"
        )

    def test_both_layers_same_score_ladder_still_decides(self):
        """
        When two candidates have equal scores, ladder preference still breaks
        the tie (Layer A < Layer B in sort order).
        """
        policy = _make_policy(ev_threshold=1.0, convexity_multiple=10.0, with_ladder=True)

        # Both have exactly the same ev/premium/max_gain/p → identical scores.
        c_a = _candidate(
            "EQUAL_A", ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10,
            spot=580.0, long_strike=540.0, short_strike=520.0,
        )
        c_b = _candidate(
            "EQUAL_B", ev_per_dollar=2.0, premium=60.0, max_gain=1800.0, p_used=0.10,
            spot=580.0, long_strike=515.0, short_strike=495.0,
        )

        actions, _ = self._run_generate([c_b, c_a], policy, inv_crash_open=0)

        assert len(actions) == 1
        # When scores are equal, Layer A (priority=0) must still beat Layer B (priority=1)
        assert actions[0].candidate_id == "EQUAL_A"


# ===========================================================================
# _layer_sort_key unit tests
# ===========================================================================

class TestLayerSortKey:
    """Tests for _layer_sort_key returning the correct 3-tuple."""

    def _key(self, candidate, regime, inv_open=0, with_ladder=False):
        from forecast_arb.allocator.open_plan import _layer_sort_key
        policy = _make_policy(with_ladder=with_ladder)
        return _layer_sort_key(candidate, policy, regime, inv_open)

    def test_non_crash_returns_priority_one(self):
        """Non-crash candidates always get priority=1."""
        c = _candidate("S1", regime="selloff", ev_per_dollar=2.0, premium=60.0,
                        max_gain=1800.0, p_used=0.10)
        key = self._key(c, "selloff")
        assert key[0] == 1

    def test_crash_inv_nonzero_returns_priority_one(self):
        """Crash with inv>0 returns priority=1 (no ladder preference)."""
        c = _candidate("C1", regime="crash", ev_per_dollar=2.0, premium=60.0,
                        max_gain=1800.0, p_used=0.10)
        key = self._key(c, "crash", inv_open=1)
        assert key[0] == 1

    def test_layer_a_gets_priority_zero(self):
        """Layer A candidate gets priority=0 in key[0]."""
        c = _candidate("A1", regime="crash", ev_per_dollar=2.0, premium=60.0,
                        max_gain=1800.0, p_used=0.10, spot=580.0,
                        long_strike=540.0, short_strike=520.0)
        key = self._key(c, "crash", inv_open=0, with_ladder=True)
        assert key[0] == 0

    def test_layer_b_gets_priority_one(self):
        """Layer B candidate gets priority=1 in key[0]."""
        c = _candidate("B1", regime="crash", ev_per_dollar=2.0, premium=60.0,
                        max_gain=1800.0, p_used=0.10, spot=580.0,
                        long_strike=515.0, short_strike=495.0)
        key = self._key(c, "crash", inv_open=0, with_ladder=True)
        assert key[0] == 1

    def test_score_is_middle_element_negated(self):
        """key[1] == -convexity_score (negated for descending sort)."""
        from forecast_arb.allocator.scoring import compute_convexity_score
        c = _candidate("C2", regime="crash", ev_per_dollar=2.0, premium=60.0,
                        max_gain=1800.0, p_used=0.10)
        key = self._key(c, "crash", inv_open=0)
        expected_score = compute_convexity_score(c)
        assert key[1] == pytest.approx(-expected_score)

    def test_ev_is_last_element_negated(self):
        """key[2] == -ev_per_dollar (negated for descending sort)."""
        c = _candidate("C3", regime="crash", ev_per_dollar=3.5, premium=60.0,
                        max_gain=1800.0, p_used=0.10)
        key = self._key(c, "crash", inv_open=0)
        assert key[2] == pytest.approx(-3.5)

    def test_missing_score_inputs_fallback_sort_by_ev(self):
        """
        When score=0.0 (missing p_used), crash+inv=0 path evaluates ladder
        which returns None layer → priority=2.  score=0.0, ev=-3.0.
        All crash candidates in the same regime without layer data get the
        same priority (2), so they sort purely by score then EV — correct.
        """
        c_no_p = {
            "candidate_id": "NO_P", "regime": "crash",
            "computed_premium_usd": 60.0, "max_gain_per_contract": 1800.0,
            "ev_per_dollar": 3.0,
            # no p_used → score=0.0; no spot data → layer=None → priority=2
        }
        from forecast_arb.allocator.open_plan import _layer_sort_key
        policy = _make_policy()
        key = _layer_sort_key(c_no_p, policy, "crash", 0)
        # priority=2: crash+inv=0 → ladder path → _classify_ladder_layer returns None
        #             {"A":0,"B":1}.get(None, 2) = 2
        # score_element = -0.0 (i.e., 0.0 negated; -0.0 == 0.0 in Python)
        assert key[0] == 2
        assert key[1] == pytest.approx(0.0)
        assert key[2] == pytest.approx(-3.0)

    def test_non_crash_missing_score_inputs_returns_priority_one(self):
        """For non-crash regime, missing inputs → priority=1, score=0.0, -ev."""
        c_no_p = {
            "candidate_id": "S_NO_P", "regime": "selloff",
            "computed_premium_usd": 60.0, "max_gain_per_contract": 1800.0,
            "ev_per_dollar": 3.0,
        }
        from forecast_arb.allocator.open_plan import _layer_sort_key
        policy = _make_policy()
        key = _layer_sort_key(c_no_p, policy, "selloff", 0)
        assert key[0] == 1
        assert key[1] == pytest.approx(0.0)
        assert key[2] == pytest.approx(-3.0)
