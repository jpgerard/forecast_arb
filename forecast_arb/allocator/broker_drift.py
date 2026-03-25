"""
forecast_arb.allocator.broker_drift
====================================
CCC v2.2 — Broker-State Drift Detector

PURPOSE
-------
Detect when CCC internal state (positions.json) disagrees with current
IBKR broker truth (exported CSV).  Detection-only patch — no automatic
repair, no file writes, no allocation logic changes.

This module answers:
  "Does CCC positions.json match current IBKR option spread positions?"

All functions are PURE (no side effects, no file writes).  Unit-testable
without IBKR API.  No external dependencies.

PUBLIC API
----------
    load_ccc_positions(positions_path)        → list[dict]
    load_ibkr_positions_from_csv(csv_path)    → list[dict]  (raw CSV rows)
    normalize_ccc_spread_positions(positions) → list[dict]  (normalised)
    normalize_ibkr_spread_positions(rows)     → list[dict]  (normalised)
    diff_ccc_vs_ibkr(ccc_norm, ibkr_norm)    → dict         (diff result)

NORMALIZED SPREAD RECORD SHAPE
-------------------------------
Each normalised spread record is a plain dict with these guaranteed keys:
    {
        "symbol":        str,    # e.g. "SPY"
        "expiry":        str,    # "YYYYMMDD"
        "long_strike":   float,  # higher put — the BUY leg
        "short_strike":  float,  # lower put  — the SELL leg
        "qty":           int,    # number of spread contracts
        "regime":        str,    # "crash" | "selloff" | "unknown"
        "_key":          tuple,  # (symbol, expiry, long_strike, short_strike) — dedup key
        "_source":       str,    # "ccc" | "ibkr"
        "_raw":          dict,   # original source record (for debugging)
    }

DIFF RESULT SHAPE
-----------------
    {
        "ok":           bool,    # True when no errors occurred
        "in_sync":      bool,    # True when CCC matches IBKR exactly
        "ccc_count":    int,
        "ibkr_count":   int,
        "only_in_ccc":  list[dict],   # spread records in CCC but absent in IBKR
        "only_in_ibkr": list[dict],   # spread records in IBKR but absent in CCC
        "qty_mismatches": list[dict], # {key, ccc_qty, ibkr_qty, ccc_record, ibkr_record}
        "headline":     str,     # human-readable summary line
        "errors":       list[str],
    }

IBKR CSV FORMAT SUPPORT
-----------------------
v2.2 supports CSV-based broker truth (no live API required).

Supported CSV inputs:
  1. IBKR Activity Statement "Positions" section (multi-section CSV)
     Header row contains: Symbol, Quantity, Mult, Type, etc.
     Option rows have Type == "OPT" and Symbol like "SPY 17APR26 590 P"
  2. Simple position export (header on first non-blank row)
     Flexible column mapping; auto-detected.
  3. BAG (combo) rows: parsed as a direct spread if cols present.

IBKR option symbol formats handled:
    "SPY 17APR26 590 P"      → SPY, 20260417, 590.0, Put
    "SPY 17APR2026 590 P"    → SPY, 20260417, 590.0, Put
    "SPY 20260417 590.0 P"   → SPY, 20260417, 590.0, Put
    "SPY260417P590"           → SPY, 20260417, 590.0, Put  (OCC-style)

NON-NEGOTIABLE INVARIANTS (CCC v2.2)
-------------------------------------
* Read-only: no file writes, no side effects.
* No automatic broker-state repair.
* No changes to allocator trade logic.
* No external dependencies.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}

# Minimum columns that must be present in an IBKR option row to produce a spread
_REQUIRED_SPREAD_FIELDS = {"symbol", "expiry", "long_strike", "short_strike"}


# ---------------------------------------------------------------------------
# Task A — load_ccc_positions
# ---------------------------------------------------------------------------

def load_ccc_positions(positions_path: Any) -> List[Dict[str, Any]]:
    """
    Load positions.json (CCC internal state).

    Parameters
    ----------
    positions_path : str | Path
        Path to positions.json.  File may be absent (returns []).

    Returns
    -------
    List of position dicts as stored in positions.json.  Returns [] on
    missing file or parse error (logged as warning, not raised).
    """
    p = Path(positions_path)
    if not p.exists():
        log.debug("broker_drift: positions file absent at %s — returning []", p)
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("broker_drift: positions.json is not a list — returning []")
            return []
        return data
    except Exception as exc:
        log.warning("broker_drift: failed to load positions.json: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Task B — load_ibkr_positions_from_csv
# ---------------------------------------------------------------------------

def load_ibkr_positions_from_csv(csv_path: Any) -> List[Dict[str, Any]]:
    """
    Load raw rows from an IBKR exported positions CSV.

    Handles two common IBKR CSV layouts:

    Layout A — Activity Statement (multi-section):
        The CSV has section rows like "Positions,Header,Symbol,Quantity,..."
        followed by "Positions,Data,..." data rows.  Each row has a leading
        "section" and "type" column.

    Layout B — Simple export (plain CSV):
        First non-blank / non-comment row is the header.
        Data rows follow directly.

    In both cases the function returns a list of plain dicts with lowercased
    keys, making downstream normalization layout-agnostic.

    Parameters
    ----------
    csv_path : str | Path
        Path to the IBKR positions CSV.  File may be absent (returns []).

    Returns
    -------
    List of plain dicts (column name → value); all keys lowercased.
    Never raises — returns [] with a logged warning on any error.
    """
    p = Path(csv_path)
    if not p.exists():
        log.warning("broker_drift: IBKR CSV absent at %s — returning []", p)
        return []

    try:
        rows: List[Dict[str, Any]] = []
        with open(p, encoding="utf-8-sig", newline="") as f:
            text = f.read()

        # Detect layout
        layout = _detect_csv_layout(text)
        log.debug("broker_drift: detected CSV layout %r for %s", layout, p)

        if layout == "activity_statement":
            rows = _parse_activity_statement_csv(text)
        else:
            rows = _parse_simple_csv(text)

        log.debug("broker_drift: loaded %d raw rows from %s", len(rows), p)
        return rows

    except Exception as exc:
        log.warning("broker_drift: failed to load IBKR CSV %s: %s", p, exc)
        return []


def _detect_csv_layout(text: str) -> str:
    """
    Return "activity_statement" if this looks like an IBKR activity statement
    (has section markers like "Positions,Header,..."), else "simple".
    """
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("Positions,Header,") or stripped.startswith("Positions,Data,"):
            return "activity_statement"
        # Some activity statements use "Open Positions,Header,..."
        if re.match(r"^(Open )?Positions,[Hh]eader,", stripped):
            return "activity_statement"
    return "simple"


def _parse_activity_statement_csv(text: str) -> List[Dict[str, Any]]:
    """
    Parse IBKR activity statement CSV.

    Rows look like:
        Positions,Header,Symbol,Quantity,Mult,Cost Price,Close Price,Value,...
        Positions,Data,SPY 17APR26 590 P,1,100,58.20,62.00,...
    """
    rows: List[Dict[str, Any]] = []
    header: Optional[List[str]] = None
    header_prefix: Optional[str] = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        parts = _csv_split(stripped)
        if not parts:
            continue

        section_key = parts[0].strip().lower()
        row_type = parts[1].strip().lower() if len(parts) > 1 else ""

        # Detect header for "Positions" or "Open Positions" sections
        if row_type == "header" and "position" in section_key:
            header = [c.strip().lower() for c in parts[2:]]
            header_prefix = section_key
            continue

        # Data row for matching section
        if row_type == "data" and header is not None and section_key == header_prefix:
            data_cols = parts[2:]
            row_dict = dict(zip(header, data_cols))
            rows.append(row_dict)

    return rows


def _parse_simple_csv(text: str) -> List[Dict[str, Any]]:
    """
    Parse a simple flat CSV (first non-blank/non-comment row = header).
    """
    rows: List[Dict[str, Any]] = []
    header: Optional[List[str]] = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = _csv_split(stripped)
        if not parts:
            continue

        if header is None:
            header = [c.strip().lower() for c in parts]
            continue

        row_dict = dict(zip(header, [v.strip() for v in parts]))
        rows.append(row_dict)

    return rows


def _csv_split(line: str) -> List[str]:
    """Split a CSV line respecting quoted fields."""
    try:
        reader = csv.reader([line])
        return next(reader)
    except Exception:
        return [c.strip() for c in line.split(",")]


# ---------------------------------------------------------------------------
# Task A — normalize_ccc_spread_positions
# ---------------------------------------------------------------------------

def normalize_ccc_spread_positions(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert CCC positions.json entries to normalised spread records.

    Filters to positions that look like bear put spreads (have two strikes,
    an underlier, and an expiry).  Skips malformed entries with a log warning.

    Parameters
    ----------
    positions : list of position dicts from positions.json

    Returns
    -------
    List of normalised spread dicts (see module docstring for shape).
    """
    result: List[Dict[str, Any]] = []

    for pos in positions:
        try:
            rec = _normalize_ccc_position(pos)
            if rec is not None:
                result.append(rec)
        except Exception as exc:
            log.warning("broker_drift: skipping malformed CCC position %r: %s", pos, exc)

    log.debug("broker_drift: normalized %d CCC spread positions", len(result))
    return result


