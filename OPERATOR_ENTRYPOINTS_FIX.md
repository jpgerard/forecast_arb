# Operator Entrypoints Fix - Campaign Mode Interactive Execution

**Date:** 2026-02-25  
**Status:** ✅ COMPLETE

## Problem Statement

Campaign mode in `daily.py` was printing recommendations but exiting before offering interactive execution menus, creating confusion where the operator saw output that looked like `run_daily_v2.py` batch mode and then nothing happened.

### Root Issues

1. **Entrypoint Confusion**: Both `daily.py` and `run_daily_v2.py` had similar-looking output, making it unclear which was the interactive operator console vs batch runner
2. **Early Exit**: Campaign mode ran grid+selector, printed recommended candidates, then exited without interactive execution
3. **Noisy Logging**: Batch runner internals polluted operator console output
4. **No Interactive Flow**: Selected candidates never went through quote-only → interactive menu flow

## Solution Overview

### A) Entrypoint Cleanup

**`scripts/daily.py`** - Operator Console
```python
print("=" * 80)
print("OPERATOR CONSOLE: daily.py")
print(f"Mode: {'CAMPAIGN' if args.campaign else 'SINGLE-REGIME'}")
print(f"Config: {args.campaign or args.config}")
print("=" * 80)
```

**`scripts/run_daily_v2.py`** - Batch Runner  
```python
print("=" * 80)
print("BATCH RUNNER: run_daily_v2.py (no execution prompts)")
print("=" * 80)
```

**Logging Suppression** (in `daily.py`)
```python
def setup_logging(verbose: bool = False):
    # Suppress noisy batch runner internals unless verbose
    if not verbose:
        logging.getLogger("forecast_arb").setLevel(logging.ERROR)
        logging.getLogger("__main__").setLevel(logging.WARNING)
```

### B) Campaign Mode Interactive Execution

The fix adds a complete interactive execution loop after campaign selection:

```python
if args.campaign:
    # Run campaign mode: grid + selector
    result = run_campaign_mode(...)
    
    # Check for NO_TRADE case
    if result['selected_count'] == 0:
        print("NO_TRADE - 0 candidates selected")
        sys.exit(0)
    
    # INTERACTIVE EXECUTION FOR EACH SELECTED CANDIDATE
    print(f"INTERACTIVE EXECUTION - {result['selected_count']} SELECTED CANDIDATE(S)")
    
    selected = result.get("selected", [])
    receipts = []
    
    for i, candidate in enumerate(selected, 1):
        # 1. Print candidate summary
        print(f"CANDIDATE {i} of {result['selected_count']}")
        print(f"Underlier: {candidate.get('underlier')}")
        print(f"Regime: {candidate.get('regime')}")
        print(f"Strikes: {candidate.get('long_strike')}/{candidate.get('short_strike')}")
        
        # 2. Build intent using intent_builder
        from forecast_arb.execution.intent_builder import build_order_intent, emit_intent
        
        candidate_for_builder = {
            "underlier": candidate.get("underlier"),
            "expiry": candidate.get("expiry"),
            "strikes": {
                "long_put": candidate.get("long_strike"),
                "short_put": candidate.get("short_strike")
            },
            "debit_per_contract": candidate.get("computed_premium_usd", 0),
            "rank": 1
        }
        
        intent = build_order_intent(candidate_for_builder, regime=candidate.get("regime"))
        intent_path = emit_intent(intent, output_dir="intents")
        
        # 3. Run quote-only check
        cmd = [
            sys.executable,
            "-m", "forecast_arb.execution.execute_trade",
            "--intent", intent_path,
            "--paper",
            "--quote-only",
            "--host", args.ibkr_host,
            "--port", str(args.ibkr_port)
        ]
        
        exec_result_proc = subprocess.run(cmd, capture_output=True, text=True)
        print(exec_result_proc.stdout)  # Ticket summary
        
        if exec_result_proc.returncode != 0:
            print(f"⚠️  Candidate {i} BLOCKED - skipping to next")
            continue
        
        # 4. Load execution result
        result_path = Path(intent_path).parent / "execution_result.json"
        with open(result_path, "r") as f:
            exec_result = json.load(f)
        
        guards_passed = exec_result.get("guards_passed", False)
        
        if not guards_passed:
            print(f"⚠️  Candidate {i} BLOCKED - skipping to next")
            continue
        
        # 5. Offer interactive menu (same as single-mode)
        receipt = offer_execution_options(
            intent_path=intent_path,
            run_dir=f"runs/campaign/{result['campaign_run_id']}",
            exec_result=exec_result,
            ibkr_host=args.ibkr_host,
            ibkr_port=args.ibkr_port
        )
        
        receipts.append(receipt)
    
    # Print final summary
    print("CAMPAIGN EXECUTION COMPLETE")
    for i, receipt in enumerate(receipts, 1):
        print(f"  {i}. {receipt.get('status')} - intent_id: {receipt.get('intent_id')[:16]}...")
```

### C) Key Workflow

**Single-Mode** (unchanged):
1. Run daily orchestration → `review_candidates.json`
2. Print candidate table
3. Select candidate interactively
4. Quote-only check → interactive menu
5. Execute user choice

**Campaign-Mode** (NEW):
1. Run campaign grid → `candidates_flat.json`
2. Run selector → `recommended.json` (0–2 candidates)
3. Print RECOMMENDED SET table
4. **FOR EACH selected candidate:**
   - Build intent via `intent_builder`
   - Run `execute_trade --quote-only`
   - If OK_TO_STAGE: show interactive menu
   - Execute user choice (keep intent / stage paper / transmit live)
5. Print CAMPAIGN EXECUTION COMPLETE summary

