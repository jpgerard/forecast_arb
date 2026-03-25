# CCC Daily Workflow Guide — v1.9

## One-Command Daily Operator Workflow

### Full paper commit + reconcile (standard daily command)

```powershell
python scripts/daily.py `
  --campaign configs/campaign_v1.yaml `
  --policy configs/allocator_ccc_v1.yaml `
  --execute --paper --reconcile
```

This single command:
1. **Campaign**: runs grid runner + selector → writes `runs/allocator/recommended.json`
2. **Allocator plan**: reads candidates, computes ACTUAL+PENDING inventory, writes `allocator_actions.json` + OPEN intents
3. **Execute (paper)**: commits intents from `allocator_actions.json` to `allocator_commit_ledger.jsonl` (dedup by intent_id)
4. **Reconcile**: reads `execution_result.json` (if present), ingests fills into `allocator_fills_ledger.jsonl`, rebuilds `positions.json`, archives OPEN intents

---

## Inventory Concepts (v1.8)

### Three inventory layers

| Layer | Definition | Source | Used For |
|-------|-----------|--------|---------|
| **ACTUAL** | Real open positions (confirmed filled) | `positions.json` → fills ledger → plan ledger (fallback) | Harvest/multiple logic only |
| **PENDING** | Committed-not-yet-filled (in commit ledger, not in fills ledger as `POSITION_OPENED`) | `commit_ledger − fills_ledger` | Gates duplicate OPEN plans |
| **EFFECTIVE** | `actual + pending` | Computed | OPEN action gating decisions |

### Why 3 layers?

- **ACTUAL** reflects real P&L exposure (harvest multiple, position management).
- **PENDING** blocks duplicate opens *reliably* without depending on file timestamps or OPEN_*.json scans.
- **EFFECTIVE** is what the allocator uses to decide "do I need to open another position?" — so a committed-but-not-yet-filled crash position correctly blocks a second crash open.

### Pending definition (durable, ledger-based)

```
pending_intent_ids = commit_ledger.intent_ids − fills_ledger[POSITION_OPENED].intent_ids
```

Key invariants:
- `ORDER_STAGED` rows (paper staging, `transmit=False`) do **NOT** remove from pending — they remain pending until a `POSITION_OPENED` row appears.
- A `POSITION_OPENED` row **does** remove from pending AND creates a `positions.json` entry.
- Pending detection does **NOT** depend on filesystem timestamps, file modification dates, or OPEN_*.json file scans.

---

## Console Output (v1.8)

The allocator console box shows three inventory rows:

```
║  INVENTORY ACTUAL  crash=0/1  selloff=0/1                           ║
║  PENDING (committed-not-filled)  crash=1  selloff=0                 ║
║  INVENTORY EFFECTIVE (gating)  crash=1/1  selloff=0/1               ║
```

- **ACTUAL** = filled positions from `positions.json`
- **PENDING** = only shown when `pending > 0`
- **EFFECTIVE** = only shown when `pending > 0` (actual + pending vs target)

When `pending = 0`, only ACTUAL and PLANNED are shown.

---

## `allocator_actions.json` Schema (v1.8)

The `inventory` block now contains 4 keys:

```json
"inventory": {
  "actual":    { "crash": {"open": 0, "target": 1}, "selloff": {"open": 0, "target": 1} },
  "planned":   { "crash": {"open": 1, "target": 1}, "selloff": {"open": 0, "target": 1} },
  "pending":   { "crash": 1, "selloff": 0 },
  "effective": { "crash": {"open": 1, "target": 1}, "selloff": {"open": 0, "target": 1} }
}
```

- `actual` = real filled positions (= `before` for backward-compat)
- `planned` = after today's proposed actions (= `after` for backward-compat)  
- `pending` = committed-not-filled counts by regime (**NEW v1.8**)
- `effective` = actual + pending (used for gating) (**NEW v1.8**)

---

## DAILY RUN SUMMARY

At the end of every run, a summary box is printed:

```
╔══════════════════════════════════════════════════════════════════╗
║  DAILY RUN SUMMARY (CCC v1.8)                                    ║
╠──────────────────────────────────────────────────────────────────╣
║  CANDIDATES FILE: runs/.../recommended.json (seen 2)             ║
║  CCC PLAN: planned_opens=1 planned_closes=0 holds=0              ║
║  INVENTORY ACTUAL: crash=0 selloff=0                             ║
║  PENDING (committed-not-filled): crash=1 selloff=0               ║
║  EFFECTIVE (gating): crash=1 selloff=0                           ║
║  CCC EXECUTE: mode=paper quote_only=false committed_new=1 committed_skipped=0 ║
║  Commit ledger: runs/allocator/allocator_commit_ledger.jsonl     ║
║  RECONCILE: positions_opened=0 dedup=0                           ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## Ledger File Semantics (v1.8)

