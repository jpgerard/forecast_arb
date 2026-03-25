"""
Structure router: Choose best structures under constraints.
"""

from typing import Dict, List, Optional
import numpy as np
import logging

logger = logging.getLogger(__name__)


def filter_dominated_structures(structures: List[Dict]) -> List[Dict]:
    """
    Remove strictly dominated structures.
    
    Structure A dominates Structure B if (with same expiry):
    - A has lower or equal debit (less paid) AND
    - A has higher or equal max_gain AND
    - A has better or equal breakeven (higher for puts, meaning less downside needed)
    
    Args:
        structures: List of evaluated structure dicts
        
    Returns:
        List with dominated structures removed
    """
    if not structures:
        return []
    
    non_dominated = []
    
    for i, struct_a in enumerate(structures):
        is_dominated = False
        
        for j, struct_b in enumerate(structures):
            if i == j:
                continue
            
            # Only compare structures with same expiry
            if struct_a.get("expiry") != struct_b.get("expiry"):
                continue
            
            # Check if struct_a is dominated by struct_b
            # For debit spreads: lower debit = better
            debit_worse = struct_a.get("debit", 0) >= struct_b.get("debit", 0)
            
            # Higher max_gain = better
            max_gain_worse = struct_a.get("max_gain", 0) <= struct_b.get("max_gain", 0)
            
            # For put spreads: higher breakeven = better (less downside needed to profit)
            breakeven_a = struct_a.get("breakeven", 0)
            breakeven_b = struct_b.get("breakeven", 0)
            breakeven_worse = breakeven_a <= breakeven_b
            
            # struct_a is dominated if ALL conditions are worse or equal
            if debit_worse and max_gain_worse and breakeven_worse:
                # At least one must be strictly worse (not just equal)
                strictly_worse = (
                    struct_a.get("debit", 0) > struct_b.get("debit", 0) or
                    struct_a.get("max_gain", 0) < struct_b.get("max_gain", 0) or
                    breakeven_a < breakeven_b
                )
                
                if strictly_worse:
                    is_dominated = True
                    break
        
        if not is_dominated:
            non_dominated.append(struct_a)
    
    n_removed = len(structures) - len(non_dominated)
    if n_removed > 0:
        logger.info(f"Dominance filter removed {n_removed} dominated structures")
    
    return non_dominated


