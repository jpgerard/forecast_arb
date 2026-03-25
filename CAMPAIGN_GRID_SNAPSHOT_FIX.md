# Campaign Grid Multi-Underlier Snapshot Fix

## Problem
The campaign grid was expanded to support:
```
UNDERLIER × REGIME × EXPIRY_BUCKET
```

However, the snapshot layer was still single-underlier oriented, causing:
- Snapshot reuse across underliers (wrong data)
- No validation that snapshot matched cell underlier
- Unclear diagnostics when processing cells

## Solution

### 1. Fresh Snapshot Per Underlier ✅
**Before:** Accepted pre-created snapshots via `snapshots` dict parameter
**After:** Creates fresh snapshot for each underlier inline

```python
# Generate snapshot filename with underlier
timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
snapshot_filename = f"{underlier}_snapshot_{timestamp}.json"
snapshot_path = str(Path(snapshot_dir) / snapshot_filename)

# Create snapshot exporter
exporter = IBKRSnapshotExporter(host="127.0.0.1", port=7496, client_id=1)
exporter.connect()

# Export snapshot for this underlier
exporter.export_snapshot(
    underlier=underlier,
    snapshot_time_utc=datetime.now(timezone.utc).isoformat(),
    dte_min=dte_min,
    dte_max=dte_max,
    tail_moneyness_floor=tail_moneyness_floor,
    out_path=snapshot_path
)

exporter.disconnect()
```

### 2. Snapshot Filename Includes Underlier ✅
**Format:** `{UNDERLIER}_snapshot_{TIMESTAMP}.json`

**Examples:**
- `SPY_snapshot_20260226_141530.json`
- `QQQ_snapshot_20260226_141645.json`
- `IWM_snapshot_20260226_141802.json`

### 3. Snapshot/Underlier Mismatch Assertion ✅
```python
# ASSERTION: Snapshot underlier must match cell underlier
snapshot_symbol = metadata.get('underlier')
if snapshot_symbol != underlier:
    raise ValueError(
        f"Snapshot underlier mismatch: expected '{underlier}', "
        f"got '{snapshot_symbol}' in {snapshot_path}"
    )

logger.info(f"✓ Snapshot validation passed: underlier={snapshot_symbol}")
```

### 4. Diagnostic Print Statement ✅
```python
print(f"[CELL] {underlier} spot={spot:.2f} expiry={expiry} strikes_min={min_strike} strikes_max={max_strike}")
```

**Example Output:**
```
[CELL] SPY spot=585.23 expiry=20260417 strikes_min=480.0 strikes_max=595.0
[CELL] SPY spot=585.23 expiry=20260417 strikes_min=480.0 strikes_max=595.0
[CELL] QQQ spot=512.45 expiry=20260417 strikes_min=420.0 strikes_max=525.0
[CELL] IWM spot=223.67 expiry=20260417 strikes_min=180.0 strikes_max=230.0
```

## API Changes

### run_campaign_grid()
**Before:**
```python
def run_campaign_grid(
    campaign_config_path: str,
    snapshots: Dict[str, str],  # Pre-created snapshots
    structuring_config_path: str,
    ...
) -> str:
```

**After:**
```python
def run_campaign_grid(
    campaign_config_path: str,
    structuring_config_path: str,
    p_external_by_underlier: Optional[Dict[str, float]] = None,
    min_debit_per_contract: float = 10.0,
    snapshot_dir: str = "snapshots",        # NEW: Where to store snapshots
    dte_min: int = 20,                      # NEW: Snapshot DTE range
    dte_max: int = 60,                      # NEW: Snapshot DTE range
    tail_moneyness_floor: float = 0.18     # NEW: Tail coverage
) -> str:
```

### CLI Usage
**Before:**
```bash
python -m forecast_arb.campaign.grid_runner \
  --campaign-config configs/campaign_v1.yaml \
  --structuring-config configs/structuring_crash_venture_v2.yaml \
  --snapshots '{"SPY": "snapshots/SPY_snapshot.json", "QQQ": "snapshots/QQQ_snapshot.json"}'
```

**After:**
```bash
python -m forecast_arb.campaign.grid_runner \
  --campaign-config configs/campaign_v1.yaml \
  --structuring-config configs/structuring_crash_venture_v2.yaml \
  --snapshot-dir snapshots \
  --dte-min 20 \
  --dte-max 60 \
  --tail-moneyness-floor 0.18
```

## Benefits

1. **Data Integrity:** No risk of using wrong underlier's snapshot for a cell
2. **Freshness:** Each underlier gets current market data
3. **Traceability:** Snapshot filenames clearly show which underlier they belong to
4. **Validation:** Runtime assertion catches snapshot mismatches immediately
5. **Diagnostics:** Clear visibility into what data is being processed per cell

## Testing

To test the fix:
```bash
# Ensure IBKR TWS/Gateway is running
python -m forecast_arb.campaign.grid_runner \
  --campaign-config configs/campaign_v1.yaml \
  --structuring-config configs/structuring_crash_venture_v2.yaml
```

Expected output should show:
- Fresh snapshot creation for each underlier
- Validation passing for each snapshot
- `[CELL]` diagnostic lines showing underlier, spot, expiry, and strike ranges

## Status
✅ **COMPLETE** - All requested fixes implemented and ready for testing
