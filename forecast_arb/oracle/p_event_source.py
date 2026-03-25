"""
P-Event Source: Pluggable event probability providers.

Provides a first-class, pluggable architecture for obtaining p_event with
explicit modes that either hard fail, fallback gracefully, or derive probabilities
from alternative sources like the options market.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Any
from enum import Enum


logger = logging.getLogger(__name__)


class KalshiUnavailableError(RuntimeError):
    """Exception raised when Kalshi API is unavailable or returns no valid data."""
    pass


class PEventSourceType(Enum):
    """Types of p_event sources."""
    KALSHI = "kalshi"
    KALSHI_OR_FALLBACK = "kalshi_or_fallback"
    FALLBACK_ONLY = "fallback_only"
    OPTIONS_IMPLIED = "options_implied"
    ENSEMBLE = "ensemble"  # Future: blend multiple sources


@dataclass
class PEventResult:
    """
    Result from a p_event source with full provenance metadata.
    
    Attributes:
        p_event: Event probability [0, 1]
        source: Source type that provided this probability
        confidence: Confidence score [0, 1]
        timestamp: When this probability was obtained
        metadata: Source-specific metadata (market IDs, strikes, etc.)
        fallback_used: Whether a fallback was used
        warnings: Any warnings encountered during fetch
    """
    p_event: float
    source: str
    confidence: float
    timestamp: str
    metadata: Dict[str, Any]
    fallback_used: bool = False
    warnings: list = None
    
    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "p_event": self.p_event,
            "source": self.source,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "fallback_used": self.fallback_used,
            "warnings": self.warnings
        }


class PEventSource(ABC):
    """Base class for p_event sources."""
    
    @abstractmethod
    def get_p_event(
        self,
        event_definition: Dict[str, Any],
        **kwargs
    ) -> PEventResult:
        """
        Get event probability.
        
        Args:
            event_definition: Event specification dict with fields like:
                - type: "index_level", "price_move", "volatility_spike", etc.
                - underlying: "SPX", "SPY", etc.
                - date: Expiry or event date
                - threshold: Level/price threshold
                - direction: "below", "above", etc.
            **kwargs: Additional source-specific parameters
            
        Returns:
            PEventResult with probability and provenance
            
        Raises:
            ValueError: If event cannot be priced by this source
            RuntimeError: If source is unavailable (for hard-fail modes)
        """
        pass


class KalshiPEventSource(PEventSource):
    """
    Kalshi-based p_event source.
    
    Searches Kalshi for relevant markets and extracts probabilities.
    Mode: Hard fail if Kalshi unavailable or no matching market found.
    """
    
    def __init__(self, client, allow_proxy: bool = True):
        """
        Initialize Kalshi source.
        
        Args:
            client: KalshiClient instance
            allow_proxy: Whether to allow proxy probabilities (default: True)
        """
        self.client = client
        self.allow_proxy = allow_proxy
    
    def get_p_event(
        self,
        event_definition: Dict[str, Any],
        **kwargs
    ) -> PEventResult:
        """Get probability from Kalshi market."""
        from ..oracle.kalshi_oracle import KalshiOracle
        from ..kalshi.market_mapper import map_event_to_markets
        from ..kalshi.multi_series_adapter import kalshi_multi_series_search
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Get spot SPX if using new mapper
        spot_spx = kwargs.get("spot_spx")
        
        # Check if event_definition uses new format (index_drawdown)
        if event_definition.get("type") == "index_drawdown" and spot_spx is not None:
            # Use multi-series adapter for enhanced search
            horizon_days = kwargs.get("horizon_days", 45)
            
            try:
                search_result = kalshi_multi_series_search(
                    event_definition=event_definition,
                    client=self.client,
                    spot_spx=spot_spx,
                    horizon_days=horizon_days,
                    allow_proxy=self.allow_proxy,
                    max_mapping_error=kwargs.get("max_mapping_error", 0.05)
                )
                
                # If exact match found
                if search_result["exact_match"] and search_result["p_external"] is not None:
                    from ..kalshi.client import BASE_URL
                    
                    # PHASE 6.5: Include diagnostics in metadata
                    metadata = {
                        "base_url": BASE_URL,
                        "market_ticker": search_result["market_ticker"],
                        "source_series": search_result["source_series"],
                        "event_def": event_definition
                    }
                    if "diagnostics" in search_result:
                        metadata["diagnostics"] = search_result["diagnostics"]
                    
                    return PEventResult(
                        p_event=search_result["p_external"],
                        source="kalshi",
                        confidence=0.70,  # Standard confidence for exact match
                        timestamp=timestamp,
                        metadata=metadata,
                        fallback_used=False,
                        warnings=search_result.get("warnings", [])
                    )
                
                # If proxy found and proxy allowed
                if search_result["proxy"] is not None:
                    from ..kalshi.client import BASE_URL
                    proxy = search_result["proxy"]
                    
                    return PEventResult(
                        p_event=None,  # Keep p_event as None - do not override
                        source="kalshi",
                        confidence=0.0,  # No p_external, so confidence is 0
                        timestamp=timestamp,
                        metadata={
                            "base_url": BASE_URL,
                            "event_def": event_definition,
                            # Proxy metadata - explicitly labeled
                            "p_external_proxy": proxy.p_external_proxy,
                            "proxy_method": proxy.proxy_method,
                            "proxy_series": proxy.proxy_series,
                            "proxy_transform": proxy.proxy_transform,
                            "proxy_horizon_days": proxy.proxy_horizon_days,
                            "proxy_market_ticker": proxy.proxy_market_ticker,
                            "proxy_source_url": proxy.proxy_source_url,
                            "proxy_confidence": proxy.confidence
                        },
                        fallback_used=False,
                        warnings=search_result.get("warnings", [])
                    )
                
                # No match found - return structured failure
                # PHASE 6.5: Include diagnostics in metadata
                metadata = {
                    "event_def": event_definition,
                    "spot_spx": spot_spx
                }
                if "diagnostics" in search_result:
                    metadata["diagnostics"] = search_result["diagnostics"]
                
                return PEventResult(
                    p_event=None,
                    source="kalshi",
                    confidence=0.0,
                    timestamp=timestamp,
                    metadata=metadata,
                    fallback_used=False,
                    warnings=search_result.get("warnings", ["NO_MARKET_MATCH"])
                )
                
            except Exception as e:
                logger.exception(f"Multi-series search failed: {e}", exc_info=True)
                # Fall through to legacy path
        
        # Legacy path (original implementation)
        if event_definition.get("type") == "index_drawdown" and spot_spx is not None:
            # Use new deterministic mapper
            try:
                # Fetch SPX markets from KXINX series (daily S&P 500 markets)
                kalshi_markets = self.client.list_markets(
                    series=["KXINX"],  # Daily S&P 500 series
                    status="open",
                    limit=200
                )
                
                # Map event to candidate markets
                candidates = map_event_to_markets(
                    event_def=event_definition,
                    spot_spx=spot_spx,
                    kalshi_markets=kalshi_markets,
                    max_mapping_error=kwargs.get("max_mapping_error", 0.05)
                )
                
                # If no candidates, return structured failure
                if not candidates:
                    return PEventResult(
                        p_event=None,
                        source="kalshi",
                        confidence=0.0,
                        timestamp=timestamp,
                        metadata={
                            "event_def": event_definition,
                            "spot_spx": spot_spx,
                            "total_markets_searched": len(kalshi_markets)
                        },
                        fallback_used=False,
                        warnings=["NO_MARKET_MATCH"]
                    )
                
                # Use top candidate
                top_candidate = candidates[0]
                
                # Find the full market dict
                market = next(
                    (m for m in kalshi_markets if m.get("ticker") == top_candidate.ticker),
                    None
                )
                
                if market is None:
                    raise KalshiUnavailableError("Candidate market not found in market list")
                
                # Get probability from oracle
                oracle = KalshiOracle(self.client)
                oracle_data = oracle.get_event_probability(market)
                
                if oracle_data is None:
                    raise KalshiUnavailableError(
                        f"Failed to get probability from market {top_candidate.ticker}"
                    )
                
                # Build result with full provenance
                from ..kalshi.client import BASE_URL
                
                target_level = spot_spx * (1 + event_definition["threshold_pct"])
                
                return PEventResult(
                    p_event=oracle_data["p_event"],
                    source="kalshi",
                    confidence=self._assess_confidence(oracle_data),
                    timestamp=timestamp,
                    metadata={
                        "base_url": BASE_URL,
                        "event_def": event_definition,
                        "target_level": target_level,
                        "market_ticker": top_candidate.ticker,
                        "mapping_error": top_candidate.mapping_error,
                        "liquidity_score": top_candidate.liquidity_score,
                        "rationale": top_candidate.rationale,
                        "bid": oracle_data["bid"],
                        "ask": oracle_data["ask"],
                        "num_candidates": len(candidates)
                    },
                    fallback_used=False,
                    warnings=[]
                )
                
            except ValueError as e:
                # Event definition validation failed
                raise KalshiUnavailableError(f"Invalid event definition: {e}")
            except Exception as e:
                # Kalshi API or mapping failed
                raise KalshiUnavailableError(f"Kalshi market mapping failed: {e}")
        
        # Fallback to legacy search path
        market = self._find_matching_market(event_definition)
        
        if market is None:
            raise KalshiUnavailableError(
                f"Kalshi smoke validation failed: No market found for event {event_definition}"
            )
        
        # Get probability from oracle
        oracle = KalshiOracle(self.client)
        oracle_data = oracle.get_event_probability(market)
        
        if oracle_data is None:
            raise KalshiUnavailableError(
                f"Kalshi smoke validation failed: Failed to get probability from market {market.get('ticker')}"
            )
        
        # Build result with full provenance (exact format per specification)
        from ..kalshi.client import BASE_URL
        
        return PEventResult(
            p_event=oracle_data["p_event"],
            source="kalshi",
            confidence=self._assess_confidence(oracle_data),
            timestamp=timestamp,
            metadata={
                "base_url": BASE_URL,
                "market_ticker": oracle_data["market_id"],
                "bid": oracle_data["bid"],
                "ask": oracle_data["ask"]
            },
            fallback_used=False,
            warnings=[]
        )
    
    def _find_matching_market(self, event_definition: Dict) -> Optional[Dict]:
        """
        Find Kalshi market matching event definition.
        
        Args:
            event_definition: Event specification
            
        Returns:
            Market dict or None
        """
        event_type = event_definition.get("type")
        underlying = event_definition.get("underlying")
        date = event_definition.get("date")
        
        # Build search based on event type
        if event_type in ["index_level", "price_move"]:
            # Search by series/tags related to the index
            search_terms = self._get_search_terms_for_underlying(underlying)
            
            try:
                markets = self.client.list_markets(status="open", limit=100)
                
                # Filter by search terms and date proximity
                matching = []
                for market in markets:
                    market_text = f"{market.get('ticker', '')} {market.get('title', '')}".upper()
                    
                    for term in search_terms:
                        if term.upper() in market_text:
                            # Check date if specified
                            if date:
                                market_close = market.get("close_time", "")
                                if date in market_close:
                                    matching.append(market)
                                    break
                            else:
                                matching.append(market)
                                break
                
                # Return highest volume market
                if matching:
                    matching.sort(key=lambda m: m.get("volume_24h", 0), reverse=True)
                    return matching[0]
                    
            except Exception as e:
                logger.error(f"Error searching Kalshi markets: {e}")
                raise KalshiUnavailableError(f"Kalshi smoke validation failed: {e}")
        
        return None
    
    def _get_search_terms_for_underlying(self, underlying: str) -> list:
        """Get search terms for an underlying."""
        terms_map = {
            "SPY": ["SPY", "SP500", "S&P 500", "SPX"],
            "SPX": ["SP500", "S&P 500", "SPX", "INX"],
            "QQQ": ["NASDAQ", "NDX", "QQQ"],
            "DIA": ["DOW", "DJI", "DJIA"],
            "VIX": ["VIX", "VOLATILITY"]
        }
        return terms_map.get(underlying, [underlying])
    
    def _assess_confidence(self, oracle_data: Dict) -> float:
        """
        Assess confidence in Kalshi probability.
        
        Based on spread and volume.
        """
        spread_cents = oracle_data.get("spread_cents", 100)
        volume = oracle_data.get("volume_24h", 0)
        
        # Confidence decreases with spread, increases with volume
        spread_score = max(0, 1.0 - (spread_cents / 50.0))  # Bad if >50 cent spread
        volume_score = min(1.0, volume / 1000.0)  # Good if >1000 volume
        
        confidence = 0.6 * spread_score + 0.4 * volume_score
        return max(0.3, min(1.0, confidence))  # Clamp to [0.3, 1.0]


class FallbackPEventSource(PEventSource):
    """
    Fallback p_event source using hardcoded conservative estimates.
    
    Used for smoke tests or when real data is unavailable.
    """
    
    def __init__(self, default_p_event: float = 0.30):
        """
        Initialize fallback source.
        
        Args:
            default_p_event: Default probability estimate
        """
        self.default_p_event = default_p_event
    
    def get_p_event(
        self,
        event_definition: Dict[str, Any],
        **kwargs
    ) -> PEventResult:
        """Get fallback probability."""
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Use default estimate
        p_fallback = kwargs.get("fallback_value", self.default_p_event)
        
        logger.warning(f"Fallback source: p_event=None, fallback estimate in metadata = {p_fallback:.2%}")
        
        return PEventResult(
            p_event=None,  # CRITICAL: p_event must be None when using fallback
            source="fallback",
            confidence=0.0,  # Zero confidence - not authoritative
            timestamp=timestamp,
            metadata={
                "default_value": self.default_p_event,
                "p_external_fallback": p_fallback,
                "event_definition": event_definition,
                "reason": "Fallback source - no real market data"
            },
            fallback_used=True,
            warnings=["FALLBACK_USED", "Using fallback probability estimate - not based on real market data"]
        )


class OptionsImpliedPEventSource(PEventSource):
    """
    Options-implied p_event source.
    
    Derives tail probability directly from the option surface using
    various methods (Breeden-Litzenberger, OTM put prices, etc.).
    """
    
    def __init__(self, options_data=None):
        """
        Initialize options-implied source.
        
        Args:
            options_data: Optional pre-loaded options chain data
        """
        self.options_data = options_data
    
    def get_p_event(
        self,
        event_definition: Dict[str, Any],
        **kwargs
    ) -> PEventResult:
        """
        Derive probability from option prices.
        
        Uses OTM put prices to estimate tail probability.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Extract event parameters
        underlying = event_definition.get("underlying")
        threshold = event_definition.get("threshold")  # Strike or barrier level
        direction = event_definition.get("direction", "below")
        
        if self.options_data is None:
            raise ValueError("Options data required for options_implied source")
        
        # Get spot price
        S0 = kwargs.get("spot_price") or self.options_data.get("spot_price")
        if S0 is None:
            raise ValueError("Spot price required for options_implied probability")
        
        # If no explicit threshold, assume event is move below some moneyness
        if threshold is None:
            # Default: probability of 15% downside move
            threshold = S0 * 0.85
        
        # Calculate implied tail probability
        p_tail = self._calculate_tail_probability(
            threshold=threshold,
            direction=direction,
            S0=S0,
            **kwargs
        )
        
        return PEventResult(
            p_event=p_tail,
            source="options_implied",
            confidence=0.7,  # Moderate-high confidence
            timestamp=timestamp,
            metadata={
                "method": "otm_put_prices",
                "underlying": underlying,
                "spot_price": S0,
                "threshold": threshold,
                "direction": direction,
                "event_definition": event_definition
            },
            fallback_used=False,
            warnings=[]
        )
    
    def _calculate_tail_probability(
        self,
        threshold: float,
        direction: str,
        S0: float,
        **kwargs
    ) -> float:
        """
        Calculate tail probability from option prices.
        
        Uses simplified Breeden-Litzenberger or OTM put butterfly spreads.
        """
        # Get ATM IV as baseline
        atm_iv = kwargs.get("atm_iv", 0.15)
        dte = kwargs.get("days_to_expiry", 45)
        T = dte / 365.0
        
        # Simple Black-Scholes based estimate for now
        # TODO: Enhance with actual option chain data
        import numpy as np
        from scipy.stats import norm
        
        r = 0.05  # Risk-free rate
        
        # Calculate probability using lognormal assumption
        if direction == "below":
            # P(S_T < K) using Black-Scholes
            d2 = (np.log(S0 / threshold) + (r - 0.5 * atm_iv**2) * T) / (atm_iv * np.sqrt(T))
            p_event = norm.cdf(-d2)
        else:
            # P(S_T > K)
            d2 = (np.log(S0 / threshold) + (r - 0.5 * atm_iv**2) * T) / (atm_iv * np.sqrt(T))
            p_event = norm.cdf(d2)
        
        return float(np.clip(p_event, 0.01, 0.99))


