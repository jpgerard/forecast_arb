"""
Tests for forecast_arb/allocator/broker_sync.py

All tests are fully deterministic, use tmp_path fixtures, and never touch
real ledger files on disk.

Coverage:
  Unit — build_ibkr_import_fill_row()
    - Valid combo with all fields
    - position_id is deterministic and stable
    - Source field == "ibkr_import"
    - Normalises YYYY-MM-DD expiry to YYYYMMDD
    - Accepts "underlier" key instead of "symbol"
    - long_strike must be > short_strike (ValueError)
    - Missing symbol raises ValueError
    - Missing/invalid expiry raises ValueError
    - Missing strikes raises ValueError
    - entry_debit=None is preserved as None
    - entry_debit<=0 is coerced to None
    - Unknown regime coerces to "crash" (with warning)
    - qty<=0 coerces to 1

  Unit — _ibkr_import_position_id()
    - Deterministic across calls
    - Includes all four fields
    - No collision with realistic CCC intent IDs (hex digest format)

  Orchestration — sync_ibkr_positions()
    - Single combo → fills ledger appended + positions.json rebuilt
    - Three combos (the real IBKR spreads) → positions.json shows all three
    - Idempotent: second call with same combos → imported=0, skipped_dedup=3
    - dry_run=True → no files written, positions_preview populated
    - Invalid combo in batch → error recorded, valid combos still imported
    - Positions.json not touched when all combos already present (zero-write)

  Required specs (from task description):
  ① TestImportedBrokerSpreadVisibleInPositionsSnapshot
      - imported spread appears in build_positions_snapshot output
  ② TestOpenCrashCountMatchesImportedPositions
      - import 3 crash spreads → compute_inventory_state_from_positions crash_open == 3
  ③ TestReportInventoryCountConsistentAfterSync
      - run_report Section B shows crash open == 3 after sync

  Diagnostics — diff_ibkr_vs_positions()
    - All three spreads missing from empty positions.json
    - All three matched after sync
    - Extra in CCC not in IBKR list detected
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from forecast_arb.allocator.broker_sync import (
    _contract_key_from_fills_row,
    _dedup_fills_rows_by_contract,
    _ibkr_debit_enrich_position_id,
    _ibkr_import_position_id,
    build_ibkr_import_fill_row,
    diff_ibkr_vs_positions,
    sync_ibkr_positions,
)
from forecast_arb.allocator.fills import (
    build_positions_snapshot,
    read_fills_ledger,
    read_positions_snapshot,
)
from forecast_arb.allocator.inventory import compute_inventory_state_from_positions

# ---------------------------------------------------------------------------
# Fixtures — the three live IBKR spreads from the task description
# ---------------------------------------------------------------------------

#: The three real IBKR spreads that triggered this patch.
LIVE_IBKR_COMBOS: List[Dict[str, Any]] = [
    {
        "symbol": "SPY",
        "expiry": "20260417",
        "long_strike": 575.0,
        "short_strike": 555.0,
        "qty": 1,
        "regime": "crash",
        "entry_debit": None,
    },
    {
        "symbol": "SPY",
        "expiry": "20260327",
        "long_strike": 590.0,
        "short_strike": 570.0,
        "qty": 1,
        "regime": "crash",
        "entry_debit": None,
    },
    {
        "symbol": "SPY",
        "expiry": "20260320",
        "long_strike": 590.0,
        "short_strike": 570.0,
        "qty": 1,
        "regime": "crash",
        "entry_debit": None,
    },
]

#: Minimal policy for inventory state (crash target=3 for this test suite)
_POLICY_CRASH3: Dict[str, Any] = {
    "inventory_targets": {"crash": 3, "selloff": 1},
}
_POLICY_CRASH1: Dict[str, Any] = {
    "inventory_targets": {"crash": 1, "selloff": 1},
}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ===========================================================================
# Unit: _ibkr_import_position_id
# ===========================================================================

class TestIbkrImportPositionId:
    def test_format(self):
        pid = _ibkr_import_position_id("SPY", "20260417", 575.0, 555.0)
        assert pid == "ibkr_import_SPY_20260417_575_555"

    def test_deterministic(self):
        a = _ibkr_import_position_id("SPY", "20260417", 575.0, 555.0)
        b = _ibkr_import_position_id("SPY", "20260417", 575.0, 555.0)
        assert a == b

    def test_upper_cases_underlier(self):
        pid = _ibkr_import_position_id("spy", "20260417", 575.0, 555.0)
        assert "SPY" in pid

    def test_strips_dashes_from_expiry(self):
        pid = _ibkr_import_position_id("SPY", "2026-04-17", 575.0, 555.0)
        assert "20260417" in pid
        assert "-" not in pid

    def test_no_collision_with_hex_digest_intent(self):
        """CCC real intent IDs look like 'a8c68f63724a0db6' — no ibkr_import_ prefix."""
        pid = _ibkr_import_position_id("SPY", "20260417", 575.0, 555.0)
        assert pid.startswith("ibkr_import_")
        # Real intent IDs (SHA1 hex) are 16 hex chars; test they can't collide
        real_intent_id = "a8c68f63724a0db6"
        assert pid != real_intent_id

    def test_three_live_spreads_are_distinct(self):
        """The three actual IBKR spreads must produce distinct keys."""
        keys = [
            _ibkr_import_position_id("SPY", "20260417", 575.0, 555.0),
            _ibkr_import_position_id("SPY", "20260327", 590.0, 570.0),
            _ibkr_import_position_id("SPY", "20260320", 590.0, 570.0),
        ]
        assert len(set(keys)) == 3, f"Keys should be distinct, got: {keys}"


# ===========================================================================
# Unit: build_ibkr_import_fill_row
# ===========================================================================

class TestBuildIbkrImportFillRow:
    _DATE = "2026-03-09"
    _MODE = "live"

    def _build(self, combo):
        return build_ibkr_import_fill_row(combo, self._DATE, self._MODE)

    # --- Happy path ---

    def test_returns_position_opened_action(self):
        combo = LIVE_IBKR_COMBOS[0]
        row = self._build(combo)
        assert row["action"] == "POSITION_OPENED"

    def test_source_is_ibkr_import(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert row["source"] == "ibkr_import"

    def test_intent_id_is_stable_key(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert row["intent_id"] == "ibkr_import_SPY_20260417_575_555"

    def test_underlier_uppercased(self):
        combo = {**LIVE_IBKR_COMBOS[0], "symbol": "spy"}
        row = self._build(combo)
        assert row["underlier"] == "SPY"

    def test_expiry_dashes_stripped(self):
        combo = {**LIVE_IBKR_COMBOS[0], "expiry": "2026-04-17"}
        row = self._build(combo)
        assert row["expiry"] == "20260417"
        assert "-" not in row["expiry"]

    def test_strikes_order_preserved_long_first(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert row["strikes"] == [575.0, 555.0]

    def test_qty_default_one(self):
        combo = {k: v for k, v in LIVE_IBKR_COMBOS[0].items() if k != "qty"}
        row = self._build(combo)
        assert row["qty"] == 1

    def test_regime_default_crash_when_absent(self):
        combo = {k: v for k, v in LIVE_IBKR_COMBOS[0].items() if k != "regime"}
        row = self._build(combo)
        assert row["regime"] == "crash"

    def test_regime_crash_explicit(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert row["regime"] == "crash"

    def test_entry_debit_none_when_absent(self):
        combo = {k: v for k, v in LIVE_IBKR_COMBOS[0].items() if k != "entry_debit"}
        row = self._build(combo)
        assert row["entry_debit_gross"] is None

    def test_entry_debit_stored_when_positive(self):
        combo = {**LIVE_IBKR_COMBOS[0], "entry_debit": 65.0}
        row = self._build(combo)
        assert row["entry_debit_gross"] == pytest.approx(65.0)

    def test_entry_debit_zero_coerced_to_none(self):
        combo = {**LIVE_IBKR_COMBOS[0], "entry_debit": 0.0}
        row = self._build(combo)
        assert row["entry_debit_gross"] is None

    def test_entry_debit_negative_coerced_to_none(self):
        combo = {**LIVE_IBKR_COMBOS[0], "entry_debit": -5.0}
        row = self._build(combo)
        assert row["entry_debit_gross"] is None

    def test_accepts_underlier_key_instead_of_symbol(self):
        combo = {k if k != "symbol" else "underlier": v for k, v in LIVE_IBKR_COMBOS[0].items()}
        row = self._build(combo)
        assert row["underlier"] == "SPY"

    def test_date_and_mode_populated(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert row["date"] == self._DATE
        assert row["mode"] == self._MODE

    def test_contains_import_note(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert "_import_note" in row
        assert "ibkr" in row["_import_note"].lower() or "IBKR" in row["_import_note"]

    # --- Three live spreads individually ---

    def test_spread_apr17_575_555(self):
        row = self._build(LIVE_IBKR_COMBOS[0])
        assert row["underlier"] == "SPY"
        assert row["expiry"] == "20260417"
        assert row["strikes"] == [575.0, 555.0]
        assert row["regime"] == "crash"
        assert row["intent_id"] == "ibkr_import_SPY_20260417_575_555"

    def test_spread_mar27_590_570(self):
        row = self._build(LIVE_IBKR_COMBOS[1])
        assert row["expiry"] == "20260327"
        assert row["strikes"] == [590.0, 570.0]
        assert row["intent_id"] == "ibkr_import_SPY_20260327_590_570"

    def test_spread_mar20_590_570(self):
        row = self._build(LIVE_IBKR_COMBOS[2])
        assert row["expiry"] == "20260320"
        assert row["strikes"] == [590.0, 570.0]
        assert row["intent_id"] == "ibkr_import_SPY_20260320_590_570"

    # --- Validation errors ---

    def test_missing_symbol_raises(self):
        combo = {k: v for k, v in LIVE_IBKR_COMBOS[0].items()
                 if k not in ("symbol", "underlier")}
        with pytest.raises(ValueError, match="missing"):
            self._build(combo)

    def test_missing_expiry_raises(self):
        combo = {k: v for k, v in LIVE_IBKR_COMBOS[0].items() if k != "expiry"}
        with pytest.raises(ValueError):
            self._build(combo)

    def test_invalid_expiry_length_raises(self):
        combo = {**LIVE_IBKR_COMBOS[0], "expiry": "2026-04"}
        with pytest.raises(ValueError):
            self._build(combo)

    def test_missing_strikes_raises(self):
        combo = {k: v for k, v in LIVE_IBKR_COMBOS[0].items()
                 if k not in ("long_strike", "short_strike")}
        with pytest.raises(ValueError):
            self._build(combo)

    def test_long_not_greater_than_short_raises(self):
        combo = {**LIVE_IBKR_COMBOS[0], "long_strike": 555.0, "short_strike": 575.0}
        with pytest.raises(ValueError, match="long_strike"):
            self._build(combo)

    def test_equal_strikes_raises(self):
        combo = {**LIVE_IBKR_COMBOS[0], "long_strike": 575.0, "short_strike": 575.0}
        with pytest.raises(ValueError):
            self._build(combo)

    def test_unknown_regime_coerces_to_crash(self):
        combo = {**LIVE_IBKR_COMBOS[0], "regime": "gamma_squeeze"}
        row = self._build(combo)
        assert row["regime"] == "crash"

    def test_qty_zero_coerces_to_one(self):
        combo = {**LIVE_IBKR_COMBOS[0], "qty": 0}
        row = self._build(combo)
        assert row["qty"] == 1

    def test_qty_negative_coerces_to_one(self):
        combo = {**LIVE_IBKR_COMBOS[0], "qty": -3}
        row = self._build(combo)
        assert row["qty"] == 1


# ===========================================================================
# Orchestration: sync_ibkr_positions
# ===========================================================================

class TestSyncIbkrPositions:
    """Tests for sync_ibkr_positions() orchestration function."""

    def _run(self, tmp_path, combos, *, dry_run=False, mode="live"):
        fills_path = tmp_path / "fills.jsonl"
        positions_path = tmp_path / "positions.json"
        return sync_ibkr_positions(
            combos=combos,
            fills_ledger_path=fills_path,
            positions_path=positions_path,
            mode=mode,
            date_str="2026-03-09",
            dry_run=dry_run,
        ), fills_path, positions_path

    def test_single_combo_appended(self, tmp_path):
        result, fills_path, pos_path = self._run(tmp_path, [LIVE_IBKR_COMBOS[0]])
        assert result["imported"] == 1
        assert result["errors"] == []
        assert fills_path.exists()

    def test_positions_json_written(self, tmp_path):
        result, fills_path, pos_path = self._run(tmp_path, [LIVE_IBKR_COMBOS[0]])
        assert result["positions_written"] is True
        assert pos_path.exists()
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1

    def test_three_live_spreads_all_imported(self, tmp_path):
        result, fills_path, pos_path = self._run(tmp_path, LIVE_IBKR_COMBOS)
        assert result["imported"] == 3
        assert result["skipped_dedup"] == 0
        assert result["errors"] == []
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 3

    def test_idempotent_second_import_no_changes(self, tmp_path):
        """Second identical import must be a strict no-op."""
        self._run(tmp_path, LIVE_IBKR_COMBOS)

        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        # Record mtime before second run
        fills_mtime_before = fills_path.stat().st_mtime
        pos_mtime_before = pos_path.stat().st_mtime

        result2, _, _ = self._run(tmp_path, LIVE_IBKR_COMBOS)
        assert result2["imported"] == 0
        assert result2["skipped_dedup"] == 3
        assert result2["positions_written"] is False

        # Files must not have been touched
        assert fills_path.stat().st_mtime == fills_mtime_before
        assert pos_path.stat().st_mtime == pos_mtime_before

    def test_dry_run_writes_no_files(self, tmp_path):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        result, _, _ = self._run(tmp_path, LIVE_IBKR_COMBOS, dry_run=True)
        assert result["imported"] == 3
        assert result["fills_written"] is False
        assert result["positions_written"] is False
        assert not fills_path.exists()
        assert not pos_path.exists()

    def test_dry_run_populates_positions_preview(self, tmp_path):
        result, _, _ = self._run(tmp_path, LIVE_IBKR_COMBOS, dry_run=True)
        assert "positions_preview" in result
        preview = result["positions_preview"]
        assert len(preview) == 3

    def test_invalid_combo_error_recorded_valid_ones_imported(self, tmp_path):
        """A bad combo in a batch should not abort the whole batch."""
        bad_combo = {"symbol": "SPY", "expiry": "20260417", "long_strike": 555.0, "short_strike": 575.0}
        combos = [LIVE_IBKR_COMBOS[0], bad_combo, LIVE_IBKR_COMBOS[1]]
        result, _, pos_path = self._run(tmp_path, combos)
        assert result["imported"] == 2
        assert len(result["errors"]) == 1
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 2

    def test_fills_ledger_rows_are_position_opened(self, tmp_path):
        """All written rows must have action=POSITION_OPENED for downstream compat."""
        self._run(tmp_path, LIVE_IBKR_COMBOS)
        rows = read_fills_ledger(tmp_path / "fills.jsonl")
        for row in rows:
            assert row["action"] == "POSITION_OPENED"

    def test_fills_ledger_source_is_ibkr_import(self, tmp_path):
        self._run(tmp_path, LIVE_IBKR_COMBOS)
        rows = read_fills_ledger(tmp_path / "fills.jsonl")
        for row in rows:
            assert row["source"] == "ibkr_import"

    def test_existing_ccc_fills_preserved(self, tmp_path):
        """Importing IBKR spreads must not disturb pre-existing CCC fills."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        existing_row = {
            "date": "2026-03-01",
            "timestamp_utc": "2026-03-01T10:00:00Z",
            "action": "POSITION_OPENED",
            "policy_id": "ccc_v1",
            "mode": "live",
            "intent_id": "existing_ccc_intent_abc123",
            "intent_path": None,
            "candidate_id": "cand_xyz",
            "regime": "crash",
            "underlier": "SPY",
            "expiry": "20260601",
            "strikes": [560.0, 540.0],
            "qty": 1,
            "entry_debit_gross": 72.0,
            "entry_debit_net": None,
            "commissions": None,
            "ibkr": {"orderId": 1001, "permId": None, "conIds": [], "fills": []},
            "source": "execution_result",
        }
        # Write existing fill
        fills_path.parent.mkdir(parents=True, exist_ok=True)
        with open(fills_path, "w") as f:
            f.write(json.dumps(existing_row) + "\n")

        result = sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        # CCC fill + 3 IBKR imports = 4 total positions
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 4

        # Original CCC fill still present
        ccc_pos = [p for p in positions if p.get("position_id") == "existing_ccc_intent_abc123"]
        assert len(ccc_pos) == 1
        assert ccc_pos[0]["source"] == "execution_result"

    def test_invalid_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="mode"):
            sync_ibkr_positions(
                combos=LIVE_IBKR_COMBOS,
                fills_ledger_path=tmp_path / "f.jsonl",
                positions_path=tmp_path / "p.json",
                mode="yolo",
            )

    def test_positions_json_not_written_when_all_dedup(self, tmp_path):
        """If every combo is already present, positions.json must not be modified."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        # First import
        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )
        pos_mtime = pos_path.stat().st_mtime

        # Second import — all dedup
        result = sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )
        assert result["positions_written"] is False
        assert pos_path.stat().st_mtime == pos_mtime


# ===========================================================================
# ① TestImportedBrokerSpreadVisibleInPositionsSnapshot
# ===========================================================================

class TestImportedBrokerSpreadVisibleInPositionsSnapshot:
    """
    REQUIRED SPEC ①
    An imported broker spread must appear in the positions.json snapshot
    produced by build_positions_snapshot (the same function used by all
    downstream readers).
    """

    def test_single_imported_spread_appears_in_snapshot(self, tmp_path):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=[LIVE_IBKR_COMBOS[0]],
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1
        pos = positions[0]
        assert pos["underlier"] == "SPY"
        assert pos["expiry"] == "20260417"
        assert pos["strikes"] == [575.0, 555.0]
        assert pos["regime"] == "crash"
        assert pos["source"] == "ibkr_import"
        assert pos["qty_open"] == 1

    def test_three_live_spreads_all_visible_in_snapshot(self, tmp_path):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 3

        expiries = {p["expiry"] for p in positions}
        assert expiries == {"20260417", "20260327", "20260320"}

    def test_snapshot_position_ids_are_stable_import_keys(self, tmp_path):
        """Position IDs must match the deterministic ibkr_import_ format."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        positions = read_positions_snapshot(pos_path)
        for pos in positions:
            assert pos["position_id"].startswith("ibkr_import_")

    def test_build_positions_snapshot_picks_up_import_rows(self, tmp_path):
        """
        Test that build_positions_snapshot (pure function) correctly includes
        ibkr_import POSITION_OPENED rows — confirming no existing code changes needed.
        """
        row = build_ibkr_import_fill_row(LIVE_IBKR_COMBOS[0], "2026-03-09", "live")
        positions = build_positions_snapshot([row])
        assert len(positions) == 1
        assert positions[0]["underlier"] == "SPY"
        assert positions[0]["source"] == "ibkr_import"

    def test_snapshot_excludes_non_position_opened_rows(self, tmp_path):
        """
        ORDER_STAGED rows (from staged pipeline orders) must NOT appear
        in the snapshot — import rows do not produce ORDER_STAGED rows.
        """
        staged_row = {
            "action": "ORDER_STAGED",
            "intent_id": "staged_001",
            "regime": "crash",
            "underlier": "SPY",
            "expiry": "20260601",
            "strikes": [560.0, 540.0],
            "qty": 1,
        }
        import_row = build_ibkr_import_fill_row(LIVE_IBKR_COMBOS[0], "2026-03-09", "live")
        positions = build_positions_snapshot([staged_row, import_row])
        # Only the POSITION_OPENED row
        assert len(positions) == 1
        assert positions[0]["source"] == "ibkr_import"