| File | Purpose | Written by |
|------|---------|-----------|
| `runs/allocator/allocator_plan_ledger.jsonl` | Plan-only OPEN records (not budget-relevant) | `plan.py` |
| `runs/allocator/allocator_commit_ledger.jsonl` | **Budget-authoritative** committed intents | `ccc_execute.py` ONLY |
| `runs/allocator/allocator_fills_ledger.jsonl` | Fill receipts (`POSITION_OPENED` + `ORDER_STAGED`) | `ccc_reconcile.py` |
| `runs/allocator/positions.json` | Snapshot of open positions (rebuilt each reconcile) | `ccc_reconcile.py` |
| `intents/allocator/_archive/YYYYMMDD/` | Archived OPEN_*.json + execution_result after fill | `ccc_reconcile.py` |

### Fill ledger row types

| `action` | Meaning | Creates position? | In pending? |
|----------|---------|------------------|------------|
| `POSITION_OPENED` | Order confirmed filled | ✅ Yes | ❌ Removed |
| `ORDER_STAGED` | Paper-staged (transmit=False), not yet filled | ❌ No | ✅ Still pending |

---

## Inventory Authority Priority (v1.8)

1. **`positions.json`** (from fills ledger reconcile) — most authoritative for actual positions
2. **Plan ledger reconcile** — fallback if positions.json missing (pre-v1.7 backward compat)
3. **Empty state** (crash_open=0) — if both above are missing

---

## Common Commands

### Preview (safe, no writes)
```powershell
python scripts/daily.py `
  --campaign configs/campaign_v1.yaml `
  --policy configs/allocator_ccc_v1.yaml `
  --execute --paper --quote-only
```

### Full paper run (the one daily command)
```powershell
python scripts/daily.py `
  --campaign configs/campaign_v1.yaml `
  --policy configs/allocator_ccc_v1.yaml `
  --execute --paper --reconcile
```

### Standalone reconcile (after a fill is confirmed)
```powershell
python scripts/ccc_reconcile.py --paper
python scripts/ccc_reconcile.py --live
```

### Dry-run reconcile (inspect only)
```powershell
python scripts/ccc_reconcile.py --paper --dry-run
```

### Inspect pending state
```python
from pathlib import Path
from forecast_arb.allocator.pending import load_pending_counts

counts = load_pending_counts(
    commit_ledger_path=Path("runs/allocator/allocator_commit_ledger.jsonl"),
    fills_ledger_path=Path("runs/allocator/allocator_fills_ledger.jsonl"),
)
print(counts)  # {"crash": 1, "selloff": 0}
```

---

## Idempotency

- Running `daily.py --execute --paper --reconcile` **multiple times** is safe:
  - **Execute**: `run_execute` deduplicates by `intent_id` → second run has `committed_new=0`
  - **Reconcile**: `run_reconcile` deduplicates by `intent_id` → second run has `positions_opened=0, dedup=1`
- The DAILY RUN SUMMARY will clearly show `committed_new=0` and `committed_skipped=1` on reruns.

---

## Diagnosing Issues

### "Why is inventory showing EFFECTIVE > ACTUAL?"
→ There are committed-but-not-filled intents in the commit ledger.  
→ Check: `runs/allocator/allocator_commit_ledger.jsonl` for recent OPEN rows.  
→ After a fill is confirmed: run `python scripts/ccc_reconcile.py --paper` to move from PENDING to a POSITION_OPENED entry.

### "Why is crash HOLD when target=1 and I see no open positions?"
→ There is a PENDING intent (committed but not filled).  
→ The allocator correctly blocks a second OPEN when effective=target.  
→ This is correct behavior. Wait for the fill, then reconcile.

