# Memory Bank: System Patterns

## Architecture Overview
Three-tier architecture for tail-risk options trading:

### 1. Data Layer
- **IBKR Snapshot** (`forecast_arb/ibkr/snapshot.py`): Live option chain fetching with bid/ask quotes
- **Kalshi Client** (`forecast_arb/kalshi/client.py`): Prediction market API integration
- **Spot Cache** (`forecast_arb/ibkr/spot_cache.py`): Cached underlier prices with sanity checks

### 2. Probability Layer  
- **P_Event System** (`forecast_arb/oracle/p_event_source.py`): 
  - Kalshi auto-mapping with market matcher
  - Options-implied calculation from vertical spreads
  - Graceful fallback with confidence scores
- **Edge Gate** (`forecast_arb/gating/edge_gate.py`):
  - Compares p_external vs p_implied
  - Minimum edge (5%) and confidence (60%) thresholds
  - External source policy (blocks fallback unless --allow-fallback-trade)

### 3. Structuring Layer
- **Crash Venture V1** (`forecast_arb/engine/crash_venture_v1_snapshot.py`):
  - Put spread generation at -8%, -10%, -12%, -15% moneyness
  - Monte Carlo evaluation with calibrated drift
  - EV/Dollar ranking metric
- **Quote System** (`forecast_arb/structuring/quotes.py`):
  - Bid/ask side pricing for executable trades
  - Model fallback for deep OTM options
  - Pricing quality tracking (EXECUTABLE, MID, MODEL)

## Key Patterns

### Determinism
- Config checksums ensure reproducibility
- Seed-based Monte Carlo (deterministic random walks)
- All outputs tagged with run_id and config hash

### Safety & Gating
- Multi-layer blocking: edge gate → external source policy → submission barriers
- Review-only mode for analysis without executable orders
- Comprehensive logging and artifact generation

### Data Integrity
- Snapshot metadata includes qualification counters, spot audit trail
- Unknown contract handling with graceful degradation
- Strike coverage verification (minimum 10 strikes below target)

### Auditability
- Run indexing system (runs/index.json, LATEST.json)
- Artifact preservation (gate_decision.json, p_event_implied.json, review_pack.md)
- Decision templates for manual workflow

## Critical Guardrails
1. **Never trade on fallback p_event** without explicit --allow-fallback-trade
2. **Minimum edge required** (5% by default) before generating structures
3. **Confidence thresholds** ensure data quality (60% minimum)
4. **Review-only mode** separates analysis from execution
5. **Submission requires confirmation** (--submit --confirm SUBMIT)