# ===========================================================================
# ② TestOpenCrashCountMatchesImportedPositions
# ===========================================================================

class TestOpenCrashCountMatchesImportedPositions:
    """
    REQUIRED SPEC ②
    After importing the 3 live IBKR bear put spread positions,
    compute_inventory_state_from_positions() must report crash_open == 3.
    """

    def test_crash_count_zero_before_import(self, tmp_path):
        pos_path = tmp_path / "empty_positions.json"
        _write_json(pos_path, [])
        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.crash_open == 0

    def test_crash_count_three_after_importing_three_spreads(self, tmp_path):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.crash_open == 3

    def test_selloff_count_unaffected_by_crash_import(self, tmp_path):
        """Importing crash spreads must not inflate selloff count."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.selloff_open == 0

    def test_mixed_regime_counts_accurate(self, tmp_path):
        """If one combo is 'selloff', the counts must split correctly."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        combos = [
            *LIVE_IBKR_COMBOS[:2],
            {**LIVE_IBKR_COMBOS[2], "regime": "selloff"},
        ]

        sync_ibkr_positions(
            combos=combos,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        policy = {"inventory_targets": {"crash": 3, "selloff": 2}}
        inv = compute_inventory_state_from_positions(policy, pos_path)
        assert inv.crash_open == 2
        assert inv.selloff_open == 1

    def test_position_file_missing_returns_zero(self, tmp_path):
        """Missing positions.json must not crash; returns 0."""
        pos_path = tmp_path / "nonexistent_positions.json"
        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.crash_open == 0

    def test_crash_open_below_target_indicates_gating_allowed_before_sync(self, tmp_path):
        """
        Before sync: crash_open=1 vs target=3 → needs_open("crash") is True
        (illustrating the pre-sync state where allocator thinks inventory is low
        and would incorrectly consider opening new spreads).
        """
        pos_path = tmp_path / "positions.json"
        _write_json(pos_path, [
            {
                "position_id": "existing_001",
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20260601",
                "strikes": [560.0, 540.0],
                "qty_open": 1,
            }
        ])
        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.crash_open == 1
        assert inv.needs_open("crash") is True   # ← stale state that caused the mismatch

    def test_crash_open_at_target_after_sync_blocks_new_opens(self, tmp_path):
        """
        After sync: crash_open == target → needs_open("crash") is False
        (the gating condition that prevents over-positioning now works correctly).
        """
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.crash_open == 3
        assert inv.needs_open("crash") is False   # ← correct post-sync gating


# ===========================================================================
# ③ TestReportInventoryCountConsistentAfterSync
# ===========================================================================

class TestReportInventoryCountConsistentAfterSync:
    """
    REQUIRED SPEC ③
    After running the broker-state sync, ccc_report.run_report() Section B
    must show crash open == 3, making the report consistent with IBKR truth.
    """

    def _capture_report(self, **kwargs) -> str:
        from scripts.ccc_report import run_report
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_report(**kwargs)
        return buf.getvalue()

    def test_report_shows_zero_crash_before_sync(self, tmp_path):
        """Before sync: empty positions.json → crash count = 0."""
        pos_path = tmp_path / "positions.json"
        _write_json(pos_path, [])

        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )
        # Crash open = 0 before sync
        assert "Crash open positions:" in out
        # Verify the count shown is 0 (not 3)
        lines = out.splitlines()
        crash_line = next(l for l in lines if "Crash open positions:" in l)
        # The value after the label should be 0
        assert crash_line.strip().endswith("0"), (
            f"Expected crash_open=0 before sync, got line: {crash_line!r}"
        )

    def test_report_shows_three_crash_positions_after_sync(self, tmp_path):
        """After sync: positions.json has 3 crash entries → report shows 3."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )

        assert "Crash open positions:" in out
        lines = out.splitlines()
        crash_line = next(l for l in lines if "Crash open positions:" in l)
        assert crash_line.strip().endswith("3"), (
            f"Expected crash_open=3 after sync, got line: {crash_line!r}"
        )

    def test_report_section_a_shows_all_three_spy_spreads(self, tmp_path):
        """Section A table must contain a row for each imported spread."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )

        assert "3 open position(s)" in out
        # Each spread's strike pair must appear
        assert "575/555" in out   # Apr 17 spread
        assert "590/570" in out   # Mar 27 and Mar 20 (both 590/570)

    def test_report_section_b_total_count_consistent_with_section_a(self, tmp_path):
        """Section B counts must match Section A table row count."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        out = self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=tmp_path / "fills.jsonl",
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )

        # Section A: "3 open position(s)"
        assert "3 open position(s)" in out
        # Section B: crash = 3, selloff = 0
        lines = out.splitlines()
        crash_line = next(l for l in lines if "Crash open positions:" in l)
        selloff_line = next(l for l in lines if "Selloff open positions:" in l)
        assert crash_line.strip().endswith("3")
        assert selloff_line.strip().endswith("0")

    def test_report_does_not_mutate_files(self, tmp_path):
        """run_report must be read-only; positions.json must not change."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        pos_mtime_before = pos_path.stat().st_mtime

        self._capture_report(
            positions_path=pos_path,
            commit_ledger_path=tmp_path / "commit.jsonl",
            fills_ledger_path=fills_path,
            actions_path=tmp_path / "actions.json",
            policy_path=tmp_path / "policy.yaml",
        )

        assert pos_path.stat().st_mtime == pos_mtime_before, (
            "run_report must not modify positions.json"
        )