### "How do I force-clear a stale pending entry?"
→ If a committed intent was never filled and you want to remove it from the pending set:  
→ Write a `POSITION_OPENED` or `ORDER_STAGED` entry for the intent_id to the fills ledger (and update positions.json).  
→ Or: manually delete the commit ledger row (use `scripts/ccc_ledger_sanitize.py`).

---

## Migration Notes

| Version | Change |
|---------|--------|
| v1.7 | Added `positions.json`, fills ledger (`POSITION_OPENED`). Inventory.actual uses positions.json if present. |
| v1.8 | Added **pending** (commit − fills) as durable ledger-based source. Added `ORDER_STAGED` rows (staged-not-filled). `inventory.pending` and `inventory.effective` added to JSON + console. `_scan_pending_open_intents()` (filesystem timestamp scan) replaced by ledger-based computation. |
| v1.9 | Added worst-case debit gating (Task A), crash ladder (Task B), roll discipline (Task C), fragility gating (Task E). `allocator_actions.json` now carries `pricing`, `layer`, `fragile` on OPEN actions. Console shows premium_used source [WC/MID/CAMPAIGN], ladder layer, and fragility status. |

---

## v1.9 Patch: Max Value / Low Complexity

### Task A — Worst-case Debit Gating (`PREMIUM_USED`)

When IBKR leg quotes are available (`long_ask`, `short_bid`) in the candidate dict:

- **`premium_wc`** = `(long_ask - short_bid) * 100`  ← worst-case execution debit
- **`premium_mid`** = `(long_mid - short_mid) * 100`  ← mid-market debit
- EV/$ is **recomputed** using `premium_wc` (or `premium_mid`) when quotes are present

Every OPEN action carries a `PREMIUM_USED:WC`, `PREMIUM_USED:MID`, or `PREMIUM_USED:CAMPAIGN` reason code. When WC or MID premium is used, the `action.pricing` dict in `allocator_actions.json` contains all pricing details.

**Interpreting the console OPEN line:**
```
║  OPEN  SPY_20260402_560_540  qty=1  $65/c[WC]  EV=1.80  conv=28.5x layer=A robust ║
```
- `[WC]` = worst-case (ask/bid) premium used for gating — **most conservative**
- `[MID]` = mid-market pricing
- `[CAMPAIGN]` = campaign-computed premium (no live quotes available)

**When OPEN flips to HOLD due to WC pricing:**  
→ The spread between long_ask and short_bid is wide enough that EV/$ with actual execution costs drops below the policy threshold.  
→ This prevents buying at the asking price systematically.

---

### Task B — Crash Ladder (Layer A / Layer B)

Crash candidates are classified by OTM moneyness relative to current spot:

| Layer | OTM band | Preference |
|-------|----------|-----------|
| **A** | 5–9% OTM | **Preferred when inv=0** (moderate crash, higher activation probability) |
| **B** | 10–16% OTM | Fallback when Layer A absent or inv=1 (deep crash, higher max gain) |
| None | Outside bands | No layer preference |

**When inv=0 (fill mode):** Layer A candidate is sorted first even if Layer B has slightly higher raw EV/$.  
**When inv≥1:** Layer preference is ignored; candidates sorted by EV/$ descending.

Console shows: `layer=A` or `layer=B` on the OPEN action line.

**Config (`configs/allocator_ccc_v1.yaml`):**
```yaml
thresholds:
  crash:
    ladder:
      layer_a:
        moneyness_min_pct: 5.0
        moneyness_max_pct: 9.0
      layer_b:
        moneyness_min_pct: 10.0
        moneyness_max_pct: 16.0
```

Requires `spot` (or `spot_price`) field in the candidate dict for layer classification. Without it, `layer=None` and no preference is applied.

---

### Task C — Roll-Forward Discipline

Separate from the harvest time-stop. The `roll` policy section triggers a `ROLL_CLOSE` when a position is **within DTE window AND has lost convexity**:

| Criterion | Trigger | Reason Code |
|-----------|---------|-------------|
| DTE ≤ `dte_max_for_roll` | Always required | `ROLL_DTE` |
| `mark / entry_debit < min_multiple_to_hold` | Yes | `ROLL_MULTIPLE` |
| `max_gain / mark_mid < min_convexity_multiple_to_hold` | Yes | `ROLL_CONVEXITY` |

**Key behavior:** A `ROLL_CLOSE` frees its inventory slot for a **replacement OPEN** in the same regime within the same plan run — even if there are pending (committed-not-filled) intents. The replacement OPEN prefers Layer A (if configured).

