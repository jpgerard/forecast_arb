"""Test campaign_name validation in crash_venture_v1_snapshot."""

import pytest
import tempfile
from pathlib import Path
import yaml

from forecast_arb.engine.crash_venture_v1_snapshot import load_frozen_config


def test_campaign_name_exact_match():
    """Test that exact 'crash_venture_v1' is accepted."""
    config = {
        "campaign_name": "crash_venture_v1",
        "structuring": {
            "underlier": "SPY"
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config, f)
        config_path = f.name
    
    try:
        loaded = load_frozen_config(config_path)
        assert loaded["campaign_name"] == "crash_venture_v1"
    finally:
        Path(config_path).unlink()


def test_campaign_name_versioned():
    """Test that versioned names like 'crash_venture_v1_1' are accepted."""
    test_cases = [
        "crash_venture_v1_1",
        "crash_venture_v1_2",
        "crash_venture_v1_test",
        "crash_venture_v1_alpha"
    ]
    
    for campaign_name in test_cases:
        config = {
            "campaign_name": campaign_name,
            "structuring": {
                "underlier": "SPY"
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config, f)
            config_path = f.name
        
        try:
            loaded = load_frozen_config(config_path)
            assert loaded["campaign_name"] == campaign_name
        finally:
            Path(config_path).unlink()


def test_campaign_name_missing():
    """Test that missing campaign_name raises clear error."""
    config = {
        "structuring": {
            "underlier": "SPY"
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(config, f)
        config_path = f.name
    
    try:
        with pytest.raises(ValueError) as exc_info:
            load_frozen_config(config_path)
        
        assert "campaign_name" in str(exc_info.value).lower()
        assert "crash_venture_v1" in str(exc_info.value)
    finally:
        Path(config_path).unlink()


def test_campaign_name_invalid():
    """Test that invalid campaign names are rejected."""
    invalid_names = [
        "wrong_campaign",
        "crash_venture_v2",
        "crash_venture",
        "crash_venture_v1x",  # Doesn't start with "crash_venture_v1_"
    ]
    
    for campaign_name in invalid_names:
        config = {
            "campaign_name": campaign_name,
            "structuring": {
                "underlier": "SPY"
            }
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config, f)
            config_path = f.name
        
        try:
            with pytest.raises(ValueError) as exc_info:
                load_frozen_config(config_path)
            
            assert "crash_venture_v1" in str(exc_info.value)
            assert campaign_name in str(exc_info.value)
        finally:
            Path(config_path).unlink()
