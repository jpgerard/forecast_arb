"""
Test Phase 4b Execution Enforcement

Tests for PR-EXEC-1 through PR-EXEC-5:
- PR-EXEC-1: Intent immutability enforcement
- PR-EXEC-2: Price band clamping  
- PR-EXEC-3: ExecutionResult v2 schema
- PR-EXEC-4: Mode invariants
- PR-EXEC-5: Ledger hook
"""

import json
import pytest
from pathlib import Path
from forecast_arb.execution.execute_trade import (
    enforce_intent_immutability,
    apply_price_band_clamping,
    enforce_mode_invariants,
)
from forecast_arb.execution.execution_result import (
    create_execution_result,
    validate_execution_result,
)
from forecast_arb.execution.outcome_ledger import append_trade_event, read_trade_events


def test_pr_exec_1_intent_immutability_pass():
    """Test PR-EXEC-1: Intent immutability passes when fields match."""
    intent = {
        "expiry": "20260327",
        "legs": [
            {"strike": 590.0, "right": "P", "action": "BUY"},
            {"strike": 570.0, "right": "P", "action": "SELL"},
        ]
    }
    
    resolved_expiry = "20260327"
    resolved_strikes = [590.0, 570.0]
    
    # Should not raise
    enforce_intent_immutability(intent, resolved_expiry, resolved_strikes)


def test_pr_exec_1_intent_immutability_fail_expiry():
    """Test PR-EXEC-1: Intent immutability fails on expiry mismatch."""
    intent = {
        "expiry": "20260327",
        "legs": [
            {"strike": 590.0, "right": "P", "action": "BUY"},
            {"strike": 570.0, "right": "P", "action": "SELL"},
        ]
    }
    
    resolved_expiry = "20260320"  # Wrong expiry
    resolved_strikes = [590.0, 570.0]
    
    with pytest.raises(AssertionError, match="IMMUTABILITY VIOLATION.*expiry"):
        enforce_intent_immutability(intent, resolved_expiry, resolved_strikes)


def test_pr_exec_1_intent_immutability_fail_strikes():
    """Test PR-EXEC-1: Intent immutability fails on strikes mismatch."""
    intent = {
        "expiry": "20260327",
        "legs": [
            {"strike": 590.0, "right": "P", "action": "BUY"},
            {"strike": 570.0, "right": "P", "action": "SELL"},
        ]
    }
    
    resolved_expiry = "20260327"
    resolved_strikes = [585.0, 565.0]  # Wrong strikes
    
    with pytest.raises(AssertionError, match="IMMUTABILITY VIOLATION.*strikes"):
        enforce_intent_immutability(intent, resolved_expiry, resolved_strikes)


def test_pr_exec_2_price_band_clamping_pass():
    """Test PR-EXEC-2: Price band clamping allows valid range."""
    intent = {
        "limit": {
            "start": 0.40,
            "max": 0.50
        }
    }
    
    computed_mid = 0.45  # Within range
    
    exec_limit_low, exec_limit_high = apply_price_band_clamping(intent, computed_mid)
    
    # Should clamp to narrower range
    assert exec_limit_low == 0.45  # max(0.40, 0.45)
    assert exec_limit_high == 0.45  # min(0.50, 0.45)


def test_pr_exec_2_price_band_clamping_blocked():
    """Test PR-EXEC-2: Price band clamping blocks on drift."""
    intent = {
        "limit": {
            "start": 0.40,
            "max": 0.42
        }
    }
    
    computed_mid = 0.50  # Outside range - drifted higher
    
    with pytest.raises(ValueError, match="BLOCKED_PRICE_DRIFT"):
        apply_price_band_clamping(intent, computed_mid)


def test_pr_exec_3_execution_result_v2_schema():
    """Test PR-EXEC-3: ExecutionResult v2 schema creation and validation."""
    result = create_execution_result(
        intent_id="test_123",
        mode="quote-only",
        verdict="OK_TO_STAGE",
        reason="Guards passed",
        quotes={
            "long": {"bid": 3.50, "ask": 3.60},
            "short": {"bid": 1.20, "ask": 1.30},
            "combo_mid": 2.25
        },
        limits={
            "intent": [0.40, 0.50],
            "effective": [0.45, 0.45]
        },
        guards={
            "max_debit": "PASS",
            "min_dte": "PASS"
        }
    )
    
    # Validate required fields
    assert result["intent_id"] == "test_123"
    assert result["mode"] == "quote-only"
    assert result["execution_verdict"] == "OK_TO_STAGE"
    assert result["reason"] == "Guards passed"
    assert "timestamp_utc" in result
    
    # Should pass validation
    validate_execution_result(result)


