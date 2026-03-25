# Interactive Daily Console

## Overview

The Interactive Daily Console (`scripts/daily.py`) provides a single command operator workflow for daily trading operations. It integrates v2 daily orchestration, candidate review, quote-only checking, and optional execution in one streamlined interface.

## Key Features

1. **No Strategy Changes**: Uses existing strategy math and candidate selection logic
2. **Interactive Selection**: Review candidates and select by rank (default: rank 1)
3. **Quote-Only First**: Always validates guards before offering execution options
4. **Explicit Confirmations**: Live transmission requires typing "SEND"
5. **No Silent No-Ops**: All errors exit with non-zero code and explicit messages
6. **Deterministic Intent IDs**: Consistent format: `<symbol>_<expiry>_<long>_<short>_<regime>_<timestamp>`
7. **Complete Receipt**: Final receipt includes intent_id, order_id, status, ledger paths

## Workflow

### Step 1: Daily Orchestration
Runs `run_daily_v2.py` to:
- Fetch or create IBKR snapshot
- Run regime structuring (default: crash)
- Generate `review_candidates.json` in run directory

### Step 2: Candidate Review
Displays compact table:
```
REGIME     | RANK | EXPIRY   | STRIKES    | EV/$   | P(Win) | DEBIT | MAX_GAIN
crash      |  1   | 20260402 | 580/560    | 23.16  | 70.6%  | $49   | $1951
```

### Step 3: Candidate Selection
- Default: auto-select rank=1 for eligible regime
- Interactive: user can choose different rank
- Exits with error if no candidates available or invalid rank

### Step 4: Quote-Only Check
- Builds OrderIntent from candidate
- Calls `execute_trade.py` with `--quote-only`
- Enforces all guards (executable legs, max debit, min DTE, etc.)
- Prints detailed ticket summary
- **If guards fail**: Exit with error (NO_TRADE)
- **If guards pass**: Proceed to execution options

### Step 5: Execution Options
After quote-only passes, offers three options:

**Option 1: Emit Intent Only**
- Writes intent JSON to `intents/` directory
- No order placed
- Useful for record-keeping or manual execution later

**Option 2: Stage Paper Order**
- Writes intent to `intents/`
- Stages order in paper trading account
- No transmission to exchange
- Returns order_id for tracking

**Option 3: Transmit Live Order**
- Requires typing "SEND" for confirmation
- Writes intent to `intents/`
- Transmits order to live exchange
- Writes OPEN entry to outcome ledgers
- Returns order_id and status

**Option 0: Exit**
- Exit without action

### Step 6: Final Receipt
Prints comprehensive receipt:
```
================================================================================
FINAL RECEIPT
================================================================================
Intent ID:    SPY_20260402_580_560_crash_20260224T095959
Order ID:     12345
Status:       LIVE_TRANSMITTED
Limit Price:  $49.00
Run ID:       crash_venture_v2_a54e721dd97bbbbc_20260224T095959
Run Dir:      runs/crash_venture_v2/crash_venture_v2_a54e721dd97bbbbc_20260224T095959
Intent Path:  intents/SPY_20260402_580_560_crash_20260224T095959.json
Ledger Paths:
  - runs/crash_venture_v2/.../artifacts/trade_outcomes.jsonl
  - runs/trade_outcomes.jsonl
================================================================================
```

## Usage

### Basic Usage
```powershell
python scripts/daily.py
```

This runs with defaults:
- Regime: crash
- Config: `configs/structuring_crash_venture_v2.yaml`
- IBKR port: 7496
- DTE range: 30-60 days
- Interactive prompts for regime and rank selection

### Auto-Select Options
```powershell
# Auto-select crash regime, rank 1 (no prompts)
python scripts/daily.py --auto-regime crash --auto-rank 1
```

### Custom Configuration
```powershell
# Use existing snapshot
python scripts/daily.py --snapshot snapshots/SPY_snapshot_20260224.json

# Different regime
python scripts/daily.py --regime selloff

# Custom DTE range
python scripts/daily.py --dte-min 45 --dte-max 90

# Custom IBKR connection
python scripts/daily.py --ibkr-host 127.0.0.1 --ibkr-port 7497
```

