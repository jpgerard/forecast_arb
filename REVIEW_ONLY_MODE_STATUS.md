# Review-Only Structuring Mode - Implementation Status

## Overview
Implementation of `--review-only-structuring` flag that allows running the structuring engine even when edge gate or external source policy blocks trading. Results are "for review only" and no executable orders are generated.

## Completed Components

### 1. Core Infrastructure ✅
- Created `forecast_arb/review/__init__.py`
- Created `forecast_arb/review/review_pack.py` with:
  - `render_review_pack()` - Generates markdown review pack
  - `render_decision_template()` - Generates decision template for manual use

### 2. CLI Flag ✅
- Added `--review-only-structuring` flag to `scripts/run_daily.py`
- Added safety check that prevents `--submit` when `--review-only-structuring` is enabled (exits with code 2)
- Added review-only mode banner display

### 3. Part Partial Review-Only Logic 🚧
- Added `would_block_trade` flag to track blocking conditions
- Modified edge gate block handling to check for review-only mode
- Initial structure for continuing to structuring when blocked

## Remaining Work

### 1. Complete Review-Only Structuring Logic
- [ ] When `would_block_trade == True` and `args.review_only_structuring == True`:
  - Run structuring engine anyway
  - Mark all structures as review-only
  - Write `review_candidates.json` instead of `tickets.json`
  - Write `review_pack.md` and `decision_template.md`
  
- [ ] Handle external source policy block similarly to edge gate block

### 2. Artifact Generation
- [ ] Implement review candidate JSON generation with full provenance:
  - `review_only: true` flag
  - `blocked_by` metadata (gate + policy reasons)
  - `estimated_entry` with pricing sources
  - `pricing_quality` field (EXECUTABLE/MID/MODEL/STALE)
  - Full warnings list

- [ ] Write review pack artifacts:
  - `artifacts/review_candidates.json`
  - `artifacts/review_pack.md`
  - `artifacts/decision_template.md`

### 3. Update final_decision.json
- [ ] Add review-only metadata:
  - `metadata.review_only_structuring = true`
  - `metadata.structuring_ran = true`
  - `metadata.structuring_block_reason`
  - `metadata.review_candidates_written = true`

### 4. Update review.txt
- [ ] Add REVIEW-ONLY MODE section when flag is set
- [ ] Show `submit_allowed: FALSE`
- [ ] Show blocking reasons

### 5. Testing
- [ ] Create test for review-only mode with edge gate block
- [ ] Create test for review-only mode with external policy block
- [ ] Test that --submit + --review-only-structuring exits with error
- [ ] Test that review artifacts are generated

### 6. Documentation
- [ ] Add section to README or create REVIEW_ONLY_MODE_README.md
- [ ] Update EDGE_GATING_INTEGRATION_COMPLETE.md
- [ ] Document example commands
- [ ] Document artifact locations and formats

## Usage Example (When Complete)

```bash
# Run with review-only mode when edge gate blocks
python scripts/run_daily.py \
  --snapshot snapshots/SPY_snapshot_20260130.json \
  --p-event-source fallback \
  --fallback-p 0.30 \
  --mode dev \
  --review-only-structuring

# Output:
# - runs/crash_venture_v1/{run_id}/artifacts/review_candidates.json
# - runs/crash_venture_v1/{run_id}/artifacts/review_pack.md
# - runs/crash_venture_v1/{run_id}/artifacts/decision_template.md
```

## Notes
- Review-only mode is designed for manual decision support
- All structures generated are non-executable and for review purposes only
- JP can paste review_pack.md to ChatGPT for analysis
- Decision template provides structured format for documenting manual decisions

## Status: PARTIAL IMPLEMENTATION
Core infrastructure is in place. Need to complete the actual review-only structuring flow and artifact generation.
