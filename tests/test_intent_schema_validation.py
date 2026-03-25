"""
Test Intent Schema Validation

D4: Validates OrderIntent schema to catch malformed intents early.
"""

import pytest
from datetime import datetime, timedelta


def validate_intent_schema(intent: dict) -> tuple[bool, list[str]]:
    """
    Validate OrderIntent schema.
    
    Returns:
        (is_valid, errors)
    """
    errors = []
    
    # Required top-level fields
    required_fields = ["symbol", "expiry", "type", "legs", "qty", "limit"]
    for field in required_fields:
        if field not in intent:
            errors.append(f"Missing required field: {field}")
    
    # Validate expiry format (YYYYMMDD)
    if "expiry" in intent:
        expiry_str = intent["expiry"]
        if not isinstance(expiry_str, str):
            errors.append(f"expiry must be string, got {type(expiry_str)}")
        elif len(expiry_str) != 8:
            errors.append(f"expiry must be YYYYMMDD format, got '{expiry_str}'")
        else:
            try:
                datetime.strptime(expiry_str, "%Y%m%d")
            except ValueError:
                errors.append(f"expiry '{expiry_str}' is not a valid date in YYYYMMDD format")
    
    # Validate legs
    if "legs" in intent:
        if not isinstance(intent["legs"], list):
            errors.append(f"legs must be a list, got {type(intent['legs'])}")
        elif len(intent["legs"]) == 0:
            errors.append("legs cannot be empty")
        else:
            for i, leg in enumerate(intent["legs"]):
                # Validate action
                if "action" not in leg:
                    errors.append(f"leg[{i}] missing 'action'")
                elif leg["action"] not in ["BUY", "SELL"]:
                    errors.append(f"leg[{i}] action must be BUY or SELL, got '{leg['action']}'")
                
                # Validate strike
                if "strike" not in leg:
                    errors.append(f"leg[{i}] missing 'strike'")
                elif not isinstance(leg["strike"], (int, float)):
                    errors.append(f"leg[{i}] strike must be numeric, got {type(leg['strike'])}")
                elif leg["strike"] <= 0:
                    errors.append(f"leg[{i}] strike must be positive, got {leg['strike']}")
                
                # Validate right (if present)
                if "right" in leg and leg["right"] not in ["P", "C"]:
                    errors.append(f"leg[{i}] right must be P or C, got '{leg['right']}'")
    
    # Validate qty
    if "qty" in intent:
        if not isinstance(intent["qty"], int):
            errors.append(f"qty must be integer, got {type(intent['qty'])}")
        elif intent["qty"] <= 0:
            errors.append(f"qty must be positive, got {intent['qty']}")
    
    # Validate limit
    if "limit" in intent:
        limit = intent["limit"]
        if not isinstance(limit, dict):
            errors.append(f"limit must be a dict, got {type(limit)}")
        else:
            if "start" not in limit:
                errors.append("limit missing 'start'")
            elif not isinstance(limit["start"], (int, float)):
                errors.append(f"limit.start must be numeric, got {type(limit['start'])}")
            elif limit["start"] <= 0:
                errors.append(f"limit.start must be positive, got {limit['start']}")
            
            if "max" not in limit:
                errors.append("limit missing 'max'")
            elif not isinstance(limit["max"], (int, float)):
                errors.append(f"limit.max must be numeric, got {type(limit['max'])}")
            elif limit["max"] <= 0:
                errors.append(f"limit.max must be positive, got {limit['max']}")
            
            # Validate start <= max
            if "start" in limit and "max" in limit:
                if isinstance(limit["start"], (int, float)) and isinstance(limit["max"], (int, float)):
                    if limit["start"] > limit["max"]:
                        errors.append(f"limit.start ({limit['start']}) > limit.max ({limit['max']})")
    
    return (len(errors) == 0, errors)


class TestIntentSchemaValidation:
    """D4: Intent schema validation test."""
    
    def test_valid_intent(self):
        """Valid intent should pass validation."""
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
            "limit": {
                "start": 0.35,
                "max": 0.36
            },
            "tif": "DAY",
            "transmit": False
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert is_valid, f"Valid intent failed: {errors}"
        assert len(errors) == 0
    
    def test_missing_required_fields(self):
        """Missing required fields should be caught."""
        intent = {
            "symbol": "SPY",
            # Missing: expiry, type, legs, qty, limit
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("expiry" in err for err in errors)
        assert any("legs" in err for err in errors)
        assert any("qty" in err for err in errors)
        assert any("limit" in err for err in errors)
    
    def test_invalid_expiry_format(self):
        """Invalid expiry format should be caught."""
        # Too short
        intent = {
            "symbol": "SPY",
            "expiry": "202603",  # Only 6 chars
            "type": "PUT_SPREAD",
            "legs": [{"action": "BUY", "right": "P", "strike": 590}],
            "qty": 1,
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("YYYYMMDD" in err for err in errors)
        
        # Invalid date
        intent["expiry"] = "20261335"  # Month 13
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("valid date" in err for err in errors)
    
    def test_invalid_leg_actions(self):
        """Invalid leg actions should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [
                {"action": "LONG", "right": "P", "strike": 590},  # Invalid action
                {"action": "SHORT", "right": "P", "strike": 570}  # Invalid action
            ],
            "qty": 1,
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("BUY or SELL" in err for err in errors)
    
    def test_non_numeric_strikes(self):
        """Non-numeric strikes should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [
                {"action": "BUY", "right": "P", "strike": "590"},  # String instead of number
                {"action": "SELL", "right": "P", "strike": 570}
            ],
            "qty": 1,
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("numeric" in err and "strike" in err for err in errors)
    
    def test_negative_strikes(self):
        """Negative strikes should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [
                {"action": "BUY", "right": "P", "strike": -590},  # Negative
                {"action": "SELL", "right": "P", "strike": 570}
            ],
            "qty": 1,
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("positive" in err and "strike" in err for err in errors)
    
    def test_non_positive_qty(self):
        """Non-positive qty should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [{"action": "BUY", "right": "P", "strike": 590}],
            "qty": 0,  # Zero qty
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("qty" in err and "positive" in err for err in errors)
        
        # Negative qty
        intent["qty"] = -1
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
    
    def test_limit_start_greater_than_max(self):
        """limit.start > limit.max should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [{"action": "BUY", "right": "P", "strike": 590}],
            "qty": 1,
            "limit": {
                "start": 0.50,
                "max": 0.35  # Max less than start
            }
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("start" in err and "max" in err for err in errors)
    
    def test_empty_legs(self):
        """Empty legs array should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [],  # Empty
            "qty": 1,
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("empty" in err for err in errors)
    
    def test_invalid_right(self):
        """Invalid right (not P or C) should be caught."""
        intent = {
            "symbol": "SPY",
            "expiry": "20260320",
            "type": "PUT_SPREAD",
            "legs": [
                {"action": "BUY", "right": "X", "strike": 590}  # Invalid right
            ],
            "qty": 1,
            "limit": {"start": 0.35, "max": 0.36}
        }
        
        is_valid, errors = validate_intent_schema(intent)
        assert not is_valid
        assert any("P or C" in err for err in errors)
