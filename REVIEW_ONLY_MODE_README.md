# Review-Only Structuring Mode

## Overview

The `--review-only-structuring` flag enables the crash venture daily cycle to run structure generation **even when trading is blocked** by edge gate or external source policy. This mode generates structures for manual review and decision support, but **never creates executable orders**.

## Use Cases

### When to Use Review-Only Mode

1. **Edge Gate Blocks** - When p_implied confidence is low or edge is insufficient, but you want to see what structures would have been generated
2. **External Source Policy Blocks** - When using fallback p_event without `--allow-fallback-trade`, but you still want to review candidates
3. **Manual Decision Support** - When you want to generate paste-friendly review packs for ChatGPT analysis before making manual trading decisions
4. **Learning & Calibration** - To understand what the engine generates without risk of accidental submission

### When NOT to Use

- When you want executable orders (use normal mode instead)
- In conjunction with `--submit` (hard error - incompatible flags)

## Safety Guardrails

### Hard Blocks

1. **Submit Protection**: If both `--review-only-structuring` and `--submit` are provided, the script exits immediately with code 2:
   ```
   ❌ FATAL ERROR: Cannot submit in review-only mode
   --review-only-structuring flag prevents order submission.
   Remove --submit flag or --review-only-structuring flag.
   ```

2. **No Executable Orders**: When review-only mode is active:
   - `tickets.json` may be empty or clearly marked as non-executable
   - Orders cannot be submitted under any circumstances
   - All structures are labeled as review candidates only

## Usage

### Basic Command

```powershell
python scripts/run_daily.py `
  --snapshot snapshots/SPY_snapshot_20260130.json `
  --p-event-source fallback `
  --fallback-p 0.30 `
  --mode dev `
  --review-only-structuring
```

### Example: Edge Gate Blocked

```powershell
# This would normally skip structuring because edge < min_edge
# With --review-only-structuring, it still generates review candidates
python scripts/run_daily.py `
  --snapshot snapshots/SPY_snapshot_20260130_155654.json `
  --p-event-source fallback `
  --fallback-p 0.30 `
  --mode dev `
  --min-debit-per-contract 15.0 `
  --review-only-structuring
```

### Example: External Source Policy Blocked

```powershell
# Using fallback without --allow-fallback-trade normally blocks trading
# With --review-only-structuring, generates structures for review
python scripts/run_daily.py `
  --snapshot snapshots/SPY_snapshot_20260130_155654.json `
  --p-event-source fallback `
  --fallback-p 0.28 `
  --mode dev `
  --review-only-structuring
# Note: --allow-fallback-trade is NOT set, so external policy would block
```

## Output Artifacts

When review-only mode runs and structuring produces candidates, three additional artifacts are generated:

### 1. `artifacts/review_candidates.json`

Full JSON representation of structures with review metadata:

```json
[
  {
    "review_only": true,
    "rank": 1,
    "expiry": "20260227",
    "strikes": {
      "long_put": 540.0,
      "short_put": 520.0
    },
    "blocked_by": {
      "type": "EDGE_GATE_BLOCKED",
      "reason": "NO_P_IMPLIED",
      "edge_gate": { ... },
      "external_policy": { ... }
    },
    "estimated_entry": {
      "debit": 12.50,
      "pricing_quality": "EXECUTABLE",
      "pricing_sources": {
        "long_put": "ask",
        "short_put": "bid"
      }
    },
    "structure": {
      "max_loss": 1250.0,
      "max_gain": 750.0,
      "width": 20.0
    },
    "metrics": {
      "ev": 85.50,
      "ev_per_dollar": 0.068,
      "breakeven": 532.50,
      "pop_estimate": 0.42
    },
    "notes": []
  }
]
```

### 2. `artifacts/review_pack.md`

Comprehensive markdown summary designed for pasting into ChatGPT or other LLMs:

**Sections:**
1. Snapshot Summary (underlier, spot, time, expiry, DTE)
2. Event Definition (threshold, moneyness)
3. Probabilities (p_external, p_implied, edge, confidences, ATM IV source details)
4. Edge Gate Decision (result, reason, thresholds, confidence breakdown)
5. External Source Policy (source, allowed, policy result)
6. Top Structure Candidates (table with top 5: expiry, strikes, debit, max loss/gain, EV, EV/$, warnings, pricing quality)
7. JP Decision Checklist (questions to consider)

**Pricing Quality Legend:**
- `EXECUTABLE`: Both legs have bid/ask quotes
- `MID`: One or more legs using mid-price fallback
- `MODEL`: One or more legs using Black-Scholes model fallback
- `STALE`: Quotes may be stale or unreliable

### 3. `artifacts/decision_template.md`

Structured template for documenting manual trading decisions:

**Sections:**
- Decision (TRADE yes/no, reason if no)
- Trade Details (candidate selected, strikes, entry pricing, position sizing)
- Risk Confirmation (checklist)
- Notes / Reasoning
- Execution Confirmation (order ID, fill price, fill time)

Use this to maintain audit trail for manual overrides.

## Workflow

### Typical Review-Only Workflow

1. **Run with review-only flag** when blocked:
   ```powershell
   python scripts/run_daily.py --snapshot <path> --p-event-source fallback --fallback-p 0.30 --review-only-structuring --mode dev
   ```

2. **Check console output** for run directory:
   ```
   📋 Review-only artifacts generated:
      • runs/crash_venture_v1/{run_id}/artifacts/review_candidates.json
      • runs/crash_venture_v1/{run_id}/artifacts/review_pack.md
      • runs/crash_venture_v1/{run_id}/artifacts/decision_template.md
   ```

