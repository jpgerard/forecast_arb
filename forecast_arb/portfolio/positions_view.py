"""
Portfolio positions view derived from event-based trade_outcomes ledger.

Reads runs/trade_outcomes.jsonl and derives:
- open_positions: intents with FILLED_OPEN and no later CLOSED event
- pending_orders: intents with SUBMITTED_LIVE and no later FILLED_OPEN/CLOSED
- open_premium_by_regime: sum of entry premiums by regime
- open_count_by_regime: count of open positions by regime
- open_clusters: set of cluster_ids with open positions

Convention: premium_usd = debit_per_contract * qty (no ×100)
"""

import json
from pathlib import Path
from typing import Dict, List, Set, Any, Optional
from collections import defaultdict


def compute_premium_usd(row: Dict[str, Any]) -> float:
    """
    Compute premium in USD from a trade outcome event.
    
    Convention: premium_usd = debit_per_contract * qty (no ×100)
    
    For FILLED_OPEN events:
    - Uses 'fill_price' if available (actual fill)
    - Falls back to 'entry_price' if available
    - Returns 0.0 if neither present
    
    Args:
        row: Event dict from trade_outcomes.jsonl
    
    Returns:
        Premium in USD (debit * qty, no multiplier)
    """
    qty = row.get("qty", 1)
    
    # Prefer fill_price for actual fills, fallback to entry_price
    price = row.get("fill_price") or row.get("entry_price")
    
    if price is None:
        return 0.0
    
    # Convention: debit_per_contract is in dollars, qty is number of spreads
    # premium_usd = debit_per_contract * qty (no ×100)
    return float(price) * qty


def load_positions_view(ledger_path: str = "runs/trade_outcomes.jsonl") -> Dict[str, Any]:
    """
    Load portfolio state from event-based trade outcomes ledger.
    
    Returns dict with:
        open_positions: List[Dict] - positions with FILLED_OPEN, no CLOSED
        pending_orders: List[Dict] - orders with SUBMITTED_LIVE, no FILLED_OPEN/CLOSED
        open_premium_by_regime: Dict[str, float] - premium at risk by regime
        open_premium_total: float - total premium at risk
        open_count_by_regime: Dict[str, int] - count of open positions by regime
        open_clusters: Set[str] - cluster_ids with open positions
    
    Args:
        ledger_path: Path to trade_outcomes.jsonl
    """
    ledger_file = Path(ledger_path)
    
    # Initialize empty state
    positions_state = {
        "open_positions": [],
        "pending_orders": [],
        "open_premium_by_regime": {},
        "open_premium_total": 0.0,
        "open_count_by_regime": {},
        "open_clusters": set(),
    }
    
    if not ledger_file.exists():
        return positions_state
    
    # Track latest event for each intent_id
    intent_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    
    with open(ledger_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            try:
                event = json.loads(line)
                
                # Skip legacy rows missing "event" field
                if "event" not in event:
                    continue
                
                intent_id = event.get("intent_id")
                if not intent_id:
                    continue
                
                # Store event with timestamp for sorting
                intent_events[intent_id].append(event)
                
            except json.JSONDecodeError:
                continue
    
    # Process each intent's event timeline
    open_positions = []
    pending_orders = []
    open_premium_by_regime = defaultdict(float)
    open_count_by_regime = defaultdict(int)
    open_clusters = set()
    
    for intent_id, events in intent_events.items():
        # Sort by timestamp to get event order
        sorted_events = sorted(events, key=lambda e: e.get("timestamp_utc", ""))
        
        if not sorted_events:
            continue
        
        latest_event = sorted_events[-1]
        event_type = latest_event["event"]
        
        # Check if position is FILLED_OPEN with no later CLOSED
        if event_type == "FILLED_OPEN":
            # Check if there's a later CLOSED event
            has_closed = any(e["event"] == "CLOSED" for e in sorted_events)
            
            if not has_closed:
                # This is an open position
                regime = latest_event.get("regime", "unknown")
                cluster_id = latest_event.get("cluster_id")
                premium = compute_premium_usd(latest_event)
                
                open_positions.append(latest_event)
                open_premium_by_regime[regime] += premium
                open_count_by_regime[regime] += 1
                
                if cluster_id:
                    open_clusters.add(cluster_id)
        
        # Check if order is SUBMITTED_LIVE with no later FILLED_OPEN or CLOSED
        elif event_type == "SUBMITTED_LIVE":
            has_filled = any(e["event"] in ["FILLED_OPEN", "CLOSED"] for e in sorted_events)
            
            if not has_filled:
                pending_orders.append(latest_event)
    
    # Calculate total premium
    open_premium_total = sum(open_premium_by_regime.values())
    
    return {
        "open_positions": open_positions,
        "pending_orders": pending_orders,
        "open_premium_by_regime": dict(open_premium_by_regime),
        "open_premium_total": open_premium_total,
        "open_count_by_regime": dict(open_count_by_regime),
        "open_clusters": open_clusters,
    }


def get_positions_summary(ledger_path: str = "runs/trade_outcomes.jsonl") -> str:
    """
    Get human-readable summary of current portfolio positions.
    
    Args:
        ledger_path: Path to trade_outcomes.jsonl
    
    Returns:
        Formatted summary string
    """
    view = load_positions_view(ledger_path)
    
    lines = []
    lines.append("=" * 60)
    lines.append("PORTFOLIO POSITIONS")
    lines.append("=" * 60)
    lines.append(f"Open Positions: {len(view['open_positions'])}")
    lines.append(f"Pending Orders: {len(view['pending_orders'])}")
    lines.append(f"Total Premium at Risk: ${view['open_premium_total']:.2f}")
    lines.append("")
    
    if view['open_count_by_regime']:
        lines.append("By Regime:")
        for regime, count in sorted(view['open_count_by_regime'].items()):
            premium = view['open_premium_by_regime'].get(regime, 0.0)
            lines.append(f"  {regime}: {count} positions, ${premium:.2f}")
        lines.append("")
    
    if view['open_clusters']:
        lines.append(f"Open Clusters: {', '.join(sorted(view['open_clusters']))}")
        lines.append("")
    
    lines.append("=" * 60)
    
    return "\n".join(lines)


if __name__ == "__main__":
    # Demo: print current portfolio state
    print(get_positions_summary())
