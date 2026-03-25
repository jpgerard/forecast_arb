# Crash Venture v1 - Architecture Modes

## Overview

Crash Venture v1 has **two distinct execution modes** with fundamentally different pricing methods:

1. **SNAPSHOT MODE** (Executable Trades) - Uses real market bid/ask quotes
2. **THEORETICAL MODE** (Model-Based) - Uses Black-Scholes pricing

## Mode Comparison

| Aspect | Snapshot Mode | Theoretical Mode |
|--------|--------------|------------------|
| **Script** | `run_real_cycle_snapshot.py` | `run_real_cycle.py` |
| **Option Pricing** | Real bid/ask from IBKR | Black-Scholes model |
| **Use Case** | Executable trade recommendations | Theoretical exploration, sensitivity analysis |
| **Truth Path** | ✅ YES - Real market quotes | ❌ NO - Model estimates |
| **Executable** | ✅ YES | ⚠️ NO - Model pricing may differ from real spreads |
| **Data Sources** | IBKR snapshot (full option chain) | IBKR spot + IV only |

## When to Use Which Mode

### Use SNAPSHOT MODE (`run_real_cycle_snapshot.py`) when:
- ✅ You want **executable trade recommendations**
- ✅ You need **real market bid/ask spreads**
- ✅ You're preparing to place actual trades
- ✅ You want to see what the market is **actually offering**

### Use THEORETICAL MODE (`run_real_cycle.py`) when:
- 📊 You want to explore **sensitivity** to parameters
- 📊 You're doing **theoretical analysis**
- 📊 You want **quick estimates** without fetching full snapshot
- 📊 You're **backtesting** with historical IV curves

## Detailed Comparison

### Snapshot Mode (EXECUTABLE)

**Data Flow:**
```
1. Connect to IBKR TWS/Gateway
2. Fetch FULL option chain with bid/ask for all strikes
3. Save to JSON snapshot (audit trail)
4. Get event probability from Kalshi
5. Run snapshot-based engine
   → Uses real bid/ask to compute spread debits
   → Outputs executable recommendations
```

**Pricing Method:**
- **Put spread debit** = (long_put_ask - short_put_bid)
- Uses **actual market quotes** from IBKR
- Reflects **real liquidity** and bid/ask spreads

**Output:**
- Executable trade tickets
- Based on REAL market conditions
- Can be placed immediately in brokerage

**Usage:**
```bash
# Fetch live snapshot and generate executable trades
python examples/run_real_cycle_snapshot.py

# Use existing snapshot
python examples/run_real_cycle_snapshot.py --snapshot-path examples/ibkr_snapshot_spy.json

# Allow fallback for p_event if Kalshi unavailable
python examples/run_real_cycle_snapshot.py --allow-fallback
```

---

### Theoretical Mode (MODEL-BASED)

**Data Flow:**
```
1. Connect to IBKR TWS/Gateway
2. Fetch SPY spot price only
3. Fetch ATM implied volatility only
4. Get event probability from Kalshi
5. Run model-based engine
   → Uses Black-Scholes to estimate option prices
   → Outputs theoretical recommendations
```

**Pricing Method:**
- **Option prices** = Black-Scholes(S, K, τ, σ, r)
- Uses **model estimates**, not real quotes
- May differ significantly from actual bid/ask spreads

**Output:**
- Theoretical structures
- For exploration and sensitivity analysis
- **NOT directly executable** (model prices ≠ market prices)

**Usage:**
```bash
# Run theoretical analysis (aborts if data unavailable)
python examples/run_real_cycle.py

# Allow fallback values
python examples/run_real_cycle.py --allow-fallback
```

## Critical Differences

### 1. Option Pricing

**Snapshot Mode:**
```python
# Real market prices
long_put_price = snapshot["puts"][K_long]["ask"]   # What you PAY
short_put_price = snapshot["puts"][K_short]["bid"]  # What you RECEIVE
debit = long_put_price - short_put_price           # REAL cost
```

**Theoretical Mode:**
```python
# Black-Scholes estimates
long_put_price = black_scholes_put(S, K_long, τ, σ, r)
short_put_price = black_scholes_put(S, K_short, τ, σ, r)
debit = long_put_price - short_put_price  # MODEL estimate
```

### 2. Bid/Ask Spreads

**Snapshot Mode:**
- Includes real bid/ask spreads
- Debit reflects actual market liquidity
- SPY typically has tight spreads (<$0.05)
- OTM puts may have wider spreads

**Theoretical Mode:**
- Assumes mid-market pricing (no spread)
- Underestimates actual transaction costs
- Can be misleading for illiquid strikes

### 3. Strike Availability

**Snapshot Mode:**
- Only uses strikes that exist in snapshot
- Reflects actual market offerings
- May not have exact strike desired

**Theoretical Mode:**
- Can use any strike (model-based)
- Not constrained by market availability
- May suggest strikes that don't exist

## Recommendations

### For Production Trading:
1. **ALWAYS use snapshot mode** for executable trades
2. Verify snapshot is recent (< 5 minutes old)
3. Check bid/ask spreads before execution
4. Compare multiple snapshots if markets are volatile

### For Research/Analysis:
1. Use theoretical mode for parameter sweeps
2. Use theoretical mode for historical analysis
3. Validate insights with snapshot mode before trading
4. Document which mode was used in analysis

## File Organization

```
examples/
├── run_real_cycle_snapshot.py    # SNAPSHOT MODE (executable)
├── run_real_cycle.py              # THEORETICAL MODE (model-based)
├── ibkr_snapshot_spy.json         # Example snapshot data
└── ...

forecast_arb/engine/
├── crash_venture_v1_snapshot.py   # Snapshot-based engine
├── crash_venture_v1.py            # Model-based engine
└── ...
```

## Warning Labels

Both scripts include prominent warnings:

**Snapshot Mode:**
```
✅ EXECUTABLE TRADE RECOMMENDATIONS
These trades use REAL market bid/ask quotes from IBKR
```

**Theoretical Mode:**
```
⚠️ THEORETICAL MODE - Uses MODEL PRICING (Black-Scholes)
For EXECUTABLE trades with real bid/ask quotes:
    Use: python examples/run_real_cycle_snapshot.py
```

## Summary

| Question | Answer |
|----------|--------|
| Which mode for real trades? | **SNAPSHOT MODE** (`run_real_cycle_snapshot.py`) |
| Which uses real bid/ask? | **SNAPSHOT MODE** only |
| Which is the truth path? | **SNAPSHOT MODE** only |
| When to use theoretical mode? | Parameter exploration, sensitivity analysis, backtesting |
| Can I execute theoretical results? | ❌ NO - Model prices differ from market |

**Golden Rule:** If you're placing real trades, use **SNAPSHOT MODE**. Period.
