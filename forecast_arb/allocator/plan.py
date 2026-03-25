"""
CCC v1 Allocator - Plan orchestrator.

Entry point: run_allocator_plan()

Orchestration sequence:
  1.  Load & validate policy
  2.  Compute budget state (from COMMIT ledger) — "before" (v1.4: commit-only)
  3.  Compute inventory state (from PLAN ledger) — "before"
  4.  Reconcile open positions (from PLAN ledger)
  5.  Populate marks (from candidates data if available)
  6.  Generate harvest/roll actions (with close-liquidity guard)
  7.  Simulate inventory after closes → "inventory_mid"
  7b. Reserve pending OPEN intents from intents_dir (Task D v1.4)
  8.  Generate open actions using inventory_mid (Task 4 gate)
  9.  Apply daily action caps, priority: HARVEST > ROLL > OPEN (Task 6)
  10. Compute inventory_after (Task 4 final)
  11. Compute planned_spend for budget before/after (Task 1)
  12. Build HOLD if no other actions possible
  13. Write allocator_actions.json
  14. Write close-intent JSON files for HARVEST/ROLL (Task 7)
  15. Write OPEN intent files (pure OrderIntent schema)
  16. Append summary record to allocator_PLAN_ledger.jsonl (plan-only, v1.4)
  17. Print PM-grade console summary

Output files:
  runs/allocator/allocator_actions.json          (always written, even if all HOLD)
  runs/allocator/allocator_plan_ledger.jsonl     (append-only — plan records only)
  runs/allocator/allocator_commit_ledger.jsonl   (written by ccc_execute.py ONLY)
  intents/allocator/<trade_id>.json              (one per HARVEST/ROLL action, Task 7)
  intents/allocator/OPEN_<candidate_id>.json     (one per OPEN action, v1.3 Task A/C)

v1.3 additions:
  - build_order_intent_from_candidate()  shared helper → valid OrderIntent dict
  - _write_open_intents()  uses the builder; writes pure executable schema only
  - _simulate_inventory_after_actions()  uses action.regime directly (no heuristic)

v1.4 additions:
  - Tasks A/B: split plan vs commit ledger; budget reads commit-only
  - Task D: _scan_pending_open_intents() prevents double-OPEN on same-day reruns
  - Task E: plan ledger entries now carry regime/underlier/expiry/strikes from action metadata
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .budget import append_ledger_record, compute_budget_state
from .budget_control import compute_premium_spent_ytd
from .harvest import generate_harvest_actions, generate_roll_discipline_actions
from .inventory import (
    compute_inventory_state,
    compute_inventory_state_with_positions,
    compute_inventory_state_full,
)
from .marks import populate_marks_from_candidates
from .open_plan import generate_open_actions
from .policy import (
    get_actions_path,
    get_close_liquidity_guard,
    get_commit_ledger_path,
    get_fills_ledger_path,
    get_intents_dir,
    get_inventory_hard_caps,
    get_inventory_targets,
    get_ledger_path,
    get_limits,
    get_plan_ledger_path,
    get_positions_path,
    get_premium_at_risk_caps,
    load_policy,
)
from .reconcile import reconcile_positions
from .types import ActionType, AllocatorAction, AllocatorPlan, BudgetState, InventoryState, SleevePosition

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema-aware candidate loader (Task 1)
# ---------------------------------------------------------------------------

def _load_candidates_any_schema(path: str) -> list:
    """
    Load a flat list of candidate dicts from *any* supported file schema.

    Supported schemas
    -----------------
    1. Campaign output  (recommended.json):
          {"selected": [...]}
    2. Flat candidates  (candidates_flat.json or similar):
          {"candidates": [...]}
    3. Raw list:
          [{...}, ...]

    Returns
    -------
    list[dict]  — flat list (may be empty; caller is responsible for fail-loud check)

    Raises
    ------
    FileNotFoundError  — if the file does not exist at *path*
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Allocator candidates_path not found: {path}")

    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # 1) Campaign output schema (recommended.json)
    if isinstance(data, dict) and "selected" in data and isinstance(data["selected"], list):
        return data["selected"]

    # 2) Flat candidates schema (candidates_flat.json or similar)
    if isinstance(data, dict) and "candidates" in data and isinstance(data["candidates"], list):
        return data["candidates"]

    # 3) Raw list schema
    if isinstance(data, list):
        return data

    return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_allocator_plan(
    policy_path: str,
    candidates_path: Optional[str] = None,
    signals: Optional[Dict[str, Any]] = None,
    verbose: bool = False,
    dry_run: bool = False,
) -> AllocatorPlan:
    """
    Run the full CCC v1 allocator plan.

    Args:
        policy_path:     Path to allocator_ccc_v1.yaml
        candidates_path: Path to recommended.json or candidates_flat.json
                         (None → no OPEN actions generated)
        signals:         Optional conditioning signals for kicker eligibility:
                         {"conditioning_confidence": 0.7, "vix_percentile": 25.0,
                          "credit_stress_elevated": False}
        verbose:         Print detailed output
        dry_run:         Compute plan but do NOT write any files

    Returns:
        AllocatorPlan dataclass with all actions

    Raises:
        FileNotFoundError: if policy or candidates file not found
        PolicyError: if policy config invalid
    """
    # ---- 1. Load policy ----
    policy = load_policy(policy_path)
    policy_id = str(policy.get("policy_id", "ccc_v1"))
    # v1.4 Task A/B: split plan ledger (inventory/reconcile/writes) from commit ledger (budget)
    plan_ledger_path = get_plan_ledger_path(policy)
    commit_ledger_path = get_commit_ledger_path(policy)
    actions_path = get_actions_path(policy)
    intents_dir = get_intents_dir(policy)
    limits = get_limits(policy)

    # ---- 2. Budget state ("before" actuals from COMMIT ledger — v1.4 Task B) ----
    # Only intents staged via ccc_execute.py appear in the commit ledger.
    # Running CCC multiple times does NOT increase spent_today_before.
    budget = compute_budget_state(policy, commit_ledger_path, signals=signals or {})

    # ---- 2b. Phase 2A Task A: annual convexity budget ----
    # annual_convexity_budget and spent_ytd are attached to budget here so that
    # open_plan._evaluate_candidate() can gate against the annual cap.
    # Backward-compat: if key absent from YAML → float('inf') → gate disabled.
    _annual_budget_raw = policy.get("budgets", {}).get("annual_convexity_budget")
    budget.annual_convexity_budget = (
        float(_annual_budget_raw) if _annual_budget_raw is not None else float("inf")
    )
    if budget.annual_budget_enabled:
        budget.spent_ytd = compute_premium_spent_ytd(commit_ledger_path)

    # ---- 3. Reconcile open positions (PLAN ledger) ----
    positions = reconcile_positions(plan_ledger_path)

    # ---- 4. Inventory state: actual + pending (v1.8) ----
    # v1.7: positions.json is authoritative for actual open positions.
    # v1.8: pending = committed (commit_ledger) − filled (fills_ledger); replaces filesystem scan.
    positions_path = get_positions_path(policy)
    fills_ledger_path = get_fills_ledger_path(policy)

    inv_actual, pending_by_regime, inv_effective = compute_inventory_state_full(
        policy=policy,
        ledger_path=plan_ledger_path,
        commit_ledger_path=commit_ledger_path,
        fills_ledger_path=fills_ledger_path,
        positions_path=positions_path,
    )
    inventory_before = inv_actual  # backward-compat alias

    log.info(
        f"[ALLOCATOR] inventory.actual crash={inv_actual.crash_open}/{inv_actual.crash_target}  "
        f"selloff={inv_actual.selloff_open}/{inv_actual.selloff_target}"
    )
    if any(pending_by_regime.values()):
        log.info(
            f"[ALLOCATOR] inventory.pending (commit-fills): "
            f"crash={pending_by_regime.get('crash', 0)}  "
            f"selloff={pending_by_regime.get('selloff', 0)}"
        )

    # ---- 5. Load candidates (schema-aware) & populate marks ----
    # Task 1: _load_candidates_any_schema() handles recommended.json {"selected":[...]},
    #         candidates_flat.json {"candidates":[...]}, and raw list [...].
    # Task 2: Raise loud ValueError if the file resolves to zero candidates.
    # Task 3: Emit a mandatory INFO log with the loaded count.
    candidates_data: Dict[str, Any] = {}
    candidates: List[Dict[str, Any]] = []
    if candidates_path:
        candidates = _load_candidates_any_schema(candidates_path)  # raises FileNotFoundError if missing

        # Task 2 — fail loud: a HOLD with empty evaluated list is a pipeline bug
        if not candidates:
            raise ValueError(
                f"Allocator received zero candidates from {candidates_path}. "
                "Schema mismatch or upstream failure. "
                "Expected recommended.json{selected:[...]} or {candidates:[...]} or a list."
            )

        # Task 3 — mandatory operational log (never silent about what was loaded)
        log.info(f"[ALLOCATOR] Loaded {len(candidates)} candidate(s) from {candidates_path}")

        # Normalise to a consistent dict keyed by "selected" so all downstream
        # consumers (generate_open_actions, _write_open_intents, _determine_hold_reasons)
        # continue to work without modification.
        candidates_data = {"selected": candidates}
        positions = populate_marks_from_candidates(positions, candidates)

    # ---- 6. Harvest/roll actions (with close-liquidity guard) ----
    harvest_actions = generate_harvest_actions(positions, policy)

    # ---- 6b. v1.9 Task C: Roll-forward discipline (separate from time-stop harvest) ----
    already_actioned = {a.trade_id for a in harvest_actions if a.trade_id}
    roll_discipline_actions = generate_roll_discipline_actions(
        positions, policy, skip_trade_ids=already_actioned
    )
    harvest_actions = harvest_actions + roll_discipline_actions

    # ---- 7. Simulate inventory after closes → use for open gating (Task 4) ----
    inventory_after_closes = _simulate_inventory_after_actions(
        inv=inventory_before,
        actions=harvest_actions,
        positions=positions,
    )

    # ---- 7b. v1.8: Ledger-based pending reservation (replaces filesystem timestamp scan) ----
    # pending_by_regime was computed in step 4 via compute_inventory_state_full().
    # "Pending" = committed (commit_ledger) minus filled (fills_ledger as POSITION_OPENED).
    # ORDER_STAGED rows remain pending (not filled) until POSITION_OPENED appears.
    # This is durable and does not depend on filesystem timestamps or OPEN_*.json scans.
    pending_intents = pending_by_regime  # alias for trace_notes / backward compat
    crash_reserved = pending_by_regime.get("crash", 0)
    selloff_reserved = pending_by_regime.get("selloff", 0)

    # v1.9 Task C: ROLL_CLOSE frees a slot for a replacement OPEN even when pending exists.
    # Subtract roll closes from pending inflation so the replacement can proceed.
    _pos_by_tid = {p.trade_id: p for p in positions}
    _roll_close_crash = sum(
        1 for a in harvest_actions
        if a.type == ActionType.ROLL_CLOSE
        and _pos_by_tid.get(a.trade_id or "") is not None
        and _pos_by_tid[a.trade_id].regime.lower() == "crash"
    )
    _roll_close_selloff = sum(
        1 for a in harvest_actions
        if a.type == ActionType.ROLL_CLOSE
        and _pos_by_tid.get(a.trade_id or "") is not None
        and _pos_by_tid[a.trade_id].regime.lower() == "selloff"
    )
    adj_crash_reserved = max(0, crash_reserved - _roll_close_crash)
    adj_selloff_reserved = max(0, selloff_reserved - _roll_close_selloff)

    if crash_reserved or selloff_reserved:
        log.info(
            f"v1.8: Pending intents (commit-fills ledger) — "
            f"crash={crash_reserved}, selloff={selloff_reserved}. "
            f"Roll closes: crash={_roll_close_crash}, selloff={_roll_close_selloff}. "
            f"Adjusted pending: crash={adj_crash_reserved}, selloff={adj_selloff_reserved}."
        )
    if adj_crash_reserved or adj_selloff_reserved:
        inventory_after_closes = InventoryState(
            crash_target=inventory_after_closes.crash_target,
            crash_open=inventory_after_closes.crash_open + adj_crash_reserved,
            selloff_target=inventory_after_closes.selloff_target,
            selloff_open=inventory_after_closes.selloff_open + adj_selloff_reserved,
        )

    # ---- 8. Open actions using post-close inventory ----
    # NOTE: generate_open_actions mutates budget.spent_today/week/month as an
    # internal per-regime guard (prevents double-booking across regimes in one call).
    # We snapshot and restore after the call so that 'spent_today' always reflects
    # ledger actuals only — planned_spend is tracked separately (Task 1).
    open_actions: List[AllocatorAction] = []
    # Task 2 (CCC v1.2): collect per-candidate gate evaluations for trace
    _rejection_log: List[Dict[str, Any]] = []
    if candidates_data:
        _snap_today = budget.spent_today
        _snap_week = budget.spent_week
        _snap_month = budget.spent_month
        open_actions = generate_open_actions(
            candidates_data=candidates_data,
            policy=policy,
            budget=budget,
            inventory=inventory_after_closes,   # Task 4: pass post-close inventory
            rejection_log=_rejection_log,       # Task 2: collect evaluations
            positions=positions,                # Phase 2A Task C: strike diversity
        )
        # Restore to ledger actuals; planned_spend set in step 11
        budget.spent_today = _snap_today
        budget.spent_week = _snap_week
        budget.spent_month = _snap_month

    # ---- 9. Apply daily action caps, priority: HARVEST > ROLL > OPEN (Task 6) ----
    approved_actions, capped_holds = _apply_action_caps(
        harvest_actions=harvest_actions,
        open_actions=open_actions,
        limits=limits,
    )

    # ---- 10. Compute inventory_after (Task 4 final) ----
    inventory_after = _simulate_inventory_after_actions(
        inv=inventory_before,
        actions=[a for a in approved_actions if a.type != ActionType.HOLD],
        positions=positions,
    )

    # ---- 11. Compute planned_spend for budget before/after (Task 1) ----
    planned_open_spend = sum(
        (a.premium or 0) * (a.qty or 1)
        for a in approved_actions
        if a.type == ActionType.OPEN
    )
    # Close actions don't add to premium spend (they receive credit).
    # planned_spend = opens only.
    budget.planned_spend_today = planned_open_spend
    budget.planned_spend_week = planned_open_spend
    budget.planned_spend_month = planned_open_spend

    # ---- 12. Compile all actions; add HOLD if nothing else ----
    all_actions: List[AllocatorAction] = []
    notes: List[str] = []

    all_actions.extend(approved_actions)
    all_actions.extend(capped_holds)

    if not any(a.type != ActionType.HOLD for a in all_actions):
        # Nothing actionable except HOLDs from caps — add a primary HOLD
        hold_reasons = _determine_hold_reasons(
            budget=budget,
            inventory=inventory_after_closes,
            candidates_data=candidates_data,
            rejection_log=_rejection_log,
        )
        # Only add HOLD if there are no already-existing HOLDs with substantive reason
        existing_holds = [a for a in all_actions if a.type == ActionType.HOLD]
        if not existing_holds:
            all_actions.append(
                AllocatorAction(
                    type=ActionType.HOLD,
                    reason_codes=hold_reasons,
                )
            )
            notes.append("No actionable harvest or open trades today.")

    # ---- 12b. Build open_gate_trace — institutional-grade diagnostics (v1.6 Task 2) ----
    # Produced whenever inventory is below target but no OPEN action was planned.
    # Provides one-glance root cause + full candidate evaluation details.
    open_gate_trace: Optional[Dict[str, Any]] = None
    needs_crash = inventory_before.needs_open("crash")
    needs_selloff = inventory_before.needs_open("selloff")
    has_open = any(a.type == ActionType.OPEN for a in all_actions)
    if (needs_crash or needs_selloff) and not has_open:
        # --- Aggregated evaluation counts ---
        evaluated_count = len(_rejection_log)
        rejected_count = sum(
            1 for e in _rejection_log if e.get("result") == "REJECTED"
        )
        approved_count = sum(
            1 for e in _rejection_log if e.get("result") == "APPROVED"
        )

        # --- Top rejection reasons (aggregated counts) ---
        rejection_reasons_top: Dict[str, int] = {}
        for entry in _rejection_log:
            if entry.get("result") == "REJECTED":
                primary = entry.get("primary_reason", "UNKNOWN") or "UNKNOWN"
                rejection_reasons_top[primary] = rejection_reasons_top.get(primary, 0) + 1

        # --- v1.6: Derive tier_used + thresholds_used from rejection_log ---
        # First entry for each regime wins (candidates sorted by EV desc, so first = best)
        tier_used: Dict[str, str] = {}
        thresholds_used: Dict[str, Dict[str, Any]] = {}
        for entry in _rejection_log:
            regime_key = entry.get("regime", "")
            if regime_key and regime_key not in tier_used:
                p_src_entry = (
                    "external"
                    if str(entry.get("tier", "")).endswith("_when_empty")
                       and False  # p_src not stored on entry; use ev_threshold_used heuristic
                    else "implied"
                )
                # Determine ev_src from ratio of thresholds
                ev_thresh = entry.get("ev_threshold_used", 0.0) or 0.0
                ev_ext = entry.get("ev_threshold_used")  # may differ if p_src == external
                tier_used[regime_key] = entry.get("tier", "unknown")
                thresholds_used[regime_key] = {
                    "ev": entry.get("ev_threshold_used", 0.0),
                    "conv": entry.get("conv_threshold_used", 0.0),
                    "ev_src": "implied",  # default; external is rare and visible in reason
                }

        # --- Gate block flags ---
        # Phase 2A Task A: also flag as budget_blocked when annual cap exceeded
        _annual_cap_breached = (
            budget.annual_budget_enabled
            and budget.spent_ytd >= budget.annual_convexity_budget
        )
        budget_blocked = (
            budget.remaining_today <= 0
            or budget.remaining_week <= 0
            or budget.remaining_month <= 0
            or _annual_cap_breached
        )
        inventory_blocked = (
            not inventory_after_closes.needs_open("crash")
            and not inventory_after_closes.needs_open("selloff")
        )

        # --- Canonical trace reason (spec: one of CANDIDATES_FILE_MISSING /
        #     CANDIDATES_EMPTY / ALL_REJECTED:<reason>) ---
        if candidates_path is None:
            trace_reason = "CANDIDATES_FILE_MISSING"
        elif not candidates:
            trace_reason = "CANDIDATES_EMPTY"
        elif rejection_reasons_top:
            top_reason = max(
                rejection_reasons_top,
                key=lambda k: rejection_reasons_top[k]
            )
            trace_reason = f"ALL_REJECTED:{top_reason}"
        elif budget_blocked:
            trace_reason = "BUDGET_BLOCKED"
        elif inventory_blocked:
            trace_reason = "INVENTORY_AT_TARGET"
        elif not _rejection_log:
            trace_reason = "NO_QUALIFYING_TRADES"
        else:
            trace_reason = "NO_QUALIFYING_TRADES"

        # --- Operator notes ---
        trace_notes: List[str] = []
        if candidates_path is None:
            trace_notes.append(
                "No candidates file was resolved before calling allocator. "
                "Check daily.py _resolve_candidates_path() — "
                "run_campaign_mode must write recommended.json."
            )
        if any(pending_intents.values()):
            pending_lines = [
                f"{regime}={count}"
                for regime, count in pending_intents.items()
                if count > 0
            ]
            trace_notes.append(
                f"Inventory inflated by pending OPEN intents today: "
                f"{', '.join(pending_lines)}. "
                "These intents count against inventory but NOT against commit ledger (spent=0)."
            )

        kicker_note_str = (
            f"kicker OFF — baseline caps active "
            f"(daily=${budget.daily_soft_cap:.0f}, "
            f"weekly=${budget.weekly_soft_cap:.0f}) — NOT a gate blocker"
            if not budget.kicker_enabled
            else "kicker ON — kicker caps active"
        )

        open_gate_trace = {
            # Primary diagnostic — visible at a glance
            "reason": trace_reason,
            # Candidates plumbing
            "candidates_path": candidates_path,
            "candidates_seen": len(candidates),
            "candidates_evaluated_count": evaluated_count,
            "candidates_rejected_count": rejected_count,
            "rejection_reasons_top": rejection_reasons_top,
            "selected_for_open_count": approved_count,
            # Gate block flags
            "budget_blocked": budget_blocked,
            "inventory_blocked": inventory_blocked,
            "kicker_blocked": False,  # kicker NEVER blocks; changes cap SIZE only
            "kicker_reasons": list(budget.kicker_reasons),
            # Operator notes
            "notes": trace_notes,
            # v1.6 Task C: inventory-aware tier + thresholds used (one entry per regime)
            "inventory_needs_open": {
                "crash": needs_crash,
                "selloff": needs_selloff,
            },
            "tier_used": tier_used,
            "thresholds_used": thresholds_used,
            # Full per-candidate evaluations (enriched with tier/delta fields in v1.6)
            "candidates_evaluated": _rejection_log,
            "kicker_note": kicker_note_str,
        }

        # Pre-eval filter note: candidates loaded but none reached evaluation loop
        if candidates and not _rejection_log:
            open_gate_trace["pre_eval_filter_note"] = (
                "No candidates reached evaluation loop. "
                "This indicates filtering removed all candidates; check filter rules."
            )

    # ---- 13. Build plan ----
    timestamp = datetime.now(timezone.utc).isoformat()

    plan = AllocatorPlan(
        timestamp_utc=timestamp,
        policy_id=policy_id,
        budgets=budget,
        inventory=inventory_before,          # Task 4: "before" state (actual)
        inventory_after=inventory_after,     # Task 4: "after" state (planned)
        positions=positions,
        actions=all_actions,
        notes=notes,
        open_gate_trace=open_gate_trace,     # v1.6: institutional-grade gate trace
        pending_open_intents=pending_by_regime,  # v1.8: from commit-fills ledger
        inv_effective=inv_effective,             # v1.8: actual + pending (for gating)
    )

    # ---- 14. Write outputs ----
    # IMPORTANT: write intent files FIRST so that action.intent_path fields are
    # populated before _write_actions_json() serialises the plan to JSON.
    if not dry_run:
        # Task 7: write close intents for HARVEST/ROLL actions (sets intent_path on actions)
        _write_close_intents(plan, intents_dir)
        # v1.3 Task A/C: write OPEN intents as pure OrderIntent to intents/allocator/
        _write_open_intents(plan, intents_dir, candidates_data=candidates_data, policy=policy)
        # Now write JSON — all intent_path fields are populated
        _write_actions_json(plan, actions_path)
        # v1.4 Task A: write summary to PLAN ledger only (not commit ledger)
        _append_ledger_summary(plan, plan_ledger_path)

    # ---- 15. Print PM console ----
    _print_pm_summary(plan, verbose=verbose)

    return plan


