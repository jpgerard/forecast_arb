"""
IBKR Contract Hygiene Validation

Ensures that candidates only use validated strikes from IBKR snapshots
to prevent "Unknown contract" warnings and invalid option contracts.

TASK 3: Contract hygiene - prevent invalid strikes from contaminating candidates.
"""

import logging
from typing import List, Dict, Set, Optional, Tuple

logger = logging.getLogger(__name__)


class InvalidContractError(Exception):
    """Raised when a candidate uses an invalid/unqualified contract."""
    pass


def extract_qualified_strikes_from_snapshot(
    snapshot: Dict,
    expiry: str,
    right: str
) -> Set[float]:
    """
    Extract qualified strikes from IBKR snapshot for a specific expiry and right.
    
    Args:
        snapshot: IBKR snapshot dict
        expiry: Expiry date string (YYYYMMDD)
        right: Option right ('C' or 'P')
        
    Returns:
        Set of qualified strike prices
    """
    qualified_strikes = set()
    
    expiries = snapshot.get("expiries", {})
    if expiry not in expiries:
        logger.warning(f"Expiry {expiry} not found in snapshot")
        return qualified_strikes
    
    expiry_data = expiries[expiry]
    options_list = expiry_data.get("calls" if right == "C" else "puts", [])
    
    for option in options_list:
        strike = option.get("strike")
        if strike is not None:
            qualified_strikes.add(float(strike))
    
    return qualified_strikes


def validate_candidate_strikes(
    candidate: Dict,
    snapshot: Dict,
    fail_on_invalid: bool = False
) -> Tuple[bool, List[str]]:
    """
    Validate that all strikes in candidate exist in the snapshot (are qualified).
    
    TASK 3 INVARIANT: No candidate may use unqualified strikes.
    
    Args:
        candidate: Candidate structure dict
        snapshot: IBKR snapshot dict
        fail_on_invalid: If True, raise exception on invalid strikes
        
    Returns:
        Tuple of (is_valid, warnings_list)
        
    Raises:
        InvalidContractError: If fail_on_invalid=True and validation fails
    """
    warnings = []
    
    # Extract candidate  strike info
    underlier = candidate.get("underlier", "UNKNOWN")
    expiry = candidate.get("expiry")
    long_strike = candidate.get("long_strike")
    short_strike = candidate.get("short_strike")
    candidate_id = candidate.get("candidate_id", "UNKNOWN")
    
    if expiry is None:
        error_msg = f"Candidate {candidate_id} missing expiry"
        warnings.append(error_msg)
        if fail_on_invalid:
            raise InvalidContractError(error_msg)
        return (False, warnings)
    
    if long_strike is None or short_strike is None:
        error_msg = f"Candidate {candidate_id} missing strike prices"
        warnings.append(error_msg)
        if fail_on_invalid:
            raise InvalidContractError(error_msg)
        return (False, warnings)
    
    # Get qualified strikes for puts (vertical spreads use puts)
    qualified_strikes = extract_qualified_strikes_from_snapshot(
        snapshot, expiry, "P"
    )
    
    if not qualified_strikes:
        error_msg = (
            f"Candidate {candidate_id}: No qualified puts found in snapshot "
            f"for expiry {expiry}"
        )
        warnings.append(error_msg)
        if fail_on_invalid:
            raise InvalidContractError(error_msg)
        return (False, warnings)
    
    # Check long strike
    if long_strike not in qualified_strikes:
        error_msg = (
            f"Candidate {candidate_id}: Long strike {long_strike} not in snapshot. "
            f"Available strikes: {sorted(qualified_strikes)[:10]}..."
        )
        warnings.append(error_msg)
        logger.warning(f"[CONTRACT_HYGIENE] {error_msg}")
        
        if fail_on_invalid:
            raise InvalidContractError(error_msg)
        return (False, warnings)
    
    # Check short strike
    if short_strike not in qualified_strikes:
        error_msg = (
            f"Candidate {candidate_id}: Short strike {short_strike} not in snapshot. "
            f"Available strikes: {sorted(qualified_strikes)[:10]}..."
        )
        warnings.append(error_msg)
        logger.warning(f"[CONTRACT_HYGIENE] {error_msg}")
        
        if fail_on_invalid:
            raise InvalidContractError(error_msg)
        return (False, warnings)
    
    # All strikes valid
    return (True, [])


def filter_candidates_by_contract_validity(
    candidates: List[Dict],
    snapshot: Dict
) -> Tuple[List[Dict], List[Dict]]:
    """
    Filter candidates to only those with valid/qualified contracts.
    
    TASK 3: Prevent invalid contracts from contaminating candidate pool.
    
    Args:
        candidates: List of candidate dicts
        snapshot: IBKR snapshot dict
        
    Returns:
        Tuple of (valid_candidates, invalid_candidates)
    """
    valid = []
    invalid = []
    
    for candidate in candidates:
        is_valid, warnings = validate_candidate_strikes(
            candidate, snapshot, fail_on_invalid=False
        )
        
        if is_valid:
            valid.append(candidate)
        else:
            # Mark as non-representable
            candidate["representable"] = False
            candidate["representability_reason"] = "INVALID_CONTRACT_STRIKES"
            candidate["contract_validation_warnings"] = warnings
            invalid.append(candidate)
            
            logger.warning(
                f"❌ [CONTRACT_HYGIENE] Filtered candidate {candidate.get('candidate_id')}: "
                f"Invalid strikes. {'; '.join(warnings[:2])}"
            )
    
    if invalid:
        logger.warning(
            f"[CONTRACT_HYGIENE] Filtered {len(invalid)} candidates with invalid contracts"
        )
    
    return (valid, invalid)


def log_contract_diagnostics(
    snapshot_metadata: Dict,
    candidates: List[Dict]
) -> None:
    """
    Log contract qualification diagnostics from snapshot.
    
    Helps diagnose "Unknown contract" issues.
    
    Args:
        snapshot_metadata: Snapshot metadata dict with diagnostics
        candidates: List of candidates generated
    """
    diagnostics = snapshot_metadata.get("option_contract_diagnostics", {})
    
    if not diagnostics:
        return
    
    totals = diagnostics.get("totals", {})
    attempted = totals.get("attempted_contracts", 0)
    qualified = totals.get("qualified_contracts", 0)
    unknown = totals.get("unknown_contracts", 0)
    
    logger.info("=" * 80)
    logger.info("IBKR CONTRACT QUALIFICATION SUMMARY")
    logger.info("=" * 80)
    logger.info(f"  Attempted contracts: {attempted}")
    logger.info(f"  Qualified contracts: {qualified}")
    logger.info(f"  Unknown contracts: {unknown}")
    
    if unknown > 0:
        pct_unknown = (unknown / attempted) * 100 if attempted > 0 else 0
        logger.warning(
            f"  ⚠️  {unknown} contracts failed qualification ({pct_unknown:.1f}%)"
        )
        logger.warning(
            f"  This indicates strikes in the snapshot failed IBKR validation."
        )
        logger.warning(
            f"  Candidates using these strikes will be marked non-representable."
        )
    
    logger.info(f"  Candidates generated: {len(candidates)}")
    logger.info("=" * 80)
    logger.info("")
