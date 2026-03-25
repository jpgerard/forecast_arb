"""
CCC v1.8 — Pending Exposure Tests

All 6 acceptance scenarios from the v1.8 patch spec.  All tests are
deterministic, no IBKR dependency, no live network calls.

Scenarios:
  1. pending is computed from commit ledger minus filled set (not filesystem timestamps)
  2. staged order row does NOT create positions.json entries
  3. filled order row DOES create positions.json entry and removes from pending
  4. gating uses actual+pending (committed-not-filled crash blocks second crash open)
  5. console summary includes ACTUAL/PENDING/EFFECTIVE and matches JSON
  6. daily.py one-liner is idempotent: second run produces committed_new=0
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FIXED_DATE = "2026-03-05"
FIXED_TS = "2026-03-05T14:30:00+00:00"
INTENT_ID_CRASH = "aaa111bbb222ccc333ddd444eee555fff666aabb"
INTENT_ID_SELLOFF = "fff666eee555ddd444ccc333bbb222aaa111ccdd"


def _make_commit_row(intent_id: str, regime: str, date: str = FIXED_DATE) -> Dict:
    """Build a minimal commit ledger row."""
    return {
        "date": date,
        "timestamp_utc": FIXED_TS,
        "action": "OPEN",
        "policy_id": "ccc_v1",
        "intent_id": intent_id,
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20260417",
        "strikes": [575.0, 555.0],
        "qty": 1,
        "premium_per_contract": 36.0,
        "premium_spent": 36.0,
        "mode": "paper",
    }


def _make_position_opened_row(intent_id: str, regime: str) -> Dict:
    """Build a minimal POSITION_OPENED fills ledger row."""
    return {
        "date": FIXED_DATE,
        "timestamp_utc": FIXED_TS,
        "action": "POSITION_OPENED",
        "policy_id": "ccc_v1",
        "mode": "paper",
        "intent_id": intent_id,
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20260417",
        "strikes": [575.0, 555.0],
        "qty": 1,
        "entry_debit_gross": 55.0,
        "source": "execution_result",
    }


def _make_order_staged_row(intent_id: str, regime: str) -> Dict:
    """Build a minimal ORDER_STAGED fills ledger row."""
    return {
        "date": FIXED_DATE,
        "timestamp_utc": FIXED_TS,
        "action": "ORDER_STAGED",
        "policy_id": "ccc_v1",
        "mode": "paper",
        "intent_id": intent_id,
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20260417",
        "strikes": [575.0, 555.0],
        "qty": 1,
        "source": "execution_result",
        "staged_note": "STAGED_PAPER",
    }


def _write_jsonl(path: Path, rows: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Scenario 1: pending = commit − filled (not filesystem timestamps)
# ---------------------------------------------------------------------------

class TestPendingFromLedgersNotTimestamps:
    """
    Scenario 1: pending_intent_ids = commit_ledger − fills_ledger(POSITION_OPENED).
    The result is INDEPENDENT of filesystem modification times, OPEN_*.json presence,
    or 'modified today' file scans.
    """

    def test_committed_but_not_filled_is_pending(self, tmp_path):
        """An intent in commit ledger but NOT in fills ledger is pending."""
        from forecast_arb.allocator.pending import compute_pending_intent_ids

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"

        # Write commit row but NO fills row
        _write_jsonl(commit_ledger, [_make_commit_row(INTENT_ID_CRASH, "crash")])
        # fills_ledger is empty (file absent)

        pending = compute_pending_intent_ids(commit_ledger, fills_ledger)

        assert INTENT_ID_CRASH in pending, "Committed-not-filled must be pending"

    def test_committed_and_filled_is_NOT_pending(self, tmp_path):
        """An intent with POSITION_OPENED in fills ledger is NOT pending."""
        from forecast_arb.allocator.pending import compute_pending_intent_ids

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"

        _write_jsonl(commit_ledger, [_make_commit_row(INTENT_ID_CRASH, "crash")])
        _write_jsonl(fills_ledger, [_make_position_opened_row(INTENT_ID_CRASH, "crash")])

        pending = compute_pending_intent_ids(commit_ledger, fills_ledger)

        assert INTENT_ID_CRASH not in pending, "POSITION_OPENED should remove from pending"

    def test_order_staged_does_NOT_remove_from_pending(self, tmp_path):
        """ORDER_STAGED in fills ledger does NOT remove intent from pending set."""
        from forecast_arb.allocator.pending import compute_pending_intent_ids

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"

        _write_jsonl(commit_ledger, [_make_commit_row(INTENT_ID_CRASH, "crash")])
        _write_jsonl(fills_ledger, [_make_order_staged_row(INTENT_ID_CRASH, "crash")])

        pending = compute_pending_intent_ids(commit_ledger, fills_ledger)

        # ORDER_STAGED does NOT fulfil the commitment — still pending
        assert INTENT_ID_CRASH in pending, (
            "ORDER_STAGED must NOT remove from pending; only POSITION_OPENED does"
        )

    def test_pending_counts_by_regime_correct(self, tmp_path):
        """pending_counts_by_regime returns correct {regime: count} from ledger."""
        from forecast_arb.allocator.pending import (
            load_commit_ledger_rows,
            compute_pending_intent_ids,
            pending_counts_by_regime,
        )

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"

        _write_jsonl(commit_ledger, [
            _make_commit_row(INTENT_ID_CRASH, "crash"),
            _make_commit_row(INTENT_ID_SELLOFF, "selloff"),
        ])
        # Only crash is filled
        _write_jsonl(fills_ledger, [
            _make_position_opened_row(INTENT_ID_CRASH, "crash"),
        ])

        rows = load_commit_ledger_rows(commit_ledger)
        pending_ids = compute_pending_intent_ids(commit_ledger, fills_ledger)
        counts = pending_counts_by_regime(rows, pending_ids)

        assert counts.get("crash", 0) == 0, "Crash is filled, not pending"
        assert counts.get("selloff", 0) == 1, "Selloff is not filled, still pending"

    def test_no_openstar_json_needed(self, tmp_path):
        """Pending detection requires NO OPEN_*.json files — only ledger files."""
        from forecast_arb.allocator.pending import compute_pending_intent_ids

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"

        # No OPEN_*.json files anywhere — only the commit ledger
        _write_jsonl(commit_ledger, [_make_commit_row(INTENT_ID_CRASH, "crash")])

        # Must work without scanning any intents/ directory
        pending = compute_pending_intent_ids(commit_ledger, fills_ledger)

        assert INTENT_ID_CRASH in pending, "Should detect pending from ledgers alone"


# ---------------------------------------------------------------------------
# Scenario 2: staged order does NOT create positions.json
# ---------------------------------------------------------------------------

class TestStagedOrderNoPosition:
    """
    Scenario 2: When exec_result has transmit=False (STAGED_PAPER), reconcile
    writes ORDER_STAGED to fills ledger but does NOT create/update positions.json.
    """

    def _make_staged_exec_result(self, intent_path: str) -> Dict:
        return {
            "success": True,
            "order_id": 99,
            "status": "STAGED_PAPER",
            "transmit": False,
            "intent_path": intent_path,
            "symbol": "SPY",
            "expiry": "20260417",
            "qty": 1,
            "market_debit": 0.55,
            "leg_quotes": [
                {"action": "BUY", "strike": 575.0, "quotes": {"ask": 2.09, "bid": 2.07}},
                {"action": "SELL", "strike": 555.0, "quotes": {"ask": 1.56, "bid": 1.54}},
            ],
            "timestamp_utc": FIXED_TS,
        }

    def _make_intent(self, intent_id: str) -> Dict:
        return {
            "strategy": "ccc_v1",
            "symbol": "SPY",
            "expiry": "20260417",
            "type": "VERTICAL_PUT_DEBIT",
            "legs": [
                {"action": "BUY", "right": "P", "strike": 575.0, "ratio": 1},
                {"action": "SELL", "right": "P", "strike": 555.0, "ratio": 1},
            ],
            "qty": 1,
            "limit": {"start": 0.69, "max": 0.704},
            "tif": "DAY",
            "guards": {"max_debit": 0.704, "max_spread_width": 0.2, "min_dte": 7},
            "regime": "crash",
            "candidate_id": "cand_abc",
            "intent_id": intent_id,
        }

    def test_staged_order_writes_order_staged_row(self, tmp_path):
        """transmit=False → ORDER_STAGED row in fills ledger."""
        from forecast_arb.allocator.fills import ingest_from_execution_result, read_fills_ledger

        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        intent = self._make_intent(INTENT_ID_CRASH)
        exec_result = self._make_staged_exec_result("intents/allocator/OPEN_test.json")

        result = ingest_from_execution_result(
            exec_result=exec_result,
            intent=intent,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            mode="paper",
            date_str=FIXED_DATE,
        )

        assert result["staged_only"] is True
        assert result["orders_staged"] == 1
        assert result["positions_opened"] == 0

        rows = read_fills_ledger(fills_ledger)
        assert len(rows) == 1
        assert rows[0]["action"] == "ORDER_STAGED"

    def test_staged_order_does_not_create_positions_json(self, tmp_path):
        """ORDER_STAGED must NOT create or update positions.json."""
        from forecast_arb.allocator.fills import ingest_from_execution_result

        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        intent = self._make_intent(INTENT_ID_CRASH)
        exec_result = self._make_staged_exec_result("intents/allocator/OPEN_test.json")

        ingest_from_execution_result(
            exec_result=exec_result,
            intent=intent,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            mode="paper",
            date_str=FIXED_DATE,
        )

        assert not positions_path.exists(), (
            "positions.json must NOT be created for staged-only orders"
        )

    def test_staged_order_row_not_in_filled_set(self, tmp_path):
        """ORDER_STAGED row does NOT appear in load_filled_intent_ids output."""
        from forecast_arb.allocator.fills import ingest_from_execution_result
        from forecast_arb.allocator.pending import load_filled_intent_ids

        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        intent = self._make_intent(INTENT_ID_CRASH)
        exec_result = self._make_staged_exec_result("intents/allocator/OPEN_test.json")

        ingest_from_execution_result(
            exec_result=exec_result,
            intent=intent,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            mode="paper",
            date_str=FIXED_DATE,
        )

        filled_ids = load_filled_intent_ids(fills_ledger)
        assert INTENT_ID_CRASH not in filled_ids, (
            "ORDER_STAGED must NOT appear in filled_intent_ids; "
            "only POSITION_OPENED rows count as 'filled'"
        )


# ---------------------------------------------------------------------------
# Scenario 3: filled order creates positions.json and removes from pending
# ---------------------------------------------------------------------------

class TestFilledOrderCreatesPosition:
    """
    Scenario 3: A POSITION_OPENED fill creates positions.json entry and
    removes the intent_id from the pending set.
    """

    def _make_filled_exec_result(self, intent_path: str) -> Dict:
        return {
            "success": True,
            "order_id": 101,
            "status": "Filled",
            # No transmit=False → treated as real fill
            "intent_path": intent_path,
            "symbol": "SPY",
            "expiry": "20260417",
            "qty": 1,
            "market_debit": 0.55,
            "leg_quotes": [
                {"action": "BUY", "strike": 575.0, "quotes": {"ask": 2.09, "bid": 2.07}},
                {"action": "SELL", "strike": 555.0, "quotes": {"ask": 1.56, "bid": 1.54}},
            ],
            "timestamp_utc": FIXED_TS,
        }

    def _make_intent(self, intent_id: str) -> Dict:
        return {
            "strategy": "ccc_v1",
            "symbol": "SPY",
            "expiry": "20260417",
            "type": "VERTICAL_PUT_DEBIT",
            "legs": [
                {"action": "BUY", "right": "P", "strike": 575.0, "ratio": 1},
                {"action": "SELL", "right": "P", "strike": 555.0, "ratio": 1},
            ],
            "qty": 1,
            "limit": {"start": 0.69, "max": 0.704},
            "tif": "DAY",
            "guards": {"max_debit": 0.704, "max_spread_width": 0.2, "min_dte": 7},
            "regime": "crash",
            "candidate_id": "cand_abc",
            "intent_id": intent_id,
        }

    def test_filled_order_creates_positions_json(self, tmp_path):
        """POSITION_OPENED → positions.json is created with 1 entry."""
        from forecast_arb.allocator.fills import (
            ingest_from_execution_result,
            read_positions_snapshot,
        )

        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        intent = self._make_intent(INTENT_ID_CRASH)
        exec_result = self._make_filled_exec_result("intents/allocator/OPEN_test.json")

        result = ingest_from_execution_result(
            exec_result=exec_result,
            intent=intent,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            mode="paper",
            date_str=FIXED_DATE,
        )

        assert result["positions_opened"] == 1
        assert result["staged_only"] is False
        assert positions_path.exists()

        positions = read_positions_snapshot(positions_path)
        assert len(positions) == 1
        assert positions[0]["regime"] == "crash"
        assert positions[0]["position_id"] == INTENT_ID_CRASH

    def test_filled_order_removes_from_pending(self, tmp_path):
        """After POSITION_OPENED, the intent is no longer in pending set."""
        from forecast_arb.allocator.fills import ingest_from_execution_result
        from forecast_arb.allocator.pending import compute_pending_intent_ids

        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        commit_ledger = tmp_path / "commit.jsonl"

        # Intent is committed first
        _write_jsonl(commit_ledger, [_make_commit_row(INTENT_ID_CRASH, "crash")])

        # Before fill: should be pending
        pending_before = compute_pending_intent_ids(commit_ledger, fills_ledger)
        assert INTENT_ID_CRASH in pending_before

        # Reconcile as filled
        intent = self._make_intent(INTENT_ID_CRASH)
        exec_result = self._make_filled_exec_result("intents/allocator/OPEN_test.json")
        ingest_from_execution_result(
            exec_result=exec_result,
            intent=intent,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            mode="paper",
            date_str=FIXED_DATE,
        )

        # After fill: should NOT be pending
        pending_after = compute_pending_intent_ids(commit_ledger, fills_ledger)
        assert INTENT_ID_CRASH not in pending_after, (
            "POSITION_OPENED must remove intent from pending set"
        )


# ---------------------------------------------------------------------------
# Scenario 4: gating uses actual + pending
# ---------------------------------------------------------------------------

class TestGatingUsesActualPlusPending:
    """
    Scenario 4: inventory.effective = actual + pending.
    A committed-not-filled crash intent blocks a second crash OPEN.
    """

    def _minimal_policy(self, crash_target: int = 1, selloff_target: int = 1) -> Dict:
        return {"inventory_targets": {"crash": crash_target, "selloff": selloff_target}}

    def test_effective_inventory_blocks_second_open(self, tmp_path):
        """With 1 committed-but-unfilled crash intent, effective crash = 0+1 = 1 = target.

        So needs_open("crash") returns False on inv_effective.
        """
        from forecast_arb.allocator.inventory import compute_inventory_state_full

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"  # doesn't exist

        # 1 committed crash intent, not filled
        _write_jsonl(commit_ledger, [_make_commit_row(INTENT_ID_CRASH, "crash")])

        policy = self._minimal_policy(crash_target=1)
        inv_actual, pending, inv_effective = compute_inventory_state_full(
            policy=policy,
            ledger_path=tmp_path / "empty_plan.jsonl",  # empty
            commit_ledger_path=commit_ledger,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
        )

        # Actual has no positions
        assert inv_actual.crash_open == 0
        # Pending has 1 crash
        assert pending.get("crash", 0) == 1
        # Effective = actual + pending = 0 + 1 = 1 = target
        assert inv_effective.crash_open == 1
        # Gate should block
        assert not inv_effective.needs_open("crash"), (
            "With pending=1 and target=1, effective is at target; should NOT open another"
        )

    def test_actual_plus_pending_equals_target_blocks_open(self, tmp_path):
        """actual=1 filled crash + pending=0 → target met, needs_open=False."""
        from forecast_arb.allocator.fills import (
            build_positions_snapshot,
            write_positions_snapshot,
        )
        from forecast_arb.allocator.inventory import compute_inventory_state_full

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"

        # Actual: 1 open crash position
        positions = build_positions_snapshot([_make_position_opened_row(INTENT_ID_CRASH, "crash")])
        write_positions_snapshot(positions_path, positions)

        # No pending
        policy = self._minimal_policy(crash_target=1)
        inv_actual, pending, inv_effective = compute_inventory_state_full(
            policy=policy,
            ledger_path=tmp_path / "empty.jsonl",
            commit_ledger_path=commit_ledger,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
        )

        assert inv_actual.crash_open == 1
        assert pending.get("crash", 0) == 0
        assert inv_effective.crash_open == 1
        assert not inv_effective.needs_open("crash")

    def test_pending_from_commit_minus_fills_not_scan(self, tmp_path):
        """Pending uses ledger subtraction, not OPEN_*.json scan."""
        from forecast_arb.allocator.pending import load_pending_counts

        commit_ledger = tmp_path / "commit.jsonl"
        fills_ledger = tmp_path / "fills.jsonl"

        # Committed crash + selloff; only crash is filled
        _write_jsonl(commit_ledger, [
            _make_commit_row(INTENT_ID_CRASH, "crash"),
            _make_commit_row(INTENT_ID_SELLOFF, "selloff"),
        ])
        _write_jsonl(fills_ledger, [
            _make_position_opened_row(INTENT_ID_CRASH, "crash"),
        ])

        counts = load_pending_counts(commit_ledger, fills_ledger)

        # crash is filled → pending=0
        assert counts["crash"] == 0
        # selloff not filled → pending=1
        assert counts["selloff"] == 1


# ---------------------------------------------------------------------------
# Scenario 5: console summary includes ACTUAL/PENDING/EFFECTIVE + matches JSON
# ---------------------------------------------------------------------------

class TestConsoleSummaryAndJSON:
    """
    Scenario 5: The allocator_actions.json inventory block includes 'pending' and
    'effective' keys.  The console output includes ACTUAL/PENDING/EFFECTIVE lines.
    """

    def _make_plan_with_pending(
        self,
        pending_crash: int = 1,
        pending_selloff: int = 0,
        actual_crash: int = 0,
        actual_selloff: int = 0,
    ):
        from forecast_arb.allocator.types import (
            AllocatorPlan,
            AllocatorAction,
            ActionType,
            InventoryState,
            BudgetState,
        )

        inv_actual = InventoryState(
            crash_target=1,
            crash_open=actual_crash,
            selloff_target=1,
            selloff_open=actual_selloff,
        )
        inv_effective = InventoryState(
            crash_target=1,
            crash_open=actual_crash + pending_crash,
            selloff_target=1,
            selloff_open=actual_selloff + pending_selloff,
        )
        budget = BudgetState(
            monthly_baseline=500.0,
            monthly_max=600.0,
            weekly_baseline=200.0,
            daily_baseline=100.0,
            weekly_kicker=300.0,
            daily_kicker=150.0,
        )
        action = AllocatorAction(
            type=ActionType.HOLD,
            reason_codes=["INVENTORY_AT_TARGET"],
        )
        return AllocatorPlan(
            timestamp_utc=FIXED_TS,
            policy_id="ccc_v1",
            budgets=budget,
            inventory=inv_actual,
            inventory_after=inv_actual,
            positions=[],
            actions=[action],
            pending_open_intents={"crash": pending_crash, "selloff": pending_selloff},
            inv_effective=inv_effective,
        )

    def test_json_inventory_has_pending_key(self):
        """allocator_actions.json inventory block has 'pending' key."""
        plan = self._make_plan_with_pending(pending_crash=1)
        d = plan.to_dict()
        assert "pending" in d["inventory"], "inventory.pending missing from JSON"
        assert d["inventory"]["pending"]["crash"] == 1

    def test_json_inventory_has_effective_key(self):
        """allocator_actions.json inventory block has 'effective' key."""
        plan = self._make_plan_with_pending(pending_crash=1, actual_crash=0)
        d = plan.to_dict()
        assert "effective" in d["inventory"], "inventory.effective missing from JSON"
        eff = d["inventory"]["effective"]
        assert eff["crash"]["open"] == 1, (
            f"effective crash.open should be actual(0) + pending(1) = 1, got {eff['crash']['open']}"
        )

    def test_json_effective_equals_actual_plus_pending(self):
        """effective.crash.open = actual.crash.open + pending.crash (in JSON)."""
        plan = self._make_plan_with_pending(actual_crash=1, pending_crash=0)
        d = plan.to_dict()
        actual_crash = d["inventory"]["actual"]["crash"]["open"]
        pending_crash = d["inventory"]["pending"]["crash"]
        effective_crash = d["inventory"]["effective"]["crash"]["open"]
        assert effective_crash == actual_crash + pending_crash

    def test_console_contains_pending_line_when_nonzero(self, capsys):
        """When pending > 0, console prints PENDING (committed-not-filled) line."""
        from forecast_arb.allocator.plan import _print_pm_summary
        plan = self._make_plan_with_pending(pending_crash=1)
        _print_pm_summary(plan)
        captured = capsys.readouterr()
        assert "PENDING" in captured.out, (
            "Console should show PENDING when pending_open_intents > 0"
        )

    def test_console_contains_effective_line_when_pending_nonzero(self, capsys):
        """When pending > 0, console also prints EFFECTIVE line."""
        from forecast_arb.allocator.plan import _print_pm_summary
        plan = self._make_plan_with_pending(pending_crash=1)
        _print_pm_summary(plan)
        captured = capsys.readouterr()
        assert "EFFECTIVE" in captured.out, (
            "Console should show EFFECTIVE inventory when pending > 0"
        )

    def test_console_no_pending_line_when_all_filled(self, capsys):
        """When pending = 0, no PENDING line is shown."""
        from forecast_arb.allocator.plan import _print_pm_summary
        plan = self._make_plan_with_pending(pending_crash=0, pending_selloff=0)
        _print_pm_summary(plan)
        captured = capsys.readouterr()
        # Should show ACTUAL but no PENDING line when pending=0
        assert "INVENTORY ACTUAL" in captured.out
        assert "PENDING" not in captured.out


# ---------------------------------------------------------------------------
# Scenario 6: daily.py one-liner idempotent (second run committed_new=0)
# ---------------------------------------------------------------------------

class TestOneLinerIdempotent:
    """
    Scenario 6: Running ccc_execute twice with the same allocator_actions.json
    produces committed_new=0 on the second run (dedup by intent_id).
    """

    def _make_minimal_actions_json(self, tmp_path: Path, intent_id: str) -> tuple[Path, Path]:
        """
        Create a minimal allocator_actions.json with 1 OPEN action and
        the corresponding OPEN_*.json intent file.

        Returns (actions_path, intent_path).
        """
        intents_dir = tmp_path / "intents" / "allocator"
        intents_dir.mkdir(parents=True, exist_ok=True)

        intent = {
            "strategy": "ccc_v1",
            "symbol": "SPY",
            "expiry": "20260417",
            "timestamp_utc": FIXED_TS,
            "type": "VERTICAL_PUT_DEBIT",
            "legs": [
                {"action": "BUY", "right": "P", "strike": 575.0, "ratio": 1,
                 "exchange": "SMART", "currency": "USD"},
                {"action": "SELL", "right": "P", "strike": 555.0, "ratio": 1,
                 "exchange": "SMART", "currency": "USD"},
            ],
            "qty": 1,
            "limit": {"start": 0.36, "max": 0.3672},
            "tif": "DAY",
            "guards": {"max_debit": 0.3672, "max_spread_width": 0.2, "min_dte": 7},
            "regime": "crash",
            "candidate_id": "test_cand_001",
            "run_id": None,
            "cluster_id": None,
            "intent_id": intent_id,
        }

        intent_path = intents_dir / f"OPEN_test_cand_001.json"
        with open(intent_path, "w") as f:
            json.dump(intent, f)

        actions = {
            "timestamp_utc": FIXED_TS,
            "policy_id": "ccc_v1",
            "actions": [
                {
                    "type": "OPEN",
                    "candidate_id": "test_cand_001",
                    "intent_path": str(intent_path),
                    "qty": 1,
                    "premium": 36.0,
                    "reason_codes": ["EV_PER_DOLLAR:1.5"],
                }
            ],
        }

        actions_path = tmp_path / "runs" / "allocator" / "allocator_actions.json"
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        with open(actions_path, "w") as f:
            json.dump(actions, f)

        return actions_path, intent_path

    def test_first_run_commits_one(self, tmp_path):
        """First run: committed_new=1."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from ccc_execute import run_execute

        intent_id = "test_intent_idempotent_aabb1122"
        actions_path, _ = self._make_minimal_actions_json(tmp_path, intent_id)
        commit_ledger = tmp_path / "commit.jsonl"

        result = run_execute(
            actions_file=str(actions_path),
            commit_ledger_path=str(commit_ledger),
            mode="paper",
            quote_only=False,
            allow_stale=True,  # bypass stale guard in tests
        )

        assert result["committed"] == 1, f"First run committed={result['committed']}"
        assert result["skipped_already_committed"] == 0

    def test_second_run_is_idempotent(self, tmp_path):
        """Second run with same allocator_actions.json: committed_new=0."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from ccc_execute import run_execute

        intent_id = "test_intent_idempotent_ccdd3344"
        actions_path, _ = self._make_minimal_actions_json(tmp_path, intent_id)
        commit_ledger = tmp_path / "commit.jsonl"

        # First run
        run_execute(
            actions_file=str(actions_path),
            commit_ledger_path=str(commit_ledger),
            mode="paper",
            quote_only=False,
            allow_stale=True,
        )

        # Second run must be idempotent
        result2 = run_execute(
            actions_file=str(actions_path),
            commit_ledger_path=str(commit_ledger),
            mode="paper",
            quote_only=False,
            allow_stale=True,
        )

        assert result2["committed"] == 0, (
            f"Second run must have committed_new=0, got {result2['committed']}"
        )
        assert result2["skipped_already_committed"] == 1, (
            "Second run must show skipped=1"
        )