### Full Options
```
--regime REGIME              Regime to run (default: crash)
--config CONFIG              Campaign config path
--snapshot SNAPSHOT          Path to existing snapshot (optional)
--ibkr-host HOST             IBKR host (default: 127.0.0.1)
--ibkr-port PORT             IBKR port (default: 7496)
--dte-min DTE_MIN            Minimum DTE (default: 30)
--dte-max DTE_MAX            Maximum DTE (default: 60)
--min-debit MIN_DEBIT        Minimum debit per contract (default: 10.0)
--fallback-p FALLBACK_P      Fallback p_external (default: 0.30)
--auto-regime REGIME         Auto-select regime (skip interactive prompt)
--auto-rank RANK             Auto-select rank (default: 1)
--verbose                    Enable verbose logging
```

## Enforcement and Safety

### Intent Immutability (PR-EXEC-1)
- Execution uses ONLY intent fields (expiry, strikes, qty, limits)
- No re-derivation during execution
- Expiry must match resolved IBKR contract (single source of truth)
- **Violation**: Blocks execution with explicit error

### Price Band Clamping (PR-EXEC-2)
- Execution may tighten but never loosen limits
- Computed mid-price compared against intent limits
- **Violation**: BLOCKED_PRICE_DRIFT error

### Mode Invariants (PR-EXEC-4)
- Quote-only: Never stages or transmits
- Paper: Staging allowed, transmission forbidden
- Live + transmit: Requires `--confirm SEND`
- **Violation**: AssertionError with explicit message

### Ledger Enforcement (FIX A, FIX B)
**FIX A**: Single OPEN per intent/order
- Checks for existing OPEN entry with same intent_id
- **Violation**: LEDGER VIOLATION error, blocks duplicate OPEN

**FIX B**: Mandatory fields
- `intent_id`: Required for all entries
- `order_id`: Required (can be None if not yet assigned)
- **Violation**: Build-time error if missing

**FIX C**: Expiry single source of truth
- Uses resolved IBKR contract expiry, not candidate file expiry
- **Violation**: Blocks execution if mismatch detected

### Guard Enforcement
All guards from OrderIntent are enforced:
- `max_debit`: Maximum debit per contract
- `max_spread_width`: Maximum spread width as % of spot
- `require_executable_legs`: All legs must have bid/ask
- `min_dte`: Minimum days to expiration
- **Violation**: Explicit error, exits with code 1

## Intent ID Format

Intent IDs are **content hashes** computed deterministically from immutable intent fields:

**Hash Input**:
```
sha1(strategy|regime|symbol|expiry|legs|qty|limit_band|max_debit|guards_version)
```

**Result**: 40-character hex SHA1 hash

**File Naming**: Human-readable format with truncated hash:
```
<symbol>_<expiry>_<long>_<short>_<regime>_<hash[:8]>.json
```

Example:
```
File: SPY_20260402_580_560_crash_a3f4b2c1.json
Intent ID (in JSON): a3f4b2c1e5d7f8a9b0c1d2e34f5a6b7c8d9e0f12
Created TS (separate): 2026-02-24T14:59:59.123456Z
```

**Benefits**:
- **Truly deterministic**: Same parameters = same hash
- **Content-based**: Detects any change in immutable fields
- **Deduplicated**: Identical intents have identical IDs
- **Auditable**: Hash proves intent immutability
- **Readable files**: Filename shows key details + hash suffix

## Outcome Ledger Schema

When Option 3 (transmit live) is chosen, an OPEN entry is written to:
1. `<run_dir>/artifacts/trade_outcomes.jsonl` (per-run)
2. `runs/trade_outcomes.jsonl` (global)

Schema:
```json
{
  "candidate_id": "eeb8d765292a",
  "run_id": "crash_venture_v2_...",
  "regime": "crash",
  "entry_ts_utc": "2026-02-24T14:59:59.123456Z",
  "entry_price": 0.49,
  "qty": 1,
  "expiry": "20260402",
  "long_strike": 580.0,
  "short_strike": 560.0,
  "intent_id": "SPY_20260402_580_560_crash_20260224T095959",
  "order_id": "12345",
  "exit_ts_utc": null,
  "exit_price": null,
  "exit_reason": null,
  "pnl": null,
  "mfe": null,
  "mae": null,
  "status": "OPEN"
}
```

