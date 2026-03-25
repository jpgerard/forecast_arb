"""
CCC v1 Allocator - Open plan: candidate selection + auto-sizing.

Reads recommended.json or candidates_flat.json, applies policy gates,
auto-sizes qty, and produces OPEN AllocatorActions.

Policy gates applied here:
  - regime matches target regimes (crash/selloff)
  - representable == True (if field present)
  - p_used_src gating + EV/$ threshold (implied-only higher bar)
  - convexity multiple (max_gain / premium)
  - inventory target check (don't open if already at target)
  - budget check (squeeze into daily/weekly/monthly soft caps)

v1.9 Task A — Worst-case debit gating:
  premium_used_for_gating = premium_wc (from long_ask/short_bid)
     if available, else premium_mid, else campaign-computed premium.
  EV/$ is recomputed using premium_used_for_gating when p_used and
  max_gain are both available in the candidate dict.
  PREMIUM_USED:WC or PREMIUM_USED:MID reason code emitted on every OPEN.
  action.pricing persists {premium_used, premium_wc, premium_mid, ...}.

v1.9 Task B — Crash ladder (A / B layers):
  For crash regime, moneyness_pct = (spot - long_strike) / spot * 100
  Layer A: [moneyness_min_pct, moneyness_max_pct] from policy.thresholds.crash.ladder.layer_a
  Layer B: [moneyness_min_pct, moneyness_max_pct] from policy.thresholds.crash.ladder.layer_b
  When inv==0 and both layers present → Layer A preferred (sorted first).
  action.layer stores "A" | "B" | None.

v1.9 Task E — Fragility gating:
  EV_shock = (p_used - p_downshift_pp/100) * max_gain
             - (1 - (p_used - p_downshift_pp/100)) * premium_used * (1 + debit_upshift_pct/100)
  If EV_shock <= 0:
    - if inv_effective==0 and allow_if_inventory_empty → allow with FRAGILE_ALLOWED_EMPTY
    - else → HOLD with EV_FRAGILE_UNDER_SHOCKS
  Requires robustness.enabled = true in policy.
  action.fragile stores True/False (None if check not applicable).

v1.6 – Inventory-aware thresholds (Task B):
  get_effective_thresholds() selects fill_when_empty vs add_when_full per regime.

No changes to execution engine; OPEN actions are plain AllocatorAction objects.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .policy import (
    get_convexity_multiple,
    get_diversity_params,
    get_effective_thresholds,
    get_inventory_hard_caps,
    get_inventory_targets,
    get_premium_at_risk_caps,
    get_threshold,
)
from .pricing import compute_pricing_detail
from .risk import compute_portfolio_premium_at_risk
from .scoring import compute_convexity_score
from .types import ActionType, AllocatorAction, BudgetState, InventoryState


# ---------------------------------------------------------------------------
# Candidate normalisation helpers
# ---------------------------------------------------------------------------

def _extract_candidates(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract a flat list of candidates from either:
      - recommended.json  → data["selected"]
      - candidates_flat.json → data["candidates"]

    Returns list (may be empty).
    """
    if "selected" in data:
        return list(data.get("selected") or [])
    if "candidates" in data:
        return list(data.get("candidates") or [])
    return []


def _get_premium(candidate: Dict[str, Any]) -> Optional[float]:
    """
    Extract premium-per-contract from candidate.

    Tries: computed_premium_usd → debit_per_contract (both in dollars/contract).
    Returns None if neither present or non-positive.
    """
    for key in ("computed_premium_usd", "debit_per_contract"):
        val = candidate.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 0:
                    return f
            except (ValueError, TypeError):
                continue
    return None


def _get_max_gain(candidate: Dict[str, Any]) -> Optional[float]:
    """Extract max_gain_per_contract from candidate (dollars/contract)."""
    val = candidate.get("max_gain_per_contract")
    if val is not None:
        try:
            f = float(val)
            if f > 0:
                return f
        except (ValueError, TypeError):
            pass
    return None


def _get_ev_per_dollar(candidate: Dict[str, Any]) -> Optional[float]:
    """Extract ev_per_dollar from candidate."""
    val = candidate.get("ev_per_dollar")
    if val is not None:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None


def _get_p_used(candidate: Dict[str, Any]) -> Optional[float]:
    """
    Extract the probability used for EV calculation from the candidate.

    Tries: p_used → p_event_used → assumed_p_event → p_used_value.
    Returns None if not found.
    """
    for key in ("p_used", "p_event_used", "assumed_p_event", "p_used_value"):
        val = candidate.get(key)
        if val is not None:
            try:
                f = float(val)
                if 0 < f <= 1:
                    return f
            except (ValueError, TypeError):
                continue
    return None


def _get_p_used_src(candidate: Dict[str, Any]) -> str:
    """Extract p_used_src / p_source from candidate, normalised to lowercase."""
    for key in ("p_used_src", "p_source"):
        val = candidate.get(key)
        if val:
            return str(val).lower()
    return ""


def _get_regime(candidate: Dict[str, Any]) -> str:
    return str(candidate.get("regime", "")).lower()


