"""
Kalshi Series Coverage Report

Deterministic coverage analysis for Kalshi series to enable fast precheck
in the mapper. Fetches markets from specified series and computes coverage
metrics (date ranges, threshold ranges, market kinds).

Usage:
    python scripts/kalshi_series_coverage.py --series KXINX,KXINXY,KXINXMINY --status open --limit 500

Outputs:
    - Console table with coverage metrics
    - JSON artifact: runs/kalshi/coverage_{timestamp}.json
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Any, Optional
from collections import defaultdict

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.kalshi.client import KalshiClient
from forecast_arb.kalshi.threshold_parser import parse_threshold_from_market
from forecast_arb.kalshi.status_map import map_status


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_event_date(market: Dict[str, Any]) -> Optional[date]:
    """
    Parse event date from market (close_time or event_date).
    
    Args:
        market: Market dict from Kalshi API
    
    Returns:
        date object or None if unparseable
    """
    # Try close_time first
    close_time_str = market.get("close_time")
    if close_time_str:
        try:
            dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            return dt.date()
        except (ValueError, AttributeError):
            pass
    
    # Try event_date (if present)
    event_date_str = market.get("event_date")
    if event_date_str:
        try:
            # Could be date or datetime
            if "T" in event_date_str:
                dt = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                return dt.date()
            else:
                return datetime.fromisoformat(event_date_str).date()
        except (ValueError, AttributeError):
            pass
    
    return None


def compute_coverage(
    series_list: List[str],
    status: str = "open",
    limit: int = 500,
    expiry_start: Optional[date] = None,
    expiry_end: Optional[date] = None
) -> Dict[str, Any]:
    """
    Compute coverage metrics for specified Kalshi series.
    
    Args:
        series_list: List of series tickers (e.g., ["KXINX", "KXINXY"])
        status: Market status filter ("open", "closed", "settled")
        limit: Max markets to fetch per series
    
    Returns:
        Dict with coverage metrics per series:
        {
            "series_name": {
                "status_filter": str,
                "markets_fetched": int,
                "unique_event_dates": int,
                "min_date": str (YYYY-MM-DD) or None,
                "max_date": str (YYYY-MM-DD) or None,
                "kind_counts": {"point": int, "range": int, "unknown": int},
                "threshold_min": float or None,
                "threshold_max": float or None,
                "range_low_min": float or None,
                "range_high_max": float or None,
                "date_examples": [str, ...] (up to 3),
                "range_examples": [str, ...] (up to 3),
                "point_examples": [str, ...] (up to 3)
            }
        }
    """
    client = KalshiClient()
    results = {}
    
    for series in series_list:
        logger.info(f"Fetching markets for series: {series} (status={status}, limit={limit})")
        
        try:
            # Map user-facing status to API status(es)
            api_status = map_status(status)
            logger.info(f"  Effective API statuses: {api_status or 'ALL (no filter)'}")
            
            # Fetch markets
            markets = client.list_markets(
                series=[series],
                status=api_status,
                limit=limit
            )
            
            logger.info(f"  Fetched {len(markets)} markets for {series}")
            
            # Status histogram
            status_histogram = {}
            for market in markets:
                s = market.get("status", "UNKNOWN")
                status_histogram[s] = status_histogram.get(s, 0) + 1
            logger.info(f"  Status histogram: {status_histogram}")
            
            # Parse each market
            event_dates = set()
            kind_counts = {"point": 0, "range": 0, "unknown": 0}
            point_thresholds = []
            range_lows = []
            range_highs = []
            date_examples = []
            range_examples = []
            point_examples = []
            
            for market in markets:
                ticker = market.get("ticker", "")
                
                # Parse event date
                event_date = parse_event_date(market)
                if event_date:
                    event_dates.add(event_date)
                    if len(date_examples) < 3 and str(event_date) not in date_examples:
                        date_examples.append(str(event_date))
                
                # Parse threshold
                parsed = parse_threshold_from_market(market, series=series)
                kind = parsed.get("kind", "unknown")
                kind_counts[kind] += 1
                
                if kind == "point":
                    threshold = parsed.get("threshold")
                    if threshold is not None:
                        point_thresholds.append(threshold)
                        if len(point_examples) < 3:
                            point_examples.append(f"{ticker}: {threshold:.4g}")
                
                elif kind == "range":
                    low = parsed.get("low")
                    high = parsed.get("high")
                    if low is not None:
                        range_lows.append(low)
                    if high is not None:
                        range_highs.append(high)
                    if len(range_examples) < 3:
                        range_examples.append(f"{ticker}: [{low:.4g}, {high:.4g}]")
            
            # Compute aggregates
            sorted_dates = sorted(event_dates) if event_dates else []
            min_date = str(sorted_dates[0]) if sorted_dates else None
            max_date = str(sorted_dates[-1]) if sorted_dates else None
            
            threshold_min = min(point_thresholds) if point_thresholds else None
            threshold_max = max(point_thresholds) if point_thresholds else None
            
            range_low_min = min(range_lows) if range_lows else None
            range_high_max = max(range_highs) if range_highs else None
            
            # Check expiry window intersection if provided
            expiry_intersects = None
            if expiry_start and expiry_end and sorted_dates:
                # Check if date range overlaps with expiry window
                series_min = sorted_dates[0]
                series_max = sorted_dates[-1]
                expiry_intersects = not (series_max < expiry_start or series_min > expiry_end)
            
            results[series] = {
                "status_filter": status,
                "markets_fetched": len(markets),
                "unique_event_dates": len(event_dates),
                "min_date": min_date,
                "max_date": max_date,
                "expiry_window_start": str(expiry_start) if expiry_start else None,
                "expiry_window_end": str(expiry_end) if expiry_end else None,
                "expiry_intersects": expiry_intersects,
                "kind_counts": kind_counts,
                "threshold_min": threshold_min,
                "threshold_max": threshold_max,
                "range_low_min": range_low_min,
                "range_high_max": range_high_max,
                "date_examples": date_examples,
                "range_examples": range_examples,
                "point_examples": point_examples
            }
            
        except Exception as e:
            logger.error(f"Failed to fetch markets for {series}: {e}")
            results[series] = {
                "status_filter": status,
                "error": str(e),
                "markets_fetched": 0
            }
    
    return results


def print_coverage_table(coverage: Dict[str, Any]) -> None:
    """
    Print coverage metrics as a formatted table.
    
    Args:
        coverage: Coverage dict from compute_coverage()
    """
    print("\n" + "="*100)
    print("KALSHI SERIES COVERAGE REPORT")
    print("="*100)
    print()
    
    for series, metrics in coverage.items():
        print(f"Series: {series}")
        print("-" * 100)
        
        if "error" in metrics:
            print(f"  ERROR: {metrics['error']}")
            print()
            continue
        
        print(f"  Status Filter:       {metrics['status_filter']}")
        print(f"  Markets Fetched:     {metrics['markets_fetched']}")
        print(f"  Unique Event Dates:  {metrics['unique_event_dates']}")
        print(f"  Date Range:          {metrics['min_date']} → {metrics['max_date']}")
        
        # Show expiry window intersection if provided
        if metrics.get('expiry_window_start') and metrics.get('expiry_window_end'):
            window_str = f"{metrics['expiry_window_start']} → {metrics['expiry_window_end']}"
            intersects = metrics.get('expiry_intersects')
            if intersects is True:
                print(f"  Target Window:       {window_str} ✓ INTERSECTS")
            elif intersects is False:
                print(f"  Target Window:       {window_str} ✗ NO OVERLAP")
            else:
                print(f"  Target Window:       {window_str} (unknown)")
        
        print()
        
        print(f"  Market Kinds:")
        kind_counts = metrics['kind_counts']
        print(f"    - Point:    {kind_counts['point']}")
        print(f"    - Range:    {kind_counts['range']}")
        print(f"    - Unknown:  {kind_counts['unknown']}")
        print()
        
        # Point thresholds
        if metrics['threshold_min'] is not None or metrics['threshold_max'] is not None:
            print(f"  Point Threshold Range:")
            print(f"    - Min: {metrics['threshold_min']:.2f}" if metrics['threshold_min'] else "    - Min: N/A")
            print(f"    - Max: {metrics['threshold_max']:.2f}" if metrics['threshold_max'] else "    - Max: N/A")
            print()
        
        # Range bounds
        if metrics['range_low_min'] is not None or metrics['range_high_max'] is not None:
            print(f"  Range Bounds:")
            print(f"    - Lowest Low:   {metrics['range_low_min']:.2f}" if metrics['range_low_min'] else "    - Lowest Low: N/A")
            print(f"    - Highest High: {metrics['range_high_max']:.2f}" if metrics['range_high_max'] else "    - Highest High: N/A")
            print()
        
        # Examples
        if metrics.get('date_examples'):
            print(f"  Date Examples: {', '.join(metrics['date_examples'])}")
        
        if metrics.get('point_examples'):
            print(f"  Point Examples:")
            for ex in metrics['point_examples']:
                print(f"    - {ex}")
        
        if metrics.get('range_examples'):
            print(f"  Range Examples:")
            for ex in metrics['range_examples']:
                print(f"    - {ex}")
        
        print()
    
    print("="*100)
    print()


def save_coverage_json(coverage: Dict[str, Any], output_dir: str = "runs/kalshi") -> str:
    """
    Save coverage report to JSON file.
    
    Args:
        coverage: Coverage dict
        output_dir: Output directory
    
    Returns:
        Path to saved file
    """
    # Ensure output directory exists
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"coverage_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    
    # Add metadata
    output = {
        "timestamp": datetime.now().isoformat(),
        "series_count": len(coverage),
        "coverage": coverage
    }
    
    # Write JSON
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2)
    
    logger.info(f"Coverage report saved to: {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Kalshi series coverage (date ranges, thresholds)"
    )
    parser.add_argument(
        "--series",
        type=str,
        required=True,
        help="Comma-separated list of series tickers (e.g., KXINX,KXINXY,KXINXMINY)"
    )
    parser.add_argument(
        "--status",
        type=str,
        default="open",
        choices=["open", "closed", "settled"],
        help="Market status filter (default: open)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum markets to fetch per series (default: 500)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="runs/kalshi",
        help="Output directory for JSON artifact (default: runs/kalshi)"
    )
    parser.add_argument(
        "--expiry-start",
        type=str,
        help="Target expiry window start date (YYYY-MM-DD), e.g., 2026-03-30"
    )
    parser.add_argument(
        "--expiry-end",
        type=str,
        help="Target expiry window end date (YYYY-MM-DD), e.g., 2026-05-15"
    )
    
    args = parser.parse_args()
    
    # Parse series list
    series_list = [s.strip() for s in args.series.split(",")]
    
    # Parse expiry dates if provided
    expiry_start = None
    expiry_end = None
    if args.expiry_start:
        try:
            expiry_start = date.fromisoformat(args.expiry_start)
        except ValueError:
            logger.error(f"Invalid expiry-start date format: {args.expiry_start}. Use YYYY-MM-DD")
            sys.exit(1)
    if args.expiry_end:
        try:
            expiry_end = date.fromisoformat(args.expiry_end)
        except ValueError:
            logger.error(f"Invalid expiry-end date format: {args.expiry_end}. Use YYYY-MM-DD")
            sys.exit(1)
    
    logger.info(f"Starting coverage analysis for series: {series_list}")
    logger.info(f"Status filter: {args.status}, Limit: {args.limit}")
    if expiry_start and expiry_end:
        logger.info(f"Target expiry window: {expiry_start} → {expiry_end}")
    
    # Compute coverage
    coverage = compute_coverage(
        series_list=series_list,
        status=args.status,
        limit=args.limit,
        expiry_start=expiry_start,
        expiry_end=expiry_end
    )
    
    # Print table
    print_coverage_table(coverage)
    
    # Save JSON
    filepath = save_coverage_json(coverage, output_dir=args.output_dir)
    
    print(f"✓ Coverage report saved to: {filepath}")
    print()


if __name__ == "__main__":
    main()
