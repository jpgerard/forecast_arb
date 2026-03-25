"""
CCC v1.7 — Fills Ledger and Reconcile Tests

All tests are deterministic, no IBKR dependency.
Uses tmp_path pytest fixtures and injects fixed date strings.

Tests (spec §K):
  1. fill_row_correct_fields: Given a fixture execution_result, reconcile writes a fills
     row with correct strikes/expiry/qty and debit calculation.
  2. dedup_by_intent_id: Running twice adds only 1 fills row.
  3. positions_snapshot_correct: positions.json is derived correctly from fills ledger.
  4. inventory_actual_uses_positions: inventory.actual uses positions.json and shows
     crash=1/1 after reconcile.
  5. harvest_multiple_net_over_gross: harvest multiple uses net if present else gross.
  6. intent_archival: Intent archival moves file(s) into _archive/YYYYMMDD.
  7. no_op_when_no_exec_result: When execution_result.json doesn't exist, run_reconcile
     returns fills_found=0 and writes nothing (no-op).
  8. build_positions_snapshot_dedup: build_positions_snapshot deduplicates by intent_id.
  9. entry_debit_from_leg_quotes: entry debit computed correctly from BUY ask / SELL bid.
  10. entry_debit_fallback_market_debit: falls back to market_debit * 100 if leg quotes missing.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# Fixtures — canonical execution_result + intent
# ---------------------------------------------------------------------------

FIXED_DATE = "2026-03-05"
FIXED_TS = "2026-03-05T14:30:00+00:00"
INTENT_ID = "a3ea604eecd990fc918733550d947fe620a93514"

FIXTURE_EXEC_RESULT: Dict[str, Any] = {
    "success": True,
    "order_id": 54,
    "status": "PendingSubmit",
    "transmit": False,
    "intent_path": "intents/allocator/OPEN_f4d3570c72fb.json",
    "symbol": "SPY",
    "expiry": "20260417",
    "qty": 1,
    "limit_price": 0.69,
    "market_debit": 0.5499999999999998,
    "spot_price": 681.54,
    "leg_quotes": [
        {
            "action": "BUY",
            "right": "P",
            "strike": 575.0,
            "quotes": {"bid": 2.07, "ask": 2.09, "mid": 2.08, "last": 2.22},
        },
        {
            "action": "SELL",
            "right": "P",
            "strike": 555.0,
            "quotes": {"bid": 1.54, "ask": 1.56, "mid": 1.55, "last": 1.66},
        },
    ],
    "timestamp_utc": FIXED_TS,
    "ledger_written": True,
}

FIXTURE_INTENT: Dict[str, Any] = {
    "strategy": "ccc_v1",
    "symbol": "SPY",
    "expiry": "20260417",
    "timestamp_utc": FIXED_TS,
    "type": "VERTICAL_PUT_DEBIT",
    "legs": [
        {"action": "BUY", "right": "P", "strike": 575.0, "ratio": 1},
        {"action": "SELL", "right": "P", "strike": 555.0, "ratio": 1},
    ],
    "qty": 1,
    "limit": {"start": 0.69, "max": 0.7038},
    "tif": "DAY",
    "guards": {"max_debit": 0.7038, "max_spread_width": 0.2, "min_dte": 7},
    "regime": "crash",
    "candidate_id": "f4d3570c72fb",
    "run_id": None,
    "cluster_id": "US_INDEX",
    "intent_id": INTENT_ID,
}


def _write_fixture_files(intents_dir: Path) -> tuple[Path, Path]:
    """Write fixture files to intents_dir, return (intent_path, exec_result_path)."""
    intents_dir.mkdir(parents=True, exist_ok=True)
    intent_path = intents_dir / "OPEN_f4d3570c72fb.json"
    exec_result_path = intents_dir / "execution_result.json"

    # Write exec_result with intent_path pointing to relative path
    exec_result = dict(FIXTURE_EXEC_RESULT)
    exec_result["intent_path"] = str(intent_path)

    with open(intent_path, "w") as f:
        json.dump(FIXTURE_INTENT, f, indent=2)
    with open(exec_result_path, "w") as f:
        json.dump(exec_result, f, indent=2)

    return intent_path, exec_result_path


def _write_filled_fixture_files(intents_dir: Path) -> tuple[Path, Path]:
    """Write fixture files with a FILLED exec_result (no transmit=False).

    v1.8: FIXTURE_EXEC_RESULT has transmit=False (paper staged order).
    This helper writes a 'filled' exec_result for tests that need POSITION_OPENED.
    """
    intents_dir.mkdir(parents=True, exist_ok=True)
    intent_path = intents_dir / "OPEN_f4d3570c72fb.json"
    exec_result_path = intents_dir / "execution_result.json"

    # Build a 'filled' exec_result: remove transmit=False, set status="Filled"
    exec_result = {k: v for k, v in FIXTURE_EXEC_RESULT.items() if k != "transmit"}
    exec_result["status"] = "Filled"
    exec_result["intent_path"] = str(intent_path)

    with open(intent_path, "w") as f:
        json.dump(FIXTURE_INTENT, f, indent=2)
    with open(exec_result_path, "w") as f:
        json.dump(exec_result, f, indent=2)

    return intent_path, exec_result_path


# ---------------------------------------------------------------------------
# Test 1: fill_row_correct_fields
# ---------------------------------------------------------------------------

class TestFillRowCorrectFields:
    """Test 1: build_fill_row produces correct fields from fixture data."""

    def test_strikes_long_and_short(self):
        """long_put = 575, short_put = 555."""
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(
            exec_result=FIXTURE_EXEC_RESULT,
            intent=FIXTURE_INTENT,
            date_str=FIXED_DATE,
            mode="paper",
        )
        assert row["strikes"] == [575.0, 555.0], f"strikes={row['strikes']}"

    def test_expiry_correct(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["expiry"] == "20260417"

    def test_qty_correct(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["qty"] == 1

    def test_regime_correct(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["regime"] == "crash"

    def test_underlier_correct(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["underlier"] == "SPY"

    def test_intent_id_present(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["intent_id"] == INTENT_ID

    def test_action_is_position_opened(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["action"] == "POSITION_OPENED"

    def test_mode_paper(self):
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["mode"] == "paper"


# ---------------------------------------------------------------------------
# Test 2: entry_debit_from_leg_quotes
# ---------------------------------------------------------------------------

class TestEntryDebitFromLegQuotes:
    """Test 2: entry_debit_gross computed correctly from leg quotes."""

    def test_entry_debit_gross_from_ask_bid(self):
        """entry_debit_gross = (buy_ask - sell_bid) * 100 = (2.09 - 1.54) * 100 = 55.0"""
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        # (2.09 - 1.54) * 100 = 55.0
        expected = round((2.09 - 1.54) * 100, 4)
        assert abs(row["entry_debit_gross"] - expected) < 0.001, \
            f"entry_debit_gross={row['entry_debit_gross']}, expected={expected}"

    def test_entry_debit_fallback_market_debit(self):
        """Falls back to market_debit * 100 when leg quotes missing."""
        from forecast_arb.allocator.fills import build_fill_row

        exec_result_no_quotes = dict(FIXTURE_EXEC_RESULT)
        exec_result_no_quotes["leg_quotes"] = []  # no quotes

        row = build_fill_row(exec_result_no_quotes, FIXTURE_INTENT, FIXED_DATE, "paper")
        # market_debit = 0.55, so entry_debit_gross = 55.0
        expected = round(0.5499999999999998 * 100, 4)
        assert abs(row["entry_debit_gross"] - expected) < 0.001, \
            f"entry_debit_gross={row['entry_debit_gross']}, expected={expected}"

    def test_entry_debit_none_when_no_data(self):
        """entry_debit_gross = None when no quotes and no market_debit."""
        from forecast_arb.allocator.fills import build_fill_row

        exec_result_empty = {
            **FIXTURE_EXEC_RESULT,
            "leg_quotes": [],
            "market_debit": None,
        }
        row = build_fill_row(exec_result_empty, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["entry_debit_gross"] is None


# ---------------------------------------------------------------------------
# Test 3: dedup_by_intent_id
# ---------------------------------------------------------------------------

class TestDedupByIntentId:
    """Test 3: Dedup by intent_id works — running twice adds only 1 fills row."""

    def test_second_run_dedup_skipped(self, tmp_path):
        from forecast_arb.allocator.fills import (
            build_fill_row,
            append_fills_ledger,
            read_fills_ledger,
        )

        fills_ledger = tmp_path / "fills.jsonl"
        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")

        # First run: appended
        appended1, reason1 = append_fills_ledger(fills_ledger, row)
        assert appended1 is True
        assert reason1 == "APPENDED"

        # Second run: dedup
        appended2, reason2 = append_fills_ledger(fills_ledger, row)
        assert appended2 is False
        assert reason2 == "DEDUP_SKIPPED"

        # Only 1 row in ledger
        rows = read_fills_ledger(fills_ledger)
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        assert rows[0]["intent_id"] == INTENT_ID

    def test_different_intent_ids_both_appended(self, tmp_path):
        """Two different intent_ids → 2 rows."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            append_fills_ledger,
            read_fills_ledger,
        )

        fills_ledger = tmp_path / "fills.jsonl"

        intent1 = {**FIXTURE_INTENT, "intent_id": "intent_aaa"}
        intent2 = {**FIXTURE_INTENT, "intent_id": "intent_bbb"}

        row1 = build_fill_row(FIXTURE_EXEC_RESULT, intent1, FIXED_DATE, "paper")
        row2 = build_fill_row(FIXTURE_EXEC_RESULT, intent2, FIXED_DATE, "paper")

        append_fills_ledger(fills_ledger, row1)
        append_fills_ledger(fills_ledger, row2)

        rows = read_fills_ledger(fills_ledger)
        assert len(rows) == 2

    def test_no_intent_id_always_appends(self, tmp_path):
        """No intent_id → dedup skipped (always append)."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            append_fills_ledger,
            read_fills_ledger,
        )

        fills_ledger = tmp_path / "fills.jsonl"
        intent_no_id = {**FIXTURE_INTENT, "intent_id": None}
        row = build_fill_row(FIXTURE_EXEC_RESULT, intent_no_id, FIXED_DATE, "paper")

        append_fills_ledger(fills_ledger, row)
        append_fills_ledger(fills_ledger, row)

        rows = read_fills_ledger(fills_ledger)
        assert len(rows) == 2  # Both appended (no dedup when intent_id=None)


# ---------------------------------------------------------------------------
# Test 4: positions_snapshot_correct
# ---------------------------------------------------------------------------

class TestPositionsSnapshotCorrect:
    """Test 4: positions.json derived correctly from fills ledger."""

    def test_positions_snapshot_has_one_position(self, tmp_path):
        from forecast_arb.allocator.fills import (
            build_fill_row,
            build_positions_snapshot,
        )

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        positions = build_positions_snapshot([row])

        assert len(positions) == 1
        pos = positions[0]
        assert pos["position_id"] == INTENT_ID
        assert pos["regime"] == "crash"
        assert pos["underlier"] == "SPY"
        assert pos["expiry"] == "20260417"
        assert pos["strikes"] == [575.0, 555.0]
        assert pos["qty_open"] == 1
        assert pos["mode"] == "paper"

    def test_positions_snapshot_dedup_by_intent_id(self, tmp_path):
        """build_positions_snapshot deduplicates rows with same intent_id."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            build_positions_snapshot,
        )

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        # Duplicate row (same intent_id)
        positions = build_positions_snapshot([row, row])
        assert len(positions) == 1, "Should dedup to 1 position"

    def test_positions_entry_debit_from_fill_row(self, tmp_path):
        """position.entry_debit_gross matches fill row."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            build_positions_snapshot,
        )

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        expected_debit = round((2.09 - 1.54) * 100, 4)
        positions = build_positions_snapshot([row])
        pos = positions[0]
        assert abs(pos["entry_debit_gross"] - expected_debit) < 0.001

    def test_positions_json_written_correctly(self, tmp_path):
        """write_positions_snapshot creates valid JSON file."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            build_positions_snapshot,
            write_positions_snapshot,
            read_positions_snapshot,
        )

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        positions = build_positions_snapshot([row])
        positions_path = tmp_path / "positions.json"

        write_positions_snapshot(positions_path, positions)
        assert positions_path.exists()

        loaded = read_positions_snapshot(positions_path)
        assert len(loaded) == 1
        assert loaded[0]["position_id"] == INTENT_ID

    def test_empty_positions_written_as_empty_list(self, tmp_path):
        """Empty positions.json is a valid empty list."""
        from forecast_arb.allocator.fills import (
            write_positions_snapshot,
            read_positions_snapshot,
        )

        positions_path = tmp_path / "positions.json"
        write_positions_snapshot(positions_path, [])
        loaded = read_positions_snapshot(positions_path)
        assert loaded == []

    def test_read_positions_returns_empty_if_missing(self, tmp_path):
        """read_positions_snapshot returns [] if file doesn't exist."""
        from forecast_arb.allocator.fills import read_positions_snapshot

        positions_path = tmp_path / "nonexistent.json"
        assert read_positions_snapshot(positions_path) == []


