# forecast_arb v0.2

Oracle + Options Structuring Engine for Kalshi Prediction Markets

## Overview

`forecast_arb` v0.2 is a **deterministic options structuring engine** that treats Kalshi market probabilities as ground truth (oracle) and generates + evaluates option structures via Monte Carlo simulation.

**Core Innovation**: Use prediction market probabilities to calibrate price dynamics, then structure options trades that exploit these insights under defined risk constraints.

## What Changed in v0.2

**Before (v0.1)**: LLM-based probability forecasting  
**Now (v0.2)**: Oracle-based options structuring

- ✅ No LLM - Kalshi market midpoint = p_event (ground truth)
- ✅ Drift calibration: Given p_event, solve for lognormal μ
- ✅ Option templates: Put spreads, call spreads, strangles
- ✅ Monte Carlo evaluation: 30k+ paths, percentiles, Greeks
- ✅ Constraint router: Filter by max loss, min prob profit, EV
- ✅ Event mapping: Kalshi markets → option underliers (SPY, QQQ, etc.)

## Installation

```powershell
# Install dependencies
pip install -r requirements.txt

# Or install in editable mode
pip install -e .
```

**Dependencies**: `numpy`, `scipy`, `py_vollib`, `pyyaml`, `requests`, `python-dotenv`

## Environment Setup

### 1. Copy Environment Template

```powershell
# Copy the example file
cp .env.example .env
```

### 2. Configure API Credentials

Edit `.env` and add your API keys:

```bash
# Kalshi API Configuration
KALSHI_API_KEY_ID=your_api_key_id_here
KALSHI_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----
your_private_key_here
-----END RSA PRIVATE KEY-----

# OpenAI API Configuration (if using LLM features)
OPENAI_API_KEY=your_openai_api_key_here

# Interactive Brokers Configuration
IBKR_HOST=127.0.0.1
IBKR_PORT=7496      # 7496 for live trading
IBKR_CLIENT_ID=19
```

**Get your credentials:**
- **Kalshi API**: https://kalshi.com/settings/api
- **OpenAI API**: https://platform.openai.com/api-keys
- **IBKR**: Configure Trader Workstation (TWS) or IB Gateway

### 3. Security Notes

✅ **DO**:
- Store credentials in `.env` file (already in `.gitignore`)
- Use environment variables for all secrets
- Keep `.env.example` updated (without real credentials)

❌ **DON'T**:
- Commit `.env` to version control
- Hardcode API keys in source code
- Share credentials in chat/email

The `KalshiClient` automatically loads credentials from environment variables:

```python
from forecast_arb.kalshi.client import KalshiClient

# Automatically uses KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY from .env
client = KalshiClient()

# Or override with explicit credentials
client = KalshiClient(api_key="custom_key", api_secret="custom_secret")
```

## Quick Start

### 2. Run Oracle Mode (Collect Probabilities)

```powershell
python -m forecast_arb.engine.run --config configs/campaign_b.yaml --mode oracle
```

**Output**: Collects Kalshi market probabilities for configured markets and saves to database.

### 3. Run Structure Mode (Generate Option Structures)

```powershell
python -m forecast_arb.engine.run --config configs/campaign_b.yaml --mode structure
```

**Output**: Generates and evaluates option structures, ranks by EV/Sharpe/prob_profit, saves top structures to database.

You can also run structure mode with a specific oracle run:
```powershell
python -m forecast_arb.engine.run --config configs/campaign_b.yaml --mode structure --oracle-run-id <run_id>
```

## Architecture

### Pipeline

```
1. Oracle → Collect Kalshi p_event
2. Mapping → Map Kalshi event to underlier (SPY, QQQ, etc.)
3. Calibration → Solve for drift μ given p_event constraint
4. Templates → Generate candidate structures (spreads, strangles)
5. Evaluation → Monte Carlo EV, std, prob_profit, percentiles
6. Router → Filter by constraints, rank by objective
```

### Core Modules

