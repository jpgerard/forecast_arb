"""
forecast_arb.core.reflection_packet
=====================================
Weekly reflection packet builder.

Aggregates run-level and ledger-level data across a date range into a compact
structured packet suitable for operator review or LLM analysis.

All data sources are optional; missing files produce partial (not empty) packets
with zero counts and explicit flags. Never raises.

Public API
----------
    build_reflection_packet(
        runs_root, since, until,
        config_paths=None,
        trade_outcomes_path=None,
    ) -> dict

Packet schema (schema_version "1.0")
--------------------------------------
    {
        "schema_version": "1.0",
        "period": {"since": str, "until": str},
        "ts_utc": str,
        "runs_scanned": int,
        "runs_skipped_no_timestamp": int,
        "run_dirs_included": list[str],
        "timestamp_strategy_used": str,
        "trade_summary": {...},
        "rejection_summary": {...},
        "quote_activity": {...},
        "signal_stats": {...},
        "regime_summary": {...},
        "active_parameters": dict,
        "config_paths_used": list[str],
    }
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

REFLECTION_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse ISO datetime string to aware datetime. Returns None on failure."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_period(since: str, until: str) -> Tuple[datetime, datetime]:
    """
    Parse 'YYYY-MM-DD' strings.
    Returns (since_dt, until_dt) where until_dt is end-of-day UTC inclusive.
    """
    since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    until_date = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    until_dt = until_date + timedelta(hours=23, minutes=59, seconds=59)
    return since_dt, until_dt


def _in_period(ts_str: str, since_dt: datetime, until_dt: datetime) -> bool:
    dt = _parse_iso(ts_str)
    if dt is None:
        return False
    return since_dt <= dt <= until_dt


# ---------------------------------------------------------------------------
# Timestamp resolution
# ---------------------------------------------------------------------------


def _resolve_run_timestamp(run_dir: Path) -> Tuple[Optional[datetime], str]:
    """
    Resolve a run directory's timestamp.

    Priority:
        1. runs_root/<run>/manifest.json → run_time_utc
        2. artifacts/final_decision.json → timestamp_utc
        3. artifacts/operator_summary.json → ts_utc or run.timestamp
        4. artifacts/regime_ledger.jsonl → ts_utc of first entry
        5. Directory mtime
        6. run_id name parsing (YYYYMMDDTHHMMSS suffix)

    Returns:
        (datetime | None, strategy_label)
    """
    artifacts = run_dir / "artifacts"

    # 1 — manifest.json
    try:
        mp = run_dir / "manifest.json"
        if mp.exists():
            with open(mp, encoding="utf-8") as fh:
                m = json.load(fh)
            dt = _parse_iso(m.get("run_time_utc") or "")
            if dt:
                return dt, "artifact_ts"
    except Exception:
        pass

    # 2 — final_decision.json timestamp_utc
    try:
        fd = artifacts / "final_decision.json"
        if fd.exists():
            with open(fd, encoding="utf-8") as fh:
                d = json.load(fh)
            dt = _parse_iso(d.get("timestamp_utc") or "")
            if dt:
                return dt, "artifact_ts"
    except Exception:
        pass

    # 3 — operator_summary.json ts_utc / run.timestamp
    try:
        op = artifacts / "operator_summary.json"
        if op.exists():
            with open(op, encoding="utf-8") as fh:
                d = json.load(fh)
            raw = d.get("ts_utc") or d.get("run", {}).get("timestamp") or ""
            dt = _parse_iso(raw)
            if dt:
                return dt, "artifact_ts"
    except Exception:
        pass

    # 4 — regime_ledger.jsonl first entry ts_utc
    try:
        rl = artifacts / "regime_ledger.jsonl"
        if rl.exists():
            with open(rl, encoding="utf-8") as fh:
                first = fh.readline().strip()
            if first:
                entry = json.loads(first)
                dt = _parse_iso(entry.get("ts_utc") or "")
                if dt:
                    return dt, "artifact_ts"
    except Exception:
        pass

    # 5 — directory mtime
    try:
        dt = datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
        return dt, "mtime"
    except Exception:
        pass

    # 6 — run_id parsing: match YYYYMMDDTHHMMSS at end of name
    try:
        m = re.search(r"(\d{8})T(\d{6})$", run_dir.name)
        if m:
            ds, ts_ = m.group(1), m.group(2)
            dt = datetime(
                int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                int(ts_[:2]), int(ts_[2:4]), int(ts_[4:6]),
                tzinfo=timezone.utc,
            )
            return dt, "run_id_parse"
    except Exception:
        pass

    return None, "failed"


# ---------------------------------------------------------------------------
# Run dir scanning
# ---------------------------------------------------------------------------

# Dirs inside runs_root that are not campaign dirs
_NON_CAMPAIGN_DIRS = frozenset({"allocator", "weekly", "intents", "snapshots"})


def _scan_run_dirs(
    runs_root: Path,
    since_dt: datetime,
    until_dt: datetime,
) -> Tuple[List[Tuple[Path, datetime]], int, str]:
    """
    Walk runs_root/<campaign>/<run_id>/ (two levels deep), resolving timestamps.

    Returns:
        ([(run_dir, ts)], skipped_no_timestamp, strategy_summary)
    """
    included: List[Tuple[Path, datetime]] = []
    skipped = 0
    strategies: Counter = Counter()

    try:
        if not runs_root.is_dir():
            return [], 0, "none"
        for campaign_dir in runs_root.iterdir():
            if not campaign_dir.is_dir():
                continue
            if campaign_dir.name in _NON_CAMPAIGN_DIRS:
                continue
            for run_dir in campaign_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                if not (run_dir / "artifacts").is_dir():
                    continue
                dt, strategy = _resolve_run_timestamp(run_dir)
                if dt is None:
                    skipped += 1
                    log.warning(f"Could not resolve timestamp for {run_dir.name} — skipped")
                    continue
                strategies[strategy] += 1
                if since_dt <= dt <= until_dt:
                    included.append((run_dir, dt))
    except Exception as exc:
        log.warning(f"_scan_run_dirs error: {exc}")

    if len(strategies) > 1:
        summary = "mixed"
    elif strategies:
        summary = next(iter(strategies))
    else:
        summary = "none"

    return included, skipped, summary


# ---------------------------------------------------------------------------
# Ledger readers
# ---------------------------------------------------------------------------


def _load_jsonl_filtered(
    path: Path,
    since_dt: datetime,
    until_dt: datetime,
    ts_field: str,
) -> List[dict]:
    """
    Read a JSONL file, returning entries whose ts_field falls in period.
    Skips lines with missing ts_field (mixed-schema guard). Never raises.
    """
    entries: List[dict] = []
    if not path.exists():
        return entries
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                raw_ts = entry.get(ts_field)
                if raw_ts is None:
                    continue
                if _in_period(str(raw_ts), since_dt, until_dt):
                    entries.append(entry)
    except Exception as exc:
        log.warning(f"_load_jsonl_filtered({path.name}): {exc}")
    return entries


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _extract_quote_activity(trade_outcomes: List[dict]) -> dict:
    """
    Aggregate quote and execution activity from trade_outcomes entries.

    Recognises:
        event == "STAGED_PAPER"         → STAGED_PAPER
        execution_verdict == "OK_TO_STAGE" → QUOTE_OK
        execution_verdict == "BLOCKED"     → QUOTE_BLOCKED

    top_block_reasons: list[{"reason": str, "count": int}]
    """
    staged_paper = 0
    quote_ok = 0
    quote_blocked = 0
    block_reasons: Counter = Counter()

    for entry in trade_outcomes:
        event = entry.get("event")
        if event == "STAGED_PAPER":
            staged_paper += 1
        elif event == "OPEN":
            pass  # counted separately in trade_summary
        elif event == "CLOSE":
            pass

        verdict = entry.get("execution_verdict")
        if verdict == "OK_TO_STAGE":
            quote_ok += 1
        elif verdict == "BLOCKED":
            quote_blocked += 1
            reason = entry.get("reason") or entry.get("block_reason")
            if reason:
                block_reasons[str(reason)] += 1

    return {
        "QUOTE_OK": quote_ok,
        "QUOTE_BLOCKED": quote_blocked,
        "STAGED_PAPER": staged_paper,
        "top_block_reasons": [
            {"reason": r, "count": c}
            for r, c in block_reasons.most_common(5)
        ],
        "data_available": len(trade_outcomes) > 0,
    }


def _compute_signal_stats(regime_entries: List[dict]) -> dict:
    """Compute mean/min/max/n stats for signals from regime_ledger entries."""
    fields = ["p_implied", "p_external", "edge", "confidence"]
    stats: Dict[str, Any] = {}
    for field in fields:
        vals = [
            e[field]
            for e in regime_entries
            if isinstance(e.get(field), (int, float))
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


def _compute_evidence_class_stats(run_dirs: List[str], runs_root: Path) -> dict:
    """Patch C/D: count EvidenceClass occurrences across per-run artifacts.

    Reads ``artifacts/p_event_external.json`` from each run directory and
    buckets the ``evidence_class`` field into counters.  Runs without the
    file (pre-Patch-C or missing artifact) increment ``unclassified``.

    Patch D additions:
    - ``by_role``: counts grouped by EVIDENCE_ROLE value.  All four known role
      strings plus ``"UNKNOWN"`` are always present (even at zero) so callers
      do not need to guard against missing keys.
    - ``informative_or_above_rate``: (EXACT + NEARBY + PROXY) / n_classified.
      Answers "how often did we have at least informative evidence."
    - ``terminal_or_above_rate``: (EXACT + NEARBY) / n_classified.
      Answers "how often did we have a terminal-quality signal."
    Rates are over *classified* runs only (unclassified excluded from denominator).

    Args:
        run_dirs: List of run-directory *names* (not full paths) as returned
                  by ``build_reflection_packet``.
        runs_root: Root path used to reconstruct absolute run-dir paths.

    Returns:
        Dict with per-class counts, by_role breakdown, rate metrics,
        authoritative_capable_count, and n_total.  Never raises.
    """
    from collections import Counter as _Counter
    from forecast_arb.oracle.evidence import EVIDENCE_ROLE, get_policy_role  # type: ignore

    _KNOWN = [
        "EXACT_TERMINAL",
        "NEARBY_TERMINAL",
        "PATHWISE_PROXY",
        "COARSE_REGIME",
        "UNUSABLE",
    ]

    # All known role strings (guaranteed present in output even at zero)
    _KNOWN_ROLES = [
        "AUTHORITATIVE_CAPABLE",
        "INFORMATIVE_ONLY",
        "CONTEXT_ONLY",
        "DIAGNOSTIC_ONLY",
        "UNKNOWN",
    ]

    counts: _Counter = _Counter({k: 0 for k in _KNOWN})
    by_role: _Counter = _Counter({r: 0 for r in _KNOWN_ROLES})
    unclassified = 0
    authoritative_capable_count = 0

    for run_name in run_dirs:
        # run dirs may be bare names OR full paths; handle both
        rd = Path(run_name)
        if not rd.is_absolute():
            # Search one level under runs_root (the structure is runs_root/<strategy>/<run_id>)
            # Use glob to find the run dir regardless of the strategy subdirectory.
            matches = list(runs_root.glob(f"**/{run_name}"))
            rd = matches[0] if matches else runs_root / run_name

        artifact = rd / "artifacts" / "p_event_external.json"
        try:
            if not artifact.exists():
                unclassified += 1
                by_role["UNKNOWN"] += 1
                continue
            with open(artifact, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                # Legacy plain-float artifact
                unclassified += 1
                by_role["UNKNOWN"] += 1
                continue
            ec = data.get("evidence_class")
            if ec in _KNOWN:
                counts[ec] += 1
                # Patch D: use get_policy_role for role lookup (None-safe)
                role = get_policy_role(ec)  # ec is a str value here
                by_role[role] += 1
                if data.get("authoritative_capable", False):
                    authoritative_capable_count += 1
            else:
                unclassified += 1
                by_role["UNKNOWN"] += 1
        except Exception:
            unclassified += 1
            by_role["UNKNOWN"] += 1

    n_total = sum(counts.values()) + unclassified
    n_classified = sum(counts.values())

    authoritative_capable_rate = (
        round(authoritative_capable_count / n_total, 4) if n_total > 0 else 0.0
    )

    # Patch D: rates over classified runs only
    informative_or_above_rate = round(
        (counts["EXACT_TERMINAL"] + counts["NEARBY_TERMINAL"] + counts["PATHWISE_PROXY"])
        / n_classified,
        4,
    ) if n_classified > 0 else 0.0

    terminal_or_above_rate = round(
        (counts["EXACT_TERMINAL"] + counts["NEARBY_TERMINAL"]) / n_classified,
        4,
    ) if n_classified > 0 else 0.0

    return {
        **{k: counts[k] for k in _KNOWN},
        "unclassified": unclassified,
        "authoritative_capable_count": authoritative_capable_count,
        "authoritative_capable_rate": authoritative_capable_rate,
        # Patch D
        "by_role": dict(by_role),
        "informative_or_above_rate": informative_or_above_rate,
        "terminal_or_above_rate": terminal_or_above_rate,
        "n_total": n_total,
    }


def _load_configs(paths: Optional[List[Path]]) -> Tuple[dict, List[str]]:
    """Load and merge YAML config files. Returns (merged_dict, loaded_path_strs)."""
    if not paths:
        return {}, []
    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("pyyaml not available — active_parameters will be empty")
        return {}, []

    merged: dict = {}
    loaded: List[str] = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            log.warning(f"Config path not found: {p}")
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict):
                merged.update(data)
                loaded.append(str(p))
        except Exception as exc:
            log.warning(f"Failed to load config {p}: {exc}")
    return merged, loaded


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_reflection_packet(
    runs_root: Path,
    since: str,
    until: str,
    config_paths: Optional[List[Path]] = None,
    trade_outcomes_path: Optional[Path] = None,
) -> dict:
    """
    Build a weekly reflection packet from run artifacts and ledgers.

    Args:
        runs_root:            Root of run directories (e.g. Path("runs")).
        since:                Start date inclusive "YYYY-MM-DD".
        until:                End date inclusive "YYYY-MM-DD".
        config_paths:         YAML config files to read active parameters from.
                              None → active_parameters={}.
        trade_outcomes_path:  Path to trade_outcomes.jsonl.
                              None → defaults to runs_root / "trade_outcomes.jsonl".

    Returns:
        Reflection packet dict (schema_version "1.0"). Never raises.
    """
    from datetime import datetime as _dt  # noqa: local alias

    ts_utc = _dt.now(timezone.utc).isoformat()
    runs_root = Path(runs_root)

    if trade_outcomes_path is None:
        trade_outcomes_path = runs_root / "trade_outcomes.jsonl"

    # ------------------------------------------------------------------
    # Parse period
    # ------------------------------------------------------------------
    try:
        since_dt, until_dt = _parse_period(since, until)
    except Exception as exc:
        log.error(f"Invalid period since={since!r} until={until!r}: {exc}")
        since_dt = datetime.min.replace(tzinfo=timezone.utc)
        until_dt = datetime.max.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------
    # Scan run dirs
    # ------------------------------------------------------------------
    run_dir_entries, skipped_no_ts, ts_strategy = _scan_run_dirs(
        runs_root, since_dt, until_dt
    )

    # ------------------------------------------------------------------
    # Per-run data: final_decision + operator_summary
    # ------------------------------------------------------------------
    total_runs = len(run_dir_entries)
    runs_with_trade = 0
    no_trade_runs = 0
    submit_executed_count = 0
    preflight_blocked = 0
    gate_rejections = 0
    notes_freq: Counter = Counter()
    run_dirs_included: List[str] = []

    for run_dir, _ts in run_dir_entries:
        run_dirs_included.append(run_dir.name)

        # final_decision.json
        try:
            fd = run_dir / "artifacts" / "final_decision.json"
            if fd.exists():
                with open(fd, encoding="utf-8") as fh:
                    d = json.load(fh)
                decision = d.get("decision", "UNKNOWN")
                if decision == "NO_TRADE":
                    no_trade_runs += 1
                else:
                    runs_with_trade += 1
                if d.get("submit_executed"):
                    submit_executed_count += 1
        except Exception:
            pass

        # operator_summary.json — notes and preflight
        try:
            op = run_dir / "artifacts" / "operator_summary.json"
            if op.exists():
                with open(op, encoding="utf-8") as fh:
                    d = json.load(fh)
                for note in d.get("notes", []):
                    notes_freq[str(note)] += 1
                pf = d.get("broker_preflight") or {}
                if pf.get("status") == "BLOCKED":
                    preflight_blocked += 1
        except Exception:
            pass

        # gate_decision.json
        try:
            gd = run_dir / "artifacts" / "gate_decision.json"
            if gd.exists():
                with open(gd, encoding="utf-8") as fh:
                    d = json.load(fh)
                decision = d.get("decision") or d.get("verdict") or ""
                if decision.upper() in ("BLOCKED", "REJECTED", "PENDING_HUMAN"):
                    gate_rejections += 1
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Regime ledger — signal stats + decision reasons
    # ------------------------------------------------------------------
    regime_ledger_path = runs_root / "regime_ledger.jsonl"
    regime_entries = _load_jsonl_filtered(
        regime_ledger_path, since_dt, until_dt, ts_field="ts_utc"
    )

    # Rejection reasons from regime ledger
    rejection_reasons: Counter = Counter()
    regimes_run: Counter = Counter()
    gate_decisions_counter: Counter = Counter()
    for entry in regime_entries:
        regime = entry.get("regime", "unknown")
        regimes_run[regime] += 1
        for reason in entry.get("reasons", []):
            rejection_reasons[str(reason)] += 1
        gd_val = entry.get("gate_decision")
        if gd_val:
            gate_decisions_counter[str(gd_val)] += 1

    signal_stats = _compute_signal_stats(regime_entries)
    signal_stats["evidence_class_stats"] = _compute_evidence_class_stats(
        run_dirs_included, runs_root
    )

    # ------------------------------------------------------------------
    # Trade outcomes — trade events + quote activity
    # ------------------------------------------------------------------
    outcomes_entries = _load_jsonl_filtered(
        Path(trade_outcomes_path), since_dt, until_dt, ts_field="timestamp_utc"
    )
    quote_activity = _extract_quote_activity(outcomes_entries)

    trade_outcomes_clean = [
        e for e in outcomes_entries
        if e.get("event") in ("OPEN", "CLOSE", "STAGED_PAPER")
    ]

    # ------------------------------------------------------------------
    # Active parameters
    # ------------------------------------------------------------------
    active_parameters, config_paths_used = _load_configs(config_paths)
    if not config_paths:
        log.info("No config_paths provided — active_parameters will be empty")

    # ------------------------------------------------------------------
    # Assemble
    # ------------------------------------------------------------------
    return {
        "schema_version": REFLECTION_SCHEMA_VERSION,
        "period": {"since": since, "until": until},
        "ts_utc": ts_utc,
        "runs_scanned": total_runs,
        "runs_skipped_no_timestamp": skipped_no_ts,
        "run_dirs_included": run_dirs_included,
        "timestamp_strategy_used": ts_strategy,
        "trade_summary": {
            "total_runs": total_runs,
            "runs_with_trade": runs_with_trade,
            "no_trade_runs": no_trade_runs,
            "submit_executed_count": submit_executed_count,
            "outcomes": trade_outcomes_clean,
        },
        "rejection_summary": {
            "total_no_trade": no_trade_runs,
            "reasons": dict(rejection_reasons),
            "preflight_blocked": preflight_blocked,
            "gate_rejections": gate_rejections,
            "notes_frequency": dict(notes_freq),
        },
        "quote_activity": quote_activity,
        "signal_stats": signal_stats,
        "regime_summary": {
            "regimes_run": dict(regimes_run),
            "gate_decisions": dict(gate_decisions_counter),
        },
        "active_parameters": active_parameters,
        "config_paths_used": config_paths_used,
    }