# ---------------------------------------------------------------------------
# Test 5: inventory_actual_uses_positions
# ---------------------------------------------------------------------------

class TestInventoryActualUsesPositions:
    """Test 5: inventory.actual uses positions.json and shows crash=1/1 after reconcile."""

    def _minimal_policy(self):
        return {"inventory_targets": {"crash": 1, "selloff": 1}}

    def test_inventory_from_positions_crash_one(self, tmp_path):
        """After reconcile with 1 crash position, crash_open=1."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            build_positions_snapshot,
            write_positions_snapshot,
        )
        from forecast_arb.allocator.inventory import compute_inventory_state_from_positions

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        positions = build_positions_snapshot([row])
        positions_path = tmp_path / "positions.json"
        write_positions_snapshot(positions_path, positions)

        policy = self._minimal_policy()
        inv = compute_inventory_state_from_positions(policy, positions_path)

        assert inv.crash_open == 1, f"crash_open={inv.crash_open}"
        assert inv.crash_target == 1
        assert inv.selloff_open == 0
        assert not inv.needs_open("crash")

    def test_inventory_from_positions_empty(self, tmp_path):
        """With empty positions.json, crash_open=0, needs_open=True."""
        from forecast_arb.allocator.fills import write_positions_snapshot
        from forecast_arb.allocator.inventory import compute_inventory_state_from_positions

        positions_path = tmp_path / "positions.json"
        write_positions_snapshot(positions_path, [])

        policy = self._minimal_policy()
        inv = compute_inventory_state_from_positions(policy, positions_path)

        assert inv.crash_open == 0
        assert inv.needs_open("crash")

    def test_inventory_with_positions_fallback_when_no_file(self, tmp_path):
        """compute_inventory_state_with_positions falls back to ledger if no positions.json."""
        from forecast_arb.allocator.inventory import compute_inventory_state_with_positions

        missing_positions = tmp_path / "missing_positions.json"
        missing_ledger = tmp_path / "missing_ledger.jsonl"

        policy = self._minimal_policy()
        # Should not raise; should return 0/0 open (empty ledger)
        inv = compute_inventory_state_with_positions(
            policy=policy,
            ledger_path=missing_ledger,
            positions_path=missing_positions,
        )
        assert inv.crash_open == 0
        assert inv.selloff_open == 0

    def test_inventory_prefers_positions_over_ledger(self, tmp_path):
        """When both positions.json and ledger exist, positions.json wins."""
        from forecast_arb.allocator.fills import (
            build_fill_row,
            build_positions_snapshot,
            write_positions_snapshot,
        )
        from forecast_arb.allocator.inventory import compute_inventory_state_with_positions

        # Write positions with 1 crash position
        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        positions = build_positions_snapshot([row])
        positions_path = tmp_path / "positions.json"
        write_positions_snapshot(positions_path, positions)

        # Ledger exists but is empty (would give crash_open=0 if read)
        empty_ledger = tmp_path / "empty_ledger.jsonl"
        empty_ledger.write_text("")

        policy = self._minimal_policy()
        inv = compute_inventory_state_with_positions(
            policy=policy,
            ledger_path=empty_ledger,
            positions_path=positions_path,
        )
        # Should use positions.json → crash_open=1
        assert inv.crash_open == 1


# ---------------------------------------------------------------------------
# Test 6: harvest_multiple_net_over_gross
# ---------------------------------------------------------------------------

class TestHarvestMultipleNetOverGross:
    """Test 6: harvest multiple uses entry_debit_net if present else entry_debit_gross."""

    def _make_sleeve_position(
        self,
        entry_debit: float = 55.0,
        entry_debit_net: float = None,
        mark_mid: float = 110.0,
    ):
        from forecast_arb.allocator.types import SleevePosition

        return SleevePosition(
            trade_id="test_trade",
            underlier="SPY",
            expiry="20260417",
            strikes=[575.0, 555.0],
            qty_open=1,
            regime="crash",
            entry_debit=entry_debit,
            mark_mid=mark_mid,
            dte=43,
            entry_debit_net=entry_debit_net,
        )

    def test_multiple_uses_gross_when_net_absent(self):
        """multiple = mark_mid / entry_debit when entry_debit_net is None."""
        pos = self._make_sleeve_position(entry_debit=55.0, entry_debit_net=None, mark_mid=110.0)
        assert pos.entry_debit_net is None
        assert abs(pos.multiple - 2.0) < 0.001, f"multiple={pos.multiple}"

    def test_multiple_uses_net_when_present(self):
        """multiple = mark_mid / entry_debit_net when entry_debit_net is not None."""
        # entry_debit_gross=55, entry_debit_net=60 (with commissions)
        pos = self._make_sleeve_position(entry_debit=55.0, entry_debit_net=60.0, mark_mid=120.0)
        # Should use net: 120 / 60 = 2.0
        assert abs(pos.multiple - 2.0) < 0.001, f"multiple={pos.multiple}"
        # NOT gross: 120 / 55 ≈ 2.18 (different)

    def test_multiple_net_over_gross_yields_lower_multiple(self):
        """Using net (higher cost with commissions) yields lower multiple vs gross."""
        pos_gross = self._make_sleeve_position(
            entry_debit=55.0, entry_debit_net=None, mark_mid=110.0
        )
        pos_net = self._make_sleeve_position(
            entry_debit=55.0, entry_debit_net=60.0, mark_mid=110.0
        )
        # Gross multiple: 110/55 = 2.0
        # Net multiple: 110/60 ≈ 1.83
        assert pos_net.multiple < pos_gross.multiple

    def test_multiple_none_when_mark_mid_missing(self):
        """multiple = None when mark_mid is None."""
        pos = self._make_sleeve_position(mark_mid=None)
        assert pos.multiple is None

    def test_multiple_none_when_both_debits_missing(self):
        """multiple = None when both entry_debit and entry_debit_net are None."""
        from forecast_arb.allocator.types import SleevePosition

        pos = SleevePosition(
            trade_id="t",
            underlier="SPY",
            expiry="20260417",
            strikes=[575.0, 555.0],
            qty_open=1,
            regime="crash",
            entry_debit=None,
            mark_mid=110.0,
            dte=43,
            entry_debit_net=None,
        )
        assert pos.multiple is None


# ---------------------------------------------------------------------------
# Test 7: intent_archival
# ---------------------------------------------------------------------------

class TestIntentArchival:
    """Test 7: Intent archival moves file(s) into _archive/YYYYMMDD."""

    def test_intent_archived_to_correct_dir(self, tmp_path):
        """OPEN_*.json moves to _archive/YYYYMMDD/OPEN_*.json."""
        from forecast_arb.allocator.fills import archive_intent_files

        intents_dir = tmp_path / "intents" / "allocator"
        intents_dir.mkdir(parents=True)
        archive_base = intents_dir / "_archive"

        intent_file = intents_dir / "OPEN_f4d3570c72fb.json"
        intent_file.write_text(json.dumps(FIXTURE_INTENT))

        date_str = FIXED_DATE  # "2026-03-05"
        archived = archive_intent_files(
            intent_path=intent_file,
            exec_result_path=None,
            archive_base=archive_base,
            date_str=date_str,
        )

        assert len(archived) == 1
        archive_dir = archive_base / "20260305"
        assert (archive_dir / "OPEN_f4d3570c72fb.json").exists()
        assert not intent_file.exists()  # source file removed

    def test_exec_result_archived_with_timestamp_suffix(self, tmp_path):
        """execution_result.json archived with timestamp suffix."""
        from forecast_arb.allocator.fills import archive_intent_files

        intents_dir = tmp_path / "intents" / "allocator"
        intents_dir.mkdir(parents=True)
        archive_base = intents_dir / "_archive"

        intent_file = intents_dir / "OPEN_abc.json"
        intent_file.write_text("{}")
        exec_result_file = intents_dir / "execution_result.json"
        exec_result_file.write_text("{}")

        date_str = FIXED_DATE
        archived = archive_intent_files(
            intent_path=intent_file,
            exec_result_path=exec_result_file,
            archive_base=archive_base,
            date_str=date_str,
        )

        assert len(archived) == 2
        archive_dir = archive_base / "20260305"
        assert (archive_dir / "OPEN_abc.json").exists()
        # Check at least one archived file starts with "execution_result"
        archived_names = [Path(p).name for p in archived]
        assert any(n.startswith("execution_result") for n in archived_names)

    def test_dry_run_does_not_archive(self, tmp_path):
        """dry_run=True: files not moved, archive paths returned."""
        from forecast_arb.allocator.fills import archive_intent_files

        intents_dir = tmp_path / "intents" / "allocator"
        intents_dir.mkdir(parents=True)

        intent_file = intents_dir / "OPEN_xyz.json"
        intent_file.write_text("{}")

        date_str = FIXED_DATE
        archived = archive_intent_files(
            intent_path=intent_file,
            exec_result_path=None,
            archive_base=intents_dir / "_archive",
            date_str=date_str,
            dry_run=True,
        )

        # File still exists (not moved)
        assert intent_file.exists()
        # But archived list is non-empty (would-be paths)
        assert len(archived) == 1

    def test_no_archive_if_intent_missing(self, tmp_path):
        """If intent file doesn't exist, archival is skipped gracefully."""
        from forecast_arb.allocator.fills import archive_intent_files

        intents_dir = tmp_path / "intents" / "allocator"
        intents_dir.mkdir(parents=True)

        intent_file = intents_dir / "OPEN_missing.json"  # doesn't exist

        archived = archive_intent_files(
            intent_path=intent_file,
            exec_result_path=None,
            archive_base=intents_dir / "_archive",
            date_str=FIXED_DATE,
        )

        assert archived == []


