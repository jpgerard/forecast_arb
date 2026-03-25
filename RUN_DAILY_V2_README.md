# run_daily_v2.py - Multi-Regime Orchestration Runner

## Overview

`run_daily_v2.py` is the **Phase 3-integrated** daily runner that supports:
- ✅ Multi-regime orchestration (crash, selloff, both, auto)
- ✅ **Automatic regime decision ledger writing**
- ✅ Phase 3 decision quality tracking
- ✅ Unified artifact generation

This is the **v2 runner** that integrates all Phase 3 infrastructure. It replaces the v1 single-regime approach with a clean multi-regime orchestration system.

## Key Differences from v1

| Feature | v1 (run_daily.py) | v2 (run_daily_v2.py) |
|---------|-------------------|----------------------|
| **Regime Support** | Single regime (crash only) | Multi-regime (crash, selloff, both, auto) |
| **Ledger Writing** | Manual (not integrated) | **Automatic** ✅ |
| **Architecture** | Monolithic | Clean orchestration via `regime_orchestration.py` |
| **Phase 3 Integration** | No | **Yes** ✅ |
| **Decision Quality Tracking** | No | **Yes** ✅ |
| **Structuring** | Full v1 engine | Placeholder (Phase 4 will add v2 engine) |

## Usage

### Basic Usage (Single Regime)

```powershell
# Run crash regime only (default)
python scripts/run_daily_v2.py --regime crash

# Run selloff regime only
python scripts/run_daily_v2.py --regime selloff

# Run both regimes
python scripts/run_daily_v2.py --regime both
```

### Using Existing Snapshot

```powershell
python scripts/run_daily_v2.py --regime crash --snapshot snapshots/SPY_snapshot_20260206_124307.json
```

### Auto Mode (Regime Selector)

```powershell
# Let the system decide which regime(s) to run
python scripts/run_daily_v2.py --regime auto
```

**Note:** Auto mode uses `oracle/regime_selector.py` to decide which regimes to run based on market conditions.

### With Fallback p_external

```powershell
# Use fallback probability instead of Kalshi
python scripts/run_daily_v2.py --regime crash --p-event-source fallback --fallback-p 0.15
```

### Full Example

```powershell
python scripts/run_daily_v2.py `
  --regime both `
  --underlier SPY `
  --dte-min 30 `
  --dte-max 60 `
  --p-event-source kalshi-auto `
  --min-debit-per-contract 10.0 `
  --campaign-config configs/structuring_crash_venture_v2.yaml
```

## CLI Arguments

### Regime Selection (NEW in v2)
- `--regime {auto,crash,selloff,both}` - Regime selection mode (default: crash)
  - `auto` - Let regime selector decide
  - `crash` - Run crash regime only (-15% moneyness)
  - `selloff` - Run selloff regime only (-9% moneyness)
  - `both` - Run both regimes in parallel

### Snapshot Options
- `--underlier UNDERLIER` - Ticker symbol (default: SPY)
- `--snapshot SNAPSHOT` - Path to existing snapshot (creates new if not provided)
- `--dte-min DTE_MIN` - Minimum DTE (default: 30)
- `--dte-max DTE_MAX` - Maximum DTE (default: 60)

### IBKR Connection
- `--ibkr-host IBKR_HOST` - IBKR host (default: 127.0.0.1)
- `--ibkr-port IBKR_PORT` - IBKR port (default: 7496 for live trading)

### External Probability
- `--p-event-source {kalshi-auto,fallback}` - p_external source (default: kalshi-auto)
- `--fallback-p FALLBACK_P` - Fallback probability (default: 0.30)

### Configuration
- `--campaign-config CAMPAIGN_CONFIG` - Config path (default: configs/structuring_crash_venture_v2.yaml)
- `--min-debit-per-contract MIN_DEBIT_PER_CONTRACT` - Minimum debit filter (default: 10.0)

## Output Structure

```
runs/crash_venture_v2/{run_id}/
├── artifacts/
│   ├── review_candidates.json      # Unified multi-regime results
│   ├── regime_decision.json        # Regime selector decision (if auto mode)
│   └── regime_ledgers/             # AUTOMATIC decision ledgers ✅
│       ├── crash_ledger.jsonl
│       └── selloff_ledger.jsonl
```

## Phase 3 Integration: Automatic Ledger Writing

**Key Feature:** The v2 runner **automatically writes regime decision ledgers** via `write_regime_ledgers()`.

This happens **every run**, capturing:
- Which regimes were evaluated
- Whether each regime decided TRADE or NO_TRADE
- Event parameters (moneyness, threshold, expiry)
- p_implied and p_external values
- Representability status
- Decision reasons

### Example Ledger Entry

```json
{
  "run_id": "crash_venture_v2_abc123_20260206T131200",
  "regime": "crash",
  "mode": "BOTH",
  "decision": "NO_TRADE",
  "reasons": ["NOT_REPRESENTABLE"],
  "event_hash": "crash_20260320_480.50",
  "expiry": "20260320",
  "moneyness": -0.15,
  "spot": 565.00,
  "threshold": 480.50,
  "p_implied": 0.018,
  "p_external": null,
  "representable": false,
  "timestamp_utc": "2026-02-06T18:12:00Z"
}
```

### Ledger Locations

1. **Local:** `runs/crash_venture_v2/{run_id}/artifacts/regime_ledgers/`
2. **Global:** `runs/regime_ledgers/` (consolidated across all runs)

The global ledger enables:
- Weekly reviews (`scripts/weekly_pm_review.py`)
- DQS scoring (`scripts/score_decision.py`)
- Decision quality analysis over time

## Workflow

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Snapshot: Fetch IBKR option chain data                  │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│ 2. Regime Resolution: Decide which regimes to run          │
│    - auto: Use regime_selector.py                          │
│    - crash/selloff/both: Direct specification              │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│ 3. p_external: Fetch Kalshi probability (or fallback)     │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│ 4. Multi-Regime Execution:                                  │
│    For each regime:                                         │
│    - Apply regime config overrides                          │
│    - Compute event spec (moneyness, threshold)             │
│    - Check representability                                 │
│    - Compute p_implied                                      │
│    - [Phase 4: Run structuring engine]                     │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│ 5. Unified Artifacts: Write review_candidates.json         │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│ 6. **AUTOMATIC LEDGER WRITING** ✅                          │
│    write_regime_ledgers() captures decisions                │
│    - Local ledgers (per run)                               │
│    - Global ledgers (consolidated)                          │
└──────────────────┬──────────────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────────────┐
│ 7. Update Index: Track run in index.json                   │
└─────────────────────────────────────────────────────────────┘
```

