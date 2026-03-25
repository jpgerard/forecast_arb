# Memory Bank: Product Context

## Why This Project Exists
forecast_arb is a tail-risk options trading system that generates executable SPY put spread structures designed to profit from market crashes. It combines:
- **Options-implied probabilities** from the market's own pricing
- **External probabilities** from prediction markets (Kalshi) when available
- **Edge gating** to only trade when significant edge exists
- **IBKR snapshot integration** for executable pricing

## Problems It Solves
1. **Probability Assessment**: Uses dual sources (options-implied + external) to detect mispriced tail risk
2. **Edge Detection**: Compares market expectations vs external forecasts to find edge
3. **Executable Structures**: Generates real put spreads with IBKR bid/ask quotes, not theoretical models
4. **Risk Management**: Built-in gating (edge, confidence, source policy) prevents bad trades
5. **Auditability**: Full artifact trail with review packs, decision logs, and run indexing

## How It Should Work
### Daily Cycle (scripts/run_daily.py)
1. **Snapshot**: Fetch live IBKR option chain for SPY (30-60 DTE)
2. **P_Event Sources**:
   - Try Kalshi auto-mapping (currently limited to 0-3 day markets)
   - Fallback: Compute p_implied from options market (vertical spread pricing)
3. **Edge Gating**: Compare p_external vs p_implied, require min edge (5%) and confidence (60%)
4. **External Source Policy**: Block fallback trades unless --allow-fallback-trade set
5. **Structure Generation**: If gates pass, generate 3 ranked put spreads (crash venture v1)
6. **Review Output**: Generate review pack for manual approval
7. **Execution**: Can submit to IBKR with --submit --confirm SUBMIT (dry-run by default)

### Key Modes
- **Review-Only**: Runs structuring even when blocked for analysis (--review-only-structuring)
- **Snapshot**: Uses real IBKR quotes (default)
- **Theoretical**: Model-based pricing (legacy, not primary mode)