def _normalize_ccc_position(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a single CCC position dict.  Returns None if not a spread."""
    underlier = str(pos.get("underlier") or pos.get("symbol") or "").upper()
    if not underlier:
        return None

    expiry_raw = str(pos.get("expiry") or "").replace("-", "")
    if not expiry_raw or len(expiry_raw) != 8:
        return None

    strikes = pos.get("strikes") or []
    if len(strikes) < 2:
        return None

    try:
        long_strike = float(strikes[0])
        short_strike = float(strikes[1])
    except (TypeError, ValueError):
        return None

    if long_strike <= 0 or short_strike <= 0:
        return None

    # CCC convention: strikes[0] = long (higher), strikes[1] = short (lower)
    # Ensure long > short (robustness)
    if long_strike < short_strike:
        long_strike, short_strike = short_strike, long_strike

    qty = int(pos.get("qty_open") or pos.get("qty") or 1)
    regime = str(pos.get("regime") or "crash").lower()
    if regime not in ("crash", "selloff"):
        regime = "unknown"

    key = (underlier, expiry_raw, long_strike, short_strike)

    return {
        "symbol":       underlier,
        "expiry":       expiry_raw,
        "long_strike":  long_strike,
        "short_strike": short_strike,
        "qty":          qty,
        "regime":       regime,
        "_key":         key,
        "_source":      "ccc",
        "_raw":         pos,
    }


# ---------------------------------------------------------------------------
# Task B — normalize_ibkr_spread_positions
# ---------------------------------------------------------------------------

def normalize_ibkr_spread_positions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert raw IBKR CSV rows to normalised spread records.

    Strategy:
    1. First pass: look for BAG/combo rows that represent a full spread.
    2. Second pass: group individual option leg rows (puts only) by
       expiry+underlier into matched long/short pairs.

    Rows that are equities, unknown instruments, or can't be parsed are
    silently skipped (no crash).

    Parameters
    ----------
    rows : list of raw CSV dicts from load_ibkr_positions_from_csv()

    Returns
    -------
    List of normalised spread dicts.
    """
    result: List[Dict[str, Any]] = []
    found_keys: set = set()

    # --- Pass 1: BAG / combo rows ---
    for row in rows:
        try:
            rec = _try_parse_bag_row(row)
            if rec is not None and rec["_key"] not in found_keys:
                result.append(rec)
                found_keys.add(rec["_key"])
        except Exception as exc:
            log.debug("broker_drift: BAG parse skipped row %r: %s", row, exc)

    # --- Pass 2: group individual option legs into spreads ---
    try:
        leg_spreads = _group_option_legs_into_spreads(rows, found_keys)
        result.extend(leg_spreads)
        for rec in leg_spreads:
            found_keys.add(rec["_key"])
    except Exception as exc:
        log.warning("broker_drift: option leg grouping failed: %s", exc)

    log.debug("broker_drift: normalized %d IBKR spread positions", len(result))
    return result


def _try_parse_bag_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Attempt to parse a BAG/combo CSV row as a single spread record.

    IBKR BAG rows may look like:
        symbol = "SPY BAG / SPY 17APR26 590 P, SPY 17APR26 570 P"
        or have explicit columns: long_leg, short_leg, expiry, ...

    Returns None if the row is not identifiable as a BAG spread.
    """
    sym_col = (
        row.get("symbol") or row.get("sym") or row.get("description") or ""
    ).strip()

    # Reject if clearly an equity or blank
    if not sym_col:
        return None

    type_col = (row.get("type") or row.get("asset class") or row.get("asset_class") or "").strip().upper()
    if type_col and type_col not in ("BAG", "OPT", ""):
        return None

    # Check for BAG keyword
    if "BAG" not in sym_col.upper():
        return None

    # Try to extract two option legs from the BAG symbol string
    # Pattern: "SPY BAG / SPY 17APR26 590 P, SPY 17APR26 570 P"
    legs_part = sym_col
    if "/" in sym_col:
        legs_part = sym_col.split("/", 1)[1]

    # Split on comma to get individual legs
    leg_strs = [l.strip() for l in legs_part.split(",")]
    parsed_legs = []
    for leg_str in leg_strs:
        parsed = _parse_option_symbol(leg_str)
        if parsed:
            parsed_legs.append(parsed)

    if len(parsed_legs) < 2:
        return None

    # Determine long and short legs from qty if available
    # Otherwise assume first is long (higher strike), second is short
    legs_sorted = sorted(parsed_legs, key=lambda x: -x["strike"])
    long_leg = legs_sorted[0]
    short_leg = legs_sorted[1]

    if long_leg["underlier"] != short_leg["underlier"]:
        return None
    if long_leg["expiry"] != short_leg["expiry"]:
        return None

    # Qty for the BAG
    qty = _parse_qty(row.get("quantity") or row.get("qty") or "1")
    qty = abs(qty) if qty != 0 else 1

    underlier = long_leg["underlier"]
    expiry = long_leg["expiry"]
    long_strike = long_leg["strike"]
    short_strike = short_leg["strike"]
    key = (underlier, expiry, long_strike, short_strike)

    return {
        "symbol":       underlier,
        "expiry":       expiry,
        "long_strike":  long_strike,
        "short_strike": short_strike,
        "qty":          qty,
        "regime":       "unknown",
        "_key":         key,
        "_source":      "ibkr",
        "_raw":         row,
    }


def _group_option_legs_into_spreads(
    rows: List[Dict[str, Any]],
    already_found: set,
) -> List[Dict[str, Any]]:
    """
    Group individual OPT put rows into long/short pairs (spreads).

    Algorithm:
    1. Parse each row that looks like a SPY put option.
    2. Group by (underlier, expiry).
    3. Within each group, pair positive-qty (long) with negative-qty (short) legs.
    4. Sort matched legs so long (higher strike) and short (lower strike) are identified.

    Handles qty normalization:
      - Positive qty = long position (BUY leg)
      - Negative qty = short position (SELL leg)
    """
    # Parse all eligible option rows
    leg_candidates: List[Dict[str, Any]] = []

    for row in rows:
        sym_col = (
            row.get("symbol") or row.get("sym") or ""
        ).strip()
        if not sym_col:
            continue

        # Skip BAG rows (already handled)
        if "BAG" in sym_col.upper():
            continue

        type_col = (
            row.get("type") or row.get("asset class") or row.get("asset_class") or ""
        ).strip().upper()
        if type_col and type_col not in ("OPT", "P", "PUT", ""):
            continue

        parsed = _parse_option_symbol(sym_col)
        if parsed is None:
            # Try to parse from separate columns
            parsed = _parse_option_from_cols(row)
        if parsed is None:
            continue

        # Only care about puts
        if parsed.get("opt_type", "P").upper() not in ("P", "PUT"):
            continue

        qty = _parse_qty(row.get("quantity") or row.get("qty") or "0")
        if qty == 0:
            continue

        parsed["qty"] = qty
        parsed["_raw"] = row
        leg_candidates.append(parsed)

    if not leg_candidates:
        return []

    # Group by (underlier, expiry)
    groups: Dict[Tuple, List[Dict]] = {}
    for leg in leg_candidates:
        gk = (leg["underlier"], leg["expiry"])
        groups.setdefault(gk, []).append(leg)

    result: List[Dict[str, Any]] = []

    for (underlier, expiry), legs in groups.items():
        long_legs  = [l for l in legs if l["qty"] > 0]   # BUY legs
        short_legs = [l for l in legs if l["qty"] < 0]   # SELL legs

        if not long_legs or not short_legs:
            continue

        # Match: highest long strike with lowest (most negative) short strike
        # to reconstruct the bear put spread
        long_legs_sorted  = sorted(long_legs,  key=lambda x: -x["strike"])
        short_legs_sorted = sorted(short_legs, key=lambda x: x["strike"])

        n_pairs = min(len(long_legs_sorted), len(short_legs_sorted))
        for i in range(n_pairs):
            ll = long_legs_sorted[i]
            sl = short_legs_sorted[i]

            long_strike  = ll["strike"]
            short_strike = sl["strike"]

            if long_strike <= short_strike:
                # Not a bear put spread shape
                continue

            # Qty = min of abs quantities for the pair
            qty = min(abs(ll["qty"]), abs(sl["qty"]))

            key = (underlier, expiry, long_strike, short_strike)
            if key in already_found:
                continue

            result.append({
                "symbol":       underlier,
                "expiry":       expiry,
                "long_strike":  long_strike,
                "short_strike": short_strike,
                "qty":          qty,
                "regime":       "unknown",
                "_key":         key,
                "_source":      "ibkr",
                "_raw":         {"long_leg": ll["_raw"], "short_leg": sl["_raw"]},
            })
            already_found.add(key)

    return result


# ---------------------------------------------------------------------------
# Option symbol parsing helpers
# ---------------------------------------------------------------------------

# Compiled patterns for IBKR option symbol formats:
#
# Format 1: "SPY 17APR26 590 P"  or  "SPY 17APR2026 590 P"
_OPT_SYMBOL_ALPHA_MONTH = re.compile(
    r"^([A-Z]+)\s+"                        # underlier
    r"(\d{1,2})([A-Z]{3})(\d{2,4})\s+"    # DD MON YY/YYYY
    r"([\d.]+)\s+"                         # strike
    r"([CP])$",                            # C or P
    re.IGNORECASE,
)

# Format 2: "SPY 20260417 590.0 P"  (CCC-style numeric date)
_OPT_SYMBOL_NUMERIC_DATE = re.compile(
    r"^([A-Z]+)\s+"                        # underlier
    r"(\d{8})\s+"                          # YYYYMMDD
    r"([\d.]+)\s+"                         # strike
    r"([CP])$",                            # C or P
    re.IGNORECASE,
)

# Format 3: OCC-style "SPY260417P590" or "SPY   260417P00590000"
_OPT_SYMBOL_OCC = re.compile(
    r"^([A-Z]+)\s*"                        # underlier
    r"(\d{6})"                             # YYMMDD
    r"([CP])"                              # C or P
    r"(\d+)"                               # strike * 1000 or integer
    r"$",
    re.IGNORECASE,
)


def _parse_option_symbol(sym: str) -> Optional[Dict[str, Any]]:
    """
    Parse an IBKR option symbol string into underlier, expiry, strike, opt_type.

    Returns None if the string cannot be parsed as a recognised option format.
    """
    sym = sym.strip()

    # Format 1: alpha month (e.g. "SPY 17APR26 590 P")
    m = _OPT_SYMBOL_ALPHA_MONTH.match(sym)
    if m:
        underlier = m.group(1).upper()
        day  = m.group(2).zfill(2)
        mon  = _MONTH_MAP.get(m.group(3).upper())
        yr   = m.group(4)
        if not mon:
            return None
        if len(yr) == 2:
            yr = "20" + yr
        expiry = f"{yr}{mon}{day}"
        strike = float(m.group(5))
        opt_type = m.group(6).upper()
        return {"underlier": underlier, "expiry": expiry, "strike": strike, "opt_type": opt_type}

    # Format 2: numeric date (e.g. "SPY 20260417 590.0 P")
    m = _OPT_SYMBOL_NUMERIC_DATE.match(sym)
    if m:
        underlier = m.group(1).upper()
        expiry = m.group(2)
        strike = float(m.group(3))
        opt_type = m.group(4).upper()
        return {"underlier": underlier, "expiry": expiry, "strike": strike, "opt_type": opt_type}

    # Format 3: OCC-style (e.g. "SPY260417P590")
    m = _OPT_SYMBOL_OCC.match(sym)
    if m:
        underlier = m.group(1).upper()
        yymmdd = m.group(2)
        opt_type = m.group(3).upper()
        raw_strike = m.group(4)
        # OCC format: strike * 1000 padded to 8 digits
        # If 8+ chars, divide by 1000; otherwise treat as integer
        if len(raw_strike) >= 7:
            strike = float(raw_strike) / 1000.0
        else:
            strike = float(raw_strike)
        year = "20" + yymmdd[:2]
        month = yymmdd[2:4]
        day = yymmdd[4:6]
        expiry = f"{year}{month}{day}"
        return {"underlier": underlier, "expiry": expiry, "strike": strike, "opt_type": opt_type}

    return None


def _parse_option_from_cols(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Attempt to parse option fields from separate CSV columns when the symbol
    column alone doesn't contain the full description.

    Looks for columns: symbol/underlier, expiry/expiration, strike, type/put_call.
    """
    underlier = (
        row.get("symbol") or row.get("underlier") or row.get("sym") or ""
    ).strip().upper()

    if not underlier:
        return None

    # Expiry: try multiple column names
    expiry_raw = (
        row.get("expiry") or row.get("expiration") or row.get("exp")
        or row.get("last_trading_day") or row.get("maturity") or ""
    ).strip().replace("-", "").replace("/", "")

    if not expiry_raw:
        return None

    # Normalise expiry to YYYYMMDD
    if len(expiry_raw) == 8 and expiry_raw.isdigit():
        expiry = expiry_raw  # already YYYYMMDD
    elif len(expiry_raw) == 6 and expiry_raw.isdigit():
        # MMDDYY
        expiry = "20" + expiry_raw[4:] + expiry_raw[:2] + expiry_raw[2:4]
    else:
        return None

    # Strike
    strike_raw = (
        row.get("strike") or row.get("strike_price") or row.get("strikeprice") or ""
    ).strip()
    if not strike_raw:
        return None
    try:
        strike = float(strike_raw)
    except ValueError:
        return None

    # Option type
    opt_type = (
        row.get("type") or row.get("put_call") or row.get("right") or row.get("call_put") or "P"
    ).strip().upper()
    if opt_type not in ("P", "C", "PUT", "CALL"):
        opt_type = "P"  # default to put for our use case

    return {"underlier": underlier, "expiry": expiry, "strike": strike, "opt_type": opt_type}


def _parse_qty(val: Any) -> int:
    """Parse a quantity value (may be string with commas, may be negative)."""
    if val is None:
        return 0
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Task A — diff_ccc_vs_ibkr
# ---------------------------------------------------------------------------

def diff_ccc_vs_ibkr(
    ccc_positions: List[Dict[str, Any]],
    ibkr_positions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compare normalised CCC spread positions against normalised IBKR spread positions.

    Parameters
    ----------
    ccc_positions  : output of normalize_ccc_spread_positions()
    ibkr_positions : output of normalize_ibkr_spread_positions()

    Returns
    -------
    Structured diff dict:
        {
            "ok":             bool,
            "in_sync":        bool,
            "ccc_count":      int,
            "ibkr_count":     int,
            "only_in_ccc":    list[dict],    # spread records in CCC not in IBKR
            "only_in_ibkr":   list[dict],    # spread records in IBKR not in CCC
            "qty_mismatches": list[dict],    # {key_str, ccc_qty, ibkr_qty}
            "headline":       str,
            "errors":         list[str],
        }

    Contract-match key: (symbol, expiry, long_strike, short_strike)
    Regime is NOT included in the match key (IBKR CSV may not carry regime).
    """
    errors: List[str] = []

    # Index by key
    ccc_by_key: Dict[tuple, Dict[str, Any]] = {}
    for rec in ccc_positions:
        k = rec.get("_key")
        if k:
            ccc_by_key[k] = rec

    ibkr_by_key: Dict[tuple, Dict[str, Any]] = {}
    for rec in ibkr_positions:
        k = rec.get("_key")
        if k:
            ibkr_by_key[k] = rec

    ccc_keys  = set(ccc_by_key.keys())
    ibkr_keys = set(ibkr_by_key.keys())

    only_in_ccc_keys  = ccc_keys  - ibkr_keys
    only_in_ibkr_keys = ibkr_keys - ccc_keys
    matched_keys      = ccc_keys  & ibkr_keys

    # Qty mismatches for matched keys
    qty_mismatches: List[Dict[str, Any]] = []
    for k in matched_keys:
        ccc_rec  = ccc_by_key[k]
        ibkr_rec = ibkr_by_key[k]
        ccc_qty  = ccc_rec.get("qty", 0)
        ibkr_qty = ibkr_rec.get("qty", 0)
        if ccc_qty != ibkr_qty:
            qty_mismatches.append({
                "key":        _key_to_str(k),
                "ccc_qty":    ccc_qty,
                "ibkr_qty":   ibkr_qty,
                "ccc_record": _safe_record_summary(ccc_rec),
                "ibkr_record": _safe_record_summary(ibkr_rec),
            })

    only_in_ccc  = [_safe_record_summary(ccc_by_key[k])  for k in sorted(only_in_ccc_keys,  key=_key_sort)]
    only_in_ibkr = [_safe_record_summary(ibkr_by_key[k]) for k in sorted(only_in_ibkr_keys, key=_key_sort)]

    in_sync = (
        len(only_in_ccc) == 0
        and len(only_in_ibkr) == 0
        and len(qty_mismatches) == 0
    )

    headline = _build_drift_headline(
        in_sync=in_sync,
        only_in_ccc=only_in_ccc,
        only_in_ibkr=only_in_ibkr,
        qty_mismatches=qty_mismatches,
        ccc_count=len(ccc_positions),
        ibkr_count=len(ibkr_positions),
    )

    return {
        "ok":             True,
        "in_sync":        in_sync,
        "ccc_count":      len(ccc_positions),
        "ibkr_count":     len(ibkr_positions),
        "only_in_ccc":    only_in_ccc,
        "only_in_ibkr":   only_in_ibkr,
        "qty_mismatches": qty_mismatches,
        "headline":       headline,
        "errors":         errors,
    }


# ---------------------------------------------------------------------------
# Convenience end-to-end entry point
# ---------------------------------------------------------------------------

def check_broker_drift(
    positions_path: Any,
    csv_path: Any,
) -> Dict[str, Any]:
    """
    End-to-end convenience wrapper: load CCC state, load IBKR CSV, diff.

    Parameters
    ----------
    positions_path : str | Path  — path to positions.json
    csv_path       : str | Path  — path to IBKR positions CSV

    Returns
    -------
    Same dict shape as diff_ccc_vs_ibkr(), with "ok"=False and errors list
    populated if file loading fails.
    """
    errors: List[str] = []

    ccc_raw = load_ccc_positions(positions_path)
    ibkr_raw = load_ibkr_positions_from_csv(csv_path)

    if not Path(csv_path).exists():
        errors.append(f"IBKR CSV not found: {csv_path}")
        return {
            "ok": False,
            "in_sync": False,
            "ccc_count": len(ccc_raw),
            "ibkr_count": 0,
            "only_in_ccc": [],
            "only_in_ibkr": [],
            "qty_mismatches": [],
            "headline": f"Broker drift check failed: IBKR CSV not found at {csv_path}.",
            "errors": errors,
        }

    ccc_norm  = normalize_ccc_spread_positions(ccc_raw)
    ibkr_norm = normalize_ibkr_spread_positions(ibkr_raw)
    result    = diff_ccc_vs_ibkr(ccc_norm, ibkr_norm)
    result["errors"] = errors + result.get("errors", [])
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _key_to_str(k: tuple) -> str:
    """Convert a spread key tuple to a human-readable string."""
    sym, expiry, ls, ss = k
    return f"{sym} {expiry} {ls:.0f}/{ss:.0f}"


def _key_sort(k: tuple) -> tuple:
    """Sort key for deterministic ordering."""
    return (str(k[0]), str(k[1]), float(k[2] or 0), float(k[3] or 0))


def _safe_record_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Return a serializable summary of a spread record (drop _raw to avoid clutter)."""
    return {
        "symbol":       rec.get("symbol", ""),
        "expiry":       rec.get("expiry", ""),
        "long_strike":  rec.get("long_strike"),
        "short_strike": rec.get("short_strike"),
        "qty":          rec.get("qty"),
        "regime":       rec.get("regime", ""),
        "key":          _key_to_str(rec["_key"]) if rec.get("_key") else "",
    }


def _build_drift_headline(
    in_sync: bool,
    only_in_ccc: List[Dict],
    only_in_ibkr: List[Dict],
    qty_mismatches: List[Dict],
    ccc_count: int,
    ibkr_count: int,
) -> str:
    """Build a one-line human-readable drift headline."""
    if in_sync:
        return (
            f"CCC state is in sync with broker: {ccc_count} spread(s) matched."
        )

    parts: List[str] = []

    if only_in_ccc:
        n = len(only_in_ccc)
        parts.append(
            f"{n} spread{'s' if n != 1 else ''} exist{'s' if n == 1 else ''} "
            f"only in CCC (not in IBKR export)"
        )

    if only_in_ibkr:
        n = len(only_in_ibkr)
        parts.append(
            f"{n} spread{'s' if n != 1 else ''} exist{'s' if n == 1 else ''} "
            f"only in IBKR export (not in CCC)"
        )

    if qty_mismatches:
        n = len(qty_mismatches)
        parts.append(
            f"{n} qty mismatch{'es' if n != 1 else ''} between CCC and IBKR"
        )

    summary = "; ".join(parts)
    return (
        f"CCC state is stale: {summary}. "
        f"CCC shows {ccc_count} crash spread(s), broker export shows {ibkr_count}. "
        f"Refresh sync before trusting summary."
    )