def test_pr_exec_3_execution_result_invalid_mode():
    """Test PR-EXEC-3: ExecutionResult v2 rejects invalid mode."""
    result = create_execution_result(
        intent_id="test_123",
        mode="invalid_mode",  # Invalid
        verdict="OK_TO_STAGE",
        reason="Test",
        quotes={"long": {}, "short": {}, "combo_mid": 0.0},
        limits={"intent": [], "effective": []},
        guards={}
    )
    
    with pytest.raises(ValueError, match="Invalid mode"):
        validate_execution_result(result)


def test_pr_exec_3_execution_result_invalid_verdict():
    """Test PR-EXEC-3: ExecutionResult v2 rejects invalid verdict."""
    result = create_execution_result(
        intent_id="test_123",
        mode="quote-only",
        verdict="INVALID_VERDICT",  # Invalid
        reason="Test",
        quotes={"long": {}, "short": {}, "combo_mid": 0.0},
        limits={"intent": [], "effective": []},
        guards={}
    )
    
    with pytest.raises(ValueError, match="Invalid verdict"):
        validate_execution_result(result)


def test_pr_exec_4_mode_invariants_quote_only():
    """Test PR-EXEC-4: Quote-only mode cannot transmit."""
    # quote-only with transmit=False should pass
    enforce_mode_invariants(
        mode="paper",
        quote_only=True,
        transmit=False,
        confirm=None
    )
    
    # quote-only with transmit=True should fail
    with pytest.raises(AssertionError, match="MODE VIOLATION.*quote-only"):
        enforce_mode_invariants(
            mode="paper",
            quote_only=True,
            transmit=True,
            confirm="SEND"
        )


def test_pr_exec_4_mode_invariants_paper():
    """Test PR-EXEC-4: Paper mode cannot transmit."""
    # paper with transmit=False should pass
    enforce_mode_invariants(
        mode="paper",
        quote_only=False,
        transmit=False,
        confirm=None
    )
    
    # paper with transmit=True should fail
    with pytest.raises(AssertionError, match="MODE VIOLATION.*paper"):
        enforce_mode_invariants(
            mode="paper",
            quote_only=False,
            transmit=True,
            confirm="SEND"
        )


def test_pr_exec_4_mode_invariants_live_requires_confirm():
    """Test PR-EXEC-4: Live mode transmit requires confirmation."""
    # live with transmit=True and confirm="SEND" should pass
    enforce_mode_invariants(
        mode="live",
        quote_only=False,
        transmit=True,
        confirm="SEND"
    )
    
    # live with transmit=True but wrong confirm should fail
    with pytest.raises(AssertionError, match="MODE VIOLATION.*confirm"):
        enforce_mode_invariants(
            mode="live",
            quote_only=False,
            transmit=True,
            confirm="wrong"
        )
    
    # live with transmit=False should pass (staging only)
    enforce_mode_invariants(
        mode="live",
        quote_only=False,
        transmit=False,
        confirm=None
    )


def test_pr_exec_5_quote_ok_event(tmp_path):
    """Test PR-EXEC-5: QUOTE_OK event written via append_trade_event."""
    ledger = tmp_path / "runs" / "trade_outcomes.jsonl"
    ledger.parent.mkdir(parents=True)

    import os
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        append_trade_event(
            event="QUOTE_OK",
            intent_id="abc123",
            candidate_id="cand_1",
            run_id="run_001",
            regime="crash",
            timestamp_utc="2026-03-25T10:00:00+00:00",
            also_global=True,
        )
        events = read_trade_events(Path("runs") / "trade_outcomes.jsonl")
        assert len(events) == 1
        assert events[0]["event"] == "QUOTE_OK"
        assert events[0]["intent_id"] == "abc123"
        assert events[0]["candidate_id"] == "cand_1"
        assert events[0]["run_id"] == "run_001"
    finally:
        os.chdir(original_cwd)


