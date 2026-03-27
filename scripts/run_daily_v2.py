"""
Run Crash Venture v2 with Multi-Regime Orchestration

This is the v2 runner that supports:
- Multi-regime orchestration (crash, selloff, both, auto)
- Automatic regime decision ledger writing
- Phase 3 decision quality tracking integration
- Unified artifact generation

Key differences from v1:
- Supports --regime flag (auto/crash/selloff/both)
- Runs multiple regimes in parallel when appropriate
- Automatically writes regime_ledgers for decision tracking
- Uses regime_orchestration.py for cleaner implementation
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.oracle.p_event_source import KalshiPEventSource, FallbackPEventSource, PEventResult
from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
from forecast_arb.structuring.snapshot_io import (
    load_snapshot,
    validate_snapshot,
    get_snapshot_metadata,
    get_expiries,
    compute_time_to_expiry
)
from forecast_arb.utils.manifest import compute_config_checksum
from forecast_arb.core.latest import set_latest_run
from forecast_arb.core.index import load_index, append_run, write_index
from forecast_arb.core.run_summary import extract_summary_safe
from forecast_arb.options.event_def import create_event_spec, EventSpec
from forecast_arb.options.implied_prob import implied_prob_terminal_below
from forecast_arb.gating.edge_gate import gate
from forecast_arb.core.regime import apply_regime_overrides
from forecast_arb.core.regime_result import create_regime_result
from forecast_arb.core.regime_orchestration import (
    resolve_regimes,
    write_unified_artifacts,
    check_representability,
    write_regime_ledgers
)
import yaml


logger = logging.getLogger(__name__)


def setup_logging():
    """Configure logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def fetch_or_create_snapshot(
    underlier: str,
    dte_min: int,
    dte_max: int,
    snapshot_path: Optional[str],
    ibkr_host: str,
    ibkr_port: int
) -> str:
    """
    Fetch IBKR snapshot or use existing file.
    
    Args:
        underlier: Ticker symbol
        dte_min: Minimum DTE
        dte_max: Maximum DTE
        snapshot_path: Path to existing snapshot (or None to create)
        ibkr_host: IBKR host
        ibkr_port: IBKR port
        
    Returns:
        Path to snapshot file
    """
    if snapshot_path and Path(snapshot_path).exists():
        logger.info(f"Using existing snapshot: {snapshot_path}")
        return snapshot_path
    
    # Create new snapshot
    logger.info("Creating new IBKR snapshot...")
    
    snapshot_time = datetime.now(timezone.utc).isoformat()
    output_path = f"snapshots/{underlier}_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    # Ensure directory exists
    Path("snapshots").mkdir(exist_ok=True)
    
    try:
        exporter = IBKRSnapshotExporter(host=ibkr_host, port=ibkr_port)
        exporter.connect()
        
        # Use tail_moneyness_floor for deeper OTM coverage
        # 0.25 = 25% below spot for crash regime depth
        exporter.export_snapshot(
            underlier=underlier,
            snapshot_time_utc=snapshot_time,
            dte_min=dte_min,
            dte_max=dte_max,
            tail_moneyness_floor=0.25,
            out_path=output_path
        )
        
        exporter.disconnect()
        
        logger.info(f"✓ Snapshot created: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Failed to create snapshot: {e}")
        raise


