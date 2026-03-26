"""
scripts/agent_weekly_reflection.py
====================================
Weekly reflection workflow. Builds a structured packet from the past week's
run data, and optionally calls OpenAI to produce an advisory analysis.

Advisory only. No config mutation. No execution changes. No autonomous action.

Steps
-----
1. build_reflection_packet() — always runs, no API calls
2. Write <out_dir>/weekly_reflection_packet.json
3. If --reflect:
   a. run_weekly_reflection(packet)
   b. Write <out_dir>/weekly_reflection_report.json
   c. Write <out_dir>/weekly_reflection_report.md
   d. Extract parameter_suggestions → write <out_dir>/weekly_parameter_proposals.json
4. Compact stdout summary

Output directory defaults to <runs-root>/weekly/<since>_<until>/

Returns
-------
{"packet": dict, "report": dict | None, "proposals": dict | None}

Usage
-----
    python scripts/agent_weekly_reflection.py --since 2026-03-18 [--until 2026-03-25]
        [--runs-root runs/] [--config configs/structuring_crash_venture_v2.yaml]
        [--trade-outcomes runs/trade_outcomes.jsonl] [--reflect]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(_SCRIPTS_DIR))

from forecast_arb.core.reflection_packet import build_reflection_packet
from forecast_arb.ops.reflection import run_weekly_reflection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _render_reflection_md(report: dict) -> str:
    """Render a reflection report dict as markdown. Never raises."""
    lines: List[str] = []

    period = report.get("period", {})
    summary = report.get("summary", {})
    since = period.get("since", "?")
    until = period.get("until", "?")

    lines.append(f"# Weekly Reflection Report: {since} → {until}")
    lines.append("")
    lines.append(f"**Status:** {report.get('status', '?')}  ")
    lines.append(f"**Evidence Strength:** {summary.get('evidence_strength', '?')}  ")
    lines.append(f"**Overall Assessment:** {summary.get('overall_assessment', '?')}  ")
    lines.append(f"**Runs Assessed:** {summary.get('n_runs_assessed', 0)}  ")
    lines.append(f"**Trades Assessed:** {summary.get('n_trades_assessed', 0)}")
    lines.append("")

    if report.get("error"):
        lines.append(f"> **Error:** {report['error']}")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"**Headline:** {summary.get('headline', '')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # What worked
    lines.append("## What Worked")
    lines.append("")
    worked = report.get("what_worked") or []
    if not worked:
        lines.append("*No observations.*")
    else:
        for item in worked:
            lines.append(f"- **{item.get('observation', '')}**")
            lines.append(f"  - Evidence: {item.get('evidence', '')}")
            lines.append(f"  - Why: {item.get('why', '')}")
            c = item.get('confidence')
            n = item.get('n_supporting', 0)
            conf_str = f"{c:.2f}" if c is not None else "N/A"
            lines.append(f"  - Confidence: {conf_str} (n={n})")
    lines.append("")

    # What failed
    lines.append("## What Failed")
    lines.append("")
    failed = report.get("what_failed") or []
    if not failed:
        lines.append("*No observations.*")
    else:
        for item in failed:
            lines.append(f"- **{item.get('observation', '')}**")
            cf = item.get('common_factors') or []
            if cf:
                lines.append(f"  - Common factors: {', '.join(cf)}")
            lines.append(f"  - Why: {item.get('why_it_failed', '')}")
            c = item.get('confidence')
            n = item.get('n_supporting', 0)
            conf_str = f"{c:.2f}" if c is not None else "N/A"
            lines.append(f"  - Confidence: {conf_str} (n={n})")
    lines.append("")

    # Calibration
    lines.append("## Calibration Assessment")
    lines.append("")
    cal = report.get("calibration_assessment") or {}
    lines.append(f"**Overall:** {cal.get('overall', '?')}  (confidence: {cal.get('confidence', 0):.2f})")
    lines.append("")
    if cal.get("edge_vs_outcome_narrative"):
        lines.append(f"**Edge vs Outcome:** {cal['edge_vs_outcome_narrative']}")
    if cal.get("rejection_pattern_narrative"):
        lines.append(f"**Rejection Patterns:** {cal['rejection_pattern_narrative']}")
    caveats = cal.get("caveats") or []
    if caveats:
        lines.append("")
        lines.append("**Caveats:**")
        for c in caveats:
            lines.append(f"- {c}")
    lines.append("")

    # Market regime
    lines.append("## Market Regime Assessment")
    lines.append("")
    reg = report.get("market_regime_assessment") or {}
    lines.append(f"**Inferred Regime:** {reg.get('inferred_regime', '?')}  (confidence: {reg.get('confidence', 0):.2f})")
    if reg.get("supporting_evidence"):
        lines.append(f"**Evidence:** {reg['supporting_evidence']}")
    if reg.get("strategy_fit_narrative"):
        lines.append(f"**Strategy Fit:** {reg['strategy_fit_narrative']}")
    lines.append("")

    # Parameter suggestions
    lines.append("## Parameter Suggestions")
    lines.append("")
    suggestions = report.get("parameter_suggestions") or []
    if not suggestions:
        lines.append("*None (hypotheses only — all suggestions require verification before any change).*")
    else:
        lines.append("*All suggestions are hypotheses only. Follow promotion_path before applying any change.*")
        lines.append("")
        for s in suggestions:
            c = s.get("confidence")
            conf_str = f"{c:.2f}" if c is not None else "N/A"
            lines.append(
                f"- **{s.get('parameter', '?')}**: "
                f"{s.get('current_value')} → {s.get('suggested_value')}  "
                f"(confidence: {conf_str}, overfit_risk: {s.get('overfit_risk', '?')})"
            )
            if s.get("reasoning"):
                lines.append(f"  - Reasoning: {s['reasoning']}")
            if s.get("expected_effect"):
                lines.append(f"  - Expected effect: {s['expected_effect']}")
            if s.get("promotion_path"):
                lines.append(f"  - Promotion path: {s['promotion_path']}")
    lines.append("")

    # Open questions
    lines.append("## Open Questions")
    lines.append("")
    questions = report.get("open_questions") or []
    if not questions:
        lines.append("*None.*")
    else:
        for q in questions:
            lines.append(f"- {q}")
    lines.append("")

    # Weak evidence flags
    lines.append("## Weak Evidence Flags")
    lines.append("")
    flags = report.get("weak_evidence_flags") or []
    if not flags:
        lines.append("*None.*")
    else:
        for f in flags:
            lines.append(f"- {f}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Proposals normalizer
# ---------------------------------------------------------------------------


def _normalize_proposals(
    suggestions: List[dict],
    period: dict,
    source_report_path: str,
) -> dict:
    """Normalize parameter_suggestions into a standalone proposals artifact."""
    proposals = []
    for s in suggestions:
        if not s.get("parameter"):
            continue
        proposals.append({
            "parameter": str(s["parameter"]),
            "current_value": s.get("current_value"),
            "suggested_value": s.get("suggested_value"),
            "reasoning": str(s.get("reasoning") or ""),
            "expected_effect": str(s.get("expected_effect") or ""),
            "overfit_risk": str(s.get("overfit_risk") or "UNKNOWN"),
            "confidence": s.get("confidence"),
            "promotion_path": str(s.get("promotion_path") or ""),
            "status": "PROPOSED",
        })
    return {
        "schema_version": "1.0",
        "period": period,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "n_proposals": len(proposals),
        "proposals": proposals,
        "source_report": source_report_path,
    }


# ---------------------------------------------------------------------------
# Managed proposals writer (Patch 6)
# ---------------------------------------------------------------------------


def _write_managed_proposals(
    report: dict,
    source_period: dict,
    source_report_path: str,
    proposals_out: Path,
    since: str,
    until: str,
) -> None:
    """
    Normalize reflection report into managed proposals store and write to disk.

    Also writes a period-scoped archive snapshot alongside the managed file:
        <proposals_out.parent>/archive/<since>_<until>_weekly_reflection_proposals.json

    Guard: if the existing managed file already contains reviewed (non-PENDING)
    proposals, logs a warning and skips the write to avoid overwriting human
    decisions.  Use a new --proposals-out path or a new period to bypass.
    """
    from forecast_arb.ops.proposals import normalize_proposals, load_proposals, save_proposals

    new_proposals = normalize_proposals(
        reflection_report=report,
        source_period=source_period,
        source_report_path=source_report_path,
    )

    container = load_proposals(proposals_out)
    already_reviewed = [
        p for p in container.get("proposals", [])
        if p.get("status") != "PENDING"
    ]
    if already_reviewed:
        logger.warning(
            "proposals-out %s already contains %d reviewed proposal(s); "
            "skipping write to avoid overwriting human decisions. "
            "Use a different --proposals-out path or clear reviewed entries first.",
            proposals_out, len(already_reviewed),
        )
        return

    container["proposals"] = new_proposals
    save_proposals(proposals_out, container)
    n = len(new_proposals)
    logger.info("Wrote managed proposals (n=%d) → %s", n, proposals_out)

    # Period-scoped archive snapshot
    archive_dir = proposals_out.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{since}_{until}_weekly_reflection_proposals.json"
    save_proposals(archive_path, {
        "schema_version": container["schema_version"],
        "ts_created": container["ts_created"],
        "ts_updated": container["ts_updated"],
        "proposals": new_proposals,
    })
    logger.info("Wrote archive snapshot → %s", archive_path)


# ---------------------------------------------------------------------------
# Core callable
# ---------------------------------------------------------------------------


def run_agent_weekly_reflection(
    runs_root: Path,
    since: str,
    until: str,
    config_paths: Optional[List[Path]] = None,
    trade_outcomes_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    reflect: bool = False,
    proposals_out: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Run the weekly reflection pipeline.

    Args:
        runs_root:            Base run directory.
        since:                Start date "YYYY-MM-DD" (inclusive).
        until:                End date "YYYY-MM-DD" (inclusive).
        config_paths:         YAML config files for active parameter context.
                              None → active_parameters will be empty; warns.
        trade_outcomes_path:  trade_outcomes.jsonl path.
                              None → defaults to runs_root / "trade_outcomes.jsonl".
        out_dir:              Output directory for artifacts.
                              None → <runs_root>/weekly/<since>_<until>/.
        reflect:              If True, call OpenAI for reflection analysis.

    Returns:
        {"packet": dict, "report": dict | None, "proposals": dict | None}
    """
    runs_root = Path(runs_root)

    if out_dir is None:
        out_dir = runs_root / "weekly" / f"{since}_{until}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not config_paths:
        logger.warning(
            "No --config provided. active_parameters will be empty. "
            "Parameter suggestions will be suppressed."
        )

    # ------------------------------------------------------------------
    # Step 1: Build reflection packet
    # ------------------------------------------------------------------
    logger.info(f"Building reflection packet for {since} → {until}...")
    packet = build_reflection_packet(
        runs_root=runs_root,
        since=since,
        until=until,
        config_paths=config_paths,
        trade_outcomes_path=trade_outcomes_path,
    )
    logger.info(
        f"Packet built: {packet['runs_scanned']} runs scanned, "
        f"{packet['runs_skipped_no_timestamp']} skipped"
    )

    # ------------------------------------------------------------------
    # Step 2: Write packet
    # ------------------------------------------------------------------
    packet_path = out_dir / "weekly_reflection_packet.json"
    with open(packet_path, "w", encoding="utf-8") as fh:
        json.dump(packet, fh, indent=2, default=str)
    logger.info(f"Wrote weekly_reflection_packet.json → {packet_path}")

    report: Optional[dict] = None
    proposals: Optional[dict] = None

    # ------------------------------------------------------------------
    # Steps 3–4: Optional reflection
    # ------------------------------------------------------------------
    if reflect:
        logger.info("Running weekly reflection (OpenAI)...")
        report = run_weekly_reflection(packet)
        logger.info(f"Reflection status: {report['status']}")

        report_path = out_dir / "weekly_reflection_report.json"
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        logger.info(f"Wrote weekly_reflection_report.json → {report_path}")

        md_text = _render_reflection_md(report)
        md_path = out_dir / "weekly_reflection_report.md"
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md_text)
        logger.info(f"Wrote weekly_reflection_report.md → {md_path}")

        suggestions = report.get("parameter_suggestions") or []
        proposals = _normalize_proposals(
            suggestions=suggestions,
            period=packet.get("period", {}),
            source_report_path=str(report_path.resolve()),
        )
        proposals_path = out_dir / "weekly_parameter_proposals.json"
        with open(proposals_path, "w", encoding="utf-8") as fh:
            json.dump(proposals, fh, indent=2, default=str)
        logger.info(f"Wrote weekly_parameter_proposals.json → {proposals_path}")

        # Optional: write managed proposals store + period-scoped archive
        if proposals_out is not None:
            _write_managed_proposals(
                report=report,
                source_period=packet.get("period", {}),
                source_report_path=str(report_path.resolve()),
                proposals_out=Path(proposals_out),
                since=since,
                until=until,
            )

    # ------------------------------------------------------------------
    # Compact stdout summary
    # ------------------------------------------------------------------
    trade_s = packet.get("trade_summary", {})
    rej_s = packet.get("rejection_summary", {})
    qa = packet.get("quote_activity", {})

    print(f"[weekly_reflection] period={since} → {until}")
    print(f"[weekly_reflection] runs_scanned={packet['runs_scanned']}  "
          f"skipped={packet['runs_skipped_no_timestamp']}")
    print(f"[weekly_reflection] trade={trade_s.get('runs_with_trade', 0)}  "
          f"no_trade={rej_s.get('total_no_trade', 0)}  "
          f"submit_executed={trade_s.get('submit_executed_count', 0)}")
    print(f"[weekly_reflection] QUOTE_OK={qa.get('QUOTE_OK', 0)}  "
          f"QUOTE_BLOCKED={qa.get('QUOTE_BLOCKED', 0)}  "
          f"STAGED_PAPER={qa.get('STAGED_PAPER', 0)}")
    print(f"[weekly_reflection] packet → {packet_path}")

    if report is not None:
        ev_str = report.get("summary", {}).get("evidence_strength", "?")
        assess = report.get("summary", {}).get("overall_assessment", "?")
        n_proposals = proposals.get("n_proposals", 0) if proposals else 0
        print(f"[weekly_reflection] reflect=OK  assessment={assess}  evidence={ev_str}  "
              f"proposals={n_proposals}")
        if report.get("error"):
            print(f"[weekly_reflection] reflect_error={report['error']}")
    else:
        print("[weekly_reflection] reflect=SKIPPED (use --reflect to enable)")

    return {"packet": packet, "report": report, "proposals": proposals}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Weekly reflection — builds data packet and optionally calls OpenAI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--since", required=True,
                   help="Start date inclusive (YYYY-MM-DD).")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p.add_argument("--until", default=today_str,
                   help="End date inclusive (YYYY-MM-DD). Default: today.")
    p.add_argument("--runs-root", type=Path, default=Path("runs"),
                   help="Base run directory.")
    p.add_argument("--config", dest="configs", type=Path, action="append", default=None,
                   metavar="PATH",
                   help="Config YAML for active parameters (repeatable). No default.")
    p.add_argument("--trade-outcomes", type=Path, default=None,
                   help="Path to trade_outcomes.jsonl. Defaults to <runs-root>/trade_outcomes.jsonl.")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="Output directory. Defaults to <runs-root>/weekly/<since>_<until>/.")
    p.add_argument("--reflect", action="store_true", default=False,
                   help="Call OpenAI for reflection analysis. Requires OPENAI_API_KEY.")
    p.add_argument(
        "--proposals-out", type=Path, default=None,
        metavar="PATH",
        help=(
            "If set (with --reflect), normalize all proposals (parameter + strategy) "
            "from the reflection report into a managed proposals store at this path. "
            "A period-scoped archive snapshot is also written to "
            "<proposals-out-dir>/archive/<since>_<until>_weekly_reflection_proposals.json. "
            "Will not overwrite if the file already contains reviewed proposals. "
            "Suggested path: runs/proposals/weekly_reflection_proposals.json"
        ),
    )
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_agent_weekly_reflection(
        runs_root=args.runs_root,
        since=args.since,
        until=args.until,
        config_paths=args.configs,
        trade_outcomes_path=args.trade_outcomes,
        out_dir=args.out_dir,
        reflect=args.reflect,
        proposals_out=args.proposals_out,
    )


if __name__ == "__main__":
    main()
