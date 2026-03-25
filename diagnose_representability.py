"""
Diagnostic tool to debug representability failures.

Checks:
1. Snapshot symbol vs expected underlier
2. Actual strike coverage in snapshot
3. Expiry selection and DTE calculation
4. Threshold calculation and units
5. Quote validity (bid/ask presence)
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

def diagnose_snapshot(snapshot_path: str, regime_threshold: float = -0.15):
    """Diagnose why representability is failing."""
    
    print("=" * 100)
    print("REPRESENTABILITY DIAGNOSTIC")
    print("=" * 100)
    print(f"Snapshot: {snapshot_path}")
    print(f"Regime threshold: {regime_threshold:.2%}")
    print("")
    
    # Load snapshot
    with open(snapshot_path, "r") as f:
        snapshot = json.load(f)
    
    metadata = snapshot.get("snapshot_metadata", {})
    
    # CHECK 1: Symbol match
    print("CHECK 1: Symbol Match")
    print("-" * 100)
    snapshot_underlier = metadata.get("underlier")
    spot = metadata.get("current_price")
    snapshot_time = metadata.get("snapshot_time")
    
    print(f"Snapshot underlier: {snapshot_underlier}")
    print(f"Spot price: ${spot:.2f}")
    print(f"Snapshot time: {snapshot_time}")
    print("")
    
    # CHECK 2: Threshold calculation
    print("CHECK 2: Threshold Calculation")
    print("-" * 100)
    threshold_price = spot * (1 + regime_threshold)
    print(f"Regime threshold: {regime_threshold:.2%}")
    print(f"Spot: ${spot:.2f}")
    print(f"Calculated threshold price: ${threshold_price:.2f}")
    print(f"Formula: spot * (1 + threshold) = {spot:.2f} * (1 + {regime_threshold}) = ${threshold_price:.2f}")
    print("")
    
    # CHECK 3: Expiry coverage
    print("CHECK 3: Expiry Coverage")
    print("-" * 100)
    expiries = snapshot.get("expiries", {})
    print(f"Total expiries in snapshot: {len(expiries)}")
    
    if not expiries:
        print("❌ NO EXPIRIES IN SNAPSHOT")
        return
    
    # Parse snapshot time
    snap_dt = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
    
    for expiry, expiry_data in list(expiries.items())[:5]:  # Show first 5
        exp_dt = datetime.strptime(expiry, "%Y%m%d").replace(tzinfo=timezone.utc)
        dte = (exp_dt - snap_dt).days
        
        puts = expiry_data.get("puts", [])
        strikes = [p["strike"] for p in puts]
        
        min_strike = min(strikes) if strikes else None
        max_strike = max(strikes) if strikes else None
        
        print(f"  {expiry} (DTE={dte:2d}): {len(puts)} puts, strikes ${min_strike:.0f}-${max_strike:.0f}")
        
        # Find nearest strike to threshold
        if strikes:
            nearest = min(strikes, key=lambda s: abs(s - threshold_price))
            distance = abs(nearest - threshold_price)
            
            # Find that strike's option
            opt = next((p for p in puts if p["strike"] == nearest), None)
            if opt:
                bid = opt.get("bid")
                ask = opt.get("ask")
                bid_ok = bid is not None and bid > 0
                ask_ok = ask is not None and ask > 0
                
                print(f"    Threshold ${threshold_price:.2f}: nearest=${nearest:.0f}, distance=${distance:.2f}, "
                      f"bid={bid}, ask={ask}, bid_ok={bid_ok}, ask_ok={ask_ok}")
                
                # Representability check
                if distance <= 5.0 and bid_ok and ask_ok:
                    print(f"    ✓ REPRESENTABLE (distance<=$5, bid>0, ask>0)")
                else:
                    reasons = []
                    if distance > 5.0:
                        reasons.append(f"distance=${distance:.2f}>$5")
                    if not bid_ok:
                        reasons.append(f"bid={bid}")
                    if not ask_ok:
                        reasons.append(f"ask={ask}")
                    print(f"    ✗ NOT REPRESENTABLE: {', '.join(reasons)}")
    
    print("")
    
    # CHECK 4: Strike coverage analysis
    print("CHECK 4: Strike Coverage Analysis")
    print("-" * 100)
    
    # Get all unique strikes across all expiries
    all_strikes = set()
    for expiry_data in expiries.values():
        puts = expiry_data.get("puts", [])
        for p in puts:
            all_strikes.add(p["strike"])
    
    strikes_sorted = sorted(all_strikes)
    
    print(f"Total unique strikes: {len(strikes_sorted)}")
    print(f"Strike range: ${min(strikes_sorted):.0f} - ${max(strikes_sorted):.0f}")
    print(f"Spot: ${spot:.2f}")
    print(f"Lowest strike as % of spot: {(min(strikes_sorted)/spot - 1):.1%}")
    print(f"Threshold needs: {regime_threshold:.1%} below spot = ${threshold_price:.2f}")
    
    if min(strikes_sorted) > threshold_price:
        shortfall = min(strikes_sorted) - threshold_price
        print(f"❌ INSUFFICIENT TAIL COVERAGE: Lowest strike ${min(strikes_sorted):.0f} > threshold ${threshold_price:.2f}")
        print(f"   Shortfall: ${shortfall:.2f}")
    else:
        print(f"✓ Tail coverage adequate: lowest strike ${min(strikes_sorted):.0f} <= threshold ${threshold_price:.2f}")
    
    print("")
    
    # CHECK 5: Tail metadata
    print("CHECK 5: Snapshot Tail Metadata")
    print("-" * 100)
    tail_metadata = metadata.get("tail_metadata", {})
    if tail_metadata:
        print(f"Tail floor strike: ${tail_metadata.get('tail_floor_strike', 'N/A')}")
        print(f"Tail moneyness floor: {tail_metadata.get('tail_moneyness_floor', 'N/A')}")
        print(f"Tail floor source: {tail_metadata.get('tail_floor_source', 'N/A')}")
        print(f"Incomplete: {tail_metadata.get('incomplete', False)}")
        if tail_metadata.get('incomplete'):
            print(f"  Requested floor: ${tail_metadata.get('requested_floor', 'N/A')}")
            print(f"  Actual floor: ${tail_metadata.get('actual_floor', 'N/A')}")
    else:
        print("No tail metadata (using legacy mode)")
    
    print("")
    print("=" * 100)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Diagnose representability failures")
    parser.add_argument("snapshot", help="Path to snapshot JSON file")
    parser.add_argument("--regime-threshold", type=float, default=-0.15, help="Regime threshold (default: -0.15 for crash)")
    
    args = parser.parse_args()
    
    diagnose_snapshot(args.snapshot, args.regime_threshold)
