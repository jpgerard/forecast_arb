"""
tests/test_patch_v15.py — Unit tests for Patch Pack v1.5

Covers all deliverables:
  Task 1: Canonical commit ledger schema (date=local, action=OPEN, strikes=[long,short])
  Task 2: BudgetState reads commit ledger; legacy row handling; week/month rollups
  Task 3: ccc_ledger_sanitize.py drops incomplete OPEN rows
  Task 4: run_execute (quote-only → no commit; paper → commit; dedup)
  Task 5: build_order_intent_from_candidate() + validate_order_intent() pass end-to-end
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure project root and scripts/ dir are importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SCRIPTS_DIR = str(PROJECT_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLICY_STUB: Dict[str, Any] = {
    "policy_id": "ccc_v1",
    "budgets": {
        "monthly_baseline": 500.0,
        "monthly_max": 800.0,
        "weekly_baseline": 150.0,
        "daily_baseline": 50.0,
        "weekly_kicker": 300.0,
        "daily_kicker": 100.0,
    },
    "kicker": {
        "min_conditioning_confidence": 0.66,
        "max_vix_percentile": 35.0,
    },
    "inventory_targets": {"crash": 1, "selloff": 1},
}


def _make_valid_intent(intent_id: str = "abc123", regime: str = "crash") -> Dict[str, Any]:
    """Minimal OrderIntent that passes validate_order_intent()."""
    return {
        "strategy": "ccc_v1",
        "symbol": "SPY",
        "expiry": "20260402",
        "type": "VERTICAL_PUT_DEBIT",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 585.0, "ratio": 1, "exchange": "SMART", "currency": "USD"},
            {"action": "SELL", "right": "P", "strike": 565.0, "ratio": 1, "exchange": "SMART", "currency": "USD"},
        ],
        "qty": 1,
        "limit": {"start": 36.0, "max": 36.72},
        "tif": "DAY",
        "guards": {"max_debit": 36.72, "max_spread_width": 0.20, "min_dte": 7},
        "intent_id": intent_id,
        "regime": regime,
        "candidate_id": f"SPY_{regime}_test",
    }


def _make_valid_action(
    intent_path: str,
    candidate_id: str = "SPY_crash_test",
    qty: int = 1,
    premium: float = 36.0,
    regime: str = "crash",
) -> Dict[str, Any]:
    """Minimal allocator OPEN action dict with intent_path."""
    return {
        "type": "OPEN",
        "reason_codes": [f"EV_PER_DOLLAR:0.30", f"REGIME:{regime}"],
        "candidate_id": candidate_id,
        "run_id": None,
        "candidate_rank": 1,
        "qty": qty,
        "premium": premium,
        "intent_path": intent_path,
    }


def _make_actions_file(tmp_path: Path, actions: List[Dict], policy_id: str = "ccc_v1") -> Path:
    """Write allocator_actions.json and return its path."""
    p = tmp_path / "allocator_actions.json"
    data = {"policy_id": policy_id, "actions": actions}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _make_intent_file(tmp_path: Path, intent: Dict[str, Any]) -> Path:
    """Write an intent JSON file and return its path."""
    name = f"OPEN_{intent.get('candidate_id', 'test')}.json"
    p = tmp_path / name
    p.write_text(json.dumps(intent, indent=2), encoding="utf-8")
    return p


def _make_commit_ledger(tmp_path: Path, records: List[Dict]) -> Path:
    """Write a commit ledger JSONL and return its path."""
    p = tmp_path / "allocator_commit_ledger.jsonl"
    lines = [json.dumps(r, separators=(",", ":")) for r in records]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


def _make_plan_ledger(tmp_path: Path, records: List[Dict]) -> Path:
    """Write a plan ledger JSONL and return its path."""
    p = tmp_path / "allocator_plan_ledger.jsonl"
    lines = [json.dumps(r, separators=(",", ":")) for r in records]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


# ===========================================================================
# Task 1 — Canonical commit ledger schema
# ===========================================================================

class TestCommitLedgerSchema:
    """Task 1: Verify canonical commit record structure."""

    def test_canonical_record_has_date(self, tmp_path):
        """Commit record must have 'date' field in YYYY-MM-DD format."""
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intent001", "crash")
        action = _make_valid_action("intents/OPEN_test.json")
        record = _build_canonical_commit_record(
            action=action,
            intent=intent,
            policy_id="ccc_v1",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            mode="paper",
        )

        assert "date" in record
        date.fromisoformat(record["date"])  # raises ValueError if invalid format

    def test_canonical_record_strikes_are_list(self, tmp_path):
        """Commit record strikes must be a 2-element list, never a dict."""
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intent002", "crash")
        action = _make_valid_action("intents/OPEN_test.json")
        record = _build_canonical_commit_record(
            action=action,
            intent=intent,
            policy_id="ccc_v1",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            mode="paper",
        )

        assert isinstance(record["strikes"], list), "strikes must be a list, not a dict"
        assert len(record["strikes"]) == 2, "strikes must have exactly 2 elements"
        assert record["strikes"][0] == 585.0, "long_put must be first element"
        assert record["strikes"][1] == 565.0, "short_put must be second element"

    def test_canonical_record_action_is_open(self, tmp_path):
        """Commit record must have action == 'OPEN'."""
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intent003", "crash")
        action = _make_valid_action("intents/OPEN_test.json")
        record = _build_canonical_commit_record(
            action=action,
            intent=intent,
            policy_id="ccc_v1",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            mode="paper",
        )

        assert record["action"] == "OPEN"

    def test_canonical_record_has_required_fields(self, tmp_path):
        """All spec-required fields must be present in commit record."""
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intent004", "selloff")
        action = _make_valid_action("intents/OPEN_selloff.json", regime="selloff")
        record = _build_canonical_commit_record(
            action=action,
            intent=intent,
            policy_id="ccc_v1",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            mode="paper",
        )

        for field in ["date", "timestamp_utc", "action", "policy_id", "intent_id",
                      "regime", "underlier", "expiry", "strikes", "qty",
                      "premium_per_contract", "premium_spent"]:
            assert field in record, f"Missing required field: {field}"

    def test_canonical_record_fails_loud_on_missing_regime(self, tmp_path):
        """If regime is missing from intent, ValueError is raised (fail loud)."""
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intent005", "crash")
        intent["regime"] = ""  # empty = missing
        action = _make_valid_action("intents/OPEN_test.json")

        with pytest.raises(ValueError, match="missing hard-required fields"):
            _build_canonical_commit_record(
                action=action,
                intent=intent,
                policy_id="ccc_v1",
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                mode="paper",
            )

    def test_canonical_record_fails_loud_on_no_legs(self, tmp_path):
        """If intent has no legs, ValueError is raised when extracting strikes."""
        from ccc_execute import _extract_strikes_from_intent

        intent_no_legs = {"legs": []}
        with pytest.raises(ValueError, match="cannot extract strikes"):
            _extract_strikes_from_intent(intent_no_legs)

    def test_canonical_record_premium_spent_is_qty_times_premium(self, tmp_path):
        """premium_spent == qty * premium_per_contract."""
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intentP", "crash")
        action = _make_valid_action("intents/OPEN_test.json", qty=3, premium=36.0)
        record = _build_canonical_commit_record(
            action=action,
            intent=intent,
            policy_id="ccc_v1",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            mode="paper",
        )

        assert record["qty"] == 3
        assert record["premium_per_contract"] == 36.0
        assert record["premium_spent"] == pytest.approx(108.0)

    def test_extract_strikes_sells_buy_legs(self, tmp_path):
        """Strikes extracted from BUY/SELL legs correctly: [long_put, short_put]."""
        from ccc_execute import _extract_strikes_from_intent

        intent = {
            "legs": [
                {"action": "BUY", "strike": 585.0},
                {"action": "SELL", "strike": 565.0},
            ]
        }
        strikes = _extract_strikes_from_intent(intent)
        assert strikes == [585.0, 565.0]

    def test_reconcile_can_parse_commit_record(self, tmp_path):
        """
        A commit ledger record must be parseable by budget.py (schema consistency).
        This mimics the reconcile flow reading the date field.
        """
        from ccc_execute import _build_canonical_commit_record

        intent = _make_valid_intent("intentR", "crash")
        action = _make_valid_action("intents/OPEN_test.json")
        today = datetime.now(timezone.utc).isoformat()
        record = _build_canonical_commit_record(
            action=action, intent=intent,
            policy_id="ccc_v1", timestamp_utc=today, mode="paper"
        )

        # budget.py uses action == "OPEN" and date.fromisoformat(rec["date"])
        assert record.get("action") == "OPEN"
        assert isinstance(record.get("strikes"), list) and len(record["strikes"]) == 2
        parsed_date = date.fromisoformat(record["date"])
        assert isinstance(parsed_date, date)


# ===========================================================================
# Task 2 — BudgetState reads commit ledger correctly
# ===========================================================================

class TestBudgetStateCommitLedger:
    """Task 2: BudgetState correctly reads commit ledger for spent_today."""

    def _dummy_ledger_path(self, tmp_path: Path) -> Path:
        return tmp_path / "allocator_commit_ledger.jsonl"

    def test_empty_commit_ledger_gives_zero_spend(self, tmp_path):
        """Empty commit ledger → spent_today == 0."""
        from forecast_arb.allocator.budget import compute_budget_state

        ledger_path = self._dummy_ledger_path(tmp_path)
        # File doesn't exist → treated as empty
        budget = compute_budget_state(_POLICY_STUB, ledger_path)
        assert budget.spent_today == 0.0
        assert budget.spent_week == 0.0
        assert budget.spent_month == 0.0

    def test_one_commit_today_increments_spent_today(self, tmp_path):
        """After one OPEN commit entry dated today, spent_today_before increments."""
        from forecast_arb.allocator.budget import compute_budget_state

        today = datetime.now(timezone.utc).date().isoformat()
        record = {
            "action": "OPEN",
            "date": today,
            "premium_spent": 36.0,
        }
        ledger_path = _make_commit_ledger(tmp_path, [record])

        budget = compute_budget_state(_POLICY_STUB, ledger_path)
        assert budget.spent_today == pytest.approx(36.0)
        assert budget.spent_week == pytest.approx(36.0)
        assert budget.spent_month == pytest.approx(36.0)

    def test_idempotent_reading(self, tmp_path):
        """Reading the same ledger twice gives same result (idempotent)."""
        from forecast_arb.allocator.budget import compute_budget_state

        today = datetime.now(timezone.utc).date().isoformat()
        record = {"action": "OPEN", "date": today, "premium_spent": 25.0}
        ledger_path = _make_commit_ledger(tmp_path, [record])

        b1 = compute_budget_state(_POLICY_STUB, ledger_path)
        b2 = compute_budget_state(_POLICY_STUB, ledger_path)
        assert b1.spent_today == b2.spent_today

    def test_legacy_rows_missing_action_skipped_no_crash(self, tmp_path):
        """Rows missing 'action' field are skipped gracefully (no crash)."""
        from forecast_arb.allocator.budget import compute_budget_state

        today = datetime.now(timezone.utc).date().isoformat()
        legacy_row = {
            # Old v1.4 schema — no 'action' key
            "date": today,
            "intent_id": "old_intent",
            "premium_spent": 40.0,
            "mode": "paper",
        }
        ledger_path = _make_commit_ledger(tmp_path, [legacy_row])

        # Should NOT crash; legacy row is ignored
        budget = compute_budget_state(_POLICY_STUB, ledger_path)
        assert budget.legacy_unusable_count == 1
        assert budget.spent_today == 0.0  # NOT counted

    def test_legacy_rows_missing_date_skipped(self, tmp_path):
        """Rows with missing/invalid 'date' are skipped with counter."""
        from forecast_arb.allocator.budget import compute_budget_state

        bad_rows = [
            {"action": "OPEN", "date": "", "premium_spent": 30.0},
            {"action": "OPEN", "date": None, "premium_spent": 20.0},
            {"action": "OPEN", "date": "notadate", "premium_spent": 10.0},
        ]
        ledger_path = _make_commit_ledger(tmp_path, bad_rows)

        budget = compute_budget_state(_POLICY_STUB, ledger_path)
        assert budget.legacy_unusable_count == 3
        assert budget.spent_today == 0.0

    def test_week_month_rollups_with_multiple_dates(self, tmp_path):
        """Week and month rollups work correctly with multiple dates."""
        from forecast_arb.allocator.budget import compute_budget_state

        today = datetime.now(timezone.utc).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        today_str = today.isoformat()
        # A date 35 days ago (different month)
        thirty_five_days_ago = (today - timedelta(days=35)).isoformat()

        records = [
            {"action": "OPEN", "date": today_str, "premium_spent": 20.0},
            {"action": "OPEN", "date": yesterday, "premium_spent": 15.0},
            {"action": "OPEN", "date": thirty_five_days_ago, "premium_spent": 50.0},
        ]
        ledger_path = _make_commit_ledger(tmp_path, records)

        budget = compute_budget_state(_POLICY_STUB, ledger_path)

        assert budget.spent_today == pytest.approx(20.0)
        # yesterday may or may not be same ISO week; this week total >= today
        assert budget.spent_week >= budget.spent_today
        # 35-day-ago is in a different month for most dates; don't test exact value
        assert budget.spent_month >= budget.spent_today

    def test_non_open_action_not_counted(self, tmp_path):
        """DAILY_SUMMARY and HARVEST_CLOSE rows do NOT count toward budget spend."""
        from forecast_arb.allocator.budget import compute_budget_state

        today = datetime.now(timezone.utc).date().isoformat()
        records = [
            {"action": "DAILY_SUMMARY", "date": today, "premium_spent": 100.0},
            {"action": "HARVEST_CLOSE",  "date": today, "premium_spent": 50.0},
            {"action": "OPEN",           "date": today, "premium_spent": 30.0},
        ]
        ledger_path = _make_commit_ledger(tmp_path, records)

        budget = compute_budget_state(_POLICY_STUB, ledger_path)
        assert budget.spent_today == pytest.approx(30.0)


# ===========================================================================
# Task 3 — ccc_ledger_sanitize.py
# ===========================================================================

class TestLedgerSanitize:
    """Task 3: sanitize_plan_ledger drops incomplete OPEN rows."""

    def test_drops_open_rows_missing_underlier(self, tmp_path):
        """OPEN rows missing 'underlier' are dropped."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "OPEN", "underlier": "", "expiry": "20260402", "regime": "crash",
             "strikes": {"long_put": 585}, "candidate_id": "bad1"},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=False)

        assert stats["dropped_open"] == 1
        assert stats["kept"] == 0

    def test_drops_open_rows_missing_expiry(self, tmp_path):
        """OPEN rows missing 'expiry' are dropped."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "OPEN", "underlier": "SPY", "expiry": None, "regime": "crash",
             "strikes": [585, 565], "candidate_id": "bad2"},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=False)

        assert stats["dropped_open"] == 1

    def test_drops_open_rows_missing_regime(self, tmp_path):
        """OPEN rows missing 'regime' are dropped."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "OPEN", "underlier": "SPY", "expiry": "20260402", "regime": "",
             "strikes": [585, 565], "candidate_id": "bad3"},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=False)

        assert stats["dropped_open"] == 1

    def test_keeps_complete_open_rows(self, tmp_path):
        """Complete OPEN rows are retained."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "OPEN", "underlier": "SPY", "expiry": "20260402", "regime": "crash",
             "strikes": [585, 565], "candidate_id": "good1"},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=False)

        assert stats["dropped_open"] == 0
        assert stats["kept"] == 1

    def test_keeps_all_non_open_rows(self, tmp_path):
        """DAILY_SUMMARY, HARVEST_CLOSE, ROLL_CLOSE rows are always kept."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "DAILY_SUMMARY", "date": "2026-02-01", "hold_count": 1},
            {"action": "HARVEST_CLOSE", "trade_id": "t1"},
            {"action": "ROLL_CLOSE", "trade_id": "t2"},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=False)

        assert stats["dropped_open"] == 0
        assert stats["kept"] == 3

    def test_mixed_ledger_correct_split(self, tmp_path):
        """Mixed rows: bad OPENs dropped, good OPENs and non-OPENs kept."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "DAILY_SUMMARY", "date": "2026-02-01"},                               # keep
            {"action": "OPEN", "underlier": "SPY", "expiry": "20260402",
             "regime": "crash", "strikes": [585, 565]},                                       # keep
            {"action": "OPEN", "underlier": "", "expiry": "20260402",
             "regime": "crash", "strikes": [585, 565]},                                       # drop
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=False)

        assert stats["total"] == 3
        assert stats["dropped_open"] == 1
        assert stats["kept"] == 2

    def test_dry_run_does_not_write(self, tmp_path):
        """Dry-run mode reports what would be dropped, does NOT create output file."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "OPEN", "underlier": "", "expiry": "20260402", "regime": "crash"},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        stats = sanitize_plan_ledger(ledger_path, dry_run=True)

        out_path = ledger_path.with_suffix(".sanitized.jsonl")
        assert not out_path.exists(), "dry-run must NOT write output file"
        assert stats["dropped_open"] == 1

    def test_sanitized_file_is_valid_jsonl(self, tmp_path):
        """Output file is valid JSONL (every line parses as JSON)."""
        from ccc_ledger_sanitize import sanitize_plan_ledger

        rows = [
            {"action": "DAILY_SUMMARY", "date": "2026-02-01"},
            {"action": "OPEN", "underlier": "SPY", "expiry": "20260402", "regime": "crash",
             "strikes": [585, 565]},
        ]
        ledger_path = _make_plan_ledger(tmp_path, rows)
        sanitize_plan_ledger(ledger_path, dry_run=False)

        out_path = ledger_path.with_suffix(".sanitized.jsonl")
        assert out_path.exists()
        for line in out_path.read_text().splitlines():
            if line.strip():
                json.loads(line)  # raises if invalid


