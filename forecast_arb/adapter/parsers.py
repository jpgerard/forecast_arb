"""
forecast_arb.adapter.parsers
============================
Light output-parsing helpers for trading_adapter.py.

These parse stdout/stderr from subprocess calls to scripts/daily.py and
scripts/ccc_report.py.  All functions are pure (no I/O, no side effects).

Design rules:
  - Parse only what is needed for the output contract.
  - Never replicate business logic from CCC scripts.
  - Return sensible defaults when patterns are absent (graceful degradation).
  - No external dependencies.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# daily.py output parsers
# ---------------------------------------------------------------------------

# The operator summary box printed by _print_operator_summary() in daily.py:
#
#   ╔══ DAILY RUN SUMMARY ════════════════════════════════════════╗
#   ║  CANDIDATES FILE: intents/allocator/recommended.json (seen 2)
#   ║  CCC PLAN: planned_opens=1 planned_closes=0 holds=2
#   ║  INVENTORY ACTUAL: crash=1 selloff=0
#   ║  PENDING (committed-not-filled): crash=0 selloff=0
#   ║  EFFECTIVE (gating): crash=1 selloff=0
#   ║  CCC EXECUTE: mode=paper quote_only=true committed_new=1 committed_skipped=0
#   ║  Commit ledger: runs/allocator/allocator_commit_ledger.jsonl
#   ╚════════════════════════════════════════════════════════════╝

_PLAN_RE = re.compile(
    r"CCC PLAN:\s+planned_opens=(\d+)\s+planned_closes=(\d+)\s+holds=(\d+)"
)
_INV_RE = re.compile(r"INVENTORY ACTUAL:\s+crash=(\d+)\s+selloff=(\d+)")
_PENDING_RE = re.compile(
    r"PENDING \(committed-not-filled\):\s+crash=(\d+)\s+selloff=(\d+)"
)
_EXECUTE_RE = re.compile(
    r"CCC EXECUTE:\s+mode=(\w+)\s+quote_only=(\w+)\s+"
    r"committed_new=(\d+)\s+committed_skipped=(\d+)"
)
_GATE_REASON_RE = re.compile(r"Gate reason:\s+(.+)")
_CANDIDATES_SEEN_RE = re.compile(r"CANDIDATES FILE:.*\(seen (\d+)\)")
_ANNUAL_BUDGET_RE = re.compile(
    r"ANNUAL BUDGET:\s+ytd_spent=\$?([\d.]+)\s+budget=\$?([\d.]+)\s+remaining=\$?([\d.]+|N/A)"
)
# Quote-only pass line emitted by ccc_execute.py
_QUOTE_OK_RE = re.compile(r"Quote-only preview:\s+(\d+)\s+intent\(s\) validated")


def parse_preview_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """
    Parse the stdout from a daily.py --execute --paper --quote-only run.

    Returns a dict suitable for use as AdapterResult.details in
    preview_daily_cycle():

        {
            "planned_opens":    int,
            "planned_closes":   int,
            "holds":            int,
            "crash_open":       int,
            "selloff_open":     int,
            "pending_crash":    int,
            "pending_selloff":  int,
            "candidates_seen":  int,
            "gate_reason":      str | None,
            "quote_only_validated": int,   # intents validated in quote-only check
            "summary_box_found": bool,     # True if summary box was present in output
            "annual_budget": {
                "ytd_spent": float | None,
                "budget":    float | None,
                "remaining": float | None,
            },
        }
    """
    full_text = stdout + "\n" + stderr

    result: Dict[str, Any] = {
        "planned_opens": 0,
        "planned_closes": 0,
        "holds": 0,
        "crash_open": 0,
        "selloff_open": 0,
        "pending_crash": 0,
        "pending_selloff": 0,
        "candidates_seen": 0,
        "gate_reason": None,
        "quote_only_validated": 0,
        "summary_box_found": False,
        "annual_budget": {"ytd_spent": None, "budget": None, "remaining": None},
    }

    # Detect summary box
    if "DAILY RUN SUMMARY" in full_text:
        result["summary_box_found"] = True

    m = _PLAN_RE.search(full_text)
    if m:
        result["planned_opens"] = int(m.group(1))
        result["planned_closes"] = int(m.group(2))
        result["holds"] = int(m.group(3))

    m = _INV_RE.search(full_text)
    if m:
        result["crash_open"] = int(m.group(1))
        result["selloff_open"] = int(m.group(2))

    m = _PENDING_RE.search(full_text)
    if m:
        result["pending_crash"] = int(m.group(1))
        result["pending_selloff"] = int(m.group(2))

    m = _GATE_REASON_RE.search(full_text)
    if m:
        result["gate_reason"] = m.group(1).strip()

    m = _CANDIDATES_SEEN_RE.search(full_text)
    if m:
        result["candidates_seen"] = int(m.group(1))

    m = _QUOTE_OK_RE.search(full_text)
    if m:
        result["quote_only_validated"] = int(m.group(1))

    m = _ANNUAL_BUDGET_RE.search(full_text)
    if m:
        result["annual_budget"]["ytd_spent"] = _safe_float(m.group(1))
        result["annual_budget"]["budget"] = _safe_float(m.group(2))
        rem = m.group(3)
        result["annual_budget"]["remaining"] = None if rem == "N/A" else _safe_float(rem)

    return result


# ---------------------------------------------------------------------------
# ccc_report.py output parsers
# ---------------------------------------------------------------------------

# Section B lines of interest:
#   Crash open positions:                  3  (soft target=1, hard cap=5)
#   Selloff open positions:                0  (soft target=1, hard cap=3)
#   Pending (committed-not-filled):        1  (crash=1, selloff=0)
#   Crash premium at risk:                 $122.80 / $500.00  (25%)
#   Selloff premium at risk:               $0.00 / $300.00  (0%)
#   Total premium at risk:                 $122.80 / $750.00  (16%)
#   YTD premium spent:                     $245.00
#   Annual convexity budget:               $30000.00
#   Annual remaining budget:               $29755.00

_CRASH_OPEN_RE = re.compile(r"Crash open positions:\s+([\d]+)")
_SELLOFF_OPEN_RE = re.compile(r"Selloff open positions:\s+([\d]+)")
_PENDING_TOTAL_RE = re.compile(r"Pending \(committed-not-filled\):\s+(\d+)")
_PENDING_DETAIL_RE = re.compile(
    r"Pending \(committed-not-filled\):\s+\d+\s+\(crash=(\d+),\s*selloff=(\d+)\)"
)
_PAR_CRASH_RE = re.compile(r"Crash premium at risk:\s+\$?([\d,.]+)")
_PAR_SELLOFF_RE = re.compile(r"Selloff premium at risk:\s+\$?([\d,.]+)")
_PAR_TOTAL_RE = re.compile(r"Total premium at risk:\s+\$?([\d,.]+)")
_PAR_LEGACY_RE = re.compile(r"Premium at risk:\s+\$?([\d,.]+)")
_YTD_RE = re.compile(r"YTD premium spent:\s+\$?([\d,.]+)")
_ANNUAL_BUDGET_RPT_RE = re.compile(r"Annual convexity budget:\s+\$?([\d,.]+)")
_ANNUAL_REMAINING_RPT_RE = re.compile(r"Annual remaining budget:\s+\$?([\d,.]+)")

# Section C Plan summary
_PLAN_OPENS_RE = re.compile(r"Planned opens:\s+(\d+)")
_PLAN_CLOSES_RE = re.compile(r"Planned closes:\s+(\d+)")
_PLAN_HOLDS_RE = re.compile(r"Holds:\s+(\d+)")
_PLAN_TS_RE = re.compile(r"Plan timestamp:\s+(\S+\s+\S+)")
_PLAN_GATE_RE = re.compile(r"Gate reason:\s+(.+)")
_SECTION_A_COUNT_RE = re.compile(r"Total:\s+(\d+)\s+open position")


def parse_report_output(stdout: str) -> Dict[str, Any]:
    """
    Parse stdout from scripts/ccc_report.py.

    Returns a structured dict:
        {
            "crash_open":       int,
            "selloff_open":     int,
            "total_open":       int,
            "pending_total":    int,
            "pending_crash":    int,
            "pending_selloff":  int,
            "par_crash":        float | None,
            "par_selloff":      float | None,
            "par_total":        float | None,
            "ytd_spent":        float | None,
            "annual_budget":    float | None,
            "annual_remaining": float | None,
            "plan_timestamp":   str | None,
            "planned_opens":    int,
            "planned_closes":   int,
            "holds":            int,
            "gate_reason":      str | None,
            "open_count_from_table": int,
            "sections_found":   list[str],
        }
    """
    result: Dict[str, Any] = {
        "crash_open": 0,
        "selloff_open": 0,
        "total_open": 0,
        "pending_total": 0,
        "pending_crash": 0,
        "pending_selloff": 0,
        "par_crash": None,
        "par_selloff": None,
        "par_total": None,
        "ytd_spent": None,
        "annual_budget": None,
        "annual_remaining": None,
        "plan_timestamp": None,
        "planned_opens": 0,
        "planned_closes": 0,
        "holds": 0,
        "gate_reason": None,
        "open_count_from_table": 0,
        "sections_found": [],
    }

    if "SECTION A" in stdout:
        result["sections_found"].append("A")
    if "SECTION B" in stdout:
        result["sections_found"].append("B")
    if "SECTION C" in stdout:
        result["sections_found"].append("C")

    m = _CRASH_OPEN_RE.search(stdout)
    if m:
        result["crash_open"] = int(m.group(1))

    m = _SELLOFF_OPEN_RE.search(stdout)
    if m:
        result["selloff_open"] = int(m.group(1))

    result["total_open"] = result["crash_open"] + result["selloff_open"]

    m = _PENDING_TOTAL_RE.search(stdout)
    if m:
        result["pending_total"] = int(m.group(1))

    m = _PENDING_DETAIL_RE.search(stdout)
    if m:
        result["pending_crash"] = int(m.group(1))
        result["pending_selloff"] = int(m.group(2))

    m = _PAR_CRASH_RE.search(stdout)
    if m:
        result["par_crash"] = _safe_float(m.group(1).replace(",", ""))

    m = _PAR_SELLOFF_RE.search(stdout)
    if m:
        result["par_selloff"] = _safe_float(m.group(1).replace(",", ""))

    m = _PAR_TOTAL_RE.search(stdout)
    if m:
        result["par_total"] = _safe_float(m.group(1).replace(",", ""))

    # Legacy (no per-regime PAR)
    if result["par_total"] is None:
        m = _PAR_LEGACY_RE.search(stdout)
        if m:
            result["par_total"] = _safe_float(m.group(1).replace(",", ""))

    m = _YTD_RE.search(stdout)
    if m:
        result["ytd_spent"] = _safe_float(m.group(1).replace(",", ""))

    m = _ANNUAL_BUDGET_RPT_RE.search(stdout)
    if m:
        result["annual_budget"] = _safe_float(m.group(1).replace(",", ""))

    m = _ANNUAL_REMAINING_RPT_RE.search(stdout)
    if m:
        result["annual_remaining"] = _safe_float(m.group(1).replace(",", ""))

    m = _PLAN_OPENS_RE.search(stdout)
    if m:
        result["planned_opens"] = int(m.group(1))

    m = _PLAN_CLOSES_RE.search(stdout)
    if m:
        result["planned_closes"] = int(m.group(1))

    m = _PLAN_HOLDS_RE.search(stdout)
    if m:
        result["holds"] = int(m.group(1))

    m = _PLAN_TS_RE.search(stdout)
    if m:
        result["plan_timestamp"] = m.group(1).strip()

    m = _PLAN_GATE_RE.search(stdout)
    if m:
        result["gate_reason"] = m.group(1).strip()

    m = _SECTION_A_COUNT_RE.search(stdout)
    if m:
        result["open_count_from_table"] = int(m.group(1))

    return result


# ---------------------------------------------------------------------------
# Headline builder helpers
# ---------------------------------------------------------------------------

def build_status_headline(
    crash_open: int,
    selloff_open: int,
    par_crash: Optional[float],
    par_selloff: Optional[float],
    par_total: Optional[float],
    pending_total: int,
) -> str:
    """Generate a concise one-line status headline."""
    parts: List[str] = []

    if crash_open > 0:
        par_str = f" and ${par_crash:.2f} premium at risk" if par_crash is not None else ""
        parts.append(
            f"Crash sleeve has {crash_open} open position{'s' if crash_open != 1 else ''}"
            + par_str
        )
    else:
        parts.append("No crash positions open")

    if selloff_open > 0:
        par_str = f" and ${par_selloff:.2f} premium at risk" if par_selloff is not None else ""
        parts.append(
            f"selloff sleeve has {selloff_open} open position{'s' if selloff_open != 1 else ''}"
            + par_str
        )
    else:
        parts.append("no selloff positions")

    if par_total is not None and (crash_open + selloff_open) > 0:
        parts.append(f"total premium at risk ${par_total:.2f}")

    if pending_total > 0:
        parts.append(
            f"{pending_total} committed-not-filled pending"
        )

    return "; ".join(parts) + "."


def build_preview_headline(parsed: Dict[str, Any], actionability: str) -> str:
    """Generate a concise one-line preview headline from parsed daily.py output."""
    opens = parsed.get("planned_opens", 0)
    closes = parsed.get("planned_closes", 0)
    holds = parsed.get("holds", 0)
    gate = parsed.get("gate_reason")
    validated = parsed.get("quote_only_validated", 0)

    if actionability == "NO_ACTION":
        base = f"No new trade today (holds={holds}"
        if closes > 0:
            base += f", closes={closes}"
        base += ")"
        if gate:
            base += f". Gate: {gate}"
        return base + "."

    if actionability in ("CANDIDATE_AVAILABLE", "PAPER_ACTION_AVAILABLE"):
        base = (
            f"Daily preview: {opens} open planned, {closes} close(s), {holds} hold(s)"
        )
        if validated > 0:
            base += f"; {validated} intent(s) quote-only validated"
        if gate:
            base += f". Gate: {gate}"
        return base + "."

    if actionability == "ERROR":
        return "Preview failed — check errors."

    return "Preview complete."


def build_summarize_headline(
    status: Dict[str, Any],
    preview: Dict[str, Any],
    report: Dict[str, Any],
) -> str:
    """
    Combine status + preview + report into a single Command Center headline.

    Example:
        "No new trade today. Crash sleeve has 3 positions, remains under premium
         cap, and latest candidate was rejected on EV/convexity quality."
    """
    parts: List[str] = []

    # Trade activity sentence
    prev_details = preview.get("details", {})
    prev_ok = preview.get("ok", False)
    prev_opens = prev_details.get("planned_opens", 0) if prev_ok else 0
    prev_act = preview.get("actionability", "NO_ACTION")

    if not prev_ok or prev_act == "ERROR":
        parts.append("Preview unavailable")
    elif prev_opens > 0:
        validated = prev_details.get("quote_only_validated", 0)
        parts.append(
            f"{prev_opens} open(s) planned"
            + (f", {validated} validated" if validated else "")
        )
    else:
        parts.append("No new trade today")

    # Sleeve state
    status_details = status.get("details", {})
    crash_open = status_details.get("crash_open", 0)
    selloff_open = status_details.get("selloff_open", 0)
    par_total = status_details.get("par_total")
    par_crash_cap = status_details.get("par_crash_cap")
    par_total_cap = status_details.get("par_total_cap")

    if crash_open > 0 or selloff_open > 0:
        sleeve_parts = []
        if crash_open > 0:
            sleeve_parts.append(f"crash={crash_open}")
        if selloff_open > 0:
            sleeve_parts.append(f"selloff={selloff_open}")
        sleeve_str = " ".join(sleeve_parts)
        parts.append(f"sleeve open positions: {sleeve_str}")

        if par_total is not None:
            cap_ref = par_total_cap or par_crash_cap
            if cap_ref and par_total < cap_ref:
                parts.append(f"${par_total:.2f} PAR — under cap")
            elif par_total is not None:
                parts.append(f"${par_total:.2f} PAR")
    else:
        parts.append("no open positions")

    # Gate reason
    gate = prev_details.get("gate_reason") if prev_ok else None
    if gate and prev_opens == 0:
        parts.append(f"gate: {gate}")

    return "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> Optional[float]:
    """Convert val to float, return None on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
