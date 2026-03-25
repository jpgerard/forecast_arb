"""
Failure-Mode Smoke Tests for Execution Guards

Tests guard validation failures produce deterministic error messages.
No IBKR connectivity required.
"""

import pytest
from datetime import datetime, timezone, timedelta
from forecast_arb.execution.execute_trade import (
    validate_order_intent,
    enforce_guards
)


def test_missing_required_field_strategy():
    """Test that missing 'strategy' field raises ValueError."""
    intent = {
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent missing required field: strategy"):
        validate_order_intent(intent)


def test_missing_required_field_symbol():
    """Test that missing 'symbol' field raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent missing required field: symbol"):
        validate_order_intent(intent)


def test_missing_required_field_legs():
    """Test that missing 'legs' field raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent missing required field: legs"):
        validate_order_intent(intent)


def test_empty_legs():
    """Test that empty legs list raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent must have at least one leg"):
        validate_order_intent(intent)


def test_leg_missing_action():
    """Test that leg missing 'action' field raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"right": "P", "strike": 590.0}
        ],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent leg 0 missing required fields"):
        validate_order_intent(intent)


def test_leg_missing_right():
    """Test that leg missing 'right' field raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "strike": 590.0}
        ],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent leg 0 missing required fields"):
        validate_order_intent(intent)


def test_leg_missing_strike():
    """Test that leg missing 'strike' field raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P"}
        ],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent leg 0 missing required fields"):
        validate_order_intent(intent)


def test_invalid_expiry_format():
    """Test that invalid expiry format is detected."""
    # Note: validate_order_intent doesn't check expiry format, but it should still parse
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "2026-03-20",  # Wrong format
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0},
            {"action": "SELL", "right": "P", "strike": 570.0}
        ],
        "qty": 1,
        "limit": {"start": 0.35, "max": 0.36},
        "tif": "DAY",
        "guards": {"min_dte": 30},
        "intent_id": "test_expiry_format_check_abc12345",   # required since Phase 4b
    }
    
    # This should pass validation (expiry format check happens in guards)
    validate_order_intent(intent)
    
    # But enforce_guards should fail when parsing expiry
    leg_quotes = [
        {"bid": 50.0, "ask": 51.0, "mid": 50.5, "last": 50.5},
        {"bid": 30.0, "ask": 31.0, "mid": 30.5, "last": 30.5}
    ]
    
    with pytest.raises(ValueError):
        enforce_guards(intent, leg_quotes, 20.0, 600.0)


def test_limit_missing_start():
    """Test that limit missing 'start' raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0}
        ],
        "qty": 1,
        "limit": {"max": 0.36},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent limit must have 'start' and 'max'"):
        validate_order_intent(intent)


def test_limit_missing_max():
    """Test that limit missing 'max' raises ValueError."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0}
        ],
        "qty": 1,
        "limit": {"start": 0.35},
        "tif": "DAY",
        "guards": {}
    }
    
    with pytest.raises(ValueError, match="OrderIntent limit must have 'start' and 'max'"):
        validate_order_intent(intent)


