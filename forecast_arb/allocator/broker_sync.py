"""
CCC — Broker-State Sync  (Diagnostic + Import Only)

ROOT CAUSE THIS FILE FIXES
==========================
positions.json is built EXCLUSIVELY from allocator_fills_ledger.jsonl rows
with action="POSITION_OPENED".  Those rows are written only when CCC's own
ccc_reconcile pipeline processes an execution_result.json.

Spreads placed directly in IBKR TWS (or via a previous system) NEVER pass
through that pipeline, so they produce no fills-ledger row, no positions.json
entry, and therefore no inventory count.  The allocator/report see crash_open=0
(or =1 from prior fills) while IBKR holds 3 live bear put spreads.

THIS FILE PROVIDES
==================
1. ``build_ibkr_import_fill_row()``
   Pure function.  Converts an IBKR combo dict into a canonical POSITION_OPENED
   fills-ledger row, indistinguishable from existing rows by downstream readers,
   except that ``source="ibkr_import"`` (diagnostic) and the ``intent_id`` is a
   deterministic string based on contract fields (not a timestamp hash).

2. ``sync_ibkr_positions()``
   Orchestrates: dedup check → append new rows → rebuild positions.json.
   Idempotent: re-running with the same combos is a no-op (dedup by intent_id).

NO EXISTING CODE IS MODIFIED.  All downstream readers (inventory.py, ccc_report,
plan.py) already read from positions.json / fills ledger — they pick up the
imported positions automatically once those files are updated.

IBKR COMBO DICT SCHEMA (input to this module)
=============================================
{
    "symbol":       str,    # e.g. "SPY" (also accepted: key "underlier")
    "expiry":       str,    # "YYYYMMDD" or "YYYY-MM-DD"
    "long_strike":  float,  # higher strike — the BUY put leg
    "short_strike": float,  # lower strike  — the SELL put leg
    "qty":          int,    # number of spreads (default 1)
    "regime":       str,    # "crash" | "selloff"  (default "crash")
    "entry_debit":  float,  # $/contract paid at open (optional; None = unknown)
}

MAPPING TO SleevePosition / POSITION_OPENED ROW
================================================
IBKR field          → CCC field
-----------------------------------------------
symbol              → underlier  (upper-cased)
expiry              → expiry     (normalised to YYYYMMDD, dashes stripped)
long_strike         → strikes[0] (long put — higher, as per CCC convention)
short_strike        → strikes[1] (short put — lower)
qty                 → qty
regime              → regime     ("crash" assumed when absent/unknown)
entry_debit         → entry_debit_gross  (per-contract $; None → time-stop only)
stable position_id  → intent_id  (used for dedup; never collides with real intents)
source="ibkr_import"→ source     (diagnostic tag; no semantic effect)

The resulting row is consumed by:
  fills.build_positions_snapshot() → positions.json
  inventory.compute_inventory_state_from_positions() → crash_open count
  ccc_report.print_positions() / print_portfolio_summary()
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contract-level helpers (dedup independent of intent_id)
# ---------------------------------------------------------------------------

def _contract_key_from_fills_row(row: Dict[str, Any]) -> Optional[tuple]:
    """
    Extract a contract dedup key from a POSITION_OPENED fills-ledger row.

    Returns:
        (underlier, expiry, long_strike, short_strike) tuple, or None if
        the row is not a POSITION_OPENED row or fields are missing/invalid.
    """
    if row.get("action") != "POSITION_OPENED":
        return None
    underlier = str(row.get("underlier") or "").upper()
    expiry = str(row.get("expiry") or "").replace("-", "")
    strikes = row.get("strikes") or []
    if not underlier or not expiry or len(strikes) < 2:
        return None
    try:
        return (underlier, expiry, float(strikes[0]), float(strikes[1]))
    except (TypeError, ValueError):
        return None


def _dedup_fills_rows_by_contract(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Rebuild a fills-row list collapsing contract-level duplicates with debit enrichment.

    Two-pass algorithm:

    Pass 1 — Dedup:
      For POSITION_OPENED rows: first occurrence of each contract key
      (underlier, expiry, strike0, strike1) is kept; subsequent rows for
      the same contract are dropped. Non-POSITION_OPENED rows (ORDER_STAGED,
      ibkr_debit_enrich, etc.) pass through unchanged.

    Pass 2 — Debit enrichment:
      After dedup, scan ALL rows again.  If a later same-contract row has a
      non-null ``entry_debit_gross`` AND the winning first-seen row has none,
      apply the debit from the later row in-place (mutable dict reference).

      Safety: enrichment is applied only when the winning row has NO debit
      (i.e. ``entry_debit_gross is None``).  CCC-executed fills that already
      carry a real debit are never overwritten.

    The winning row's dict is stored by reference in ``result`` and in the
    ``seen`` dict, so in-place mutation in Pass 2 automatically updates the
    object that will be returned.

    Args:
        rows: full list of fills-ledger rows (any mix of action types)

    Returns:
        Deduped + debit-enriched list — one POSITION_OPENED entry per contract.
    """
    # ---- Pass 1: dedup ----
    seen: Dict[tuple, Dict[str, Any]] = {}   # ck → mutable copy of winning row
    result: List[Dict[str, Any]] = []

    for row in rows:
        ck = _contract_key_from_fills_row(row)
        if ck is None:
            # Non-POSITION_OPENED (ORDER_STAGED, etc.) — pass through
            result.append(row)
            continue
        if ck not in seen:
            # First occurrence — keep as the winning position
            row_copy: Dict[str, Any] = dict(row)
            seen[ck] = row_copy
            result.append(row_copy)
        # else: duplicate POSITION_OPENED row — dropped from result,
        # but considered for debit enrichment in Pass 2.

    # ---- Pass 2: debit enrichment ----
    for row in rows:
        ck = _contract_key_from_fills_row(row)
        if ck is None or ck not in seen:
            continue

        winning = seen[ck]

        # Skip if this IS the winning row itself (same intent_id)
        if winning.get("intent_id") == row.get("intent_id"):
            continue

        # Enrich: only if winning has no debit AND this row provides one
        incoming_debit = row.get("entry_debit_gross")
        if (
            winning.get("entry_debit_gross") is None
            and incoming_debit is not None
            and float(incoming_debit or 0) > 0
        ):
            winning["entry_debit_gross"] = incoming_debit
            log.info(
                f"broker_sync: DEBIT_ENRICHED contract={ck} "
                f"entry_debit_gross={incoming_debit} "
                f"from row intent_id={row.get('intent_id')!r}"
            )
        else:
            # Not enrichable (winning already has a debit, or incoming has none).
            # Warn only for two CCC-executed rows (truly unexpected).
            # ibkr_import / ibkr_debit_enrich duplicates are expected and logged at INFO.
            is_import_source = row.get("source", "") in ("ibkr_import", "ibkr_debit_enrich")
            if is_import_source:
                log.info(
                    f"broker_sync: CONTRACT_DEDUP ibkr row superseded by first-seen "
                    f"intent_id={row.get('intent_id')!r} for contract {ck}"
                )
            else:
                log.warning(
                    f"broker_sync: CONTRACT_DEDUP dropping duplicate fills row "
                    f"intent_id={row.get('intent_id')!r} for contract {ck} "
                    f"— first-seen row wins"
                )

    return result