# ===========================================================================
# Diagnostics: diff_ibkr_vs_positions
# ===========================================================================

class TestDiffIbkrVsPositions:
    """Tests for the diagnostic diff helper."""

    def test_all_missing_from_empty_positions(self, tmp_path):
        pos_path = tmp_path / "positions.json"
        _write_json(pos_path, [])

        diff = diff_ibkr_vs_positions(LIVE_IBKR_COMBOS, pos_path)
        assert len(diff["missing_from_ccc"]) == 3
        assert len(diff["matched"]) == 0
        assert diff["ibkr_count"] == 3
        assert diff["ccc_count"] == 0

    def test_all_matched_after_sync(self, tmp_path):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        diff = diff_ibkr_vs_positions(LIVE_IBKR_COMBOS, pos_path)
        assert len(diff["missing_from_ccc"]) == 0
        assert len(diff["matched"]) == 3

    def test_missing_positions_file_returns_all_missing(self, tmp_path):
        pos_path = tmp_path / "nonexistent_positions.json"
        diff = diff_ibkr_vs_positions(LIVE_IBKR_COMBOS, pos_path)
        assert len(diff["missing_from_ccc"]) == 3

    def test_partial_match_shows_both_sides(self, tmp_path):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        # Import only first spread
        sync_ibkr_positions(
            combos=[LIVE_IBKR_COMBOS[0]],
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        diff = diff_ibkr_vs_positions(LIVE_IBKR_COMBOS, pos_path)
        assert len(diff["matched"]) == 1
        assert len(diff["missing_from_ccc"]) == 2


# ===========================================================================
# NEW: TestContractLevelDedup
# ===========================================================================

class TestContractLevelDedup:
    """
    Root cause: import previously used intent_id-only dedup.  When a CCC-executed
    fill (intent_id="a3ea604e...") and an ibkr_import row had the same contract
    but different intent_ids, BOTH passed through → duplicate position.

    These tests verify the contract-level pre-filter introduced in this patch.
    """

    # --- helper: a minimal CCC-executed fills row for SPY 20260417 575/555 ---
    _CCC_ROW_APR17 = {
        "date": "2026-03-01",
        "timestamp_utc": "2026-03-01T10:00:00Z",
        "action": "POSITION_OPENED",
        "policy_id": "ccc_v1",
        "mode": "live",
        "intent_id": "a3ea604eecd990fc918733550d947fe620a93514",   # real CCC UUID
        "intent_path": None,
        "candidate_id": "cand_apr17",
        "regime": "crash",
        "underlier": "SPY",
        "expiry": "20260417",
        "strikes": [575.0, 555.0],
        "qty": 1,
        "entry_debit_gross": 68.50,
        "entry_debit_net": None,
        "commissions": None,
        "ibkr": {"orderId": 1001, "permId": None, "conIds": [], "fills": []},
        "source": "execution_result",
    }

    def _seed_fills_with_ccc_row(self, fills_path: Path) -> None:
        fills_path.parent.mkdir(parents=True, exist_ok=True)
        with open(fills_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._CCC_ROW_APR17) + "\n")

    def test_ccc_executed_contract_blocks_same_contract_import(self, tmp_path):
        """
        SPEC: If a CCC-executed POSITION_OPENED row exists for SPY 20260417 575/555,
        importing ibkr_import_SPY_20260417_575_555 MUST be skipped — not appended.
        """
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        self._seed_fills_with_ccc_row(fills_path)

        # Try to import the same contract (SPY 20260417 575/555) + 2 others
        result = sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,   # includes SPY 20260417 575/555
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        # SPY 20260417 must be skipped (contract dedup); remaining 2 imported
        assert result["imported"] == 2, (
            f"Expected 2 imports (two spreads not in ledger), got {result['imported']}"
        )
        assert result["skipped_dedup"] == 1, (
            f"Expected 1 contract-level skip for SPY 20260417 575/555"
        )

    def test_no_duplicate_position_when_contract_already_tracked(self, tmp_path):
        """
        SPEC: positions.json must contain exactly 3 entries, not 4, when
        one of the 3 IBKR combos matches an existing CCC-executed position.
        """
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        self._seed_fills_with_ccc_row(fills_path)

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 3, (
            f"Expected 3 positions (1 CCC + 2 ibkr_import), got {len(positions)}: "
            f"{[p.get('position_id') for p in positions]}"
        )

    def test_open_crash_count_correct_no_duplicate(self, tmp_path):
        """
        After import with pre-existing CCC fill, crash_open == 3 (not 4).
        """
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        self._seed_fills_with_ccc_row(fills_path)

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        inv = compute_inventory_state_from_positions(_POLICY_CRASH3, pos_path)
        assert inv.crash_open == 3

    def test_ccc_position_entry_debit_preserved_not_overwritten(self, tmp_path):
        """
        The CCC-executed position (entry_debit_gross=68.50) must survive unchanged;
        the ibkr_import row must NOT overwrite it.
        """
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        self._seed_fills_with_ccc_row(fills_path)

        sync_ibkr_positions(
            combos=LIVE_IBKR_COMBOS,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        positions = read_positions_snapshot(pos_path)
        apr17 = next(
            (p for p in positions if p.get("expiry") == "20260417"),
            None,
        )
        assert apr17 is not None
        # The CCC-executed row has entry_debit_gross=68.50; import row has None
        # First-seen wins → CCC row → debit preserved
        assert apr17.get("entry_debit_gross") == pytest.approx(68.50), (
            f"CCC entry_debit_gross should be 68.50, got {apr17.get('entry_debit_gross')}"
        )
        assert apr17.get("source") == "execution_result"

    # --- _contract_key_from_fills_row unit tests ---

    def test_contract_key_from_position_opened_row(self):
        row = {
            "action": "POSITION_OPENED",
            "underlier": "SPY",
            "expiry": "20260417",
            "strikes": [575.0, 555.0],
        }
        ck = _contract_key_from_fills_row(row)
        assert ck == ("SPY", "20260417", 575.0, 555.0)

    def test_contract_key_returns_none_for_non_position_opened(self):
        row = {"action": "ORDER_STAGED", "underlier": "SPY", "expiry": "20260417",
               "strikes": [575.0, 555.0]}
        assert _contract_key_from_fills_row(row) is None

    def test_contract_key_returns_none_for_missing_strikes(self):
        row = {"action": "POSITION_OPENED", "underlier": "SPY", "expiry": "20260417"}
        assert _contract_key_from_fills_row(row) is None

    # --- _dedup_fills_rows_by_contract unit tests ---

    def test_dedup_first_seen_wins(self):
        """CCC-executed row comes before ibkr_import row → CCC row wins."""
        ccc_row = {**self._CCC_ROW_APR17}
        import_row = build_ibkr_import_fill_row(LIVE_IBKR_COMBOS[0], "2026-03-09", "live")
        deduped = _dedup_fills_rows_by_contract([ccc_row, import_row])
        assert len(deduped) == 1
        assert deduped[0]["source"] == "execution_result"
        assert deduped[0]["intent_id"] == self._CCC_ROW_APR17["intent_id"]

    def test_dedup_ibkr_import_row_wins_if_first(self):
        """If ibkr_import row is written before a CCC row, import row wins (first-seen)."""
        import_row = build_ibkr_import_fill_row(LIVE_IBKR_COMBOS[0], "2026-03-09", "live")
        ccc_row = {**self._CCC_ROW_APR17}
        deduped = _dedup_fills_rows_by_contract([import_row, ccc_row])
        assert len(deduped) == 1
        assert deduped[0]["source"] == "ibkr_import"

    def test_dedup_non_position_opened_rows_pass_through(self):
        """ORDER_STAGED rows must not be affected by contract dedup."""
        staged = {"action": "ORDER_STAGED", "intent_id": "stg_001"}
        ccc_row = {**self._CCC_ROW_APR17}
        import_row = build_ibkr_import_fill_row(LIVE_IBKR_COMBOS[0], "2026-03-09", "live")
        deduped = _dedup_fills_rows_by_contract([staged, ccc_row, import_row])
        assert len(deduped) == 2   # staged + first ccc_row (import_row dropped)
        assert deduped[0]["action"] == "ORDER_STAGED"
        assert deduped[1]["source"] == "execution_result"

    def test_dedup_distinct_contracts_all_kept(self):
        """Three different contracts → all three kept (no false dedup)."""
        rows = [
            build_ibkr_import_fill_row(c, "2026-03-09", "live")
            for c in LIVE_IBKR_COMBOS
        ]
        deduped = _dedup_fills_rows_by_contract(rows)
        assert len(deduped) == 3


# ===========================================================================
# NEW: TestDebitEnrichment
# ===========================================================================

class TestDebitEnrichment:
    """
    Tests for entry_debit enrichment:
    1. import with entry_debit → positions.json stores entry_debit_gross
    2. compute_premium_at_risk reads entry_debit_gross when entry_debit absent
    3. run_report Section B shows non-zero Premium at risk
    """

    def _import_with_debit(self, tmp_path: Path, debit: float):
        """Helper: import SPY 20260417 575/555 with a known entry_debit."""
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        combo = {**LIVE_IBKR_COMBOS[0], "entry_debit": debit}
        sync_ibkr_positions(
            combos=[combo],
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )
        return pos_path

    def test_imported_spread_with_debit_stores_entry_debit_gross(self, tmp_path):
        """
        SPEC: After importing a spread with entry_debit=65.00, positions.json
        must contain entry_debit_gross=65.00 for that spread.
        """
        pos_path = self._import_with_debit(tmp_path, 65.0)
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.get("entry_debit_gross") == pytest.approx(65.0), (
            f"entry_debit_gross should be 65.0, got {pos.get('entry_debit_gross')}"
        )

    def test_premium_at_risk_nonzero_when_entry_debit_gross_set(self, tmp_path):
        """
        SPEC: compute_premium_at_risk() must return non-zero when positions.json
        contains entry_debit_gross (even when entry_debit key is absent).
        """
        from scripts.ccc_report import compute_premium_at_risk
        pos_path = self._import_with_debit(tmp_path, 65.0)
        positions = read_positions_snapshot(pos_path)
        risk = compute_premium_at_risk(positions)
        # 65.00 × qty_open=1 = 65.00
        assert risk == pytest.approx(65.0), (
            f"Premium at risk should be $65.00, got {risk}"
        )

    def test_premium_at_risk_reads_entry_debit_gross_from_positions_json_snapshot(self):
        """
        Unit: compute_premium_at_risk works directly with entry_debit_gross key
        (as stored by build_positions_snapshot).
        """
        from scripts.ccc_report import compute_premium_at_risk
        positions = [{"entry_debit_gross": 72.0, "qty_open": 1}]
        assert compute_premium_at_risk(positions) == pytest.approx(72.0)

    def test_premium_at_risk_prefers_entry_debit_net_over_gross(self):
        """entry_debit_net takes priority over entry_debit_gross."""
        from scripts.ccc_report import compute_premium_at_risk
        positions = [{"entry_debit_gross": 72.0, "entry_debit_net": 68.0, "qty_open": 1}]
        assert compute_premium_at_risk(positions) == pytest.approx(68.0)

    def test_premium_at_risk_still_reads_legacy_entry_debit(self):
        """Backward compat: positions written with entry_debit key still work."""
        from scripts.ccc_report import compute_premium_at_risk
        positions = [{"entry_debit": 65.0, "qty_open": 1}]
        assert compute_premium_at_risk(positions) == pytest.approx(65.0)

    def test_premium_at_risk_zero_when_no_debit_fields(self):
        """Positions with no debit at all contribute zero."""
        from scripts.ccc_report import compute_premium_at_risk
        positions = [{"qty_open": 1}]
        assert compute_premium_at_risk(positions) == 0.0

    def test_report_premium_at_risk_nonzero_after_import_with_debit(self, tmp_path):
        """
        SPEC: ccc_report Section B must show non-zero Premium at risk when
        imported positions carry a known entry_debit (via entry_debit_gross).
        """
        from scripts.ccc_report import run_report

        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        # Import all three spreads — only the first has a debit
        combos = [
            {**LIVE_IBKR_COMBOS[0], "entry_debit": 65.0},
            {**LIVE_IBKR_COMBOS[1], "entry_debit": 72.0},
            {**LIVE_IBKR_COMBOS[2], "entry_debit": 58.0},
        ]
        sync_ibkr_positions(
            combos=combos,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            run_report(
                positions_path=pos_path,
                commit_ledger_path=tmp_path / "commit.jsonl",
                fills_ledger_path=fills_path,
                actions_path=tmp_path / "actions.json",
                policy_path=tmp_path / "policy.yaml",
            )
        out = buf.getvalue()

        assert "Premium at risk:" in out
        # 65 + 72 + 58 = 195
        assert "$195.00" in out, (
            f"Expected '$195.00' in report output (sum of 3 debits × qty=1 each).\n"
            f"Report output:\n{out}"
        )

    def test_report_section_a_debit_column_nonzero_for_imported_with_debit(self, tmp_path):
        """
        SPEC: Section A DEBIT column must show the debit value, not 'N/A',
        when entry_debit_gross is set on an imported position.
        """
        from scripts.ccc_report import run_report

        pos_path = tmp_path / "positions.json"
        # Write a positions.json directly (as build_positions_snapshot would)
        _write_json(pos_path, [
            {
                "position_id": "ibkr_import_SPY_20260417_575_555",
                "policy_id": "ccc_v1",
                "mode": "live",
                "regime": "crash",
                "underlier": "SPY",
                "expiry": "20260417",
                "strikes": [575.0, 555.0],
                "qty_open": 1,
                "entry_debit_gross": 65.0,
                "entry_debit_net": None,
                "opened_utc": "2026-03-09T15:00:00Z",
                "source": "ibkr_import",
            }
        ])

        buf = io.StringIO()
        with redirect_stdout(buf):
            run_report(
                positions_path=pos_path,
                commit_ledger_path=tmp_path / "commit.jsonl",
                fills_ledger_path=tmp_path / "fills.jsonl",
                actions_path=tmp_path / "actions.json",
                policy_path=tmp_path / "policy.yaml",
            )
        out = buf.getvalue()

        assert "$65.00" in out, (
            f"Section A DEBIT column should show $65.00, not N/A.\nOutput:\n{out}"
        )


# ===========================================================================
# NEW: TestDebitEnrichmentOnExistingSpread
# ===========================================================================

class TestDebitEnrichmentOnExistingSpread:
    """
    Tests for the debit enrichment path:

    When a spread is already present in the fills ledger (e.g. imported without
    a debit on first run), a subsequent import call with a debit must enrich
    the position so that:
      - positions.json shows the debit
      - ccc_report premium-at-risk increases accordingly
      - No duplicate positions are created
      - CCC-executed fills with existing debit are NOT overwritten
    """

    # --- helpers ---

    @staticmethod
    def _import(tmp_path: Path, combos, *, date_str: str = "2026-03-09"):
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"
        result = sync_ibkr_positions(
            combos=combos,
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str=date_str,
        )
        return result, fills_path, pos_path

    # ------------------------------------------------------------------ #
    # 1.  Already-present ibkr_import spread → enriched on re-run        #
    # ------------------------------------------------------------------ #

    def test_already_present_ibkr_spread_gets_debit_on_rerun(self, tmp_path):
        """
        ACCEPTANCE CRITERION 1:
        - First import: SPY 20260327 590/570, no debit  → DEBIT=N/A in positions.json
        - Second import: same spread, entry_debit=38.20  → DEBIT=$38.20 in positions.json
        - No duplicate position (still 1 position)
        """
        combo_no_debit = {
            "symbol": "SPY", "expiry": "20260327",
            "long_strike": 590.0, "short_strike": 570.0,
            "qty": 1, "regime": "crash", "entry_debit": None,
        }
        combo_with_debit = {**combo_no_debit, "entry_debit": 38.20}

        # First import — no debit
        _, fills_path, pos_path = self._import(tmp_path, [combo_no_debit])
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1
        assert positions[0].get("entry_debit_gross") is None

        # Second import — supply debit
        result2, _, _ = self._import(tmp_path, [combo_with_debit])

        # Must trigger positions.json rebuild (enriched > 0 or positions_written)
        assert result2["positions_written"] is True, (
            "Enrichment must trigger a positions.json rebuild"
        )
        # No NEW position imported (contract already tracked)
        assert result2["imported"] == 0, (
            "Enrichment must not create a new import row"
        )

        # Positions.json now has debit
        positions_after = read_positions_snapshot(pos_path)
        assert len(positions_after) == 1, (
            f"Should still be 1 position (no duplicate), got {len(positions_after)}"
        )
        debit = positions_after[0].get("entry_debit_gross")
        assert debit == pytest.approx(38.20), (
            f"entry_debit_gross should be 38.20, got {debit}"
        )

    def test_already_present_spread_enriched_on_rerun_with_multiple_combos(self, tmp_path):
        """
        Enrich TWO existing spreads with different debits in a single re-run.
        """
        no_debit_combos = [
            {"symbol": "SPY", "expiry": "20260327", "long_strike": 590.0,
             "short_strike": 570.0, "qty": 1, "regime": "crash", "entry_debit": None},
            {"symbol": "SPY", "expiry": "20260320", "long_strike": 590.0,
             "short_strike": 570.0, "qty": 1, "regime": "crash", "entry_debit": None},
        ]
        with_debit_combos = [
            {**no_debit_combos[0], "entry_debit": 38.20},
            {**no_debit_combos[1], "entry_debit": 29.60},
        ]

        # First import
        self._import(tmp_path, no_debit_combos)
        # Second import with debits
        self._import(tmp_path, with_debit_combos)

        pos_path = tmp_path / "positions.json"
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 2

        mar27 = next(p for p in positions if p["expiry"] == "20260327")
        mar20 = next(p for p in positions if p["expiry"] == "20260320")
        assert mar27.get("entry_debit_gross") == pytest.approx(38.20)
        assert mar20.get("entry_debit_gross") == pytest.approx(29.60)

    # ------------------------------------------------------------------ #
    # 2.  CCC-executed spread's debit must NOT be overwritten             #
    # ------------------------------------------------------------------ #

    def test_ccc_executed_spread_debit_not_overwritten_by_enrichment(self, tmp_path):
        """
        ACCEPTANCE CRITERION 2:
        CCC-executed fill (entry_debit_gross=68.50) must NOT be updated
        when the import is re-run with a different debit (e.g. 75.00).
        """
        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        # Pre-write a CCC-executed fill WITH a real debit
        ccc_row = {
            "date": "2026-03-01",
            "timestamp_utc": "2026-03-01T10:00:00Z",
            "action": "POSITION_OPENED",
            "policy_id": "ccc_v1",
            "mode": "live",
            "intent_id": "a3ea604eecd990fc918733550d947fe620a93514",
            "intent_path": None,
            "candidate_id": "cand_apr17",
            "regime": "crash",
            "underlier": "SPY",
            "expiry": "20260417",
            "strikes": [575.0, 555.0],
            "qty": 1,
            "entry_debit_gross": 68.50,
            "entry_debit_net": None,
            "commissions": None,
            "ibkr": {"orderId": 1001, "permId": None, "conIds": [], "fills": []},
            "source": "execution_result",
        }
        fills_path.parent.mkdir(parents=True, exist_ok=True)
        with open(fills_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(ccc_row) + "\n")

        # Import attempt: same contract, different debit
        combo_with_different_debit = {
            "symbol": "SPY", "expiry": "20260417",
            "long_strike": 575.0, "short_strike": 555.0,
            "qty": 1, "regime": "crash",
            "entry_debit": 75.00,   # different from 68.50
        }
        sync_ibkr_positions(
            combos=[combo_with_different_debit],
            fills_ledger_path=fills_path,
            positions_path=pos_path,
            mode="live",
            date_str="2026-03-09",
        )

        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1

        apr17 = positions[0]
        # CCC debit (68.50) must NOT be overwritten with 75.00
        assert apr17.get("entry_debit_gross") == pytest.approx(68.50), (
            f"CCC debit 68.50 must not be overwritten; got {apr17.get('entry_debit_gross')}"
        )
        assert apr17.get("source") == "execution_result", (
            "Source must still be 'execution_result', not overwritten by ibkr_import"
        )

    # ------------------------------------------------------------------ #
    # 3.  positions.json after rebuild shows debit                        #
    # ------------------------------------------------------------------ #

    def test_positions_json_after_enrichment_rebuild_shows_debit(self, tmp_path):
        """
        ACCEPTANCE CRITERION 3:
        After enrichment, positions.json rebuild (--rebuild-positions path)
        must carry the debit even when force_rebuild=True is used later.
        """
        combo_no_debit = {
            "symbol": "SPY", "expiry": "20260320", "long_strike": 590.0,
            "short_strike": 570.0, "qty": 1, "regime": "crash", "entry_debit": None,
        }
        combo_with_debit = {**combo_no_debit, "entry_debit": 29.60}

        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        # Step 1: import without debit
        sync_ibkr_positions(combos=[combo_no_debit], fills_ledger_path=fills_path,
                            positions_path=pos_path, mode="live", date_str="2026-03-09")

        # Step 2: enrich with debit
        sync_ibkr_positions(combos=[combo_with_debit], fills_ledger_path=fills_path,
                            positions_path=pos_path, mode="live", date_str="2026-03-09")

        # Step 3: force-rebuild (simulates --rebuild-positions on next run)
        result3 = sync_ibkr_positions(combos=[combo_with_debit], fills_ledger_path=fills_path,
                                       positions_path=pos_path, mode="live",
                                       date_str="2026-03-10", force_rebuild=True)
        assert result3["positions_written"] is True

        # Debit must still be present in the rebuilt positions.json
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1
        assert positions[0].get("entry_debit_gross") == pytest.approx(29.60), (
            f"Debit should survive force-rebuild; got {positions[0].get('entry_debit_gross')}"
        )

    # ------------------------------------------------------------------ #
    # 4.  premium-at-risk increases after enrichment                      #
    # ------------------------------------------------------------------ #

    def test_premium_at_risk_increases_after_enrichment(self, tmp_path):
        """
        ACCEPTANCE CRITERION 4:
        Before enrichment → premium at risk = $55.00 (only Apr17 has debit).
        After enrichment  → premium at risk = $55.00 + $38.20 + $29.60 = $122.80.
        """
        from scripts.ccc_report import compute_premium_at_risk

        fills_path = tmp_path / "fills.jsonl"
        pos_path = tmp_path / "positions.json"

        # Import Apr17 WITH debit, Mar27 and Mar20 WITHOUT
        combos_initial = [
            {**LIVE_IBKR_COMBOS[0], "entry_debit": 55.00},  # Apr17 has debit
            {**LIVE_IBKR_COMBOS[1], "entry_debit": None},   # Mar27 no debit
            {**LIVE_IBKR_COMBOS[2], "entry_debit": None},   # Mar20 no debit
        ]
        sync_ibkr_positions(combos=combos_initial, fills_ledger_path=fills_path,
                            positions_path=pos_path, mode="live", date_str="2026-03-09")

        positions_before = read_positions_snapshot(pos_path)
        risk_before = compute_premium_at_risk(positions_before)
        assert risk_before == pytest.approx(55.00), (
            f"Before enrichment: only Apr17 debit ($55); got {risk_before}"
        )

        # Now enrich Mar27 and Mar20 with their debits
        combos_enrich = [
            {**LIVE_IBKR_COMBOS[1], "entry_debit": 38.20},   # Mar27
            {**LIVE_IBKR_COMBOS[2], "entry_debit": 29.60},   # Mar20
        ]
        sync_ibkr_positions(combos=combos_enrich, fills_ledger_path=fills_path,
                            positions_path=pos_path, mode="live", date_str="2026-03-09")

        positions_after = read_positions_snapshot(pos_path)
        risk_after = compute_premium_at_risk(positions_after)
        # 55.00 + 38.20 + 29.60 = 122.80
        assert risk_after == pytest.approx(122.80, abs=0.01), (
            f"After enrichment: expected $122.80; got {risk_after}"
        )

    def test_no_duplicates_after_enrichment(self, tmp_path):
        """
        After enrichment run, positions.json must not contain duplicates.
        """
        combo_no_debit = {
            "symbol": "SPY", "expiry": "20260327", "long_strike": 590.0,
            "short_strike": 570.0, "qty": 1, "regime": "crash", "entry_debit": None,
        }
        # First import
        self._import(tmp_path, [combo_no_debit])
        # Enrich twice (idempotent)
        self._import(tmp_path, [{**combo_no_debit, "entry_debit": 38.20}])
        self._import(tmp_path, [{**combo_no_debit, "entry_debit": 38.20}])

        pos_path = tmp_path / "positions.json"
        positions = read_positions_snapshot(pos_path)
        assert len(positions) == 1, (
            f"Idempotent enrichment must not create duplicates; got {len(positions)}"
        )

    def test_debit_enrichment_position_id_format(self):
        """_ibkr_debit_enrich_position_id produces correct format."""
        pid = _ibkr_debit_enrich_position_id("SPY", "20260327", 590.0, 570.0)
        assert pid == "ibkr_debit_enrich_SPY_20260327_590_570"
        assert pid.startswith("ibkr_debit_enrich_")
        assert pid != _ibkr_import_position_id("SPY", "20260327", 590.0, 570.0)

    def test_enrichment_result_key_present(self, tmp_path):
        """sync_ibkr_positions result dict must contain 'enriched' key."""
        combo = {
            "symbol": "SPY", "expiry": "20260327", "long_strike": 590.0,
            "short_strike": 570.0, "qty": 1, "regime": "crash", "entry_debit": None,
        }
        # First import without debit
        self._import(tmp_path, [combo])
        # Second import with debit
        result2, _, _ = self._import(tmp_path, [{**combo, "entry_debit": 38.20}])
        assert "enriched" in result2, "result must contain 'enriched' key"
        assert result2["enriched"] == 1