# ---------------------------------------------------------------------------
# Inventory simulation (Task 4)
# ---------------------------------------------------------------------------

def _simulate_inventory_after_actions(
    inv: InventoryState,
    actions: List[AllocatorAction],
    positions: List[SleevePosition],
) -> InventoryState:
    """
    Deterministically simulate inventory changes from a list of actions.

    Rules:
      HARVEST_CLOSE / ROLL_CLOSE → decrement regime's open count (floor 0)
      OPEN                       → increment regime's open count
      HOLD                       → no change

    Position regime is resolved via trade_id→position lookup.
    For OPEN actions, regime is extracted from candidate_id / reason_codes.
    """
    crash_open = inv.crash_open
    selloff_open = inv.selloff_open

    # Build trade_id→regime lookup from positions
    pos_regime: Dict[str, str] = {p.trade_id: p.regime.lower() for p in positions}

    for action in actions:
        if action.type in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE):
            regime = pos_regime.get(action.trade_id or "", "")
            if regime == "crash":
                crash_open = max(0, crash_open - 1)
            elif regime == "selloff":
                selloff_open = max(0, selloff_open - 1)

        elif action.type == ActionType.OPEN:
            # v1.3 Task B/C: use explicit regime field populated by open_plan._evaluate_candidate
            # Fall back to heuristic only for legacy actions lacking the field
            regime = (action.regime or _extract_regime_from_action(action)).lower()
            if regime == "crash":
                crash_open += 1
            elif regime == "selloff":
                selloff_open += 1

    return InventoryState(
        crash_target=inv.crash_target,
        crash_open=crash_open,
        selloff_target=inv.selloff_target,
        selloff_open=selloff_open,
    )


