"""
scripts/evaluate_parameter_overlay.py
======================================
CLI harness for counterfactual evaluation of an approved parameter overlay.

What this does
--------------
- Loads a baseline config and an overlay YAML.
- Merges overlay onto baseline (overlay wins at each leaf).
- Enumerates run directories for the specified period (via build_reflection_packet).
- Applies threshold simulation against captured signals in existing run artifacts.
- Produces a structured comparison and assessment.
- Writes four artifacts to the output directory:
    evaluation_baseline.json    — metrics under baseline config
    evaluation_overlay.json     — metrics under overlay config
    evaluation_comparison.json  — delta, assessment, classification
    evaluation_comparison.md    — human-readable markdown summary

IMPORTANT
---------
All results are COUNTERFACTUAL ONLY (``simulated_only: true``).  The gate
simulation is a simplified threshold check against signals read from existing
artifacts.  It is NOT the full gate pipeline and cannot model structural
parameters (structuring.*, regimes.*) without re-execution.

This script never modifies any config file, never submits trades, and never
updates the proposal status unless ``--proposals`` is explicitly passed.

Usage
-----
    python scripts/evaluate_parameter_overlay.py \\
        --baseline configs/structuring_crash_venture_v2.yaml \\
        --overlay  configs/overlays/20240315T120000_reflection_test.yaml \\
        --since    2024-03-01 \\
        --until    2024-03-15 \\
        --runs-dir runs/ \\
        --out-dir  runs/evaluations/

Optional:
    --proposals  runs/proposals/weekly_reflection_proposals.json
                 When provided, records ``evaluation_path`` on each proposal
                 whose ``overlay_path`` matches the overlay file used.
    --dry-run    Print the markdown summary without writing output files.
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

import yaml  # pyyaml

from forecast_arb.ops.evaluation import (
    SIMULATION_DISCLAIMER,
    _collect_run_dirs,
    build_evaluation_report,
    deep_merge_configs,
)
from forecast_arb.core.reflection_packet import build_reflection_packet

_DEFAULT_RUNS_DIR = Path("runs")
_DEFAULT_OUT_DIR = Path("runs/evaluations")

# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_comparison_md(
    comparison: dict,
    baseline_metrics: dict,
    overlay_metrics: dict,
    period: dict,
    baseline_path: Path,
    overlay_path: Path,
    ts_utc: str,
) -> str:
    """Render a human-readable markdown summary of the evaluation."""
    lines: List[str] = []

    lines.append("# Parameter Overlay Evaluation — Counterfactual Summary")
    lines.append("")
    lines.append(f"> **{SIMULATION_DISCLAIMER}**")
    lines.append("")

    # Metadata
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|-------|-------|")
    lines.append(f"| Generated | {ts_utc} |")
    lines.append(f"| Period | {period.get('since', '?')} → {period.get('until', '?')} |")
    lines.append(f"| Baseline | `{baseline_path}` |")
    lines.append(f"| Overlay | `{overlay_path}` |")
    lines.append("")

    # Overlay classification
    lines.append("## Overlay Key Classification")
    lines.append("")
    for bucket, label in [
        ("fully_evaluable_parameters", "Fully evaluable (threshold simulation)"),
        ("partially_evaluable_parameters", "Partially evaluable (candidates only)"),
        ("requires_rerun_parameters", "Requires re-execution"),
        ("unknown_parameters", "Unknown / skipped"),
    ]:
        keys = comparison.get(bucket, [])
        if keys:
            lines.append(f"**{label}:** {', '.join(f'`{k}`' for k in keys)}")
        else:
            lines.append(f"**{label}:** _(none)_")
    lines.append("")

    # Coverage
    lines.append("## Simulation Coverage")
    lines.append("")
    b_cov = baseline_metrics.get("coverage_rate")
    o_cov = overlay_metrics.get("coverage_rate")
    b_total = baseline_metrics.get("runs_total", 0)
    o_total = overlay_metrics.get("runs_total", 0)
    b_full_sim = baseline_metrics.get("runs_fully_simulated", 0)
    o_full_sim = overlay_metrics.get("runs_fully_simulated", 0)
    b_partial = baseline_metrics.get("runs_partial_signals", 0)
    o_partial = overlay_metrics.get("runs_partial_signals", 0)
    b_nosig = baseline_metrics.get("runs_without_signals", 0)
    o_nosig = overlay_metrics.get("runs_without_signals", 0)

    lines.append("| Metric | Baseline | Overlay |")
    lines.append("|--------|----------|---------|")
    lines.append(f"| Runs total | {b_total} | {o_total} |")
    lines.append(f"| Fully simulated | {b_full_sim} | {o_full_sim} |")
    lines.append(f"| Partial signals | {b_partial} | {o_partial} |")
    lines.append(f"| No signals | {b_nosig} | {o_nosig} |")

    def _pct(v: object) -> str:
        if isinstance(v, float):
            return f"{v:.1%}"
        return "N/A"

    lines.append(f"| Coverage rate | {_pct(b_cov)} | {_pct(o_cov)} |")
    lines.append("")

    # Gate pass rates
    lines.append("## Gate Pass Rate (Fully Simulated Runs Only)")
    lines.append("")
    b_gpr = baseline_metrics.get("gate_pass_rate")
    o_gpr = overlay_metrics.get("gate_pass_rate")
    delta = comparison.get("delta", {})
    gate_delta = delta.get("gate_pass_rate")

    lines.append("| Metric | Baseline | Overlay | Delta |")
    lines.append("|--------|----------|---------|-------|")
    lines.append(
        f"| Gate pass rate | {_pct(b_gpr)} | {_pct(o_gpr)} "
        f"| {_pct(gate_delta) if gate_delta is not None else 'N/A'} |"
    )

    notrade_delta = delta.get("no_trade_rate")
    b_nt = baseline_metrics.get("no_trade_rate")
    o_nt = overlay_metrics.get("no_trade_rate")
    lines.append(
        f"| No-trade rate | {_pct(b_nt)} | {_pct(o_nt)} "
        f"| {_pct(notrade_delta) if notrade_delta is not None else 'N/A'} |"
    )
    lines.append("")

    # Fail reasons
    b_fails = baseline_metrics.get("gate_fail_reasons", {})
    o_fails = overlay_metrics.get("gate_fail_reasons", {})
    all_reasons = sorted(set(b_fails) | set(o_fails))
    if all_reasons:
        lines.append("## Gate Fail Reasons")
        lines.append("")
        lines.append("| Reason | Baseline | Overlay |")
        lines.append("|--------|----------|---------|")
        for reason in all_reasons:
            lines.append(f"| {reason} | {b_fails.get(reason, 0)} | {o_fails.get(reason, 0)} |")
        lines.append("")

    # Assessment
    lines.append("## Assessment")
    lines.append("")
    assessment = comparison.get("assessment", "KEEP_TESTING")
    lines.append(f"**{assessment}**")
    lines.append("")
    rationale = comparison.get("assessment_rationale", "")
    if rationale:
        lines.append(rationale)
        lines.append("")
    caveats = comparison.get("assessment_caveats", [])
    if caveats:
        lines.append("### Caveats")
        lines.append("")
        for caveat in caveats:
            lines.append(f"- {caveat}")
        lines.append("")

    lines.append("---")
    lines.append("_Counterfactual evaluation — not a system re-execution._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core callable (testable without subprocess)
# ---------------------------------------------------------------------------


def run_evaluation(
    baseline_path: Path,
    overlay_path: Path,
    since: str,
    until: str,
    runs_dir: Path,
    out_dir: Path,
    dry_run: bool = False,
    proposals_path: Optional[Path] = None,
) -> int:
    """
    Run a counterfactual evaluation of *overlay_path* vs *baseline_path*.

    Returns 0 on success, 1 on error.
    """
    baseline_path = Path(baseline_path)
    overlay_path = Path(overlay_path)
    runs_dir = Path(runs_dir)
    out_dir = Path(out_dir)

    ts_utc = datetime.now(timezone.utc).isoformat()

    # Load configs
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            baseline_config = yaml.safe_load(fh) or {}
    except Exception as exc:
        print(f"[evaluate] ERROR: cannot load baseline {baseline_path}: {exc}", file=sys.stderr)
        return 1

    try:
        with open(overlay_path, encoding="utf-8") as fh:
            overlay_config_raw = yaml.safe_load(fh) or {}
    except Exception as exc:
        print(f"[evaluate] ERROR: cannot load overlay {overlay_path}: {exc}", file=sys.stderr)
        return 1

    # Strip YAML comment-only keys (pyyaml already strips them; raw dict is clean)
    merged_config = deep_merge_configs(baseline_config, overlay_config_raw)

    # Enumerate run dirs via build_reflection_packet
    period = {"since": since, "until": until}
    try:
        packet = build_reflection_packet(
            runs_root=runs_dir,
            since=since,
            until=until,
        )
        run_dir_names: List[str] = packet.get("run_dirs_included", [])
    except Exception as exc:
        print(
            f"[evaluate] WARNING: build_reflection_packet failed ({exc}). "
            "Proceeding with zero run dirs.",
            file=sys.stderr,
        )
        run_dir_names = []

    run_dirs = _collect_run_dirs(runs_dir, run_dir_names)
    print(
        f"[evaluate] Period {since} → {until}: {len(run_dirs)} run dir(s) found.",
        file=sys.stderr,
    )

    # Build report
    report = build_evaluation_report(
        baseline_config=baseline_config,
        overlay_config=merged_config,
        run_dirs=run_dirs,
        period=period,
        ts_utc=ts_utc,
    )

    baseline_metrics = report.get("baseline", {})
    overlay_metrics = report.get("overlay", {})
    comparison = report.get("comparison", {})

    # Render markdown
    md_text = _render_comparison_md(
        comparison=comparison,
        baseline_metrics=baseline_metrics,
        overlay_metrics=overlay_metrics,
        period=period,
        baseline_path=baseline_path,
        overlay_path=overlay_path,
        ts_utc=ts_utc,
    )

    if dry_run:
        print(md_text)
        print(
            f"[evaluate] --dry-run: would have written artifacts to {out_dir}.",
            file=sys.stderr,
        )
        return 0

    # Write artifacts
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_json(out_dir / "evaluation_baseline.json", baseline_metrics)
    _write_json(out_dir / "evaluation_overlay.json", overlay_metrics)
    _write_json(out_dir / "evaluation_comparison.json", comparison)
    (out_dir / "evaluation_comparison.md").write_text(md_text, encoding="utf-8")

    print(f"[evaluate] Wrote evaluation artifacts → {out_dir}")
    print(f"[evaluate] Assessment: {comparison.get('assessment', '?')}")

    # Optionally record evaluation_path in matching proposals
    if proposals_path is not None:
        _record_evaluation_path(
            proposals_path=Path(proposals_path),
            overlay_path=overlay_path,
            evaluation_dir=out_dir,
        )

    return 0


def _write_json(path: Path, obj: object) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)


def _record_evaluation_path(
    proposals_path: Path,
    overlay_path: Path,
    evaluation_dir: Path,
) -> None:
    """
    Update overlay_path-matched proposals with the evaluation directory path.

    Never raises — failures are printed as warnings.
    """
    try:
        from forecast_arb.ops.proposals import load_proposals, save_proposals

        overlay_str = str(overlay_path.resolve())
        eval_str = str(evaluation_dir.resolve())

        container = load_proposals(proposals_path)
        matched = 0
        for p in container.get("proposals", []):
            if p.get("overlay_path") == overlay_str:
                p["evaluation_path"] = eval_str
                matched += 1
        if matched:
            save_proposals(proposals_path, container)
            print(
                f"[evaluate] Recorded evaluation_path on {matched} proposal(s) "
                f"in {proposals_path}."
            )
        else:
            print(
                f"[evaluate] NOTE: no proposals with overlay_path={overlay_str!r} found "
                f"in {proposals_path}. evaluation_path not recorded.",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[evaluate] WARNING: could not update proposals: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Counterfactual evaluation of a parameter overlay against captured run signals. "
            "Never modifies configs, never submits trades. "
            "All results are simulated_only=True."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--baseline", type=Path, required=True,
        help="Path to the baseline config YAML.",
    )
    p.add_argument(
        "--overlay", type=Path, required=True,
        help="Path to the overlay YAML to evaluate.",
    )
    p.add_argument(
        "--since", required=True,
        help="Period start date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--until", required=True,
        help="Period end date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--runs-dir", type=Path, default=_DEFAULT_RUNS_DIR,
        help="Root runs directory.",
    )
    p.add_argument(
        "--out-dir", type=Path, default=_DEFAULT_OUT_DIR,
        help="Output directory for evaluation artifacts.",
    )
    p.add_argument(
        "--proposals", type=Path, default=None,
        help=(
            "Optional: path to managed proposals JSON. When provided, records "
            "evaluation_path on proposals whose overlay_path matches --overlay."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print markdown summary to stdout; do not write output files.",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    rc = run_evaluation(
        baseline_path=args.baseline,
        overlay_path=args.overlay,
        since=args.since,
        until=args.until,
        runs_dir=args.runs_dir,
        out_dir=args.out_dir,
        dry_run=args.dry_run,
        proposals_path=args.proposals,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
