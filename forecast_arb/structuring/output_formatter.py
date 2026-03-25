"""
Output formatter for option structures with deterministic, clean formatting.
"""

import json
from typing import Dict, List
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def validate_structure_output(structure: Dict) -> bool:
    """
    Validate that structure has all required fields for output.
    
    Args:
        structure: Structure dict to validate
        
    Returns:
        True if valid
        
    Raises:
        ValueError: If structure is missing required fields
    """
    required_fields = [
        "expiry", "premium", "max_loss", "max_gain",
        "ev", "underlier", "template_name"
    ]
    
    for field in required_fields:
        if field not in structure:
            raise ValueError(f"Structure missing required field: {field}")
    
    # Check that we have legs with strikes
    if "legs" not in structure or not structure["legs"]:
        raise ValueError("Structure missing legs")
    
    for leg in structure["legs"]:
        if "strike" not in leg:
            raise ValueError("Leg missing strike")
    
    return True


def assert_structure_sanity(
    structure: Dict,
    max_loss_usd: float,
    snapshot: Dict = None
) -> None:
    """
    Assert sanity checks before final output.
    
    Args:
        structure: Structure to validate
        max_loss_usd: Maximum allowed loss per contract (USD)
        snapshot: Optional options snapshot for validation
        
    Raises:
        AssertionError: If any sanity check fails
    """
    # Assert max loss constraint (convert per-share to per-contract)
    multiplier = structure.get("multiplier", 100)
    max_loss_per_share = structure["max_loss"]
    max_loss_per_contract = max_loss_per_share * multiplier
    
    assert max_loss_per_contract <= max_loss_usd, \
        f"Max loss ${max_loss_per_contract:.2f} exceeds limit ${max_loss_usd:.2f}"
    
    # Assert breakeven is not null
    assert structure.get("breakeven") is not None, "Breakeven must not be null"
    assert structure["breakeven"] > 0, f"Invalid breakeven: {structure['breakeven']}"
    
    # Assert bid/ask validity if available
    for leg in structure.get("legs", []):
        if "bid" in leg and "ask" in leg:
            assert leg["bid"] > 0, f"Invalid bid: {leg['bid']} for strike {leg['strike']}"
            assert leg["ask"] > leg["bid"], \
                f"Ask {leg['ask']} not > bid {leg['bid']} for strike {leg['strike']}"
    
    # Assert EV calculation exists
    assert "ev" in structure, "Structure missing EV calculation"
    assert "std" in structure, "Structure missing std calculation"
    
    # Assert strikes are valid
    for leg in structure["legs"]:
        assert leg["strike"] > 0, f"Invalid strike: {leg['strike']}"
    
    # Assert debit is positive
    assert structure.get("debit", 0) > 0, "Debit must be positive"
    
    logger.info(f"Sanity checks passed for {structure['template_name']}")


def get_reason_selected(structure: Dict, rank: int, objective: str) -> str:
    """
    Generate deterministic reason_selected text (no LLM).
    
    Args:
        structure: Structure dict
        rank: Rank (1, 2, 3, etc.)
        objective: Optimization objective used
        
    Returns:
        Reason string (1-2 sentences)
    """
    # CRITICAL: Use EV per contract, not per-share
    # EV from evaluator is per-share, so multiply by 100 for per-contract
    multiplier = structure.get("multiplier", 100)
    ev_per_contract = structure.get("ev", 0) * multiplier
    ev_per_dollar = structure.get("ev_per_dollar", 0)
    prob_profit = structure.get("prob_profit", 0)
    
    if rank == 1:
        if objective == "max_ev_per_dollar":
            return f"Highest EV/dollar ratio ({ev_per_dollar:.3f}), offering ${ev_per_contract:.2f} expected value per contract with {prob_profit:.1%} win probability."
        elif objective == "max_ev":
            return f"Highest expected value (${ev_per_contract:.2f} per contract) among candidates, with {prob_profit:.1%} win probability."
        else:
            return f"Top-ranked structure by {objective} optimization criterion."
    elif rank == 2:
        return f"Second-best alternative with ${ev_per_contract:.2f} EV per contract and {prob_profit:.1%} win probability."
    elif rank == 3:
        return f"Third option with ${ev_per_contract:.2f} EV per contract, balancing risk and return."
    else:
        return f"Alternative structure ranked #{rank} with ${ev_per_contract:.2f} expected value per contract."