# ---------------------------------------------------------------------------
# Daily action caps (Task 6)
# ---------------------------------------------------------------------------

def _apply_action_caps(
    harvest_actions: List[AllocatorAction],
    open_actions: List[AllocatorAction],
    limits: Dict[str, int],
) -> tuple[List[AllocatorAction], List[AllocatorAction]]:
    """
    Enforce daily action caps with priority: HARVEST_CLOSE > ROLL_CLOSE > OPEN.

    Actions beyond the cap are converted to HOLD with reason DAILY_ACTION_LIMIT.

    Returns:
        (approved_actions, capped_holds)
    """
    max_close = limits.get("max_close_actions_per_day", 999)
    max_open = limits.get("max_open_actions_per_day", 999)

    # Separate HOLD actions from the guard (they pass through unchanged)
    guard_holds = [a for a in harvest_actions if a.type == ActionType.HOLD]
    close_candidates = [a for a in harvest_actions
                        if a.type in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE)]

    # Priority within closes: HARVEST_CLOSE first, then ROLL_CLOSE
    harvests = [a for a in close_candidates if a.type == ActionType.HARVEST_CLOSE]
    rolls = [a for a in close_candidates if a.type == ActionType.ROLL_CLOSE]
    ordered_closes = harvests + rolls

    approved: List[AllocatorAction] = list(guard_holds)
    capped: List[AllocatorAction] = []

    # Apply close cap
    for a in ordered_closes:
        close_count = sum(1 for x in approved
                          if x.type in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE))
        if close_count < max_close:
            approved.append(a)
        else:
            capped.append(AllocatorAction(
                type=ActionType.HOLD,
                trade_id=a.trade_id,
                reason_codes=[
                    "DAILY_ACTION_LIMIT",
                    f"MAX_CLOSE_ACTIONS_{max_close}_REACHED",
                    f"BLOCKED_ACTION:{a.type}",
                ],
            ))

    # Apply open cap
    for a in open_actions:
        open_count = sum(1 for x in approved if x.type == ActionType.OPEN)
        if open_count < max_open:
            approved.append(a)
        else:
            capped.append(AllocatorAction(
                type=ActionType.HOLD,
                trade_id=a.trade_id,
                candidate_id=a.candidate_id,
                reason_codes=[
                    "DAILY_ACTION_LIMIT",
                    f"MAX_OPEN_ACTIONS_{max_open}_REACHED",
                    f"BLOCKED_ACTION:{a.type}",
                ],
            ))

    return approved, capped


