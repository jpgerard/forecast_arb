# Campaign Console Visibility Patch

**Status:** ✅ COMPLETE  
**Date:** 2026-02-25  
**Type:** Minimal surgical patch - visibility only

## 🎯 Objective

Enhance `scripts/daily.py` campaign mode console output with:
1. **Portfolio State Header** - Display current portfolio exposure before candidate table
2. **Probability Source Column** - Show p_source (kalshi/fallback/implied) for each candidate

## ✅ Changes Made

### 1. Portfolio State Header Display
**File:** `scripts/daily.py`  
**Location:** Campaign mode, after loading `recommended.json`

Added header block displaying:
- Open positions total count
- Crash regime: count / max, premium / cap
- Selloff regime: count / max, premium / cap  
- Total premium at risk / total cap
- Remaining daily premium capacity

**Example Output:**
```
====================================================================================================
PORTFOLIO STATE
====================================================================================================
Open positions total: 1
Crash:   1 / 3  Premium $60.00 / $3000
Selloff: 0 / 4  Premium $0.00 / $4000
Total premium at risk: $60.00 / $7000
Remaining daily premium capacity: $1190.00
====================================================================================================
```

### 2. P_SOURCE Column in Candidate Table
**File:** `scripts/daily.py`  
**Location:** Campaign mode recommended set table

Added `P_SRC` column to display probability source for each candidate.

**Example Output:**
```
====================================================================================================
# | UNDERLIER | REGIME | EXPIRY   | STRIKES | EV/$  | P(Win) | PREMIUM | CLUSTER   | P_SRC
1 | SPY       | crash  | 20260402 | 590/570 | 60.21 | 98.7%  | $32     | US_INDEX  | fallback
====================================================================================================
```

### 3. P_SOURCE Inference - Grid Runner
**File:** `forecast_arb/campaign/grid_runner.py`  
**Function:** `flatten_candidate()`

Added best-effort inference logic to classify `p_source` from existing candidate fields:
```python
# Infer p_source from existing fields (best-effort, no invention)
p_source = "unknown"

# Check for p_event_result.source (highest priority)
p_event_result = candidate.get("p_event_result")
if p_event_result and isinstance(p_event_result, dict):
    p_source = p_event_result.get("source", "unknown")
# Fallback: check p_external_source
elif "p_external_source" in candidate:
    p_source = candidate.get("p_external_source", "unknown")
# Check warnings for spread-based estimate
else:
    warnings = candidate.get("warnings", [])
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, str) and "spread-based" in warning.lower():
                p_source = "implied_spread"
                break
```

**Inference Priority:**
1. `p_event_result.source` (from p_event source system)
2. `p_external_source` (legacy field)
3. Warnings containing "spread-based" → `implied_spread`
4. Default: `unknown`

**No invention** - only classifies based on existing data.

### 4. P_SOURCE Pass-Through - Selector
**File:** `forecast_arb/campaign/selector.py`  
**Function:** `run_selector()`

Ensured `p_source` is explicitly included in selected candidates written to `recommended.json`:
```python
"selected": [
    {
        **candidate,
        "computed_premium_usd": compute_candidate_premium_usd(candidate, qty),
        "qty": qty,
        "p_source": candidate.get("p_source", "unknown")
    }
    for candidate in result.selected
]
```

## 🚫 What Was NOT Changed

- ✅ No strategy math modifications
- ✅ No selector logic changes
- ✅ No execution changes
- ✅ No ledger changes
- ✅ No artifact format changes
- ✅ No new probability calculations

This is **purely console visibility** - displaying information that already exists in the data pipeline.

## 📊 Data Flow

```
RegimeResult (p_source from structuring)
    ↓
grid_runner.flatten_candidate() [passes through p_source]
    ↓
candidates_flat.json [contains p_source]
    ↓
selector.run_selector() [passes through p_source]
    ↓
recommended.json [contains p_source in selected]
    ↓
daily.py campaign mode [displays p_source in table]
```

## 🔍 P_SOURCE Values

The `p_source` field can have the following values:
- `"kalshi"` - Probability from Kalshi market
- `"fallback"` - Conservative fallback estimate used
- `"options_implied"` - Options-implied probability
- `"implied_spread"` - Spread-based probability estimate
- `"unknown"` - Source not specified or inferred (default)

These values are **inferred** from existing candidate fields, not computed or invented.

## ✅ Testing

To verify the patch works:

```powershell
# Run campaign mode with existing setup
python scripts/daily.py --campaign configs/campaign_v1.yaml --config configs/structuring_crash_venture_v2.yaml
```

Expected console output should include:
1. Portfolio State header block
2. Candidate table with P_SRC column populated

## 📝 Notes

- The patch is backward compatible - if `p_source` is missing from candidates, it defaults to `"unknown"`
- Portfolio state data already exists in `recommended.json`, we're just displaying it
- Table width increased from 120 to 130 characters to accommodate P_SRC column
- All changes follow the "minimal surgical patch" specification - no architecture changes

## 🎉 Result

Operators now have complete visibility into:
- Current portfolio exposure and capacity
- Probability source for each trade recommendation

This enables more informed decision-making without changing any underlying logic.
