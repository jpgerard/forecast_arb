"""
Crash Venture v1 Engine - IBKR Snapshot Mode

Generates SPY put-spread candidates using real IBKR snapshot data.
Uses actual strikes and market prices (no decimals, no computed strikes).

Key features:
- Strike selection from actual snapshot strikes (nearest to moneyness targets)
- Pricing validation: bid>0, ask>bid, mid=(bid+ask)/2
- Min debit filter with diagnostics
- Fail fast on invalid quotes
- Non-zero validation for debit/max_loss/max_gain
"""

import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import yaml

from ..structuring.snapshot_io import (
    load_snapshot,
    validate_snapshot,
    get_snapshot_metadata,
    get_expiries,
    get_strikes_for_expiry,
    get_puts_for_expiry,
    get_option_by_strike,
    compute_time_to_expiry
)
from ..structuring.calibrator import calibrate_drift
from ..structuring.evaluator import evaluate_structure
from ..probability import get_regime_signals, adjust_crash_probability
from ..structuring.router import (
    filter_dominated_structures,
    choose_best_structure,
    rank_structures
)
from ..structuring.output_formatter import (
    get_reason_selected,
    format_structure_output,
    write_structures_json,
    write_summary_md,
    write_dry_run_tickets
)
from ..structuring.quotes import price_buy, price_sell
from ..utils.manifest import compute_config_checksum


logger = logging.getLogger(__name__)


