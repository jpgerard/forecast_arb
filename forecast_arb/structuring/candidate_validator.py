"""
Candidate Validation for Multi-Regime System

Validates that candidates match their regime's expected parameters.
Critical for preventing "wrong trade ticket"  scenarios where selloff
candidates use crash strikes/moneyness.
"""

import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class CandidateRegimeMismatchError(Exception):
    """Raised when candidate doesn't match expected regime parameters."""
    pass


def validate_candidate_regime(
    candidate: Dict,
    regime: str,
    expected_moneyness: float,
    tolerance: float = 0.001
) -> None:
    """
    Validate that candidate matches regime parameters.
    
    Args:
        candidate: Candidate structure dict
        regime: Expected regime ("crash" or "selloff")
        expected_moneyness: Expected moneyness for this regime
        tolerance: Tolerance for float comparison (default 0.1%)
        
    Raises:
        CandidateRegimeMismatchError: If validation fails
    """
    # Check regime field
    candidate_regime = candidate.get("regime")
    if candidate_regime != regime:
        raise CandidateRegimeMismatchError(
            f"Candidate regime '{candidate_regime}' does not match expected '{regime}'"
        )
    
    # Check moneyness_target
    candidate_moneyness = candidate.get("moneyness_target")
    if candidate_moneyness is None:
        raise CandidateRegimeMismatchError(
            f"Candidate missing moneyness_target field"
        )
    
    moneyness_diff = abs(candidate_moneyness - expected_moneyness)
    if moneyness_diff > tolerance:
        raise CandidateRegimeMismatchError(
            f"Candidate moneyness {candidate_moneyness:.4f} does not match "
            f"expected {expected_moneyness:.4f} (diff: {moneyness_diff:.4f}, "
            f"tolerance: {tolerance:.4f})"
        )


def validate_all_candidates(
    candidates: List[Dict],
    regime: str,
    expected_moneyness: float,
    tolerance: float = 0.001
) -> tuple[bool, List[str]]:
    """
    Validate all candidates match regime parameters.
    
    Args:
        candidates: List of candidate structures
        regime: Expected regime
        expected_moneyness: Expected moneyness for this regime
        tolerance: Tolerance for float comparison
        
    Returns:
        Tuple of (all_valid, error_messages)
        - all_valid: True if all candidates valid
        - error_messages: List of error messages (empty if all valid)
    """
    errors = []
    
    for i, candidate in enumerate(candidates):
        try:
            validate_candidate_regime(
                candidate=candidate,
                regime=regime,
                expected_moneyness=expected_moneyness,
                tolerance=tolerance
            )
        except CandidateRegimeMismatchError as e:
            error_msg = f"Candidate {i} ({candidate.get('candidate_id', 'unknown')}): {str(e)}"
            errors.append(error_msg)
            logger.error(error_msg)
    
    return (len(errors) == 0, errors)


def enforce_regime_consistency(
    candidates: List[Dict],
    regime: str,
    expected_moneyness: float,
    tolerance: float = 0.001,
    fail_fast: bool = True
) -> List[Dict]:
    """
    Enforce regime consistency on candidates.
    
    If fail_fast=True, raises on first mismatch.
    If fail_fast=False, filters out invalid candidates and logs warnings.
    
    Args:
        candidates: List of candidate structures
        regime: Expected regime
        expected_moneyness: Expected moneyness
        tolerance: Tolerance for float comparison
        fail_fast: If True, raise on mismatch; if False, filter
        
    Returns:
        List of valid candidates (all candidates if fail_fast and no errors)
        
    Raises:
        CandidateRegimeMismatchError: If fail_fast=True and validation fails
    """
    if fail_fast:
        # Strict mode: fail on first error
        all_valid, errors = validate_all_candidates(
            candidates=candidates,
            regime=regime,
            expected_moneyness=expected_moneyness,
            tolerance=tolerance
        )
        
        if not all_valid:
            error_summary = f"Regime consistency check failed for {regime}:\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                error_summary += f"\n... and {len(errors) - 5} more errors"
            raise CandidateRegimeMismatchError(error_summary)
        
        return candidates
    
    else:
        # Permissive mode: filter out invalid candidates
        valid_candidates = []
        
        for candidate in candidates:
            try:
                validate_candidate_regime(
                    candidate=candidate,
                    regime=regime,
                    expected_moneyness=expected_moneyness,
                    tolerance=tolerance
                )
                valid_candidates.append(candidate)
            except CandidateRegimeMismatchError as e:
                logger.warning(
                    f"Filtered out candidate {candidate.get('candidate_id', 'unknown')}: {e}"
                )
        
        if len(valid_candidates) < len(candidates):
            logger.warning(
                f"Filtered {len(candidates) - len(valid_candidates)} candidates "
                f"due to regime mismatch (regime={regime}, expected_moneyness={expected_moneyness:.4f})"
            )
        
        return valid_candidates
