#!/usr/bin/env python3
"""
CCC Portfolio Report  —  Phase 2A Closeout (Read-Only)

Provides a one-command PM-grade snapshot of allocator state from
existing artifact files.  This script never writes any files.

Sources (all read-only; missing files shown as empty / N/A):
  runs/allocator/positions.json              → open positions
  runs/allocator/allocator_commit_ledger.jsonl → committed intents
  runs/allocator/allocator_fills_ledger.jsonl  → fills / position_opened events
  runs/allocator/allocator_actions.json      → latest plan output (optional)
  configs/allocator_ccc_v1.yaml              → policy for annual budget (optional)

Usage:
  python scripts/ccc_report.py
  python scripts/ccc_report.py --policy configs/allocator_ccc_v1.yaml
  python scripts/ccc_report.py --positions path/to/positions.json
  python scripts/ccc_report.py --no-plan   # skip Section C
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path so allocator modules are importable
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import shared risk helpers (v2.0) — graceful fallback if module absent
try:
    from forecast_arb.allocator.risk import compute_portfolio_premium_at_risk as _compute_par
    _RISK_MODULE_AVAILABLE = True
except ImportError:
    _RISK_MODULE_AVAILABLE = False

    def _compute_par(positions):  # type: ignore[misc]
        """Fallback: inline computation when risk module unavailable."""
        result = {"crash": 0.0, "selloff": 0.0, "total": 0.0}
        for pos in positions:
            qty = int(pos.get("qty_open", 0) or 0)
            if qty <= 0:
                continue
            basis = (
                pos.get("entry_debit_net")
                or pos.get("entry_debit")
                or pos.get("entry_debit_gross")
            )
            if basis is None:
                continue
            par = float(basis) * qty
            regime = str(pos.get("regime", "") or "").lower()
            if regime in ("crash", "selloff"):
                result[regime] = round(result[regime] + par, 4)
            result["total"] = round(result["total"] + par, 4)
        return result

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_DEFAULT_POSITIONS     = Path("runs/allocator/positions.json")
_DEFAULT_COMMIT_LEDGER = Path("runs/allocator/allocator_commit_ledger.jsonl")
_DEFAULT_FILLS_LEDGER  = Path("runs/allocator/allocator_fills_ledger.jsonl")
_DEFAULT_ACTIONS       = Path("runs/allocator/allocator_actions.json")
_DEFAULT_POLICY        = Path("configs/allocator_ccc_v1.yaml")

# ---------------------------------------------------------------------------
# Data loaders (all read-only, graceful on missing files)
# ---------------------------------------------------------------------------

def load_positions(positions_path: Path) -> List[Dict[str, Any]]:
    """
    Load positions from positions.json.

    Returns empty list when the file does not exist or is unreadable.
    positions.json contains an array of position dicts written by ccc_reconcile.
    """
    if not positions_path.exists():
        return []
    try:
        with open(positions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # Some versions write {"positions": [...]}
        if isinstance(data, dict) and "positions" in data:
            return list(data["positions"])
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a JSONL file; return empty list on any error."""
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def compute_ytd_spent(commit_ledger_path: Path) -> float:
    """
    Compute YTD premium spent from the commit ledger.

    Reuses the same logic as budget_control.compute_premium_spent_ytd.
    Falls back to direct computation if the module is not importable.
    """
    try:
        from forecast_arb.allocator.budget_control import compute_premium_spent_ytd
        return compute_premium_spent_ytd(commit_ledger_path)
    except ImportError:
        pass

    # Fallback: inline implementation
    current_year = datetime.now(timezone.utc).date().year
    ytd = 0.0
    for rec in _read_jsonl(commit_ledger_path):
        if rec.get("action") != "OPEN":
            continue
        date_str = rec.get("date", "")
        if not date_str:
            continue
        try:
            rec_d = date.fromisoformat(str(date_str))
        except (ValueError, TypeError):
            continue
        if rec_d.year == current_year:
            ytd += float(rec.get("premium_spent", 0.0))
    return round(ytd, 2)


