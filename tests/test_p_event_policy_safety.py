"""
Test P-Event Policy Safety Invariants

These tests verify the critical safety invariant:
    "Non-exact Kalshi matches cannot authorize trades"

These tests would have caught the original proxy probability promotion bug.
"""

import pytest
from datetime import datetime, timezone

from forecast_arb.oracle.p_event_source import PEventResult
from forecast_arb.oracle.p_event_policy import (
    classify_external,
    verify_invariants,
    PExternalClassification
)


class TestProxyPromotionPrevention:
    """
    REGRESSION TEST: Prevent proxy probability from being promoted to p_external.
    
    This is the bug that was fixed - proxy probabilities were being used as
    authoritative p_external values. These tests ensure it cannot happen again.
    """
    
    def test_proxy_not_authoritative(self):
        """
        D1) Safety regression test for promotion bug.
        
        When Kalshi returns a proxy (no exact match):
        - p_external_value must be None
        - p_external_confidence must be 0.0
        - proxy must be in metadata only
        - is_authoritative must be False
        """
        # Mock a PEventResult with proxy (the bug scenario)
        result = PEventResult(
            p_event=None,  # No exact match
            source="kalshi",
            confidence=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={
                "p_external_proxy": 0.12,  # Proxy available
                "proxy_method": "hazard_rate_yearly_min",
                "proxy_series": "KXSPY-YEAR",
                "proxy_confidence": 0.30,  # LOW
                "proxy_market_ticker": "KXSPY-25-T4000"
            },
            fallback_used=False,
            warnings=["PROXY_USED", "HORIZON_MISMATCH"]
        )
        
        # Classify
        classification = classify_external(
            result=result,
            mode="kalshi-auto",
            fallback_p=0.30,
            allow_fallback_authorization=False
        )
        
        # ASSERTIONS: The original bug would have failed these
        assert classification.p_external_value is None, \
            "BUG: Proxy was promoted to p_external_value"
        
        assert classification.p_external_confidence == 0.0, \
            "BUG: Non-zero confidence with no authoritative value"
        
        assert not classification.p_external_is_authoritative, \
            "BUG: Proxy marked as authoritative"
        
        # Proxy should be in metadata
        assert "p_external_proxy" in classification.p_external_metadata
        assert classification.p_external_metadata["p_external_proxy"] == 0.12
        
        # Verify invariants
        verify_invariants(classification)
    
    def test_review_pack_labels_proxy_correctly(self):
        """
        Verify that proxy is labeled as "NOT authoritative" in review pack.
        
        This ensures humans reading the review pack cannot be misled.
        """
        result = PEventResult(
            p_event=None,
            source="kalshi",
            confidence=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={
                "p_external_proxy": 0.15,
                "proxy_method": "hazard_rate",
                "proxy_series": "KXSPY-YEAR",
                "proxy_confidence": 0.25,
                "proxy_market_ticker": "KXSPY-25-T4200"
            },
            fallback_used=False
        )
        
        classification = classify_external(
            result=result,
            mode="kalshi-auto",
            fallback_p=0.30
        )
        
        # Check metadata contains necessary info for review pack rendering
        assert not classification.p_external_is_authoritative
        assert "p_external_proxy" in classification.p_external_metadata
        assert classification.p_external_metadata["proxy_method"] is not None