def test_pr_exec_5_quote_blocked_event(tmp_path):
    """Test PR-EXEC-5: QUOTE_BLOCKED event written via append_trade_event."""
    import os
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        append_trade_event(
            event="QUOTE_BLOCKED",
            intent_id="def456",
            candidate_id="cand_2",
            run_id="run_002",
            regime="selloff",
            timestamp_utc="2026-03-25T10:01:00+00:00",
            also_global=True,
        )
        events = read_trade_events(Path("runs") / "trade_outcomes.jsonl")
        assert len(events) == 1
        assert events[0]["event"] == "QUOTE_BLOCKED"
        assert events[0]["regime"] == "selloff"
    finally:
        os.chdir(original_cwd)


def test_pr_exec_5_ledger_hook_legacy(tmp_path):
    """Test PR-EXEC-5 (legacy): Ledger hook writes to outcome ledger."""
    intent = {
        "candidate_id": "test_candidate",
        "run_id": "test_run",
        "regime": "crash",
        "qty": 1,
        "expiry": "20260327",
        "legs": [
            {"strike": 590.0, "right": "P", "action": "BUY"},
            {"strike": 570.0, "right": "P", "action": "SELL"},
        ]
    }
    
    # Change to tmp directory for testing
    import os
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    
    try:
        # Use append_trade_event (write_ledger_hook was removed; replaced by append_trade_event)
        append_trade_event(
            event="QUOTE_OK",
            intent_id="test_intent_id",
            candidate_id=intent["candidate_id"],
            run_id=intent["run_id"],
            regime=intent["regime"],
            timestamp_utc="2026-03-25T10:00:00+00:00",
            also_global=True,
        )

        # Check that runs/trade_outcomes.jsonl was created
        ledger_file = tmp_path / "runs" / "trade_outcomes.jsonl"
        assert ledger_file.exists(), "Ledger file should be created"

    finally:
        os.chdir(original_cwd)


def test_price_band_clamping_tightens_not_loosens():
    """Test that price band clamping tightens but never loosens limits."""
    intent = {
        "limit": {
            "start": 0.40,
            "max": 0.50
        }
    }
    
    # Computed mid within range - should tighten
    computed_mid = 0.45
    exec_low, exec_high = apply_price_band_clamping(intent, computed_mid)
    assert exec_low >= 0.40  # Never looser than intent
    assert exec_high <= 0.50  # Never looser than intent
    
    # Computed mid at lower bound - should use intent start
    computed_mid = 0.35
    exec_low, exec_high = apply_price_band_clamping(intent, computed_mid)
    assert exec_low == 0.40  # Uses intent start (tighter)
    assert exec_high == 0.35  # Uses computed (tighter)
    
    # Computed mid at upper bound - should use intent max  
    computed_mid = 0.48
    exec_low, exec_high = apply_price_band_clamping(intent, computed_mid)
    assert exec_low == 0.48  # Uses computed (tighter)
    assert exec_high == 0.48  # Uses computed (tighter)


def test_execution_result_optional_order_id():
    """Test that ExecutionResult can include optional order_id."""
    result = create_execution_result(
        intent_id="test_123",
        mode="paper",
        verdict="OK_TO_STAGE",
        reason="Test",
        quotes={"long": {}, "short": {}, "combo_mid": 0.0},
        limits={"intent": [], "effective": []},
        guards={},
        order_id="ORDER_12345"
    )
    
    assert result["order_id"] == "ORDER_12345"
    validate_execution_result(result)


def test_execution_result_missing_required_fields():
    """Test that ExecutionResult validation catches missing fields."""
    # Missing quotes
    with pytest.raises(ValueError, match="missing required field"):
        validate_execution_result({
            "intent_id": "test",
            "mode": "quote-only",
            "execution_verdict": "OK_TO_STAGE",
            "reason": "Test",
            # missing quotes
            "limits": {"intent": [], "effective": []},
            "guards": {},
            "timestamp_utc": "2026-01-01T00:00:00Z"
        })
    
    # Missing limits
    with pytest.raises(ValueError, match="missing required field"):
        validate_execution_result({
            "intent_id": "test",
            "mode": "quote-only",
            "execution_verdict": "OK_TO_STAGE",
            "reason": "Test",
            "quotes": {"long": {}, "short": {}, "combo_mid": 0.0},
            # missing limits
            "guards": {},
            "timestamp_utc": "2026-01-01T00:00:00Z"
        })


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
