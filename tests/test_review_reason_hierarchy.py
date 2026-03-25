"""
Tests for review output decision reason hierarchy.

Validates that the review formatter correctly displays:
1. External source policy blocking
2. Edge gate blocking  
3. Structuring failure (no candidates)
4. Correct confidence composition in gate decisions
"""

import pytest
from forecast_arb.execution.review import format_review


def test_external_source_blocked_shows_in_review():
    """
    Scenario: Edge gate passes, but external source policy blocks.
    Expected: Review shows external source blocked, NOT "No valid structures".
    """
    # Simulate: gate PASS, external blocked, no structures (skipped)
    gate_decision = {
        "decision": "PASS",
        "reason": "PASSED_GATES",
        "edge": 0.10,
        "p_external": 0.35,
        "p_implied": 0.25,
        "confidence_gate": 0.70,
        "confidence_external": 0.70,
        "confidence_implied": 0.85,
        "metadata": {}
    }
    
    review = format_review(
        run_id="test_123",
        decision="NO_TRADE",
        reason="EXTERNAL_SOURCE_BLOCKED:BLOCKED_FALLBACK",
        p_external=0.35,
        p_implied=0.25,
        edge=0.10,
        confidence=0.70,
        tickets=[],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="fallback",
        external_source_blocked=True,
        gate_decision=gate_decision
    )
    
    # Assertions
    assert "EXTERNAL SOURCE POLICY" in review
    assert "Source: fallback" in review
    assert "Allowed: False" in review
    assert "BLOCKED (FALLBACK)" in review
    
    assert "EDGE GATE" in review
    assert "Result: PASS" in review
    assert "Reason: PASSED_GATES" in review
    
    assert "STRUCTURING" in review
    assert "SKIPPED (external source policy blocked)" in review
    
    assert "FINAL DECISION" in review
    assert "Decision: NO_TRADE" in review
    assert "EXTERNAL_SOURCE_BLOCKED:BLOCKED_FALLBACK" in review
    
    # Must NOT say "No valid structures" - structuring was skipped
    assert "No valid structures" not in review
    assert "no candidates produced" not in review


def test_edge_gate_blocked_shows_in_review():
    """
    Scenario: Edge gate blocks (insufficient edge).
    Expected: Review shows edge gate blocked, structuring skipped.
    """
    gate_decision = {
        "decision": "NO_TRADE",
        "reason": "INSUFFICIENT_EDGE",
        "edge": 0.02,  # Below threshold
        "p_external": 0.27,
        "p_implied": 0.25,
        "confidence_gate": 0.70,
        "confidence_external": 0.70,
        "confidence_implied": 0.85,
        "metadata": {}
    }
    
    review = format_review(
        run_id="test_456",
        decision="NO_TRADE",
        reason="EDGE_GATE_BLOCKED:INSUFFICIENT_EDGE",
        p_external=0.27,
        p_implied=0.25,
        edge=0.02,
        confidence=0.70,
        tickets=[],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="kalshi",
        external_source_blocked=False,
        gate_decision=gate_decision
    )
    
    # Assertions
    assert "EXTERNAL SOURCE POLICY" in review
    assert "Source: kalshi" in review
    assert "Allowed: True" in review
    assert "Policy: OK" in review
    
    assert "EDGE GATE" in review
    assert "Result: NO_TRADE" in review
    assert "Reason: INSUFFICIENT_EDGE" in review
    
    assert "STRUCTURING" in review
    assert "SKIPPED (edge gate blocked)" in review
    
    assert "FINAL DECISION" in review
    assert "Decision: NO_TRADE" in review
    assert "EDGE_GATE_BLOCKED:INSUFFICIENT_EDGE" in review


def test_structuring_no_candidates_shows_in_review():
    """
    Scenario: Gate passes, external source OK, but structuring finds no candidates.
    Expected: Review shows structuring ran but produced no candidates.
    """
    gate_decision = {
        "decision": "PASS",
        "reason": "PASSED_GATES",
        "edge": 0.10,
        "p_external": 0.35,
        "p_implied": 0.25,
        "confidence_gate": 0.70,
        "confidence_external": 0.70,
        "confidence_implied": 0.85,
        "metadata": {}
    }
    
    review = format_review(
        run_id="test_789",
        decision="NO_TRADE",
        reason="STRUCTURING_NO_CANDIDATES",
        p_external=0.35,
        p_implied=0.25,
        edge=0.10,
        confidence=0.70,
        tickets=[],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="kalshi",
        external_source_blocked=False,
        gate_decision=gate_decision
    )
    
    # Assertions
    assert "EXTERNAL SOURCE POLICY" in review
    assert "Allowed: True" in review
    
    assert "EDGE GATE" in review
    assert "Result: PASS" in review
    
    assert "STRUCTURING" in review
    assert "RAN (no candidates produced)" in review
    
    assert "FINAL DECISION" in review
    assert "Decision: NO_TRADE" in review
    assert "STRUCTURING_NO_CANDIDATES" in review


