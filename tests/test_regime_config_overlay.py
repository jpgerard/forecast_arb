"""
Test regime config overlay system.

PR1 Acceptance Tests:
- Config without regimes section behaves like crash-only (backward compat)
- Config with regimes section applies overlays correctly
- Regime overlay doesn't mutate original config
"""

import pytest
from forecast_arb.core.regime import apply_regime_overrides, get_regime_config


def test_backward_compatibility_no_regimes_section():
    """Config without regimes section returns unchanged."""
    base_config = {
        "campaign_name": "crash_venture_v1",
        "edge_gating": {
            "event_moneyness": -0.15,
            "min_edge": 0.05
        }
    }
    
    # Apply override for crash regime
    result = apply_regime_overrides(base_config, "crash")
    
    # Should be unchanged (no regimes section)
    assert result["edge_gating"]["event_moneyness"] == -0.15
    assert result == base_config  # Exactly the same


def test_backward_compatibility_no_mutation():
    """Applying override doesn't mutate original config."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {
            "crash": {"moneyness": -0.15},
            "selloff": {"moneyness": -0.09}
        }
    }
    
    original_moneyness = base_config["edge_gating"]["event_moneyness"]
    
    # Apply selloff override
    result = apply_regime_overrides(base_config, "selloff")
    
    # Original config should be unchanged
    assert base_config["edge_gating"]["event_moneyness"] == original_moneyness
    assert base_config["edge_gating"]["event_moneyness"] == -0.15
    
    # Result should have override applied
    assert result["edge_gating"]["event_moneyness"] == -0.09


def test_crash_regime_overlay():
    """Crash regime overlay applies moneyness correctly."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.10  # Different from crash default
        },
        "regimes": {
            "crash": {
                "moneyness": -0.15,
                "min_otm_boundary": -0.13
            }
        }
    }
    
    result = apply_regime_overrides(base_config, "crash")
    
    # Moneyness should be overridden
    assert result["edge_gating"]["event_moneyness"] == -0.15
    
    # Optional fields should be added
    assert result["edge_gating"]["min_otm_boundary"] == -0.13


def test_selloff_regime_overlay():
    """Selloff regime overlay applies moneyness correctly."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15  # Crash default
        },
        "regimes": {
            "selloff": {
                "moneyness": -0.09,
                "otm_bounds": [-0.07, -0.12]
            }
        }
    }
    
    result = apply_regime_overrides(base_config, "selloff")
    
    # Moneyness should be overridden to selloff value
    assert result["edge_gating"]["event_moneyness"] == -0.09
    
    # OTM bounds should be added
    assert result["edge_gating"]["otm_bounds"] == [-0.07, -0.12]


def test_missing_regime_in_overlay():
    """Requesting regime not in config returns base config."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {
            "crash": {"moneyness": -0.15}
            # selloff not defined
        }
    }
    
    # Request selloff (not in config)
    result = apply_regime_overrides(base_config, "selloff")
    
    # Should return base config unchanged (except deep copy)
    assert result["edge_gating"]["event_moneyness"] == -0.15


def test_empty_regimes_section():
    """Empty regimes section returns base config."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {}
    }
    
    result = apply_regime_overrides(base_config, "crash")
    
    # Should return base config unchanged
    assert result["edge_gating"]["event_moneyness"] == -0.15


def test_get_regime_config_defaults():
    """get_regime_config returns sensible defaults."""
    crash_defaults = get_regime_config("crash")
    assert crash_defaults["moneyness"] == -0.15
    assert crash_defaults["min_otm_boundary"] == -0.13
    assert crash_defaults["selector_p_threshold"] == 0.015
    
    selloff_defaults = get_regime_config("selloff")
    assert selloff_defaults["moneyness"] == -0.09
    assert selloff_defaults["otm_bounds"] == [-0.07, -0.12]
    assert selloff_defaults["selector_p_min"] == 0.08
    assert selloff_defaults["selector_p_max"] == 0.25


def test_unknown_regime_defaults():
    """get_regime_config returns empty dict for unknown regime."""
    result = get_regime_config("unknown_regime")
    assert result == {}


def test_multiple_overrides():
    """Multiple overrides can be applied in sequence."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "edge_gating": {
            "event_moneyness": -0.15
        },
        "regimes": {
            "crash": {"moneyness": -0.15},
            "selloff": {"moneyness": -0.09}
        }
    }
    
    # Apply crash override
    crash_cfg = apply_regime_overrides(base_config, "crash")
    assert crash_cfg["edge_gating"]["event_moneyness"] == -0.15
    
    # Apply selloff override (from same base)
    selloff_cfg = apply_regime_overrides(base_config, "selloff")
    assert selloff_cfg["edge_gating"]["event_moneyness"] == -0.09
    
    # Crash config should be unchanged
    assert crash_cfg["edge_gating"]["event_moneyness"] == -0.15


def test_edge_gating_section_created_if_missing():
    """If edge_gating section missing, it's created when applying overlay."""
    base_config = {
        "campaign_name": "crash_venture_v2",
        "regimes": {
            "selloff": {"moneyness": -0.09}
        }
    }
    
    result = apply_regime_overrides(base_config, "selloff")
    
    # edge_gating section should be created
    assert "edge_gating" in result
    assert result["edge_gating"]["event_moneyness"] == -0.09