def load_frozen_config(config_path: str) -> Dict:
    """Load and validate frozen config."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    
    campaign = config.get("campaign_name", "")
    
    # Accept "crash_venture_v1" or versioned variants like "crash_venture_v1_1"
    if not campaign:
        raise ValueError(
            "Config must have 'campaign_name' field. "
            "Expected 'crash_venture_v1' or versioned variant (e.g., 'crash_venture_v1_1')"
        )
    
    if campaign != "crash_venture_v1" and not campaign.startswith("crash_venture_v1_"):
        raise ValueError(
            f"Config campaign_name must be 'crash_venture_v1' or start with 'crash_venture_v1_', got '{campaign}'"
        )
    
    return config


def setup_determinism(run_id: str) -> int:
    """Set up deterministic RNG seeding."""
    import random
    import numpy as np
    
    rng_seed = abs(hash(run_id)) % (2**32)
    random.seed(rng_seed)
    np.random.seed(rng_seed)
    
    logger.info(f"Deterministic seed: {rng_seed}")
    return rng_seed


def find_nearest_strike(available_strikes: List[float], target: float) -> float:
    """
    Find nearest available strike to target.
    
    Args:
        available_strikes: List of actual strikes from snapshot
        target: Target strike value
        
    Returns:
        Nearest available strike
    """
    if not available_strikes:
        raise ValueError("No available strikes")
    
    return min(available_strikes, key=lambda k: abs(k - target))


def validate_and_price_buy_leg(put_option: Dict, strike: float) -> Tuple[Optional[float], str]:
    """
    Validate and price a BUY leg (long put in debit spread).
    
    For BUY legs, we use ask price (what we pay to enter the position).
    
    Args:
        put_option: Put option dict from snapshot
        strike: Strike price
        
    Returns:
        (price, reason) - price is None if invalid, reason explains the outcome
    """
    if put_option is None:
        return None, f"NO_OPTION_DATA"
    
    price, source = price_buy(put_option)
    
    if price is None:
        return None, f"NO_EXECUTABLE_PRICE_LONG"
    
    return price, source


def validate_and_price_sell_leg(put_option: Dict, strike: float) -> Tuple[Optional[float], str]:
    """
    Validate and price a SELL leg (short put in debit spread).
    
    For SELL legs, we use bid price (what we receive when entering the position).
    
    Args:
        put_option: Put option dict from snapshot
        strike: Strike price
        
    Returns:
        (price, reason) - price is None if invalid, reason explains the outcome
    """
    if put_option is None:
        return None, f"NO_OPTION_DATA"
    
    price, source = price_sell(put_option)
    
    if price is None:
        return None, f"NO_EXECUTABLE_PRICE_SHORT"
    
    return price, source


def compute_debit_from_put_spread(
    long_put_price: float,
    short_put_price: float
) -> float:
    """
    Compute debit for put spread using executable prices.
    
    Args:
        long_put_price: Price to BUY long put (typically ask)
        short_put_price: Price to SELL short put (typically bid)
        
    Returns:
        Debit per share (>0 means we pay)
    """
    # Debit = what we pay - what we collect
    debit = long_put_price - short_put_price
    
    return debit


def detect_strike_grid_spacing(strikes: List[float], around_strike: float, window: int = 5) -> float:
    """
    Detect typical strike spacing near a given strike.
    
    Args:
        strikes: Sorted list of available strikes
        around_strike: Strike to check spacing around
        window: Number of strikes to check on each side
        
    Returns:
        Most common spacing (e.g., 1.0, 5.0, 10.0)
    """
    if len(strikes) < 2:
        return 5.0  # Default fallback
    
    # Find strikes near the target
    idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - around_strike))
    
    # Get window of strikes
    start = max(0, idx - window)
    end = min(len(strikes), idx + window + 1)
    nearby_strikes = strikes[start:end]
    
    # Compute spacing between consecutive strikes
    spacings = []
    for i in range(len(nearby_strikes) - 1):
        spacing = nearby_strikes[i + 1] - nearby_strikes[i]
        if spacing > 0:
            spacings.append(spacing)
    
    if not spacings:
        return 5.0
    
    # Return most common spacing (round to avoid floating point issues)
    from collections import Counter
    spacing_counts = Counter([round(s, 2) for s in spacings])
    most_common_spacing = spacing_counts.most_common(1)[0][0]
    
    return most_common_spacing


def snap_width_to_grid(requested_width: float, grid_spacing: float) -> float:
    """
    Snap requested width to nearest feasible multiple of grid spacing.
    
    Args:
        requested_width: Desired spread width
        grid_spacing: Strike grid spacing
        
    Returns:
        Snapped width (multiple of grid_spacing)
    """
    # Round to nearest multiple of grid_spacing
    snapped = round(requested_width / grid_spacing) * grid_spacing
    
    # Ensure at least one grid unit
    if snapped < grid_spacing:
        snapped = grid_spacing
    
    return snapped


def generate_candidates_from_snapshot(
    snapshot: Dict,
    expiry: str,
    S0: float,
    moneyness_targets: List[float],
    spread_widths: List[int],
    min_debit_per_contract: float,
    max_candidates: int,
    regime: str = "crash"
) -> Tuple[List[Dict], List[Dict]]:
    """
    Generate candidate put spreads from snapshot using actual strikes.
    
    Grid-aware width snapping: If requested width not feasible, snap to nearest
    feasible width based on actual strike grid spacing.
    
    Args:
        snapshot: IBKR snapshot dict
        expiry: Target expiry (YYYYMMDD)
        S0: Spot price from snapshot
        moneyness_targets: List of moneyness values (e.g., [-0.10, -0.15])
        spread_widths: List of spread widths in dollars
        min_debit_per_contract: Minimum debit filter
        max_candidates: Maximum candidates to generate
        regime: Regime identifier ("crash" or "selloff"), default "crash" for backward compat
        
    Returns:
        (valid_candidates, filtered_diagnostics)
    """
    available_strikes = get_strikes_for_expiry(snapshot, expiry)
    puts = get_puts_for_expiry(snapshot, expiry)
    
    if not available_strikes:
        raise ValueError(f"No strikes available for expiry {expiry}")
    
    if not puts:
        raise ValueError(f"No puts available for expiry {expiry}")
    
    logger.info(f"Available strikes for {expiry}: {len(available_strikes)}")
    logger.info(f"  Range: ${min(available_strikes):.2f} to ${max(available_strikes):.2f}")
    
    candidates = []
    filtered_out = []
    
    for moneyness in moneyness_targets:
        for requested_width in spread_widths:
            if len(candidates) >= max_candidates:
                break
            
            # Find nearest actual strikes
            target_long = S0 * (1 + moneyness)
            K_long = find_nearest_strike(available_strikes, target_long)
            
            # For K_short, only consider strikes strictly below K_long
            strikes_below_long = [s for s in available_strikes if s < K_long]
            
            if not strikes_below_long:
                reason = f"No strikes below K_long={K_long:.2f}"
                logger.warning(reason)
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "K_long": K_long,
                    "reason": reason
                })
                continue
            
            # GRID-AWARE WIDTH SNAPPING
            # Detect local grid spacing near K_long
            grid_spacing = detect_strike_grid_spacing(available_strikes, K_long, window=5)
            snapped_width = snap_width_to_grid(requested_width, grid_spacing)
            
            logger.info(
                f"Grid spacing near K_long=${K_long:.2f}: ${grid_spacing:.2f}, "
                f"requested width=${requested_width}, snapped=${snapped_width:.2f}"
            )
            
            # Use snapped width as the target
            target_short = K_long - snapped_width
            K_short = find_nearest_strike(strikes_below_long, target_short)
            
            # Validate strikes
            if K_short <= 0 or K_long <= K_short:
                reason = f"Invalid strikes: K_long={K_long:.2f}, K_short={K_short:.2f}"
                logger.warning(reason)
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "snapped_width": snapped_width,
                    "reason": reason
                })
                continue
            
            # ENFORCE WIDTH INTEGRITY: Check effective width vs snapped width
            effective_width = K_long - K_short
            width_deviation = abs(effective_width - snapped_width)
            
            # More lenient tolerance for grid snapping (allow up to 15% deviation)
            tolerance = max(2.50, snapped_width * 0.15)
            
            if width_deviation > tolerance:
                reason = f"Width deviation too large: requested=${requested_width}, snapped=${snapped_width:.2f}, effective=${effective_width:.2f}, deviation=${width_deviation:.2f} > ${tolerance:.2f}"
                logger.warning(reason)
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "snapped_width": snapped_width,
                    "effective_width": effective_width,
                    "K_long": K_long,
                    "K_short": K_short,
                    "reason": reason
                })
                continue
            
            # Get put options
            long_put = get_option_by_strike(puts, K_long)
            short_put = get_option_by_strike(puts, K_short)
            
            # Price long leg (BUY - use ask)
            long_price, long_source = validate_and_price_buy_leg(long_put, K_long)
            if long_price is None:
                logger.warning(f"Long put K=${K_long:.2f}: {long_source}")
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "K_long": K_long,
                    "K_short": K_short,
                    "reason": f"Long put: {long_source}"
                })
                continue
            
            # Price short leg (SELL - use bid)
            short_price, short_source = validate_and_price_sell_leg(short_put, K_short)
            if short_price is None:
                logger.warning(f"Short put K=${K_short:.2f}: {short_source}")
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "K_long": K_long,
                    "K_short": K_short,
                    "reason": f"Short put: {short_source}"
                })
                continue
            
            # Compute debit (per-share from executable prices)
            debit_per_share = compute_debit_from_put_spread(long_price, short_price)
            
            # Convert to per-contract for filtering and storage
            debit_per_contract = debit_per_share * 100
            
            # Apply min debit filter (compare per-contract values)
            if debit_per_contract < min_debit_per_contract:
                reason = f"Debit per contract ${debit_per_contract:.2f} < min ${min_debit_per_contract:.2f}"
                logger.info(f"Filtered by min debit: {reason}")
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "effective_width": effective_width,
                    "K_long": K_long,
                    "K_short": K_short,
                    "debit_per_share": debit_per_share,
                    "debit_per_contract": debit_per_contract,
                    "reason": reason
                })
                continue
            
            # Compute max loss/gain (per-contract)
            max_loss_per_contract = debit_per_contract
            max_gain_per_contract = (K_long - K_short) * 100 - debit_per_contract
            
            # CRITICAL: Validate non-zero
            if debit_per_contract <= 0 or max_loss_per_contract <= 0 or max_gain_per_contract <= 0:
                reason = f"Zero values: debit_per_contract={debit_per_contract:.2f}, max_loss_per_contract={max_loss_per_contract:.2f}, max_gain_per_contract={max_gain_per_contract:.2f}"
                logger.error(reason)
                filtered_out.append({
                    "moneyness": moneyness,
                    "requested_width": requested_width,
                    "effective_width": effective_width,
                    "K_long": K_long,
                    "K_short": K_short,
                    "debit_per_contract": debit_per_contract,
                    "reason": reason
                })
                continue
            
            # Generate stable candidate_id (survives rank changes, quote drift)
            import hashlib
            candidate_id = hashlib.sha1(
                f"{regime}|{expiry}|{K_long}|{K_short}|P|100|{snapshot['snapshot_metadata']['underlier']}".encode()
            ).hexdigest()[:12]
            
            # Create candidate structure with grid metadata and price provenance
            candidate = {
                "regime": regime,  # CRITICAL: Add regime to candidate
                "underlier": snapshot["snapshot_metadata"]["underlier"],
                "expiry": expiry,
                "template_name": "put_spread",
                "candidate_id": candidate_id,
                "legs": [
                    {
                        "type": "put",
                        "side": "long",
                        "strike": K_long,
                        "quantity": 1,
                        "price": long_price,  # Executable price (ask)
                        "price_source": long_source,  # "ask", "bid_fallback", etc.
                        "bid": long_put.get("bid"),
                        "ask": long_put.get("ask"),
                        "implied_vol": long_put.get("implied_vol"),
                        "delta": long_put.get("delta")
                    },
                    {
                        "type": "put",
                        "side": "short",
                        "strike": K_short,
                        "quantity": 1,
                        "price": short_price,  # Executable price (bid)
                        "price_source": short_source,  # "bid", "ask_fallback", etc.
                        "bid": short_put.get("bid"),
                        "ask": short_put.get("ask"),
                        "implied_vol": short_put.get("implied_vol"),
                        "delta": short_put.get("delta")
                    }
                ],
                "debit_per_contract": debit_per_contract,
                "max_loss_per_contract": max_loss_per_contract,
                "max_gain_per_contract": max_gain_per_contract,
                "strikes": {
                    "long_put": K_long,
                    "short_put": K_short
                },
                "spread_width": effective_width,  # Use actual effective width
                "moneyness_target": moneyness,
                "width_target": requested_width,  # Store original request
                "width_snapped": snapped_width,    # Store snapped value
                "grid_spacing": grid_spacing       # Store detected grid spacing
            }
            
            candidates.append(candidate)
            logger.info(
                f"✓ Candidate: {moneyness:.1%} / req=${requested_width} snapped=${snapped_width:.2f} → "
                f"K_long=${K_long:.2f}, K_short=${K_short:.2f}, effective_width=${effective_width:.2f}, "
                f"debit_per_contract=${debit_per_contract:.2f}"
            )
    
    logger.info(f"Generated {len(candidates)} valid candidates")
    logger.info(f"Filtered out: {len(filtered_out)} candidates")
    
    return candidates, filtered_out


def run_crash_venture_v1_snapshot(
    config_path: str,
    snapshot_path: str,
    p_event: float,
    min_debit_per_contract: float = 30.0,
    regime: str = "crash"
) -> Dict:
    """
    Run Crash Venture v1 with IBKR snapshot.
    
    Args:
        config_path: Path to frozen config
        snapshot_path: Path to IBKR snapshot JSON
        p_event: Event probability
        min_debit_per_contract: Minimum debit filter
        regime: Regime identifier ("crash" or "selloff"), default "crash" for backward compat
        
    Returns:
        Run results dict
    """
    # Load frozen config
    config = load_frozen_config(config_path)
    config_checksum = compute_config_checksum(config)
    logger.info(f"Config checksum: {config_checksum}")
    
    # Load and validate snapshot
    snapshot = load_snapshot(snapshot_path)
    validate_snapshot(snapshot)
    metadata = get_snapshot_metadata(snapshot)
    
    # Use snapshot spot price
    S0 = metadata["current_price"]
    underlier = metadata["underlier"]
    
    logger.info(f"Snapshot: {underlier} @ ${S0:.2f}")
    
    # Create run ID
    campaign = config["campaign_name"]
    run_time_utc = datetime.now(timezone.utc).isoformat()
    timestamp = run_time_utc.replace(':', '').replace('-', '').replace('.', '')[:15]
    run_id = f"{campaign}_{config_checksum}_{timestamp}"
    
    logger.info(f"Starting run: {run_id}")
    
    # Setup determinism
    rng_seed = setup_determinism(run_id)
    
    # Extract frozen parameters
    struct_config = config["structuring"]
    
    # ENFORCE: Underlier must match snapshot
    config_underlier = struct_config["underlier"]
    if underlier != config_underlier:
        raise ValueError(
            f"Snapshot underlier '{underlier}' does not match config '{config_underlier}'"
        )
    
    # Select best expiry using unified selection logic
    from ..structuring.expiry_selection import select_best_expiry
    
    dte_min = struct_config["dte_range_days"]["min"]
    dte_max = struct_config["dte_range_days"]["max"]
    target_dte_midpoint = (dte_min + dte_max) // 2
    
    expiry, expiry_diagnostics = select_best_expiry(
        snapshot=snapshot,
        target_dte=target_dte_midpoint,
        dte_min=dte_min,
        dte_max=dte_max
    )
    
    if expiry is None:
        # Fallback to first available if selection failed
        logger.warning(f"select_best_expiry returned None: {expiry_diagnostics}")
        expiries = get_expiries(snapshot)
        if not expiries:
            raise ValueError("No expiries in snapshot")
        expiry = expiries[0]
        logger.warning(f"Using fallback expiry: {expiry}")
    
    T = compute_time_to_expiry(metadata["snapshot_time"], expiry)
    days_to_expiry = int(T * 365)
    
    logger.info(f"Using expiry: {expiry} (DTE={days_to_expiry})")
    logger.info(f"Expiry selection: {expiry_diagnostics.get('selection_reason', 'FALLBACK')}")
    logger.info(f"Coverage score: {expiry_diagnostics.get('selected_coverage_score', 'N/A')}")
    
    # Get parameters
    moneyness_targets = struct_config["moneyness_targets"]
    spread_widths = struct_config["spread_widths"]
    constraints = struct_config["constraints"]
    mc_config = struct_config["monte_carlo"]
    objective = struct_config["objective"]
    
    # Estimate sigma from snapshot (use first put's IV if available)
    puts = get_puts_for_expiry(snapshot, expiry)
    sigma = None
    for put in puts:
        if put.get("implied_vol") and put["implied_vol"] > 0:
            sigma = put["implied_vol"]
            break
    
    if sigma is None:
        logger.warning("No IV found in snapshot, using fallback sigma=0.15")
        sigma = 0.15
    else:
        logger.info(f"Using IV from snapshot: {sigma:.3f}")
    
    # Risk-free rate from snapshot or fallback
    r = metadata.get("risk_free_rate", 0.05)
    
    # === PHASE 4: PROBABILITY CONDITIONING LAYER ===
    logger.info("=" * 80)
    logger.info("APPLYING PROBABILITY CONDITIONING")
    logger.info("=" * 80)
    logger.info(f"Base p_event (input): {p_event:.4f}")
    
    # Fetch regime signals
    regime_signals = get_regime_signals(lookback_days=252)
    
    # Apply conditioning to get adjusted probability
    conditioning_result = adjust_crash_probability(
        base_p=p_event,
        vix_pct=regime_signals.get("vix_pct"),
        skew_pct=regime_signals.get("skew_pct"),
        credit_pct=regime_signals.get("credit_pct")
    )
    
    p_adjusted = conditioning_result["p_adjusted"]
    p_used = p_adjusted  # Use adjusted probability for calibration
    
    logger.info(
        f"P_BASE={p_event:.4f} | P_ADJ={p_adjusted:.4f} | "
        f"CONF={conditioning_result['confidence_score']:.2f} | "
        f"VIX={regime_signals.get('vix_pct', 'N/A')} | "
        f"CREDIT={regime_signals.get('credit_pct', 'N/A')}"
    )
    logger.info(
        f"Multipliers: vol={conditioning_result['multipliers']['vol']:.2f}, "
        f"skew={conditioning_result['multipliers']['skew']:.2f}, "
        f"credit={conditioning_result['multipliers']['credit']:.2f}, "
        f"combined={conditioning_result['multipliers']['combined']:.2f}"
    )
    logger.info("=" * 80)
    
    # Calibrate drift for p_used (adjusted probability)
    K_barrier = S0 * 0.95
    
    logger.info(f"Calibrating drift for p_used={p_used:.4f} (conditioned from base={p_event:.4f})")
    mu_calib, p_achieved = calibrate_drift(
        p_event=p_used,
        S0=S0,
        K_barrier=K_barrier,
        T=T,
        sigma=sigma,
        n_samples=10000,
        seed=rng_seed
    )
    
    logger.info(f"Calibrated: μ={mu_calib:.4f}, achieved p={p_achieved:.3f}")
    
    # Generate candidates from snapshot
    logger.info("=" * 80)
    logger.info("GENERATING CANDIDATES FROM SNAPSHOT")
    logger.info("=" * 80)
    
    candidates, filtered_out = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        min_debit_per_contract=min_debit_per_contract,
        max_candidates=constraints["max_candidates_evaluated"],
        regime=regime
    )
    
    # TASK 3: CONTRACT HYGIENE - Filter candidates to only valid/qualified strikes
    logger.info("=" * 80)
    logger.info("VALIDATING CONTRACT HYGIENE")
    logger.info("=" * 80)
    
    from ..structuring.contract_validation import (
        filter_candidates_by_contract_validity,
        log_contract_diagnostics
    )
    
    # Log IBKR qualification summary
    log_contract_diagnostics(snapshot["snapshot_metadata"], candidates)
    
    # Filter to valid contracts only
    valid_candidates, invalid_candidates = filter_candidates_by_contract_validity(
        candidates, snapshot
    )
    
    logger.info(f"Contract validation: {len(valid_candidates)} valid, {len(invalid_candidates)} invalid")
    
    # Update candidates list and filtered_out diagnostics
    candidates = valid_candidates
    filtered_out.extend(invalid_candidates)
    
    # If no candidates, return NO_TRADE result cleanly (do NOT raise)
    if not candidates:
        logger.warning("=" * 80)
        logger.warning("NO TRADE: No candidates survived filters")
        logger.warning("=" * 80)
        logger.warning(f"Total filtered out: {len(filtered_out)}")
        
        for item in filtered_out:
            logger.warning(f"  - Moneyness={item.get('moneyness', 'N/A'):.2%}, "
                        f"Width=${item.get('requested_width', 'N/A')}: {item['reason']}")
        
        # Determine NO_TRADE reason
        no_trade_reason = "NO_CANDIDATES_SURVIVED_FILTERS"
        if filtered_out and all("No strikes below" in item.get("reason", "") for item in filtered_out):
            no_trade_reason = "INSUFFICIENT_STRIKE_COVERAGE"
        
        # Create run directory for NO_TRADE artifacts
        run_dir = Path("runs") / campaign / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        
        # Write empty candidates.json
        candidates_path = run_dir / "candidates.json"
        with open(candidates_path, "w") as f:
            json.dump([], f, indent=2)
        
        # Write empty tickets
        tickets_path = artifacts_dir / "tickets.json"
        with open(tickets_path, "w") as f:
            json.dump([], f, indent=2)
        
        # Write final_decision.json with NO_TRADE
        final_decision = {
            "run_id": run_id,
            "decision": "NO_TRADE",
            "reason": no_trade_reason,
            "details": f"Filtered out {len(filtered_out)} candidates. Try lowering min_debit_per_contract (current: ${min_debit_per_contract:.2f})",
            "timestamp_utc": run_time_utc
        }
        decision_path = artifacts_dir / "final_decision.json"
        with open(decision_path, "w") as f:
            json.dump(final_decision, f, indent=2)
        
        # Write review.txt
        review_path = artifacts_dir / "review.txt"
        review_text = f"""NO TRADE Decision

