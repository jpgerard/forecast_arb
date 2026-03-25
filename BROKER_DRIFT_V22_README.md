# CCC v2.2 — Broker-State Drift Check

**Safety / visibility patch — detection only, no auto-repair.**

---

## What Is Broker Drift?

**Broker drift** means that CCC's internal state (`positions.json`) disagrees with what
the IBKR broker actually holds.

CCC tracks open positions in a fills ledger and `positions.json`.  Those records are
authoritative for all CCC decisions (inventory counts, premium-at-risk, gating).
But IBKR can hold spreads that CCC does not know about (e.g. manually entered,
pre-CCC legacy trades, or trades from a previous system) — or CCC can show spreads
that have since expired or been closed in IBKR.

When drift exists, the CCC report will show:

- **Wrong open crash count** — e.g. `crash_open=1` but IBKR holds 3 spreads
- **Wrong premium-at-risk** — underestimated because missing spreads have no debit
- **Misleading actionability** — allocator may plan new opens when capacity is already full

Drift does NOT mean a trade was made incorrectly.  It means the operator needs to
reconcile before trusting the summary.

---

## Why Summaries May Be Stale

`positions.json` is built **exclusively** from `allocator_fills_ledger.jsonl` rows.
Fills rows are only written when CCC's own pipeline processes an `execution_result.json`.

Spreads that bypass the CCC execution pipeline (TWS manual entry, legacy import,
or positions carried over from before CCC was set up) produce no fills-ledger row.

The broker drift check compares `positions.json` against a **fresh IBKR export CSV**
to surface this gap.

---

## How to Run

### Status check with drift detection

```powershell
python scripts/trading_adapter.py status --broker-csv /path/to/ibkr_positions.csv
python scripts/trading_adapter.py status --broker-csv /path/to/ibkr_positions.csv --json
```

### Summarize with drift detection

```powershell
python scripts/trading_adapter.py summarize --broker-csv /path/to/ibkr_positions.csv
python scripts/trading_adapter.py summarize --broker-csv /path/to/ibkr_positions.csv --no-preview --json
```

### Direct Python API

```python
from forecast_arb.allocator.broker_drift import check_broker_drift

result = check_broker_drift(
    positions_path="runs/allocator/positions.json",
    csv_path="/path/to/ibkr_positions.csv",
)

print(result["in_sync"])        # True / False
print(result["headline"])       # human-readable summary
print(result["only_in_ccc"])    # spreads in CCC not in IBKR
print(result["only_in_ibkr"])   # spreads in IBKR not in CCC
print(result["qty_mismatches"]) # qty discrepancies
```

---

## How to Obtain the IBKR Positions CSV

**From TWS (Trader Workstation):**
1. Account → Portfolio → Export (top-right) → CSV
2. Or: Reports → Account Reports → Portfolio Analyst → Export

**From IBKR Client Portal:**
1. Portfolio → … → Export → CSV

**From Activity Statement:**
1. Reports → Activity → Statements → Run
2. Sections: Positions
3. Format: CSV

The module handles both the **Activity Statement** (multi-section with
`Positions,Header,...` / `Positions,Data,...` rows) and a **simple flat CSV**
(header on first row) formats automatically.

---

## What the Output Looks Like

### In sync

```
Status          : ✓ IN SYNC
CCC positions   : 3
IBKR positions  : 3
Headline        : CCC state is in sync with broker: 3 spread(s) matched.
```

### Drift detected (CCC=3, IBKR=2)

```
Status          : ⚠ DRIFT DETECTED
CCC positions   : 3
IBKR positions  : 2
Only in CCC     : 1 spread(s)
  - SPY 20260320 590/570
Headline        : CCC state is stale: 1 spread exists only in CCC (not in IBKR export).
                  CCC shows 3 crash spread(s), broker export shows 2.
                  Refresh sync before trusting summary.
```

### JSON output (--json)

```json
{
  "ok": true,
  "actionability": "REVIEW_ONLY",
  "headline": "Broker drift detected: ...",
  "details": {
    "crash_open": 3,
    "broker_drift": {
      "ok": true,
      "in_sync": false,
      "ccc_count": 3,
      "ibkr_count": 2,
      "only_in_ccc": [
        {"symbol": "SPY", "expiry": "20260320", "long_strike": 590.0, "short_strike": 570.0, "qty": 1, "key": "SPY 20260320 590/570"}
      ],
      "only_in_ibkr": [],
      "qty_mismatches": [],
      "headline": "CCC state is stale: ...",
      "errors": []
    },
    "in_sync": false,
    "only_in_ccc": [...],
    "only_in_ibkr": [],
    "qty_mismatches": []
  }
}
```

---

## Supported CSV Formats

| Format | Description | Example |
|--------|-------------|---------|
| Activity Statement | IBKR multi-section CSV with `Positions,Header,…` / `Positions,Data,…` rows | Full account activity export |
| Simple CSV | Plain flat CSV with header on first row | Manual position export |

**Supported option symbol formats:**

| Format | Example | Notes |
|--------|---------|-------|
| Alpha-month (TWS default) | `SPY 17APR26 590 P` | 2-digit or 4-digit year |
| Numeric date (CCC-style) | `SPY 20260417 590.0 P` | YYYYMMDD |
| OCC-style | `SPY260417P590` | 6-digit date + C/P + strike |

**Equity and unrelated rows** are silently ignored (no crash).

---

## Behavior on Drift

When `--broker-csv` is supplied and drift is detected:

1. **`details.broker_drift`** is populated with the full diff result
2. **`details.in_sync`** is set to `False`
3. **Actionability** is degraded to at least `REVIEW_ONLY`
4. **Headline** is prefixed with a clear warning:
   > `Broker drift detected: CCC shows 3 crash spread(s) but broker export shows 2. Refresh sync before trusting summary.`
5. Drift headline is added to the `errors` list for agent consumption

**No automatic repair** — no positions are added, deleted, or modified.

---

## How to Fix Drift

If drift is confirmed, use the existing `ccc_import_ibkr_positions.py` workflow to
reconcile:

```powershell
# Dry-run preview
python scripts/ccc_import_ibkr_positions.py --dry-run --mode live `
    --spread SPY 20260417 590 570 1

# Live import (idempotent)
python scripts/ccc_import_ibkr_positions.py --mode live `
    --spread SPY 20260417 590 570 1

# Verify
python scripts/ccc_report.py
python scripts/trading_adapter.py status --broker-csv /path/to/ibkr.csv
```

See `IBKR_BROKER_STATE_SYNC_PATCH.md` for full sync workflow.

---

## Non-Negotiable Invariants (v2.2)

- **Read-only** — no file writes, no side effects
- **No automatic repair** — operator must explicitly invoke import pipeline
- **Allocator decisions unchanged** — drift check is detection + warning only
- **Backward-compatible** — all existing CLI commands work without `--broker-csv`
- **No external dependencies** — pure Python, no IBKR API required

---

## Module Location

```
forecast_arb/allocator/broker_drift.py     — pure detection functions
forecast_arb/adapter/trading_adapter.py   — adapter integration (status_snapshot, summarize_latest)
scripts/trading_adapter.py                — CLI wrapper (--broker-csv flag)
tests/test_broker_drift_v22.py            — 65 deterministic tests
```

---

*CCC v2.2 — 2026-03-23*
