"""
RegimeResult - Standardized Output Structure

Provides a unified return type for regime-based structuring runs,
preventing downstream code from branching per regime.
"""

from dataclasses import dataclass
from typing import List, Dict, Any, Optional


@dataclass
class RegimeResult:
    """
    Standardized result from running a regime.
    
    This prevents branching proliferation in review pack rendering,
    intent building, and artifact writing. All regimes return the
    same structure, consumed uniformly downstream.
    
    Attributes:
        regime: Regime identifier ("crash" or "selloff")
        event_spec: EventSpec dict (single source of truth)
        event_hash: Unique hash for this event
        p_implied: Options-implied probability for this event
        p_implied_confidence: Confidence in p_implied calculation
        p_implied_warnings: Warnings from p_implied calculation
        p_event_external: External probability block with full provenance (regime-level authoritative)
        candidates: List of candidate structures (ranked)
        filtered_out: List of filtered candidate diagnostics
        expiry_used: Expiry date selected (YYYYMMDD)
        expiry_selection_reason: Why this expiry was chosen
        representable: Whether event is representable on Kalshi/market
        warnings: General warnings for this regime
        run_id: Run identifier
        manifest: Run manifest metadata
    """
    regime: str
    event_spec: Dict[str, Any]
    event_hash: str
    p_implied: Optional[float]
    p_implied_confidence: float
    p_implied_warnings: List[str]
    candidates: List[Dict[str, Any]]
    filtered_out: List[Dict[str, Any]]
    expiry_used: str
    expiry_selection_reason: str
    representable: bool
    warnings: List[str]
    run_id: str
    manifest: Dict[str, Any]
    # p_event_external added in a later patch — optional with default None for backward-compat
    # (must appear after all required fields in the dataclass definition)
    p_event_external: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "regime": self.regime,
            "event_spec": self.event_spec,
            "event_hash": self.event_hash,
            "p_implied": self.p_implied,
            "p_implied_confidence": self.p_implied_confidence,
            "p_implied_warnings": self.p_implied_warnings,
            "p_event_external": self.p_event_external,
            "candidates": self.candidates,
            "filtered_out": self.filtered_out,
            "expiry_used": self.expiry_used,
            "expiry_selection_reason": self.expiry_selection_reason,
            "representable": self.representable,
            "warnings": self.warnings,
            "run_id": self.run_id,
            "manifest": self.manifest
        }
        
        # Enrich candidates with p_event_external reference
        if self.p_event_external and self.candidates:
            for candidate in result["candidates"]:
                candidate["p_event_external_ref"] = {
                    "regime": self.regime,
                    "asof_ts_utc": self.p_event_external.get("asof_ts_utc"),
                    "source": self.p_event_external.get("source"),
                    "authoritative": self.p_event_external.get("authoritative"),
                    # Patch C — semantic evidence fields
                    "evidence_class": self.p_event_external.get("evidence_class"),
                    "authoritative_capable": self.p_event_external.get("authoritative_capable", False),
                    "p_external_role": self.p_event_external.get("p_external_role"),
                }
                candidate["p_event_external_p"] = self.p_event_external.get("p")
        
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RegimeResult":
        """Create from dictionary."""
        return cls(**data)
    
    def has_candidates(self) -> bool:
        """Check if any candidates were generated."""
        return len(self.candidates) > 0
    
    def get_top_candidate(self) -> Optional[Dict[str, Any]]:
        """Get top-ranked candidate, if any."""
        if not self.candidates:
            return None
        
        # Find candidate with rank=1
        for cand in self.candidates:
            if cand.get("rank") == 1:
                return cand
        
        # Fallback to first candidate
        return self.candidates[0]
    
    def get_candidate_by_rank(self, rank: int) -> Optional[Dict[str, Any]]:
        """Get candidate by rank number."""
        for cand in self.candidates:
            if cand.get("rank") == rank:
                return cand
        return None
    
    def summary_line(self) -> str:
        """One-line summary of this regime result."""
        n_cands = len(self.candidates)
        status = "REPRESENTABLE" if self.representable else "NOT_REPRESENTABLE"
        
        if n_cands == 0:
            return f"{self.regime}: NO_CANDIDATES | {status} | {self.expiry_selection_reason}"
        else:
            p_impl_str = f"{self.p_implied:.3f}" if self.p_implied is not None else "N/A"
            return f"{self.regime}: {n_cands} candidates | p_implied={p_impl_str} | {status} | {self.expiry_selection_reason}"


def create_regime_result(
    regime: str,
    engine_output: Dict[str, Any],
    expiry_selection_reason: str,
    representable: bool = False,
    p_implied: Optional[float] = None,
    p_implied_confidence: float = 0.0,
    p_implied_warnings: Optional[List[str]] = None,
    p_event_external: Optional[Dict[str, Any]] = None
) -> RegimeResult:
    """
    Create RegimeResult from engine output.
    
    This is a helper to wrap existing engine outputs into the
    standardized RegimeResult structure.
    
    Args:
        regime: Regime identifier
        engine_output: Output dict from run_crash_venture_v1_snapshot()
        expiry_selection_reason: Reason expiry was selected
        representable: Whether event is representable
        p_implied: Options-implied probability (optional)
        p_implied_confidence: Confidence in p_implied
        p_implied_warnings: Warnings from p_implied calculation
        p_event_external: External probability block with full provenance (optional)
        
    Returns:
        RegimeResult wrapping the engine output
    """
    return RegimeResult(
        regime=regime,
        event_spec=engine_output.get("event_spec", {}),
        event_hash=engine_output.get("event_hash", ""),
        p_implied=p_implied,
        p_implied_confidence=p_implied_confidence,
        p_implied_warnings=p_implied_warnings or [],
        p_event_external=p_event_external,
        candidates=engine_output.get("top_structures", []),
        filtered_out=engine_output.get("filtered_out", []),
        expiry_used=engine_output.get("expiry_used", ""),
        expiry_selection_reason=expiry_selection_reason,
        representable=representable,
        warnings=engine_output.get("warnings", []),
        run_id=engine_output.get("run_id", ""),
        manifest=engine_output.get("manifest", {})
    )
