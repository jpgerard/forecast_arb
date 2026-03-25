# Crash Venture v2: Two-Regime System

**Status:** Foundation Complete - Ready for Integration  
**Date:** February 6, 2026

## Overview

Crash Venture v2 extends the single-event crash hedge system to a **two-regime framework**:

- **CRASH regime** (rare, convex, lottery hedge): terminal move ≤ -15%
- **SELLOFF regime** (tactical downside): terminal move ≤ -8% to -10%

A **deterministic regime selector** decides which regime(s) are eligible each day based on observable market conditions, preventing "crash sleeve drift" into non-crash structures.

---

## What's Been Implemented

### ✅ Part A: Selloff Event Specification

**File:** `forecast_arb/options/event_def.py`

- Extended `EventSpec` dataclass with:
  - `regime: Optional[str]` - "crash" or "selloff"
  - `event_hash: Optional[str]` - unique identifier for event
- Updated `create_event_spec()` to accept `regime` parameter
- Event hash computed from: `{underlier}_{expiry}_{moneyness}_{spot}_{regime}`

**Example:**
```python
crash_spec = create_event_spec(
    underlier="SPY",
    expiry="20260320",
    spot=600.0,
    moneyness=-0.15,
    regime="crash"
)
# Event: P(SPY < $510 at 20260320)
# Event Hash: 81e5f85eb7288b80

selloff_spec = create_event_spec(
    underlier="SPY",
    expiry="20260320", 
    spot=600.0,
    moneyness=-0.09,
    regime="selloff"
)
# Event: P(SPY < $546 at 20260320)
# Event Hash: fb20b016c81cd52d
```

**Regime Boundaries (Guardrails):**
- Crash structures: ≥ -13% OTM (prevents overlap with selloff)
- Selloff structures: -7% to -12% OTM (prevents "crash-ish" structures)

---

### ✅ Part B: Regime Decision Rule

**File:** `forecast_arb/oracle/regime_selector.py`

Deterministic selector that outputs one of four modes:
- `CRASH_ONLY`
- `SELLOFF_ONLY`
- `BOTH`
- `STAND_DOWN`

**Decision Logic (v1 - Simple & Transparent):**

#### A) CRASH Eligibility
Enable crash candidate generation if:
- `p_implied_crash <= 1.5%` (crash not already priced), OR
- (Optional) `drawdown >= 5%` AND `skew elevated`

Otherwise: crash already priced → skip

#### B) SELLOFF Eligibility
Enable selloff candidate generation if:
- `0.08 <= p_implied_selloff <= 0.25` (normal band)

Special handling:
- If `p_implied_selloff > 0.25`: allow but flag `PRICED_IN_WARNING`
- If `p_implied_selloff < 0.08`: too cheap → skip

#### C) STAND_DOWN
If neither eligible → `STAND_DOWN`

#### D) BOTH
If both eligible → `BOTH`

**Conservative Fallback:**
- Missing inputs → `STAND_DOWN` (safe default)

---

### ✅ Acceptance Tests

**File:** `scripts/regime_smoke_test.py`

All tests **PASS**:

#### Test 1: Stable Comparability ✅
- Same inputs produce same regime decision
- Deterministic behavior verified

#### Test 2: No Regime Drift (Boundaries) ✅
- Crash moneyness: -15% (≥ -13% OTM) ✅
- Selloff moneyness: -9% (-7% to -12% OTM) ✅
- Boundaries enforced to prevent regime overlap

#### Test 3: Conservative on Missing Inputs ✅
- Missing `p_implied_crash` AND `p_implied_selloff` → `STAND_DOWN` ✅
- Graceful degradation

**Run test:**
```powershell
python scripts/regime_smoke_test.py
```

---

## Configuration Example

Suggested config structure (not yet integrated):

