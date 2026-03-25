"""
Campaign Selector - Portfolio-aware candidate selection with hard governors.

Applies portfolio constraints and deterministically selects 0-2 recommended trades.

Convention: premium_usd = debit_per_contract * qty (no ×100)
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional, Set
from collections import defaultdict
from dataclasses import dataclass

from ..portfolio.positions_view import load_positions_view, compute_premium_usd


logger = logging.getLogger(__name__)


@dataclass
class SelectionResult:
    """Result of portfolio-aware selection."""
    selected: List[Dict[str, Any]]
    rejected: List[Dict[str, Any]]
    reasons: Dict[str, Any]
    portfolio_state: Dict[str, Any]


def compute_candidate_premium_usd(candidate: Dict[str, Any], qty: int = 1) -> float:
    """
    Compute premium in USD for a candidate.
    
    Convention: premium_usd = debit_per_contract * qty (no ×100)
    
    Args:
        candidate: Candidate dict with debit_per_contract
        qty: Quantity (default: 1)
        
    Returns:
        Premium in USD
    """
    debit = candidate.get("debit_per_contract", 0.0)
    return debit * qty


def compute_robustness_score(candidate: Dict[str, Any]) -> tuple[float, List[str]]:
    """
    Compute robustness multiplier and flags for a candidate.
    
    Penalizes candidates with fragile probability sources:
    - fallback p_used_src: 0.5x multiplier
    - NO_MARKET / BLOCKED / AUTH_FAIL p_ext_status: 0.7x multiplier
    - not representable: 0.0x (already filtered separately)
    
    Args:
        candidate: Candidate dict with p_used_src, p_ext_status
        
    Returns:
        Tuple of (robustness_multiplier, flags_list)
    """
    robustness = 1.0
    flags = []
    
    # Check representability (should be filtered before this, but enforce here)
    if not candidate.get("representable", False):
        return 0.0, ["NOT_REPRESENTABLE"]
    
    # Check p_used_src
    p_used_src = candidate.get("p_used_src")
    if p_used_src == "fallback":
        robustness *= 0.5
        flags.append("P_FALLBACK")
    
    # Check p_ext_status
    p_ext_status = candidate.get("p_ext_status")
    if p_ext_status in ["NO_MARKET", "BLOCKED", "AUTH_FAIL", "MISSING"]:
        robustness *= 0.7
        flags.append(f"P_EXT_{p_ext_status}")
    
    return robustness, flags


def select_candidates(
    candidates_flat: List[Dict[str, Any]],
    governors: Dict[str, Any],
    positions_view: Dict[str, Any],
    qty: int = 1,
    scoring_method: str = "ev_per_dollar"
) -> SelectionResult:
    """
    Select 0-2 recommended candidates subject to portfolio governors.
    
    PHASE 3 PATCH: Uses ONLY canonical EV fields and applies robustness penalty.
    
    Governor rules:
    1. Filter representable=True only
    2. Daily governors on NEW selections:
       - Sum of new premium <= daily_premium_cap_usd
       - Cluster cap: <= cluster_cap_per_day per cluster_id
    3. Open exposure governors (existing + new):
       - open_count_by_regime[regime] <= max_open_positions_by_regime[regime]
       - open_premium_by_regime[regime] + new_premium <= premium_at_risk_caps_usd[regime]
       - open_premium_total + new_premium_total <= premium_at_risk_caps_usd[total]
    4. Robustness penalty:
       - fallback p_used_src: 0.5x score multiplier
       - NO_MARKET / BLOCKED / AUTH_FAIL p_ext_status: 0.7x multiplier
    5. Selection: greedy pick by (canonical_ev * robustness) desc, stop at max_trades_per_day
    
    Args:
        candidates_flat: List of flat candidate dicts
        governors: Governor config dict
        positions_view: Portfolio positions view from positions_view.py
        qty: Quantity per trade (default: 1)
        scoring_method: Scoring field (default: "ev_per_dollar" - MUST be canonical)
        
    Returns:
        SelectionResult with selected, rejected, reasons
    """
    logger.info("=" * 80)
    logger.info("PORTFOLIO-AWARE CANDIDATE SELECTION")
    logger.info("=" * 80)
    
    # Extract governor parameters
    daily_premium_cap_usd = governors.get("daily_premium_cap_usd", 1250.0)
    cluster_cap_per_day = governors.get("cluster_cap_per_day", 1)
    max_open_positions_by_regime = governors.get("max_open_positions_by_regime", {})
    premium_at_risk_caps_usd = governors.get("premium_at_risk_caps_usd", {})
    max_trades_per_day = governors.get("max_trades_per_day", 2)  # From selection config
    
    # Extract portfolio state
    open_positions = positions_view["open_positions"]
    open_premium_by_regime = positions_view["open_premium_by_regime"]
    open_premium_total = positions_view["open_premium_total"]
    open_count_by_regime = positions_view["open_count_by_regime"]
    open_clusters = positions_view["open_clusters"]
    
    logger.info(f"Portfolio State:")
    logger.info(f"  Open positions: {len(open_positions)}")
    logger.info(f"  Open premium total: ${open_premium_total:.2f}")
    logger.info(f"  Open by regime: {open_count_by_regime}")
    logger.info(f"  Open clusters: {open_clusters}")
    logger.info("")
    
    logger.info(f"Governors:")
    logger.info(f"  Daily premium cap: ${daily_premium_cap_usd:.2f}")
    logger.info(f"  Cluster cap per day: {cluster_cap_per_day}")
    logger.info(f"  Max trades per day: {max_trades_per_day}")
    logger.info(f"  Max open by regime: {max_open_positions_by_regime}")
    logger.info(f"  Premium caps by regime: {premium_at_risk_caps_usd}")
    logger.info("")
    
    # RULE 1: Filter representable candidates only
    representable_candidates = [
        c for c in candidates_flat 
        if c.get("representable", False) == True
    ]
    
    logger.info(f"Candidate Pool:")
    logger.info(f"  Total candidates: {len(candidates_flat)}")
    logger.info(f"  Representable: {len(representable_candidates)}")
    logger.info(f"  Filtered out (not representable): {len(candidates_flat) - len(representable_candidates)}")
    logger.info("")
    
    if not representable_candidates:
        logger.warning("⚠️  No representable candidates available")
        # TASK 4: Return structured reasons even when no representable candidates
        reasons = {
            "total_candidates": len(candidates_flat),
            "representable_candidates": 0,
            "selected_count": 0,
            "rejected_count": 0,
            "new_premium_total": 0.0,
            "no_representable_candidates": True,
            "robustness_stats": {
                "external_count": 0,
                "implied_count": 0,
                "fallback_count": 0,
                "no_market_count": 0,
            },
            "rejection_reasons": {}
        }
        return SelectionResult(
            selected=[],
            rejected=[],
            reasons=reasons,
            portfolio_state=positions_view
        )
    
    # RULE 1.5: Compute robustness scores and validate canonical EV exists
    candidates_with_robustness = []
    for candidate in representable_candidates:
        # CRITICAL: Validate canonical EV field exists (no silent defaults)
        canonical_ev = candidate.get(scoring_method)
        if canonical_ev is None:
            logger.error(
                f"❌ Candidate {candidate.get('candidate_id')} missing canonical field '{scoring_method}'"
            )
            raise ValueError(
                f"Candidate {candidate.get('candidate_id')} missing required canonical field '{scoring_method}'. "
                f"Available keys: {list(candidate.keys())}"
            )
        
        # Compute robustness
        robustness, flags = compute_robustness_score(candidate)
        
        # Add to candidate dict for audit trail
        candidate["robustness"] = robustness
        candidate["robustness_flags"] = flags
        
        candidates_with_robustness.append(candidate)
    
    # RULE 4: Sort by (canonical_score * robustness) desc (deterministic)
    # Tie-breaking: higher p_profit, then lower premium
    def sort_key(candidate):
        base_score = candidate.get(scoring_method, 0.0)
        robustness = candidate.get("robustness", 1.0)
        adjusted_score = base_score * robustness
        
        p_profit = candidate.get("p_profit", candidate.get("prob_profit", 0.0))
        premium = compute_candidate_premium_usd(candidate, qty)
        
        return (-adjusted_score, -p_profit, premium)  # Descending score/prob, ascending premium
    
    sorted_candidates = sorted(candidates_with_robustness, key=sort_key)
    
    # TASK 2: PRINT PRE-GOVERNOR RANKING TABLE (Top N by score)
    logger.info("=" * 80)
    logger.info("PRE-GOVERNOR RANKING (Top 5 by score)")
    logger.info("=" * 80)
    logger.info("")
    logger.info(f"{'Rank':<6} {'Underlier':<10} {'Regime':<10} {'Expiry':<12} {'Strikes':<18} "
                f"{'EV/$':<8} {'Robust':<8} {'Score':<8} {'P_Used':<8} {'P_Src':<12} {'P_Ext_Status':<12}")
    logger.info("-" * 120)
    
    for i, candidate in enumerate(sorted_candidates[:5]):
        base_score = candidate.get(scoring_method, 0.0)
        robustness = candidate.get("robustness", 1.0)
        adjusted_score = base_score * robustness
        
        underlier = candidate.get("underlier", "UNKNOWN")
        regime = candidate.get("regime", "unknown")
        expiry = candidate.get("expiry", "N/A")
        long_strike = candidate.get("long_strike", 0)
        short_strike = candidate.get("short_strike", 0)
        strikes_str = f"{long_strike:.0f}/{short_strike:.0f}"
        
        p_used = candidate.get("p_used", 0.0)
        p_used_src = candidate.get("p_used_src", "unknown")
        p_ext_status = candidate.get("p_ext_status", "UNKNOWN")
        
        logger.info(f"{i+1:<6} {underlier:<10} {regime:<10} {expiry:<12} {strikes_str:<18} "
                   f"{base_score:<8.3f} {robustness:<8.2f} {adjusted_score:<8.3f} "
                   f"{p_used:<8.3f} {p_used_src:<12} {p_ext_status:<12}")
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("")
    
    # Greedy selection with governor checks
    selected = []
    rejected = []
    
    # Track daily usage (incremental from NEW selections only)
    new_premium_total = 0.0
    new_premium_by_regime = defaultdict(float)
    new_clusters_used: Set[str] = set()
    
    logger.info("Greedy Selection:")
    logger.info("-" * 80)
    
    for candidate in sorted_candidates:
        candidate_id = candidate.get("candidate_id", "UNKNOWN")
        regime = candidate.get("regime", "unknown")
        cluster_id = candidate.get("cluster_id", "UNKNOWN")
        candidate_premium = compute_candidate_premium_usd(candidate, qty)
        
        # Check if we've hit max_trades_per_day
        if len(selected) >= max_trades_per_day:
            rejected.append({
                "candidate": candidate,
                "reason": f"MAX_TRADES_PER_DAY_REACHED ({max_trades_per_day})",
                "blocked_by": "max_trades_per_day"
            })
            continue
        
        # Check daily premium cap
        if new_premium_total + candidate_premium > daily_premium_cap_usd:
            rejected.append({
                "candidate": candidate,
                "reason": f"DAILY_PREMIUM_CAP (would be ${new_premium_total + candidate_premium:.2f} > ${daily_premium_cap_usd:.2f})",
                "blocked_by": "daily_premium_cap"
            })
            logger.info(f"  ✗ {candidate_id}: DAILY_PREMIUM_CAP (${new_premium_total + candidate_premium:.2f} > ${daily_premium_cap_usd:.2f})")
            continue
        
        # Check cluster cap (only NEW selections today)
        if cluster_id in new_clusters_used:
            # This cluster already has a selection today
            cluster_count = sum(1 for c in selected if c["cluster_id"] == cluster_id)
            if cluster_count >= cluster_cap_per_day:
                rejected.append({
                    "candidate": candidate,
                    "reason": f"CLUSTER_CAP ({cluster_id} already has {cluster_count} trade(s) today, cap={cluster_cap_per_day})",
                    "blocked_by": "cluster_cap"
                })
                logger.info(f"  ✗ {candidate_id}: CLUSTER_CAP ({cluster_id} maxed at {cluster_cap_per_day})")
                continue
        
        # Check open position count by regime
        current_regime_count = open_count_by_regime.get(regime, 0)
        new_regime_count = sum(1 for c in selected if c["regime"] == regime)
        total_regime_count = current_regime_count + new_regime_count + 1  # +1 for this candidate
        
        max_regime_positions = max_open_positions_by_regime.get(regime, float('inf'))
        
        if total_regime_count > max_regime_positions:
            rejected.append({
                "candidate": candidate,
                "reason": f"REGIME_POSITION_CAP ({regime}: {total_regime_count} > {max_regime_positions})",
                "blocked_by": "regime_position_cap"
            })
            logger.info(f"  ✗ {candidate_id}: REGIME_POSITION_CAP ({regime}: {total_regime_count} > {max_regime_positions})")
            continue
        
        # Check premium at risk cap by regime
        current_regime_premium = open_premium_by_regime.get(regime, 0.0)
        new_regime_premium = new_premium_by_regime[regime]
        total_regime_premium = current_regime_premium + new_regime_premium + candidate_premium
        
        regime_premium_cap = premium_at_risk_caps_usd.get(regime, float('inf'))
        
        if total_regime_premium > regime_premium_cap:
            rejected.append({
                "candidate": candidate,
                "reason": f"REGIME_PREMIUM_CAP ({regime}: ${total_regime_premium:.2f} > ${regime_premium_cap:.2f})",
                "blocked_by": "regime_premium_cap"
            })
            logger.info(f"  ✗ {candidate_id}: REGIME_PREMIUM_CAP ({regime}: ${total_regime_premium:.2f} > ${regime_premium_cap:.2f})")
            continue
        
        # Check total premium at risk cap
        total_premium_cap = premium_at_risk_caps_usd.get("total", float('inf'))
        projected_total_premium = open_premium_total + new_premium_total + candidate_premium
        
        if projected_total_premium > total_premium_cap:
            rejected.append({
                "candidate": candidate,
                "reason": f"TOTAL_PREMIUM_CAP (${projected_total_premium:.2f} > ${total_premium_cap:.2f})",
                "blocked_by": "total_premium_cap"
            })
            logger.info(f"  ✗ {candidate_id}: TOTAL_PREMIUM_CAP (${projected_total_premium:.2f} > ${total_premium_cap:.2f})")
            continue
        
        # All checks passed - SELECT this candidate
        selected.append(candidate)
        new_premium_total += candidate_premium
        new_premium_by_regime[regime] += candidate_premium
        new_clusters_used.add(cluster_id)
        
        logger.info(f"  ✓ {candidate_id}: SELECTED")
        logger.info(f"    Premium: ${candidate_premium:.2f}")
        logger.info(f"    EV/$: {candidate.get(scoring_method, 0):.3f}")
        logger.info(f"    Regime: {regime}, Cluster: {cluster_id}")
        logger.info(f"    Running total: ${new_premium_total:.2f}")
        logger.info("")
    
    logger.info("-" * 80)
    logger.info(f"Selection Complete:")
    logger.info(f"  Selected: {len(selected)}")
    logger.info(f"  Rejected: {len(rejected)}")
    logger.info(f"  Total new premium: ${new_premium_total:.2f}")
    logger.info("")
    
    # Create summary reasons with robustness stats
    robustness_stats = {
        "external_count": sum(1 for c in representable_candidates if c.get("p_used_src") == "external"),
        "implied_count": sum(1 for c in representable_candidates if c.get("p_used_src") == "implied"),
        "fallback_count": sum(1 for c in representable_candidates if c.get("p_used_src") == "fallback"),
        "no_market_count": sum(1 for c in representable_candidates if c.get("p_ext_status") == "NO_MARKET"),
    }
    
    reasons = {
        "total_candidates": len(candidates_flat),
        "representable_candidates": len(representable_candidates),
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "new_premium_total": new_premium_total,
        "robustness_stats": robustness_stats,
        "rejection_reasons": {}
    }
    
    # Count rejection reasons
    for rej in rejected:
        reason = rej.get("blocked_by", "unknown")
        reasons["rejection_reasons"][reason] = reasons["rejection_reasons"].get(reason, 0) + 1
    
    return SelectionResult(
        selected=selected,
        rejected=rejected[:10],  # Return top 10 rejected for visibility
        reasons=reasons,
        portfolio_state=positions_view
    )


def run_selector(
    candidates_flat_path: str,
    campaign_config: Dict[str, Any],
    ledger_path: str = "runs/trade_outcomes.jsonl",
    qty: int = 1,
    output_dir: Optional[Path] = None
) -> str:
    """
    Run portfolio-aware selector and write recommended.json.
    
    Args:
        candidates_flat_path: Path to candidates_flat.json
        campaign_config: Campaign config dict with governors
        ledger_path: Path to trade_outcomes.jsonl
        qty: Quantity per trade
        output_dir: Output directory (default: same as candidates_flat)
        
    Returns:
        Path to recommended.json
    """
    logger.info("=" * 80)
    logger.info("CAMPAIGN SELECTOR")
    logger.info("=" * 80)
    
    # Load candidates
    with open(candidates_flat_path, "r") as f:
        candidates_flat = json.load(f)
    
    logger.info(f"Loaded {len(candidates_flat)} candidates from {candidates_flat_path}")
    
    # Load portfolio positions
    positions_view = load_positions_view(ledger_path)
    
    # Extract governors
    governors = campaign_config.get("governors", {})
    selection_config = campaign_config.get("selection", {})
    
    # Merge selection config into governors for selector
    governors["max_trades_per_day"] = selection_config.get("max_trades_per_day", 2)
    scoring_method = selection_config.get("scoring", "ev_per_dollar")
    
    # Run selection
    result = select_candidates(
        candidates_flat=candidates_flat,
        governors=governors,
        positions_view=positions_view,
        qty=qty,
        scoring_method=scoring_method
    )
    
    # Determine output directory
    if output_dir is None:
        output_dir = Path(candidates_flat_path).parent
    
    # Write recommended.json
    recommended_path = output_dir / "recommended.json"
    
    # TASK 1: timestamp_utc must NEVER be null
    timestamp_utc = positions_view.get("timestamp_utc")
    if timestamp_utc is None:
        from datetime import datetime, timezone
        timestamp_utc = datetime.now(timezone.utc).isoformat()
    
    recommended_output = {
        "timestamp_utc": timestamp_utc,  # NEVER null
        "scoring_method": scoring_method,
        "qty": qty,
        "portfolio_state": {
            "open_positions_count": len(result.portfolio_state["open_positions"]),
            "open_premium_total": result.portfolio_state["open_premium_total"],
            "open_count_by_regime": result.portfolio_state["open_count_by_regime"],
            "open_premium_by_regime": result.portfolio_state["open_premium_by_regime"],
            "open_clusters": list(result.portfolio_state["open_clusters"]),
        },
        "selection_summary": {
            "total_candidates": result.reasons["total_candidates"],
            "representable_count": result.reasons["representable_candidates"],
            "non_representable_count": result.reasons["total_candidates"] - result.reasons["representable_candidates"],
            "selected_count": result.reasons["selected_count"],
            "no_representable_candidates": result.reasons.get("no_representable_candidates", False),
            "blocked_by_governor": {
                "daily_premium_cap": result.reasons["rejection_reasons"].get("daily_premium_cap", 0),
                "open_premium_cap": result.reasons["rejection_reasons"].get("regime_premium_cap", 0) + result.reasons["rejection_reasons"].get("total_premium_cap", 0),
                "regime_slot_cap": result.reasons["rejection_reasons"].get("regime_position_cap", 0),
                "cluster_cap": result.reasons["rejection_reasons"].get("cluster_cap", 0),
            },
            "probability_breakdown": {
                "external_count": result.reasons["robustness_stats"]["external_count"],
                "implied_count": result.reasons["robustness_stats"]["implied_count"],
                "fallback_count": result.reasons["robustness_stats"]["fallback_count"],
            },
            "new_premium_total": result.reasons["new_premium_total"],
        },
        "selected": [
            {
                **candidate,
                "computed_premium_usd": compute_candidate_premium_usd(candidate, qty),
                "qty": qty,
                # Canonical fields (MUST be present - selector validates)
                "ev_per_dollar": candidate.get("ev_per_dollar"),  # CANONICAL
                "ev_usd": candidate.get("ev_usd"),               # CANONICAL
                "p_used": candidate.get("p_used"),                # CANONICAL
                "p_used_src": candidate.get("p_used_src"),        # CANONICAL
                # Raw fields (optional, for audit)
                "ev_per_dollar_raw": candidate.get("ev_per_dollar_raw"),
                "prob_profit_raw": candidate.get("prob_profit_raw"),
                "ev_usd_raw": candidate.get("ev_usd_raw"),
                # Robustness (computed by selector)
                "robustness": candidate.get("robustness"),
                "robustness_flags": candidate.get("robustness_flags"),
                # Probability metadata
                "p_impl": candidate.get("p_impl"),
                "p_ext": candidate.get("p_ext"),
                "p_ext_status": candidate.get("p_ext_status"),
                "p_ext_reason": candidate.get("p_ext_reason"),
                "p_profit": candidate.get("p_profit"),
                "p_confidence": candidate.get("p_confidence"),
                # Legacy fields (for backwards compatibility)
                "p_event_used": candidate.get("p_event_used"),
                "p_implied": candidate.get("p_implied"),
                "p_external": candidate.get("p_external"),
                "p_source": candidate.get("p_source"),
                "prob_profit": candidate.get("prob_profit"),
                # Full p_external provenance
                "p_external_metadata": candidate.get("p_external_metadata"),
            }
            for candidate in result.selected
        ],
        "rejected_top10": [
            {
                "candidate_id": rej["candidate"].get("candidate_id"),
                "underlier": rej["candidate"].get("underlier"),
                "regime": rej["candidate"].get("regime"),
                "cluster_id": rej["candidate"].get("cluster_id"),
                "ev_per_dollar": rej["candidate"].get("ev_per_dollar", 0),  # CANONICAL
                "ev_per_dollar_raw": rej["candidate"].get("ev_per_dollar_raw"),
                "debit_per_contract": rej["candidate"].get("debit_per_contract", 0),
                "robustness": rej["candidate"].get("robustness"),
                "robustness_flags": rej["candidate"].get("robustness_flags"),
                "reason": rej.get("reason"),
                "blocked_by": rej.get("blocked_by"),
                # Canonical probability fields
                "p_used": rej["candidate"].get("p_used"),
                "p_used_src": rej["candidate"].get("p_used_src"),
                "p_impl": rej["candidate"].get("p_impl"),
                "p_ext_status": rej["candidate"].get("p_ext_status"),
                "p_ext_reason": rej["candidate"].get("p_ext_reason"),
                # Legacy fields
                "p_event_used": rej["candidate"].get("p_event_used"),
                "p_implied": rej["candidate"].get("p_implied"),
                "p_source": rej["candidate"].get("p_source"),
                "p_external_status": rej["candidate"].get("p_external_status"),
                "p_external_reason": rej["candidate"].get("p_external_reason"),
            }
            for rej in result.rejected
        ]
    }
    
    with open(recommended_path, "w") as f:
        json.dump(recommended_output, f, indent=2)
    
    logger.info(f"✓ Recommended set written: {recommended_path}")
    logger.info(f"  Selected: {len(result.selected)}")
    logger.info(f"  Rejected (top 10): {len(result.rejected)}")
    logger.info("")
    
    return str(recommended_path)


if __name__ == "__main__":
    # Demo usage
    import argparse
    import yaml
    
    parser = argparse.ArgumentParser(description="Run campaign selector")
    parser.add_argument("--candidates", required=True, help="Path to candidates_flat.json")
    parser.add_argument("--campaign-config", required=True, help="Path to campaign config YAML")
    parser.add_argument("--ledger", default="runs/trade_outcomes.jsonl", help="Path to trade outcomes ledger")
    parser.add_argument("--qty", type=int, default=1, help="Quantity per trade")
    
    args = parser.parse_args()
    
    # Load campaign config
    with open(args.campaign_config, "r") as f:
        campaign_config = yaml.safe_load(f)
    
    # Run selector
    recommended_path = run_selector(
        candidates_flat_path=args.candidates,
        campaign_config=campaign_config,
        ledger_path=args.ledger,
        qty=args.qty
    )
    
    print(f"Recommended set: {recommended_path}")