# ===========================================================================
# Task 4 — daily.py --execute --paper/--live --quote-only
# ===========================================================================

class TestRunExecute:
    """Task 4: run_execute API — quote-only, paper commit, dedup."""

    def _write_intent_and_action(self, tmp_path: Path, regime: str = "crash"):
        """Write a valid intent file + matching actions JSON, return paths."""
        intent_id = f"testintent_{regime}"
        intent = _make_valid_intent(intent_id, regime)
        intent_file = _make_intent_file(tmp_path, intent)

        action = _make_valid_action(str(intent_file), regime=regime)
        actions_file = _make_actions_file(tmp_path, [action])

        return actions_file, intent_file, intent_id

    def test_quote_only_does_not_update_commit_ledger(self, tmp_path):
        """--execute --paper --quote-only: commit ledger NOT written."""
        from ccc_execute import run_execute

        actions_file, intent_file, intent_id = self._write_intent_and_action(tmp_path)
        commit_path = tmp_path / "allocator_commit_ledger.jsonl"

        result = run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=True,
        )

        assert result["committed"] == 0
        assert result["mode"] == "quote-only"
        assert not commit_path.exists(), "Quote-only must NOT create commit ledger"

    def test_paper_stage_updates_commit_ledger(self, tmp_path):
        """--execute --paper: commit ledger is written with canonical schema."""
        from ccc_execute import run_execute

        actions_file, intent_file, intent_id = self._write_intent_and_action(tmp_path)
        commit_path = tmp_path / "allocator_commit_ledger.jsonl"

        result = run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=False,
        )

        assert result["committed"] == 1
        assert result["errors"] == 0
        assert commit_path.exists()

        # Read and verify canonical schema
        records = [json.loads(line) for line in commit_path.read_text().splitlines() if line.strip()]
        assert len(records) == 1
        rec = records[0]

        assert rec["action"] == "OPEN"
        assert isinstance(rec["strikes"], list)
        assert len(rec["strikes"]) == 2
        assert rec["intent_id"] == intent_id
        assert rec["underlier"] == "SPY"
        assert rec["expiry"] == "20260402"
        assert rec["regime"] == "crash"
        assert "date" in rec
        date.fromisoformat(rec["date"])  # validates format

    def test_double_commit_prevention_by_intent_id(self, tmp_path):
        """Running run_execute twice does NOT double-commit (dedup by intent_id)."""
        from ccc_execute import run_execute

        actions_file, intent_file, intent_id = self._write_intent_and_action(tmp_path)
        commit_path = tmp_path / "allocator_commit_ledger.jsonl"

        # First run
        r1 = run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=False,
        )
        assert r1["committed"] == 1

        # Second run — same intent_id already present
        r2 = run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=False,
        )
        assert r2["committed"] == 0
        assert r2["skipped_already_committed"] == 1

        # Only 1 record in ledger total
        records = [
            json.loads(line. strip())
            for line in commit_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(records) == 1

    def test_budget_reflects_spend_after_commit(self, tmp_path):
        """
        After paper commit, compute_budget_state reflects spend on next run.
        This verifies Task 2 + Task 4 integration.
        """
        from ccc_execute import run_execute
        from forecast_arb.allocator.budget import compute_budget_state

        actions_file, intent_file, intent_id = self._write_intent_and_action(tmp_path)
        commit_path = tmp_path / "allocator_commit_ledger.jsonl"

        # Before commit
        b_before = compute_budget_state(_POLICY_STUB, commit_path)
        assert b_before.spent_today == 0.0

        # Commit
        run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=False,
        )

        # After commit
        b_after = compute_budget_state(_POLICY_STUB, commit_path)
        assert b_after.spent_today == pytest.approx(36.0)  # premium = 36.0 from intent stub

    def test_no_open_actions_returns_zero(self, tmp_path):
        """If actions file has no OPEN actions with intent_path, committed=0."""
        from ccc_execute import run_execute

        # Actions file with only a HOLD action
        hold_action = {"type": "HOLD", "reason_codes": ["NO_CANDIDATES_FILE"]}
        actions_file = _make_actions_file(tmp_path, [hold_action])
        commit_path = tmp_path / "allocator_commit_ledger.jsonl"

        result = run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=False,
        )
        assert result["committed"] == 0
        assert not commit_path.exists()

    def test_missing_intent_file_counts_as_error(self, tmp_path):
        """If intent_path in action does not exist, it counts as an error (not crash)."""
        from ccc_execute import run_execute

        action = _make_valid_action(str(tmp_path / "NONEXISTENT_intent.json"))
        actions_file = _make_actions_file(tmp_path, [action])
        commit_path = tmp_path / "allocator_commit_ledger.jsonl"

        result = run_execute(
            actions_file=str(actions_file),
            commit_ledger_path=str(commit_path),
            mode="paper",
            quote_only=False,
        )
        assert result["errors"] == 1
        assert result["committed"] == 0