### D) Interactive Menu (Shared)

Both single-mode and campaign-mode use the same interactive menu after quote-only passes:

```
Quote-only check passed. Choose execution option:
  1. Keep intent only (no order)
  2. Stage paper order (no transmission)
  3. Transmit live order (REQUIRES TYPING 'SEND')
  0. Exit without action

Enter choice (default: 0):
```

- **Option 1**: Intent already created, just exit
- **Option 2**: Execute `--paper` (no transmit)
- **Option 3**: Require typing "SEND", execute `--live --transmit --confirm SEND`

## Files Modified

### scripts/daily.py
- Added operator console header with mode/config display
- Implemented campaign mode interactive execution loop
- Added logging suppression for batch internals
- Fixed NO_TRADE early exit case
- Added per-candidate blocking with skip-to-next behavior

### scripts/run_daily_v2.py
- Added batch runner header ("no execution prompts")
- No functional changes

## Non-Negotiables Preserved

✅ Strategy math and candidate generation unchanged  
✅ execute_trade guard logic unchanged  
✅ execute_trade remains only writer of `trade_outcomes.jsonl`  
✅ Deterministic intent_id from intent_builder unchanged  
✅ Single-mode behavior unchanged

## Acceptance Criteria

✅ **Criterion 1**: Running `python scripts/daily.py --campaign configs/campaign_v1.yaml` always ends in:
   - "NO_TRADE" (0 selected), OR
   - quote-only → interactive menu for each selection

✅ **Criterion 2**: No `run_daily_v2` warnings appear in operator console (unless `--verbose`)

✅ **Criterion 3**: Single-mode `daily.py` behavior remains unchanged

✅ **Criterion 4**: Clear entrypoint headers distinguish operator console vs batch runner

✅ **Criterion 5**: Campaign mode processes 0-2 selected candidates interactively

## Output Examples

### Campaign Mode - NO_TRADE
```
================================================================================
OPERATOR CONSOLE: daily.py
Mode: CAMPAIGN
Config: configs/campaign_v1.yaml
================================================================================

RECOMMENDED SET (0-2 CANDIDATES)
⚠️  NO CANDIDATES RECOMMENDED (all blocked by governors)

================================================================================
NO_TRADE - 0 candidates selected
================================================================================
```

### Campaign Mode - Interactive Execution
```
================================================================================
OPERATOR CONSOLE: daily.py
Mode: CAMPAIGN
Config: configs/campaign_v1.yaml
================================================================================

RECOMMENDED SET (0-2 CANDIDATES)
# | UNDERLIER | REGIME | EXPIRY   | STRIKES    | EV/$  | P(Win) | PREMIUM
1 | SPY       | crash  | 20260320 | 590/570    | 2.45  | 72.3%  | $49

================================================================================
INTERACTIVE EXECUTION - 1 SELECTED CANDIDATE(S)
================================================================================

================================================================================
CANDIDATE 1 of 1
================================================================================
Underlier: SPY
Regime: crash
Expiry: 20260320
Strikes: 590.0/570.0
EV/$: 2.45

================================================================================
TICKET SUMMARY
================================================================================
INTENT: SPY 20260320 P590/P570 x1  LIMIT start=0.49 max=0.50  transmit=false
LEGS: 590P bid/ask/mid=0.52/0.54/0.53 | 570P bid/ask/mid=0.03/0.04/0.035
SPREAD(synth): bid/ask/mid=0.49/0.51/0.50
GUARDS: executable_legs=PASS | max_spread_width=PASS | max_debit=PASS | min_dte=PASS
DECISION: OK_TO_STAGE (quote-only mode, not placing order)
================================================================================

Quote-only check passed. Choose execution option:
  1. Keep intent only (no order)
  2. Stage paper order (no transmission)
  3. Transmit live order (REQUIRES TYPING 'SEND')
  0. Exit without action

Enter choice (default: 0): 1

================================================================================
CAMPAIGN EXECUTION COMPLETE
================================================================================
Processed: 1 candidate(s)
  1. INTENT_EMITTED - intent_id: a6d0b9af1234567...
================================================================================
```

## Testing

### Manual Testing Checklist
- [x] Campaign mode with 0 selected → NO_TRADE exit
- [x] Campaign mode with 1 selected → quotes + menu appears
- [x] Campaign mode with 2 selected → both candidates processed sequentially
- [x] Single-mode continues to work unchanged
- [x] Headers clearly distinguish operator console vs batch runner
- [x] Logging noise suppressed (no batch warnings unless --verbose)

### Regression Prevention
The changes are isolated to:
- Entrypoint display/logging (cosmetic)
- Campaign mode execution flow (new functionality)
- No changes to strategy, guards, ledger writing, or intent building

## Migration Notes

### For Operators
- **Before**: Campaign mode printed recommendations then exited
- **After**: Campaign mode prints recommendations, then interactively executes each selected candidate

### For Developers
- `daily.py` is the ONE operator command (interactive)
- `run_daily_v2.py` remains batch/orchestration only (no prompts)
- Campaign candidates flow through the same `intent_builder` + `execute_trade` pipeline as single-mode

## Future Enhancements (Out of Scope)

- [ ] Automated regression tests for campaign interactive flow
- [ ] Support for batch execution mode (no prompts) via `--batch` flag
- [ ] Campaign-aware intent_builder CLI (currently uses manual mapping)
- [ ] Multi-underlier snapshot fetching optimization
- [ ] Resume from interrupted campaign execution

## Conclusion

The operator entrypoints are now clearly distinguished, and campaign mode provides the same interactive execution experience as single-mode. The fix eliminates user confusion and ensures all selected candidates go through the full quote-only → interactive menu flow before any execution decisions are made.
