# Phase 3 — Campaign Matrix Implementation Complete

**Status:** ✅ COMPLETE  
**Date:** 2026-02-24  
**Convention:** `premium_usd = debit_per_contract * qty` (no ×100 multiplier)

## Summary

Phase 3 delivers a **portfolio-aware campaign matrix** that generates candidates across multiple cells (underlier × regime × expiry_bucket) and applies hard governors to select 0-2 recommended trades per day.

**Core Design Principle:** Add a campaign layer without changing strategy math, structuring, execution, or ledger writing.

---

## Deliverables

### A) Configuration

**File:** `configs/campaign_v1.yaml`

```yaml
underliers: [SPY, QQQ]
regimes:
  - {name: crash, threshold: -0.15}
  - {name: selloff, threshold: -0.09}
expiry_buckets:
  - {name: dte_30_60, dte_min: 30, dte_max: 60}
cluster_map:
  SPY: US_INDEX
  QQQ: US_INDEX
governors:
  sleeve_usd: 100000
  daily_premium_cap_usd: 1250
  cluster_cap_per_day: 1
  max_open_positions_by_regime: {crash: 3, selloff: 4}
  premium_at_risk_caps_usd: {crash: 3000, selloff: 4000, total: 7000}
selection:
  max_trades_per_day: 2
  scoring: ev_per_dollar
```

### B) Portfolio Positions View

**Module:** `forecast_arb/portfolio/positions_view.py`

- **Reads:** `runs/trade_outcomes.jsonl` (event-based ledger)
- **Derives:**
  - `open_positions`: intents with `FILLED_OPEN` and no later `CLOSED`
  - `pending_orders`: intents with `SUBMITTED_LIVE` and no later fills/closes
  - `open_premium_by_regime`: sum of entry premiums by regime
  - `open_count_by_regime`: count of open positions by regime
  - `open_clusters`: set of cluster_ids with open positions
- **Robust:** Ignores legacy rows missing "event" field
- **Helper:** `compute_premium_usd(row)` uses standardized convention

**Convention:**
```python
premium_usd = debit_per_contract * qty  # No ×100 multiplier
```

### C) Grid Runner

**Module:** `forecast_arb/campaign/grid_runner.py`

- **Orchestrates** structuring across all (underlier × regime × expiry_bucket) cells
- **Reuses** existing `run_regime()` from `run_daily_v2.py` — **no duplication** of strategy logic
- **Filters** candidates to DTE range of each bucket
- **Flattens** to canonical schema with required metadata:
  - `underlier`, `regime`, `expiry_bucket`, `cluster_id`, `cell_id`
  - `candidate_id`, `expiry`, `long_strike`, `short_strike`
  - `debit_per_contract`, `ev_per_dollar`, `prob_profit`, `max_gain_per_contract`
  - `representable`, `rank`, `spread_width`, `warnings`
- **Outputs:**
  - `runs/campaign/<run_id>/manifest.json`
  - `runs/campaign/<run_id>/candidates_flat.json`
  - `runs/campaign/<run_id>/cells/<cell_id>.json` (optional)

### D) Selector (Portfolio-Aware Governor)

**Module:** `forecast_arb/campaign/selector.py`

**Governor Rules (Enforced in Order):**

1. **Representability:** Filter `representable == True` only
2. **Daily Limits** (NEW selections):
   - `sum(candidate_premium_usd) ≤ daily_premium_cap_usd` (1250)
   - `≤ cluster_cap_per_day` trades per `cluster_id` (1)
3. **Open Exposure** (existing + new):
   - `open_count_by_regime[r] < max_open_positions_by_regime[r]`
   - `open_premium_by_regime[r] + new ≤ premium_at_risk_caps_usd[r]`
   - `open_premium_total + new_total ≤ premium_at_risk_caps_usd[total]`
4. **Selection Method:**
   - Sort by `ev_per_dollar` desc
   - Tie-break: higher `prob_profit`, then lower `premium_usd`
   - Greedy pick while constraints allow
   - Stop at `max_trades_per_day` (2)

**Outputs:**
- `runs/campaign/<run_id>/recommended.json` with:
  - `selected`: 0-2 candidates with reasons and computed premiums
  - `rejected_top10`: rejected candidates with blocking constraints

**Deterministic:** Same input → same output (auditable)

### E) Daily Console Integration

**File:** `scripts/daily.py`

**New Flag:** `--campaign configs/campaign_v1.yaml`

**When Provided:**
1. Run `grid_runner` → produces `candidates_flat.json`
2. Run `selector` → produces `recommended.json`
3. Print **RECOMMENDED SET** table (0-2 rows):
   ```
   # | UNDERLIER | REGIME | EXPIRY   | STRIKES    | EV/$  | P(Win) | PREMIUM | CLUSTER
   1 | SPY       | crash  | 20260402 | 580/560    | 25.16 | 70.6%  | $49     | US_INDEX
   ```
4. Guide user to next steps (use `intent_builder` + `execute_trade` for each)