## Regime Configuration Overlay

The v2 runner uses `apply_regime_overrides()` to apply regime-specific parameters from the config:

### Example Config (structuring_crash_venture_v2.yaml)

```yaml
campaign_name: crash_venture_v2

edge_gating:
  event_moneyness: -0.15  # Default (crash)

regimes:
  crash:
    moneyness: -0.15      # 15% OTM (deep crash)
    min_otm_boundary: -0.13
  
  selloff:
    moneyness: -0.09      # 9% OTM (moderate selloff)
    otm_bounds: [-0.07, -0.12]
```

When you run `--regime selloff`, the system automatically uses `-0.09` moneyness instead of the default `-0.15`.

## Phase 3 Complete ✅

The v2 runner provides **complete Phase 3 integration**:

1. ✅ **Regime Ledger Writing** - Automatic via `write_regime_ledgers()`
2. ✅ **Trade Outcome Logging** - Already integrated in `execute_trade.py`
3. ✅ **DQS Scoring** - Ready via `scripts/score_decision.py`
4. ✅ **Weekly Reviews** - Ready via `scripts/weekly_pm_review.py`

## Phase 4 Preview

The current v2 runner has a **structuring placeholder** (returns empty candidates). Phase 4 will:
- Integrate the full v2 structuring engine
- Add multi-regime candidate generation
- Add unified ranking across regimes
- Add executable order intent generation

## Migration Guide

### When to use v1 vs v2

**Use v1 (`run_daily.py`):**
- Production trading today
- Need full structuring + execution
- Single crash regime is sufficient

**Use v2 (`run_daily_v2.py`):**
- Multi-regime exploration
- Decision quality tracking
- Phase 3 ledger integration
- Preparing for Phase 4

**Future:** Once Phase 4 is complete, v2 will become the production runner.

## Testing

```powershell
# Test with fallback mode (no Kalshi required)
python scripts/run_daily_v2.py --regime crash --p-event-source fallback --fallback-p 0.15

# Test multi-regime
python scripts/run_daily_v2.py --regime both --p-event-source fallback --fallback-p 0.15

# Verify ledger writing
# Check: runs/crash_venture_v2/{run_id}/artifacts/regime_ledgers/
# Check: runs/regime_ledgers/
```

## Key Files

- `scripts/run_daily_v2.py` - This runner
- `forecast_arb/core/regime_orchestration.py` - Orchestration logic
- `forecast_arb/core/regime.py` - Regime definitions and config overlay
- `forecast_arb/core/regime_result.py` - RegimeResult data structure
- `forecast_arb/core/ledger.py` - Ledger entry creation
- `forecast_arb/oracle/regime_selector.py` - Auto regime selection

## Related Documentation

- `PHASE3_DECISION_QUALITY_LOOP_COMPLETE.md` - Phase 3 completion summary
- `PHASE3_INTEGRATION_GUIDE.md` - Integration guide for Phase 3
- `CRASH_VENTURE_V2_README.md` - Overall v2 system documentation
- `configs/structuring_crash_venture_v2.yaml` - v2 config example

## Support

For questions or issues:
1. Check `PHASE3_INTEGRATION_GUIDE.md` for Phase 3 details
2. Review test files: `tests/test_phase3_pr*.py`
3. Check regime orchestration tests: `test_pr4_standalone.py`, `test_pr5_standalone.py`