# ---------------------------------------------------------------------------
# Test 8: no_op_when_no_exec_result
# ---------------------------------------------------------------------------

class TestNoOpWhenNoExecResult:
    """Test 8: No-op when execution_result.json doesn't exist."""

    def test_no_fills_returns_zero(self, tmp_path):
        from forecast_arb.allocator.fills import run_reconcile

        empty_dir = tmp_path / "intents" / "allocator"
        empty_dir.mkdir(parents=True)

        result = run_reconcile(
            mode="paper",
            intents_dir=empty_dir,
            fills_ledger_path=tmp_path / "fills.jsonl",
            positions_path=tmp_path / "positions.json",
            archive_base_dir=empty_dir / "_archive",
        )

        assert result["fills_found"] == 0
        assert result["positions_opened"] == 0
        assert not (tmp_path / "fills.jsonl").exists()
        assert not (tmp_path / "positions.json").exists()


# ---------------------------------------------------------------------------
# Test 9: Full run_reconcile integration
# ---------------------------------------------------------------------------

class TestRunReconcileIntegration:
    """Integration tests for run_reconcile with fixture files."""

    def test_full_reconcile_writes_fills_and_positions(self, tmp_path):
        """Full reconcile with FILLED exec_result: fills ledger + positions.json written.

        v1.8: Uses _write_filled_fixture_files (no transmit=False) because
        FIXTURE_EXEC_RESULT has transmit=False which produces ORDER_STAGED, not POSITION_OPENED.
        """
        from forecast_arb.allocator.fills import run_reconcile, read_fills_ledger, read_positions_snapshot

        intents_dir = tmp_path / "intents" / "allocator"
        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        archive_base = intents_dir / "_archive"

        _write_filled_fixture_files(intents_dir)  # use "filled" exec_result

        result = run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        assert result["fills_found"] == 1
        assert result["positions_opened"] == 1
        assert result["dedup_skipped"] == 0

        # Fills ledger has 1 row
        rows = read_fills_ledger(fills_ledger)
        assert len(rows) == 1
        assert rows[0]["intent_id"] == INTENT_ID
        assert rows[0]["action"] == "POSITION_OPENED"

        # positions.json has 1 entry
        positions = read_positions_snapshot(positions_path)
        assert len(positions) == 1
        assert positions[0]["regime"] == "crash"

    def test_second_reconcile_is_dedup(self, tmp_path):
        """Running reconcile twice: second run is dedup, no new rows."""
        from forecast_arb.allocator.fills import run_reconcile, read_fills_ledger

        intents_dir = tmp_path / "intents" / "allocator"
        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        archive_base = intents_dir / "_archive"

        _write_fixture_files(intents_dir)

        # First run
        run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        # Restore execution_result.json from archive (simulating re-run)
        # Actually after first run, intent files are archived.
        # Rewrite them for second run.
        _write_fixture_files(intents_dir)

        # Second run: same intent_id → dedup
        result2 = run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        assert result2["dedup_skipped"] == 1
        assert result2["positions_opened"] == 0

        # Still only 1 row in fills ledger
        rows = read_fills_ledger(fills_ledger)
        assert len(rows) == 1

    def test_reconcile_archives_intent_files(self, tmp_path):
        """After reconcile with FILLED exec_result, OPEN_*.json archived."""
        from forecast_arb.allocator.fills import run_reconcile

        intents_dir = tmp_path / "intents" / "allocator"
        archive_base = intents_dir / "_archive"

        intent_path, _ = _write_filled_fixture_files(intents_dir)  # use filled

        run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=tmp_path / "fills.jsonl",
            positions_path=tmp_path / "positions.json",
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        # Intent file should be archived (only when POSITION_OPENED + positions written)
        assert not intent_path.exists(), "OPEN_*.json should be archived after fill"
        archive_dir = archive_base / "20260305"
        assert archive_dir.exists()
        assert any(archive_dir.iterdir()), "Archive dir should have files"


