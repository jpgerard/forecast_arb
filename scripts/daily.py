"""
Interactive Daily Console - Single Command Operator Workflow

This script provides a single interactive console for daily trading operations:
1. Runs v2 daily orchestration to produce run_dir and review_candidates.json
2. Loads nested review_candidates schema (regimes -> <regime> -> candidates)
3. Prints compact candidate table
4. Selects default candidate rank=1 for each eligible regime (or user chooses)
5. Performs quote-only via existing execute_trade logic, enforcing guards
6. If quote-only returns OK_TO_STAGE, offers:
   - emit intent (deterministic intent_id and file path in intents/)
   - stage paper
   - transmit live (requires typing SEND)
7. Prints final receipt block with: intent_id, order_id, status, limit, run_id, ledger paths
8. No silent no-ops: if any step produces no artifact, exit non-zero with explicit error

REQUIREMENTS:
- Do not introduce new strategy features
- Do not change candidate selection logic beyond interactive selection
- Add minimal tests for: no candidate -> NO_TRADE exit; quote-only pass -> offers stage; live requires SEND
"""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

# Add parent to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Prepare environment with PYTHONPATH for subprocesses
SUBPROCESS_ENV = os.environ.copy()
SUBPROCESS_ENV['PYTHONPATH'] = str(PROJECT_ROOT) + os.pathsep + SUBPROCESS_ENV.get('PYTHONPATH', '')

# NOTE: daily.py DOES NOT WRITE outcomes - only execute_trade.py writes events
# daily.py only prints receipts and paths for manual inspection
# daily.py calls intent_builder CLI to construct intents (no manual construction)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CCC v1.9: stale pending threshold (operator hygiene)
# ---------------------------------------------------------------------------
_STALE_PENDING_DAYS: int = 2  # flag pending intents older than this many days


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Suppress noisy batch runner internals unless verbose
    if not verbose:
        logging.getLogger("forecast_arb").setLevel(logging.ERROR)
        logging.getLogger("__main__").setLevel(logging.WARNING)


def find_latest_run_dir(strategy: str = "crash_venture_v2") -> Optional[str]:
    """
    Find the latest run directory by timestamp in directory name.
    
    Args:
        strategy: Strategy name (default: "crash_venture_v2")
        
    Returns:
        Path to latest run directory, or None if not found
    """
    runs_base = Path("runs") / strategy
    
    if not runs_base.exists():
        return None
    
    # Find all run directories
    run_dirs = [d for d in runs_base.iterdir() if d.is_dir()]
    
    if not run_dirs:
        return None
    
    # Sort by directory name (which includes timestamp)
    run_dirs.sort(reverse=True)
    
    return str(run_dirs[0])


def run_daily_orchestration(
    regime: str = "crash",
    config_path: str = "configs/structuring_crash_venture_v2.yaml",
    snapshot_path: Optional[str] = None,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7496,
    dte_min: int = 30,
    dte_max: int = 60,
    min_debit: float = 10.0,
    fallback_p: float = 0.30
) -> Tuple[str, str]:
    """
    Run v2 daily orchestration using run_daily_v2.py.
    
    Determines latest run_dir by directory timestamp (not stdout parsing).
    
    Returns:
        Tuple of (run_dir, review_candidates_path)
    """
    logger.info("=" * 80)
    logger.info("STEP 1: DAILY ORCHESTRATION")
    logger.info("=" * 80)
    
    # Build command
    cmd = [
        sys.executable,
        "scripts/run_daily_v2.py",
        "--regime", regime,
        "--campaign-config", config_path,
        "--ibkr-host", ibkr_host,
        "--ibkr-port", str(ibkr_port),
        "--dte-min", str(dte_min),
        "--dte-max", str(dte_max),
        "--min-debit-per-contract", str(min_debit),
        "--fallback-p", str(fallback_p),
        "--p-event-source", "kalshi-auto"
    ]
    
    if snapshot_path:
        cmd.extend(["--snapshot", snapshot_path])
    
    logger.info(f"Running: {' '.join(cmd)}")
    
    # Run orchestration
    result = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROCESS_ENV)
    
    if result.returncode != 0:
        logger.error(f"❌ Orchestration failed:\n{result.stderr}")
        sys.exit(1)
    
    # Determine latest run_dir by directory timestamp
    run_dir = find_latest_run_dir("crash_venture_v2")
    
    if not run_dir or not Path(run_dir).exists():
        logger.error(f"❌ Failed to locate run_dir - no directories found in runs/crash_venture_v2/")
        sys.exit(1)
    
    # Locate review_candidates.json
    review_candidates_path = Path(run_dir) / "artifacts" / "review_candidates.json"
    
    if not review_candidates_path.exists():
        logger.error(f"❌ review_candidates.json not found at: {review_candidates_path}")
        sys.exit(1)
    
    logger.info(f"✓ Run Dir: {run_dir}")
    logger.info(f"✓ Review Candidates: {review_candidates_path}")
    logger.info("")
    
    return str(run_dir), str(review_candidates_path)


def load_review_candidates(review_candidates_path: str) -> Dict[str, Any]:
    """
    Load review_candidates.json.
    
    Schema: {"regimes": {"<regime>": {"candidates": [...], ...}, ...}}
    
    Returns:
        Review candidates dict
    """
    with open(review_candidates_path, "r") as f:
        data = json.load(f)
    
    if "regimes" not in data:
        logger.error(f"❌ Invalid review_candidates.json: missing 'regimes' key")
        sys.exit(1)
    
    return data


def calculate_ev_at_probability(p: float, debit: float, max_gain: float) -> Tuple[float, float]:
    """
    Calculate EV and EV/$ at a given probability.
    
    Args:
        p: Probability of event occurring
        debit: Debit per contract (cost)
        max_gain: Max gain per contract
        
    Returns:
        Tuple of (ev, ev_per_dollar)
    """
    # For put spread: if event occurs, gain = max_gain; if not, loss = -debit
    ev = p * max_gain - (1 - p) * debit
    ev_per_dollar = (ev / debit) if debit > 0 else 0
    return ev, ev_per_dollar


def print_regime_probability_context(regime_data: Dict[str, Any], regime_name: str) -> None:
    """
    Print regime-level probability context with provenance, edge, and sanity checks.
    
    Args:
        regime_data: Regime data from review_candidates
        regime_name: Name of regime (e.g., "crash")
    """
    print("")
    print("=" * 100)
    print(f"REGIME PROBABILITY CONTEXT: {regime_name}")
    print("=" * 100)
    
    # Extract probability metadata
    p_event_external = regime_data.get("p_event_external")
    p_implied = regime_data.get("p_implied")
    p_implied_confidence = regime_data.get("p_implied_confidence", 0.0)
    
    # Get assumed_p_event from first candidate (all candidates in regime use same assumption)
    candidates = regime_data.get("candidates", [])
    assumed_p = candidates[0].get("assumed_p_event") if candidates else None
    
    # Display External (Kalshi) probability
    if p_event_external:
        p_ext = p_event_external.get("p")
        source = p_event_external.get("source", "unknown")
        authoritative = p_event_external.get("authoritative", False)
        market_info = p_event_external.get("market", {})
        match_info = p_event_external.get("match", {})
        
        market_ticker = market_info.get("ticker", "N/A")
        exact_match = match_info.get("exact_match", False)
        
        print(f"External ({source.title()}): {p_ext:6.3f} ({p_ext*100:5.1f}%)  "
              f"[{market_ticker}, exact_match={exact_match}]")
        
        if authoritative:
            print(f"Status:              AUTHORITATIVE ✓")
        else:
            print(f"Status:              NON-AUTHORITATIVE (policy blocked)")
            warnings = p_event_external.get("quality", {}).get("warnings", [])
            if warnings:
                print(f"Warnings:            {', '.join(warnings)}")
    else:
        print(f"External (Kalshi):   N/A")
    
    # Display Implied (Options) probability
    if p_implied is not None:
        print(f"Implied (Options):   {p_implied:6.3f} ({p_implied*100:5.1f}%)  "
              f"[confidence={p_implied_confidence:.2f}]")
    else:
        print(f"Implied (Options):   N/A")
    
    # Calculate and display edge
    if p_event_external and p_implied is not None:
        p_ext = p_event_external.get("p")
        edge_decimal = p_ext - p_implied
        edge_bps = edge_decimal * 10000
        edge_sign = "+" if edge_bps >= 0 else ""
        print(f"Edge:                {edge_sign}{edge_bps:6.0f} bps  (External - Implied)")
    else:
        print(f"Edge:                N/A")
    
    # Display assumed probability for EV calculation
    if assumed_p is not None:
        # Determine source of assumed_p
        if p_event_external and p_event_external.get("authoritative"):
            p_source = f"external ({p_event_external.get('source')})"
        else:
            p_source = "fallback"
        
        print(f"Assumed for EV:      {assumed_p:6.3f} ({assumed_p*100:5.1f}%)  [source: {p_source}]")
    else:
        print(f"Assumed for EV:      N/A")
    
    # Crash regime sanity check
    if regime_name.lower() == "crash" and p_implied is not None and assumed_p is not None:
        print("")
        print("Crash Regime Sanity Check:")
        print(f"  Market view (implied):  {p_implied*100:5.1f}% chance of crash by expiry")
        print(f"  Model assumption:       {assumed_p*100:5.1f}% chance")
        
        if assumed_p > p_implied:
            multiplier = assumed_p / p_implied if p_implied > 0 else float('inf')
            print(f"  Relative difference:    {multiplier:.1f}x higher than market")
            
            if multiplier > 3.0:
                print(f"  ⚠️  WARNING: Betting {multiplier:.1f}x against market pricing - review carefully")
            else:
                print(f"  ℹ️  Using moderately optimistic assumption vs market")
        else:
            print(f"  ℹ️  Conservative: using probability below market pricing")
    
    print("=" * 100)
    print("")


