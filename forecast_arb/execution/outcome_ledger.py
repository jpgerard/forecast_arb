"""
Trade Outcome Ledger - Append-Only Trade-Level Logging

Provides append-only logging for trade outcomes.
When a trade is transmitted/filled, append an outcome stub.
When closed, append a close event (no in-place mutation).

This enables post-hoc analysis of trade performance and decision quality.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional


def append_trade_open(
    run_dir: Path,
    candidate_id: str,
    run_id: str,
    regime: str,
    entry_ts_utc: str,
    entry_price: float,
    qty: int,
    expiry: str,
    long_strike: float,
    short_strike: float,
    intent_id: str,
    order_id: Optional[str] = None,
    also_global: bool = True
) -> None:
    """
    Append a trade OPEN event to the outcome ledger.
    
    FIX A: Enforces single OPEN per intent/order.
    Only write OPEN when order is Filled or at least Submitted.
    
    FIX B: Mandatory fields: intent_id, order_id
    
    Writes to both per-run and global ledgers:
    - Per-run: <run_dir>/artifacts/trade_outcomes.jsonl
    - Global: runs/trade_outcomes.jsonl
    
    Args:
        run_dir: Run directory path
        candidate_id: Unique candidate identifier
        run_id: Run identifier
        regime: Regime name ("crash" or "selloff")
        entry_ts_utc: Entry timestamp (ISO format)
        entry_price: Entry price per contract (USD)
        qty: Quantity (number of spreads)
        expiry: Expiry date (YYYYMMDD) - MUST be from resolved IBKR contract
        long_strike: Long put strike
        short_strike: Short put strike
        intent_id: OrderIntent ID (mandatory)
        order_id: IBKR order ID (optional, None if not yet assigned)
        also_global: Whether to also write to global ledger
    """
    # FIX A: Check for existing OPEN entry with this intent_id
    # Check both run_dir ledger and global ledger
    run_ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
    global_ledger_path = Path("runs") / "trade_outcomes.jsonl"
    
    for ledger_path in [run_ledger_path, global_ledger_path]:
        if ledger_path.exists():
            with open(ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    existing = json.loads(line)
                    if (existing.get("intent_id") == intent_id and 
                        existing.get("status") == "OPEN"):
                        raise ValueError(
                            f"LEDGER VIOLATION: OPEN entry already exists for intent_id={intent_id}. "
                            f"Cannot write duplicate OPEN. Use INTENT_STAGED or ORDER_SUBMITTED instead."
                        )
    
    # FIX B: Mandatory fields
    entry = {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "regime": regime,
        "entry_ts_utc": entry_ts_utc,
        "entry_price": entry_price,
        "qty": qty,
        "expiry": expiry,
        "long_strike": long_strike,
        "short_strike": short_strike,
        "intent_id": intent_id,  # FIX B: Mandatory
        "order_id": order_id,     # FIX B: Mandatory (can be None)
        "exit_ts_utc": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl": None,
        "mfe": None,
        "mae": None,
        "status": "OPEN"
    }
    
    # Write to per-run ledger
    run_ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
    _append_jsonl(run_ledger_path, entry)
    
    # Write to global ledger if requested
    if also_global:
        _append_jsonl(global_ledger_path, entry)


def append_trade_close(
    run_dir: Path,
    candidate_id: str,
    exit_ts_utc: str,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    mfe: Optional[float] = None,
    mae: Optional[float] = None,
    also_global: bool = True
) -> None:
    """
    Append a trade CLOSE event to the outcome ledger.
    
    This does NOT modify the OPEN entry - it appends a new line
    with status="CLOSED" that can be matched by candidate_id.
    
    Args:
        run_dir: Run directory path
        candidate_id: Unique candidate identifier (matches OPEN entry)
        exit_ts_utc: Exit timestamp (ISO format)
        exit_price: Exit price per contract (USD)
        exit_reason: Reason for exit (TAKE_PROFIT, TIME_STOP, EXPIRED, MANUAL)
        pnl: Profit/loss per contract (USD)
        mfe: Maximum favorable excursion (optional)
        mae: Maximum adverse excursion (optional)
        also_global: Whether to also write to global ledger
    """
    entry = {
        "candidate_id": candidate_id,
        "exit_ts_utc": exit_ts_utc,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl": pnl,
        "mfe": mfe,
        "mae": mae,
        "status": "CLOSED"
    }
    
    # Write to per-run ledger
    run_ledger_path = run_dir / "artifacts" / "trade_outcomes.jsonl"
    _append_jsonl(run_ledger_path, entry)
    
    # Write to global ledger if requested
    if also_global:
        global_ledger_path = Path("runs") / "trade_outcomes.jsonl"
        _append_jsonl(global_ledger_path, entry)


def append_trade_event(
    event: str,
    intent_id: str,
    candidate_id: str,
    run_id: str,
    regime: str,
    timestamp_utc: str,
    order_id: Optional[str] = None,
    expiry: Optional[str] = None,
    long_strike: Optional[float] = None,
    short_strike: Optional[float] = None,
    qty: Optional[int] = None,
    entry_price: Optional[float] = None,
    also_global: bool = True
) -> None:
    """
    Append a trade event to the outcome ledger.
    
    Event types:
    - QUOTE_OK: Quote-only check passed all guards
    - QUOTE_BLOCKED: Quote-only check failed guards
    - STAGED_PAPER: Paper order staged (not transmitted)
    - SUBMITTED_LIVE: Live order submitted to exchange
    - FILLED_OPEN: Order filled, position now open
    
    Only FILLED_OPEN events represent actual open positions.
    
    Args:
        event: Event type (QUOTE_OK, QUOTE_BLOCKED, STAGED_PAPER, SUBMITTED_LIVE, FILLED_OPEN)
        intent_id: OrderIntent ID (mandatory)
        candidate_id: Unique candidate identifier
        run_id: Run identifier
        regime: Regime name ("crash" or "selloff")
        timestamp_utc: Event timestamp (ISO format)
        order_id: IBKR order ID (optional, None if not yet assigned)
        expiry: Expiry date (YYYYMMDD) - required for FILLED_OPEN
        long_strike: Long put strike - required for FILLED_OPEN
        short_strike: Short put strike - required for FILLED_OPEN
        qty: Quantity (number of spreads) - required for FILLED_OPEN
        entry_price: Entry price per contract - required for FILLED_OPEN
        also_global: Whether to also write to global ledger
    """
    # Validate event type
    valid_events = ["QUOTE_OK", "QUOTE_BLOCKED", "STAGED_PAPER", "SUBMITTED_LIVE", "FILLED_OPEN"]
    if event not in valid_events:
        raise ValueError(f"Invalid event type: {event}. Must be one of {valid_events}")
    
    # Validate mandatory fields for FILLED_OPEN
    if event == "FILLED_OPEN":
        if not all([expiry, long_strike is not None, short_strike is not None, qty, entry_price is not None]):
            raise ValueError(
                f"FILLED_OPEN event requires: expiry, long_strike, short_strike, qty, entry_price"
            )
    
    entry = {
        "event": event,
        "intent_id": intent_id,
        "candidate_id": candidate_id,
        "run_id": run_id,
        "regime": regime,
        "timestamp_utc": timestamp_utc,
        "order_id": order_id
    }
    
    # Add optional fields if provided
    if expiry:
        entry["expiry"] = expiry
    if long_strike is not None:
        entry["long_strike"] = long_strike
    if short_strike is not None:
        entry["short_strike"] = short_strike
    if qty:
        entry["qty"] = qty
    if entry_price is not None:
        entry["entry_price"] = entry_price
    
    # Write to global ledger
    global_ledger_path = Path("runs") / "trade_outcomes.jsonl"
    _append_jsonl(global_ledger_path, entry)
    
    # Write to per-run ledger if also_global
    # Note: We don't have run_dir here, so we only write to global for now
    # Individual runs can maintain their own ledgers separately


def _append_jsonl(path: Path, obj: dict) -> None:
    """
    Internal helper to append a JSON object to a JSONL file.
    
    Args:
        path: Path to JSONL file
        obj: Dictionary to append as JSON line
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write as compact JSON
    line = json.dumps(obj, separators=(",", ":"), sort_keys=False)
    
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_trade_events(ledger_path: Path) -> List[Dict[str, Any]]:
    """
    Return all event-type entries from the ledger, in file order.

    Event-type entries are written by append_trade_event() and carry an 'event'
    field (QUOTE_OK, QUOTE_BLOCKED, STAGED_PAPER, SUBMITTED_LIVE, FILLED_OPEN).

    This is distinct from read_trade_outcomes() which handles OPEN/CLOSED entries.

    Args:
        ledger_path: Path to trade_outcomes.jsonl

    Returns:
        List of event dicts, preserving file order
    """
    if not ledger_path.exists():
        return []

    events: List[Dict[str, Any]] = []
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if "event" in entry:
                events.append(entry)

    return events


