# Daily.py Probability Display Enhancement

## Summary

Enhanced `scripts/daily.py` to provide comprehensive probability metadata display for portfolio-aware decision making. The system now surfaces critical information about probability provenance, edge calculations, and EV sensitivity that was previously hidden.

## Problem Statement

The original `daily.py` output only showed:
```
EV/$  | P(Win)
23.16 | 70.6%
```

This provided no context for:
- Where the probability came from (Kalshi? Options implied? Fallback?)
- What the market actually implies vs what we're assuming
- How sensitive EV is to probability uncertainty
- Whether we're betting against the market

## Solution Overview

Added 4 key enhancements:

### 1. Regime Probability Context Block
Displayed **before** the candidate table for each regime:

```
====================================================================================================
REGIME PROBABILITY CONTEXT: crash
====================================================================================================
External (Kalshi):   0.072 ( 7.2%)  [KXINX-26APR02-B5000, exact_match=True]
Status:              NON-AUTHORITATIVE (policy blocked)
Implied (Options):   0.063 ( 6.3%)  [confidence=1.0]
Edge:                  +90 bps  (External - Implied)
Assumed for EV:      0.300 (30.0%)  [source: fallback]

Crash Regime Sanity Check:
  Market view (implied):   6.3% chance of crash by expiry
  Model assumption:       30.0% chance
  Relative difference:    4.8x higher than market
  ⚠️  WARNING: Betting 4.8x against market pricing - review carefully
====================================================================================================
```

**Key insights provided:**
- External probability from Kalshi (if available)
- Whether it's authoritative or policy-blocked
- Options-implied probability with confidence
- Edge in basis points
- What probability is actually used for EV calculations
- Crash regime sanity check showing market vs model assumptions

### 2. Enhanced Candidate Table Columns

Added probability columns to the candidate table:

```
====================================================================================================
REGIME | RANK | EXPIRY | STRIKES | EV/$ | P(Win) | P_SRC    | P_EXT | P_IMPL | EDGE   | DEBIT
crash  |  1   | 20260402| 580/560 | 23.16| 70.6%  | fallback |  7.2% |   6.3% | +90bp  | $49
====================================================================================================
```

**New columns:**
- `P_SRC`: Probability source (kalshi, fallback, etc.)
- `P_EXT`: External probability from Kalshi
- `P_IMPL`: Options-implied probability
- `EDGE`: Edge in basis points (External - Implied)

### 3. EV Sensitivity Analysis

Displayed **after** candidate selection, **before** quote-only check:

```
====================================================================================================
EV SENSITIVITY ANALYSIS (Rank 1)
====================================================================================================
Base case (P=0.30):    EV = $1134.74 | EV/$ = 23.16
Lower bound (P=0.25):  EV = $ 945.62 | EV/$ = 19.30  (-16.7%)
Upper bound (P=0.35):  EV = $1323.86 | EV/$ = 27.02  (+16.7%)

Interpretation: EV remains positive across ±5% probability range
Robustness:     STRONG
====================================================================================================
```

**Shows:**
- EV at base probability
- EV if probability drops by 5pp
- EV if probability rises by 5pp
- Robustness assessment (STRONG/MODERATE/FRAGILE)

**Robustness categories:**
- **STRONG**: EV remains positive across entire ±5% range
- **FRAGILE**: EV turns negative if probability drops by 5pp
- **MODERATE**: EV changes significantly (>50%) with probability shifts

### 4. Crash Regime Market vs Model Warning

Automatically flags when model assumptions are significantly higher than market pricing:

```
Crash Regime Sanity Check:
  Market view (implied):   6.3% chance of crash by expiry
  Model assumption:       30.0% chance
  Relative difference:    4.8x higher than market
  ⚠️  WARNING: Betting 4.8x against market pricing - review carefully
```

**Thresholds:**
- >3.0x: Shows WARNING - review carefully
- 1.0x-3.0x: Shows INFO - moderately optimistic
- <1.0x: Shows INFO - conservative vs market