def compute_pending_count(
    commit_ledger_path: Path,
    fills_ledger_path: Path,
) -> Dict[str, int]:
    """
    Compute pending (committed-not-filled) counts by regime.

    Pending = OPEN intents in commit_ledger whose intent_id has NOT
    appeared as a POSITION_OPENED event in fills_ledger.

    Returns: {"crash": N, "selloff": N, "total": N}
    """
    # Collect filled intent_ids from fills_ledger
    filled_ids: set = set()
    for rec in _read_jsonl(fills_ledger_path):
        if rec.get("event_type") == "POSITION_OPENED":
            iid = rec.get("intent_id")
            if iid:
                filled_ids.add(str(iid))

    # Count committed-not-filled by regime
    pending: Dict[str, int] = {"crash": 0, "selloff": 0, "total": 0}
    for rec in _read_jsonl(commit_ledger_path):
        if rec.get("action") != "OPEN":
            continue
        iid = rec.get("intent_id")
        if iid and str(iid) in filled_ids:
            continue  # already filled
        regime = str(rec.get("regime", "")).lower()
        if regime in ("crash", "selloff"):
            pending[regime] = pending.get(regime, 0) + 1
        pending["total"] = pending.get("total", 0) + 1

    return pending


def load_annual_budget(policy_path: Path) -> Dict[str, Any]:
    """
    Load annual_convexity_budget from policy YAML.

    Returns {"budget": float|None, "enabled": bool}.
    Returns disabled when file missing or key absent.
    """
    if not policy_path.exists():
        return {"budget": None, "enabled": False}
    try:
        import yaml  # type: ignore[import]
        with open(policy_path, "r", encoding="utf-8") as f:
            policy = yaml.safe_load(f)
        if not isinstance(policy, dict):
            return {"budget": None, "enabled": False}
        raw = policy.get("budgets", {}).get("annual_convexity_budget")
        if raw is None:
            return {"budget": None, "enabled": False}
        val = float(raw)
        return {"budget": val, "enabled": val < 1e15}
    except Exception:
        return {"budget": None, "enabled": False}


def load_premium_at_risk_caps(policy_path: Path) -> Dict[str, Any]:
    """
    Load premium_at_risk_caps from policy YAML (v2.0).

    Returns:
        {
            "crash":   float | None,
            "selloff": float | None,
            "total":   float | None,
            "enabled": bool,
        }
    Returns disabled when file missing or section absent.
    """
    if not policy_path.exists():
        return {"crash": None, "selloff": None, "total": None, "enabled": False}
    try:
        import yaml  # type: ignore[import]
        with open(policy_path, "r", encoding="utf-8") as f:
            policy = yaml.safe_load(f)
        if not isinstance(policy, dict):
            return {"crash": None, "selloff": None, "total": None, "enabled": False}
        caps = policy.get("premium_at_risk_caps", {})
        if not caps:
            return {"crash": None, "selloff": None, "total": None, "enabled": False}
        return {
            "crash":   float(caps["crash"])   if "crash"   in caps else None,
            "selloff": float(caps["selloff"]) if "selloff" in caps else None,
            "total":   float(caps["total"])   if "total"   in caps else None,
            "enabled": True,
        }
    except Exception:
        return {"crash": None, "selloff": None, "total": None, "enabled": False}


