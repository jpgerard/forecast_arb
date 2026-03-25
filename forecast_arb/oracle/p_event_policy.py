"""
P-Event Policy: Single source of truth for p_external classification.

This module enforces the critical safety invariant:
    "Non-exact Kalshi matches cannot authorize trades"

A p_event is authoritative (can be used as p_external) ONLY when:
- Source is Kalshi AND is_exact == True
- OR explicitly configured to allow fallback (dev-only)

Proxy probabilities are NEVER authoritative; they exist in metadata for review only.
"""

import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass

from .p_event_source import PEventResult


logger = logging.getLogger(__name__)


@dataclass
class PExternalClassification:
    """
    Classification result for p_external.
    
    This is the single source of truth for determining what p_external value
    (if any) should be used for trade authorization.
    """
    # Authoritative p_external value - None if not authoritative
    p_external_value: Optional[float]
    
    # Confidence in p_external (0.0 if not authoritative)
    p_external_confidence: float
    
    # Source label
    p_external_source: str
    
    # Whether this p_external is authoritative (can authorize trades)
    p_external_is_authoritative: bool
    
    # Metadata for review (includes proxy, fallback, warnings)
    p_external_metadata: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "p_external_value": self.p_external_value,
            "p_external_confidence": self.p_external_confidence,
            "p_external_source": self.p_external_source,
            "p_external_is_authoritative": self.p_external_is_authoritative,
            "p_external_metadata": self.p_external_metadata
        }


def classify_external(
    result: PEventResult,
    mode: str,
    fallback_p: Optional[float] = None,
    allow_fallback_authorization: bool = False
) -> PExternalClassification:
    """
    Classify a PEventResult to determine p_external authorization.
    
    SAFETY INVARIANT ENFORCEMENT:
    - Kalshi with is_exact=True → authoritative, use p_event
    - Kalshi with is_exact=False → NOT authoritative, p_external_value=None
    - Fallback source → NOT authoritative by default (unless allow_fallback_authorization=True)
    - Proxy probabilities → ALWAYS in metadata only, NEVER authoritative
    
    Args:
        result: PEventResult from p_event source
        mode: Mode string ("kalshi", "kalshi-auto", "fallback")
        fallback_p: Fallback probability value (if applicable)
        allow_fallback_authorization: Whether to allow fallback to authorize trades (dev-only)
        
    Returns:
        PExternalClassification with authoritative status and metadata
    """
    metadata = dict(result.metadata) if result.metadata else {}
    metadata["source"] = result.source
    
    # Determine if this is an exact Kalshi match
    is_exact_kalshi = (
        result.source == "kalshi" and 
        result.p_event is not None
    )
    
    # Check for proxy in metadata
    has_proxy = "p_external_proxy" in metadata
    
    # Check for fallback in metadata
    has_fallback = "p_external_fallback" in metadata or result.source == "fallback"
    
    # CLASSIFICATION LOGIC
    
    if is_exact_kalshi:
        # EXACT KALSHI MATCH: Authoritative
        logger.info(
            f"P_EVENT_CLASSIFICATION: source=kalshi exact=YES authoritative=YES "
            f"value={result.p_event:.3f} conf={result.confidence:.2f} proxy_present={'YES' if has_proxy else 'NO'}"
        )
        
        return PExternalClassification(
            p_external_value=result.p_event,
            p_external_confidence=result.confidence,
            p_external_source="kalshi",
            p_external_is_authoritative=True,
            p_external_metadata=metadata
        )
    
    elif result.source == "kalshi" and not is_exact_kalshi:
        # KALSHI NO EXACT MATCH: Not authoritative
        # This includes proxy scenarios
        
        # Add fallback to metadata if provided
        if fallback_p is not None:
            metadata["p_external_fallback"] = fallback_p
        
        logger.warning(
            f"P_EVENT_CLASSIFICATION: source=kalshi exact=NO authoritative=NO "
            f"value=None conf=0.00 proxy_present={'YES' if has_proxy else 'NO'}"
        )
        
        if has_proxy:
            logger.warning(
                f"  PROXY DETECTED: proxy_value={metadata['p_external_proxy']:.3f} "
                f"proxy_method={metadata.get('proxy_method', 'N/A')} "
                f"proxy_conf={metadata.get('proxy_confidence', 0.0):.2f}"
            )
            logger.warning("  ⚠️  POLICY: Proxy NOT authoritative, p_external_value=None")
        
        return PExternalClassification(
            p_external_value=None,
            p_external_confidence=0.0,
            p_external_source="kalshi",
            p_external_is_authoritative=False,
            p_external_metadata=metadata
        )
    
    elif result.source == "fallback":
        # FALLBACK SOURCE: Not authoritative by default
        
        # Extract fallback value from metadata
        fallback_value = metadata.get("p_external_fallback") or fallback_p
        
        if allow_fallback_authorization:
            # DEV-ONLY: Fallback can authorize
            logger.warning(
                f"P_EVENT_CLASSIFICATION: source=fallback exact=NO authoritative=YES(DEV_ONLY) "
                f"value={fallback_value:.3f} conf=0.00 proxy_present=NO"
            )
            logger.warning("  ⚠️  DEV MODE: Fallback authorized (--allow-fallback-trade enabled)")
            
            return PExternalClassification(
                p_external_value=fallback_value,
                p_external_confidence=0.0,  # Still 0 confidence even if authorized
                p_external_source="fallback",
                p_external_is_authoritative=True,  # Dev override
                p_external_metadata=metadata
            )
        else:
            # NORMAL MODE: Fallback not authoritative
            logger.warning(
                f"P_EVENT_CLASSIFICATION: source=fallback exact=NO authoritative=NO "
                f"value=None conf=0.00 proxy_present=NO"
            )
            logger.warning(f"  ⚠️  POLICY: Fallback NOT authoritative (value={fallback_value:.3f} in metadata only)")
            
            return PExternalClassification(
                p_external_value=None,
                p_external_confidence=0.0,
                p_external_source="fallback",
                p_external_is_authoritative=False,
                p_external_metadata=metadata
            )
    
    else:
        # UNKNOWN SOURCE: Not authoritative
        logger.warning(
            f"P_EVENT_CLASSIFICATION: source={result.source} exact=NO authoritative=NO "
            f"value=None conf=0.00 proxy_present=NO"
        )
        
        return PExternalClassification(
            p_external_value=None,
            p_external_confidence=0.0,
            p_external_source=result.source,
            p_external_is_authoritative=False,
            p_external_metadata=metadata
        )