```yaml
campaign_name: crash_venture_v2

regime_selector:
  crash_p_threshold: 0.015      # 1.5% max for crash eligibility
  selloff_p_min: 0.08           # 8% min for selloff eligibility
  selloff_p_max: 0.25           # 25% max (normal band)
  drawdown_threshold: 0.05      # 5% for crash override
  min_skew_threshold: null      # Not yet implemented

regimes:
  crash:
    moneyness: -0.15            # -15% below spot
    dte_range: [28, 60]
    spread_widths: [15, 20]
    min_otm: -0.13              # Guardrail: ≥ -13% OTM
  
  selloff:
    moneyness: -0.09            # -9% below spot (configurable: -0.08, -0.10)
    dte_range: [28, 45]
    spread_widths: [10, 15]
    otm_bounds: [-0.07, -0.12]  # Guardrail: -7% to -12% OTM
```

---

## What's NOT Implemented Yet

The following components are **specified but not coded**:

### 🔲 Part A2: Selloff Structure Set
- Debit put spread generation for selloff regime
- Width selection logic (default $15 for $1 increments, $20 for $5)
- Liquidity ranking and slippage-adjusted pricing
- **Location:** Extend `forecast_arb/engine/crash_venture_v1_snapshot.py`

### 🔲 Part C: Pipeline Integration
- Multi-event processing in `run_daily.py`
- CLI flags: `--regime auto|crash|selloff|both`
- Separate candidate tables in review pack
- **Location:** `scripts/run_daily.py`

### 🔲 Part D: Intent Emission Updates
- Bind intent to `regime + event_hash`
- Require `--regime` flag for intent emission
- Abort if rank doesn't exist for regime
- **Location:** `forecast_arb/execution/intent_builder.py`

### 🔲 Part E: Review Pack Multi-Regime Output
- Regime selector section
- Separate candidate tables for crash/selloff
- Event representability status per regime
- **Location:** `forecast_arb/review/review_pack.py`

---

## Design Decisions

### Why Two Regimes?

1. **Crash regime** (rare, convex): 
   - Lottery-like payoff when markets collapse
   - Should NOT be active when crash already priced
   - Moneyness ≤ -15% ensures true "crash" exposure

2. **Selloff regime** (tactical):
   - More frequent, smaller drawdowns
   - Bridges gap between normal vol and crash
   - Moneyness -8% to -10% captures tactical downside

### Why Deterministic Selection?

- **Transparency:** Rule-based, auditable
- **No drift:** Explicit boundaries prevent crash structures from becoming selloff-like
- **Conservative:** Defaults to `STAND_DOWN` on missing data
- **Forward compatible:** Can add ML/adaptive rules later

### Event Hash Purpose

- **Uniqueness:** Prevents confusion between crash and selloff events on same expiry
- **Intent binding:** OrderIntent references exact event spec
- **Auditability:** Track which event a structure was designed for

---

## Usage (Once Integrated)

### Scenario 1: Auto-Select Regime (Recommended)
```powershell
python scripts/run_daily.py `
    --underlier SPY `
    --regime auto `
    --review-only-structuring
```

Regime selector will:
1. Compute `p_implied_crash` (-15% event)
2. Compute `p_implied_selloff` (-9% event)
3. Decide which regime(s) eligible
4. Generate candidates for eligible regimes only

### Scenario 2: Force Crash Only
```powershell
python scripts/run_daily.py `
    --regime crash
```

### Scenario 3: Generate Both for Comparison
```powershell
python scripts/run_daily.py `
    --regime both
```

### Scenario 4: Emit Intent for Specific Regime
```powershell
# After review pack generated with --regime both
python scripts/run_daily.py `
    --emit-intent `
    --regime selloff `
    --pick-rank 1
```

Intent will include:
```json
{
  "regime": "selloff",
  "event_spec_hash": "fb20b016c81cd52d",
  "expiry": "20260320",
  "strikes": {...}
}
```

---

## Testing Strategy

### Unit Tests (Completed)
✅ `scripts/regime_smoke_test.py` - All scenarios pass

### Integration Tests (TODO)
- [ ] Run with actual snapshot → verify dual event computation
- [ ] Test regime selector with real p_implied values
- [ ] Verify candidate tables separated by regime
- [ ] Test intent emission with both regimes

### Regression Tests (TODO)
- [ ] Ensure crash-only mode still works (backward compat)
- [ ] Verify existing crash venture v1 configs unaffected
- [ ] Test edge cases (missing expiries, no representable events)

---

## Migration Path

### Phase 1: Foundation (✅ COMPLETE)
- [x] Regime selector implementation
- [x] EventSpec regime support
- [x] Smoke tests passing