class KalshiOrFallbackPEventSource(PEventSource):
    """
    Kalshi-or-fallback p_event source.
    
    Tries Kalshi first, falls back to conservative estimate if unavailable.
    Logs warnings but does not fail.
    """
    
    def __init__(self, client, fallback_p_event: float = 0.30, allow_proxy: bool = False):
        """
        Initialize Kalshi-or-fallback source.
        
        Args:
            client: KalshiClient instance
            fallback_p_event: Fallback probability if Kalshi unavailable
            allow_proxy: Whether to allow proxy probabilities (default: False)
        """
        self.kalshi_source = KalshiPEventSource(client, allow_proxy=allow_proxy)
        self.fallback_source = FallbackPEventSource(fallback_p_event)
    
    def get_p_event(
        self,
        event_definition: Dict[str, Any],
        **kwargs
    ) -> PEventResult:
        """Try Kalshi, fallback if unavailable."""
        warnings = []
        
        try:
            # Try Kalshi first
            result = self.kalshi_source.get_p_event(event_definition, **kwargs)
            
            # If Kalshi succeeded (even with None for no exact match), return it
            if result.p_event is not None:
                logger.info(f"Kalshi source succeeded: p_event={result.p_event:.2%}")
            else:
                logger.info(f"Kalshi searched but no exact match (p_event=None, proxy metadata may be present)")
            
            return result
            
        except Exception as e:
            # Kalshi failed, use fallback
            logger.warning(f"Kalshi source failed: {e}")
            logger.warning("Falling back to conservative probability estimate")
            warnings.append(f"Kalshi unavailable: {str(e)}")
            
            result = self.fallback_source.get_p_event(event_definition, **kwargs)
            result.warnings.extend(warnings)
            return result