def verify_invariants(classification: PExternalClassification) -> None:
    """
    Verify safety invariants on a classification.
    
    This function contains hard assertions that would have caught the original
    promotion bug. Call this after classification to ensure invariants hold.
    
    INVARIANTS:
    1. If source is "kalshi" and p_external_value is not None, must be authoritative
    2. If not authoritative, p_external_value must be None
    3. If proxy present in metadata, p_external_value must be None (unless exact match also present)
    
    Raises:
        AssertionError: If any invariant is violated
    """
    source = classification.p_external_source
    value = classification.p_external_value
    is_auth = classification.p_external_is_authoritative
    metadata = classification.p_external_metadata
    
    # INVARIANT 1: Kalshi with value must be authoritative
    if source == "kalshi" and value is not None:
        assert is_auth, (
            f"INVARIANT VIOLATION: source=kalshi with p_external_value={value} "
            f"but is_authoritative={is_auth}"
        )
    
    # INVARIANT 2: Not authoritative means value must be None
    if not is_auth:
        assert value is None, (
            f"INVARIANT VIOLATION: is_authoritative=False but p_external_value={value}"
        )
    
    # INVARIANT 3: Proxy present without exact match means value must be None
    has_proxy = "p_external_proxy" in metadata
    if has_proxy and not is_auth:
        assert value is None, (
            f"INVARIANT VIOLATION: Proxy present but p_external_value={value} "
            f"(proxy should never be promoted to p_external_value)"
        )
    
    logger.debug(f"✓ Safety invariants verified for source={source}, is_authoritative={is_auth}")