## Error Handling

### No Candidates Available
```
❌ NO CANDIDATES AVAILABLE - NO_TRADE
Exit code: 1
```

### Invalid Regime
```
❌ Regime 'invalid_regime' not available
Exit code: 1
```

### Invalid Rank
```
❌ No candidate with rank=99. Available ranks: [1, 2, 3]
Exit code: 1
```

### Guards Failed
```
❌ GUARDS FAILED: GUARD VIOLATION: Debit $55.00 exceeds max $50.00
Exit code: 1
```

### Live Confirmation Failed
```
❌ Confirmation failed - aborting
Exit code: 1
```

### Orchestration Failed
```
❌ Orchestration failed:
<stderr output>
Exit code: 1
```

## Testing

Run standalone tests:
```powershell
python test_daily_standalone.py
```

Tests cover:
1. ✅ No candidates -> NO_TRADE exit
2. ✅ Invalid schema -> explicit error
3. ✅ Auto-select rank 1 by default
4. ✅ User can select custom rank
5. ✅ Invalid rank -> exit with error
6. ✅ Quote-only pass -> enables execution options
7. ✅ Intent builder creates valid intents
8. ✅ Intent ID is deterministic
9. ✅ No silent no-ops (all errors are explicit)

## Integration with Existing Systems

### Phase 3 Decision Quality Loop
- Outcome ledger feeds into DQS (Decision Quality Score)
- Weekly PM reviews analyze trade outcomes
- P&L attribution tracked per candidate

### Phase 4 Structuring
- Uses v2 regime orchestration
- Respects candidate validation
- No changes to strategy math

### Phase 4b Execution Enforcement
- Enforces all PRs (PR-EXEC-1 through PR-EXEC-5)
- Intent immutability
- Price band clamping
- Mode invariants
- Ledger hooks

## Comparison to Run Scripts

### scripts/daily.py (THIS SCRIPT)
- **Purpose**: Interactive daily operator workflow
- **User**: Human operator
- **Mode**: Interactive prompts
- **Output**: Candidate table, interactive selection, final receipt
- **Execution**: Optional (emit intent, stage paper, transmit live)

### scripts/run_daily_v2.py
- **Purpose**: Batch orchestration only
- **User**: Automation / cron jobs
- **Mode**: Non-interactive
- **Output**: Artifacts in run_dir, automatic ledger writing
- **Execution**: No execution, only orchestration + intent emission via flags

### scripts/run_real_cycle.py
- **Purpose**: Full automated cycle (orchestration + execution)
- **User**: Automation / cron jobs
- **Mode**: Non-interactive
- **Output**: Full cycle with automatic execution
- **Execution**: Automatic based on config

## Best Practices

1. **Start with Quote-Only**: Always review the ticket summary before executing
2. **Test with Paper First**: Use Option 2 to stage paper orders before going live
3. **Verify Ledger Writes**: Check that outcome ledgers are written correctly
4. **Monitor Run Dirs**: Review artifacts in run directories for audit trails
5. **Use Auto-Flags for Automation**: In automated workflows, use `--auto-regime` and `--auto-rank`
6. **Keep Intents**: The `intents/` directory provides a complete audit trail of all decisions

## Troubleshooting

### IBKR Connection Failed
- Check TWS/Gateway is running
- Verify port (7496 for paper/live, 7497 for common alternative)
- Check API settings in TWS

### No Candidates Generated
- Check DTE range (may be too restrictive)
- Verify snapshot has option chains in range
- Check min_debit filter (may be too high)
- Review campaign config for regime parameters

### Guards Failing Unexpectedly
- Review ticket summary for specific guard violations
- Check if market prices have moved since orchestration
- Adjust limit bands if price drift is the issue
- Consider widening guard constraints in config

### Duplicate OPEN Error
- This is a LEDGER VIOLATION - check for existing OPEN with same intent_id
- Indicates order may have already been transmitted
- Do NOT override - investigate the duplicate first

## Future Enhancements

Potential future additions:
- Multi-regime selection (select from both crash and selloff)
- Bracket orders (take-profit + stop-loss)
- Position sizing based on portfolio
- Risk limits across all open positions
- Real-time P&L monitoring
- Slack/email notifications on fills
