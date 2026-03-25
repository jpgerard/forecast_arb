"""
Run Crash Venture v1 with Real IBKR Snapshot + Kalshi Oracle

This script:
1. Creates/loads an IBKR snapshot for SPY options
2. Fetches Kalshi p_event (or uses fallback)
3. Runs Crash Venture v1 STRUCTURE mode with real market data
4. Validates all pricing rules and filters

NO YFINANCE. IBKR snapshot only.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.oracle.kalshi_oracle import KalshiOracle
from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter
from forecast_arb.structuring.snapshot_io import (
    load_snapshot,
    validate_snapshot,
    get_snapshot_metadata,
    get_expiries,
    compute_time_to_expiry
)


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
        
        exporter.export_snapshot(
            underlier=underlier,
            snapshot_time_utc=snapshot_time,
            dte_min=dte_min,
            dte_max=dte_max,
            strikes_below=30,
            strikes_above=30,
            out_path=output_path
        )
        
        exporter.disconnect()
        
        logger.info(f"✓ Snapshot created: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Failed to create snapshot: {e}")
        raise


def fetch_kalshi_p_event(market_ticker: Optional[str]) -> Optional[float]:
    """
    Fetch p_event from Kalshi.
    
    Args:
        market_ticker: Kalshi market ticker (e.g., "INXD-26FEB07-T4350")
        
    Returns:
        p_event or None if not found
    """
    if not market_ticker:
        logger.info("No Kalshi market ticker provided")
        return None
    
    try:
        api_key = os.environ.get("KALSHI_API_KEY")
        api_secret = os.environ.get("KALSHI_API_SECRET")
        
        if not api_key or not api_secret:
            logger.warning("Kalshi API credentials not found in environment")
            return None
        
        client = KalshiClient(api_key=api_key, api_secret=api_secret)
        oracle = KalshiOracle(client)
        
        # Get market by ticker
        markets = client.list_markets(tickers=[market_ticker], status="open", limit=1)
        
        if not markets:
            logger.warning(f"Kalshi market not found: {market_ticker}")
            return None
        
        market = markets[0]
        oracle_data = oracle.get_event_probability(market)
        
        if oracle_data:
            p_event = oracle_data['p_event']
            logger.info(f"✓ Kalshi p_event: {p_event:.3f} (ticker: {market_ticker})")
            logger.info(f"  Bid: {oracle_data['bid']:.3f}, Ask: {oracle_data['ask']:.3f}")
            logger.info(f"  Volume 24h: {oracle_data['volume_24h']:,}")
            return p_event
        
        return None
        
    except Exception as e:
        logger.error(f"Error fetching Kalshi data: {e}")
        return None


def run_crash_venture_v1_with_snapshot(
    config_path: str,
    snapshot_path: str,
    p_event: float,
    min_debit_per_contract: float = 30.0
) -> Dict:
    """
    Run Crash Venture v1 with snapshot-based structure generation.
    
    Args:
        config_path: Path to config YAML
        snapshot_path: Path to IBKR snapshot JSON
        p_event: Event probability
        min_debit_per_contract: Minimum debit filter
        
    Returns:
        Run results dict
    """
    # Import here to avoid circular dependencies
    from forecast_arb.engine.crash_venture_v1_snapshot import run_crash_venture_v1_snapshot
    
    logger.info("=" * 80)
    logger.info("RUNNING CRASH VENTURE V1 WITH IBKR SNAPSHOT")
    logger.info("=" * 80)
    
    result = run_crash_venture_v1_snapshot(
        config_path=config_path,
        snapshot_path=snapshot_path,
        p_event=p_event,
        min_debit_per_contract=min_debit_per_contract
    )
    
    return result


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Run Crash Venture v1 with real IBKR snapshot + Kalshi oracle"
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
        help="Path to existing snapshot JSON (if not provided, creates new)"
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
        help="IBKR TWS/Gateway host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--ibkr-port",
        type=int,
        default=7496,
        help="IBKR TWS/Gateway port (default: 7496 for live)"
    )
    
    # Kalshi oracle
    parser.add_argument(
        "--p-event-source",
        type=str,
        choices=["kalshi", "fallback"],
        default="fallback",
        help="Event probability source (default: fallback)"
    )
    parser.add_argument(
        "--kalshi-ticker",
        type=str,
        default=None,
        help="Kalshi market ticker (e.g., INXD-26FEB07-T4350)"
    )
    parser.add_argument(
        "--fallback-p",
        type=float,
        default=0.30,
        help="Fallback p_event if Kalshi unavailable (default: 0.30)"
    )
    
    # Filters
    parser.add_argument(
        "--min-debit-per-contract",
        type=float,
        default=10.0,
        help="Minimum debit per contract filter in USD (default: 10.0)"
    )
    
    # Config
    parser.add_argument(
        "--campaign-config",
        type=str,
        default="configs/structuring_crash_venture_v1_1.yaml",
        help="Path to campaign config YAML (default: configs/structuring_crash_venture_v1_1.yaml)"
    )
    
    args = parser.parse_args()
    
    setup_logging()
    
    logger.info("=" * 80)
    logger.info("CRASH VENTURE V1 - REAL CYCLE WITH IBKR SNAPSHOT")
    logger.info("=" * 80)
    logger.info("")
    
    # Step 1: Create or load snapshot
    logger.info("Step 1: IBKR Option Chain Snapshot")
    logger.info("-" * 80)
    
    try:
        snapshot_path = fetch_or_create_snapshot(
            underlier=args.underlier,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            snapshot_path=args.snapshot,
            ibkr_host=args.ibkr_host,
            ibkr_port=args.ibkr_port
        )
    except Exception as e:
        logger.error(f"❌ Snapshot creation/loading failed: {e}", exc_info=True)
        sys.exit(1)
    
    # Validate snapshot
    try:
        snapshot = load_snapshot(snapshot_path)
        validate_snapshot(snapshot)
        metadata = get_snapshot_metadata(snapshot)
        
        logger.info(f"✓ Snapshot loaded and validated")
        logger.info(f"  Underlier: {metadata['underlier']}")
        logger.info(f"  Spot Price: ${metadata['current_price']:.2f}")
        logger.info(f"  Snapshot Time: {metadata['snapshot_time']}")
        logger.info(f"  Expiries: {len(get_expiries(snapshot))}")
    except Exception as e:
        logger.error(f"❌ Snapshot validation failed: {e}", exc_info=True)
        sys.exit(1)
    
    logger.info("")
    
    # Step 2: Fetch p_event
    logger.info("Step 2: Event Probability (p_event)")
    logger.info("-" * 80)
    
    p_event = None
    is_fallback = False
    
    if args.p_event_source == "kalshi":
        p_event = fetch_kalshi_p_event(args.kalshi_ticker)
    
    if p_event is None:
        p_event = args.fallback_p
        is_fallback = True
        logger.warning("=" * 80)
        logger.warning("SMOKE TEST MODE: Using fallback p_event (no real Kalshi market data)")
        logger.warning(f"Fallback p_event: {p_event:.3f}")
        logger.warning("=" * 80)
    
    logger.info("")
    
    # Step 3: Run Crash Venture v1
    logger.info("Step 3: Run Crash Venture v1 STRUCTURE Mode")
    logger.info("-" * 80)
    logger.info(f"Campaign Config: {args.campaign_config}")
    logger.info(f"Snapshot: {snapshot_path}")
    logger.info(f"p_event: {p_event:.3f}")
    logger.info(f"Min Debit Per Contract: ${args.min_debit_per_contract:.2f}")
    logger.info("")
    
    try:
        result = run_crash_venture_v1_with_snapshot(
            config_path=args.campaign_config,
            snapshot_path=snapshot_path,
            p_event=p_event,
            min_debit_per_contract=args.min_debit_per_contract
        )
        
        # Display results
        logger.info("")
        logger.info("=" * 80)
        logger.info("✅ REAL CYCLE COMPLETE!")
        logger.info("=" * 80)
        logger.info(f"Run ID: {result['run_id']}")
        logger.info(f"Output Directory: {result['run_dir']}")
        logger.info(f"Top Structures: {len(result['top_structures'])}")
        logger.info("")
        
        # Display top structures
        for struct in result['top_structures']:
            rank = struct['rank']
            logger.info(f"--- Trade #{rank} ---")
            logger.info(f"  Expiry: {struct['expiry']}")
            logger.info(f"  Long Put: ${struct['strikes']['long_put']:.2f}")
            logger.info(f"  Short Put: ${struct['strikes']['short_put']:.2f}")
            
            # Print per-contract values (already in correct units from structures.json)
            debit_per_contract = struct['debit_per_contract']
            max_loss_per_contract = struct['max_loss_per_contract']
            max_gain_per_contract = struct['max_gain_per_contract']
            
            logger.info(f"  Debit (per contract): ${debit_per_contract:.2f}")
            logger.info(f"  Debit (per share): ${debit_per_contract/100:.4f}")
            logger.info(f"  Max Loss (per contract): ${max_loss_per_contract:.2f}")
            logger.info(f"  Max Loss (per share): ${max_loss_per_contract/100:.4f}")
            logger.info(f"  Max Gain (per contract): ${max_gain_per_contract:.2f}")
            logger.info(f"  Max Gain (per share): ${max_gain_per_contract/100:.4f}")
            logger.info(f"  EV: ${struct['ev_per_contract']:.2f}")
            logger.info(f"  EV/Dollar: {struct['ev_per_dollar']:.3f}")
            logger.info("")
        
        logger.info(f"📁 View outputs: {result['run_dir']}")
        logger.info("")
        
    except Exception as e:
        logger.error(f"❌ Run failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
