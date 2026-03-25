"""
CCC v1 Allocator - Policy loader and validator.

Loads allocator_ccc_v1.yaml and validates all required fields.
Fails loud on missing canonical fields.

v1.6 additions:
  - Inventory-aware thresholds: fill_when_empty / add_when_full per regime.
  - _normalize_thresholds() converts legacy flat keys to nested structure.
  - get_effective_thresholds(policy, regime, inv_mid) selects the correct tier.
  - Backward compat: legacy flat keys treated as add_when_full; fill_when_empty
    is set to the same values (conservative).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import yaml

if TYPE_CHECKING:
    from .types import InventoryState


# Required top-level sections
_REQUIRED_SECTIONS = [
    "policy_id",
    "budgets",
    "inventory_targets",
    "thresholds",
    "harvest",
    "sizing",
    "kicker",
]

# Required budget fields
_REQUIRED_BUDGET_FIELDS = [
    "monthly_baseline",
    "monthly_max",
    "weekly_baseline",
    "daily_baseline",
    "weekly_kicker",
    "daily_kicker",
]

# Required harvest fields
_REQUIRED_HARVEST_FIELDS = [
    "partial_close_multiple",
    "full_close_multiple",
    "time_stop_dte",
    "time_stop_min_multiple",
    "partial_close_fraction",
]

# Required sizing fields
_REQUIRED_SIZING_FIELDS = [
    "max_qty_per_trade",
]

# Required kicker fields
_REQUIRED_KICKER_FIELDS = [
    "min_conditioning_confidence",
    "max_vix_percentile",
]

# Required inventory targets
_REQUIRED_INVENTORY_FIELDS = ["crash", "selloff"]

# Threshold tier sub-keys
_THRESHOLD_FIELDS = ["ev_per_dollar_implied", "ev_per_dollar_external", "convexity_multiple"]


class PolicyError(ValueError):
    """Raised when the policy config is invalid."""


# ---------------------------------------------------------------------------
# Threshold normalisation (v1.6)
# ---------------------------------------------------------------------------

def _normalize_thresholds(policy: Dict[str, Any]) -> None:
    """
    Normalize legacy flat threshold keys to fill_when_empty / add_when_full structure.

    Modifies policy["thresholds"] in-place.

    Backward-compat rule (spec §Task A):
      If the regime block uses legacy flat keys (ev_per_dollar_implied, etc.),
      treat them as add_when_full and set fill_when_empty = add_when_full
      (same values — conservative default until operator overrides).
    """
    thresholds = policy["thresholds"]
    for regime in ["crash", "selloff"]:
        t = thresholds.get(regime, {})
        if not isinstance(t, dict):
            continue
        # Already normalised → skip
        if "fill_when_empty" in t or "add_when_full" in t:
            continue
        # Legacy flat keys → normalise
        if "ev_per_dollar_implied" in t:
            legacy_tier: Dict[str, float] = {
                "ev_per_dollar_implied": float(t.get("ev_per_dollar_implied", 0.0)),
                "ev_per_dollar_external": float(t.get("ev_per_dollar_external", 0.0)),
                "convexity_multiple": float(t.get("convexity_multiple", 0.0)),
            }
            thresholds[regime] = {
                "fill_when_empty": dict(legacy_tier),   # same as add (conservative)
                "add_when_full": dict(legacy_tier),
            }


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------

def load_policy(config_path: str) -> Dict[str, Any]:
    """
    Load and validate the allocator policy YAML.

    Args:
        config_path: Path to allocator_ccc_v1.yaml

    Returns:
        Validated policy dict (thresholds normalised to fill_when_empty/add_when_full)

    Raises:
        PolicyError: on any missing or invalid field
        FileNotFoundError: if config file doesn't exist
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Allocator policy config not found: {config_path}")

    with open(path, "r") as f:
        policy = yaml.safe_load(f)

    if not isinstance(policy, dict):
        raise PolicyError(f"Policy YAML must be a mapping, got {type(policy)}")

    # Check top-level sections
    for section in _REQUIRED_SECTIONS:
        if section not in policy:
            raise PolicyError(f"Policy YAML missing required section: '{section}'")

    # Validate budgets
    budgets = policy["budgets"]
    for field in _REQUIRED_BUDGET_FIELDS:
        if field not in budgets:
            raise PolicyError(f"Policy budgets missing required field: '{field}'")
        if not isinstance(budgets[field], (int, float)):
            raise PolicyError(f"Policy budgets.{field} must be numeric, got {type(budgets[field])}")
        if budgets[field] < 0:
            raise PolicyError(f"Policy budgets.{field} must be >= 0, got {budgets[field]}")

    # Validate monthly_max >= monthly_baseline
    if budgets["monthly_max"] < budgets["monthly_baseline"]:
        raise PolicyError(
            f"monthly_max ({budgets['monthly_max']}) must be >= monthly_baseline ({budgets['monthly_baseline']})"
        )

    # Validate inventory targets
    inv = policy["inventory_targets"]
    for regime in _REQUIRED_INVENTORY_FIELDS:
        if regime not in inv:
            raise PolicyError(f"Policy inventory_targets missing regime: '{regime}'")
        if not isinstance(inv[regime], int) or inv[regime] < 0:
            raise PolicyError(f"Policy inventory_targets.{regime} must be non-negative int")

    # Validate thresholds (supports both legacy flat and new nested structure)
    thresholds = policy["thresholds"]
    for regime in ["crash", "selloff"]:
        if regime not in thresholds:
            raise PolicyError(f"Policy thresholds missing regime: '{regime}'")
        t = thresholds[regime]
        if not isinstance(t, dict):
            raise PolicyError(f"Policy thresholds.{regime} must be a mapping")

        # Detect and validate structure
        has_nested = "fill_when_empty" in t or "add_when_full" in t
        if has_nested:
            # New nested structure: require at least add_when_full
            if "add_when_full" not in t:
                raise PolicyError(
                    f"Policy thresholds.{regime}: new nested structure requires 'add_when_full'"
                )
            for tier_name in ("add_when_full", "fill_when_empty"):
                tier_data = t.get(tier_name)
                if tier_data is None:
                    continue   # fill_when_empty is optional (falls back to add_when_full)
                if not isinstance(tier_data, dict):
                    raise PolicyError(
                        f"Policy thresholds.{regime}.{tier_name} must be a mapping"
                    )
                for field in _THRESHOLD_FIELDS:
                    if field not in tier_data:
                        raise PolicyError(
                            f"Policy thresholds.{regime}.{tier_name} missing field: '{field}'"
                        )
        else:
            # Legacy flat structure: require three flat keys
            for field in _THRESHOLD_FIELDS:
                if field not in t:
                    raise PolicyError(f"Policy thresholds.{regime} missing field: '{field}'")

    # Normalise thresholds to fill_when_empty/add_when_full structure
    _normalize_thresholds(policy)

    # Validate harvest
    harvest = policy["harvest"]
    for field in _REQUIRED_HARVEST_FIELDS:
        if field not in harvest:
            raise PolicyError(f"Policy harvest missing required field: '{field}'")

    # Validate sizing
    sizing = policy["sizing"]
    for field in _REQUIRED_SIZING_FIELDS:
        if field not in sizing:
            raise PolicyError(f"Policy sizing missing required field: '{field}'")

    # Validate kicker
    kicker = policy["kicker"]
    for field in _REQUIRED_KICKER_FIELDS:
        if field not in kicker:
            raise PolicyError(f"Policy kicker missing required field: '{field}'")

    return policy