def fetch_p_external(
    p_event_source: str,
    kalshi_ticker: Optional[str],
    fallback_p: float,
    metadata: Dict,
    target_expiry: str,
    event_moneyness: float
) -> Dict:
    """
    Fetch external probability using unified p_event_source.
    
    Returns:
        Dict with p_event_external block matching schema (regime-level source of truth)
    """
    p_event_result = None
    p_external_value = None
    source_actual = None
    kalshi_auto_mapping = None
    
    if p_event_source == "fallback":
        # Fallback-only mode
        logger.info(f"Using fallback p_external (mode={p_event_source})")
        fallback_source = FallbackPEventSource(default_p_event=fallback_p)
        p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=fallback_p)
        p_external_value = p_event_result.p_event
        source_actual = p_event_result.source
        
    elif p_event_source == "kalshi-auto":
        # Auto-mapping mode
        logger.info("=" * 80)
        logger.info("KALSHI AUTO-MAPPING MODE")
        logger.info("=" * 80)
        
        spot_spy = metadata['current_price']
        spot_spx = spot_spy * 10  # Approximate SPY to SPX conversion
        
        logger.info(f"Event Parameters:")
        logger.info(f"  SPY Spot: ${spot_spy:.2f}")
        logger.info(f"  SPX Spot (estimated): ${spot_spx:.2f}")
        logger.info(f"  Moneyness: {event_moneyness:.2%}")
        logger.info(f"  Expiry: {target_expiry}")
        logger.info("")
        
        # Parse expiry to date
        from datetime import datetime as dt_parser
        expiry_date = dt_parser.strptime(target_expiry, "%Y%m%d").date()
        
        # Create event definition
        event_def_for_mapping = {
            "type": "index_drawdown",
            "index": "SPX",
            "threshold_pct": event_moneyness,
            "expiry": expiry_date
        }
        
        try:
            api_key_id = os.environ.get("KALSHI_API_KEY_ID")
            private_key = os.environ.get("KALSHI_PRIVATE_KEY")
            
            if not api_key_id or not private_key:
                logger.warning("⚠️  Kalshi credentials not found - falling back")
                fallback_source = FallbackPEventSource(default_p_event=fallback_p)
                p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=fallback_p)
                p_external_value = p_event_result.p_event
                source_actual = p_event_result.source
            else:
                client = KalshiClient(api_key=api_key_id, private_key_str=private_key)
                kalshi_source = KalshiPEventSource(client, allow_proxy=True)
                
                # Compute horizon_days
                snapshot_dt = dt_parser.fromisoformat(metadata['snapshot_time'].replace('Z', '+00:00'))
                horizon_days = int((expiry_date - snapshot_dt.date()).days)
                
                # Get p_external
                p_event_result = kalshi_source.get_p_event(
                    event_definition=event_def_for_mapping,
                    spot_spx=spot_spx,
                    horizon_days=horizon_days,
                    max_mapping_error=0.10  # Increased from 0.05 to improve match rate
                )
                
                source_actual = p_event_result.source
                
                # Check for exact match vs proxy
                has_exact = (p_event_result.p_event is not None)
                has_proxy = ("p_external_proxy" in p_event_result.metadata)
                
                if has_exact:
                    # Exact match - use as authoritative
                    p_external_value = p_event_result.p_event
                    logger.info(f"✓ EXACT MATCH: p_external={p_external_value:.3f}")
                    logger.info(f"  Ticker: {p_event_result.metadata.get('market_ticker')}")
                    
                    kalshi_auto_mapping = {
                        "ticker": p_event_result.metadata.get('market_ticker'),
                        "exact_match": True
                    }
                elif has_proxy:
                    # Proxy available but not authoritative
                    proxy_value = p_event_result.metadata['p_external_proxy']
                    logger.warning(f"⚠️  PROXY detected (not authoritative): {proxy_value:.3f}")
                    logger.warning(f"  Using fallback: {fallback_p:.3f}")
                    p_external_value = fallback_p
                    
                    kalshi_auto_mapping = {
                        "exact_match": False,
                        "proxy_available": True,
                        "proxy_p_event": proxy_value
                    }
                else:
                    # No match
                    logger.warning("⚠️  No Kalshi match - falling back")
                    p_external_value = fallback_p
                    kalshi_auto_mapping = {"exact_match": False}
                    
        except Exception as e:
            logger.error(f"Kalshi source failed: {e}", exc_info=True)
            fallback_source = FallbackPEventSource(default_p_event=fallback_p)
            p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=fallback_p)
            p_external_value = p_event_result.p_event
            source_actual = p_event_result.source
            kalshi_auto_mapping = {"error": str(e)}
    
    else:
        raise ValueError(f"Unknown p_event_source: {p_event_source}")
    
    # Build p_event_external block (regime-level source of truth)
    p_event_external = {
        "p": p_external_value,
        "source": source_actual or "unknown",
        # Legacy boolean — kept for backward compat with downstream readers.
        # Use authoritative_capable (Patch C) for semantic gating decisions.
        "authoritative": (p_external_value is not None and source_actual == "kalshi"),
        "asof_ts_utc": datetime.now(timezone.utc).isoformat(),
        "market": {
            "ticker": kalshi_auto_mapping.get("ticker") if kalshi_auto_mapping else None,
            "market_id": kalshi_auto_mapping.get("ticker") if kalshi_auto_mapping else None,
            "title": None
        } if kalshi_auto_mapping else None,
        "match": {
            "exact_match": kalshi_auto_mapping.get("exact_match", False) if kalshi_auto_mapping else False,
            "proxy_used": kalshi_auto_mapping.get("proxy_available", False) if kalshi_auto_mapping else False,
            "match_reason": "exact_match" if (kalshi_auto_mapping and kalshi_auto_mapping.get("exact_match")) else "fallback",
            "mapping_confidence": 0.7 if (kalshi_auto_mapping and kalshi_auto_mapping.get("exact_match")) else 0.35
        } if kalshi_auto_mapping else None,
        "quality": {
            "liquidity_ok": True,
            "staleness_ok": True,
            "spread_ok": True,
            "warnings": []
        }
    }

    # Patch C: transfer Patch B evidence fields from PEventResult into the block.
    # p_event_result is None only when a branch above fails to assign it (should
    # not happen in practice; guarded defensively).
    if p_event_result is not None:
        from forecast_arb.oracle.evidence import is_authoritative_capable, EVIDENCE_ROLE
        _raw_ec = getattr(p_event_result, "evidence_class", None)
        _ec_str = _raw_ec.value if _raw_ec is not None else None
        _auth_cap = is_authoritative_capable(_raw_ec)
        _role = EVIDENCE_ROLE.get(_raw_ec) if _raw_ec is not None else None
        p_event_external["evidence_class"] = _ec_str
        p_event_external["semantic_notes"] = list(
            getattr(p_event_result, "semantic_notes", None) or []
        )
        p_event_external["authoritative_capable"] = _auth_cap
        p_event_external["p_external_role"] = _role
    else:
        p_event_external["evidence_class"] = None
        p_event_external["semantic_notes"] = []
        p_event_external["authoritative_capable"] = False
        p_event_external["p_external_role"] = None

    return p_event_external


