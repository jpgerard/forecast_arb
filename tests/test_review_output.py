"""
Tests for human-readable review output.

Verifies review.txt formatting and deterministic output.
"""

import pytest
from forecast_arb.execution.review import format_review


class TestReviewFormatting:
    """Test review output formatting."""
    
    def test_review_contains_required_sections(self):
        """Review should contain all required sections."""
        review = format_review(
            run_id="test_run_001",
            decision="TRADE",
            reason="Structures generated",
            p_external=0.30,
            p_implied=0.25,
            edge=0.05,
            confidence=85.0,
            tickets=[],
            caps={"max_orders": 3, "max_debit_total": 10000.0},
            mode="dev",
            config_hash="abc123"
        )
        
        # Check for section headers
        assert "TRADE REVIEW SUMMARY" in review
        assert "MARKET ASSESSMENT" in review
        assert "GATE DECISION" in review
        assert "ORDER TICKETS" in review
        assert "CAPS & GUARDRAILS" in review
        assert "SUBMISSION STATUS" in review
        
        # Check for run metadata
        assert "test_run_001" in review
        assert "DEV" in review
        assert "abc123" in review
    
    def test_review_market_assessment_formatting(self):
        """Test market assessment section formatting."""
        review = format_review(
            run_id="test_run_002",
            decision="TRADE",
            reason="Edge found",
            p_external=0.3500,
            p_implied=0.2800,
            edge=0.0700,
            confidence=90.5,
            tickets=[],
            caps={},
            mode="dev"
        )
        
        # Check probability formatting
        assert "P(External): 0.3500 (35.00%)" in review
        assert "P(Implied):  0.2800 (28.00%)" in review
        
        # Check edge in basis points
        assert "Edge:        0.0700 (+700.0 bps)" in review
        
        # Check confidence (already in percentage, 1 decimal place is fine)
        assert "Confidence:  90.5%" in review
    
    def test_review_no_tickets_formatting(self):
        """Test NO_TRADE case with no tickets."""
        review = format_review(
            run_id="test_run_003",
            decision="NO_TRADE",
            reason="Insufficient edge",
            p_external=0.30,
            p_implied=0.29,
            edge=0.01,
            confidence=None,
            tickets=[],
            caps={"max_orders": 3},
            mode="dev"
        )
        
        assert "Decision: NO_TRADE" in review
        assert "Reason:   Insufficient edge" in review
        assert "No tickets generated (NO_TRADE)" in review
    
    def test_review_with_tickets_formatting(self):
        """Test TRADE case with tickets."""
        tickets = [
            {
                "symbol": "SPY",
                "expiry": "20260227",
                "limit_price": 12.50,
                "quantity": 2,
                "legs": [
                    {"action": "BUY", "strike": 450, "right": "P"},
                    {"action": "SELL", "strike": 440, "right": "P"}
                ],
                "metadata": {
                    "ev_per_dollar": 0.125,
                    "prob_profit": 0.45
                }
            }
        ]
        
        review = format_review(
            run_id="test_run_004",
            decision="TRADE",
            reason="Structures generated",
            p_external=0.30,
            p_implied=None,
            edge=None,
            confidence=None,
            tickets=tickets,
            caps={"max_orders": 3, "max_debit_total": 5000.0},
            mode="dev"
        )
        
        assert "Total Tickets: 1" in review
        assert "Ticket #1:" in review
        assert "SPY 2026-02-27 450/440 Put Spread" in review
        assert "BUY 450P / SELL 440P" in review
        assert "@ $12.50 x2" in review  # Has $ sign
        assert "EV/$: 0.125" in review
        assert "P(Profit): 45.0%" in review
    
    def test_review_submission_disabled(self):
        """Test submission disabled message."""
        review = format_review(
            run_id="test_run_005",
            decision="TRADE",
            reason="Structures generated",
            p_external=None,
            p_implied=None,
            edge=None,
            confidence=None,
            tickets=[],
            caps={},
            mode="dev",
            submit_requested=False,
            submit_blocked=False
        )
        
        assert "SUBMISSION: DISABLED (dry-run)" in review
        assert "No orders will be submitted to IBKR" in review
    
    def test_review_submission_blocked(self):
        """Test submission blocked message."""
        review = format_review(
            run_id="test_run_006",
            decision="NO_TRADE",
            reason="Submission not confirmed",
            p_external=None,
            p_implied=None,
            edge=None,
            confidence=None,
            tickets=[],
            caps={},
            mode="smoke",
            submit_requested=True,
            submit_blocked=True,
            submit_block_reason="SUBMIT_BLOCKED_SMOKE_MODE"
        )
        
        assert "SUBMISSION: BLOCKED" in review
        assert "SUBMIT_BLOCKED_SMOKE_MODE" in review
    
    def test_review_submission_enabled(self):
        """Test submission enabled warning."""
        review = format_review(
            run_id="test_run_007",
            decision="TRADE",
            reason="Structures generated",
            p_external=None,
            p_implied=None,
            edge=None,
            confidence=None,
            tickets=[],
            caps={},
            mode="prod",
            submit_requested=True,
            submit_blocked=False
        )
        
        assert "SUBMISSION: ENABLED (will submit)" in review
        assert "⚠️  LIVE ORDERS WILL BE PLACED" in review
    
    def test_review_caps_display(self):
        """Test caps and guardrails display."""
        tickets = [
            {
                "symbol": "SPY",
                "expiry": "20260227",
                "limit_price": 10.00,
                "quantity": 2,
                "legs": [{"action": "BUY", "strike": 450, "right": "P"},
                        {"action": "SELL", "strike": 440, "right": "P"}]
            }
        ]
        
        review = format_review(
            run_id="test_run_008",
            decision="TRADE",
            reason="Structures generated",
            p_external=None,
            p_implied=None,
            edge=None,
            confidence=None,
            tickets=tickets,
            caps={"max_orders": 5, "max_debit_total": 10000.0},
            mode="dev"
        )
        
        assert "Max Orders: 5" in review
        assert "Applied: 1 ticket(s)" in review
        assert "Max Debit Total: $10,000.00" in review
        assert "Applied: $2,000.00" in review  # 10.00 * 2 * 100
    
    def test_review_deterministic_output(self):
        """Test that review output is deterministic."""
        args = {
            "run_id": "test_run_009",
            "decision": "TRADE",
            "reason": "Test",
            "p_external": 0.30,
            "p_implied": 0.25,
            "edge": 0.05,
            "confidence": 85.0,
            "tickets": [],
            "caps": {"max_orders": 3},
            "mode": "dev",
            "config_hash": "abc123"
        }
        
        review1 = format_review(**args)
        review2 = format_review(**args)
        
        # Should produce identical output (except timestamp)
        # Compare line by line, excluding timestamp line
        lines1 = [l for l in review1.split("\n") if "Timestamp:" not in l]
        lines2 = [l for l in review2.split("\n") if "Timestamp:" not in l]
        
        assert lines1 == lines2
    
    def test_review_confidence_zero_shows_na(self):
        """Test that confidence=0.0 displays as N/A, not 0.0%."""
        review = format_review(
            run_id="test_run_010",
            decision="NO_TRADE",
            reason="NO_P_IMPLIED",
            p_external=0.40,
            p_implied=None,
            edge=None,
            confidence=0.0,  # Should display as N/A
            tickets=[],
            caps={},
            mode="dev"
        )
        
        assert "Confidence:  N/A" in review
        # Check that Confidence line specifically doesn't show percentage
        lines = review.split("\n")
        confidence_line = [l for l in lines if l.startswith("Confidence:")][0]
        assert "N/A" in confidence_line
        assert "%" not in confidence_line
    
    def test_review_confidence_none_shows_na(self):
        """Test that confidence=None displays as N/A."""
        review = format_review(
            run_id="test_run_011",
            decision="NO_TRADE",
            reason="NO_P_IMPLIED",
            p_external=None,
            p_implied=None,
            edge=None,
            confidence=None,
            tickets=[],
            caps={},
            mode="dev"
        )
        
        assert "Confidence:  N/A" in review
    
    def test_review_confidence_decimal_converts_to_percent(self):
        """Test that confidence in [0,1] converts to percentage properly."""
        review = format_review(
            run_id="test_run_012",
            decision="TRADE",
            reason="PASSED_GATES",
            p_external=0.40,
            p_implied=0.30,
            edge=0.10,
            confidence=0.75,  # Should display as 75.0%
            tickets=[],
            caps={},
            mode="dev"
        )
        
        assert "Confidence:  75.0%" in review
        assert "0.75%" not in review  # Wrong format
    
    def test_review_confidence_already_percent(self):
        """Test that confidence >1 is treated as already in percent."""
        review = format_review(
            run_id="test_run_013",
            decision="TRADE",
            reason="PASSED_GATES",
            p_external=0.40,
            p_implied=0.30,
            edge=0.10,
            confidence=80.5,  # Already in percentage
            tickets=[],
            caps={},
            mode="dev"
        )
        
        assert "Confidence:  80.5%" in review