# ---------------------------------------------------------------------------
# Test 11: Fix 1 — ibkr.fills prices take priority over leg_quotes
# ---------------------------------------------------------------------------

class TestIbkrFillPriorityOverQuotes:
    """Fix 1: ibkr.fills fill prices used first; quotes only as fallback."""

    def test_ibkr_fills_override_quotes(self):
        """ibkr.fills present → use fill prices, not leg_quotes ask/bid."""
        from forecast_arb.allocator.fills import build_fill_row

        exec_result_with_fills = {
            **FIXTURE_EXEC_RESULT,
            "ibkr": {
                "orderId": 54,
                "permId": None,
                "conIds": [12345, 67890],
                "fills": [
                    {"conId": 12345, "side": "BUY", "price": 2.00, "qty": 1},
                    {"conId": 67890, "side": "SELL", "price": 1.50, "qty": 1},
                ],
            },
        }

        row = build_fill_row(exec_result_with_fills, FIXTURE_INTENT, FIXED_DATE, "paper")

        # ibkr fill prices: (2.00 - 1.50) * 100 = 50.0
        # quotes would give: (2.09 - 1.54) * 100 = 55.0 — should NOT use this
        expected_from_fills = round((2.00 - 1.50) * 100.0, 4)
        expected_from_quotes = round((2.09 - 1.54) * 100.0, 4)

        assert abs(row["entry_debit_gross"] - expected_from_fills) < 0.001, \
            f"Should use fill prices ({expected_from_fills}), got {row['entry_debit_gross']}"
        assert abs(row["entry_debit_gross"] - expected_from_quotes) > 0.1, \
            "Should NOT use quote prices when ibkr.fills present"

    def test_quotes_used_when_no_ibkr_fills(self):
        """When ibkr.fills is empty, fall back to leg_quotes."""
        from forecast_arb.allocator.fills import build_fill_row

        # No ibkr.fills (existing FIXTURE_EXEC_RESULT has no ibkr key)
        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        expected_from_quotes = round((2.09 - 1.54) * 100.0, 4)
        assert abs(row["entry_debit_gross"] - expected_from_quotes) < 0.001

    def test_ibkr_fills_partial_ignored_falls_through_to_quotes(self):
        """ibkr.fills with only BUY leg → can't compute, fall through to quotes."""
        from forecast_arb.allocator.fills import build_fill_row

        exec_result_partial_fills = {
            **FIXTURE_EXEC_RESULT,
            "ibkr": {
                "fills": [
                    {"side": "BUY", "price": 2.00},
                    # missing SELL fill
                ],
            },
        }
        row = build_fill_row(exec_result_partial_fills, FIXTURE_INTENT, FIXED_DATE, "paper")
        # Falls through to quotes: (2.09 - 1.54) * 100 = 55.0
        expected_from_quotes = round((2.09 - 1.54) * 100.0, 4)
        assert abs(row["entry_debit_gross"] - expected_from_quotes) < 0.001