def run_regime(
    regime: str,
    config: Dict,
    snapshot: Dict,
    snapshot_path: str,
    p_event_external: Dict,
    min_debit_per_contract: float,
    run_id: str
) -> Dict:
    """
    Run structuring for a single regime.
    
    Args:
        regime: Regime name ("crash" or "selloff")
        config: Base config dict
        snapshot: Snapshot data
        snapshot_path: Path to snapshot file
        p_event_external: Full p_event_external block with provenance metadata
        min_debit_per_contract: Minimum debit filter
        run_id: Run ID for this execution
        
    Returns:
        RegimeResult dict
    """
    logger.info("=" * 80)
    logger.info(f"RUNNING REGIME: {regime.upper()}")
    logger.info("=" * 80)
    
    # Apply regime-specific overrides
    regime_config = apply_regime_overrides(config, regime)
    
    # Get event parameters
    metadata = get_snapshot_metadata(snapshot)
    edge_gating_config = regime_config.get("edge_gating", {})
    event_moneyness = edge_gating_config.get("event_moneyness", -0.15)
    
    logger.info(f"Event moneyness: {event_moneyness:.2%}")
    
    # Select expiry
    from forecast_arb.structuring.expiry_selection import select_best_expiry
    dte_min = config.get("structuring", {}).get("dte_range", [30, 60])[0]
    dte_max = config.get("structuring", {}).get("dte_range", [30, 60])[1]
    target_dte_midpoint = (dte_min + dte_max) // 2

    # Calculate event threshold for representability check
    spot = metadata["current_price"]
    event_threshold = spot * (1 + event_moneyness)
    
    logger.info(f"Event threshold for representability: ${event_threshold:.2f} (spot ${spot:.2f} × (1 + {event_moneyness}))")

    target_expiry, expiry_diagnostics = select_best_expiry(
        snapshot=snapshot,
        target_dte=target_dte_midpoint,
        dte_min=dte_min,
        dte_max=dte_max,
        event_threshold=event_threshold
    )
    
    if not target_expiry:
        logger.warning(f"⚠️  No expiry available for {regime}")
        # Create synthetic engine output for no expiry case
        engine_output = {
            "event_spec": {},
            "event_hash": "NO_EXPIRY",
            "top_structures": [],
            "filtered_out": [],
            "expiry_used": None,
            "warnings": ["NO_EXPIRY_AVAILABLE"],
            "run_id": run_id,
            "manifest": {}
        }
        return create_regime_result(
            regime=regime,
            engine_output=engine_output,
            expiry_selection_reason="NO_EXPIRY_AVAILABLE",
            representable=False,
            p_implied=None,
            p_implied_confidence=0.0,
            p_implied_warnings=["NO_EXPIRY_AVAILABLE"],
            p_event_external=p_event_external
        )
    
    logger.info(f"Selected expiry: {target_expiry}")
    
    # Create EventSpec
    event_spec = create_event_spec(
        underlier=metadata['underlier'],
        expiry=target_expiry,
        spot=metadata['current_price'],
        moneyness=event_moneyness,
        regime=regime
    )
    
    logger.info(f"Event threshold: ${event_spec.threshold:.2f}")
    
    # Check representability
    representable = check_representability(
        snapshot=snapshot,
        expiry=target_expiry,
        threshold=event_spec.threshold,
        tolerance=5.0
    )
    
    if not representable:
        logger.warning(f"⚠️  Event NOT representable for {regime}")
    
    # Compute p_implied
    from forecast_arb.options.event_def import create_terminal_below_event
    event_def = create_terminal_below_event(
        underlier=metadata['underlier'],
        expiry=target_expiry,
        spot=metadata['current_price'],
        event_moneyness=event_moneyness
    )
    
    p_implied_value, p_implied_confidence, p_implied_warnings = implied_prob_terminal_below(
        snapshot=snapshot,
        expiry=target_expiry,
        threshold=event_def.threshold,
        r=0.0
    )
    
    if p_implied_value is not None:
        logger.info(f"p_implied: {p_implied_value:.3f} (confidence: {p_implied_confidence:.2f})")
    else:
        logger.warning("⚠️  p_implied calculation failed")
    
    # PHASE 4: Full structuring integration
    logger.info(f"Running full structuring for {regime}...")
    
    # Extract p_external value from block
    p_external_value = p_event_external.get("p") if p_event_external else None
    
    # Use p_external if available, otherwise use a conservative default based on p_implied
    p_event_for_structuring = p_external_value if p_external_value is not None else (p_implied_value if p_implied_value is not None else 0.30)
    
    logger.info(f"Using p_event for Monte Carlo calibration: {p_event_for_structuring:.3f}")
    
    # Import and call the v1 structuring engine
    from forecast_arb.engine.crash_venture_v1_snapshot import generate_candidates_from_snapshot
    from forecast_arb.structuring.calibrator import calibrate_drift
    from forecast_arb.structuring.evaluator import evaluate_structure
    from forecast_arb.structuring.router import filter_dominated_structures, choose_best_structure, rank_structures
    from forecast_arb.structuring.output_formatter import get_reason_selected
    
    # Get structuring parameters from config
    struct_config = regime_config.get("structuring", {})
    # CRITICAL FIX: Use regime-specific moneyness, not base config moneyness list
    moneyness_targets = [event_moneyness]  # Use regime's moneyness from event_spec
    spread_widths = struct_config.get("spread_widths", [15, 20])
    mc_config = struct_config.get("monte_carlo", {"paths": 10000})
    constraints = struct_config.get("constraints", {
        "max_candidates_evaluated": 50,
        "max_loss_usd_per_trade": 10000,
        "top_n_output": 5
    })
    objective = struct_config.get("objective", "max_ev_per_dollar")
    
    # Generate candidates
    try:
        candidates, filtered_out = generate_candidates_from_snapshot(
            snapshot=snapshot,
            expiry=target_expiry,
            S0=metadata['current_price'],
            moneyness_targets=moneyness_targets,
            spread_widths=spread_widths,
            min_debit_per_contract=min_debit_per_contract,
            max_candidates=constraints.get("max_candidates_evaluated", 50),
            regime=regime
        )
        
        logger.info(f"Generated {len(candidates)} candidates, filtered out {len(filtered_out)}")
        
        # VALIDATION GUARDRAIL: Ensure all candidates match regime parameters
        from forecast_arb.structuring.candidate_validator import enforce_regime_consistency
        try:
            candidates = enforce_regime_consistency(
                candidates=candidates,
                regime=regime,
                expected_moneyness=event_moneyness,
                tolerance=0.001,
                fail_fast=True  # Fail immediately if mismatch detected
            )
            logger.info(f"✓ All candidates validated for regime consistency")
        except Exception as validation_error:
            logger.error(f"❌ REGIME VALIDATION FAILED for {regime}: {validation_error}")
            # Return STAND_DOWN result to prevent bad trades
            engine_output = {
                "event_spec": event_spec.to_dict(),
                "event_hash": event_spec.event_hash,
                "top_structures": [],
                "filtered_out": filtered_out,
                "expiry_used": target_expiry,
                "warnings": [f"VALIDATION_ERROR: {str(validation_error)}"],
                "run_id": run_id,
                "manifest": {
                    "n_candidates_generated": len(candidates),
                    "n_filtered_out": len(filtered_out),
                    "validation_error": str(validation_error)
                }
            }
            return create_regime_result(
                regime=regime,
                engine_output=engine_output,
                expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
                representable=representable,
                p_implied=p_implied_value,
                p_implied_confidence=p_implied_confidence,
                p_implied_warnings=p_implied_warnings
            )
        
        # If no candidates, return early
        if not candidates:
            logger.warning(f"No candidates generated for {regime}")
            engine_output = {
                "event_spec": event_spec.to_dict(),
                "event_hash": event_spec.event_hash,
                "top_structures": [],
                "filtered_out": filtered_out,
                "expiry_used": target_expiry,
                "warnings": ["NO_CANDIDATES_GENERATED"],
                "run_id": run_id,
                "manifest": {
                    "n_candidates_generated": 0,
                    "n_filtered_out": len(filtered_out)
                }
            }
            return create_regime_result(
                regime=regime,
                engine_output=engine_output,
                expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
                representable=representable,
                p_implied=p_implied_value,
                p_implied_confidence=p_implied_confidence,
                p_implied_warnings=p_implied_warnings
            )
        
        # Calibrate drift for Monte Carlo
        T = compute_time_to_expiry(metadata['snapshot_time'], target_expiry)
        
        # Get sigma from snapshot
        puts = get_expiries(snapshot)
        sigma = 0.15  # Default
        if target_expiry in snapshot:
            for put in snapshot[target_expiry]:
                if put.get("implied_vol") and put["implied_vol"] > 0:
                    sigma = put["implied_vol"]
                    break
        
        logger.info(f"Using sigma: {sigma:.3f}")
        
        # Calibrate drift
        K_barrier = metadata['current_price'] * (1 + event_moneyness)
        rng_seed = abs(hash(run_id)) % (2**32)
        
        mu_calib, p_achieved = calibrate_drift(
            p_event=p_event_for_structuring,
            S0=metadata['current_price'],
            K_barrier=K_barrier,
            T=T,
            sigma=sigma,
            n_samples=10000,
            seed=rng_seed
        )
        
        logger.info(f"Calibrated: μ={mu_calib:.4f}, achieved p={p_achieved:.3f}")
        
        # Evaluate candidates
        evaluated = []
        for i, candidate in enumerate(candidates):
            try:
                candidate["spot_used"] = metadata['current_price']
                candidate["assumed_p_event"] = p_event_for_structuring
                
                eval_result = evaluate_structure(
                    structure=candidate,
                    mu=mu_calib,
                    sigma=sigma,
                    S0=metadata['current_price'],
                    T=T,
                    n_paths=mc_config.get("paths", 10000),
                    seed=rng_seed + i
                )
                
                # Calculate EV per dollar
                debit_per_contract = eval_result.get("debit_per_contract", 0)
                if debit_per_contract > 0:
                    ev_per_contract = eval_result["ev"] * 100
                    eval_result["ev_per_dollar"] = ev_per_contract / debit_per_contract
                    evaluated.append(eval_result)
                    
            except Exception as e:
                logger.warning(f"Evaluation failed for candidate {i}: {e}")
                continue
        
        logger.info(f"Successfully evaluated {len(evaluated)} structures")
        
        if not evaluated:
            logger.warning(f"No structures evaluated successfully for {regime}")
            engine_output = {
                "event_spec": event_spec.to_dict(),
                "event_hash": event_spec.event_hash,
                "top_structures": [],
                "filtered_out": filtered_out,
                "expiry_used": target_expiry,
                "warnings": ["NO_STRUCTURES_EVALUATED"],
                "run_id": run_id,
                "manifest": {
                    "n_candidates_generated": len(candidates),
                    "n_filtered_out": len(filtered_out),
                    "n_evaluated": 0
                }
            }
            return create_regime_result(
                regime=regime,
                engine_output=engine_output,
                expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
                representable=representable,
                p_implied=p_implied_value,
                p_implied_confidence=p_implied_confidence,
                p_implied_warnings=p_implied_warnings
            )
        
        # Filter dominated structures
        non_dominated = filter_dominated_structures(evaluated)
        logger.info(f"After dominance filter: {len(non_dominated)} structures remain")
        
        if not non_dominated:
            logger.warning(f"No non-dominated structures for {regime}")
            engine_output = {
                "event_spec": event_spec.to_dict(),
                "event_hash": event_spec.event_hash,
                "top_structures": [],
                "filtered_out": filtered_out,
                "expiry_used": target_expiry,
                "warnings": ["NO_NON_DOMINATED_STRUCTURES"],
                "run_id": run_id,
                "manifest": {
                    "n_candidates_generated": len(candidates),
                    "n_filtered_out": len(filtered_out),
                    "n_evaluated": len(evaluated),
                    "n_non_dominated": 0
                }
            }
            return create_regime_result(
                regime=regime,
                engine_output=engine_output,
                expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
                representable=representable,
                p_implied=p_implied_value,
                p_implied_confidence=p_implied_confidence,
                p_implied_warnings=p_implied_warnings
            )
        
        # Choose best structures
        constraints_dict = {
            "max_loss_usd_per_trade": constraints.get("max_loss_usd_per_trade", 10000),
            "min_prob_profit": 0.0,
            "min_ev": 0.0
        }
        
        best_structures = choose_best_structure(
            non_dominated,
            constraints=constraints_dict,
            objective=objective
        )
        
        if not best_structures:
            logger.warning(f"No structures meet constraints for {regime}")
            engine_output = {
                "event_spec": event_spec.to_dict(),
                "event_hash": event_spec.event_hash,
                "top_structures": [],
                "filtered_out": filtered_out,
                "expiry_used": target_expiry,
                "warnings": ["NO_STRUCTURES_MEET_CONSTRAINTS"],
                "run_id": run_id,
                "manifest": {
                    "n_candidates_generated": len(candidates),
                    "n_filtered_out": len(filtered_out),
                    "n_evaluated": len(evaluated),
                    "n_non_dominated": len(non_dominated),
                    "n_best": 0
                }
            }
            return create_regime_result(
                regime=regime,
                engine_output=engine_output,
                expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
                representable=representable,
                p_implied=p_implied_value,
                p_implied_confidence=p_implied_confidence,
                p_implied_warnings=p_implied_warnings
            )
        
        # Rank top N
        top_n = constraints.get("top_n_output", 5)
        top_structures = rank_structures(best_structures, top_n=top_n)
        
        # Add reason_selected
        for struct in top_structures:
            struct["reason_selected"] = get_reason_selected(
                struct,
                rank=struct["rank"],
                objective=objective
            )
        
        logger.info(f"✓ {regime} structuring complete: {len(top_structures)} top structures")
        
        # Create engine output
        engine_output = {
            "event_spec": event_spec.to_dict(),
            "event_hash": event_spec.event_hash,
            "top_structures": top_structures,
            "filtered_out": filtered_out,
            "expiry_used": target_expiry,
            "warnings": [],
            "run_id": run_id,
            "manifest": {
                "n_candidates_generated": len(candidates),
                "n_filtered_out": len(filtered_out),
                "n_evaluated": len(evaluated),
                "n_non_dominated": len(non_dominated),
                "n_best": len(best_structures),
                "n_top": len(top_structures),
                "mu_calibrated": mu_calib,
                "p_achieved": p_achieved,
                "sigma": sigma
            }
        }
        
        return create_regime_result(
            regime=regime,
            engine_output=engine_output,
            expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
            representable=representable,
            p_implied=p_implied_value,
            p_implied_confidence=p_implied_confidence,
            p_implied_warnings=p_implied_warnings,
            p_event_external=p_event_external
        )
        
    except Exception as e:
        logger.error(f"Structuring failed for {regime}: {e}", exc_info=True)
        # Return error result
        engine_output = {
            "event_spec": event_spec.to_dict(),
            "event_hash": event_spec.event_hash if hasattr(event_spec, 'event_hash') else f"{regime}_{target_expiry}",
            "top_structures": [],
            "filtered_out": [],
            "expiry_used": target_expiry,
            "warnings": [f"STRUCTURING_ERROR: {str(e)}"],
            "run_id": run_id,
            "manifest": {"error": str(e)}
        }
        return create_regime_result(
            regime=regime,
            engine_output=engine_output,
            expiry_selection_reason=expiry_diagnostics.get("reason", "SELECTED"),
            representable=representable,
            p_implied=p_implied_value,
            p_implied_confidence=p_implied_confidence,
            p_implied_warnings=p_implied_warnings,
            p_event_external=p_event_external
        )