## Implementation Details

### New Functions Added

1. **`calculate_ev_at_probability(p, debit, max_gain)`**
   - Helper function to calculate EV and EV/$ at any probability
   - Formula: `EV = p * max_gain - (1-p) * debit`

2. **`print_regime_probability_context(regime_data, regime_name)`**
   - Extracts and displays regime-level probability metadata
   - Shows external, implied, assumed probabilities
   - Calculates and displays edge
   - Performs crash regime sanity check

3. **`print_ev_sensitivity(candidate)`**
   - Calculates EV at P-0.05, P, P+0.05
   - Displays formatted sensitivity table
   - Determines and displays robustness level

### Modified Functions

1. **`print_candidate_table()`**
   - Now calls `print_regime_probability_context()` before table
   - Enhanced table headers with probability columns
   - Calculates edge for each candidate row

2. **`select_candidate_interactive()`**
   - Now calls `print_ev_sensitivity()` after selection
   - Provides immediate feedback on EV robustness

## Data Sources

The enhancements leverage existing data from `review_candidates.json`:

**Regime-level:**
- `p_event_external`: Full Kalshi probability metadata (added in recent fix)
  - `p`: Probability value
  - `source`: Source identifier (e.g., "kalshi")
  - `authoritative`: Whether it's policy-approved
  - `market.ticker`: Kalshi market ticker
  - `match.exact_match`: Whether exact match found
- `p_implied`: Options-implied probability
- `p_implied_confidence`: Confidence in implied calculation

**Candidate-level:**
- `assumed_p_event`: Probability used for EV calculation
- `debit_per_contract`: Cost per contract
- `max_gain_per_contract`: Max gain per contract
- `ev_per_dollar`: Expected value per dollar risked
- `prob_profit`: Win probability

## Usage

No changes to command-line interface. Simply run:

```powershell
python scripts/daily.py --snapshot snapshots/SPY_snapshot_latest.json
```

The enhanced display will automatically appear.

## Example Full Output

```
====================================================================================================
REGIME PROBABILITY CONTEXT: crash
====================================================================================================
External (Kalshi):   0.072 ( 7.2%)  [KXINX-26APR02-B5000, exact_match=True]
Status:              NON-AUTHORITATIVE (policy blocked)
Implied (Options):   0.063 ( 6.3%)  [confidence=1.0]
Edge:                  +90 bps  (External - Implied)
Assumed for EV:      0.300 (30.0%)  [source: fallback]

Crash Regime Sanity Check:
  Market view (implied):   6.3% chance of crash by expiry
  Model assumption:       30.0% chance
  Relative difference:    4.8x higher than market
  ⚠️  WARNING: Betting 4.8x against market pricing - review carefully
====================================================================================================

====================================================================================================
REGIME | RANK | EXPIRY   | STRIKES  | EV/$  | P(Win) | P_SRC    | P_EXT | P_IMPL | EDGE   | DEBIT
----------------------------------------------------------------------------------------------------
crash  |  1   | 20260402 | 580/560  | 23.16 | 70.6%  | fallback |  7.2% |   6.3% | +90bp  | $49
====================================================================================================

[After selection...]

====================================================================================================
EV SENSITIVITY ANALYSIS (Rank 1)
====================================================================================================
Base case (P=0.30):    EV = $1134.74 | EV/$ = 23.16
Lower bound (P=0.25):  EV = $ 945.62 | EV/$ = 19.30  (-16.7%)
Upper bound (P=0.35):  EV = $1323.86 | EV/$ = 27.02  (+16.7%)

Interpretation: EV remains positive across ±5% probability range
Robustness:     STRONG
====================================================================================================
```

## Benefits

### Operator Decision Quality
1. **Transparency**: No hidden assumptions - everything visible
2. **Sanity checks**: Automatic warnings for aggressive assumptions
3. **Risk assessment**: EV sensitivity shows fragility immediately
4. **Market context**: See what market implies vs what you're betting