def _get_candidate_id(candidate: Dict[str, Any]) -> str:
    """Return canonical candidate_id exactly as emitted by campaign."""
    for key in ("candidate_id", "id"):
        val = candidate.get(key)
        if val:
            return str(val)
    underlier = candidate.get("underlier", "")
    expiry = candidate.get("expiry", "")
    strikes = candidate.get("strikes", {}) or {}
    ls = candidate.get("long_strike") or strikes.get("long_put", "")
    ss = candidate.get("short_strike") or strikes.get("short_put", "")
    regime = candidate.get("regime", "")
    return f"{underlier}_{expiry}_{ls}_{ss}_{regime}"


def _compute_convexity_detail(
    candidate: Dict[str, Any],
    premium_per_contract: float,
    max_gain_per_contract: float,
) -> Dict[str, Any]:
    """Build the convexity decomposition dict with explicit units (Task 3)."""
    strikes = candidate.get("strikes", {}) or {}
    long_strike = float(
        candidate.get("long_strike") or strikes.get("long_put", 0) or 0
    )
    short_strike = float(
        candidate.get("short_strike") or strikes.get("short_put", 0) or 0
    )
    width = round(long_strike - short_strike, 2)
    debit_per_share = round(premium_per_contract / 100.0, 4)

    if premium_per_contract > 0:
        multiple = round(max_gain_per_contract / premium_per_contract, 2)
    else:
        multiple = 0.0

    return {
        "width": width,
        "debit": debit_per_share,
        "max_gain_per_contract": round(max_gain_per_contract, 2),
        "premium_per_contract": round(premium_per_contract, 2),
        "multiple": multiple,
    }


# ---------------------------------------------------------------------------
# v1.9 Task A: worst-case premium resolution
# ---------------------------------------------------------------------------

def _resolve_premium_for_gating(
    candidate: Dict[str, Any],
    standard_premium: float,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    Determine the premium to use for gating (worst-case if available).

    Returns:
      (premium_used, source_tag, pricing_detail_dict)

    source_tag: "WC" | "MID" | "CAMPAIGN"

    pricing_detail_dict contains all quote/debit fields for action.pricing.
    """
    pd = compute_pricing_detail(candidate)

    if pd["premium_wc"] is not None and pd["premium_wc"] > 0:
        return pd["premium_wc"], "WC", pd

    if pd["premium_mid"] is not None and pd["premium_mid"] > 0:
        return pd["premium_mid"], "MID", pd

    # No leg quotes — use campaign-computed premium
    return standard_premium, "CAMPAIGN", pd


def _recompute_ev_per_dollar(
    p_used: Optional[float],
    max_gain_per_contract: Optional[float],
    premium_for_gating: float,
) -> Optional[float]:
    """
    Recompute EV/$ using the premium actually used for gating.

    EV/$ = (p * max_gain - (1-p) * premium) / premium

    Returns None if p_used or max_gain not available.
    Caps EV/$ at a very large positive number to avoid inf.
    """
    if p_used is None or max_gain_per_contract is None:
        return None
    if premium_for_gating <= 0:
        return None
    ev = p_used * max_gain_per_contract - (1 - p_used) * premium_for_gating
    return ev / premium_for_gating


# ---------------------------------------------------------------------------
# v1.9 Task B: crash ladder layer classification
# ---------------------------------------------------------------------------

def _get_spot(candidate: Dict[str, Any]) -> Optional[float]:
    """Extract spot price from candidate dict."""
    for key in ("spot", "spot_price", "underlier_price", "spot_at_entry"):
        val = candidate.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 0:
                    return f
            except (ValueError, TypeError):
                continue
    return None


def _classify_ladder_layer(
    candidate: Dict[str, Any],
    policy: Dict[str, Any],
    regime: str,
) -> Tuple[Optional[str], Optional[float]]:
    """
    Classify a crash candidate into ladder Layer A, Layer B, or None.

    Returns (layer, moneyness_pct):
      layer = "A" | "B" | None
      moneyness_pct = float | None
    """
    # Ladder only defined for crash regime
    ladder = policy.get("thresholds", {}).get(regime.lower(), {}).get("ladder")
    if not ladder:
        return None, None

    spot = _get_spot(candidate)
    if spot is None or spot <= 0:
        return None, None

    strikes = candidate.get("strikes", {}) or {}
    long_strike = float(
        candidate.get("long_strike") or strikes.get("long_put", 0) or 0
    )
    if long_strike <= 0:
        return None, None

    # OTM% for puts: spot > long_strike → positive moneyness
    moneyness_pct = (spot - long_strike) / spot * 100.0

    layer_a_cfg = ladder.get("layer_a", {})
    layer_b_cfg = ladder.get("layer_b", {})
    a_min = float(layer_a_cfg.get("moneyness_min_pct", 5.0))
    a_max = float(layer_a_cfg.get("moneyness_max_pct", 9.0))
    b_min = float(layer_b_cfg.get("moneyness_min_pct", 10.0))
    b_max = float(layer_b_cfg.get("moneyness_max_pct", 16.0))

    if a_min <= moneyness_pct <= a_max:
        return "A", moneyness_pct
    if b_min <= moneyness_pct <= b_max:
        return "B", moneyness_pct
    return None, moneyness_pct


def _layer_sort_key(
    candidate: Dict[str, Any],
    policy: Dict[str, Any],
    regime: str,
    inv_open_for_regime: int,
) -> Tuple[int, float, float]:
    """
    Sort key for candidates: ladder first, then convexity_score, then EV/$.

    Phase 2A Task B — updated ranking order:
      a) ladder preference  (crash, inv==0 only):   Layer A=0, B=1, None=2
      b) convexity_score descending                  (new middle rank)
      c) ev_per_dollar descending                    (tiebreaker, unchanged)

    Returns (layer_priority, -convexity_score, -ev_per_dollar).

    When ladder is not applicable (non-crash or already holding):
      All candidates get priority=1 so they compete purely on score then EV/$.
    When scoring inputs are unavailable (score=0.0), falls back gracefully to
      pure EV/$ tie-breaking (same behaviour as the original 2-tuple key).
    """
    score = compute_convexity_score(candidate)
    ev = float(candidate.get("ev_per_dollar", 0) or 0)

    if regime != "crash" or inv_open_for_regime > 0:
        # No ladder differentiation when already holding or non-crash regime
        return (1, -score, -ev)

    layer, _ = _classify_ladder_layer(candidate, policy, regime)
    priority = {"A": 0, "B": 1}.get(layer, 2)
    return (priority, -score, -ev)


# ---------------------------------------------------------------------------
# v1.9 Task E: fragility gating
# ---------------------------------------------------------------------------

def _compute_ev_shock(
    p_used: float,
    max_gain_per_contract: float,
    premium_used: float,
    robustness_params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compute EV under probability downshift and debit upshift shocks.

    Returns:
      {ev_base, ev_shock, ev_base_per_dollar, ev_shock_per_dollar,
       p_shock, premium_shock, fragile}
    """
    p_shift    = robustness_params.get("p_downshift_pp", 3.0) / 100.0
    debit_shift = robustness_params.get("debit_upshift_pct", 10.0) / 100.0

    ev_base = p_used * max_gain_per_contract - (1 - p_used) * premium_used
    ev_base_per_dollar = ev_base / premium_used if premium_used > 0 else 0.0

    p_shock = max(0.0, p_used - p_shift)
    premium_shock = premium_used * (1 + debit_shift)

    ev_shock = p_shock * max_gain_per_contract - (1 - p_shock) * premium_shock
    ev_shock_per_dollar = ev_shock / premium_shock if premium_shock > 0 else 0.0

    return {
        "ev_base": round(ev_base, 4),
        "ev_shock": round(ev_shock, 4),
        "ev_base_per_dollar": round(ev_base_per_dollar, 4),
        "ev_shock_per_dollar": round(ev_shock_per_dollar, 4),
        "p_used": p_used,
        "p_shock": round(p_shock, 4),
        "premium_used": round(premium_used, 4),
        "premium_shock": round(premium_shock, 4),
        "fragile": ev_shock <= 0,
    }