def run_daily_core(
    regime: str,
    underlier: str,
    dte_min: int,
    dte_max: int,
    p_event_source: str,
    fallback_p: float,
    campaign_config: str,
    min_debit_per_contract: float,
    snapshot_path: Optional[str] = None,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7496,
    runs_root: Path = Path("runs"),
) -> Dict:
    """
    Deterministic daily run: snapshot → regime resolution → structuring →
    artifact writing.  Returns a result dict; never calls sys.exit().

    Steps covered:
      1. Load config and compute checksum
      2. Fetch or load IBKR snapshot
      3. Resolve regimes to run
      4. Fetch p_external (shared across regimes)
      5. Run structuring for each regime via run_regime()
      6. Write unified artifacts and regime ledgers

    Index / latest-pointer publishing (Step 7) is deliberately NOT performed
    here — that side effect belongs in the CLI caller (main()).

    Args:
        regime:                  Regime flag ("auto", "crash", "selloff", "both")
        underlier:               Ticker symbol (e.g. "SPY")
        dte_min:                 Minimum days to expiry
        dte_max:                 Maximum days to expiry
        p_event_source:          Probability source ("kalshi-auto" | "fallback")
        fallback_p:              Fallback p_external when Kalshi unavailable
        campaign_config:         Path string to YAML config file
        min_debit_per_contract:  Minimum debit filter for structuring
        snapshot_path:           Path to existing snapshot JSON; None → fetch live
        ibkr_host:               IBKR host for live snapshot fetch
        ibkr_port:               IBKR port for live snapshot fetch
        runs_root:               Root directory for run output (default: Path("runs"))

    Returns:
        {
            "run_id":         str,
            "run_dir":        Path,
            "regimes_run":    list[str],
            "regime_results": dict,       # regime name → RegimeResult
            "snapshot_path":  str,
            "config_checksum": str,
            "ts_utc":         str,
        }

    Raises:
        RuntimeError: on fatal errors (missing expiry, snapshot failure, etc.)
    """
    import yaml
    ts_utc = datetime.now(timezone.utc).isoformat()

    # Step 1: Load config
    with open(campaign_config, "r") as f:
        config = yaml.safe_load(f)

    config_checksum = compute_config_checksum(config)

    # Step 2: Snapshot
    logger.info("Step 1: IBKR Snapshot")
    logger.info("-" * 80)

    try:
        resolved_snapshot_path = fetch_or_create_snapshot(
            underlier=underlier,
            dte_min=dte_min,
            dte_max=dte_max,
            snapshot_path=snapshot_path,
            ibkr_host=ibkr_host,
            ibkr_port=ibkr_port,
        )

        snapshot = load_snapshot(resolved_snapshot_path)
        validate_snapshot(snapshot)
        metadata = get_snapshot_metadata(snapshot)

        logger.info("✓ Snapshot validated")
        logger.info(f"  Underlier: {metadata['underlier']}")
        logger.info(f"  Spot: ${metadata['current_price']:.2f}")
        logger.info(f"  Expiries: {len(get_expiries(snapshot))}")
        logger.info("")

    except Exception as exc:
        raise RuntimeError(f"Snapshot failed: {exc}") from exc

    # Step 3: Resolve regimes
    logger.info("Step 2: Regime Resolution")
    logger.info("-" * 80)

    selector_inputs = None
    if regime == "auto":
        logger.warning("⚠️  Auto mode not fully implemented, defaulting to crash")
        regimes_to_run = ["crash"]
    else:
        regimes_to_run = resolve_regimes(
            regime_flag=regime,
            selector_inputs=selector_inputs,
            config=config,
        )

    logger.info(f"Regimes to run: {regimes_to_run}")
    logger.info("")

    # Step 4: Fetch p_external (shared across regimes)
    logger.info("Step 3: External Probability")
    logger.info("-" * 80)

    edge_gating_config = config.get("edge_gating", {})
    event_moneyness = edge_gating_config.get("event_moneyness", -0.15)

    from forecast_arb.structuring.expiry_selection import select_best_expiry
    target_dte_midpoint = (dte_min + dte_max) // 2
    event_threshold_main = metadata["current_price"] * (1 + event_moneyness)

    target_expiry, _ = select_best_expiry(
        snapshot=snapshot,
        target_dte=target_dte_midpoint,
        dte_min=dte_min,
        dte_max=dte_max,
        event_threshold=event_threshold_main,
    )

    if not target_expiry:
        raise RuntimeError("No expiry available in snapshot for the requested DTE range")

    p_event_external_block = fetch_p_external(
        p_event_source=p_event_source,
        kalshi_ticker=None,
        fallback_p=fallback_p,
        metadata=metadata,
        target_expiry=target_expiry,
        event_moneyness=event_moneyness,
    )

    p_external_value = p_event_external_block.get("p")
    p_ext_display = f"{p_external_value:.3f}" if p_external_value is not None else "None"
    p_ext_source = p_event_external_block.get("source", "unknown")
    logger.info(f"p_external: {p_ext_display} (source: {p_ext_source})")
    if p_event_external_block.get("market") and p_event_external_block["market"].get("ticker"):
        logger.info(f"  Market: {p_event_external_block['market']['ticker']}")
        logger.info(
            f"  Exact match: {p_event_external_block.get('match', {}).get('exact_match', False)}"
        )
    logger.info("")

    # Step 5: Run each regime
    logger.info("Step 4: Multi-Regime Execution")
    logger.info("-" * 80)

    timestamp = datetime.now(timezone.utc).isoformat().replace(":", "").replace("-", "").replace(".", "")[:15]
    run_id = f"crash_venture_v2_{config_checksum}_{timestamp}"
    run_dir = runs_root / "crash_venture_v2" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Patch C: write p_event_external.json so run_summary / decision_packet
    # can read evidence_class and related provenance fields.
    _ext_artifacts_dir = run_dir / "artifacts"
    _ext_artifacts_dir.mkdir(parents=True, exist_ok=True)
    _ext_artifact_path = _ext_artifacts_dir / "p_event_external.json"
    try:
        import json as _json
        with open(_ext_artifact_path, "w", encoding="utf-8") as _fh:
            _json.dump(p_event_external_block, _fh, indent=2)
        logger.info(f"✓ p_event_external.json written: {_ext_artifact_path}")
    except Exception as _exc:
        logger.warning(f"Failed to write p_event_external.json: {_exc}")

    results_by_regime = {}
    for r in regimes_to_run:
        result = run_regime(
            regime=r,
            config=config,
            snapshot=snapshot,
            snapshot_path=resolved_snapshot_path,
            p_event_external=p_event_external_block,
            min_debit_per_contract=min_debit_per_contract,
            run_id=run_id,
        )
        results_by_regime[r] = result
        logger.info("")

    # Step 6: Write unified artifacts
    logger.info("Step 5: Unified Artifacts")
    logger.info("-" * 80)

    write_unified_artifacts(
        results_by_regime=results_by_regime,
        selector_decision=None,
        run_dir=run_dir,
    )
    logger.info("")

    # Step 6b: Write regime ledgers
    write_regime_ledgers(
        results_by_regime=results_by_regime,
        regime_mode=regime.upper(),
        p_external_value=p_external_value,
        run_dir=run_dir,
    )

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "regimes_run": regimes_to_run,
        "regime_results": results_by_regime,
        "snapshot_path": resolved_snapshot_path,
        "config_checksum": config_checksum,
        "ts_utc": ts_utc,
    }