def print_ev_sensitivity(candidate: Dict[str, Any]) -> None:
    """
    Print EV sensitivity analysis showing how EV changes with ±5% probability shifts.
    
    Args:
        candidate: Selected candidate dict
    """
    print("")
    print("=" * 100)
    print(f"EV SENSITIVITY ANALYSIS (Rank {candidate.get('rank', 'N/A')})")
    print("=" * 100)
    
    # Extract parameters - CRITICAL: Use actual p_event_used, never hardcoded default
    # Check both field names (campaign mode uses p_event_used, single-regime uses assumed_p_event)
    base_p = candidate.get("p_event_used") or candidate.get("assumed_p_event")
    
    if base_p is None:
        print("⚠️  Cannot compute sensitivity: p_event_used not available")
        print("=" * 100)
        print("")
        return
    
    debit = candidate.get("debit_per_contract", 0)
    max_gain = candidate.get("max_gain_per_contract", 0)
    
    # Calculate EV at different probabilities
    p_low = max(0.01, base_p - 0.05)
    p_high = min(0.99, base_p + 0.05)
    
    ev_base, ev_per_dollar_base = calculate_ev_at_probability(base_p, debit, max_gain)
    ev_low, ev_per_dollar_low = calculate_ev_at_probability(p_low, debit, max_gain)
    ev_high, ev_per_dollar_high = calculate_ev_at_probability(p_high, debit, max_gain)
    
    # Calculate percentage changes
    pct_change_low = ((ev_low - ev_base) / abs(ev_base) * 100) if ev_base != 0 else 0
    pct_change_high = ((ev_high - ev_base) / abs(ev_base) * 100) if ev_base != 0 else 0
    
    print(f"Base case (P={base_p:.2f}):    EV = ${ev_base:7.2f} | EV/$ = {ev_per_dollar_base:5.2f}")
    print(f"Lower bound (P={p_low:.2f}):  EV = ${ev_low:7.2f} | EV/$ = {ev_per_dollar_low:5.2f}  ({pct_change_low:+.1f}%)")
    print(f"Upper bound (P={p_high:.2f}):  EV = ${ev_high:7.2f} | EV/$ = {ev_per_dollar_high:5.2f}  ({pct_change_high:+.1f}%)")
    print("")
    
    # Determine robustness
    if ev_low > 0 and ev_base > 0 and ev_high > 0:
        robustness = "STRONG"
        interpretation = "EV remains positive across ±5% probability range"
    elif ev_low <= 0 and ev_base > 0:
        robustness = "FRAGILE"
        interpretation = "EV turns negative if probability drops by 5pp"
    elif abs(pct_change_low) > 50 or abs(pct_change_high) > 50:
        robustness = "MODERATE"
        interpretation = "EV changes significantly (>50%) with probability shifts"
    else:
        robustness = "MODERATE"
        interpretation = "EV shows moderate sensitivity to probability uncertainty"
    
    print(f"Interpretation: {interpretation}")
    print(f"Robustness:     {robustness}")
    print("=" * 100)
    print("")


def print_candidate_table(review_candidates: Dict[str, Any]) -> None:
    """
    Print candidate table with enhanced probability metadata.
    
    New format includes probability context block per regime and enhanced columns.
    """
    logger.info("=" * 80)
    logger.info("STEP 2: CANDIDATE REVIEW")
    logger.info("=" * 80)
    
    total_candidates = 0
    all_candidates_for_ranking = []
    
    for regime_name, regime_data in review_candidates.get("regimes", {}).items():
        # Print regime probability context BEFORE candidate table
        print_regime_probability_context(regime_data, regime_name)
        
        candidates = regime_data.get("candidates", [])
        
        # Collect all candidates for pre-governor ranking
        for c in candidates:
            c_copy = c.copy()
            c_copy["regime"] = regime_name
            all_candidates_for_ranking.append(c_copy)

        
        if not candidates:
            print(f"No candidates available for regime: {regime_name}")
            print("")
            continue
        
        # Get regime-level probability data for table columns
        p_event_external = regime_data.get("p_event_external")
        p_implied = regime_data.get("p_implied")
        
        # Extract for table columns
        p_ext = p_event_external.get("p") if p_event_external else None
        p_ext_source = p_event_external.get("source", "N/A") if p_event_external else "N/A"
        
        # Enhanced header with probability columns
        print("=" * 140)
        print(f"{'REGIME':<8} | {'RANK':<4} | {'EXPIRY':<8} | {'STRIKES':<10} | {'EV/$':<6} | {'P(Win)':<6} | "
              f"{'P_SRC':<12} | {'P_EXT':<6} | {'P_IMPL':<7} | {'EDGE':<8} | {'DEBIT':<6}")
        print("-" * 140)
        
        for candidate in candidates:
            rank = candidate.get("rank", "N/A")
            expiry = candidate.get("expiry", "N/A")
            
            # Extract strikes
            strikes = candidate.get("strikes", {})
            long_strike = strikes.get("long_put", 0)
            short_strike = strikes.get("short_put", 0)
            strike_str = f"{long_strike:.0f}/{short_strike:.0f}"
            
            # Extract metrics
            ev_per_dollar = candidate.get("ev_per_dollar", 0)
            prob_profit = candidate.get("prob_profit", 0)
            debit = candidate.get("debit_per_contract", 0)
            
            # Determine p_source from candidate or regime
            if p_event_external and p_event_external.get("authoritative"):
                p_src_display = p_ext_source[:12]  # Truncate if needed
            else:
                p_src_display = "fallback"
            
            # Format probability columns
            p_ext_display = f"{p_ext*100:5.1f}%" if p_ext is not None else "N/A"
            p_impl_display = f"{p_implied*100:6.1f}%" if p_implied is not None else "N/A"
            
            # Calculate edge
            if p_ext is not None and p_implied is not None:
                edge_bps = (p_ext - p_implied) * 10000
                edge_display = f"{edge_bps:+6.0f}bp"
            else:
                edge_display = "N/A"
            
            print(f"{regime_name:<8} | {rank:<4} | {expiry:<8} | {strike_str:<10} | "
                  f"{ev_per_dollar:<6.2f} | {prob_profit*100:<6.1f}% | "
                  f"{p_src_display:<12} | {p_ext_display:<6} | {p_impl_display:<7} | {edge_display:<8} | "
                  f"${debit:<5.0f}")
            
            total_candidates += 1
        
        print("=" * 140)
        print("")
    
    # TASK 4: Print PRE-GOVERNOR TOP-N ranking
    if all_candidates_for_ranking:
        print("")
        print("=" * 140)
        print("PRE-GOVERNOR RANKING (Top 5 by Score)")
        print("=" * 140)
        print("")
        
        # Compute robustness for each candidate
        for cand in all_candidates_for_ranking:
            # Determine robustness - simplified version (daily.py doesn't have full p_ext_status)
            p_src = cand.get("p_source", "")
            robustness = 1.0
            
            if "fallback" in p_src.lower():
                robustness *= 0.5
            
            # Check if p_external was unavailable
            p_ext = cand.get("p_external")
            if p_ext is None:
                robustness *= 0.7
            
            cand["robustness"] = robustness
            cand["score"] = cand.get("ev_per_dollar", 0.0) * robustness
        
        # Sort by score descending
        sorted_candidates = sorted(
            all_candidates_for_ranking,
            key=lambda c: (-c["score"], -c.get("prob_profit", 0), c.get("debit_per_contract", 0))
        )
        
        # Print header
        print(f"{'Rank':<6} {'Underlier':<10} {'Regime':<10} {'Expiry':<12} {'Strikes':<18} "
              f"{'EV/$':<8} {'Robust':<8} {'Score':<8} {'P_Used':<8} {'P_Src':<12}")
        print("-" * 120)
        
        # Print top 5
        for i, cand in enumerate(sorted_candidates[:5]):
            rank_num = i + 1
            underlier = cand.get("underlier", "UNKNOWN")[:10]
            regime = cand.get("regime", "unknown")[:10]
            expiry = cand.get("expiry", "N/A")
            
            strikes = cand.get("strikes", {})
            long_strike = strikes.get("long_put", 0)
            short_strike = strikes.get("short_put", 0)
            strikes_str = f"{long_strike:.0f}/{short_strike:.0f}"
            
            ev_per_dollar = cand.get("ev_per_dollar", 0.0)
            robustness = cand.get("robustness", 1.0)
            score = cand.get("score", 0.0)
            
            p_used = cand.get("assumed_p_event", 0.0)
            p_src = cand.get("p_source", "unknown")[:12]
            
            print(f"{rank_num:<6} {underlier:<10} {regime:<10} {expiry:<12} {strikes_str:<18} "
                  f"{ev_per_dollar:<8.3f} {robustness:<8.2f} {score:<8.3f} {p_used:<8.3f} {p_src:<12}")
        
        print("")
        print("=" * 140)
        print("")
    
    if total_candidates == 0:
        logger.error("❌ NO CANDIDATES AVAILABLE - NO_TRADE")
        sys.exit(1)


