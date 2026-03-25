# forecast_arb Architecture

## Overview
This document describes the canonical package structure and architectural patterns for the forecast_arb project.

## Package Layout

```
forecast_arb/
├── core/              # Core infrastructure (NEW)
│   ├── __init__.py
│   ├── manifest.py    # Run tracking and config checksums
│   ├── artifacts.py   # Centralized I/O (JSON, YAML, text)
│   ├── config.py      # Config loading utilities
│   └── logging.py     # Logging configuration
│
├── ibkr/              # Interactive Brokers integration
│   ├── __init__.py
│   ├── snapshot.py    # Option chain snapshot exporter
│   ├── spot_cache.py  # Spot price caching
│   └── types.py       # IBKR data types
│
├── kalshi/            # Kalshi prediction market integration
│   ├── __init__.py
│   ├── client.py      # API client
│   └── market_mapper.py  # Market mapping utilities
│
├── oracle/            # Event probability sources
│   ├── __init__.py
│   ├── kalshi_oracle.py  # Kalshi-based oracle
│   └── p_event_source.py # Pluggable p_event interface
│
├── options/           # Options pricing and analysis
│   ├── __init__.py
│   ├── implied_prob.py   # Implied probability from options
│   └── event_to_strike.py  # Event mapping to strikes
│
├── gating/            # Trade filters and gates
│   ├── __init__.py
│   └── edge_gate.py   # Edge/EV filtering
│
├── structuring/       # Strategy templates and evaluation
│   ├── __init__.py
│   ├── templates.py   # Trade structure templates
│   ├── calibrator.py  # Monte Carlo calibration
│   ├── evaluator.py   # Structure evaluation
│   ├── units.py       # Unit conversions
│   ├── router.py      # Structure routing
│   ├── output_formatter.py  # Output formatting
│   ├── snapshot_io.py # Snapshot I/O utilities
│   └── event_map.py   # Event mapping
│
├── engine/            # Execution engines
│   ├── __init__.py
│   ├── crash_venture_v1.py  # Model-based mode
│   ├── crash_venture_v1_snapshot.py  # Snapshot mode
│   └── run.py         # Common runner logic
│
├── utils/             # General utilities
│   ├── __init__.py
│   ├── cache.py       # Caching utilities
│   └── database.py    # Database utilities
│
└── data/              # DEPRECATED - being phased out
    ├── __init__.py
    └── ibkr_snapshot.py  # STUB - redirects to ibkr.snapshot
```

## Entry Points

### Production Runner
**`scripts/run_daily.py`** - THE canonical daily runner

This is the **only** production entrypoint. It supports:
- Real IBKR option snapshots
- Kalshi oracle integration
- Configurable filters and parameters
- Both live and fallback modes

### Development Scripts
- `scripts/kalshi_smoke.py` - Connectivity smoke test

## Where Things Go

### Core Infrastructure (`forecast_arb/core/`)
**Purpose:** Cross-cutting concerns that support the entire system

**Put here:**
- Manifest and run tracking
- Artifact I/O (JSON, YAML, text writing)
- Configuration loading
- Logging setup
- Common runner logic

**Examples:**
```python
from forecast_arb.core import ManifestWriter, write_json, ensure_dir
```

### Data Sources

**IBKR (`forecast_arb/ibkr/`):**
- Option chain snapshots
- Spot price fetching and caching
- IBKR data types

**Kalshi (`forecast_arb/kalshi/`):**
- API client
- Market discovery and mapping

**Oracle (`forecast_arb/oracle/`):**
- Event probability sources
- Pluggable p_event interface

### Strategy Logic

**Options (`forecast_arb/options/`):**
- Option pricing (Black-Scholes, implied vol)
- Event-to-strike mapping
- Probability extraction from options

**Structuring (`forecast_arb/structuring/`):**
- Trade templates (spreads, butterflies, etc.)
- Monte Carlo calibration
- Structure evaluation and ranking
- Output formatting

**Gating (`forecast_arb/gating/`):**
- Trade filters (EV, expected return, etc.)
- Risk gates

**Engine (`forecast_arb/engine/`):**
- Complete strategy execution
- Mode-specific implementations (model vs snapshot)

## Import Patterns