```
forecast_arb/
├── oracle/
│   └── kalshi_oracle.py       # Collect market probabilities as ground truth
├── structuring/
│   ├── event_map.py           # Map Kalshi events → option underliers
│   ├── calibrator.py          # Calibrate drift μ from p_event
│   ├── templates.py           # Generate option structures
│   ├── option_math.py         # Black-Scholes, Greeks (py_vollib)
│   ├── evaluator.py           # Monte Carlo evaluation (30k paths)
│   └── router.py              # Choose best structures under constraints
├── engine/
│   └── run.py                 # CLI entrypoint (oracle/structure modes)
├── utils/
│   └── database.py            # SQLite: oracle_markets, structures tables
└── kalshi/
    └── client.py              # Kalshi REST API client
```

## Configuration

Example `configs/campaign_b.yaml`:

```yaml
campaign_name: structure_demo

universe:
  buckets:
    SP500:
      series: ["INX"]
      tags: ["stocks", "sp500"]
    VIX:
      series: ["VIX"]
      tags: ["volatility"]
  max_markets_per_bucket: 5
  status: open

structuring:
  constraints:
    max_loss_usd_per_trade: 500   # Max risk per structure
    min_prob_profit: 0.4           # Min probability of profit
    min_ev: 0                      # Min expected value
  
  option_params:
    S0: 500.0                      # Current underlier price (SPY)
    r: 0.05                        # Risk-free rate
    T: 0.0822                      # Time to expiry (30 days)
    sigma_vol: 0.15                # Volatility assumption
    n_paths: 30000                 # Monte Carlo paths

storage:
  path: runs/forecasts.db

logging:
  level: INFO
```

## How It Works

### 1. Oracle Mode: Collect Ground Truth

```python
# Fetch Kalshi orderbook
orderbook = client.get_orderbook("INX-26FEB28")
yes_bid = orderbook["yes"]["bid"]  # e.g., 0.60
yes_ask = orderbook["yes"]["ask"]  # e.g., 0.64

# Compute event probability
p_event = (yes_bid + yes_ask) / 2.0  # = 0.62
```

**No LLM, no forecasting** - just treat the market as ground truth.

### 2. Event Mapping: Kalshi → Underlier

```python
# Map Kalshi event to option underlier
market_id = "INX-26FEB28"  # S&P 500 event
underlier = "SPY"          # Trade SPY options

# Extract expiry from market
expiry = "2026-02-28"
```

**Supported underliers**: SPY, QQQ, DIA, IWM, VIX, AAPL, TSLA, MSFT, NVDA, GLD, USO, BTC-USD, ETH-USD

### 3. Calibration: Solve for Drift

Given:
- `p_event = 0.62` (Kalshi probability)
- Event = "S&P 500 above 5250 on Feb 28" (5% barrier)
- Current price `S0 = 500`

Solve for drift `μ` such that:
```
P(S_T > K_barrier) = p_event
```

Using lognormal dynamics:
```
S_T = S0 * exp((μ - σ²/2) * T + σ * sqrt(T) * Z)
```

**Calibrator** uses binary search to find `μ` that matches `p_event`.

### 4. Templates: Generate Structures

**Put Spread** (bearish):
```python
structure = {
    "template_name": "put_spread",
    "legs": [
        {"type": "put", "strike": 490, "side": "long"},   # Buy 490P
        {"type": "put", "strike": 480, "side": "short"}   # Sell 480P
    ],
    "premium": -3.0,      # Pay $3 to enter
    "max_loss": -3.0,     # Max loss = premium
    "max_gain": 7.0       # Max gain = 10 (spread) - 3 (premium)
}
```

**Call Spread** (bullish):
```python
structure = {
    "template_name": "call_spread",
    "legs": [
        {"type": "call", "strike": 510, "side": "long"},  # Buy 510C
        {"type": "call", "strike": 520, "side": "short"}  # Sell 520C
    ]
}
```

