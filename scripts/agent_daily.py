"""
scripts/agent_daily.py
======================
Non-interactive daily workflow wrapper for the forecast_arb operator pipeline.

Orchestrates:
1. Broker preflight check (IBKR CSV vs CCC positions.json)
2. Deterministic daily run via run_daily_core()
3. Decision packet assembly via build_decision_packet()
4. Operator markdown summary rendering via render_operator_summary()
5. Artifact writes:
   - <run_dir>/artifacts/operator_summary.json  — decision packet
   - <run_dir>/artifacts/operator_summary.md    — rendered markdown
6. Global pointer write: <runs_root>/agent_last_run.json
7. Compact stdout summary

Optional LLM analyst layer (--analyst flag) calls OpenAI for an advisory
recommendation. Analyst output is written to a separate artifact and is
never merged into the decision packet. No autonomous action is taken.
All other actions are read/write to local filesystem only.

Returns the decision packet dict (for testability when called as a function).

Usage
-----
    python scripts/agent_daily.py [options]

Key options:
    --runs-root PATH        Base run directory (default: runs/)
    --positions PATH        CCC positions.json path
    --fills-ledger PATH     allocator_fills_ledger.jsonl path
    --ibkr-csv PATH         IBKR CSV export for drift check
    --trade-outcomes PATH   runs/trade_outcomes.jsonl for positions view
    --regime REGIME         Regime to run (auto/crash/selloff/both)
    --underlier UNDERLIER   Underlier symbol (default: SPY)
    See --help for full option list.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Path setup — consistent with daily.py and run_daily_v2.py patterns
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(_SCRIPTS_DIR))

from forecast_arb.ops.preflight import run_broker_preflight
from forecast_arb.core.decision_packet import build_decision_packet
from forecast_arb.ops.summary import render_operator_summary
from forecast_arb.ops.analyst import run_analyst

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core callable
# ---------------------------------------------------------------------------


def run_agent_daily(
    runs_root: Path,
    positions_path: Optional[Path] = None,
    fills_ledger_path: Optional[Path] = None,
    ibkr_csv_path: Optional[Path] = None,
    trade_outcomes_path: Optional[Path] = None,
    # run_daily_core kwargs
    regime: str = "auto",
    underlier: str = "SPY",
    dte_min: int = 30,
    dte_max: int = 60,
    p_event_source: str = "kalshi-auto",
    fallback_p: float = 0.30,
    campaign_config: str = "configs/structuring_crash_venture_v2.yaml",
    min_debit_per_contract: float = 10.0,
    snapshot_path: Optional[str] = None,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7496,
    # optional analyst layer
    analyst: bool = False,
) -> Dict[str, Any]:
    """
    Run the full agent daily pipeline (non-interactive, no autonomous orders).

    Args:
        runs_root:            Base directory for run output (absolute path recommended).
        positions_path:       CCC positions.json for preflight inventory/drift.
        fills_ledger_path:    allocator_fills_ledger.jsonl for pending counts.
        ibkr_csv_path:        IBKR CSV export for broker drift comparison.
        trade_outcomes_path:  trade_outcomes.jsonl for positions view.
        regime:               Regime selector passed to run_daily_core().
        underlier:            Underlier symbol.
        dte_min/dte_max:      DTE range for expiry selection.
        p_event_source:       Probability source ("kalshi-auto" or "fallback").
        fallback_p:           Fallback p_event probability.
        campaign_config:      Path to campaign YAML config.
        min_debit_per_contract: Minimum debit filter.
        snapshot_path:        Optional path to existing IBKR snapshot JSON.
        ibkr_host/ibkr_port:  IBKR gateway connection params.
        analyst:              If True, call run_analyst() after building packet.
                              Result written to artifacts/analyst_recommendation.json.
                              Analyst failure is non-fatal.

    Returns:
        Decision packet dict (schema_version "2.0").
        If analyst=True, packet["_analyst"] holds the analyst result dict
        (underscore-prefixed; not written to operator_summary.json).
    """
    from run_daily_v2 import run_daily_core

    runs_root = Path(runs_root)

    # ------------------------------------------------------------------
    # Step 1: Broker preflight
    # ------------------------------------------------------------------
    logger.info("Running broker preflight...")
    preflight = run_broker_preflight(
        positions_path=positions_path or Path("data/positions.json"),
        fills_ledger_path=fills_ledger_path,
        ibkr_csv_path=ibkr_csv_path,
        trade_outcomes_path=trade_outcomes_path,
    )
    pf_status = preflight["status"]
    logger.info(f"Preflight status: {pf_status} — {preflight.get('reason', '')}")

    # ------------------------------------------------------------------
    # Step 2: Deterministic daily run
    # ------------------------------------------------------------------
    logger.info("Starting daily run...")
    try:
        run_result = run_daily_core(
            regime=regime,
            underlier=underlier,
            dte_min=dte_min,
            dte_max=dte_max,
            p_event_source=p_event_source,
            fallback_p=fallback_p,
            campaign_config=campaign_config,
            min_debit_per_contract=min_debit_per_contract,
            snapshot_path=snapshot_path,
            ibkr_host=ibkr_host,
            ibkr_port=ibkr_port,
            runs_root=runs_root,
        )
    except RuntimeError as exc:
        logger.error(f"run_daily_core failed: {exc}")
        raise

    run_dir: Path = run_result["run_dir"]
    run_id: str = run_result["run_id"]
    logger.info(f"Run complete: {run_id}  dir={run_dir}")

    # ------------------------------------------------------------------
    # Step 3: Build decision packet
    # ------------------------------------------------------------------
    packet = build_decision_packet(
        run_dir=run_dir,
        preflight=preflight,
        max_candidates_per_regime=3,
    )

    # ------------------------------------------------------------------
    # Step 4: Render markdown summary
    # ------------------------------------------------------------------
    md_text = render_operator_summary(packet, run_dir=run_dir)

    # ------------------------------------------------------------------
    # Step 5: Write artifacts into run_dir
    # ------------------------------------------------------------------
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    summary_json_path = artifacts_dir / "operator_summary.json"
    summary_md_path = artifacts_dir / "operator_summary.md"

    with open(summary_json_path, "w", encoding="utf-8") as fh:
        json.dump(packet, fh, indent=2)
    logger.info(f"Wrote operator_summary.json → {summary_json_path}")

    with open(summary_md_path, "w", encoding="utf-8") as fh:
        fh.write(md_text)
    logger.info(f"Wrote operator_summary.md → {summary_md_path}")

    # ------------------------------------------------------------------
    # Step 6: Write global agent_last_run.json pointer
    # ------------------------------------------------------------------
    decision = packet.get("run", {}).get("decision", "UNKNOWN")
    num_candidates = len(packet.get("top_candidates", []))

    last_run_record = {
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "ts_utc": packet.get("ts_utc") or datetime.now(timezone.utc).isoformat(),
        "preflight_status": pf_status,
        "decision": decision,
        "num_candidates": num_candidates,
    }
    last_run_path = runs_root / "agent_last_run.json"
    with open(last_run_path, "w", encoding="utf-8") as fh:
        json.dump(last_run_record, fh, indent=2)
    logger.info(f"Wrote agent_last_run.json → {last_run_path}")

    # ------------------------------------------------------------------
    # Step 7 (optional): Analyst layer
    # ------------------------------------------------------------------
    if analyst:
        logger.info("Running analyst...")
        analyst_result = run_analyst(packet, md_text)
        analyst_path = artifacts_dir / "analyst_recommendation.json"
        with open(analyst_path, "w", encoding="utf-8") as fh:
            json.dump(analyst_result, fh, indent=2)
        logger.info(f"Wrote analyst_recommendation.json → {analyst_path}")
        # Attach to in-memory packet for testability (not written to operator_summary.json)
        packet["_analyst"] = analyst_result
    else:
        analyst_result = None

    # ------------------------------------------------------------------
    # Step 8: Compact stdout summary
    # ------------------------------------------------------------------
    notes_str = ", ".join(packet.get("notes", [])) or "none"
    print(f"[agent_daily] run_id={run_id}")
    print(f"[agent_daily] preflight={pf_status}")
    print(f"[agent_daily] decision={decision}  candidates={num_candidates}")
    print(f"[agent_daily] notes={notes_str}")
    print(f"[agent_daily] summary → {summary_md_path}")
    if analyst_result is not None:
        rec = analyst_result.get("recommendation") or analyst_result.get("status", "ERROR").upper()
        print(f"[agent_daily] analyst={rec}")

    return packet


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Non-interactive daily workflow agent (no LLM, no orders).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--runs-root", type=Path, default=Path("runs"),
                   help="Base directory for run output.")
    p.add_argument("--positions", type=Path, default=None,
                   help="Path to CCC positions.json.")
    p.add_argument("--fills-ledger", type=Path, default=None,
                   help="Path to allocator_fills_ledger.jsonl.")
    p.add_argument("--ibkr-csv", type=Path, default=None,
                   help="Path to IBKR CSV export for drift check.")
    p.add_argument("--trade-outcomes", type=Path, default=None,
                   help="Path to trade_outcomes.jsonl for positions view.")
    # run_daily_core arguments
    p.add_argument("--regime", default="auto",
                   choices=["auto", "crash", "selloff", "both"],
                   help="Regime selector.")
    p.add_argument("--underlier", default="SPY",
                   help="Underlier symbol.")
    p.add_argument("--dte-min", type=int, default=30,
                   help="Minimum DTE for expiry selection.")
    p.add_argument("--dte-max", type=int, default=60,
                   help="Maximum DTE for expiry selection.")
    p.add_argument("--p-event-source", default="kalshi-auto",
                   choices=["kalshi-auto", "fallback"],
                   help="Probability source.")
    p.add_argument("--fallback-p", type=float, default=0.30,
                   help="Fallback p_event probability.")
    p.add_argument("--campaign-config", default="configs/structuring_crash_venture_v2.yaml",
                   help="Path to campaign YAML config.")
    p.add_argument("--min-debit", type=float, default=10.0,
                   help="Minimum debit per contract filter.")
    p.add_argument("--snapshot", type=str, default=None,
                   help="Optional path to existing IBKR snapshot JSON.")
    p.add_argument("--ibkr-host", default="127.0.0.1",
                   help="IBKR gateway host.")
    p.add_argument("--ibkr-port", type=int, default=7496,
                   help="IBKR gateway port.")
    p.add_argument("--analyst", action="store_true", default=False,
                   help="Call OpenAI analyst after building the decision packet. "
                        "Requires OPENAI_API_KEY env var. Advisory only.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging level.")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        run_agent_daily(
            runs_root=args.runs_root,
            positions_path=args.positions,
            fills_ledger_path=args.fills_ledger,
            ibkr_csv_path=args.ibkr_csv,
            trade_outcomes_path=args.trade_outcomes,
            regime=args.regime,
            underlier=args.underlier,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            p_event_source=args.p_event_source,
            fallback_p=args.fallback_p,
            campaign_config=args.campaign_config,
            min_debit_per_contract=args.min_debit,
            snapshot_path=args.snapshot,
            ibkr_host=args.ibkr_host,
            ibkr_port=args.ibkr_port,
            analyst=args.analyst,
        )
    except RuntimeError as exc:
        logger.error(f"Agent daily run failed: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
