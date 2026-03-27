"""
Kalshi Multi-Series Adapter: Extended p_event search with proxy support.

Enhances the Kalshi adapter to search multiple series and provide
proxy probabilities when exact matches are not available.

Series Supported:
- KXINX: Daily S&P 500 close levels
- KXINXY: Yearly S&P 500 close levels
- KXINXMINY: Yearly S&P 500 minimum levels
- KXINXMAXY: Yearly S&P 500 maximum levels
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple


logger = logging.getLogger(__name__)


# Default series to search (in priority order: exact first)
DEFAULT_KALSHI_SERIES = ["KXINX", "KXINXY", "KXINXMINY", "KXINXMAXY"]


# INDEX FAMILY MAPPING: Maps SPY/QQQ/etc to candidate Kalshi series
# Format: {underlier: [candidate_series_list]}
# For QQQ/NDX: Multiple candidates since Kalshi naming varies
INDEX_FAMILY_SERIES = {
    "SPY": ["KXINX", "KXINXY", "KXINXMINY", "KXINXMAXY"],
    "SPX": ["KXINX", "KXINXY", "KXINXMINY", "KXINXMAXY"],
    # Nasdaq-100 candidates — stale KXNDX/KXNDXY/NASDAQ100 names removed.
    # These are primary candidate families; probe each at runtime and discard
    # any that return no markets (Kalshi naming is not guaranteed-stable).
    "QQQ": ["KXNASDAQ100", "KXNASDAQ100Y", "KXNASDAQ100U"],
    "NDX": ["KXNASDAQ100", "KXNASDAQ100Y", "KXNASDAQ100U"],
}


@dataclass
class ProxyProbability:
    """
    Proxy probability with full provenance.
    
    Attributes:
        p_external_proxy: Proxy probability [0, 1]
        proxy_method: Method used (e.g., "yearly_min_hazard_scale")
        proxy_series: Series used (e.g., "KXINXMINY")
        proxy_transform: Transform applied (e.g., "hazard_scale")
        proxy_horizon_days: Horizon days for scaling
        proxy_market_ticker: Source market ticker
        proxy_source_url: URL to source market
        confidence: Confidence score [0, 1] (always low for proxy)
    """
    p_external_proxy: float
    proxy_method: str
    proxy_series: str
    proxy_transform: str
    proxy_horizon_days: int
    proxy_market_ticker: str
    proxy_source_url: str
    confidence: float


def probe_series_availability(
    client,
    series_candidates: List[str],
    status: str = "open",
    min_markets: int = 1
) -> Optional[str]:
    """
    Probe series candidates and return first non-empty series.
    
    Used to discover which Kalshi series actually exists (e.g., for NDX)
    since naming conventions may vary.
    
    Args:
        client: KalshiClient instance
        series_candidates: List of candidate series tickers to probe
        status: Market status filter
        min_markets: Minimum number of markets to consider series "available"
        
    Returns:
        First series ticker with markets, or None if all empty
    """
    logger.info(f"[SERIES_PROBE] Probing candidates: {series_candidates}")
    
    for series in series_candidates:
        try:
            markets = client.list_markets(
                series=[series],
                status=status,
                limit=10  # Just need to know if markets exist
            )
            
            if len(markets) >= min_markets:
                logger.info(f"[SERIES_PROBE] ✓ Found {len(markets)} markets in {series}")
                return series
            else:
                logger.info(f"[SERIES_PROBE] ✗ Series {series} has {len(markets)} markets (< {min_markets})")
                
        except Exception as e:
            logger.warning(f"[SERIES_PROBE] ✗ Series {series} probe failed: {e}")
            continue
    
    logger.warning(f"[SERIES_PROBE] ⚠️  No available series found in {series_candidates}")
    return None


def discover_series_for_underlier(
    client,
    underlier: str,
    status: str = "open"
) -> List[str]:
    """
    Discover available Kalshi series for an underlier.
    
    Uses INDEX_FAMILY_SERIES mapping to get candidates, then probes
    to find which actually have markets.
    
    Args:
        client: KalshiClient instance
        underlier: Underlier symbol (SPY, QQQ, etc.)
        status: Market status filter
        
    Returns:
        List of available series tickers (may be empty)
    """
    candidates = INDEX_FAMILY_SERIES.get(underlier)
    
    if not candidates:
        # Fallback to default series if underlier not in mapping
        logger.warning(f"[SERIES_DISCOVERY] No index family mapping for {underlier}, using defaults")
        return DEFAULT_KALSHI_SERIES
    
    logger.info(f"[SERIES_DISCOVERY] Discovering series for {underlier}")
    logger.info(f"[SERIES_DISCOVERY] Candidates: {candidates}")
    
    # Probe each candidate
    available_series = []
    for series in candidates:
        try:
            markets = client.list_markets(
                series=[series],
                status=status,
                limit=10
            )
            
            if len(markets) > 0:
                available_series.append(series)
                logger.info(f"[SERIES_DISCOVERY] ✓ {series}: {len(markets)} markets")
            else:
                logger.info(f"[SERIES_DISCOVERY] ✗ {series}: 0 markets")
                
        except Exception as e:
            logger.warning(f"[SERIES_DISCOVERY] ✗ {series}: probe failed ({e})")
            continue
    
    if not available_series:
        logger.warning(
            f"[SERIES_DISCOVERY] ⚠️  No available series found for {underlier}. "
            f"Tried: {candidates}"
        )
        # Return candidates anyway - let downstream handle empty results
        return candidates
    
    logger.info(f"[SERIES_DISCOVERY] Available series for {underlier}: {available_series}")
    return available_series


def fetch_all_series_markets(
    client,
    series_list: List[str] = None,
    status: str = "open"
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch markets for all series with pagination.
    
    Args:
        client: KalshiClient instance
        series_list: List of series tickers to fetch
        status: Market status filter
        
    Returns:
        Dict mapping series_ticker -> list of markets
    """
    if series_list is None:
        series_list = DEFAULT_KALSHI_SERIES
    
    results = {}
    
    for series in series_list:
        logger.info(f"Fetching markets for series: {series}")
        
        try:
            # Fetch with pagination (limit=200 is max per API call)
            # Note: Kalshi API doesn't support cursor pagination in v2,
            # so we fetch with high limit and assume we get all markets
            markets = client.list_markets(
                series=[series],
                status=status,
                limit=200  # Max allowed by API
            )
            
            results[series] = markets
            
            # TASK 1: Series sanity check when returned_markets=0
            if len(markets) == 0:
                logger.warning(f"[KALSHI_SERIES] series={series} returned_markets=0 with status='{status}'")
                
                # Try broader query without status filter to diagnose
                try:
                    all_markets = client.list_markets(
                        series=[series],
                        status=None,  # No status filter
                        limit=200
                    )
                    
                    series_exists = len(all_markets) > 0
                    series_market_count_total = len(all_markets)
                    
                    # Get sample markets (first 5)
                    series_sample_markets = []
                    for m in all_markets[:5]:
                        series_sample_markets.append({
                            "market_id": m.get("ticker", "UNKNOWN"),
                            "title": m.get("title", "")[:50],  # Truncate
                            "status": m.get("status", "UNKNOWN"),
                            "close_time": m.get("close_time", "")
                        })
                    
                    logger.info(
                        f"[KALSHI_SERIES] series={series} exists={'yes' if series_exists else 'no'} "
                        f"total={series_market_count_total} sample={series_sample_markets}"
                    )
                    
                    # Store diagnostic info in results metadata (we'll return this separately)
                    if not hasattr(results, '_diagnostics'):
                        results['_diagnostics'] = {}
                    results['_diagnostics'][series] = {
                        "series_exists": series_exists,
                        "series_market_count_total": series_market_count_total,
                        "series_sample_markets": series_sample_markets,
                        "failure_reason": "FILTER_SHAPE_MISMATCH" if series_exists else "SERIES_EMPTY_OR_ACCESS"
                    }
                    
                except Exception as diagnostic_error:
                    logger.error(f"[KALSHI_SERIES] series={series} diagnostic query failed: {diagnostic_error}")
                    if not hasattr(results, '_diagnostics'):
                        results['_diagnostics'] = {}
                    results['_diagnostics'][series] = {
                        "series_exists": None,
                        "series_market_count_total": None,
                        "series_sample_markets": [],
                        "failure_reason": "DIAGNOSTIC_QUERY_FAILED",
                        "error": str(diagnostic_error)
                    }
            else:
                logger.info(f"Fetched {len(markets)} markets for {series}")
            
        except Exception as e:
            logger.warning(f"Failed to fetch markets for {series}: {e}")
            results[series] = []
            # Record fetch failure
            if not hasattr(results, '_diagnostics'):
                results['_diagnostics'] = {}
            results['_diagnostics'][series] = {
                "series_exists": None,
                "series_market_count_total": None,
                "series_sample_markets": [],
                "failure_reason": "FETCH_FAILED",
                "error": str(e)
            }
    
    return results


