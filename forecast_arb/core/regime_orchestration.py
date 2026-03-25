"""
Regime Orchestration Helper

Provides orchestration logic for multi-regime runs without exploding run_daily.py.
"""

import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
import json

from .regime import apply_regime_overrides
from .regime_result import RegimeResult, create_regime_result
from .ledger import create_regime_ledger_entry, write_regime_ledger_entry
from ..oracle.regime_selector import RegimeSelector, RegimeMode


logger = logging.getLogger(__name__)


def resolve_regimes(
    regime_flag: str,
    selector_inputs: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None
) -> List[str]:
    """
    Determine which regimes to run based on CLI flag or selector.
    
    Args:
        regime_flag: CLI flag value ("auto", "crash", "selloff", "both")
        selector_inputs: Inputs for regime selector (for auto mode)
        config: Config dict with regime_selector section (for auto mode)
        
    Returns:
        List of regime names to run (e.g., ["crash"] or ["crash", "selloff"])
    """
    if regime_flag in ["crash", "selloff"]:
        return [regime_flag]
    
    if regime_flag == "both":
        return ["crash", "selloff"]
    
    if regime_flag == "auto":
        # Use regime selector
        if selector_inputs is None:
            logger.warning("Auto mode requires selector_inputs, defaulting to crash-only")
            return ["crash"]
        
        # Create selector
        selector = RegimeSelector(
            crash_p_threshold=config.get("regime_selector", {}).get("crash_p_threshold", 0.015),
            selloff_p_min=config.get("regime_selector", {}).get("selloff_p_min", 0.08),
            selloff_p_max=config.get("regime_selector", {}).get("selloff_p_max", 0.25)
        ) if config else RegimeSelector()
        
        # Decide
        decision = selector.select_regime(**selector_inputs)
        
        logger.info(f"Regime selector decision: {decision.regime_mode.value}")
        logger.info(f"Eligible regimes: {decision.eligible_regimes}")
        logger.info(f"Confidence: {decision.confidence:.2f}")
        
        # Store decision for artifacts
        selector_inputs["_decision"] = decision
        
        return decision.eligible_regimes
    
    raise ValueError(f"Unknown regime flag: {regime_flag}")


def write_unified_artifacts(
    results_by_regime: Dict[str, RegimeResult],
    selector_decision: Optional[Dict[str, Any]],
    run_dir: Path
) -> None:
    """
    Write unified artifacts for multi-regime run.
    
    Args:
        results_by_regime: Dict of regime -> RegimeResult
        selector_decision: Regime selector decision (if auto mode)
        run_dir: Run directory path
    """
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    
    # Write regime_decision.json (if selector was used)
    if selector_decision:
        decision_path = artifacts_dir / "regime_decision.json"
        with open(decision_path, "w") as f:
            json.dump(selector_decision, f, indent=2)
        logger.info(f"✓ Regime decision written: {decision_path}")
    
    # Write review_candidates.json (unified structure)
    candidates_data = {
        "regimes": {}
    }
    
    if selector_decision:
        candidates_data["selector_decision"] = selector_decision
    
    for regime, result in results_by_regime.items():
        candidates_data["regimes"][regime] = {
            "event_spec": result.event_spec,
            "event_hash": result.event_hash,
            "p_implied": result.p_implied,
            "p_implied_confidence": result.p_implied_confidence,
            "p_implied_warnings": result.p_implied_warnings,
            "representable": result.representable,
            "expiry_used": result.expiry_used,
            "expiry_selection_reason": result.expiry_selection_reason,
            "candidates": result.candidates,
            "filtered_out_count": len(result.filtered_out)
        }
    
    candidates_path = artifacts_dir / "review_candidates.json"
    with open(candidates_path, "w") as f:
        json.dump(candidates_data, f, indent=2)
    logger.info(f"✓ Review candidates written: {candidates_path}")
    
    # Log summary
    logger.info("=" * 80)
    logger.info("MULTI-REGIME RUN SUMMARY")
    logger.info("=" * 80)
    for regime, result in results_by_regime.items():
        logger.info(result.summary_line())
    logger.info("=" * 80)