def main():
    """Main CLI entrypoint."""
    # Print batch runner header
    print("")
    print("=" * 80)
    print("BATCH RUNNER: run_daily_v2.py (no execution prompts)")
    print("=" * 80)
    print("")
    
    parser = argparse.ArgumentParser(
        description="Run Crash Venture v2 with multi-regime orchestration"
    )
    
    # Regime selection (NEW in v2)
    parser.add_argument(
        "--regime",
        type=str,
        choices=["auto", "crash", "selloff", "both"],
        default="crash",
        help="Regime selection mode (default: crash)"
    )
    
    # Snapshot options
    parser.add_argument(
        "--underlier",
        type=str,
        default="SPY",
        help="Underlier ticker (default: SPY)"
    )
    parser.add_argument(
        "--snapshot",
        type=str,
        default=None,
        help="Path to existing snapshot JSON"
    )
    parser.add_argument(
        "--dte-min",
        type=int,
        default=30,
        help="Minimum days to expiry (default: 30)"
    )
    parser.add_argument(
        "--dte-max",
        type=int,
        default=60,
        help="Maximum days to expiry (default: 60)"
    )
    
    # IBKR connection
    parser.add_argument(
        "--ibkr-host",
        type=str,
        default="127.0.0.1",
        help="IBKR host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--ibkr-port",
        type=int,
        default=7496,
        help="IBKR port (default: 7496)"
    )
    
    # External probability source
    parser.add_argument(
        "--p-event-source",
        type=str,
        choices=["kalshi-auto", "fallback"],
        default="kalshi-auto",
        help="External probability source (default: kalshi-auto)"
    )
    parser.add_argument(
        "--fallback-p",
        type=float,
        default=0.30,
        help="Fallback p_external (default: 0.30)"
    )
    
    # Config
    parser.add_argument(
        "--campaign-config",
        type=str,
        default="configs/structuring_crash_venture_v2.yaml",
        help="Campaign config path (default: configs/structuring_crash_venture_v2.yaml)"
    )
    
    # Filters
    parser.add_argument(
        "--min-debit-per-contract",
        type=float,
        default=10.0,
        help="Minimum debit per contract (default: 10.0)"
    )
    
    # Intent emission (NEW)
    parser.add_argument(
        "--emit-intent",
        action="store_true",
        help="Emit OrderIntent without execution"
    )
    parser.add_argument(
        "--pick-rank",
        type=int,
        help="Rank of candidate to emit (required with --emit-intent)"
    )
    parser.add_argument(
        "--qty",
        type=int,
        default=1,
        help="Order quantity (default: 1)"
    )
    parser.add_argument(
        "--limit-start",
        type=float,
        help="Starting limit price (required with --emit-intent)"
    )
    parser.add_argument(
        "--limit-max",
        type=float,
        help="Maximum limit price (required with --emit-intent)"
    )
    parser.add_argument(
        "--intent-out",
        type=str,
        help="Output path for intent JSON (required with --emit-intent)"
    )
    
    args = parser.parse_args()
    
    setup_logging()
    
    # STEP 2: Validate intent mode early
    if args.emit_intent:
        required = [
            args.regime,
            args.pick_rank,
            args.limit_start,
            args.limit_max,
            args.intent_out,
        ]
        if any(v is None for v in required):
            raise SystemExit("❌ --emit-intent requires --regime, --pick-rank, --limit-start, --limit-max, --intent-out")
        
        if args.regime not in ("crash", "selloff"):
            raise SystemExit("❌ --emit-intent requires --regime crash|selloff (not auto/both)")
    
    logger.info("=" * 80)
    logger.info("CRASH VENTURE V2 - MULTI-REGIME ORCHESTRATION")
    logger.info("=" * 80)
    logger.info(f"Regime Mode: {args.regime}")
    if args.emit_intent:
        logger.info(f"Intent Emission: ENABLED (rank={args.pick_rank}, qty={args.qty})")
    logger.info(f"Underlier: {args.underlier}")
    logger.info(f"DTE Range: {args.dte_min}-{args.dte_max}")
    logger.info(f"Min Debit: ${args.min_debit_per_contract:.2f}")
    logger.info("")
    
    # Steps 1-6: deterministic run generation and artifact writing
    try:
        result = run_daily_core(
            regime=args.regime,
            underlier=args.underlier,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            p_event_source=args.p_event_source,
            fallback_p=args.fallback_p,
            campaign_config=args.campaign_config,
            min_debit_per_contract=args.min_debit_per_contract,
            snapshot_path=args.snapshot,
            ibkr_host=args.ibkr_host,
            ibkr_port=args.ibkr_port,
        )
    except RuntimeError as exc:
        logger.error(f"❌ Run failed: {exc}", exc_info=True)
        sys.exit(1)

    run_id = result["run_id"]
    run_dir = result["run_dir"]
    regimes_to_run = result["regimes_run"]
    results_by_regime = result["regime_results"]

    # INTENT EMISSION MODE: Emit intent and exit early
    if args.emit_intent:
        logger.info("")
        logger.info("=" * 80)
        logger.info("INTENT EMISSION MODE")
        logger.info("=" * 80)
        logger.info(f"Target regime: {args.regime}")
        logger.info(f"Target rank: {args.pick_rank}")
        logger.info("")
        
        # Get regime result (RegimeResult dataclass)
        regime_result = results_by_regime.get(args.regime)
        if not regime_result:
            raise SystemExit(f"❌ No results for regime={args.regime}")
        
        # Check if candidates available
        if not regime_result.candidates:
            raise SystemExit(f"❌ No candidates available for regime={args.regime}")
        
        # Use built-in method to get candidate by rank
        candidate = regime_result.get_candidate_by_rank(args.pick_rank)
        
        if candidate is None:
            available = [c.get("rank") for c in regime_result.candidates]
            raise SystemExit(f"❌ No candidate with rank={args.pick_rank}. Available ranks: {available}")
        
        logger.info(f"✓ Found candidate rank={args.pick_rank}")
        logger.info(f"  Expiry: {candidate.get('expiry')}")
        logger.info(f"  Strikes: {candidate.get('strikes')}")
        logger.info(f"  EV/$ : {candidate.get('metrics', {}).get('ev_per_dollar', 0):.3f}")
        logger.info("")
        
        # Build OrderIntent
        from forecast_arb.execution.intent_builder import build_order_intent
        
        intent = build_order_intent(
            candidate=candidate,
            regime=args.regime,
            qty=args.qty,
            limit_start=args.limit_start,
            limit_max=args.limit_max,
            run_id=run_id,
            source_run_dir=str(run_dir),
        )
        
        logger.info("OrderIntent generated:")
        logger.info(f"  Strategy: {intent['strategy']}")
        logger.info(f"  Regime: {intent['regime']}")
        logger.info(f"  Symbol: {intent['symbol']}")
        logger.info(f"  Expiry: {intent['expiry']}")
        logger.info(f"  Qty: {intent['qty']}")
        logger.info(f"  Limit: ${intent['limit']['start']:.2f} - ${intent['limit']['max']:.2f}")
        logger.info("")
        
        # Write intent atomically
        intent_path = Path(args.intent_out)
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(intent_path, "w") as f:
            json.dump(intent, f, indent=2)
        
        logger.info(f"✓ OrderIntent written: {intent_path}")
        logger.info("")
        logger.info("ℹ️  Intent emission complete. No order staged or transmitted.")
        logger.info("")
        
        # Explicit termination - do not continue to index update
        return
    
    # Step 7: Update index
    logger.info("Step 7: Update Run Index")
    logger.info("-" * 80)
    
    try:
        runs_root = Path("runs")
        
        # Update latest pointer
        set_latest_run(
            runs_root=runs_root,
            run_dir=run_dir,
            decision="MULTI_REGIME",
            reason=f"Ran {len(regimes_to_run)} regime(s)",
            run_id=run_id
        )
        logger.info(f"✓ Latest run pointer updated")
        
        # Update index
        summary = extract_summary_safe(run_dir)
        index = load_index(runs_root)
        index = append_run(index, summary, max_entries=500)
        write_index(runs_root, index)
        logger.info(f"✓ Run index updated")
        
    except Exception as e:
        logger.warning(f"Failed to update index (non-fatal): {e}")
    
    logger.info("")
    
    # Final summary
    logger.info("=" * 80)
    logger.info("✅ MULTI-REGIME RUN COMPLETE")
    logger.info("=" * 80)
    logger.info(f"Run ID: {run_id}")
    logger.info(f"Run Dir: {run_dir}")
    logger.info(f"Regimes: {', '.join(regimes_to_run)}")
    logger.info(f"Ledgers Written: ✓ (automatic)")
    logger.info("")
    logger.info(f"📁 View outputs: {run_dir}")
    logger.info("")


if __name__ == "__main__":
    main()