def compute_p_implied(
    event_def: Dict[str, Any],
    spot: float,
    dte: int,
    atm_iv: float,
    strikes: list[float],
    r: float = 0.0
) -> PEventResult:
    """
    Compute options-implied probability for an event.
    
    Args:
        event_def: Event definition (must be index_drawdown with index=SPX)
        spot: Current spot price (SPX level)
        dte: Days to expiration
        atm_iv: ATM implied volatility (used as baseline)
        strikes: Available strikes in the option chain
        r: Risk-free rate (default 0.0)
        
    Returns:
        PEventResult with p_implied or None if computation fails
    """
    from ..options.implied_prob import options_implied_p_event
    from ..options.event_to_strike import pick_implied_strike_for_event
    
    timestamp = datetime.now(timezone.utc).isoformat()
    warnings = []
    
    try:
        # Validate event definition
        if event_def.get("type") != "index_drawdown":
            return PEventResult(
                p_event=None,
                source="options_implied",
                confidence=0.0,
                timestamp=timestamp,
                metadata={"error": "Unsupported event type"},
                fallback_used=False,
                warnings=["P_IMPLIED_FAILED: Unsupported event type"]
            )
        
        if event_def.get("index") != "SPX":
            return PEventResult(
                p_event=None,
                source="options_implied",
                confidence=0.0,
                timestamp=timestamp,
                metadata={"error": "Unsupported index"},
                fallback_used=False,
                warnings=["P_IMPLIED_FAILED: Unsupported index"]
            )
        
        # Pick the strike
        strike_info = pick_implied_strike_for_event(
            event_def=event_def,
            spot=spot,
            available_strikes=strikes
        )
        
        picked_strike = strike_info["picked_strike"]
        target_level = strike_info["target_level"]
        strike_error_pct = strike_info["strike_error_pct"]
        
        # Note that we're using ATM IV
        warnings.append("IV_APPROX_ATM")
        
        # Compute implied probability
        p_implied = options_implied_p_event(
            spot=spot,
            strike=picked_strike,
            iv=atm_iv,
            dte=dte,
            r=r
        )
        
        return PEventResult(
            p_event=p_implied,
            source="options_implied",
            confidence=0.70,  # Fixed confidence
            timestamp=timestamp,
            metadata={
                "target_level": target_level,
                "picked_strike": picked_strike,
                "strike_error_pct": strike_error_pct,
                "iv_used": atm_iv,
                "dte": dte,
                "spot": spot,
                "r": r
            },
            fallback_used=False,
            warnings=warnings
        )
        
    except Exception as e:
        logger.error(f"Failed to compute p_implied: {e}")
        return PEventResult(
            p_event=None,
            source="options_implied",
            confidence=0.0,
            timestamp=timestamp,
            metadata={"error": str(e)},
            fallback_used=False,
            warnings=["P_IMPLIED_FAILED"]
        )