def load_inventory_targets_and_caps(policy_path: Path) -> Dict[str, Any]:
    """
    Load inventory soft targets and hard caps from policy YAML (v2.1).

    Uses the same policy helper functions as the allocator for consistency.

    Returns:
        {
            "soft_targets": {"crash": int, "selloff": int},
            "hard_caps":    {"crash": int, "selloff": int},
            "enabled": bool,
        }
    Returns empty dicts when file missing or helpers unavailable.
    """
    if not policy_path.exists():
        return {"soft_targets": {}, "hard_caps": {}, "enabled": False}
    try:
        import yaml  # type: ignore[import]
        with open(policy_path, "r", encoding="utf-8") as f:
            policy = yaml.safe_load(f)
        if not isinstance(policy, dict):
            return {"soft_targets": {}, "hard_caps": {}, "enabled": False}

        # Use the same policy helpers as the allocator for consistency
        try:
            from forecast_arb.allocator.policy import (
                get_inventory_targets,
                get_inventory_hard_caps,
            )
            soft_targets = get_inventory_targets(policy)
            hard_caps = get_inventory_hard_caps(policy)
        except ImportError:
            # Fallback: read directly from YAML keys
            soft_targets = dict(policy.get("inventory_targets", {}))
            hard_caps = dict(policy.get("inventory_hard_caps", soft_targets))

        return {
            "soft_targets": soft_targets,
            "hard_caps":    hard_caps,
            "enabled":      bool(soft_targets or hard_caps),
        }
    except Exception:
        return {"soft_targets": {}, "hard_caps": {}, "enabled": False}


