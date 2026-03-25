# Forecast Arb Repository Structure

## Overview
This repository contains a sophisticated forecasting arbitrage system that identifies and executes options trades by comparing market probabilities across different platforms (IBKR options and Kalshi prediction markets).

---

## 📁 Root Directory

### Core Documentation
- **README.md** - Main project documentation
- **SETUP.md** - Installation and setup instructions
- **ARCHITECTURE_MODES.md** - System architecture and operational modes
- **DAILY_WORKFLOW_GUIDE.md** - Daily operational workflow for running the system

### Feature Documentation
- **CRASH_VENTURE_V1_README.md** - Crash venture strategy v1 documentation
- **CRASH_VENTURE_V2_README.md** - Crash venture strategy v2 documentation (enhanced)
- **RUN_DAILY_V2_README.md** - Daily run v2 workflow documentation
- **EXECUTION_REFACTOR_README.md** - Execution system refactoring notes
- **IBKR_TAIL_STRIKES_README.md** - Tail strike handling documentation
- **KALSHI_AUTO_MAPPING_README.md** - Kalshi market auto-mapping system
- **KALSHI_MULTI_SERIES_README.md** - Multi-series Kalshi market handling
- **P_EVENT_SYSTEM_README.md** - Probabilistic event system documentation
- **REVIEW_ONLY_MODE_README.md** - Review-only mode (no execution) documentation

### Phase Completion Docs
- **PHASE3_DECISION_QUALITY_LOOP_COMPLETE.md** - Decision quality loop implementation
- **PHASE3_INTEGRATION_GUIDE.md** - Phase 3 integration guide
- **PHASE3_V2_INTEGRATION_COMPLETE.md** - Phase 3 v2 completion notes
- **PHASE4_STRUCTURING_COMPLETE.md** - Phase 4 structuring completion
- **PHASE4B_EXECUTION_ENFORCEMENT_COMPLETE.md** - Execution enforcement completion

### Fix Documentation
- **CAVEAT_FIXES.md** - Various caveat fixes
- **LEDGER_SEMANTICS_FIX.md** - Ledger semantics corrections
- **MONEYNESS_MISMATCH_FIX.md** - Moneyness calculation fix
- **PROXY_PROBABILITY_FIX.md** - Proxy probability calculation fix
- **STRIKE_COVERAGE_ENHANCEMENT.md** - Strike coverage improvements

### Configuration & Dependencies
- **pyproject.toml** - Python project configuration
- **requirements.txt** - Python dependencies
- **.env.example** - Environment variable template
- **.gitignore** - Git ignore rules

### Verification Scripts (Root Level)
- **verify_setup.py** - Setup verification script
- **check_kalshi_markets.py** - Kalshi market availability checker
- **check_snapshot_coverage.py** - Snapshot coverage verification
- **diagnose_kalshi_spx.py** - SPX market diagnostics
- **explain_kalshi_mismatch.py** - Mismatch explanation tool
- **fetch_docs.py** - Documentation fetcher

### Standalone Test Scripts (Root Level)
- **test_intent_emission_standalone.py** - Intent emission testing
- **test_ledger_semantics_fix.py** - Ledger semantics testing
- **test_phase4b_enforcement_standalone.py** - Enforcement testing
- **test_pr1_standalone.py** through **test_pr6_standalone.py** - Pull request tests
- **test_selloff_fix_standalone.py** - Selloff regime fix testing
- **test_financial_events.py** - Financial event testing
- **test_kalshi_connection.py** - Kalshi API connection testing

---

## 📁 forecast_arb/ (Main Package)

### Core Modules (`forecast_arb/core/`)
The fundamental business logic and orchestration:
- **regime.py** - Market regime detection and classification
- **regime_result.py** - Regime detection results
- **regime_orchestration.py** - Multi-regime orchestration system
- **ledger.py** - Trade and decision ledger for tracking history
- **dqs.py** - Decision Quality Score calculation

### Data Layer (`forecast_arb/data/`)
Data fetching, caching, and management

