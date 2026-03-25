"""
Campaign Grid Runner - Generate candidates across multi-cell matrix.

Orchestrates structuring runs across (underlier × regime × expiry_bucket) cells
and produces a flat candidate list for portfolio-aware selection.

Convention: premium_usd = debit_per_contract * qty (no ×100)
"""

import json
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional
import yaml

from ..structuring.snapshot_io import load_snapshot, get_snapshot_metadata, compute_time_to_expiry
from ..utils.manifest import compute_config_checksum


logger = logging.getLogger(__name__)


def generate_campaign_run_id(campaign_config: Dict[str, Any]) -> str:
    """
    Generate unique run ID for campaign execution.
    
    Args:
        campaign_config: Campaign configuration dict
        
    Returns:
        Campaign run ID string
    """
    config_hash = hashlib.sha256(
        json.dumps(campaign_config, sort_keys=True).encode()
    ).hexdigest()[:12]
    
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    
    return f"campaign_v1_{config_hash}_{timestamp}"


def filter_candidates_by_dte(
    candidates: List[Dict[str, Any]],
    dte_min: int,
    dte_max: int,
    snapshot_time: str,
    expiry_key: str = "expiry"
) -> List[Dict[str, Any]]:
    """
    Filter candidates to DTE range for expiry bucket.
    
    Args:
        candidates: List of candidate dicts
        dte_min: Minimum DTE
        dte_max: Maximum DTE
        snapshot_time: Snapshot timestamp ISO string
        expiry_key: Key for expiry field (default: "expiry")
        
    Returns:
        Filtered candidate list
    """
    filtered = []
    
    for candidate in candidates:
        expiry = candidate.get(expiry_key)
        if not expiry:
            continue
        
        # Compute DTE
        dte_days = compute_time_to_expiry(snapshot_time, expiry) * 365
        
        if dte_min <= dte_days <= dte_max:
            filtered.append(candidate)
    
    return filtered