def find_exact_match(
    event_definition: Dict[str, Any],
    spot_spx: float,
    markets_by_series: Dict[str, List[Dict[str, Any]]],
    max_mapping_error: float = 0.05
) -> Tuple[Optional[Tuple[Dict[str, Any], str]], Optional[Dict[str, Any]]]:
    """
    Find exact match to event definition with closest match tracking.
    
    Searches daily and yearly series for markets that match the exact
    expiry and threshold of the event.
    
    Args:
        event_definition: Event definition (index_drawdown format)
        spot_spx: Current SPX spot
        markets_by_series: Markets grouped by series
        max_mapping_error: Max acceptable mapping error
        
    Returns:
        Tuple of (
            exact_match: (market_dict, series_ticker) or None,
            closest_match_info: dict with best attempted match or None
        )
    """
    from ..kalshi.market_mapper import map_event_to_markets
    
    # Try exact-match series first (KXINX daily, KXINXY yearly)
    exact_series = ["KXINX", "KXINXY"]
    
    # Track closest match across all series
    closest_match_info = None
    best_error = float('inf')
    
    for series in exact_series:
        if series not in markets_by_series:
            continue
        
        markets = markets_by_series[series]
        if not markets:
            continue
        
        # Use existing mapper to find candidates
        try:
            # PHASE 6.5: Get ALL candidates (not just those passing tolerance)
            # by temporarily setting very high tolerance, then filter ourselves
            all_candidates = map_event_to_markets(
                event_def=event_definition,
                spot_spx=spot_spx,
                kalshi_markets=markets,
                max_mapping_error=1.0  # Get all candidates regardless of error
            )
            
            if all_candidates:
                # Track best candidate overall (even if it fails tolerance)
                for cand in all_candidates:
                    if cand.mapping_error < best_error:
                        best_error = cand.mapping_error
                        closest_match_info = {
                            "series": series,
                            "ticker": cand.ticker,
                            "mapping_error_pct": cand.mapping_error * 100,
                            "implied_level": cand.implied_level,
                            "liquidity_score": cand.liquidity_score
                        }
                
                # Check if any pass our actual tolerance
                passing_candidates = [c for c in all_candidates if c.mapping_error <= max_mapping_error]
                
                if passing_candidates:
                    # Return top passing candidate
                    top = passing_candidates[0]
                    # Find full market dict
                    market = next(
                        (m for m in markets if m.get("ticker") == top.ticker),
                        None
                    )
                    if market:
                        logger.info(f"Found exact match in {series}: {top.ticker} (error={top.mapping_error:.2%})")
                        return ((market, series), closest_match_info)
        
        except Exception as e:
            logger.warning(f"Error searching {series}: {e}")
            continue
    
    return (None, closest_match_info)