### Canonical Imports (USE THESE)
```python
# Core infrastructure
from forecast_arb.core import ManifestWriter, write_json, ensure_dir

# IBKR
from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
from forecast_arb.ibkr.spot_cache import load_cached_spot

# Kalshi
from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.kalshi.market_mapper import map_event_to_market

# Oracle
from forecast_arb.oracle.p_event_source import create_p_event_source

# Options
from forecast_arb.options.implied_prob import compute_implied_probability

# Strategy
from forecast_arb.structuring.templates import get_template
from forecast_arb.structuring.calibrator import calibrate_strategy
from forecast_arb.gating.edge_gate import apply_edge_filter

# Engine
from forecast_arb.engine.crash_venture_v1_snapshot import run_crash_venture_v1_snapshot
```

### Deprecated Imports (AVOID - will be removed)
```python
# DEPRECATED - use forecast_arb.ibkr.snapshot
from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter

# DEPRECATED - use forecast_arb.core.manifest
from forecast_arb.utils.manifest import ManifestWriter
```

## Design Principles

### 1. Single Responsibility
Each package has a clear, focused purpose:
- `core` = infrastructure
- `ibkr` = IBKR data
- `kalshi` = Kalshi data
- `oracle` = probability sources
- `options` = option pricing
- `structuring` = strategy logic
- `gating` = filters
- `engine` = execution

### 2. No Duplication
- ONE canonical runner: `scripts/run_daily.py`
- ONE IBKR snapshot module: `forecast_arb/ibkr/snapshot.py`
- ONE manifest writer: `forecast_arb/core/manifest.py`

### 3. Backward Compatibility (Temporary)
Deprecated modules remain as stubs that:
- Emit `DeprecationWarning` on import
- Re-export from new location
- Will be removed in future version

### 4. Centralized I/O
All artifact writes use `forecast_arb.core.artifacts`:
```python
from forecast_arb.core import write_json, write_yaml, ensure_dir

# Instead of ad-hoc:
# with open(...) as f: json.dump(...)

# Use:
write_json("output.json", data)
```

### 5. Testability
- Repo hygiene enforced by `tests/test_repo_hygiene.py`
- Package structure validated on every test run
- Prevents regression to duplicate code

## Migration Guide

### For Code Authors

**Moving from old imports:**
```python
# OLD
from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter
from forecast_arb.utils.manifest import ManifestWriter

# NEW
from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
from forecast_arb.core.manifest import ManifestWriter
```

**Using centralized I/O:**
```python
# OLD
import json
with open("output.json", "w") as f:
    json.dump(data, f, indent=2)

# NEW
from forecast_arb.core import write_json
write_json("output.json", data)
```

### For Users

**Running daily strategies:**
```bash
# The ONLY production runner
python scripts/run_daily.py --help

# Snapshot mode (executable trades)
python scripts/run_daily.py --snapshot path/to/snapshot.json

# Or create new snapshot
python scripts/run_daily.py --dte-min 30 --dte-max 60
```

## File Organization Rules

### ✅ Allowed Locations

| Type | Location | Example |
|------|----------|---------|
| Production runner | `scripts/run_daily.py` | Daily strategy execution |
| Smoke tests | `scripts/kalshi_smoke.py` | Connectivity tests |
| Core logic | `forecast_arb/<package>/*.py` | All business logic |
| Tests | `tests/test_*.py` | Unit/integration tests |
| Configs | `configs/*.yaml` | Strategy configurations |
| Docs | `docs/*.md` | Documentation |

### ❌ Not Allowed

| Type | Reason |
|------|--------|
| Runner scripts in `examples/` | Consolidate to `scripts/run_daily.py` |
| Business logic in `scripts/` | Move to `forecast_arb/` packages |
| Duplicate implementations | Keep single canonical version |
| Imports from `.data.ibkr_snapshot` | Use `.ibkr.snapshot` |
| Imports from `.utils.manifest` | Use `.core.manifest` |

## Testing

Run hygiene tests:
```bash
pytest tests/test_repo_hygiene.py -v
```

This validates:
- ✓ Canonical runner exists
- ✓ No duplicate runners
- ✓ Core package structure
- ✓ Modules in correct locations
- ✓ Compatibility stubs present

## Summary

**Key Points:**
1. **ONE runner:** `scripts/run_daily.py`
2. **Core package:** Infrastructure in `forecast_arb/core/`
3. **IBKR in right place:** `forecast_arb/ibkr/snapshot.py`
4. **Manifest in core:** `forecast_arb/core/manifest.py`
5. **Centralized I/O:** Use `forecast_arb.core.artifacts`
6. **No duplication:** Single canonical implementation for each concern
7. **Hygiene tests:** Enforced by `tests/test_repo_hygiene.py`