def select_candidate_interactive(
    review_candidates: Dict[str, Any],
    regime_filter: Optional[str] = None,
    auto_rank: int = 1
) -> Tuple[str, Dict[str, Any]]:
    """
    Select a candidate interactively.
    
    Default: rank=1 for each eligible regime
    User can override by typing regime and rank (e.g., "crash 2")
    
    Args:
        review_candidates: Review candidates dict
        regime_filter: Optional regime filter (if None, prompts user)
        auto_rank: Auto-select this rank (default: 1)
        
    Returns:
        Tuple of (regime, candidate_dict)
    """
    logger.info("=" * 80)
    logger.info("STEP 3: CANDIDATE SELECTION")
    logger.info("=" * 80)
    
    # Build list of available regimes
    available_regimes = []
    for regime_name, regime_data in review_candidates.get("regimes", {}).items():
        if regime_data.get("candidates"):
            available_regimes.append(regime_name)
    
    if not available_regimes:
        logger.error("❌ NO CANDIDATES AVAILABLE - NO_TRADE")
        sys.exit(1)
    
    logger.info(f"Available regimes: {', '.join(available_regimes)}")
    
    # If regime_filter specified, use it
    if regime_filter:
        if regime_filter not in available_regimes:
            logger.error(f"❌ Regime '{regime_filter}' not available")
            sys.exit(1)
        
        selected_regime = regime_filter
        logger.info(f"Using regime filter: {selected_regime}")
    else:
        # Interactive selection
        if len(available_regimes) == 1:
            selected_regime = available_regimes[0]
            logger.info(f"Auto-selecting only available regime: {selected_regime}")
        else:
            print(f"\nSelect regime (default: {available_regimes[0]}): {', '.join(available_regimes)}")
            user_input = input("Enter regime (or press Enter for default): ").strip()
            
            if not user_input:
                selected_regime = available_regimes[0]
            elif user_input in available_regimes:
                selected_regime = user_input
            else:
                logger.error(f"❌ Invalid regime: {user_input}")
                sys.exit(1)
    
    # Get candidates for selected regime
    regime_data = review_candidates["regimes"][selected_regime]
    candidates = regime_data["candidates"]
    
    # Determine rank - bypass prompt if:
    # 1. regime_filter is set (explicit auto mode), OR
    # 2. Only one regime available (auto-selected)
    auto_mode = regime_filter is not None or len(available_regimes) == 1
    
    if auto_mode:
        # Auto mode - use auto_rank without prompting
        selected_rank = auto_rank
        logger.info(f"Using auto rank: {selected_rank}")
    else:
        # Interactive mode - prompt for rank
        print(f"\nSelect rank for {selected_regime} (default: {auto_rank}): ", end="")
        rank_input = input().strip()
        
        if not rank_input:
            selected_rank = auto_rank
        else:
            try:
                selected_rank = int(rank_input)
            except ValueError:
                logger.error(f"❌ Invalid rank: {rank_input}")
                sys.exit(1)
    
    # Find candidate with selected rank
    candidate = None
    for c in candidates:
        if c.get("rank") == selected_rank:
            candidate = c
            break
    
    if not candidate:
        available_ranks = [c.get("rank") for c in candidates]
        logger.error(f"❌ No candidate with rank={selected_rank}. Available ranks: {available_ranks}")
        sys.exit(1)
    
    logger.info(f"✓ Selected: {selected_regime} rank={selected_rank}")
    logger.info(f"  Expiry: {candidate.get('expiry')}")
    logger.info(f"  Strikes: {candidate.get('strikes')}")
    logger.info(f"  EV/$: {candidate.get('ev_per_dollar', 0):.2f}")
    logger.info("")
    
    # Print EV sensitivity analysis for selected candidate
    print_ev_sensitivity(candidate)
    
    return selected_regime, candidate


def perform_quote_only(
    review_candidates_path: str,
    regime: str,
    rank: int,
    run_dir: str,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7496
) -> Tuple[Dict[str, Any], str]:
    """
    Perform quote-only check via execute_trade logic.
    
    This:
    1. Calls intent_builder to create OrderIntent from review_candidates.json
    2. Calls execute_trade.py with --quote-only
    3. Returns the execution result and intent file path
    
    Returns:
        Tuple of (execution_result dict with 'guards_passed' status, intent_file_path)
    """
    logger.info("=" * 80)
    logger.info("STEP 4: QUOTE-ONLY CHECK")
    logger.info("=" * 80)
    
    # Call intent_builder to create OrderIntent
    intent_builder_cmd = [
        sys.executable,
        "-m", "forecast_arb.execution.intent_builder",
        "--candidates", review_candidates_path,
        "--regime", regime,
        "--rank", str(rank)
    ]
    
    logger.info(f"Building intent: {' '.join(intent_builder_cmd)}")
    
    result = subprocess.run(intent_builder_cmd, capture_output=True, text=True, env=SUBPROCESS_ENV)
    
    if result.returncode != 0:
        logger.error(f"❌ Intent builder failed:\n{result.stderr}")
        sys.exit(1)
    
    # Capture intent file path from stdout
    intent_path = result.stdout.strip()
    
    if not intent_path or not Path(intent_path).exists():
        logger.error(f"❌ Intent builder did not produce valid file: {intent_path}")
        sys.exit(1)
    
    logger.info(f"✓ OrderIntent created: {intent_path}")
    
    # Build execute_trade command
    cmd = [
        sys.executable,
        "-m", "forecast_arb.execution.execute_trade",
        "--intent", intent_path,
        "--paper",
        "--quote-only",
        "--host", ibkr_host,
        "--port", str(ibkr_port)
    ]
    
    logger.info(f"Running: {' '.join(cmd)}")
    logger.info("")
    
    # Run execute_trade
    exec_result = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROCESS_ENV)
    
    # Print stdout (includes ticket summary)
    print(exec_result.stdout)
    
    if exec_result.returncode != 0:
        logger.error(f"❌ Quote-only check failed:\n{exec_result.stderr}")
        sys.exit(1)
    
    # Load execution result
    intent_dir = Path(intent_path).parent
    result_path = intent_dir / "execution_result.json"
    
    if not result_path.exists():
        logger.error(f"❌ execution_result.json not found at: {result_path}")
        sys.exit(1)
    
    with open(result_path, "r") as f:
        execution_result = json.load(f)
    
    guards_passed = execution_result.get("guards_passed", False)
    
    if guards_passed:
        logger.info("✅ GUARDS PASSED - OK_TO_STAGE")
    else:
        logger.error(f"❌ GUARDS FAILED: {execution_result.get('guards_result')}")
        sys.exit(1)
    
    logger.info("")
    
    return execution_result, intent_path