### Engine (`forecast_arb/engine/`)
Strategy engines and campaign execution:
- **crash_venture_v1_snapshot.py** - Crash venture v1 strategy engine

### Execution (`forecast_arb/execution/`)
Trade execution and outcome tracking:
- **intent_builder.py** - Builds trade intents from candidates
- **execute_trade.py** - Executes trades on IBKR
- **execution_result.py** - Execution result data structures
- **outcome_ledger.py** - Trade outcome tracking and ledger

### Gating (`forecast_arb/gating/`)
Trade gating and risk controls before execution

### IBKR Integration (`forecast_arb/ibkr/`)
Interactive Brokers API integration:
- **snapshot.py** - IBKR options snapshot fetching
- Contract lookup, pricing, and order management

### Kalshi Integration (`forecast_arb/kalshi/`)
Kalshi prediction market API integration:
- Market lookup, pricing, and contract mapping
- Multi-series market handling

### Options (`forecast_arb/options/`)
Options-specific logic:
- **event_def.py** - Financial event definitions
- Option pricing, implied probability calculations
- Contract specifications

### Oracle (`forecast_arb/oracle/`)
Regime detection and selection:
- **regime_selector.py** - Selects appropriate regime for current market conditions

### Review (`forecast_arb/review/`)
Candidate review and decision-making before execution

### Risk (`forecast_arb/risk/`)
Risk management and position sizing

### Structuring (`forecast_arb/structuring/`)
Trade structuring and candidate generation:
- **candidate_validator.py** - Validates trade candidates

### Utils (`forecast_arb/utils/`)
Shared utilities and helper functions

---

## 📁 scripts/
Operational scripts for running the system:

- **run_daily.py** - Daily workflow execution (v1)
- **run_daily_v2.py** - Enhanced daily workflow (v2)
- **run_real_cycle.py** - Real trading cycle execution
- **regime_smoke_test.py** - Regime detection smoke tests
- **score_decision.py** - Score past decisions
- **weekly_pm_review.py** - Weekly portfolio manager review
- **kalshi_smoke.py** - Kalshi API smoke tests
- **runs.py** - Run management utilities

---

## 📁 tests/
Comprehensive test suite:

### Integration Tests
- **test_phase3_integration.py** - Phase 3 integration tests
- **test_crash_venture_v1_regression.py** - Crash venture regression tests
- **test_selloff_regime_wiring.py** - Selloff regime wiring tests

### Component Tests
- **test_calibrator.py** - Probability calibrator tests
- **test_evaluator.py** - Trade evaluator tests
- **test_edge_gate.py** - Edge gating tests
- **test_execution_guards.py** - Execution guard tests
- **test_intent_emission_v2.py** - Intent emission v2 tests
- **test_intent_schema_validation.py** - Intent schema validation

### IBKR Tests
- **test_ibkr_snapshot_unknown_contracts.py** - Unknown contract handling
- **test_ibkr_tail_strikes.py** - Tail strike handling

### Kalshi Tests
- **test_kalshi_market_mapper.py** - Market mapper tests
- **test_kalshi_multi_series.py** - Multi-series tests
- **test_kalshi_numeric_validation.py** - Numeric validation tests

### Regime & Ledger Tests
- **test_regime_config_overlay.py** - Regime configuration tests
- **test_phase3_pr31_ledger.py** through **test_phase3_pr35_wiring.py** - Phase 3 component tests

### Risk & Options Tests
- **test_caps.py** - Capital caps tests
- **test_min_debit_units.py** - Minimum debit unit tests
- **test_options_implied_prob.py** - Implied probability tests
- **test_iv_source.py** - Implied volatility source tests

### Execution Tests
- **test_phase4_structuring.py** - Structuring tests
- **test_fallback_trade_block.py** - Fallback blocking tests
- **test_no_trade_no_raise.py** - No-trade scenario tests
- **test_ev_per_dollar_regression.py** - EV per dollar regression

---

## 📁 configs/
Strategy configuration files:

