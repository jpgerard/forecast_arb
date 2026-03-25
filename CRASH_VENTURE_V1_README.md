# Crash Venture v1 - Locked & Hardened

**Status**: PRODUCTION-READY  
**Version**: 1.0.0  
**Campaign**: `crash_venture_v1`  
**Config Checksum**: `38b76374dd3a68de`

## Overview

Crash Venture v1 is a deterministic, locked options structuring engine that produces **1-3 clean, executable SPY put-spread trade candidates** per run. 

**Key Features:**
- ✅ Zero parameter drift - all parameters frozen in config
- ✅ Deterministic outputs - same inputs → same results
- ✅ Sanity checks enforced before output
- ✅ Dominance filtering removes sub-optimal candidates
- ✅ Single underlier enforcement (SPY only)
- ✅ Clean, machine-readable outputs
- ✅ Regression test suite to detect algorithm changes

## Quick Start

```powershell
# Run with example parameters
python examples/run_crash_venture_v1.py
```

## Configuration

**Frozen Config**: `configs/structuring_crash_venture_v1.yaml`

```yaml
campaign_name: "crash_venture_v1"

structuring:
  underlier: "SPY"                    # LOCKED
  dte_range_days: [30, 60]            # LOCKED
  moneyness_targets: [-0.10, -0.15, -0.20]  # LOCKED
  spread_widths: [5, 10, 15]          # LOCKED
  constraints:
    max_loss_usd_per_trade: 500       # LOCKED
    max_candidates_evaluated: 30      # LOCKED
    top_n_output: 3                   # LOCKED
  monte_carlo:
    paths: 30000                      # LOCKED
    seed_mode: "run_id"               # Deterministic
  objective: "max_ev_per_dollar"      # LOCKED
```

**⚠️ DO NOT MODIFY** - Any change to config creates new campaign ID via checksum.

## Usage

### Python API

```python
from forecast_arb.engine.crash_venture_v1 import run_crash_venture_v1

result = run_crash_venture_v1(
    config_path="configs/structuring_crash_venture_v1.yaml",
    p_event=0.35,          # Event probability from Kalshi
    spot_price=500.0,      # Current SPY price
    atm_iv=0.15,           # ATM implied volatility
    expiry_date="2026-03-15",
    days_to_expiry=45
)

# Access results
run_id = result['run_id']
structures = result['top_structures']
manifest = result['manifest']
```

### Command Line

```powershell
python -c "from forecast_arb.engine.crash_venture_v1 import run_crash_venture_v1; run_crash_venture_v1('configs/structuring_crash_venture_v1.yaml', 0.35, 500.0, 0.15, '2026-03-15', 45)"
```

## Outputs

Each run creates a timestamped directory with:

```
runs/crash_venture_v1/crash_venture_v1_{checksum}_{timestamp}/
├── structures.json         # Machine-readable trade data
├── summary.md              # Human-readable markdown summary
├── dry_run_tickets.txt     # IBKR-style trade tickets
└── manifest.json           # Run metadata + config checksum
```

### Structure Output Format

Each recommended structure contains:

```json
{
  "expiry": "2026-03-15",
  "strikes": {
    "long_put": 450.0,
    "short_put": 445.0
  },
  "premium": -0.07,
  "max_loss": -0.07,
  "max_gain": 4.93,
  "breakeven": null,
  "ev": 1.06,
  "ev_per_dollar": 15.013,
  "assumed_p_event": 0.35,
  "spot_used": 500.0,
  "atm_iv_used": 0.15,
  "reason_selected": "Highest EV/dollar ratio...",
  "rank": 1,
  "underlier": "SPY",
  "template_name": "put_spread"
}
```

## Guardrails & Validation

### 1. Config Checksum
- SHA256 hash of config embedded in run_id
- Any config change → new campaign_id
- Prevents silent parameter drift

### 2. Dominance Filter (Critical)
Before ranking, removes any structure that is strictly dominated:
- Higher or equal premium paid AND
- Lower or equal max gain AND  
- Worse or equal EV

Logged: `Dominance filter removed N dominated structures`

### 3. Single Event → Single Underlier
- `enrich_oracle_data_with_mapping()` enforces SPY-only
- FAILS FAST if ambiguous mapping
- FAILS FAST if missing expiry
- FAILS FAST if low confidence mapping

### 4. Sanity Assertions (Non-Negotiable)
Before final output, asserts:
- ✅ `max_loss <= max_loss_usd_per_trade`
- ✅ All strikes exist and are valid
- ✅ `bid > 0` and `ask > bid` for all legs
- ✅ EV calculation ran with correct seed

