"""
CLI entrypoint for running forecast_arb engine.

Modes:
    oracle: Collect Kalshi probabilities as ground truth (no LLM)
    structure: Generate and evaluate option structures from oracle data
    paper: Legacy LLM forecasting mode (deprecated)
"""

import argparse
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import yaml

from ..kalshi.client import KalshiClient
from ..oracle.kalshi_oracle import collect_oracle_data
from ..structuring.event_map import enrich_oracle_data_with_mapping
from ..structuring.templates import generate_put_spread, generate_call_spread, generate_strangle
from ..structuring.calibrator import calibrate_drift
from ..structuring.evaluator import evaluate_structure
from ..structuring.router import choose_best_structure, rank_structures, generate_summary
from ..utils.manifest import ManifestWriter
from ..utils.database import Database


def setup_logging(log_level: str = "INFO"):
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def load_config(config_path: str) -> Dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def setup_determinism(run_id: str):
    """
    Set up deterministic RNG seeding.
    
    Args:
        run_id: Run identifier
    """
    # Derive seed from run_id
    rng_seed = abs(hash(run_id)) % (2**32)
    
    # Set seeds
    random.seed(rng_seed)
    np.random.seed(rng_seed)
    
    return rng_seed


def select_markets_by_buckets(
    client: KalshiClient,
    universe_config: Dict
) -> Dict[str, List[Dict]]:
    """
    Select markets by bucket configuration.
    
    Args:
        client: Kalshi client
        universe_config: Universe configuration
        
    Returns:
        Dict mapping bucket name to list of markets
    """
    buckets = universe_config.get("buckets", {})
    max_per_bucket = universe_config.get("max_markets_per_bucket", 5)
    status = universe_config.get("status", "open")
    
    markets_by_bucket = {}
    
    for bucket_name, bucket_config in buckets.items():
        series = bucket_config.get("series", [])
        tags = bucket_config.get("tags", [])
        tickers = bucket_config.get("tickers", [])
        
        # Fetch markets for this bucket
        markets = client.list_markets(
            series=series if series else None,
            tags=tags if tags else None,
            tickers=tickers if tickers else None,
            status=status,
            limit=max_per_bucket * 2  # Fetch more to filter later
        )
        
        # Limit to max_per_bucket
        markets_by_bucket[bucket_name] = markets[:max_per_bucket]
        
        logging.info(f"Bucket {bucket_name}: {len(markets_by_bucket[bucket_name])} markets")
    
    return markets_by_bucket


def run_oracle_mode(config_path: str):
    """
    Run in oracle mode: collect Kalshi probabilities as ground truth.
    
    Args:
        config_path: Path to configuration YAML
    """
    config = load_config(config_path)
    log_config = config.get("logging", {})
    setup_logging(log_config.get("level", "INFO"))
    
    logger = logging.getLogger(__name__)
    logger.info("Starting oracle mode")
    
    # Create run metadata
    campaign = config.get("campaign_name", "oracle")
    run_time_utc = datetime.now(timezone.utc).isoformat()
    run_id = f"{campaign}_{run_time_utc.replace(':', '').replace('-', '').replace('.', '')[:15]}"
    
    setup_determinism(run_id)
    
    # Initialize database
    db_config = config.get("storage", {})
    db = Database(db_path=db_config.get("path", "runs/forecasts.db"))
    
    # Initialize Kalshi client (uses official Trade API endpoint)
    kalshi_config = config.get("kalshi", {})
    
    # Client will use environment variables KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
    # Or pass directly if configured
    api_key = os.environ.get(kalshi_config.get("api_key_env", "KALSHI_API_KEY_ID"))
    private_key_path = os.environ.get(kalshi_config.get("private_key_path_env", "KALSHI_PRIVATE_KEY_PATH"))
    
    client = KalshiClient(
        api_key=api_key,
        private_key_path=private_key_path,
        rate_limit_per_second=kalshi_config.get("rate_limit_per_second", 10)
    )
    
    # Select markets
    universe_config = config.get("universe", {})
    markets_by_bucket = select_markets_by_buckets(client, universe_config)
    
    all_markets = []
    for markets in markets_by_bucket.values():
        all_markets.extend(markets)
    
    logger.info(f"Collecting oracle data for {len(all_markets)} markets")
    
    # Collect oracle data
    oracle_data = collect_oracle_data(client, all_markets, run_time_utc)
    
    # Enrich with underlier mappings
    enriched_data = enrich_oracle_data_with_mapping(oracle_data)
    
    # Store in database
    for data in enriched_data:
        db.insert_oracle_market(run_id, data)
    
    # Store run metadata
    manifest = {
        "run_id": run_id,
        "campaign": campaign,
        "n_markets": len(enriched_data),
        "mode": "oracle"
    }
    db.insert_run(run_id, campaign, run_time_utc, "oracle", config, manifest)
    
    logger.info(f"Oracle run complete: {run_id}")
    logger.info(f"Collected {len(enriched_data)} oracle probabilities")
    
    return run_id, enriched_data