def check_representability(
    snapshot: Dict[str, Any],
    expiry: str,
    threshold: float,
    tolerance: float = 5.0
) -> bool:
    """
    Check if an event threshold is representable in the option chain.
    
    Simple heuristic: threshold strike exists within tolerance.
    
    Args:
        snapshot: IBKR snapshot
        expiry: Expiry date (YYYYMMDD)
        threshold: Event threshold strike
        tolerance: Allowable strike distance (default $5)
        
    Returns:
        True if representable, False otherwise
    """
    try:
        from ..structuring.snapshot_io import get_strikes_for_expiry, get_puts_for_expiry, get_option_by_strike
        
        strikes = get_strikes_for_expiry(snapshot, expiry)
        if not strikes:
            return False
        
        # Find nearest strike
        nearest = min(strikes, key=lambda k: abs(k - threshold))
        
        # Check tolerance
        if abs(nearest - threshold) > tolerance:
            return False
        
        # Check for valid quotes
        puts = get_puts_for_expiry(snapshot, expiry)
        opt = get_option_by_strike(puts, nearest)
        
        if not opt:
            return False
        
        bid = opt.get("bid")
        ask = opt.get("ask")
        
        return bid is not None and ask is not None and bid > 0
        
    except Exception as e:
        logger.warning(f"Representability check failed: {e}")
        return False


def write_regime_ledgers(
    results_by_regime: Dict[str, RegimeResult],
    regime_mode: str,
    p_external_value: Optional[float],
    run_dir: Path
) -> None:
    """
    Write regime decision ledger entries for all regimes in run.
    
    This captures what the system decided for each regime (TRADE/NO_TRADE)
    even if no orders are placed. Critical for decision quality analysis.
    
    Args:
        results_by_regime: Dict of regime -> RegimeResult
        regime_mode: Mode string (CRASH_ONLY, SELLOFF_ONLY, BOTH, AUTO)
        p_external_value: External probability (from Kalshi, etc.)
        run_dir: Run directory path
    """
    logger.info("=" * 80)
    logger.info("WRITING REGIME DECISION LEDGERS")
    logger.info("=" * 80)
    
    for regime_name, result in results_by_regime.items():
        # Determine decision
        top_candidate = result.get_top_candidate()
        decision = "TRADE" if top_candidate else "NO_TRADE"
        
        # Determine reasons
        reasons = []
        if decision == "TRADE":
            reasons.append("CANDIDATES_AVAILABLE")
            if top_candidate:
                reasons.append(f"TOP_RANK_{top_candidate.get('rank', 1)}")
        else:
            if not result.candidates:
                if result.filtered_out:
                    reasons.append("NO_CANDIDATES_SURVIVED_FILTERS")
                else:
                    reasons.append("NO_CANDIDATES_GENERATED")
            
            if not result.representable:
                reasons.append("NOT_REPRESENTABLE")
            
            if result.warnings:
                reasons.extend([f"WARNING:{w[:30]}" for w in result.warnings[:3]])
        
        if not reasons:
            reasons = ["N/A"]
        
        # Create ledger entry
        try:
            entry = create_regime_ledger_entry(
                run_id=result.run_id,
                regime=result.regime,
                mode=regime_mode,
                decision=decision,
                reasons=reasons,
                event_hash=result.event_hash,
                expiry=result.expiry_used,
                moneyness=result.event_spec.get("moneyness", 0.0),
                spot=result.event_spec.get("spot", 0.0),
                threshold=result.event_spec.get("threshold", 0.0),
                p_implied=result.p_implied,
                p_external=p_external_value,
                representable=result.representable,
                candidate_id=top_candidate.get("candidate_id") if top_candidate else None,
                debit=top_candidate.get("debit_per_contract") if top_candidate else None,
                max_loss=top_candidate.get("max_loss_per_contract") if top_candidate else None
            )
            
            # Write ledger
            write_regime_ledger_entry(run_dir, entry, also_global=True)
            
            logger.info(f"✓ Ledger entry written for {regime_name}: {decision}")
            logger.info(f"  Reasons: {', '.join(reasons)}")
            logger.info(f"  Representable: {result.representable}")
            if top_candidate:
                logger.info(f"  Candidate: {top_candidate.get('candidate_id')}")
            
        except Exception as e:
            logger.error(f"Failed to write ledger entry for {regime_name}: {e}", exc_info=True)
    
    logger.info("=" * 80)
    logger.info("")
