# IBKR Broker-State Sync — Diagnostics + Sync Patch

**Date:** 2026-03-09  
**Scope:** Narrow diagnostics + safe sync for existing live IBKR positions  
**No changes to:** strategy logic, allocator gating semantics, existing ledger rows

---

## 1. Root Cause Summary

### The Mismatch

IBKR holds **3 live SPY bear put spreads**:

| Spread                          | Long Strike | Short Strike |
|---------------------------------|-------------|--------------|
| SPY Apr 17 2026 — Bear Put      | 575         | 555          |
| SPY Mar 27 2026 — Bear Put      | 590         | 570          |
| SPY Mar 20 2026 — Bear Put      | 590         | 570          |

CCC internal state (`runs/allocator/positions.json`) reported only **1** open crash position.

### Why Positions Were Missing

`positions.json` is built **exclusively** from `allocator_fills_ledger.jsonl` rows with
`action="POSITION_OPENED"`, via `fills.build_positions_snapshot()`.

Those rows are written **only** when `ccc_reconcile` processes an `execution_result.json`.

`execution_result.json` is **only** created when CCC's own `ccc_execute.py` places the trade.

The 3 IBKR spreads were placed **directly in IBKR TWS** (or by a previous system), so:
- No `execution_result.json` was ever written for them
- No `POSITION_OPENED` row was ever appended to the fills ledger
- `positions.json` never received entries for them
- `compute_inventory_state_from_positions()` counted `crash_open=1` instead of `crash_open=3`

### Why Reconcile Didn't Catch It

`reconcile.py` has a `reconcile_from_ibkr_stubs()` function that can parse raw IBKR
positions into `SleevePosition` objects, but:
- It was **never wired** into `inventory.py`, `plan.py`, or any script
- It only produces `SleevePosition` objects — it does **not** write to `positions.json`
  or `allocator_fills_ledger.jsonl`
- It depends on a live IBKR snapshot being provided by the caller — no caller exists

### Why Inventory Counted 1 Instead of 3

The single position that was counted came from an earlier CCC-executed intent that
**did** flow through the fills pipeline. The other 2 (or all 3) spreads that existed
before CCC's fills ledger was established had no representation in the ledger.

---

## 2. Files Changed

| File | Status | Description |
|------|--------|-------------|
| `forecast_arb/allocator/broker_sync.py` | **NEW** | Core sync module (pure functions + orchestrator) |
| `scripts/ccc_import_ibkr_positions.py`  | **NEW** | CLI operator interface |
| `tests/test_broker_sync_diagnostics.py` | **NEW** | 67 deterministic tests (all pass) |

**No existing files were modified.**  
All downstream readers (`inventory.py`, `ccc_report.py`, `plan.py`, `harvest.py`) already
read from `positions.json` / `allocator_fills_ledger.jsonl` — they pick up the imported
positions automatically once those files are updated.

---

## 3. Exact Sync Mechanism Added

### Step 1: Build a fills-ledger row from the IBKR combo dict

`broker_sync.build_ibkr_import_fill_row(combo, date_str, mode)` (pure function):

```python
# Input (IBKR combo dict):
{
    "symbol":       "SPY",
    "expiry":       "20260417",    # YYYYMMDD or YYYY-MM-DD (normalised)
    "long_strike":  575.0,         # BUY put leg (higher strike)
    "short_strike": 555.0,         # SELL put leg (lower strike)
    "qty":          1,
    "regime":       "crash",       # default "crash" if absent
    "entry_debit":  None,          # $/contract (optional; None → time-stop only)
}

# Output row (identical to fills.build_fill_row() output, except):
#   source    = "ibkr_import"     ← diagnostic tag (no semantic effect)
#   intent_id = "ibkr_import_SPY_20260417_575_555"  ← stable dedup key
```

### Step 2: Dedup-safe append to fills ledger

`fills.append_fills_ledger()` deduplicates by `intent_id`.  
Because the `intent_id` is a **deterministic contract-based string** (not a timestamp hash),
re-running the import with the same combo list is a strict no-op.

### Step 3: Rebuild positions.json

`fills.build_positions_snapshot()` reads all `POSITION_OPENED` rows (including the new
`ibkr_import` rows) and rebuilds `positions.json`.  
`build_positions_snapshot()` requires no changes — it already handles any row with
`action="POSITION_OPENED"` regardless of `source`.

### Step 4: Downstream readers pick up automatically

After positions.json is rebuilt:
- `compute_inventory_state_from_positions()` → `crash_open=3`
- `ccc_report.print_portfolio_summary()` → Section B shows `Crash open positions: 3`
- `harvest.py` → DTE / multiple tracking begins for imported positions
- Allocator gating → `inv_effective.needs_open("crash")` returns False (target met)

### Operator command

```powershell
# Import the 3 live IBKR spreads:
python scripts/ccc_import_ibkr_positions.py --mode live `
    --spread SPY 20260417 575 555 1 `
    --spread SPY 20260327 590 570 1 `
    --spread SPY 20260320 590 570 1

# Dry-run first to preview:
python scripts/ccc_import_ibkr_positions.py --dry-run --mode live `
    --spread SPY 20260417 575 555 1 `
    --spread SPY 20260327 590 570 1 `
    --spread SPY 20260320 590 570 1

# Diagnose diff (no import):
python scripts/ccc_import_ibkr_positions.py --diagnose --mode live `
    --spread SPY 20260417 575 555 1 `
    --spread SPY 20260327 590 570 1 `
    --spread SPY 20260320 590 570 1