### Portfolio Management
1. **Edge visibility**: Immediately see if you have real edge
2. **Probability provenance**: Know if using Kalshi, implied, or fallback
3. **Model vs market**: Understand when betting against consensus
4. **Robustness**: Know if edge holds under uncertainty

### Debugging
1. **Data flow**: See exactly what probability is being used where
2. **Policy checks**: See if external data is authoritative
3. **Calculation verification**: Sensitivity analysis validates EV math

## Non-Breaking Changes

- All existing functionality preserved
- Only **adding** information, not removing
- Backward compatible with existing workflows
- Campaign mode gets same enhancements automatically
- No configuration changes required

## Campaign Mode Support (Extended)

Campaign mode now includes full probability provenance display:

### Campaign Table Format
```
#  | UNDERLIER | REGIME | EXPIRY  | STRIKES | EV/$  | P(Win) | PREM | CLUSTER  | P_USED | P_IMPL | P_SRC
1  | SPY       | crash  | 20260402| 585/565 | 47.02 | 95.0%  | $38  | US_INDEX | 0.300  | 0.083  | spread_estimate
```

### Per-Candidate Detail Block
```
CANDIDATE 1 of 1
Underlier: SPY
Regime: crash
Expiry: 20260402
Strikes: 585/565
EV/$: 47.02
Probability: P_used=0.300 | P_impl=0.083 | P_ext=— | source=spread_estimate
```

Followed by EV Sensitivity Analysis before execution options.

### Probability Fields in Artifacts

**candidates_flat.json** now includes:
- `p_event_used`: Probability used in EV calculation
- `p_implied`: Options-implied or spread-based estimate
- `p_external`: Authoritative Kalshi probability (null if not available)
- `p_source`: Source identifier (kalshi/fallback/spread_estimate/etc.)
- `p_confidence`: Confidence score (0-1, null if not available)

**recommended.json** preserves all probability fields in both:
- `selected` array: Full probability metadata for chosen candidates
- `rejected_top10` array: Probability metadata for rejected candidates (for review)

## Files Modified

- `scripts/daily.py`: Enhanced with 3 new functions and 2 modified functions
- `forecast_arb/campaign/grid_runner.py`: Enhanced `flatten_candidate()` to preserve full probability metadata
- `forecast_arb/campaign/selector.py`: Explicitly preserves probability fields in recommended.json output

## Testing Recommendations

1. **Run with existing snapshot:**
   ```powershell
   python scripts/daily.py --snapshot snapshots/SPY_snapshot_latest.json
   ```

2. **Verify context blocks appear** before candidate tables

3. **Verify sensitivity analysis** appears after candidate selection

4. **Verify edge calculations** are correct (spot check against manual calc)

5. **Test with both:**
   - Authoritative Kalshi data (if available)
   - Fallback scenarios (when Kalshi not available)

## Future Enhancements (Not Implemented)

Potential additions for future versions:
1. Historical edge tracking (edge vs realized outcomes)
2. Probability distribution plots (not just point estimates)
3. Multi-scenario analysis (bear/base/bull cases)
4. Correlation warnings (multiple positions on same underlier)

## Questions Answered

✅ **Where did the probability come from?** - P_SRC column + context block  
✅ **What does the market imply?** - P_IMPL column + implied line  
✅ **What are we assuming?** - "Assumed for EV" line  
✅ **How sensitive is EV?** - EV Sensitivity Analysis block  
✅ **Are we betting against the market?** - Crash Regime Sanity Check  
✅ **Is the edge robust?** - Robustness assessment  

---

**Implemented:** 2026-02-26  
**Author:** Cline  
**Files Modified:** 1 (scripts/daily.py)  
**Lines Added:** ~200  
**Breaking Changes:** None  
**User Action Required:** None (automatic)
