"""
forecast_arb.ops.evaluation
=============================
Counterfactual parameter overlay evaluation.

Applies new parameter thresholds from an overlay config to signals already
captured in existing run artifacts. Does NOT re-execute the system, does NOT
re-fetch market data, does NOT call any external API.

COUNTERFACTUAL DISCLAIMER
--------------------------
All results carry ``"simulated_only": True``.  The gate simulation is a
simplified threshold check against signals read from ``gate_decision.json``,
``p_event_implied.json``, ``p_event_external.json``, and ``tickets.json``.
It is NOT the full gate pipeline and cannot model structural parameters
(dte_range_days, spread_widths, moneyness targets) without re-execution.

Overlay key classification
---------------------------
    fully_evaluable    — threshold against a captured scalar signal
                         (regime_selector.*, edge_gating.min_edge, min_confidence)
    partially_evaluable — evaluable only when candidates were generated
                         (min_debit_per_contract)
    requires_rerun     — structural; need re-execution
                         (structuring.*, regimes.*)
    unknown            — not recognised; logged and skipped

Public API
----------
    flatten_config(config, prefix="") -> dict
    deep_merge_configs(baseline, overlay) -> dict
    classify_overlay_keys(overlay) -> dict
    extract_run_signals(run_dir) -> dict
    apply_threshold_gate(signals, config) -> dict
    compute_evaluation_metrics(run_signal_list, config) -> dict
    compute_comparison(baseline_metrics, overlay_metrics, overlay_classification) -> dict
    build_evaluation_report(baseline_config, overlay_config, run_dirs, period, ts_utc) -> dict
"""
from __future__ import annotations

import copy
import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

EVALUATION_SCHEMA_VERSION = "1.0"
SIMULATION_DISCLAIMER = (
    "COUNTERFACTUAL_ONLY — thresholds applied to captured artifact signals. "
    "Not a system re-execution."
)

# ---------------------------------------------------------------------------
# Overlay key classification constants
# ---------------------------------------------------------------------------

_FULLY_EVALUABLE_KEYS = frozenset({
    "regime_selector.crash_p_threshold",
    "regime_selector.selloff_p_min",
    "regime_selector.selloff_p_max",
    "edge_gating.min_edge",
    "edge_gating.min_confidence",
})

_PARTIALLY_EVALUABLE_KEYS = frozenset({
    # Evaluable only when candidates were actually generated and debit is captured
    "min_debit_per_contract",
})

# Any key starting with these prefixes requires re-execution
_REQUIRES_RERUN_PREFIXES: Tuple[str, ...] = (
    "structuring.",
    "regimes.",
)

# ---------------------------------------------------------------------------
# Assessment thresholds (named constants — adjust here, not in test assertions)
# ---------------------------------------------------------------------------

_NO_CHANGE_DELTA: float = 0.02          # |gate_delta| < 2% → NO_CHANGE
_MIN_RUNS_FOR_PROMOTE: int = 5          # need >= 5 fully-simulated runs
_MIN_COVERAGE_FOR_PROMOTE: float = 0.40 # coverage_rate < 40% → cap at KEEP_TESTING
_PROMOTE_GATE_DELTA: float = 0.10       # |gate_delta| >= 10% → eligible for PROMOTE


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def flatten_config(config: dict, prefix: str = "") -> dict:
    """
    Flatten a nested dict to dotted-key form.

    Example:
        {"structuring": {"dte_range_days": {"min": 30}}}
        → {"structuring.dte_range_days.min": 30}
    """
    result: Dict[str, Any] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten_config(value, full_key))
        else:
            result[full_key] = value
    return result