# ---------------------------------------------------------------------------
# Phase 2A Task C: Strike diversity guard
# ---------------------------------------------------------------------------

def _get_long_strike_from_candidate(candidate: Dict[str, Any]) -> Optional[float]:
    """Extract long_strike from candidate using standard field aliases."""
    strikes = candidate.get("strikes", {}) or {}
    val = (
        candidate.get("long_strike")
        or strikes.get("long_put")
        or strikes.get("long")
    )
    if val is not None:
        try:
            f = float(val)
            if f > 0:
                return f
        except (ValueError, TypeError):
            pass
    return None


def _check_strike_diversity(
    candidate_long_strike: float,
    spot: float,
    diversity_threshold_pct: float,
    existing_positions: List[Any],      # List[SleevePosition] — typed as Any to avoid import
    approved_opens: List[AllocatorAction],
    cand_underlier: str,
    cand_regime: str,
) -> Optional[str]:
    """
    Phase 2A Task C: Check if candidate long_strike is too close to any
    existing open position or to any already-approved OPEN in the current run.

    Scope: same underlier + same regime only (never cross-underlier/cross-regime).

    Args:
        candidate_long_strike: The candidate's long put strike.
        spot:                  Current spot price from the candidate.
        diversity_threshold_pct: Minimum allowed distance (% of spot).
        existing_positions:    List of SleevePosition objects (plan ledger positions).
        approved_opens:        OPEN AllocatorActions already approved this run.
        cand_underlier:        Candidate underlier (e.g. "SPY"); case-insensitive.
        cand_regime:           Candidate regime (e.g. "crash"); case-insensitive.

    Returns:
        Colon-separated reason string if too close, else None.
        Format: "STRIKE_TOO_CLOSE:MIN_STRIKE_DISTANCE_PCT:<x>:
                 EXISTING_LONG_STRIKE:<y>:EXISTING_TRADE_ID:<z>"
    """
    threshold_fraction = diversity_threshold_pct / 100.0
    cand_u = cand_underlier.lower()
    cand_r = cand_regime.lower()

    # --- Check against existing portfolio positions ---
    for pos in existing_positions:
        # Scope: same underlier + same regime
        if pos.underlier.lower() != cand_u:
            continue
        if pos.regime.lower() != cand_r:
            continue
        if not pos.strikes:
            continue

        existing_long = pos.strikes[0]   # strikes[0] = long put strike
        if existing_long <= 0:
            continue

        distance_fraction = abs(candidate_long_strike - existing_long) / spot
        if distance_fraction < threshold_fraction:
            return (
                f"STRIKE_TOO_CLOSE:"
                f"MIN_STRIKE_DISTANCE_PCT:{diversity_threshold_pct:.1f}:"
                f"EXISTING_LONG_STRIKE:{existing_long:.1f}:"
                f"EXISTING_TRADE_ID:{pos.trade_id}"
            )

    # --- Check against already-approved OPEN actions in this planning run ---
    for action in approved_opens:
        if action.type != ActionType.OPEN:
            continue
        if not action.underlier or action.underlier.lower() != cand_u:
            continue
        if not action.regime or action.regime.lower() != cand_r:
            continue

        existing_long = action.long_strike
        if existing_long is None or existing_long <= 0:
            continue

        distance_fraction = abs(candidate_long_strike - existing_long) / spot
        if distance_fraction < threshold_fraction:
            return (
                f"STRIKE_TOO_CLOSE:"
                f"MIN_STRIKE_DISTANCE_PCT:{diversity_threshold_pct:.1f}:"
                f"EXISTING_LONG_STRIKE:{existing_long:.1f}:"
                f"EXISTING_CANDIDATE_ID:{action.candidate_id or 'unknown'}"
            )

    return None  # passes diversity check


