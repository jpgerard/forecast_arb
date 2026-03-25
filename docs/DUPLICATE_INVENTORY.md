# Duplicate Inventory and Refactoring Plan

## Overview
This document identifies duplicate code and overlapping responsibilities in the forecast_arb repository.

## 1. Runner Scripts (CRITICAL DUPLICATION)

### Current State
Multiple entrypoints with overlapping functionality:

| File | Purpose | Mode | Status |
|------|---------|------|--------|
| `scripts/run_real_cycle.py` | Full cycle with IBKR snapshot | Snapshot (executable) | **KEEP as canonical** |
| `examples/run_real_cycle_snapshot.py` | Full cycle with IBKR snapshot | Snapshot (executable) | **DUPLICATE - Remove** |
| `examples/run_real_cycle.py` | Full cycle with model pricing | Theoretical (B-S) | **DUPLICATE - Remove** |
| `examples/run_crash_venture_v1.py` | Demo runner | Legacy | **Remove** |

### Action Plan
- **CANONICAL:** `scripts/run_daily.py` (rename from `scripts/run_real_cycle.py`)
  - This will be the ONLY production entrypoint
  - Supports both snapshot and model modes via CLI flags
- **REMOVE:** All runners in `examples/` directory
- **CONSOLIDATE:** Common runner logic into `forecast_arb/core/run_cycle.py`

## 2. IBKR Snapshot Module Location

### Current State
- **`forecast_arb/data/ibkr_snapshot.py`** - Main implementation (1500+ lines)
- Should be in `forecast_arb/ibkr/` package

### Action Plan
- **MOVE:** `forecast_arb/data/ibkr_snapshot.py` → `forecast_arb/ibkr/snapshot.py`
- **ADD STUB:** Keep `forecast_arb/data/ibkr_snapshot.py` as compatibility stub with deprecation warning
- **REMOVE:** `forecast_arb/data/` directory (only contains one misplaced file)

## 3. Manifest Writer Location

### Current State
- **`forecast_arb/utils/manifest.py`** - Manifest and config checksums
- Should be core infrastructure, not "util"

### Action Plan
- **MOVE:** `forecast_arb/utils/manifest.py` → `forecast_arb/core/manifest.py`
- **ADD STUB:** Keep `forecast_arb/utils/manifest.py` as compatibility stub

## 4. Missing Core Infrastructure

### Need to Create `forecast_arb/core/`
New canonical core package for cross-cutting concerns:

| File | Purpose | Status |
|------|---------|--------|
| `forecast_arb/core/__init__.py` | Package init | **CREATE** |
| `forecast_arb/core/manifest.py` | Manifest writer (moved from utils) | **MOVE** |
| `forecast_arb/core/artifacts.py` | Centralized artifact writing | **CREATE** |
| `forecast_arb/core/logging.py` | Logging configuration | **CREATE** |
| `forecast_arb/core/config.py` | Config loading utilities | **CREATE** |
| `forecast_arb/core/run_cycle.py` | Shared runner logic | **CREATE** |

## 5. Examples Directory

### Current State
- Mix of demo scripts, JSON fixtures, and deprecated runners
- Clutters repo root

### Action Plan
- **KEEP:** JSON fixtures (move to `tests/fixtures/`)
- **REMOVE:** All runner scripts (consolidated into scripts/run_daily.py)
- **DECISION:** Delete `examples/` directory entirely

## 6. Scripts Directory Cleanup

### Target State
Only 2 scripts allowed:
- `scripts/run_daily.py` - **THE** canonical production runner
- `scripts/kalshi_smoke.py` - Connectivity smoke test (keep)

## 7. Import Paths to Update

After moves, update imports in:
- All test files
- All engine files
- All structuring files
- Any remaining scripts

### Import Changes
```python
# OLD
from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter
from forecast_arb.utils.manifest import ManifestWriter

# NEW
from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
from forecast_arb.core.manifest import ManifestWriter
```

## 8. No Other Duplicates Found

### Clean Areas (No Action Needed)
- ✅ `forecast_arb/kalshi/` - Single client, single mapper
- ✅ `forecast_arb/oracle/` - Well-separated p_event sources
- ✅ `forecast_arb/options/` - Clean implied prob + event mapping
- ✅ `forecast_arb/gating/` - Single edge gate implementation
- ✅ `forecast_arb/structuring/` - Well-organized templates/calibrator/evaluator
- ✅ `forecast_arb/engine/` - Clear separation between modes (crash_venture_v1.py vs crash_venture_v1_snapshot.py)

## Summary Statistics

| Category | Count | Action |
|----------|-------|--------|
| Duplicate runners | 3 | Consolidate → 1 canonical |
| Misplaced modules | 2 | Move to correct package |
| New core modules | 6 | Create core/ infrastructure |
| Compatibility stubs | 2 | Temporary deprecation warnings |
| Directories to remove | 2 | `examples/`, `forecast_arb/data/` |

## Risk Mitigation

1. **Keep compatibility stubs** - Don't break existing imports immediately
2. **Add deprecation warnings** - Alert on old import paths
3. **Run full test suite** - Verify no regressions
4. **Update all internal code first** - Before removing stubs
5. **Document changes** - Clear migration guide in docs/architecture.md