# ---------------------------------------------------------------------------
# Threshold helpers (v1.6 inventory-aware)
# ---------------------------------------------------------------------------

def get_effective_thresholds(
    policy: Dict[str, Any],
    regime: str,
    inv_mid: "InventoryState",
) -> Dict[str, Any]:
    """
    Return the effective threshold tier and values for a regime, given current inventory.

    Selects:
      fill_when_empty  — when inventory is below target (needs fill)
      add_when_full    — when inventory is at or above target (already covered)

    Falls back gracefully:
      - If fill_when_empty is absent, use add_when_full.
      - If only legacy flat keys exist (policy not yet normalised), use those.

    Returns:
      {
        "tier": "fill_when_empty" | "add_when_full",
        "ev_implied": float,
        "ev_external": float,
        "convexity_multiple": float,
      }
    """
    t = policy["thresholds"].get(regime.lower(), {})

    # Determine tier based on inventory need
    needs_fill = inv_mid.needs_open(regime)
    tier = "fill_when_empty" if needs_fill else "add_when_full"

    # Resolve tier data with fallback chain
    tier_data: Optional[Dict[str, Any]] = t.get(tier)
    if not tier_data:
        # Fall back to other tier
        other_tier = "add_when_full" if tier == "fill_when_empty" else "fill_when_empty"
        tier_data = t.get(other_tier)
    if not tier_data:
        # Final fallback: raw dict may contain legacy flat keys
        tier_data = t

    return {
        "tier": tier,
        "ev_implied": float((tier_data or {}).get("ev_per_dollar_implied", 0.0)),
        "ev_external": float((tier_data or {}).get("ev_per_dollar_external", 0.0)),
        "convexity_multiple": float((tier_data or {}).get("convexity_multiple", 0.0)),
    }