# ---------------------------------------------------------------------------
# Test 12: Fix 2 — archive only when positions_written confirmed
# ---------------------------------------------------------------------------

class TestArchiveOnlyWhenPositionsWritten:
    """Fix 2: intents not archived unless both fills AND positions.json written."""

    def test_archive_happens_on_success(self, tmp_path):
        """Normal FILLED flow: fills+positions written → intent archived.

        v1.8: Uses _write_filled_fixture_files because staged orders (transmit=False)
        do not create POSITION_OPENED rows and therefore are not archived.
        """
        from forecast_arb.allocator.fills import run_reconcile

        intents_dir = tmp_path / "intents" / "allocator"
        archive_base = intents_dir / "_archive"
        _write_filled_fixture_files(intents_dir)  # use "filled" exec_result

        result = run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=tmp_path / "fills.jsonl",
            positions_path=tmp_path / "positions.json",
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        assert result["positions_opened"] == 1
        assert len(result["archived"]) > 0, "Intent should be archived after successful write"

    def test_archive_not_called_on_dedup(self, tmp_path):
        """On dedup (second run), fills not written → intent NOT re-archived."""
        from forecast_arb.allocator.fills import run_reconcile

        intents_dir = tmp_path / "intents" / "allocator"
        archive_base = intents_dir / "_archive"
        fills_ledger = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"

        # First run: successful
        _write_fixture_files(intents_dir)
        run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        # Restore files for second run
        _write_fixture_files(intents_dir)
        result2 = run_reconcile(
            mode="paper",
            intents_dir=intents_dir,
            fills_ledger_path=fills_ledger,
            positions_path=positions_path,
            archive_base_dir=archive_base,
            date_str=FIXED_DATE,
        )

        assert result2["dedup_skipped"] == 1
        assert result2["positions_opened"] == 0
        # archived list is empty since we didn't write anything new
        assert len(result2["archived"]) == 0


