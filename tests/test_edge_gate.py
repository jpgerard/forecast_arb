"""
Tests for edge and confidence gating.
"""

import pytest
from datetime import datetime, timezone

from forecast_arb.gating.edge_gate import gate, GateDecision
from forecast_arb.oracle.p_event_source import PEventResult


class TestEdgeGate:
    """Test edge and confidence gating logic."""
    
    def create_p_result(self, p_event, source="test", confidence=0.8):
        """Helper to create PEventResult."""
        return PEventResult(
            p_event=p_event,
            source=source,
            confidence=confidence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata={"test": True},
            fallback_used=False,
            warnings=[]
        )
    
    def test_no_p_implied(self):
        """Test NO_TRADE when p_implied is None."""
        p_external = self.create_p_result(0.40, confidence=0.75)
        p_implied = self.create_p_result(None)
        
        decision = gate(p_external, p_implied)
        
        assert decision.decision == "NO_TRADE"
        assert decision.reason == "NO_P_IMPLIED"
        assert decision.edge is None
        assert decision.p_implied is None
        assert decision.confidence == 0.0  # Must be 0.0 when p_implied unavailable
        assert decision.confidence_external == 0.75
        assert decision.confidence_implied is None  # None because p_implied.p_event is None
        assert decision.metadata["confidence_source"] == "implied"
        assert decision.metadata["implied_available"] is False
    
    def test_no_p_external(self):
        """Test NO_TRADE when p_external is None."""
        p_external = self.create_p_result(None)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied)
        
        assert decision.decision == "NO_TRADE"
        assert decision.reason == "NO_P_EXTERNAL"
        assert decision.edge is None
        assert decision.p_external is None
    
    def test_low_confidence(self):
        """Test NO_TRADE when confidence too low."""
        p_external = self.create_p_result(0.40, confidence=0.50)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied, min_confidence=0.60)
        
        assert decision.decision == "NO_TRADE"
        assert decision.reason == "LOW_CONFIDENCE"
        assert abs(decision.edge - 0.15) < 1e-10
        assert decision.confidence == 0.50
    
    def test_edge_too_small(self):
        """Test NO_TRADE when edge below threshold."""
        p_external = self.create_p_result(0.28, confidence=0.80)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied, min_edge=0.05)
        
        assert decision.decision == "NO_TRADE"
        assert decision.reason == "INSUFFICIENT_EDGE"
        assert abs(decision.edge - 0.03) < 1e-10
        assert decision.confidence == 0.80  # min(0.80, 0.80)
    
    def test_negative_edge(self):
        """Test NO_TRADE when edge is negative."""
        p_external = self.create_p_result(0.20, confidence=0.80)
        p_implied = self.create_p_result(0.30)
        
        decision = gate(p_external, p_implied, min_edge=0.05)
        
        assert decision.decision == "NO_TRADE"
        assert decision.reason == "INSUFFICIENT_EDGE"
        assert abs(decision.edge - (-0.10)) < 1e-10
    
    def test_pass_all_gates(self):
        """Test PASS when all gates pass."""
        p_external = self.create_p_result(0.40, confidence=0.80)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied, min_edge=0.05, min_confidence=0.60)
        
        assert decision.decision == "PASS"
        assert decision.reason == "PASSED_GATES"
        assert abs(decision.edge - 0.15) < 1e-10
        assert decision.p_external == 0.40
        assert decision.p_implied == 0.25
        assert decision.confidence == 0.80  # min(0.80, 0.80)
    
    def test_edge_exactly_at_threshold(self):
        """Test PASS when edge just above threshold."""
        # Use 0.051 to avoid floating point boundary issues
        p_external = self.create_p_result(0.301, confidence=0.80)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied, min_edge=0.05, min_confidence=0.60)
        
        assert decision.decision == "PASS"
        assert abs(decision.edge - 0.051) < 1e-10
    
    def test_confidence_exactly_at_threshold(self):
        """Test PASS when confidence exactly at threshold."""
        p_external = self.create_p_result(0.40, confidence=0.60)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied, min_edge=0.05, min_confidence=0.60)
        
        assert decision.decision == "PASS"
        assert decision.confidence == 0.60  # min(0.60, 0.80)
    
    def test_metadata_combined(self):
        """Test that metadata from both sources is combined."""
        p_external = self.create_p_result(0.40)
        p_external.metadata = {"source": "kalshi", "market": "TEST"}
        
        p_implied = self.create_p_result(0.25)
        p_implied.metadata = {"strike": 4500, "iv": 0.15}
        
        decision = gate(p_external, p_implied)
        
        assert "p_external_metadata" in decision.metadata
        assert "p_implied_metadata" in decision.metadata
        assert decision.metadata["p_external_metadata"]["source"] == "kalshi"
        assert decision.metadata["p_implied_metadata"]["strike"] == 4500
    
    def test_to_dict_serialization(self):
        """Test GateDecision can be serialized to dict."""
        p_external = self.create_p_result(0.40, confidence=0.80)
        p_implied = self.create_p_result(0.25)
        
        decision = gate(p_external, p_implied)
        
        result_dict = decision.to_dict()
        
        assert isinstance(result_dict, dict)
        assert result_dict["decision"] == "PASS"
        assert result_dict["reason"] == "PASSED_GATES"
        assert abs(result_dict["edge"] - 0.15) < 1e-10
        assert result_dict["p_external"] == 0.40
        assert result_dict["p_implied"] == 0.25
        assert result_dict["confidence_gate"] == 0.80
        assert "metadata" in result_dict
    
    def test_gate_order(self):
        """Test that gates are evaluated in correct order."""
        # If both p_implied and p_external are None, p_implied check comes first
        decision = gate(None, None)
        assert decision.reason == "NO_P_IMPLIED"
        
        # If only p_external is None, that comes second
        p_implied = self.create_p_result(0.25)
        decision = gate(None, p_implied)
        assert decision.reason == "NO_P_EXTERNAL"
    
    def test_custom_thresholds(self):
        """Test with custom min_edge and min_confidence thresholds."""
        p_external = self.create_p_result(0.35, confidence=0.70)
        p_implied = self.create_p_result(0.25)
        
        # Should pass with default thresholds
        decision1 = gate(p_external, p_implied, min_edge=0.05, min_confidence=0.60)
        assert decision1.decision == "PASS"
        
        # Should fail with higher edge threshold
        decision2 = gate(p_external, p_implied, min_edge=0.15, min_confidence=0.60)
        assert decision2.decision == "NO_TRADE"
        assert decision2.reason == "INSUFFICIENT_EDGE"
        
        # Should fail with higher confidence threshold
        decision3 = gate(p_external, p_implied, min_edge=0.05, min_confidence=0.80)
        assert decision3.decision == "NO_TRADE"
        assert decision3.reason == "LOW_CONFIDENCE"