class TestExactKalshiMatch:
    """
    D2) Test exact Kalshi path - the ONLY scenario where p_external is authoritative.
    """
    
    def test_exact_match_is_authoritative(self):
        """
        When Kalshi returns an exact match:
        - p_external_value should be set
        - confidence should be preserved
        - is_authoritative should be True
        """
        result = PEventResult(
            p_event=0.02,  # Exact match value
            source="kalshi",
            confidence=0.75,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={
                "market_ticker": "INXD-26FEB28-B4500",
                "source_series": "INXD",
                "bid": 0.01,
                "ask": 0.03
            },
            fallback_used=False
        )
        
        classification = classify_external(
            result=result,
            mode="kalshi",
            fallback_p=None
        )
        
        # ASSERTIONS: Exact match should be authoritative
        assert classification.p_external_value == 0.02
        assert classification.p_external_confidence == 0.75
        assert classification.p_external_is_authoritative
        assert classification.p_external_source == "kalshi"
        
        # Verify invariants
        verify_invariants(classification)
    
    def test_exact_match_preserves_confidence(self):
        """Verify that confidence from exact Kalshi match is preserved."""
        result = PEventResult(
            p_event=0.05,
            source="kalshi",
            confidence=0.82,  # High confidence
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"market_ticker": "TEST-TICKER"},
            fallback_used=False
        )
        
        classification = classify_external(result, mode="kalshi")
        
        assert classification.p_external_confidence == 0.82, \
            "Confidence should be preserved from exact match"


class TestFallbackPath:
    """
    D3) Test fallback path - should NOT be authoritative by default.
    """
    
    def test_fallback_not_authoritative_by_default(self):
        """
        Fallback source should NOT be authoritative unless explicitly allowed.
        
        This is a safety feature - fallback is a conservative estimate,
        not real market data.
        """
        result = PEventResult(
            p_event=None,  # Fallback doesn't set p_event
            source="fallback",
            confidence=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={
                "default_value": 0.30,
                "p_external_fallback": 0.30,
                "reason": "No Kalshi match"
            },
            fallback_used=True,
            warnings=["FALLBACK_USED"]
        )
        
        classification = classify_external(
            result=result,
            mode="fallback",
            fallback_p=0.30,
            allow_fallback_authorization=False  # Default
        )
        
        # ASSERTIONS: Fallback should NOT be authoritative
        assert classification.p_external_value is None
        assert classification.p_external_confidence == 0.0
        assert not classification.p_external_is_authoritative
        assert classification.p_external_source == "fallback"
        
        # Fallback value should be in metadata
        assert "p_external_fallback" in classification.p_external_metadata
        
        # Verify invariants
        verify_invariants(classification)
    
    def test_fallback_can_be_authorized_in_dev_mode(self):
        """
        In dev mode with explicit flag, fallback can be authorized.
        
        This is for testing purposes only - not for production.
        """
        result = PEventResult(
            p_event=None,
            source="fallback",
            confidence=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"p_external_fallback": 0.28},
            fallback_used=True
        )
        
        classification = classify_external(
            result=result,
            mode="fallback",
            fallback_p=0.28,
            allow_fallback_authorization=True  # DEV-ONLY override
        )
        
        # In dev mode, fallback CAN be authoritative
        assert classification.p_external_value == 0.28
        assert classification.p_external_is_authoritative  # Dev override
        assert classification.p_external_source == "fallback"
        
        # Confidence still 0 even when authorized (it's just a fallback)
        assert classification.p_external_confidence == 0.0
        
        # Verify invariants (should still pass even with dev override)
        verify_invariants(classification)
    
    def test_review_pack_labels_fallback_correctly(self):
        """Verify fallback is clearly labeled in review artifacts."""
        result = PEventResult(
            p_event=None,
            source="fallback",
            confidence=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"p_external_fallback": 0.30},
            fallback_used=True
        )
        
        classification = classify_external(
            result=result,
            mode="fallback",
            fallback_p=0.30
        )
        
        # Check labeling
        assert classification.p_external_source == "fallback"
        assert not classification.p_external_is_authoritative
        assert "p_external_fallback" in classification.p_external_metadata