**If liquidity guard blocks the close** (`WIDE_MARKET_NO_CLOSE`):  
→ The action is downgraded to `HOLD`.  
→ **No replacement OPEN is planned** (orphan open is blocked).

**Config (`configs/allocator_ccc_v1.yaml`):**
```yaml
roll:
  enabled: true
  dte_max_for_roll: 21                   # DTE window for roll
  min_multiple_to_hold: 1.10             # roll if mark < 1.10x entry
  min_convexity_multiple_to_hold: 8.0    # roll if max_gain/mark < 8x
```

Set `enabled: false` to disable roll discipline entirely (backward compat).

---

### Task E — Fragility Gating

Before allowing any OPEN, stress-test the trade under adverse market shocks:
- **p_shock** = `p_used − p_downshift_pp/100` (shift probability down)
- **premium_shock** = `premium_used × (1 + debit_upshift_pct/100)` (inflate debit)
- **EV_shock** = `p_shock × max_gain − (1 − p_shock) × premium_shock`

If `EV_shock ≤ 0`: the position is **fragile** (would have negative EV under modest adverse scenarios).

| Condition | Result |
|-----------|--------|
| `inv > 0` and fragile | **HOLD** with reason `EV_FRAGILE_UNDER_SHOCKS` |
| `inv = 0` and fragile and `allow_if_inventory_empty: true` | **ALLOW** with tag `FRAGILE_ALLOWED_EMPTY` ← still opens to build inventory, but tagged |
| Not fragile | **ALLOW** with tag `EV_ROBUST` |

When fragile is allowed at empty inventory, `action.fragile = True` appears in `allocator_actions.json`.

**Config (`configs/allocator_ccc_v1.yaml`):**
```yaml
robustness:
  enabled: true
  p_downshift_pp: 3.0          # shift p down by 3 percentage points
  debit_upshift_pct: 10.0      # inflate premium by 10%
  require_positive_ev_under_shocks: true
  allow_if_inventory_empty: true   # still open when inv=0 even if fragile
```

Set `enabled: false` to disable fragility gating (backward compat).

---

### v1.9 `allocator_actions.json` — OPEN action schema additions

```json
{
  "type": "OPEN",
  "reason_codes": [
    "PREMIUM_USED:WC",
    "PREMIUM_WC_PER_CONTRACT:65.00",
    "PREMIUM_MID_PER_CONTRACT:60.00",
    "LADDER_LAYER:A",
    "MONEYNESS_PCT:6.9",
    "EV_ROBUST"
  ],
  "pricing": {
    "premium_used": 65.0,
    "premium_used_source": "WC",
    "premium_wc": 65.0,
    "premium_mid": 60.0,
    "debit_wc_share": 0.65,
    "debit_mid_share": 0.60
  },
  "layer": "A",
  "fragile": false
}
```

---

### v1.9 Diagnosing Issues

**"Why did OPEN become HOLD after v1.9 upgrade?"**  
→ Could be worst-case pricing: if `long_ask - short_bid` is wide, `premium_wc > premium_mid`.  
→ Check `allocator_actions.json` → `open_gate_trace` → `candidates_evaluated[0].reason`.  
→ If `EV_BELOW_THRESHOLD`, the WC cost is too high to justify the position under policy thresholds.

**"Why does OPEN show `[CAMPAIGN]` instead of `[WC]`?"**  
→ No live leg quotes in the candidate dict (candidate lacks `long_ask`, `short_bid` fields).  
→ Campaign-computed premium is used as fallback. This is correct behavior when quotes are unavailable.

**"Why was a ROLL_CLOSE generated even though no replacement OPEN happened?"**  
→ The replacement OPEN failed policy gates (EV below threshold, convexity too low, etc.).  
→ The ROLL_CLOSE still happens; the gate trace in `allocator_actions.json` explains why OPEN was not approved.

**"Position shows `fragile: true` in actions.json — should I worry?"**  
→ `fragile: true` with `FRAGILE_ALLOWED_EMPTY` means: the position passed EV gating but FAILS the stress test under -3pp probability + +10% debit.  
→ This is allowed when inventory is empty (fill mode) to maintain convexity coverage.  
→ Once inventory is filled (inv=1), a second fragile position would be blocked.