def get_budget_params(policy: Dict[str, Any]) -> Dict[str, float]:
    """Extract budget parameters from policy."""
    return dict(policy["budgets"])


def get_inventory_targets(policy: Dict[str, Any]) -> Dict[str, int]:
    """Extract inventory soft targets {regime: target_count}.

    CCC v2.1: These are SOFT targets / preferred steady state.
    They select between fill_when_empty and add_when_full EV threshold tiers.
    They do NOT block OPEN when premium_at_risk_caps is configured and below cap.
    Hard absolute limits are in inventory_hard_caps (see get_inventory_hard_caps).
    """
    return dict(policy["inventory_targets"])


def get_threshold(policy: Dict[str, Any], regime: str, src: str) -> float:
    """
    Get EV/$ threshold for a regime + probability source type.

    Uses add_when_full tier after normalisation (strictest — backward-compat default).

    Args:
        regime: 'crash' or 'selloff'
        src: 'external' -> use external threshold, else implied threshold

    Returns:
        EV/$ minimum required
    """
    t = policy["thresholds"].get(regime.lower(), {})
    # After normalisation, add_when_full always present.
    # Before normalisation (direct-dict usage in tests), fall to flat keys.
    tier_data: Dict[str, Any] = t.get("add_when_full") or t
    if src == "external":
        return float(tier_data.get("ev_per_dollar_external", 0.0))
    return float(tier_data.get("ev_per_dollar_implied", 0.0))


def get_convexity_multiple(policy: Dict[str, Any], regime: str) -> float:
    """Get required convexity multiple (max_gain/premium) for regime.

    Uses add_when_full tier after normalisation (strictest — backward-compat default).
    """
    t = policy["thresholds"].get(regime.lower(), {})
    tier_data: Dict[str, Any] = t.get("add_when_full") or t
    return float(tier_data.get("convexity_multiple", 0.0))


def get_harvest_params(policy: Dict[str, Any]) -> Dict[str, float]:
    """Extract harvest rule parameters."""
    return dict(policy["harvest"])


