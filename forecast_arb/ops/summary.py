"""
forecast_arb.ops.summary
========================
Operator-facing markdown summary renderer.

Renders a build_decision_packet() result (schema_version "2.0") as
compact, human-readable markdown for daily operator review.

Public API
----------
    render_operator_summary(packet, run_dir=None) -> str

Raises:
    ValueError: if packet["schema_version"] != SUPPORTED_SCHEMA_VERSION
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

SUPPORTED_SCHEMA_VERSION = "2.0"


def render_operator_summary(
    packet: Dict[str, Any],
    run_dir: Optional[Path] = None,
) -> str:
    """
    Render a decision packet as operator-facing markdown.

    Args:
        packet:   Result of build_decision_packet(); must be schema_version "2.0".
        run_dir:  Run directory path used in the Artifacts section. None → "N/A".

    Returns:
        Markdown string.

    Raises:
        ValueError: if packet schema_version is not SUPPORTED_SCHEMA_VERSION.
    """
    schema = packet.get("schema_version", "MISSING")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"render_operator_summary: unsupported schema_version={schema!r}. "
            f"Expected {SUPPORTED_SCHEMA_VERSION!r}. "
            f"Rebuild packet with build_decision_packet() or update the renderer."
        )

    run = packet.get("run", {})
    signals = packet.get("signals", {})
    preflight = packet.get("broker_preflight")
    candidates = packet.get("top_candidates", [])
    notes = packet.get("notes", [])

    lines: List[str] = []

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    lines.append("# Daily Operator Summary")
    lines.append("")
    lines.append(f"**Run:** `{run.get('run_id') or 'N/A'}`  ")
    lines.append(f"**Date:** {run.get('timestamp') or packet.get('ts_utc') or 'N/A'}  ")
    lines.append(f"**Mode:** {run.get('mode') or 'N/A'}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ------------------------------------------------------------------
    # Broker Preflight
    # ------------------------------------------------------------------
    lines.append("## Broker Preflight")
    lines.append("")

    if preflight is None:
        lines.append("*Not run.*")
    else:
        status = preflight.get("status", "UNKNOWN")
        reason = preflight.get("reason", "")
        lines.append(f"**Status:** {status}")
        lines.append(f"**Reason:** {reason}")

        # Drift details on BLOCKED
        drift = preflight.get("drift")
        if status == "BLOCKED" and drift:
            only_ibkr = drift.get("only_in_ibkr", [])
            qty_issues = drift.get("qty_mismatches", [])
            if only_ibkr:
                lines.append("")
                lines.append(f"**In IBKR, not in CCC ({len(only_ibkr)}):**")
                for rec in only_ibkr:
                    sym = rec.get("symbol", "?")
                    exp = rec.get("expiry", "?")
                    qty = rec.get("qty", "?")
                    lines.append(f"- {sym} {exp} qty={qty}")
            if qty_issues:
                lines.append("")
                lines.append(f"**Qty mismatches ({len(qty_issues)}):**")
                for m in qty_issues:
                    key = m.get("key", "?")
                    lines.append(
                        f"- {key}: CCC={m.get('ccc_qty', '?')} IBKR={m.get('ibkr_qty', '?')}"
                    )

        inv = preflight.get("inventory", {})
        if inv:
            lines.append("")
            lines.append(
                f"**Inventory:** "
                f"crash_open={inv.get('crash_open', 0)}  "
                f"selloff_open={inv.get('selloff_open', 0)}"
            )

        pv = preflight.get("positions_view", {})
        if pv:
            lines.append(
                f"**Positions View:** "
                f"pending_orders={pv.get('pending_orders_count', 0)}  "
                f"open_premium=${pv.get('open_premium_total', 0.0):.2f}"
            )

        errors = preflight.get("errors", [])
        if errors:
            lines.append("")
            lines.append(f"**Preflight Errors ({len(errors)}):**")
            for e in errors:
                lines.append(f"- {e}")

    lines.append("")

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------
    lines.append("## Signals")
    lines.append("")

    def _fmt(val: Any) -> str:
        if val is None:
            return "N/A"
        if isinstance(val, float):
            return f"{val:.4f}"
        return str(val)

    lines.append("| Signal | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| p_external | {_fmt(signals.get('p_external'))} |")
    lines.append(f"| p_implied  | {_fmt(signals.get('p_implied'))} |")
    lines.append(f"| edge       | {_fmt(signals.get('edge'))} |")
    lines.append(f"| confidence | {_fmt(signals.get('confidence'))} |")
    lines.append(f"| gate       | {signals.get('gate_decision') or 'N/A'} |")
    lines.append("")

    # ------------------------------------------------------------------
    # Top Candidates
    # ------------------------------------------------------------------
    lines.append("## Top Candidates")
    lines.append("")

    if not candidates:
        lines.append("*No candidates.*")
    else:
        lines.append("| Regime | Rank | Expiry | Long K | Short K | EV/$ | Debit |")
        lines.append("|--------|------|--------|--------|---------|------|-------|")
        for c in candidates:
            regime_name = c.get("regime", "?")
            rank = c.get("rank", "?")
            expiry = c.get("expiry", "?")
            lk = c.get("long_strike")
            sk = c.get("short_strike")
            ev = c.get("ev_per_dollar")
            debit = c.get("debit_per_contract")
            debit_str = f"${debit:.2f}" if debit is not None else "N/A"
            lines.append(
                f"| {regime_name} | {rank} | {expiry}"
                f" | {lk if lk is not None else '?'}"
                f" | {sk if sk is not None else '?'}"
                f" | {_fmt(ev)}"
                f" | {debit_str} |"
            )

    lines.append("")

    # ------------------------------------------------------------------
    # Run Status
    # ------------------------------------------------------------------
    lines.append("## Run Status")
    lines.append("")
    lines.append(f"**Decision:** {run.get('decision', 'UNKNOWN')}")
    lines.append(f"**Reason:** {run.get('reason', 'N/A')}")
    lines.append(f"**Tickets:** {run.get('num_tickets', 0)}")
    lines.append(f"**Submit Requested:** {run.get('submit_requested', False)}")
    lines.append(f"**Submit Executed:** {run.get('submit_executed', False)}")
    lines.append("")

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------
    lines.append("## Notes")
    lines.append("")

    if not notes:
        lines.append("*None.*")
    else:
        for note in notes:
            lines.append(f"- {note}")

    lines.append("")

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------
    lines.append("## Artifacts")
    lines.append("")

    if run_dir is not None:
        artifacts_dir = Path(run_dir) / "artifacts"
        lines.append(f"- **Run dir:** `{run_dir}`")
        lines.append(f"- **Packet:** `{artifacts_dir / 'operator_summary.json'}`")
        lines.append(f"- **Summary:** `{artifacts_dir / 'operator_summary.md'}`")
    else:
        lines.append("*Run dir not available.*")

    lines.append("")

    return "\n".join(lines)
