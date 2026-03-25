"""
Regime Types and Configuration Overlay

Defines regime types and provides config overlay logic for multi-regime system.
"""

from typing import Literal, Dict, Any
from copy import deepcopy


# Regime types
Regime = Literal["crash", "selloff"]

# Regime selector output modes
RegimeMode = Literal[
    "AUTO",
    "CRASH_ONLY",
    "SELLOFF_ONLY",
    "BOTH",
    "STAND_DOWN"
]


def apply_regime_overrides(base_cfg: Dict[str, Any], regime: str) -> Dict[str, Any]:
    """
    Apply regime-specific overrides to base config.
    
    This allows a single config YAML to support multiple regimes without duplication.
    
    Args:
        base_cfg: Base configuration dict
        regime: Regime name ("crash" or "selloff")
        
    Returns:
        Config dict with regime-specific overrides applied
        
    Example:
        Base config:
        ```yaml
        campaign_name: crash_venture_v2
        edge_gating:
          event_moneyness: -0.15  # Default
        
        regimes:
          crash:
            moneyness: -0.15
          selloff:
            moneyness: -0.09
        ```
        
        After override for "selloff":
        ```python
        cfg = apply_regime_overrides(base_cfg, "selloff")
        # cfg["edge_gating"]["event_moneyness"] == -0.09
        ```
    """
    # Deep copy to avoid mutating original config
    cfg = deepcopy(base_cfg)
    
    # If no regimes section, return as-is (backward compatibility)
    if "regimes" not in cfg:
        return cfg
    
    # Get regime-specific overlay
    regime_overlay = cfg.get("regimes", {}).get(regime, {})
    
    if not regime_overlay:
        # No overlay for this regime - use base config
        return cfg
    
    # Apply moneyness override (most common override)
    if "moneyness" in regime_overlay:
        # Ensure edge_gating section exists
        if "edge_gating" not in cfg:
            cfg["edge_gating"] = {}
        
        cfg["edge_gating"]["event_moneyness"] = regime_overlay["moneyness"]
    
    # Apply min_otm_boundary override (optional guardrail)
    if "min_otm_boundary" in regime_overlay:
        if "edge_gating" not in cfg:
            cfg["edge_gating"] = {}
        
        cfg["edge_gating"]["min_otm_boundary"] = regime_overlay["min_otm_boundary"]
    
    # Apply otm_bounds override (optional guardrail for selloff)
    if "otm_bounds" in regime_overlay:
        if "edge_gating" not in cfg:
            cfg["edge_gating"] = {}
        
        cfg["edge_gating"]["otm_bounds"] = regime_overlay["otm_bounds"]
    
    # Future-proof: Can add more overrides here as needed
    # Examples:
    # - spread_widths override
    # - dte_range override
    # - objective override
    
    return cfg


def get_regime_config(regime: str) -> Dict[str, Any]:
    """
    Get default configuration for a regime.
    
    This provides sensible defaults when no config overlay is specified.
    
    Args:
        regime: Regime name ("crash" or "selloff")
        
    Returns:
        Default config overlay for regime
    """
    defaults = {
        "crash": {
            "moneyness": -0.15,
            "min_otm_boundary": -0.13,
            "selector_p_threshold": 0.015  # 1.5% max
        },
        "selloff": {
            "moneyness": -0.09,
            "otm_bounds": [-0.07, -0.12],
            "selector_p_min": 0.08,
            "selector_p_max": 0.25
        }
    }
    
    return defaults.get(regime, {})
