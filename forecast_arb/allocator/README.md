# CCC v1 Allocator — Operator Guide

## Overview

The CCC (Convexity-Confidence-Crash) v1 allocator manages open positions and generates trading intent files for SPY/QQQ vertical put debits. It enforces daily/weekly/monthly budget caps, inventory targets, and harvest rules.

**Single authority**: when `--policy` is passed to `daily.py`, the allocator is the **only** generator of `OPEN` intents. Campaign mode produces selection candidates but does NOT emit intent files.

---

## One-Line Daily Workflow

```powershell
# Full daily pipeline: campaign selection → allocator planning → paper execute (quote-only preview)
python scripts/daily.py --campaign configs/campaign_v1.yaml --policy configs/allocator_ccc_v1.yaml --execute --paper --quote-only

# Full daily pipeline: campaign selection → allocator planning → paper commit (writes to commit ledger)
python scripts/daily.py --campaign configs/campaign_v1.yaml --policy configs/allocator_ccc_v1.yaml --execute --paper

# Live commit (writes live commit records; IBKR transmission is a manual follow-up step)
# Requires typing SEND at the confirmation prompt
python scripts/daily.py --campaign configs/campaign_v1.yaml --policy configs/allocator_ccc_v1.yaml --execute --live
```

### Flags

| Flag | Purpose |
|------|---------|
| `--campaign <path>` | Campaign config (e.g. `configs/campaign_v1.yaml`) |
| `--policy <path>` | Allocator policy config (e.g. `configs/allocator_ccc_v1.yaml`) |
| `--execute` | Run CCC execute after planning. Requires `--paper` or `--live`. |
| `--paper` | Paper mode: write commit records, no IBKR transmission |
| `--live` | Live mode: write live commit records; IBKR transmission is a separate step; requires typing `SEND` |
| `--quote-only` | Preview intents only — commit ledger NOT updated (valid with `--execute`) |
| `--verbose` | Enable verbose logging |

---

## Two-Step Workflow (Still Supported)

```powershell
# Step 1: Run daily planning only (no commit, no spend increase)
python scripts/daily.py --campaign configs/campaign_v1.yaml --policy configs/allocator_ccc_v1.yaml

# Step 2a: Preview intents without committing
python scripts/ccc_execute.py --actions runs\allocator\allocator_actions.json --paper --quote-only

# Step 2b: Stage intents (writes to commit ledger → counts toward spent_today_before)
python scripts/ccc_execute.py --actions runs\allocator\allocator_actions.json --paper

# Step 2c: Live mode (requires SEND)
python scripts/ccc_execute.py --actions runs\allocator\allocator_actions.json --live
```

---

## Ledger Architecture

| File | Writer | Purpose |
|------|--------|---------|
| `runs/allocator/allocator_plan_ledger.jsonl` | `plan.py` (daily planning) | Plan records: OPEN, HARVEST_CLOSE, ROLL_CLOSE, DAILY_SUMMARY |
| `runs/allocator/allocator_commit_ledger.jsonl` | `ccc_execute.py` ONLY | Committed spend records. Budget reads from here. |
| `runs/allocator/allocator_actions.json` | `plan.py` (overwritten each run) | Current plan actions + intent paths |
| `intents/allocator/OPEN_<candidate_id>.json` | `plan.py` | Executable OrderIntent files (pass `validate_order_intent()`) |

**Key invariant:** Running `daily.py` multiple times in a day does NOT increase `spent_today_before`. Only `ccc_execute.py` (or `--execute` in `daily.py`) writes to the commit ledger.

---

## Commit Ledger Schema (v1.5)

Every record written to `allocator_commit_ledger.jsonl` is canonical:

```json
{
  "date": "2026-03-02",
  "timestamp_utc": "2026-03-02T14:00:00.000000+00:00",
  "action": "OPEN",
  "policy_id": "ccc_v1",
  "intent_id": "<sha1>",
  "candidate_id": "SPY_crash_20260402_585_565",
  "run_id": null,
  "candidate_rank": 1,
  "regime": "crash",
  "underlier": "SPY",
  "expiry": "20260402",
  "strikes": [585.0, 565.0],
  "qty": 1,
  "premium_per_contract": 36.0,
  "premium_spent": 36.0,
  "reason_codes": ["EV_PER_DOLLAR:0.30"],
  "intent_path": "intents/allocator/OPEN_SPY_crash_20260402_585_565.json",
  "mode": "paper"
}
```

Rules:
- `date` = local date (America/New_York)
- `strikes` = always a 2-element list `[long_put, short_put]`, never a dict
- Missing hard-required fields → `ValueError` raised, partial records never written
- Idempotent: same `intent_id` can only appear once in the ledger

---

## Budget Behavior

| Scenario | `spent_today_before` |
|----------|---------------------|
| Plan N times, no execute | 0 (no change) |
| Plan + `--quote-only` | 0 (no change) |
| Plan + `--paper` (first run) | += premium_spent |
| Plan + `--paper` (same intent, second run) | unchanged (dedup) |
| Legacy v1.4 commit rows (no `action` key) | skipped with warning, 0 counted |

---

## One-Time Ledger Cleanup

If the plan ledger has legacy OPEN rows missing `underlier`/`expiry`/`strikes`/`regime`:

```powershell
# Preview what would be dropped (no files written)
python scripts/ccc_ledger_sanitize.py --dry-run

# Write sanitized output to allocator_plan_ledger.sanitized.jsonl
python scripts/ccc_ledger_sanitize.py

# Apply (after reviewing sanitized file):
copy runs\allocator\allocator_plan_ledger.sanitized.jsonl runs\allocator\allocator_plan_ledger.jsonl
```

---

## File Index

```
forecast_arb/allocator/
  __init__.py          — Package init
  budget.py            — compute_budget_state() — reads commit ledger
  harvest.py           — generate_harvest_actions()
  inventory.py         — compute_inventory_state()
  marks.py             — populate_marks_from_candidates()
  open_plan.py         — generate_open_actions()
  plan.py              — run_allocator_plan() — main orchestrator
  policy.py            — load_policy(), ledger path helpers
  reconcile.py         — reconcile_positions()
  types.py             — BudgetState, InventoryState, AllocatorAction, AllocatorPlan

scripts/
  daily.py             — One-line daily entrypoint (--execute --paper --quote-only)
  ccc_execute.py       — Execute intents, write commit ledger (also callable as API)
  ccc_ledger_sanitize.py — One-time plan ledger cleanup helper

configs/
  allocator_ccc_v1.yaml — Policy config (budgets, thresholds, harvest, sizing)
  campaign_v1.yaml      — Campaign grid config (underliers, regimes, governors)
```

---

## Policy Config Reference (`allocator_ccc_v1.yaml`)

```yaml
policy_id: ccc_v1
budgets:
  monthly_baseline: 500
  monthly_max: 800
  weekly_baseline: 150
  daily_baseline: 50
  weekly_kicker: 300
  daily_kicker: 100
inventory_targets:
  crash: 1
  selloff: 1
thresholds:
  crash:
    ev_per_dollar_implied: 0.20
    ev_per_dollar_external: 0.15
    convexity_multiple: 2.5
  selloff:
    ev_per_dollar_implied: 0.15
    ev_per_dollar_external: 0.12
    convexity_multiple: 2.0
harvest:
  partial_close_multiple: 3.0
  full_close_multiple: 5.0
  time_stop_dte: 14
  time_stop_min_multiple: 1.0
  partial_close_fraction: 0.5
sizing:
  max_qty_per_trade: 1
kicker:
  min_conditioning_confidence: 0.66
  max_vix_percentile: 35.0
ledger_dir: runs/allocator
output_dir: runs/allocator
intents_dir: intents/allocator
```

---

## Acceptance Tests

Run the Patch Pack v1.5 test suite:

```powershell
python -m pytest tests/test_patch_v15.py -v
```

Expected: all 30+ tests pass.
