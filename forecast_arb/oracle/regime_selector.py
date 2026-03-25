"""
Regime Selector for Crash Venture v2

Determines which regime(s) are eligible for structuring each day:
- CRASH regime: rare, convex, lottery hedge (≤ -15% moneyness)
- SELLOFF regime: tactical downside (≤ -8% to -10% moneyness)

Decision is based on observable market conditions to prevent drift
and ensure crash sleeve remains a "crash" hedge.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum


logger = logging.getLogger(__name__)


class RegimeMode(Enum):
    """Regime selector output modes."""
    CRASH_ONLY = "CRASH_ONLY"
    SELLOFF_ONLY = "SELLOFF_ONLY"
    BOTH = "BOTH"
    STAND_DOWN = "STAND_DOWN"


@dataclass
class RegimeDecision:
    """
    Output from regime selector.
    
    Attributes:
        regime_mode: What regime(s) to enable
        eligible_regimes: List of eligible regime names
        reasons: Dict of regime -> reason
        metrics: Observable metrics used in decision
        confidence: Confidence score (0.0-1.0) based on data quality
        timestamp_utc: Decision timestamp
    """
    regime_mode: RegimeMode
    eligible_regimes: List[str]
    reasons: Dict[str, str]
    metrics: Dict[str, Optional[float]]
    confidence: float
    timestamp_utc: str
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "regime_mode": self.regime_mode.value,
            "eligible_regimes": self.eligible_regimes,
            "reasons": self.reasons,
            "metrics": self.metrics,
            "confidence": self.confidence,
            "timestamp_utc": self.timestamp_utc
        }


class RegimeSelector:
    """
    Deterministic regime selector based on observable market conditions.
    
    Rules (v1 - simple, transparent):
    
    A) CRASH eligibility:
       - Enable if p_implied_crash <= 1.5% (crash not already priced)
       - OR (optional) drawdown >= 5% and skew elevated
       - Otherwise: crash already priced, do not generate
    
    B) SELLOFF eligibility:
       - Enable if 0.08 <= p_implied_selloff <= 0.25 (normal band)
       - Flag PRICED_IN_WARNING if very high but allow
    
    C) STAND_DOWN:
       - If neither eligible
    
    D) BOTH:
       - If both eligible
    """
    
    def __init__(
        self,
        crash_p_threshold: float = 0.015,
        selloff_p_min: float = 0.08,
        selloff_p_max: float = 0.25,
        drawdown_threshold: float = 0.05,
        min_skew_threshold: Optional[float] = None
    ):
        """
        Initialize regime selector.
        
        Args:
            crash_p_threshold: Max p_implied for crash eligibility (default 1.5%)
            selloff_p_min: Min p_implied for selloff eligibility (default 8%)
            selloff_p_max: Max p_implied for selloff eligibility (default 25%)
            drawdown_threshold: Drawdown threshold for crash override (default 5%)
            min_skew_threshold: Minimum skew for crash override (optional)
        """
        self.crash_p_threshold = crash_p_threshold
        self.selloff_p_min = selloff_p_min
        self.selloff_p_max = selloff_p_max
        self.drawdown_threshold = drawdown_threshold
        self.min_skew_threshold = min_skew_threshold
        
        logger.info(f"RegimeSelector initialized:")
        logger.info(f"  crash_p_threshold: {crash_p_threshold:.3f}")
        logger.info(f"  selloff_p_band: [{selloff_p_min:.3f}, {selloff_p_max:.3f}]")
        logger.info(f"  drawdown_threshold: {drawdown_threshold:.2%}")
    
    def select_regime(
        self,
        p_implied_crash: Optional[float],
        p_implied_selloff: Optional[float],
        representable_crash: bool = True,
        representable_selloff: bool = True,
        drawdown: Optional[float] = None,
        skew: Optional[float] = None,
        timestamp_utc: Optional[str] = None
    ) -> RegimeDecision:
        """
        Select eligible regime(s) based on market conditions.
        
        Args:
            p_implied_crash: Implied probability of crash event (-15%)
            p_implied_selloff: Implied probability of selloff event (-9%)
            representable_crash: Whether crash event is representable on Kalshi (default True)
            representable_selloff: Whether selloff event is representable on Kalshi (default True)
            drawdown: Recent drawdown from 20-day high (optional)
            skew: Skew metric (e.g., 25Δ put IV - ATM IV) (optional)
            timestamp_utc: Timestamp for decision
            
        Returns:
            RegimeDecision with selected regime(s)
        """
        from datetime import datetime, timezone
        
        if timestamp_utc is None:
            timestamp_utc = datetime.now(timezone.utc).isoformat()
        
        reasons_dict = {}
        eligible_regimes = []
        confidence = 1.0  # Start at full confidence
        
        metrics = {
            "p_implied_crash": p_implied_crash,
            "p_implied_selloff": p_implied_selloff,
            "representable_crash": representable_crash,
            "representable_selloff": representable_selloff,
            "drawdown": drawdown,
            "skew": skew,
            "crash_p_threshold": self.crash_p_threshold,
            "selloff_p_min": self.selloff_p_min,
            "selloff_p_max": self.selloff_p_max
        }
        
        # Check for missing inputs - degrade gracefully
        if p_implied_crash is None and p_implied_selloff is None:
            logger.warning("Both p_implied_crash and p_implied_selloff are None - STAND_DOWN")
            return RegimeDecision(
                regime_mode=RegimeMode.STAND_DOWN,
                eligible_regimes=[],
                reasons={"STAND_DOWN": "MISSING_INPUTS: Both p_implied values unavailable"},
                metrics=metrics,
                confidence=0.0,
                timestamp_utc=timestamp_utc
            )
        
        # A) Check CRASH eligibility
        crash_eligible = False
        
        if p_implied_crash is None:
            reasons_dict["crash"] = "CRASH_SKIP: p_implied_crash unavailable"
        elif p_implied_crash <= self.crash_p_threshold:
            crash_eligible = True
            reason = f"ELIGIBLE: p_implied={p_implied_crash:.3f} <= {self.crash_p_threshold:.3f}"
            if not representable_crash:
                reason += " (NOT_REPRESENTABLE)"
                confidence *= 0.5  # Reduce confidence if not representable
            reasons_dict["crash"] = reason
        else:
            # Check drawdown override (optional)
            if (drawdown is not None and 
                drawdown >= self.drawdown_threshold and
                self.min_skew_threshold is not None and
                skew is not None and
                skew >= self.min_skew_threshold):
                crash_eligible = True
                reason = f"ELIGIBLE: drawdown={drawdown:.2%} >= {self.drawdown_threshold:.2%}"
                if not representable_crash:
                    reason += " (NOT_REPRESENTABLE)"
                    confidence *= 0.5
                reasons_dict["crash"] = reason
            else:
                reasons_dict["crash"] = f"INELIGIBLE: p_implied={p_implied_crash:.3f} > {self.crash_p_threshold:.3f} (already priced)"
        
        # B) Check SELLOFF eligibility
        selloff_eligible = False
        
        if p_implied_selloff is None:
            reasons_dict["selloff"] = "SELLOFF_SKIP: p_implied_selloff unavailable"
        elif self.selloff_p_min <= p_implied_selloff <= self.selloff_p_max:
            selloff_eligible = True
            reason = f"ELIGIBLE: p_implied={p_implied_selloff:.3f} in [{self.selloff_p_min:.3f}, {self.selloff_p_max:.3f}]"
            if not representable_selloff:
                reason += " (NOT_REPRESENTABLE)"
                confidence *= 0.5
            reasons_dict["selloff"] = reason
        elif p_implied_selloff > self.selloff_p_max:
            # Allow but flag warning
            selloff_eligible = True
            reason = f"ELIGIBLE_PRICED_IN_WARNING: p_implied={p_implied_selloff:.3f} > {self.selloff_p_max:.3f}"
            if not representable_selloff:
                reason += " (NOT_REPRESENTABLE)"
                confidence *= 0.5
            reasons_dict["selloff"] = reason
        else:
            # p_implied_selloff < selloff_p_min
            reasons_dict["selloff"] = f"INELIGIBLE: p_implied={p_implied_selloff:.3f} < {self.selloff_p_min:.3f} (too cheap)"
        
        # Build eligible_regimes list
        if crash_eligible:
            eligible_regimes.append("crash")
        if selloff_eligible:
            eligible_regimes.append("selloff")
        
        # C & D) Determine final mode
        if crash_eligible and selloff_eligible:
            regime_mode = RegimeMode.BOTH
        elif crash_eligible:
            regime_mode = RegimeMode.CRASH_ONLY
        elif selloff_eligible:
            regime_mode = RegimeMode.SELLOFF_ONLY
        else:
            regime_mode = RegimeMode.STAND_DOWN
        
        decision = RegimeDecision(
            regime_mode=regime_mode,
            eligible_regimes=eligible_regimes,
            reasons=reasons_dict,
            metrics=metrics,
            confidence=confidence,
            timestamp_utc=timestamp_utc
        )
        
        logger.info(f"Regime Decision: {regime_mode.value} (confidence={confidence:.2f})")
        for regime, reason in reasons_dict.items():
            logger.info(f"  {regime}: {reason}")
        
        return decision


def create_regime_selector(config: Optional[Dict] = None) -> RegimeSelector:
    """
    Factory function to create RegimeSelector from config.
    
    Args:
        config: Optional config dict with regime_selector section
        
    Returns:
        RegimeSelector instance
    """
    if config is None:
        config = {}
    
    regime_config = config.get("regime_selector", {})
    
    return RegimeSelector(
        crash_p_threshold=regime_config.get("crash_p_threshold", 0.015),
        selloff_p_min=regime_config.get("selloff_p_min", 0.08),
        selloff_p_max=regime_config.get("selloff_p_max", 0.25),
        drawdown_threshold=regime_config.get("drawdown_threshold", 0.05),
        min_skew_threshold=regime_config.get("min_skew_threshold", None)
    )
