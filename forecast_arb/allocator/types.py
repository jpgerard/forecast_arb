"""
CCC v1 Allocator - Shared dataclasses and types.

All types are plain dataclasses (no external deps) to keep
the allocator import-safe even without the full project environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------

class ActionType:
    HARVEST_CLOSE = "HARVEST_CLOSE"
    ROLL_CLOSE = "ROLL_CLOSE"
    OPEN = "OPEN"
    HOLD = "HOLD"


# ---------------------------------------------------------------------------
# Budget state
# ---------------------------------------------------------------------------

@dataclass
class BudgetState:
    monthly_baseline: float
    monthly_max: float
    weekly_baseline: float
    daily_baseline: float
    weekly_kicker: float
    daily_kicker: float

    spent_month: float = 0.0
    spent_week: float = 0.0
    spent_today: float = 0.0

    kicker_enabled: bool = False
    kicker_reasons: List[str] = field(default_factory=list)

    # Planned spend (set in plan.py after all actions are determined)
    # These cover open premiums + estimated close costs (if any)
    planned_spend_today: float = 0.0
    planned_spend_week: float = 0.0
    planned_spend_month: float = 0.0

    # v1.5 Task 2: count of commit-ledger rows that could not be parsed for budget purposes
    # (missing 'action' or missing/invalid 'date').  0 = all rows were usable.
    legacy_unusable_count: int = 0

    # Phase 2A Task A: annual convexity budget
    # annual_convexity_budget: float('inf') means "disabled" (backward-compat default).
    # spent_ytd: 0.0 by default; populated by plan.py from budget_control.py.
    annual_convexity_budget: float = float("inf")
    spent_ytd: float = 0.0

    @property
    def daily_soft_cap(self) -> float:
        return self.daily_kicker if self.kicker_enabled else self.daily_baseline

    @property
    def weekly_soft_cap(self) -> float:
        return self.weekly_kicker if self.kicker_enabled else self.weekly_baseline

    @property
    def monthly_soft_cap(self) -> float:
        return self.monthly_max if self.kicker_enabled else self.monthly_baseline

    # --- "before" = ledger actuals ---
    @property
    def remaining_today_before(self) -> float:
        return max(0.0, self.daily_soft_cap - self.spent_today)

    @property
    def remaining_week_before(self) -> float:
        return max(0.0, self.weekly_soft_cap - self.spent_week)

    @property
    def remaining_month_before(self) -> float:
        return max(0.0, self.monthly_soft_cap - self.spent_month)

    # --- "after" = before minus planned actions ---
    @property
    def remaining_today_after(self) -> float:
        return max(0.0, self.remaining_today_before - self.planned_spend_today)

    @property
    def remaining_week_after(self) -> float:
        return max(0.0, self.remaining_week_before - self.planned_spend_week)

    @property
    def remaining_month_after(self) -> float:
        return max(0.0, self.remaining_month_before - self.planned_spend_month)

    # --- backward-compat aliases (point to "before") ---
    @property
    def remaining_today(self) -> float:
        """Alias for remaining_today_before (backward compat)."""
        return self.remaining_today_before

    @property
    def remaining_week(self) -> float:
        """Alias for remaining_week_before (backward compat)."""
        return self.remaining_week_before

    @property
    def remaining_month(self) -> float:
        """Alias for remaining_month_before (backward compat)."""
        return self.remaining_month_before

    # --- Phase 2A Task A: annual budget helpers ---
    @property
    def annual_budget_enabled(self) -> bool:
        """True when annual_convexity_budget is set to a finite value (not inf)."""
        return self.annual_convexity_budget < 1e15

    @property
    def remaining_annual(self) -> float:
        """Remaining annual convexity budget. Returns inf if not configured."""
        if not self.annual_budget_enabled:
            return float("inf")
        return max(0.0, self.annual_convexity_budget - self.spent_ytd)

    def can_spend(self, amount: float) -> bool:
        """True if amount fits inside all soft caps."""
        return (
            self.spent_today + amount <= self.daily_soft_cap
            and self.spent_week + amount <= self.weekly_soft_cap
            and self.spent_month + amount <= self.monthly_soft_cap
        )


# ---------------------------------------------------------------------------
# Inventory state
# ---------------------------------------------------------------------------

@dataclass
class InventoryState:
    crash_target: int
    crash_open: int
    selloff_target: int
    selloff_open: int

    def needs_open(self, regime: str) -> bool:
        regime = regime.lower()
        if regime == "crash":
            return self.crash_open < self.crash_target
        if regime == "selloff":
            return self.selloff_open < self.selloff_target
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "crash": {"target": self.crash_target, "open": self.crash_open},
            "selloff": {"target": self.selloff_target, "open": self.selloff_open},
        }


# ---------------------------------------------------------------------------
# Sleeve / reconciled position
# ---------------------------------------------------------------------------

@dataclass
class SleevePosition:
    trade_id: str           # intent_id or allocator trade id
    underlier: str
    expiry: str             # YYYYMMDD
    strikes: List[float]    # [long_strike, short_strike]
    qty_open: int
    regime: str
    entry_debit: Optional[float]   # None = MISSING_ENTRY_DEBIT
    mark_mid: Optional[float]      # None = not computed yet
    dte: Optional[int]             # calendar days to expiry
    candidate_id: Optional[str] = None

    # Live spread bid/ask (in $/contract).
    # Populated when IBKR quotes available; used by close-liquidity guard.
    spread_bid: Optional[float] = None
    spread_ask: Optional[float] = None

    # v1.7: entry_debit_net from fills ledger (gross minus commissions, if known).
    # When present, used as premium_basis for harvest multiple instead of gross.
    entry_debit_net: Optional[float] = None

    @property
    def multiple(self) -> Optional[float]:
        """
        Current mark / entry_debit.  None if either is missing.

        v1.7: Uses entry_debit_net as basis if present, else entry_debit (gross).
        Spec §H: premium_basis = entry_debit_net if not null else entry_debit_gross
        """
        # Prefer net debit (commissions included) for accurate harvest multiple
        basis = self.entry_debit_net if self.entry_debit_net is not None else self.entry_debit
        if basis and self.mark_mid is not None:
            return self.mark_mid / basis
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "underlier": self.underlier,
            "expiry": self.expiry,
            "strikes": self.strikes,
            "qty_open": self.qty_open,
            "regime": self.regime,
            "entry_debit": self.entry_debit,
            "mark_mid": self.mark_mid,
            "multiple": self.multiple,
            "dte": self.dte,
            "candidate_id": self.candidate_id,
            "spread_bid": self.spread_bid,
            "spread_ask": self.spread_ask,
        }


# ---------------------------------------------------------------------------
# Allocator action
# ---------------------------------------------------------------------------

@dataclass
class AllocatorAction:
    type: str                                # ActionType constant
    reason_codes: List[str] = field(default_factory=list)
    trade_id: Optional[str] = None
    candidate_id: Optional[str] = None      # canonical id from candidates_flat.json
    run_id: Optional[str] = None            # run_id from campaign (Task 2)
    candidate_rank: Optional[int] = None    # rank within campaign output (Task 2)
    qty: Optional[int] = None
    premium: Optional[float] = None         # per-contract premium (dollars)
    convexity_detail: Optional[Dict[str, Any]] = None  # Task 3 decomposition
    intent_path: Optional[str] = None       # Task 7: path to close-intent JSON (or None)
    notes: Optional[str] = None
    # v1.3: metadata stored on action for intent building + deterministic inventory simulation
    underlier: Optional[str] = None         # e.g. "SPY"
    regime: Optional[str] = None            # "crash" | "selloff" — explicit, no heuristic needed
    expiry: Optional[str] = None            # YYYYMMDD
    long_strike: Optional[float] = None     # BUY leg
    short_strike: Optional[float] = None    # SELL leg
    cluster_id: Optional[str] = None        # campaign cluster / cell_id

    # v1.9 Task A: worst-case / mid pricing detail persisted on action
    # pricing = {premium_used, premium_wc, premium_mid, debit_wc_share,
    #            debit_mid_share, premium_used_source}
    pricing: Optional[Dict[str, Any]] = None

    # v1.9 Task B: crash ladder layer ("A" | "B" | None)
    layer: Optional[str] = None

    # v1.9 Task E: fragility flag — True when EV_shock <= 0
    fragile: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": self.type,
            "reason_codes": self.reason_codes,
        }
        if self.trade_id is not None:
            d["trade_id"] = self.trade_id
        if self.candidate_id is not None:
            d["candidate_id"] = self.candidate_id
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.candidate_rank is not None:
            d["candidate_rank"] = self.candidate_rank
        if self.qty is not None:
            d["qty"] = self.qty
        if self.premium is not None:
            d["premium"] = self.premium
        if self.convexity_detail is not None:
            d["convexity"] = self.convexity_detail
        if self.intent_path is not None:
            d["intent_path"] = self.intent_path
        if self.notes is not None:
            d["notes"] = self.notes
        # v1.9 Task A/B/E: new optional fields
        if self.pricing is not None:
            d["pricing"] = self.pricing
        if self.layer is not None:
            d["layer"] = self.layer
        if self.fragile is not None:
            d["fragile"] = self.fragile
        return d


# ---------------------------------------------------------------------------
# Full allocator plan result
# ---------------------------------------------------------------------------

@dataclass
class AllocatorPlan:
    timestamp_utc: str
    policy_id: str
    budgets: BudgetState
    inventory: InventoryState            # inventory_before (ledger actuals)
    positions: List[SleevePosition]
    actions: List[AllocatorAction]
    notes: List[str] = field(default_factory=list)
    inventory_after: Optional[InventoryState] = None  # after planned actions (Task 4)
    # CCC v1.2 Task 2: trace of gate evaluation when no OPEN was produced
    # and inventory is below target.  None when not applicable.
    open_gate_trace: Optional[Dict[str, Any]] = None
    # v1.6: pending OPEN intents today (regime → count).
    # v1.8: populated from commit_ledger − fills_ledger (not OPEN_*.json timestamp scan).
    pending_open_intents: Dict[str, int] = field(default_factory=dict)
    # v1.8: effective inventory used for gating = actual + pending
    inv_effective: Optional["InventoryState"] = None

    def to_dict(self) -> Dict[str, Any]:
        b = self.budgets
        inv_before = self.inventory
        inv_after = self.inventory_after or self.inventory  # fallback = before if not computed
        inv_eff = self.inv_effective or inv_before  # v1.8: effective = actual + pending

        # v1.8: pending counts from pending_open_intents (populated from commit - fills)
        pending_crash = self.pending_open_intents.get("crash", 0)
        pending_selloff = self.pending_open_intents.get("selloff", 0)

        return {
            "timestamp_utc": self.timestamp_utc,
            "policy_id": self.policy_id,

            # ---- Budget: explicit before/after split (Task 1) ----
            "budgets": {
                # Caps
                "daily_soft_cap": b.daily_soft_cap,
                "weekly_soft_cap": b.weekly_soft_cap,
                "monthly_soft_cap": b.monthly_soft_cap,
                # Config baselines
                "monthly_baseline": b.monthly_baseline,
                "monthly_max": b.monthly_max,
                "weekly_baseline": b.weekly_baseline,
                "daily_baseline": b.daily_baseline,
                # Ledger actuals ("before")
                "spent_today_before": round(b.spent_today, 2),
                "remaining_today_before": round(b.remaining_today_before, 2),
                "spent_week_before": round(b.spent_week, 2),
                "remaining_week_before": round(b.remaining_week_before, 2),
                "spent_month_before": round(b.spent_month, 2),
                "remaining_month_before": round(b.remaining_month_before, 2),
                # Planned (sum of action premiums)
                "planned_spend_today": round(b.planned_spend_today, 2),
                "planned_spend_week": round(b.planned_spend_week, 2),
                "planned_spend_month": round(b.planned_spend_month, 2),
                # After planned actions
                "remaining_today_after": round(b.remaining_today_after, 2),
                "remaining_week_after": round(b.remaining_week_after, 2),
                "remaining_month_after": round(b.remaining_month_after, 2),
                # Kicker
                "kicker_enabled": b.kicker_enabled,
                "kicker_reasons": b.kicker_reasons,
                # Backward-compat aliases
                "spent_today": round(b.spent_today, 2),
                "spent_week": round(b.spent_week, 2),
                "spent_month": round(b.spent_month, 2),
                "remaining_today": round(b.remaining_today_before, 2),
                "weekly_remaining": round(b.remaining_week_before, 2),
                "monthly_remaining": round(b.remaining_month_before, 2),
            },

            # ---- Inventory: before / after / pending / effective (v1.8) ----
            # 'actual'    = real filled positions (from positions.json or plan ledger)
            # 'planned'   = projected after today's proposed actions
            # 'pending'   = committed-not-yet-filled (commit_ledger − fills_ledger)
            # 'effective' = actual + pending (used for gating; blocks duplicate opens)
            "inventory": {
                "before": inv_before.to_dict(),          # backward-compat
                "after": inv_after.to_dict(),            # backward-compat
                "actual": inv_before.to_dict(),          # CCC v1.2: real positions
                "planned": inv_after.to_dict(),          # CCC v1.2: after proposed actions
                # v1.8: pending (committed-not-filled) and effective (actual+pending)
                "pending": {                              # NEW v1.8
                    "crash": pending_crash,
                    "selloff": pending_selloff,
                },
                "effective": inv_eff.to_dict(),          # NEW v1.8: used for OPEN gating
            },

            "positions": [p.to_dict() for p in self.positions],
            "actions": [a.to_dict() for a in self.actions],
            "notes": self.notes,
            # CCC v1.2 Task 2: gate trace (only present when HOLD and inventory < target)
            "open_gate_trace": self.open_gate_trace,
        }
