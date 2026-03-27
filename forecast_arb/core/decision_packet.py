"""
forecast_arb.core.decision_packet
==================================
Compact daily decision packet builder.

Assembles a structured, schema-versioned snapshot of one run's key outputs
for operator review and eventual LLM consumption.

Contents:
- Run-level summary (from run_summary.extract_summary_safe)
- Broker preflight status (passed in; not fetched here)
- Top candidates per regime (review_candidates.json preferred, tickets.json fallback)
- Signals (p_external, p_implied, edge, confidence, gate_decision)
- Notes flags (human-readable warnings)

No LLM calls. No file writes. Purely additive read path.

Public API
----------
    build_decision_packet(
        run_dir,
        preflight,
        max_candidates_per_regime,
    ) -> dict

Packet schema (schema_version "2.0")
-------------------------------------
    {
        "schema_version": "2.0",
        "ts_utc": str,
        "run": {
            run_id, timestamp, mode, decision, reason,
            edge, p_external, p_implied, confidence,
            num_tickets, submit_requested, submit_executed
        },
        "broker_preflight": dict | None,
        "top_candidates": [
            {regime, rank, expiry, long_strike, short_strike,
             ev_per_dollar, debit_per_contract, candidate_id}
        ],
        "signals": {
            p_external, p_implied, edge, confidence, gate_decision
        },
        "notes": list[str],   # "BROKER_DRIFT_BLOCKED", "NO_TRADE",
                              # "LOW_CONFIDENCE", "SUBMIT_EXECUTED"
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_SCHEMA_VERSION = "2.0"
_LOW_CONFIDENCE_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_top_candidates(run_dir: Path, max_per_regime: int) -> List[Dict[str, Any]]:
    """
    Load top candidates from run artifacts. Never raises.

    Priority:
    1. artifacts/review_candidates.json  — preferred (multi-regime keyed schema)
    2. artifacts/tickets.json            — fallback (flat list or {"tickets": [...]})

    Returns at most max_per_regime candidates per regime, sorted by rank ascending.
    Each returned dict has: regime, rank, expiry, long_strike, short_strike,
    ev_per_dollar, debit_per_contract, candidate_id.
    """
    artifacts_dir = run_dir / "artifacts"
    candidates: List[Dict[str, Any]] = []

    # ---- Attempt 1: review_candidates.json --------------------------------
    rc_path = artifacts_dir / "review_candidates.json"
    if rc_path.exists():
        try:
            with open(rc_path, "r", encoding="utf-8") as fh:
                rc = json.load(fh)
            # Schema: {"regimes": {regime_name: {"candidates": [...]}}}
            for regime_name, regime_data in rc.get("regimes", {}).items():
                regime_list = sorted(
                    regime_data.get("candidates", []),
                    key=lambda c: c.get("rank", 9999),
                )
                for c in regime_list[:max_per_regime]:
                    strikes = c.get("strikes", {})
                    candidates.append({
                        "regime": regime_name,
                        "rank": c.get("rank"),
                        "expiry": c.get("expiry"),
                        "long_strike": strikes.get("long_put"),
                        "short_strike": strikes.get("short_put"),
                        "ev_per_dollar": c.get("ev_per_dollar"),
                        "debit_per_contract": c.get("debit_per_contract"),
                        "candidate_id": c.get("candidate_id"),
                    })
            if candidates:
                return candidates
        except Exception as exc:
            log.debug(f"review_candidates.json load failed: {exc}")

    # ---- Fallback: tickets.json -------------------------------------------
    tickets_path = artifacts_dir / "tickets.json"
    if tickets_path.exists():
        try:
            with open(tickets_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                ticket_list = raw
            elif isinstance(raw, dict):
                ticket_list = raw.get("tickets", [])
            else:
                ticket_list = []

            # Group by regime, sort by rank, take top N per regime
            by_regime: Dict[str, List] = {}
            for t in ticket_list:
                r = t.get("regime", "unknown")
                by_regime.setdefault(r, []).append(t)

            for regime_name, regime_tickets in by_regime.items():
                sorted_tickets = sorted(
                    regime_tickets, key=lambda t: t.get("rank", 9999)
                )
                for t in sorted_tickets[:max_per_regime]:
                    strikes = t.get("strikes", {})
                    candidates.append({
                        "regime": regime_name,
                        "rank": t.get("rank"),
                        "expiry": t.get("expiry"),
                        "long_strike": strikes.get("long_put"),
                        "short_strike": strikes.get("short_put"),
                        "ev_per_dollar": t.get("metrics", {}).get("ev_per_dollar"),
                        "debit_per_contract": t.get("debit_per_contract"),
                        "candidate_id": t.get("candidate_id"),
                    })
        except Exception as exc:
            log.debug(f"tickets.json load failed: {exc}")

    return candidates


def _read_gate_decision(run_dir: Path) -> Optional[str]:
    """Read gate_decision from artifacts/gate_decision.json. Returns None on any failure."""
    gate_path = run_dir / "artifacts" / "gate_decision.json"
    if not gate_path.exists():
        return None
    try:
        with open(gate_path, "r", encoding="utf-8") as fh:
            gd = json.load(fh)
        return gd.get("decision") or gd.get("verdict")
    except Exception:
        return None


def _read_gate_decision_full(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the full gate_decision dict. Returns None on any failure."""
    gate_path = run_dir / "artifacts" / "gate_decision.json"
    if not gate_path.exists():
        return None
    try:
        with open(gate_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _p_external_used_for_gating(gate_dict: Optional[Dict[str, Any]]) -> bool:
    """True if p_external was a non-None input when the gate was evaluated.

    Patch C definition: "available / consulted by gate" — not "determinative".
    A True value means Kalshi data existed and was fed to the gate logic.
    It does NOT imply the gate passed or that external evidence drove the outcome.
    A False value means p_external was None at gate time (fallback / no match).
    """
    if gate_dict is None:
        return False
    return gate_dict.get("p_external") is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_decision_packet(
    run_dir: Optional[Path] = None,
    preflight: Optional[Dict[str, Any]] = None,
    max_candidates_per_regime: int = 3,
) -> Dict[str, Any]:
    """
    Build a compact daily decision packet.

    Args:
        run_dir:                   Path to a run directory. None → run fields
                                   are empty/None, top_candidates=[].
        preflight:                 Result from run_broker_preflight().
                                   None → broker_preflight key is None.
        max_candidates_per_regime: Max top candidates to include per regime.

    Returns:
        Packet dict matching schema_version "2.0".
    """
    from forecast_arb.core.run_summary import extract_summary_safe

    ts_utc = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Run summary
    # ------------------------------------------------------------------
    if run_dir is not None:
        run_summary = extract_summary_safe(run_dir)
    else:
        run_summary = {
            "run_id": None,
            "timestamp": None,
            "mode": None,
            "decision": "UNKNOWN",
            "reason": "NO_RUN_DIR",
            "edge": None,
            "p_external": None,
            "p_implied": None,
            "confidence": None,
            "num_tickets": 0,
            "submit_requested": False,
            "submit_executed": False,
            "p_evidence_class": None,  # Patch B
            # Patch C
            "p_external_authoritative_capable": False,
            "p_external_semantic_notes": [],
            "p_external_role": None,
            "p_baseline_source": "options_implied",
        }

    # ------------------------------------------------------------------
    # Gate decision
    # ------------------------------------------------------------------
    gate_decision: Optional[str] = None
    gate_decision_full: Optional[Dict[str, Any]] = None
    if run_dir is not None:
        gate_decision = _read_gate_decision(run_dir)
        gate_decision_full = _read_gate_decision_full(run_dir)

    # ------------------------------------------------------------------
    # Top candidates
    # ------------------------------------------------------------------
    top_candidates: List[Dict[str, Any]] = []
    if run_dir is not None:
        top_candidates = _load_top_candidates(run_dir, max_candidates_per_regime)

    # ------------------------------------------------------------------
    # Notes flags
    # ------------------------------------------------------------------
    notes: List[str] = []

    if preflight is not None and preflight.get("status") == "BLOCKED":
        notes.append("BROKER_DRIFT_BLOCKED")

    if run_summary.get("decision") == "NO_TRADE":
        notes.append("NO_TRADE")

    confidence = run_summary.get("confidence")
    if confidence is not None and confidence < _LOW_CONFIDENCE_THRESHOLD:
        notes.append("LOW_CONFIDENCE")

    if run_summary.get("submit_executed"):
        notes.append("SUBMIT_EXECUTED")

    # ------------------------------------------------------------------
    # Assemble
    # ------------------------------------------------------------------
    return {
        "schema_version": _SCHEMA_VERSION,
        "ts_utc": ts_utc,
        "run": {
            "run_id": run_summary.get("run_id"),
            "timestamp": run_summary.get("timestamp"),
            "mode": run_summary.get("mode"),
            "decision": run_summary.get("decision"),
            "reason": run_summary.get("reason"),
            "edge": run_summary.get("edge"),
            "p_external": run_summary.get("p_external"),
            "p_implied": run_summary.get("p_implied"),
            "confidence": run_summary.get("confidence"),
            "num_tickets": run_summary.get("num_tickets", 0),
            "submit_requested": run_summary.get("submit_requested", False),
            "submit_executed": run_summary.get("submit_executed", False),
            "p_evidence_class": run_summary.get("p_evidence_class"),  # Patch B
            # Patch C
            "p_external_authoritative_capable": run_summary.get(
                "p_external_authoritative_capable", False
            ),
            "p_external_semantic_notes": run_summary.get("p_external_semantic_notes", []),
            "p_external_role": run_summary.get("p_external_role"),
            "p_baseline_source": run_summary.get("p_baseline_source", "options_implied"),
        },
        "broker_preflight": preflight,
        "top_candidates": top_candidates,
        "signals": {
            "p_external": run_summary.get("p_external"),
            "p_implied": run_summary.get("p_implied"),
            "edge": run_summary.get("edge"),
            "confidence": run_summary.get("confidence"),
            "gate_decision": gate_decision,
            "p_evidence_class": run_summary.get("p_evidence_class"),  # Patch B
            # Patch C
            "p_external_authoritative_capable": run_summary.get(
                "p_external_authoritative_capable", False
            ),
            # p_external_used_for_gating: True when p_external was a non-None input
            # to the gate (available/consulted — not "determinative").
            "p_external_used_for_gating": _p_external_used_for_gating(gate_decision_full),
            "p_external_role": run_summary.get("p_external_role"),
            "p_baseline_source": run_summary.get("p_baseline_source", "options_implied"),
            "p_external_semantic_notes": run_summary.get("p_external_semantic_notes", []),
        },
        "notes": notes,
    }