def choose_best_structure(
    evaluated_structures: List[Dict],
    constraints: Dict,
    objective: str = "max_ev"
) -> List[Dict]:
    """
    Choose best structures subject to constraints.
    
    Args:
        evaluated_structures: List of evaluated structure dicts
        constraints: Dict with max_loss_usd, min_prob_profit, etc.
        objective: Optimization objective:
            - "max_ev": Maximize expected value
            - "max_ev_per_dollar": Maximize EV per dollar risked
            - "max_sharpe": Maximize EV/std ratio
            - "max_prob_profit": Maximize probability of profit
            
    Returns:
        Sorted list of structures meeting constraints (best first)
    """
    max_loss_usd = constraints.get("max_loss_usd_per_trade", float('inf'))
    min_prob_profit = constraints.get("min_prob_profit", 0.0)
    min_ev = constraints.get("min_ev", -float('inf'))
    
    # Filter by constraints
    valid_structures = []
    for struct in evaluated_structures:
        # Check max loss constraint
        if abs(struct["max_loss"]) > max_loss_usd:
            continue
        
        # Check probability of profit
        if struct["prob_profit"] < min_prob_profit:
            continue
        
        # Check minimum EV
        if struct["ev"] < min_ev:
            continue
        
        valid_structures.append(struct)
    
    if not valid_structures:
        return []
    
    # DEBUG: Print sort keys before sorting
    logger.info(f"Sorting {len(valid_structures)} structures by objective={objective}")
    for i, s in enumerate(valid_structures):
        ev_per_dollar = s.get("ev_per_dollar", 0)
        ev_per_contract = s.get("ev", 0) * 100
        debit_per_contract = s.get("debit_per_contract", s.get("debit", 0) * 100)
        win_prob = s.get("prob_profit", 0)
        
        # Type check: ensure ev_per_dollar is float
        if not isinstance(ev_per_dollar, (int, float)):
            logger.error(f"Structure {i}: ev_per_dollar is {type(ev_per_dollar)}, not float: {ev_per_dollar}")
            raise TypeError(f"ev_per_dollar must be float, got {type(ev_per_dollar)}")
        
        logger.info(
            f"  [BEFORE] #{i}: ev_per_dollar={ev_per_dollar:.4f}, "
            f"ev_per_contract=${ev_per_contract:.2f}, "
            f"debit_per_contract=${debit_per_contract:.2f}, "
            f"win_prob={win_prob:.3f}"
        )
    
    # Sort by objective
    if objective == "max_ev":
        valid_structures.sort(key=lambda s: s["ev"], reverse=True)
    elif objective == "max_ev_per_dollar":
        # Sort by EV per dollar risked (highest first)
        valid_structures.sort(key=lambda s: float(s["ev_per_dollar"]), reverse=True)
    elif objective == "max_sharpe":
        # Compute Sharpe-like ratio: EV / std
        for s in valid_structures:
            s["sharpe"] = s["ev"] / s["std"] if s["std"] > 0 else 0
        valid_structures.sort(key=lambda s: s.get("sharpe", 0), reverse=True)
    elif objective == "max_prob_profit":
        valid_structures.sort(key=lambda s: s["prob_profit"], reverse=True)
    else:
        logger.warning(f"Unknown objective '{objective}', defaulting to max_ev")
        valid_structures.sort(key=lambda s: s["ev"], reverse=True)
    
    # DEBUG: Print sort keys after sorting
    logger.info(f"After sorting by {objective}:")
    for i, s in enumerate(valid_structures):
        ev_per_dollar = s.get("ev_per_dollar", 0)
        ev_per_contract = s.get("ev", 0) * 100
        debit_per_contract = s.get("debit_per_contract", s.get("debit", 0) * 100)
        win_prob = s.get("prob_profit", 0)
        logger.info(
            f"  [AFTER] #{i}: ev_per_dollar={ev_per_dollar:.4f}, "
            f"ev_per_contract=${ev_per_contract:.2f}, "
            f"debit_per_contract=${debit_per_contract:.2f}, "
            f"win_prob={win_prob:.3f}"
        )
    
    return valid_structures


def rank_structures(structures: List[Dict], top_n: int = 3) -> List[Dict]:
    """
    Return top N structures with ranking.
    
    Args:
        structures: Sorted list of structures
        top_n: Number of top structures to return
        
    Returns:
        Top N structures with rank added
    """
    top_structures = structures[:top_n]
    
    for i, struct in enumerate(top_structures, 1):
        struct["rank"] = i
    
    return top_structures


def generate_summary(
    top_structures: List[Dict],
    oracle_data: Optional[Dict] = None
) -> str:
    """
    Generate human-readable summary of top structures.
    
    Args:
        top_structures: Top ranked structures
        oracle_data: Optional oracle probability data
        
    Returns:
        Markdown-formatted summary
    """
    lines = ["# Option Structure Recommendations\n"]
    
    if oracle_data:
        lines.append(f"**Market Event Probability**: {oracle_data.get('p_event', 'N/A'):.1%}\n")
        lines.append(f"**Market ID**: {oracle_data.get('market_id', 'N/A')}\n")
    
    lines.append("\n## Top Structures\n")
    
    for struct in top_structures:
        rank = struct.get("rank", "?")
        name = struct.get("template_name", "Unknown")
        underlier = struct.get("underlier", "?")
        expiry = struct.get("expiry", "?")
        ev = struct.get("ev", 0)
        max_loss = struct.get("max_loss", 0)
        prob_profit = struct.get("prob_profit", 0)
        
        lines.append(f"\n### #{rank}: {name}\n")
        lines.append(f"- **Underlier**: {underlier}\n")
        lines.append(f"- **Expiry**: {expiry}\n")
        lines.append(f"- **Expected Value**: ${ev:.2f}\n")
        lines.append(f"- **Max Loss**: ${abs(max_loss):.2f}\n")
        lines.append(f"- **Prob Profit**: {prob_profit:.1%}\n")
        
        # Percentiles
        if "percentiles" in struct:
            p = struct["percentiles"]
            lines.append(f"- **P5/P50/P95**: ${p['p05']:.2f} / ${p['p50']:.2f} / ${p['p95']:.2f}\n")
    
    return "".join(lines)