class TestInvariantEnforcement:
    """
    Test that invariant verification catches violations.
    
    These are the hard assertions that would have caught the original bug.
    """
    
    def test_invariant_1_kalshi_with_value_must_be_authoritative(self):
        """
        INVARIANT 1: If source is kalshi and value is set, must be authoritative.
        """
        # Create a classification that violates invariant 1
        bad_classification = PExternalClassification(
            p_external_value=0.10,  # Value set
            p_external_confidence=0.50,
            p_external_source="kalshi",
            p_external_is_authoritative=False,  # VIOLATION: Should be True
            p_external_metadata={}
        )
        
        # Should raise assertion error
        with pytest.raises(AssertionError, match="INVARIANT VIOLATION"):
            verify_invariants(bad_classification)
    
    def test_invariant_2_not_authoritative_means_value_none(self):
        """
        INVARIANT 2: If not authoritative, value must be None.
        """
        bad_classification = PExternalClassification(
            p_external_value=0.12,  # VIOLATION: Should be None
            p_external_confidence=0.0,
            p_external_source="kalshi",
            p_external_is_authoritative=False,
            p_external_metadata={}
        )
        
        with pytest.raises(AssertionError, match="INVARIANT VIOLATION"):
            verify_invariants(bad_classification)
    
    def test_invariant_3_proxy_without_auth_means_value_none(self):
        """
        INVARIANT 3: Proxy present without authorization means value must be None.
        
        Note: This actually triggers invariant 2 first (not authoritative -> value must be None),
        which is also correct. The key point is that invariant verification catches the violation.
        """
        bad_classification = PExternalClassification(
            p_external_value=0.12,  # VIOLATION: Proxy should not be promoted
            p_external_confidence=0.0,
            p_external_source="kalshi",
            p_external_is_authoritative=False,
            p_external_metadata={"p_external_proxy": 0.12}  # Proxy present
        )
        
        # This will trigger invariant 2 first (not authoritative -> value None)
        # which is correct - the important thing is that the violation is caught
        with pytest.raises(AssertionError, match="INVARIANT VIOLATION"):
            verify_invariants(bad_classification)
    
    def test_valid_classification_passes_invariants(self):
        """Valid classifications should pass all invariants."""
        # Valid: Exact Kalshi match
        valid_exact = PExternalClassification(
            p_external_value=0.05,
            p_external_confidence=0.70,
            p_external_source="kalshi",
            p_external_is_authoritative=True,
            p_external_metadata={"market_ticker": "TEST"}
        )
        verify_invariants(valid_exact)  # Should not raise
        
        # Valid: No authorization, no value
        valid_no_auth = PExternalClassification(
            p_external_value=None,
            p_external_confidence=0.0,
            p_external_source="kalshi",
            p_external_is_authoritative=False,
            p_external_metadata={"p_external_proxy": 0.12}
        )
        verify_invariants(valid_no_auth)  # Should not raise


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_unknown_source(self):
        """Unknown source should default to not authoritative."""
        result = PEventResult(
            p_event=0.15,
            source="unknown_source",
            confidence=0.50,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={},
            fallback_used=False
        )
        
        classification = classify_external(result, mode="unknown")
        
        assert not classification.p_external_is_authoritative
        assert classification.p_external_value is None
        assert classification.p_external_source == "unknown_source"
    
    def test_empty_metadata(self):
        """Classification should handle empty metadata gracefully."""
        result = PEventResult(
            p_event=None,
            source="kalshi",
            confidence=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={},  # Empty
            fallback_used=False
        )
        
        classification = classify_external(result, mode="kalshi")
        
        assert not classification.p_external_is_authoritative
        assert classification.p_external_value is None
        assert isinstance(classification.p_external_metadata, dict)


def test_classification_to_dict():
    """Test serialization to dict."""
    classification = PExternalClassification(
        p_external_value=0.05,
        p_external_confidence=0.75,
        p_external_source="kalshi",
        p_external_is_authoritative=True,
        p_external_metadata={"test": "data"}
    )
    
    result_dict = classification.to_dict()
    
    assert result_dict["p_external_value"] == 0.05
    assert result_dict["p_external_confidence"] == 0.75
    assert result_dict["p_external_source"] == "kalshi"
    assert result_dict["p_external_is_authoritative"] is True
    assert result_dict["p_external_metadata"]["test"] == "data"
