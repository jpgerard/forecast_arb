"""
Kalshi Series Probe - Diagnostic utility for series availability.

Usage:
    python scripts/kalshi_probe.py --series KXNDX --status open --limit 10
    python scripts/kalshi_probe.py --series KXINX --status closed --limit 10
    python scripts/kalshi_probe.py --series KXINXMINY --status open --limit 10
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.kalshi.threshold_parser import (
    parse_threshold_from_market,
    format_threshold_display
)
from forecast_arb.kalshi.status_map import map_status


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def probe_series(
    series: str,
    status: str = "open",
    limit: int = 10
) -> None:
    """
    Probe a Kalshi series and display market sample.
    
    Args:
        series: Series ticker (e.g., "KXNDX", "KXINX")
        status: Market status filter ("open", "closed", or None for all)
        limit: Number of markets to display
    """
    logger.info("=" * 80)
    logger.info(f"KALSHI SERIES PROBE: {series}")
    logger.info("=" * 80)
    logger.info(f"Status filter: {status or 'ALL'}")
    logger.info(f"Limit: {limit}")
    logger.info("")
    
    # Initialize client
    try:
        client = KalshiClient()
        logger.info("✓ Client initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Kalshi client: {e}")
        logger.error("Make sure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY are set in .env")
        sys.exit(1)
    
    # Fetch markets
    try:
        logger.info(f"Fetching markets for series '{series}'...")
        # Map user-facing status to API status(es)
        api_status = map_status(status)
        logger.info(f"Effective API statuses: {api_status or 'ALL (no filter)'}")
        
        markets = client.list_markets(
            series=[series],
            status=api_status,
            limit=200  # Fetch max to get accurate count
        )
        
        total_count = len(markets)
        logger.info(f"✓ Found {total_count} markets")
        
        # Count by status
        status_histogram = {}
        for market in markets:
            s = market.get("status", "UNKNOWN")
            status_histogram[s] = status_histogram.get(s, 0) + 1
        
        logger.info(f"Status histogram: {status_histogram}")
        logger.info("")
        
        if total_count == 0:
            logger.warning("⚠️  SERIES RETURNED ZERO MARKETS")
            logger.info("")
            logger.info("Possible reasons:")
            logger.info("  1. Series does not exist or is not accessible")
            logger.info("  2. No markets match the status filter")
            logger.info("  3. Incorrect API credentials or permissions")
            logger.info("")
            logger.info("Try running without status filter:")
            logger.info(f"  python scripts/kalshi_probe.py --series {series} --status all")
            sys.exit(0)
        
        # Display sample
        logger.info(f"MARKET SAMPLE (first {min(limit, total_count)} of {total_count}):")
        logger.info("-" * 80)
        
        for i, market in enumerate(markets[:limit]):
            ticker = market.get("ticker", "UNKNOWN")
            title = market.get("title", "")[:60]  # Truncate
            status_val = market.get("status", "UNKNOWN")
            close_time = market.get("close_time", "")
            expiry_time = market.get("expiry_time", "")
            
            # Parse threshold with series-aware parser
            parsed = parse_threshold_from_market(market, series=series)
            level_str = format_threshold_display(parsed)
            
            # Use most relevant time field
            time_field = close_time or expiry_time or "N/A"
            if time_field != "N/A":
                # Parse and format
                try:
                    dt = datetime.fromisoformat(time_field.replace('Z', '+00:00'))
                    time_field = dt.strftime("%Y-%m-%d")
                except:
                    pass
            
            logger.info(f"{i+1:2d}. {ticker}")
            logger.info(f"    Title:  {title}")
            logger.info(f"    Status: {status_val}")
            logger.info(f"    Date:   {time_field}")
            logger.info(f"    Level:  {level_str}")
            logger.info("")
        
        # Summary statistics
        logger.info("=" * 80)
        logger.info("SUMMARY")
        logger.info("=" * 80)
        
        # Count by status
        status_counts = {}
        for market in markets:
            s = market.get("status", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1
        
        logger.info(f"Total markets: {total_count}")
        logger.info(f"Status breakdown: {status_counts}")
        
        # Parse thresholds from all markets (track point vs range separately)
        point_thresholds = []
        ranges = []
        
        for market in markets:
            parsed = parse_threshold_from_market(market, series=series)
            
            if parsed["kind"] == "point" and parsed["threshold"] is not None:
                point_thresholds.append(parsed["threshold"])
            elif parsed["kind"] == "range":
                if parsed["low"] is not None and parsed["high"] is not None:
                    ranges.append((parsed["low"], parsed["high"]))
        
        # Display thresholds summary
        if point_thresholds:
            logger.info(f"Point thresholds: {min(point_thresholds):.4g} – {max(point_thresholds):.4g} ({len(point_thresholds)} markets)")
        
        if ranges:
            logger.info(f"Range markets: {len(ranges)}")
            # Show sample of ranges
            for i, (low, high) in enumerate(ranges[:3]):
                logger.info(f"  Example {i+1}: [{low:.4g}, {high:.4g}]")
        
        logger.info("")
        
    except Exception as e:
        logger.error(f"❌ Failed to fetch markets: {e}")
        sys.exit(1)


def probe_all_series(status: str = "open", limit: int = 5) -> None:
    """
    Probe multiple known series.
    
    Args:
        status: Market status filter
        limit: Number of markets to display per series
    """
    # Known series for different indices
    series_list = [
        ("KXINX", "S&P 500 Daily Close"),
        ("KXINXY", "S&P 500 Yearly Close"),
        ("KXINXMINY", "S&P 500 Yearly Min"),
        ("KXINXMAXY", "S&P 500 Yearly Max"),
        ("KXNDX", "NASDAQ-100 (potential)"),
    ]
    
    logger.info("=" * 80)
    logger.info("PROBING ALL KNOWN SERIES")
    logger.info("=" * 80)
    logger.info("")
    
    client = KalshiClient()
    
    results = []
    
    for series, description in series_list:
        logger.info(f"Probing {series} ({description})...")
        
        try:
            # Map user-facing status to API status
            api_status = map_status(status)
            markets = client.list_markets(
                series=[series],
                status=api_status,
                limit=50
            )
            
            count = len(markets)
            results.append((series, description, count, "OK"))
            
            if count > 0:
                logger.info(f"  ✓ {count} markets found")
            else:
                logger.info(f"  ⚠️  0 markets (series may not exist)")
            
        except Exception as e:
            results.append((series, description, 0, f"ERROR: {e}"))
            logger.error(f"  ❌ Error: {e}")
        
        logger.info("")
    
    # Summary table
    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"{'Series':<15} {'Description':<30} {'Markets':<10} {'Status'}")
    logger.info("-" * 80)
    
    for series, description, count, status_msg in results:
        logger.info(f"{series:<15} {description:<30} {count:<10} {status_msg}")
    
    logger.info("")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Probe Kalshi series for market availability",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        "--series",
        type=str,
        help="Series ticker to probe (e.g., KXNDX, KXINX). If omitted, probes all known series."
    )
    
    parser.add_argument(
        "--status",
        type=str,
        default="open",
        choices=["open", "closed", "all"],
        help="Market status filter (default: open)"
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of markets to display (default: 10)"
    )
    
    args = parser.parse_args()
    
    # Convert 'all' status to None
    status_filter = args.status if args.status != "all" else None
    
    if args.series:
        # Probe single series
        probe_series(
            series=args.series,
            status=status_filter,
            limit=args.limit
        )
    else:
        # Probe all known series
        probe_all_series(
            status=status_filter,
            limit=args.limit
        )
