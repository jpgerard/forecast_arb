"""
scripts/review_overlay_promotion.py
=====================================
CLI harness for producing a promotion decision on an approved parameter overlay.

What this does
--------------
- Loads a managed proposals store and filters to proposals matching --overlay.
- Loads the evaluation comparison artifact (evaluation_comparison.json).
- Calls build_promotion_decision() to produce a structured recommendation.
- Writes two artifacts to --out-dir:
    promotion_decision.json   — machine-readable decision record
    promotion_decision.md     — human-readable summary with blockers section
- Updates promotion_path on matched proposals in the proposals store.
- Optionally appends a PROMOTION_DECIDED lineage event.

IMPORTANT
---------
This script is advisory only.  It NEVER:
- submits paper or live orders
- modifies any config file
- applies any overlay automatically

The promotion decision is a recommendation artifact for operator review.

Usage
-----
    python scripts/review_overlay_promotion.py \\
        --proposals   runs/proposals/weekly_reflection_proposals.json \\
        --overlay     configs/overlays/20240315T120000_reflection_test.yaml \\
        --evaluation  runs/evaluations/evaluation_comparison.json \\
        --out-dir     runs/promotions/ \\
        [--lineage    runs/indexes/improvement_lineage.jsonl] \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from forecast_arb.core.lineage import append_lineage_event
from forecast_arb.ops.promotion import (
    DO_NOT_PROMOTE,
    PROMOTE,
    SIMULATION_DISCLAIMER,
    build_promotion_decision,
)
from forecast_arb.ops.proposals import load_proposals, save_proposals

_DEFAULT_OUT_DIR = Path("runs/promotions")


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_promotion_md(
    decision_dict: dict,
    overlay_path: Path,
    evaluation_path: Path,
    ts_utc: str,
) -> str:
    """Render a human-readable markdown summary of the promotion decision."""
    lines: List[str] = []
    decision = decision_dict.get("decision")
    status = decision_dict.get("status", "ok")

    # ----------------------------------------------------------------
    # Title + disclaimer
    # ----------------------------------------------------------------
    lines.append("# Overlay Promotion Decision")
    lines.append("")
    lines.append(f"> **{SIMULATION_DISCLAIMER}**")
    lines.append("")

    # ----------------------------------------------------------------
    # Decision banner (prominent)
    # ----------------------------------------------------------------
    if decision == PROMOTE:
        lines.append(f"## Decision: ✔ PROMOTE_TO_PAPER_REVIEW")
    elif decision == DO_NOT_PROMOTE:
        lines.append(f"## Decision: ✖ DO_NOT_PROMOTE")
    else:
        lines.append(f"## Decision: ⚠ {decision or 'UNKNOWN'}")
    lines.append("")

    if status == "error":
        lines.append(f"> **Status: ERROR** — {decision_dict.get('reasoning', '')}")
        lines.append("")

    lines.append(f"**Confidence:** {decision_dict.get('confidence', 0.0):.4f}")
    lines.append("")
    conf_note = decision_dict.get("confidence_note", "")
    if conf_note:
        lines.append(f"_{conf_note}_")
        lines.append("")

    # ----------------------------------------------------------------
    # Metadata
    # ----------------------------------------------------------------
    lines.append("## Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Generated | {ts_utc} |")
    lines.append(f"| Overlay | `{overlay_path}` |")
    lines.append(f"| Evaluation | `{evaluation_path}` |")
    proposal_ids = decision_dict.get("proposal_ids", [])
    lines.append(f"| Proposals | {', '.join(f'`{p}`' for p in proposal_ids) or '_(none)_'} |")
    lines.append("")

    # Source kind breakdown
    skc = decision_dict.get("source_kind_counts", {})
    if skc:
        lines.append("**Proposal source kinds:**")
        for kind, count in sorted(skc.items()):
            lines.append(f"- `{kind}`: {count}")
        lines.append("")

    # ----------------------------------------------------------------
    # Reasoning
    # ----------------------------------------------------------------
    lines.append("## Reasoning")
    lines.append("")
    reasoning = decision_dict.get("reasoning", "")
    if reasoning:
        lines.append(reasoning)
        lines.append("")

    # ----------------------------------------------------------------
    # Blockers to Promotion (only when DO_NOT_PROMOTE)
    # ----------------------------------------------------------------
    blockers = decision_dict.get("blockers", [])
    if decision == DO_NOT_PROMOTE and blockers:
        lines.append("## Blockers to Promotion")
        lines.append("")
        lines.append(
            "The following issues must be resolved before this overlay "
            "can be considered for paper review:"
        )
        lines.append("")
        for i, blocker in enumerate(blockers, start=1):
            lines.append(f"{i}. {blocker}")
        lines.append("")

    # ----------------------------------------------------------------
    # Warnings
    # ----------------------------------------------------------------
    warnings = decision_dict.get("warnings", [])
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # ----------------------------------------------------------------
    # Footer
    # ----------------------------------------------------------------
    lines.append("---")
    lines.append(
        "_Advisory only — promotion decision requires operator review "
        "before any config change is applied._"
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core callable (testable without subprocess)
# ---------------------------------------------------------------------------


def run_promotion(
    proposals_path: Path,
    overlay_path: Path,
    evaluation_json: Path,
    out_dir: Path,
    dry_run: bool = False,
    lineage_path: Optional[Path] = None,
) -> int:
    """
    Produce a promotion decision for the given overlay.

    Returns 0 on success, 1 on error.
    """
    proposals_path = Path(proposals_path)
    overlay_path = Path(overlay_path)
    evaluation_json = Path(evaluation_json)
    out_dir = Path(out_dir)

    ts_utc = datetime.now(timezone.utc).isoformat()

    # ----------------------------------------------------------------
    # Load evaluation comparison
    # ----------------------------------------------------------------
    if not evaluation_json.exists():
        print(
            f"[promote] ERROR: evaluation JSON not found: {evaluation_json}",
            file=sys.stderr,
        )
        return 1

    try:
        with open(evaluation_json, encoding="utf-8") as fh:
            comparison = json.load(fh)
    except Exception as exc:
        print(
            f"[promote] ERROR: cannot load evaluation JSON {evaluation_json}: {exc}",
            file=sys.stderr,
        )
        return 1

    # ----------------------------------------------------------------
    # Load proposals matching this overlay
    # ----------------------------------------------------------------
    overlay_str = str(overlay_path.resolve())
    container = load_proposals(proposals_path)
    matched_proposals = [
        p for p in container.get("proposals", [])
        if p.get("overlay_path") == overlay_str
    ]

    if not matched_proposals:
        print(
            f"[promote] WARNING: no proposals found with overlay_path={overlay_str!r} "
            f"in {proposals_path}. Proceeding with empty proposal list "
            "(decision will be conservative).",
            file=sys.stderr,
        )

    # ----------------------------------------------------------------
    # Build decision
    # ----------------------------------------------------------------
    evaluation_path_str = str(evaluation_json.parent.resolve())
    decision_dict = build_promotion_decision(
        comparison=comparison,
        proposals=matched_proposals,
        overlay_path=overlay_str,
        evaluation_path=evaluation_path_str,
        ts_utc=ts_utc,
    )

    # ----------------------------------------------------------------
    # Render markdown
    # ----------------------------------------------------------------
    md_text = _render_promotion_md(
        decision_dict=decision_dict,
        overlay_path=overlay_path,
        evaluation_path=evaluation_json.parent,
        ts_utc=ts_utc,
    )

    decision = decision_dict.get("decision")
    print(f"[promote] Decision: {decision or '(null)'}")
    print(f"[promote] Confidence: {decision_dict.get('confidence', 0.0):.4f}")
    if decision_dict.get("blockers"):
        print(
            f"[promote] Blockers: {len(decision_dict['blockers'])}",
            file=sys.stderr,
        )

    if dry_run:
        print(md_text)
        print(
            f"[promote] --dry-run: would have written artifacts to {out_dir}.",
            file=sys.stderr,
        )
        return 0

    # ----------------------------------------------------------------
    # Write artifacts
    # ----------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "promotion_decision.json"
    md_path = out_dir / "promotion_decision.md"

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(decision_dict, fh, indent=2, default=str)
    md_path.write_text(md_text, encoding="utf-8")

    print(f"[promote] Wrote promotion_decision.json → {json_path}")
    print(f"[promote] Wrote promotion_decision.md   → {md_path}")

    # ----------------------------------------------------------------
    # Update promotion_path on matched proposals
    # ----------------------------------------------------------------
    promotion_path_str = str(out_dir.resolve())
    updated_container = load_proposals(proposals_path)
    promotion_ids = set(decision_dict.get("proposal_ids", []))
    updated = 0
    for p in updated_container.get("proposals", []):
        if p.get("id") in promotion_ids:
            p["promotion_decision_path"] = promotion_path_str
            updated += 1
    if updated:
        save_proposals(proposals_path, updated_container)
        print(f"[promote] Recorded promotion_decision_path on {updated} proposal(s).")

    # ----------------------------------------------------------------
    # Lineage event
    # ----------------------------------------------------------------
    if lineage_path is not None:
        # Derive source_period from proposals if available
        source_period = None
        if matched_proposals:
            sp = matched_proposals[0].get("source_period")
            if isinstance(sp, dict) and sp.get("since") and sp.get("until"):
                source_period = sp

        append_lineage_event(
            lineage_path=Path(lineage_path),
            event={
                "event_type": "PROMOTION_DECIDED",
                "ts_utc": ts_utc,
                "source_period": source_period,
                "proposal_ids": decision_dict.get("proposal_ids", []),
                "overlay_path": overlay_str,
                "evaluation_path": evaluation_path_str,
                "promotion_path": promotion_path_str,
                "notes": f"Decision: {decision}",
            },
        )
        print(f"[promote] Appended PROMOTION_DECIDED → {lineage_path}")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Produce a promotion decision for an approved parameter overlay. "
            "Advisory only — never submits orders, never modifies configs."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--proposals", type=Path, required=True,
        help="Path to the managed proposals JSON store.",
    )
    p.add_argument(
        "--overlay", type=Path, required=True,
        help="Path to the overlay YAML to evaluate.",
    )
    p.add_argument(
        "--evaluation", type=Path, required=True,
        help="Path to evaluation_comparison.json produced by evaluate_parameter_overlay.py.",
    )
    p.add_argument(
        "--out-dir", type=Path, default=_DEFAULT_OUT_DIR,
        help="Output directory for promotion artifacts.",
    )
    p.add_argument(
        "--lineage", type=Path, default=None,
        help="Optional: lineage JSONL path. Appends PROMOTION_DECIDED event when provided.",
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print markdown to stdout; do not write output files.",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    rc = run_promotion(
        proposals_path=args.proposals,
        overlay_path=args.overlay,
        evaluation_json=args.evaluation,
        out_dir=args.out_dir,
        dry_run=args.dry_run,
        lineage_path=args.lineage,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
