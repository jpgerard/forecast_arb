"""
Daily Review Summary Tool

E2: Extracts and prints key lines from review_pack.md for quick scanning.
"""

import argparse
import re
import sys
from pathlib import Path


def extract_key_lines(review_pack_path: Path) -> dict:
    """Extract key information from review_pack.md."""
    if not review_pack_path.exists():
        raise FileNotFoundError(f"Review pack not found: {review_pack_path}")
    
    with open(review_pack_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    info = {}
    
    # Extract run ID
    run_id_match = re.search(r'\*\*Run ID:\*\* `([^`]+)`', content)
    if run_id_match:
        info['run_id'] = run_id_match.group(1)
    
    # Extract snapshot info
    underlier_match = re.search(r'- \*\*Underlier:\*\* (.+)', content)
    spot_match = re.search(r'- \*\*Spot Price:\*\* \$([0-9.]+)', content)
    expiry_match = re.search(r'- \*\*Expiry Used:\*\* (.+)', content)
    dte_match = re.search(r'- \*\*DTE:\*\* ([0-9]+)', content)
    
    if underlier_match:
        info['underlier'] = underlier_match.group(1)
    if spot_match:
        info['spot'] = float(spot_match.group(1))
    if expiry_match:
        info['expiry'] = expiry_match.group(1)
    if dte_match:
        info['dte'] = int(dte_match.group(1))
    
    # Extract event
    event_match = re.search(r'- \*\*Event:\*\* P\((.+?) < \$([0-9.]+) at ([0-9]+)\)', content)
    if event_match:
        info['event_underlier'] = event_match.group(1)
        info['event_threshold'] = float(event_match.group(2))
        info['event_expiry'] = event_match.group(3)
    
    # Extract p_external
    p_ext_match = re.search(r'- \*\*Value:\*\* ([0-9.]+) \(([0-9.]+)%\)', content, re.MULTILINE)
    if p_ext_match:
        info['p_external'] = float(p_ext_match.group(1))
    else:
        # Try N/A pattern
        if "- **Value:** N/A" in content:
            info['p_external'] = None
    
    # Extract source
    source_match = re.search(r'- \*\*Source:\*\* (.+)', content)
    if source_match:
        info['source'] = source_match.group(1).strip()
    
    # Check for proxy
    info['has_proxy'] = "Proxy Probability Available" in content
    
    # Extract p_implied
    p_impl_section = re.search(r'### Options-Implied Probability \(p_implied\)(.+?)(?:###|##|\n\n)', content, re.DOTALL)
    if p_impl_section:
        p_impl_val_match = re.search(r'- \*\*Value:\*\* ([0-9.]+)', p_impl_section.group(1))
        if p_impl_val_match:
            info['p_implied'] = float(p_impl_val_match.group(1))
        elif "N/A" in p_impl_section.group(1):
            info['p_implied'] = None
    
    # Extract edge
    edge_match = re.search(r'### Edge.+?- \*\*Value:\*\* ([0-9.+-]+)', content, re.DOTALL)
    if edge_match:
        info['edge'] = float(edge_match.group(1))
    elif "- **Value:** N/A" in content:
        if 'edge' not in info:
            info['edge'] = None
    
    # Extract gate decision
    gate_match = re.search(r'- \*\*Result:\*\* `(.+?)`', content)
    gate_reason_match = re.search(r'- \*\*Reason:\*\* (.+)', content)
    if gate_match:
        info['gate_decision'] = gate_match.group(1)
    if gate_reason_match:
        info['gate_reason'] = gate_reason_match.group(1).strip()
    
    # Extract external policy
    policy_match = re.search(r'- \*\*Allowed:\*\* (Yes|No)', content)
    policy_result_match = re.search(r'- \*\*Policy Result:\*\* (.+)', content)
    if policy_match:
        info['policy_allowed'] = policy_match.group(1) == "Yes"
    if policy_result_match:
        info['policy_result'] = policy_result_match.group(1).strip()
    
    # Extract top 3 candidates
    table_match = re.search(r'\| Rank \| Expiry \|(.+?)\n\n', content, re.DOTALL)
    if table_match:
        table_rows = table_match.group(1).strip().split('\n')
        candidates = []
        for row in table_rows:
            if row.startswith('|') and not row.startswith('|---'):
                parts = [p.strip() for p in row.split('|')[1:-1]]  # Skip empty first/last
                if len(parts) >= 8 and parts[0].isdigit():
                    candidates.append({
                        'rank': int(parts[0]),
                        'expiry': parts[1],
                        'strikes': parts[2],
                        'debit': parts[3],
                        'ev_per_dollar': parts[6] if len(parts) > 6 else None
                    })
        info['top_candidates'] = candidates[:3]
    
    return info


def print_summary(info: dict):
    """Print formatted summary."""
    print("=" * 80)
    print("DAILY REVIEW SUMMARY")
    print("=" * 80)
    print()
    
    # Run ID
    if 'run_id' in info:
        print(f"Run ID: {info['run_id']}")
        print()
    
    # Market snapshot
    print("MARKET SNAPSHOT:")
    print("-" * 80)
    if 'underlier' in info and 'spot' in info:
        print(f"  {info['underlier']}: ${info['spot']:.2f}")
    if 'expiry' in info and 'dte' in info:
        print(f"  Target Expiry: {info['expiry']} ({info['dte']} DTE)")
    print()
    
    # Event
    print("EVENT:")
    print("-" * 80)
    if 'event_underlier' in info and 'event_threshold' in info:
        print(f"  P({info['event_underlier']} < ${info['event_threshold']:.2f})")
    print()
    
    # Probabilities
    print("PROBABILITIES:")
    print("-" * 80)
    p_ext = info.get('p_external')
    p_impl = info.get('p_implied')
    edge = info.get('edge')
    
    if p_ext is not None:
        print(f"  p_external:  {p_ext:.4f} ({p_ext*100:.2f}%)")
    else:
        print(f"  p_external:  N/A")
    
    source = info.get('source', 'unknown')
    print(f"  Source:      {source}")
    
    if info.get('has_proxy'):
        print(f"  ⚠️  PROXY DETECTED (not exact match)")
    
    if p_impl is not None:
        print(f"  p_implied:   {p_impl:.4f} ({p_impl*100:.2f}%)")
    else:
        print(f"  p_implied:   N/A")
    
    if edge is not None:
        edge_bps = edge * 10000
        print(f"  Edge:        {edge:.4f} ({edge_bps:+.1f} bps)")
    else:
        print(f"  Edge:        N/A")
    print()
    
    # Gate & Policy
    print("GATE & POLICY:")
    print("-" * 80)
    gate_decision = info.get('gate_decision', 'UNKNOWN')
    gate_reason = info.get('gate_reason', 'N/A')
    print(f"  Gate:        {gate_decision}")
    print(f"  Reason:      {gate_reason}")
    
    policy_allowed = info.get('policy_allowed')
    policy_result = info.get('policy_result', 'N/A')
    if policy_allowed is not None:
        print(f"  Policy:      {'ALLOWED' if policy_allowed else 'BLOCKED'} ({policy_result})")
    print()
    
    # Top candidates
    print("TOP 3 CANDIDATES:")
    print("-" * 80)
    candidates = info.get('top_candidates', [])
    if candidates:
        for cand in candidates:
            print(f"  #{cand['rank']}: {cand['expiry']} {cand['strikes']} @ {cand['debit']}")
            if cand.get('ev_per_dollar'):
                print(f"       EV/$: {cand['ev_per_dollar']}")
    else:
        print("  (No candidates)")
    print()
    
    # Overall assessment
    print("QUICK ASSESSMENT:")
    print("-" * 80)
    if gate_decision == "NO_TRADE":
        print("  ❌ NO TRADE (gate blocked)")
        print(f"     Reason: {gate_reason}")
    elif not info.get('policy_allowed', True):
        print("  ❌ NO TRADE (policy blocked)")
        print(f"     Reason: {policy_result}")
    elif not candidates:
        print("  ❌ NO TRADE (no candidates)")
    else:
        print("  ✓ Candidates available for review")
        if info.get('has_proxy'):
            print("  ⚠️  WARNING: Using proxy probability (not exact match)")
    
    print("=" * 80)


def main():
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Extract key information from review_pack.md"
    )
    parser.add_argument(
        "review_pack",
        nargs="?",
        default=None,
        help="Path to review_pack.md (default: latest run)"
    )
    
    args = parser.parse_args()
    
    # Determine review pack path
    if args.review_pack:
        review_pack_path = Path(args.review_pack)
    else:
        # Use latest run
        runs_root = Path("runs")
        latest_file = runs_root / "LATEST.json"
        
        if not latest_file.exists():
            print("❌ No LATEST.json found and no path provided")
            print("Usage: python tools/review_summary.py [path/to/review_pack.md]")
            sys.exit(1)
        
        import json
        with open(latest_file, "r") as f:
            latest = json.load(f)
        
        run_dir = Path(latest.get("run_dir", ""))
        review_pack_path = run_dir / "artifacts" / "review_pack.md"
    
    # Extract and print
    try:
        info = extract_key_lines(review_pack_path)
        print_summary(info)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error extracting summary: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