# Verify after import:
python scripts/ccc_report.py
```

---

## 4. Mapping IBKR Combo Positions → CCC SleevePosition Objects

The mapping used by `build_ibkr_import_fill_row()`:

| IBKR field          | CCC fills-row field     | Notes |
|---------------------|-------------------------|-------|
| `symbol`            | `underlier`             | Upper-cased |
| `expiry`            | `expiry`                | Normalised to YYYYMMDD (dashes stripped) |
| `long_strike`       | `strikes[0]`            | Higher strike (BUY put leg) — CCC convention |
| `short_strike`      | `strikes[1]`            | Lower strike (SELL put leg) |
| `qty`               | `qty`                   | Default 1 |
| `regime`            | `regime`                | Default "crash"; validated |
| `entry_debit`       | `entry_debit_gross`     | $/contract; None → time-stop only |
| *(no CCC intent)*   | `intent_id`             | Stable "ibkr_import_SPY_..." key |
| *(import only)*     | `source`                | "ibkr_import" (diagnostic tag) |
| *(import only)*     | `candidate_id`          | "" (no CCC candidate) |

The fills row is then passed through `build_positions_snapshot()` which produces
`SleevePosition`-compatible position dicts in positions.json.

`reconcile.py`'s `reconcile_positions()` function (used by `plan.py`) reads from the
allocator plan ledger, not positions.json.  The allocator plan ledger path is separate.
For full `SleevePosition` reconciliation via plan.py, the operator can additionally
use `reconcile_from_ibkr_stubs()` (already in reconcile.py but not wired).  However,
for inventory gating and reporting purposes (the immediate issue), positions.json is
the authoritative source and is now correctly populated.

---

## 5. Test Results

### New tests: `tests/test_broker_sync_diagnostics.py`

**67 tests, all passing (0.82s)**

| Test Class | Count | Description |
|-----------|-------|-------------|
| `TestIbkrImportPositionId` | 6 | Deterministic key format, collision-safety, distinctness |
| `TestBuildIbkrImportFillRow` | 29 | Pure builder: happy path, validation, normalisation, 3 live spreads |
| `TestSyncIbkrPositions` | 13 | Orchestration: import, idempotency, dry_run, error handling, file preservation |
| `TestImportedBrokerSpreadVisibleInPositionsSnapshot` (**SPEC ①**) | 5 | Imported spread visible in positions.json |
| `TestOpenCrashCountMatchesImportedPositions` (**SPEC ②**) | 7 | crash_open == 3 after import |
| `TestReportInventoryCountConsistentAfterSync` (**SPEC ③**) | 5 | ccc_report shows crash=3 after sync |
| `TestDiffIbkrVsPositions` | 4 | Diagnostic diff: missing/matched/partial |

### Pre-existing suite (unchanged)

**164 passed, 1 warning** (`test_ccc_report.py`, `test_ccc_reconcile.py`,
`test_patch_v19_max_value_low_complexity.py`, `test_ccc_v18_pending.py`,
`test_ccc_clean_base.py`) — zero regressions.

---

## 6. Idempotency Proof

```
ibkr_import_SPY_20260417_575_555
ibkr_import_SPY_20260327_590_570
ibkr_import_SPY_20260320_590_570
```

Each key is the `intent_id` stored in the fills ledger row.  
`append_fills_ledger()` skips any row whose `intent_id` already exists.
Running the import twice → `imported=0, skipped_dedup=3` on the second run.
`positions.json` is never touched on a no-op run (mtime preserved — test verified).

---

## 7. What Was NOT Changed (Scope Constraints)

- No changes to strategy logic (`open_plan.py`, `plan.py`, `policy.py`, etc.)
- No changes to allocator gating semantics
- No changes to `fills.py`, `inventory.py`, `reconcile.py`, or `ccc_report.py`
- No changes to any existing test
- `reconcile_from_ibkr_stubs()` in `reconcile.py` (pre-existing) remains un-wired;
  it was not the right hook because it doesn't write to positions.json / fills ledger

---

## 8. Outstanding Items (Not in Scope for This Patch)

1. **Entry debit recovery:** The 3 imported spreads have `entry_debit_gross=None`
   because the fill prices are unknown.  Harvest multiple cannot be computed.
   Operator should manually amend with actual fill prices via a direct fills-ledger
   edit or a follow-up patch.

2. **`reconcile_from_ibkr_stubs()` wiring:** That function was added in an earlier
   patch to reconcile IBKR leg-level positions into spreads, but was never connected
   to the fills pipeline.  A future patch could wire it into `ccc_reconcile.py` so
   that any position present in a live IBKR snapshot but absent from the fills ledger
   triggers an automatic `ibkr_import` row — eliminating the need for the manual
   CLI import command.

3. **`ccc_report.compute_pending_count()` key mismatch:** That function looks for
   `event_type == "POSITION_OPENED"` in the fills ledger, but the actual key is
   `action == "POSITION_OPENED"`.  This means the pending count in Section B is
   inaccurate (never subtracts filled intents).  Not in scope for this patch but
   flagged for future hardening.