def read_trade_outcomes(ledger_path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Read trade outcomes from a ledger and reconstruct full trade records.
    
    This merges OPEN and CLOSED events by candidate_id to create
    complete trade records.
    
    Args:
        ledger_path: Path to trade_outcomes.jsonl
        
    Returns:
        Dictionary mapping candidate_id to trade record
    """
    if not ledger_path.exists():
        return {}
    
    trades = {}
    
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            
            entry = json.loads(line)
            # Skip event-type entries (written by append_trade_event; no 'status' field)
            status = entry.get("status")
            if status is None:
                continue
            candidate_id = entry["candidate_id"]
            
            if status == "OPEN":
                # Initialize trade record
                trades[candidate_id] = entry.copy()
            elif status == "CLOSED":
                # Update existing trade with close data
                if candidate_id in trades:
                    trades[candidate_id].update({
                        "exit_ts_utc": entry["exit_ts_utc"],
                        "exit_price": entry["exit_price"],
                        "exit_reason": entry["exit_reason"],
                        "pnl": entry["pnl"],
                        "mfe": entry.get("mfe"),
                        "mae": entry.get("mae"),
                        "status": "CLOSED"
                    })
                else:
                    # CLOSED without OPEN - store as-is
                    trades[candidate_id] = entry.copy()
    
    return trades