def format_structure_output(structure: Dict) -> Dict:
    """
    Format structure with exactly the required output fields.
    Outputs per-contract values (USD).
    
    Args:
        structure: Evaluated structure dict (per-share values)
        
    Returns:
        Dict with clean output format (per-contract values)
    """
    multiplier = structure.get("multiplier", 100)
    
    # Extract strikes
    put_legs = [leg for leg in structure["legs"] if leg["type"] == "put"]
    call_legs = [leg for leg in structure["legs"] if leg["type"] == "call"]
    
    # For put spreads
    long_put = None
    short_put = None
    if put_legs:
        long_legs = [leg for leg in put_legs if leg["side"] == "long"]
        short_legs = [leg for leg in put_legs if leg["side"] == "short"]
        long_put = long_legs[0]["strike"] if long_legs else None
        short_put = short_legs[0]["strike"] if short_legs else None
    
    # Check if per-contract fields already exist (from snapshot mode)
    # If not, compute from per-share values
    if "debit_per_contract" in structure:
        # Already have per-contract values (from snapshot mode)
        debit_per_contract = structure["debit_per_contract"]
        max_loss_per_contract = structure["max_loss_per_contract"]
        max_gain_per_contract = structure["max_gain_per_contract"]
        ev_per_contract = structure.get("ev", 0) * multiplier  # EV is always per-share from evaluator
    else:
        # Compute from per-share values (legacy mode)
        debit_per_contract = abs(structure.get("debit", 0) * multiplier)
        max_loss_per_contract = abs(structure.get("max_loss", 0) * multiplier)
        max_gain_per_contract = abs(structure.get("max_gain", 0) * multiplier)
        ev_per_contract = structure.get("ev", 0) * multiplier
    
    # Calculate EV per dollar if not already present
    # CRITICAL: ev_per_dollar = ev_per_contract / debit_per_contract
    # Both must be floats in USD per-contract
    if "ev_per_dollar" not in structure:
        # Guard: debit_per_contract must be positive
        if debit_per_contract > 0:
            # Ensure both values are floats
            ev_per_dollar = float(ev_per_contract) / float(debit_per_contract)
        else:
            # Invalid structure - set to None for exclusion from ranking
            logger.warning(f"Invalid debit_per_contract={debit_per_contract} <= 0, setting ev_per_dollar=None")
            ev_per_dollar = None
        structure["ev_per_dollar"] = ev_per_dollar
    else:
        ev_per_dollar = structure["ev_per_dollar"]
    
    return {
        "expiry": structure["expiry"],
        "strikes": {
            "long_put": long_put,
            "short_put": short_put,
        },
        "debit_per_contract": debit_per_contract,       # USD, positive
        "max_loss_per_contract": max_loss_per_contract, # USD, positive
        "max_gain_per_contract": max_gain_per_contract, # USD, positive
        "breakeven": structure.get("breakeven"),        # Price level
        "ev_per_contract": ev_per_contract,             # USD
        "ev_per_dollar": ev_per_dollar,                 # Ratio
        "assumed_p_event": structure.get("assumed_p_event", None),
        "spot_used": structure.get("spot_used", None),
        "atm_iv_used": structure.get("atm_iv_used", None),
        "reason_selected": structure.get("reason_selected", ""),
        "rank": structure.get("rank", None),
        "underlier": structure["underlier"],
        "template_name": structure["template_name"]
    }