def get_sizing_params(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Extract sizing parameters."""
    return dict(policy["sizing"])


def get_kicker_params(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Extract kicker eligibility parameters."""
    return dict(policy["kicker"])


def get_plan_ledger_path(policy: Dict[str, Any]) -> Path:
    """Return path to the plan-only ledger JSONL (written during planning, NOT by ccc_execute).

    This ledger records planned OPENs, HARVEST_CLOSE, ROLL_CLOSE, and DAILY_SUMMARY.
    It is used for inventory state (reconcile/compute_inventory_state).
    It is NOT used for budget spend tracking (see get_commit_ledger_path).
    """
    base = policy.get("ledger_dir", "runs/allocator")
    return Path(base) / "allocator_plan_ledger.jsonl"


def get_commit_ledger_path(policy: Dict[str, Any]) -> Path:
    """Return path to the commit ledger JSONL (written ONLY by ccc_execute.py after staging).

    This ledger records only intents that have been committed (staged / transmitted).
    BudgetState.spent_today_before is derived from this ledger only.
    Running CCC multiple times in a day does NOT increase committed spend.
    """
    base = policy.get("ledger_dir", "runs/allocator")
    return Path(base) / "allocator_commit_ledger.jsonl"


def get_ledger_path(policy: Dict[str, Any]) -> Path:
    """Return path to the allocator plan ledger JSONL file.

    Backward-compat alias for get_plan_ledger_path().
    Callers that pass the result to compute_budget_state() should switch to
    get_commit_ledger_path() — budget now reads from the commit ledger only.
    """
    return get_plan_ledger_path(policy)


def get_actions_path(policy: Dict[str, Any]) -> Path:
    """Return path for writing allocator_actions.json."""
    base = policy.get("output_dir", "runs/allocator")
    return Path(base) / "allocator_actions.json"


def get_intents_dir(policy: Dict[str, Any]) -> Path:
    """Return directory for writing close-intent JSON files."""
    base = policy.get("intents_dir", "intents/allocator")
    return Path(base)


def get_limits(policy: Dict[str, Any]) -> Dict[str, int]:
    """
    Return daily action cap limits.

    Defaults (very permissive) used if 'limits' section absent
    so the guard is optional and backward-compatible.
    """
    limits = policy.get("limits", {})
    return {
        "max_open_actions_per_day": int(limits.get("max_open_actions_per_day", 999)),
        "max_close_actions_per_day": int(limits.get("max_close_actions_per_day", 999)),
    }


def get_close_liquidity_guard(policy: Dict[str, Any]) -> Dict[str, float]:
    """
    Return close-liquidity guard config.

    max_width_pct defaults to 1.0 (100%) = effectively disabled when section absent.
    """
    guard = policy.get("close_liquidity_guard", {})
    return {
        "max_width_pct": float(guard.get("max_width_pct", 1.0)),
    }


def get_positions_path(policy: Dict[str, Any]) -> Path:
    """
    Return path to positions.json snapshot (written by ccc_reconcile).

    v1.7: This file is the authoritative source for inventory.actual.
    If it exists, plan.py will use it instead of the plan ledger for
    crash_open / selloff_open counts.

    Default: runs/allocator/positions.json
    Can be overridden via policy key 'output_dir'.
    """
    base = policy.get("output_dir", "runs/allocator")
    return Path(base) / "positions.json"


def get_fills_ledger_path(policy: Dict[str, Any]) -> Path:
    """
    Return path to allocator_fills_ledger.jsonl (written by ccc_reconcile).

    v1.7: Append-only fills ledger. Authoritative record of all POSITION_OPENED events.
    Default: runs/allocator/allocator_fills_ledger.jsonl
    """
    base = policy.get("output_dir", "runs/allocator")
    return Path(base) / "allocator_fills_ledger.jsonl"


# ---------------------------------------------------------------------------
# v1.9 param helpers (all return dicts with safe defaults — never raise)
# ---------------------------------------------------------------------------

def get_diversity_params(policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return strike diversity guard parameters with safe defaults.

    Phase 2A Task C: prevents opening economically similar spreads.
    If the diversity section is absent, returns enabled=False (gate disabled).

    Returns:
        {
            "enabled": bool,                      # True when section present & pct > 0
            "min_strike_distance_pct": float,     # % of spot; 0.0 = disabled
        }
    """
    d = policy.get("diversity", {})
    raw_pct = d.get("min_strike_distance_pct")
    if raw_pct is None:
        return {"enabled": False, "min_strike_distance_pct": 0.0}
    pct = float(raw_pct)
    return {"enabled": pct > 0, "min_strike_distance_pct": pct}


def get_annual_budget_params(policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return annual convexity budget parameters with safe defaults.

    Phase 2A Task A: annual premium burn limit.
    If annual_convexity_budget is absent, returns float('inf') (disabled).

    Returns:
        {
            "annual_convexity_budget": float,  # float('inf') = disabled
            "enabled": bool,                    # True when a finite limit is set
        }
    """
    b = policy.get("budgets", {})
    raw = b.get("annual_convexity_budget")
    if raw is None:
        return {"annual_convexity_budget": float("inf"), "enabled": False}
    val = float(raw)
    return {"annual_convexity_budget": val, "enabled": val < 1e15}


def get_robustness_params(policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return fragility-gating parameters with safe defaults.

    Policy key: robustness
    Defaults (if section absent or key missing):
      enabled=False, p_downshift_pp=3.0, debit_upshift_pct=10.0,
      require_positive_ev_under_shocks=True, allow_if_inventory_empty=True
    """
    r = policy.get("robustness", {})
    return {
        "enabled":                          bool(r.get("enabled", False)),
        "p_downshift_pp":                   float(r.get("p_downshift_pp", 3.0)),
        "debit_upshift_pct":                float(r.get("debit_upshift_pct", 10.0)),
        "require_positive_ev_under_shocks": bool(r.get("require_positive_ev_under_shocks", True)),
        "allow_if_inventory_empty":         bool(r.get("allow_if_inventory_empty", True)),
    }


def get_roll_params(policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return roll-discipline parameters with safe defaults.

    Policy key: roll
    Defaults (if section absent or key missing):
      enabled=False, dte_max_for_roll=21,
      min_multiple_to_hold=1.10, min_convexity_multiple_to_hold=8.0
    """
    r = policy.get("roll", {})
    return {
        "enabled":                      bool(r.get("enabled", False)),
        "dte_max_for_roll":             int(r.get("dte_max_for_roll", 21)),
        "min_multiple_to_hold":         float(r.get("min_multiple_to_hold", 1.10)),
        "min_convexity_multiple_to_hold": float(r.get("min_convexity_multiple_to_hold", 8.0)),
    }


# ---------------------------------------------------------------------------
# v2.0 param helpers — premium-at-risk caps + inventory hard caps
# ---------------------------------------------------------------------------

def get_premium_at_risk_caps(policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return premium-at-risk caps per regime and total, with safe defaults.

    Policy key: premium_at_risk_caps
    When section is absent or empty, all caps default to float('inf')
    and enabled=False → backward-compat / gate disabled.

    Returns:
        {
            "crash":   float,   # float('inf') = disabled
            "selloff": float,   # float('inf') = disabled
            "total":   float,   # float('inf') = disabled
            "enabled": bool,    # True only when section is present and non-empty
        }
    """
    caps = policy.get("premium_at_risk_caps", {})
    if not caps:
        return {
            "crash":   float("inf"),
            "selloff": float("inf"),
            "total":   float("inf"),
            "enabled": False,
        }
    return {
        "crash":   float(caps.get("crash",   float("inf"))),
        "selloff": float(caps.get("selloff", float("inf"))),
        "total":   float(caps.get("total",   float("inf"))),
        "enabled": True,
    }


# Minimum hard cap floors used when inventory_hard_caps section is absent.
# Prevents inventory_targets (soft) from becoming a hard count blocker when
# premium_at_risk_caps is configured.  The PAR budget gates first; these floors
# ensure the count backstop is at least sensible, not just the soft target.
# CCC v2.1: crash floor=3, selloff floor=2.
_HARD_CAP_FALLBACK_FLOORS: Dict[str, int] = {"crash": 3, "selloff": 2}


def get_inventory_hard_caps(policy: Dict[str, Any]) -> Dict[str, int]:
    """
    Return inventory hard caps (absolute maximum count per regime).

    CCC v2.1 — inventory_hard_caps vs inventory_targets semantics:
      inventory_targets  = SOFT target / preferred steady state.
                           Selects between fill_when_empty and add_when_full
                           EV threshold tiers.  Does NOT block OPEN when
                           premium_at_risk_caps is configured and below cap.
      inventory_hard_caps = HARD absolute backstop on open position count.
                            Blocks OPEN regardless of PAR budget.
                            Prevents over-fragmentation.

    Policy key: inventory_hard_caps (optional section).

    Fallback when section is absent:
      hard_cap = max(inventory_target, floor)
      floors: crash=3, selloff=2
      This prevents a soft target of 1 from acting as a hard blocker when
      premium-at-risk budget allows more positions.

    Returns:
        {"crash": int, "selloff": int}
        Values default to 999 (effectively unlimited) when missing entirely.
    """
    hard_caps = policy.get("inventory_hard_caps", {})
    if hard_caps:
        return {
            "crash":   int(hard_caps.get("crash",   999)),
            "selloff": int(hard_caps.get("selloff", 999)),
        }
    # Fallback: max(inventory_target, floor) so soft target does not become hard cap.
    # Only relevant when premium_at_risk_caps is configured (par_caps_enabled=True);
    # legacy mode (no PAR caps) uses inventory.needs_open() instead of hard_caps.
    targets = policy.get("inventory_targets", {})
    return {
        "crash":   max(
            int(targets.get("crash",   0)),
            _HARD_CAP_FALLBACK_FLOORS.get("crash", 3),
        ),
        "selloff": max(
            int(targets.get("selloff", 0)),
            _HARD_CAP_FALLBACK_FLOORS.get("selloff", 2),
        ),
    }


def get_ladder_params(policy: Dict[str, Any], regime: str = "crash") -> Optional[Dict[str, Any]]:
    """
    Return crash ladder layer parameters for the given regime.

    Returns None if the ladder section is absent (backward compat).
    """
    ladder = policy.get("thresholds", {}).get(regime.lower(), {}).get("ladder")
    if not ladder:
        return None
    return {
        "layer_a": {
            "moneyness_min_pct": float(
                ladder.get("layer_a", {}).get("moneyness_min_pct", 5.0)
            ),
            "moneyness_max_pct": float(
                ladder.get("layer_a", {}).get("moneyness_max_pct", 9.0)
            ),
        },
        "layer_b": {
            "moneyness_min_pct": float(
                ladder.get("layer_b", {}).get("moneyness_min_pct", 10.0)
            ),
            "moneyness_max_pct": float(
                ladder.get("layer_b", {}).get("moneyness_max_pct", 16.0)
            ),
        },
    }