def run_structure_mode(config_path: str, oracle_run_id: Optional[str] = None):
    """
    Run in structure mode: generate and evaluate option structures.
    
    Args:
        config_path: Path to configuration YAML
        oracle_run_id: Optional oracle run ID to load data from. If None, runs oracle mode first.
    """
    config = load_config(config_path)
    log_config = config.get("logging", {})
    setup_logging(log_config.get("level", "INFO"))
    
    logger = logging.getLogger(__name__)
    logger.info("Starting structure mode")
    
    # Initialize database
    db_config = config.get("storage", {})
    db = Database(db_path=db_config.get("path", "runs/forecasts.db"))
    
    # Get oracle data
    if oracle_run_id:
        logger.info(f"Loading oracle data from run: {oracle_run_id}")
        oracle_data = db.get_oracle_markets_by_run(oracle_run_id)
    else:
        logger.info("Running oracle mode first...")
        oracle_run_id, oracle_data = run_oracle_mode(config_path)
    
    if not oracle_data:
        logger.error("No oracle data available")
        return None
    
    # Create structure run
    campaign = config.get("campaign_name", "structure")
    run_time_utc = datetime.now(timezone.utc).isoformat()
    run_id = f"{campaign}_{run_time_utc.replace(':', '').replace('-', '').replace('.', '')[:15]}"
    
    rng_seed = setup_determinism(run_id)
    
    # Get structuring config
    struct_config = config.get("structuring", {})
    constraints = struct_config.get("constraints", {
        "max_loss_usd_per_trade": 500,
        "min_prob_profit": 0.4,
        "min_ev": 0
    })
    
    # Option chain assumptions (in real version, would fetch from broker)
    option_params = struct_config.get("option_params", {
        "S0": 500.0,  # Current underlier price
        "r": 0.05,    # Risk-free rate
        "T": 30/365,  # 30 days to expiry
        "sigma_vol": 0.15,  # Volatility assumption
        "n_paths": 30000  # Monte Carlo paths
    })
    
    all_structures = []
    
    for oracle_entry in oracle_data:
        market_id = oracle_entry.get("market_id")
        p_event = oracle_entry.get("p_event")
        underlier = oracle_entry.get("underlier", "SPY")
        expiry = oracle_entry.get("expiry", "2026-02-28")
        
        logger.info(f"Structuring for {market_id}: p_event={p_event:.3f}, underlier={underlier}")
        
        # Calibrate drift given p_event constraint
        S0 = option_params["S0"]
        T = option_params["T"]
        sigma = option_params["sigma_vol"]
        K_barrier = S0 * 1.05  # Assume event = "underlier > 5% gain"
        
        try:
            mu_calib, p_achieved = calibrate_drift(
                p_event=p_event,
                S0=S0,
                K_barrier=K_barrier,
                T=T,
                sigma=sigma,
                n_samples=10000,
                seed=rng_seed
            )
            
            logger.info(f"Calibrated drift: μ={mu_calib:.4f}, achieved p={p_achieved:.3f}")
        except Exception as e:
            logger.error(f"Calibration failed for {market_id}: {e}")
            continue
        
        # Generate candidate structures
        candidate_structures = []
        
        # Put spread (bearish)
        put_spread = generate_put_spread(
            underlier=underlier,
            expiry=expiry,
            S0=S0,
            K_long=S0 * 0.95,
            K_short=S0 * 0.90,
            r=option_params["r"],
            sigma=sigma,
            T=T
        )
        candidate_structures.append(put_spread)
        
        # Call spread (bullish)
        call_spread = generate_call_spread(
            underlier=underlier,
            expiry=expiry,
            S0=S0,
            K_long=S0 * 1.05,
            K_short=S0 * 1.10,
            r=option_params["r"],
            sigma=sigma,
            T=T
        )
        candidate_structures.append(call_spread)
        
        # Strangle (high volatility)
        strangle = generate_strangle(
            underlier=underlier,
            expiry=expiry,
            S0=S0,
            K_put=S0 * 0.90,
            K_call=S0 * 1.10,
            r=option_params["r"],
            sigma=sigma,
            T=T
        )
        candidate_structures.append(strangle)
        
        # Evaluate each structure
        evaluated = []
        for struct in candidate_structures:
            try:
                eval_result = evaluate_structure(
                    structure=struct,
                    mu=mu_calib,
                    sigma=sigma,
                    S0=S0,
                    T=T,
                    n_paths=option_params["n_paths"],
                    seed=rng_seed
                )
                evaluated.append(eval_result)
            except Exception as e:
                logger.error(f"Evaluation failed: {e}")
        
        # Choose best structures
        best = choose_best_structure(
            evaluated,
            constraints=constraints,
            objective="max_ev"
        )
        
        if best:
            ranked = rank_structures(best, top_n=3)
            all_structures.extend(ranked)
            
            # Store in database
            for struct in ranked:
                struct["oracle_run_id"] = oracle_run_id
                struct["original_market_id"] = market_id
                db.insert_structure(run_id, struct)
            
            logger.info(f"Found {len(ranked)} viable structures for {market_id}")
        else:
            logger.warning(f"No viable structures for {market_id}")
    
    # Store run metadata
    manifest = {
        "run_id": run_id,
        "campaign": campaign,
        "n_structures": len(all_structures),
        "mode": "structure",
        "oracle_run_id": oracle_run_id
    }
    db.insert_run(run_id, campaign, run_time_utc, "structure", config, manifest)
    
    # Generate summary
    if all_structures:
        summary = generate_summary(all_structures[:5])
        logger.info(f"\n{summary}")
    
    logger.info(f"Structure run complete: {run_id}")
    logger.info(f"Generated {len(all_structures)} option structures")
    
    return run_id


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Run forecast_arb engine"
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to configuration YAML file"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["oracle", "structure"],
        default="structure",
        help="Run mode: oracle (collect probabilities) or structure (generate options)"
    )
    parser.add_argument(
        "--oracle-run-id",
        type=str,
        default=None,
        help="Oracle run ID to use for structure mode (optional, will run oracle if not provided)"
    )
    
    args = parser.parse_args()
    
    if args.mode == "oracle":
        run_id, _ = run_oracle_mode(args.config)
        print(f"\nOracle run complete: {run_id}")
    elif args.mode == "structure":
        run_id = run_structure_mode(args.config, args.oracle_run_id)
        print(f"\nStructure run complete: {run_id}")
    else:
        raise ValueError(f"Unknown mode: {args.mode}")


if __name__ == "__main__":
    main()
