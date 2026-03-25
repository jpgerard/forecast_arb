"""
CCC v1.9 "Max Value / Low Complexity" patch tests.

Tests 1-10 as specified in the patch spec §7 TEST PLAN.
All tests are deterministic and mock quotes where needed.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixture builders
# ─────────────────────────────────────────────────────────────────────────────

BASE_POLICY = {
    "policy_id": "ccc_v1_test",
    "budgets": {
        "monthly_baseline": 1000.0,
        "monthly_max": 2000.0,
        "weekly_baseline": 500.0,
        "weekly_kicker": 1000.0,
        "daily_baseline": 200.0,
        "daily_kicker": 400.0,
    },
    "inventory_targets": {"crash": 1, "selloff": 1},
    "thresholds": {
        "crash": {
            "fill_when_empty": {
                "ev_per_dollar_implied": 1.20,
                "ev_per_dollar_external": 0.30,
                "convexity_multiple": 10.0,
            },
            "add_when_full": {
                "ev_per_dollar_implied": 1.60,
                "ev_per_dollar_external": 0.50,
                "convexity_multiple": 20.0,
            },
        },
        "selloff": {
            "fill_when_empty": {
                "ev_per_dollar_implied": 1.10,
                "ev_per_dollar_external": 0.25,
                "convexity_multiple": 8.0,
            },
            "add_when_full": {
                "ev_per_dollar_implied": 1.30,
                "ev_per_dollar_external": 0.30,
                "convexity_multiple": 12.0,
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
    "kicker": {
        "min_conditioning_confidence": 0.66,
        "max_vix_percentile": 35.0,
    },
    "robustness": {
        "enabled": True,
        "p_downshift_pp": 3.0,
        "debit_upshift_pct": 10.0,
        "require_positive_ev_under_shocks": True,
        "allow_if_inventory_empty": True,
    },
    "roll": {
        "enabled": True,
        "dte_max_for_roll": 21,
        "min_multiple_to_hold": 1.10,
        "min_convexity_multiple_to_hold": 8.0,
    },
    "close_liquidity_guard": {"max_width_pct": 0.25},
    "limits": {
        "max_open_actions_per_day": 2,
        "max_close_actions_per_day": 2,
    },
}


def make_budget(daily=200.0, weekly=500.0, monthly=1000.0, spent=0.0):
    from forecast_arb.allocator.types import BudgetState
    return BudgetState(
        monthly_baseline=monthly,
        monthly_max=2000.0,
        weekly_baseline=weekly,
        weekly_kicker=1000.0,
        daily_baseline=daily,
        daily_kicker=400.0,
        spent_today=spent,
        spent_week=spent,
        spent_month=spent,
    )


def make_inventory(
    crash_open=0, crash_target=1,
    selloff_open=0, selloff_target=1,
):
    from forecast_arb.allocator.types import InventoryState
    return InventoryState(
        crash_target=crash_target,
        crash_open=crash_open,
        selloff_target=selloff_target,
        selloff_open=selloff_open,
    )


def make_candidate(
    regime="crash",
    ev_per_dollar=1.60,
    premium=60.0,
    max_gain=1200.0,
    p_used=0.07,
    spot=None,
    long_strike=None,
    long_ask=None,
    long_bid=None,
    long_mid=None,
    short_ask=None,
    short_bid=None,
    short_mid=None,
    candidate_id=None,
) -> Dict[str, Any]:
    if long_strike is None:
        long_strike = (spot or 560.0)
    candidate = {
        "candidate_id": candidate_id or f"SPY_20260402_{long_strike:.0f}_{regime}",
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20260402",
        "long_strike": long_strike,
        "short_strike": long_strike - 20.0,
        "computed_premium_usd": premium,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar": ev_per_dollar,
        "p_used": p_used,
        "representable": True,
    }
    if spot is not None:
        candidate["spot"] = spot
    if long_ask is not None:
        candidate["long_ask"] = long_ask
    if long_bid is not None:
        candidate["long_bid"] = long_bid
    if long_mid is not None:
        candidate["long_mid"] = long_mid
    if short_ask is not None:
        candidate["short_ask"] = short_ask
    if short_bid is not None:
        candidate["short_bid"] = short_bid
    if short_mid is not None:
        candidate["short_mid"] = short_mid
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Worst-case debit flips OPEN → HOLD when WC premium is higher
# ─────────────────────────────────────────────────────────────────────────────

class TestWorstCasePricingGating:
    """Task A: worst-case debit is used for gating and can flip OPEN→HOLD."""

    def test_wc_premium_flips_pass_to_hold(self):
        """
        A candidate that passes EV threshold at mid-price BUT fails when
        worst-case (ask/bid) premium is significantly wider should be HELD.

        Setup:
          p_used=0.07, max_gain=1200, premium_mid=60 → ev_mid=1.80
          But long_ask=0.85, short_bid=0.05 → premium_wc=80
          ev_wc = (0.07*1200 - 0.93*80)/80 = (84-74.4)/80 = 0.12 → passes EV 1.2 threshold
          But if we set threshold higher:

        Let's use a more dramatic case to guarantee a flip:
          p_used=0.07, max_gain=1200, premium_mid=60, premium_wc=200
          ev_wc = (0.07*1200 - 0.93*200)/200 = (84-186)/200 = -0.51 → fails EV threshold of 1.2

        We need premium_wc > threshold_crossing:
          ev_wc >= 1.2 iff p*max_gain - (1-p)*wc >= 1.2*wc
          p*max_gain >= wc*(1+1.2) + (1-p)*wc ... no wait:
          ev_wc/$ = (p*max_gain - (1-p)*wc) / wc = p*max_gain/wc - (1-p)
          For ev_wc/$ < 1.2:
            p*max_gain/wc < 1.2 + (1-p)
            p*max_gain < wc * (1.2 + 1 - p)
            wc > p*max_gain / (2.2 - p)
            wc > 0.07*1200 / (2.2 - 0.07) = 84 / 2.13 ≈ 39.4

        So premium_wc=40 → ev_wc/$ < 1.2 (below fill_when_empty threshold)
        But mid premium of 30 → ev_mid/$ = (0.07*1200 - 0.93*30)/30 = (84-27.9)/30 = 1.87 → passes

        Let me verify: with premium_wc=40:
          ev_wc = 0.07*1200 - 0.93*40 = 84 - 37.2 = 46.8
          ev_wc/$ = 46.8/40 = 1.17 < 1.20 ← FAILS fill_when_empty threshold

        With premium_mid=30:
          ev_mid = 0.07*1200 - 0.93*30 = 84 - 27.9 = 56.1
          ev_mid/$ = 56.1/30 = 1.87 > 1.20 ← PASSES

        So: long_ask - short_bid = 0.40 (per share) → premium_wc = 40 $/contract
            long_mid - short_mid = 0.30 → premium_mid = 30 $/contract
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        # Candidate with mid=30 passing EV, but WC=40 failing EV
        candidate = make_candidate(
            regime="crash",
            ev_per_dollar=1.87,       # mid-based EV (passes)
            premium=30.0,             # campaign-computed mid premium $/contract
            max_gain=1200.0,
            p_used=0.07,
            long_ask=0.65,            # ask for long leg ~ per share
            long_bid=0.55,
            long_mid=0.60,
            short_ask=0.30,           # ask for short leg
            short_bid=0.25,           # bid for short leg (used in WC)
            short_mid=0.28,
            # WC = long_ask - short_bid = 0.65 - 0.25 = 0.40 → premium_wc = 40
            # mid = long_mid - short_mid = 0.60 - 0.28 = 0.32 → premium_mid = 32
            # ev_wc = (0.07 * 1200 - 0.93 * 40) / 40 = 46.8/40 = 1.17 < 1.20 ← HOLDS
        )

        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0, crash_target=1)
        rejection_log = []

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
        )

        # Should have NO open action (WC premium fails EV gate)
        assert len(actions) == 0, (
            f"Expected HOLD but got {len(actions)} open actions. "
            f"Rejection log: {rejection_log}"
        )
        # Verify rejection reason is EV-related
        rejection = rejection_log[0]
        assert "EV_BELOW_THRESHOLD" in rejection.get("reason", ""), (
            f"Expected EV_BELOW_THRESHOLD rejection, got: {rejection.get('reason')}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: PREMIUM_USED reason codes present (WC vs MID)
# ─────────────────────────────────────────────────────────────────────────────

class TestPremiumUsedReasonCodes:

    def _make_passing_candidate(self, with_quotes=True, long_ask=None, short_bid=None):
        """Make a candidate that will pass all gates."""
        c = make_candidate(
            regime="crash",
            ev_per_dollar=2.0,
            premium=50.0,
            max_gain=2000.0,
            p_used=0.10,
        )
        if with_quotes:
            # WC = long_ask - short_bid, ensure WC premium still passes EV gate
            # ev_wc = (0.10*2000 - 0.90*(long_ask-short_bid)*100) / ((long_ask-short_bid)*100)
            # We want WC premium = 60 → ev_wc = (200-54)/60 = 146/60 = 2.43 > 1.20
            c["long_ask"] = long_ask or 0.80
            c["long_bid"] = 0.70
            c["long_mid"] = 0.75
            c["short_bid"] = short_bid or 0.20
            c["short_ask"] = 0.30
            c["short_mid"] = 0.25
            # WC = 0.80 - 0.20 = 0.60 → premium_wc = 60
        return c

    def test_premium_used_wc_tag_present(self):
        """When leg quotes present with WC, PREMIUM_USED:WC reason code is emitted."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        candidate = self._make_passing_candidate(with_quotes=True)
        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1, "Expected one OPEN action"
        action = actions[0]
        rc_str = " ".join(action.reason_codes)
        assert "PREMIUM_USED:WC" in rc_str, (
            f"Expected PREMIUM_USED:WC in reason_codes, got: {action.reason_codes}"
        )

    def test_premium_used_campaign_tag_when_no_quotes(self):
        """When no leg quotes present, PREMIUM_USED:CAMPAIGN reason code is emitted."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        candidate = self._make_passing_candidate(with_quotes=False)
        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1, "Expected one OPEN action"
        action = actions[0]
        rc_str = " ".join(action.reason_codes)
        assert "PREMIUM_USED:CAMPAIGN" in rc_str, (
            f"Expected PREMIUM_USED:CAMPAIGN in reason_codes, got: {action.reason_codes}"
        )

    def test_pricing_dict_on_action(self):
        """action.pricing contains premium_used, premium_wc, debit fields."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        candidate = self._make_passing_candidate(with_quotes=True)
        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1
        p = actions[0].pricing
        assert p is not None, "action.pricing should not be None when quotes present"
        assert "premium_used" in p
        assert "premium_wc" in p
        assert "debit_wc_share" in p
        assert p["premium_used_source"] == "WC"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Ladder preference selects Layer A when inv=0 and both exist
# ─────────────────────────────────────────────────────────────────────────────

class TestLadderLayerPreference:

    LADDER_POLICY = {
        **BASE_POLICY,
        "thresholds": {
            "crash": {
                "fill_when_empty": {
                    "ev_per_dollar_implied": 1.20,
                    "ev_per_dollar_external": 0.30,
                    "convexity_multiple": 8.0,
                },
                "add_when_full": {
                    "ev_per_dollar_implied": 1.60,
                    "ev_per_dollar_external": 0.50,
                    "convexity_multiple": 15.0,
                },
                "ladder": {
                    "layer_a": {"moneyness_min_pct": 5.0, "moneyness_max_pct": 9.0},
                    "layer_b": {"moneyness_min_pct": 10.0, "moneyness_max_pct": 16.0},
                },
            },
            "selloff": BASE_POLICY["thresholds"]["selloff"],
        },
    }

    def test_layer_a_selected_over_layer_b_when_inv_empty(self):
        """
        When inv=0, Layer A is preferred even if Layer B has higher raw EV/$.
        Spot = 580, Layer A long_strike = 540 (6.9% OTM), Layer B long_strike = 510 (12.1% OTM).
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        spot = 580.0
        # Layer B: higher EV/$ but deeper OTM (12.1%)
        layer_b = make_candidate(
            regime="crash",
            ev_per_dollar=2.50,   # higher EV/$ than Layer A
            premium=30.0,
            max_gain=1400.0,
            p_used=0.05,
            spot=spot,
            long_strike=510.0,    # 12.1% OTM → Layer B
            candidate_id="SPY_B_510_crash",
        )
        # Layer A: lower EV/$ but moderate OTM (6.9%)
        layer_a = make_candidate(
            regime="crash",
            ev_per_dollar=1.80,   # lower EV/$ but Layer A
            premium=40.0,
            max_gain=1200.0,
            p_used=0.08,
            spot=spot,
            long_strike=540.0,    # 6.9% OTM → Layer A
            candidate_id="SPY_A_540_crash",
        )

        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0, crash_target=1)

        actions = generate_open_actions(
            candidates_data={"selected": [layer_b, layer_a]},
            policy=self.LADDER_POLICY,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1, f"Expected 1 OPEN, got {len(actions)}"
        action = actions[0]
        # Should select Layer A (SPY_A_540)
        assert action.layer == "A", f"Expected layer=A, got {action.layer}"
        assert "SPY_A_540" in (action.candidate_id or ""), (
            f"Expected Layer A candidate, got {action.candidate_id}"
        )

    def test_layer_b_selected_when_layer_a_absent(self):
        """When no Layer A candidates exist, Layer B is selected (unchanged behavior)."""
        from forecast_arb.allocator.open_plan import generate_open_actions

        spot = 580.0
        layer_b_only = make_candidate(
            regime="crash",
            ev_per_dollar=2.0,
            premium=30.0,
            max_gain=1400.0,
            p_used=0.06,
            spot=spot,
            long_strike=510.0,   # 12.1% OTM → Layer B
            candidate_id="SPY_B_510_crash",
        )

        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0)

        actions = generate_open_actions(
            candidates_data={"selected": [layer_b_only]},
            policy=self.LADDER_POLICY,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1
        assert actions[0].layer == "B"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Roll-close triggers and enables replacement open same run
# ─────────────────────────────────────────────────────────────────────────────

class TestRollCloseEnablesReplacementOpen:

    def _make_roll_position(
        self,
        trade_id="pos_crash_001",
        dte=18,
        mark_mid=15.0,
        entry_debit=60.0,
        regime="crash",
    ):
        from forecast_arb.allocator.types import SleevePosition
        # multiple = 15/60 = 0.25x < 1.10 → triggers ROLL_MULTIPLE
        return SleevePosition(
            trade_id=trade_id,
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],  # max_gain = 2000, convexity_now = 2000/15 = 133x OK
            qty_open=1,
            regime=regime,
            entry_debit=entry_debit,
            mark_mid=mark_mid,  # mark_mid / entry_debit = 0.25 < 1.10 → ROLL_MULTIPLE
            dte=dte,
        )

    def test_roll_close_triggers_for_low_multiple_within_dte(self):
        """Position with DTE<=21 and multiple < 1.10 generates ROLL_CLOSE."""
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions

        pos = self._make_roll_position(dte=18, mark_mid=15.0, entry_debit=60.0)
        # multiple = 15/60 = 0.25 < 1.10 → triggers ROLL_MULTIPLE

        actions = generate_roll_discipline_actions([pos], BASE_POLICY)

        assert len(actions) == 1
        action = actions[0]
        from forecast_arb.allocator.types import ActionType
        assert action.type == ActionType.ROLL_CLOSE
        assert any("ROLL_MULTIPLE" in rc for rc in action.reason_codes), action.reason_codes

    def test_roll_close_enables_open_same_run(self):
        """
        When ROLL_CLOSE frees a slot, a replacement OPEN is allowed in the same run
        even if the inventory was at target before the close.
        """
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions
        from forecast_arb.allocator.open_plan import generate_open_actions
        from forecast_arb.allocator.types import ActionType, InventoryState

        # Position is at target (crash=1/1)
        pos = self._make_roll_position(dte=18, mark_mid=15.0, entry_debit=60.0)

        # Simulate harvest step — roll close generated
        roll_actions = generate_roll_discipline_actions(
            [pos], BASE_POLICY, skip_trade_ids=set()
        )
        assert len(roll_actions) == 1
        assert roll_actions[0].type == ActionType.ROLL_CLOSE

        # Simulate inventory after ROLL_CLOSE (crash goes from 1→0)
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        inv_before = make_inventory(crash_open=1, crash_target=1)
        inv_after_close = _simulate_inventory_after_actions(inv_before, roll_actions, [pos])
        assert inv_after_close.crash_open == 0, "ROLL_CLOSE should free the crash slot"

        # Now open actions should proceed (slot is free)
        candidate = make_candidate(
            regime="crash",
            ev_per_dollar=1.80,
            premium=50.0,
            max_gain=1500.0,
            p_used=0.09,
        )
        budget = make_budget(daily=200.0)
        open_actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inv_after_close,
        )
        assert len(open_actions) == 1, (
            "Replacement OPEN should be allowed after ROLL_CLOSE frees slot"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Liquidity guard blocks close → no replacement open
# ─────────────────────────────────────────────────────────────────────────────

class TestLiquidityGuardBlocksCloseAndOpen:

    def test_wide_market_blocks_roll_and_prevents_replacement(self):
        """
        When spread is wider than max_width_pct (25%), ROLL_CLOSE → HOLD.
        That HOLD should NOT enable a replacement OPEN (slot not freed).
        """
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions
        from forecast_arb.allocator.open_plan import generate_open_actions
        from forecast_arb.allocator.types import ActionType, SleevePosition

        # Position with wide spread (would trigger roll discipline, but market is too wide)
        pos = SleevePosition(
            trade_id="pos_wide_001",
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],
            qty_open=1,
            regime="crash",
            entry_debit=60.0,
            mark_mid=15.0,        # multiple=0.25 < 1.10 → roll triggered
            dte=18,
            spread_bid=10.0,      # bid = $10/contract
            spread_ask=30.0,      # ask = $30/contract
            # width = 20, pct = 20/15 = 1.33 > 0.25 max_width_pct → BLOCKED
        )

        roll_actions = generate_roll_discipline_actions(
            [pos], BASE_POLICY, skip_trade_ids=set()
        )

        assert len(roll_actions) == 1
        hold_action = roll_actions[0]
        assert hold_action.type == ActionType.HOLD, (
            f"Expected HOLD (liquidity blocked), got {hold_action.type}"
        )
        assert "WIDE_MARKET_NO_CLOSE" in hold_action.reason_codes, hold_action.reason_codes

        # Inventory stays at 1/1 (HOLD didn't close anything)
        from forecast_arb.allocator.plan import _simulate_inventory_after_actions
        inv_before = make_inventory(crash_open=1, crash_target=1)
        inv_after = _simulate_inventory_after_actions(inv_before, roll_actions, [pos])
        assert inv_after.crash_open == 1, "HOLD should not free inventory slot"

        # Open actions should NOT proceed (still at target)
        candidate = make_candidate(
            regime="crash",
            ev_per_dollar=2.0,
            premium=50.0,
            max_gain=1500.0,
            p_used=0.09,
        )
        budget = make_budget(daily=200.0)
        open_actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inv_after,  # still 1/1
        )
        assert len(open_actions) == 0, (
            "No replacement OPEN when close was blocked by liquidity guard"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 & 7: Effective inventory blocks open when committed-not-filled exists
# ─────────────────────────────────────────────────────────────────────────────

class TestEffectiveInventoryGating:

    def _write_commit_ledger(self, tmpdir, intent_id="intent_001", regime="crash"):
        """Write a commit ledger with one OPEN row."""
        ledger_path = Path(tmpdir) / "allocator_commit_ledger.jsonl"
        row = {
            "date": "2026-03-05",
            "action": "OPEN",
            "mode": "paper",
            "intent_id": intent_id,
            "regime": regime,
            "premium_per_contract": 60.0,
            "timestamp_utc": "2026-03-05T10:00:00+00:00",
        }
        with open(ledger_path, "w") as f:
            f.write(json.dumps(row) + "\n")
        return ledger_path

    def _write_fills_ledger(self, tmpdir, intent_id=None):
        """Write empty fills ledger (or with POSITION_OPENED for intent_id)."""
        fills_path = Path(tmpdir) / "allocator_fills_ledger.jsonl"
        if intent_id:
            row = {
                "action": "POSITION_OPENED",
                "intent_id": intent_id,
                "date": "2026-03-05",
                "timestamp_utc": "2026-03-05T11:00:00+00:00",
            }
            with open(fills_path, "w") as f:
                f.write(json.dumps(row) + "\n")
        else:
            fills_path.touch()
        return fills_path

    def test_committed_not_filled_blocks_additional_open(self):
        """
        When a commit exists without a fill, effective inventory is at target
        and blocks additional OPEN.
        """
        from forecast_arb.allocator.inventory import compute_pending_from_ledgers

        with tempfile.TemporaryDirectory() as tmpdir:
            commit_path = self._write_commit_ledger(tmpdir, intent_id="i001", regime="crash")
            fills_path = self._write_fills_ledger(tmpdir, intent_id=None)  # no fill

            pending = compute_pending_from_ledgers(commit_path, fills_path)
            assert pending.get("crash", 0) == 1, (
                f"Expected 1 pending crash, got {pending}"
            )

    def test_after_fill_committed_not_filled_clears(self):
        """
        After POSITION_OPENED appears in fills ledger, committed-not-filled = 0.
        """
        from forecast_arb.allocator.inventory import compute_pending_from_ledgers

        with tempfile.TemporaryDirectory() as tmpdir:
            commit_path = self._write_commit_ledger(tmpdir, intent_id="i001", regime="crash")
            fills_path = self._write_fills_ledger(tmpdir, intent_id="i001")  # filled!

            pending = compute_pending_from_ledgers(commit_path, fills_path)
            assert pending.get("crash", 0) == 0, (
                f"Expected 0 pending crash after fill, got {pending}"
            )

    def test_effective_inventory_blocks_duplicate_open(self):
        """
        With committed_not_filled=1 and actual_filled=0, effective inventory=1
        which equals target(1), so no OPEN planned.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions
        from forecast_arb.allocator.types import InventoryState

        # Simulate: actual=0, pending=1 → effective crash=1 (at target)
        inventory_effective = make_inventory(
            crash_open=1,   # effective (actual=0 + pending=1)
            crash_target=1,
        )

        candidate = make_candidate(
            regime="crash",
            ev_per_dollar=2.0,
            premium=50.0,
            max_gain=1500.0,
            p_used=0.09,
        )
        budget = make_budget(daily=200.0)
        rejection_log = []

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inventory_effective,
            rejection_log=rejection_log,
        )

        # Inventory is at target → no OPEN
        assert len(actions) == 0, (
            "Expected HOLD when effective inventory is at target"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 & 9: Fragility gating
# ─────────────────────────────────────────────────────────────────────────────

class TestFragilityGating:

    FRAGILITY_POLICY = {
        **BASE_POLICY,
        "robustness": {
            "enabled": True,
            "p_downshift_pp": 3.0,
            "debit_upshift_pct": 10.0,
            "require_positive_ev_under_shocks": True,
            "allow_if_inventory_empty": True,
        },
    }

    NO_ALLOW_POLICY = {
        **BASE_POLICY,
        "robustness": {
            "enabled": True,
            "p_downshift_pp": 3.0,
            "debit_upshift_pct": 10.0,
            "require_positive_ev_under_shocks": True,
            "allow_if_inventory_empty": False,  # strict!
        },
    }

    def _make_fragile_candidate(self):
        """
        Candidate that passes EV gate but FAILS fragility check.

        p_used=0.05, max_gain=500, premium=50 → ev = (0.05*500 - 0.95*50)/50 = (25-47.5)/50 = -0.45
        EV/$ = -0.45 < 1.20, this fails EV already...

        Need a case that passes EV but fails fragility:
        p_used=0.20, max_gain=500, premium=50:
          ev = 0.20*500 - 0.80*50 = 100-40 = 60, ev/$ = 60/50 = 1.2 (just at threshold)
        Shock: p_shock = 0.17, prem_shock = 55
          ev_shock = 0.17*500 - 0.83*55 = 85 - 45.65 = 39.35 → ev_shock/$ > 0 (not fragile!)

        Let me try harder:
        p_used=0.10, max_gain=200, premium=50:
          ev = 0.10*200 - 0.90*50 = 20-45 = -25 → ev/$ = -25/50 = -0.5 < 1.2 → fails EV

        This is tricky. Let me make p high enough to pass EV but fragile under shocks:
        p_used=0.50, max_gain=200, premium=80:
          ev = 0.50*200 - 0.50*80 = 100-40 = 60 → ev/$ = 60/80 = 0.75 < 1.2 → fails EV

        Try: p_used=0.80, max_gain=200, premium=90:
          ev = 0.80*200 - 0.20*90 = 160-18 = 142 → ev/$ = 142/90 = 1.578 > 1.2 ✓
          Shock: p_shock=0.77, prem_shock=99
          ev_shock = 0.77*200 - 0.23*99 = 154 - 22.77 = 131.23 → positive still... not fragile

        OK, need a case where shocking p_down by 3pp flips EV_shock to negative:
        That requires p*max_gain ≈ (1-p)*premium (near-breakeven base case):
        p_used=0.09, max_gain=1200, premium=100:
          ev = 0.09*1200 - 0.91*100 = 108-91 = 17 → ev/$ = 17/100 = 0.17 < 1.20 → fails EV

        Hmm, I need to be more creative. The issue is that to pass EV threshold of 1.20,
        ev_mid/$ ≥ 1.20 → p*max_gain - (1-p)*prem ≥ 1.20*prem → p*max_gain ≥ prem*(2.20-p)
        
        And to fail fragility:
        ev_shock/$ ≤ 0 → p_shock*max_gain ≤ (1-p_shock)*prem_shock
        → (p-0.03)*max_gain ≤ (1-(p-0.03)) * (prem*1.10)
        
        Let me solve: set p=0.06 (just above fragile zone), max_gain=2000, prem=60:
        ev = 0.06*2000 - 0.94*60 = 120-56.4 = 63.6 → ev/$ = 63.6/60 = 1.06 < 1.20 → fails EV

        Try p=0.08, max_gain=2000, prem=60:
        ev = 0.08*2000 - 0.92*60 = 160-55.2 = 104.8 → ev/$ = 104.8/60 = 1.747 > 1.2 ✓
        Shock: p_shock=0.05, prem_shock=66
        ev_shock = 0.05*2000 - 0.95*66 = 100-62.7 = 37.3 → still positive!

        I need bigger shock to make EV_shock negative. Let me try prem closer to max_gain:
        p=0.80, max_gain=100, prem=40:
          ev = 0.80*100 - 0.20*40 = 80-8 = 72 → ev/$ = 72/40 = 1.8 > 1.2 ✓
          Shock: p_shock=0.77, prem_shock=44
          ev_shock = 0.77*100 - 0.23*44 = 77-10.12 = 66.88 → positive! Not fragile.

        The fragility check is really hard to trigger with normal parameters because
        the shock percentages are small (3pp + 10%). Let me think differently.

        Actually, a put spread with p=0.10, max_gain=200, premium=15:
          ev = 0.10*200 - 0.90*15 = 20-13.5 = 6.5 → ev/$ = 6.5/15 = 0.43 → fails EV

        It seems like for a spread to have EV/$ > 1.2 AND be fragile to a 3pp p-shift,
        we need max_gain to be large relative to premium (so convexity is very high),
        but then the p should be small. Let me try:
        p=0.06, max_gain=3000, prem=50:
          ev = 0.06*3000 - 0.94*50 = 180-47 = 133 → ev/$ = 133/50 = 2.66 > 1.2 ✓
          Shock: p_shock=0.03, prem_shock=55
          ev_shock = 0.03*3000 - 0.97*55 = 90-53.35 = 36.65 → positive! Not fragile.
          
        Even smaller p: p=0.04, max_gain=5000, prem=50:
          ev = 0.04*5000 - 0.96*50 = 200-48 = 152 → ev/$ = 152/50 = 3.04 > 1.2 ✓
          Shock: p_shock=0.01, prem_shock=55
          ev_shock = 0.01*5000 - 0.99*55 = 50-54.45 = -4.45 ≤ 0 → FRAGILE! ✓

        So we need very low p (4%) and very high convexity (5000/50 = 100x).
        The convexity threshold in the test policy is 8x... but wait the
        fill_when_empty threshold is 8x and 5000/50 = 100x which passes.

        Actually wait: convexity = max_gain / premium_used = 5000/50 = 100x > 8x ✓
        ev/$ = 3.04 > 1.2 ✓
        fragility: ev_shock/$ = -4.45/55 = -0.081 ≤ 0 → fragile ✓

        But the strike width would need to be huge: max_gain=5000 → (long-short)*100=5000 → 50 pts wide!
        That's unusual but let's use it for the test.
        """
        return make_candidate(
            regime="crash",
            ev_per_dollar=3.04,   # passes EV gate
            premium=50.0,         # campaign premium
            max_gain=5000.0,      # 50pt spread * 100 = 5000 max gain
            p_used=0.04,          # 4% probability
            # No leg quotes → premium_used = 50 (CAMPAIGN)
            candidate_id="fragile_candidate_001",
        )

    def test_fragility_blocks_when_inventory_has_position(self):
        """
        Fragile candidate (ev_shock <= 0) is blocked when inv > 0
        and allow_if_inventory_empty policy doesn't apply.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        candidate = self._make_fragile_candidate()
        budget = make_budget(daily=200.0)
        # inv=1/1 (has a position already, attempting to add when full)
        inventory = make_inventory(crash_open=1, crash_target=1)
        # This inventory is AT target, so needs_open = False → won't even evaluate
        # Try inv=0/2 to force evaluation while inv > 0
        inventory = make_inventory(crash_open=1, crash_target=2)  # needs one more

        # Use the NO_ALLOW policy (allow_if_inventory_empty=False)
        rejection_log = []
        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=self.NO_ALLOW_POLICY,
            budget=budget,
            inventory=inventory,
            rejection_log=rejection_log,
        )

        assert len(actions) == 0, (
            f"Expected HOLD when fragile and inv>0, got {len(actions)} opens. "
            f"Log: {rejection_log}"
        )
        if rejection_log:
            assert "EV_FRAGILE_UNDER_SHOCKS" in (rejection_log[0].get("reason", "") or ""), (
                f"Expected EV_FRAGILE_UNDER_SHOCKS reason, got: {rejection_log[0].get('reason')}"
            )

    def test_fragility_allows_when_inventory_empty_and_configured(self):
        """
        Fragile candidate is ALLOWED when inv=0 and allow_if_inventory_empty=True.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        candidate = self._make_fragile_candidate()
        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0, crash_target=1)  # inv = 0

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=self.FRAGILITY_POLICY,  # allow_if_inventory_empty=True
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1, (
            f"Expected ALLOW when fragile but inv=0 and allow_if_inventory_empty=True"
        )
        action = actions[0]
        assert action.fragile is True, "action.fragile should be True"
        assert any("FRAGILE_ALLOWED_EMPTY" in rc for rc in action.reason_codes), (
            f"Expected FRAGILE_ALLOWED_EMPTY reason code, got: {action.reason_codes}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: allocator_actions.json contains pricing + fragility + layer fields
# ─────────────────────────────────────────────────────────────────────────────

class TestAllocatorActionsJsonContainsNewFields:

    def test_open_action_to_dict_contains_all_v19_fields(self):
        """
        When an OPEN action is serialized via to_dict(), it carries:
        - pricing (with premium_used, premium_wc, debit_wc_share)
        - layer (A/B/None)
        - fragile (True/False/None)
        """
        from forecast_arb.allocator.types import AllocatorAction, ActionType

        action = AllocatorAction(
            type=ActionType.OPEN,
            candidate_id="test_candidate",
            regime="crash",
            qty=1,
            premium=65.0,
            reason_codes=["PREMIUM_USED:WC", "LADDER_LAYER:A"],
            pricing={
                "premium_used": 65.0,
                "premium_used_source": "WC",
                "premium_wc": 65.0,
                "premium_mid": 60.0,
                "debit_wc_share": 0.65,
                "debit_mid_share": 0.60,
            },
            layer="A",
            fragile=False,
        )

        d = action.to_dict()
        assert "pricing" in d, f"pricing missing from to_dict: {d.keys()}"
        assert "layer" in d, f"layer missing from to_dict: {d.keys()}"
        assert "fragile" in d, f"fragile missing from to_dict: {d.keys()}"
        assert d["pricing"]["premium_used_source"] == "WC"
        assert d["layer"] == "A"
        assert d["fragile"] is False

    def test_actions_json_via_generate_open_actions_has_pricing(self):
        """
        A full generate_open_actions call produces actions with non-None pricing dict.
        """
        from forecast_arb.allocator.open_plan import generate_open_actions

        candidate = make_candidate(
            regime="crash",
            ev_per_dollar=2.0,
            premium=55.0,
            max_gain=1500.0,
            p_used=0.09,
            long_ask=0.70,
            long_mid=0.65,
            long_bid=0.60,
            short_bid=0.15,
            short_mid=0.18,
            short_ask=0.20,
            # WC = 0.70 - 0.15 = 0.55 → premium_wc = 55
        )

        budget = make_budget(daily=200.0)
        inventory = make_inventory(crash_open=0)

        actions = generate_open_actions(
            candidates_data={"selected": [candidate]},
            policy=BASE_POLICY,
            budget=budget,
            inventory=inventory,
        )

        assert len(actions) == 1
        d = actions[0].to_dict()
        assert "pricing" in d
        assert d["pricing"]["premium_used_source"] in ("WC", "MID", "CAMPAIGN")


# ─────────────────────────────────────────────────────────────────────────────
# Additional unit tests: pricing.py
# ─────────────────────────────────────────────────────────────────────────────

class TestPricingUtilities:

    def test_compute_debit_mid(self):
        from forecast_arb.allocator.pricing import compute_debit_mid
        assert compute_debit_mid(0.75, 0.25) == pytest.approx(0.50)
        assert compute_debit_mid(None, 0.25) is None
        assert compute_debit_mid(0.75, None) is None
        # Negative result → clamped to 0
        assert compute_debit_mid(0.20, 0.30) == pytest.approx(0.0)

    def test_compute_debit_worstcase(self):
        from forecast_arb.allocator.pricing import compute_debit_worstcase
        # long_ask=0.80, short_bid=0.20 → WC = 0.60
        assert compute_debit_worstcase(0.80, 0.20) == pytest.approx(0.60)
        assert compute_debit_worstcase(None, 0.20) is None
        assert compute_debit_worstcase(0.80, None) is None

    def test_compute_premium_per_contract(self):
        from forecast_arb.allocator.pricing import compute_premium_per_contract
        assert compute_premium_per_contract(0.60) == pytest.approx(60.0)
        assert compute_premium_per_contract(0.0) == pytest.approx(0.0)

    def test_compute_pricing_detail_with_quotes(self):
        from forecast_arb.allocator.pricing import compute_pricing_detail
        candidate = {
            "long_ask": 0.80, "long_bid": 0.70, "long_mid": 0.75,
            "short_ask": 0.30, "short_bid": 0.20, "short_mid": 0.25,
        }
        pd = compute_pricing_detail(candidate)
        assert pd["premium_wc"] == pytest.approx(60.0)  # (0.80-0.20)*100
        assert pd["premium_mid"] == pytest.approx(50.0)  # (0.75-0.25)*100
        assert pd["has_quotes"] is True

    def test_compute_pricing_detail_no_quotes(self):
        from forecast_arb.allocator.pricing import compute_pricing_detail
        candidate = {}
        pd = compute_pricing_detail(candidate)
        assert pd["premium_wc"] is None
        assert pd["premium_mid"] is None
        assert pd["has_quotes"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Additional: policy.py helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestPolicyHelpers:

    def test_get_robustness_params_defaults(self):
        from forecast_arb.allocator.policy import get_robustness_params
        params = get_robustness_params({})
        assert params["enabled"] is False
        assert params["p_downshift_pp"] == 3.0
        assert params["allow_if_inventory_empty"] is True

    def test_get_roll_params_defaults(self):
        from forecast_arb.allocator.policy import get_roll_params
        params = get_roll_params({})
        assert params["enabled"] is False
        assert params["dte_max_for_roll"] == 21
        assert params["min_multiple_to_hold"] == pytest.approx(1.10)

    def test_get_ladder_params_absent(self):
        from forecast_arb.allocator.policy import get_ladder_params
        result = get_ladder_params({})
        assert result is None

    def test_get_ladder_params_present(self):
        from forecast_arb.allocator.policy import get_ladder_params
        policy = {
            "thresholds": {
                "crash": {
                    "ladder": {
                        "layer_a": {"moneyness_min_pct": 5.0, "moneyness_max_pct": 9.0},
                        "layer_b": {"moneyness_min_pct": 10.0, "moneyness_max_pct": 16.0},
                    }
                }
            }
        }
        result = get_ladder_params(policy, "crash")
        assert result is not None
        assert result["layer_a"]["moneyness_min_pct"] == 5.0
        assert result["layer_b"]["moneyness_max_pct"] == 16.0


# ─────────────────────────────────────────────────────────────────────────────
# Additional: roll discipline edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestRollDisciplineEdgeCases:

    def test_roll_not_triggered_within_dte_but_above_multiple(self):
        """Position with DTE<=21 but multiple >= min_multiple_to_hold should NOT roll."""
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions
        from forecast_arb.allocator.types import SleevePosition

        pos = SleevePosition(
            trade_id="pos_good_001",
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],
            qty_open=1,
            regime="crash",
            entry_debit=50.0,
            mark_mid=60.0,   # multiple = 60/50 = 1.2 >= 1.10 ✓
            dte=15,
        )
        # convexity_now = 2000/60 = 33.3x >= 8.0 ✓
        # So no roll should be triggered
        actions = generate_roll_discipline_actions([pos], BASE_POLICY)
        assert len(actions) == 0, f"Expected no roll, got: {actions}"

    def test_roll_not_triggered_outside_dte_window(self):
        """Position with DTE > dte_max_for_roll (21) should NOT roll."""
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions
        from forecast_arb.allocator.types import SleevePosition

        pos = SleevePosition(
            trade_id="pos_dte_ok_001",
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],
            qty_open=1,
            regime="crash",
            entry_debit=60.0,
            mark_mid=10.0,  # multiple = 0.17 < 1.10 (would trigger if in window)
            dte=30,  # Outside the 21-day window
        )
        actions = generate_roll_discipline_actions([pos], BASE_POLICY)
        assert len(actions) == 0, f"Expected no roll outside DTE window, got: {actions}"

    def test_roll_triggered_by_convexity_failure(self):
        """Position with LOW convexity (high mark relative to max gain) should trigger roll."""
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions
        from forecast_arb.allocator.types import SleevePosition, ActionType

        pos = SleevePosition(
            trade_id="pos_low_conv_001",
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],   # max_gain = 2000
            qty_open=1,
            regime="crash",
            entry_debit=50.0,
            mark_mid=400.0,  # multiple = 400/50 = 8x > 1.10 ✓ (won't trigger ROLL_MULTIPLE)
                             # convexity_now = 2000/400 = 5.0x < 8.0 → triggers ROLL_CONVEXITY
            dte=15,
        )
        actions = generate_roll_discipline_actions([pos], BASE_POLICY)
        assert len(actions) == 1
        action = actions[0]
        assert action.type == ActionType.ROLL_CLOSE
        assert any("ROLL_CONVEXITY" in rc for rc in action.reason_codes), action.reason_codes

    def test_roll_disabled_when_policy_says_false(self):
        """Roll discipline is skipped when policy.roll.enabled = False."""
        from forecast_arb.allocator.harvest import generate_roll_discipline_actions
        from forecast_arb.allocator.types import SleevePosition

        disabled_policy = {**BASE_POLICY, "roll": {"enabled": False}}
        pos = SleevePosition(
            trade_id="pos_001",
            underlier="SPY",
            expiry="20260402",
            strikes=[580.0, 560.0],
            qty_open=1,
            regime="crash",
            entry_debit=60.0,
            mark_mid=10.0,  # would trigger if enabled
            dte=15,
        )
        actions = generate_roll_discipline_actions([pos], disabled_policy)
        assert len(actions) == 0, "Roll should be disabled when policy.roll.enabled=False"