# ---------------------------------------------------------------------------
# Close intent emission (Task 7)
# ---------------------------------------------------------------------------

def _write_close_intents(plan: AllocatorPlan, intents_dir: Path) -> None:
    """
    For each HARVEST_CLOSE or ROLL_CLOSE action, write a close-intent JSON file.

    Since execute_trade does not support spread closes, intent files are generated
    for manual review/execution. Each action gets intent_path set and
    MANUAL_CLOSE_REQUIRED added to reason codes.
    """
    pos_by_id = {p.trade_id: p for p in plan.positions}

    for action in plan.actions:
        if action.type not in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE):
            continue

        pos = pos_by_id.get(action.trade_id or "")
        if pos is None:
            # No matching position found; mark as manual with null path
            action.reason_codes.append("MANUAL_CLOSE_REQUIRED")
            # intent_path stays None
            continue

        # Build intent dict
        intent = {
            "intent_type": "CLOSE",
            "action_type": action.type,
            "policy_id": plan.policy_id,
            "timestamp_utc": plan.timestamp_utc,
            "trade_id": action.trade_id,
            "candidate_id": pos.candidate_id,
            "underlier": pos.underlier,
            "expiry": pos.expiry,
            "strikes": {
                "long_put": pos.strikes[0] if len(pos.strikes) > 0 else None,
                "short_put": pos.strikes[1] if len(pos.strikes) > 1 else None,
            },
            "qty": action.qty,
            "estimated_credit_per_contract": action.premium,
            "reason_codes": action.reason_codes,
            # execute_trade doesn't support spread closes; requires manual execution
            "manual_close_required": True,
        }

        # Safe filename: no special chars
        slug = f"{pos.underlier}_{pos.expiry}_{action.type}_{(action.trade_id or 'unknown')[:12]}"
        filename = slug.replace("/", "_").replace(":", "_") + ".json"
        intent_path = intents_dir / filename

        try:
            intents_dir.mkdir(parents=True, exist_ok=True)
            with open(intent_path, "w", encoding="utf-8") as f:
                json.dump(intent, f, indent=2, default=str)
            # Store path on the action so it appears in allocator_actions.json
            action.intent_path = str(intent_path)
            action.reason_codes.append("MANUAL_CLOSE_REQUIRED")
        except OSError as e:
            log.warning(f"Could not write close intent for {action.trade_id}: {e}")
            action.reason_codes.append("MANUAL_CLOSE_REQUIRED")