def test_confidence_composition_in_gate_decision():
    """
    Test that gate decision shows all three confidence values correctly:
    - confidence_external
    - confidence_implied
    - confidence_gate (min of the two)
    """
    gate_decision = {
        "decision": "PASS",
        "reason": "PASSED_GATES",
        "edge": 0.10,
        "p_external": 0.35,
        "p_implied": 0.25,
        "confidence_gate": 0.70,  # min(0.70, 0.85) = 0.70
        "confidence_external": 0.70,
        "confidence_implied": 0.85,
        "metadata": {}
    }
    
    review = format_review(
        run_id="test_conf",
        decision="TRADE",
        reason="TRADE_READY",
        p_external=0.35,
        p_implied=0.25,
        edge=0.10,
        confidence=0.70,
        tickets=[],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="kalshi",
        external_source_blocked=False,
        gate_decision=gate_decision
    )
    
    # Assertions - check all three confidence values are displayed
    assert "Confidence (External): 0.70" in review
    assert "Confidence (Implied): 0.85" in review
    assert "Confidence (Gate): 0.70" in review
    
    # Market assessment should show gate confidence
    assert "Confidence:  70.0%" in review


def test_trade_ready_shows_correctly():
    """
    Scenario: Everything passes, tickets generated.
    Expected: Review shows all systems passing and tickets generated.
    """
    gate_decision = {
        "decision": "PASS",
        "reason": "PASSED_GATES",
        "edge": 0.10,
        "p_external": 0.35,
        "p_implied": 0.25,
        "confidence_gate": 0.70,
        "confidence_external": 0.70,
        "confidence_implied": 0.85,
        "metadata": {}
    }
    
    # Mock ticket
    ticket = {
        "symbol": "SPY",
        "expiry": "20260228",
        "legs": [
            {"action": "BUY", "strike": 450.0, "right": "P"},
            {"action": "SELL", "strike": 440.0, "right": "P"}
        ],
        "limit_price": 3.50,
        "quantity": 1,
        "metadata": {"ev_per_dollar": 0.15}
    }
    
    review = format_review(
        run_id="test_trade",
        decision="TRADE",
        reason="TRADE_READY",
        p_external=0.35,
        p_implied=0.25,
        edge=0.10,
        confidence=0.70,
        tickets=[ticket],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="kalshi",
        external_source_blocked=False,
        gate_decision=gate_decision
    )
    
    # Assertions
    assert "EXTERNAL SOURCE POLICY" in review
    assert "Allowed: True" in review
    
    assert "EDGE GATE" in review
    assert "Result: PASS" in review
    
    assert "STRUCTURING" in review
    assert "RAN (1 candidate(s) produced)" in review
    
    assert "FINAL DECISION" in review
    assert "Decision: TRADE" in review
    assert "TRADE_READY" in review
    
    assert "ORDER TICKETS" in review
    assert "Total Tickets: 1" in review


def test_low_confidence_gate_block():
    """
    Scenario: Gate blocks due to low confidence.
    Expected: Review shows LOW_CONFIDENCE as gate reason.
    """
    gate_decision = {
        "decision": "NO_TRADE",
        "reason": "LOW_CONFIDENCE",
        "edge": 0.10,
        "p_external": 0.35,
        "p_implied": 0.25,
        "confidence_gate": 0.40,  # Below threshold
        "confidence_external": 0.70,
        "confidence_implied": 0.40,  # Low implied confidence
        "metadata": {}
    }
    
    review = format_review(
        run_id="test_low_conf",
        decision="NO_TRADE",
        reason="EDGE_GATE_BLOCKED:LOW_CONFIDENCE",
        p_external=0.35,
        p_implied=0.25,
        edge=0.10,
        confidence=0.40,
        tickets=[],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="kalshi",
        external_source_blocked=False,
        gate_decision=gate_decision
    )
    
    # Assertions
    assert "EDGE GATE" in review
    assert "Result: NO_TRADE" in review
    assert "Reason: LOW_CONFIDENCE" in review
    assert "Confidence (Gate): 0.40" in review
    
    assert "FINAL DECISION" in review
    assert "EDGE_GATE_BLOCKED:LOW_CONFIDENCE" in review


def test_no_p_implied_gate_block():
    """
    Scenario: Gate blocks because p_implied unavailable.
    Expected: Review shows NO_P_IMPLIED as gate reason.
    """
    gate_decision = {
        "decision": "NO_TRADE",
        "reason": "NO_P_IMPLIED",
        "edge": None,
        "p_external": 0.35,
        "p_implied": None,
        "confidence_gate": 0.0,
        "confidence_external": 0.70,
        "confidence_implied": None,
        "metadata": {}
    }
    
    review = format_review(
        run_id="test_no_impl",
        decision="NO_TRADE",
        reason="EDGE_GATE_BLOCKED:NO_P_IMPLIED",
        p_external=0.35,
        p_implied=None,
        edge=None,
        confidence=0.0,
        tickets=[],
        caps={"max_orders": 3, "max_debit_total": None},
        mode="dev",
        config_hash="abc123",
        submit_requested=False,
        submit_blocked=False,
        submit_block_reason=None,
        p_external_source="kalshi",
        external_source_blocked=False,
        gate_decision=gate_decision
    )
    
    # Assertions
    assert "P(Implied):  N/A" in review
    assert "Edge:        N/A" in review
    
    assert "EDGE GATE" in review
    assert "Result: NO_TRADE" in review
    assert "Reason: NO_P_IMPLIED" in review
    
    assert "FINAL DECISION" in review
    assert "EDGE_GATE_BLOCKED:NO_P_IMPLIED" in review