def compute_yearly_min_proxy(
    event_definition: Dict[str, Any],
    spot_spx: float,
    markets_by_series: Dict[str, List[Dict[str, Any]]],
    horizon_days: int
) -> Optional[ProxyProbability]:
    """
    Compute proxy using yearly minimum hazard rate scaling.
    
    Method:
    1. Find KXINXMINY market for "yearly min < barrier"
    2. Extract p_1y_breach from Kalshi pricing
    3. Scale to horizon T: p_T = 1 - (1 - p_1y)^(T/365)
    4. Return with low confidence and warnings
    
    Args:
        event_definition: Event definition
        spot_spx: Current SPX spot
        markets_by_series: Markets grouped by series
        horizon_days: Target horizon in days
        
    Returns:
        ProxyProbability or None if unable to compute
    """
    from ..kalshi.client import BASE_URL
    from ..kalshi.numeric import as_float, as_probability, safe_hazard_scale
    
    # Extract target level
    threshold_pct = event_definition.get("threshold_pct", 0)
    target_level = spot_spx * (1 + threshold_pct)
    
    # Get KXINXMINY markets
    if "KXINXMINY" not in markets_by_series:
        return None
    
    miny_markets = markets_by_series["KXINXMINY"]
    if not miny_markets:
        return None
    
    # Find market closest to target level
    # For yearly min, we want market that represents "min < target"
    best_match = None
    best_error = float('inf')
    
    for market in miny_markets:
        # Parse market level from ticker/title
        from ..kalshi.market_mapper import parse_market_level
        
        level_info = parse_market_level(
            market.get("ticker", ""),
            market.get("title", "")
        )
        
        if level_info is None:
            continue
        
        try:
            if level_info["market_type"] == "level":
                market_level = as_float(level_info["level"], "market_level")
            elif level_info["market_type"] == "range":
                market_level = as_float(level_info["mid"], "market_mid")
            else:
                continue
        except ValueError as e:
            logger.warning(f"Skipping market with invalid level: {e}")
            continue
        
        # Calculate error
        error = abs(market_level - target_level) / target_level
        
        if error < best_error:
            best_error = error
            best_match = market
    
    if best_match is None or best_error > 0.15:  # Max 15% error for proxy
        logger.info("No suitable KXINXMINY market found for proxy")
        return None
    
    # Get probability from market
    # Try to get yes_bid/yes_ask from market data
    yes_bid = best_match.get("yes_bid")
    yes_ask = best_match.get("yes_ask")
    
    if yes_bid is None or yes_ask is None:
        logger.warning("No pricing data for KXINXMINY market, cannot compute proxy")
        return None
    
    try:
        # Validate pricing data
        # NOTE: Kalshi returns prices in CENTS (0-100), need to normalize to probabilities (0-1)
        logger.info(f"KXINXMINY market {best_match.get('ticker')}: yes_bid={yes_bid}, yes_ask={yes_ask} (cents)")
        
        # Convert from cents to probability
        yes_bid_cents = as_float(yes_bid, "yes_bid_cents")
        yes_ask_cents = as_float(yes_ask, "yes_ask_cents")
        
        # Normalize: cents to probability (divide by 100)
        yes_bid_float = yes_bid_cents / 100.0
        yes_ask_float = yes_ask_cents / 100.0
        
        # Validate the normalized probabilities
        yes_bid_float = as_probability(yes_bid_float, "yes_bid")
        yes_ask_float = as_probability(yes_ask_float, "yes_ask")
        
        # Compute mid-price
        p_1y_breach = (yes_bid_float + yes_ask_float) / 2.0
        
        # Validate the mid-price is also a valid probability
        p_1y_breach = as_probability(p_1y_breach, "p_1y_breach")
        
        logger.info(f"Normalized to probabilities: yes_bid={yes_bid_float:.4f}, yes_ask={yes_ask_float:.4f}, mid={p_1y_breach:.4f}")
        
    except ValueError as e:
        logger.warning(
            f"Invalid pricing data in KXINXMINY market {best_match.get('ticker')}: "
            f"yes_bid={yes_bid}, yes_ask={yes_ask}. Error: {e}"
        )
        return None
    
    # Apply hazard rate scaling with validation
    try:
        p_T = safe_hazard_scale(p_1y_breach, horizon_days, "p_1y_breach")
    except ValueError as e:
        logger.warning(f"Hazard scaling failed: {e}")
        return None
    
    # Clamp to reasonable range for proxy (avoid extreme values)
    p_T = max(0.01, min(0.99, p_T))
    
    # Build proxy result
    ticker = best_match.get("ticker", "UNKNOWN")
    
    proxy = ProxyProbability(
        p_external_proxy=p_T,
        proxy_method="yearly_min_hazard_scale",
        proxy_series="KXINXMINY",
        proxy_transform="hazard_scale",
        proxy_horizon_days=horizon_days,
        proxy_market_ticker=ticker,
        proxy_source_url=f"{BASE_URL}/markets/{ticker}",
        confidence=0.35  # Fixed low confidence
    )
    
    logger.info(
        f"Computed proxy: p_1y={p_1y_breach:.2%} -> p_{horizon_days}d={p_T:.2%} "
        f"(market={ticker})"
    )
    
    return proxy


