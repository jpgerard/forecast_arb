"""
forecast_arb.adapter.trading_adapter
=====================================
Thin adapter around the CCC workflow for the JP Life Command Center.

V1 — read-mostly, no execution authority.

NON-NEGOTIABLE INVARIANTS
--------------------------
* CCC remains the sole authority for: candidate generation, allocator decisions,
  premium-at-risk gating, hard-cap / soft-target logic, execution staging /
  live confirmation.
* No live execution path in this module.
* No new trading logic introduced here.
* No external dependencies.
* Subprocess / shell-out only for scripts that run the full workflow;
  direct import only for pure read-only helpers in ccc_report.py.

Public API (v1)
--------------
    AdapterResult           — output contract dataclass
    TradingAdapter          — main class
        .status_snapshot()      → Task A
        .preview_daily_cycle()  → Task B
        .report_snapshot()      → Task C
        .summarize_latest()     → Task D

Actionability states
--------------------
    NO_ACTION               — no open candidates; hold only
    REVIEW_ONLY             — report available but no daily preview run
    CANDIDATE_AVAILABLE     — OPEN exists but no quote-only validation
    PAPER_ACTION_AVAILABLE  — OPEN exists AND quote-only validated
    ERROR                   — one or more fatal errors
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Project root wiring
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent.parent  # forecast_arb/adapter/.. -> project root

# Subprocess env with PYTHONPATH set so sub-scripts can import forecast_arb,
# and PYTHONIOENCODING=utf-8 so box-drawing characters in ccc_report.py /
# daily.py output don't crash on Windows cp1252 consoles.
_SUBPROCESS_ENV = os.environ.copy()
_SUBPROCESS_ENV["PYTHONPATH"] = (
    str(_PROJECT_ROOT)
    + os.pathsep
    + _SUBPROCESS_ENV.get("PYTHONPATH", "")
)
_SUBPROCESS_ENV["PYTHONIOENCODING"] = "utf-8"

# ---------------------------------------------------------------------------
# Parsers (thin local module — no business logic)
# ---------------------------------------------------------------------------
from .parsers import (
    parse_preview_output,
    parse_report_output,
    build_status_headline,
    build_preview_headline,
    build_summarize_headline,
)

# Broker drift module (v2.2) — imported lazily to avoid hard dependency
def _run_broker_drift_check(
    positions_path: Optional[Path],
    broker_csv_path: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """
    Run broker-state drift check if broker_csv_path is provided.

    Returns the drift result dict, or None if csv path is None / not provided.
    Never raises — returns an error result on failure.
    """
    if broker_csv_path is None:
        return None
    try:
        from forecast_arb.allocator.broker_drift import check_broker_drift
        pos_path = positions_path if positions_path else _DEFAULT_POSITIONS
        result = check_broker_drift(
            positions_path=str(pos_path),
            csv_path=str(broker_csv_path),
        )
        return result
    except Exception as exc:
        return {
            "ok": False,
            "in_sync": False,
            "ccc_count": 0,
            "ibkr_count": 0,
            "only_in_ccc": [],
            "only_in_ibkr": [],
            "qty_mismatches": [],
            "headline": f"Broker drift check raised an exception: {exc}",
            "errors": [str(exc), traceback.format_exc()],
        }

# ---------------------------------------------------------------------------
# Supported actionability states
# ---------------------------------------------------------------------------
_ACTIONABILITY_STATES = frozenset(
    {
        "NO_ACTION",
        "REVIEW_ONLY",
        "CANDIDATE_AVAILABLE",
        "PAPER_ACTION_AVAILABLE",
        "ERROR",
    }
)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

@dataclass
class AdapterResult:
    """
    Stable output contract for every top-level TradingAdapter method.

    Fields
    ------
    ok           : True if the operation completed without fatal errors.
    actionability: One of NO_ACTION | REVIEW_ONLY | CANDIDATE_AVAILABLE
                   | PAPER_ACTION_AVAILABLE | ERROR
    headline     : One-line human-readable summary for console / agent use.
    details      : Parsed structured data (method-specific schema).
    raw_output   : Captured stdout (or None when not applicable).
    errors       : List of error/warning strings; empty on clean run.
    """

    ok: bool
    actionability: str
    headline: str
    details: Dict[str, Any] = field(default_factory=dict)
    raw_output: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (JSON-serializable)."""
        return {
            "ok": self.ok,
            "actionability": self.actionability,
            "headline": self.headline,
            "details": self.details,
            "raw_output": self.raw_output,
            "errors": self.errors,
        }

    @staticmethod
    def error_result(errors: List[str], *, raw_output: Optional[str] = None) -> "AdapterResult":
        """Convenience constructor for error cases."""
        return AdapterResult(
            ok=False,
            actionability="ERROR",
            headline="Adapter error — check errors list.",
            details={},
            raw_output=raw_output,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Default path constants (mirrored from ccc_report.py to stay in sync)
# ---------------------------------------------------------------------------
_DEFAULT_POSITIONS     = _PROJECT_ROOT / "runs/allocator/positions.json"
_DEFAULT_COMMIT_LEDGER = _PROJECT_ROOT / "runs/allocator/allocator_commit_ledger.jsonl"
_DEFAULT_FILLS_LEDGER  = _PROJECT_ROOT / "runs/allocator/allocator_fills_ledger.jsonl"
_DEFAULT_ACTIONS       = _PROJECT_ROOT / "runs/allocator/allocator_actions.json"
_DEFAULT_POLICY        = _PROJECT_ROOT / "configs/allocator_ccc_v1.yaml"
_DEFAULT_CAMPAIGN      = _PROJECT_ROOT / "configs/campaign_v1.yaml"
_SCRIPT_DAILY          = _PROJECT_ROOT / "scripts/daily.py"
_SCRIPT_REPORT         = _PROJECT_ROOT / "scripts/ccc_report.py"


# ---------------------------------------------------------------------------
# TradingAdapter
# ---------------------------------------------------------------------------

class TradingAdapter:
    """
    Thin adapter that wraps the CCC workflow for agent/Command Center use.

    Parameters
    ----------
    policy_path   : Path to allocator_ccc_v1.yaml
    campaign_path : Path to campaign_v1.yaml
    timeout_secs  : Subprocess timeout in seconds (default 120)
    """

    def __init__(
        self,
        policy_path: Optional[Path] = None,
        campaign_path: Optional[Path] = None,
        timeout_secs: int = 120,
    ) -> None:
        self.policy_path   = Path(policy_path)  if policy_path   else _DEFAULT_POLICY
        self.campaign_path = Path(campaign_path) if campaign_path else _DEFAULT_CAMPAIGN
        self.timeout_secs  = timeout_secs

    # ------------------------------------------------------------------
    # Task A — status_snapshot()
    # ------------------------------------------------------------------

    def status_snapshot(
        self,
        *,
        positions_path:     Optional[Path] = None,
        commit_ledger_path: Optional[Path] = None,
        fills_ledger_path:  Optional[Path] = None,
        actions_path:       Optional[Path] = None,
        broker_csv_path:    Optional[Path] = None,
    ) -> AdapterResult:
        """
        Return current sleeve state without running a new daily cycle.

        Uses the same read-only data loaders as scripts/ccc_report.py
        (imported directly — these are pure file-read helpers with no
        trading logic).

        Parameters
        ----------
        broker_csv_path : Optional[Path]
            If provided, run broker-state drift check (CCC v2.2).
            Includes drift result in details["broker_drift"].
            If drift is detected, actionability degrades to REVIEW_ONLY
            and the headline warns about stale state.

        Returns
        -------
        AdapterResult with details:
            crash_open, selloff_open, total_open,
            pending_crash, pending_selloff, pending_total,
            par_crash, par_selloff, par_total,
            par_crash_cap, par_selloff_cap, par_total_cap,
            crash_soft_target, crash_hard_cap,
            selloff_soft_target, selloff_hard_cap,
            ytd_spent, annual_budget, annual_remaining,
            latest_plan_ts, latest_plan_opens, latest_plan_closes,
            latest_plan_holds, latest_plan_gate_reason,
            broker_drift (if broker_csv_path provided):
              {ok, in_sync, ccc_count, ibkr_count, only_in_ccc,
               only_in_ibkr, qty_mismatches, headline, errors}
        """
        positions_path     = Path(positions_path)     if positions_path     else _DEFAULT_POSITIONS
        commit_ledger_path = Path(commit_ledger_path) if commit_ledger_path else _DEFAULT_COMMIT_LEDGER
        fills_ledger_path  = Path(fills_ledger_path)  if fills_ledger_path  else _DEFAULT_FILLS_LEDGER
        actions_path       = Path(actions_path)        if actions_path        else _DEFAULT_ACTIONS

        errors: List[str] = []

        try:
            # Import pure read-only helpers from ccc_report.py (safe — no trading logic)
            _scripts_dir = str(_PROJECT_ROOT / "scripts")
            if _scripts_dir not in sys.path:
                sys.path.insert(0, _scripts_dir)

            # Use importlib to avoid name-collision with any 'ccc_report' already on path
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "ccc_report_helpers",
                str(_PROJECT_ROOT / "scripts/ccc_report.py"),
            )
            _rpt = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
            _spec.loader.exec_module(_rpt)        # type: ignore[union-attr]

            positions    = _rpt.load_positions(positions_path)
            pending      = _rpt.compute_pending_count(commit_ledger_path, fills_ledger_path)
            ytd_spent    = _rpt.compute_ytd_spent(commit_ledger_path)
            annual_bud   = _rpt.load_annual_budget(self.policy_path)
            par_caps     = _rpt.load_premium_at_risk_caps(self.policy_path)
            inv_tc       = _rpt.load_inventory_targets_and_caps(self.policy_path)
            portfolio_par = _rpt._compute_par(positions)
            actions_data = _rpt.load_actions(actions_path)

        except Exception as exc:
            errors.append(f"status_snapshot data load failed: {exc}")
            errors.append(traceback.format_exc())
            return AdapterResult.error_result(errors)

        # --- Build details dict ---
        crash_open   = sum(1 for p in positions if str(p.get("regime","")).lower() == "crash")
        selloff_open = sum(1 for p in positions if str(p.get("regime","")).lower() == "selloff")

        par_crash   = round(portfolio_par.get("crash", 0.0),   2)
        par_selloff = round(portfolio_par.get("selloff", 0.0), 2)
        par_total   = round(portfolio_par.get("total", 0.0),   2)

        soft_tgts   = inv_tc.get("soft_targets", {}) if inv_tc else {}
        hard_caps_m = inv_tc.get("hard_caps", {})    if inv_tc else {}

        annual_remaining: Optional[float] = None
        if annual_bud.get("enabled") and annual_bud.get("budget") is not None:
            annual_remaining = max(0.0, float(annual_bud["budget"]) - ytd_spent)

        # Latest plan fields (from actions_data; gracefully absent)
        latest_plan_ts     = None
        latest_plan_opens  = 0
        latest_plan_closes = 0
        latest_plan_holds  = 0
        latest_gate_reason = None
        if actions_data:
            latest_plan_ts = actions_data.get("timestamp_utc")
            acts = actions_data.get("actions", [])
            latest_plan_opens  = sum(1 for a in acts if a.get("type") == "OPEN")
            latest_plan_closes = sum(
                1 for a in acts
                if a.get("type") in ("HARVEST_CLOSE", "ROLL_CLOSE")
            )
            latest_plan_holds  = sum(1 for a in acts if a.get("type") == "HOLD")
            trace = actions_data.get("open_gate_trace")
            if trace:
                latest_gate_reason = trace.get("reason")

        details: Dict[str, Any] = {
            # Positions
            "crash_open":    crash_open,
            "selloff_open":  selloff_open,
            "total_open":    crash_open + selloff_open,
            # Pending
            "pending_crash":   pending.get("crash", 0),
            "pending_selloff": pending.get("selloff", 0),
            "pending_total":   pending.get("total", 0),
            # Premium at risk
            "par_crash":   par_crash,
            "par_selloff": par_selloff,
            "par_total":   par_total,
            # Caps
            "par_crash_cap":   par_caps.get("crash")   if par_caps.get("enabled") else None,
            "par_selloff_cap": par_caps.get("selloff") if par_caps.get("enabled") else None,
            "par_total_cap":   par_caps.get("total")   if par_caps.get("enabled") else None,
            # Inventory soft targets / hard caps
            "crash_soft_target":   soft_tgts.get("crash"),
            "crash_hard_cap":      hard_caps_m.get("crash"),
            "selloff_soft_target": soft_tgts.get("selloff"),
            "selloff_hard_cap":    hard_caps_m.get("selloff"),
            # Budget
            "ytd_spent":       round(ytd_spent, 2),
            "annual_budget":   annual_bud.get("budget"),
            "annual_remaining": annual_remaining,
            # Latest plan
            "latest_plan_ts":          latest_plan_ts,
            "latest_plan_opens":       latest_plan_opens,
            "latest_plan_closes":      latest_plan_closes,
            "latest_plan_holds":       latest_plan_holds,
            "latest_plan_gate_reason": latest_gate_reason,
        }

        headline = build_status_headline(
            crash_open=crash_open,
            selloff_open=selloff_open,
            par_crash=par_crash if par_crash > 0 else None,
            par_selloff=par_selloff if par_selloff > 0 else None,
            par_total=par_total if par_total > 0 else None,
            pending_total=pending.get("total", 0),
        )

        # Actionability: if there are open positions or pending items → REVIEW_ONLY
        actionability = "REVIEW_ONLY" if (crash_open + selloff_open + pending.get("total", 0)) > 0 else "NO_ACTION"

        # --- CCC v2.2: Broker-state drift check (optional) ---
        broker_csv_resolved = Path(broker_csv_path) if broker_csv_path else None
        drift_result = _run_broker_drift_check(positions_path, broker_csv_resolved)
        if drift_result is not None:
            details["broker_drift"] = drift_result
            details["in_sync"]      = drift_result.get("in_sync", True)
            details["only_in_ccc"]  = drift_result.get("only_in_ccc", [])
            details["only_in_ibkr"] = drift_result.get("only_in_ibkr", [])
            details["qty_mismatches"] = drift_result.get("qty_mismatches", [])

            if not drift_result.get("in_sync", True):
                # Drift detected — degrade actionability to REVIEW_ONLY (minimum)
                if actionability == "NO_ACTION":
                    actionability = "REVIEW_ONLY"
                # Prefix headline with drift warning
                ccc_cnt  = drift_result.get("ccc_count", crash_open + selloff_open)
                ibkr_cnt = drift_result.get("ibkr_count", 0)
                drift_warn = (
                    f"Broker drift detected: CCC shows {ccc_cnt} crash spread(s) "
                    f"but broker export shows {ibkr_cnt}. "
                    f"Refresh sync before trusting summary."
                )
                headline = drift_warn + " | " + headline
                errors.append(f"broker_drift: {drift_result.get('headline', 'drift detected')}")

        return AdapterResult(
            ok=True,
            actionability=actionability,
            headline=headline,
            details=details,
            raw_output=None,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Task B — preview_daily_cycle()
    # ------------------------------------------------------------------

    def preview_daily_cycle(
        self,
        *,
        campaign_path: Optional[Path] = None,
        policy_path:   Optional[Path] = None,
    ) -> AdapterResult:
        """
        Run the exact safe daily preview workflow via subprocess.

        Command:
            python scripts/daily.py
                --campaign configs/campaign_v1.yaml
                --policy   configs/allocator_ccc_v1.yaml
                --execute --paper --quote-only

        No files are committed.  Captures stdout/stderr.

        Returns
        -------
        AdapterResult with details from parse_preview_output().
        Actionability:
            NO_ACTION              — planned_opens == 0
            PAPER_ACTION_AVAILABLE — planned_opens > 0 AND quote_only_validated > 0
            CANDIDATE_AVAILABLE    — planned_opens > 0 AND quote_only_validated == 0
            ERROR                  — non-zero returncode or exception
        """
        campaign  = Path(campaign_path) if campaign_path else self.campaign_path
        policy    = Path(policy_path)   if policy_path   else self.policy_path
        errors: List[str] = []

        # Validate required files exist before launching subprocess
        missing: List[str] = []
        for label, p in [("campaign", campaign), ("policy", policy), ("daily.py", _SCRIPT_DAILY)]:
            if not p.exists():
                missing.append(f"{label}: {p}")
        if missing:
            return AdapterResult.error_result(
                [f"Required file not found: {m}" for m in missing]
            )

        cmd = [
            sys.executable,
            str(_SCRIPT_DAILY),
            "--campaign", str(campaign),
            "--policy",   str(policy),
            "--execute",
            "--paper",
            "--quote-only",
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(_PROJECT_ROOT),
                env=_SUBPROCESS_ENV,
                timeout=self.timeout_secs,
            )
        except subprocess.TimeoutExpired as te:
            return AdapterResult.error_result(
                [f"preview_daily_cycle timed out after {self.timeout_secs}s: {te}"]
            )
        except Exception as exc:
            return AdapterResult.error_result(
                [f"preview_daily_cycle subprocess error: {exc}",
                 traceback.format_exc()]
            )

        raw_output = proc.stdout or ""
        combined   = (proc.stdout or "") + "\n" + (proc.stderr or "")

        if proc.returncode != 0:
            # Surface stderr as errors but still attempt to parse whatever we got
            errors.append(
                f"daily.py exited with code {proc.returncode}"
            )
            if proc.stderr.strip():
                errors.append(f"stderr: {proc.stderr.strip()[:2000]}")
            parsed = parse_preview_output(proc.stdout, proc.stderr)
            return AdapterResult(
                ok=False,
                actionability="ERROR",
                headline="Daily preview failed — check errors.",
                details=parsed,
                raw_output=raw_output,
                errors=errors,
            )

        parsed = parse_preview_output(proc.stdout, proc.stderr)

        opens     = parsed.get("planned_opens", 0)
        validated = parsed.get("quote_only_validated", 0)

        if opens == 0:
            actionability = "NO_ACTION"
        elif validated > 0:
            actionability = "PAPER_ACTION_AVAILABLE"
        else:
            actionability = "CANDIDATE_AVAILABLE"

        headline = build_preview_headline(parsed, actionability)

        return AdapterResult(
            ok=True,
            actionability=actionability,
            headline=headline,
            details=parsed,
            raw_output=raw_output,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Task C — report_snapshot()
    # ------------------------------------------------------------------

    def report_snapshot(
        self,
        *,
        policy_path: Optional[Path] = None,
    ) -> AdapterResult:
        """
        Run scripts/ccc_report.py --policy <policy> and return structured fields.

        Premium-at-risk logic is NOT duplicated here: the subprocess call
        ensures the authoritative ccc_report.py PAR computation is used.

        Returns
        -------
        AdapterResult with details from parse_report_output():
            crash_open, selloff_open, total_open,
            pending_*, par_crash, par_selloff, par_total,
            ytd_spent, annual_budget, annual_remaining,
            planned_opens, planned_closes, holds,
            plan_timestamp, gate_reason,
            sections_found
        """
        policy = Path(policy_path) if policy_path else self.policy_path
        errors: List[str] = []

        if not _SCRIPT_REPORT.exists():
            return AdapterResult.error_result(
                [f"ccc_report.py not found at {_SCRIPT_REPORT}"]
            )

        cmd = [
            sys.executable,
            str(_SCRIPT_REPORT),
            "--policy", str(policy),
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(_PROJECT_ROOT),
                env=_SUBPROCESS_ENV,
                timeout=self.timeout_secs,
            )
        except subprocess.TimeoutExpired as te:
            return AdapterResult.error_result(
                [f"report_snapshot timed out after {self.timeout_secs}s: {te}"]
            )
        except Exception as exc:
            return AdapterResult.error_result(
                [f"report_snapshot subprocess error: {exc}", traceback.format_exc()]
            )

        raw_output = proc.stdout or ""

        if proc.returncode != 0:
            errors.append(f"ccc_report.py exited with code {proc.returncode}")
            if (proc.stderr or "").strip():
                errors.append(f"stderr: {proc.stderr.strip()[:2000]}")
            parsed = parse_report_output(proc.stdout or "")
            return AdapterResult(
                ok=False,
                actionability="ERROR",
                headline="Report snapshot failed — check errors.",
                details=parsed,
                raw_output=raw_output,
                errors=errors,
            )

        parsed = parse_report_output(proc.stdout)

        crash_open   = parsed.get("crash_open", 0)
        selloff_open = parsed.get("selloff_open", 0)
        par_total    = parsed.get("par_total")
        par_crash    = parsed.get("par_crash")
        par_selloff  = parsed.get("par_selloff")

        headline = build_status_headline(
            crash_open=crash_open,
            selloff_open=selloff_open,
            par_crash=par_crash,
            par_selloff=par_selloff,
            par_total=par_total,
            pending_total=parsed.get("pending_total", 0),
        )

        actionability = (
            "REVIEW_ONLY"
            if (crash_open + selloff_open + parsed.get("pending_total", 0)) > 0
            else "NO_ACTION"
        )

        return AdapterResult(
            ok=True,
            actionability=actionability,
            headline=headline,
            details=parsed,
            raw_output=raw_output,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Task D — summarize_latest()
    # ------------------------------------------------------------------

    def summarize_latest(
        self,
        *,
        run_preview: bool = True,
        campaign_path: Optional[Path] = None,
        policy_path:   Optional[Path] = None,
        broker_csv_path: Optional[Path] = None,
    ) -> AdapterResult:
        """
        Combine latest status + latest report + most recent daily preview
        into one concise result for Command Center consumption.

        Parameters
        ----------
        run_preview : bool
            If True (default), runs preview_daily_cycle() as part of the
            summary.  Set False to summarize from cached artifacts only.
        broker_csv_path : Optional[Path]
            If provided, include broker-state drift check in the summary.
            Drift result appears in details["status"]["broker_drift"].
            If drift detected, headline warns about stale state.

        Returns
        -------
        AdapterResult with combined details:
            {
                "status":  <status_snapshot details>,   # includes broker_drift if csv given
                "report":  <report_snapshot details>,
                "preview": <preview_daily_cycle details>,  # {} if run_preview=False
            }

        Headline example:
            "No new trade today; sleeve open positions: crash=3;
             $122.80 PAR — under cap; gate: EV_BELOW_THRESHOLD."
        """
        errors: List[str] = []

        # --- status (with optional drift check) ---
        status_result = self.status_snapshot(
            broker_csv_path=broker_csv_path,
        )
        if not status_result.ok:
            errors.extend(status_result.errors)

        # --- report ---
        report_result = self.report_snapshot(policy_path=policy_path)
        if not report_result.ok:
            errors.extend(report_result.errors)

        # --- preview (optional) ---
        preview_result: AdapterResult
        if run_preview:
            preview_result = self.preview_daily_cycle(
                campaign_path=campaign_path,
                policy_path=policy_path,
            )
            if not preview_result.ok:
                errors.extend(preview_result.errors)
        else:
            # Synthesize a no-op preview result from the latest plan in status
            status_details = status_result.details
            preview_result = AdapterResult(
                ok=True,
                actionability="NO_ACTION",
                headline="(preview not run)",
                details={
                    "planned_opens":  status_details.get("latest_plan_opens", 0),
                    "planned_closes": status_details.get("latest_plan_closes", 0),
                    "holds":          status_details.get("latest_plan_holds", 0),
                    "gate_reason":    status_details.get("latest_plan_gate_reason"),
                    "quote_only_validated": 0,
                    "summary_box_found": False,
                },
                raw_output=None,
                errors=[],
            )

        # Resolve combined actionability
        actionability = _combine_actionability(
            status_result.actionability,
            report_result.actionability,
            preview_result.actionability,
        )

        # Build combined headline
        combined_details: Dict[str, Any] = {
            "status":  status_result.details,
            "report":  report_result.details,
            "preview": preview_result.details,
        }

        headline = build_summarize_headline(
            status=status_result.to_dict(),
            preview=preview_result.to_dict(),
            report=report_result.to_dict(),
        )

        ok = status_result.ok and report_result.ok  # preview failure is non-fatal
        if not ok:
            actionability = "ERROR"

        return AdapterResult(
            ok=ok,
            actionability=actionability,
            headline=headline,
            details=combined_details,
            raw_output=None,  # combined — raw_output per sub-result available in .details
            errors=errors,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _combine_actionability(*states: str) -> str:
    """
    Resolve the highest-priority actionability across multiple results.

    Priority (highest → lowest):
        ERROR > PAPER_ACTION_AVAILABLE > CANDIDATE_AVAILABLE
        > REVIEW_ONLY > NO_ACTION
    """
    priority = {
        "ERROR":                  4,
        "PAPER_ACTION_AVAILABLE": 3,
        "CANDIDATE_AVAILABLE":    2,
        "REVIEW_ONLY":            1,
        "NO_ACTION":              0,
    }
    best = "NO_ACTION"
    for state in states:
        if priority.get(state, 0) > priority.get(best, 0):
            best = state
    return best
