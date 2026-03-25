"""
Tests for IV sourcing helper (get_atm_iv).
"""

import pytest
from forecast_arb.options.iv_source import get_atm_iv


def test_iv_from_near_atm_call_with_quotes():
    """Test IV inferred from near-ATM call with valid bid/ask."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 695.0,
                        "implied_vol": 0.18,
                        "bid": 12.0,
                        "ask": 12.5
                    },
                    {
                        "strike": 705.0,
                        "implied_vol": 0.19,
                        "bid": 8.0,
                        "ask": 8.3
                    }
                ],
                "puts": []
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv is not None
    assert 0.01 < iv < 2.0
    assert source == "iv_inferred_atm"
    assert iv in [0.18, 0.19]  # Should be one of the two available IVs


def test_iv_from_near_atm_put_with_quotes():
    """Test IV inferred from near-ATM put with valid bid/ask."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
       "expiries": {
            "20260306": {
                "calls": [],
                "puts": [
                    {
                        "strike": 695.0,
                        "implied_vol": 0.17,
                        "bid": 5.0,
                        "ask": 5.2
                    },
                    {
                        "strike": 705.0,
                        "implied_vol": 0.16,
                        "bid": 8.5,
                        "ask": 8.9
                    }
                ]
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv is not None
    assert source == "iv_inferred_atm"
    assert iv in [0.16, 0.17]


def test_no_iv_source_when_no_quotes():
    """Test returns None when options have IV but no executable quotes."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 695.0,
                        "implied_vol": 0.18,
                        "bid": None,
                        "ask": None
                    }
                ],
                "puts": [
                    {
                        "strike": 705.0,
                        "implied_vol": 0.17,
                        "bid": None,
                        "ask": None
                    }
                ]
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv is None
    assert source == "NO_IV_SOURCE"
    assert len(warnings) > 0
    assert any("NO_IV_SOURCE" in w for w in warnings)


def test_no_iv_source_when_iv_missing():
    """Test returns None when options have quotes but no IV."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 695.0,
                        "implied_vol": None,
                        "bid": 12.0,
                        "ask": 12.5
                    }
                ],
                "puts": []
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv is None
    assert source == "NO_IV_SOURCE"


def test_prefers_tighter_spread():
    """Test prefers option with tighter bid/ask spread."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 695.0,  # 5 from ATM
                        "implied_vol": 0.18,
                        "bid": 10.0,
                        "ask": 15.0  # 50% spread
                    },
                    {
                        "strike": 705.0,  # 5 from ATM
                        "implied_vol": 0.20,
                        "bid": 8.0,
                        "ask": 8.2  # 2.5% spread - much tighter
                    }
                ],
                "puts": []
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv == 0.20  # Should pick the one with tighter spread


def test_prefers_closer_to_atm():
    """Test prefers option closer to ATM when spreads are similar."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 698.0,  # 2 from ATM
                        "implied_vol": 0.18,
                        "bid": 10.0,
                        "ask": 10.5  # 5% spread
                    },
                    {
                        "strike": 710.0,  # 10 from ATM
                        "implied_vol": 0.22,
                        "bid": 5.0,
                        "ask": 5.2  # 4% spread (slightly tighter)
                    }
                ],
                "puts": []
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    # Due to binary sorting (distance first, then spread quality second),
    # should pick the closer one
    assert iv == 0.18


def test_snapshot_atm_iv_preferred():
    """Test snapshot-level ATM IV is preferred if present (future compatibility)."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0,
            "atm_iv": 0.25  # If this field exists, it should be used
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 700.0,
                        "implied_vol": 0.18,
                        "bid": 10.0,
                        "ask": 10.5
                    }
                ],
                "puts": []
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv == 0.25
    assert source == "snapshot_atm_iv"


def test_rejects_invalid_iv_values():
    """Test filters out invalid IV values (too low or too high)."""
    snapshot = {
        "snapshot_metadata": {
            "current_price": 700.0
        },
        "expiries": {
            "20260306": {
                "calls": [
                    {
                        "strike": 695.0,
                        "implied_vol": 0.005,  # Too low (<1%)
                        "bid": 10.0,
                        "ask": 10.5
                    },
                    {
                        "strike": 705.0,
                        "implied_vol": 3.0,  # Too high (>200%)
                        "bid": 8.0,
                        "ask": 8.3
                    },
                    {
                        "strike": 700.0,
                        "implied_vol": 0.19,  # Valid
                        "bid": 9.0,
                        "ask": 9.2
                    }
                ],
                "puts": []
            }
        }
    }
    
    iv, source, warnings = get_atm_iv(snapshot, "20260306", 700.0)
    
    assert iv == 0.19  # Should only use the valid one
    assert source == "iv_inferred_atm"