# ---------------------------------------------------------------------------
# Main open-plan function
# ---------------------------------------------------------------------------

def generate_open_actions(
    candidates_data: Dict[str, Any],
    policy: Dict[str, Any],
    budget: BudgetState,
    inventory: InventoryState,
    rejection_log: Optional[List[Dict[str, Any]]] = None,
    positions: Optional[List[Any]] = None,
) -> List[AllocatorAction]:
    """
    Select candidates from recommended/flat data and generate OPEN actions.

    For each regime below inventory target, tries to find a qualifying candidate.
    Applies all policy gates. Returns one OPEN action per qualifying candidate
    (up to one per regime, matching inventory need).

    Args:
        candidates_data: Loaded recommended.json or candidates_flat.json dict
        policy:          Validated policy dict
        budget:          Current BudgetState (spend computed)
        inventory:       Current InventoryState (post-close, post-pending-inflation).
                         MUST be the post-close inventory for correct gating (Task 4).
        rejection_log:   Optional mutable list.  If provided, one entry per evaluated
                         candidate is appended with full metrics + tier + deltas for
                         operator-grade trace (Task C v1.6).

    Returns:
        List of AllocatorAction with type=OPEN (may be empty)
    """
    candidates = _extract_candidates(candidates_data)
    sizing = policy["sizing"]
    max_qty = int(sizing.get("max_qty_per_trade", 10))

    # Robustness params (v1.9 Task E)
    robustness_cfg = policy.get("robustness", {})
    robustness_enabled = bool(robustness_cfg.get("enabled", False))
    robustness_params = {
        "p_downshift_pp":   float(robustness_cfg.get("p_downshift_pp", 3.0)),
        "debit_upshift_pct": float(robustness_cfg.get("debit_upshift_pct", 10.0)),
        "allow_if_inventory_empty": bool(robustness_cfg.get("allow_if_inventory_empty", True)),
    }

    # Phase 2A Task C: diversity guard params (disabled when section absent)
    diversity_cfg = get_diversity_params(policy)
    diversity_enabled = diversity_cfg["enabled"]
    diversity_threshold_pct = diversity_cfg["min_strike_distance_pct"]

    # v2.0 / v2.1 Task C/D: premium-at-risk caps (primary gate) and hard count caps (secondary)
    par_caps = get_premium_at_risk_caps(policy)
    par_caps_enabled = par_caps["enabled"]
    hard_caps = get_inventory_hard_caps(policy)

    # v2.1: Soft targets (inventory_targets) for informational tagging only.
    # Being above soft target does NOT block when par_caps_enabled=True;
    # it only shifts the EV threshold tier to add_when_full (stricter).
    inv_soft_targets = get_inventory_targets(policy)

    # v2.0: Current PAR from existing open positions (computed once per planning run)
    current_portfolio_par = compute_portfolio_premium_at_risk(positions or [])

    actions: List[AllocatorAction] = []

    for regime in ("crash", "selloff"):
        inv_open_for_regime = (
            inventory.crash_open if regime == "crash" else inventory.selloff_open
        )
        # v2.1: soft target for SOFT_TARGET_EXCEEDED_ALLOWED tagging
        inv_soft_target_for_regime = inv_soft_targets.get(regime, 0)

        if par_caps_enabled:
            # v2.1 Task B: Gating order when PAR caps enabled:
            #   1. Evaluate candidate (EV, convexity, diversity, fragility, budget)
            #   2. Check projected PAR vs regime cap + total cap  → PREMIUM_AT_RISK_CAP
            #   3. ONLY then check hard count cap               → HARD_COUNT_CAP
            # Soft target (inventory_targets) is purely informational here:
            #   it shifts the EV threshold tier but does NOT block.
            inv_hard_cap = hard_caps.get(regime, 999)
            if inv_open_for_regime >= inv_hard_cap:
                # Hard count backstop reached — log reason for operator visibility
                if rejection_log is not None:
                    rejection_log.append({
                        "candidate_id": f"__HARD_COUNT_CAP_{regime.upper()}__",
                        "regime": regime,
                        "ev_per_dollar": 0.0,
                        "convexity_multiple": 0.0,
                        "premium": None,
                        "result": "REJECTED",
                        "reason": (
                            f"HARD_COUNT_CAP:"
                            f"{regime.upper()}_OPEN:{inv_open_for_regime}>="
                            f"HARD_CAP:{inv_hard_cap}"
                        ),
                        "primary_reason": "HARD_COUNT_CAP",
                        "budget_remaining_today": round(budget.remaining_today, 2),
                        "kicker_enabled": budget.kicker_enabled,
                        "tier": "N/A",
                        "ev_threshold_used": 0.0,
                        "conv_threshold_used": 0.0,
                        "passes_ev": False,
                        "passes_convexity": False,
                        "delta_ev": 0.0,
                        "delta_conv": 0.0,
                        "decision": "REJECT",
                        "layer": None,
                        "moneyness_pct": None,
                        "convexity_score": 0.0,
                    })
                continue  # hard count backstop: skip to next regime
        else:
            # LEGACY: primary gating by count target (original behavior, backward compat)
            if not inventory.needs_open(regime):
                continue

        # v1.6 Task B: inventory-aware thresholds (still governed by soft target)
        # Note: when par_caps_enabled and inv_open >= soft_target, uses "add_when_full"
        # tier (stricter requirements). Candidates must still pass tighter thresholds.
        eff_thresh = get_effective_thresholds(policy, regime, inventory)

        # v2.0: projected PAR base = current positions PAR + already-approved OPENs this run
        # Recomputed per regime iteration (prior-regime OPENs may have been added to actions)
        approved_par_crash = sum(
            (a.premium or 0.0) * (a.qty or 1)
            for a in actions
            if a.type == ActionType.OPEN and (a.regime or "").lower() == "crash"
        )
        approved_par_selloff = sum(
            (a.premium or 0.0) * (a.qty or 1)
            for a in actions
            if a.type == ActionType.OPEN and (a.regime or "").lower() == "selloff"
        )
        projected_par_base: Dict[str, float] = {
            "crash":   current_portfolio_par["crash"]   + approved_par_crash,
            "selloff": current_portfolio_par["selloff"] + approved_par_selloff,
            "total":   current_portfolio_par["total"]   + approved_par_crash + approved_par_selloff,
        }

        # Filter to regime + sort by ladder preference (Task B) then EV desc
        regime_candidates = [c for c in candidates if _get_regime(c) == regime]
        regime_candidates.sort(
            key=lambda c: _layer_sort_key(c, policy, regime, inv_open_for_regime)
        )

        for candidate in regime_candidates:
            # Phase 2A Task C: strike diversity check (runs before expensive gate eval)
            #
            # Compares candidate.long_strike against existing open positions AND
            # against OPEN actions already approved in this planning run.
            # If spot is missing, the check is gracefully skipped (no crash, no block).
            # Scope: same underlier + same regime only.
            action: Optional[AllocatorAction] = None
            reject_reason: str = ""

            if diversity_enabled:
                spot = _get_spot(candidate)
                cand_long_strike = _get_long_strike_from_candidate(candidate)
                cand_underlier = candidate.get("underlier", "") or ""
                if spot is not None and cand_long_strike is not None and cand_underlier:
                    reject_reason = _check_strike_diversity(
                        candidate_long_strike=cand_long_strike,
                        spot=spot,
                        diversity_threshold_pct=diversity_threshold_pct,
                        existing_positions=positions or [],
                        approved_opens=actions,
                        cand_underlier=cand_underlier,
                        cand_regime=regime,
                    ) or ""

            if not reject_reason:
                # Diversity passed (or skipped) — run full gate evaluation
                action, reject_reason = _evaluate_candidate(
                    candidate=candidate,
                    regime=regime,
                    policy=policy,
                    budget=budget,
                    max_qty=max_qty,
                    eff_thresh=eff_thresh,
                    inv_open_for_regime=inv_open_for_regime,
                    robustness_enabled=robustness_enabled,
                    robustness_params=robustness_params,
                    # v2.0 PAR gate params
                    projected_par_base=projected_par_base if par_caps_enabled else None,
                    par_caps=par_caps if par_caps_enabled else None,
                    par_caps_enabled=par_caps_enabled,
                )

            # Collect enriched trace entry (v1.6 Task B/C)
            if rejection_log is not None:
                primary = reject_reason.split(":")[0] if reject_reason else ""
                ev_per_dollar = _get_ev_per_dollar(candidate) or 0.0
                p_src = _get_p_used_src(candidate)
                ev_threshold_used = (
                    eff_thresh["ev_external"]
                    if p_src == "external"
                    else eff_thresh["ev_implied"]
                )
                conv_threshold_used = eff_thresh["convexity_multiple"]
                premium = _get_premium(candidate)
                max_gain = _get_max_gain(candidate)
                convexity = (
                    round(max_gain / premium, 2)
                    if (premium and max_gain and premium > 0) else 0.0
                )
                passes_ev = ev_per_dollar >= ev_threshold_used
                passes_conv = convexity >= conv_threshold_used
                delta_ev   = round(ev_per_dollar - ev_threshold_used, 4)
                delta_conv = round(convexity - conv_threshold_used, 2)

                # v1.9 Task B: layer info for trace
                layer, moneyness_pct = _classify_ladder_layer(candidate, policy, regime)

                # Phase 2A Task B: convexity score for ranking trace
                cand_score = compute_convexity_score(candidate)

                rejection_log.append({
                    "candidate_id": _get_candidate_id(candidate),
                    "regime": regime,
                    "ev_per_dollar": ev_per_dollar,
                    "convexity_multiple": convexity,
                    "premium": premium,
                    "result": "APPROVED" if action is not None else "REJECTED",
                    "reason": reject_reason,
                    "primary_reason": primary,
                    "budget_remaining_today": round(budget.remaining_today, 2),
                    "kicker_enabled": budget.kicker_enabled,
                    # v1.6 Task B/C
                    "tier": eff_thresh["tier"],
                    "ev_threshold_used": ev_threshold_used,
                    "conv_threshold_used": conv_threshold_used,
                    "passes_ev": passes_ev,
                    "passes_convexity": passes_conv,
                    "delta_ev": delta_ev,
                    "delta_conv": delta_conv,
                    "decision": "APPROVE" if action is not None else "REJECT",
                    # v1.9 Task B
                    "layer": layer,
                    "moneyness_pct": round(moneyness_pct, 2) if moneyness_pct is not None else None,
                    # Phase 2A Task B: convexity score used for ranking
                    "convexity_score": round(cand_score, 4),
                })

            if action is not None:
                # v2.1 Task B: Informational tag when OPEN approved above soft target.
                # This is visible in reason_codes and open_gate_trace.
                # It does NOT block the OPEN — the PAR cap is the economic gate.
                if par_caps_enabled and inv_open_for_regime >= inv_soft_target_for_regime:
                    action.reason_codes.append("SOFT_TARGET_EXCEEDED_ALLOWED")
                actions.append(action)
                # Simulate budget spend to prevent double-counting for next regime
                if action.premium is not None and action.qty is not None:
                    total_cost = action.premium * action.qty
                    budget.spent_today += total_cost
                    budget.spent_week += total_cost
                    budget.spent_month += total_cost
                break  # One open per regime per day

    return actions