# ---------------------------------------------------------------------------
# OrderIntent builder (v1.3 Task A) — shared helper
# ---------------------------------------------------------------------------

def build_order_intent_from_candidate(
    candidate: Dict[str, Any],
    qty: int,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a valid OrderIntent dict from a candidate record.

    Produces a dict that passes validate_order_intent() in execute_trade.py.

    Required OrderIntent fields:
      strategy, symbol, expiry, type, legs,
      qty, limit (start/max), tif, guards, intent_id

    Args:
        candidate:  Candidate dict from recommended.json / candidates_flat.json
        qty:        Number of contracts (from allocator auto-sizing)
        policy:     Allocator policy dict (strategy name pulled from policy_id)

    Returns:
        Complete OrderIntent dict ready for execute_trade.
    """
    symbol = str(candidate.get("underlier") or "SPY")
    expiry = str(candidate.get("expiry") or "")

    # Extract strikes
    strikes_dict = candidate.get("strikes", {}) or {}
    long_strike = float(
        candidate.get("long_strike")
        or strikes_dict.get("long_put", 0)
        or 0
    )
    short_strike = float(
        candidate.get("short_strike")
        or strikes_dict.get("short_put", 0)
        or 0
    )

    # Debit — try multiple field names (same order as open_plan._get_premium)
    debit: float = 0.01
    for key in ("debit_per_contract", "computed_premium_usd"):
        v = candidate.get(key)
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    debit = f
                    break
            except (ValueError, TypeError):
                pass

    # Normalize to per-share for IBKR lmtPrice (IBKR always expects per-share)
    # debit extracted above is per-contract (e.g., 69.00) → divide by 100
    debit_per_share = round(debit / 100.0, 4)
    limit_max_per_share = round(debit_per_share * 1.02, 4)

    # Safety invariant: per-share limit > $10 almost certainly means a unit error
    if debit_per_share > 10.0:
        raise ValueError(
            f"build_order_intent_from_candidate: limit.start={debit_per_share} > $10/share "
            f"for candidate_id={candidate.get('candidate_id')!r} — "
            f"debit_per_contract={debit}. Check unit convention (expected $/contract → /100 → $/share)."
        )

    # min_dte guard: use 7 as a safe floor
    min_dte = 7

    # Build the intent body (intent_id computed after, excluded from hash)
    intent_body: Dict[str, Any] = {
        # Required by validate_order_intent()
        "strategy": str(policy.get("policy_id", "ccc_v1")),
        "symbol": symbol,
        "expiry": expiry,
        # Embedded timestamp — used by stale-intent guard in ccc_execute.py (Task 4 v1.6)
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "type": "VERTICAL_PUT_DEBIT",
        "legs": [
            {
                "action": "BUY",
                "right": "P",
                "strike": long_strike,
                "ratio": 1,
                "exchange": "SMART",
                "currency": "USD",
            },
            {
                "action": "SELL",
                "right": "P",
                "strike": short_strike,
                "ratio": 1,
                "exchange": "SMART",
                "currency": "USD",
            },
        ],
        "qty": qty,
        # Per-share limit prices (IBKR lmtPrice convention)
        "limit": {
            "start": debit_per_share,
            "max": limit_max_per_share,
            # Metadata for reporting/ledger (per-contract dollars)
            "limit_per_contract_start": round(debit, 2),
            "limit_per_contract_max": round(debit * 1.02, 2),
        },
        "tif": "DAY",
        "guards": {
            "max_debit": limit_max_per_share,
            "max_spread_width": 0.20,
            "min_dte": min_dte,
        },
        # Extra metadata (not required by validate_order_intent but useful for ledger/tracking)
        "regime": str(candidate.get("regime") or ""),
        "candidate_id": str(
            candidate.get("candidate_id")
            or candidate.get("id")
            or ""
        ),
        "run_id": candidate.get("run_id"),
        "cluster_id": candidate.get("cluster_id") or candidate.get("cell_id"),
    }

    # Deterministic intent_id: SHA1 of body serialised without intent_id field
    id_content = json.dumps(
        {k: v for k, v in intent_body.items() if k != "intent_id"},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    intent_id = hashlib.sha1(id_content.encode("utf-8")).hexdigest()
    intent_body["intent_id"] = intent_id

    return intent_body


# ---------------------------------------------------------------------------
# Open intent emission (v1.3 Task A/C — writes pure executable OrderIntent)
# ---------------------------------------------------------------------------

def _write_open_intents(
    plan: AllocatorPlan,
    intents_dir: Path,
    candidates_data: Optional[Dict[str, Any]] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> None:
    """
    For each OPEN action, write an OrderIntent JSON file to intents_dir.

    v1.3 Task A: Intent file is a pure executable OrderIntent that passes
    validate_order_intent().  Action metadata (reason_codes, convexity, etc.)
    stays in allocator_actions.json only (Task C).

    Strategy:
      1. Build candidate_by_id lookup from candidates_data.
      2. For each OPEN action look up the full candidate record.
      3. Call build_order_intent_from_candidate() to get a proper OrderIntent.
      4. Write ONLY the OrderIntent dict to file (no allocator action fields).

    Fallback: if the candidate record is not found in candidates_data, construct
    a minimal candidate dict from the action's stored metadata fields (v1.3 Task B).
    """
    # Build candidate lookup {candidate_id: candidate_dict}
    candidate_by_id: Dict[str, Any] = {}
    if candidates_data:
        raw = (
            candidates_data.get("selected")
            or candidates_data.get("candidates")
            or []
        )
        for c in raw:
            for key in ("candidate_id", "id"):
                val = c.get(key)
                if val:
                    candidate_by_id[str(val)] = c
                    break

    # Guarantee directory exists (Task D)
    try:
        intents_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning(f"Could not create intents directory {intents_dir}: {e}")
        return

    effective_policy = policy or {"policy_id": plan.policy_id}

    for action in plan.actions:
        if action.type != ActionType.OPEN:
            continue
        if action.intent_path:
            continue  # already written

        candidate_id = action.candidate_id or "unknown"

        # Look up full candidate record; fall back to action-stored metadata
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            # v1.3 Task B fallback: reconstruct minimal candidate from action fields
            candidate = {
                "underlier": action.underlier or "SPY",
                "regime": action.regime or "",
                "expiry": action.expiry or "",
                "long_strike": action.long_strike or 0.0,
                "short_strike": action.short_strike or 0.0,
                "debit_per_contract": action.premium or 0.01,
                "candidate_id": candidate_id,
            }
            log.warning(
                f"OPEN intent candidate '{candidate_id}' not found in candidates_data; "
                f"using action metadata fallback"
            )

        # Build pure executable OrderIntent (Task A/C)
        intent = build_order_intent_from_candidate(
            candidate=candidate,
            qty=action.qty or 1,
            policy=effective_policy,
        )

        # v1.5 Task 5: Validate the intent passes execute_trade's validator BEFORE writing.
        # This ensures every OPEN intent written to disk is immediately executable.
        try:
            from forecast_arb.execution.execute_trade import validate_order_intent as _validate
            _validate(intent)
        except ImportError:
            log.warning(
                "execute_trade.validate_order_intent not importable — "
                "skipping OPEN intent validation (non-fatal in test environments)"
            )
        except Exception as _val_exc:
            raise ValueError(
                f"OPEN intent for candidate_id={candidate_id!r} failed validate_order_intent(): "
                f"{_val_exc}. "
                f"Intent NOT written. Fix build_order_intent_from_candidate() or the candidate data."
            ) from _val_exc

        # Safe filename: OPEN_<candidate_id_slug>.json
        slug = f"OPEN_{candidate_id}"[:80]
        filename = (
            slug.replace("/", "_").replace(":", "_").replace(" ", "_")
            + ".json"
        )
        intent_path = intents_dir / filename

        try:
            with open(intent_path, "w", encoding="utf-8") as f:
                json.dump(intent, f, indent=2, default=str)
            action.intent_path = str(intent_path)
            log.info(f"OPEN intent written: {intent_path}")
        except OSError as e:
            log.warning(f"Could not write OPEN intent for {candidate_id}: {e}")


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _write_actions_json(plan: AllocatorPlan, path: Path) -> None:
    """Write allocator_actions.json (overwrites each run)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, indent=2, default=str)
    log.info(f"Allocator actions written to {path}")


