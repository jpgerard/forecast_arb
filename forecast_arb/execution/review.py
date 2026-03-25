"""
Human-Readable Review Formatter

Generates detailed review summaries for order tickets.
Always outputs to both console and file.
"""

from typing import List, Dict, Optional, Any
from datetime import datetime


def format_review(
    run_id: str,
    decision: str,
    reason: str,
    p_external: Optional[float],
    p_implied: Optional[float],
    edge: Optional[float],
    confidence: Optional[float],
    tickets: List[Dict[str, Any]],
    caps: Dict[str, Any],
    mode: str = "unknown",
    config_hash: Optional[str] = None,
    submit_requested: bool = False,
    submit_blocked: bool = False,
    submit_block_reason: Optional[str] = None,
    p_external_source: Optional[str] = None,
    external_source_blocked: bool = False,
    gate_decision: Optional[Dict[str, Any]] = None,
    external_source_policy_skipped: bool = False
) -> str:
    """
    Format human-readable review summary.
    
    Args:
        run_id: Run identifier
        decision: Final decision (TRADE/NO_TRADE)
        reason: Reason for decision
        p_external: External probability (e.g., from Kalshi)
        p_implied: Implied probability from options
        edge: Edge value (p_external - p_implied)
        confidence: Confidence level (gate confidence)
        tickets: List of order tickets (dicts)
        caps: Caps applied (max_orders, max_debit_total)
        mode: Run mode (prod/dev/smoke)
        config_hash: Config checksum
        submit_requested: Whether submission was requested
        submit_blocked: Whether submission was blocked
        submit_block_reason: Reason submission was blocked (if any)
        p_external_source: Source of p_external (kalshi/fallback)
        external_source_blocked: Whether external source blocked trading
        gate_decision: Gate decision dict (optional, for detailed gate info)
        
    Returns:
        Formatted review string
    """
    lines = []
    
    # Header
    lines.append("=" * 80)
    lines.append("TRADE REVIEW SUMMARY")
    lines.append("=" * 80)
    lines.append("")
    
    # Run metadata
    lines.append(f"Run ID: {run_id}")
    lines.append(f"Mode: {mode.upper()}")
    if config_hash:
        lines.append(f"Config Hash: {config_hash}")
    lines.append(f"Timestamp: {datetime.utcnow().isoformat()}Z")
    lines.append("")
    
    # Market assessment
    lines.append("-" * 80)
    lines.append("MARKET ASSESSMENT")
    lines.append("-" * 80)
    
    if p_external is not None:
        lines.append(f"P(External): {p_external:.4f} ({p_external*100:.2f}%)")
    else:
        lines.append("P(External): N/A")
    
    if p_implied is not None:
        lines.append(f"P(Implied):  {p_implied:.4f} ({p_implied*100:.2f}%)")
    else:
        lines.append("P(Implied):  N/A")
    
    if edge is not None:
        edge_bps = edge * 10000
        lines.append(f"Edge:        {edge:.4f} ({edge_bps:+.1f} bps)")
    else:
        lines.append("Edge:        N/A")
    
    # Format confidence: handle None, 0.0, or convert from [0,1] to percentage
    if confidence is None or confidence == 0.0:
        lines.append("Confidence:  N/A")
    elif confidence <= 1.0:
        # Convert from [0,1] to percentage
        lines.append(f"Confidence:  {confidence * 100:.1f}%")
    else:
        # Already in percentage format
        lines.append(f"Confidence:  {confidence:.1f}%")
    
    lines.append("")
    
    # External Source Policy
    lines.append("-" * 80)
    lines.append("EXTERNAL SOURCE POLICY")
    lines.append("-" * 80)
    if external_source_policy_skipped:
        lines.append("Status: SKIPPED (edge gate blocked)")
    elif p_external_source:
        lines.append(f"Source: {p_external_source}")
        if external_source_blocked:
            lines.append(f"Allowed: False")
            lines.append(f"Policy: BLOCKED ({p_external_source.upper()})")
        else:
            lines.append(f"Allowed: True")
            lines.append(f"Policy: OK")
    else:
        lines.append("Source: N/A")
        lines.append("Allowed: N/A")
    lines.append("")
    
    # Edge Gate Decision
    lines.append("-" * 80)
    lines.append("EDGE GATE")
    lines.append("-" * 80)
    if gate_decision:
        gate_result = gate_decision.get("decision", "UNKNOWN")
        gate_reason = gate_decision.get("reason", "UNKNOWN")
        lines.append(f"Result: {gate_result}")
        lines.append(f"Reason: {gate_reason}")
        
        # Show confidence breakdown
        conf_gate = gate_decision.get("confidence_gate")
        conf_ext = gate_decision.get("confidence_external")
        conf_impl = gate_decision.get("confidence_implied")
        
        if conf_ext is not None:
            lines.append(f"Confidence (External): {conf_ext:.2f}")
        if conf_impl is not None:
            lines.append(f"Confidence (Implied): {conf_impl:.2f}")
        if conf_gate is not None:
            lines.append(f"Confidence (Gate): {conf_gate:.2f}")
    else:
        lines.append("Result: N/A")
        lines.append("Reason: N/A")
    lines.append("")
    
    # Structuring Decision
    lines.append("-" * 80)
    lines.append("STRUCTURING")
    lines.append("-" * 80)
    
    # Determine if structuring was skipped or ran
    if external_source_blocked:
        lines.append("Status: SKIPPED (external source policy blocked)")
    elif gate_decision and gate_decision.get("decision") == "NO_TRADE":
        lines.append(f"Status: SKIPPED (edge gate blocked)")
    elif not tickets:
        lines.append("Status: RAN (no candidates produced)")
    else:
        lines.append(f"Status: RAN ({len(tickets)} candidate(s) produced)")
    lines.append("")
    
    # Final Decision
    lines.append("-" * 80)
    lines.append("FINAL DECISION")
    lines.append("-" * 80)
    lines.append(f"Decision: {decision}")
    lines.append(f"Reason:   {reason}")
    lines.append("")
    
    # Order tickets
    lines.append("-" * 80)
    lines.append("ORDER TICKETS")
    lines.append("-" * 80)
    
    if not tickets:
        lines.append("No tickets generated (NO_TRADE)")
        lines.append("")
    else:
        lines.append(f"Total Tickets: {len(tickets)}")
        lines.append("")
        
        for i, ticket in enumerate(tickets, 1):
            lines.append(f"Ticket #{i}:")
            lines.append(f"  {_format_ticket_summary(ticket)}")
            
            # Include key metrics
            metadata = ticket.get("metadata", {})
            if metadata:
                if "ev_per_dollar" in metadata:
                    lines.append(f"  EV/$: {metadata['ev_per_dollar']:.3f}")
                if "prob_profit" in metadata:
                    lines.append(f"  P(Profit): {metadata['prob_profit']*100:.1f}%")
                
                # Liquidity stats if present
                if "bid_ask_spread_long" in metadata:
                    lines.append(f"  Long Put Spread: ${metadata['bid_ask_spread_long']:.2f}")
                if "bid_ask_spread_short" in metadata:
                    lines.append(f"  Short Put Spread: ${metadata['bid_ask_spread_short']:.2f}")
            
            lines.append("")
    
    # Caps and guardrails
    lines.append("-" * 80)
    lines.append("CAPS & GUARDRAILS")
    lines.append("-" * 80)
    
    max_orders = caps.get("max_orders")
    max_debit_total = caps.get("max_debit_total")
    
    if max_orders is not None:
        lines.append(f"Max Orders: {max_orders}")
        lines.append(f"  Applied: {len(tickets)} ticket(s)")
    
    if max_debit_total is not None:
        lines.append(f"Max Debit Total: ${max_debit_total:,.2f}")
        
        if tickets:
            total_debit = sum(_calculate_ticket_debit(t) for t in tickets)
            lines.append(f"  Applied: ${total_debit:,.2f}")
        else:
            lines.append(f"  Applied: $0.00")
    
    lines.append("")
    
    # Submission status
    lines.append("-" * 80)
    lines.append("SUBMISSION STATUS")
    lines.append("-" * 80)
    
    if submit_blocked:
        lines.append("SUBMISSION: BLOCKED")
        if submit_block_reason:
            lines.append(f"Reason: {submit_block_reason}")
    elif submit_requested:
        lines.append("SUBMISSION: ENABLED (will submit)")
        lines.append("⚠️  LIVE ORDERS WILL BE PLACED")
    else:
        lines.append("SUBMISSION: DISABLED (dry-run)")
        lines.append("No orders will be submitted to IBKR")
    
    lines.append("")
    lines.append("=" * 80)
    
    return "\n".join(lines)