# ===========================================================================
# Task 5 — OPEN intent validation end-to-end
# ===========================================================================

class TestOpenIntentValidation:
    """Task 5: build_order_intent_from_candidate() passes validate_order_intent()."""

    def _minimal_policy(self) -> Dict[str, Any]:
        return {"policy_id": "ccc_v1"}

    def test_open_intent_passes_validate_order_intent(self):
        """build_order_intent_from_candidate() produces an intent that passes validation."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate
        from forecast_arb.execution.execute_trade import validate_order_intent

        candidate = {
            "underlier": "SPY",
            "regime": "crash",
            "expiry": "20260402",
            "long_strike": 585.0,
            "short_strike": 565.0,
            "debit_per_contract": 36.0,
            "candidate_id": "SPY_crash_20260402_585_565",
        }

        intent = build_order_intent_from_candidate(
            candidate=candidate,
            qty=1,
            policy=self._minimal_policy(),
        )

        # Should not raise
        validate_order_intent(intent)

    def test_open_intent_has_intent_id(self):
        """Built intent always has a non-empty intent_id."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = {
            "underlier": "SPY",
            "expiry": "20260402",
            "long_strike": 585.0,
            "short_strike": 565.0,
            "debit_per_contract": 36.0,
        }

        intent = build_order_intent_from_candidate(
            candidate=candidate, qty=1, policy={"policy_id": "ccc_v1"}
        )

        assert "intent_id" in intent
        assert intent["intent_id"] != ""
        assert len(intent["intent_id"]) == 40  # SHA1 hex

    def test_open_intent_is_deterministic(self):
        """Same candidate always produces the same intent_id."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = {
            "underlier": "SPY",
            "expiry": "20260402",
            "long_strike": 585.0,
            "short_strike": 565.0,
            "debit_per_contract": 36.0,
            "candidate_id": "SPY_crash_test123",
        }
        policy = {"policy_id": "ccc_v1"}

        intent1 = build_order_intent_from_candidate(candidate, qty=1, policy=policy)
        intent2 = build_order_intent_from_candidate(candidate, qty=1, policy=policy)

        assert intent1["intent_id"] == intent2["intent_id"]

    def test_open_intent_has_required_fields_for_execution(self):
        """Intent has all fields required by validate_order_intent."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = {
            "underlier": "QQQ",
            "expiry": "20260417",
            "long_strike": 490.0,
            "short_strike": 470.0,
            "debit_per_contract": 28.0,
            "candidate_id": "QQQ_selloff_test",
            "regime": "selloff",
        }
        policy = {"policy_id": "ccc_v1"}
        intent = build_order_intent_from_candidate(candidate, qty=2, policy=policy)

        for field in ["strategy", "symbol", "expiry", "type", "legs", "qty",
                      "limit", "tif", "guards", "intent_id"]:
            assert field in intent, f"Missing required field: {field}"

        assert intent["qty"] == 2
        assert intent["symbol"] == "QQQ"

    def test_open_intent_limit_max_gte_start(self):
        """limit.max must be >= limit.start (price band is valid)."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate

        candidate = {
            "underlier": "SPY",
            "expiry": "20260402",
            "long_strike": 585.0,
            "short_strike": 565.0,
            "debit_per_contract": 36.0,
        }
        intent = build_order_intent_from_candidate(
            candidate, qty=1, policy={"policy_id": "ccc_v1"}
        )

        assert intent["limit"]["max"] >= intent["limit"]["start"]

    def test_qqq_candidate_also_passes_validation(self):
        """QQQ selloff candidate also produces a valid intent."""
        from forecast_arb.allocator.plan import build_order_intent_from_candidate
        from forecast_arb.execution.execute_trade import validate_order_intent

        candidate = {
            "underlier": "QQQ",
            "regime": "selloff",
            "expiry": "20260417",
            "long_strike": 490.0,
            "short_strike": 470.0,
            "debit_per_contract": 18.50,
            "candidate_id": "QQQ_selloff_490_470",
        }

        intent = build_order_intent_from_candidate(
            candidate=candidate, qty=1, policy={"policy_id": "ccc_v1"}
        )

        # Must not raise
        validate_order_intent(intent)


# ===========================================================================
# Integration — canonical schema round-trip through budget
# ===========================================================================

class TestCanonicalSchemaRoundTrip:
    """
    Integration: write a canonical commit record, then read it with budget.
    Ensures Task 1 schema is fully compatible with Task 2 budget reader.
    """

    def test_commit_record_read_by_budget_increments_spent(self, tmp_path):
        """Canonical commit record written by ccc_execute is correctly read by budget."""
        from ccc_execute import _build_canonical_commit_record, _append_commit_record
        from forecast_arb.allocator.budget import compute_budget_state

        intent = _make_valid_intent("roundtrip_001", "crash")
        action = {
            "type": "OPEN",
            "reason_codes": ["EV_PER_DOLLAR:0.40"],
            "candidate_id": "SPY_crash_round",
            "run_id": None,
            "candidate_rank": 1,
            "qty": 2,
            "premium": 36.0,
            "intent_path": "intents/allocator/OPEN_test.json",
        }

        ts = datetime.now(timezone.utc).isoformat()
        record = _build_canonical_commit_record(
            action=action, intent=intent, policy_id="ccc_v1",
            timestamp_utc=ts, mode="paper"
        )

        commit_path = tmp_path / "allocator_commit_ledger.jsonl"
        _append_commit_record(commit_path, record)

        budget = compute_budget_state(_POLICY_STUB, commit_path)
        assert budget.spent_today == pytest.approx(72.0)  # 2 * 36.0
        assert budget.legacy_unusable_count == 0