def _evaluate_candidate(
    candidate: Dict[str, Any],
    regime: str,
    policy: Dict[str, Any],
    budget: BudgetState,
    max_qty: int,
    eff_thresh: Optional[Dict[str, Any]] = None,
    inv_open_for_regime: int = 0,
    robustness_enabled: bool = False,
    robustness_params: Optional[Dict[str, Any]] = None,
    # v2.0 Task C: premium-at-risk cap params (optional — backward compat when None)
    projected_par_base: Optional[Dict[str, float]] = None,
    par_caps: Optional[Dict[str, Any]] = None,
    par_caps_enabled: bool = False,
) -> Tuple[Optional[AllocatorAction], str]:
    """
    Evaluate a single candidate through all policy gates.

    v1.9 additions:
      - Worst-case debit pricing for EV/$ gating (Task A)
      - Ladder layer classification (Task B)
      - Fragility gating (Task E)

    v2.0 additions:
      - Premium-at-risk cap gate (Task C/D): primary economic gate
        checks projected PAR vs caps after auto-sizing.
        Gate is only active when par_caps_enabled=True.

    Returns:
        (AllocatorAction, "") if passed
        (None, reason) if rejected
    """
    candidate_id = _get_candidate_id(candidate)
    run_id = candidate.get("run_id")
    candidate_rank = candidate.get("rank") or candidate.get("candidate_rank")
    if candidate_rank is not None:
        try:
            candidate_rank = int(candidate_rank)
        except (TypeError, ValueError):
            candidate_rank = None

    # 0. Phase 2A Task A: Annual convexity budget gate
    # Checked first — hard annual cap blocks regardless of EV/convexity quality.
    # gate is disabled when annual_budget_enabled is False (default when key absent from YAML).
    if budget.annual_budget_enabled and budget.spent_ytd >= budget.annual_convexity_budget:
        return None, (
            f"BUDGET_ANNUAL_CAP:"
            f"YTD_SPENT:{budget.spent_ytd:.2f}:"
            f"ANNUAL_BUDGET:{budget.annual_convexity_budget:.2f}"
        )

    # 1. Representable check
    representable = candidate.get("representable")
    if representable is not None and not representable:
        return None, f"NOT_REPRESENTABLE:{candidate_id}"

    # 2. Standard premium extraction (campaign-computed baseline)
    standard_premium = _get_premium(candidate)
    if standard_premium is None:
        return None, f"NO_PREMIUM:{candidate_id}"

    # 3. v1.9 Task A: resolve worst-case premium for gating
    premium_used, premium_src, pricing_detail = _resolve_premium_for_gating(
        candidate, standard_premium
    )

    # 4. EV/$ — recompute ONLY when using a different premium than campaign.
    #
    # Semantic: campaign already computed ev_per_dollar using campaign premium.
    # If gating uses the SAME campaign premium, honour the stored ev_per_dollar
    # (backward compat: existing tests deliberately set ev_per_dollar to a low value
    # to exercise the rejection gate).
    # If gating uses WC or MID premium, we MUST recompute because the execution
    # cost differs from what the campaign assumed.
    p_used = _get_p_used(candidate)
    max_gain = _get_max_gain(candidate)

    if premium_src != "CAMPAIGN" and p_used is not None and max_gain is not None:
        # Recompute EV/$ with actual execution premium (WC or MID)
        ev_per_dollar = _recompute_ev_per_dollar(p_used, max_gain, premium_used)
        if ev_per_dollar is None:
            ev_per_dollar = _get_ev_per_dollar(candidate)
    else:
        # Campaign premium → stored ev_per_dollar is the authoritative gate value
        ev_per_dollar = _get_ev_per_dollar(candidate)

    if ev_per_dollar is None:
        return None, f"NO_EV_PER_DOLLAR:{candidate_id}"

    # EV/$ threshold (inventory-aware)
    p_src = _get_p_used_src(candidate)
    if eff_thresh is not None:
        ev_threshold = (
            eff_thresh["ev_external"]
            if p_src == "external"
            else eff_thresh["ev_implied"]
        )
    else:
        ev_threshold = get_threshold(
            policy, regime, src="external" if p_src == "external" else "implied"
        )

    if ev_per_dollar < ev_threshold:
        return None, (
            f"EV_BELOW_THRESHOLD:{ev_per_dollar:.2f}<{ev_threshold:.2f}:{candidate_id}"
        )

    # 5. Convexity multiple check (uses gating premium for accurate convexity)
    if max_gain is None:
        return None, f"NO_MAX_GAIN:{candidate_id}"

    if eff_thresh is not None:
        required_convexity = eff_thresh["convexity_multiple"]
    else:
        required_convexity = get_convexity_multiple(policy, regime)

    convexity = max_gain / premium_used
    if convexity < required_convexity:
        return None, (
            f"CONVEXITY_TOO_LOW:{convexity:.1f}x<{required_convexity:.1f}x:{candidate_id}"
        )

    # 6. v1.9 Task B: layer classification (crash only)
    layer, moneyness_pct = _classify_ladder_layer(candidate, policy, regime)

    # 7. v1.9 Task E: fragility gating
    fragile: Optional[bool] = None
    shock_detail: Optional[Dict[str, Any]] = None

    if robustness_enabled and robustness_params and p_used is not None and max_gain is not None:
        shock_detail = _compute_ev_shock(p_used, max_gain, premium_used, robustness_params)
        fragile = shock_detail["fragile"]

        if fragile:
            allow_if_empty = robustness_params.get("allow_if_inventory_empty", True)
            if allow_if_empty and inv_open_for_regime == 0:
                # Allow fragile when inventory is empty — tag but don't block
                pass  # fragile=True but will proceed; reason code added below
            else:
                return None, f"EV_FRAGILE_UNDER_SHOCKS:{candidate_id}"

    # 8. Auto-size qty (uses gating premium for conservative sizing)
    qty, size_reason = _autosize_qty(
        premium=premium_used,
        budget=budget,
        max_qty=max_qty,
    )
    if qty is None:
        return None, f"BUDGET_BLOCKED:{size_reason}:{candidate_id}"

    # 8b. v2.0 Task C: Premium-at-risk cap gate (primary economic gate)
    #
    # Checks whether adding this candidate would push projected PAR over the
    # configured caps.  Uses actual planned qty (post auto-sizing) so the
    # check reflects the real commitment, not just a per-contract floor.
    #
    # Gate is only active when par_caps_enabled=True (section present in YAML).
    # When disabled (legacy config), this block is a no-op.
    if par_caps_enabled and projected_par_base is not None and par_caps is not None:
        candidate_par = premium_used * qty
        proj_regime_par = projected_par_base.get(regime, 0.0) + candidate_par
        proj_total_par = projected_par_base.get("total", 0.0) + candidate_par

        regime_cap = par_caps.get(regime, float("inf"))
        total_cap  = par_caps.get("total", float("inf"))

        if proj_regime_par > regime_cap:
            return None, (
                f"PREMIUM_AT_RISK_CAP:"
                f"PROJECTED_{regime.upper()}_PREMIUM_AT_RISK:{proj_regime_par:.2f}:"
                f"{regime.upper()}_PREMIUM_CAP:{regime_cap:.2f}"
            )
        if proj_total_par > total_cap:
            return None, (
                f"PREMIUM_AT_RISK_CAP:"
                f"PROJECTED_TOTAL_PREMIUM_AT_RISK:{proj_total_par:.2f}:"
                f"TOTAL_PREMIUM_CAP:{total_cap:.2f}"
            )

    # 9. Build convexity decomposition (using gating premium)
    convexity_detail = _compute_convexity_detail(
        candidate=candidate,
        premium_per_contract=premium_used,
        max_gain_per_contract=max_gain,
    )

    # 10. Extract metadata for inventory simulation + intent building
    _underlier = candidate.get("underlier") or "SPY"
    _expiry = candidate.get("expiry") or ""
    _strikes_dict = candidate.get("strikes", {}) or {}
    _long_strike = float(
        candidate.get("long_strike") or _strikes_dict.get("long_put", 0) or 0
    )
    _short_strike = float(
        candidate.get("short_strike") or _strikes_dict.get("short_put", 0) or 0
    )
    _cluster_id = candidate.get("cluster_id") or candidate.get("cell_id")

    # 11. Build reason codes
    tier_str = eff_thresh["tier"] if eff_thresh else "unknown"
    reason_codes = [
        f"EV_PER_DOLLAR:{ev_per_dollar:.2f}",
        f"CONVEXITY:{convexity:.1f}x",
        f"P_SRC:{p_src or 'unknown'}",
        f"QTY:{qty}",
        f"PREMIUM_USED:{premium_src}",
        f"PREMIUM_{premium_src}_PER_CONTRACT:{premium_used:.2f}",
        f"TIER:{tier_str}",
    ]
    if budget.kicker_enabled:
        reason_codes.append("KICKER_ACTIVE")
    if layer is not None:
        reason_codes.append(f"LADDER_LAYER:{layer}")
    if moneyness_pct is not None:
        reason_codes.append(f"MONEYNESS_PCT:{moneyness_pct:.1f}")
    if fragile is True:
        reason_codes.append("FRAGILE_ALLOWED_EMPTY")
    if fragile is False:
        reason_codes.append("EV_ROBUST")

    # Add both WC and MID to reason codes when both available (A3 spec)
    pd = pricing_detail
    if pd.get("premium_wc") is not None:
        reason_codes.append(f"PREMIUM_WC_PER_CONTRACT:{pd['premium_wc']:.2f}")
    if pd.get("premium_mid") is not None:
        reason_codes.append(f"PREMIUM_MID_PER_CONTRACT:{pd['premium_mid']:.2f}")

    # 12. Build pricing dict for action.pricing (A4 spec)
    pricing_dict = {
        "premium_used":        round(premium_used, 4),
        "premium_used_source": premium_src,
        "premium_wc":          pd.get("premium_wc"),
        "premium_mid":         pd.get("premium_mid"),
        "debit_wc_share":      pd.get("debit_wc_share"),
        "debit_mid_share":     pd.get("debit_mid_share"),
    }

    # 13. Build open_gate_trace extension with shock details
    if shock_detail:
        reason_codes.append(
            f"EV_BASE:{shock_detail['ev_base']:.2f},"
            f"EV_SHOCK:{shock_detail['ev_shock']:.2f},"
            f"P_SHOCK:{shock_detail['p_shock']:.4f},"
            f"PREM_SHOCK:{shock_detail['premium_shock']:.2f}"
        )

    return AllocatorAction(
        type=ActionType.OPEN,
        candidate_id=candidate_id,
        run_id=run_id,
        candidate_rank=candidate_rank,
        qty=qty,
        premium=premium_used,        # gating premium used for sizing / ledger
        convexity_detail=convexity_detail,
        reason_codes=reason_codes,
        underlier=_underlier,
        regime=regime,
        expiry=_expiry,
        long_strike=_long_strike,
        short_strike=_short_strike,
        cluster_id=_cluster_id,
        # v1.9 new fields
        pricing=pricing_dict,        # Task A
        layer=layer,                 # Task B
        fragile=fragile,             # Task E
    ), ""