def create_p_event_source(
    mode: str,
    kalshi_client=None,
    fallback_p_event: float = 0.30,
    options_data=None
) -> PEventSource:
    """
    Factory function to create p_event source based on mode.
    
    Args:
        mode: One of "kalshi", "kalshi_or_fallback", "fallback_only", "options_implied"
        kalshi_client: KalshiClient instance (required for kalshi modes)
        fallback_p_event: Default probability for fallback
        options_data: Options chain data (required for options_implied)
        
    Returns:
        PEventSource instance
        
    Raises:
        ValueError: If mode is invalid or required parameters missing
    """
    mode_enum = PEventSourceType(mode.lower())
    
    if mode_enum == PEventSourceType.KALSHI:
        if kalshi_client is None:
            raise ValueError("kalshi_client required for 'kalshi' mode")
        return KalshiPEventSource(kalshi_client)
    
    elif mode_enum == PEventSourceType.KALSHI_OR_FALLBACK:
        if kalshi_client is None:
            raise ValueError("kalshi_client required for 'kalshi_or_fallback' mode")
        return KalshiOrFallbackPEventSource(kalshi_client, fallback_p_event)
    
    elif mode_enum == PEventSourceType.FALLBACK_ONLY:
        return FallbackPEventSource(fallback_p_event)
    
    elif mode_enum == PEventSourceType.OPTIONS_IMPLIED:
        return OptionsImpliedPEventSource(options_data)
    
    elif mode_enum == PEventSourceType.ENSEMBLE:
        raise NotImplementedError("Ensemble mode not yet implemented")
    
    else:
        raise ValueError(f"Unknown p_event source mode: {mode}")