def _append_ledger_summary(plan: AllocatorPlan, ledger_path: Path) -> None:
    """Append a summary record to allocator_ledger.jsonl."""
    today_str = datetime.now(timezone.utc).date().isoformat()

    # Record per OPEN action
    for action in plan.actions:
        if action.type == ActionType.OPEN:
            # v1.3 Task B: use metadata fields stored on action (populated in open_plan.py)
            regime = action.regime or _extract_regime_from_action(action)
            strikes_dict = None
            if action.long_strike or action.short_strike:
                strikes_dict = {
                    "long_put": action.long_strike,
                    "short_put": action.short_strike,
                }
            record = {
                "date": today_str,
                "action": "OPEN",
                "policy_id": plan.policy_id,
                "timestamp_utc": plan.timestamp_utc,
                "trade_id": action.trade_id or f"alloc_{action.candidate_id}_{today_str}",
                "candidate_id": action.candidate_id,
                "run_id": action.run_id,
                "candidate_rank": action.candidate_rank,
                "regime": regime,
                "qty": action.qty,
                "premium_per_contract": action.premium,
                "premium_spent": (action.premium or 0) * (action.qty or 1),
                "reason_codes": action.reason_codes,
                "convexity": action.convexity_detail,
                # v1.3: populated from action metadata (no external lookup needed)
                "underlier": action.underlier,
                "expiry": action.expiry,
                "strikes": strikes_dict,
                "cluster_id": action.cluster_id,
                "intent_path": action.intent_path,
            }
            append_ledger_record(ledger_path, record)

    # Record HARVEST/ROLL actions
    for action in plan.actions:
        if action.type in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE):
            record = {
                "date": today_str,
                "action": action.type,
                "policy_id": plan.policy_id,
                "timestamp_utc": plan.timestamp_utc,
                "trade_id": action.trade_id,
                "qty": action.qty,
                "mark_mid": action.premium,
                "reason_codes": action.reason_codes,
                "intent_path": action.intent_path,
            }
            append_ledger_record(ledger_path, record)

    # Daily summary record
    b = plan.budgets
    inv_before = plan.inventory
    inv_after = plan.inventory_after or plan.inventory

    summary = {
        "date": today_str,
        "action": "DAILY_SUMMARY",
        "policy_id": plan.policy_id,
        "timestamp_utc": plan.timestamp_utc,
        # Budget before/after
        "budget_spent_today_before": round(b.spent_today, 2),
        "budget_remaining_today_before": round(b.remaining_today_before, 2),
        "budget_planned_spend_today": round(b.planned_spend_today, 2),
        "budget_remaining_today_after": round(b.remaining_today_after, 2),
        "budget_spent_week": round(b.spent_week, 2),
        "budget_spent_month": round(b.spent_month, 2),
        "kicker_enabled": b.kicker_enabled,
        # Inventory before/after
        "crash_open_before": inv_before.crash_open,
        "selloff_open_before": inv_before.selloff_open,
        "crash_open_after": inv_after.crash_open,
        "selloff_open_after": inv_after.selloff_open,
        # Action counts
        "harvest_count": sum(1 for a in plan.actions if a.type == ActionType.HARVEST_CLOSE),
        "roll_count": sum(1 for a in plan.actions if a.type == ActionType.ROLL_CLOSE),
        "open_count": sum(1 for a in plan.actions if a.type == ActionType.OPEN),
        "hold_count": sum(1 for a in plan.actions if a.type == ActionType.HOLD),
        "open_premium_today": round(b.planned_spend_today, 2),
        "warnings": plan.notes,
    }
    append_ledger_record(ledger_path, summary)