def kalshi_multi_series_search(
    event_definition: Dict[str, Any],
    client,
    spot_spx: float,
    horizon_days: int = 45,
    allow_proxy: bool = False,
    max_mapping_error: float = 0.05,
    series_list: List[str] = None
) -> Dict[str, Any]:
    """
    Search Kalshi markets across multiple series for event probability.
    
    Search order:
    1. Exact match in daily/yearly series (p_external)
    2. If no exact and allow_proxy=True: compute proxy (p_external_proxy)
    3. If no exact and allow_proxy=False: return "no match"
    
    Args:
        event_definition: Event definition (index_drawdown format)
        client: KalshiClient instance
        spot_spx: Current SPX spot
        horizon_days: Event horizon in days (for proxy scaling)
        allow_proxy: Whether to allow proxy probabilities
        max_mapping_error: Max acceptable mapping error for exact match
        series_list: List of series to search (defaults to DEFAULT_KALSHI_SERIES)
        
    Returns:
        Dict with:
        - exact_match: bool
        - p_external: float or None
        - market_ticker: str or None
        - source_series: str or None
        - proxy: ProxyProbability or None (if proxy used)
        - warnings: List[str]
    """
    if series_list is None:
        series_list = DEFAULT_KALSHI_SERIES
    
    warnings = []
    
    # Fetch all markets
    logger.info(f"Fetching markets from series: {series_list}")
    markets_by_series = fetch_all_series_markets(client, series_list)
    
    total_markets = sum(len(ms) for ms in markets_by_series.values())
    logger.info(f"Fetched {total_markets} total markets across {len(markets_by_series)} series")
    
    # Try exact match first (returns tuple: (match_result, closest_match_info))
    exact_match_result, closest_match_info = find_exact_match(
        event_definition,
        spot_spx,
        markets_by_series,
        max_mapping_error
    )
    
    # TASK 2: Build diagnostics dict with filters/retrieval separation
    diagnostics = {
        "filters": {
            "target_expiry": event_definition.get("date"),
            "target_level": spot_spx * (1 + event_definition.get("threshold_pct", 0)),
            "comparator": event_definition.get("comparator", "below"),
            "max_mapping_error": max_mapping_error,
            "status_tried": "open"
        },
        "retrieval": {
            "series_tried": series_list,
            "returned_markets_filtered": total_markets,
            "returned_markets_unfiltered": sum(
                len(markets_by_series.get('_diagnostics', {}).get(s, {}).get('series_sample_markets', [])) 
                for s in series_list
            ) if total_markets == 0 else total_markets,
            "markets_by_series": {s: len(ms) for s, ms in markets_by_series.items() if s != '_diagnostics'}
        },
        "closest_match": closest_match_info
    }
    
    # Add series diagnostics if available
    if '_diagnostics' in markets_by_series:
        diagnostics["series_diagnostics"] = markets_by_series['_diagnostics']
    
    if exact_match_result:
        market, series = exact_match_result
        
        # Get probability from oracle
        from ..oracle.kalshi_oracle import KalshiOracle
        oracle = KalshiOracle(client)
        oracle_data = oracle.get_event_probability(market)
        
        if oracle_data is None:
            warnings.append("EXACT_MATCH_FOUND_BUT_NO_PROBABILITY")
            logger.warning(f"Found market {market.get('ticker')} but failed to get probability")
        else:
            # Return exact match with diagnostics
            return {
                "exact_match": True,
                "p_external": oracle_data["p_event"],
                "market_ticker": oracle_data["market_id"],
                "source_series": series,
                "proxy": None,
                "warnings": warnings,
                "diagnostics": diagnostics  # PHASE 6.5
            }
    
    # No exact match - try proxy if allowed
    if not allow_proxy:
        logger.info("No exact match found and proxy disabled - returning no match")
        return {
            "exact_match": False,
            "p_external": None,
            "market_ticker": None,
            "source_series": None,
            "proxy": None,
            "warnings": ["NO_EXACT_MATCH"],
            "diagnostics": diagnostics  # PHASE 6.5
        }
    
    # Compute proxy
    logger.info("No exact match - attempting proxy calculation")
    proxy = compute_yearly_min_proxy(
        event_definition,
        spot_spx,
        markets_by_series,
        horizon_days
    )
    
    if proxy is None:
        warnings.append("NO_EXACT_MATCH")
        warnings.append("PROXY_CALCULATION_FAILED")
        return {
            "exact_match": False,
            "p_external": None,
            "market_ticker": None,
            "source_series": None,
            "proxy": None,
            "warnings": warnings,
            "diagnostics": diagnostics  # PHASE 6.5
        }
    
    # Return with proxy
    warnings.extend([
        "PROXY_USED",
        "LOW_CONFIDENCE_PROXY",
        "HORIZON_MISMATCH",
        "ASSUMPTION_HAZARD_RATE"
    ])
    
    return {
        "exact_match": False,
        "p_external": None,  # Keep as None to not override existing behavior
        "market_ticker": None,
        "source_series": None,
        "proxy": proxy,
        "warnings": warnings,
        "diagnostics": diagnostics  # PHASE 6.5
    }
