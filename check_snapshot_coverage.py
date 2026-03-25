import json
import sys

snap_file = sys.argv[1] if len(sys.argv) > 1 else 'snapshots/SPY_snapshot_20260129_152322.json'

with open(snap_file) as f:
    snap = json.load(f)

meta = snap['snapshot_metadata']
spot = meta['current_price']

print(f"Spot: ${spot:.2f}")
print(f"Tail metadata: {meta.get('tail_metadata', 'NOT PRESENT - using legacy mode')}")
print()

# Check each expiry
for exp_date, exp_data in snap['expiries'].items():
    puts = exp_data['puts']
    if not puts:
        continue
    
    strikes = [p['strike'] for p in puts]
    min_strike = min(strikes)
    max_strike = max(strikes)
    moneyness_min = min_strike / spot
    
    print(f"Expiry {exp_date}:")
    print(f"  Put strikes: ${min_strike:.2f} to ${max_strike:.2f}")
    print(f"  Min moneyness: {moneyness_min:.2%} (target: <=75.0% for tail_moneyness_floor=0.25)")
    print(f"  Coverage: {'✅ GOOD' if moneyness_min <= 0.75 else '❌ TOO SHALLOW'}")
    print()