def _expand_dotted_keys(d: dict) -> dict:
    """
    Expand any dotted keys in *d* to nested-dict form. Non-dotted keys are
    kept as-is (preserving nested dicts that were already expanded).

    Handles mixed input (some dotted, some already nested).
    """
    result: Dict[str, Any] = {}
    for key, value in d.items():
        if "." in key:
            parts = key.split(".")
            node = result
            for part in parts[:-1]:
                if part not in node or not isinstance(node[part], dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
        else:
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = _deep_merge_dicts(result[key], copy.deepcopy(value))
            else:
                result[key] = value
    return result


def _deep_merge_dicts(base: dict, over: dict) -> dict:
    """Recursively merge *over* into *base*. *over* wins at every leaf."""
    for key, val in over.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            base[key] = _deep_merge_dicts(base[key], val)
        else:
            base[key] = val
    return base


def deep_merge_configs(baseline: dict, overlay: dict) -> dict:
    """
    Merge *overlay* onto *baseline* deterministically.

    Overlay wins at every leaf. Baseline keys absent from the overlay are
    preserved unchanged.  Neither input is mutated.

    Handles overlay keys in both forms:
        dotted flat:  ``{"structuring.dte_min": 25}``
        nested dict:  ``{"structuring": {"dte_min": 25}}``
    """
    base_copy = copy.deepcopy(baseline)
    expanded = _expand_dotted_keys(overlay)
    return _deep_merge_dicts(base_copy, expanded)


# ---------------------------------------------------------------------------
# Overlay key classification
# ---------------------------------------------------------------------------


def classify_overlay_keys(overlay: dict) -> Dict[str, List[str]]:
    """
    Classify all keys in *overlay* by evaluability.

    Returns:
        {
            "fully_evaluable":    list[str],
            "partially_evaluable": list[str],
            "requires_rerun":     list[str],
            "unknown":            list[str],
        }

    Never raises.
    """
    try:
        expanded = _expand_dotted_keys(overlay)
        flat_keys = set(flatten_config(expanded).keys())
    except Exception as exc:
        log.warning("classify_overlay_keys: error flattening overlay: %s", exc)
        return {
            "fully_evaluable": [],
            "partially_evaluable": [],
            "requires_rerun": [],
            "unknown": [],
        }

    fully: List[str] = []
    partial: List[str] = []
    rerun: List[str] = []
    unknown: List[str] = []

    for key in sorted(flat_keys):
        if key in _FULLY_EVALUABLE_KEYS:
            fully.append(key)
        elif key in _PARTIALLY_EVALUABLE_KEYS:
            partial.append(key)
        elif any(key.startswith(p) for p in _REQUIRES_RERUN_PREFIXES):
            rerun.append(key)
        else:
            unknown.append(key)
            log.debug("classify_overlay_keys: unrecognized key %r", key)

    if unknown:
        log.warning(
            "classify_overlay_keys: %d unrecognized overlay key(s): %s",
            len(unknown), unknown,
        )

    return {
        "fully_evaluable": fully,
        "partially_evaluable": partial,
        "requires_rerun": rerun,
        "unknown": unknown,
    }


# ---------------------------------------------------------------------------
# Per-run signal extraction
# ---------------------------------------------------------------------------


def _load_min_debit_from_candidates(arts: Path) -> Optional[float]:
    """Return the minimum debit_per_contract across all candidates. None if unavailable."""
    # review_candidates.json preferred
    rc = arts / "review_candidates.json"
    if rc.exists():
        try:
            with open(rc, encoding="utf-8") as fh:
                data = json.load(fh)
            debits = [
                float(c["debit_per_contract"])
                for regime_data in data.get("regimes", {}).values()
                for c in regime_data.get("candidates", [])
                if isinstance(c.get("debit_per_contract"), (int, float))
            ]
            if debits:
                return min(debits)
        except Exception:
            pass

    # Fallback: tickets.json
    t = arts / "tickets.json"
    if t.exists():
        try:
            with open(t, encoding="utf-8") as fh:
                raw = json.load(fh)
            tickets = raw if isinstance(raw, list) else raw.get("tickets", [])
            debits = [
                float(ticket["debit_per_contract"])
                for ticket in tickets
                if isinstance(ticket.get("debit_per_contract"), (int, float))
            ]
            if debits:
                return min(debits)
        except Exception:
            pass

    return None


def extract_run_signals(run_dir: Path) -> dict:
    """
    Read per-run artifacts and return a normalised signals dict.

    Uses ``extract_summary_safe()`` for basic fields, then supplements with
    candidate debit data.  Never raises.

    Returns:
        {
            run_id, run_dir, decision, reason,
            p_implied, p_external, edge, confidence,
            num_tickets,
            gate_artifact_present: bool,
            candidates_artifact_present: bool,
            min_debit_in_candidates: float | None,
        }
    """
    from forecast_arb.core.run_summary import extract_summary_safe

    run_dir = Path(run_dir)
    arts = run_dir / "artifacts"

    try:
        base = extract_summary_safe(run_dir)
    except Exception as exc:
        log.warning("extract_run_signals(%s): extract_summary_safe failed: %s", run_dir.name, exc)
        base = {}

    gate_present = (arts / "gate_decision.json").exists()
    candidates_present = (
        (arts / "review_candidates.json").exists()
        or (arts / "tickets.json").exists()
    )
    min_debit = _load_min_debit_from_candidates(arts) if candidates_present else None

    return {
        "run_id": base.get("run_id", run_dir.name),
        "run_dir": str(run_dir),
        "decision": base.get("decision", "UNKNOWN"),
        "reason": base.get("reason", ""),
        "p_implied": base.get("p_implied"),
        "p_external": base.get("p_external"),
        "edge": base.get("edge"),
        "confidence": base.get("confidence"),
        "num_tickets": base.get("num_tickets", 0),
        "gate_artifact_present": gate_present,
        "candidates_artifact_present": candidates_present,
        "min_debit_in_candidates": min_debit,
    }


# ---------------------------------------------------------------------------
# Threshold gate simulation
# ---------------------------------------------------------------------------


def apply_threshold_gate(signals: dict, config: dict) -> dict:
    """
    Simulate whether a run would pass gating under *config*.

    CONSERVATIVE APPROACH: if a signal required by an evaluable overlay key
    is absent, the outcome is ``PARTIAL_SIGNALS`` (``simulated=False``) rather
    than inferring a pass or fail.  This avoids overconfident counterfactuals.

    Gate outcome values:
        PASS                  — all applicable checks passed, simulated=True
        FAIL_EDGE             — edge < min_edge, simulated=True
        FAIL_CONFIDENCE       — confidence < min_confidence, simulated=True
        FAIL_CRASH_THRESHOLD  — p_implied > crash_p_threshold, simulated=True
        FAIL_DEBIT            — min_debit_in_candidates < min_debit_per_contract
                                (partial, only if candidates present)
        PARTIAL_SIGNALS       — some but not all required signals available,
                                simulated=False
        NO_SIGNALS            — no relevant signals captured, simulated=False

    Returns:
        {
            gate_outcome:              str,
            gate_reasons:              list[str],   hard failures
            missing_signal_checks:     list[str],   checks that could not run
            simulated:                 bool,
            signals_used:              dict,
            config_thresholds_applied: list[str],
        }
    """
    flat_cfg = flatten_config(config)

    p_implied = signals.get("p_implied")
    edge = signals.get("edge")
    confidence = signals.get("confidence")
    min_debit_in_cands = signals.get("min_debit_in_candidates")
    gate_present = signals.get("gate_artifact_present", False)
    candidates_present = signals.get("candidates_artifact_present", False)

    # Which overlay keys are in this config?
    has_crash = "regime_selector.crash_p_threshold" in flat_cfg
    has_edge = "edge_gating.min_edge" in flat_cfg
    has_conf = "edge_gating.min_confidence" in flat_cfg
    has_debit = "min_debit_per_contract" in flat_cfg

    # Signal availability
    p_implied_ok = p_implied is not None
    edge_ok = edge is not None and gate_present
    conf_ok = confidence is not None and gate_present

    # ----------------------------------------------------------------
    # Fast path: truly no signals
    # ----------------------------------------------------------------
    if not p_implied_ok and not gate_present:
        return {
            "gate_outcome": "NO_SIGNALS",
            "gate_reasons": [],
            "missing_signal_checks": ["p_implied", "gate_decision"],
            "simulated": False,
            "signals_used": {},
            "config_thresholds_applied": [],
        }

    # ----------------------------------------------------------------
    # Partial path: gate artifact absent but some evaluable keys need it
    # ----------------------------------------------------------------
    if not gate_present and (has_edge or has_conf):
        missing = []
        if has_edge:
            missing.append("edge_gating.min_edge (gate_decision.json absent)")
        if has_conf:
            missing.append("edge_gating.min_confidence (gate_decision.json absent)")
        return {
            "gate_outcome": "PARTIAL_SIGNALS",
            "gate_reasons": [],
            "missing_signal_checks": missing,
            "simulated": False,
            "signals_used": {"p_implied": p_implied} if p_implied_ok else {},
            "config_thresholds_applied": [],
        }

    # ----------------------------------------------------------------
    # Full simulation
    # ----------------------------------------------------------------
    hard_fails: List[str] = []
    missing_checks: List[str] = []
    signals_used: Dict[str, Any] = {}
    thresholds_applied: List[str] = []

    # Crash threshold check
    if has_crash:
        threshold = flat_cfg["regime_selector.crash_p_threshold"]
        if p_implied_ok:
            signals_used["p_implied"] = p_implied
            thresholds_applied.append("regime_selector.crash_p_threshold")
            if p_implied > threshold:
                hard_fails.append("FAIL_CRASH_THRESHOLD")
        else:
            missing_checks.append("regime_selector.crash_p_threshold (p_implied absent)")

    # Edge check
    if has_edge:
        threshold = flat_cfg["edge_gating.min_edge"]
        if edge_ok:
            signals_used["edge"] = edge
            thresholds_applied.append("edge_gating.min_edge")
            if edge < threshold:
                hard_fails.append("FAIL_EDGE")
        else:
            missing_checks.append("edge_gating.min_edge (edge absent)")

    # Confidence check
    if has_conf:
        threshold = flat_cfg["edge_gating.min_confidence"]
        if conf_ok:
            signals_used["confidence"] = confidence
            thresholds_applied.append("edge_gating.min_confidence")
            if confidence < threshold:
                hard_fails.append("FAIL_CONFIDENCE")
        else:
            missing_checks.append("edge_gating.min_confidence (confidence absent)")

    # Debit check (partial — only when candidates were generated)
    if has_debit:
        threshold = flat_cfg["min_debit_per_contract"]
        if candidates_present and min_debit_in_cands is not None:
            signals_used["min_debit_in_candidates"] = min_debit_in_cands
            thresholds_applied.append("min_debit_per_contract")
            if min_debit_in_cands < threshold:
                hard_fails.append("FAIL_DEBIT")
        # If candidates not present, skip silently (no candidates → no debit check)

    # Determine outcome
    if missing_checks:
        # Some evaluable checks couldn't run → conservative PARTIAL
        outcome = "PARTIAL_SIGNALS"
        simulated = False
    elif hard_fails:
        outcome = hard_fails[0]  # primary failure
        simulated = True
    else:
        outcome = "PASS"
        simulated = True

    return {
        "gate_outcome": outcome,
        "gate_reasons": hard_fails,
        "missing_signal_checks": missing_checks,
        "simulated": simulated,
        "signals_used": signals_used,
        "config_thresholds_applied": thresholds_applied,
    }


# ---------------------------------------------------------------------------
# Run dir collection
# ---------------------------------------------------------------------------

_NON_CAMPAIGN_DIRS = frozenset({
    "allocator", "weekly", "intents", "snapshots", "proposals", "evaluations",
})


def _collect_run_dirs(runs_root: Path, run_dir_names: List[str]) -> List[Path]:
    """
    Reconstruct full Paths for run directories given a list of run dir names.

    Walks runs_root two levels deep (campaign/run_id), matching names returned
    by build_reflection_packet()["run_dirs_included"].

    Never raises.
    """
    runs_root = Path(runs_root)
    name_set = set(run_dir_names)
    result: List[Path] = []
    try:
        for campaign_dir in runs_root.iterdir():
            if not campaign_dir.is_dir():
                continue
            if campaign_dir.name in _NON_CAMPAIGN_DIRS:
                continue
            for run_dir in campaign_dir.iterdir():
                if run_dir.is_dir() and run_dir.name in name_set:
                    result.append(run_dir)
    except Exception as exc:
        log.warning("_collect_run_dirs: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Signal stats helper
# ---------------------------------------------------------------------------


def _compute_signal_stats(run_signals: List[dict]) -> dict:
    fields = ["p_implied", "p_external", "edge", "confidence"]
    stats: Dict[str, Any] = {}
    for field in fields:
        vals = [
            s[field]
            for s in run_signals
            if isinstance(s.get(field), (int, float))
        ]
        if vals:
            stats[field] = {
                "mean": statistics.mean(vals),
                "min": min(vals),
                "max": max(vals),
                "n": len(vals),
            }
        else:
            stats[field] = {"mean": None, "min": None, "max": None, "n": 0}
    return stats


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def compute_evaluation_metrics(
    run_signal_list: List[dict],
    config: dict,
) -> dict:
    """
    Aggregate gate simulation results across all runs under *config*.

    Coverage fields:
        runs_total             — all runs in the period
        runs_fully_simulated   — simulated=True (reliable gate result)
        runs_partial_signals   — gate_outcome=PARTIAL_SIGNALS (some data, not full)
        runs_without_signals   — gate_outcome=NO_SIGNALS (no data)
        runs_with_signals      — runs_fully_simulated + runs_partial_signals
        coverage_rate          — runs_fully_simulated / runs_total

    Gate rates are computed over runs_fully_simulated only.

    Always includes ``"simulated_only": True``.
    """
    if not run_signal_list:
        return {
            "runs_total": 0,
            "runs_fully_simulated": 0,
            "runs_partial_signals": 0,
            "runs_without_signals": 0,
            "runs_with_signals": 0,
            "coverage_rate": 0.0,
            "decision_counts": {},
            "no_trade_rate": None,
            "gate_pass_count": 0,
            "gate_pass_rate": None,
            "gate_fail_reasons": {},
            "candidate_debit_mean": None,
            "signal_stats": _compute_signal_stats([]),
            "simulated_only": True,
        }

    gate_results = [apply_threshold_gate(s, config) for s in run_signal_list]

    runs_total = len(run_signal_list)
    runs_fully_sim = sum(1 for g in gate_results if g["simulated"])
    runs_partial = sum(1 for g in gate_results if g["gate_outcome"] == "PARTIAL_SIGNALS")
    runs_no_sig = sum(1 for g in gate_results if g["gate_outcome"] == "NO_SIGNALS")
    runs_with_sig = runs_fully_sim + runs_partial
    coverage_rate = runs_fully_sim / runs_total if runs_total > 0 else 0.0

    gate_pass_count = sum(
        1 for g in gate_results if g["simulated"] and g["gate_outcome"] == "PASS"
    )
    gate_pass_rate = gate_pass_count / runs_fully_sim if runs_fully_sim > 0 else None

    decision_counts: Counter = Counter(
        s.get("decision", "UNKNOWN") for s in run_signal_list
    )
    no_trade_count = decision_counts.get("NO_TRADE", 0)
    no_trade_rate = no_trade_count / runs_total if runs_total > 0 else None

    gate_fail_reasons: Counter = Counter()
    for g in gate_results:
        for reason in g.get("gate_reasons", []):
            gate_fail_reasons[reason] += 1

    debit_vals = [
        s["min_debit_in_candidates"]
        for s in run_signal_list
        if isinstance(s.get("min_debit_in_candidates"), (int, float))
    ]
    candidate_debit_mean = statistics.mean(debit_vals) if debit_vals else None

    return {
        "runs_total": runs_total,
        "runs_fully_simulated": runs_fully_sim,
        "runs_partial_signals": runs_partial,
        "runs_without_signals": runs_no_sig,
        "runs_with_signals": runs_with_sig,
        "coverage_rate": coverage_rate,
        "decision_counts": dict(decision_counts),
        "no_trade_rate": no_trade_rate,
        "gate_pass_count": gate_pass_count,
        "gate_pass_rate": gate_pass_rate,
        "gate_fail_reasons": dict(gate_fail_reasons),
        "candidate_debit_mean": candidate_debit_mean,
        "signal_stats": _compute_signal_stats(run_signal_list),
        "simulated_only": True,
    }


# ---------------------------------------------------------------------------
# Comparison and assessment
# ---------------------------------------------------------------------------


def _assess(
    gate_delta: Optional[float],
    n_runs: int,
    coverage: float,
    overlay_classification: dict,
) -> Tuple[str, str, List[str]]:
    """Return (assessment, rationale, caveats)."""
    caveats: List[str] = []

    if overlay_classification.get("requires_rerun"):
        n_rr = len(overlay_classification["requires_rerun"])
        caveats.append(
            f"Counterfactual only — {n_rr} structural parameter(s) not simulated "
            "(require re-execution)."
        )
    if overlay_classification.get("unknown"):
        n_uk = len(overlay_classification["unknown"])
        caveats.append(f"{n_uk} unrecognized overlay key(s) were skipped.")

    fully_eval = overlay_classification.get("fully_evaluable", [])
    partially_eval = overlay_classification.get("partially_evaluable", [])

    # No evaluable parameters at all
    if not fully_eval and not partially_eval:
        caveats.append(
            "No evaluable parameters in overlay — all parameters require re-execution "
            "or are unrecognized."
        )
        return (
            "KEEP_TESTING",
            "No fully or partially evaluable parameters to simulate.",
            caveats,
        )

    # No runs evaluated
    if n_runs == 0:
        return "NO_CHANGE", "No runs found in the specified period.", caveats

    # Coverage too low — cap assessment at KEEP_TESTING regardless of delta
    if coverage < _MIN_COVERAGE_FOR_PROMOTE:
        caveats.append(
            f"Coverage {coverage:.0%} below minimum {_MIN_COVERAGE_FOR_PROMOTE:.0%} "
            f"required for PROMOTE_TO_PAPER_REVIEW. "
            f"Insufficient gate artifacts to simulate reliably."
        )

    if gate_delta is None:
        return (
            "KEEP_TESTING",
            "Gate pass rate not available — no fully simulated runs to compare.",
            caveats,
        )

    abs_delta = abs(gate_delta)

    if abs_delta < _NO_CHANGE_DELTA:
        return (
            "NO_CHANGE",
            (
                f"Gate pass rate delta {gate_delta:+.1%} is within noise threshold "
                f"({_NO_CHANGE_DELTA:.0%}). Overlay has negligible effect on captured signals."
            ),
            caveats,
        )

    # Check promote eligibility (coverage already handled above)
    if (
        abs_delta >= _PROMOTE_GATE_DELTA
        and n_runs >= _MIN_RUNS_FOR_PROMOTE
        and coverage >= _MIN_COVERAGE_FOR_PROMOTE
    ):
        direction = "more passes" if gate_delta > 0 else "fewer passes"
        return (
            "PROMOTE_TO_PAPER_REVIEW",
            (
                f"Gate pass rate delta {gate_delta:+.1%} ({direction}), "
                f"n={n_runs} run(s), coverage={coverage:.0%}. "
                "Meets threshold for paper review consideration. "
                "Operator review required before any config change."
            ),
            caveats,
        )

    # Build KEEP_TESTING rationale
    reasons: List[str] = []
    if abs_delta >= _PROMOTE_GATE_DELTA and n_runs < _MIN_RUNS_FOR_PROMOTE:
        reasons.append(
            f"n={n_runs} below minimum {_MIN_RUNS_FOR_PROMOTE} for promotion"
        )
    if abs_delta >= _PROMOTE_GATE_DELTA and coverage < _MIN_COVERAGE_FOR_PROMOTE:
        reasons.append(
            f"coverage {coverage:.0%} below minimum {_MIN_COVERAGE_FOR_PROMOTE:.0%}"
        )
    if abs_delta < _PROMOTE_GATE_DELTA:
        reasons.append(
            f"gate delta {gate_delta:+.1%} below promotion threshold {_PROMOTE_GATE_DELTA:.0%}"
        )

    return (
        "KEEP_TESTING",
        f"Non-trivial delta detected ({gate_delta:+.1%}), but: " + "; ".join(reasons) + ".",
        caveats,
    )


def compute_comparison(
    baseline_metrics: dict,
    overlay_metrics: dict,
    overlay_classification: dict,
) -> dict:
    """
    Compute delta metrics and produce a structured assessment.

    Always includes:
        ``"simulated_only": True``
        ``"simulation_disclaimer": SIMULATION_DISCLAIMER``

    Returns:
        {
            schema_version, simulated_only, simulation_disclaimer,
            fully_evaluable_parameters, partially_evaluable_parameters,
            requires_rerun_parameters, unknown_parameters,
            delta: {gate_pass_rate, no_trade_rate, runs_total, coverage_rate},
            assessment,           NO_CHANGE | KEEP_TESTING | PROMOTE_TO_PAPER_REVIEW
            assessment_rationale, str
            assessment_caveats,   list[str]
        }
    """
    b_gate = baseline_metrics.get("gate_pass_rate")
    o_gate = overlay_metrics.get("gate_pass_rate")
    b_notrade = baseline_metrics.get("no_trade_rate")
    o_notrade = overlay_metrics.get("no_trade_rate")
    n_runs = overlay_metrics.get("runs_total", 0)
    coverage = overlay_metrics.get("coverage_rate", 0.0)

    gate_delta = (
        (o_gate - b_gate) if (o_gate is not None and b_gate is not None) else None
    )
    notrade_delta = (
        (o_notrade - b_notrade)
        if (o_notrade is not None and b_notrade is not None)
        else None
    )

    assessment, rationale, caveats = _assess(
        gate_delta, n_runs, coverage, overlay_classification
    )

    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "simulated_only": True,
        "simulation_disclaimer": SIMULATION_DISCLAIMER,
        "fully_evaluable_parameters": overlay_classification.get("fully_evaluable", []),
        "partially_evaluable_parameters": overlay_classification.get("partially_evaluable", []),
        "requires_rerun_parameters": overlay_classification.get("requires_rerun", []),
        "unknown_parameters": overlay_classification.get("unknown", []),
        "delta": {
            "gate_pass_rate": gate_delta,
            "no_trade_rate": notrade_delta,
            "runs_total": n_runs,
            "coverage_rate": coverage,
        },
        "assessment": assessment,
        "assessment_rationale": rationale,
        "assessment_caveats": caveats,
    }


# ---------------------------------------------------------------------------
# Top-level report builder
# ---------------------------------------------------------------------------


def build_evaluation_report(
    baseline_config: dict,
    overlay_config: dict,
    run_dirs: List[Path],
    period: dict,
    ts_utc: str,
) -> dict:
    """
    Orchestrate the full counterfactual evaluation.

    Args:
        baseline_config: Loaded baseline YAML dict.
        overlay_config:  Merged (baseline + overlay) config dict.
        run_dirs:        List of run directory Paths to evaluate.
        period:          {"since": str, "until": str}
        ts_utc:          ISO 8601 timestamp for this report.

    Returns:
        Full evaluation report dict. Never raises.
    """
    classification: Dict[str, List[str]] = {
        "fully_evaluable": [],
        "partially_evaluable": [],
        "requires_rerun": [],
        "unknown": [],
    }
    baseline_metrics: dict = {}
    overlay_metrics: dict = {}

    try:
        overlay_raw = _recover_overlay(baseline_config, overlay_config)
        classification = classify_overlay_keys(overlay_raw)
        run_signals = [extract_run_signals(d) for d in run_dirs]
        baseline_metrics = compute_evaluation_metrics(run_signals, baseline_config)
        overlay_metrics = compute_evaluation_metrics(run_signals, overlay_config)
        comparison = compute_comparison(baseline_metrics, overlay_metrics, classification)
    except Exception as exc:
        log.error("build_evaluation_report failed: %s", exc, exc_info=True)
        comparison = {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "simulated_only": True,
            "simulation_disclaimer": SIMULATION_DISCLAIMER,
            "assessment": "KEEP_TESTING",
            "assessment_rationale": f"Report build failed: {exc}",
            "assessment_caveats": [],
            "fully_evaluable_parameters": [],
            "partially_evaluable_parameters": [],
            "requires_rerun_parameters": [],
            "unknown_parameters": [],
            "delta": {},
        }

    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "simulated_only": True,
        "simulation_disclaimer": SIMULATION_DISCLAIMER,
        "ts_utc": ts_utc,
        "period": period,
        "runs_evaluated": len(run_dirs),
        "overlay_classification": classification,
        "baseline": baseline_metrics,
        "overlay": overlay_metrics,
        "comparison": comparison,
    }


def _recover_overlay(baseline: dict, merged: dict) -> dict:
    """
    Derive the overlay-only keys by comparing merged config against baseline.

    Used internally to classify what changed — we don't require the raw
    overlay dict at this stage since build_evaluation_report works from
    the already-merged config.
    """
    flat_base = flatten_config(baseline)
    flat_merged = flatten_config(merged)
    overlay_keys: Dict[str, Any] = {}
    for key, val in flat_merged.items():
        if key not in flat_base or flat_base[key] != val:
            overlay_keys[key] = val
    return overlay_keys