def _autosize_qty(
    premium: float,
    budget: BudgetState,
    max_qty: int,
) -> Tuple[Optional[int], str]:
    """
    Compute auto-sized qty.

    Formula:
      qty = floor(daily_soft_cap / premium)
      clamp to [1, max_qty]
      If premium > daily_soft_cap:
        - allow qty=1 only if weekly remaining >= premium
        - else return None (HOLD)
    """
    daily_cap = budget.daily_soft_cap
    remaining_today = budget.remaining_today
    remaining_week = budget.remaining_week

    if remaining_today <= 0:
        return None, "DAILY_BUDGET_EXHAUSTED"

    if budget.remaining_month <= 0:
        return None, "MONTHLY_BUDGET_EXHAUSTED"

    qty = math.floor(daily_cap / premium)

    if qty < 1:
        if remaining_week >= premium and budget.remaining_month >= premium:
            return 1, ""
        else:
            return None, f"PREMIUM_{premium:.0f}>DAILY_CAP_{daily_cap:.0f}_AND_WEEKLY_INSUFFICIENT"

    qty = min(qty, max_qty)

    total_cost = premium * qty
    if not budget.can_spend(total_cost):
        while qty > 1 and not budget.can_spend(premium * qty):
            qty -= 1
        if not budget.can_spend(premium * qty):
            return None, "BUDGET_WOULD_EXCEED_SOFT_CAPS"

    return qty, ""
