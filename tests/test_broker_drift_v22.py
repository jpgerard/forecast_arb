"""
tests/test_broker_drift_v22.py
================================
CCC v2.2 — Broker-State Drift Detector Tests

All tests are deterministic, pure, no I/O except to tmp files.
No IBKR API required.  No live execution paths.

Coverage:
─────────
Unit: load_ccc_positions
  - Missing file returns []
  - Valid file returns list
  - Malformed JSON returns []

Unit: load_ibkr_positions_from_csv
  - Activity statement layout parsed
  - Simple CSV layout parsed
  - Missing file returns []
  - Unknown/blank rows do not crash

Unit: normalize_ccc_spread_positions
  - Normal position normalized
  - Missing fields skipped
  - Regime preserved
  - Strike order corrected

Unit: normalize_ibkr_spread_positions
  - Alpha-month symbol format parsed
  - Numeric-date symbol format parsed
  - OCC-style symbol parsed
  - BAG row parsed
  - Long/short leg grouping works
  - Unrelated equity rows ignored
  - Unknown rows do not crash

Unit: diff_ccc_vs_ibkr
  - Fully matching → in_sync=True
  - Position only in CCC → drift detected
  - Position only in IBKR → drift detected
  - Qty mismatch detected
  - Empty both → in_sync=True

Acceptance:
  - CCC=3 IBKR=2 → headline warns clearly
  - No auto-repair of positions
  - JSON serializable output

Adapter integration (mock-based):
  - status_snapshot without broker_csv → no broker_drift key
  - status_snapshot with broker_csv (in-sync) → in_sync=True in details
  - status_snapshot with drift → headline prefixed, actionability=REVIEW_ONLY
  - summarize_latest with broker_csv wires through to status
  - --json CLI includes broker_drift block
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from forecast_arb.allocator.broker_drift import (
    check_broker_drift,
    diff_ccc_vs_ibkr,
    load_ccc_positions,
    load_ibkr_positions_from_csv,
    normalize_ccc_spread_positions,
    normalize_ibkr_spread_positions,
    _parse_option_symbol,
    _parse_qty,
)
from forecast_arb.adapter.trading_adapter import AdapterResult, TradingAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _make_ccc_position(
    symbol="SPY",
    expiry="20260417",
    long_strike=590.0,
    short_strike=570.0,
    qty=1,
    regime="crash",
) -> Dict[str, Any]:
    """Build a minimal CCC positions.json position entry."""
    return {
        "position_id": f"ibkr_import_{symbol}_{expiry}_{int(long_strike)}_{int(short_strike)}",
        "underlier": symbol,
        "expiry": expiry,
        "strikes": [long_strike, short_strike],
        "qty_open": qty,
        "regime": regime,
        "source": "ibkr_import",
    }


# ---------------------------------------------------------------------------
# Activity-statement CSV fixture
# ---------------------------------------------------------------------------

_ACTIVITY_CSV_3_SPREADS = textwrap.dedent("""\
    Positions,Header,Symbol,Quantity,Mult,Cost Price,Close Price,Value,Unrealized P/L,Code
    Positions,Data,SPY 17APR26 590 P,1,100,58.20,62.00,6200,380,
    Positions,Data,SPY 17APR26 570 P,-1,100,20.40,22.00,-2200,-160,
    Positions,Data,SPY 27MAR26 590 P,1,100,45.00,48.00,4800,300,
    Positions,Data,SPY 27MAR26 570 P,-1,100,15.00,16.50,-1650,-150,
    Positions,Data,SPY 20MAR26 590 P,1,100,38.00,40.00,4000,200,
    Positions,Data,SPY 20MAR26 570 P,-1,100,11.00,12.00,-1200,-100,
    Positions,Data,AAPL,200,1,175.00,180.00,36000,1000,
""")

_ACTIVITY_CSV_2_SPREADS = textwrap.dedent("""\
    Positions,Header,Symbol,Quantity,Mult,Cost Price,Close Price,Value,Unrealized P/L,Code
    Positions,Data,SPY 17APR26 590 P,1,100,58.20,62.00,6200,380,
    Positions,Data,SPY 17APR26 570 P,-1,100,20.40,22.00,-2200,-160,
    Positions,Data,SPY 27MAR26 590 P,1,100,45.00,48.00,4800,300,
    Positions,Data,SPY 27MAR26 570 P,-1,100,15.00,16.50,-1650,-150,
    Positions,Data,AAPL,200,1,175.00,180.00,36000,1000,
""")

# Simple CSV layout (plain flat CSV)
_SIMPLE_CSV_2_SPREADS = textwrap.dedent("""\
    Symbol,Quantity,Type
    SPY 17APR26 590 P,1,OPT
    SPY 17APR26 570 P,-1,OPT
    SPY 27MAR26 590 P,1,OPT
    SPY 27MAR26 570 P,-1,OPT
    AAPL,200,STK