def test_guard_debit_too_high():
    """Test that guard violation for debit too high produces deterministic error."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0},
            {"action": "SELL", "right": "P", "strike": 570.0}
        ],
        "qty": 1,
        "limit": {"start": 20.0, "max": 25.0},
        "tif": "DAY",
        "guards": {
            "max_debit": 15.0
        }
    }
    
    leg_quotes = [
        {"bid": 50.0, "ask": 51.0, "mid": 50.5, "last": 50.5},
        {"bid": 30.0, "ask": 31.0, "mid": 30.5, "last": 30.5}
    ]
    
    combo_debit = 20.0  # Exceeds max_debit of 15.0
    spot_price = 600.0
    
    with pytest.raises(ValueError, match=r"GUARD VIOLATION: Debit \$20\.00 exceeds max \$15\.00"):
        enforce_guards(intent, leg_quotes, combo_debit, spot_price)


def test_guard_dte_too_low():
    """Test that guard violation for DTE too low produces deterministic error."""
    # Create expiry that's 10 days from now
    future_date = datetime.now(timezone.utc).date() + timedelta(days=10)
    expiry = future_date.strftime("%Y%m%d")
    
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": expiry,
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0},
            {"action": "SELL", "right": "P", "strike": 570.0}
        ],
        "qty": 1,
        "limit": {"start": 20.0, "max": 25.0},
        "tif": "DAY",
        "guards": {
            "min_dte": 30  # Requires at least 30 days
        }
    }
    
    leg_quotes = [
        {"bid": 50.0, "ask": 51.0, "mid": 50.5, "last": 50.5},
        {"bid": 30.0, "ask": 31.0, "mid": 30.5, "last": 30.5}
    ]
    
    combo_debit = 20.0
    spot_price = 600.0
    
    with pytest.raises(ValueError, match=r"GUARD VIOLATION: DTE \d+ < min 30"):
        enforce_guards(intent, leg_quotes, combo_debit, spot_price)


def test_guard_missing_executable_legs():
    """Test that guard violation for missing bid/ask produces deterministic error."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0},
            {"action": "SELL", "right": "P", "strike": 570.0}
        ],
        "qty": 1,
        "limit": {"start": 20.0, "max": 25.0},
        "tif": "DAY",
        "guards": {
            "require_executable_legs": True
        }
    }
    
    leg_quotes = [
        {"bid": None, "ask": 51.0, "mid": 50.5, "last": 50.5},  # Missing bid
        {"bid": 30.0, "ask": 31.0, "mid": 30.5, "last": 30.5}
    ]
    
    combo_debit = 20.0
    spot_price = 600.0
    
    with pytest.raises(ValueError, match=r"GUARD VIOLATION: Leg 0 missing executable quotes"):
        enforce_guards(intent, leg_quotes, combo_debit, spot_price)


def test_guard_spread_width_too_wide():
    """Test that guard violation for spread width too wide produces deterministic error."""
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": "20260320",
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0},
            {"action": "SELL", "right": "P", "strike": 500.0}  # 90-point spread
        ],
        "qty": 1,
        "limit": {"start": 20.0, "max": 25.0},
        "tif": "DAY",
        "guards": {
            "max_spread_width": 0.10  # 10% max
        }
    }
    
    leg_quotes = [
        {"bid": 100.0, "ask": 101.0, "mid": 100.5, "last": 100.5},
        {"bid": 10.0, "ask": 11.0, "mid": 10.5, "last": 10.5}
    ]
    
    combo_debit = 90.0
    spot_price = 600.0  # 90/600 = 15% > 10%
    
    with pytest.raises(ValueError, match=r"GUARD VIOLATION: Spread width 15\.00% exceeds max 10\.00%"):
        enforce_guards(intent, leg_quotes, combo_debit, spot_price)


def test_valid_intent_passes_validation():
    """Test that a valid intent passes all validations."""
    # Create expiry that's 45 days from now
    future_date = datetime.now(timezone.utc).date() + timedelta(days=45)
    expiry = future_date.strftime("%Y%m%d")
    
    intent = {
        "strategy": "crash_venture_v1",
        "symbol": "SPY",
        "expiry": expiry,
        "type": "PUT_SPREAD",
        "legs": [
            {"action": "BUY", "right": "P", "strike": 590.0},
            {"action": "SELL", "right": "P", "strike": 570.0}
        ],
        "qty": 1,
        "limit": {"start": 20.0, "max": 25.0},
        "tif": "DAY",
        "guards": {
            "max_debit": 25.0,
            "min_dte": 30,
            "max_spread_width": 0.10,
            "require_executable_legs": True
        },
        "intent_id": "test_valid_intent_abc1234567890abcd",  # required since Phase 4b
    }
    
    # Should not raise
    validate_order_intent(intent)
    
    leg_quotes = [
        {"bid": 50.0, "ask": 51.0, "mid": 50.5, "last": 50.5},
        {"bid": 30.0, "ask": 31.0, "mid": 30.5, "last": 30.5}
    ]
    
    combo_debit = 21.0  # 51 - 30 = 21 (within max_debit of 25)
    spot_price = 600.0  # 20/600 = 3.33% (within max_spread_width of 10%)
    
    # Should not raise
    enforce_guards(intent, leg_quotes, combo_debit, spot_price)
