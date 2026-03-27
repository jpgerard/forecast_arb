"""
Kalshi Market Mapper: Deterministic event-to-market mapping.

Provides auditable, explicit mapping from internal event definitions to
Kalshi markets with ranking and provenance tracking.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Any

from forecast_arb.kalshi.series_coverage import get_coverage_manager
from forecast_arb.kalshi.threshold_parser import (
    parse_threshold_from_market,
    infer_series_from_ticker,
)


# ---------------------------------------------------------------------------
# Diagnostic codes — emitted in the structured mapping summary block.
# Callers may inspect logs for these strings to distinguish failure modes.
# ---------------------------------------------------------------------------

#: The ``kalshi_markets`` input list was empty.
DIAG_NO_MARKETS_RETURNED = "NO_MARKETS_RETURNED"
#: Markets were provided but none matched the SPX/S&P 500 filter.
DIAG_NO_REPRESENTABLE_MARKETS = "NO_REPRESENTABLE_MARKETS"
#: SPX markets found but none matched the target expiry date.
DIAG_DATE_MISMATCH = "DATE_MISMATCH"
#: Date-matched markets found but level/range could not be parsed from any.
DIAG_PARSE_FAILURE = "PARSE_FAILURE"
#: Parsed markets found but all fell outside ``max_mapping_error``.
DIAG_THRESHOLD_MISMATCH = "THRESHOLD_MISMATCH"
#: Candidates found; best candidate has mapping_error > 0 (inexact match).
DIAG_EXACT_MATCH_UNAVAILABLE = "EXACT_MATCH_UNAVAILABLE"


logger = logging.getLogger(__name__)


@dataclass
class MappedKalshiMarket:
    """
    A candidate Kalshi market matched to an event definition.
    
    Attributes:
        ticker: Kalshi market ticker (e.g., "INX-26FEB27-B4200")
        title: Human-readable market title
        close_time: Market close/settlement datetime
        implied_level: The strike/level implied by this market
        mapping_error: Relative error between target and market level
        liquidity_score: Normalized liquidity score [0, 1]
        rationale: Human-readable explanation of mapping decision
    """
    ticker: str
    title: str
    close_time: datetime
    implied_level: float
    mapping_error: float
    liquidity_score: float
    rationale: str


def validate_event_def(event_def: Dict[str, Any]) -> None:
    """
    Validate event definition is supported.
    
    Args:
        event_def: Event definition dict
        
    Raises:
        ValueError: If event type or index not supported
    """
    event_type = event_def.get("type")
    index = event_def.get("index")
    
    # Hard fail on unsupported types
    if event_type != "index_drawdown":
        raise ValueError(
            f"Unsupported event type: {event_type}. "
            f"Only 'index_drawdown' is supported."
        )
    
    # Hard fail on unsupported indices
    if index != "SPX":
        raise ValueError(
            f"Unsupported index: {index}. "
            f"Only 'SPX' is supported."
        )
    
    # Validate required fields
    required_fields = ["threshold_pct", "expiry"]
    for field in required_fields:
        if field not in event_def:
            raise ValueError(f"Missing required field: {field}")


def parse_market_level(ticker: str, title: str) -> Optional[Dict[str, Any]]:
    """
    Parse market level or range from ticker and title.

    TRANSITIONAL SHIM — delegates to
    ``threshold_parser.parse_threshold_from_market()``.

    TODO (Patch B): Remove this function and migrate call sites to use
    ``parse_threshold_from_market()`` directly, which returns the richer
    typed result dict.

    Behavioural changes vs the old implementation
    ----------------------------------------------
    * **B-tickers without a range title now return None.**  The old
      implementation extracted ``level=<B_value>`` from the ticker digit
      sequence.  This was incorrect — B-tickers mark range boundaries, not
      point levels — and has been removed.  Callers relying on that path
      should pass a title with explicit "between X and Y" text.
    * Decimal thresholds (e.g. ``T7199.9999``) are now supported.
    * KXINXMINY / KXINXMAXY final-segment decimals
      (e.g. ``KXINXMINY-01JAN2027-6600.01``) are now supported.
    * Comma-separated numbers in titles are now handled.

    Args:
        ticker: Market ticker (e.g., "KXINX-26FEB27H1600-T7199.9999")
        title:  Market title

    Returns:
        Dict with ``market_type`` key ("level" or "range") and associated
        fields, or ``None`` if the market cannot be parsed.
    """
    market = {"ticker": ticker, "title": title}
    parsed = parse_threshold_from_market(market)

    kind = parsed.get("kind")

    if kind == "point":
        threshold = parsed.get("threshold")
        if threshold is None:
            return None
        direction = "above" if "above" in title.lower() else "below"
        return {
            "market_type": "level",
            "level": threshold,
            "direction": direction,
        }

    if kind == "range":
        low = parsed.get("low")
        high = parsed.get("high")
        if low is None or high is None:
            return None
        return {
            "market_type": "range",
            "low": low,
            "high": high,
            "mid": (low + high) / 2,
        }

    # threshold_parser returned unknown; try title-based range as an
    # additional fallback for non-KXINX series with "between X and Y" titles.
    range_pattern = (
        r'\b(?:between|from)\s+([\d,]+(?:\.\d+)?)\s+(?:and|to)\s+([\d,]+(?:\.\d+)?)\b'
    )
    m = re.search(range_pattern, title, re.IGNORECASE)
    if m:
        try:
            low = float(m.group(1).replace(",", ""))
            high = float(m.group(2).replace(",", ""))
            if low < high and low > 0:
                return {
                    "market_type": "range",
                    "low": low,
                    "high": high,
                    "mid": (low + high) / 2,
                }
        except ValueError:
            pass

    return None


def parse_market_date(close_time: str) -> Optional[date]:
    """
    Parse market close date.
    
    Args:
        close_time: ISO format datetime string
        
    Returns:
        date object or None if unparseable
    """
    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        return dt.date()
    except (ValueError, AttributeError):
        return None


def is_spx_market(ticker: str, title: str) -> bool:
    """
    Check if market is SPX/S&P 500 related.
    
    Args:
        ticker: Market ticker
        title: Market title
        
    Returns:
        True if SPX market
    """
    spx_indicators = [
        "SPX", "S&P 500", "S&P500", "INX", "^GSPC"
    ]
    
    text = f"{ticker} {title}".upper()
    return any(indicator.upper() in text for indicator in spx_indicators)


def calculate_liquidity_score(market: Dict[str, Any]) -> float:
    """
    Calculate normalized liquidity score.
    
    Args:
        market: Market dict with volume/depth info
        
    Returns:
        Liquidity score [0, 1]
    """
    # Use volume_24h if available
    volume = market.get("volume_24h", 0) or market.get("volume", 0)
    
    # Use open_interest if available
    open_interest = market.get("open_interest", 0)
    
    # Normalize: score increases with volume and open interest
    # Baseline: 100 volume = 0.5 score, 1000 volume = 1.0 score
    if volume > 0:
        volume_score = min(1.0, volume / 1000.0)
    else:
        volume_score = 0.3  # Default for unknown
    
    if open_interest > 0:
        oi_score = min(1.0, open_interest / 500.0)
        return 0.6 * volume_score + 0.4 * oi_score
    
    return volume_score


def map_event_to_markets(
    event_def: Dict[str, Any],
    spot_spx: float,
    kalshi_markets: List[Dict[str, Any]],
    max_mapping_error: float = 0.05,
    enable_coverage_precheck: bool = True
) -> List[MappedKalshiMarket]:
    """
    Map event definition to ranked Kalshi markets.
    
    Args:
        event_def: Event definition with fields:
            - type: "index_drawdown"
            - index: "SPX"
            - threshold_pct: float (e.g., -0.15)
            - expiry: date object
        spot_spx: Current SPX spot price
        kalshi_markets: List of Kalshi market dicts
        max_mapping_error: Maximum acceptable mapping error
        enable_coverage_precheck: If True, use coverage precheck to skip out-of-range series
        
    Returns:
        List of MappedKalshiMarket, ranked by mapping_error and liquidity
        
    Raises:
        ValueError: If event_def is invalid
    """
    # Validate event definition
    validate_event_def(event_def)
    
    # Extract event parameters
    threshold_pct = event_def["threshold_pct"]
    expiry = event_def["expiry"]
    
    # Convert expiry to date if needed
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry).date()
    elif isinstance(expiry, datetime):
        expiry = expiry.date()
    
    # Calculate target level
    target_level = spot_spx * (1 + threshold_pct)
    
    logger.info(
        f"Mapping event: threshold_pct={threshold_pct:.2%}, "
        f"expiry={expiry}, target_level={target_level:.2f}"
    )
    
    # Coverage precheck (if enabled)
    coverage_precheck_results = {}
    if enable_coverage_precheck:
        coverage_precheck_results = _run_coverage_precheck(expiry)
    
    # Iterate all markets and find candidates; track waterfall counters for diagnostics
    candidates = []
    n_input = len(kalshi_markets)
    n_spx = 0
    n_date_matched = 0
    n_level_parsed = 0

    for market in kalshi_markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        close_time_str = market.get("close_time", "")

        # Skip if not binary market
        market_type = market.get("market_type", "")
        if market_type and market_type != "binary":
            continue

        # Skip if not SPX market
        if not is_spx_market(ticker, title):
            continue
        n_spx += 1

        # Coverage precheck: skip if market's series doesn't cover target expiry
        if enable_coverage_precheck and coverage_precheck_results:
            series = infer_series_from_ticker(ticker)
            if series and series in coverage_precheck_results:
                precheck = coverage_precheck_results[series]
                if not precheck.get("covers", False):
                    logger.debug(
                        f"Skipping {ticker}: series {series} doesn't cover expiry "
                        f"(reason: {precheck.get('reason')})"
                    )
                    continue

        # Parse close date
        market_date = parse_market_date(close_time_str)
        if market_date is None:
            continue

        # Check expiry match (must be exact)
        if market_date != expiry:
            continue
        n_date_matched += 1

        # Parse market level/range
        level_info = parse_market_level(ticker, title)
        if level_info is None:
            continue
        n_level_parsed += 1

        # Calculate mapping error based on market type
        if level_info["market_type"] == "level":
            market_level = level_info["level"]
            mapping_error = abs(market_level - target_level) / target_level
            rationale = (
                f"Level-based market at {market_level:.0f} "
                f"vs target {target_level:.0f} "
                f"({mapping_error:.2%} error)"
            )

        elif level_info["market_type"] == "range":
            market_mid = level_info["mid"]
            mapping_error = abs(market_mid - target_level) / target_level
            rationale = (
                f"Range-based market [{level_info['low']:.0f}, {level_info['high']:.0f}], "
                f"mid={market_mid:.0f} vs target {target_level:.0f} "
                f"({mapping_error:.2%} error)"
            )

        else:
            continue

        # Filter by max mapping error
        if mapping_error > max_mapping_error:
            continue

        # Calculate liquidity score
        liquidity_score = calculate_liquidity_score(market)

        # Parse close_time to datetime
        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            close_time = datetime.now()

        # Create mapped market
        mapped = MappedKalshiMarket(
            ticker=ticker,
            title=title,
            close_time=close_time,
            implied_level=level_info.get("level") or level_info.get("mid"),
            mapping_error=mapping_error,
            liquidity_score=liquidity_score,
            rationale=rationale,
        )

        candidates.append(mapped)

    # Sort by mapping error (ascending), then liquidity (descending)
    candidates.sort(key=lambda m: (m.mapping_error, -m.liquidity_score))

    # ------------------------------------------------------------------
    # Structured diagnostic summary block — one entry per mapping pass.
    # Distinguishes failure modes at each filtering stage.
    # ------------------------------------------------------------------
    n_candidates = len(candidates)
    if n_input == 0:
        diag_code = DIAG_NO_MARKETS_RETURNED
    elif n_spx == 0:
        diag_code = DIAG_NO_REPRESENTABLE_MARKETS
    elif n_date_matched == 0:
        diag_code = DIAG_DATE_MISMATCH
    elif n_level_parsed == 0:
        diag_code = DIAG_PARSE_FAILURE
    elif n_candidates == 0:
        diag_code = DIAG_THRESHOLD_MISMATCH
    elif candidates[0].mapping_error > 0:
        diag_code = DIAG_EXACT_MATCH_UNAVAILABLE
    else:
        diag_code = None  # exact match found

    rejected_series = (
        [s for s, r in coverage_precheck_results.items() if not r.get("covers", False)]
        if enable_coverage_precheck and coverage_precheck_results
        else []
    )

    diag_summary = {
        "event": "MAP_EVENT_TO_MARKETS",
        "target_level": round(target_level, 2),
        "expiry": str(expiry),
        "n_input_markets": n_input,
        "n_spx_markets": n_spx,
        "n_date_matched": n_date_matched,
        "n_level_parsed": n_level_parsed,
        "n_candidates": n_candidates,
        "diag_code": diag_code,
        "coverage_precheck_rejected": rejected_series,
    }
    logger.info("[market_mapper] mapping pass summary: %s", diag_summary)

    return candidates


def _run_coverage_precheck(target_expiry: date) -> Dict[str, Dict[str, Any]]:
    """
    Run coverage precheck for known series.
    
    Args:
        target_expiry: Target expiry date
    
    Returns:
        Dict mapping series -> coverage check result
    """
    # Known SPX series (KXINXMAXY added — yearly max — alongside yearly min)
    known_series = ["KXINX", "KXINXY", "KXINXMINY", "KXINXMAXY"]
    
    coverage_manager = get_coverage_manager()
    
    # Get coverage for all known series
    try:
        coverage = coverage_manager.get_coverage(known_series, status="open", limit=500)
    except Exception as e:
        logger.warning(f"Coverage precheck failed: {e}")
        return {}
    
    # Check each series
    results = {}
    for series in known_series:
        check = coverage_manager.check_expiry_coverage(
            series=series,
            target_expiry=target_expiry,
            coverage=coverage
        )
        results[series] = check
    
    return results