- **campaign_b.yaml** - Campaign B configuration
- **structuring_crash_venture_v1.yaml** - Crash venture v1 config
- **structuring_crash_venture_v1_1.yaml** - Crash venture v1.1 config
- **structuring_crash_venture_v2.yaml** - Crash venture v2 config
- **test_structuring_crash_venture_v1.yaml** - Test configuration

---

## 📁 intents/
Generated trade intents (JSON):

- **spy_20260320_590_570_crash.json** - Example crash trade intent
- **spy_20260327_585_565_crash.json** - Example crash trade intent
- **execution_result.json** - Execution result example
- Trade intents ready for review or execution

---

## 📁 runs/
Historical run data and artifacts:

### Run Ledgers
- **regime_ledger.jsonl** - Historical regime detection log
- **trade_outcomes.jsonl** - Historical trade outcomes log

### Run Directories
- **crash_venture_v1/** - Crash venture v1 run archives
- **crash_venture_v2/** - Crash venture v2 run archives
- **weekly_reviews/** - Weekly PM review archives

Each run directory contains:
- **artifacts/** - Decision artifacts, gate decisions, review candidates
- Timestamped execution records

---

## 📁 snapshots/
IBKR market snapshots (JSON):

- **SPY_snapshot_YYYYMMDD_HHMMSS.json** - Timestamped SPY options snapshots
- Used for backtesting and analysis
- Historical market data for regime detection

---

## 📁 examples/
Example data files for documentation:

- **ibkr_snapshot_spy.json** - Example IBKR snapshot (minimal)
- **ibkr_snapshot_spy_full.json** - Example IBKR snapshot (complete)
- **options_snapshot_spy.json** - Example options snapshot

---

## 📁 artifacts/
Build and test artifacts:

- **REFACTOR_TEST_BASELINE.md** - Test baseline documentation
- **test_post_refactor_results.txt** - Post-refactor test results
- **trade_outcomes.jsonl** - Trade outcome logs

---

## 📁 docs/
Additional documentation:

- **architecture.md** - System architecture documentation
- **DUPLICATE_INVENTORY.md** - Duplicate inventory handling

---

## 📁 cline_docs/
AI assistant memory bank:

- **productContext.md** - Product vision and context
- **activeContext.md** - Current work and next steps
- **systemPatterns.md** - System architecture patterns
- **techContext.md** - Technical stack and constraints
- **progress.md** - Development progress tracking

---

## 📁 tools/
Development tools and utilities

---

## Key System Flow

1. **Snapshot Collection** (`forecast_arb/ibkr/snapshot.py`)
   - Fetches current market data from IBKR for SPY options

2. **Regime Detection** (`forecast_arb/oracle/regime_selector.py`, `forecast_arb/core/regime.py`)
   - Analyzes market conditions to determine current regime (crash, selloff, normal)

3. **Candidate Generation** (`forecast_arb/engine/`, `forecast_arb/structuring/`)
   - Generates potential trade candidates based on regime

4. **Trade Structuring** (`forecast_arb/structuring/candidate_validator.py`)
   - Validates and structures trade candidates

5. **Intent Building** (`forecast_arb/execution/intent_builder.py`)
   - Converts candidates to executable trade intents

6. **Gating & Review** (`forecast_arb/gating/`, `forecast_arb/review/`)
   - Applies risk controls and review gates

7. **Execution** (`forecast_arb/execution/execute_trade.py`)
   - Executes approved trades on IBKR (or review-only mode)

8. **Outcome Tracking** (`forecast_arb/execution/outcome_ledger.py`, `forecast_arb/core/ledger.py`)
   - Records trade outcomes and decisions for analysis

9. **Weekly Review** (`scripts/weekly_pm_review.py`)
   - Reviews performance and decision quality

---

## Development Commands

### Run Daily Workflow
```bash
python scripts/run_daily_v2.py
```

### Run Tests
```bash
pytest tests/
```

### Verify Setup
```bash
python verify_setup.py
```

### Score Past Decisions
```bash
python scripts/score_decision.py
```

### Weekly PM Review
```bash
python scripts/weekly_pm_review.py
```

---

**Last Updated:** 2026-02-24
