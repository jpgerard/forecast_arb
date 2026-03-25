"""
forecast_arb.ops.preflight
==========================
Broker-state preflight check.

Compares IBKR broker truth (CSV export) against CCC internal state
(positions.json, event ledger) and returns a compact OK/BLOCKED/SKIPPED
report.

All inputs are explicit Path objects — no cwd defaults.
Pure diagnostic: no writes, no mutations, no side effects.

Public API
----------
    run_broker_preflight(
        positions_path,
        fills_ledger_path,
        ibkr_csv_path,
        trade_outcomes_path,
    ) -> dict

Return schema
-------------
    {
        "status":        "OK" | "BLOCKED" | "SKIPPED",
        "reason":        str,
        "drift":         {in_sync, ccc_count, ibkr_count, only_in_ccc,
                          only_in_ibkr, qty_mismatches, headline} | None,
        "inventory":     {"crash_open": int, "selloff_open": int},
        "pending":       {"crash": int, "selloff": int},
        "positions_view": {"open_count_by_regime": dict,
                           "open_premium_total": float,
                           "pending_orders_count": int},
        "errors":        list[str],
        "ts_utc":        str,
    }

Status meanings:
  SKIPPED  — ibkr_csv_path is None or file absent; drift check not attempted
  OK       — CSV present, CCC and IBKR positions agree
  BLOCKED  — CSV present, positions disagree (only_in_ibkr or qty_mismatches)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def run_broker_preflight(
    positions_path: Path,
    fills_ledger_path: Optional[Path],
    ibkr_csv_path: Optional[Path],
    trade_outcomes_path: Optional[Path],
) -> Dict[str, Any]:
    """
    Run a broker-state preflight check.

    Args:
        positions_path:      Path to positions.json (CCC internal state).
                             Absence is tolerated — inventory counts zero.
        fills_ledger_path:   Path to allocator_fills_ledger.jsonl.
                             The commit ledger is inferred as a sibling file
                             (allocator_commit_ledger.jsonl in the same dir).
                             None → pending counts skipped.
        ibkr_csv_path:       Path to IBKR CSV export.
                             None or absent → status="SKIPPED".
        trade_outcomes_path: Path to runs/trade_outcomes.jsonl (event ledger).
                             None or absent → positions_view left empty.

    Returns:
        Preflight result dict (see module docstring for full schema).
    """
    errors: List[str] = []
    ts_utc = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Inventory — read positions.json directly; no policy dict needed
    # ------------------------------------------------------------------
    inventory: Dict[str, int] = {"crash_open": 0, "selloff_open": 0}
    try:
        if positions_path.exists():
            with open(positions_path, "r", encoding="utf-8") as fh:
                raw_positions = json.load(fh)
            if isinstance(raw_positions, list):
                for pos in raw_positions:
                    if int(pos.get("qty_open", 0)) > 0:
                        regime = pos.get("regime", "").lower()
                        if regime == "crash":
                            inventory["crash_open"] += 1
                        elif regime == "selloff":
                            inventory["selloff_open"] += 1
    except Exception as exc:
        errors.append(f"inventory_read_error: {exc}")

    # ------------------------------------------------------------------
    # Pending — commit ledger minus fills ledger
    # ------------------------------------------------------------------
    pending: Dict[str, int] = {"crash": 0, "selloff": 0}
    if fills_ledger_path is not None:
        try:
            commit_ledger_path = fills_ledger_path.parent / "allocator_commit_ledger.jsonl"
            from forecast_arb.allocator.pending import load_pending_counts
            pending = load_pending_counts(
                commit_ledger_path=commit_ledger_path,
                fills_ledger_path=fills_ledger_path,
            )
        except Exception as exc:
            errors.append(f"pending_read_error: {exc}")

    # ------------------------------------------------------------------
    # Positions view — event-ledger based portfolio state
    # ------------------------------------------------------------------
    positions_view: Dict[str, Any] = {
        "open_count_by_regime": {},
        "open_premium_total": 0.0,
        "pending_orders_count": 0,
    }
    if trade_outcomes_path is not None and trade_outcomes_path.exists():
        try:
            from forecast_arb.portfolio.positions_view import load_positions_view
            pv = load_positions_view(str(trade_outcomes_path))
            positions_view = {
                "open_count_by_regime": pv.get("open_count_by_regime", {}),
                "open_premium_total": pv.get("open_premium_total", 0.0),
                "pending_orders_count": len(pv.get("pending_orders", [])),
            }
        except Exception as exc:
            errors.append(f"positions_view_error: {exc}")

    # ------------------------------------------------------------------
    # Drift check — only when CSV is supplied and exists
    # ------------------------------------------------------------------
    if ibkr_csv_path is None or not Path(ibkr_csv_path).exists():
        return {
            "status": "SKIPPED",
            "reason": "No IBKR CSV provided — drift check skipped",
            "drift": None,
            "inventory": inventory,
            "pending": pending,
            "positions_view": positions_view,
            "errors": errors,
            "ts_utc": ts_utc,
        }

    drift: Optional[Dict[str, Any]] = None
    status = "OK"
    reason = "CCC and IBKR positions match"

    try:
        from forecast_arb.allocator.broker_drift import (
            load_ccc_positions,
            load_ibkr_positions_from_csv,
            normalize_ccc_spread_positions,
            normalize_ibkr_spread_positions,
            diff_ccc_vs_ibkr,
        )
        ccc_raw = load_ccc_positions(positions_path)
        ibkr_raw = load_ibkr_positions_from_csv(ibkr_csv_path)
        ccc_norm = normalize_ccc_spread_positions(ccc_raw)
        ibkr_norm = normalize_ibkr_spread_positions(ibkr_raw)
        diff = diff_ccc_vs_ibkr(ccc_norm, ibkr_norm)

        drift = {
            "in_sync": diff.get("in_sync", False),
            "ccc_count": diff.get("ccc_count", 0),
            "ibkr_count": diff.get("ibkr_count", 0),
            "only_in_ccc": diff.get("only_in_ccc", []),
            "only_in_ibkr": diff.get("only_in_ibkr", []),
            "qty_mismatches": diff.get("qty_mismatches", []),
            "headline": diff.get("headline", ""),
        }

        if not diff.get("ok", True):
            errors.extend(diff.get("errors", []))

        only_ibkr = diff.get("only_in_ibkr", [])
        qty_issues = diff.get("qty_mismatches", [])

        if only_ibkr or qty_issues:
            status = "BLOCKED"
            parts: List[str] = []
            if only_ibkr:
                parts.append(f"{len(only_ibkr)} spread(s) in IBKR not in CCC")
            if qty_issues:
                parts.append(f"{len(qty_issues)} qty mismatch(es)")
            reason = "Broker drift detected: " + "; ".join(parts)
        else:
            status = "OK"
            reason = diff.get("headline", "CCC and IBKR positions match")

    except Exception as exc:
        errors.append(f"drift_check_error: {exc}")
        status = "SKIPPED"
        reason = f"Drift check failed with exception: {exc}"

    return {
        "status": status,
        "reason": reason,
        "drift": drift,
        "inventory": inventory,
        "pending": pending,
        "positions_view": positions_view,
        "errors": errors,
        "ts_utc": ts_utc,
    }
