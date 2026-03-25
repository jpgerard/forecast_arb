"""
tests/test_ccc_v19_operator_hygiene.py

CCC v1.9 "Operator Hygiene" — Test Suite

Covers:
  1. TestCancelIdempotency        — cancel twice is safe; second call is NO_OP
  2. TestCancelRefusalWhenFilled  — refuse cancel if POSITION_OPENED in fills
  3. TestCancelClearsFromPending  — after cancel, intent not in pending set
  4. TestCommitPendingFillCleared — Task 4 lifecycle: commit -> pending; fill -> cleared
  5. TestStalePendingWarning      — pending > N days triggers stale warnings
  6. TestCancelValidation         — empty intent_id / reason raise ValueError
  7. TestCancelExpiredIdempotency — OPEN_EXPIRED also treated as terminal (no-op)
  8. TestStatusOutput             — run_status returns correct structure
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Any
import tempfile
import os

# Ensure project root on path
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
# Also add scripts/ so ccc_cancel can be imported directly
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from forecast_arb.allocator.pending import (
    compute_pending_intent_ids,
    load_commit_intent_ids,
    load_canceled_intent_ids,
    load_filled_intent_ids,
    load_pending_rows_with_age,
)


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows) -> None:
    """Write a list of dicts to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _make_open_row(intent_id: str, regime: str = "crash", date_str: str = "2026-03-05") -> Dict[str, Any]:
    return {
        "date": date_str,
        "timestamp_utc": f"{date_str}T10:00:00+00:00",
        "action": "OPEN",
        "policy_id": "ccc_v1",
        "intent_id": intent_id,
        "candidate_id": f"cand_{intent_id[:6]}",
        "regime": regime,
        "underlier": "SPY",
        "expiry": "20260402",
        "strikes": [585.0, 565.0],
        "qty": 1,
        "premium_per_contract": 36.0,
        "premium_spent": 36.0,
        "mode": "paper",
    }


def _make_fill_row(intent_id: str, date_str: str = "2026-03-05") -> Dict[str, Any]:
    return {
        "date": date_str,
        "timestamp_utc": f"{date_str}T12:00:00+00:00",
        "action": "POSITION_OPENED",
        "intent_id": intent_id,
        "regime": "crash",
        "mode": "paper",
    }


# ---------------------------------------------------------------------------
# 1.  TestCancelIdempotency
# ---------------------------------------------------------------------------

