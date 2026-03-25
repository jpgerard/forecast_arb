"""
OrderIntent Builder - CLI

Builds OrderIntent JSON from review_candidates.json with deterministic intent_id.

This is the single entry point for intent creation. It:
1. Loads nested review_candidates schema: regimes -> <regime> -> candidates -> [...]
2. Selects candidate by regime + rank
3. Constructs OrderIntent with required schema
4. Computes deterministic intent_id as SHA1 of sorted JSON (excluding intent_id)
5. Writes file to intents/
6. Prints path of file
7. Exits non-zero if no candidate found or file not written
"""

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def load_review_candidates(candidates_path: str) -> Dict[str, Any]:
    """
    Load review_candidates.json.
    
    Schema: {"regimes": {"<regime>": {"candidates": [...], ...}, ...}}
    
    Args:
        candidates_path: Path to review_candidates.json
        
    Returns:
        Review candidates dict
        
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If schema is invalid
    """
    candidates_file = Path(candidates_path)
    
    if not candidates_file.exists():
        raise FileNotFoundError(f"review_candidates.json not found: {candidates_path}")
    
    with open(candidates_file, "r") as f:
        data = json.load(f)
    
    if "regimes" not in data:
        raise ValueError(f"Invalid review_candidates.json: missing 'regimes' key")
    
    return data


def select_candidate(
    review_candidates: Dict[str, Any],
    regime: str,
    rank: int
) -> Dict[str, Any]:
    """
    Select candidate by regime and rank.
    
    Args:
        review_candidates: Review candidates dict
        regime: Regime name (e.g., "crash", "selloff")
        rank: Candidate rank (1-indexed)
        
    Returns:
        Candidate dict
        
    Raises:
        ValueError: If regime not found or candidate with rank not found
    """
    if regime not in review_candidates.get("regimes", {}):
        available = list(review_candidates.get("regimes", {}).keys())
        raise ValueError(f"Regime '{regime}' not found. Available: {available}")
    
    regime_data = review_candidates["regimes"][regime]
    candidates = regime_data.get("candidates", [])
    
    if not candidates:
        raise ValueError(f"No candidates found for regime '{regime}'")
    
    # Find candidate with specified rank
    for candidate in candidates:
        if candidate.get("rank") == rank:
            return candidate
    
    # Rank not found
    available_ranks = [c.get("rank") for c in candidates]
    raise ValueError(f"No candidate with rank={rank} in regime '{regime}'. Available ranks: {available_ranks}")


def compute_intent_id(intent_content: Dict[str, Any]) -> str:
    """
    Compute deterministic intent_id as SHA1 of sorted JSON.
    
    The intent_id is computed from all fields EXCEPT intent_id itself.
    This ensures identical intents (same strategy, symbol, expiry, strikes, limits, guards)
    produce the same intent_id.
    
    Args:
        intent_content: OrderIntent dict WITHOUT intent_id field
        
    Returns:
        40-character hex SHA1 hash
    """
    # Ensure intent_id is not in the content
    content_copy = {k: v for k, v in intent_content.items() if k != "intent_id"}
    
    # Serialize to sorted JSON (for determinism)
    json_str = json.dumps(content_copy, sort_keys=True, separators=(',', ':'))
    
    # Compute SHA1
    intent_id = hashlib.sha1(json_str.encode('utf-8')).hexdigest()
    
    return intent_id