# ---------------------------------------------------------------------------
# Test 13: Fix 3 — position_id = intent_id (not hash of timestamps)
# ---------------------------------------------------------------------------

class TestPositionIdIsIntentId:
    """Fix 3: position_id must equal intent_id (stable, not time-based hash)."""

    def test_position_id_equals_intent_id(self, tmp_path):
        """position_id == intent_id when intent has intent_id."""
        from forecast_arb.allocator.fills import build_fill_row, build_positions_snapshot

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        positions = build_positions_snapshot([row])

        assert len(positions) == 1
        assert positions[0]["position_id"] == INTENT_ID, \
            f"position_id should be intent_id={INTENT_ID}, got {positions[0]['position_id']}"

    def test_position_id_is_stable_string_when_no_intent_id(self, tmp_path):
        """When intent_id is None, position_id is a stable string (not SHA1 hash of timestamp)."""
        from forecast_arb.allocator.fills import build_fill_row, build_positions_snapshot

        intent_no_id = {**FIXTURE_INTENT, "intent_id": None}
        row = build_fill_row(FIXTURE_EXEC_RESULT, intent_no_id, FIXED_DATE, "paper")
        positions = build_positions_snapshot([row])

        assert len(positions) == 1
        pos_id = positions[0]["position_id"]

        # Must contain symbol, expiry, strikes — NOT a hex hash of timestamp
        assert "SPY" in pos_id, f"Stable key should contain underlier, got: {pos_id}"
        assert "20260417" in pos_id, f"Stable key should contain expiry, got: {pos_id}"
        # Verify it's not a 16-char hex hash (the old bad fallback)
        assert not (len(pos_id) == 16 and all(c in "0123456789abcdef" for c in pos_id)), \
            f"position_id should NOT be a short hex hash, got: {pos_id}"

    def test_position_id_stable_across_runs(self, tmp_path):
        """Stable string is deterministic — same position_id on repeated builds."""
        from forecast_arb.allocator.fills import build_fill_row, build_positions_snapshot

        intent_no_id = {**FIXTURE_INTENT, "intent_id": None}

        row1 = build_fill_row(FIXTURE_EXEC_RESULT, intent_no_id, FIXED_DATE, "paper")
        row2 = build_fill_row(FIXTURE_EXEC_RESULT, intent_no_id, FIXED_DATE, "paper")

        pos1 = build_positions_snapshot([row1])
        pos2 = build_positions_snapshot([row2])

        assert pos1[0]["position_id"] == pos2[0]["position_id"], \
            "position_id must be identical across builds without intent_id"

    def test_fills_row_intent_id_preserved(self):
        """build_fill_row preserves intent_id from intent dict in the fills row."""
        from forecast_arb.allocator.fills import build_fill_row

        row = build_fill_row(FIXTURE_EXEC_RESULT, FIXTURE_INTENT, FIXED_DATE, "paper")
        assert row["intent_id"] == INTENT_ID


# ---------------------------------------------------------------------------
# Test 10: policy.get_positions_path
# ---------------------------------------------------------------------------

class TestGetPositionsPath:
    """Test 10: policy.get_positions_path returns correct path."""

    def test_default_positions_path(self):
        """Default output_dir → runs/allocator/positions.json."""
        from forecast_arb.allocator.policy import get_positions_path

        policy = {"output_dir": "runs/allocator"}
        p = get_positions_path(policy)
        assert p == Path("runs/allocator/positions.json")

    def test_custom_output_dir(self):
        """Custom output_dir respected."""
        from forecast_arb.allocator.policy import get_positions_path

        policy = {"output_dir": "tmp/test_run"}
        p = get_positions_path(policy)
        assert p == Path("tmp/test_run/positions.json")

    def test_fills_ledger_path(self):
        """get_fills_ledger_path returns correct path."""
        from forecast_arb.allocator.policy import get_fills_ledger_path

        policy = {"output_dir": "runs/allocator"}
        p = get_fills_ledger_path(policy)
        assert p == Path("runs/allocator/allocator_fills_ledger.jsonl")
