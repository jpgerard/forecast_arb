"""
Crash Venture v1 Engine - Locked and Hardened

Produces 1-3 clean, executable SPY put-spread trade candidates per run.
No parameter drift. No hidden discretion.
"""

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from ..structuring.templates import generate_put_spread
from ..structuring.calibrator import calibrate_drift
from ..structuring.evaluator import evaluate_structure
from ..structuring.router import (
    choose_best_structure,
    rank_structures,
    filter_dominated_structures
)
from ..structuring.event_map import enrich_oracle_data_with_mapping
from ..structuring.output_formatter import (
    assert_structure_sanity,
    get_reason_selected,
    format_structure_output,
    write_structures_json,
    write_summary_md,
    write_dry_run_tickets
)
from ..utils.manifest import compute_config_checksum, ManifestWriter


logger = logging.getLogger(__name__)


def load_frozen_config(config_path: str) -> Dict:
    """
    Load frozen configuration with validation.
    
    Args:
        config_path: Path to config file
        
    Returns:
        Config dict
        
    Raises:
        ValueError: If config is not crash_venture_v1
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    
    # Validate it's the correct campaign
    campaign = config.get("campaign_name", "")
    if campaign != "crash_venture_v1":
        raise ValueError(
            f"Config campaign_name must be 'crash_venture_v1', got '{campaign}'"
        )
    
    logger.info(f"Loaded frozen config: {config_path}")
    logger.info(f"Config version: {config.get('config_version', 'N/A')}")
    
    return config


def setup_determinism(run_id: str) -> int:
    """
    Set up deterministic RNG seeding.
    
    Args:
        run_id: Run identifier
        
    Returns:
        Seed value
    """
    import random
    import numpy as np
    
    # Derive seed from run_id
    rng_seed = abs(hash(run_id)) % (2**32)
    
    # Set seeds
    random.seed(rng_seed)
    np.random.seed(rng_seed)
    
    logger.info(f"Deterministic seed: {rng_seed}")
    
    return rng_seed


def generate_candidate_structures(
    underlier: str,
    expiry: str,
    S0: float,
    moneyness_targets: List[float],
    spread_widths: List[int],
    r: float,
    sigma: float,
    T: float,
    max_candidates: int
) -> List[Dict]:
    """
    Generate all candidate put spreads from configuration.
    
    Args:
        underlier: Ticker (must be "SPY")
        expiry: Expiration date
        S0: Spot price
        moneyness_targets: List of moneyness values (e.g., [-0.10, -0.15, -0.20])
        spread_widths: List of spread widths in dollars
        r: Risk-free rate
        sigma: Implied volatility
        T: Time to expiry (years)
        max_candidates: Maximum number of candidates to generate
        
    Returns:
        List of candidate structure dicts
    """
    candidates = []
    
    for moneyness in moneyness_targets:
        for width in spread_widths:
            if len(candidates) >= max_candidates:
                break
                
            K_long = S0 * (1 + moneyness)
            K_short = K_long - width
            
            if K_short <= 0:
                logger.warning(
                    f"Skipping invalid strikes: K_long={K_long:.2f}, K_short={K_short:.2f}"
                )
                continue
            
            try:
                put_spread = generate_put_spread(
                    underlier=underlier,
                    expiry=expiry,
                    S0=S0,
                    K_long=K_long,
                    K_short=K_short,
                    r=r,
                    sigma=sigma,
                    T=T
                )
                candidates.append(put_spread)
            except Exception as e:
                logger.error(f"Failed to generate structure: {e}")
                continue
    
    logger.info(f"Generated {len(candidates)} candidate structures")
    return candidates


def run_crash_venture_v1(
    config_path: str,
    p_event: float,
    spot_price: float,
    atm_iv: float,
    expiry_date: str,
    days_to_expiry: int
) -> Dict:
    """
    Run Crash Venture v1 with locked parameters.
    
    Args:
        config_path: Path to frozen config file
        p_event: Event probability (from Kalshi oracle)
        spot_price: Current SPY spot price
        atm_iv: ATM implied volatility
        expiry_date: Option expiry date (YYYY-MM-DD)
        days_to_expiry: Days to expiration
        
    Returns:
        Dict with run results
    """
    # Load frozen config
    config = load_frozen_config(config_path)
    
    # Compute config checksum
    config_checksum = compute_config_checksum(config)
    logger.info(f"Config checksum: {config_checksum}")
    
    # Create run ID with checksum
    campaign = config["campaign_name"]
    run_time_utc = datetime.now(timezone.utc).isoformat()
    timestamp = run_time_utc.replace(':', '').replace('-', '').replace('.', '')[:15]
    run_id = f"{campaign}_{config_checksum}_{timestamp}"
    
    logger.info(f"Starting Crash Venture v1 run: {run_id}")
    
    # Setup determinism
    rng_seed = setup_determinism(run_id)
    
    # Extract frozen parameters
    struct_config = config["structuring"]
    
    # ENFORCE: Underlier must be SPY
    underlier = struct_config["underlier"]
    if underlier != "SPY":
        raise ValueError(f"Underlier must be 'SPY', got '{underlier}'")
    
    # ENFORCE: DTE in range
    dte_min = struct_config["dte_range_days"]["min"]
    dte_max = struct_config["dte_range_days"]["max"]
    if not (dte_min <= days_to_expiry <= dte_max):
        raise ValueError(
            f"DTE {days_to_expiry} outside allowed range [{dte_min}, {dte_max}]"
        )
    
    # Get parameters
    moneyness_targets = struct_config["moneyness_targets"]
    spread_widths = struct_config["spread_widths"]
    constraints = struct_config["constraints"]
    mc_config = struct_config["monte_carlo"]
    objective = struct_config["objective"]
    
    # Option parameters
    S0 = spot_price
    r = 0.05  # Fixed risk-free rate
    sigma = atm_iv
    T = days_to_expiry / 365.0
    n_paths = mc_config["paths"]
    
    # Calibrate drift for p_event
    K_barrier = S0 * 0.95  # Assume event = SPY drops below 95% of current
    
    logger.info(f"Calibrating drift for p_event={p_event:.3f}")
    mu_calib, p_achieved = calibrate_drift(
        p_event=p_event,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=10000,
        seed=rng_seed
    )
    
    logger.info(f"Calibrated: μ={mu_calib:.4f}, achieved p={p_achieved:.3f}")
    
    # Generate candidate structures
    candidates = generate_candidate_structures(
        underlier=underlier,
        expiry=expiry_date,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        r=r,
        sigma=sigma,
        T=T,
        max_candidates=constraints["max_candidates_evaluated"]
    )
    
    if not candidates:
        raise ValueError("No valid candidate structures generated")
    
    # Evaluate all candidates
    logger.info(f"Evaluating {len(candidates)} candidates with {n_paths} Monte Carlo paths")
    evaluated = []
    
    for i, candidate in enumerate(candidates):
        try:
            # Add metadata
            candidate["spot_used"] = S0
            candidate["atm_iv_used"] = sigma
            candidate["assumed_p_event"] = p_event
            
            eval_result = evaluate_structure(
                structure=candidate,
                mu=mu_calib,
                sigma=sigma,
                S0=S0,
                T=T,
                n_paths=n_paths,
                seed=rng_seed + i
            )
            
            # Calculate EV per dollar (per-share basis)
            max_loss_per_share = eval_result["max_loss"]
            eval_result["ev_per_dollar"] = (
                eval_result["ev"] / max_loss_per_share if max_loss_per_share > 0 else 0
            )
            
            evaluated.append(eval_result)
        except Exception as e:
            logger.error(f"Evaluation failed for candidate {i}: {e}")
            continue
    
    logger.info(f"Successfully evaluated {len(evaluated)} structures")
    
    # Apply dominance filter (CRITICAL)
    non_dominated = filter_dominated_structures(evaluated)
    logger.info(f"After dominance filter: {len(non_dominated)} structures remain")
    
    if not non_dominated:
        raise ValueError("No non-dominated structures found")
    
    # Choose best structures
    constraints_dict = {
        "max_loss_usd_per_trade": constraints["max_loss_usd_per_trade"],
        "min_prob_profit": 0.0,
        "min_ev": 0.0
    }
    
    best_structures = choose_best_structure(
        non_dominated,
        constraints=constraints_dict,
        objective=objective
    )
    
    if not best_structures:
        raise ValueError("No structures meet constraints")
    
    # Rank top N
    top_n = constraints["top_n_output"]
    top_structures = rank_structures(best_structures, top_n=top_n)
    
    logger.info(f"Selected top {len(top_structures)} structures")
    
    # Add reason_selected to each structure
    for struct in top_structures:
        struct["reason_selected"] = get_reason_selected(
            struct,
            rank=struct["rank"],
            objective=objective
        )
    
    # SANITY ASSERTIONS (NON-NEGOTIABLE)
    logger.info("Running sanity assertions...")
    for struct in top_structures:
        try:
            assert_structure_sanity(
                struct,
                max_loss_usd=constraints["max_loss_usd_per_trade"]
            )
        except AssertionError as e:
            logger.error(f"Sanity check failed for rank {struct['rank']}: {e}")
            raise
    
    logger.info("All sanity checks passed ✓")
    
    # Format outputs
    formatted_structures = [format_structure_output(s) for s in top_structures]
    
    # Write outputs
    output_config = struct_config.get("output", {})
    run_dir = Path("runs") / campaign / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    # Write structures.json
    if output_config.get("structures_json", True):
        json_path = run_dir / "structures.json"
        write_structures_json(formatted_structures, json_path)
    
    # Write summary.md
    if output_config.get("summary_md", True):
        md_path = run_dir / "summary.md"
        metadata = {
            "run_id": run_id,
            "campaign": campaign,
            "p_event": p_event,
            "underlier": underlier,
            "spot_used": S0
        }
        write_summary_md(formatted_structures, md_path, metadata)
    
    # Write dry-run tickets
    if output_config.get("dry_run_tickets", True):
        ticket_path = run_dir / "dry_run_tickets.txt"
        write_dry_run_tickets(top_structures, ticket_path)
    
    # Write manifest with checksum
    manifest_writer = ManifestWriter(campaign, run_id)
    manifest = {
        "run_id": run_id,
        "campaign": campaign,
        "config_checksum": config_checksum,
        "config_version": config.get("config_version", "N/A"),
        "run_time_utc": run_time_utc,
        "mode": "crash_venture_v1",
        "inputs": {
            "p_event": p_event,
            "spot_price": S0,
            "atm_iv": sigma,
            "expiry_date": expiry_date,
            "days_to_expiry": days_to_expiry
        },
        "calibration": {
            "mu_calibrated": mu_calib,
            "p_achieved": p_achieved,
            "rng_seed": rng_seed
        },
        "n_candidates_generated": len(candidates),
        "n_candidates_evaluated": len(evaluated),
        "n_non_dominated": len(non_dominated),
        "n_output_structures": len(top_structures)
    }
    
    manifest_path = run_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    
    logger.info(f"✓ Run complete: {run_id}")
    logger.info(f"✓ Output directory: {run_dir}")
    logger.info(f"✓ Top {len(top_structures)} structures written")
    
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "top_structures": formatted_structures,
        "manifest": manifest
    }