def load_actions(actions_path: Path) -> Optional[Dict[str, Any]]:
    """Load allocator_actions.json; return None when absent or unreadable."""
    if not actions_path.exists():
        return None
    try:
        with open(actions_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def compute_premium_at_risk(positions: List[Dict[str, Any]]) -> float:
    """
    Premium at risk = sum(entry_debit * qty_open) across all open positions.

    Priority: entry_debit_net > entry_debit > entry_debit_gross
    (build_positions_snapshot writes 'entry_debit_gross'; older pipeline
    tests write 'entry_debit' directly — both are accepted here.)
    Skips positions where all debit fields are None.
    """
    total = 0.0
    for pos in positions:
        qty = int(pos.get("qty_open", 0) or 0)
        if qty <= 0:
            continue
        # Prefer net (most accurate), then legacy 'entry_debit', then 'entry_debit_gross'
        basis = (
            pos.get("entry_debit_net")
            or pos.get("entry_debit")
            or pos.get("entry_debit_gross")
        )
        if basis is None:
            continue
        total += float(basis) * qty
    return round(total, 2)


def _fmt_strikes(strikes: Any) -> str:
    """Format strikes list or dict as 'Long/Short'."""
    if isinstance(strikes, list) and len(strikes) >= 2:
        return f"{strikes[0]:.0f}/{strikes[1]:.0f}"
    if isinstance(strikes, dict):
        ls = strikes.get("long_put", strikes.get("long", "?"))
        ss = strikes.get("short_put", strikes.get("short", "?"))
        try:
            return f"{float(ls):.0f}/{float(ss):.0f}"
        except (TypeError, ValueError):
            return "?/?"
    return str(strikes)


def _fmt_money(val: Optional[float], *, prefix: str = "$") -> str:
    if val is None:
        return "N/A"
    return f"{prefix}{val:,.2f}"


def _fmt_multiple(pos: Dict[str, Any]) -> str:
    basis = pos.get("entry_debit_net") or pos.get("entry_debit")
    mark = pos.get("mark_mid")
    if basis and mark and float(basis) > 0:
        return f"{float(mark) / float(basis):.2f}x"
    return "N/A"


# ---------------------------------------------------------------------------
# Printer helpers
# ---------------------------------------------------------------------------

_SEP  = "═" * 72
_DASH = "─" * 72


def _hdr(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_DASH)


def _footer() -> None:
    print(_SEP)


# ---------------------------------------------------------------------------
# Section A: Open positions table
# ---------------------------------------------------------------------------

def print_positions(positions: List[Dict[str, Any]]) -> None:
    """Print the open positions table (Section A)."""
    _hdr("SECTION A — OPEN POSITIONS")

    if not positions:
        print("  (no open positions)")
        _footer()
        return

    # Header
    print(
        f"  {'UNDERLIER':<10} {'REGIME':<8} {'EXPIRY':<10} {'STRIKES':<12} "
        f"{'QTY':>4} {'DEBIT':>8} {'MARK':>8} {'MULT':>7} {'MAX_GAIN':>10}"
    )
    print("  " + "─" * 68)

    low_weight_flags: List[str] = []

    for pos in positions:
        underlier = str(pos.get("underlier", "?"))[:10]
        regime    = str(pos.get("regime", "?"))[:8]
        expiry    = str(pos.get("expiry", "?"))[:10]
        strikes   = _fmt_strikes(pos.get("strikes"))
        qty       = int(pos.get("qty_open", 0) or 0)

        debit_val = (
            pos.get("entry_debit_net")
            or pos.get("entry_debit")
            or pos.get("entry_debit_gross")
        )
        debit_str = f"${float(debit_val):.2f}" if debit_val is not None else "N/A"

        mark_val  = pos.get("mark_mid")
        mark_str  = f"${float(mark_val):.2f}" if mark_val is not None else "N/A"

        mult_str  = _fmt_multiple(pos)

        max_gain  = pos.get("max_gain_per_contract")
        gain_str  = f"${float(max_gain):.0f}" if max_gain is not None else "N/A"

        # Task F: low remaining economic weight flag (reporting only, no gate change)
        # Triggers when: entry_debit present AND mark present AND mark/entry_debit < 0.25
        low_weight_tag = ""
        if debit_val is not None and mark_val is not None:
            try:
                ratio = float(mark_val) / float(debit_val)
                if ratio < 0.25:
                    low_weight_tag = " [LOW_WEIGHT]"
                    trade_id = pos.get("trade_id", expiry)
                    low_weight_flags.append(
                        f"{underlier.strip()} {expiry.strip()} {strikes} "
                        f"mark/entry={ratio:.2f}x"
                    )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        print(
            f"  {underlier:<10} {regime:<8} {expiry:<10} {strikes:<12} "
            f"{qty:>4} {debit_str:>8} {mark_str:>8} {mult_str:>7} {gain_str:>10}"
            + low_weight_tag
        )

    print(f"\n  Total: {len(positions)} open position(s)")
    if low_weight_flags:
        print(f"\n  ⚠  LOW_REMAINING_ECONOMIC_WEIGHT (mark/entry < 25%):")
        for flag in low_weight_flags:
            print(f"     {flag}")
    _footer()


# ---------------------------------------------------------------------------
# Section B: Portfolio summary
# ---------------------------------------------------------------------------

def print_portfolio_summary(
    positions: List[Dict[str, Any]],
    pending: Dict[str, int],
    ytd_spent: float,
    annual_budget: Dict[str, Any],
    par_caps: Optional[Dict[str, Any]] = None,
    inv_targets_caps: Optional[Dict[str, Any]] = None,
) -> None:
    """Print the portfolio summary (Section B).

    v2.0: Shows per-regime premium-at-risk vs configured caps when par_caps present.
    v2.1: Shows soft target and hard cap alongside position counts.
    """
    _hdr("SECTION B — PORTFOLIO SUMMARY")

    crash_open   = sum(1 for p in positions if str(p.get("regime","")).lower() == "crash")
    selloff_open = sum(1 for p in positions if str(p.get("regime","")).lower() == "selloff")

    # v2.0: Use shared risk helper for per-regime PAR (same logic as allocator gating)
    portfolio_par = _compute_par(positions)
    premium_risk = round(portfolio_par["total"], 2)

    def _row(label: str, value: str) -> None:
        print(f"  {label:<30}  {value}")

    def _par_row(label: str, par_val: float, cap_val: Optional[float]) -> None:
        """Format a premium-at-risk row with optional cap."""
        if cap_val is not None:
            pct = (par_val / cap_val * 100.0) if cap_val > 0 else 0.0
            print(
                f"  {label:<30}  "
                f"{_fmt_money(par_val)} / {_fmt_money(cap_val)}  "
                f"({pct:.0f}%)"
            )
        else:
            print(f"  {label:<30}  {_fmt_money(par_val)}")

    # v2.1: Show soft target and hard cap alongside position counts
    if inv_targets_caps and inv_targets_caps.get("enabled"):
        soft_tgts = inv_targets_caps.get("soft_targets", {})
        hard_caps_map = inv_targets_caps.get("hard_caps", {})
        crash_soft = soft_tgts.get("crash", "?")
        crash_hard = hard_caps_map.get("crash", "?")
        selloff_soft = soft_tgts.get("selloff", "?")
        selloff_hard = hard_caps_map.get("selloff", "?")
        _row("Crash open positions:",
             f"{crash_open}  (soft target={crash_soft}, hard cap={crash_hard})")
        _row("Selloff open positions:",
             f"{selloff_open}  (soft target={selloff_soft}, hard cap={selloff_hard})")
    else:
        _row("Crash open positions:",    str(crash_open))
        _row("Selloff open positions:",  str(selloff_open))

    _row("Pending (committed-not-filled):",
         f"{pending['total']}  (crash={pending['crash']}, selloff={pending['selloff']})")

    # v2.0: Per-regime PAR with caps (when configured) or total-only (legacy)
    if par_caps and par_caps.get("enabled"):
        crash_cap   = par_caps.get("crash")
        selloff_cap = par_caps.get("selloff")
        total_cap   = par_caps.get("total")
        _par_row("Crash premium at risk:",   round(portfolio_par["crash"],   2), crash_cap)
        _par_row("Selloff premium at risk:", round(portfolio_par["selloff"], 2), selloff_cap)
        _par_row("Total premium at risk:",   premium_risk,                       total_cap)
    else:
        _row("Premium at risk:",         _fmt_money(premium_risk))

    _row("YTD premium spent:",       _fmt_money(ytd_spent))

    if annual_budget["enabled"] and annual_budget["budget"] is not None:
        budget_val = annual_budget["budget"]
        remaining  = max(0.0, budget_val - ytd_spent)
        _row("Annual convexity budget:",  _fmt_money(budget_val))
        _row("Annual remaining budget:",  _fmt_money(remaining))
    else:
        _row("Annual convexity budget:",  "N/A (not configured)")
        _row("Annual remaining budget:",  "N/A")

    _footer()


# ---------------------------------------------------------------------------
# Section C: Latest plan summary
# ---------------------------------------------------------------------------

def print_plan_summary(actions_data: Optional[Dict[str, Any]]) -> None:
    """Print latest plan summary from allocator_actions.json (Section C)."""
    _hdr("SECTION C — LATEST PLAN SUMMARY")

    if actions_data is None:
        print("  (allocator_actions.json not found — run `daily.py` to generate)")
        _footer()
        return

    ts = actions_data.get("timestamp_utc", "?")

    # Action counts
    actions = actions_data.get("actions", [])
    opens   = sum(1 for a in actions if a.get("type") == "OPEN")
    closes  = sum(1 for a in actions
                  if a.get("type") in ("HARVEST_CLOSE", "ROLL_CLOSE"))
    holds   = sum(1 for a in actions if a.get("type") == "HOLD")

    def _row(label: str, value: str) -> None:
        print(f"  {label:<30}  {value}")

    _row("Plan timestamp:", ts[:19] if len(ts) >= 19 else ts)
    _row("Planned opens:",  str(opens))
    _row("Planned closes:", str(closes))
    _row("Holds:",          str(holds))

    # Gate trace
    trace = actions_data.get("open_gate_trace")
    if trace:
        reason = trace.get("reason", "N/A")
        _row("Gate reason:", reason)

        rejection_reasons = trace.get("rejection_reasons_top", {})
        if rejection_reasons:
            top5 = sorted(rejection_reasons.items(), key=lambda x: -x[1])[:5]
            top_str = ", ".join(f"{k}({v})" for k, v in top5)
            _row("Top rejections:", top5[0][0])  # primary
            if len(top5) > 1:
                print(f"  {'':30}  {top_str}")

        budget_blocked = trace.get("budget_blocked", False)
        if budget_blocked:
            _row("Budget blocked:", "YES")

    # OPEN action detail
    for action in actions:
        if action.get("type") == "OPEN":
            cid = action.get("candidate_id", "?")[:24]
            prem = action.get("premium")
            qty  = action.get("qty")
            rc   = action.get("reason_codes", [])
            src = next((r.split(":")[-1] for r in rc if r.startswith("PREMIUM_USED:")), "")
            layer = action.get("layer", "")
            fragile = action.get("fragile")
            prem_str = f"${float(prem):.0f}/c[{src}]" if prem else "?"
            layer_str = f" layer={layer}" if layer else ""
            frag_str = " FRAGILE" if fragile else (" robust" if fragile is False else "")
            print(f"  OPEN: {cid}  qty={qty}  {prem_str}{layer_str}{frag_str}")

    _footer()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_report(
    positions_path:     Path = _DEFAULT_POSITIONS,
    commit_ledger_path: Path = _DEFAULT_COMMIT_LEDGER,
    fills_ledger_path:  Path = _DEFAULT_FILLS_LEDGER,
    actions_path:       Path = _DEFAULT_ACTIONS,
    policy_path:        Path = _DEFAULT_POLICY,
    show_plan:          bool = True,
) -> None:
    """
    Run the full CCC report.  Prints three sections to stdout.

    This function is importable for testing; all I/O is via print().
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{_SEP}")
    print(f"  CCC PORTFOLIO REPORT  —  {now_str}")
    print(f"  Positions : {positions_path}")
    print(f"  Ledger    : {commit_ledger_path}")
    print(_SEP)

    # --- Load data ---
    positions        = load_positions(positions_path)
    pending          = compute_pending_count(commit_ledger_path, fills_ledger_path)
    ytd_spent        = compute_ytd_spent(commit_ledger_path)
    annual_budget    = load_annual_budget(policy_path)
    par_caps         = load_premium_at_risk_caps(policy_path)           # v2.0
    inv_targets_caps = load_inventory_targets_and_caps(policy_path)     # v2.1
    actions_data     = load_actions(actions_path) if show_plan else None

    # --- Print sections ---
    print_positions(positions)
    print_portfolio_summary(
        positions, pending, ytd_spent, annual_budget,
        par_caps=par_caps,
        inv_targets_caps=inv_targets_caps,
    )
    if show_plan:
        print_plan_summary(actions_data)

    print(f"\n{_SEP}")
    print("  Report complete. All data read-only — no files were modified.")
    print(_SEP)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CCC Portfolio Report — read-only PM visibility"
    )
    parser.add_argument(
        "--positions",
        default=str(_DEFAULT_POSITIONS),
        help=f"Path to positions.json (default: {_DEFAULT_POSITIONS})",
    )
    parser.add_argument(
        "--commit-ledger",
        default=str(_DEFAULT_COMMIT_LEDGER),
        dest="commit_ledger",
        help=f"Path to allocator_commit_ledger.jsonl (default: {_DEFAULT_COMMIT_LEDGER})",
    )
    parser.add_argument(
        "--fills-ledger",
        default=str(_DEFAULT_FILLS_LEDGER),
        dest="fills_ledger",
        help=f"Path to allocator_fills_ledger.jsonl (default: {_DEFAULT_FILLS_LEDGER})",
    )
    parser.add_argument(
        "--actions",
        default=str(_DEFAULT_ACTIONS),
        help=f"Path to allocator_actions.json (default: {_DEFAULT_ACTIONS})",
    )
    parser.add_argument(
        "--policy",
        default=str(_DEFAULT_POLICY),
        help=f"Path to policy YAML for annual budget (default: {_DEFAULT_POLICY})",
    )
    parser.add_argument(
        "--no-plan",
        action="store_true",
        default=False,
        dest="no_plan",
        help="Suppress Section C (latest plan summary)",
    )
    args = parser.parse_args()

    run_report(
        positions_path=Path(args.positions),
        commit_ledger_path=Path(args.commit_ledger),
        fills_ledger_path=Path(args.fills_ledger),
        actions_path=Path(args.actions),
        policy_path=Path(args.policy),
        show_plan=not args.no_plan,
    )


if __name__ == "__main__":
    main()