class TestCancelIdempotency(unittest.TestCase):
    """Cancel the same intent twice — second call must be NO_OP."""

    def _run_cancel(self, intent_id, reason, commit_path, fills_path):
        from ccc_cancel import run_cancel  # type: ignore[import]
        return run_cancel(
            intent_id=intent_id,
            reason=reason,
            mode="paper",
            commit_ledger_path=commit_path,
            fills_ledger_path=fills_path,
        )

    def test_first_cancel_writes_row(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            # Seed a committed intent
            _write_jsonl(commit, [_make_open_row("intent_abc")])

            result = self._run_cancel("intent_abc", "test reason", commit, fills)
            self.assertEqual(result["action"], "CANCELED")
            rows = _read_jsonl(commit)
            cancel_rows = [r for r in rows if r.get("action") == "OPEN_CANCELED"]
            self.assertEqual(len(cancel_rows), 1)
            self.assertEqual(cancel_rows[0]["intent_id"], "intent_abc")
            self.assertEqual(cancel_rows[0]["reason"], "test reason")

    def test_second_cancel_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(commit, [_make_open_row("intent_abc")])

            self._run_cancel("intent_abc", "first cancel", commit, fills)
            result2 = self._run_cancel("intent_abc", "second cancel", commit, fills)

            self.assertEqual(result2["action"], "NO_OP")
            rows = _read_jsonl(commit)
            cancel_rows = [r for r in rows if r.get("action") == "OPEN_CANCELED"]
            # Still only one OPEN_CANCELED row (no duplicate written)
            self.assertEqual(len(cancel_rows), 1)

    def test_cancel_on_unknown_intent_still_writes(self):
        """Canceling an intent that was never committed is allowed (no refusal)."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            # Empty ledgers

            result = self._run_cancel("unknown_xyz", "stale", commit, fills)
            self.assertEqual(result["action"], "CANCELED")


# ---------------------------------------------------------------------------
# 2.  TestCancelRefusalWhenFilled
# ---------------------------------------------------------------------------

class TestCancelRefusalWhenFilled(unittest.TestCase):
    """Cannot cancel an intent that has a POSITION_OPENED row in fills ledger."""

    def _run_cancel(self, intent_id, commit_path, fills_path):
        from ccc_cancel import run_cancel  # type: ignore[import]
        return run_cancel(
            intent_id=intent_id,
            reason="trying to cancel filled position",
            mode="paper",
            commit_ledger_path=commit_path,
            fills_ledger_path=fills_path,
        )

    def test_refuse_when_position_opened(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(commit, [_make_open_row("intent_filled")])
            _write_jsonl(fills, [_make_fill_row("intent_filled")])

            result = self._run_cancel("intent_filled", commit, fills)
            self.assertEqual(result["action"], "REFUSED")
            self.assertIn("POSITION_OPENED", result["message"])
            self.assertIn("intent_filled", result["message"])

    def test_refuse_does_not_write_to_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(commit, [_make_open_row("intent_filled")])
            _write_jsonl(fills, [_make_fill_row("intent_filled")])

            self._run_cancel("intent_filled", commit, fills)

            # Commit ledger must have only the original OPEN row
            rows = _read_jsonl(commit)
            cancel_rows = [r for r in rows if r.get("action") == "OPEN_CANCELED"]
            self.assertEqual(len(cancel_rows), 0)

    def test_unfilled_intent_can_be_canceled(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(commit, [_make_open_row("intent_pending")])
            # fills ledger is empty — cancel should succeed
            result_dict = {}
            from ccc_cancel import run_cancel  # type: ignore[import]
            result_dict = run_cancel(
                intent_id="intent_pending",
                reason="stale",
                mode="paper",
                commit_ledger_path=commit,
                fills_ledger_path=fills,
            )
            self.assertEqual(result_dict["action"], "CANCELED")


# ---------------------------------------------------------------------------
# 3.  TestCancelClearsFromPending
# ---------------------------------------------------------------------------

class TestCancelClearsFromPending(unittest.TestCase):
    """After cancel, intent_id must not appear in compute_pending_intent_ids."""

    def test_pending_before_cancel_after_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(commit, [_make_open_row("intent_X")])

            # Before cancel: intent_X is pending
            pending_before = compute_pending_intent_ids(commit, fills)
            self.assertIn("intent_X", pending_before)

            # Cancel
            from ccc_cancel import run_cancel  # type: ignore[import]
            run_cancel("intent_X", "stale", "paper", commit, fills)

            # After cancel: intent_X is NOT pending
            pending_after = compute_pending_intent_ids(commit, fills)
            self.assertNotIn("intent_X", pending_after)

    def test_cancel_does_not_affect_other_intents(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(
                commit,
                [_make_open_row("intent_A"), _make_open_row("intent_B")],
            )

            from ccc_cancel import run_cancel  # type: ignore[import]
            run_cancel("intent_A", "stale", "paper", commit, fills)

            pending = compute_pending_intent_ids(commit, fills)
            self.assertNotIn("intent_A", pending)
            self.assertIn("intent_B", pending)

    def test_load_canceled_intent_ids(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            _write_jsonl(commit, [_make_open_row("intent_Y")])

            from ccc_cancel import run_cancel  # type: ignore[import]
            run_cancel("intent_Y", "stale", "paper", commit, fills)

            canceled = load_canceled_intent_ids(commit)
            self.assertIn("intent_Y", canceled)


# ---------------------------------------------------------------------------
# 4.  TestCommitPendingFillCleared  (Task 4 explicit lifecycle test)
# ---------------------------------------------------------------------------

class TestCommitPendingFillCleared(unittest.TestCase):
    """
    Full lifecycle:
      1. Commit intent -> appears in pending
      2. Fill intent (POSITION_OPENED) -> removed from pending
    """

    def test_full_lifecycle_commit_then_fill(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"

            # Step 1: commit
            _write_jsonl(commit, [_make_open_row("intent_lifecycle")])
            pending_after_commit = compute_pending_intent_ids(commit, fills)
            self.assertIn("intent_lifecycle", pending_after_commit,
                          "Intent must be pending after commit, before fill")

            # Step 2: fill
            _write_jsonl(fills, [_make_fill_row("intent_lifecycle")])
            pending_after_fill = compute_pending_intent_ids(commit, fills)
            self.assertNotIn("intent_lifecycle", pending_after_fill,
                             "Intent must be cleared from pending after POSITION_OPENED fill")

    def test_staged_order_does_not_clear_pending(self):
        """ORDER_STAGED must NOT remove intent from pending."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"

            _write_jsonl(commit, [_make_open_row("intent_staged")])
            staged_row = {
                "date": "2026-03-05",
                "action": "ORDER_STAGED",
                "intent_id": "intent_staged",
                "mode": "paper",
            }
            _write_jsonl(fills, [staged_row])

            # ORDER_STAGED must not clear pending
            pending = compute_pending_intent_ids(commit, fills)
            self.assertIn("intent_staged", pending,
                          "ORDER_STAGED must not remove intent from pending")

    def test_multiple_intents_partial_fill(self):
        """Fill one of two committed intents; only the filled one is cleared."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"

            _write_jsonl(
                commit,
                [_make_open_row("intent_filled_1"), _make_open_row("intent_pending_2")],
            )
            _write_jsonl(fills, [_make_fill_row("intent_filled_1")])

            pending = compute_pending_intent_ids(commit, fills)
            self.assertNotIn("intent_filled_1", pending)
            self.assertIn("intent_pending_2", pending)


# ---------------------------------------------------------------------------
# 5.  TestStalePendingWarning
# ---------------------------------------------------------------------------

class TestStalePendingWarning(unittest.TestCase):
    """
    load_pending_rows_with_age should flag intents whose commit date is old.
    _check_stale_pending in daily.py should return stale rows.
    """

    def _make_old_open_row(self, intent_id, days_ago=3):
        old_date = (date.today() - timedelta(days=days_ago)).isoformat()
        return _make_open_row(intent_id, date_str=old_date)

    def test_fresh_intent_not_stale(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            today = date.today().isoformat()
            _write_jsonl(commit, [_make_open_row("intent_fresh", date_str=today)])

            rows = load_pending_rows_with_age(commit, fills, today=today)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["age_days"], 0)

    def test_old_intent_has_positive_age(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            old_date = (date.today() - timedelta(days=3)).isoformat()
            _write_jsonl(commit, [_make_open_row("intent_old", date_str=old_date)])

            rows = load_pending_rows_with_age(commit, fills, today=date.today().isoformat())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["age_days"], 3)

    def test_stale_threshold_triggers_at_3_days(self):
        """Intent that is 3 days old triggers stale with default threshold=2."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            old_date = (date.today() - timedelta(days=3)).isoformat()
            _write_jsonl(commit, [_make_open_row("intent_stale", date_str=old_date)])

            rows = load_pending_rows_with_age(commit, fills, today=date.today().isoformat())
            stale = [r for r in rows if r["age_days"] > 2]  # default threshold
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["intent_id"], "intent_stale")

    def test_threshold_exactly_at_boundary_not_stale(self):
        """Intent that is exactly 2 days old is NOT stale (threshold=2 means >2)."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            boundary_date = (date.today() - timedelta(days=2)).isoformat()
            _write_jsonl(commit, [_make_open_row("intent_boundary", date_str=boundary_date)])

            rows = load_pending_rows_with_age(commit, fills, today=date.today().isoformat())
            stale = [r for r in rows if r["age_days"] > 2]
            self.assertEqual(len(stale), 0, "Exactly 2 days old should NOT be stale (threshold is >2)")

    def test_filled_intent_not_in_stale_list(self):
        """Filled intents must not appear in pending rows (stale or otherwise)."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            old_date = (date.today() - timedelta(days=5)).isoformat()
            _write_jsonl(commit, [_make_open_row("intent_filled_old", date_str=old_date)])
            _write_jsonl(fills, [_make_fill_row("intent_filled_old")])

            rows = load_pending_rows_with_age(commit, fills)
            iids = [r["intent_id"] for r in rows]
            self.assertNotIn("intent_filled_old", iids)

    def test_canceled_intent_not_in_stale_list(self):
        """Canceled intents must not appear in pending rows."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            old_date = (date.today() - timedelta(days=5)).isoformat()
            _write_jsonl(commit, [_make_open_row("intent_canceled_old", date_str=old_date)])

            # Cancel it
            from ccc_cancel import run_cancel  # type: ignore[import]
            run_cancel("intent_canceled_old", "stale", "paper", commit, fills)

            rows = load_pending_rows_with_age(commit, fills)
            iids = [r["intent_id"] for r in rows]
            self.assertNotIn("intent_canceled_old", iids)

    def test_check_stale_pending_helper_in_daily(self):
        """_check_stale_pending in daily.py returns stale rows via commit ledger path."""
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"  # adjacent file — same dir
            old_date = (date.today() - timedelta(days=3)).isoformat()
            _write_jsonl(commit, [_make_open_row("intent_stale2", date_str=old_date)])

            # Import helper from daily module
            _scripts = str(_PROJECT_ROOT / "scripts")
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)

            # Patch allocator fills_ledger detection: daily._check_stale_pending derives
            # fills path as commit_path.parent / "allocator_fills_ledger.jsonl"
            # We need the fills file to be at that derived path
            expected_fills = commit.parent / "allocator_fills_ledger.jsonl"
            expected_fills.touch()  # empty fills → nothing is filled

            import daily  # type: ignore[import]
            stale_rows = daily._check_stale_pending(
                str(commit), stale_days=2
            )
            self.assertEqual(len(stale_rows), 1)
            self.assertEqual(stale_rows[0]["intent_id"], "intent_stale2")

    def test_check_stale_pending_no_ledger_returns_empty(self):
        """_check_stale_pending with missing ledger path returns []."""
        import daily  # type: ignore[import]
        stale_rows = daily._check_stale_pending(None)
        self.assertEqual(stale_rows, [])


# ---------------------------------------------------------------------------
# 6.  TestCancelValidation
# ---------------------------------------------------------------------------

class TestCancelValidation(unittest.TestCase):
    """Empty intent_id or reason must raise ValueError."""

    def _cancel(self, intent_id, reason):
        from ccc_cancel import run_cancel  # type: ignore[import]
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            return run_cancel(intent_id, reason, "paper", commit, fills)

    def test_empty_intent_id_raises(self):
        with self.assertRaises(ValueError):
            self._cancel("", "some reason")

    def test_whitespace_intent_id_raises(self):
        with self.assertRaises(ValueError):
            self._cancel("   ", "some reason")

    def test_empty_reason_raises(self):
        with self.assertRaises(ValueError):
            self._cancel("intent_abc", "")

    def test_invalid_mode_raises(self):
        from ccc_cancel import run_cancel  # type: ignore[import]
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            with self.assertRaises(ValueError):
                run_cancel("intent_abc", "reason", "invalid_mode", commit, fills)


# ---------------------------------------------------------------------------
# 7.  TestCancelExpiredIdempotency
# ---------------------------------------------------------------------------

class TestCancelExpiredIdempotency(unittest.TestCase):
    """OPEN_EXPIRED in commit ledger should also prevent a second OPEN_CANCELED write."""

    def test_open_expired_prevents_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            # Pre-seed an OPEN_EXPIRED row
            expired_row = {
                "date": "2026-03-01",
                "timestamp_utc": "2026-03-01T08:00:00+00:00",
                "action": "OPEN_EXPIRED",
                "mode": "paper",
                "intent_id": "intent_expired",
                "reason": "system expiry",
            }
            _write_jsonl(commit, [expired_row])

            from ccc_cancel import run_cancel  # type: ignore[import]
            result = run_cancel("intent_expired", "manual cancel", "paper", commit, fills)
            self.assertEqual(result["action"], "NO_OP")
            self.assertIn("OPEN_EXPIRED", result["message"])


# ---------------------------------------------------------------------------
# 8.  TestStatusOutput
# ---------------------------------------------------------------------------

class TestStatusOutput(unittest.TestCase):
    """run_status in ccc_status returns correct structured output."""

    def test_empty_ledgers_returns_zero_counts(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            positions = Path(td) / "positions.json"

            from ccc_status import run_status  # type: ignore[import]
            result = run_status(
                commit_ledger_path=commit,
                fills_ledger_path=fills,
                positions_path=positions,
            )
            self.assertEqual(result["pending_counts"]["crash"], 0)
            self.assertEqual(result["pending_counts"]["selloff"], 0)
            self.assertEqual(result["stale_count"], 0)

    def test_pending_intent_appears_in_status(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            positions = Path(td) / "positions.json"
            _write_jsonl(commit, [_make_open_row("intent_status_test")])

            from ccc_status import run_status  # type: ignore[import]
            result = run_status(
                commit_ledger_path=commit,
                fills_ledger_path=fills,
                positions_path=positions,
            )
            self.assertEqual(result["pending_counts"]["crash"], 1)
            iids = [r["intent_id"] for r in result["pending_rows"]]
            self.assertIn("intent_status_test", iids)

    def test_stale_count_correct(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            positions = Path(td) / "positions.json"
            old_date = (date.today() - timedelta(days=4)).isoformat()
            fresh_date = date.today().isoformat()
            _write_jsonl(
                commit,
                [
                    _make_open_row("stale_A", date_str=old_date),
                    _make_open_row("fresh_B", date_str=fresh_date),
                ],
            )

            from ccc_status import run_status  # type: ignore[import]
            result = run_status(
                commit_ledger_path=commit,
                fills_ledger_path=fills,
                positions_path=positions,
                stale_days=2,
                today=date.today().isoformat(),
            )
            self.assertEqual(result["stale_count"], 1)
            stale_ids = [r["intent_id"] for r in result["pending_rows"] if r["age_days"] > 2]
            self.assertIn("stale_A", stale_ids)
            self.assertNotIn("fresh_B", stale_ids)

    def test_canceled_not_pending_in_status(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            positions = Path(td) / "positions.json"
            _write_jsonl(commit, [_make_open_row("intent_to_cancel")])

            from ccc_cancel import run_cancel  # type: ignore[import]
            run_cancel("intent_to_cancel", "stale test", "paper", commit, fills)

            from ccc_status import run_status  # type: ignore[import]
            result = run_status(
                commit_ledger_path=commit,
                fills_ledger_path=fills,
                positions_path=positions,
            )
            self.assertEqual(result["pending_counts"]["crash"], 0)
            iids = [r["intent_id"] for r in result["pending_rows"]]
            self.assertNotIn("intent_to_cancel", iids)

    def test_filled_not_counted_as_pending(self):
        with tempfile.TemporaryDirectory() as td:
            commit = Path(td) / "commit.jsonl"
            fills = Path(td) / "fills.jsonl"
            positions = Path(td) / "positions.json"
            _write_jsonl(commit, [_make_open_row("intent_filled_status")])
            _write_jsonl(fills, [_make_fill_row("intent_filled_status")])

            from ccc_status import run_status  # type: ignore[import]
            result = run_status(
                commit_ledger_path=commit,
                fills_ledger_path=fills,
                positions_path=positions,
            )
            self.assertEqual(result["pending_counts"]["crash"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
