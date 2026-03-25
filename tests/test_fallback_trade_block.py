"""
Test fallback trade blocking policy.

Verifies that trades are blocked when using fallback p_event source
unless explicitly allowed via --allow-fallback-trade flag.
"""

import pytest
from forecast_arb.oracle.p_event_source import PEventResult
from forecast_arb.gating.edge_gate import gate
from datetime import datetime, timezone


def test_fallback_source_blocks_trade_by_default():
    """
    Test that fallback p_external source blocks trade because p_event=None.
    
    POST-FIX BEHAVIOR:
    - p_external from fallback source has p_event=None (proxy value in metadata only)
    - p_implied exists = 0.10
    - Gate blocks with NO_P_EXTERNAL
    
    Expected:
    - Gate blocks immediately (p_external.p_event is None)
    - Reason: NO_P_EXTERNAL
    - Proxy value lives in metadata only
    """
    # Create p_external from fallback source (POST-FIX: p_event=None)
    p_external = PEventResult(
        p_event=None,  # CRITICAL: Must be None for fallback/proxy
        source="fallback",
        confidence=0.0,  # Zero confidence for non-authoritative
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"source": "fallback", "p_external_fallback": 0.30},
        fallback_used=True
    )
    
    # Create p_implied (options calculation succeeded)
    p_implied = PEventResult(
        p_event=0.10,
        source="options_implied",
        confidence=0.85,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"warnings": []},
        fallback_used=False
    )
    
    # Apply gate with default thresholds
    gate_decision = gate(
        p_external=p_external,
        p_implied=p_implied,
        min_edge=0.05,  # 5% threshold
        min_confidence=0.60
    )
    
    # Verify gate blocks because p_external is None
    assert gate_decision.decision == "NO_TRADE"
    assert gate_decision.reason == "NO_P_EXTERNAL"
    assert gate_decision.edge is None
    assert gate_decision.p_external is None  # Not populated
    assert gate_decision.p_implied == pytest.approx(0.10)
    
    # Verify external source is fallback with p_event=None
    assert p_external.source == "fallback"
    assert p_external.fallback_used is True
    assert p_external.p_event is None  # The fix!
    assert p_external.metadata["p_external_fallback"] == pytest.approx(0.30)  # Proxy in metadata


def test_fallback_source_allows_trade_when_explicitly_enabled():
    """
    OBSOLETE TEST - Fallback now always returns p_event=None.
    
    POST-FIX: This test is no longer valid because fallback p_event is always None.
    Even with allow_fallback_trade=True, the gate will block with NO_P_EXTERNAL.
    The proxy value lives in metadata only and cannot authorize trades.
    
    This test is kept for documentation but should always block now.
    """
    # Create p_external from fallback source (POST-FIX: p_event=None)
    p_external = PEventResult(
        p_event=None,  # CRITICAL: Fallback always returns None
        source="fallback",
        confidence=0.0,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"source": "fallback", "p_external_fallback": 0.30},
        fallback_used=True
    )
    
    # Create p_implied
    p_implied = PEventResult(
        p_event=0.10,
        source="options_implied",
        confidence=0.85,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"warnings": []},
        fallback_used=False
    )
    
    # Apply gate
    gate_decision = gate(
        p_external=p_external,
        p_implied=p_implied,
        min_edge=0.05,
        min_confidence=0.60
    )
    
    # Gate blocks because p_external.p_event is None (even in dev mode)
    assert gate_decision.decision == "NO_TRADE"
    assert gate_decision.reason == "NO_P_EXTERNAL"
    assert gate_decision.p_external is None


def test_kalshi_source_not_blocked():
    """
    Test that Kalshi p_external source is never blocked by policy.
    
    Scenario:
    - p_external from Kalshi source = 0.30
    - p_implied exists = 0.10
    - edge = 0.20
    
    Expected:
    - Gate passes
    - External source policy allows (Kalshi is trusted)
    - Can proceed regardless of allow_fallback_trade flag
    """
    # Create p_external from Kalshi source
    p_external = PEventResult(
        p_event=0.30,
        source="kalshi",
        confidence=0.7,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"source": "kalshi", "market_ticker": "INXD-26FEB07-T4350"},
        fallback_used=False
    )
    
    # Create p_implied
    p_implied = PEventResult(
        p_event=0.10,
        source="options_implied",
        confidence=0.85,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"warnings": []},
        fallback_used=False
    )
    
    # Apply gate
    gate_decision = gate(
        p_external=p_external,
        p_implied=p_implied,
        min_edge=0.05,
        min_confidence=0.60
    )
    
    # Gate should pass
    assert gate_decision.decision == "PASS"
    assert gate_decision.reason == "PASSED_GATES"
    
    # Verify source is Kalshi (not fallback)
    assert p_external.source == "kalshi"
    assert p_external.fallback_used is False
    
    # External source policy would not block Kalshi source


def test_edge_gate_fail_takes_precedence_over_external_source_policy():
    """
    Test that edge gate failures take precedence over external source policy.
    
    Scenario:
    - p_external from fallback = 0.12
    - p_implied = 0.10
    - edge = 0.02 (below 0.05 threshold)
    - allow_fallback_trade = False
    
    Expected:
    - Gate fails with EDGE_TOO_SMALL
    - External source policy would also block, but gate blocks first
    - Reason: EDGE_GATE_BLOCKED (not EXTERNAL_SOURCE_BLOCKED)
    """
    # Create p_external from fallback
    p_external = PEventResult(
        p_event=0.12,
        source="fallback",
        confidence=0.7,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"source": "fallback"},
        fallback_used=True
    )
    
    # Create p_implied
    p_implied = PEventResult(
        p_event=0.10,
        source="options_implied",
        confidence=0.85,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"warnings": []},
        fallback_used=False
    )
    
    # Apply gate
    gate_decision = gate(
        p_external=p_external,
        p_implied=p_implied,
        min_edge=0.05,  # Edge is 0.02, below threshold
        min_confidence=0.60
    )
    
    # Gate should fail due to insufficient edge
    assert gate_decision.decision == "NO_TRADE"
    assert gate_decision.reason == "INSUFFICIENT_EDGE"
    assert gate_decision.edge == pytest.approx(0.02)
    
    # External source policy would also block,
    # but gate failure takes precedence in decision flow


def test_p_implied_failure_blocks_before_external_source_policy():
    """
    Test that p_implied calculation failure blocks before external source policy.
    
    Scenario:
    - p_external from fallback = 0.30
    - p_implied = None (calculation failed)
    - allow_fallback_trade = False
    
    Expected:
    - Gate fails with NO_P_IMPLIED
    - External source policy would also block, but gate blocks first
    - Reason: EDGE_GATE_BLOCKED:NO_P_IMPLIED
    """
    # Create p_external from fallback
    p_external = PEventResult(
        p_event=0.30,
        source="fallback",
        confidence=0.7,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"source": "fallback"},
        fallback_used=True
    )
    
    # p_implied is None (calculation failed)
    p_implied = None
    
    # Apply gate
    gate_decision = gate(
        p_external=p_external,
        p_implied=p_implied,
        min_edge=0.05,
        min_confidence=0.60
    )
    
    # Gate should fail due to missing p_implied
    assert gate_decision.decision == "NO_TRADE"
    assert gate_decision.reason == "NO_P_IMPLIED"
    assert gate_decision.edge is None
    assert gate_decision.p_implied is None
    
    # External source policy would also block,
    # but gate failure takes precedence