def build_order_intent(
    candidate: Dict[str, Any],
    regime: str,
    qty: Optional[int] = None,
    limit_start: Optional[float] = None,
    limit_max: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Build OrderIntent from candidate with exact schema required by execute_trade.

    Required fields per execute_trade.validate_order_intent:
    - strategy
    - symbol
    - expiry
    - type
    - legs (each with: action, right, strike, ratio, exchange, currency)
    - qty
    - limit (with: start, max)
    - tif
    - guards
    - intent_id (computed deterministically)

    Args:
        candidate: Candidate dict from review_candidates.json
        regime: Regime name (crash/selloff)
        qty: Number of contracts (default 1; overrides candidate if provided)
        limit_start: Explicit limit start price (overrides candidate debit if provided)
        limit_max: Explicit limit max price (overrides candidate debit * 1.02 if provided)

    Returns:
        OrderIntent dict
    """
    # Extract fields from candidate
    # Support both "underlier" (allocator schema) and "symbol" (structuring schema)
    symbol = candidate.get("underlier") or candidate.get("symbol", "SPY")
    expiry = candidate["expiry"]

    # Extract strikes (support dict form)
    strikes_raw = candidate.get("strikes", {})
    if isinstance(strikes_raw, dict):
        long_strike = float(strikes_raw.get("long_put", 0))
        short_strike = float(strikes_raw.get("short_put", 0))
    else:
        long_strike = float(candidate.get("long_strike", 0))
        short_strike = float(candidate.get("short_strike", 0))

    # Resolve pricing — explicit args take precedence over candidate fields
    if limit_start is not None:
        debit_start = float(limit_start)
    else:
        raw_debit = (
            candidate.get("debit_per_contract")
            or candidate.get("computed_premium_usd")
            or 0.01
        )
        debit_start = float(raw_debit)

    if limit_max is not None:
        debit_max = float(limit_max)
    else:
        debit_max = round(debit_start * 1.02, 4)

    # Resolve qty
    resolved_qty = int(qty) if qty is not None else 1

    # Build legs with all required fields
    legs = [
        {
            "action": "BUY",
            "right": "P",
            "strike": long_strike,
            "ratio": 1,
            "exchange": "SMART",
            "currency": "USD",
        },
        {
            "action": "SELL",
            "right": "P",
            "strike": short_strike,
            "ratio": 1,
            "exchange": "SMART",
            "currency": "USD",
        },
    ]

    # Build metadata from candidate (optional fields — safe defaults)
    metrics = candidate.get("metrics") or {}
    metadata: Dict[str, Any] = {
        "regime": regime,
    }
    if "rank" in candidate:
        metadata["rank"] = candidate["rank"]
    if "ev_per_dollar" in metrics:
        metadata["ev_per_dollar"] = metrics["ev_per_dollar"]
    elif "ev_per_dollar" in candidate:
        metadata["ev_per_dollar"] = candidate["ev_per_dollar"]
    if "moneyness_target" in candidate:
        metadata["moneyness_target"] = candidate["moneyness_target"]
    if "candidate_id" in candidate:
        metadata["candidate_id"] = candidate["candidate_id"]
    if "event_spec_hash" in candidate:
        metadata["event_spec_hash"] = candidate["event_spec_hash"]

    # Build intent WITHOUT intent_id first
    intent: Dict[str, Any] = {
        "strategy": "crash_venture_v2",
        "symbol": symbol,
        "regime": regime,
        "expiry": expiry,
        "type": "PUT_SPREAD",
        "legs": legs,
        "qty": resolved_qty,
        "limit": {
            "start": debit_start,
            "max": debit_max,
        },
        "tif": "DAY",
        "transmit": False,  # Never transmit in intent mode; explicit flag required
        "guards": {
            "max_debit": debit_max,
            "max_spread_width": 0.20,
            "min_dte": 7,
        },
        "metadata": metadata,
    }

    # Compute deterministic intent_id from content (excluding intent_id itself)
    intent_id = compute_intent_id(intent)
    intent["intent_id"] = intent_id

    return intent


def emit_intent(
    intent: Dict[str, Any],
    output_dir: str = "intents"
) -> str:
    """
    Write OrderIntent to file in intents/ directory.
    
    Filename format: {symbol}_{expiry}_{long_strike}_{short_strike}_{regime}_{intent_id[:8]}.json
    
    Args:
        intent: OrderIntent dict
        output_dir: Output directory (default: "intents")
        
    Returns:
        Path to written file
        
    Raises:
        RuntimeError: If file write fails
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Extract fields for filename
    symbol = intent["symbol"]
    expiry = intent["expiry"]
    intent_id = intent["intent_id"]
    
    # Extract strikes
    long_strike = None
    short_strike = None
    for leg in intent["legs"]:
        if leg["action"] == "BUY":
            long_strike = int(leg["strike"])
        elif leg["action"] == "SELL":
            short_strike = int(leg["strike"])
    
    # Build filename
    filename = f"{symbol}_{expiry}_{long_strike}_{short_strike}_crash_{intent_id[:8]}.json"
    file_path = output_path / filename
    
    # Write file
    try:
        with open(file_path, "w") as f:
            json.dump(intent, f, indent=2)
    except Exception as e:
        raise RuntimeError(f"Failed to write intent file: {e}")
    
    logger.info(f"✓ Intent written: {file_path}")
    
    return str(file_path)


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Build OrderIntent from review_candidates.json"
    )
    
    parser.add_argument(
        "--candidates",
        type=str,
        required=True,
        help="Path to review_candidates.json"
    )
    parser.add_argument(
        "--regime",
        type=str,
        required=True,
        help="Regime name (e.g., 'crash', 'selloff')"
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=1,
        help="Candidate rank (default: 1)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="intents",
        help="Output directory (default: 'intents')"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    setup_logging(verbose=args.verbose)
    
    try:
        # Load review candidates
        logger.info(f"Loading review candidates: {args.candidates}")
        review_candidates = load_review_candidates(args.candidates)
        
        # Select candidate
        logger.info(f"Selecting candidate: regime={args.regime}, rank={args.rank}")
        candidate = select_candidate(review_candidates, args.regime, args.rank)
        
        logger.info(f"✓ Selected candidate: {candidate.get('candidate_id', 'unknown')}")
        logger.info(f"  Expiry: {candidate.get('expiry')}")
        logger.info(f"  Strikes: {candidate.get('strikes')}")
        logger.info(f"  Debit: ${candidate.get('debit_per_contract', 0):.2f}")
        
        # Build OrderIntent
        logger.info("Building OrderIntent...")
        intent = build_order_intent(candidate, args.regime)
        
        logger.info(f"✓ OrderIntent built")
        logger.info(f"  Strategy: {intent['strategy']}")
        logger.info(f"  Symbol: {intent['symbol']}")
        logger.info(f"  Expiry: {intent['expiry']}")
        logger.info(f"  Type: {intent['type']}")
        logger.info(f"  Intent ID: {intent['intent_id']}")
        
        # Emit intent to file
        logger.info(f"Writing intent to {args.output_dir}...")
        file_path = emit_intent(intent, args.output_dir)
        
        # Print file path to stdout (for capture by caller)
        print(file_path)
        
        logger.info("✅ Intent emission complete")
        sys.exit(0)
        
    except FileNotFoundError as e:
        logger.error(f"❌ File not found: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"❌ Validation error: {e}")
        sys.exit(1)
    except RuntimeError as e:
        logger.error(f"❌ Runtime error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