# ---------------------------------------------------------------------------
# Enrichment row builder (append-only, durability layer for debit)
# ---------------------------------------------------------------------------

def _ibkr_debit_enrich_position_id(
    underlier: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
) -> str:
    """
    Build the stable intent_id for a debit-enrichment fills-ledger row.

    Format: ``ibkr_debit_enrich_SPY_20260327_590_570``

    Each (contract, earliest-enrichment) pair gets exactly one enrichment
    row in the fills ledger (deduped by this key).  A second call with
    the same contract but a different debit would be deduped; the first
    enrichment's debit is preserved.  To override, edit the fills ledger
    directly.
    """
    exp_norm = expiry.replace("-", "")
    return (
        f"ibkr_debit_enrich_{underlier.upper()}_{exp_norm}"
        f"_{long_strike:.0f}_{short_strike:.0f}"
    )


def _build_debit_enrich_row(
    underlier: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
    entry_debit: float,
    existing_row: Dict[str, Any],
    date_str: str,
    mode: str,
    policy_id: str = "ccc_v1",
) -> Dict[str, Any]:
    """
    Build a POSITION_OPENED fills-ledger row whose sole purpose is to supply
    a ``entry_debit_gross`` to an existing zero-debit position.

    The row is structurally identical to a normal POSITION_OPENED row so that
    ``_dedup_fills_rows_by_contract()`` can process it; it will be dropped from
    the positions list (the original first-seen row is kept) but its debit will
    be applied in-place to the winning row via debit enrichment (Pass 2).

    The ``intent_id`` prefix ``ibkr_debit_enrich_`` guarantees:
    - No collision with real CCC intents (``a3ea...`` hex) or import rows
      (``ibkr_import_...``)
    - Idempotent: ``append_fills_ledger`` deduplication prevents re-writing if
      this enrichment was already applied
    """
    from datetime import datetime, timezone
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    position_id = _ibkr_debit_enrich_position_id(underlier, expiry, long_strike, short_strike)

    # Inherit most fields from the existing row so the row is self-consistent
    return {
        "date": date_str,
        "timestamp_utc": timestamp_utc,
        "action": "POSITION_OPENED",
        "policy_id": existing_row.get("policy_id", policy_id),
        "mode": mode,
        "intent_id": position_id,
        "intent_path": None,
        "candidate_id": existing_row.get("candidate_id", ""),
        "regime": existing_row.get("regime", "crash"),
        "underlier": underlier,
        "expiry": expiry,
        "strikes": [long_strike, short_strike],
        "qty": existing_row.get("qty", 1),
        "entry_debit_gross": entry_debit,
        "entry_debit_net": None,
        "commissions": None,
        "ibkr": {"orderId": None, "permId": None, "conIds": [], "fills": []},
        "source": "ibkr_debit_enrich",
        "_enrich_note": (
            f"Debit enrichment for pre-existing position on {date_str}. "
            f"This row provides entry_debit_gross to the winning first-seen row. "
            f"It is dropped from positions list; only its debit is applied."
        ),
    }