**Non-Campaign Behavior:** Unchanged (standard single-regime flow)

### F) Tests

**File:** `tests/test_campaign_selector.py`

- ✅ `test_cluster_cap_enforcement`: SPY+QQQ both US_INDEX → picks at most 1
- ✅ `test_daily_premium_cap_enforcement`: Blocks selections exceeding $1250 daily cap
- ✅ `test_open_premium_caps_by_regime`: Respects regime-specific and total caps
- ✅ `test_deterministic_selection_ordering`: Same input → same output
- ✅ `test_no_representable_candidates`: Gracefully handles 0 representable
- ✅ `test_premium_usd_convention`: Validates no ×100 multiplier

**Test Coverage:**
- All tests use **fixture data** (no IBKR required)
- All tests validate **hard governor constraints**
- All tests are **deterministic** and **repeatable**

---

## Usage Examples

### Campaign Mode (DEV)

```bash
python scripts/daily.py --campaign configs/campaign_v1.yaml
```

**Expected Output:**
```
================================================================================
RECOMMENDED SET (0-2 CANDIDATES)
================================================================================
# | UNDERLIER | REGIME | EXPIRY   | STRIKES    | EV/$  | P(Win) | PREMIUM | CLUSTER
1 | SPY       | crash  | 20260402 | 580/560    | 25.16 | 70.6%  | $49     | US_INDEX

Selected 1 candidate(s) for execution

Next: For each selected candidate, use intent_builder + execute_trade
  Recommended set: runs/campaign/campaign_v1_abc123_20260224T143000/recommended.json
```

### Run Tests

```bash
# If pytest available:
python -m pytest tests/test_campaign_selector.py -v

# Or run directly:
python tests/test_campaign_selector.py
```

---

## Acceptance Criteria

- [x] **End-to-End:** `python scripts/daily.py --campaign configs/campaign_v1.yaml` runs (DEV mode)
- [x] **Governor Enforcement:** Recommended set is 0-2 and respects all caps
- [x] **No Strategy Changes:** Strategy math, structuring, execution, ledger writing unchanged
- [x] **Artifacts:** All outputs written to `runs/campaign/<run_id>/`
- [x] **Convention:** `premium_usd = debit_per_contract * qty` (no ×100) documented and enforced
- [x] **Tests:** Governor logic validated without IBKR dependency

---

## Architecture Notes

### Why Separate Grid Runner + Selector?

1. **Separation of Concerns:**
   - Grid runner = candidate generation (leverages existing structuring)
   - Selector = portfolio-aware filtering (new capability)

2. **Testability:**
   - Selector can be tested with fixture data
   - No need to mock IBKR or Kalshi

3. **Debuggability:**
   - `candidates_flat.json` = full candidate universe
   - `recommended.json` = filtered set with rejection reasons

### No Strategy Duplication

Grid runner **reuses** `run_regime()` from `run_daily_v2.py`:
- Same calibration logic
- Same Monte Carlo paths
- Same structuring pipeline
- Same validation guards

### Event-Based Ledger Integration

Positions view reads `trade_outcomes.jsonl`:
- Uses `FILLED_OPEN` / `CLOSED` events
- Ignores legacy rows gracefully
- Computes exposure in real-time from event stream

---

## File Structure

```
forecast_arb/
├── campaign/
│   ├── __init__.py
│   ├── grid_runner.py          # Multi-cell orchestration
│   └── selector.py              # Portfolio-aware governor
├── portfolio/
│   ├── __init__.py
│   └── positions_view.py        # Event-based positions reader
configs/
└── campaign_v1.yaml             # Campaign configuration
scripts/
└── daily.py                     # Updated with --campaign flag
tests/
└── test_campaign_selector.py   # Governor enforcement tests
runs/
└── campaign/
    └── <run_id>/
        ├── manifest.json
       ├── candidates_flat.json
        ├── recommended.json
        └── cells/
            └── <cell_id>.json
```

---

## Next Steps (Post-Phase 3)

1. **Live Integration:**
   - Test campaign mode with live IBKR snapshots
   - Validate multi-underlier snapshot fetching
   - Confirm Kalshi p_external integration

2. **Position Management:**
   - Implement position closing logic
   - Track P&L by campaign run
   - Add campaign-level reporting

3. **Enhanced Governors:**
   - Delta exposure limits
   - Correlation-aware diversification
   - Time-of-day restrictions

4. **Multi-Day Optimization:**
   - Intra-week portfolio rebalancing
   - Expiry rollover strategies
   - Campaign performance attribution

---

## Summary

Phase 3 delivers a **production-ready campaign layer** that:
- ✅ Generates candidates across multi-cell grid
- ✅ Applies hard portfolio governors
- ✅ Selects 0-2 recommended trades deterministically
- ✅ Integrates with existing daily console
- ✅ Maintains backward compatibility
- ✅ Validates via comprehensive tests

**Convention is standardized and enforced:**
```python
premium_usd = debit_per_contract * qty  # No ×100
```

All goals achieved. ✅
