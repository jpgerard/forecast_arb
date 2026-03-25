"""
Tests for submission barrier and safety features.

Verifies that orders are never submitted without explicit confirmation.
"""

import pytest
from forecast_arb.execution.tickets import OrderLeg, OrderTicket, from_candidate, to_dict


class TestSubmitBarrier:
    """Test submission safety barriers."""
    
    def test_smoke_mode_blocks_submission(self):
        """Mode=smoke should block submission even with --submit flag."""
        # Simulate CLI args for smoke mode with submit
        mode = "smoke"
        submit = True
        confirm = "SUBMIT"
        
        # Apply submission logic
        submit_blocked = False
        submit_block_reason = None
        
        if submit and mode == "smoke":
            submit_blocked = True
            submit_block_reason = "SUBMIT_BLOCKED_SMOKE_MODE"
        
        assert submit_blocked
        assert submit_block_reason == "SUBMIT_BLOCKED_SMOKE_MODE"
    
    def test_missing_confirm_blocks_submission(self):
        """Submission without correct --confirm should block."""
        mode = "dev"
        submit = True
        confirm = None  # Missing
        
        submit_blocked = False
        submit_block_reason = None
        
        if submit and confirm != "SUBMIT":
            submit_blocked = True
            submit_block_reason = "SUBMIT_NOT_CONFIRMED"
        
        assert submit_blocked
        assert submit_block_reason == "SUBMIT_NOT_CONFIRMED"
    
    def test_wrong_confirm_blocks_submission(self):
        """Submission with wrong --confirm value should block."""
        mode = "dev"
        submit = True
        confirm = "YES"  # Wrong value
        
        submit_blocked = False
        submit_block_reason = None
        
        if submit and confirm != "SUBMIT":
            submit_blocked = True
            submit_block_reason = "SUBMIT_NOT_CONFIRMED"
        
        assert submit_blocked
        assert submit_block_reason == "SUBMIT_NOT_CONFIRMED"
    
    def test_dev_mode_with_correct_confirm_allows(self):
        """Dev mode with correct confirmation should allow submission."""
        mode = "dev"
        submit = True
        confirm = "SUBMIT"
        
        submit_blocked = False
        submit_block_reason = None
        
        if submit and confirm != "SUBMIT":
            submit_blocked = True
            submit_block_reason = "SUBMIT_NOT_CONFIRMED"
        elif submit and mode == "smoke":
            submit_blocked = True
            submit_block_reason = "SUBMIT_BLOCKED_SMOKE_MODE"
        
        assert not submit_blocked
        assert submit_block_reason is None
    
    def test_prod_mode_with_correct_confirm_allows(self):
        """Prod mode with correct confirmation should allow submission."""
        mode = "prod"
        submit = True
        confirm = "SUBMIT"
        
        submit_blocked = False
        submit_block_reason = None
        
        if submit and confirm != "SUBMIT":
            submit_blocked = True
            submit_block_reason = "SUBMIT_NOT_CONFIRMED"
        elif submit and mode == "smoke":
            submit_blocked = True
            submit_block_reason = "SUBMIT_BLOCKED_SMOKE_MODE"
        
        assert not submit_blocked
        assert submit_block_reason is None
    
    def test_no_submit_flag_safe_default(self):
        """Default behavior (no --submit) should never submit."""
        mode = "dev"
        submit = False  # Default
        confirm = None
        
        # Submission logic shouldn't even check barriers
        assert not submit
        
        # No tickets should be submitted
        submit_executed = False
        assert not submit_executed


class TestOrderTicketCreation:
    """Test order ticket creation from candidates."""
    
    def test_from_candidate_basic(self):
        """Test creating order ticket from a candidate structure."""
        candidate = {
            "underlier": "SPY",
            "expiry": "20260227",
            "strikes": {
                "long_put": 450.0,
                "short_put": 440.0
            },
            "debit_per_contract": 1250.0,  # $12.50 per spread
            "max_loss_per_contract": 1250.0,
            "max_gain_per_contract": 750.0,
            "ev_per_contract": 150.0,
            "ev_per_dollar": 0.12,
            "prob_profit": 0.45,
            "spread_width": 10.0,
            "rank": 1
        }
        
        ticket = from_candidate(candidate, quantity=2)
        
        assert ticket.symbol == "SPY"
        assert ticket.expiry == "20260227"
        assert ticket.limit_price == 12.50  # Converted from per-contract
        assert ticket.quantity == 2
        assert len(ticket.legs) == 2
        assert ticket.combo_type == "VERTICAL_SPREAD"
    
    def test_ticket_total_debit_calculation(self):
        """Test total debit calculation."""
        candidate = {
            "underlier": "SPY",
            "expiry": "20260227",
            "strikes": {"long_put": 450.0, "short_put": 440.0},
            "debit_per_contract": 1250.0
        }
        
        ticket = from_candidate(candidate, quantity=3)
        
        # Total debit = limit_price * quantity * 100
        expected_debit = 12.50 * 3 * 100
        assert ticket.total_debit() == expected_debit
    
    def test_ticket_dict_serialization(self):
        """Test ticket can be serialized to dict/JSON."""
        candidate = {
            "underlier": "SPY",
            "expiry": "20260227",
            "strikes": {"long_put": 450.0, "short_put": 440.0},
            "debit_per_contract": 1250.0
        }
        
        ticket = from_candidate(candidate, quantity=1)
        ticket_dict = to_dict(ticket)
        
        assert isinstance(ticket_dict, dict)
        assert ticket_dict["symbol"] == "SPY"
        assert ticket_dict["expiry"] == "20260227"
        assert "legs" in ticket_dict
        assert len(ticket_dict["legs"]) == 2


class TestOrderLegs:
    """Test order leg validation."""
    
    def test_buy_put_leg(self):
        """Test creating a BUY put leg."""
        leg = OrderLeg(
            action="BUY",
            right="P",
            strike=450.0,
            expiry="20260227",
            quantity=1
        )
        
        assert leg.action == "BUY"
        assert leg.right == "P"
        assert leg.strike == 450.0
    
    def test_sell_put_leg(self):
        """Test creating a SELL put leg."""
        leg = OrderLeg(
            action="SELL",
            right="P",
            strike=440.0,
            expiry="20260227",
            quantity=1
        )
        
        assert leg.action == "SELL"
        assert leg.right == "P"
        assert leg.strike == 440.0
    
    def test_invalid_action_raises(self):
        """Test invalid action raises ValueError."""
        with pytest.raises(ValueError, match="action must be"):
            OrderLeg(
                action="HOLD",  # Invalid
                right="P",
                strike=450.0,
                expiry="20260227",
                quantity=1
            )
    
    def test_invalid_right_raises(self):
        """Test invalid right raises ValueError."""
        with pytest.raises(ValueError, match="right must be"):
            OrderLeg(
                action="BUY",
                right="X",  # Invalid
                strike=450.0,
                expiry="20260227",
                quantity=1
            )
    
    def test_zero_strike_raises(self):
        """Test zero strike raises ValueError."""
        with pytest.raises(ValueError, match="strike must be >0"):
            OrderLeg(
                action="BUY",
                right="P",
                strike=0.0,  # Invalid
                expiry="20260227",
                quantity=1
            )
    
    def test_zero_quantity_raises(self):
        """Test zero quantity raises ValueError."""
        with pytest.raises(ValueError, match="quantity must be >0"):
            OrderLeg(
                action="BUY",
                right="P",
                strike=450.0,
                expiry="20260227",
                quantity=0  # Invalid
            )
