import json
from datetime import datetime

snapshot_path = "snapshots/QQQ_snapshot_20260226_134924.json"

with open(snapshot_path) as f:
    s = json.load(f)

m = s['snapshot_metadata']
print(f"Snapshot: {snapshot_path}")
print(f"Spot: ${m['current_price']:.2f}")
print(f"Timestamp: {m['snapshot_time']}")
print(f"\nExpiries: {list(s['expiries'].keys())}")

# Check each expiry
for expiry, data in s['expiries'].items():
    puts = data.get('puts', [])
    if puts:
        strikes = [p['strike'] for p in puts]
        print(f"\n{expiry}:")
        print(f"  Strikes: ${min(strikes):.2f} - ${max(strikes):.2f}")
        print(f"  Count: {len(strikes)} puts")
        
        # Check crash threshold coverage (-15%, -20%)
        spot = m['current_price']
        threshold_15 = spot * 0.85
        threshold_20 = spot * 0.80
        
        has_15 = any(abs(s - threshold_15) <= 5.0 for s in strikes)
        has_20 = any(abs(s - threshold_20) <= 5.0 for s in strikes)
        
        print(f"  Crash -15% (${threshold_15:.2f}): {'✓' if has_15 else '✗'}")
        print(f"  Crash -20% (${threshold_20:.2f}): {'✓' if has_20 else '✗'}")

# Check DTE
snap_dt = datetime.fromisoformat(m['snapshot_time'].replace('Z', '+00:00'))
print(f"\n DTE Analysis:")
for expiry in s['expiries'].keys():
    exp_dt = datetime.strptime(expiry, "%Y%m%d")
    dte = (exp_dt - snap_dt).days
    print(f"  {expiry}: {dte} days")