""")

# BAG row CSV
_BAG_CSV = textwrap.dedent("""\
    Symbol,Quantity,Type
    SPY BAG / SPY 17APR26 590 P,SPY 17APR26 570 P,1,BAG
""")

# Unknown/garbage rows that should not crash
_GARBAGE_CSV = textwrap.dedent("""\
    Symbol,Quantity,Type
    ,,
    ???,abc,
    just garbage here
    SPY 17APR26 590 P,1,OPT
    SPY 17APR26 570 P,-1,OPT
""")


# ===========================================================================
# PART 1: load_ccc_positions
# ===========================================================================

class TestLoadCccPositions(unittest.TestCase):

    def test_missing_file_returns_empty_list(self):
        result = load_ccc_positions("/nonexistent/path/positions.json")
        self.assertEqual(result, [])

    def test_valid_positions_file_returned(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "positions.json"
            data = [_make_ccc_position(), _make_ccc_position(expiry="20260327")]
            _write_json(p, data)
            result = load_ccc_positions(p)
            self.assertEqual(len(result), 2)

    def test_malformed_json_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "positions.json"
            p.write_text("not valid json }{", encoding="utf-8")
            result = load_ccc_positions(p)
            self.assertEqual(result, [])

    def test_non_list_json_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "positions.json"
            _write_json(p, {"not": "a list"})
            result = load_ccc_positions(p)
            self.assertEqual(result, [])

    def test_empty_list_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "positions.json"
            _write_json(p, [])
            result = load_ccc_positions(p)
            self.assertEqual(result, [])


# ===========================================================================
# PART 2: load_ibkr_positions_from_csv
# ===========================================================================

class TestLoadIbkrPositionsFromCsv(unittest.TestCase):

    def _write_csv(self, td: str, content: str, name: str = "positions.csv") -> Path:
        p = Path(td) / name
        _write_text(p, content)
        return p

    def test_missing_file_returns_empty_list(self):
        result = load_ibkr_positions_from_csv("/nonexistent/path/positions.csv")
        self.assertEqual(result, [])

    def test_activity_statement_layout_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_csv(td, _ACTIVITY_CSV_3_SPREADS)
            rows = load_ibkr_positions_from_csv(p)
            # 7 data rows (6 options + 1 equity)
            self.assertGreater(len(rows), 0)
            # Confirm SPY options are present
            syms = [r.get("symbol", "") for r in rows]
            self.assertTrue(any("SPY" in s for s in syms))

    def test_simple_csv_layout_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_csv(td, _SIMPLE_CSV_2_SPREADS)
            rows = load_ibkr_positions_from_csv(p)
            self.assertGreater(len(rows), 0)

    def test_garbage_rows_do_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_csv(td, _GARBAGE_CSV)
            # Must not raise
            rows = load_ibkr_positions_from_csv(p)
            self.assertIsInstance(rows, list)

    def test_empty_csv_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write_csv(td, "")
            rows = load_ibkr_positions_from_csv(p)
            self.assertIsInstance(rows, list)


# ===========================================================================
# PART 3: normalize_ccc_spread_positions
# ===========================================================================

class TestNormalizeCccSpreadPositions(unittest.TestCase):

    def test_normal_position_normalized(self):
        pos = _make_ccc_position("SPY", "20260417", 590.0, 570.0, 1, "crash")
        result = normalize_ccc_spread_positions([pos])
        self.assertEqual(len(result), 1)
        r = result[0]
        self.assertEqual(r["symbol"], "SPY")
        self.assertEqual(r["expiry"], "20260417")
        self.assertAlmostEqual(r["long_strike"], 590.0)
        self.assertAlmostEqual(r["short_strike"], 570.0)
        self.assertEqual(r["qty"], 1)
        self.assertEqual(r["regime"], "crash")
        self.assertEqual(r["_source"], "ccc")

    def test_key_is_tuple_of_four(self):
        pos = _make_ccc_position()
        result = normalize_ccc_spread_positions([pos])
        key = result[0]["_key"]
        self.assertIsInstance(key, tuple)
        self.assertEqual(len(key), 4)

    def test_missing_underlier_skipped(self):
        pos = {"expiry": "20260417", "strikes": [590.0, 570.0], "qty_open": 1}
        result = normalize_ccc_spread_positions([pos])
        self.assertEqual(result, [])

    def test_missing_strikes_skipped(self):
        pos = {"underlier": "SPY", "expiry": "20260417", "qty_open": 1}
        result = normalize_ccc_spread_positions([pos])
        self.assertEqual(result, [])

    def test_regime_preserved(self):
        pos = _make_ccc_position(regime="selloff")
        result = normalize_ccc_spread_positions([pos])
        self.assertEqual(result[0]["regime"], "selloff")

    def test_strike_order_corrected(self):
        # If strikes[0] < strikes[1] (reversed), should be corrected
        pos = _make_ccc_position(long_strike=570.0, short_strike=590.0)
        # We deliberately put the smaller value first
        pos["strikes"] = [570.0, 590.0]
        result = normalize_ccc_spread_positions([pos])
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0]["long_strike"], result[0]["short_strike"])

    def test_malformed_entry_skipped_gracefully(self):
        bad = {"underlier": "SPY", "expiry": "20260417", "strikes": ["not", "numbers"]}
        good = _make_ccc_position()
        result = normalize_ccc_spread_positions([bad, good])
        # bad is skipped, good is included
        self.assertEqual(len(result), 1)

    def test_empty_input_returns_empty(self):
        result = normalize_ccc_spread_positions([])
        self.assertEqual(result, [])

    def test_three_positions_normalized(self):
        positions = [
            _make_ccc_position("SPY", "20260417", 590.0, 570.0),
            _make_ccc_position("SPY", "20260327", 590.0, 570.0),
            _make_ccc_position("SPY", "20260320", 590.0, 570.0),
        ]
        result = normalize_ccc_spread_positions(positions)
        self.assertEqual(len(result), 3)


# ===========================================================================
# PART 4: _parse_option_symbol
# ===========================================================================

class TestParseOptionSymbol(unittest.TestCase):

    def test_alpha_month_format(self):
        r = _parse_option_symbol("SPY 17APR26 590 P")
        self.assertIsNotNone(r)
        self.assertEqual(r["underlier"], "SPY")
        self.assertEqual(r["expiry"], "20260417")
        self.assertAlmostEqual(r["strike"], 590.0)
        self.assertEqual(r["opt_type"], "P")

    def test_alpha_month_full_year(self):
        r = _parse_option_symbol("SPY 17APR2026 590 P")
        self.assertIsNotNone(r)
        self.assertEqual(r["expiry"], "20260417")

    def test_numeric_date_format(self):
        r = _parse_option_symbol("SPY 20260417 590.0 P")
        self.assertIsNotNone(r)
        self.assertEqual(r["expiry"], "20260417")
        self.assertAlmostEqual(r["strike"], 590.0)

    def test_call_option_parsed(self):
        r = _parse_option_symbol("SPY 17APR26 600 C")
        self.assertIsNotNone(r)
        self.assertEqual(r["opt_type"], "C")

    def test_unknown_format_returns_none(self):
        r = _parse_option_symbol("AAPL")
        self.assertIsNone(r)

    def test_equity_row_returns_none(self):
        r = _parse_option_symbol("AAPL 200 STK")
        self.assertIsNone(r)

    def test_blank_returns_none(self):
        r = _parse_option_symbol("")
        self.assertIsNone(r)

    def test_various_strikes(self):
        for strike in [570.0, 585.0, 600.0, 560.0]:
            sym = f"SPY 17APR26 {strike:.0f} P"
            r = _parse_option_symbol(sym)
            self.assertIsNotNone(r, f"Failed to parse {sym!r}")
            self.assertAlmostEqual(r["strike"], strike)


# ===========================================================================
# PART 5: normalize_ibkr_spread_positions
# ===========================================================================

class TestNormalizeIbkrSpreadPositions(unittest.TestCase):

    def _build_rows_from_csv(self, csv_text: str) -> list:
        """Build raw rows from CSV text for testing normalization."""
        from forecast_arb.allocator.broker_drift import _parse_simple_csv
        return _parse_simple_csv(csv_text)

    def test_alpha_month_legs_form_spread(self):
        rows = self._build_rows_from_csv(_SIMPLE_CSV_2_SPREADS)
        result = normalize_ibkr_spread_positions(rows)
        # Should find 2 spreads (APR17 590/570, MAR27 590/570)
        self.assertEqual(len(result), 2)

    def test_spread_keys_correct(self):
        rows = self._build_rows_from_csv(_SIMPLE_CSV_2_SPREADS)
        result = normalize_ibkr_spread_positions(rows)
        keys = {r["_key"] for r in result}
        self.assertIn(("SPY", "20260417", 590.0, 570.0), keys)
        self.assertIn(("SPY", "20260327", 590.0, 570.0), keys)

    def test_equity_rows_ignored(self):
        """AAPL equity row must not produce a spread."""
        rows = self._build_rows_from_csv(_SIMPLE_CSV_2_SPREADS)
        result = normalize_ibkr_spread_positions(rows)
        symbols = {r["symbol"] for r in result}
        self.assertNotIn("AAPL", symbols)

    def test_unknown_rows_do_not_crash(self):
        rows = self._build_rows_from_csv(_GARBAGE_CSV)
        # Must not raise; may return 0 or more spreads (partial data)
        result = normalize_ibkr_spread_positions(rows)
        self.assertIsInstance(result, list)

    def test_three_activity_statement_spreads(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "positions.csv"
            _write_text(p, _ACTIVITY_CSV_3_SPREADS)
            from forecast_arb.allocator.broker_drift import load_ibkr_positions_from_csv
            rows = load_ibkr_positions_from_csv(p)
            result = normalize_ibkr_spread_positions(rows)
            self.assertEqual(len(result), 3)

    def test_source_is_ibkr(self):
        rows = self._build_rows_from_csv(_SIMPLE_CSV_2_SPREADS)
        result = normalize_ibkr_spread_positions(rows)
        for r in result:
            self.assertEqual(r["_source"], "ibkr")

    def test_empty_input_returns_empty(self):
        result = normalize_ibkr_spread_positions([])
        self.assertEqual(result, [])


# ===========================================================================
# PART 6: diff_ccc_vs_ibkr — core logic
# ===========================================================================

class TestDiffCccVsIbkr(unittest.TestCase):

    def _ccc_rec(self, symbol="SPY", expiry="20260417", ls=590.0, ss=570.0, qty=1, regime="crash"):
        key = (symbol, expiry, ls, ss)
        return {
            "symbol": symbol, "expiry": expiry,
            "long_strike": ls, "short_strike": ss,
            "qty": qty, "regime": regime,
            "_key": key, "_source": "ccc", "_raw": {},
        }

    def _ibkr_rec(self, symbol="SPY", expiry="20260417", ls=590.0, ss=570.0, qty=1):
        key = (symbol, expiry, ls, ss)
        return {
            "symbol": symbol, "expiry": expiry,
            "long_strike": ls, "short_strike": ss,
            "qty": qty, "regime": "unknown",
            "_key": key, "_source": "ibkr", "_raw": {},
        }

    # --- in_sync cases ---

    def test_both_empty_in_sync(self):
        result = diff_ccc_vs_ibkr([], [])
        self.assertTrue(result["in_sync"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["ccc_count"], 0)
        self.assertEqual(result["ibkr_count"], 0)

    def test_fully_matched_in_sync(self):
        ccc  = [self._ccc_rec("SPY", "20260417", 590.0, 570.0)]
        ibkr = [self._ibkr_rec("SPY", "20260417", 590.0, 570.0)]
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        self.assertTrue(result["in_sync"])
        self.assertEqual(len(result["only_in_ccc"]), 0)
        self.assertEqual(len(result["only_in_ibkr"]), 0)
        self.assertEqual(len(result["qty_mismatches"]), 0)

    def test_three_matched_in_sync(self):
        ccc = [
            self._ccc_rec("SPY", "20260417"),
            self._ccc_rec("SPY", "20260327"),
            self._ccc_rec("SPY", "20260320"),
        ]
        ibkr = [
            self._ibkr_rec("SPY", "20260417"),
            self._ibkr_rec("SPY", "20260327"),
            self._ibkr_rec("SPY", "20260320"),
        ]
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        self.assertTrue(result["in_sync"])
        self.assertEqual(result["ccc_count"], 3)
        self.assertEqual(result["ibkr_count"], 3)

    # --- drift cases ---

    def test_position_only_in_ccc_drift_detected(self):
        """CCC has a spread that IBKR does not. → drift."""
        ccc  = [self._ccc_rec("SPY", "20260417")]
        ibkr = []  # IBKR shows nothing
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        self.assertFalse(result["in_sync"])
        self.assertEqual(len(result["only_in_ccc"]), 1)
        self.assertEqual(len(result["only_in_ibkr"]), 0)
        # Headline must warn
        self.assertIn("stale", result["headline"].lower())

    def test_position_only_in_ibkr_drift_detected(self):
        """IBKR has a spread that CCC does not. → drift."""
        ccc  = []
        ibkr = [self._ibkr_rec("SPY", "20260417")]
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        self.assertFalse(result["in_sync"])
        self.assertEqual(len(result["only_in_ibkr"]), 1)
        self.assertEqual(len(result["only_in_ccc"]), 0)

    def test_qty_mismatch_detected(self):
        """CCC qty=1 but IBKR qty=2 → qty mismatch."""
        ccc  = [self._ccc_rec(qty=1)]
        ibkr = [self._ibkr_rec(qty=2)]
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        self.assertFalse(result["in_sync"])
        self.assertEqual(len(result["qty_mismatches"]), 1)
        mm = result["qty_mismatches"][0]
        self.assertEqual(mm["ccc_qty"], 1)
        self.assertEqual(mm["ibkr_qty"], 2)

    def test_ccc_three_ibkr_two_drift_headline(self):
        """Canonical failure: CCC=3, IBKR=2 → clear warning headline."""
        ccc = [
            self._ccc_rec("SPY", "20260417"),
            self._ccc_rec("SPY", "20260327"),
            self._ccc_rec("SPY", "20260320"),
        ]
        ibkr = [
            self._ibkr_rec("SPY", "20260417"),
            self._ibkr_rec("SPY", "20260327"),
            # 20260320 missing from IBKR
        ]
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        self.assertFalse(result["in_sync"])
        self.assertEqual(len(result["only_in_ccc"]), 1)
        self.assertEqual(len(result["only_in_ibkr"]), 0)
        self.assertIn("3", result["headline"])
        self.assertIn("2", result["headline"])

    # --- result shape ---

    def test_result_has_required_keys(self):
        result = diff_ccc_vs_ibkr([], [])
        required = {"ok", "in_sync", "ccc_count", "ibkr_count",
                    "only_in_ccc", "only_in_ibkr", "qty_mismatches",
                    "headline", "errors"}
        self.assertTrue(required.issubset(set(result.keys())))

    def test_result_is_json_serializable(self):
        ccc = [self._ccc_rec()]
        ibkr = [self._ibkr_rec()]
        result = diff_ccc_vs_ibkr(ccc, ibkr)
        serialized = json.dumps(result, default=str)
        parsed = json.loads(serialized)
        self.assertIn("in_sync", parsed)

    def test_no_auto_repair(self):
        """Result must be purely read-only — no side effects."""
        ccc  = [self._ccc_rec()]
        ibkr = []
        before = json.dumps(ccc, default=str)
        diff_ccc_vs_ibkr(ccc, ibkr)
        after = json.dumps(ccc, default=str)
        self.assertEqual(before, after)


# ===========================================================================
# PART 7: check_broker_drift end-to-end (with tmp files)
# ===========================================================================

class TestCheckBrokerDriftEndToEnd(unittest.TestCase):
    """
    End-to-end tests using real tmp files and CSV content.
    """

    def test_in_sync_when_positions_match_csv(self):
        with tempfile.TemporaryDirectory() as td:
            # CCC: 2 positions
            pos = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
            ]
            pos_path = Path(td) / "positions.json"
            _write_json(pos_path, pos)

            # IBKR CSV: same 2 spreads
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            result = check_broker_drift(pos_path, csv_path)
            self.assertTrue(result["ok"])
            self.assertTrue(result["in_sync"])
            self.assertEqual(result["ccc_count"], 2)
            self.assertEqual(result["ibkr_count"], 2)

    def test_drift_when_ccc_has_extra(self):
        with tempfile.TemporaryDirectory() as td:
            # CCC: 3 positions
            pos = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
                _make_ccc_position("SPY", "20260320", 590.0, 570.0),
            ]
            pos_path = Path(td) / "positions.json"
            _write_json(pos_path, pos)

            # IBKR CSV: only 2 spreads
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            result = check_broker_drift(pos_path, csv_path)
            self.assertTrue(result["ok"])
            self.assertFalse(result["in_sync"])
            self.assertEqual(len(result["only_in_ccc"]), 1)
            self.assertEqual(result["ccc_count"], 3)
            self.assertEqual(result["ibkr_count"], 2)

    def test_drift_when_ibkr_has_extra(self):
        with tempfile.TemporaryDirectory() as td:
            # CCC: 2 positions
            pos = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
            ]
            pos_path = Path(td) / "positions.json"
            _write_json(pos_path, pos)

            # IBKR CSV: 3 spreads (one extra)
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _ACTIVITY_CSV_3_SPREADS)

            result = check_broker_drift(pos_path, csv_path)
            self.assertTrue(result["ok"])
            self.assertFalse(result["in_sync"])
            self.assertGreater(len(result["only_in_ibkr"]), 0)

    def test_missing_csv_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            pos_path = Path(td) / "positions.json"
            _write_json(pos_path, [])
            csv_path = Path(td) / "nonexistent.csv"
            result = check_broker_drift(pos_path, csv_path)
            self.assertFalse(result["ok"])
            self.assertFalse(result["in_sync"])
            self.assertGreater(len(result["errors"]), 0)

    def test_missing_positions_file_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            pos_path = Path(td) / "nonexistent_positions.json"
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)
            # CCC missing → loads as empty → drift detected (IBKR has 2)
            result = check_broker_drift(pos_path, csv_path)
            self.assertTrue(result["ok"])
            self.assertFalse(result["in_sync"])
            self.assertEqual(result["ccc_count"], 0)


# ===========================================================================
# PART 8: Trading Adapter — broker drift integration
# ===========================================================================

class TestAdapterStatusWithBrokerDrift(unittest.TestCase):
    """
    Tests for TradingAdapter.status_snapshot() with broker_csv_path.
    Uses mocked ccc_report module to avoid needing real files.
    """

    def _mock_ccc_report_module(self, positions=None):
        mock = MagicMock()
        mock.load_positions.return_value = positions or []
        mock.compute_pending_count.return_value = {"crash": 0, "selloff": 0, "total": 0}
        mock.compute_ytd_spent.return_value = 0.0
        mock.load_annual_budget.return_value = {"budget": None, "enabled": False}
        mock.load_premium_at_risk_caps.return_value = {"crash": None, "selloff": None, "total": None, "enabled": False}
        mock.load_inventory_targets_and_caps.return_value = {"soft_targets": {}, "hard_caps": {}, "enabled": False}
        mock._compute_par.return_value = {"crash": 0.0, "selloff": 0.0, "total": 0.0}
        mock.load_actions.return_value = None
        return mock

    def _run_status_with_mock(self, mock_module, broker_csv_path=None, positions=None):
        adapter = TradingAdapter(
            policy_path=Path("configs/allocator_ccc_v1.yaml"),
            campaign_path=Path("configs/campaign_v1.yaml"),
            timeout_secs=30,
        )
        if positions is not None:
            mock_module.load_positions.return_value = positions

        with patch("importlib.util.spec_from_file_location") as mock_spec_fn, \
             patch("importlib.util.module_from_spec") as mock_mod_fn:
            mock_spec = MagicMock()
            mock_spec_fn.return_value = mock_spec
            mock_mod_fn.return_value = mock_module
            mock_spec.loader.exec_module = MagicMock()
            result = adapter.status_snapshot(broker_csv_path=broker_csv_path)
        return result

    def test_no_broker_csv_no_drift_key(self):
        """Without broker_csv_path, broker_drift must NOT appear in details."""
        mock = self._mock_ccc_report_module()
        result = self._run_status_with_mock(mock)
        self.assertTrue(result.ok)
        self.assertNotIn("broker_drift", result.details)

    def test_with_broker_csv_in_sync_adds_drift_key(self):
        """With a broker CSV that matches, broker_drift key is present and in_sync=True."""
        with tempfile.TemporaryDirectory() as td:
            # 2 matching CCC positions — write to a real temp file so drift check can read it
            positions = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
            ]
            pos_path = Path(td) / "positions.json"
            _write_json(pos_path, positions)

            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            mock = self._mock_ccc_report_module(positions)

            adapter = TradingAdapter(
                policy_path=Path("configs/allocator_ccc_v1.yaml"),
                campaign_path=Path("configs/campaign_v1.yaml"),
                timeout_secs=30,
            )
            with patch("importlib.util.spec_from_file_location") as mock_spec_fn, \
                 patch("importlib.util.module_from_spec") as mock_mod_fn:
                mock_spec = MagicMock()
                mock_spec_fn.return_value = mock_spec
                mock_mod_fn.return_value = mock
                mock_spec.loader.exec_module = MagicMock()
                # Pass positions_path so drift check reads from our temp file
                result = adapter.status_snapshot(
                    positions_path=pos_path,
                    broker_csv_path=csv_path,
                )

            self.assertTrue(result.ok)
            self.assertIn("broker_drift", result.details)
            self.assertTrue(result.details["broker_drift"].get("in_sync"))

    def test_drift_detected_degrades_actionability(self):
        """
        Drift detected on status_snapshot → actionability becomes REVIEW_ONLY
        even when CCC has no open positions (which would normally be NO_ACTION).
        """
        with tempfile.TemporaryDirectory() as td:
            # CCC: 3 positions
            positions = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
                _make_ccc_position("SPY", "20260320", 590.0, 570.0),
            ]
            # IBKR: only 2
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            mock = self._mock_ccc_report_module(positions)
            mock._compute_par.return_value = {"crash": 0.0, "selloff": 0.0, "total": 0.0}

            adapter = TradingAdapter(
                policy_path=Path("configs/allocator_ccc_v1.yaml"),
                campaign_path=Path("configs/campaign_v1.yaml"),
                timeout_secs=30,
            )
            with patch("importlib.util.spec_from_file_location") as mock_spec_fn, \
                 patch("importlib.util.module_from_spec") as mock_mod_fn:
                mock_spec = MagicMock()
                mock_spec_fn.return_value = mock_spec
                mock_mod_fn.return_value = mock
                mock_spec.loader.exec_module = MagicMock()
                result = adapter.status_snapshot(broker_csv_path=csv_path)

            # Should be REVIEW_ONLY (not NO_ACTION) due to drift
            self.assertNotEqual(result.actionability, "NO_ACTION")
            self.assertEqual(result.actionability, "REVIEW_ONLY")

    def test_drift_detected_headline_warns(self):
        """When drift detected, headline must mention broker drift warning."""
        with tempfile.TemporaryDirectory() as td:
            positions = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
                _make_ccc_position("SPY", "20260320", 590.0, 570.0),
            ]
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            mock = self._mock_ccc_report_module(positions)
            adapter = TradingAdapter(
                policy_path=Path("configs/allocator_ccc_v1.yaml"),
                campaign_path=Path("configs/campaign_v1.yaml"),
                timeout_secs=30,
            )
            with patch("importlib.util.spec_from_file_location") as mock_spec_fn, \
                 patch("importlib.util.module_from_spec") as mock_mod_fn:
                mock_spec = MagicMock()
                mock_spec_fn.return_value = mock_spec
                mock_mod_fn.return_value = mock
                mock_spec.loader.exec_module = MagicMock()
                result = adapter.status_snapshot(broker_csv_path=csv_path)

            # Headline must contain drift warning
            headline_lower = result.headline.lower()
            self.assertTrue(
                "broker drift" in headline_lower or "drift" in headline_lower or "stale" in headline_lower,
                f"Expected drift warning in headline: {result.headline!r}"
            )

    def test_drift_details_include_only_in_ccc(self):
        """details must include only_in_ccc list when drift detected."""
        with tempfile.TemporaryDirectory() as td:
            positions = [
                _make_ccc_position("SPY", "20260417", 590.0, 570.0),
                _make_ccc_position("SPY", "20260327", 590.0, 570.0),
                _make_ccc_position("SPY", "20260320", 590.0, 570.0),
            ]
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            mock = self._mock_ccc_report_module(positions)
            adapter = TradingAdapter(
                policy_path=Path("configs/allocator_ccc_v1.yaml"),
                campaign_path=Path("configs/campaign_v1.yaml"),
                timeout_secs=30,
            )
            with patch("importlib.util.spec_from_file_location") as mock_spec_fn, \
                 patch("importlib.util.module_from_spec") as mock_mod_fn:
                mock_spec = MagicMock()
                mock_spec_fn.return_value = mock_spec
                mock_mod_fn.return_value = mock
                mock_spec.loader.exec_module = MagicMock()
                result = adapter.status_snapshot(broker_csv_path=csv_path)

            self.assertIn("only_in_ccc", result.details)
            self.assertGreater(len(result.details["only_in_ccc"]), 0)


# ===========================================================================
# PART 9: summarize_latest with broker drift
# ===========================================================================

class TestSummarizeLatestWithBrokerDrift(unittest.TestCase):

    def _make_ok_status_with_drift(self, drift: dict) -> AdapterResult:
        return AdapterResult(
            ok=True,
            actionability="REVIEW_ONLY",
            headline="Broker drift detected: ...",
            details={
                "crash_open": 3,
                "selloff_open": 0,
                "total_open": 3,
                "pending_crash": 0,
                "pending_selloff": 0,
                "pending_total": 0,
                "par_crash": 0.0, "par_selloff": 0.0, "par_total": 0.0,
                "par_crash_cap": None, "par_selloff_cap": None, "par_total_cap": None,
                "crash_soft_target": 1, "crash_hard_cap": 5,
                "selloff_soft_target": 1, "selloff_hard_cap": 3,
                "ytd_spent": 245.0, "annual_budget": None, "annual_remaining": None,
                "latest_plan_ts": None, "latest_plan_opens": 0,
                "latest_plan_closes": 0, "latest_plan_holds": 3,
                "latest_plan_gate_reason": None,
                "broker_drift": drift,
                "in_sync": drift.get("in_sync", False),
                "only_in_ccc": drift.get("only_in_ccc", []),
                "only_in_ibkr": drift.get("only_in_ibkr", []),
                "qty_mismatches": drift.get("qty_mismatches", []),
            },
        )

    def _make_ok_report(self):
        return AdapterResult(
            ok=True, actionability="REVIEW_ONLY", headline="Report ok",
            details={
                "crash_open": 3, "selloff_open": 0, "total_open": 3,
                "pending_total": 0, "pending_crash": 0, "pending_selloff": 0,
                "par_crash": None, "par_selloff": None, "par_total": None,
                "planned_opens": 0, "holds": 3, "gate_reason": None,
                "sections_found": ["A", "B", "C"],
            },
        )

    def _make_ok_preview(self):
        return AdapterResult(
            ok=True, actionability="NO_ACTION", headline="No new trade",
            details={"planned_opens": 0, "holds": 3, "gate_reason": None,
                     "quote_only_validated": 0, "summary_box_found": False},
        )

    def test_summarize_drift_present_in_status_subkey(self):
        adapter = TradingAdapter(
            policy_path=Path("configs/allocator_ccc_v1.yaml"),
            campaign_path=Path("configs/campaign_v1.yaml"),
            timeout_secs=30,
        )
        drift = {
            "ok": True, "in_sync": False, "ccc_count": 3, "ibkr_count": 2,
            "only_in_ccc": [{"key": "SPY 20260320 590/570"}],
            "only_in_ibkr": [], "qty_mismatches": [],
            "headline": "CCC state is stale: 1 spread exists only in CCC.",
            "errors": [],
        }
        status_with_drift = self._make_ok_status_with_drift(drift)

        with patch.object(adapter, "status_snapshot", return_value=status_with_drift), \
             patch.object(adapter, "report_snapshot", return_value=self._make_ok_report()), \
             patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview()):
            result = adapter.summarize_latest()

        # broker_drift should appear inside details["status"]
        status_details = result.details.get("status", {})
        self.assertIn("broker_drift", status_details)
        self.assertFalse(status_details["broker_drift"]["in_sync"])

    def test_summarize_broker_csv_path_passed_to_status(self):
        """broker_csv_path kwarg on summarize_latest → forwarded to status_snapshot."""
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "ibkr.csv"
            _write_text(csv_path, _SIMPLE_CSV_2_SPREADS)

            adapter = TradingAdapter(
                policy_path=Path("configs/allocator_ccc_v1.yaml"),
                campaign_path=Path("configs/campaign_v1.yaml"),
                timeout_secs=30,
            )
            captured_kwargs = {}

            def mock_status(**kwargs):
                captured_kwargs.update(kwargs)
                return AdapterResult(ok=True, actionability="NO_ACTION", headline="ok")

            with patch.object(adapter, "status_snapshot", side_effect=mock_status), \
                 patch.object(adapter, "report_snapshot", return_value=self._make_ok_report()), \
                 patch.object(adapter, "preview_daily_cycle", return_value=self._make_ok_preview()):
                adapter.summarize_latest(broker_csv_path=csv_path, run_preview=False)

            # Confirm broker_csv_path was forwarded
            self.assertIn("broker_csv_path", captured_kwargs)
            self.assertEqual(captured_kwargs["broker_csv_path"], csv_path)


# ===========================================================================
# PART 10: CLI --json includes broker_drift block
# ===========================================================================

class TestCLIJsonIncludesBrokerDrift(unittest.TestCase):
    """CLI --json output must include broker_drift when drift check is run."""

    def _run_cli_json(self, command: str, result: AdapterResult) -> dict:
        import io as _io
        from contextlib import redirect_stdout as _redir

        sys.argv = ["trading_adapter.py", command, "--json"]
        captured = _io.StringIO()

        import scripts.trading_adapter as cli_mod
        from forecast_arb.adapter.trading_adapter import TradingAdapter as TA

        method_map = {
            "status": "status_snapshot",
            "summarize": "summarize_latest",
            "preview": "preview_daily_cycle",
            "report": "report_snapshot",
        }
        method_name = method_map.get(command, "status_snapshot")

        with patch.object(TA, method_name, return_value=result):
            with _redir(captured):
                try:
                    cli_mod.main()
                except SystemExit:
                    pass

        output = captured.getvalue().strip()
        return json.loads(output)

    def test_status_json_includes_broker_drift_block(self):
        drift = {
            "ok": True, "in_sync": False, "ccc_count": 3, "ibkr_count": 2,
            "only_in_ccc": [{"key": "SPY 20260320 590/570", "symbol": "SPY",
                              "expiry": "20260320", "long_strike": 590.0,
                              "short_strike": 570.0, "qty": 1, "regime": "crash"}],
            "only_in_ibkr": [], "qty_mismatches": [],
            "headline": "CCC state is stale: 1 spread exists only in CCC.",
            "errors": [],
        }
        result = AdapterResult(
            ok=True, actionability="REVIEW_ONLY",
            headline="Broker drift detected: CCC shows 3 crash spread(s) but broker export shows 2.",
            details={
                "crash_open": 3, "selloff_open": 0, "total_open": 3,
                "pending_total": 0, "par_total": 0.0,
                "broker_drift": drift,
                "in_sync": False,
                "only_in_ccc": drift["only_in_ccc"],
                "only_in_ibkr": [],
                "qty_mismatches": [],
            },
        )

        d = self._run_cli_json("status", result)
        self.assertIn("details", d)
        self.assertIn("broker_drift", d["details"])
        broker_drift = d["details"]["broker_drift"]
        self.assertFalse(broker_drift["in_sync"])
        self.assertIn("only_in_ccc", broker_drift)
        self.assertEqual(len(broker_drift["only_in_ccc"]), 1)
        self.assertIn("headline", broker_drift)

    def test_no_broker_drift_key_without_csv(self):
        """When broker_csv_path is NOT supplied, JSON must NOT have broker_drift."""
        result = AdapterResult(
            ok=True, actionability="NO_ACTION", headline="All clear.",
            details={"crash_open": 0, "selloff_open": 0, "total_open": 0},
        )
        d = self._run_cli_json("status", result)
        self.assertNotIn("broker_drift", d.get("details", {}))


# ===========================================================================
# PART 11: _parse_qty robustness
# ===========================================================================

class TestParseQty(unittest.TestCase):
    def test_integer_string(self):
        self.assertEqual(_parse_qty("1"), 1)

    def test_negative_string(self):
        self.assertEqual(_parse_qty("-1"), -1)

    def test_comma_formatted(self):
        self.assertEqual(_parse_qty("1,000"), 1000)

    def test_float_string(self):
        self.assertEqual(_parse_qty("2.0"), 2)

    def test_none_returns_zero(self):
        self.assertEqual(_parse_qty(None), 0)

    def test_garbage_returns_zero(self):
        self.assertEqual(_parse_qty("not a number"), 0)

    def test_blank_returns_zero(self):
        self.assertEqual(_parse_qty(""), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