def _extract_regime_from_action(action: AllocatorAction) -> str:
    """Attempt to extract regime from candidate_id or reason codes."""
    cid = action.candidate_id or ""
    for regime in ("crash", "selloff"):
        if regime in cid.lower():
            return regime
    return "unknown"


# ---------------------------------------------------------------------------
# Pending intent reservation (v1.4 Task D)
# ---------------------------------------------------------------------------

def _scan_pending_open_intents(intents_dir: Path) -> Dict[str, int]:
    """
    Scan intents_dir for OPEN_*.json files written/modified today.

    v1.4 Task D: Prevents CCC from planning a second OPEN when an intent
    for the same regime already exists in the intents directory today.

    Returns:
        Dict[str, int] mapping regime → count of pending intents today.
        e.g. {"crash": 1, "selloff": 0}
    """
    today_str = datetime.now(timezone.utc).date().isoformat()
    pending: Dict[str, int] = {}

    if not intents_dir.exists():
        return pending

    for intent_file in intents_dir.glob("OPEN_*.json"):
        try:
            # Check if file was modified today (UTC)
            mtime = datetime.fromtimestamp(
                intent_file.stat().st_mtime, tz=timezone.utc
            )
            if mtime.date().isoformat() != today_str:
                continue

            with open(intent_file, "r", encoding="utf-8") as f:
                intent = json.load(f)

            regime = str(intent.get("regime", "")).lower()
            if regime in ("crash", "selloff"):
                pending[regime] = pending.get(regime, 0) + 1

        except OSError:
            continue
        except (json.JSONDecodeError, ValueError):
            continue

    return pending


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def _print_pm_summary(plan: AllocatorPlan, verbose: bool = False) -> None:
    """Print PM-grade daily allocator summary."""
    b = plan.budgets
    inv_before = plan.inventory
    inv_after = plan.inventory_after or plan.inventory
    today_str = datetime.now(timezone.utc).date().isoformat()

    kicker_str = "ON ✓" if b.kicker_enabled else "OFF"

    print("")
    print("╔" + "═" * 66 + "╗")
    print(f"║  CCC v1 ALLOCATOR  —  {today_str:<43}║")
    print("╠" + "═" * 66 + "╣")

    # Budget before/after line
    print(
        f"║  BUDGET BEFORE  "
        f"Month ${b.spent_today:.0f}/${b.monthly_soft_cap:.0f}  "
        f"Week ${b.spent_week:.0f}/${b.weekly_soft_cap:.0f}  "
        f"Today ${b.spent_today:.0f}/${b.daily_soft_cap:.0f}  "
        f"Kicker: {kicker_str}"
        .ljust(67) + "║"
    )
    print(
        f"║  BUDGET AFTER   "
        f"Planned=${b.planned_spend_today:.0f}  "
        f"Remaining_today=${b.remaining_today_after:.0f}  "
        f"Remaining_week=${b.remaining_week_after:.0f}"
        .ljust(67) + "║"
    )

    # Phase 2A Task A: annual convexity budget line (only shown when gate is enabled)
    if b.annual_budget_enabled:
        print(
            f"║  ANNUAL BUDGET  "
            f"YTD=${b.spent_ytd:.0f}  "
            f"Budget=${b.annual_convexity_budget:.0f}  "
            f"Remaining=${b.remaining_annual:.0f}"
            .ljust(67) + "║"
        )

    # v1.8: ACTUAL / PENDING (committed-not-filled) / EFFECTIVE inventory display
    pending_crash = plan.pending_open_intents.get("crash", 0)
    pending_selloff = plan.pending_open_intents.get("selloff", 0)
    pending_total = pending_crash + pending_selloff
    inv_eff = plan.inv_effective or inv_before  # fallback if not populated

    # INVENTORY ACTUAL row
    print(
        f"║  INVENTORY ACTUAL  "
        f"crash={inv_before.crash_open}/{inv_before.crash_target}  "
        f"selloff={inv_before.selloff_open}/{inv_before.selloff_target}"
        .ljust(67) + "║"
    )
    # PENDING row — only shown when there are pending intents
    if pending_total > 0:
        print(
            f"║  PENDING (committed-not-filled)  "
            f"crash={pending_crash}  selloff={pending_selloff}"
            .ljust(67) + "║"
        )
        # EFFECTIVE row — gating view (actual + pending)
        print(
            f"║  INVENTORY EFFECTIVE (gating)  "
            f"crash={inv_eff.crash_open}/{inv_eff.crash_target}  "
            f"selloff={inv_eff.selloff_open}/{inv_eff.selloff_target}"
            .ljust(67) + "║"
        )
    # PLANNED row — after today's proposed actions
    print(
        f"║  INVENTORY PLANNED "
        f"crash={inv_after.crash_open}/{inv_after.crash_target}  "
        f"selloff={inv_after.selloff_open}/{inv_after.selloff_target}  "
        f"Positions: {len(plan.positions)}"
        .ljust(67) + "║"
    )

    print("╠" + "─" * 66 + "╣")
    open_c = sum(1 for a in plan.actions if a.type == ActionType.OPEN)
    close_c = sum(1 for a in plan.actions
                  if a.type in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE))
    hold_c = sum(1 for a in plan.actions if a.type == ActionType.HOLD)
    print(
        f"║  ACTIONS ({len(plan.actions)} total: {open_c} OPEN, {close_c} CLOSE, {hold_c} HOLD)"
        .ljust(67) + "║"
    )
    print("╠" + "─" * 66 + "╣")

    for action in plan.actions:
        if action.type == ActionType.HARVEST_CLOSE:
            tid = (action.trade_id or "")[:20]
            multiple_str = ""
            for rc in action.reason_codes:
                if "MULTIPLE" in rc:
                    multiple_str = rc.split(":")[1] if ":" in rc else rc
                    break
            intent_note = f"  intent={'set' if action.intent_path else 'null'}"
            line = f"  HARVEST  trade={tid}  qty={action.qty}  {multiple_str}{intent_note}"
            print(f"║  {line[:63]:<63}║")

        elif action.type == ActionType.ROLL_CLOSE:
            tid = (action.trade_id or "")[:20]
            dte_str = ""
            for rc in action.reason_codes:
                if "TIME_STOP" in rc:
                    dte_str = rc
                    break
            intent_note = f"  intent={'set' if action.intent_path else 'null'}"
            line = f"  ROLL     trade={tid}  qty={action.qty}  {dte_str}{intent_note}"
            print(f"║  {line[:63]:<63}║")

        elif action.type == ActionType.OPEN:
            cid = (action.candidate_id or "")[:20]
            ev_str = ""
            for rc in action.reason_codes:
                if rc.startswith("EV_PER_DOLLAR"):
                    ev_str = rc.split(":")[-1] if ":" in rc else rc
            prem_str = f"${action.premium:.0f}/c" if action.premium else ""
            conv = action.convexity_detail
            conv_str = f"{conv['multiple']:.1f}x" if conv else ""
            # v1.9 Task F: show premium source, layer, fragility
            prem_src = ""
            for rc in action.reason_codes:
                if rc.startswith("PREMIUM_USED:"):
                    prem_src = rc.split(":")[-1]
                    break
            layer_str = f" layer={action.layer}" if action.layer else ""
            fragile_str = " FRAGILE" if action.fragile else (" robust" if action.fragile is False else "")
            line = (
                f"  OPEN     {cid}  qty={action.qty}  "
                f"{prem_str}[{prem_src}]  EV={ev_str}  conv={conv_str}"
                f"{layer_str}{fragile_str}"
            )
            print(f"║  {line[:63]:<63}║")

        elif action.type == ActionType.HOLD:
            reasons = ", ".join(action.reason_codes[:3])
            line = f"  HOLD     {reasons}"
            print(f"║  {line[:63]:<63}║")

    if plan.notes:
        print("╠" + "─" * 66 + "╣")
        for note in plan.notes:
            print(f"║  NOTE: {note[:58]:<58}║")

    print("╚" + "═" * 66 + "╝")

    # v1.6 Task C: HOLD delta section — operator-grade rejection explanation
    # Printed when inventory needs an OPEN but all candidates were rejected.
    # Shows the top candidate's EV and convexity deltas so the PM can see
    # exactly how far short the market was from the policy thresholds.
    trace = plan.open_gate_trace
    if trace is not None:
        candidates_eval = trace.get("candidates_evaluated") or []
        rejected_eval = [
            e for e in candidates_eval
            if e.get("decision") == "REJECT" or e.get("result") == "REJECTED"
        ]
        if rejected_eval:
            # Top candidate = first entry (already sorted by EV desc in generate_open_actions)
            top = rejected_eval[0]
            top_ev = top.get("ev_per_dollar", 0.0)
            top_conv = top.get("convexity_multiple", 0.0)
            ev_thr = top.get("ev_threshold_used", 0.0)
            conv_thr = top.get("conv_threshold_used", 0.0)
            delta_ev = top.get("delta_ev", top_ev - ev_thr)
            delta_conv = top.get("delta_conv", top_conv - conv_thr)
            tier = top.get("tier", "unknown")
            regime = top.get("regime", "?")

            ev_sign = "" if delta_ev >= 0 else "\u2212"
            conv_sign = "" if delta_conv >= 0 else "\u2212"

            print("")
            print(f"  HOLD DETAIL  top candidate: {regime}  tier={tier}")
            # EV line: e.g. "  EV/$ 1.44 < 1.60 (−0.16)"
            print(
                f"    EV/$ {top_ev:.2f} {'<' if delta_ev < 0 else '>='} {ev_thr:.2f} "
                f"({ev_sign}{abs(delta_ev):.2f})"
            )
            # Convexity line: e.g. "  Convexity 19.2x < 25.0x (−5.8x)"
            print(
                f"    Convexity {top_conv:.1f}x {'<' if delta_conv < 0 else '>='} {conv_thr:.1f}x "
                f"({conv_sign}{abs(delta_conv):.1f}x)"
            )

    if verbose:
        # Print full positions table
        if plan.positions:
            print("\nOPEN POSITIONS:")
            print(f"{'TRADE_ID':<24} {'UNDERLIER':<8} {'EXPIRY':<8} {'STRIKES':<12} "
                  f"{'QTY':<4} {'DEBIT':>6} {'MARK':>6} {'MULT':>5} {'DTE':>4}")
            print("-" * 80)
            for pos in plan.positions:
                strikes_str = "/".join(f"{s:.0f}" for s in pos.strikes[:2])
                mult_str = f"{pos.multiple:.1f}x" if pos.multiple else "N/A"
                mark_str = f"{pos.mark_mid:.1f}" if pos.mark_mid else "N/A"
                debit_str = f"{pos.entry_debit:.1f}" if pos.entry_debit else "N/A"
                dte_str = str(pos.dte) if pos.dte is not None else "N/A"
                print(f"{pos.trade_id[:24]:<24} {pos.underlier[:8]:<8} {pos.expiry[:8]:<8} "
                      f"{strikes_str:<12} {pos.qty_open:<4} {debit_str:>6} "
                      f"{mark_str:>6} {mult_str:>5} {dte_str:>4}")


