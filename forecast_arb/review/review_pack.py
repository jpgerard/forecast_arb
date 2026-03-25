"""
Review Pack Generator

Generates human-readable review artifacts for manual decision support
when trading is blocked by edge gate or external source policy.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime


def render_review_pack(
    run_context: Dict[str, Any],
    gate_decision: Dict[str, Any],
    external_policy: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    p_implied_artifact: Optional[Dict[str, Any]] = None,
    campaign_risk: Optional[Dict[str, Any]] = None,
    live_quotes: Optional[Dict[str, Any]] = None
) -> str:
    """
    Render review pack for manual review.
    
    This is designed to be paste-friendly for ChatGPT or other LLM decision support.
    
    Args:
        run_context: Run metadata (run_id, run_dir, snapshot info, etc.)
        gate_decision: Edge gate decision dict
        external_policy: External source policy dict
        candidates: List of review candidate structures
        p_implied_artifact: P_implied calculation artifact (optional)
        campaign_risk: Campaign risk summary (optional)
        live_quotes: Live quote snapshots keyed by candidate ticker (optional)
        
    Returns:
        Markdown-formatted review pack
    """
    lines = []
    
    # Header
    lines.append("# Crash Venture Review Pack")
    lines.append("")
    lines.append(f"**Run ID:** `{run_context.get('run_id', 'N/A')}`")
    lines.append(f"**Run Directory:** `{run_context.get('run_dir', 'N/A')}`")
    lines.append(f"**Generated:** {datetime.utcnow().isoformat()}Z")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    # 1) Campaign Risk Summary
    if campaign_risk:
        lines.append("## 1. Campaign Risk Summary")
        lines.append("")
        lines.append(f"- **Open Positions:** {campaign_risk.get('open_positions', 0)}")
        lines.append(f"- **Capital Deployed:** ${campaign_risk.get('open_max_loss', 0):.2f}")
        
        # Handle None values for campaign_cap and remaining_capacity
        cap = campaign_risk.get('campaign_cap')
        remaining = campaign_risk.get('remaining_capacity')
        
        if cap is not None:
            lines.append(f"- **Campaign Cap:** ${cap:.2f}")
            if remaining is not None:
                lines.append(f"- **Remaining Capacity:** ${remaining:.2f}")
            
            # Calculate percentage used
            deployed = campaign_risk.get('open_max_loss', 0)
            if cap > 0:
                pct_used = (deployed / cap) * 100
                lines.append(f"- **Capacity Used:** {pct_used:.1f}%")
        else:
            lines.append(f"- **Campaign Cap:** Not configured")
            lines.append(f"- **Remaining Capacity:** N/A")
        
        lines.append("")
        lines.append("*Note: This is informational only. No persistent position tracking yet.*")
        lines.append("")
    
    # 2) Snapshot Summary
    lines.append("## 2. Snapshot Summary")
    lines.append("")
    snapshot = run_context.get("snapshot_metadata", {})
    lines.append(f"- **Underlier:** {snapshot.get('underlier', 'N/A')}")
    lines.append(f"- **Spot Price:** ${snapshot.get('spot', 0):.2f}")
    lines.append(f"- **Snapshot Time:** {snapshot.get('snapshot_time', 'N/A')}")
    lines.append(f"- **Expiry Used:** {run_context.get('expiry_used', 'N/A')}")
    lines.append(f"- **DTE:** {run_context.get('dte', 'N/A')} days")
    lines.append("")
    
    # 3) Event Definition (from EventSpec - Single Source of Truth)
    lines.append("## 3. Event Definition")
    lines.append("")
    event_spec = run_context.get("event_spec", {})
    
    # EventSpec fields are canonical - no recomputation
    moneyness = event_spec.get('moneyness', 0)
    threshold = event_spec.get('threshold', 0)
    spot = event_spec.get('spot', 0)
    expiry = event_spec.get('expiry', 'N/A')
    underlier = event_spec.get('underlier', 'N/A')
    
    lines.append(f"- **Event:** P({underlier} < ${threshold:.2f} at {expiry})")
    lines.append(f"- **Threshold:** ${threshold:.2f}")
    lines.append(f"- **Spot (at EventSpec creation):** ${spot:.2f}")
    lines.append(f"- **Event Moneyness (config):** {moneyness:.2%}")
    lines.append(f"- **Computed Threshold:** {threshold:.2f} = spot × (1 + moneyness) = {spot:.2f} × {1+moneyness:.4f}")
    
    # Validate consistency: recompute to check for drift
    expected_threshold = spot * (1 + moneyness) if spot > 0 else 0
    threshold_diff = abs(threshold - expected_threshold)
    
    if threshold_diff > 0.01:  # 1 cent tolerance
        lines.append(f"- **⚠️ MONEYNESS_MISMATCH:** Expected ${expected_threshold:.2f}, got ${threshold:.2f} (diff ${threshold_diff:.2f})")
    else:
        lines.append(f"- **✓ Threshold Consistency:** PASS (diff ${threshold_diff:.4f})")
    
    lines.append("")
    
    # 3) Probabilities
    lines.append("## 3. Probabilities")
    lines.append("")
    
    # External probability
    p_ext = gate_decision.get("p_external")
    p_ext_source = external_policy.get("source", "unknown")
    p_ext_conf = gate_decision.get("confidence_external", 0)
    
    lines.append(f"### External Probability (p_external)")
    lines.append(f"- **Value:** {p_ext:.4f} ({p_ext*100:.2f}%)" if p_ext is not None else "- **Value:** N/A")
    lines.append(f"- **Source:** {gate_decision.get('source', 'N/A')}")
    lines.append(f"- **Confidence:** {gate_decision.get('confidence_external', 0.0):.2f}")
    
    # Check for proxy metadata
    p_ext_metadata = gate_decision.get('p_external_metadata', {})
    if 'p_external_proxy' in p_ext_metadata:
        lines.append("")
        lines.append(f"#### ⚠️ Proxy Probability Available (NOT exact match)")
        proxy_val = p_ext_metadata.get('p_external_proxy')
        lines.append(f"- **Proxy Value:** {proxy_val:.4f} ({proxy_val*100:.2f}%)" if proxy_val is not None else "- **Proxy Value:** N/A")
        lines.append(f"- **Method:** {p_ext_metadata.get('proxy_method', 'N/A')}")
        lines.append(f"- **Series:** {p_ext_metadata.get('proxy_series', 'N/A')}")
        
        proxy_conf = p_ext_metadata.get('proxy_confidence')
        if proxy_conf is not None:
            conf_label = "LOW" if proxy_conf < 0.5 else "MODERATE"
            lines.append(f"- **Confidence:** {proxy_conf:.2f} ({conf_label})")
        
        proxy_ticker = p_ext_metadata.get('proxy_market_ticker')
        if proxy_ticker:
            lines.append(f"- **Market Ticker:** {proxy_ticker}")
        
        lines.append("")
        lines.append(f"**⚠️ IMPORTANT:** This is a proxy probability, NOT an exact Kalshi market match.")
        lines.append(f"Use with extreme caution. Not recommended for automated trading.")
    
    # Check for fallback metadata
    elif 'p_external_fallback' in p_ext_metadata:
        lines.append("")
        lines.append(f"#### Fallback Value (informational)")
        fallback_val = p_ext_metadata.get('p_external_fallback')
        lines.append(f"- **Fallback Value:** {fallback_val:.4f} ({fallback_val*100:.2f}%)" if fallback_val is not None else "- **Fallback Value:** N/A")
        lines.append(f"- **Note:** This is a conservative estimate, not from real market data")
    
    lines.append("")
    
    # Check for proxy metadata
    p_ext_metadata = gate_decision.get("p_external_metadata", {})
    if "p_external_proxy" in p_ext_metadata:
        proxy_prob = p_ext_metadata.get("p_external_proxy")
        proxy_method = p_ext_metadata.get("proxy_method", "unknown")
        proxy_series = p_ext_metadata.get("proxy_series", "unknown")
        proxy_conf = p_ext_metadata.get("proxy_confidence", 0)
        proxy_horizon = p_ext_metadata.get("proxy_horizon_days", 0)
        proxy_ticker = p_ext_metadata.get("proxy_market_ticker", "N/A")
        
        lines.append(f"#### ⚠️ Proxy Probability Available (NOT exact match)")
        lines.append(f"- **Proxy Value:** {proxy_prob:.4f} ({proxy_prob*100:.2f}%)")
        lines.append(f"- **Method:** {proxy_method}")
        lines.append(f"- **Series:** {proxy_series}")
        lines.append(f"- **Horizon:** {proxy_horizon} days")
        lines.append(f"- **Market Ticker:** {proxy_ticker}")
        lines.append(f"- **Confidence:** {proxy_conf:.2f} (LOW)")
        lines.append("")
        lines.append("**⚠️ IMPORTANT:** This is a proxy probability, NOT an exact Kalshi market match.")
        lines.append("It is estimated using yearly minimum data with hazard rate scaling.")
        lines.append("Use with extreme caution and heavy discounting. Not recommended for automated trading.")
        lines.append("")
    
    # Implied probability
    p_impl = gate_decision.get("p_implied")
    p_impl_conf = gate_decision.get("confidence_implied", 0)
    p_impl_method = "options_implied"
    
    lines.append(f"### Options-Implied Probability (p_implied)")
    if p_impl is not None:
        lines.append(f"- **Value:** {p_impl:.4f} ({p_impl*100:.2f}%)")
        lines.append(f"- **Method:** {p_impl_method}")
        lines.append(f"- **Confidence:** {p_impl_conf:.2f}")
    else:
        lines.append(f"- **Value:** N/A")
        lines.append(f"- **Reason:** Calculation failed or insufficient data")
    
    # ATM IV source details (if available)
    if p_implied_artifact:
        iv_source = p_implied_artifact.get("iv_source", {})
        if iv_source:
            lines.append("")
            lines.append(f"#### ATM IV Source Details")
            lines.append(f"- **Strike Used:** ${iv_source.get('strike', 0):.2f}")
            lines.append(f"- **Distance from Spot:** {iv_source.get('distance_from_spot', 0):.2%}")
            lines.append(f"- **Quote Quality:** {iv_source.get('quote_quality', 'UNKNOWN')}")
            lines.append(f"- **IV Value:** {iv_source.get('iv', 0):.3f}")
    
    lines.append("")
    
    # Edge
    edge = gate_decision.get("edge")
    lines.append(f"### Edge")
    if edge is not None:
        edge_bps = edge * 10000
        lines.append(f"- **Value:** {edge:.4f} ({edge_bps:+.1f} bps)")
        lines.append(f"- **Calculation:** p_external - p_implied = {p_ext:.4f} - {p_impl:.4f}" if (p_ext and p_impl) else "- **Calculation:** N/A")
    else:
        lines.append(f"- **Value:** N/A")
    lines.append("")
    
    # 4) Gate Result
    lines.append("## 4. Edge Gate Decision")
    lines.append("")
    gate_result = gate_decision.get("decision", "UNKNOWN")
    gate_reason = gate_decision.get("reason", "UNKNOWN")
    
    lines.append(f"- **Result:** `{gate_result}`")
    lines.append(f"- **Reason:** {gate_reason}")
    lines.append("")
    
    # Gate thresholds
    lines.append(f"### Gate Thresholds")
    lines.append(f"- **Min Edge:** {run_context.get('min_edge', 0):.2%}")
    lines.append(f"- **Min Confidence:** {run_context.get('min_confidence', 0):.2%}")
    lines.append("")
    
    # FIX #2: Fixed confidence breakdown
    lines.append(f"### Confidence Breakdown")
    lines.append(f"- **Confidence (External):** {p_ext_conf:.2f}")
    lines.append(f"- **Confidence (Implied):** {p_impl_conf:.2f}" if p_impl is not None else "- **Confidence (Implied):** N/A")
    
    # Compute gate confidence as min(external, implied)
    conf_gate = min(p_ext_conf, p_impl_conf) if p_impl is not None else p_ext_conf
    lines.append(f"- **Confidence Gate:** {conf_gate:.2f} (min of external/implied)")
    lines.append(f"- **Min Confidence Threshold:** {run_context.get('min_confidence', 0):.2f}")
    
    # Show result correspondence
    if conf_gate < run_context.get('min_confidence', 0):
        lines.append(f"- **Result:** NO_TRADE / LOW_CONFIDENCE ✓")
    else:
        lines.append(f"- **Result:** Confidence threshold met")
    
    lines.append("")
    
    # 5) External Source Policy
    lines.append("## 5. External Source Policy")
    lines.append("")
    policy_allowed = not external_policy.get("blocked", True)
    policy_result = external_policy.get("policy", "UNKNOWN")
    
    lines.append(f"- **Source:** {p_ext_source}")
    lines.append(f"- **Allowed:** {'Yes' if policy_allowed else 'No'}")
    lines.append(f"- **Policy Result:** {policy_result}")
    lines.append(f"- **Fallback Used:** {'Yes' if p_ext_source == 'fallback' else 'No'}")
    lines.append("")
    
    # 6) Top Candidates Table
    lines.append("## 6. Top Structure Candidates")
    lines.append("")
    
    if not candidates:
        lines.append("*No candidates generated (structuring did not run or no valid candidates found)*")
    else:
        lines.append(f"**Total Candidates:** {len(candidates)}")
        lines.append("")
        
        # Table header
        lines.append("| Rank | Expiry | Long/Short | Debit | Max Loss | Max Gain | EV | EV/$ | Warnings |")
        lines.append("|------|--------|------------|-------|----------|----------|----|----- |----------|")
        
        # Show top 5
        for i, cand in enumerate(candidates[:5], 1):
            expiry = cand.get("expiry", "N/A")
            strikes = cand.get("strikes", {})
            long_put = strikes.get("long_put", 0)
            short_put = strikes.get("short_put", 0)
            
            debit = cand.get("estimated_entry", {}).get("debit", 0)
            max_loss = cand.get("structure", {}).get("max_loss", 0)
            max_gain = cand.get("structure", {}).get("max_gain", 0)
            ev = cand.get("metrics", {}).get("ev", 0)
            ev_per_dollar = cand.get("metrics", {}).get("ev_per_dollar", 0)
            
            # Get warnings
            warnings_list = cand.get("notes", [])
            warnings_str = "; ".join(warnings_list[:2]) if warnings_list else "None"
            if len(warnings_list) > 2:
                warnings_str += f" (+{len(warnings_list)-2} more)"
            
            # Pricing quality indicator
            pricing_quality = cand.get("estimated_entry", {}).get("pricing_quality", "UNKNOWN")
            
            lines.append(
                f"| {i} | {expiry} | {long_put:.0f}/{short_put:.0f} | "
                f"${debit:.2f} | ${max_loss:.2f} | ${max_gain:.2f} | "
                f"${ev:.2f} | {ev_per_dollar:.3f} | {warnings_str} ({pricing_quality}) |"
            )
        
        lines.append("")
        
        # Add note about pricing quality
        lines.append("**Pricing Quality Legend:**")
        lines.append("- `EXECUTABLE`: Both legs have bid/ask quotes")
        lines.append("- `MID`: One or more legs using mid-price fallback")
        lines.append("- `MODEL`: One or more legs using Black-Scholes model fallback")
        lines.append("- `STALE`: Quotes may be stale or unreliable")
        lines.append("")
        
        # Live Quotes Section (if available)
        if live_quotes:
            lines.append("### Live Quote Snapshots (Decision-Time Pricing)")
            lines.append("")
            lines.append("*These are live quotes fetched at decision time for comparison with model prices.*")
            lines.append("")
            
            for cand in candidates[:5]:
                # Build ticker from candidate info
                expiry = cand.get("expiry", "")
                strikes_dict = cand.get("strikes", {})
                long_put = strikes_dict.get("long_put", 0)
                short_put = strikes_dict.get("short_put", 0)
                
                # Check if we have quotes for this candidate
                # The live_quotes dict is keyed by a ticker string
                # We need to find the matching entry
                quote_data = None
                for ticker, data in live_quotes.items():
                    if expiry in ticker:
                        quote_data = data
                        break
                
                if quote_data:
                    lines.append(f"#### Rank #{cand.get('rank', 0)}: {expiry} {long_put:.0f}/{short_put:.0f}")
                    lines.append("")
                    
                    # Show spread-level quote
                    spread_mid = quote_data.get('spread_mid')
                    spread_natural = quote_data.get('spread_natural')
                    
                    lines.append(f"**Spread (Synthetic):**")
                    if spread_mid is not None:
                        lines.append(f"- Mid: ${spread_mid * 100:.2f} per contract")
                    if spread_natural is not None:
                        lines.append(f"- Natural (ask-bid): ${spread_natural * 100:.2f} per contract")
                    lines.append("")
                    
                    # Show leg-level quotes
                    legs = quote_data.get('legs', [])
                    if len(legs) >= 2:
                        lines.append(f"**Leg Quotes:**")
                        
                        # Long leg (index 0)
                        long_leg = legs[0]
                        lines.append(f"- **Long {long_put:.0f}P:** bid=${long_leg.get('bid', 0)*100:.2f}, "
                                   f"mid=${long_leg.get('mid', 0)*100:.2f}, ask=${long_leg.get('ask', 0)*100:.2f}")
                        
                        # Short leg (index 1)
                        short_leg = legs[1]
                        lines.append(f"- **Short {short_put:.0f}P:** bid=${short_leg.get('bid', 0)*100:.2f}, "
                                   f"mid=${short_leg.get('mid', 0)*100:.2f}, ask=${short_leg.get('ask', 0)*100:.2f}")
                        lines.append("")
                    
                    # Show diagnostics/warnings
                    diagnostics = quote_data.get('diagnostics', {})
                    warnings = diagnostics.get('warnings', [])
                    if warnings:
                        lines.append(f"**Warnings:** {'; '.join(warnings)}")
                        lines.append("")
            
            lines.append("")
    
    # 7) What Changed (optional - skip for now to keep simple)
    # lines.append("## 7. What Changed vs Yesterday")
    # lines.append("")
    # lines.append("*(Not implemented - compare manually if needed)*")
    # lines.append("")
    
    # 8) JP Decision Checklist
    lines.append("## 7. JP Decision Checklist")
    lines.append("")
    lines.append("After reviewing the above data, consider:")
    lines.append("")
    lines.append("- [ ] **Do I agree with the gate decision?**")
    lines.append("  - Is the edge calculation reasonable?")
    lines.append("  - Is the implied probability confidence acceptable?")
    lines.append("")
    lines.append("- [ ] **Should I override the gate/policy block?**")
    lines.append("  - Is there information the model doesn't have?")
    lines.append("  - Are there market conditions that justify trading anyway?")
    lines.append("")
    lines.append("- [ ] **Which candidate (if any) should I trade?**")
    lines.append("  - Review EV/$ and risk/reward ratios")
    lines.append("  - Check pricing quality and warnings")
    lines.append("  - Verify strikes and expiry match my view")
    lines.append("")
    lines.append("- [ ] **What position size is appropriate?**")
    lines.append("  - Given the edge (or lack thereof)")
    lines.append("  - Given the pricing quality")
    lines.append("  - Given my risk tolerance")
    lines.append("")
    lines.append("**Next Step:** If proceeding with manual trade, fill out `decision_template.md`")
    lines.append("")
    
    # FIX #3: Add Manual Operator Section
    lines.append("## 8. Manual Operator Section")
    lines.append("")
    
    lines.append("### Suggested Entry Checklist")
    lines.append("")
    lines.append("- [ ] **Are both legs executable at bid/ask?**")
    lines.append("  - Check pricing quality column in candidate table above")
    lines.append("  - If not EXECUTABLE: Don't trade; if you override, size 1-lot only")
    lines.append("")
    
    lines.append("### Pre-Trade Exit Plan Template")
    lines.append("")
    lines.append("Before entering, document your exit plan:")
    lines.append("")
    lines.append("**Take-Profit Target:**")
    lines.append("- Value target: $_______ (or _____% of max gain)")
    lines.append("- Time-based: Exit after ____ days if profitable")
    lines.append("")
    lines.append("**Max Loss Accepted:**")
    lines.append("- Should equal the debit paid (limited risk)")
    lines.append("- Debit for selected candidate: $______")
    lines.append("- Confirm you accept this loss: [ ]")
    lines.append("")
    lines.append("**Time Stop:**")
    lines.append("- Exit ____ trading days pre-expiry (e.g., 10 DTE)")
    lines.append("- Calendar date: _________")
    lines.append("")
    
    lines.append("### Order Entry Format")
    lines.append("")
    lines.append("Use a single spread order (NOT separate legs):")
    lines.append("")
    lines.append("```")
    lines.append("BUY 1 SPY <expiry> <long_strike>/<short_strike> Put Vertical @ $<limit>")
    lines.append("```")
    lines.append("")
    lines.append("**Example (using Rank #1 from table above):**")
    if candidates:
        cand = candidates[0]
        expiry_example = cand.get("expiry", "YYYYMMDD")
        long_strike = cand.get("strikes", {}).get("long_put", 0)
        short_strike = cand.get("strikes", {}).get("short_put", 0)
        debit_example = cand.get("estimated_entry", {}).get("debit", 0)
        
        lines.append("```")
        lines.append(f"BUY 1 SPY {expiry_example} {long_strike:.0f}/{short_strike:.0f} Put Vertical @ ${debit_example:.2f}")
        lines.append("```")
    else:
        lines.append("```")
        lines.append("BUY 1 SPY 20260228 450/440 Put Vertical @ $12.50")
        lines.append("```")
    lines.append("")
    
    # Footer
    lines.append("---")
    lines.append("")
    lines.append("*This review pack was generated by the crash_venture_v1 engine in REVIEW-ONLY mode.*")
    lines.append("*Structures shown are for review purposes only and are NOT executable orders.*")
    lines.append("")
    
    return "\n".join(lines)


def render_decision_template() -> str:
    """
    Render decision template for JP to fill out after review.
    
    Returns:
        Markdown-formatted decision template
    """
    lines = []
    
    lines.append("# Manual Decision Template")
    lines.append("")
    lines.append(f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d')}")
    lines.append(f"**Time:** {datetime.utcnow().strftime('%H:%M:%S')} UTC")
    lines.append("")
    lines.append("---")
    lines.append("")
    
    lines.append("## Decision")
    lines.append("")
    lines.append("**TRADE?** (yes/no): _______________")
    lines.append("")
    lines.append("If YES, proceed below. If NO, explain reason and stop.")
    lines.append("")
    lines.append("**Reason if NO:**")
    lines.append("```")
    lines.append("")
    lines.append("```")
    lines.append("")
    
    lines.append("---")
    lines.append("")
    
    lines.append("## Trade Details (if TRADE = yes)")
    lines.append("")
    lines.append("**Candidate Selected:**")
    lines.append("- Rank/ID: _______________")
    lines.append("- Expiry: _______________")
    lines.append("- Long Put Strike: $_______________")
    lines.append("- Short Put Strike: $_______________")
    lines.append("")
    
    lines.append("**Entry Pricing:**")
    lines.append("- Limit Price (debit per spread): $_______________")
    lines.append("- Source (live quote / manual adjustment): _______________")
    lines.append("")
    
    lines.append("**Position Sizing:**")
    lines.append("- Number of Spreads: _______________")
    lines.append("- Total Debit (# spreads × limit price × 100): $_______________")
    lines.append("- Max Loss (= Total Debit): $_______________")
    lines.append("")
    
    lines.append("**Risk Confirmation:**")
    lines.append("- [ ] Total debit is within my risk tolerance")
    lines.append("- [ ] I understand this is a manual override of automated blocks")
    lines.append("- [ ] I have reviewed pricing quality and warnings")
    lines.append("")
    
    lines.append("**Notes / Reasoning:**")
    lines.append("```")
    lines.append("")
    lines.append("")
    lines.append("```")
    lines.append("")
    
    lines.append("---")
    lines.append("")
    
    lines.append("## Execution Confirmation")
    lines.append("")
    lines.append("**Order Placed?** (yes/no): _______________")
    lines.append("")
    lines.append("**Order ID / Confirmation:** _______________")
    lines.append("")
    lines.append("**Actual Fill Price:** $_______________")
    lines.append("")
    lines.append("**Fill Time:** _______________")
    lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append("*Keep this template for audit trail and post-trade review.*")
    lines.append("")
    
    return "\n".join(lines)
