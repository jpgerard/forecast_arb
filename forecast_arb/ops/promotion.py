"""
forecast_arb.ops.promotion
============================
Promotion decision workflow for approved parameter overlays.

Reads pre-loaded dicts (evaluation comparison + matched proposals) and
produces a structured promotion decision.  All file I/O lives in the
calling script (review_overlay_promotion.py).

Advisory only.  No execution changes.  No config mutation.
Never promotes into paper execution automatically.

Decision values
---------------
    DO_NOT_PROMOTE         — default / conservative
    PROMOTE_TO_PAPER_REVIEW — evidence meets all criteria

Output schema
-------------
{
  "schema_version": "1.0",
  "status":          "ok" | "error",
  "simulated_only":  True,
  "simulation_disclaimer": str,
  "overlay_path":    str,
  "evaluation_path": str,
  "proposal_ids":    [str],
  "source_kind_counts": {str: int},
  "decision":        "DO_NOT_PROMOTE" | "PROMOTE_TO_PAPER_REVIEW" | null,
  "reasoning":       str,
  "confidence":      float,
  "confidence_note": str,
  "warnings":        [str],
  "ts_utc":          str,
}

Decision rules (conservative by design)
-----------------------------------------
1. Malformed / missing comparison → status=error, decision=null.
2. Any proposal with type=="strategy" → DO_NOT_PROMOTE (strategy proposals
   require research/paper review workflows, not overlay promotion).
3. Any proposal with overfit_risk=="HIGH" → DO_NOT_PROMOTE.
4. evaluation assessment != "PROMOTE_TO_PAPER_REVIEW" → DO_NOT_PROMOTE.
5. All criteria pass → PROMOTE_TO_PAPER_REVIEW.

Confidence
----------
    base        = min(proposal.confidence or 0.0)  over matched proposals
    confidence  = round(base × coverage_rate, 4)
    Note: naturally conservative — low when coverage is low or proposals
          carry low confidence.

Public API
----------
    build_promotion_decision(comparison, proposals, overlay_path,
                             evaluation_path, ts_utc) -> dict
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

PROMOTION_SCHEMA_VERSION = "1.0"
PROMOTE = "PROMOTE_TO_PAPER_REVIEW"
DO_NOT_PROMOTE = "DO_NOT_PROMOTE"

SIMULATION_DISCLAIMER = (
    "ADVISORY ONLY — promotion decision is based on counterfactual evaluation "
    "of captured artifact signals.  Never applied automatically."
)

_CONFIDENCE_NOTE_TEMPLATE = (
    "Computed as min(proposal.confidence) × coverage_rate "
    "= {base:.3f} × {coverage:.3f}. "
    "Conservative — lower when coverage is low or proposals carry low confidence."
)

_LOW_COVERAGE_WARN_THRESHOLD = 0.50


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_warnings(
    comparison: dict,
    proposals: List[dict],
) -> List[str]:
    """Collect all non-blocking warnings from comparison and proposals."""
    warnings: List[str] = []

    # Propagate assessment caveats from evaluation
    for caveat in comparison.get("assessment_caveats", []):
        if caveat and caveat not in warnings:
            warnings.append(caveat)

    # Structural parameters not evaluated
    rerun_keys = comparison.get("requires_rerun_parameters", [])
    if rerun_keys:
        warnings.append(
            f"{len(rerun_keys)} structural parameter(s) were not simulated "
            f"and require re-execution: {', '.join(rerun_keys)}."
        )

    # Unknown overlay keys
    unknown_keys = comparison.get("unknown_parameters", [])
    if unknown_keys:
        warnings.append(
            f"{len(unknown_keys)} unrecognized overlay key(s) were skipped: "
            f"{', '.join(unknown_keys)}."
        )

    # Coverage below confidence threshold (softer than the blocker in _decide)
    coverage = comparison.get("delta", {}).get("coverage_rate", 0.0)
    if isinstance(coverage, float) and coverage < _LOW_COVERAGE_WARN_THRESHOLD:
        warnings.append(
            f"Simulation coverage {coverage:.0%} is below "
            f"{_LOW_COVERAGE_WARN_THRESHOLD:.0%} — results may be unreliable."
        )

    # HIGH overfit risk proposals (warn even if not blocking yet — _decide will block)
    high_overfit = [
        p.get("id", "?") for p in proposals
        if str(p.get("overfit_risk", "")).upper() == "HIGH"
    ]
    if high_overfit:
        warnings.append(
            f"Proposal(s) with HIGH overfit risk: {', '.join(high_overfit)}. "
            "Promotion blocked."
        )

    return warnings


def _decide(
    comparison: dict,
    proposals: List[dict],
) -> tuple:
    """
    Return (decision, reasoning, blockers).

    decision:  DO_NOT_PROMOTE | PROMOTE_TO_PAPER_REVIEW
    reasoning: human-readable paragraph
    blockers:  list[str] — reasons that prevented promotion (empty when promoting)
    """
    assessment = comparison.get("assessment", "")
    delta = comparison.get("delta", {})
    coverage = delta.get("coverage_rate", 0.0) if isinstance(delta.get("coverage_rate"), float) else 0.0
    gate_delta = delta.get("gate_pass_rate")
    n_runs = delta.get("runs_total", 0)
    rationale = comparison.get("assessment_rationale", "")

    blockers: List[str] = []

    # --- Blocker 1: strategy proposals ----------------------------------------
    strategy_proposals = [p for p in proposals if p.get("type") == "strategy"]
    if strategy_proposals:
        ids = ", ".join(p.get("id", "?") for p in strategy_proposals)
        blockers.append(
            f"Strategy proposal(s) present (ids: {ids}). "
            "Strategy proposals require dedicated research or paper review workflows — "
            "they are not eligible for overlay promotion.  "
            "Only parameter proposals may be promoted via this workflow."
        )

    # --- Blocker 2: HIGH overfit risk ------------------------------------------
    high_overfit = [
        p for p in proposals
        if str(p.get("overfit_risk", "")).upper() == "HIGH"
    ]
    if high_overfit:
        ids = ", ".join(p.get("id", "?") for p in high_overfit)
        blockers.append(
            f"Proposal(s) with HIGH overfit risk (ids: {ids}) — "
            "promotion blocked until overfit risk is reassessed as MEDIUM or LOW."
        )

    # --- Blocker 3: assessment not PROMOTE ------------------------------------
    if assessment != PROMOTE:
        blockers.append(
            f"Evaluation assessment was '{assessment}' — "
            f"promotion requires '{PROMOTE}'.  "
            f"Rationale: {rationale or '(not provided)'}"
        )

    # --- Decision -------------------------------------------------------------
    if blockers:
        if len(blockers) == 1 and strategy_proposals and not high_overfit and assessment != PROMOTE:
            pass  # single blocker fallthrough — already captured

        # Build reasoning
        gate_str = (
            f"{gate_delta:+.1%}" if isinstance(gate_delta, float) else "N/A"
        )
        reasoning = (
            f"DO_NOT_PROMOTE.  "
            f"Gate pass rate delta: {gate_str}, coverage: {coverage:.0%}, "
            f"runs evaluated: {n_runs}.  "
            f"Blockers: {len(blockers)} issue(s) must be resolved before promotion."
        )
        return DO_NOT_PROMOTE, reasoning, blockers

    # All clear — promote
    gate_str = f"{gate_delta:+.1%}" if isinstance(gate_delta, float) else "N/A"
    reasoning = (
        f"PROMOTE_TO_PAPER_REVIEW.  "
        f"Evaluation assessment: {PROMOTE}.  "
        f"Gate pass rate delta: {gate_str}, coverage: {coverage:.0%}, "
        f"runs evaluated: {n_runs}.  "
        f"All {len(proposals)} proposal(s) have acceptable overfit risk and are "
        f"parameter type.  "
        "Operator review required before any config change is applied."
    )
    return PROMOTE, reasoning, []


def _compute_confidence(
    comparison: dict,
    proposals: List[dict],
) -> tuple:
    """Return (confidence, base, coverage_rate) for output."""
    coverage = comparison.get("delta", {}).get("coverage_rate", 0.0)
    if not isinstance(coverage, float):
        coverage = 0.0

    if not proposals:
        base = 0.0
    else:
        confs = [p.get("confidence") or 0.0 for p in proposals]
        base = min(max(0.0, float(c)) for c in confs)

    confidence = round(base * coverage, 4)
    return confidence, base, coverage


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_promotion_decision(
    comparison: dict,
    proposals: List[dict],
    overlay_path: str,
    evaluation_path: str,
    ts_utc: str,
) -> dict:
    """
    Build a promotion decision from evaluation comparison and matched proposals.

    Args:
        comparison:      Loaded evaluation_comparison.json dict.
        proposals:       List of proposal dicts matching this overlay
                         (from the managed proposals store).
        overlay_path:    Absolute path string to the overlay YAML (record only).
        evaluation_path: Absolute path string to the evaluation directory (record only).
        ts_utc:          ISO 8601 timestamp for this decision.

    Returns:
        Promotion decision dict.  Never raises.
    """
    proposal_ids = [p.get("id", "") for p in proposals if p.get("id")]
    source_kind_counts = dict(Counter(
        p.get("source_kind", "unknown") for p in proposals
    ))

    # ----------------------------------------------------------------
    # Guard: malformed comparison
    # ----------------------------------------------------------------
    if not comparison or not isinstance(comparison, dict):
        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "status": "error",
            "simulated_only": True,
            "simulation_disclaimer": SIMULATION_DISCLAIMER,
            "overlay_path": str(overlay_path),
            "evaluation_path": str(evaluation_path),
            "proposal_ids": proposal_ids,
            "source_kind_counts": source_kind_counts,
            "decision": None,
            "reasoning": "Evaluation comparison is missing or malformed — cannot decide.",
            "confidence": 0.0,
            "confidence_note": _CONFIDENCE_NOTE_TEMPLATE.format(base=0.0, coverage=0.0),
            "warnings": [],
            "ts_utc": ts_utc,
        }

    try:
        warnings = _collect_warnings(comparison, proposals)
        decision, reasoning, blockers = _decide(comparison, proposals)
        confidence, base, coverage_rate = _compute_confidence(comparison, proposals)
        confidence_note = _CONFIDENCE_NOTE_TEMPLATE.format(
            base=base, coverage=coverage_rate,
        )

        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "status": "ok",
            "simulated_only": True,
            "simulation_disclaimer": SIMULATION_DISCLAIMER,
            "overlay_path": str(overlay_path),
            "evaluation_path": str(evaluation_path),
            "proposal_ids": proposal_ids,
            "source_kind_counts": source_kind_counts,
            "decision": decision,
            "reasoning": reasoning,
            "confidence": confidence,
            "confidence_note": confidence_note,
            "warnings": warnings,
            "blockers": blockers,
            "ts_utc": ts_utc,
        }

    except Exception as exc:
        log.error("build_promotion_decision failed: %s", exc, exc_info=True)
        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "status": "error",
            "simulated_only": True,
            "simulation_disclaimer": SIMULATION_DISCLAIMER,
            "overlay_path": str(overlay_path),
            "evaluation_path": str(evaluation_path),
            "proposal_ids": proposal_ids,
            "source_kind_counts": source_kind_counts,
            "decision": DO_NOT_PROMOTE,
            "reasoning": f"Decision build failed: {exc}. Defaulting to DO_NOT_PROMOTE.",
            "confidence": 0.0,
            "confidence_note": _CONFIDENCE_NOTE_TEMPLATE.format(base=0.0, coverage=0.0),
            "warnings": [f"Internal error: {exc}"],
            "blockers": [f"Internal error prevented full evaluation: {exc}"],
            "ts_utc": ts_utc,
        }
