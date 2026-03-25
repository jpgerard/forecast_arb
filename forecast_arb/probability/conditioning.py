"""
Probability conditioning engine.

Applies regime-aware multipliers to base crash probability before EV computation.

Multipliers are bounded and simple:
- No machine learning
- No regression fitting
- Fully explainable
- Stability guarantees via hard bounds
"""

import logging
from typing import Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ConditioningConfig:
    """Configuration for probability conditioning."""
    
    # VIX percentile thresholds
    vix_low_threshold: float = 0.2
    vix_high_threshold: float = 0.8
    
    # Skew percentile thresholds
    skew_low_threshold: float = 0.2
    skew_high_threshold: float = 0.8
    
    # Credit percentile thresholds
    credit_low_threshold: float = 0.2
    credit_high_threshold: float = 0.8
    
    # Multipliers
    vol_low_mult: float = 0.85      # Calm VIX
    vol_neutral_mult: float = 1.0   # Normal VIX
    vol_high_mult: float = 1.20     # Stressed VIX
    
    skew_low_mult: float = 0.90     # Cheap skew
    skew_neutral_mult: float = 1.0  # Normal skew
    skew_high_mult: float = 1.15    # Expensive skew
    
    credit_low_mult: float = 0.90   # Calm credit
    credit_neutral_mult: float = 1.0  # Normal credit
    credit_high_mult: float = 1.25  # Stressed credit
    
    # Hard bounds for final adjustment
    min_multiplier: float = 0.25    # p_adj >= 0.25 * base_p
    max_multiplier: float = 3.0     # p_adj <= 3.0 * base_p
    absolute_cap: float = 0.35      # p_adj <= 0.35 (absolute crash prob cap)


def compute_vol_multiplier(vix_pct: Optional[float], config: ConditioningConfig) -> float:
    """
    Compute volatility regime multiplier.
    
    Args:
        vix_pct: VIX percentile [0, 1], or None
        config: Conditioning config
        
    Returns:
        Multiplier in [vol_low_mult, vol_high_mult]
    """
    if vix_pct is None:
        return config.vol_neutral_mult
    
    if vix_pct < config.vix_low_threshold:
        return config.vol_low_mult
    elif vix_pct > config.vix_high_threshold:
        return config.vol_high_mult
    else:
        return config.vol_neutral_mult


def compute_skew_multiplier(skew_pct: Optional[float], config: ConditioningConfig) -> float:
    """
    Compute skew regime multiplier.
    
    Args:
        skew_pct: Skew percentile [0, 1], or None
        config: Conditioning config
        
    Returns:
        Multiplier in [skew_low_mult, skew_high_mult]
    """
    if skew_pct is None:
        return config.skew_neutral_mult
    
    if skew_pct < config.skew_low_threshold:
        return config.skew_low_mult
    elif skew_pct > config.skew_high_threshold:
        return config.skew_high_mult
    else:
        return config.skew_neutral_mult


def compute_credit_multiplier(credit_pct: Optional[float], config: ConditioningConfig) -> float:
    """
    Compute credit regime multiplier.
    
    Args:
        credit_pct: Credit spread percentile [0, 1], or None
        config: Conditioning config
        
    Returns:
        Multiplier in [credit_low_mult, credit_high_mult]
    """
    if credit_pct is None:
        return config.credit_neutral_mult
    
    if credit_pct < config.credit_low_threshold:
        return config.credit_low_mult
    elif credit_pct > config.credit_high_threshold:
        return config.credit_high_mult
    else:
        return config.credit_neutral_mult


def compute_confidence_score(
    vix_pct: Optional[float],
    skew_pct: Optional[float],
    credit_pct: Optional[float]
) -> float:
    """
    Compute confidence score based on signal availability.
    
    Each available signal contributes 0.33 to confidence.
    
    Args:
        vix_pct: VIX percentile (None = unavailable)
        skew_pct: Skew percentile (None = unavailable)
        credit_pct: Credit percentile (None = unavailable)
        
    Returns:
        Confidence score [0, 1]
    """
    score = 0.0
    
    if vix_pct is not None:
        score += 0.33
    
    if skew_pct is not None:
        score += 0.33
    
    if credit_pct is not None:
        score += 0.33
    
    # Round to avoid floating point oddities (0.99 instead of 1.0)
    return round(score, 2)


def adjust_crash_probability(
    base_p: float,
    vix_pct: Optional[float],
    skew_pct: Optional[float],
    credit_pct: Optional[float],
    config: Optional[ConditioningConfig] = None
) -> Dict:
    """
    Adjust crash probability based on regime signals.
    
    Multiplies base probability by vol, skew, and credit multipliers.
    Applies hard bounds to prevent instability.
    
    Args:
        base_p: Base crash probability (implied from options surface)
        vix_pct: VIX percentile [0, 1], or None
        skew_pct: Skew percentile [0, 1], or None
        credit_pct: Credit percentile [0, 1], or None
        config: Conditioning config (uses defaults if None)
        
    Returns:
        Dict with:
            - p_adjusted: Adjusted probability
            - multipliers: Dict of vol, skew, credit multipliers
            - confidence_score: Data availability score [0, 1]
            - p_source: "conditioned" if any multiplier != 1, else "base"
    """
    if config is None:
        config = ConditioningConfig()
    
    # Validate base_p
    if base_p <= 0 or base_p >= 1:
        raise ValueError(f"base_p must be in (0, 1), got {base_p}")
    
    # Compute individual multipliers
    vol_mult = compute_vol_multiplier(vix_pct, config)
    skew_mult = compute_skew_multiplier(skew_pct, config)
    credit_mult = compute_credit_multiplier(credit_pct, config)
    
    # Combine multiplicatively
    combined_mult = vol_mult * skew_mult * credit_mult
    
    # Apply to base probability
    p_adjusted = base_p * combined_mult
    
    # Apply hard bounds
    # 1. Relative bounds (0.25x to 3x base_p)
    p_adjusted = max(p_adjusted, base_p * config.min_multiplier)
    p_adjusted = min(p_adjusted, base_p * config.max_multiplier)
    
    # 2. Absolute cap (crash prob should never exceed 35%)
    p_adjusted = min(p_adjusted, config.absolute_cap)
    
    # 3. Ensure in valid probability range
    p_adjusted = max(0.001, min(0.999, p_adjusted))
    
    # Compute confidence
    confidence = compute_confidence_score(vix_pct, skew_pct, credit_pct)
    
    # Determine source
    any_adjustment = (vol_mult != 1.0 or skew_mult != 1.0 or credit_mult != 1.0)
    p_source = "conditioned" if any_adjustment else "base"
    
    # Log conditioning application
    if any_adjustment:
        logger.info(
            f"Probability conditioning: {base_p:.4f} → {p_adjusted:.4f} "
            f"(vol={vol_mult:.2f}, skew={skew_mult:.2f}, credit={credit_mult:.2f}, "
            f"combined={combined_mult:.2f}, confidence={confidence:.2f})"
        )
    else:
        logger.info(
            f"Probability conditioning: {base_p:.4f} (no adjustment, confidence={confidence:.2f})"
        )
    
    return {
        "p_adjusted": p_adjusted,
        "multipliers": {
            "vol": vol_mult,
            "skew": skew_mult,
            "credit": credit_mult,
            "combined": combined_mult
        },
        "confidence_score": confidence,
        "p_source": p_source,
        "regime_signals": {
            "vix_pct": vix_pct,
            "skew_pct": skew_pct,
            "credit_pct": credit_pct
        }
    }
