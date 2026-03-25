"""
CCC v1.7 — Fills Ledger and Position Reconciliation.

Authoritative fill ingestion pipeline:
  1. Read execution_result.json (from intents/allocator/ or custom path)
  2. Read the corresponding OPEN_*.json intent for regime/candidate metadata
  3. Compute entry_debit_gross from leg quotes (or market_debit fallback)
  4. Append a POSITION_OPENED row to allocator_fills_ledger.jsonl (dedup by intent_id)
  5. Rebuild positions.json snapshot from full fills ledger
  6. Archive OPEN_*.json + execution_result.json into _archive/YYYYMMDD/

All core logic is pure-function — accepts parsed dicts + Path objects.
No IBKR dependency (ib_insync never imported here).

Fills ledger row schema (CCC v1.7 spec §F):
  {
    "date": "YYYY-MM-DD",
    "timestamp_utc": "...",
    "action": "POSITION_OPENED",
    "policy_id": "ccc_v1",
    "mode": "paper|live",
    "intent_id": "<sha1 or null>",
    "intent_path": "...",
    "candidate_id": "...",
    "regime": "crash|selloff",
    "underlier": "SPY|QQQ",
    "expiry": "YYYYMMDD",
    "strikes": [long_put, short_put],
    "qty": <int>,
    "entry_debit_gross": <float dollars per contract>,
    "entry_debit_net": <float or null>,
    "commissions": <float or null>,
    "ibkr": {
      "orderId": <int or null>,
      "permId": <int or null>,
      "conIds": [],
      "fills": []
    },
    "source": "execution_result|ibkr_fills"
  }

positions.json snapshot (§G):
  [
    {
      "position_id": "<intent_id or hash>",
      "policy_id": "...",
      "mode": "...",
      "regime": "...",
      "underlier": "...",
      "expiry": "...",
      "strikes": [..],
      "qty_open": <int>,
      "entry_debit_gross": ...,
      "entry_debit_net": ...,
      "opened_utc": "...",
      "source": "execution_result|ibkr_fills"
    },
    ...
  ]
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public path constants (defaults; callers may override)
# ---------------------------------------------------------------------------

DEFAULT_FILLS_LEDGER_PATH = Path("runs/allocator/allocator_fills_ledger.jsonl")
DEFAULT_POSITIONS_PATH = Path("runs/allocator/positions.json")
DEFAULT_INTENTS_DIR = Path("intents/allocator")
DEFAULT_ARCHIVE_BASE = Path("intents/allocator/_archive")

# ---------------------------------------------------------------------------
# Staged-order detection helper (CCC v1.8 §E)
# ---------------------------------------------------------------------------

def _is_staged_only(exec_result: Dict[str, Any]) -> bool:
    """
    Return True if the execution_result indicates the order was STAGED but NOT filled.

    Staged = order placed on exchange with transmit=false (paper staging) OR
    status explicitly contains "STAGED" (e.g. "STAGED_PAPER").

    IMPORTANT: Only respond to EXPLICIT staging signals. Do NOT infer "staged"
    from absence of fills — that would falsely classify real fills (where the
    exec_result simply doesn't include ibkr fill data) as staged.

    A staged order:
    - SHOULD block duplicate OPEN (pending semantics)
    - SHOULD NOT create a position.json entry (not yet filled)
    - SHOULD produce an ORDER_STAGED row in fills ledger

    Returns:
        True  → explicitly staged, write ORDER_STAGED
        False → treat as fill (or unknown state), write POSITION_OPENED
    """
    # Explicit staged status text (case-insensitive match for "STAGED")
    status = str(exec_result.get("status", "")).upper()
    if "STAGED" in status:
        return True
    # transmit=false is a paper staging flag set by execute_trade
    if exec_result.get("transmit") is False:
        return True
    # No other inference — absence of fills is NOT evidence of staging
    return False



# ---------------------------------------------------------------------------
# Timezone helper (mirrors ccc_execute._get_local_date)
# ---------------------------------------------------------------------------

def _get_eastern_date(dt: Optional[datetime] = None) -> str:
    """Return YYYY-MM-DD in America/New_York timezone (falls back to UTC)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        pass
    try:
        import pytz  # type: ignore[import]
        return dt.astimezone(pytz.timezone("America/New_York")).date().isoformat()
    except Exception:
        pass
    return dt.astimezone(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Entry debit computation (pure, from execution_result dict)
# ---------------------------------------------------------------------------

def _compute_entry_debit_gross(exec_result: Dict[str, Any]) -> Optional[float]:
    """
    Compute entry_debit_gross (dollars per contract) from execution_result.

    Priority (spec: use fill prices, fall back to quotes if fills missing):
      1. ibkr.fills: (buy_fill_price - sell_fill_price) * 100  — actual fill prices
      2. leg_quotes:  (buy_ask - sell_bid) * 100               — proxy from market quotes
      3. market_debit * 100                                     — pre-computed by execute_trade
      4. None                                                    — not available

    Note: all prices are per-share; multiply by 100 for per-contract dollars.
    """
    # Priority 1: actual fill prices from IBKR execution report
    ibkr_data = exec_result.get("ibkr") or {}
    ibkr_fills = ibkr_data.get("fills") or []

    if ibkr_fills:
        buy_fill: Optional[float] = None
        sell_fill: Optional[float] = None
        for fill in ibkr_fills:
            side = str(fill.get("side", "")).upper()
            price_raw = fill.get("price")
            if price_raw is None:
                continue
            try:
                price = float(price_raw)
            except (TypeError, ValueError):
                continue
            if side == "BUY":
                buy_fill = price
            elif side == "SELL":
                sell_fill = price
        if buy_fill is not None and sell_fill is not None:
            return round((buy_fill - sell_fill) * 100.0, 4)

    # Priority 2: market quotes (proxy: pay ask for BUY, receive bid for SELL)
    leg_quotes = exec_result.get("leg_quotes") or []
    buy_price: Optional[float] = None
    sell_price: Optional[float] = None

    for leg in leg_quotes:
        action = str(leg.get("action", "")).upper()
        quotes = leg.get("quotes") or {}

        if action == "BUY":
            # Use ask (what we paid) → if missing use mid
            v = quotes.get("ask") or quotes.get("mid")
            if v is not None:
                try:
                    buy_price = float(v)
                except (TypeError, ValueError):
                    pass

        elif action == "SELL":
            # Use bid (what we received) → if missing use mid
            v = quotes.get("bid") or quotes.get("mid")
            if v is not None:
                try:
                    sell_price = float(v)
                except (TypeError, ValueError):
                    pass

    if buy_price is not None and sell_price is not None:
        return round((buy_price - sell_price) * 100.0, 4)

    # Priority 3: market_debit (per-share) × 100
    market_debit = exec_result.get("market_debit")
    if market_debit is not None:
        try:
            return round(float(market_debit) * 100.0, 4)
        except (TypeError, ValueError):
            pass

    return None


def _extract_strikes_from_intent(intent: Dict[str, Any]) -> List[float]:
    """
    Extract [long_put, short_put] from intent legs.
    long_put = BUY strike (higher); short_put = SELL strike (lower).
    """
    legs = intent.get("legs") or []
    buy_strikes = [float(l["strike"]) for l in legs if l.get("action") == "BUY" and "strike" in l]
    sell_strikes = [float(l["strike"]) for l in legs if l.get("action") == "SELL" and "strike" in l]

    if buy_strikes and sell_strikes:
        return [max(buy_strikes), min(sell_strikes)]

    all_strikes = sorted([float(l["strike"]) for l in legs if "strike" in l], reverse=True)
    if len(all_strikes) >= 2:
        return [all_strikes[0], all_strikes[-1]]

    raise ValueError(f"Cannot extract strikes from intent legs: {legs}")


def _position_id_from_intent(intent_id: Optional[str], exec_result: Dict[str, Any]) -> str:
    """
    Derive a stable position_id.
    Prefer intent_id; fall back to hash of (symbol, expiry, strikes, timestamp).
    """
    if intent_id:
        return intent_id

    # Fallback: hash key fields
    symbol = exec_result.get("symbol", "")
    expiry = exec_result.get("expiry", "")
    ts = exec_result.get("timestamp_utc", "")
    raw = f"{symbol}|{expiry}|{ts}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Core pure function: build fills ledger row
# ---------------------------------------------------------------------------

def build_fill_row(
    exec_result: Dict[str, Any],
    intent: Dict[str, Any],
    date_str: str,
    mode: str,
    policy_id: str = "ccc_v1",
    source: str = "execution_result",
) -> Dict[str, Any]:
    """
    Build a canonical fills ledger row from an execution_result + intent dict.

    Pure function — no file I/O, no IBKR calls.

    Args:
        exec_result:  Parsed execution_result.json dict
        intent:       Parsed OPEN_*.json intent dict
        date_str:     "YYYY-MM-DD" (caller provides; allows inject in tests)
        mode:         "paper" | "live"
        policy_id:    Policy identifier string
        source:       "execution_result" | "ibkr_fills"

    Returns:
        Fills ledger row dict matching spec §F schema.

    Raises:
        ValueError: if required fields cannot be derived
    """
    # --- From intent ---
    intent_id: Optional[str] = intent.get("intent_id") or None
    candidate_id: str = str(intent.get("candidate_id") or "")
    regime: str = str(intent.get("regime") or "").lower()
    underlier: str = str(intent.get("symbol") or exec_result.get("symbol") or "")
    expiry: str = str(intent.get("expiry") or exec_result.get("expiry") or "")
    qty: int = int(intent.get("qty") or exec_result.get("qty") or 1)

    try:
        strikes = _extract_strikes_from_intent(intent)
    except ValueError:
        # Fallback: scrape from exec_result leg_quotes
        leg_quotes = exec_result.get("leg_quotes") or []
        buy_s = [float(l["strike"]) for l in leg_quotes if l.get("action") == "BUY"]
        sell_s = [float(l["strike"]) for l in leg_quotes if l.get("action") == "SELL"]
        if buy_s and sell_s:
            strikes = [max(buy_s), min(sell_s)]
        else:
            raises = ValueError("Cannot derive strikes from intent or exec_result")
            raise raises

    # --- Entry debit ---
    entry_debit_gross = _compute_entry_debit_gross(exec_result)

    # --- IBKR metadata ---
    order_id = exec_result.get("order_id")
    timestamp_utc = exec_result.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()

    # intent_path from exec_result or None
    intent_path_raw = exec_result.get("intent_path") or intent.get("_source_path") or None
    intent_path = str(intent_path_raw) if intent_path_raw else None

    row: Dict[str, Any] = {
        "date": date_str,
        "timestamp_utc": timestamp_utc,
        "action": "POSITION_OPENED",
        "policy_id": policy_id,
        "mode": mode,
        "intent_id": intent_id,
        "intent_path": intent_path,
        "candidate_id": candidate_id,
        "regime": regime,
        "underlier": underlier,
        "expiry": expiry,
        "strikes": strikes,
        "qty": qty,
        "entry_debit_gross": entry_debit_gross,
        "entry_debit_net": None,       # populated if commissions known
        "commissions": None,           # populated from IBKR execution report
        "ibkr": {
            "orderId": order_id,
            "permId": None,
            "conIds": [],
            "fills": [],
        },
        "source": source,
    }

    return row


# ---------------------------------------------------------------------------
# ORDER_STAGED row builder (CCC v1.8 §E)
# ---------------------------------------------------------------------------

def build_staged_row(
    exec_result: Dict[str, Any],
    intent: Dict[str, Any],
    date_str: str,
    mode: str,
    policy_id: str = "ccc_v1",
) -> Dict[str, Any]:
    """
    Build an ORDER_STAGED fills ledger row.

    ORDER_STAGED rows represent orders that were placed with transmit=false
    (paper staging) or where the status is STAGED_PAPER — i.e., the order
    exists on the system but has NOT been confirmed filled yet.

    Key differences from POSITION_OPENED:
    - action = "ORDER_STAGED"
    - Does NOT trigger positions.json entry creation
    - Does NOT count as "filled" in pending.load_filled_intent_ids()
    - DOES count as "pending" (committed but not filled)
    - DOES block duplicate OPEN intents via inventory gating

    Args:
        exec_result:  Parsed execution_result.json dict
        intent:       Parsed OPEN_*.json intent dict
        date_str:     "YYYY-MM-DD"
        mode:         "paper" | "live"
        policy_id:    Policy identifier string

    Returns:
        ORDER_STAGED ledger row dict.
    """
    intent_id: Optional[str] = intent.get("intent_id") or None
    candidate_id: str = str(intent.get("candidate_id") or "")
    regime: str = str(intent.get("regime") or "").lower()
    underlier: str = str(intent.get("symbol") or exec_result.get("symbol") or "")
    expiry: str = str(intent.get("expiry") or exec_result.get("expiry") or "")
    qty: int = int(intent.get("qty") or exec_result.get("qty") or 1)

    try:
        strikes = _extract_strikes_from_intent(intent)
    except ValueError:
        leg_quotes = exec_result.get("leg_quotes") or []
        buy_s = [float(l["strike"]) for l in leg_quotes if l.get("action") == "BUY"]
        sell_s = [float(l["strike"]) for l in leg_quotes if l.get("action") == "SELL"]
        if buy_s and sell_s:
            strikes = [max(buy_s), min(sell_s)]
        else:
            strikes = []

    order_id = exec_result.get("order_id")
    timestamp_utc = exec_result.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()
    intent_path_raw = exec_result.get("intent_path") or intent.get("_source_path") or None
    intent_path = str(intent_path_raw) if intent_path_raw else None

    return {
        "date": date_str,
        "timestamp_utc": timestamp_utc,
        "action": "ORDER_STAGED",
        "policy_id": policy_id,
        "mode": mode,
        "intent_id": intent_id,
        "intent_path": intent_path,
        "candidate_id": candidate_id,
        "regime": regime,
        "underlier": underlier,
        "expiry": expiry,
        "strikes": strikes,
        "qty": qty,
        "ibkr": {
            "orderId": order_id,
            "permId": None,
            "conIds": [],
            "fills": [],
        },
        "source": "execution_result",
        "staged_note": "STAGED_PAPER – not yet confirmed filled; pending until POSITION_OPENED",
    }


def _is_staged_already_recorded(
    intent_id: Optional[str],
    existing_rows: List[Dict[str, Any]],
) -> bool:
    """Return True if an ORDER_STAGED row for this intent_id already exists."""
    if not intent_id:
        return False
    return any(
        r.get("action") == "ORDER_STAGED" and r.get("intent_id") == intent_id
        for r in existing_rows
    )


# ---------------------------------------------------------------------------
# Fills ledger I/O (append-only, dedup by intent_id)
# ---------------------------------------------------------------------------

def read_fills_ledger(fills_ledger_path: Path) -> List[Dict[str, Any]]:
    """
    Read all rows from the fills ledger file.
    Returns empty list if file doesn't exist.
    """
    if not fills_ledger_path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    with open(fills_ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning(f"Skipping malformed fills ledger line: {line[:80]}")

    return rows


def _is_already_recorded(
    intent_id: Optional[str],
    existing_rows: List[Dict[str, Any]],
) -> bool:
    """
    Return True if a POSITION_OPENED row for this intent_id already exists.
    If intent_id is None, dedup is skipped (always append).
    """
    if not intent_id:
        return False
    return any(
        r.get("action") == "POSITION_OPENED" and r.get("intent_id") == intent_id
        for r in existing_rows
    )


def append_fills_ledger(
    fills_ledger_path: Path,
    row: Dict[str, Any],
    existing_rows: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Append a fills row to the fills ledger with dedup by intent_id.

    Args:
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        row:                Fill row dict (from build_fill_row)
        existing_rows:      Pre-loaded ledger (avoids double-read; None → read from disk)
        dry_run:            If True, validate but don't write

    Returns:
        (appended: bool, reason: str)
        reason is "APPENDED", "DEDUP_SKIPPED", or "DRY_RUN"
    """
    intent_id = row.get("intent_id")

    # Load existing rows for dedup check if not provided
    if existing_rows is None:
        existing_rows = read_fills_ledger(fills_ledger_path)

    if _is_already_recorded(intent_id, existing_rows):
        log.info(f"fills: DEDUP_SKIPPED intent_id={intent_id}")
        return False, "DEDUP_SKIPPED"

    if dry_run:
        log.info(f"fills: DRY_RUN would append intent_id={intent_id}")
        return False, "DRY_RUN"

    fills_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(fills_ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")

    log.info(f"fills: APPENDED intent_id={intent_id} → {fills_ledger_path}")
    return True, "APPENDED"


# ---------------------------------------------------------------------------
# positions.json snapshot
# ---------------------------------------------------------------------------

def build_positions_snapshot(fills_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build positions.json snapshot from fills ledger rows.

    Assumption: all POSITION_OPENED rows represent currently-open positions
    (no closing entries yet). Dedup by position_id (= intent_id if present).

    Returns:
        List of position dicts (§G schema), deduped and sorted by opened_utc.
    """
    seen_ids: set = set()
    positions: List[Dict[str, Any]] = []

    for row in fills_rows:
        if row.get("action") != "POSITION_OPENED":
            continue

        # Spec: position_id = intent_id (authoritative).
        # If intent_id is absent use a stable contract-based string (NOT a hash of timestamps).
        pos_id: str = row.get("intent_id") or ""  # type: ignore[assignment]
        if not pos_id:
            underlier = row.get("underlier", "")
            expiry = row.get("expiry", "")
            strikes_list = row.get("strikes") or []
            strikes_str = "_".join(f"{s:.0f}" for s in strikes_list)
            pos_id = f"{underlier}_{expiry}_{strikes_str}"
            log.warning(f"fills: position_id fallback to contract key (no intent_id): {pos_id}")

        if pos_id in seen_ids:
            continue
        seen_ids.add(pos_id)

        pos: Dict[str, Any] = {
            "position_id": pos_id,
            "policy_id": row.get("policy_id", "ccc_v1"),
            "mode": row.get("mode", "paper"),
            "regime": row.get("regime", ""),
            "underlier": row.get("underlier", ""),
            "expiry": row.get("expiry", ""),
            "strikes": row.get("strikes", []),
            "qty_open": int(row.get("qty", 1)),
            "entry_debit_gross": row.get("entry_debit_gross"),
            "entry_debit_net": row.get("entry_debit_net"),
            "opened_utc": row.get("timestamp_utc", ""),
            "source": row.get("source", "execution_result"),
        }
        positions.append(pos)

    # Sort by opened_utc ascending
    positions.sort(key=lambda p: p.get("opened_utc") or "")
    return positions


def write_positions_snapshot(
    positions_path: Path,
    positions: List[Dict[str, Any]],
    dry_run: bool = False,
) -> None:
    """Write positions.json snapshot (overwrites each reconcile)."""
    if dry_run:
        log.info(f"fills: DRY_RUN would write {len(positions)} positions to {positions_path}")
        return

    positions_path.parent.mkdir(parents=True, exist_ok=True)
    with open(positions_path, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2, default=str)

    log.info(f"fills: wrote {len(positions)} positions → {positions_path}")


def read_positions_snapshot(positions_path: Path) -> List[Dict[str, Any]]:
    """
    Read positions.json snapshot.
    Returns empty list if file doesn't exist or on parse error.
    """
    if not positions_path.exists():
        return []

    try:
        with open(positions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"fills: could not read positions.json ({e}) — returning []")

    return []


# ---------------------------------------------------------------------------
# Intent archival
# ---------------------------------------------------------------------------

def archive_intent_files(
    intent_path: Path,
    exec_result_path: Optional[Path],
    archive_base: Path,
    date_str: str,
    dry_run: bool = False,
) -> List[str]:
    """
    Move intent and execution_result files into the archive directory.

    Destination:
      archive_base / YYYYMMDD / <filename>

    Returns:
        List of archived file paths (strings).
    """
    archived: List[str] = []
    archive_dir = archive_base / date_str.replace("-", "")

    files_to_archive: List[Tuple[Path, str]] = []

    if intent_path and intent_path.exists():
        files_to_archive.append((intent_path, intent_path.name))

    if exec_result_path and exec_result_path.exists():
        # Add timestamp suffix to avoid collisions on same-day multi-runs
        ts_suffix = datetime.now(timezone.utc).strftime("%H%M%S")
        dest_name = f"{exec_result_path.stem}_{ts_suffix}{exec_result_path.suffix}"
        files_to_archive.append((exec_result_path, dest_name))

    if not files_to_archive:
        return archived

    if dry_run:
        for src, dest_name in files_to_archive:
            log.info(f"fills: DRY_RUN would archive {src} → {archive_dir / dest_name}")
            archived.append(str(archive_dir / dest_name))
        return archived

    archive_dir.mkdir(parents=True, exist_ok=True)
    for src, dest_name in files_to_archive:
        dest = archive_dir / dest_name
        try:
            shutil.move(str(src), str(dest))
            log.info(f"fills: archived {src.name} → {dest}")
            archived.append(str(dest))
        except OSError as e:
            log.warning(f"fills: could not archive {src}: {e}")

    return archived


# ---------------------------------------------------------------------------
# High-level: ingest from execution_result.json
# ---------------------------------------------------------------------------

def ingest_from_execution_result(
    exec_result: Dict[str, Any],
    intent: Dict[str, Any],
    fills_ledger_path: Path,
    positions_path: Path,
    mode: str,
    policy_id: str = "ccc_v1",
    date_str: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Core reconcile step: ingest a single execution_result + intent pair.

    CCC v1.8 §E: Staged-order branching:
    - If exec_result indicates STAGED only (transmit=false, status=STAGED_PAPER, etc.):
        → Write ORDER_STAGED row to fills ledger
        → Do NOT rebuild positions.json
        → Do NOT archive intent files
    - If exec_result indicates a genuine fill:
        → Write POSITION_OPENED row
        → Rebuild positions.json
        → Archive intent + exec_result

    Args:
        exec_result:        Parsed execution_result.json dict
        intent:             Parsed OPEN_*.json intent dict
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        positions_path:     Path to positions.json snapshot
        mode:               "paper" | "live"
        policy_id:          Policy identifier
        date_str:           Override date (YYYY-MM-DD); defaults to today Eastern
        dry_run:            If True, validate but don't write files

    Returns:
        {
          "fills_found": int,
          "positions_opened": int,
          "orders_staged": int,
          "dedup_skipped": int,
          "fills_written": bool,
          "positions_written": bool,
          "staged_only": bool,
          "fill_row": dict or None,
        }
    """
    if date_str is None:
        date_str = _get_eastern_date()

    existing_rows = read_fills_ledger(fills_ledger_path)
    staged_only = _is_staged_only(exec_result)

    # --- CCC v1.8 §E: staged-order branch ---
    if staged_only:
        staged_row = build_staged_row(
            exec_result=exec_result,
            intent=intent,
            date_str=date_str,
            mode=mode,
            policy_id=policy_id,
        )
        intent_id = staged_row.get("intent_id")

        # Dedup: skip if ORDER_STAGED already recorded for this intent_id
        if _is_staged_already_recorded(intent_id, existing_rows):
            log.info(f"fills: ORDER_STAGED DEDUP for intent_id={intent_id}")
            return {
                "fills_found": 1,
                "positions_opened": 0,
                "orders_staged": 0,
                "dedup_skipped": 1,
                "fills_written": False,
                "positions_written": False,
                "staged_only": True,
                "fill_row": staged_row,
            }

        if not dry_run:
            fills_ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(fills_ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(staged_row, separators=(",", ":"), default=str) + "\n")
            log.info(f"fills: ORDER_STAGED appended intent_id={intent_id} → {fills_ledger_path}")

        log.info(
            f"fills: STAGED ORDER (not yet filled) — "
            f"intent_id={intent_id}, regime={staged_row.get('regime')}. "
            "No position created. Pending gating will block duplicate OPEN."
        )
        return {
            "fills_found": 1,
            "positions_opened": 0,
            "orders_staged": 1,
            "dedup_skipped": 0,
            "fills_written": not dry_run,
            "positions_written": False,
            "staged_only": True,
            "fill_row": staged_row,
        }

    # --- Normal fill branch: build POSITION_OPENED row ---
    try:
        fill_row = build_fill_row(
            exec_result=exec_result,
            intent=intent,
            date_str=date_str,
            mode=mode,
            policy_id=policy_id,
        )
    except ValueError as e:
        log.error(f"fills: could not build fill row: {e}")
        return {
            "fills_found": 1,
            "positions_opened": 0,
            "orders_staged": 0,
            "dedup_skipped": 0,
            "fills_written": False,
            "positions_written": False,
            "staged_only": False,
            "fill_row": None,
            "error": str(e),
        }

    # Append to fills ledger
    appended, reason = append_fills_ledger(
        fills_ledger_path=fills_ledger_path,
        row=fill_row,
        existing_rows=existing_rows,
        dry_run=dry_run,
    )

    dedup_skipped = 1 if reason == "DEDUP_SKIPPED" else 0
    positions_opened = 1 if appended else 0
    positions_written = False

    # Rebuild positions.json if we appended
    if appended or dry_run:
        all_rows = existing_rows + ([fill_row] if not dry_run else [])
        if dry_run:
            all_rows = existing_rows + [fill_row]
        positions = build_positions_snapshot(all_rows)
        write_positions_snapshot(positions_path, positions, dry_run=dry_run)
        positions_written = not dry_run

    return {
        "fills_found": 1,
        "positions_opened": positions_opened,
        "orders_staged": 0,
        "dedup_skipped": dedup_skipped,
        "fills_written": appended,
        "positions_written": positions_written,
        "staged_only": False,
        "fill_row": fill_row,
    }


# ---------------------------------------------------------------------------
# Full reconcile orchestration
# ---------------------------------------------------------------------------

def run_reconcile(
    mode: str,
    execution_result_path: Optional[Path] = None,
    fills_ledger_path: Optional[Path] = None,
    positions_path: Optional[Path] = None,
    intents_dir: Optional[Path] = None,
    archive_base_dir: Optional[Path] = None,
    policy_id: str = "ccc_v1",
    dry_run: bool = False,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full reconcile orchestration. Called by scripts/ccc_reconcile.py.

    Fill source priority (spec §C):
      1. execution_result_path if provided and exists
      2. intents_dir / "execution_result.json" if present
      3. No fills found → no-op, exit 0

    Steps:
      A. Locate execution_result.json
      B. Load corresponding OPEN_*.json intent
      C. Ingest fill (build row, dedup, append, rebuild positions)
      D. Archive OPEN_*.json + execution_result.json
      E. Return summary dict

    Args:
        mode:                   "paper" | "live"
        execution_result_path:  Override path to execution_result.json
        fills_ledger_path:      defaults to DEFAULT_FILLS_LEDGER_PATH
        positions_path:         defaults to DEFAULT_POSITIONS_PATH
        intents_dir:            defaults to DEFAULT_INTENTS_DIR
        archive_base_dir:       defaults to DEFAULT_ARCHIVE_BASE
        policy_id:              Policy ID string
        dry_run:                Validate only, do not write files
        date_str:               Override today's date (YYYY-MM-DD)

    Returns:
        {
          "fills_found": int,
          "positions_opened": int,
          "dedup_skipped": int,
          "files_written": list[str],
          "archived": list[str],
          "errors": list[str],
          "mode": str,
          "dry_run": bool,
        }
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")

    fills_ledger_path = fills_ledger_path or DEFAULT_FILLS_LEDGER_PATH
    positions_path = positions_path or DEFAULT_POSITIONS_PATH
    intents_dir = intents_dir or DEFAULT_INTENTS_DIR
    archive_base_dir = archive_base_dir or DEFAULT_ARCHIVE_BASE

    if date_str is None:
        date_str = _get_eastern_date()

    summary: Dict[str, Any] = {
        "fills_found": 0,
        "positions_opened": 0,
        "dedup_skipped": 0,
        "files_written": [],
        "archived": [],
        "errors": [],
        "mode": mode,
        "dry_run": dry_run,
    }

    # --- A. Locate execution_result.json ---
    exec_result_path: Optional[Path] = None

    if execution_result_path and Path(execution_result_path).exists():
        exec_result_path = Path(execution_result_path)
    else:
        candidate = intents_dir / "execution_result.json"
        if candidate.exists():
            exec_result_path = candidate

    if exec_result_path is None:
        log.info("fills: no execution_result.json found — no-op")
        return summary

    # --- Load execution_result ---
    try:
        with open(exec_result_path, "r", encoding="utf-8") as f:
            exec_result = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        summary["errors"].append(f"Could not load {exec_result_path}: {e}")
        return summary

    # --- B. Load corresponding intent ---
    # intent_path is embedded in exec_result
    intent_path_raw = exec_result.get("intent_path")
    intent: Optional[Dict[str, Any]] = None
    intent_path: Optional[Path] = None

    if intent_path_raw:
        # Normalise: exec_result may use backslashes (Windows paths)
        intent_path = Path(intent_path_raw.replace("\\", "/"))
        if not intent_path.is_absolute():
            # Try relative to CWD
            if intent_path.exists():
                pass
            else:
                # Try relative to repo root from exec_result_path location
                alt = exec_result_path.parent / intent_path.name
                if alt.exists():
                    intent_path = alt

        if intent_path and intent_path.exists():
            try:
                with open(intent_path, "r", encoding="utf-8") as f:
                    intent = json.load(f)
                intent["_source_path"] = str(intent_path)
            except (json.JSONDecodeError, OSError) as e:
                summary["errors"].append(f"Could not load intent {intent_path}: {e}")

    if intent is None:
        # Fallback: try to find the OPEN_*.json file that matches exec_result symbol/expiry
        log.warning("fills: intent file not found via exec_result.intent_path — scanning intents_dir")
        symbol = exec_result.get("symbol", "")
        expiry = exec_result.get("expiry", "")
        for candidate_intent in (intents_dir.glob("OPEN_*.json") if intents_dir.exists() else []):
            try:
                with open(candidate_intent, "r", encoding="utf-8") as f:
                    cand = json.load(f)
                if cand.get("symbol") == symbol and cand.get("expiry") == expiry:
                    intent = cand
                    intent["_source_path"] = str(candidate_intent)
                    intent_path = candidate_intent
                    log.info(f"fills: matched intent by symbol/expiry: {candidate_intent}")
                    break
            except (json.JSONDecodeError, OSError):
                continue

    if intent is None:
        # Last-resort: build minimal intent from exec_result fields
        log.warning("fills: building minimal intent from exec_result fields (no OPEN_*.json found)")
        intent = {
            "intent_id": None,
            "regime": exec_result.get("regime", ""),
            "symbol": exec_result.get("symbol", ""),
            "expiry": exec_result.get("expiry", ""),
            "qty": exec_result.get("qty", 1),
            "candidate_id": "",
            "legs": [
                {"action": lq["action"], "strike": lq["strike"], "right": lq.get("right", "P")}
                for lq in (exec_result.get("leg_quotes") or [])
            ],
        }

    # --- C. Ingest fill ---
    ingestion = ingest_from_execution_result(
        exec_result=exec_result,
        intent=intent,
        fills_ledger_path=fills_ledger_path,
        positions_path=positions_path,
        mode=mode,
        policy_id=policy_id,
        date_str=date_str,
        dry_run=dry_run,
    )

    summary["fills_found"] = ingestion["fills_found"]
    summary["positions_opened"] = ingestion["positions_opened"]
    summary["dedup_skipped"] = ingestion["dedup_skipped"]

    if ingestion.get("error"):
        summary["errors"].append(ingestion["error"])

    if ingestion["fills_written"]:
        summary["files_written"].append(str(fills_ledger_path))
    if ingestion["positions_written"]:
        summary["files_written"].append(str(positions_path))

    # --- D. Archive intent + execution_result ---
    # Safety rule: ONLY archive after both fills ledger AND positions.json written successfully.
    # Sequence: read exec_result → compute debit → append fills → rebuild positions → verify → THEN archive.
    # If positions write failed, do NOT archive — operator can re-run; the intent is still present.
    if ingestion["fills_written"] and ingestion["positions_written"] and intent_path is not None:
        archived = archive_intent_files(
            intent_path=intent_path,
            exec_result_path=exec_result_path,
            archive_base=archive_base_dir,
            date_str=date_str,
            dry_run=dry_run,
        )
        summary["archived"].extend(archived)
    elif ingestion["dedup_skipped"] > 0:
        log.info("fills: dedup — intent not re-archived")

    return summary