### Phase 2: Pipeline Integration (TODO)
- [ ] Update `run_daily.py` with `--regime` flag
- [ ] Compute p_implied for both crash and selloff events
- [ ] Wire regime selector into decision flow
- [ ] Generate separate candidate lists

### Phase 3: Review Pack (TODO)
- [ ] Add regime selector section to review pack
- [ ] Separate tables for crash/selloff candidates
- [ ] Show event representability per regime

### Phase 4: Intent System (TODO)
- [ ] Update intent schema with `regime` field
- [ ] Bind intent to `event_spec_hash`
- [ ] Update intent builder validation

### Phase 5: Testing & Hardening (TODO)
- [ ] Integration tests with real snapshots
- [ ] Regression tests for backward compatibility
- [ ] Production smoke test

---

## Default Values (Hardcoded First)

Per spec, these are the initial defaults:

### Regime Selector
- `crash_p_threshold: 0.015` (1.5%)
- `selloff_p_min: 0.08` (8%)
- `selloff_p_max: 0.25` (25%)

### Event Specs
- Crash moneyness: `-0.15` (-15%)
- Selloff moneyness: `-0.09` (-9%)
  - Alternates allowed: `-0.08`, `-0.10`

### OTM Boundaries (Guardrails)
- Crash: `≤ -13%` OTM
- Selloff: `-7%` to `-12%` OTM

---

## Architecture Notes

### Single Source of Truth: EventSpec

Before v2:
```python
# threshold recomputed in multiple places → drift risk
threshold = spot * (1 + moneyness)
```

After v2:
```python
# Created ONCE, passed everywhere
event_spec = create_event_spec(
    underlier="SPY",
    expiry=expiry,
    spot=spot,
    moneyness=-0.15,
    regime="crash"
)
# threshold = event_spec.threshold (canonical)
```

### Regime Selector: Observable Inputs Only

Uses only data you already have:
- `p_implied_crash` - from options-implied calc
- `p_implied_selloff` - from options-implied calc
- `drawdown` - optional, from price history
- `skew` - optional, from IV surface

No new data dependencies introduced.

### Conservative Defaults

- Missing inputs → `STAND_DOWN`
- Unknown regime → skip
- Boundary violations → log + skip
- No silent failures

---

## Next Steps

1. **Immediate:**
   - Review this README with stakeholders
   - Decide on integration timeline
   - Choose whether to config-drive or hardcode defaults initially

2. **Phase 2 Work (Pipeline Integration):**
   - Update `run_daily.py` with regime flag(s)
   - Compute dual p_implied values
   - Generate separate candidate lists
   - Test with real snapshot

3. **Phase 3 Work (Review Pack):**
   - Add regime selector section
   - Render separate tables
   - Include event hashes

4. **Phase 4 Work (Intent System):**
   - Update intent schema
   - Validate regime binding
   - Test intent → execution flow

---

## Questions for Review

1. **Moneyness Choice:** Use -9% as default for selloff, or make it user-selectable from {-8%, -9%, -10%}?

2. **Expiry Selection:** Same DTE range for both regimes, or different (e.g., crash 30-60, selloff 28-45)?

3. **Width Defaults:** Should selloff use narrower spreads than crash (e.g., $10-15 vs $15-20)?

4. **Auto Mode:** Should `--regime auto` be the default, or require explicit opt-in?

5. **Backward Compat:** Keep existing crash venture v1 configs running unchanged?

---

## References

- **Original Spec:** Task description (this README captures it)
- **Regime Selector:** `forecast_arb/oracle/regime_selector.py`
- **EventSpec:** `forecast_arb/options/event_def.py`
- **Smoke Test:** `scripts/regime_smoke_test.py`
- **Crash Venture v1:** `CRASH_VENTURE_V1_README.md`

---

## Summary

✅ **Foundation Complete:**
- Regime selector implemented and tested
- EventSpec extended with regime support
- All acceptance tests passing

🔲 **Integration Needed:**
- Pipeline updates (`run_daily.py`)
- Review pack rendering
- Intent emission binding

**Next Action:** Review this document, then begin Phase 2 (Pipeline Integration).