def _format_ticket_summary(ticket: Dict[str, Any]) -> str:
    """
    Format a single ticket as a readable summary line.
    
    Example: "SPY 2026-02-27 450/440 Put Spread: BUY 450P / SELL 440P @ $12.50 x2 (MaxLoss=$2500 MaxGain=$1500)"
    
    Args:
        ticket: Order ticket dict
        
    Returns:
        Formatted summary string
    """
    symbol = ticket.get("symbol", "???")
    expiry = ticket.get("expiry", "????????")
    combo_type = ticket.get("combo_type", "UNKNOWN")
    legs = ticket.get("legs", [])
    limit_price = ticket.get("limit_price", 0)
    quantity = ticket.get("quantity", 0)
    
    # Format expiry as YYYY-MM-DD
    if len(expiry) == 8:
        expiry_formatted = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    else:
        expiry_formatted = expiry
    
    # Extract strikes for vertical spread
    if len(legs) >= 2:
        strikes = sorted([leg["strike"] for leg in legs], reverse=True)
        strike_str = f"{strikes[0]:.0f}/{strikes[1]:.0f}"
    else:
        strike_str = "???"
    
    # Format legs (e.g., "BUY 450P / SELL 440P")
    leg_strs = []
    for leg in legs:
        action = leg.get("action", "???")
        strike = leg.get("strike", 0)
        right = leg.get("right", "?")
        leg_strs.append(f"{action} {strike:.0f}{right}")
    
    legs_formatted = " / ".join(leg_strs)
    
    # Calculate max loss and max gain
    # Max loss = limit_price * quantity * 100 (for debit spread)
    max_loss = limit_price * quantity * 100
    
    # Max gain = (strike_width * 100 * quantity) - max_loss
    if len(legs) >= 2:
        strike_width = strikes[0] - strikes[1]
        max_gain = (strike_width * 100 * quantity) - max_loss
    else:
        max_gain = 0
    
    summary = (
        f"{symbol} {expiry_formatted} {strike_str} Put Spread: "
        f"{legs_formatted} @ ${limit_price:.2f} x{quantity} "
        f"(MaxLoss=${max_loss:.0f} MaxGain=${max_gain:.0f})"
    )
    
    return summary


def _calculate_ticket_debit(ticket: Dict[str, Any]) -> float:
    """
    Calculate total debit for a ticket.
    
    Args:
        ticket: Order ticket dict
        
    Returns:
        Total debit in dollars
    """
    limit_price = ticket.get("limit_price", 0)
    quantity = ticket.get("quantity", 0)
    
    return limit_price * quantity * 100