# ---------------------------------------------------------------------------
# Stable position_id / intent_id for imported positions
# ---------------------------------------------------------------------------

def _ibkr_import_position_id(
    underlier: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
) -> str:
    """
    Build the deterministic dedup key used as intent_id for imported positions.

    Format: ``ibkr_import_SPY_20260417_575_555``

    Properties:
    - Fully deterministic from contract fields → idempotent re-import
    - Prefix "ibkr_import_" guarantees no collision with real intent UUIDs
      (real intents use hex digests or ISO-timestamp-based IDs)
    - Human-readable: operator can grep fills ledger to verify imports

    Args:
        underlier:    e.g. "SPY"
        expiry:       "YYYYMMDD" (dashes already stripped)
        long_strike:  float, e.g. 575.0
        short_strike: float, e.g. 555.0

    Returns:
        str position_id
    """
    exp_norm = expiry.replace("-", "")
    return (
        f"ibkr_import_{underlier.upper()}_{exp_norm}"
        f"_{long_strike:.0f}_{short_strike:.0f}"
    )


# ---------------------------------------------------------------------------
# Pure builder: combo dict → POSITION_OPENED fills-ledger row
# ---------------------------------------------------------------------------

def build_ibkr_import_fill_row(
    combo: Dict[str, Any],
    date_str: str,
    mode: str,
    policy_id: str = "ccc_v1",
) -> Dict[str, Any]:
    """
    Build a canonical POSITION_OPENED fills-ledger row from an IBKR combo dict.

    Pure function — no file I/O.  The resulting row is structurally identical
    to a row produced by ``fills.build_fill_row()`` and is consumed by all the
    same downstream readers (``build_positions_snapshot``, ``inventory``, etc.).

    Distinguishing fields (diagnostic only; no semantic effect):
      source="ibkr_import"
      intent_id = stable "ibkr_import_..." key (not a real CCC intent UUID)

    Args:
        combo:      IBKR combo dict (see module-level docstring for schema)
        date_str:   "YYYY-MM-DD" — date of import (caller provides)
        mode:       "paper" | "live"
        policy_id:  Policy identifier (default "ccc_v1")

    Returns:
        POSITION_OPENED dict matching fills-ledger row schema (§F)

    Raises:
        ValueError: if required fields are missing or invalid
    """
    # --- underlier ---
    underlier = str(combo.get("symbol") or combo.get("underlier") or "").upper()
    if not underlier:
        raise ValueError(f"combo missing 'symbol' or 'underlier': {combo!r}")

    # --- expiry (normalise to YYYYMMDD) ---
    expiry_raw = str(combo.get("expiry") or "").replace("-", "")
    if not expiry_raw or len(expiry_raw) != 8:
        raise ValueError(
            f"combo 'expiry' must be YYYYMMDD or YYYY-MM-DD, got {combo.get('expiry')!r}"
        )

    # --- strikes ---
    try:
        long_strike = float(combo["long_strike"])
        short_strike = float(combo["short_strike"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"combo missing/invalid long_strike or short_strike: {combo!r}") from e

    if long_strike <= 0 or short_strike <= 0:
        raise ValueError(f"strikes must be positive, got {long_strike}/{short_strike}")

    if long_strike <= short_strike:
        raise ValueError(
            f"long_strike ({long_strike}) must be > short_strike ({short_strike}) "
            f"for a bear put debit spread — check combo: {combo!r}"
        )

    # --- qty ---
    qty = int(combo.get("qty", 1))
    if qty <= 0:
        qty = 1

    # --- regime (default crash for un-labelled SPY bear put spreads) ---
    regime = str(combo.get("regime") or "crash").lower()
    if regime not in ("crash", "selloff"):
        log.warning(
            f"broker_sync: unrecognised regime {regime!r} for "
            f"{underlier} {expiry_raw} {long_strike}/{short_strike} — using 'crash'"
        )
        regime = "crash"

    # --- entry debit (optional — None → time-stop only on harvest) ---
    entry_debit: Optional[float] = None
    raw_debit = combo.get("entry_debit")
    if raw_debit is not None:
        try:
            v = float(raw_debit)
            entry_debit = v if v > 0 else None
        except (TypeError, ValueError):
            entry_debit = None

    # --- stable intent_id (dedup key) ---
    position_id = _ibkr_import_position_id(underlier, expiry_raw, long_strike, short_strike)

    timestamp_utc = datetime.now(timezone.utc).isoformat()

    return {
        "date": date_str,
        "timestamp_utc": timestamp_utc,
        "action": "POSITION_OPENED",
        "policy_id": policy_id,
        "mode": mode,
        # intent_id doubles as the stable dedup key for this import row.
        # Prefix "ibkr_import_" guarantees no collision with CCC-issued intent UUIDs.
        "intent_id": position_id,
        "intent_path": None,
        "candidate_id": "",          # no CCC candidate — position pre-dates CCC pipeline
        "regime": regime,
        "underlier": underlier,
        "expiry": expiry_raw,
        "strikes": [long_strike, short_strike],
        "qty": qty,
        "entry_debit_gross": entry_debit,
        "entry_debit_net": None,
        "commissions": None,
        "ibkr": {
            "orderId": None,
            "permId": None,
            "conIds": [],
            "fills": [],
        },
        # source="ibkr_import" is the only diagnostic tag that differs from a
        # normal fill row.  All downstream readers (build_positions_snapshot,
        # compute_inventory_state_from_positions, harvest, marks) treat this row
        # exactly like any other POSITION_OPENED row.
        "source": "ibkr_import",
        "_import_note": (
            f"Imported from live IBKR account on {date_str}. "
            f"This position was NOT created through the CCC execution pipeline."
        ),
    }


# ---------------------------------------------------------------------------
# Orchestration: import a list of combos into fills ledger + positions.json
# ---------------------------------------------------------------------------

def sync_ibkr_positions(
    combos: List[Dict[str, Any]],
    fills_ledger_path: Path,
    positions_path: Path,
    mode: str,
    policy_id: str = "ccc_v1",
    date_str: Optional[str] = None,
    dry_run: bool = False,
    force_rebuild: bool = False,
) -> Dict[str, Any]:
    """
    Import a list of IBKR combo positions into the fills ledger and rebuild
    positions.json.

    This is the main entry point called by ``scripts/ccc_import_ibkr_positions.py``.

    IDEMPOTENT: Re-running with the same combos is a strict no-op.  Each row's
    ``intent_id`` is the stable ``ibkr_import_...`` key; ``append_fills_ledger``
    deduplicates on intent_id before writing.

    ATOMIC for positions.json: positions.json is only rebuilt when at least one
    new row was appended, or force_rebuild=True, or in dry_run mode for preview.
    It is NOT touched if all combos are already in the fills ledger (unless
    force_rebuild=True is passed to collapse pre-existing contract duplicates).

    Args:
        combos:             List of IBKR combo dicts (see module-level docstring)
        fills_ledger_path:  Path to allocator_fills_ledger.jsonl
        positions_path:     Path to positions.json
        mode:               "paper" | "live"
        policy_id:          CCC policy identifier
        date_str:           Override date "YYYY-MM-DD"; defaults to today Eastern
        dry_run:            If True, validate and preview but do NOT write files
        force_rebuild:      If True, always rebuild positions.json from fills ledger
                            even when no new rows were imported.  Use this to
                            collapse pre-existing contract-level duplicates (e.g.
                            when an ibkr_import row was previously written for a
                            spread that already had a CCC-executed row).

    Returns:
        {
            "imported":          int,          # new rows appended to fills ledger
            "skipped_dedup":     int,          # already present (idempotent)
            "errors":            List[str],    # per-combo validation errors
            "positions_written": bool,         # True if positions.json was rebuilt
            "fills_written":     bool,         # True if fills ledger was written
            "dry_run":           bool,
            "force_rebuild":     bool,
            "positions_preview": List[dict],   # (only when dry_run=True)
        }
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")

    # Lazy imports to avoid circular deps
    from .fills import (
        _get_eastern_date,
        append_fills_ledger,
        build_positions_snapshot,
        read_fills_ledger,
        write_positions_snapshot,
    )

    if date_str is None:
        date_str = _get_eastern_date()

    # Read existing fills ledger once (passed to append for in-memory dedup)
    existing_rows = read_fills_ledger(fills_ledger_path)

    result: Dict[str, Any] = {
        "imported": 0,
        "enriched": 0,
        "skipped_dedup": 0,
        "errors": [],
        "positions_written": False,
        "fills_written": False,
        "dry_run": dry_run,
    }
    if dry_run:
        result["positions_preview"] = []

    new_rows: List[Dict[str, Any]] = []
    # Combos that need debit enrichment: (combo, contract_key) pairs where
    # the contract is already tracked but has no debit and new combo provides one.
    debit_enrichment_candidates: List[tuple] = []   # [(combo, ck), ...]
    enrich_rows: List[Dict[str, Any]] = []

    # --- Contract-level pre-filter (fix for the duplicate problem) ---
    # Build a set of already-tracked contract keys from EXISTING fills ledger rows.
    # This catches the case where a CCC-executed fill (different intent_id) already
    # represents the same physical spread: e.g.
    #   existing row: intent_id="a3ea604e..." SPY 20260417 [575, 555] src=execution_result
    #   import attempt: intent_id="ibkr_import_SPY_20260417_575_555" same contract
    #
    # append_fills_ledger only dedup-checks by intent_id, so without this pre-filter
    # both rows would be written — creating a duplicate position in positions.json.
    existing_contracts: set = set()
    for r in existing_rows:
        ck = _contract_key_from_fills_row(r)
        if ck:
            existing_contracts.add(ck)

    for combo in combos:
        try:
            row = build_ibkr_import_fill_row(
                combo=combo,
                date_str=date_str,
                mode=mode,
                policy_id=policy_id,
            )
        except ValueError as e:
            error_msg = f"broker_sync: skipping combo {combo!r}: {e}"
            log.warning(error_msg)
            result["errors"].append(error_msg)
            continue

        # Contract-level dedup: skip if ANY existing POSITION_OPENED row
        # already covers this physical spread, regardless of intent_id.
        combo_ck = (
            row["underlier"],
            row["expiry"],
            float(row["strikes"][0]),
            float(row["strikes"][1]),
        )
        if combo_ck in existing_contracts:
            result["skipped_dedup"] += 1
            log.info(
                f"broker_sync: CONTRACT_DEDUP_SKIPPED {row['intent_id']} "
                f"— contract {combo_ck} already tracked in fills ledger "
                f"(possibly under a different intent_id from a CCC execution)"
            )
            # --- Debit enrichment candidate check ---
            # If this combo has a debit but the stored fills-ledger row
            # has none, queue for enrichment (fills ledger append-only path).
            incoming_debit = combo.get("entry_debit")
            if incoming_debit is not None and float(incoming_debit or 0) > 0:
                existing_row = next(
                    (r for r in existing_rows if _contract_key_from_fills_row(r) == combo_ck),
                    None,
                )
                if existing_row is not None:
                    if existing_row.get("entry_debit_gross") is None:
                        debit_enrichment_candidates.append((combo, combo_ck, existing_row))
                        log.info(
                            f"broker_sync: ENRICH_CANDIDATE {combo_ck} "
                            f"debit={incoming_debit} (existing row has no debit)"
                        )
                    else:
                        log.info(
                            f"broker_sync: ENRICH_SKIPPED {combo_ck} "
                            f"— existing row already has "
                            f"debit={existing_row['entry_debit_gross']}"
                        )
            continue

        # intent_id-level dedup (catches re-import of ibkr_import rows)
        appended, reason = append_fills_ledger(
            fills_ledger_path=fills_ledger_path,
            row=row,
            existing_rows=existing_rows + new_rows,
            dry_run=dry_run,
        )

        if reason == "DEDUP_SKIPPED":
            result["skipped_dedup"] += 1
            log.info(
                f"broker_sync: INTENT_DEDUP_SKIPPED {row['intent_id']} "
                f"(ibkr_import row already in fills ledger)"
            )
        elif appended or (dry_run and reason == "DRY_RUN"):
            result["imported"] += 1
            new_rows.append(row)
            existing_contracts.add(combo_ck)   # guard within-batch duplicates
            log.info(
                f"broker_sync: "
                f"{'DRY_RUN_WOULD_IMPORT' if dry_run else 'IMPORTED'} "
                f"{row['intent_id']}  regime={row['regime']}  "
                f"strikes={row['strikes'][0]:.0f}/{row['strikes'][1]:.0f}"
            )
        else:
            log.warning(
                f"broker_sync: unexpected result for "
                f"{row.get('intent_id')}: appended={appended} reason={reason}"
            )

    # --- Debit enrichment pass ---
    # For each candidate: append a POSITION_OPENED enrichment row to the fills
    # ledger.  The row carries the debit and is deduplicated by the stable
    # ibkr_debit_enrich_... key (idempotent on re-runs).
    # _dedup_fills_rows_by_contract() Pass 2 then applies the debit in-place
    # to the winning first-seen row before positions.json is rebuilt.
    for (combo, combo_ck, existing_row) in debit_enrichment_candidates:
        entry_debit = float(combo["entry_debit"])
        underlier = str(existing_row.get("underlier") or "")
        expiry = str(existing_row.get("expiry") or "").replace("-", "")
        strikes = existing_row.get("strikes") or [0, 0]
        long_strike = float(strikes[0])
        short_strike = float(strikes[1])

        enrich_row = _build_debit_enrich_row(
            underlier=underlier,
            expiry=expiry,
            long_strike=long_strike,
            short_strike=short_strike,
            entry_debit=entry_debit,
            existing_row=existing_row,
            date_str=date_str,
            mode=mode,
            policy_id=policy_id,
        )

        appended, reason = append_fills_ledger(
            fills_ledger_path=fills_ledger_path,
            row=enrich_row,
            existing_rows=existing_rows + new_rows + enrich_rows,
            dry_run=dry_run,
        )

        if appended or (dry_run and reason == "DRY_RUN"):
            enrich_rows.append(enrich_row)
            result["enriched"] += 1
            log.info(
                f"broker_sync: "
                f"{'DRY_RUN_WOULD_ENRICH' if dry_run else 'DEBIT_ENRICHED'} "
                f"{combo_ck} entry_debit_gross={entry_debit}"
            )
        elif reason == "DEDUP_SKIPPED":
            # Enrichment row already in ledger from a prior run.
            # Still re-include it so _dedup can apply the debit.
            enrich_rows.append(enrich_row)
            log.info(
                f"broker_sync: ENRICH_DEDUP_SKIPPED {combo_ck} "
                f"(enrichment row already in fills ledger; debit still applied in rebuild)"
            )

    # --- Rebuild positions.json ---
    any_new = result["imported"] > 0
    any_enrich = len(enrich_rows) > 0
    # Also rebuild if positions.json doesn't exist yet (e.g. when all fills-ledger
    # rows are from a pre-existing CCC execution but positions.json was never built).
    positions_missing = not positions_path.exists()

    if any_new or any_enrich or dry_run or force_rebuild or positions_missing:
        # Combine: existing fills ledger rows + new ibkr_import rows + enrichment rows.
        # _dedup_fills_rows_by_contract() runs TWO passes:
        #   Pass 1: dedup → one position per contract
        #   Pass 2: debit enrichment → applies debit from enrich_rows to winning rows
        all_rows = existing_rows + new_rows + enrich_rows
        deduped_rows = _dedup_fills_rows_by_contract(all_rows)
        positions = build_positions_snapshot(deduped_rows)

        if dry_run:
            result["positions_preview"] = positions
            log.info(
                f"broker_sync: DRY_RUN — would write {len(positions)} "
                f"position(s) to {positions_path}"
            )
        else:
            write_positions_snapshot(positions_path, positions, dry_run=False)
            result["positions_written"] = True
            result["fills_written"] = True
            log.info(
                f"broker_sync: rebuilt positions.json with "
                f"{len(positions)} open position(s)"
            )

    return result


# ---------------------------------------------------------------------------
# Diagnostic helper: diff CCC positions vs IBKR combos
# ---------------------------------------------------------------------------

def diff_ibkr_vs_positions(
    combos: List[Dict[str, Any]],
    positions_path: Path,
) -> Dict[str, Any]:
    """
    Compare a list of IBKR combos against the current positions.json.

    Returns a diagnostic report identifying:
    - ``missing_from_ccc``: IBKR spreads not in positions.json
    - ``extra_in_ccc``:     CCC positions not in IBKR list
    - ``matched``:          spreads present in both

    Args:
        combos:         List of IBKR combo dicts
        positions_path: Path to positions.json

    Returns:
        {
            "missing_from_ccc": List[str],  # position_ids that need import
            "extra_in_ccc":     List[str],  # position_ids in CCC but not IBKR
            "matched":          List[str],  # position_ids present in both
            "ibkr_count":       int,
            "ccc_count":        int,
        }
    """
    from .fills import read_positions_snapshot

    # Build set of stable keys from IBKR combos
    ibkr_keys: Dict[str, Dict[str, Any]] = {}
    for combo in combos:
        underlier = str(combo.get("symbol") or combo.get("underlier") or "").upper()
        expiry = str(combo.get("expiry") or "").replace("-", "")
        try:
            long_s = float(combo.get("long_strike", 0))
            short_s = float(combo.get("short_strike", 0))
        except (TypeError, ValueError):
            continue
        key = _ibkr_import_position_id(underlier, expiry, long_s, short_s)
        ibkr_keys[key] = combo

    # Build set of stable keys from CCC positions.json
    ccc_positions = read_positions_snapshot(positions_path)
    ccc_keys: Dict[str, Dict[str, Any]] = {}
    for pos in ccc_positions:
        pos_id = str(pos.get("position_id") or "")
        strikes = pos.get("strikes") or []
        underlier = str(pos.get("underlier") or "").upper()
        expiry = str(pos.get("expiry") or "").replace("-", "")
        if len(strikes) >= 2:
            # Reconstruct the ibkr_import key to check for match
            cand_key = _ibkr_import_position_id(
                underlier, expiry, float(strikes[0]), float(strikes[1])
            )
            ccc_keys[cand_key] = pos
        # Also index by position_id (for positions already in fills ledger)
        if pos_id:
            ccc_keys[pos_id] = pos

    ibkr_set = set(ibkr_keys.keys())
    ccc_set = set(ccc_keys.keys())

    missing_from_ccc = sorted(ibkr_set - ccc_set)
    extra_in_ccc = sorted(ccc_set - ibkr_set)
    matched = sorted(ibkr_set & ccc_set)

    return {
        "missing_from_ccc": missing_from_ccc,
        "extra_in_ccc": extra_in_ccc,
        "matched": matched,
        "ibkr_count": len(ibkr_set),
        "ccc_count": len(ccc_positions),
    }
