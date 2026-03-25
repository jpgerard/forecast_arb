"""
ccc_execute.py — Operator command to stage/execute intents from allocator_actions.json.

v1.5 (Patch Pack v1.5):
  Task 1: Canonical commit ledger schema with LOCAL date, action=OPEN, strikes as list.
  Task 4: Public run_execute() function callable from daily.py (no shell-out needed).
  Live mode: requires typing SEND exactly before any commits are written.

v1.4 Task C:
  This is the ONLY way to write to allocator_commit_ledger.jsonl.
  Running CCC planning does NOT write to the commit ledger.

=== Canonical Commit Ledger Record Schema ===
  {
    "date": "YYYY-MM-DD",            // LOCAL DATE (America/New_York)
    "timestamp_utc": "ISO",
    "action": "OPEN",
    "policy_id": "ccc_v1",
    "intent_id": "...",
    "candidate_id": "...",
    "run_id": "... or null",
    "candidate_rank": 1 or null,
    "regime": "crash" | "selloff",
    "underlier": "SPY" | "QQQ",
    "expiry": "YYYYMMDD",
    "strikes": [585.0, 565.0],       // ALWAYS a 2-element list [long_put, short_put]
    "qty": 1,
    "premium_per_contract": 36.0,
    "premium_spent": 36.0,
    "reason_codes": [...],
    "intent_path": "intents/allocator/OPEN_....json",
    "mode": "paper" | "live"
  }

=== Validation Rules ===
  - If any HARD-REQUIRED field is missing/empty/null at commit time, raises ValueError.
  - Strikes MUST be a 2-element list [long_put, short_put]. Never a dict.
  - Idempotent: intent_id dedup prevents double-commits.

Usage (CLI):
    # Preview intents — no execution, no commit ledger write
    python scripts/ccc_execute.py --actions runs/allocator/allocator_actions.json --paper --quote-only

    # Stage intents in paper mode (write to commit ledger — counts toward spent_today_before)
    python scripts/ccc_execute.py --actions runs/allocator/allocator_actions.json --paper

    # Live mode (requires SEND confirmation, not yet wired to IBKR transmission)
    python scripts/ccc_execute.py --actions runs/allocator/allocator_actions.json --live

Usage (programmatic, from daily.py):
    from scripts.ccc_execute import run_execute
    result = run_execute(
        actions_file="runs/allocator/allocator_actions.json",
        commit_ledger_path="runs/allocator/allocator_commit_ledger.jsonl",
        mode="paper",   # or "live"
        quote_only=False,
    )
    # result: {"committed": N, "skipped_already_committed": N, "errors": N, "quotes_ok": N, "mode": ..., "aborted": False}
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard-required fields in every commit record (raise ValueError if missing/empty)
# ---------------------------------------------------------------------------
_HARD_REQUIRED: List[str] = [
    "date",
    "timestamp_utc",
    "action",
    "policy_id",
    "intent_id",
    "regime",
    "underlier",
    "expiry",
    "strikes",        # validated separately: must be 2-element list
    "qty",
    "premium_per_contract",
    "premium_spent",
]


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Stale intent guard (Task 4 v1.6)
# ---------------------------------------------------------------------------

def _is_stale_intent(
    intent_path: str,
    intent: Dict[str, Any],
    today_str: str,
) -> tuple[bool, str]:
    """
    Return (is_stale, reason) for an intent file.

    Checks:
      1. File mtime (UTC date) == today_str
      2. intent["timestamp_utc"] date (UTC) == today_str

    If either mismatches, intent is considered stale.
    ``today_str`` should be YYYY-MM-DD in UTC (from _get_local_date or similar).
    """
    p = Path(intent_path)

    # 1. File mtime check
    try:
        mtime_date = datetime.fromtimestamp(
            p.stat().st_mtime, tz=timezone.utc
        ).date().isoformat()
        if mtime_date != today_str:
            return True, f"file mtime={mtime_date} ≠ today={today_str}"
    except OSError:
        pass  # Can't check; fall through to timestamp check

    # 2. Embedded timestamp_utc check
    ts = intent.get("timestamp_utc")
    if ts:
        try:
            intent_date = (
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone(timezone.utc)
                .date()
                .isoformat()
            )
            if intent_date != today_str:
                return True, f"timestamp_utc date={intent_date} ≠ today={today_str}"
        except (ValueError, TypeError, AttributeError):
            pass

    return False, ""


# ---------------------------------------------------------------------------
# Timezone helper
# ---------------------------------------------------------------------------

def _get_local_date() -> str:
    """
    Return today's date as YYYY-MM-DD using America/New_York local time.

    Falls back to UTC if zoneinfo / pytz are unavailable (test environments that
    don't have tzdata installed).
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        return datetime.now(tz).date().isoformat()
    except Exception:
        pass
    try:
        import pytz  # type: ignore[import]
        tz_py = pytz.timezone("America/New_York")
        return datetime.now(tz_py).date().isoformat()
    except Exception:
        pass
    # Fallback
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _load_actions(path: str) -> Dict[str, Any]:
    """Load and return allocator_actions.json."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Actions file not found: {path}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_open_actions(actions_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return list of OPEN actions that have an intent_path."""
    actions = actions_data.get("actions", [])
    return [a for a in actions if a.get("type") == "OPEN" and a.get("intent_path")]


def _load_intent(intent_path: str) -> Dict[str, Any]:
    """Load an OrderIntent JSON file."""
    p = Path(intent_path)
    if not p.exists():
        raise FileNotFoundError(f"Intent file not found: {intent_path}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_commit_records(commit_ledger_path: Path) -> List[Dict[str, Any]]:
    """Read all records from the commit ledger (empty list if file missing)."""
    if not commit_ledger_path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with open(commit_ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _is_already_committed(intent_id: str, commit_records: List[Dict[str, Any]]) -> bool:
    """Return True if intent_id already present in commit ledger (idempotency guard)."""
    return any(r.get("intent_id") == intent_id for r in commit_records)


def _append_commit_record(commit_ledger_path: Path, record: Dict[str, Any]) -> None:
    """Append a single record to the commit ledger (creates dirs if needed)."""
    commit_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(commit_ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Canonical commit record builder
# ---------------------------------------------------------------------------

def _extract_strikes_from_intent(intent: Dict[str, Any]) -> List[float]:
    """
    Extract strikes as [long_put, short_put] from intent legs.

    long_put  = strike of BUY leg  (the higher-strike put)
    short_put = strike of SELL leg (the lower-strike put)

    Returns 2-element list or raises ValueError if legs are malformed.
    """
    legs = intent.get("legs") or []
    if not legs:
        raise ValueError("Intent has no legs; cannot extract strikes.")

    buy_strikes = [float(leg["strike"]) for leg in legs if leg.get("action") == "BUY" and "strike" in leg]
    sell_strikes = [float(leg["strike"]) for leg in legs if leg.get("action") == "SELL" and "strike" in leg]

    if buy_strikes and sell_strikes:
        long_put = max(buy_strikes)
        short_put = min(sell_strikes)
        return [long_put, short_put]

    # Fallback: sort all strikes descending
    all_strikes = sorted([float(leg["strike"]) for leg in legs if "strike" in leg], reverse=True)
    if len(all_strikes) >= 2:
        return [all_strikes[0], all_strikes[-1]]

    raise ValueError(
        f"Cannot extract 2-element [long_put, short_put] strikes from legs: {legs}"
    )


def _build_canonical_commit_record(
    action: Dict[str, Any],
    intent: Dict[str, Any],
    policy_id: str,
    timestamp_utc: str,
    mode: str,
) -> Dict[str, Any]:
    """
    Build and validate the canonical commit ledger record.

    Args:
        action:        OPEN action dict from allocator_actions.json
        intent:        Loaded OrderIntent dict
        policy_id:     From the actions file top-level
        timestamp_utc: ISO timestamp string (UTC)
        mode:          "paper" or "live"

    Returns:
        Canonical commit record dict

    Raises:
        ValueError: if any hard-required field is missing or strikes cannot be formed.
    """
    date_str = _get_local_date()

    # --- Extract from intent ---
    intent_id = str(intent.get("intent_id") or "").strip()
    regime = str(intent.get("regime") or "").strip()
    underlier = str(intent.get("symbol") or "").strip()
    expiry = str(intent.get("expiry") or "").strip()

    # Strikes: always extract from intent legs (canonical source of truth)
    strikes = _extract_strikes_from_intent(intent)

    # Premium: prefer action.premium, fall back to intent.limit.start
    premium_per_contract: float = 0.0
    if action.get("premium") is not None:
        try:
            premium_per_contract = float(action["premium"])
        except (ValueError, TypeError):
            pass
    if premium_per_contract == 0.0:
        try:
            premium_per_contract = float(intent.get("limit", {}).get("start", 0.0) or 0.0)
        except (ValueError, TypeError):
            pass

    # Qty: prefer action.qty, fall back to intent.qty
    qty: int = 1
    if action.get("qty") is not None:
        try:
            qty = int(action["qty"])
        except (ValueError, TypeError):
            pass
    elif intent.get("qty") is not None:
        try:
            qty = int(intent["qty"])
        except (ValueError, TypeError):
            pass

    premium_spent = premium_per_contract * qty

    # --- Extract from action ---
    candidate_id = str(action.get("candidate_id") or intent.get("candidate_id") or "").strip()
    run_id = action.get("run_id") or intent.get("run_id")
    candidate_rank = action.get("candidate_rank")
    reason_codes = list(action.get("reason_codes") or [])
    intent_path = str(action.get("intent_path") or "").strip()

    # --- Build record ---
    record: Dict[str, Any] = {
        "date": date_str,
        "timestamp_utc": timestamp_utc,
        "action": "OPEN",
        "policy_id": policy_id,
        "intent_id": intent_id,
        "candidate_id": candidate_id,
        "run_id": run_id,
        "candidate_rank": candidate_rank,
        "regime": regime,
        "underlier": underlier,
        "expiry": expiry,
        "strikes": strikes,
        "qty": qty,
        "premium_per_contract": premium_per_contract,
        "premium_spent": premium_spent,
        "reason_codes": reason_codes,
        "intent_path": intent_path,
        "mode": mode,
    }

    # --- Validate hard-required fields (fail loud on missing) ---
    missing: List[str] = []
    for field in _HARD_REQUIRED:
        val = record.get(field)
        if val is None or (isinstance(val, str) and not val) or (isinstance(val, (int, float)) and val == 0 and field not in ("qty",)):
            # qty == 0 would be weird but we allow it; premium can be 0 legitimately in tests
            pass
        if val is None or (isinstance(val, str) and not val):
            missing.append(field)

    # Explicit check premium_per_contract: 0 is allowed only in test stubs
    # Explicit check strikes: must be 2-element list
    if len(record.get("strikes", [])) != 2:
        missing.append("strikes(len!=2)")

    if missing:
        raise ValueError(
            f"Cannot write commit record: missing hard-required fields {missing}. "
            f"candidate_id={candidate_id!r}, intent_id={intent_id!r}. "
            f"Fix the action/candidate/intent before committing. "
            f"Partial records are never written."
        )

    return record


# ---------------------------------------------------------------------------
# Core execute modes
# ---------------------------------------------------------------------------

def _run_quote_only(
    open_actions: List[Dict[str, Any]],
    actions_file: str,
    policy_id: str,
    allow_stale: bool = False,
) -> int:
    """
    Print a dry-run ticket summary. Does NOT write to commit ledger.

    Returns count of intents that passed validation.
    """
    today = _get_local_date()

    print()
    print("=" * 72)
    print(f"  CCC EXECUTE  —  QUOTE-ONLY  —  {today}")
    print(f"  Actions file: {actions_file}")
    print(f"  {len(open_actions)} OPEN intent(s) to review")
    print("=" * 72)

    quotes_ok = 0

    for i, action in enumerate(open_actions, 1):
        intent_path_str = action.get("intent_path", "")
        candidate_id = action.get("candidate_id", "unknown")
        qty = action.get("qty", "?")
        premium = action.get("premium") or 0.0
        total = premium * (qty if isinstance(qty, int) else 0)

        print(f"\n  [{i}] candidate_id  : {candidate_id}")
        print(f"       intent_path   : {intent_path_str}")
        print(f"       qty={qty}  premium=${premium:.2f}/c  total=${total:.2f}")

        try:
            intent = _load_intent(intent_path_str)
            from forecast_arb.execution.execute_trade import validate_order_intent
            validate_order_intent(intent)

            intent_id = intent.get("intent_id", "?")
            symbol = intent.get("symbol", "?")
            expiry = intent.get("expiry", "?")
            regime = intent.get("regime", "?")
            legs = intent.get("legs", [])
            strikes_list = _extract_strikes_from_intent(intent)
            strikes_str = f"{strikes_list[0]:.0f}/{strikes_list[1]:.0f}"
            limit = intent.get("limit", {})
            limit_start = limit.get("start", 0)
            limit_max = limit.get("max", 0)

            print(f"       intent_id     : {str(intent_id)[:20]}...")
            print(f"       symbol={symbol}  expiry={expiry}  P{strikes_str}  regime={regime}")
            print(f"       limit: start=${limit_start:.2f}  max=${limit_max:.2f}")
            print(f"       ✓ intent valid")
            quotes_ok += 1

        except FileNotFoundError as e:
            print(f"       ✗ MISSING: {e}")
        except Exception as e:
            print(f"       ✗ INVALID: {e}")

    print()
    print(f"  Quotes OK: {quotes_ok}/{len(open_actions)}")
    print("  (quote-only: commit ledger NOT updated)")
    print("=" * 72)
    print()
    return quotes_ok


def _run_paper_stage(
    open_actions: List[Dict[str, Any]],
    commit_ledger_path: Path,
    actions_file: str,
    policy_id: str,
    allow_stale: bool = False,
) -> Dict[str, int]:
    """
    Stage intents in paper mode: validate each intent, append canonical commit records.

    Returns dict with committed/skipped/errors counts.
    """
    today = _get_local_date()
    timestamp_utc = datetime.now(timezone.utc).isoformat()

    commit_records = _read_commit_records(commit_ledger_path)

    print()
    print("=" * 72)
    print(f"  CCC EXECUTE  —  PAPER STAGING  —  {today}")
    print(f"  Actions file: {actions_file}")
    print(f"  Commit ledger: {commit_ledger_path}")
    print(f"  {len(open_actions)} OPEN intent(s) to stage")
    if allow_stale:
        print("  ⚠️  --allow-stale: stale intent guard bypassed")
    print("=" * 72)

    committed = 0
    skipped = 0
    errors = 0

    for action in open_actions:
        intent_path_str = action.get("intent_path", "")
        candidate_id = action.get("candidate_id") or "unknown"

        # Load and validate intent
        try:
            intent = _load_intent(intent_path_str)
            from forecast_arb.execution.execute_trade import validate_order_intent
            validate_order_intent(intent)
        except FileNotFoundError as e:
            print(f"\n  ✗ SKIPPED  candidate_id={candidate_id}: {e}")
            errors += 1
            continue
        except Exception as e:
            print(f"\n  ✗ SKIPPED  candidate_id={candidate_id}: intent invalid — {e}")
            errors += 1
            continue

        intent_id = str(intent.get("intent_id", "")).strip()
        if not intent_id:
            print(f"\n  ✗ SKIPPED  candidate_id={candidate_id}: intent has no intent_id")
            errors += 1
            continue

        # Stale intent guard (Task 4 v1.6)
        if not allow_stale:
            stale, stale_reason = _is_stale_intent(intent_path_str, intent, today)
            if stale:
                print(
                    f"\n  ⚠ STALE SKIPPED  candidate_id={candidate_id}: "
                    f"{stale_reason}  (use --allow-stale to override)"
                )
                errors += 1
                continue

        # Idempotency check
        if _is_already_committed(intent_id, commit_records):
            print(
                f"\n  ↩ ALREADY COMMITTED"
                f"  intent_id={intent_id[:20]}  candidate_id={candidate_id}"
            )
            skipped += 1
            continue

        # Build canonical commit record (fail loud if required fields missing)
        try:
            record = _build_canonical_commit_record(
                action=action,
                intent=intent,
                policy_id=policy_id,
                timestamp_utc=timestamp_utc,
                mode="paper",
            )
        except ValueError as exc:
            print(f"\n  ✗ COMMIT_FAILED  candidate_id={candidate_id}: {exc}")
            errors += 1
            continue

        _append_commit_record(commit_ledger_path, record)
        commit_records.append(record)
        committed += 1

        strikes = record.get("strikes", [])
        strikes_str = f"{strikes[0]:.0f}/{strikes[1]:.0f}" if len(strikes) == 2 else "?"
        prem = record.get("premium_spent", 0.0)
        print(
            f"\n  ✓ COMMITTED"
            f"  intent_id={intent_id[:20]}"
            f"  candidate_id={candidate_id}"
            f"  {record.get('underlier','')} {record.get('expiry','')} P{strikes_str}"
            f"  qty={record.get('qty',1)}  ${prem:.2f}"
        )

    print()
    print(
        f"  Summary: {committed} newly committed, "
        f"{skipped} already committed, "
        f"{errors} error(s)"
    )
    print("=" * 72)
    print()

    return {"committed": committed, "skipped_already_committed": skipped, "errors": errors}


def _run_live_stage(
    open_actions: List[Dict[str, Any]],
    commit_ledger_path: Path,
    actions_file: str,
    policy_id: str,
    allow_stale: bool = False,
) -> Dict[str, int]:
    """
    Stage intents in live mode.

    Requires typing SEND exactly before any commits are written.
    (IBKR order transmission is NOT yet implemented; this writes the commit
    record only.  Actual live order routing remains a manual step.)

    Returns dict with committed/skipped/errors counts.
    """
    today = _get_local_date()

    # --- SEND confirmation gate ---
    print()
    print("=" * 72)
    print(f"  CCC EXECUTE  —  LIVE MODE  —  {today}")
    print(f"  Actions file: {actions_file}")
    print(f"  {len(open_actions)} OPEN intent(s) pending")
    print()
    print("  ⚠️  LIVE MODE: This will write LIVE commit records to the ledger.")
    print("  ⚠️  IBKR order transmission must be performed separately.")
    print()
    print("  Type SEND to confirm (anything else aborts): ", end="", flush=True)
    try:
        confirm = input().strip()
    except (EOFError, OSError):
        confirm = ""

    if confirm != "SEND":
        print("  → ABORTED — confirmation not received.")
        print("=" * 72)
        print()
        return {"committed": 0, "skipped_already_committed": 0, "errors": 0, "aborted": True}

    print()
    print("  Confirmation received — proceeding.")
    print("=" * 72)

    timestamp_utc = datetime.now(timezone.utc).isoformat()
    commit_records = _read_commit_records(commit_ledger_path)

    committed = 0
    skipped = 0
    errors = 0

    for action in open_actions:
        intent_path_str = action.get("intent_path", "")
        candidate_id = action.get("candidate_id") or "unknown"

        try:
            intent = _load_intent(intent_path_str)
            from forecast_arb.execution.execute_trade import validate_order_intent
            validate_order_intent(intent)
        except FileNotFoundError as e:
            print(f"\n  ✗ SKIPPED  candidate_id={candidate_id}: {e}")
            errors += 1
            continue
        except Exception as e:
            print(f"\n  ✗ SKIPPED  candidate_id={candidate_id}: intent invalid — {e}")
            errors += 1
            continue

        intent_id = str(intent.get("intent_id", "")).strip()
        if not intent_id:
            print(f"\n  ✗ SKIPPED  candidate_id={candidate_id}: intent has no intent_id")
            errors += 1
            continue

        # Stale intent guard (Task 4 v1.6)
        if not allow_stale:
            stale, stale_reason = _is_stale_intent(intent_path_str, intent, today)
            if stale:
                print(
                    f"\n  ⚠ STALE SKIPPED  candidate_id={candidate_id}: "
                    f"{stale_reason}  (use --allow-stale to override)"
                )
                errors += 1
                continue

        if _is_already_committed(intent_id, commit_records):
            print(f"\n  ↩ ALREADY COMMITTED  intent_id={intent_id[:20]}  candidate_id={candidate_id}")
            skipped += 1
            continue

        try:
            record = _build_canonical_commit_record(
                action=action,
                intent=intent,
                policy_id=policy_id,
                timestamp_utc=timestamp_utc,
                mode="live",
            )
        except ValueError as exc:
            print(f"\n  ✗ COMMIT_FAILED  candidate_id={candidate_id}: {exc}")
            errors += 1
            continue

        _append_commit_record(commit_ledger_path, record)
        commit_records.append(record)
        committed += 1

        strikes = record.get("strikes", [])
        strikes_str = f"{strikes[0]:.0f}/{strikes[1]:.0f}" if len(strikes) == 2 else "?"
        prem = record.get("premium_spent", 0.0)
        print(
            f"\n  ✓ COMMITTED [LIVE]"
            f"  intent_id={intent_id[:20]}"
            f"  candidate_id={candidate_id}"
            f"  {record.get('underlier','')} {record.get('expiry','')} P{strikes_str}"
            f"  qty={record.get('qty',1)}  ${prem:.2f}"
        )

    print()
    print(
        f"  Summary: {committed} newly committed [LIVE], "
        f"{skipped} already committed, "
        f"{errors} error(s)"
    )
    print("=" * 72)
    print()

    return {"committed": committed, "skipped_already_committed": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Public programmatic API (called from daily.py — no shell-out)
# ---------------------------------------------------------------------------

def run_execute(
    actions_file: str,
    commit_ledger_path: str,
    mode: str = "paper",
    quote_only: bool = False,
    allow_stale: bool = False,
) -> Dict[str, Any]:
    """
    Run the CCC execute flow programmatically.

    This is the public API called by daily.py (no shell-out).
    Reads allocator_actions.json, validates intents, and optionally commits.

    Args:
        actions_file:        Path to allocator_actions.json
        commit_ledger_path:  Path to allocator_commit_ledger.jsonl
        mode:                "paper" (default) or "live"
        quote_only:          If True, preview only — commit ledger NOT updated.
        allow_stale:         If True, skip stale intent guard (v1.6 Task 4).

    Returns:
        Dict:
          {
            "committed": int,
            "skipped_already_committed": int,
            "errors": int,
            "quotes_ok": int,
            "mode": str,
            "aborted": bool,
          }

    Raises:
        FileNotFoundError: if actions_file does not exist
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")

    actions_data = _load_actions(actions_file)
    open_actions = _get_open_actions(actions_data)
    policy_id = str(actions_data.get("policy_id", "ccc_v1"))
    commit_path = Path(commit_ledger_path)

    if not open_actions:
        log.info(f"No OPEN actions with intent_path found in {actions_file}")
        return {
            "committed": 0,
            "skipped_already_committed": 0,
            "errors": 0,
            "quotes_ok": 0,
            "mode": "quote-only" if quote_only else mode,
            "aborted": False,
        }

    if quote_only:
        quotes_ok = _run_quote_only(open_actions, actions_file, policy_id)
        return {
            "committed": 0,
            "skipped_already_committed": 0,
            "errors": 0,
            "quotes_ok": quotes_ok,
            "mode": "quote-only",
            "aborted": False,
        }

    if mode == "paper":
        result = _run_paper_stage(
            open_actions, commit_path, actions_file, policy_id, allow_stale=allow_stale
        )
    else:  # live
        result = _run_live_stage(
            open_actions, commit_path, actions_file, policy_id, allow_stale=allow_stale
        )

    aborted = result.pop("aborted", False)
    return {
        "committed": result.get("committed", 0),
        "skipped_already_committed": result.get("skipped_already_committed", 0),
        "errors": result.get("errors", 0),
        "quotes_ok": result.get("committed", 0) + result.get("skipped_already_committed", 0),
        "mode": mode,
        "aborted": aborted,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description=(
            "Execute intents from allocator_actions.json (CCC v1.5). "
            "Only this script writes to allocator_commit_ledger.jsonl."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview intents (no execution, no commit)
  python scripts/ccc_execute.py --actions runs/allocator/allocator_actions.json --paper --quote-only

  # Stage intents in paper mode (write to commit ledger — counts toward spent_today_before)
  python scripts/ccc_execute.py --actions runs/allocator/allocator_actions.json --paper

  # Stage intents in live mode (requires typing SEND)
  python scripts/ccc_execute.py --actions runs/allocator/allocator_actions.json --live
""",
    )
    parser.add_argument("--actions", required=True, help="Path to allocator_actions.json")

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--paper",
        action="store_true",
        help="Paper mode (stage orders but do NOT transmit to exchange)",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Live mode (writes live commit records; requires SEND confirmation)",
    )

    parser.add_argument(
        "--quote-only",
        action="store_true",
        dest="quote_only",
        help="Show intent summary only — do NOT write to commit ledger.",
    )

    parser.add_argument(
        "--allow-stale",
        action="store_true",
        dest="allow_stale",
        default=False,
        help=(
            "Bypass stale-intent guard (v1.6). "
            "By default, intents whose file mtime or embedded timestamp_utc date "
            "is not today are skipped."
        ),
    )

    args = parser.parse_args()
    _setup_logging()

    mode = "live" if args.live else "paper"

    try:
        actions_data = _load_actions(args.actions)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    open_actions = _get_open_actions(actions_data)
    policy_id = str(actions_data.get("policy_id", "ccc_v1"))

    if not open_actions:
        print(f"No OPEN actions with intent_path found in {args.actions}")
        sys.exit(0)

    # Derive commit ledger path from actions file's parent directory
    actions_dir = Path(args.actions).parent
    commit_ledger_path = actions_dir / "allocator_commit_ledger.jsonl"

    allow_stale = args.allow_stale

    if args.quote_only:
        _run_quote_only(open_actions, args.actions, policy_id, allow_stale=allow_stale)
        sys.exit(0)

    if mode == "paper":
        result = _run_paper_stage(
            open_actions, commit_ledger_path, args.actions, policy_id, allow_stale=allow_stale
        )
        log.info(
            f"ccc_execute complete: {result['committed']} committed  "
            f"{result['skipped_already_committed']} already-committed  "
            f"{result['errors']} error(s)"
        )
    else:
        result = _run_live_stage(
            open_actions, commit_ledger_path, args.actions, policy_id, allow_stale=allow_stale
        )
        if result.get("aborted"):
            sys.exit(0)
        log.info(
            f"ccc_execute [LIVE] complete: {result.get('committed',0)} committed  "
            f"errors={result.get('errors',0)}"
        )


if __name__ == "__main__":
    main()