**Aborts run on failure**

### 5. Deterministic Execution
- RNG seed derived from run_id
- Same inputs → exact same outputs
- Monte Carlo paths: 30,000 (fixed)

## Regression Testing

```powershell
# Run regression tests
pytest tests/test_crash_venture_v1_regression.py -v

# Key tests:
# - test_calibration_determinism
# - test_evaluation_determinism  
# - test_top_structure_regression
# - test_dominance_filter
```

**These tests MUST pass** - failure indicates math or ranking logic changed.

## Architecture

```
Inputs (Market Data)
  ↓
Load Frozen Config + Validate
  ↓
Compute Config Checksum → Run ID
  ↓
Setup Deterministic RNG Seed
  ↓
Calibrate Drift (p_event → μ)
  ↓
Generate N Candidate Structures (9 max)
  ↓
Evaluate via Monte Carlo (30k paths each)
  ↓
Apply Dominance Filter
  ↓
Rank by Objective (max_ev_per_dollar)
  ↓
Select Top 3
  ↓
Run Sanity Assertions
  ↓
Format & Write Outputs
```

## Components

### Core Modules
- `forecast_arb/engine/crash_venture_v1.py` - Main engine
- `forecast_arb/structuring/router.py` - Dominance filter + ranking
- `forecast_arb/structuring/event_map.py` - Underlier enforcement
- `forecast_arb/structuring/output_formatter.py` - Output hardening
- `forecast_arb/utils/manifest.py` - Config checksum

### Supporting Modules
- `forecast_arb/structuring/templates.py` - Put spread generation
- `forecast_arb/structuring/calibrator.py` - Drift calibration
- `forecast_arb/structuring/evaluator.py` - Monte Carlo evaluation

## Example Run Output

```
Run ID: crash_venture_v1_38b76374dd3a68de_20260128T170013
Output Directory: runs/crash_venture_v1/crash_venture_v1_38b76374dd3a68de_20260128T170013

--- Trade #1 ---
  Template: put_spread
  Expiry: 2026-03-15
  Strikes: Long $450.00, Short $445.00
  Premium: $-0.07
  Max Loss: $0.07
  Max Gain: $4.93
  Expected Value: $1.06
  EV per Dollar: 15.013
  Reason: Highest EV/dollar ratio (15.013), offering $1.06 expected value with 25.8% win probability.

--- Trade #2 ---
  Template: put_spread
  Expiry: 2026-03-15
  Strikes: Long $450.00, Short $440.00
  Premium: $-0.11
  Max Loss: $0.11
  Max Gain: $9.89
  Expected Value: $1.89
  EV per Dollar: 17.132
  Reason: Second-best alternative with $1.89 EV and 26.2% win probability.

--- Trade #3 ---
  Template: put_spread
  Expiry: 2026-03-15
  Strikes: Long $450.00, Short $435.00
  Premium: $-0.13
  Max Loss: $0.13
  Max Gain: $14.87
  Expected Value: $2.44
  EV per Dollar: 18.467
  Reason: Third option with $2.44 EV, balancing risk and return.
```

## Limitations & Boundaries

### What It Does
✅ Generate 1-3 SPY put spread candidates  
✅ Enforce risk constraints ($500 max loss)  
✅ Deterministic, reproducible results  
✅ Clean output formatting  

### What It Does NOT Do
❌ Execute trades (dry-run only)  
❌ Fetch live market data (requires manual input)  
❌ Support other underliers (SPY only)  
❌ Support other structures (put spreads only)  
❌ Optimize parameters (frozen config)  
❌ Provide probability estimation (requires oracle input)

## Next Steps (Future Work)

**Do NOT proceed without explicit approval:**
- [ ] Live trading integration
- [ ] Automation/scheduling
- [ ] Parameter optimization
- [ ] Additional underliers
- [ ] Additional option structures
- [ ] Live data integration

## Version Control

- **v1.0.0** (2026-01-28): Initial locked release
  - Config checksum: `38b76374dd3a68de`
  - All parameters frozen
  - Regression tests passing
  - End-to-end tested

## Support

For issues or questions:
1. Check regression tests: `pytest tests/test_crash_venture_v1_regression.py -v`
2. Verify config unchanged: checksum must be `38b76374dd3a68de`
3. Review run manifest.json for diagnostics
4. Check sanity assertion logs

---

**Last Updated**: 2026-01-28  
**Maintainer**: Crash Venture Team  
**License**: Internal Use Only