Run ID: {run_id}
Campaign: {campaign}
Decision: NO_TRADE
Reason: {no_trade_reason}

Details:
{final_decision['details']}

Filtered Candidates: {len(filtered_out)}
"""
        with open(review_path, "w") as f:
            f.write(review_text)
        
        # Write manifest with NO_TRADE metadata
        manifest = {
            "run_id": run_id,
            "campaign": campaign,
            "config_checksum": config_checksum,
            "config_version": config.get("config_version", "N/A"),
            "run_time_utc": run_time_utc,
            "mode": "crash_venture_v1_snapshot",
            "inputs": {
                "snapshot_path": snapshot_path,
                "p_event": p_event,
                "spot_price": S0,
                "expiry_date": expiry,
                "days_to_expiry": days_to_expiry,
                "sigma": sigma,
                "min_debit_per_contract": min_debit_per_contract
            },
            "calibration": {
                "mu_calibrated": mu_calib,
                "p_achieved": p_achieved,
                "rng_seed": rng_seed
            },
            "n_candidates_generated": 0,
            "n_candidates_evaluated": 0,
            "n_non_dominated": 0,
            "n_output_structures": 0,
            "n_filtered_out": len(filtered_out),
            "decision": "NO_TRADE",
            "reason": no_trade_reason
        }
        
        manifest_path = run_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        
        # Write filter diagnostics
        if filtered_out:
            diagnostics_path = run_dir / "filter_diagnostics.json"
            with open(diagnostics_path, "w") as f:
                json.dump(filtered_out, f, indent=2)
        
        logger.info(f"✓ NO_TRADE artifacts written to: {run_dir}")
        
        # Return NO_TRADE result (structured, does NOT raise)
        return {
            "ok": True,
            "decision": "NO_TRADE",
            "reason": no_trade_reason,
            "warnings": [f"Filtered out {len(filtered_out)} candidates"],
            "candidates": [],
            "filtered_out": len(filtered_out),
            "filtered_reasons": [item.get("reason", "unknown") for item in filtered_out[:10]],
            "run_id": run_id,
            "run_dir": str(run_dir),
            "top_structures": [],
            "manifest": manifest,
            "debug": {
                "min_debit_per_contract": min_debit_per_contract,
                "moneyness_targets": moneyness_targets,
                "spread_widths": spread_widths
            }
        }
    
    # Evaluate all candidates
    logger.info(f"Evaluating {len(candidates)} candidates with {mc_config['paths']} Monte Carlo paths")
    evaluated = []
    
    for i, candidate in enumerate(candidates):
        try:
            # Add metadata (including conditioning provenance))
            candidate["spot_used"] = S0
            candidate["p_base"] = p_event
            candidate["p_adjusted"] = p_adjusted
            candidate["p_used"] = p_used
            candidate["conditioning"] = {
                "confidence_score": conditioning_result["confidence_score"],
                "p_source": conditioning_result["p_source"],
                "multipliers": conditioning_result["multipliers"],
                "regime_signals": conditioning_result["regime_signals"]
            }
            
            # Legacy field for backward compat
            candidate["assumed_p_event"] = p_used
            
            eval_result = evaluate_structure(
                structure=candidate,
                mu=mu_calib,
                sigma=sigma,
                S0=S0,
                T=T,
                n_paths=mc_config["paths"],
                seed=rng_seed + i
            )
            
            # Calculate EV per dollar using debit_per_contract (not max_loss_per_share)
            debit_per_contract = eval_result.get("debit_per_contract", 0)
            assert debit_per_contract > 0, f"debit_per_contract must be >0, got {debit_per_contract}"
            
            # EV per contract = EV per share * 100 (shares per contract)
            ev_per_contract = eval_result["ev"] * 100
            eval_result["ev_per_dollar"] = ev_per_contract / debit_per_contract
            
            evaluated.append(eval_result)
        except Exception as e:
            logger.error(f"Evaluation failed for candidate {i}: {e}")
            continue
    
    logger.info(f"Successfully evaluated {len(evaluated)} structures")
    
    if not evaluated:
        raise ValueError("No structures successfully evaluated")
    
    # Apply dominance filter
    non_dominated = filter_dominated_structures(evaluated)
    logger.info(f"After dominance filter: {len(non_dominated)} structures remain")
    
    if not non_dominated:
        raise ValueError("No non-dominated structures found")
    
    # Choose best structures
    constraints_dict = {
        "max_loss_usd_per_trade": constraints["max_loss_usd_per_trade"],
        "min_prob_profit": 0.0,
        "min_ev": 0.0
    }
    
    best_structures = choose_best_structure(
        non_dominated,
        constraints=constraints_dict,
        objective=objective
    )
    
    if not best_structures:
        raise ValueError("No structures meet constraints")
    
    # Rank top N
    top_n = constraints["top_n_output"]
    top_structures = rank_structures(best_structures, top_n=top_n)
    
    logger.info(f"Selected top {len(top_structures)} structures")
    
    # Add reason_selected
    for struct in top_structures:
        struct["reason_selected"] = get_reason_selected(
            struct,
            rank=struct["rank"],
            objective=objective
        )
    
    # SANITY CHECK: Non-zero validation
    logger.info("Running sanity checks...")
    for struct in top_structures:
        debit = struct.get("debit_per_contract", 0)
        max_loss = struct.get("max_loss_per_contract", 0)
        max_gain = struct.get("max_gain_per_contract", 0)
        
        if debit <= 0:
            raise AssertionError(f"Rank {struct['rank']}: debit_per_contract={debit:.2f} must be >0")
        if max_loss <= 0:
            raise AssertionError(f"Rank {struct['rank']}: max_loss_per_contract={max_loss:.2f} must be >0")
        if max_gain <= 0:
            raise AssertionError(f"Rank {struct['rank']}: max_gain_per_contract={max_gain:.2f} must be >0")
    
    logger.info("All sanity checks passed ✓")
    
    # Format outputs
    formatted_structures = [format_structure_output(s) for s in top_structures]
    
    # Write outputs
    output_config = struct_config.get("output", {})
    run_dir = Path("runs") / campaign / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    # Write structures.json
    if output_config.get("structures_json", True):
        json_path = run_dir / "structures.json"
        write_structures_json(formatted_structures, json_path)
    
    # Write summary.md
    if output_config.get("summary_md", True):
        md_path = run_dir / "summary.md"
        summary_metadata = {
            "run_id": run_id,
            "campaign": campaign,
            "p_event": p_event,
            "underlier": underlier,
            "spot_used": S0
        }
        write_summary_md(formatted_structures, md_path, summary_metadata)
    
    # Write dry-run tickets
    if output_config.get("dry_run_tickets", True):
        ticket_path = run_dir / "dry_run_tickets.txt"
        write_dry_run_tickets(top_structures, ticket_path)
    
    # Write manifest
    manifest = {
        "run_id": run_id,
        "campaign": campaign,
        "config_checksum": config_checksum,
        "config_version": config.get("config_version", "N/A"),
        "run_time_utc": run_time_utc,
        "mode": "crash_venture_v1_snapshot",
        "inputs": {
            "snapshot_path": snapshot_path,
            "p_event": p_event,
            "spot_price": S0,
            "expiry_date": expiry,
            "days_to_expiry": days_to_expiry,
            "sigma": sigma,
            "min_debit_per_contract": min_debit_per_contract
        },
        "calibration": {
            "mu_calibrated": mu_calib,
            "p_achieved": p_achieved,
            "rng_seed": rng_seed
        },
        "n_candidates_generated": len(candidates),
        "n_candidates_evaluated": len(evaluated),
        "n_non_dominated": len(non_dominated),
        "n_output_structures": len(top_structures),
        "n_filtered_out": len(filtered_out)
    }
    
    manifest_path = run_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    
    # Write filter diagnostics
    if filtered_out:
        diagnostics_path = run_dir / "filter_diagnostics.json"
        with open(diagnostics_path, "w") as f:
            json.dump(filtered_out, f, indent=2)
        logger.info(f"Filter diagnostics written to: {diagnostics_path}")
    
    logger.info(f"✓ Run complete: {run_id}")
    logger.info(f"✓ Output directory: {run_dir}")
    logger.info(f"✓ Top {len(top_structures)} structures written")
    
    return {
        "ok": True,
        "decision": "TRADE",
        "reason": "STRUCTURES_GENERATED",
        "warnings": [],
        "run_id": run_id,
        "run_dir": str(run_dir),
        "top_structures": formatted_structures,
        "manifest": manifest,
        "filtered_out": filtered_out
    }