**Strangle** (high volatility):
```python
structure = {
    "template_name": "strangle",
    "legs": [
        {"type": "put", "strike": 490, "side": "long"},   # Buy 490P
        {"type": "call", "strike": 510, "side": "long"}   # Buy 510C
    ]
}
```

### 5. Evaluation: Monte Carlo

For each structure:

```python
# Simulate 30k price paths with calibrated μ
paths = simulate_paths(S0, mu, sigma, T, n_paths=30000, seed=42)

# Compute payoff for each path
payoffs = [compute_payoff(structure, S_T) for S_T in paths]

# Statistics
ev = mean(payoffs)                    # Expected value
std = stdev(payoffs)                  # Standard deviation
prob_profit = mean(payoffs > 0)       # Probability of profit
percentiles = [p05, p50, p95]         # Risk metrics
```

**Deterministic**: Same seed → same results

### 6. Router: Filter & Rank

```python
# Filter by constraints
valid = [
    s for s in structures
    if abs(s["max_loss"]) <= 500           # Max $500 risk
    and s["prob_profit"] >= 0.4            # Min 40% win rate
    and s["ev"] >= 0                       # Positive EV
]

# Rank by objective
if objective == "max_ev":
    valid.sort(key=lambda s: s["ev"], reverse=True)
elif objective == "max_sharpe":
    valid.sort(key=lambda s: s["ev"] / s["std"], reverse=True)
```

## Database Schema

### oracle_markets

Stores Kalshi market probabilities:

```sql
CREATE TABLE oracle_markets (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    market_id TEXT,
    p_event REAL,              -- Market-implied probability
    bid REAL,
    ask REAL,
    spread_cents REAL,
    volume_24h INTEGER,
    asof_utc TEXT,
    raw_json TEXT,
    created_at TEXT
);
```

### structures

Stores evaluated option structures:

```sql
CREATE TABLE structures (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    underlier TEXT,            -- SPY, QQQ, etc.
    expiry TEXT,
    template TEXT,             -- put_spread, call_spread, strangle
    legs_json TEXT,            -- JSON array of legs
    premium REAL,
    max_loss REAL,
    max_gain REAL,
    ev REAL,                   -- Expected value
    ev_std REAL,               -- Standard deviation
    prob_profit REAL,          -- P(profit > 0)
    greeks_json TEXT,          -- Delta, gamma, vega, theta
    rank INTEGER,              -- 1 = best
    created_at TEXT
);
```

## Example Workflow

### Collect Oracle Data

```powershell
python -m forecast_arb.engine.run --config configs/campaign_b.yaml --mode oracle
```

**Output**:
```
INFO: Collecting oracle data for INX-26FEB28
INFO: p_event = 0.62, bid = 0.60, ask = 0.64
INFO: Mapped to underlier: SPY
INFO: Oracle run complete: oracle_20260127T171500
```

### Generate Structures

```powershell
python -m forecast_arb.engine.run --config configs/campaign_b.yaml --mode structure
```

**Output**:
```
INFO: Structuring for INX-26FEB28: p_event=0.620, underlier=SPY
INFO: Calibrated drift: μ=0.6850, achieved p=0.618
INFO: Evaluating call_spread: EV=$1.23, prob_profit=0.58
INFO: Evaluating put_spread: EV=-$0.45, prob_profit=0.38
INFO: Found 2 viable structures for INX-26FEB28
INFO: Structure run complete: structure_20260127T171600

# Top Structures
#1: call_spread
- Underlier: SPY
- Expected Value: $1.23
- Max Loss: $4.25
- Prob Profit: 58%
- P5/P50/P95: -$4.25 / $0.80 / $5.75
```

## Testing

Run comprehensive unit tests:

```powershell
# All tests
pytest tests/ -v

# Specific modules
pytest tests/test_calibrator.py -v    # Drift calibration tests
pytest tests/test_templates.py -v     # Option template tests
pytest tests/test_evaluator.py -v     # Monte Carlo evaluation tests
```

**Test Coverage**:
- ✅ Calibration convergence & determinism
- ✅ Template generation & payoff computation
- ✅ Monte Carlo path simulation
- ✅ Statistics (EV, std, percentiles, prob_profit)
- ✅ Constraint filtering & ranking