def offer_execution_options(
    intent_path: str,
    run_dir: str,
    exec_result: Dict[str, Any],
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7496
) -> Dict[str, Any]:
    """
    Offer execution options after quote-only passes.
    
    The intent has already been created by intent_builder during quote-only check.
    
    Options:
    1. Keep intent only (no order)
    2. Stage paper
    3. Transmit live (requires typing SEND)
    
    Args:
        intent_path: Path to already-created intent file
        run_dir: Path to run directory
        exec_result: Execution result from quote-only
        ibkr_host: IBKR host
        ibkr_port: IBKR port
    
    Returns:
        Final receipt dict with: intent_id, order_id, status, limit, run_id, ledger paths
    """
    logger.info("=" * 80)
    logger.info("STEP 5: EXECUTION OPTIONS")
    logger.info("=" * 80)
    
    # Load intent to get metadata
    with open(intent_path, "r") as f:
        intent = json.load(f)
    
    intent_id = intent.get("intent_id", "unknown")
    
    receipt = {
        "intent_id": intent_id,
        "order_id": None,
        "status": "NO_ACTION",
        "limit_price": intent["limit"]["start"],
        "run_id": Path(run_dir).name,
        "run_dir": run_dir,
        "intent_path": intent_path,
        "ledger_paths": []
    }
    
    print("\nQuote-only check passed. Choose execution option:")
    print("  1. Keep intent only (no order)")
    print("  2. Stage paper order (no transmission)")
    print("  3. Transmit live order (REQUIRES TYPING 'SEND')")
    print("  0. Exit without action")
    
    choice = input("\nEnter choice (default: 0): ").strip()
    
    if not choice or choice == "0":
        logger.info("ℹ️  Exiting without action")
        sys.exit(0)
    
    if choice == "1":
        # Keep intent only
        logger.info("Option 1: Keep intent only")
        logger.info(f"✓ Intent already created: {intent_path}")
        
        receipt["status"] = "INTENT_EMITTED"
        
    elif choice == "2":
        # Stage paper order
        logger.info("Option 2: Stage paper order")
        # Execute with --paper (no transmit)
        cmd = [
            sys.executable,
            "-m", "forecast_arb.execution.execute_trade",
            "--intent", intent_path,
            "--paper",
            "--host", ibkr_host,
            "--port", str(ibkr_port)
        ]
        
        logger.info(f"Running: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROCESS_ENV)
        
        print(result.stdout)
        
        if result.returncode != 0:
            logger.error(f"❌ Paper staging failed:\n{result.stderr}")
            sys.exit(1)
        # Load execution result
        result_path = Path(intent_path).parent / "execution_result.json"
        
        with open(result_path, "r") as f:
            paper_result = json.load(f)
        
        order_id = paper_result.get("order_id")
        
        logger.info(f"✓ Paper order staged: order_id={order_id}")
        receipt["status"] = "PAPER_STAGED"
        receipt["order_id"] = order_id
        
    elif choice == "3":
        # Transmit live order
        logger.info("Option 3: Transmit live order")
        logger.warning("⚠️  WARNING: This will transmit a LIVE order to the exchange")
        
        # Require SEND confirmation
        print("\nType 'SEND' to confirm live transmission: ", end="")
        confirm = input().strip()
        
        if confirm != "SEND":
            logger.error("❌ Confirmation failed - aborting")
            sys.exit(1)
        # Execute with --live --transmit --confirm SEND
        cmd = [
            sys.executable,
            "-m", "forecast_arb.execution.execute_trade",
            "--intent", intent_path,
            "--live",
            "--transmit",
            "--confirm", "SEND",
            "--host", ibkr_host,
            "--port", str(ibkr_port)
        ]
        
        logger.info(f"Running: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROCESS_ENV)
        
        print(result.stdout)
        
        if result.returncode != 0:
            logger.error(f"❌ Live transmission failed:\n{result.stderr}")
            sys.exit(1)
        # Load execution result
        result_path = Path(intent_path).parent / "execution_result.json"
        
        with open(result_path, "r") as f:
            live_result = json.load(f)
        
        order_id = live_result.get("order_id")
        order_status = live_result.get("status")
        
        logger.info(f"✓ Live order transmitted: order_id={order_id}, status={order_status}")
        
        # NOTE: Ledger writing is handled by execute_trade itself
        # execute_trade will write to outcome ledger when order is submitted/filled
        ledger_written = live_result.get("ledger_written", False)
        if ledger_written:
            ledger_path_local = Path(run_dir) / "artifacts" / "trade_outcomes.jsonl"
            ledger_path_global = Path("runs") / "trade_outcomes.jsonl"
            receipt["ledger_paths"] = [str(ledger_path_local), str(ledger_path_global)]
            logger.info(f"✓ Trade outcome ledger written by execute_trade")
        receipt["status"] = "LIVE_TRANSMITTED"
        receipt["order_id"] = order_id
        
    else:
        logger.error(f"❌ Invalid choice: {choice}")
        sys.exit(1)
    
    return receipt


def print_final_receipt(receipt: Dict[str, Any]) -> None:
    """
    Print final receipt block.
    
    Format:
    ================================================================================
    FINAL RECEIPT
    ================================================================================
    Intent ID:    spy_20260402_580_560_crash_20260224T095959
    Order ID:     12345
    Status:       LIVE_TRANSMITTED
    Limit Price:  $49.00
    Run ID:       crash_venture_v2_a54e721dd97bbbbc_20260224T095959
    Run Dir:      runs/crash_venture_v2/crash_venture_v2_a54e721dd97bbbbc_20260224T095959
    Intent Path:  intents/spy_20260402_580_560_crash_20260224T095959.json
    Ledger Paths:
      - runs/crash_venture_v2/.../artifacts/trade_outcomes.jsonl
      - runs/trade_outcomes.jsonl
    ================================================================================
    """
    print("")
    print("=" * 80)
    print("FINAL RECEIPT")
    print("=" * 80)
    print(f"Intent ID:    {receipt.get('intent_id', 'N/A')}")
    print(f"Order ID:     {receipt.get('order_id', 'N/A')}")
    print(f"Status:       {receipt.get('status', 'N/A')}")
    print(f"Limit Price:  ${receipt.get('limit_price', 0):.2f}")
    print(f"Run ID:       {receipt.get('run_id', 'N/A')}")
    print(f"Run Dir:      {receipt.get('run_dir', 'N/A')}")
    
    if "intent_path" in receipt:
        print(f"Intent Path:  {receipt['intent_path']}")
    
    if receipt.get("ledger_paths"):
        print("Ledger Paths:")
        for path in receipt["ledger_paths"]:
            print(f"  - {path}")
    
    print("=" * 80)
    print("")


def run_campaign_mode(
    campaign_config_path: str,
    structuring_config_path: str,
    snapshot_path: Optional[str] = None,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7496,
    qty: int = 1
) -> Dict[str, Any]:
    """
    Run campaign mode: grid runner + selector + recommended set.
    
    Args:
        campaign_config_path: Path to campaign_v1.yaml
        structuring_config_path: Path to structuring config
        snapshot_path: Optional snapshot path
        ibkr_host: IBKR host
        ibkr_port: IBKR port
        qty: Quantity per trade
        
    Returns:
        Dict with campaign_run_id, candidates_path, recommended_path
    """
    import yaml
    from forecast_arb.campaign.grid_runner import run_campaign_grid
    from forecast_arb.campaign.selector import run_selector
    from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
    
    logger.info("=" * 80)
    logger.info("campaign MODE - MULTI-CELL GRID")
    logger.info("=" * 80)
    
    # Resolve paths relative to project root (parent of scripts/)
    project_root = Path(__file__).parent.parent
    campaign_config_path = str(project_root / campaign_config_path)
    structuring_config_path = str(project_root / structuring_config_path)
    if snapshot_path:
        snapshot_path = str(project_root / snapshot_path)
    
    # Load campaign config
    with open(campaign_config_path, "r") as f:
        campaign_config = yaml.safe_load(f)
    
    underliers = campaign_config["underliers"]
    
    # Step 1: Run grid runner (creates fresh snapshots for each underlier internally)
    # NOTE: snapshot_path parameter is ignored in campaign mode - grid_runner creates fresh snapshots
    logger.info("Step 1: Running campaign grid (will create fresh snapshots per underlier)")
    logger.info("-" * 80)
    
    candidates_flat_path = run_campaign_grid(
        campaign_config_path=campaign_config_path,
        structuring_config_path=structuring_config_path,
        p_external_by_underlier=None,  # Let run_regime handle Kalshi fetch internally
        min_debit_per_contract=10.0,
        snapshot_dir="snapshots"
        # dte_min=30, dte_max=60 use defaults (aligned with campaign config)
        # tail_moneyness_floor=None uses auto-calculation (deepest regime + 5% buffer)
    )
    
    logger.info("")
    
    # Step 2: Run selector
    logger.info("Step 2: Running portfolio-aware selector")
    logger.info("-" * 80)
    
    recommended_path = run_selector(
        candidates_flat_path=candidates_flat_path,
        campaign_config=campaign_config,
        ledger_path="runs/trade_outcomes.jsonl",
        qty=qty
    )
    
    logger.info("")
    
    # Step 3: Load and display recommended set
    logger.info("=" * 80)
    logger.info("RECOMMENDED SET (0-2 CANDIDATES)")
    logger.info("=" * 80)
    
    with open(recommended_path, "r") as f:
        recommended = json.load(f)
    
    # Display Portfolio State Header
    portfolio_state = recommended.get("portfolio_state", {})
    gov = campaign_config.get("governors", {})
    
    open_count = portfolio_state.get("open_positions_count", 0)
    open_count_by_regime = portfolio_state.get("open_count_by_regime", {})
    open_premium_by_regime = portfolio_state.get("open_premium_by_regime", {})
    open_premium_total = portfolio_state.get("open_premium_total", 0.0)
    
    daily_cap = gov.get("daily_premium_cap_usd", 0)
    crash_cap = gov.get("premium_at_risk_caps_usd", {}).get("crash", 0)
    selloff_cap = gov.get("premium_at_risk_caps_usd", {}).get("selloff", 0)
    total_cap = gov.get("premium_at_risk_caps_usd", {}).get("total", 0)
    max_crash = gov.get("max_open_positions_by_regime", {}).get("crash", 0)
    max_selloff = gov.get("max_open_positions_by_regime", {}).get("selloff", 0)
    
    new_premium_total = recommended.get("selection_summary", {}).get("new_premium_total", 0)
    
    print("\n" + "="*100)
    print("PORTFOLIO STATE")
    print("="*100)
    print(f"Open positions total: {open_count}")
    print(f"Crash:   {open_count_by_regime.get('crash',0)} / {max_crash}  "
          f"Premium ${open_premium_by_regime.get('crash',0):.2f} / ${crash_cap}")
    print(f"Selloff: {open_count_by_regime.get('selloff',0)} / {max_selloff}  "
          f"Premium ${open_premium_by_regime.get('selloff',0):.2f} / ${selloff_cap}")
    print(f"Total premium at risk: ${open_premium_total:.2f} / ${total_cap}")
    print(f"Remaining daily premium capacity: ${daily_cap - new_premium_total:.2f}")
    print("="*100 + "\n")
    
    selected = recommended.get("selected", [])
    
    if not selected:
        logger.info("⚠️  NO CANDIDATES RECOMMENDED (all blocked by governors)")
        logger.info("")
        
        # Show why candidates were rejected
        rejected = recommended.get("rejected_top10", [])
        if rejected:
            logger.info("Top rejected candidates:")
            for rej in rejected[:5]:
                logger.info(f"  - {rej['candidate_id']}: {rej['reason']}")
        
        logger.info("")
        logger.info("Run complete with 0 recommended trades.")
        return {
            "campaign_run_id": Path(candidates_flat_path).parent.name,
            "candidates_path": candidates_flat_path,
            "recommended_path": recommended_path,
            "selected_count": 0
        }
    
    # Print recommended table with probability provenance
    print("")
    print("=" * 150)
    print(f"{'#':<3} | {'UNDERLIER':<10} | {'REGIME':<8} | {'EXPIRY':<8} | {'STRIKES':<10} | {'EV/$':<6} | {'P(event)':<8} | "
          f"{'PREM':<6} | {'CLUSTER':<10} | {'P_IMPL':<7} | {'P_SRC':<14}")
    print("-" * 150)
    
    for i, candidate in enumerate(selected, 1):
        underlier = candidate.get("underlier", "N/A")
        regime = candidate.get("regime", "N/A")
        expiry = candidate.get("expiry", "N/A")
        long_strike = candidate.get("long_strike", 0)
        short_strike = candidate.get("short_strike", 0)
        strike_str = f"{long_strike:.0f}/{short_strike:.0f}"
        premium = candidate.get("computed_premium_usd", 0)
        cluster = candidate.get("cluster_id", "N/A")
        
        # CANONICAL probability fields (use p_used, not p_event_used)
        p_used = candidate.get("p_used") or candidate.get("p_event_used")
        p_impl = candidate.get("p_impl") or candidate.get("p_implied")
        p_src = candidate.get("p_used_src") or candidate.get("p_source")
        
        # Use CANONICAL ev_per_dollar (already recomputed by campaign selector)
        ev_per_dollar = candidate.get("ev_per_dollar", 0.0)
        
        # Format: show value or dash if None
        p_used_display = f"{p_used*100:.1f}%" if p_used is not None else "—"
        p_impl_display = f"{p_impl:.3f}" if p_impl is not None else "—"
        p_src_display = p_src if p_src is not None else "—"
        
        print(f"{i:<3} | {underlier:<10} | {regime:<8} | {expiry:<8} | {strike_str:<10} | "
              f"{ev_per_dollar:<6.2f} | {p_used_display:<8} | ${premium:<5.0f} | {cluster:<10} | "
              f"{p_impl_display:<7} | {p_src_display:<14}")
    
    print("=" * 150)
    print("")
    
    logger.info(f"Selected {len(selected)} candidate(s) for execution")
    logger.info("")
    
    return {
        "campaign_run_id": Path(candidates_flat_path).parent.name,
        "candidates_path": candidates_flat_path,
        "recommended_path": recommended_path,
        "selected_count": len(selected),
        "selected": selected
    }


def _resolve_candidates_path(
    result: Dict[str, Any],
    run_dir: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve a canonical candidates file path for the allocator.

    Priority (v1.6 Task 1):
      1. result["recommended_path"] if present and exists on disk
      2. run_dir / "recommended.json" if exists
      3. run_dir / "candidates_flat.json" if exists
      4. None — prints loud warning; allocator will HOLD with CANDIDATES_FILE_MISSING

    Always prints three diagnostic lines:
      Allocator candidates_path = <path or NONE>
      Candidates file exists? yes / no
      Candidates count found = N

    Returns:
        Resolved path string or None.
    """
    resolved: Optional[str] = None

    # Priority 1: result["recommended_path"]
    rp = result.get("recommended_path")
    if rp and Path(rp).exists():
        resolved = str(rp)
    # Priority 2: run_dir / recommended.json
    elif run_dir and (Path(run_dir) / "recommended.json").exists():
        resolved = str(Path(run_dir) / "recommended.json")
    # Priority 3: run_dir / candidates_flat.json
    elif run_dir and (Path(run_dir) / "candidates_flat.json").exists():
        resolved = str(Path(run_dir) / "candidates_flat.json")

    # Count candidates in the resolved file
    count = 0
    if resolved:
        try:
            with open(resolved, "r", encoding="utf-8") as _f:
                _data = json.load(_f)
            if isinstance(_data, dict):
                count = len(
                    _data.get("selected") or _data.get("candidates") or []
                )
            elif isinstance(_data, list):
                count = len(_data)
        except Exception:
            count = -1  # unreadable

    # Always print diagnostics (Task 1 requirement)
    print(f"  Allocator candidates_path = {resolved or 'NONE'}")
    print(f"  Candidates file exists? {'yes' if resolved else 'no'}")
    print(f"  Candidates count found = {count}")

    if not resolved:
        print(
            "  ⚠️  WARNING: No candidates file found — "
            "allocator will HOLD with CANDIDATES_FILE_MISSING"
        )

    return resolved


def _run_execute_stage(
    policy_path: str,
    execute_mode: str,
    quote_only: bool,
    allow_stale: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Run the CCC execute flow after planning (Task 4 v1.5, stale guard v1.6).

    Imports run_execute from scripts/ccc_execute.py — no shell-out.

    Args:
        policy_path:   Path to allocator policy YAML
        execute_mode:  "paper" or "live"
        quote_only:    If True, preview only — commit ledger NOT updated
        allow_stale:   If True, skip stale intent guard

    Returns:
        Result dict from run_execute, or None on error/skip.
    """
    # Add scripts/ dir to path so we can import ccc_execute directly
    _scripts_dir = str(Path(__file__).parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    try:
        from ccc_execute import run_execute  # type: ignore[import]
    except ImportError as _ie:
        print(f"  ⚠️  Could not import run_execute from ccc_execute: {_ie}")
        return

    try:
        from forecast_arb.allocator.policy import (
            get_actions_path,
            get_commit_ledger_path,
            load_policy,
        )
        pol = load_policy(policy_path)
    except Exception as _pe:
        print(f"  ⚠️  Could not load policy {policy_path}: {_pe}")
        return

    actions_path = get_actions_path(pol)
    commit_ledger_path = get_commit_ledger_path(pol)

    if not actions_path.exists():
        print(
            f"\n  ⚠️  allocator_actions.json not found at {actions_path}"
            f" — run planning first (no --execute without a completed plan)"
        )
        return

    print("")
    print("=" * 72)
    print(
        f"  CCC EXECUTE  —  mode={execute_mode.upper()}"
        f"{'  QUOTE-ONLY' if quote_only else ''}"
    )
    print(f"  Actions file : {actions_path}")
    print(f"  Commit ledger: {commit_ledger_path}")
    print("=" * 72)

    try:
        result = run_execute(
            actions_file=str(actions_path),
            commit_ledger_path=str(commit_ledger_path),
            mode=execute_mode,
            quote_only=quote_only,
        )
    except Exception as _exc:
        print(f"\n  ✗ Execute failed: {_exc}")
        return

    if result.get("aborted"):
        print("\n  → Execute ABORTED by operator (SEND not confirmed)")
        print("=" * 72)
        return None

    if quote_only:
        print(f"\n  Quote-only preview: {result.get('quotes_ok', 0)} intent(s) validated")
        print("  Commit ledger NOT updated (--quote-only)")
    else:
        committed = result.get("committed", 0)
        already = result.get("skipped_already_committed", 0)
        errors = result.get("errors", 0)
        print(f"\n  Committed        : {committed}")
        print(f"  Already committed: {already}")
        print(f"  Errors           : {errors}")
        if committed > 0:
            print(f"\n  ✓ Budget will reflect new spend (${0:.0f}+) on next plan run.")
    print("=" * 72)
    print("")
    return result  # v1.6: callers use this for operator summary


def _run_allocator(
    policy_path: str,
    candidates_path: Optional[str] = None,
    verbose: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Run the CCC v1 allocator planner and print summary.

    Safe wrapper: imports allocator lazily so missing optional deps don't break
    non-policy daily runs.

    Args:
        policy_path:     Path to allocator_ccc_v1.yaml
        candidates_path: Path to recommended.json or candidates_flat.json (may be None)
        verbose:         Pass through to allocator console

    Returns:
        Dict with opens/closes/holds counts, or None on error.
    """
    print("")
    print("=" * 80)
    print("CCC v1 ALLOCATOR PLAN")
    print("=" * 80)

    try:
        from forecast_arb.allocator.plan import run_allocator_plan
        from forecast_arb.allocator.types import ActionType

        plan = run_allocator_plan(
            policy_path=policy_path,
            candidates_path=candidates_path,
            signals=None,  # No live conditioning signals in script mode
            verbose=verbose,
            dry_run=False,
        )

        # Print path to output file
        from forecast_arb.allocator.policy import get_actions_path, load_policy
        pol = load_policy(policy_path)
        actions_path = get_actions_path(pol)
        print(f"\n  allocator_actions.json → {actions_path}")

        # Build plan summary for operator output
        opens = sum(1 for a in plan.actions if a.type == ActionType.OPEN)
        closes = sum(1 for a in plan.actions
                     if a.type in (ActionType.HARVEST_CLOSE, ActionType.ROLL_CLOSE))
        holds = sum(1 for a in plan.actions if a.type == ActionType.HOLD)
        # v1.9 Task F: extract OPEN details for console display
        open_details = []
        for a in plan.actions:
            if a.type == ActionType.OPEN:
                prem_src = "CAMPAIGN"
                for rc in a.reason_codes:
                    if rc.startswith("PREMIUM_USED:"):
                        prem_src = rc.split(":")[-1]
                        break
                open_details.append({
                    "candidate_id": a.candidate_id or "",
                    "regime": a.regime or "",
                    "premium": a.premium,
                    "premium_src": prem_src,
                    "layer": a.layer,
                    "fragile": a.fragile,
                    "qty": a.qty,
                })

        return {
            "opens": opens,
            "closes": closes,
            "holds": holds,
            "actions_path": str(actions_path),
            # v1.8: pending and positions summaries
            "pending_by_regime": dict(plan.pending_open_intents),
            "positions_by_regime": {
                "crash": plan.inventory.crash_open,
                "selloff": plan.inventory.selloff_open,
            },
            # v1.9 Task F: OPEN action details for operator summary
            "open_details": open_details,
            # v1.9 Task D: committed-not-filled explicit
            "committed_not_filled_by_regime": dict(plan.pending_open_intents),
            # Phase 2A Task A: annual budget fields
            "annual_budget_enabled": plan.budgets.annual_budget_enabled,
            "annual_convexity_budget": plan.budgets.annual_convexity_budget,
            "ytd_spent": round(plan.budgets.spent_ytd, 2),
            "remaining_annual": (
                round(plan.budgets.remaining_annual, 2)
                if plan.budgets.annual_budget_enabled else None
            ),
        }

    except Exception as exc:
        print(f"  ⚠️  Allocator error: {exc}")
        if verbose:
            import traceback
            traceback.print_exc()
        return None


def _get_commit_ledger_path_for_policy(policy_path: str) -> Optional[str]:
    """Return commit ledger path from policy YAML, or None on error."""
    try:
        from forecast_arb.allocator.policy import get_commit_ledger_path, load_policy
        pol = load_policy(policy_path)
        return str(get_commit_ledger_path(pol))
    except Exception:
        return None


def _run_reconcile_stage(
    policy_path: str,
    execute_mode: str,
) -> Optional[Dict[str, Any]]:
    """
    Run ccc_reconcile after execute step (v1.7 --reconcile flag).

    Only called when --execute and NOT --quote-only.

    Args:
        policy_path:   Path to allocator policy YAML (for intents_dir path)
        execute_mode:  "paper" | "live"

    Returns:
        reconcile summary dict, or None on error.
    """
    try:
        from forecast_arb.allocator.fills import (
            DEFAULT_FILLS_LEDGER_PATH,
            DEFAULT_POSITIONS_PATH,
            DEFAULT_ARCHIVE_BASE,
            run_reconcile,
        )
        from forecast_arb.allocator.policy import get_intents_dir, load_policy
        pol = load_policy(policy_path)
        intents_dir = get_intents_dir(pol)
    except Exception as _pe:
        print(f"  ⚠️  Could not load policy for reconcile: {_pe}")
        return None

    print("")
    print("=" * 72)
    print(f"  CCC RECONCILE  —  mode={execute_mode.upper()}")
    print(f"  intents_dir  : {intents_dir}")
    print(f"  fills_ledger : {DEFAULT_FILLS_LEDGER_PATH}")
    print(f"  positions    : {DEFAULT_POSITIONS_PATH}")
    print("=" * 72)

    try:
        result = run_reconcile(
            mode=execute_mode,
            intents_dir=intents_dir,
            fills_ledger_path=DEFAULT_FILLS_LEDGER_PATH,
            positions_path=DEFAULT_POSITIONS_PATH,
            archive_base_dir=DEFAULT_ARCHIVE_BASE,
        )
    except Exception as _exc:
        print(f"\n  ✗ Reconcile failed: {_exc}")
        return None

    fills_found = result.get("fills_found", 0)
    positions_opened = result.get("positions_opened", 0)
    dedup_skipped = result.get("dedup_skipped", 0)

    if fills_found == 0:
        print("\n  ℹ️  No fills found — reconcile no-op (intents archived after execution)")
    elif positions_opened > 0:
        print(f"\n  ✓ RECONCILE: fills_found={fills_found}  positions_opened={positions_opened}  "
              f"dedup_skipped={dedup_skipped}")
    else:
        print(f"\n  ↩ RECONCILE: DEDUP — positions_opened=0  dedup_skipped={dedup_skipped}")

    print("=" * 72)
    print("")
    return result


def _check_stale_pending(
    commit_ledger_path: Optional[str],
    stale_days: int = _STALE_PENDING_DAYS,
) -> List[Dict[str, Any]]:
    """
    Return list of pending intent rows that exceed stale_days threshold.

    v1.9 Operator Hygiene — called from _print_operator_summary to embed
    STALE_PENDING warnings in the DAILY RUN SUMMARY box.

    Args:
        commit_ledger_path: Path string to commit ledger (or None → return [])
        stale_days:         Threshold; intents older than this are stale.

    Returns:
        List of enriched row dicts with "age_days" key; empty if none stale.
    """
    if not commit_ledger_path:
        return []
    try:
        from forecast_arb.allocator.pending import load_pending_rows_with_age
        commit_path = Path(commit_ledger_path)
        fills_path = commit_path.parent / "allocator_fills_ledger.jsonl"
        rows = load_pending_rows_with_age(commit_path, fills_path)
        return [r for r in rows if r.get("age_days", 0) > stale_days]
    except Exception:
        return []


def _print_operator_summary(
    candidates_path: Optional[str],
    plan_summary: Optional[Dict[str, Any]],
    execute: bool,
    exec_mode: str,
    quote_only: bool,
    exec_summary: Optional[Dict[str, Any]],
    commit_ledger_path: Optional[str],
    reconcile_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Print concise operator-facing summary at end of daily run (v1.8).

    Format:
      ╔══ DAILY RUN SUMMARY ═══════════════════════════════════════╗
      ║  CANDIDATES FILE: <path> (seen N)
      ║  CCC PLAN: planned_opens=X planned_closes=Y holds=Z
      ║  INVENTORY: positions crash=a selloff=b  pending crash=x selloff=y
      ║  CCC EXECUTE: mode=paper committed_new=N committed_skipped=N
      ║  RECONCILE: positions_opened=N dedup=M
      ║  Commit ledger: <path>
      ╚════════════════════════════════════════════════════════════╝
    """
    # Count from candidates path
    cand_count = 0
    if candidates_path:
        try:
            with open(candidates_path, "r", encoding="utf-8") as _f:
                _d = json.load(_f)
            if isinstance(_d, dict):
                cand_count = len(_d.get("selected") or _d.get("candidates") or [])
            elif isinstance(_d, list):
                cand_count = len(_d)
        except Exception:
            cand_count = -1

    print("")
    print("╔" + "═" * 62 + "╗")
    print("║  DAILY RUN SUMMARY (CCC v1.8)" + " " * 32 + "║")
    print("╠" + "─" * 62 + "╣")

    # Candidates file
    cand_display = str(candidates_path) if candidates_path else "NONE"
    print(f"║  CANDIDATES FILE: {cand_display[:40]} (seen {cand_count})".ljust(63) + "║")

    # CCC plan with v1.8 labels
    if plan_summary:
        opens = plan_summary.get("opens", 0)
        closes = plan_summary.get("closes", 0)
        holds = plan_summary.get("holds", 0)
        print(
            f"║  CCC PLAN: planned_opens={opens} planned_closes={closes} holds={holds}"
            .ljust(63) + "║"
        )

        # v1.8: Inventory actual + pending
        positions_by_regime = plan_summary.get("positions_by_regime", {})
        pending_by_regime = plan_summary.get("pending_by_regime", {})
        pos_crash = positions_by_regime.get("crash", 0)
        pos_selloff = positions_by_regime.get("selloff", 0)
        pend_crash = pending_by_regime.get("crash", 0)
        pend_selloff = pending_by_regime.get("selloff", 0)
        print(
            f"║  INVENTORY ACTUAL: crash={pos_crash} selloff={pos_selloff}"
            .ljust(63) + "║"
        )
        if pend_crash or pend_selloff:
            print(
                f"║  PENDING (committed-not-filled): crash={pend_crash} selloff={pend_selloff}"
                .ljust(63) + "║"
            )
            print(
                f"║  EFFECTIVE (gating): crash={pos_crash + pend_crash} selloff={pos_selloff + pend_selloff}"
                .ljust(63) + "║"
            )
            # v1.9: stale pending check — warn if any pending intent exceeds threshold
            _stale_rows = _check_stale_pending(commit_ledger_path)
            for _r in _stale_rows:
                _warn = (
                    f"STALE_PENDING: intent_id={_r['intent_id'][:20]}"
                    f"  age={_r['age_days']}d"
                    f"  suggest ccc_cancel.py"
                )
                print(f"║  ⚠  {_warn}".ljust(63) + "║")
    else:
        print("║  CCC PLAN: (not run or errored)".ljust(63) + "║")

    # Execute summary with v1.8 labels
    if execute:
        qo_str = "true" if quote_only else "false"
        if exec_summary:
            committed_new = exec_summary.get("committed", 0)
            committed_skipped = exec_summary.get("skipped_already_committed", 0)
            print(
                f"║  CCC EXECUTE: mode={exec_mode} quote_only={qo_str} "
                f"committed_new={committed_new} committed_skipped={committed_skipped}"
                .ljust(63) + "║"
            )
        else:
            print(
                f"║  CCC EXECUTE: mode={exec_mode} quote_only={qo_str} (aborted/error)"
                .ljust(63) + "║"
            )
        # Commit ledger path
        ledger_display = str(commit_ledger_path) if commit_ledger_path else "N/A"
        print(f"║  Commit ledger: {ledger_display[:44]}".ljust(63) + "║")

    # Phase 2A Task A: annual convexity budget summary
    if plan_summary and plan_summary.get("annual_budget_enabled"):
        ytd = plan_summary.get("ytd_spent", 0.0)
        ann_budget = plan_summary.get("annual_convexity_budget", 0.0)
        remaining_ann = plan_summary.get("remaining_annual")
        remaining_str = f"${remaining_ann:.0f}" if remaining_ann is not None else "N/A"
        print(
            f"║  ANNUAL BUDGET: ytd_spent=${ytd:.0f}  budget=${ann_budget:.0f}"
            f"  remaining={remaining_str}"
            .ljust(63) + "║"
        )

    # v1.7/v1.8: Reconcile summary (only when --reconcile was used)
    if reconcile_summary is not None:
        pos_opened = reconcile_summary.get("positions_opened", 0)
        dedup = reconcile_summary.get("dedup_skipped", 0)
        staged = reconcile_summary.get("orders_staged", 0)
        rec_line = f"positions_opened={pos_opened} dedup={dedup}"
        if staged:
            rec_line += f" staged={staged}"
        print(
            f"║  RECONCILE: {rec_line}"
            .ljust(63) + "║"
        )

    print("╚" + "═" * 62 + "╝")
    print("")


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Interactive Daily Console - Single Command Operator Workflow"
    )
    
    # Campaign mode (NEW)
    parser.add_argument(
        "--campaign",
        type=str,
        default=None,
        help="Run campaign mode with specified campaign config (e.g., configs/campaign_v1.yaml)"
    )

    # Allocator policy mode (CCC v1)
    parser.add_argument(
        "--policy",
        type=str,
        default=None,
        help="Run CCC v1 allocator after campaign (e.g., configs/allocator_ccc_v1.yaml)"
    )

    # Execution flags (v1.5 Task 4: one-liner execute)
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help=(
            "Run CCC execute after planning. "
            "Requires --paper or --live.  "
            "Add --quote-only to preview without committing."
        ),
    )

    _exec_mode_group = parser.add_mutually_exclusive_group()
    _exec_mode_group.add_argument(
        "--paper",
        action="store_true",
        default=False,
        help="Execute in paper mode (writes commit records, no IBKR transmission)",
    )
    _exec_mode_group.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Execute in live mode (writes live commit records; requires typing SEND)",
    )

    parser.add_argument(
        "--quote-only",
        action="store_true",
        default=False,
        dest="quote_only",
        help=(
            "Preview intents only — do NOT write to commit ledger. "
            "Valid only with --execute."
        ),
    )

    parser.add_argument(
        "--reconcile",
        action="store_true",
        default=False,
        dest="reconcile",
        help=(
            "Run ccc_reconcile after execute step (only when --execute and NOT --quote-only). "
            "Reads execution_result.json, writes fills ledger + positions.json, "
            "archives OPEN intents. Safe to run multiple times (idempotent)."
        ),
    )
    
    # Orchestration options
    parser.add_argument(
        "--regime",
        type=str,
        default="crash",
        help="Regime to run (default: crash) [non-campaign mode only]"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/structuring_crash_venture_v2.yaml",
        help="Structuring config path"
    )
    parser.add_argument(
        "--snapshot",
        type=str,
        default=None,
        help="Path to existing snapshot (optional)"
    )
    parser.add_argument(
        "--ibkr-host",
        type=str,
        default="127.0.0.1",
        help="IBKR host"
    )
    parser.add_argument(
        "--ibkr-port",
        type=int,
        default=7496,
        help="IBKR port"
    )
    parser.add_argument(
        "--dte-min",
        type=int,
        default=30,
        help="Minimum DTE"
    )
    parser.add_argument(
        "--dte-max",
        type=int,
        default=60,
        help="Maximum DTE"
    )
    parser.add_argument(
        "--min-debit",
        type=float,
        default=10.0,
        help="Minimum debit per contract"
    )
    parser.add_argument(
        "--fallback-p",
        type=float,
        default=0.30,
        help="Fallback p_external"
    )
    
    # Interactive options
    parser.add_argument(
        "--auto-regime",
        type=str,
        default=None,
        help="Auto-select regime (skip interactive prompt)"
    )
    parser.add_argument(
        "--auto-rank",
        type=int,
        default=1,
        help="Auto-select rank (default: 1)"
    )
    
    # Debugging
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()

    # Validate execute flags
    if args.execute and not args.paper and not args.live:
        parser.error("--execute requires --paper or --live")
    if args.quote_only and not args.execute:
        parser.error("--quote-only is only valid with --execute")

    setup_logging(verbose=args.verbose)

    # Print operator console header
    print("")
    print("=" * 80)
    print("OPERATOR CONSOLE: daily.py")
    if args.campaign:
        print(f"Mode: CAMPAIGN")
        print(f"Config: {args.campaign}")
    else:
        print(f"Mode: SINGLE-REGIME")
        print(f"Regime: {args.regime}")
        print(f"Config: {args.config}")
    if args.execute:
        exec_mode_str = "live" if args.live else "paper"
        print(
            f"Execute: {exec_mode_str.upper()}"
            f"{'  QUOTE-ONLY (no commit)' if args.quote_only else ''}"
        )
    print("=" * 80)
    print("")
    
    logger.info("=" * 80)
    logger.info("INTERACTIVE DAILY CONSOLE")
    logger.info("=" * 80)
    logger.info("")
    
    # Check if campaign mode
    if args.campaign:
        # Campaign mode - run grid + selector
        logger.info("Mode: CAMPAIGN")
        logger.info(f"Campaign Config: {args.campaign}")
        logger.info("")
        
        result = run_campaign_mode(
            campaign_config_path=args.campaign,
            structuring_config_path=args.config,
            snapshot_path=args.snapshot,
            ibkr_host=args.ibkr_host,
            ibkr_port=args.ibkr_port,
            qty=1
        )
        
        # Check for NO_TRADE case
        if result['selected_count'] == 0:
            print("")
            print("=" * 80)
            print("NO_TRADE - 0 candidates selected")
            print("=" * 80)
            logger.info("✅ CAMPAIGN MODE COMPLETE (NO_TRADE)")
            sys.exit(0)
        
        # TASK A (CCC v1.2 — Single Authority):
        # When --policy is provided, CCC is the SOLE authority for OPEN intents.
        # Campaign step displays selection only; intent_builder is NOT called.
        # All OPEN intents will be written to intents/allocator/ by the allocator.
        if args.policy is not None:
            # Policy mode: display selected candidates for review, then hand off to CCC.
            print("")
            print("=" * 80)
            print(f"CAMPAIGN SELECTION — {result['selected_count']} CANDIDATE(S) SELECTED")
            print("ℹ️  --policy active: CCC allocator is the sole authority for OPEN intents.")
            print("ℹ️  No intents/*.json written here; see intents/allocator/ after CCC runs.")
            print("=" * 80)
            print("")
            for i, candidate in enumerate(result.get("selected", []), 1):
                p_used = candidate.get("p_used") or candidate.get("p_event_used")
                p_src = candidate.get("p_used_src") or candidate.get("p_source", "—")
                ev = candidate.get("ev_per_dollar", 0.0)
                try:
                    strikes_str = (f"{candidate.get('long_strike'):.0f}/"
                                   f"{candidate.get('short_strike'):.0f}")
                except (TypeError, ValueError):
                    strikes_str = "?/?"
                p_str = f"{p_used*100:.1f}%" if p_used is not None else "—"
                print(f"  {i}. {candidate.get('underlier','?')} {candidate.get('regime','?')} "
                      f"{candidate.get('expiry','?')} {strikes_str}  "
                      f"EV/$ {ev:.2f}  P_used={p_str} [{p_src}]")
            print("")
        else:
            # Standard mode: full interactive execution flow (no policy, intent_builder used)
            print("")
            print("=" * 80)
            print(f"INTERACTIVE EXECUTION - {result['selected_count']} SELECTED CANDIDATE(S)")
            print("=" * 80)
            print("")
            
            logger.info(f"Proceeding to quote-only for {result['selected_count']} selected candidate(s)...")
            logger.info("")
            
            selected = result.get("selected", [])
            receipts = []
            
            for i, candidate in enumerate(selected, 1):
                print(f"\n{'='*80}")
                print(f"CANDIDATE {i} of {result['selected_count']}")
                print(f"{'='*80}")
                # CRITICAL: Recalculate EV/$ using SAME values as sensitivity analysis
                p_used = candidate.get("p_event_used") or candidate.get("assumed_p_event")
                debit = candidate.get("debit_per_contract") or candidate.get("computed_premium_usd", 0)
                max_gain = candidate.get("max_gain_per_contract", 0)
                
                if p_used is not None and debit > 0:
                    ev_usd, ev_per_premium_dollar = calculate_ev_at_probability(p_used, debit, max_gain)
                    stored_ev_per_dollar = candidate.get('ev_per_dollar', 0)
                    if abs(ev_per_premium_dollar - stored_ev_per_dollar) > 0.01:
                        logger.warning(
                            f"⚠️  EV/$ RECALCULATED: stored={stored_ev_per_dollar:.2f}, "
                            f"recalc={ev_per_premium_dollar:.2f} (using p={p_used:.3f})"
                        )
                else:
                    ev_usd = 0
                    ev_per_premium_dollar = 0
                
                print(f"Underlier: {candidate.get('underlier')}")
                print(f"Regime: {candidate.get('regime')}")
                print(f"Expiry: {candidate.get('expiry')}")
                print(f"Strikes: {candidate.get('long_strike'):.0f}/{candidate.get('short_strike'):.0f}")
                print(f"EV_USD: ${ev_usd:.2f} | EV_per_premium_dollar: {ev_per_premium_dollar:.2f}")
                
                p_used = candidate.get("p_event_used")
                p_impl = candidate.get("p_implied")
                p_ext = candidate.get("p_external")
                p_src = candidate.get("p_source")
                p_ext_metadata = candidate.get("p_external_metadata")
                p_ext_status = candidate.get("p_external_status")
                p_ext_reason = candidate.get("p_external_reason")
                
                p_used_str = f"{p_used:.3f}" if p_used is not None else "—"
                p_impl_str = f"{p_impl:.3f}" if p_impl is not None else "—"
                p_ext_str = f"{p_ext:.3f}" if p_ext is not None else "—"
                p_src_str = p_src if p_src is not None else "—"
                
                print(f"Probability: P_used={p_used_str} | P_impl={p_impl_str} | P_ext={p_ext_str} | source={p_src_str}")
                
                if p_ext_metadata and isinstance(p_ext_metadata, dict):
                    ticker = p_ext_metadata.get("market_ticker") or "N/A"
                    value = p_ext_metadata.get("value")
                    source = p_ext_metadata.get("source") or "N/A"
                    timestamp = p_ext_metadata.get("asof_ts_utc") or "N/A"
                    exact_match = p_ext_metadata.get("exact_match", False)
                    confidence = p_ext_metadata.get("mapping_confidence", 0)
                    value_str = f"{value:.3f}" if value is not None else "N/A"
                    print(f"P_EXT: {value_str} ({source}) | market: {ticker} | exact_match: {exact_match} | "
                          f"ts: {timestamp[:19] if len(timestamp) > 19 else timestamp} | conf: {confidence:.1f}")
                elif p_ext_status:
                    print(f"P_EXT: — | status: {p_ext_status} | reason: {p_ext_reason or 'N/A'}")
                else:
                    print(f"P_EXT: — | status: NO_DATA")
                
                print("")
                
                logger.info(f"Building intent for candidate {i}...")
                
                from forecast_arb.execution.intent_builder import build_order_intent
                
                candidate_for_builder = {
                    "underlier": candidate.get("underlier"),
                    "expiry": candidate.get("expiry"),
                    "strikes": {
                        "long_put": candidate.get("long_strike"),
                        "short_put": candidate.get("short_strike")
                    },
                    "debit_per_contract": candidate.get("computed_premium_usd", 0),
                    "rank": 1
                }
                
                intent = build_order_intent(
                    candidate=candidate_for_builder,
                    regime=candidate.get("regime", "crash")
                )
                
                from forecast_arb.execution.intent_builder import emit_intent
                intent_path = emit_intent(intent, output_dir="intents")
                
                logger.info(f"✓ Intent created: {intent_path}")
                logger.info("Running quote-only check...")
                
                cmd = [
                    sys.executable,
                    "-m", "forecast_arb.execution.execute_trade",
                    "--intent", intent_path,
                    "--paper",
                    "--quote-only",
                    "--host", args.ibkr_host,
                    "--port", str(args.ibkr_port)
                ]
                
                exec_result_proc = subprocess.run(cmd, capture_output=True, text=True, env=SUBPROCESS_ENV)
                print(exec_result_proc.stdout)
                
                if exec_result_proc.returncode != 0:
                    logger.error(f"❌ Quote-only check failed for candidate {i}:")
                    logger.error(exec_result_proc.stderr)
                    print(f"\n⚠️  Candidate {i} BLOCKED - skipping to next\n")
                    continue
                
                result_path = Path(intent_path).parent / "execution_result.json"
                if not result_path.exists():
                    logger.error(f"❌ execution_result.json not found")
                    continue
                
                with open(result_path, "r") as f:
                    exec_result = json.load(f)
                
                guards_passed = exec_result.get("guards_passed", False)
                if not guards_passed:
                    logger.error(f"❌ Guards failed for candidate {i}: {exec_result.get('guards_result')}")
                    print(f"\n⚠️  Candidate {i} BLOCKED - skipping to next\n")
                    continue
                
                logger.info(f"✅ GUARDS PASSED for candidate {i} - OK_TO_STAGE")
                print_ev_sensitivity(candidate)
                
                receipt = offer_execution_options(
                    intent_path=intent_path,
                    run_dir=f"runs/campaign/{result['campaign_run_id']}",
                    exec_result=exec_result,
                    ibkr_host=args.ibkr_host,
                    ibkr_port=args.ibkr_port
                )
                receipts.append(receipt)
            
            print("")
            print("=" * 80)
            print("CAMPAIGN EXECUTION COMPLETE")
            print("=" * 80)
            print(f"Processed: {len(receipts)} candidate(s)")
            for i, receipt in enumerate(receipts, 1):
                print(f"  {i}. {receipt.get('status')} - intent_id: {receipt.get('intent_id')[:16]}...")
            print("=" * 80)
            print("")

        # Run allocator if --policy passed
        if args.policy:
            # v1.6 Task 1: resolve candidates path using fallback chain (always print diagnostics)
            print("")
            print("=" * 80)
            print("CANDIDATES PATH RESOLUTION")
            print("=" * 80)
            candidates_path = _resolve_candidates_path(result)

            plan_summary = _run_allocator(
                args.policy, candidates_path, verbose=args.verbose
            )

            # v1.5 Task 4 / v1.6 Task 3: run execute if --execute flag set
            exec_summary: Optional[Dict[str, Any]] = None
            reconcile_summary: Optional[Dict[str, Any]] = None
            if args.execute:
                execute_mode = "live" if args.live else "paper"
                exec_summary = _run_execute_stage(
                    policy_path=args.policy,
                    execute_mode=execute_mode,
                    quote_only=args.quote_only,
                )

                # v1.7: run reconcile if --reconcile flag and not quote-only
                if args.reconcile and not args.quote_only and exec_summary is not None:
                    reconcile_summary = _run_reconcile_stage(
                        policy_path=args.policy,
                        execute_mode=execute_mode,
                    )

            # v1.6 Task 3: operator-facing summary (always printed)
            _print_operator_summary(
                candidates_path=candidates_path,
                plan_summary=plan_summary,
                execute=args.execute,
                exec_mode="live" if args.live else "paper",
                quote_only=args.quote_only,
                exec_summary=exec_summary,
                commit_ledger_path=_get_commit_ledger_path_for_policy(args.policy),
                reconcile_summary=reconcile_summary,
            )

        logger.info("✅ CAMPAIGN MODE COMPLETE")

        return
    
    # Non-campaign mode (standard flow)
    logger.info("Mode: STANDARD (single regime)")
    logger.info("")
    
    # Step 1: Run daily orchestration
    run_dir, review_candidates_path = run_daily_orchestration(
        regime=args.regime,
        config_path=args.config,
        snapshot_path=args.snapshot,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port,
        dte_min=args.dte_min,
        dte_max=args.dte_max,
        min_debit=args.min_debit,
        fallback_p=args.fallback_p
    )
    
    # Step 2: Load review candidates
    review_candidates = load_review_candidates(review_candidates_path)
    
    # Step 3: Print candidate table
    print_candidate_table(review_candidates)
    
    # Step 4: Select candidate
    regime, candidate = select_candidate_interactive(
        review_candidates,
        regime_filter=args.auto_regime,
        auto_rank=args.auto_rank
    )
    
    # Step 5: Perform quote-only check (also creates intent via intent_builder)
    exec_result, intent_path = perform_quote_only(
        review_candidates_path=review_candidates_path,
        regime=regime,
        rank=candidate.get("rank", 1),
        run_dir=run_dir,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port
    )
    
    # Step 6: Offer execution options (uses already-created intent)
    receipt = offer_execution_options(
        intent_path=intent_path,
        run_dir=run_dir,
        exec_result=exec_result,
        ibkr_host=args.ibkr_host,
        ibkr_port=args.ibkr_port
    )
    
    # Step 7: Print final receipt
    print_final_receipt(receipt)
    
    logger.info("✅ DAILY CONSOLE COMPLETE")


if __name__ == "__main__":
    main()