def write_structures_json(structures: List[Dict], output_path: Path) -> None:
    """
    Write structures to JSON file (machine-readable).
    
    Args:
        structures: List of formatted structure dicts
        output_path: Path to output JSON file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    output = {
        "n_structures": len(structures),
        "structures": structures
    }
    
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    
    logger.info(f"Wrote {len(structures)} structures to {output_path}")


def write_summary_md(structures: List[Dict], output_path: Path, metadata: Dict = None) -> None:
    """
    Write trade summary in Markdown (human-readable, trade-ticket style).
    
    Args:
        structures: List of formatted structure dicts
        output_path: Path to output Markdown file
        metadata: Optional metadata (run_id, p_event, etc.)
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    lines = ["# Crash Venture v1 - Trade Recommendations\n\n"]
    
    # Add EV interpretation warning
    lines.append("## ⚠️ IMPORTANT: EV Metrics Interpretation\n\n")
    lines.append("**The EV (Expected Value) and EV/$ metrics shown are RANKING SCORES, not actual expected returns.**\n\n")
    lines.append("These metrics are based on:\n")
    lines.append("- Monte Carlo simulation with assumed crash probabilities\n")
    lines.append("- Simplified market assumptions (may not reflect real conditions)\n")
    lines.append("- Single-expiry analysis without hedging dynamics\n\n")
    lines.append("**Use these for relative comparison between candidates, NOT as return forecasts.**\n")
    lines.append("Real-world returns will likely differ significantly. Conduct independent analysis before trading.\n\n")
    lines.append("---\n\n")
    
    if metadata:
        lines.append("## Run Metadata\n\n")
        lines.append(f"- **Run ID**: {metadata.get('run_id', 'N/A')}\n")
        lines.append(f"- **Campaign**: {metadata.get('campaign', 'N/A')}\n")
        lines.append(f"- **Event Probability**: {metadata.get('p_event', 'N/A')}\n")
        lines.append(f"- **Underlier**: {metadata.get('underlier', 'SPY')}\n")
        lines.append(f"- **Spot Price**: ${metadata.get('spot_used', 0):.2f}\n\n")
    
    lines.append(f"## Recommended Structures ({len(structures)})\n\n")
    
    for struct in structures:
        rank = struct.get("rank", "?")
        name = struct.get("template_name", "Unknown")
        
        lines.append(f"### Trade #{rank}: {name.replace('_', ' ').title()}\n\n")
        lines.append(f"**{struct.get('reason_selected', '')}**\n\n")
        
        lines.append("#### Trade Details\n\n")
        lines.append(f"- **Expiry**: {struct.get('expiry', 'N/A')}\n")
        
        strikes = struct.get("strikes", {})
        if strikes.get("long_put"):
            lines.append(f"- **Long Put Strike**: ${strikes['long_put']:.2f}\n")
        if strikes.get("short_put"):
            lines.append(f"- **Short Put Strike**: ${strikes['short_put']:.2f}\n")
        
        lines.append(f"- **Net Debit**: ${struct.get('debit_per_contract', 0):.2f}\n")
        lines.append(f"- **Max Loss**: ${abs(struct.get('max_loss_per_contract', 0)):.2f}\n")
        lines.append(f"- **Max Gain**: ${struct.get('max_gain_per_contract', 0):.2f}\n")
        
        if struct.get("breakeven"):
            lines.append(f"- **Breakeven**: ${struct['breakeven']:.2f}\n")
        
        lines.append(f"\n#### Expected Outcomes\n\n")
        lines.append(f"- **Expected Value**: ${struct.get('ev_per_contract', 0):.2f}\n")
        lines.append(f"- **EV per Dollar**: {struct.get('ev_per_dollar', 0):.3f}\n")
        
        lines.append("\n---\n\n")
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    
    logger.info(f"Wrote summary to {output_path}")


def format_dry_run_ticket(structure: Dict) -> str:
    """
    Format structure as IBKR-style ticket (text-only, no execution).
    
    Args:
        structure: Structure dict (may have per-share or per-contract values)
        
    Returns:
        Formatted ticket string
    """
    multiplier = structure.get("multiplier", 100)
    
    # Use per-contract values if available, otherwise compute from per-share
    if "debit_per_contract" in structure:
        debit_per_contract = structure["debit_per_contract"]
        max_loss_per_contract = structure["max_loss_per_contract"]
        max_gain_per_contract = structure["max_gain_per_contract"]
        ev_per_contract = structure.get("ev_per_contract", structure.get("ev", 0) * multiplier)
    else:
        debit_per_contract = structure.get("debit", 0) * multiplier
        max_loss_per_contract = structure.get("max_loss", 0) * multiplier
        max_gain_per_contract = structure.get("max_gain", 0) * multiplier
        ev_per_contract = structure.get("ev", 0) * multiplier
    
    lines = []
    lines.append("=" * 60)
    lines.append(f"TRADE TICKET - {structure['template_name'].upper()}")
    lines.append("=" * 60)
    lines.append("WARNING: EV is a ranking score, not an actual return forecast")
    lines.append("=" * 60)
    lines.append(f"Underlier: {structure['underlier']}")
    lines.append(f"Expiry: {structure['expiry']}")
    lines.append("")
    lines.append("ORDER: BUY 1 VERTICAL PUT SPREAD")
    lines.append("")
    
    for leg in structure.get("legs", []):
        action = "BUY" if leg["side"] == "long" else "SELL"
        opt_type = leg["type"].upper()
        strike = leg["strike"]
        lines.append(f"  {action} 1 {opt_type} @ ${strike:.2f}")
    
    lines.append("")
    lines.append(f"Net Debit per Contract: ${debit_per_contract:.2f}")
    lines.append(f"Max Risk per Contract: ${max_loss_per_contract:.2f}")
    lines.append(f"Max Profit per Contract: ${max_gain_per_contract:.2f}")
    
    # Breakeven may be at top level or in nested dict
    breakeven = structure.get("breakeven")
    if breakeven:
        lines.append(f"Breakeven: ${breakeven:.2f}")
    
    lines.append(f"Expected Value per Contract: ${ev_per_contract:.2f}")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def write_dry_run_tickets(structures: List[Dict], output_path: Path) -> None:
    """
    Write all dry-run tickets to text file.
    
    Args:
        structures: List of structure dicts
        output_path: Path to output text file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    tickets = []
    for struct in structures:
        ticket = format_dry_run_ticket(struct)
        tickets.append(ticket)
    
    with open(output_path, "w") as f:
        f.write("\n\n".join(tickets))
    
    logger.info(f"Wrote {len(tickets)} dry-run tickets to {output_path}")