3. **Open review_pack.md** and copy contents

4. **Paste into ChatGPT** with prompt:
   ```
   I'm reviewing automated trading suggestions that were blocked by my risk system.
   Please analyze the following data and help me decide if I should manually override
   the block and execute a trade. Key considerations:
   - Is the edge calculation reasonable given the data quality?
   - Are the pricing quality issues acceptable for this trade?
   - What are the key risks I should be aware of?
   
   [paste review_pack.md contents]
   ```

5. **Make decision** based on analysis

6. **If trading**, fill out `decision_template.md` for audit trail

7. **Execute manually** through your trading platform (IBKR, etc.)

## Metadata in Artifacts

### final_decision.json Enhancement

When review-only mode is active, `final_decision.json` includes:

```json
{
  "decision": "NO_TRADE",
  "reason": "EDGE_GATE_BLOCKED:NO_P_IMPLIED",
  "metadata": {
    "review_only_structuring": true,
    "structuring_ran": true,
    "would_block_trade": true,
    "structuring_block_reason": "EDGE_GATE_BLOCKED: NO_P_IMPLIED",
    "review_candidates_written": true
  }
}
```

### review.txt Enhancement

Standard review includes REVIEW-ONLY MODE section when flag is set:

```
REVIEW-ONLY MODE: TRUE
submit_allowed: FALSE
reason_for_blocking: EDGE_GATE_BLOCKED: NO_P_IMPLIED
```

## Behavior Matrix

| Scenario | Normal Mode | Review-Only Mode |
|----------|-------------|------------------|
| Edge gate passes, policy passes | Generate tickets, allow submit | **Run structuring**, generate review artifacts only, REVIEW_ONLY decision, NEVER submit |
| Edge gate blocks | Skip structuring, NO_TRADE | **Run structuring**, generate review artifacts, REVIEW_ONLY decision |
| External policy blocks | Skip structuring, NO_TRADE | **Run structuring**, generate review artifacts, REVIEW_ONLY decision |
| Structuring generates candidates | Write tickets.json | **DO NOT write tickets.json**, write review_candidates.json + review pack |
| --submit flag provided | Honor (if confirmed) | **Hard error, exit code 2** |

**KEY PRINCIPLE:** Review-only mode ALWAYS operates in review-only behavior for the entire run. The decision is always "REVIEW_ONLY", tickets.json is never written, and submission is never allowed, regardless of whether gates/policies would have passed.

## Testing

Run the test suite to verify review-only mode:

```powershell
pytest tests/test_review_only_mode.py -v
```

Tests verify:
- `--review-only-structuring` + `--submit` causes exit code 2
- Flag is recognized in help output
- (Future: integration tests for artifact generation)

## Implementation Notes

### Code Locations

- **CLI Flag**: `scripts/run_daily.py` (argparse)
- **Safety Check**: `scripts/run_daily.py` (main function, early exit)
- **Review Pack Generator**: `forecast_arb/review/review_pack.py`
- **Flow Logic**: `scripts/run_daily.py` (Step 3 structuring conditional)
- **Artifact Generation**: `scripts/run_daily.py` (Step 5)

### Key Variables

- `args.review_only_structuring` - CLI flag boolean
- `would_block_trade` - Boolean tracking if edge gate or external policy blocked
- `block_type` - String: "EDGE_GATE_BLOCKED" or "EXTERNAL_SOURCE_BLOCKED"
- `block_reason_detail` - Specific reason (e.g., "NO_P_IMPLIED", "BLOCKED_FALLBACK")

## Future Enhancements

Potential improvements (not yet implemented):

1. **Historical Comparison** - "What changed vs yesterday" section in review pack
2. **Confidence Scores** - Overall confidence score for each candidate
3. **Market Context** - VIX level, recent moves, upcoming events
4. **Automated LLM Integration** - Direct API call to GPT-4 with review pack
5. **Email/Slack Notifications** - Alert when review candidates are generated
6. **Web Dashboard** - View review packs in browser with interactive charts

## Troubleshooting

### Review Artifacts Not Generated

**Problem**: Review-only mode enabled but no review_pack.md created

**Causes**:
1. Structuring didn't produce any candidates (check `filter_diagnostics.json`)
2. Not actually blocked (edge gate and policy both passed - review mode does nothing)
3. Error during structuring (check console output for exceptions)

**Solution**: Check logs and verify:
- `would_block_trade = True`
- `result['top_structures']` has candidates
- No exceptions during structuring

### Exit Code 2 Error

**Problem**: Script exits immediately with code 2

**Cause**: Both `--review-only-structuring` and `--submit` provided

**Solution**: Remove one of the flags. Review-only mode is incompatible with submission.

## Related Documentation

- [EDGE_GATING_INTEGRATION_COMPLETE.md](./EDGE_GATING_INTEGRATION_COMPLETE.md) - Edge gating system
- [P_EVENT_SYSTEM_README.md](./P_EVENT_SYSTEM_README.md) - External probability sources
- [CRASH_VENTURE_V1_README.md](./CRASH_VENTURE_V1_README.md) - Core engine documentation

## Version History

- **v1.0.0** (2026-01-30): Initial implementation
  - CLI flag added
  - Safety checks implemented
  - Review pack generator created
  - Artifact generation integrated
  - Basic tests added
