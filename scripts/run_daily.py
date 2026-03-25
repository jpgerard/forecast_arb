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
from forecast_arb.oracle.p_event_source import KalshiPEventSource, FallbackPEventSource, PEventResult
from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
from forecast_arb.structuring.snapshot_io import (
    load_snapshot,
    validate_snapshot,
    get_snapshot_metadata,
    get_expiries,
    compute_time_to_expiry
)
from forecast_arb.execution.tickets import from_candidate, to_dict
from forecast_arb.execution.review import format_review
from forecast_arb.execution.ibkr_submit import submit_tickets
from forecast_arb.utils.manifest import compute_config_checksum
from forecast_arb.core.latest import set_latest_run
from forecast_arb.core.index import load_index, append_run, write_index
from forecast_arb.core.run_summary import extract_summary_safe
from forecast_arb.options.event_def import create_terminal_below_event, create_event_spec, EventSpec
from forecast_arb.options.implied_prob import implied_prob_terminal_below
from forecast_arb.gating.edge_gate import gate
from forecast_arb.ibkr.quote_snapshot import fetch_quotes_for_candidates
from forecast_arb.risk.campaign_summary import get_campaign_risk_summary
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
        
        # Use tail_moneyness_floor for deeper OTM coverage (crash venture)
        # 0.25 = 25% below spot, ensures coverage for -20% moneyness target
        exporter.export_snapshot(
            underlier=underlier,
            snapshot_time_utc=snapshot_time,
            dte_min=dte_min,
            dte_max=dte_max,
            tail_moneyness_floor=0.25,  # Default to 25% below spot for crash venture
            out_path=output_path
        )
        
        exporter.disconnect()
        
        logger.info(f"✓ Snapshot created: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Failed to create snapshot: {e}")
        raise




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
        choices=["kalshi", "kalshi-auto", "fallback"],
        default="kalshi-auto",
        help="Event probability source: kalshi (manual ticker), kalshi-auto (auto-map), fallback (default: kalshi-auto)"
    )
    parser.add_argument(
        "--kalshi-ticker",
        type=str,
        default=None,
        help="Kalshi market ticker for manual mode (e.g., INXD-26FEB07-T4350)"
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
    
    # OrderIntent emission (new flow)
    parser.add_argument(
        "--emit-intent",
        action="store_true",
        help="Emit OrderIntent JSON for external execution (default: False)"
    )
    parser.add_argument(
        "--pick-rank",
        type=int,
        default=None,
        help="Select candidate by rank for intent emission"
    )
    parser.add_argument(
        "--pick-expiry",
        type=str,
        default=None,
        help="Select candidate by expiry (YYYYMMDD) for intent emission"
    )
    parser.add_argument(
        "--pick-long",
        type=float,
        default=None,
        help="Select candidate by long strike for intent emission"
    )
    parser.add_argument(
        "--pick-short",
        type=float,
        default=None,
        help="Select candidate by short strike for intent emission"
    )
    parser.add_argument(
        "--intent-out",
        type=str,
        default=None,
        help="Output path for order_intent.json (default: <run_dir>/order_intent.json)"
    )
    parser.add_argument(
        "--limit-start",
        type=float,
        default=None,
        help="Starting limit price for intent (default: 92%% of model debit)"
    )
    parser.add_argument(
        "--limit-max",
        type=float,
        default=None,
        help="Maximum limit price for intent (default: 95%% of model debit)"
    )
    parser.add_argument(
        "--qty",
        type=int,
        default=1,
        help="Quantity for intent (default: 1)"
    )
    parser.add_argument(
        "--guard-max-spread-width",
        type=float,
        default=0.10,
        help="Guard: maximum spread width as fraction of spot (default: 0.10)"
    )
    parser.add_argument(
        "--guard-require-executable-legs",
        action="store_true",
        default=True,
        help="Guard: require executable leg pricing (default: True)"
    )
    
    # Execution & Safety flags
    parser.add_argument(
        "--allow-fallback-trade",
        action="store_true",
        help="Allow trading with fallback p_event source (dev-only, default: False)"
    )
    parser.add_argument(
        "--review-only-structuring",
        action="store_true",
        help="Run structuring even when blocked for review purposes only (no executable orders, default: False)"
    )
    parser.add_argument(
        "--include-live-quotes",
        action="store_true",
        help="Include live quote snapshots for top candidates in review pack (default: False)"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["prod", "dev", "smoke"],
        default="dev",
        help="Run mode: prod (live), dev (testing), smoke (no real data) (default: dev)"
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Enable order submission (requires --confirm SUBMIT)"
    )
    parser.add_argument(
        "--confirm",
        type=str,
        default=None,
        help="Confirmation string (must be 'SUBMIT' when --submit is enabled)"
    )
    parser.add_argument(
        "--max-orders",
        type=int,
        default=None,
        help="Maximum number of orders to place (hard cap)"
    )
    parser.add_argument(
        "--max-debit-total",
        type=float,
        default=None,
        help="Maximum total debit across all orders in USD (hard cap)"
    )
    
    args = parser.parse_args()
    
    setup_logging()
    
    # DEPRECATION WARNING: Live submission from run_daily.py
    if args.submit and args.mode == "prod":
        logger.warning("=" * 80)
        logger.warning("⚠️  DEPRECATION WARNING")
        logger.warning("=" * 80)
        logger.warning("Live submission via run_daily.py is deprecated.")
        logger.warning("Please use forecast_arb/execution/execute_trade.py instead.")
        logger.warning("")
        logger.warning("New flow:")
        logger.warning("  1. Run run_daily.py with --emit-intent to generate order_intent.json")
        logger.warning("  2. Review the intent manually or with ChatGPT")
        logger.warning("  3. Execute via: python -m forecast_arb.execution.execute_trade --intent <path>")
        logger.warning("=" * 80)
        logger.warning("")
    
    # SAFETY: Check for conflicting flags
    if args.review_only_structuring and args.submit:
        logger.error("=" * 80)
        logger.error("❌ FATAL ERROR: Cannot submit in review-only mode")
        logger.error("=" * 80)
        logger.error("--review-only-structuring flag prevents order submission.")
        logger.error("Review-only mode generates structures for manual review ONLY.")
        logger.error("Remove --submit flag or --review-only-structuring flag.")
        logger.error("")
        sys.exit(2)
    
    # Display review-only banner if enabled
    if args.review_only_structuring:
        logger.info("=" * 80)
        logger.info("⚠️  REVIEW-ONLY MODE ENABLED")
        logger.info("=" * 80)
        logger.info("Structuring will run even if blocked by edge gate or external policy.")
        logger.info("NO executable orders will be generated.")
        logger.info("Structures are for REVIEW PURPOSES ONLY.")
        logger.info("=" * 80)
        logger.info("")
    
    # Load config early for resolution
    with open(args.campaign_config, "r") as f:
        config = yaml.safe_load(f)
    
    # Compute config checksum early (needed for gate-blocked run IDs)
    config_checksum = compute_config_checksum(config)
    
    # STEP: Resolve min_debit_per_contract with explicit CLI priority
    # CLI flag always wins; config only supplies value if CLI flag not provided
    min_debit_per_contract = None
    min_debit_source = None
    
    # Check if CLI argument was explicitly provided (not just default)
    # We need to check if it differs from the default or use a sentinel
    cli_default = 10.0
    if args.min_debit_per_contract != cli_default:
        # User explicitly provided a different value
        min_debit_per_contract = args.min_debit_per_contract
        min_debit_source = "cli"
    else:
        # Check if config has it
        config_value = config.get("min_debit_per_contract")
        if config_value is not None:
            min_debit_per_contract = float(config_value)
            min_debit_source = "config"
        else:
            # Use CLI default
            min_debit_per_contract = args.min_debit_per_contract
            min_debit_source = "default"
    
    # SANITY GUARD: Check for unusually high values (likely cents vs dollars confusion)
    if min_debit_per_contract > 500:
        logger.warning("=" * 80)
        logger.warning(
            "min_debit_per_contract=%.2f USD is unusually high. "
            "This value is interpreted as USD per contract (not cents). "
            "Did you mean %.2f?",
            min_debit_per_contract,
            min_debit_per_contract / 100
        )
        logger.warning("=" * 80)
    
    # HARD STOP in prod mode for safety
    if args.mode == "prod" and min_debit_per_contract > 1000:
        logger.error("❌ min_debit_per_contract=%.2f exceeds hard safety limit (1000) in prod mode", min_debit_per_contract)
        sys.exit(1)
    
    logger.info("=" * 80)
    logger.info("CRASH VENTURE V1 - REAL CYCLE WITH IBKR SNAPSHOT")
    logger.info("=" * 80)
    logger.info(f"Min Debit Per Contract: ${min_debit_per_contract:.2f} (source: {min_debit_source})")
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
    
    # Step 2: Fetch p_event using unified p_event_source
    logger.info("Step 2: Event Probability (p_event)")
    logger.info("-" * 80)
    
    p_event_result = None
    p_event = None
    p_event_source_actual = None
    kalshi_auto_mapping = None  # Store mapping details for audit
    
    # Determine allow_proxy based on mode
    allow_proxy = (args.p_event_source == "kalshi-auto")
    
    # Handle different p_event sources
    if args.p_event_source == "fallback":
        # Fallback-only mode
        logger.info(f"Using fallback p_event source (--p-event-source={args.p_event_source})")
        fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
        p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
        p_event = p_event_result.p_event
        p_event_source_actual = p_event_result.source
        
        # Log unified P_EVENT_SOURCE line
        logger.info("")
        logger.info(f"P_EVENT_SOURCE: mode={args.p_event_source} source={p_event_result.source} exact=no proxy=no value={p_event:.3f} confidence={p_event_result.confidence:.2f}")
        logger.info("")
        
    elif args.p_event_source == "kalshi-auto":
        # Auto-mapping mode - find Kalshi market automatically
        logger.info("=" * 80)
        logger.info("KALSHI AUTO-MAPPING MODE")
        logger.info("=" * 80)
        logger.info("Finding Kalshi market automatically based on event parameters...")
        logger.info("")
        
        # We need event parameters, but they're computed later in edge gating
        # So we need to peek ahead and get them now
        edge_gating_config = config.get("edge_gating", {})
        event_moneyness_auto = edge_gating_config.get("event_moneyness", -0.15)
        
        # Get event parameters early for auto-mapping
        from forecast_arb.structuring.expiry_selection import select_best_expiry
        target_dte_midpoint_auto = (args.dte_min + args.dte_max) // 2
        
        target_expiry_auto, expiry_diagnostics_auto = select_best_expiry(
            snapshot=snapshot,
            target_dte=target_dte_midpoint_auto,
            dte_min=args.dte_min,
            dte_max=args.dte_max
        )
        
        if target_expiry_auto is None:
            logger.warning("⚠️  Cannot auto-map: no expiry available")
            logger.warning(f"   Diagnostics: {expiry_diagnostics_auto}")
            # Fall back to fallback source
            fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
            p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
            p_event = p_event_result.p_event
            p_event_source_actual = p_event_result.source
        else:
            # Convert SPY to SPX (SPX = SPY × 10 approximately)
            spot_spy = metadata['current_price']
            spot_spx = spot_spy * 10  # Approximate conversion
            
            logger.info(f"Event Parameters:")
            logger.info(f"  SPY Spot: ${spot_spy:.2f}")
            logger.info(f"  SPX Spot (estimated): ${spot_spx:.2f}")
            logger.info(f"  Moneyness: {event_moneyness_auto:.2%}")
            logger.info(f"  Expiry: {target_expiry_auto}")
            logger.info("")
            
            # Parse expiry to date object
            from datetime import datetime as dt_parser
            expiry_date = dt_parser.strptime(target_expiry_auto, "%Y%m%d").date()
            
            # Create event definition for p_event_source
            event_def_for_mapping = {
                "type": "index_drawdown",
                "index": "SPX",
                "threshold_pct": event_moneyness_auto,
                "expiry": expiry_date
            }
            
            try:
                # Use p_event_source with allow_proxy=True
                api_key_id = os.environ.get("KALSHI_API_KEY_ID")
                private_key = os.environ.get("KALSHI_PRIVATE_KEY")
                
                if not api_key_id or not private_key:
                    logger.warning("⚠️  Kalshi API credentials not found - falling back")
                    fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
                    p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
                    p_event = p_event_result.p_event
                    p_event_source_actual = p_event_result.source
                else:
                    client = KalshiClient(api_key=api_key_id, private_key_str=private_key)
                    kalshi_source = KalshiPEventSource(client, allow_proxy=True)
                    
                    # Compute horizon_days
                    from datetime import datetime as dt_parser
                    snapshot_dt = dt_parser.fromisoformat(metadata['snapshot_time'].replace('Z', '+00:00'))
                    horizon_days = int((expiry_date - snapshot_dt.date()).days)
                    
                    # Get p_event using unified source
                    p_event_result = kalshi_source.get_p_event(
                        event_definition=event_def_for_mapping,
                        spot_spx=spot_spx,
                        horizon_days=horizon_days,
                        max_mapping_error=0.10
                    )
                    
                    # Extract p_event and metadata
                    p_event_source_actual = p_event_result.source
                    
                    # CRITICAL SAFETY CHECK: Classify response
                    has_exact = (p_event_result.p_event is not None)
                    has_proxy = ("p_external_proxy" in p_event_result.metadata)
                    
                    # INVARIANT: p_external is set ONLY when is_exact == True
                    logger.info("")
                    logger.info("=" * 80)
                    logger.info("P_EVENT_CLASSIFICATION")
                    logger.info("=" * 80)
                    logger.info(f"  exact_match: {'YES' if has_exact else 'NO'}")
                    logger.info(f"  proxy_present: {'YES' if has_proxy else 'NO'}")
                    logger.info(f"  p_external_authoritative: {'YES' if has_exact else 'NO'}")
                    logger.info("")
                    
                    if has_exact:
                        # EXACT MATCH: Use p_event as authoritative p_external
                        p_event = p_event_result.p_event
                        
                        # ASSERTION: Verify p_event is not None when we think we have exact match
                        assert p_event is not None, "INVARIANT VIOLATION: has_exact=True but p_event is None"
                        
                        logger.info(f"✓ EXACT MATCH: p_external={p_event:.3f} (authoritative)")
                        logger.info(f"  Ticker: {p_event_result.metadata.get('market_ticker')}")
                        logger.info(f"  Series: {p_event_result.metadata.get('source_series')}")
                        logger.info("")
                        
                        # Store mapping for audit
                        kalshi_auto_mapping = {
                            "ticker": p_event_result.metadata.get('market_ticker'),
                            "source_series": p_event_result.metadata.get('source_series'),
                            "exact_match": True
                        }
                    elif has_proxy:
                        # PROXY AVAILABLE: Do NOT use for p_external
                        proxy_value = p_event_result.metadata['p_external_proxy']
                        
                        logger.warning("=" * 80)
                        logger.warning("⚠️  PROXY PROBABILITY DETECTED (NOT EXACT MATCH)")
                        logger.warning("=" * 80)
                        logger.warning(f"  Proxy value: {proxy_value:.3f}")
                        logger.warning(f"  Proxy method: {p_event_result.metadata.get('proxy_method')}")
                        logger.warning(f"  Proxy series: {p_event_result.metadata.get('proxy_series')}")
                        logger.warning(f"  Proxy confidence: {p_event_result.metadata.get('proxy_confidence', 0):.2f} (LOW)")
                        logger.warning("")
                        logger.warning("  ⚠️  POLICY: Proxy NOT used as authoritative p_external")
                        logger.warning("  ⚠️  Falling back to fallback p_event for safety")
                        logger.warning("=" * 80)
                        logger.warning("")
                        
                        # Use fallback for actual p_event (POLICY: proxy not authoritative)
                        p_event = args.fallback_p
                        
                        # ASSERTION: p_event must NOT be the proxy value
                        assert p_event != proxy_value, "INVARIANT VIOLATION: p_event was set to proxy value"
                        
                        logger.info(f"  Using fallback p_event: {p_event:.3f} (source: fallback, NOT proxy)")
                        logger.info("")
                        
                        # Store proxy metadata for review only (informational)
                        kalshi_auto_mapping = {
                            "exact_match": False,
                            "proxy_available": True,
                            "proxy_p_event": proxy_value,
                            "proxy_method": p_event_result.metadata.get('proxy_method'),
                            "proxy_series": p_event_result.metadata.get('proxy_series'),
                            "proxy_confidence": p_event_result.metadata.get('proxy_confidence'),
                            "proxy_market_ticker": p_event_result.metadata.get('proxy_market_ticker')
                        }
                    else:
                        # NO MATCH: Use fallback
                        logger.warning("⚠️  No Kalshi match found (exact or proxy)")
                        p_event = args.fallback_p
                        logger.info(f"  Using fallback p_event: {p_event:.3f}")
                        logger.info("")
                        
                        kalshi_auto_mapping = {
                            "exact_match": False,
                            "proxy_available": False,
                            "warnings": p_event_result.warnings
                        }
                        
            except Exception as e:
                logger.error(f"Kalshi source failed: {e}", exc_info=True)
                fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
                p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
                p_event = p_event_result.p_event
                p_event_source_actual = p_event_result.source
                kalshi_auto_mapping = {
                    "error": f"EXCEPTION: {str(e)}"
                }
        
        # Log unified P_EVENT_SOURCE line with classification
        if p_event_result:
            has_exact = (p_event_result.p_event is not None)
            has_proxy = ("p_external_proxy" in p_event_result.metadata)
            proxy_val = p_event_result.metadata.get('p_external_proxy', 'N/A')
            proxy_conf = p_event_result.metadata.get('proxy_confidence', 0.0)
            
            logger.info("")
            logger.info("=" * 80)
            logger.info("P_EVENT_SOURCE_SUMMARY")
            logger.info("=" * 80)
            logger.info(f"  Mode: {args.p_event_source}")
            logger.info(f"  Source: {p_event_source_actual}")
            logger.info(f"  Exact Match: {'YES' if has_exact else 'NO'}")
            logger.info(f"  Proxy Present: {'YES' if has_proxy else 'NO'}")
            logger.info(f"  p_external Value: {p_event:.3f} (authoritative: {'YES' if has_exact else 'NO'})")
            logger.info(f"  Confidence: {p_event_result.confidence:.2f}")
            
            if has_proxy:
                logger.info("")
                logger.info("  PROXY DETAILS (informational only, NOT authoritative):")
                proxy_val_str = f"{proxy_val:.3f}" if isinstance(proxy_val, float) else str(proxy_val)
                logger.info(f"    Proxy value: {proxy_val_str}")
                logger.info(f"    Proxy confidence: {proxy_conf:.2f} (LOW)")
                logger.info(f"    Proxy method: {p_event_result.metadata.get('proxy_method', 'N/A')}")
                logger.info(f"    ⚠️  WARNING: Proxy NOT used for p_external")
            
            logger.info("=" * 80)
            logger.info("")
            
            # SAFETY ASSERTION: Verify p_external is None or came from exact match
            if not has_exact and p_event_source_actual == "kalshi":
                # If source is kalshi but no exact match, p_event must be fallback
                assert p_event == args.fallback_p, \
                    f"INVARIANT VIOLATION: No exact Kalshi match but p_event={p_event} != fallback={args.fallback_p}"
    
    elif args.p_event_source == "kalshi":
        # Kalshi exact-only mode (proxy disabled)
        # This mode uses manual ticker if provided, otherwise tries auto-mapping without proxy
        logger.info("Kalshi exact-only mode (proxy disabled)")
        
        try:
            api_key_id = os.environ.get("KALSHI_API_KEY_ID")
            private_key = os.environ.get("KALSHI_PRIVATE_KEY")
            
            if not api_key_id or not private_key:
                logger.warning("⚠️  Kalshi API credentials not found - falling back")
                fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
                p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
                p_event = p_event_result.p_event
                p_event_source_actual = p_event_result.source
            else:
                # For now, use fallback (manual ticker mode not fully implemented)
                logger.warning("⚠️  Kalshi manual ticker mode not yet supported - falling back")
                fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
                p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
                p_event = p_event_result.p_event
                p_event_source_actual = p_event_result.source
                
        except Exception as e:
            logger.error(f"Kalshi source failed: {e}", exc_info=True)
            fallback_source = FallbackPEventSource(default_p_event=args.fallback_p)
            p_event_result = fallback_source.get_p_event(event_definition={}, fallback_value=args.fallback_p)
            p_event = p_event_result.p_event
            p_event_source_actual = p_event_result.source
        
        # Log unified P_EVENT_SOURCE line
        if p_event_result:
            logger.info("")
            logger.info(f"P_EVENT_SOURCE: mode={args.p_event_source} source={p_event_source_actual} exact={'yes' if p_event_result.p_event else 'no'} proxy=no value={p_event:.3f} confidence={p_event_result.confidence:.2f}")
            logger.info("")
    
    logger.info("")
    
    # Step 2.5: Options-Implied Probability & Edge Gating
    logger.info("Step 2.5: Options-Implied Probability & Edge Gating")
    logger.info("-" * 80)
    
    # Get edge gating config
    edge_gating_config = config.get("edge_gating", {})
    event_moneyness = edge_gating_config.get("event_moneyness", -0.15)
    min_edge = edge_gating_config.get("min_edge", 0.05)
    min_confidence = edge_gating_config.get("min_confidence", 0.60)
    
    logger.info(f"Edge Gating Config:")
    logger.info(f"  event_moneyness: {event_moneyness:.2%}")
    logger.info(f"  min_edge: {min_edge:.2%}")
    logger.info(f"  min_confidence: {min_confidence:.2%}")
    logger.info("")
    
    # Pick the expiry to use for p_implied calculation using unified selection
    from forecast_arb.structuring.expiry_selection import select_best_expiry
    
    # Compute target DTE (midpoint of range)
    target_dte_midpoint = (args.dte_min + args.dte_max) // 2
    
    target_expiry, expiry_diagnostics = select_best_expiry(
        snapshot=snapshot,
        target_dte=target_dte_midpoint,
        dte_min=args.dte_min,
        dte_max=args.dte_max
    )
    
    if target_expiry is None:
        logger.warning("⚠️  select_best_expiry returned None")
        logger.warning(f"   Diagnostics: {expiry_diagnostics}")
        # Fallback to first available expiry
        expiries = get_expiries(snapshot)
        target_expiry = sorted(expiries)[0] if expiries else None
    
    p_implied_value = None
    p_implied_confidence = 0.0
    p_implied_warnings = []
    gate_decision = None
    
    if target_expiry:
        logger.info(f"Using expiry {target_expiry} for p_implied calculation")
        
        # Create EventSpec - SINGLE SOURCE OF TRUTH for event parameters
        try:
            event_spec = create_event_spec(
                underlier=metadata['underlier'],
                expiry=target_expiry,
                spot=metadata['current_price'],
                moneyness=event_moneyness
            )
            
            logger.info(f"Event: P({event_spec.underlier} < ${event_spec.threshold:.2f} at {event_spec.expiry})")
            logger.info(f"  Spot: ${event_spec.spot:.2f}, Moneyness: {event_spec.moneyness:.2%}")
            logger.info(f"  Threshold: ${event_spec.threshold:.2f} = {event_spec.spot:.2f} × {1+event_spec.moneyness:.4f}")
            
            # Create event_def for backward compat with p_implied_artifact
            event_def = create_terminal_below_event(
                underlier=metadata['underlier'],
                expiry=target_expiry,
                spot=metadata['current_price'],
                event_moneyness=event_moneyness
            )
            
            # VALIDATE: Ensure event_def matches EventSpec (detect any threshold recomputation)
            event_spec.validate_threshold_consistency(event_def.threshold, tolerance=0.01)
            
            # Compute p_implied
            p_implied_value, p_implied_confidence, p_implied_warnings = implied_prob_terminal_below(
                snapshot=snapshot,
                expiry=target_expiry,
                threshold=event_def.threshold,
                r=0.0
            )
            
            if p_implied_value is not None:
                logger.info(f"✓ p_implied: {p_implied_value:.3f} (confidence: {p_implied_confidence:.2f})")
            else:
                logger.warning(f"⚠️  p_implied calculation failed")
                
            if p_implied_warnings:
                for warning in p_implied_warnings:
                    logger.warning(f"  Warning: {warning}")
            
            # Create p_implied_artifact for review pack (define here so it's always available)
            p_implied_artifact = {
                "p_event": p_implied_value,
                "confidence": p_implied_confidence,
                "warnings": p_implied_warnings,
                "event_definition": event_def.to_dict(),
                "timestamp_utc": datetime.now(timezone.utc).isoformat()
            }
                    
        except Exception as e:
            logger.error(f"Failed to compute p_implied: {e}", exc_info=True)
            p_implied_value = None
            p_implied_confidence = 0.0
            p_implied_warnings = [f"EXCEPTION: {str(e)}"]
            # Create p_implied_artifact even on exception
            p_implied_artifact = {
                "p_event": None,
                "confidence": 0.0,
                "warnings": [f"EXCEPTION: {str(e)}"],
                "event_definition": None,
                "timestamp_utc": datetime.now(timezone.utc).isoformat()
            }
    else:
        logger.warning("⚠️  No expiry available for p_implied calculation")
        p_implied_warnings = ["NO_EXPIRY_AVAILABLE"]
        # Create p_implied_artifact for no expiry case
        p_implied_artifact = {
            "p_event": None,
            "confidence": 0.0,
            "warnings": ["NO_EXPIRY_AVAILABLE"],
            "event_definition": None,
            "timestamp_utc": datetime.now(timezone.utc).isoformat()
        }
    
    # Create PEventResult objects for gating - CRITICAL: p_event only when exact match
    p_external_metadata = {"source": p_event_source_actual}
    
    # Include proxy metadata if available - check BOTH kalshi_auto_mapping AND p_event_result.metadata
    # This ensures proxy is surfaced even when source ends up as "fallback"
    if kalshi_auto_mapping and kalshi_auto_mapping.get("proxy_available"):
        # From kalshi_auto_mapping
        p_external_metadata.update({
            "p_external_proxy": kalshi_auto_mapping.get("proxy_p_event"),
            "proxy_method": kalshi_auto_mapping.get("proxy_method"),
            "proxy_series": kalshi_auto_mapping.get("proxy_series"),
            "proxy_confidence": kalshi_auto_mapping.get("proxy_confidence"),
            "proxy_market_ticker": kalshi_auto_mapping.get("proxy_market_ticker"),
        })
    
    # Also check p_event_result.metadata directly (in case it came from different path)
    if p_event_result and "p_external_proxy" in p_event_result.metadata:
        p_external_metadata.update({
            "p_external_proxy": p_event_result.metadata.get("p_external_proxy"),
            "proxy_method": p_event_result.metadata.get("proxy_method"),
            "proxy_series": p_event_result.metadata.get("proxy_series"),
            "proxy_confidence": p_event_result.metadata.get("proxy_confidence"),
            "proxy_market_ticker": p_event_result.metadata.get("proxy_market_ticker"),
            "proxy_horizon_days": p_event_result.metadata.get("proxy_horizon_days")
        })
    
    # CRITICAL FIX: p_event may ONLY be populated when is_exact == True
    # Check if this was an exact Kalshi match
    is_exact_kalshi = (
        p_event_source_actual == "kalshi" and 
        kalshi_auto_mapping is not None and 
        kalshi_auto_mapping.get("exact_match") == True
    )
    
    # Determine p_event value and confidence
    if is_exact_kalshi:
        # Exact Kalshi match - use the value
        p_external_value = p_event
        p_external_confidence = 0.7
    else:
        # NOT exact - p_event must be None
        p_external_value = None
        p_external_confidence = 0.0
        # Store the fallback value in metadata only
        if p_event is not None:
            p_external_metadata["p_external_fallback"] = p_event
    
    p_external_result = PEventResult(
        p_event=p_external_value,  # None unless exact match
        source=p_event_source_actual,
        confidence=p_external_confidence,  # 0.0 unless exact match
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata=p_external_metadata,
        fallback_used=(p_event_source_actual == "fallback")
    )
    
    p_implied_result = PEventResult(
        p_event=p_implied_value,
        source="options_implied",
        confidence=p_implied_confidence,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"warnings": p_implied_warnings},
        fallback_used=False
    ) if p_implied_value is not None else None
    
    # Apply edge gate
    gate_decision = gate(
        p_external=p_external_result,
        p_implied=p_implied_result,
        min_edge=min_edge,
        min_confidence=min_confidence
    )
    
    logger.info("")
    logger.info(f"🚦 Gate Decision: {gate_decision.decision}")
    logger.info(f"   Reason: {gate_decision.reason}")
    if gate_decision.edge is not None:
        logger.info(f"   Edge: {gate_decision.edge:.3f} ({gate_decision.edge*10000:.0f} bps)")
    logger.info(f"   p_external: {gate_decision.p_external:.3f}" if gate_decision.p_external else "   p_external: None")
    logger.info(f"   p_implied: {gate_decision.p_implied:.3f}" if gate_decision.p_implied else "   p_implied: None")
    logger.info(f"   Confidence (external): {gate_decision.confidence_external:.2f}")
    logger.info(f"   Confidence (implied): {gate_decision.confidence_implied:.2f}" if gate_decision.confidence_implied is not None else "   Confidence (implied): N/A")
    logger.info(f"   Confidence (gate): {gate_decision.confidence:.2f}")
    logger.info("")
    
    # Determine if we would block the trade (for review-only mode decision)
    would_block_trade = False
    would_have_traded = False  # Track if gate+policy would have allowed trade
    block_type = None
    block_reason_detail = None
    
    # Short-circuit: If edge gate blocked, skip external source policy check
    external_source_policy = None
    external_source_blocked = False
    external_source_policy_skipped = False
    
    if gate_decision.decision == "NO_TRADE":
        # Edge gate blocked
        would_block_trade = True
        block_type = "EDGE_GATE_BLOCKED"
        block_reason_detail = gate_decision.reason
        
        # In review-only mode, we still run structuring
        if args.review_only_structuring:
            logger.warning("=" * 80)
            logger.warning("⚠️  EDGE GATE BLOCKED TRADE (REVIEW-ONLY MODE)")
            logger.warning("=" * 80)
            logger.warning(f"Reason: {gate_decision.reason}")
            logger.warning("Proceeding to structuring for REVIEW PURPOSES ONLY.")
            logger.warning("")
        else:
            # Normal mode - skip structuring
            logger.warning("=" * 80)
            logger.warning("⚠️  EDGE GATE BLOCKED TRADE")
            logger.warning("=" * 80)
            logger.warning(f"Reason: {gate_decision.reason}")
            logger.warning("Skipping external source policy check and structuring engine.")
            logger.warning("")
        
        external_source_policy_skipped = True
        
        # Create a minimal run directory for gate-blocked runs
        # Generate run ID inline (same pattern as engine code)
        gate_timestamp = datetime.now(timezone.utc).isoformat().replace(':', '').replace('-', '').replace('.', '')[:15]
        gate_run_id = f"crash_venture_v1_{config_checksum}_{gate_timestamp}"
        gate_run_dir = Path(f"runs/crash_venture_v1/{gate_run_id}")
        gate_run_dir.mkdir(parents=True, exist_ok=True)
        gate_artifacts_dir = gate_run_dir / "artifacts"
        gate_artifacts_dir.mkdir(exist_ok=True)
        
        # Write gate decision
        gate_decision_path = gate_artifacts_dir / "gate_decision.json"
        with open(gate_decision_path, "w") as f:
            json.dump(gate_decision.to_dict(), f, indent=2)
        logger.info(f"✓ Gate decision written: {gate_decision_path}")
        
        # Write p_event_implied
        p_implied_artifact = {
            "p_event": p_implied_value,
            "confidence": p_implied_confidence,
            "warnings": p_implied_warnings,
            "event_definition": event_def.to_dict() if target_expiry else None,
            "timestamp_utc": datetime.now(timezone.utc).isoformat()
        }
        p_implied_path = gate_artifacts_dir / "p_event_implied.json"
        with open(p_implied_path, "w") as f:
            json.dump(p_implied_artifact, f, indent=2)
        logger.info(f"✓ p_event_implied written: {p_implied_path}")
        
        # Write external source policy artifact
        external_source_artifact = {
            "source": p_external_result.source,
            "policy": external_source_policy,
            "blocked": external_source_blocked,
            "allow_fallback_trade": args.allow_fallback_trade,
            "timestamp_utc": datetime.now(timezone.utc).isoformat()
        }
        external_source_path = gate_artifacts_dir / "external_source_policy.json"
        with open(external_source_path, "w") as f:
            json.dump(external_source_artifact, f, indent=2)
        logger.info(f"✓ External source policy written: {external_source_path}")
        
        gate_artifacts_written = True
        
        # Determine whether to skip structuring
        if args.review_only_structuring:
            # Review-only mode: proceed to structuring
            skip_structuring = False
            result = None  # Will be populated by structuring engine
        else:
            # Normal mode: skip structuring
            # Create a synthetic NO_TRADE result to continue the flow
            result = {
                "ok": True,
                "decision": "NO_TRADE",
                "reason": f"{block_type}: {block_reason_detail}",
                "warnings": [f"{block_type}: {block_reason_detail}"],
                "candidates": [],
                "filtered_out": 0,
                "filtered_reasons": [],
                "run_id": gate_run_id,
                "run_dir": str(gate_run_dir),
                "top_structures": [],
                "manifest": {},
                "debug": {
                    "gate_decision": gate_decision.to_dict(),
                    "p_external": p_external_result.to_dict(),
                    "p_implied": p_implied_result.to_dict() if p_implied_result else None,
                    "external_source_policy": external_source_artifact
                }
            }
            
            # Skip to Step 4 (ticket generation will be empty)
            decision = "NO_TRADE"
            reason = f"{block_type}: {block_reason_detail}"
            skip_structuring = True
        
    else:
        # Edge gate passed - run Step 2.6: External Source Policy
        logger.info("Step 2.6: External Source Policy")
        logger.info("-" * 80)
        
        external_source_policy = "OK"
        external_source_blocked = False
        
        # Check if using fallback source and if trades are allowed
        if p_external_result.source == "fallback" and not args.allow_fallback_trade:
            external_source_policy = "BLOCKED_FALLBACK"
            external_source_blocked = True
            logger.warning("⚠️  External source is FALLBACK and --allow-fallback-trade not set")
            logger.warning("   Trading with fallback p_event is BLOCKED by policy")
            logger.info(f"   Source: {p_external_result.source}")
            logger.info(f"   Allow Fallback Trade: {args.allow_fallback_trade}")
        else:
            logger.info(f"✓ External source policy: {external_source_policy}")
            logger.info(f"   Source: {p_external_result.source}")
            logger.info(f"   Allow Fallback Trade: {args.allow_fallback_trade}")
        
        logger.info("")
        
        # Check if external source policy blocks
        if external_source_blocked:
            would_block_trade = True
            block_type = "EXTERNAL_SOURCE_BLOCKED"
            block_reason_detail = external_source_policy
            
            # CRITICAL FIX: In review-only mode, proceed to structuring
            if args.review_only_structuring:
                logger.warning("=" * 80)
                logger.warning("⚠️  EXTERNAL SOURCE POLICY BLOCKED TRADE (REVIEW-ONLY MODE)")
                logger.warning("=" * 80)
                logger.warning(f"Reason: {external_source_policy}")
                logger.warning("Proceeding to structuring for REVIEW PURPOSES ONLY.")
                logger.warning("")
                skip_structuring = False  # PROCEED to structuring in review-only mode
            else:
                # Normal mode - skip structuring and write artifacts
                logger.warning("=" * 80)
                logger.warning("⚠️  EXTERNAL SOURCE POLICY BLOCKED TRADE")
                logger.warning("=" * 80)
                logger.warning(f"Reason: {external_source_policy}")
                logger.warning("Skipping structuring engine. Writing artifacts.")
                logger.warning("")
                
                # Create run directory and write artifacts
                gate_timestamp = datetime.now(timezone.utc).isoformat().replace(':', '').replace('-', '').replace('.', '')[:15]
                gate_run_id = f"crash_venture_v1_{config_checksum}_{gate_timestamp}"
                gate_run_dir = Path(f"runs/crash_venture_v1/{gate_run_id}")
                gate_run_dir.mkdir(parents=True, exist_ok=True)
                gate_artifacts_dir = gate_run_dir / "artifacts"
                gate_artifacts_dir.mkdir(exist_ok=True)
                
                # Write gate decision
                gate_decision_path = gate_artifacts_dir / "gate_decision.json"
                with open(gate_decision_path, "w") as f:
                    json.dump(gate_decision.to_dict(), f, indent=2)
                logger.info(f"✓ Gate decision written: {gate_decision_path}")
                
                # Write p_event_implied
                p_implied_artifact = {
                    "p_event": p_implied_value,
                    "confidence": p_implied_confidence,
                    "warnings": p_implied_warnings,
                    "event_definition": event_def.to_dict() if target_expiry else None,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat()
                }
                p_implied_path = gate_artifacts_dir / "p_event_implied.json"
                with open(p_implied_path, "w") as f:
                    json.dump(p_implied_artifact, f, indent=2)
                logger.info(f"✓ p_event_implied written: {p_implied_path}")
                
                # Write external source policy artifact
                external_source_artifact = {
                    "source": p_external_result.source,
                    "policy": external_source_policy,
                    "blocked": external_source_blocked,
                    "allow_fallback_trade": args.allow_fallback_trade,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat()
                }
                external_source_path = gate_artifacts_dir / "external_source_policy.json"
                with open(external_source_path, "w") as f:
                    json.dump(external_source_artifact, f, indent=2)
                logger.info(f"✓ External source policy written: {external_source_path}")
                
                # Create synthetic NO_TRADE result
                result = {
                    "ok": True,
                    "decision": "NO_TRADE",
                    "reason": f"{block_type}: {block_reason_detail}",
                    "warnings": [f"{block_type}: {block_reason_detail}"],
                    "candidates": [],
                    "filtered_out": 0,
                    "filtered_reasons": [],
                    "run_id": gate_run_id,
                    "run_dir": str(gate_run_dir),
                    "top_structures": [],
                    "manifest": {},
                    "debug": {
                        "gate_decision": gate_decision.to_dict(),
                        "p_external": p_external_result.to_dict(),
                        "p_implied": p_implied_result.to_dict() if p_implied_result else None,
                        "external_source_policy": external_source_artifact
                    }
                }
                
                decision = "NO_TRADE"
                reason = f"{block_type}: {block_reason_detail}"
                skip_structuring = True
        else:
            # Both gate and external source policy passed - proceed to structuring
            would_have_traded = True  # FIX #2: Track that conditions allow trading
            skip_structuring = False
    
    logger.info("")

    
    # Step 3: Run Crash Venture v1
    if not skip_structuring:
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
                min_debit_per_contract=min_debit_per_contract
            )
        except ValueError as e:
            # Defensive compatibility layer: catch legacy NO_TRADE exceptions
            error_msg = str(e)
            if error_msg.startswith("NO TRADE:"):
                logger.warning("=" * 80)
                logger.warning(f"NO_TRADE (caught legacy exception): {error_msg}")
                logger.warning("=" * 80)
                
                # Parse reason from message
                if "No candidates survived filters" in error_msg:
                    no_trade_reason = "NO_CANDIDATES_SURVIVED_FILTERS"
                elif "No strikes below" in error_msg:
                    no_trade_reason = "INSUFFICIENT_STRIKE_COVERAGE"
                else:
                    no_trade_reason = "NO_CANDIDATES"
                
                # Create synthetic NO_TRADE result
                result = {
                    "ok": True,
                    "decision": "NO_TRADE",
                    "reason": no_trade_reason,
                    "warnings": [error_msg],
                    "candidates": [],
                    "filtered_out": 0,
                    "filtered_reasons": [],
                    "run_id": "NO_RUN_ID",
                    "run_dir": "runs/no_trade",
                    "top_structures": [],
                    "manifest": {},
                    "debug": {}
                }
                
                logger.info(f"Converted legacy NO_TRADE exception to structured result")
            else:
                # Not a NO_TRADE exception - re-raise
                raise
    else:
        logger.info("Step 3: SKIPPED (Edge Gate Blocked)")
    
    # Check result decision
    decision = result.get("decision", "TRADE")
    
    if decision == "NO_TRADE":
        # NO_TRADE result - display and continue to artifact generation
        logger.info("")
        logger.info("=" * 80)
        logger.info("⚠️  NO TRADE DECISION")
        logger.info("=" * 80)
        logger.info(f"Run ID: {result.get('run_id', 'N/A')}")
        logger.info(f"Reason: {result.get('reason', 'NO_CANDIDATES')}")
        logger.info(f"Warnings: {result.get('warnings', [])}")
        logger.info(f"Output Directory: {result.get('run_dir', 'N/A')}")
        logger.info("")
    else:
        # TRADE result - display structures
        logger.info("")
        logger.info("=" * 80)
        logger.info("✅ STRUCTURE GENERATION COMPLETE")
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
    
    # Step 4: Generate Order Tickets & Caps
    logger.info("=" * 80)
    logger.info("Step 4: Order Ticket Generation & Caps")
    logger.info("=" * 80)
    
    # Config already loaded earlier for resolution
    config_hash = compute_config_checksum(config)
    
    # Determine final decision and reason using correct hierarchy
    # FIX #1 & #2: Review-only mode ALWAYS gets REVIEW_ONLY decision
    # Precedence (highest to lowest):
    # 0) REVIEW_ONLY mode (if enabled, always REVIEW_ONLY regardless of other conditions)
    # 1) External source policy blocked
    # 2) Edge gate blocked
    # 3) Structuring ran but no candidates
    # 4) Trade ready
    
    if args.review_only_structuring:
        # FIX #1 & #2: In review-only mode, decision is ALWAYS "REVIEW_ONLY"
        decision = "REVIEW_ONLY"
        reason = "REVIEW_ONLY_MODE"
    elif external_source_blocked:
        decision = "NO_TRADE"
        reason = f"EXTERNAL_SOURCE_BLOCKED:{external_source_policy}"
    elif gate_decision.decision == "NO_TRADE":
        decision = "NO_TRADE"
        reason = f"EDGE_GATE_BLOCKED:{gate_decision.reason}"
    elif not result['top_structures']:
        decision = "NO_TRADE"
        reason = "STRUCTURING_NO_CANDIDATES"
    else:
        decision = "TRADE"
        reason = "TRADE_READY"
    
    # Check submission barriers
    submit_requested = args.submit
    submit_blocked = False
    submit_block_reason = None
    submit_executed = False
    
    if submit_requested:
        # Check confirmation
        if args.confirm != "SUBMIT":
            submit_blocked = True
            submit_block_reason = "SUBMIT_NOT_CONFIRMED"
            decision = "NO_TRADE"
            reason = "Submission requested but not confirmed (--confirm SUBMIT required)"
            logger.error(f"❌ {reason}")
        
        # Check mode
        elif args.mode == "smoke":
            submit_blocked = True
            submit_block_reason = "SUBMIT_BLOCKED_SMOKE_MODE"
            decision = "NO_TRADE"
            reason = "Submission not allowed in smoke mode"
            logger.error(f"❌ {reason}")
    
    # Apply caps to structures
    caps = {
        "max_orders": args.max_orders if args.max_orders is not None else config.get("structuring", {}).get("constraints", {}).get("top_n_output", 3),
        "max_debit_total": args.max_debit_total
    }
    
    # Generate tickets with caps
    tickets = []
    warnings = []
    
    if decision == "TRADE" and result['top_structures']:
        # Apply max_orders cap
        structures_to_ticket = result['top_structures'][:caps["max_orders"]]
        
        if len(result['top_structures']) > caps["max_orders"]:
            warnings.append(f"CAP_TRUNCATED_ORDERS: {len(result['top_structures'])} -> {caps['max_orders']}")
            logger.warning(f"⚠️  Truncated {len(result['top_structures'])} structures to {caps['max_orders']} (max_orders cap)")
        
        # Generate tickets
        for struct in structures_to_ticket:
            try:
                ticket = from_candidate(struct, quantity=1, account=None)
                tickets.append(to_dict(ticket))
            except Exception as e:
                logger.error(f"Failed to create ticket from structure rank {struct['rank']}: {e}")
                warnings.append(f"TICKET_CREATION_FAILED_RANK_{struct['rank']}")
        
        # Apply max_debit_total cap
        if caps["max_debit_total"] is not None:
            total_debit = sum(t["limit_price"] * t["quantity"] * 100 for t in tickets)
            
            if total_debit > caps["max_debit_total"]:
                logger.warning(f"⚠️  Total debit ${total_debit:,.2f} exceeds cap ${caps['max_debit_total']:,.2f}")
                
                # Truncate tickets to fit cap (keep highest EV/$ first)
                tickets_sorted = sorted(
                    tickets,
                    key=lambda t: t.get("metadata", {}).get("ev_per_dollar", 0),
                    reverse=True
                )
                
                capped_tickets = []
                running_debit = 0
                
                for ticket in tickets_sorted:
                    ticket_debit = ticket["limit_price"] * ticket["quantity"] * 100
                    if running_debit + ticket_debit <= caps["max_debit_total"]:
                        capped_tickets.append(ticket)
                        running_debit += ticket_debit
                    else:
                        # Try to reduce quantity to fit
                        max_allowed_qty = int((caps["max_debit_total"] - running_debit) / (ticket["limit_price"] * 100))
                        if max_allowed_qty > 0:
                            ticket_copy = ticket.copy()
                            ticket_copy["quantity"] = max_allowed_qty
                            capped_tickets.append(ticket_copy)
                            warnings.append(f"CAP_REDUCED_QUANTITY_TICKET")
                            break
                
                tickets = capped_tickets
                warnings.append(f"CAP_TRUNCATED_ORDERS_DEBIT: ${total_debit:,.2f} -> ${running_debit:,.2f}")
                logger.warning(f"⚠️  Applied debit cap: kept {len(tickets)} ticket(s), total debit: ${running_debit:,.2f}")
    
    logger.info(f"Generated {len(tickets)} order ticket(s)")
    logger.info("")
    
    # Step 5: Generate Review & Write Artifacts
    logger.info("=" * 80)
    logger.info("Step 5: Review Output & Artifacts")
    logger.info("=" * 80)
    
    # ALWAYS generate review artifacts in review-only mode (even with 0 candidates)
    if args.review_only_structuring:
        logger.info("⚠️  REVIEW-ONLY MODE: Generating review artifacts")
        logger.info(f"   Candidates found: {len(result.get('top_structures', []))}")
        logger.info("")
        
        # Ensure run_dir and artifacts_dir are defined
        run_dir = Path(result['run_dir'])
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        
        # Import review pack generator
        from forecast_arb.review.review_pack import render_review_pack, render_decision_template
        
        # STEP B: Get campaign risk summary
        logger.info("Fetching campaign risk summary...")
        campaign_cap = config.get("risk_management", {}).get("campaign_max_loss_cap")
        campaign_risk = get_campaign_risk_summary(
            runs_root=Path("runs"),
            campaign_cap=campaign_cap
        )
        logger.info(f"✓ Campaign risk: {campaign_risk['open_positions']} open positions, "
                   f"${campaign_risk['open_max_loss']:.2f} deployed, "
                   f"${campaign_risk.get('remaining_capacity', 0) or 0:.2f} remaining")
        
        # STEP A: Fetch live quotes if requested
        live_quotes = None
        if args.include_live_quotes and result.get('top_structures'):
            logger.info("Fetching live quotes for top candidates...")
            try:
                live_quotes = fetch_quotes_for_candidates(
                    candidates=result['top_structures'],
                    underlier=metadata['underlier'],
                    ibkr_host=args.ibkr_host,
                    ibkr_port=args.ibkr_port,
                    top_n=5
                )
                
                # Log diagnostics
                for ticker, quote_data in live_quotes.items():
                    if 'diagnostics' in quote_data and quote_data['diagnostics'].get('warnings'):
                        for warning in quote_data['diagnostics']['warnings']:
                            logger.warning(f"  Quote warning ({ticker}): {warning}")
                
                logger.info(f"✓ Fetched live quotes for {len(live_quotes)} candidates")
            except Exception as e:
                logger.error(f"Failed to fetch live quotes: {e}", exc_info=True)
                live_quotes = None
        elif args.include_live_quotes:
            logger.info("⚠️  --include-live-quotes enabled but no candidates to quote")
        
        # Prepare review candidates (simplified - structures as-is)
        review_candidates = []
        for struct in result['top_structures']:
            # Determine pricing quality based on leg price sources
            legs = struct.get('legs', [])
            pricing_quality = "EXECUTABLE"
            if any(leg.get('price_source', '').startswith('model') for leg in legs):
                pricing_quality = "MODEL"
            elif any(leg.get('price_source', '').endswith('fallback') for leg in legs):
                pricing_quality = "MID"
            
            # FIX #4: Stable blocked_by schema
            blocked_by_schema = {
                "would_block_trade": would_block_trade,
                "edge_gate": {
                    "decision": gate_decision.decision if gate_decision else "UNKNOWN",
                    "reason": gate_decision.reason if gate_decision else "UNKNOWN",
                    "edge": gate_decision.edge if gate_decision else None,
                    "confidence": gate_decision.confidence if gate_decision else None,
                    "thresholds": {
                        "min_edge": min_edge,
                        "min_confidence": min_confidence
                    }
                } if gate_decision else None,
                "external_policy": {
                    "allowed": not external_source_blocked,
                    "source": p_event_source_actual,
                    "reason": external_source_policy if external_source_policy else "OK"
                }
            }
            
            # FIX #4: Add comprehensive fields for manual decision-making
            strikes_dict = struct.get('strikes', {})
            long_put_strike = strikes_dict.get('long_put', 0)
            short_put_strike = strikes_dict.get('short_put', 0)
            width = struct.get('spread_width', 0)
            debit_per_contract = struct.get('debit_per_contract', 0)
            max_loss_per_contract = struct.get('max_loss_per_contract', 0)
            max_gain_per_contract = struct.get('max_gain_per_contract', 0)
            
            # Compute debit_natural from leg prices if available
            debit_mid = debit_per_contract  # Already uses mid/executable prices
            debit_natural = None
            if len(legs) >= 2:
                long_bid = legs[0].get('bid')
                long_ask = legs[0].get('ask')
                short_bid = legs[1].get('bid')
                short_ask = legs[1].get('ask')
                
                # Natural debit = what we pay (long ask) - what we collect (short bid)
                if long_ask is not None and short_bid is not None:
                    debit_natural = (long_ask - short_bid) * 100  # per contract
            
            # Compute breakeven: long_strike - debit_per_share
            debit_per_share = debit_per_contract / 100 if debit_per_contract > 0 else 0
            breakeven = long_put_strike - debit_per_share if long_put_strike > 0 else 0
            
            # Suggested limit price (use mid initially, but expose natural for comparison)
            suggested_limit_price = debit_per_contract
            
            # Per-leg pricing quality
            long_pricing_quality = "UNKNOWN"
            short_pricing_quality = "UNKNOWN"
            
            if len(legs) >= 1:
                long_source = legs[0].get('price_source', '')
                if 'ask' in long_source.lower() or 'bid' in long_source.lower():
                    long_pricing_quality = "EXECUTABLE"
                elif 'fallback' in long_source.lower():
                    long_pricing_quality = "MID_FALLBACK"
                elif 'model' in long_source.lower():
                    long_pricing_quality = "MODEL"
                    
            if len(legs) >= 2:
                short_source = legs[1].get('price_source', '')
                if 'bid' in short_source.lower() or 'ask' in short_source.lower():
                    short_pricing_quality = "EXECUTABLE"
                elif 'fallback' in short_source.lower():
                    short_pricing_quality = "MID_FALLBACK"
                elif 'model' in short_source.lower():
                    short_pricing_quality = "MODEL"
            
            # Overall pricing quality (most conservative)
            overall_pricing_quality = pricing_quality
            
            candidate = {
                "review_only": True,
                "rank": struct.get('rank', 0),
                "expiry": struct.get('expiry'),
                "strikes": struct.get('strikes', {}),
                "blocked_by": blocked_by_schema,
                "estimated_entry": {
                    "debit": debit_per_contract,
                    "debit_mid": debit_mid,
                    "debit_natural": debit_natural,
                    "pricing_quality": pricing_quality,
                    "pricing_quality_per_leg": {
                        "long_put": long_pricing_quality,
                        "short_put": short_pricing_quality,
                        "overall": overall_pricing_quality
                    },
                    "pricing_sources": {
                        "long_put": legs[0].get('price_source', 'unknown') if len(legs) > 0 else 'unknown',
                        "short_put": legs[1].get('price_source', 'unknown') if len(legs) > 1 else 'unknown'
                    },
                    "suggested_limit_price": suggested_limit_price
                },
                "structure": {
                    "expiry": struct.get('expiry'),
                    "long_put": long_put_strike,
                    "short_put": short_put_strike,
                    "width": width,
                    "max_loss": max_loss_per_contract,
                    "max_gain": max_gain_per_contract,
                    "max_value": max_gain_per_contract + max_loss_per_contract  # Total spread value at expiry
                },
                "metrics": {
                    "ev": struct.get('ev_per_contract', 0),
                    "ev_per_dollar": struct.get('ev_per_dollar', 0),
                    "breakeven": breakeven,
                    "pop_estimate": struct.get('prob_profit', 0)
                },
                "notes": []
            }
            
            # Add warnings/notes
            if pricing_quality != "EXECUTABLE":
                candidate["notes"].append(f"Pricing quality: {pricing_quality}")
            
            review_candidates.append(candidate)
        
        # Write review_candidates.json
        review_candidates_path = artifacts_dir / "review_candidates.json"
        with open(review_candidates_path, "w") as f:
            json.dump(review_candidates, f, indent=2)
        logger.info(f"✓ Review candidates written: {review_candidates_path}")
        
        # Generate and write review_pack.md
        run_context = {
            "run_id": result['run_id'],
            "run_dir": str(run_dir),
            "snapshot_metadata": {
                "underlier": metadata['underlier'],
                "spot": metadata['current_price'],
                "snapshot_time": metadata['snapshot_time']
            },
            "expiry_used": target_expiry,
            "dte": int(compute_time_to_expiry(metadata['snapshot_time'], target_expiry) * 365) if target_expiry else 0,
            "event_spec": event_spec.to_dict() if target_expiry else {},  # NEW: EventSpec single source of truth
            "event_definition": event_def.to_dict() if target_expiry else {},  # LEGACY: for backward compat
            "min_edge": min_edge,
            "min_confidence": min_confidence
        }
        
        external_policy_dict = {
            "source": p_event_source_actual,
            "policy": external_source_policy if external_source_policy else "N/A",
            "blocked": external_source_blocked
        }
        
        # Enhance gate_decision with proxy metadata for review pack
        gate_decision_dict = gate_decision.to_dict() if gate_decision else {}
        if p_external_result and p_external_result.metadata:
            # Include proxy metadata in gate_decision for review pack
            gate_decision_dict["p_external_metadata"] = p_external_result.metadata
        
        review_pack_md = render_review_pack(
            run_context=run_context,
            gate_decision=gate_decision_dict,
            external_policy=external_policy_dict,
            candidates=review_candidates,
            p_implied_artifact=p_implied_artifact if target_expiry else None,
            campaign_risk=campaign_risk,
            live_quotes=live_quotes
        )
        
        review_pack_path = artifacts_dir / "review_pack.md"
        with open(review_pack_path, "w", encoding="utf-8") as f:
            f.write(review_pack_md)
        logger.info(f"✓ Review pack written: {review_pack_path}")
        
        # Generate and write decision_template.md
        decision_template_md = render_decision_template()
        decision_template_path = artifacts_dir / "decision_template.md"
        with open(decision_template_path, "w", encoding="utf-8") as f:
            f.write(decision_template_md)
        logger.info(f"✓ Decision template written: {decision_template_path}")
        
        logger.info("")
        logger.info("📋 Review-only artifacts generated:")
        logger.info(f"   • {review_candidates_path}")
        logger.info(f"   • {review_pack_path}")
        logger.info(f"   • {decision_template_path}")
        logger.info("")
    
    # Format review with gate decision data
    review_text = format_review(
        run_id=result['run_id'],
        decision=decision,
        reason=reason,
        p_external=gate_decision.p_external if gate_decision else p_event,
        p_implied=gate_decision.p_implied if gate_decision else None,
        edge=gate_decision.edge if gate_decision else None,
        confidence=gate_decision.confidence if gate_decision else None,
        tickets=tickets,
        caps=caps,
        mode=args.mode,
        config_hash=config_hash,
        submit_requested=submit_requested,
        submit_blocked=submit_blocked,
        submit_block_reason=submit_block_reason,
        p_external_source=p_event_source_actual,
        external_source_blocked=external_source_blocked,
        gate_decision=gate_decision.to_dict() if gate_decision else None,
        external_source_policy_skipped=external_source_policy_skipped
    )
    
    # Display review
    print("\n" + review_text + "\n")
    
    # Write artifacts
    run_dir = Path(result['run_dir'])
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    
    # FIX #3: Do NOT write tickets.json in review-only mode
    if not args.review_only_structuring:
        tickets_path = artifacts_dir / "tickets.json"
        with open(tickets_path, "w") as f:
            json.dump(tickets, f, indent=2)
        logger.info(f"✓ Tickets written: {tickets_path}")
    else:
        logger.info(f"⚠️  Skipping tickets.json (review-only mode)")
    
    # Write review.txt
    review_path = artifacts_dir / "review.txt"
    with open(review_path, "w") as f:
        f.write(review_text)
    logger.info(f"✓ Review written: {review_path}")
    
    # Write final_decision.json with enhanced NO_TRADE details
    final_decision = {
        "run_id": result['run_id'],
        "decision": decision,
        "reason": reason,
        "submit_requested": submit_requested,
        "submit_executed": submit_executed,
        "submit_block_reason": submit_block_reason,
        "caps_applied": caps,
        "warnings": warnings,
        "mode": args.mode,
        "config_hash": config_hash,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "min_debit_per_contract_usd": min_debit_per_contract,
            "min_debit_source": min_debit_source,
            "p_event": p_event,
            "p_event_source": p_event_source_actual,
            "kalshi_auto_mapping": kalshi_auto_mapping if kalshi_auto_mapping else None
        },
        "metadata": {
            "review_only_structuring": args.review_only_structuring,
            "structuring_ran": not skip_structuring if 'skip_structuring' in locals() else True,
            "would_block_trade": would_block_trade,
            "would_have_traded": would_have_traded,  # FIX #2: Add would_have_traded metadata
            "review_candidates_written": args.review_only_structuring and result['top_structures']
        }
    }
    
    # Add blocking reason if applicable
    if would_block_trade:
        final_decision["metadata"]["structuring_block_reason"] = f"{block_type}: {block_reason_detail}"
    
    # Add details for NO_TRADE decisions
    if decision == "NO_TRADE":
        filtered_count = result.get('filtered_out', 0)
        if filtered_count > 0:
            final_decision["details"] = (
                f"Filtered out {filtered_count} candidates. "
                f"min_debit_per_contract_usd={min_debit_per_contract:.2f}"
            )
    
    decision_path = artifacts_dir / "final_decision.json"
    with open(decision_path, "w") as f:
        json.dump(final_decision, f, indent=2)
    logger.info(f"✓ Decision written: {decision_path}")
    
    # Step 6: OrderIntent Emission (new flow) or Legacy Submission
    execution_result = None
    intent_written = False
    
    if args.emit_intent:
        logger.info("")
        logger.info("=" * 80)
        logger.info("Step 6: OrderIntent Emission")
        logger.info("=" * 80)
        
        # Select candidate
        selected_candidate = None
        
        if args.pick_rank is not None:
            # Select by rank
            logger.info(f"Selecting candidate by rank: {args.pick_rank}")
            for struct in result.get('top_structures', []):
                if struct.get('rank') == args.pick_rank:
                    selected_candidate = struct
                    break
            
            if selected_candidate is None:
                logger.error(f"❌ No candidate found with rank {args.pick_rank}")
                logger.error(f"   Available ranks: {[s.get('rank') for s in result.get('top_structures', [])]}")
                sys.exit(1)
                
        elif args.pick_expiry and args.pick_long and args.pick_short:
            # Select by exact match
            logger.info(f"Selecting candidate by expiry={args.pick_expiry}, long={args.pick_long}, short={args.pick_short}")
            for struct in result.get('top_structures', []):
                strikes = struct.get('strikes', {})
                if (struct.get('expiry') == args.pick_expiry and
                    strikes.get('long_put') == args.pick_long and
                    strikes.get('short_put') == args.pick_short):
                    selected_candidate = struct
                    break
            
            if selected_candidate is None:
                logger.error(f"❌ No candidate found matching criteria")
                sys.exit(1)
        else:
            logger.error("❌ --emit-intent requires candidate selection:")
            logger.error("   Use --pick-rank INT")
            logger.error("   OR  --pick-expiry YYYYMMDD --pick-long STRIKE --pick-short STRIKE")
            sys.exit(1)
        
        # Build OrderIntent
        logger.info(f"✓ Selected candidate: rank {selected_candidate.get('rank')}")
        logger.info(f"  Expiry: {selected_candidate.get('expiry')}")
        logger.info(f"  Strikes: {selected_candidate.get('strikes')}")
        
        # Calculate limit prices
        model_debit = selected_candidate.get('debit_per_contract', 0) / 100.0  # Convert to USD
        
        if args.limit_start is not None:
            limit_start = args.limit_start
        else:
            limit_start = round(model_debit * 0.92, 2)
        
        if args.limit_max is not None:
            limit_max = args.limit_max
        else:
            limit_max = round(model_debit * 0.95, 2)
        
        # Build intent structure
        strikes = selected_candidate.get('strikes', {})
        order_intent = {
            "strategy": "crash_venture_v1",
            "run_id": result.get('run_id'),
            "symbol": metadata['underlier'],
            "expiry": selected_candidate.get('expiry'),
            "type": "vertical_put_debit",
            "legs": [
                {
                    "action": "BUY",
                    "right": "P",
                    "strike": strikes.get('long_put')
                },
                {
                    "action": "SELL",
                    "right": "P",
                    "strike": strikes.get('short_put')
                }
            ],
            "qty": args.qty,
            "limit": {
                "start": limit_start,
                "max": limit_max
            },
            "tif": "DAY",
            "transmit": False,
            "guards": {
                "min_dte": args.dte_min,
                "max_debit": limit_max,
                "max_spread_width": args.guard_max_spread_width,
                "require_executable_legs": args.guard_require_executable_legs
            },
            "created_utc": datetime.now(timezone.utc).isoformat()
        }
        
        # Determine output path
        if args.intent_out:
            intent_path = Path(args.intent_out)
        else:
            intent_path = Path(result['run_dir']) / "artifacts" / "order_intent.json"
        
        # Ensure directory exists
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write intent
        with open(intent_path, "w") as f:
            json.dump(order_intent, f, indent=2)
        
        logger.info("")
        logger.info(f"✅ WROTE_ORDER_INTENT: {intent_path}")
        logger.info("")
        logger.info("Intent Details:")
        logger.info(f"  Symbol: {order_intent['symbol']}")
        logger.info(f"  Expiry: {order_intent['expiry']}")
        logger.info(f"  Type: {order_intent['type']}")
        logger.info(f"  Qty: {order_intent['qty']}")
        logger.info(f"  Limit Range: ${order_intent['limit']['start']:.2f} - ${order_intent['limit']['max']:.2f}")
        logger.info(f"  Legs:")
        for leg in order_intent['legs']:
            logger.info(f"    {leg['action']} {leg['right']} ${leg['strike']:.2f}")
        logger.info("")
        logger.info("Next Steps:")
        logger.info(f"  1. Review intent: {intent_path}")
        logger.info(f"  2. Execute: python -m forecast_arb.execution.execute_trade --intent {intent_path} --live --transmit --confirm SEND")
        logger.info("")
        
        intent_written = True
    
    elif submit_requested and not submit_blocked and tickets:
        logger.info("")
        logger.info("=" * 80)
        logger.info("Step 6: Order Submission")
        logger.info("=" * 80)
        
        try:
            # In dry-run by default, real submission would require IBKR client
            execution_result = submit_tickets(
                tickets=tickets,
                ibkr_client=None,  # Would need to connect to IBKR for live submission
                dry_run=True  # Always dry-run for now (would be False in prod with client)
            )
            
            submit_executed = execution_result.success and not execution_result.dry_run
            
            # Write execution result
            exec_result_path = artifacts_dir / "execution_result.json"
            with open(exec_result_path, "w") as f:
                json.dump(execution_result.to_dict(), f, indent=2)
            logger.info(f"✓ Execution result written: {exec_result_path}")
            
        except Exception as e:
            logger.error(f"❌ Submission failed: {e}", exc_info=True)
            
            # Update decision
            final_decision["decision"] = "TRADE"  # Keep as TRADE but failed
            final_decision["reason"] = "SUBMIT_FAILED"
            final_decision["submit_executed"] = False
            
            # Write exception details
            exception_details = {
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "timestamp_utc": datetime.now(timezone.utc).isoformat()
            }
            
            exceptions_path = artifacts_dir / "exceptions.json"
            with open(exceptions_path, "w") as f:
                json.dump(exception_details, f, indent=2)
            logger.info(f"✓ Exception details written: {exceptions_path}")
            
            # Re-write final_decision with updated info
            with open(decision_path, "w") as f:
                json.dump(final_decision, f, indent=2)
    
    elif not submit_requested:
        logger.info("")
        logger.info("=" * 80)
        logger.info("SUBMISSION DISABLED (use --submit --confirm SUBMIT to enable)")
        logger.info("=" * 80)
    
    # Final summary
    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ RUN COMPLETE")
    logger.info("=" * 80)
    logger.info(f"Decision: {decision}")
    logger.info(f"Tickets Generated: {len(tickets)}")
    logger.info(f"Intent Emitted: {intent_written}")
    logger.info(f"Submit Requested: {submit_requested}")
    logger.info(f"Submit Executed: {submit_executed}")
    logger.info(f"Artifacts: {artifacts_dir}")
    logger.info("")
    
    # FIX #5: One Command Output Pointer
    if args.review_only_structuring and result['top_structures']:
        logger.info("=" * 80)
        logger.info("📋 REVIEW PACK OUTPUT")
        logger.info("=" * 80)
        
        # Compute snapshot age/freshness
        snapshot_age_seconds = None
        snapshot_freshness = "UNKNOWN"
        try:
            snapshot_dt = datetime.fromisoformat(metadata['snapshot_time'].replace('Z', '+00:00'))
            now_dt = datetime.now(timezone.utc)
            snapshot_age_seconds = (now_dt - snapshot_dt).total_seconds()
            
            if snapshot_age_seconds < 300:  # < 5 minutes
                snapshot_freshness = "FRESH"
            elif snapshot_age_seconds < 3600:  # < 1 hour
                snapshot_freshness = "RECENT"
            else:
                snapshot_freshness = "STALE"
        except Exception:
            pass
        
        logger.info(f"Review Pack: {artifacts_dir / 'review_pack.md'}")
        logger.info(f"Review Candidates JSON: {artifacts_dir / 'review_candidates.json'}")
        logger.info(f"Snapshot: {snapshot_path}")
        logger.info(f"Snapshot Age: {int(snapshot_age_seconds/60) if snapshot_age_seconds else 'N/A'} minutes ({snapshot_freshness})")
        logger.info("")
    
    # Step 7: Update run index and latest pointer
    try:
        runs_root = Path("runs")
        
        # Update latest pointer
        set_latest_run(
            runs_root=runs_root,
            run_dir=run_dir,
            decision=decision,
            reason=reason,
            run_id=result['run_id']
        )
        logger.info(f"✓ Latest run pointer updated: {runs_root / 'LATEST.json'}")
        
        # Extract summary and update index
        summary = extract_summary_safe(run_dir)
        index = load_index(runs_root)
        index = append_run(index, summary, max_entries=500)
        write_index(runs_root, index)
        logger.info(f"✓ Run index updated: {runs_root / 'index.json'}")
        
    except Exception as e:
        logger.warning(f"Failed to update run tracking (non-fatal): {e}")


if __name__ == "__main__":
    main()