# ---------------------------------------------------------------------------
# HOLD reason logic
# ---------------------------------------------------------------------------

def _determine_hold_reasons(
    budget: BudgetState,
    inventory: InventoryState,
    candidates_data: Dict[str, Any],
    rejection_log: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """
    Determine why we're holding (for HOLD reason codes).

    CCC v1.2 Task 3: HOLD reason codes are derived from actual gate failures.
    Kicker on/off is NOT a blocking reason — it only affects soft cap sizes.
    Kicker state is reflected in the budget lines and open_gate_trace only.

    Recognised reason codes: DAILY_BUDGET_EXHAUSTED, WEEKLY_BUDGET_EXHAUSTED,
    MONTHLY_BUDGET_EXHAUSTED, INVENTORY_AT_TARGET, NO_CANDIDATES_FILE,
    NO_QUALIFYING_CANDIDATES, EV_BELOW_THRESHOLD, CONVEXITY_TOO_LOW,
    BUDGET_BLOCKED, NOT_REPRESENTABLE, DAILY_ACTION_LIMIT,
    NO_QUALIFYING_TRADES.
    """
    reasons: List[str] = []

    # Budget exhausted at baseline (or kicker) cap?
    if budget.remaining_today <= 0:
        reasons.append("DAILY_BUDGET_EXHAUSTED")
    if budget.remaining_week <= 0:
        reasons.append("WEEKLY_BUDGET_EXHAUSTED")
    if budget.remaining_month <= 0:
        reasons.append("MONTHLY_BUDGET_EXHAUSTED")

    # Inventory at target?
    if not inventory.needs_open("crash") and not inventory.needs_open("selloff"):
        reasons.append("INVENTORY_AT_TARGET")

    # No candidates?
    if not candidates_data:
        reasons.append("NO_CANDIDATES_FILE")
    else:
        raw = (
            candidates_data.get("selected")
            or candidates_data.get("candidates")
            or []
        )
        if not raw:
            reasons.append("NO_QUALIFYING_CANDIDATES")

    # Derive reasons from per-candidate gate evaluations (Task 3)
    # These tell the operator precisely what gate blocked the opens.
    if rejection_log:
        primary_reasons_seen: set = set()
        for entry in rejection_log:
            primary = entry.get("primary_reason", "")
            if primary and primary not in primary_reasons_seen:
                primary_reasons_seen.add(primary)
                reasons.append(primary)

    # NOTE: KICKER_OFF is intentionally NOT included here.
    # Kicker state affects cap SIZE only; opens proceed at baseline caps when kicker is off.
    # Kicker state is visible in: budget.kicker_enabled + budgets.kicker_reasons + open_gate_trace.

    if not reasons:
        reasons.append("NO_QUALIFYING_TRADES")

    return reasons