## Example Data

See `examples/options_snapshot_spy.json` for complete example with:
- Oracle input (Kalshi market data)
- Option chain (calls/puts with strikes, prices, IV)
- Candidate structures (call spread, put spread, strangle)
- Calibration params (μ, σ, event definition)
- Monte Carlo config (n_paths, seed, method)

## Advanced Usage

### Custom Constraints

```yaml
structuring:
  constraints:
    max_loss_usd_per_trade: 1000     # Higher risk tolerance
    min_prob_profit: 0.5             # Require 50%+ win rate
    min_ev: 10                       # Minimum $10 EV
    max_sharpe: 1.5                  # Max Sharpe ratio filter
```

### Different Objectives

```python
from forecast_arb.structuring.router import choose_best_structure

# Maximize EV
best = choose_best_structure(structures, constraints, objective="max_ev")

# Maximize Sharpe ratio
best = choose_best_structure(structures, constraints, objective="max_sharpe")

# Maximize probability of profit
best = choose_best_structure(structures, constraints, objective="max_prob_profit")
```

### Query Results

```python
from forecast_arb.utils.database import Database

db = Database("runs/forecasts.db")

# Get oracle data
oracle_data = db.get_oracle_markets_by_run("oracle_20260127T171500")

# Get structures
structures = db.get_structures_by_run("structure_20260127T171600")

for s in structures:
    print(f"#{s['rank']}: {s['template_name']} - EV=${s['ev']:.2f}, Win%={s['prob_profit']:.1%}")
```

## Important Notes & Caveats

### EV Metrics Interpretation ⚠️

**The EV (Expected Value) and EV/$ metrics are RANKING SCORES, not actual expected returns.**

These metrics help compare candidates within a run but should NOT be used as return forecasts. They rely on:
- Monte Carlo simulation with assumed crash probabilities
- Simplified market assumptions
- Single-expiry analysis without hedging dynamics

**Before trading**: Verify market prices, check liquidity, and perform independent analysis with real-world assumptions.

See [CAVEAT_FIXES.md](CAVEAT_FIXES.md) for detailed guidance.

### Strike Grid Alignment

For SPY options, use **v1.1 configuration** which aligns spread widths to actual IBKR strike grids:
- Spread widths: `[10, 20]` (aligned to $10 increments for far OTM strikes)
- Moneyness targets: `[-0.08, -0.10, -0.12, -0.15]` (8-15% OTM liquidity zones)

The v1.0 config is preserved for historical compatibility but may produce sparse candidates.

See [CAVEAT_FIXES.md](CAVEAT_FIXES.md) for migration guide and technical details.

## Roadmap

- [ ] Real-time option chain fetching (IBKR, TastyTrade APIs)
- [ ] More templates (iron condor, butterfly, calendar spreads)
- [ ] Greeks-based hedging strategies
- [ ] Portfolio-level risk management
- [ ] Live execution connector
- [ ] Backtesting framework with historical Kalshi + options data

## Migration from v0.1

If upgrading from v0.1 (LLM forecasting):

1. **Old LLM modules removed**: `llm/`, `evidence/`, `placebo.py`, `scoring.py`
2. **New modes**: Use `--mode oracle` or `--mode structure` (not `--mode paper`)
3. **Database schema changed**: New tables `oracle_markets` and `structures`
4. **Config changes**: Add `structuring` section (see example above)

## License

MIT

## Contributing

Contributions welcome! Please:
1. Add unit tests for new features
2. Maintain determinism (use seeded RNG)
3. Document API changes
4. Follow existing code style (Black formatter)

## Support

For issues or questions:
- Open a GitHub issue
- Check `examples/options_snapshot_spy.json` for data format
- Run tests: `pytest tests/ -v`

---

**v0.2 Status**: Core structuring engine complete. Oracle + calibration + evaluation + routing fully functional. Ready for production use with real option chain data.
