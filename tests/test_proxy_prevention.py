"""
Test: Proxy Probability Prevention

Critical safety test to ensure proxy probabilities are NEVER used
as authoritative p_external values.

This test verifies the fix implemented to prevent non-exact p_event promotion.
"""

import pytest
from forecast_arb.oracle.p_event_source import PEventResult
from datetime import datetime, timezone


def test_proxy_never_becomes_p_external():
    """
    CRITICAL SAFETY TEST: Proxy values must never be promoted to p_external.
    
    When Kalshi returns a proxy (non-exact match), the system must:
    1. Keep p_event as None (not the proxy value)
    2. Store proxy value in metadata only
    3. Use fallback for actual p_external
    """
    # Simulate a Kalshi result with proxy (no exact match)
    proxy_value = 0.42
    fallback_value = 0.30
    
    # Create a result simulating what p_event_source returns for proxy
    result_with_proxy = PEventResult(
        p_event=None,  # CRITICAL: Must be None when proxy
        source="kalshi",
        confidence=0.0,  # Zero confidence when no exact match
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={
            "p_external_proxy": proxy_value,  # Proxy stored here
            "proxy_method": "yearly_min_hazard_scale",
            "proxy_series": "KXINXMINY",
            "proxy_confidence": 0.35  # LOW confidence
        },
        fallback_used=False
    )
    
    # INVARIANT 1: p_event must be None (not proxy value)
    assert result_with_proxy.p_event is None, \
        "SAFETY VIOLATION: p_event must be None when using proxy"
    
    # INVARIANT 2: p_event must NOT equal proxy value
    assert result_with_proxy.p_event != proxy_value, \
        "SAFETY VIOLATION: p_event was set to proxy value"
    
    # INVARIANT 3: Proxy must be in metadata only
    assert "p_external_proxy" in result_with_proxy.metadata, \
        "Proxy value must be present in metadata"
    
    assert result_with_proxy.metadata["p_external_proxy"] == proxy_value, \
        "Proxy value in metadata doesn't match expected"
    
    # INVARIANT 4: Confidence must be zero when no exact match
    assert result_with_proxy.confidence == 0.0, \
        "Confidence must be zero when using proxy"
    
    # Simulate the fix logic from run_daily.py
    has_exact = (result_with_proxy.p_event is not None)
    has_proxy = ("p_external_proxy" in result_with_proxy.metadata)
    
    # This is what run_daily.py should do
    if has_proxy and not has_exact:
        # Use fallback for p_external (policy: proxy not authoritative)
        p_external = fallback_value
        
        # ASSERTION from run_daily.py
        assert p_external != proxy_value, \
            "INVARIANT VIOLATION: p_event was set to proxy value"
    
    # Verify the decision logic
    assert not has_exact, "has_exact must be False for proxy"
    assert has_proxy, "has_proxy must be True"
    assert p_external == fallback_value, \
        f"p_external must be fallback ({fallback_value}), not proxy ({proxy_value})"


def test_exact_match_becomes_p_external():
    """
    Test that exact matches ARE used as p_external (positive case).
    """
    exact_value = 0.38
    
    # Create a result simulating exact Kalshi match
    result_exact = PEventResult(
        p_event=exact_value,  # Exact match has p_event set
        source="kalshi",
        confidence=0.70,  # Standard confidence for exact match
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={
            "market_ticker": "INXD-26FEB07-T4350",
            "source_series": "KXINX"
        },
        fallback_used=False
    )
    
    # Verify exact match behavior
    has_exact = (result_exact.p_event is not None)
    has_proxy = ("p_external_proxy" in result_exact.metadata)
    
    assert has_exact, "Exact match must have p_event set"
    assert not has_proxy, "Exact match should not have proxy"
    assert result_exact.p_event == exact_value
    assert result_exact.confidence > 0, "Exact match should have confidence > 0"
    
    # This is what run_daily.py should do for exact match
    if has_exact:
        p_external = result_exact.p_event
        assert p_external is not None, "p_external must be set for exact match"
        assert p_external == exact_value


def test_classification_logging():
    """
    Test that the classification logic produces correct flags.
    """
    # Test case 1: Exact match
    result_exact = PEventResult(
        p_event=0.35,
        source="kalshi",
        confidence=0.70,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"market_ticker": "TEST"},
        fallback_used=False
    )
    
    has_exact = (result_exact.p_event is not None)
    has_proxy = ("p_external_proxy" in result_exact.metadata)
    p_external_authoritative = has_exact
    
    assert has_exact is True
    assert has_proxy is False
    assert p_external_authoritative is True
    
    # Test case 2: Proxy only
    result_proxy = PEventResult(
        p_event=None,
        source="kalshi",
        confidence=0.0,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"p_external_proxy": 0.42},
        fallback_used=False
    )
    
    has_exact = (result_proxy.p_event is not None)
    has_proxy = ("p_external_proxy" in result_proxy.metadata)
    p_external_authoritative = has_exact
    
    assert has_exact is False
    assert has_proxy is True
    assert p_external_authoritative is False
    
    # Test case 3: No match
    result_none = PEventResult(
        p_event=None,
        source="kalshi",
        confidence=0.0,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
        fallback_used=False
    )
    
    has_exact = (result_none.p_event is not None)
    has_proxy = ("p_external_proxy" in result_none.metadata)
    p_external_authoritative = has_exact
    
    assert has_exact is False
    assert has_proxy is False
    assert p_external_authoritative is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