def flatten_candidate(
    candidate: Dict[str, Any],
    underlier: str,
    regime: str,
    expiry_bucket: str,
    cluster_id: str,
    cell_id: str,
    regime_p_implied: Optional[float] = None,
    regime_p_external: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Convert regime result candidate to canonical flat schema.
    
    PHASE 3 PATCH: Separates raw (from generator) and canonical (recomputed by campaign) EV fields.
    
    Canonical schema includes:
    - candidate_id
    - underlier, regime, expiry_bucket, cluster_id, cell_id
    - expiry, long_strike, short_strike
    - debit_per_contract
    - RAW fields (from generator): ev_per_dollar_raw, prob_profit_raw, ev_usd_raw
    - CANONICAL fields (recomputed): ev_per_dollar, ev_usd, p_used, p_used_src
    - representable, rank
    - spread_width (optional)
    - warnings (optional)
    
    Args:
        candidate: Candidate dict from structuring
        underlier: Underlier symbol
        regime: Regime name
        expiry_bucket: Expiry bucket name
        cluster_id: Cluster ID from campaign config
        cell_id: Cell identifier
        regime_p_implied: Regime-level implied probability
        regime_p_external: Regime-level external probability metadata
        
    Returns:
        Flattened candidate dict
    """
    # Extract strikes (strikes is a dict, not a list)
    strikes = candidate.get("strikes", {})
    long_strike = strikes.get("long_put")
    short_strike = strikes.get("short_put")
    
    # Calculate spread width
    spread_width = abs(long_strike - short_strike) if long_strike and short_strike else None
    
    # Extract and validate critical fields (NO SILENT DEFAULTS!)
    # Fields are directly on candidate dict, not nested in "metrics"
    debit = candidate.get("debit_per_contract")
    max_gain = candidate.get("max_gain_per_contract")
    
    if debit is None:
        raise ValueError(f"Missing debit_per_contract in candidate {candidate.get('candidate_id')}")
    if max_gain is None:
        raise ValueError(f"Missing max_gain_per_contract in candidate {candidate.get('candidate_id')}")
    
    # STORE RAW VALUES (from generator, may use different probability)
    ev_per_dollar_raw = candidate.get("ev_per_dollar")
    prob_profit_raw = candidate.get("prob_profit")
    ev_usd_raw = candidate.get("ev_usd")  # May not exist
    # TASK 2: ENFORCE COMPLETE SCHEMA - Extract and validate all canonical fields
    # NO SILENT DEFAULTS - fail loud if missing
    
    # CANONICAL PROBABILITY (p_used)
    # Determine which probability to use for canonical EV calculation
    # Priority: external (if authoritative) > implied > fallback
    
    p_used = None
    p_used_src = None
    p_impl = None
    p_ext = None
    p_ext_status = None
    p_ext_reason = None
    p_source = None  # Operational source (kalshi / options_implied / implied_spread / unknown)
    
    # Extract p_implied
    p_impl = candidate.get("p_implied")
    if p_impl is None and regime_p_implied is not None:
        p_impl = regime_p_implied
    
    # Extract p_external
    if regime_p_external and isinstance(regime_p_external, dict):
        if regime_p_external.get("authoritative"):
            p_ext = regime_p_external.get("p")
            p_ext_status = "OK"
            p_ext_reason = "Authoritative external source used"
        else:
            p_ext = regime_p_external.get("p")
            p_ext_status = "AUTH_FAIL"
            quality_data = regime_p_external.get("quality", {})
            warnings_list = quality_data.get("warnings", [])
            if warnings_list:
                p_ext_reason = f"Policy blocked: {', '.join(warnings_list)}"
            else:
                p_ext_reason = "Non-authoritative (policy criteria not met)"
    else:
        p_ext_status = "NO_MARKET"
        p_ext_reason = "No Kalshi market found or mapped for this event"
    
    # TASK 3: CLEAN PROBABILITY SEMANTICS
    # Determine p_used with priority logic AND set p_source (operational source)
    if p_ext is not None and regime_p_external and regime_p_external.get("authoritative"):
        # Priority 1: Authoritative external
        p_used = p_ext
        p_used_src = "external"  # Which probability was used
        p_source = regime_p_external.get("source", "kalshi")  # Operational source
    elif p_impl is not None:
        # Priority 2: Implied probability
        p_used = p_impl
        p_used_src = "implied"
        # Determine operational source for implied
        if candidate.get("p_event_result"):
            p_event_result = candidate.get("p_event_result", {})
            p_source = p_event_result.get("source", "options_implied")
        else:
            p_source = "options_implied"
    else:
        # Priority 3: Fallback (use what generator used)
        p_event_candidate = candidate.get("assumed_p_event")
        if p_event_candidate is not None:
            p_used = p_event_candidate
            p_used_src = "fallback"
            p_source = "unknown"  # Fallback has no operational source
        else:
            # No probability available - cannot compute canonical EV
            raise ValueError(
                f"Cannot determine p_used for candidate {candidate.get('candidate_id')}: "
                f"no external, implied, or fallback probability available"
            )
    
    # TASK 3: ASSERTION - If p_used_src == "external", then p_ext_status MUST == "OK"
    if p_used_src == "external" and p_ext_status != "OK":
        # This should never happen - if external is used, it must be authoritative
        raise ValueError(
            f"Inconsistent probability state for {candidate.get('candidate_id')}: "
            f"p_used_src='external' but p_ext_status='{p_ext_status}' (expected 'OK')"
        )
    
    # COMPUTE CANONICAL EV FIELDS using p_used
    # EV = p_used * max_gain - (1 - p_used) * debit
    ev_usd = p_used * max_gain - (1 - p_used) * debit
    ev_per_dollar = (ev_usd / debit) if debit > 0 else 0.0
    
    # Compute probability of profit (spread ITM)
    # For put spread: profit if underlier < short_strike at expiry
    # This is approximated as p_event (event = underlier drops below threshold)
    p_profit = p_used  # Simplified: assumes p_event ≈ P(spread ITM)
    
    # Task C (CCC v1.2 — EV mismatch warning hygiene):
    # Raw vs canonical EV divergence is expected when campaign recomputes EV
    # using a different probability source than the generator used.
    # Guard: only emit WARNING for extreme bug conditions; log INFO otherwise.
    if ev_per_dollar_raw is not None:
        ev_diff = abs(ev_per_dollar - ev_per_dollar_raw)
        if ev_diff > 0.01:  # More than 1 cent difference
            # Extreme bug condition: canonical EV is 0 or negative while raw is
            # large and positive AND external source was used (should be near-identical).
            _is_extreme_bug = (
                ev_per_dollar <= 0.0
                and ev_per_dollar_raw > 1.0
                and (p_used_src or "").startswith("external")
            )
            if _is_extreme_bug:
                # Loud: possible data corruption or probability assignment bug
                logger.warning(
                    f"EV/$ EXTREME MISMATCH (possible bug) in {candidate.get('candidate_id')}: "
                    f"canonical={ev_per_dollar:.3f}≤0 but raw={ev_per_dollar_raw:.3f}>1.0 "
                    f"with p_used_src='{p_used_src}' — canonical probability may be wrong"
                )
            else:
                # Normal: canonical EV differs from raw because campaign uses
                # a different (authoritative) probability.  Selector ranks by
                # canonical, so this divergence is expected — log at INFO only.
                logger.info(
                    f"EV/$ recomputed in {candidate.get('candidate_id')}: "
                    f"raw={ev_per_dollar_raw:.3f} → canonical={ev_per_dollar:.3f} "
                    f"(p_used={p_used:.3f} from {p_used_src})"
                )
    
    # TASK 5: PRESERVE PHASE 4 CONDITIONING PROVENANCE (pass-through only)
    # If upstream candidate includes conditioning, preserve it unchanged
    conditioning = candidate.get("conditioning")
    if conditioning:
        # Verify conditioning structure (basic validation only, no recomputation)
        if isinstance(conditioning, dict):
            # If conditioning present, use p_adjusted as p_used
            p_adjusted = conditioning.get("p_adjusted")
            if p_adjusted is not None:
                # Override p_used with conditioned value
                p_used = p_adjusted
                # Mark that conditioning was applied
                p_used_src = f"{p_used_src}_conditioned"
        else:
            # Malformed conditioning - log warning but don't fail
            logger.warning(f"Malformed conditioning block in {candidate.get('candidate_id')}, ignoring")
            conditioning = None
    
    # Extract full probability metadata for provenance tracking
    # Rule: If unknown, set to None (not missing) - never drop fields
    
    # p_event_used: LEGACY - map to p_used for backwards compatibility
    p_event_used = p_used
    # p_confidence: Optional confidence score
    p_confidence = None
    p_event_result = candidate.get("p_event_result")
    if p_event_result and isinstance(p_event_result, dict):
        p_confidence = p_event_result.get("confidence")
    
    # p_external_metadata: Full provenance object (comprehensive Kalshi details)
    # This enables weekly "why we weren't anchored" analysis
    p_external_metadata = None
    
    if regime_p_external and isinstance(regime_p_external, dict):
        # Extract full metadata from regime p_external block
        market_data = regime_p_external.get("market") or {}
        match_data = regime_p_external.get("match") or {}
        quality_data = regime_p_external.get("quality") or {}
        
        p_external_metadata = {
            "value": regime_p_external.get("p"),
            "source": regime_p_external.get("source"),
            "asof_ts_utc": regime_p_external.get("asof_ts_utc"),
            "authoritative": regime_p_external.get("authoritative", False),
            # Market details
            "market_ticker": market_data.get("ticker") if market_data else None,
            "market_id": market_data.get("market_id") if market_data else None,
            "market_title": market_data.get("title") if market_data else None,
            # Mapping quality
            "exact_match": match_data.get("exact_match", False) if match_data else False,
            "proxy_used": match_data.get("proxy_used", False) if match_data else False,
            "match_reason": match_data.get("match_reason") if match_data else None,
            "mapping_confidence": match_data.get("mapping_confidence") if match_data else None,
            # Quality indicators
            "liquidity_ok": quality_data.get("liquidity_ok") if quality_data else None,
            "staleness_ok": quality_data.get("staleness_ok") if quality_data else None,
            "spread_ok": quality_data.get("spread_ok") if quality_data else None,
            "warnings": quality_data.get("warnings", []) if quality_data else []
        }
    
    # TASK 2: Build canonical record with COMPLETE SCHEMA
    # ALL canonical fields MUST be present (fail loud if missing)
    flat_candidate = {
        # Identifiers
        "candidate_id": candidate.get("candidate_id", f"{cell_id}_{candidate.get('rank', 0)}"),
        "underlier": underlier,
        "regime": regime,
        "expiry_bucket": expiry_bucket,
        "cluster_id": cluster_id,
        "cell_id": cell_id,
        
        # Position details
        "expiry": candidate.get("expiry"),
        "long_strike": long_strike,
        "short_strike": short_strike,
        
        # Economics - RAW (from generator, may use different probability)
        "debit_per_contract": debit,
        "max_gain_per_contract": max_gain,
        "ev_per_dollar_raw": ev_per_dollar_raw,
        "prob_profit_raw": prob_profit_raw,
        "ev_usd_raw": ev_usd_raw,
        
        # Economics - CANONICAL (recomputed using p_used) - REQUIRED
        "ev_per_dollar": ev_per_dollar,  # CANONICAL - use for scoring
        "ev_usd": ev_usd,
        "p_profit": p_profit,
        
        # Probability metadata - CANONICAL - REQUIRED
        "p_used": p_used,           # The probability used for canonical EV - REQUIRED
        "p_used_src": p_used_src,   # "external" | "implied" | "fallback" - REQUIRED
        "p_impl": p_impl,           # Options-implied probability (can be None)
        "p_ext": p_ext,             # External (Kalshi) probability (can be None)
        "p_ext_status": p_ext_status, # "OK" | "AUTH_FAIL" | "NO_MARKET" - REQUIRED
        "p_ext_reason": p_ext_reason, # REQUIRED
        "p_event": p_used,          # Alias for backwards compatibility
        
        # TASK 3: Probability provenance (CLEAN SEMANTICS)
        "p_source": p_source,       # Operational source: kalshi / options_implied / implied_spread / unknown
        
        # Legacy fields (for backwards compatibility)
        "p_event_used": p_event_used,
        "p_implied": p_impl,
        "p_external": p_ext,
        "prob_profit": p_profit,
        "p_confidence": p_confidence,
        
        # Full p_external provenance (for weekly analysis and diagnostics)
        "p_external_metadata": p_external_metadata,
        "p_external_status": p_ext_status,
        "p_external_reason": p_ext_reason,
        
        # TASK 2: Robustness fields (required for selector)
        "robustness": None,  # Computed by selector
        "robustness_flags": [],
        
        # TASK 5: Conditioning provenance (pass-through if present)
        "conditioning": conditioning if conditioning else None,
        
        # Metadata
        "representable": candidate.get("representable", True),
        "rank": candidate.get("rank", 0),
        "spread_width": spread_width,
        "warnings": candidate.get("warnings", []),
        
        # Source tracking
        "source_candidate": candidate.get("candidate_id"),
    }
    
    # TASK 2: VALIDATE CANONICAL FIELDS (fail loud on missing required fields)
    required_fields = ["ev_per_dollar", "ev_usd", "p_used", "p_used_src", "p_ext_status", "p_ext_reason", "p_source"]
    for field in required_fields:
        if flat_candidate.get(field) is None and field in ["ev_per_dollar", "ev_usd", "p_used", "p_used_src", "p_ext_status", "p_ext_reason", "p_source"]:
            raise ValueError(
                f"Missing required canonical field '{field}' in candidate {candidate.get('candidate_id')}"
            )
    
    return flat_candidate


def run_campaign_grid(
    campaign_config_path: str,
    structuring_config_path: str,
    p_external_by_underlier: Optional[Dict[str, float]] = None,
    min_debit_per_contract: float = 10.0,
    snapshot_dir: str = "snapshots",
    dte_min: int = 30,
    dte_max: int = 60,
    tail_moneyness_floor: Optional[float] = None
) -> str:
    """
    Execute campaign grid across all cells and produce flat candidate list.
    
    MULTI-UNDERLIER: Creates fresh snapshot for each underlier (no reuse).
    REPRESENTABILITY FIX: DTE aligned to campaign config (30-60), tail coverage auto-calculated.
    
    Args:
        campaign_config_path: Path to campaign YAML config
        structuring_config_path: Path to structuring config (crash_venture_v2.yaml)
        p_external_by_underlier: Optional external probabilities by underlier
        min_debit_per_contract: Minimum debit filter
        snapshot_dir: Directory for snapshot storage
        dte_min: Minimum days to expiry for snapshots (default: 30, aligned with campaign)
        dte_max: Maximum days to expiry for snapshots (default: 60, aligned with campaign)
        tail_moneyness_floor: Override tail coverage (default: auto-calculated from regime thresholds)
        
    Returns:
        Path to candidates_flat.json
    """
    logger.info("=" * 80)
    logger.info("CAMPAIGN GRID RUNNER")
    logger.info("=" * 80)
    
    # Load campaign config
    with open(campaign_config_path, "r") as f:
        campaign_config = yaml.safe_load(f)
    
    # Load structuring config
    with open(structuring_config_path, "r") as f:
        structuring_config = yaml.safe_load(f)
    
    # Extract campaign parameters
    underliers = campaign_config["underliers"]
    regimes_config = campaign_config["regimes"]
    expiry_buckets = campaign_config["expiry_buckets"]
    cluster_map = campaign_config["cluster_map"]
    
    logger.info(f"Underliers: {underliers}")
    logger.info(f"Regimes: {[r['name'] for r in regimes_config]}")
    logger.info(f"Expiry Buckets: {[b['name'] for b in expiry_buckets]}")
    logger.info("")
    
    # Generate campaign run ID
    campaign_run_id = generate_campaign_run_id(campaign_config)
    run_dir = Path(f"runs/campaign/{campaign_run_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Campaign Run ID: {campaign_run_id}")
    logger.info(f"Run Directory: {run_dir}")
    logger.info("")
    
    # Initialize flat candidate list
    candidates_flat = []
    
    # Cell tracking
    cells_processed = []
    
    # Calculate required tail coverage from regime thresholds
    # Need deepest regime threshold + buffer for representability
    max_abs_threshold = max(abs(r["threshold"]) for r in regimes_config)
    if tail_moneyness_floor is None:
        # Auto-calculate: deepest regime + 5% buffer
        tail_moneyness_floor = max_abs_threshold + 0.05
        logger.info(f"Auto-calculated tail coverage: {tail_moneyness_floor:.2%} (deepest regime: {max_abs_threshold:.2%} + 5% buffer)")
    else:
        logger.info(f"Using explicit tail coverage: {tail_moneyness_floor:.2%}")
    
    # Ensure snapshot directory exists
    snapshot_path_obj = Path(snapshot_dir)
    snapshot_path_obj.mkdir(parents=True, exist_ok=True)
    logger.info(f"Snapshot directory: {snapshot_path_obj.absolute()}")
    
    # Iterate over grid cells
    # MULTI-UNDERLIER FIX: Create fresh snapshot per underlier (no reuse)
    import time
    
    for underlier in underliers:
        logger.info(f"=" * 60)
        logger.info(f"UNDERLIER: {underlier}")
        logger.info(f"=" * 60)
        
        # CREATE FRESH SNAPSHOT FOR THIS UNDERLIER WITH RETRY LOGIC
        from ..ibkr.snapshot import IBKRSnapshotExporter
        
        # Generate snapshot filename with underlier
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_filename = f"{underlier}_snapshot_{timestamp}.json"
        snapshot_path = str(Path(snapshot_dir) / snapshot_filename)
        
        logger.info(f"Creating fresh snapshot: {snapshot_path}")
        
        # Retry logic for IBKR connection
        max_retries = 3
        retry_delay = 5  # seconds
        snapshot_created = False
        exporter = None
        
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Connection attempt {attempt}/{max_retries}")
                
                # Create snapshot exporter with unique client ID per attempt
                client_id = 1 + (attempt - 1)
                exporter = IBKRSnapshotExporter(host="127.0.0.1", port=7496, client_id=client_id)
                exporter.connect()
                
                # Export snapshot for this underlier
                exporter.export_snapshot(
                    underlier=underlier,
                    snapshot_time_utc=datetime.now(timezone.utc).isoformat(),
                    dte_min=dte_min,
                    dte_max=dte_max,
                    tail_moneyness_floor=tail_moneyness_floor,
                    out_path=snapshot_path
                )
                
                exporter.disconnect()
                logger.info(f"✓ Fresh snapshot created: {snapshot_path}")
                snapshot_created = True
                break
                
            except Exception as e:
                logger.error(f"❌ Attempt {attempt} failed for {underlier}: {e}", exc_info=True)
                
                # Ensure cleanup
                if exporter and exporter.ib.isConnected():
                    try:
                        exporter.disconnect()
                    except:
                        pass
                
                # If not last attempt, wait before retry
                if attempt < max_retries:
                    wait_time = retry_delay * attempt  # Exponential backoff
                    logger.info(f"⏳ Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ All {max_retries} attempts failed for {underlier}")
        
        # If snapshot creation failed after all retries, skip this underlier
        if not snapshot_created:
            logger.warning(f"⚠️  Skipping {underlier} due to snapshot creation failure")
            continue
        
        # Small delay between underliers to avoid overwhelming IBKR
        if underlier != underliers[-1]:  # Don't wait after last underlier
            logger.info("⏳ Waiting 2s before next underlier...")
            time.sleep(2)
        
        # Load the snapshot we just created
        snapshot = load_snapshot(snapshot_path)
        metadata = get_snapshot_metadata(snapshot)
        snapshot_time = metadata['snapshot_time']
        spot = metadata['current_price']
        
        # TASK 6: SNAPSHOT ISOLATION RUNTIME GUARD
        # CRITICAL: Snapshot underlier must match cell underlier (prevent contamination)
        snapshot_symbol = metadata.get('underlier')
        if snapshot_symbol != underlier:
            raise ValueError(
                f"SNAPSHOT UNDERLIER MISMATCH: expected '{underlier}', "
                f"got '{snapshot_symbol}' in {snapshot_path}. "
                f"This would cause cross-underlier contamination and invalid candidate generation."
            )
        
        logger.info(f"✓ Snapshot validation passed: underlier={snapshot_symbol}")
        logger.info(f"Processing {underlier}: spot=${spot:.2f}")
        
        # Get cluster ID
        cluster_id = cluster_map.get(underlier, "UNKNOWN")
        
        for regime_config in regimes_config:
            regime_name = regime_config["name"]
            regime_threshold = regime_config["threshold"]
            
            logger.info(f"  Regime: {regime_name} (threshold={regime_threshold:.2%})")
            
            # Get p_external for this underlier
            p_external = None
            if p_external_by_underlier:
                p_external = p_external_by_underlier.get(underlier)
            
            # Run structuring for this regime
            # Reuse run_regime from regime_orchestration (imported from run_daily_v2 logic)
            # NOTE: We need to adapt this to work with our needs
            # For now, we'll use a simplified inline version
            
            from ..core.regime import apply_regime_overrides
            
            # Apply regime overrides
            regime_structuring_config = apply_regime_overrides(structuring_config, regime_name)
            
            # Import run_regime equivalent logic
            # For simplicity, we'll call the existing run_regime function if possible
            # Otherwise, we need to inline the structuring logic
            
            try:
                from scripts.run_daily_v2 import run_regime
                
                # Generate temporary run ID for this cell
                cell_run_id = f"{campaign_run_id}_{underlier}_{regime_name}"
                
                # Call run_regime
                # Build p_event_external block if p_external provided
                p_event_external_block = None
                if p_external is not None:
                    p_event_external_block = {
                        "p": p_external,
                        "source": "campaign_provided",
                        "authoritative": True,
                        "asof_ts_utc": datetime.now(timezone.utc).isoformat(),
                        "market": None,
                        "match": None,
                        "quality": {
                            "liquidity_ok": True,
                            "staleness_ok": True,
                            "spread_ok": True,
                            "warnings": []
                        }
                    }
                
                regime_result = run_regime(
                    regime=regime_name,
                    config=structuring_config,
                    snapshot=snapshot,
                    snapshot_path=snapshot_path,
                    p_event_external=p_event_external_block,
                    min_debit_per_contract=min_debit_per_contract,
                    run_id=cell_run_id
                )
                
                # Get candidates and regime-level probability metadata from result
                cell_candidates = regime_result.candidates
                regime_p_implied = regime_result.p_implied
                regime_p_external_block = regime_result.p_event_external
                
                if not cell_candidates:
                    logger.info(f"    No candidates generated")
                    continue
                
                logger.info(f"    Generated {len(cell_candidates)} candidates")
                logger.info(f"    Regime p_implied: {regime_p_implied}")
                logger.info(f"    Regime p_external: {regime_p_external_block}")
                
                # Filter by expiry buckets
                for bucket in expiry_buckets:
                    bucket_name = bucket["name"]
                    bucket_dte_min = bucket["dte_min"]
                    bucket_dte_max = bucket["dte_max"]
                    
                    # Filter candidates to bucket DTE range
                    bucket_candidates = filter_candidates_by_dte(
                        candidates=cell_candidates,
                        dte_min=bucket_dte_min,
                        dte_max=bucket_dte_max,
                        snapshot_time=snapshot_time
                    )
                    
                    # PHASE 5: Flatten candidates FIRST (required for accounting)
                    cell_id = f"{underlier}_{regime_name}_{bucket_name}"
                    
                    # Flatten candidates to get p_used_src properly
                    flattened_bucket_candidates = []
                    for i, candidate in enumerate(bucket_candidates):
                        flat_candidate = flatten_candidate(
                            candidate=candidate,
                            underlier=underlier,
                            regime=regime_name,
                            expiry_bucket=bucket_name,
                            cluster_id=cluster_id,
                            cell_id=cell_id,
                            regime_p_implied=regime_p_implied,
                            regime_p_external=regime_p_external_block
                        )
                        flattened_bucket_candidates.append(flat_candidate)
                    
                    # TASK 1: FIX p_used_breakdown accounting (use flattened candidates)
                    # Read from candidate['p_used_src'] directly, normalize _conditioned variants
                    p_used_breakdown = {"external": 0, "implied": 0, "fallback": 0}
                    p_ext_status_breakdown = {"OK": 0, "NO_MARKET": 0, "AUTH_FAIL": 0, "BLOCKED": 0}
                    
                    for flat_cand in flattened_bucket_candidates:
                        # TASK 1: Normalize p_used_src (strip _conditioned suffix)
                        p_used_src = flat_cand.get("p_used_src", "unknown")
                        if p_used_src.endswith("_conditioned"):
                            # Map external_conditioned -> external, etc.
                            p_used_src = p_used_src.replace("_conditioned", "")
                        
                        if p_used_src in p_used_breakdown:
                            p_used_breakdown[p_used_src] += 1
                        
                        # p_ext_status is computed in flatten_candidate
                        p_ext_status = flat_cand.get("p_ext_status", "NO_MARKET")
                        if p_ext_status in p_ext_status_breakdown:
                            p_ext_status_breakdown[p_ext_status] += 1
                    
                    # Determine dominant rejection reason if 0 survivors
                    dominant_rejection = "N/A"
                    if len(bucket_candidates) == 0:
                        # Check if it was DTE filter or no candidates generated
                        if len(cell_candidates) > 0:
                            dominant_rejection = f"DTE_FILTER (0 candidates in {bucket_dte_min}-{bucket_dte_max} DTE range)"
                        else:
                            dominant_rejection = "NO_CANDIDATES_GENERATED"
                    
                    # PHASE 5: Print per-cell accounting log (MUST BE PRINTED)
                    print(f"[CELL_ACCOUNTING] "
                          f"cell_id={cell_id} | "
                          f"underlier={underlier} | "
                          f"regime={regime_name} | "
                          f"expiry_bucket={bucket_name} | "
                          f"generated_count={len(cell_candidates)} | "
                          f"after_guards_count={len(cell_candidates)} | "
                          f"after_filter_count={len(bucket_candidates)} | "
                          f"p_used_breakdown={p_used_breakdown} | "
                          f"p_ext_status_breakdown={p_ext_status_breakdown} | "
                          f"dominant_rejection={dominant_rejection}")
                    
                    if not bucket_candidates:
                        logger.info(f"      Bucket {bucket_name}: 0 candidates")
                        continue
                    
                    # DIAGNOSTIC: Print cell details
                    # Calculate strike range from bucket candidates
                    all_strikes = []
                    for cand in bucket_candidates:
                        strikes = cand.get("strikes", {})
                        if strikes.get("long_put"):
                            all_strikes.append(strikes["long_put"])
                        if strikes.get("short_put"):
                            all_strikes.append(strikes["short_put"])
                    
                    min_strike = min(all_strikes) if all_strikes else None
                    max_strike = max(all_strikes) if all_strikes else None
                    
                    # Get representative expiry from first candidate
                    expiry = bucket_candidates[0].get("expiry", "N/A")
                    
                    print(f"[CELL] {underlier} spot={spot:.2f} expiry={expiry} strikes_min={min_strike} strikes_max={max_strike}")
                    
                    logger.info(f"      Bucket {bucket_name}: {len(bucket_candidates)} candidates")
                    
                    # TASK 3: Add Kalshi mapping debug for NO_MARKET cases (PHASE 6.5 ENHANCED)
                    for flat_candidate in flattened_bucket_candidates:
                        p_ext_status = flat_candidate.get("p_ext_status")
                        if p_ext_status and p_ext_status != "OK":
                            # PHASE 6.5: Extract enhanced diagnostics if available
                            p_ext_metadata = flat_candidate.get("p_external_metadata") or {}
                            p_ext_reason = flat_candidate.get("p_ext_reason", "Unknown")
                            
                            # PHASE 6.5: Get diagnostics from metadata (if available from multi_series_search)
                            diagnostics = p_ext_metadata.get("diagnostics", {})
                            
                            # Extract event parameters
                            expiry_val = flat_candidate.get("expiry", "unknown")
                            threshold = regime_threshold
                            
                            # TASK 3 FIX: Use dynamic series discovery instead of hardcoded mapping
                            # Import series discovery function
                            from ..kalshi.multi_series_adapter import discover_series_for_underlier
                            from ..kalshi.client import KalshiClient
                            
                            # Discover available series for this underlier
                            try:
                                kalshi_client = KalshiClient()
                                discovered_series = discover_series_for_underlier(kalshi_client, underlier)
                                target_series_list = discovered_series if discovered_series else []
                                logger.info(f"Discovered Kalshi series for {underlier}: {target_series_list}")
                            except Exception as e:
                                logger.warning(f"Series discovery failed for {underlier}: {e}, using defaults")
                                # Fallback to legacy hardcoded mapping
                                kalshi_symbol_map = {"SPY": "KXINX", "QQQ": "KXNDX", "SPX": "KXINX", "NDX": "KXNDX"}
                                fallback_series = kalshi_symbol_map.get(underlier, underlier)
                                target_series_list = [fallback_series]
                            
                            # Calculate target threshold level
                            target_threshold = spot * (1 + threshold) if threshold < 0 else spot * threshold
                            
                            # PHASE 6.5: Extract closest match info from diagnostics
                            closest_match = diagnostics.get("closest_match")
                            best_match_dict = None
                            if closest_match:
                                best_match_dict = {
                                    "series": closest_match.get("series"),
                                    "ticker": closest_match.get("ticker"),
                                    "error_pct": closest_match.get("mapping_error_pct"),
                                    "implied_level": closest_match.get("implied_level")
                                }
                            
                            kalshi_mapping_debug = {
                                "target_underlier": underlier,  # FIXED: Use underlier directly
                                "target_expiry": expiry_val,
                                "target_threshold": float(target_threshold),
                                "threshold_pct": float(threshold),
                                "max_mapping_error": 0.10,
                                "series_tried": diagnostics.get("series_searched", target_series_list),
                                "market_status_tried": ["open", "closed"],
                                "returned_markets_count": diagnostics.get("total_markets_fetched", 0),  # PHASE 6.5
                                "markets_by_series": diagnostics.get("markets_by_series", {}),  # PHASE 6.5
                                "best_match": best_match_dict,  # PHASE 6.5: Now has data!
                                "failure_reason": p_ext_reason,
                                "status": p_ext_status
                            }
                            
                            flat_candidate["kalshi_mapping_debug"] = kalshi_mapping_debug
                            
                            # PHASE 6.5: Enhanced console output with diagnostics
                            if p_ext_status == "NO_MARKET":
                                target_level = target_threshold
                                markets_count = diagnostics.get("total_markets_fetched", 0)
                                series_list = diagnostics.get("series_searched", target_series_list)
                                
                                # Format closest match info
                                closest_str = "none"
                                if closest_match:
                                    closest_str = f"{closest_match.get('ticker')} (error={closest_match.get('mapping_error_pct', 0):.1f}%)"
                                
                                print(f"[KALSHI_DEBUG] {flat_candidate['candidate_id']}: "
                                      f"tried SERIES={series_list} "
                                      f"returned_markets={markets_count} "
                                      f"target_level={target_level:.0f} "
                                      f"closest_match={closest_str} "
                                      f"reason='{p_ext_reason}'")
                        
                        candidates_flat.append(flat_candidate)
                    
                    # Track cell
                    cells_processed.append({
                        "cell_id": cell_id,
                        "underlier": underlier,
                        "regime": regime_name,
                        "expiry_bucket": bucket_name,
                        "cluster_id": cluster_id,
                        "candidates_count": len(bucket_candidates)
                    })
                
            except Exception as e:
                logger.error(f"    Cell processing failed: {e}", exc_info=True)
                continue
        
        logger.info("")
    
    # Write artifacts
    logger.info("=" * 80)
    logger.info("WRITING CAMPAIGN ARTIFACTS")
    logger.info("=" * 80)
    
    # Write manifest
    manifest = {
        "campaign_run_id": campaign_run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "campaign_config": campaign_config,
        "cells_processed": cells_processed,
        "total_candidates": len(candidates_flat),
        "snapshot_dir": snapshot_dir,
    }
    
    manifest_path = run_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    
    logger.info(f"✓ Manifest: {manifest_path}")
    
    # Write flat candidates
    candidates_flat_path = run_dir / "candidates_flat.json"
    with open(candidates_flat_path, "w") as f:
        json.dump(candidates_flat, f, indent=2)
    
    logger.info(f"✓ Candidates Flat: {candidates_flat_path}")
    logger.info(f"  Total candidates: {len(candidates_flat)}")
    logger.info(f"  Cells processed: {len(cells_processed)}")
    logger.info("")
    
    # Write cell-level artifacts (optional)
    cells_dir = run_dir / "cells"
    cells_dir.mkdir(exist_ok=True)
    
    for cell_info in cells_processed:
        cell_id = cell_info["cell_id"]
        cell_candidates = [c for c in candidates_flat if c["cell_id"] == cell_id]
        
        cell_path = cells_dir / f"{cell_id}.json"
        with open(cell_path, "w") as f:
            json.dump({
                "cell_info": cell_info,
                "candidates": cell_candidates
            }, f, indent=2)
    
    logger.info(f"✓ Cell artifacts: {cells_dir} ({len(cells_processed)} files)")
    logger.info("")
    logger.info("=" * 80)
    logger.info("CAMPAIGN GRID COMPLETE")
    logger.info("=" * 80)
    logger.info("")
    
    return str(candidates_flat_path)


if __name__ == "__main__":
    # Demo usage
    import argparse
    
    parser = argparse.ArgumentParser(description="Run campaign grid")
    parser.add_argument("--campaign-config", required=True, help="Path to campaign config YAML")
    parser.add_argument("--structuring-config", required=True, help="Path to structuring config YAML")
    parser.add_argument("--snapshot-dir", default="snapshots", help="Directory for snapshots")
    parser.add_argument("--dte-min", type=int, default=20, help="Minimum DTE for snapshots")
    parser.add_argument("--dte-max", type=int, default=60, help="Maximum DTE for snapshots")
    parser.add_argument("--tail-moneyness-floor", type=float, default=0.18, help="Tail moneyness floor")
    
    args = parser.parse_args()
    
    # Run grid
    candidates_path = run_campaign_grid(
        campaign_config_path=args.campaign_config,
        structuring_config_path=args.structuring_config,
        snapshot_dir=args.snapshot_dir,
        dte_min=args.dte_min,
        dte_max=args.dte_max,
        tail_moneyness_floor=args.tail_moneyness_floor
    )
    
    print(f"Candidates written to: {candidates_path}")
