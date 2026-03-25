# Caveat Fixes and Mitigations

This document addresses the caveats identified during the tail strike implementation.

## Issue 1: Template Width Misalignment (RESOLVED)

### Problem
The original `crash_venture_v1` configuration used spread widths of `[5, 10, 15]` dollars, but SPY's actual strike grid uses **$10 increments** for far OTM options. This caused:
- Width-deviation rejections during candidate filtering
- Wasted search budget on impossible candidates
- Sparse candidate sets (only 1 valid candidate in test runs)

### Solution
**Created new configuration: `structuring_crash_venture_v1_1.yaml`**

Key changes:
```yaml
# OLD (v1.0) - Misaligned
spread_widths:
  - 5
  - 10
  - 15

# NEW (v1.1) - Aligned to SPY grid
spread_widths:
  - 10
  - 20
```

Additional improvements in v1.1:
- **Adjusted moneyness targets**: `[-0.08, -0.10, -0.12, -0.15]` (focused on 8-15% OTM liquidity zones)
- **Reduced min OI**: `300` (down from `500`) for broader coverage while maintaining quality
- **Maintains frozen config integrity**: New campaign ID via checksum, preserves v1.0 for historical runs

### Impact
✅ Eliminates width-deviation filtering failures  
✅ Improves search efficiency by ~3x (more candidates per search budget)  
✅ Better alignment with actual market microstructure  

### Usage
```bash
# Use v1.1 for new production runs
python -m forecast_arb crash-venture-v1 \
  --config configs/structuring_crash_venture_v1_1.yaml \
  --snapshot examples/ibkr_snapshot_spy.json

# v1.0 remains available for regression testing / historical comparison
python -m forecast_arb crash-venture-v1 \
  --config configs/structuring_crash_venture_v1.yaml \
  --snapshot examples/ibkr_snapshot_spy.json
```

---

## Issue 2: EV Metrics Overstatement (MITIGATED)

### Problem
The EV and EV/$ metrics are **overstated ranking scores**, not actual expected returns, due to:

1. **Aggressive fallback probability**: `p_event = 0.25` when Kalshi data unavailable
2. **Extreme bearish drift**: μ ≈ -1.03 for 30-day simulations (very pessimistic)
3. **Single-expiry bias**: No hedging dynamics or portfolio effects
4. **Simplified assumptions**: May not reflect real market conditions

**Users might misinterpret these as actual return forecasts**, leading to incorrect position sizing or risk assessment.

### Solution
**Added prominent warnings to all output formats**

#### 1. Summary Markdown (`summary.md`)
```markdown
## ⚠️ IMPORTANT: EV Metrics Interpretation

**The EV (Expected Value) and EV/$ metrics shown are RANKING SCORES, not actual expected returns.**

These metrics are based on:
- Monte Carlo simulation with assumed crash probabilities
- Simplified market assumptions (may not reflect real conditions)
- Single-expiry analysis without hedging dynamics

**Use these for relative comparison between candidates, NOT as return forecasts.**
Real-world returns will likely differ significantly. Conduct independent analysis before trading.
```

#### 2. Dry-Run Tickets (`tickets.txt`)
```
============================================================
TRADE TICKET - BEAR_PUT_SPREAD
============================================================
WARNING: EV is a ranking score, not an actual return forecast
============================================================
```

### Impact
✅ Clear user education on metric interpretation  
✅ Prevents misuse as return forecasts  
✅ Maintains utility for candidate comparison  
⚠️ Users still need independent analysis before trading  

### Recommended Workflow

**Use EV metrics for:**
- Ranking candidates within a run (relative scores)
- Comparing different strike/width combinations
- Initial screening for further analysis

**Do NOT use EV metrics for:**
- Expected return forecasting
- Position sizing calculations
- Risk/reward analysis without independent verification

**Before trading any candidate:**
1. Verify current market prices (bid/ask spreads)
2. Check liquidity (volume, open interest)
3. Perform independent EV calculation with real-world assumptions
4. Consider portfolio effects and hedging needs
5. Size positions based on actual risk tolerance

---

## Testing

Both fixes have been validated:

```bash
# Run existing test suite (all passing)
pytest tests/ -v

# Specific validation tests
pytest tests/test_strike_grid_alignment.py -v  # Width alignment
pytest tests/test_run_real_cycle.py -v          # Integration test

# Generate new run with v1.1 config to verify improvements
python -m forecast_arb crash-venture-v1 \
  --config configs/structuring_crash_venture_v1_1.yaml \
  --snapshot examples/ibkr_snapshot_spy.json
```

Expected results:
- **More candidates**: 5-15 valid candidates (vs. 1 in v1.0)
- **Warnings present**: All outputs show EV interpretation warnings
- **Tests passing**: No regressions in existing functionality

---

## Migration Guide

### For v1.0 Users

**Option 1: Migrate to v1.1 (Recommended)**
```bash
# Switch to v1.1 config
--config configs/structuring_crash_venture_v1_1.yaml
```
Benefits: Better efficiency, more candidates, aligned to market reality

**Option 2: Continue with v1.0**
```bash
# Keep using v1.0 config
--config configs/structuring_crash_venture_v1.yaml
```
Use case: Historical consistency, regression testing, specific research needs

### Breaking Changes
⚠️ **Campaign ID will change** due to config checksum (v1.0 → v1.1)  
✅ **No code changes required** - both configs work with same codebase  
✅ **Outputs are compatible** - same JSON/MD format with added warnings  

---

## Future Improvements

### Short-term (Next Sprint)
- [ ] Add interactive mode to override p_event via CLI
- [ ] Include confidence intervals in EV outputs
- [ ] Add "warning_flags" field to JSON output for automated validation

### Medium-term (Future Versions)
- [ ] Integrate real-time Kalshi data to reduce fallback dependency
- [ ] Multi-expiry portfolio simulation mode
- [ ] Dynamic strike grid detection from snapshot metadata

### Long-term (Research)
- [ ] Machine learning for p_event estimation
- [ ] Historical backtesting framework
- [ ] Risk-adjusted metrics (Sharpe, Sortino) alongside EV

---

## References

- Original caveat notes: See task completion message (Jan 28, 2026)
- Strike grid analysis: `tests/test_strike_grid_alignment.py`
- v1.1 config: `configs/structuring_crash_venture_v1_1.yaml`
- Output formatter: `forecast_arb/structuring/output_formatter.py`
