"""
Test run_real_cycle.py with IBKR snapshot integration
"""

import pytest
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from forecast_arb.engine.crash_venture_v1_snapshot import (
    run_crash_venture_v1_snapshot,
    generate_candidates_from_snapshot,
    find_nearest_strike,
    validate_put_option_pricing,
    compute_debit_from_put_spread
)
from forecast_arb.structuring.snapshot_io import (
    load_snapshot,
    validate_snapshot,
    get_snapshot_metadata,
    get_expiries,
    get_strikes_for_expiry,
    get_puts_for_expiry,
    get_option_by_strike
)


def test_load_ibkr_snapshot_spy():
    """Test loading the example IBKR snapshot."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    
    snapshot = load_snapshot(snapshot_path)
    assert snapshot is not None
    
    # Validate structure
    validate_snapshot(snapshot)
    
    # Check metadata
    metadata = get_snapshot_metadata(snapshot)
    assert metadata["underlier"] == "SPY"
    assert metadata["current_price"] == 502.45
    assert "snapshot_time" in metadata
    
    # Check expiries
    expiries = get_expiries(snapshot)
    assert len(expiries) > 0
    assert "20260227" in expiries


def test_strikes_are_in_snapshot():
    """Test that strikes are from actual snapshot, not computed."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    snapshot = load_snapshot(snapshot_path)
    
    expiry = "20260227"
    available_strikes = get_strikes_for_expiry(snapshot, expiry)
    
    assert len(available_strikes) > 0
    
    # Check that strikes are integers (no decimals)
    for strike in available_strikes:
        assert strike == int(strike) or strike == float(int(strike)), \
            f"Strike {strike} has decimals"
    
    # Check specific strikes from the snapshot
    assert 485.0 in available_strikes
    assert 490.0 in available_strikes
    assert 495.0 in available_strikes
    assert 500.0 in available_strikes
    assert 505.0 in available_strikes


def test_find_nearest_strike():
    """Test finding nearest available strike."""
    available_strikes = [485.0, 490.0, 495.0, 500.0, 505.0, 510.0]
    
    # Test exact match
    assert find_nearest_strike(available_strikes, 500.0) == 500.0
    
    # Test nearest below
    assert find_nearest_strike(available_strikes, 492.0) == 490.0
    
    # Test nearest above
    assert find_nearest_strike(available_strikes, 493.0) == 495.0
    
    # Test computed target (should find nearest actual strike)
    S0 = 502.45
    target = S0 * 0.90  # 452.205
    nearest = find_nearest_strike(available_strikes, target)
    assert nearest in available_strikes  # Must be actual strike


def test_validate_put_option_pricing():
    """Test put option pricing validation."""
    # Valid put
    valid_put = {
        "strike": 500.0,
        "bid": 7.50,
        "ask": 8.00
    }
    is_valid, reason = validate_put_option_pricing(valid_put, 500.0)
    assert is_valid
    assert reason == "OK"
    
    # Invalid: bid = 0
    invalid_bid = {
        "strike": 500.0,
        "bid": 0.0,
        "ask": 8.00
    }
    is_valid, reason = validate_put_option_pricing(invalid_bid, 500.0)
    assert not is_valid
    assert "bid" in reason.lower()
    
    # Invalid: ask <= bid
    invalid_ask = {
        "strike": 500.0,
        "bid": 8.00,
        "ask": 7.50
    }
    is_valid, reason = validate_put_option_pricing(invalid_ask, 500.0)
    assert not is_valid
    assert "ask" in reason.lower()
    
    # Invalid: None option
    is_valid, reason = validate_put_option_pricing(None, 500.0)
    assert not is_valid
    assert "not found" in reason.lower()


def test_compute_debit_from_put_spread():
    """Test debit computation for put spread."""
    long_put = {
        "bid": 7.50,
        "ask": 8.00
    }
    short_put = {
        "bid": 5.15,
        "ask": 5.65
    }
    
    # Debit = long_mid - short_mid
    debit = compute_debit_from_put_spread(long_put, short_put)
    
    long_mid = (7.50 + 8.00) / 2.0  # 7.75
    short_mid = (5.15 + 5.65) / 2.0  # 5.40
    expected = long_mid - short_mid  # 2.35
    
    assert abs(debit - expected) < 0.01


def test_generate_candidates_from_snapshot():
    """Test candidate generation from snapshot."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    snapshot = load_snapshot(snapshot_path)
    
    metadata = get_snapshot_metadata(snapshot)
    S0 = metadata["current_price"]  # 502.45
    expiry = "20260227"
    
    # Use less aggressive moneyness that will find strikes in snapshot (485-530 range)
    # S0=502.45, so -0.02 → ~492, -0.04 → ~482
    moneyness_targets = [-0.02, -0.04]
    spread_widths = [5, 10]
    min_debit = 1.0  # Low threshold for testing
    max_candidates = 10
    
    candidates, filtered_out = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        min_debit_per_contract=min_debit,
        max_candidates=max_candidates
    )
    
    # Should generate some candidates
    assert len(candidates) > 0, "No candidates generated"
    
    # Check each candidate
    available_strikes = get_strikes_for_expiry(snapshot, expiry)
    
    for candidate in candidates:
        # Strikes must be from snapshot
        K_long = candidate["strikes"]["long_put"]
        K_short = candidate["strikes"]["short_put"]
        
        assert K_long in available_strikes, f"Long strike {K_long} not in snapshot"
        assert K_short in available_strikes, f"Short strike {K_short} not in snapshot"
        
        # Debit must be > 0
        debit = candidate["debit_per_contract"]
        assert debit > 0, f"Debit {debit} must be >0"
        
        # Max loss must be > 0
        max_loss = candidate["max_loss_per_contract"]
        assert max_loss > 0, f"Max loss {max_loss} must be >0"
        
        # Max gain must be > 0
        max_gain = candidate["max_gain_per_contract"]
        assert max_gain > 0, f"Max gain {max_gain} must be >0"
        
        # Check debit >= min_debit filter
        assert debit >= min_debit, f"Debit {debit} < min {min_debit}"


def test_run_with_ibkr_snapshot():
    """Test full run with IBKR snapshot."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    config_path = "configs/test_structuring_crash_venture_v1.yaml"
    p_event = 0.30
    min_debit = 1.0  # Lower threshold to ensure we get candidates
    
    result = run_crash_venture_v1_snapshot(
        config_path=config_path,
        snapshot_path=snapshot_path,
        p_event=p_event,
        min_debit_per_contract=min_debit
    )
    
    # Check result structure
    assert "run_id" in result
    assert "run_dir" in result
    assert "top_structures" in result
    assert "manifest" in result
    
    # Check we got structures
    assert len(result["top_structures"]) > 0, "No structures output"
    
    # Check first structure
    struct = result["top_structures"][0]
    
    # Underlier should be SPY (from snapshot)
    assert struct["underlier"] == "SPY"
    
    # Check non-zero values
    assert struct["debit_per_contract"] > 0, "Debit must be >0"
    assert struct["max_loss_per_contract"] > 0, "Max loss must be >0"
    assert struct["max_gain_per_contract"] > 0, "Max gain must be >0"
    
    # Check strikes are from snapshot
    snapshot = load_snapshot(snapshot_path)
    expiry = struct["expiry"]
    available_strikes = get_strikes_for_expiry(snapshot, expiry)
    
    K_long = struct["strikes"]["long_put"]
    K_short = struct["strikes"]["short_put"]
    
    assert K_long in available_strikes, f"Long strike {K_long} not in snapshot"
    assert K_short in available_strikes, f"Short strike {K_short} not in snapshot"
    
    # Check that spot price matches snapshot
    metadata = get_snapshot_metadata(snapshot)
    assert struct["spot_used"] == metadata["current_price"]
    
    # Check manifest
    manifest = result["manifest"]
    assert manifest["mode"] == "crash_venture_v1_snapshot"
    assert manifest["inputs"]["snapshot_path"] == snapshot_path
    assert manifest["inputs"]["p_event"] == p_event
    assert manifest["n_candidates_generated"] > 0
    
    print(f"\n✓ Test passed!")
    print(f"  Run ID: {result['run_id']}")
    print(f"  Structures: {len(result['top_structures'])}")
    print(f"  First structure: {struct['template_name']}")
    print(f"    Long Put: ${K_long:.2f}")
    print(f"    Short Put: ${K_short:.2f}")
    print(f"    Debit: ${struct['debit_per_contract']:.2f}")


def test_min_debit_filter():
    """Test that min_debit filter works correctly."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    snapshot = load_snapshot(snapshot_path)
    
    metadata = get_snapshot_metadata(snapshot)
    S0 = metadata["current_price"]
    expiry = "20260227"
    
    moneyness_targets = [-0.10]
    spread_widths = [5]
    min_debit_low = 1.0
    min_debit_high = 100.0  # Very high threshold
    max_candidates = 10
    
    # Generate with low threshold
    candidates_low, _ = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        min_debit_per_contract=min_debit_low,
        max_candidates=max_candidates
    )
    
    # Generate with high threshold
    candidates_high, filtered_high = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        min_debit_per_contract=min_debit_high,
        max_candidates=max_candidates
    )
    
    # High threshold should filter out more candidates
    assert len(candidates_high) <= len(candidates_low)
    
    # High threshold might filter out all candidates
    if len(candidates_high) == 0:
        assert len(filtered_high) > 0, "Should have diagnostics for filtered candidates"
        
        # Check diagnostic reasons include per-contract debit values
        for item in filtered_high:
            assert "reason" in item
            if "debit_per_contract" in item:
                # Verify the filter is using per-contract values
                assert item["debit_per_contract"] < min_debit_high
                # Reason should mention per-contract units
                assert "per contract" in item["reason"].lower() or "per-contract" in item["reason"].lower()


def test_min_debit_per_contract_units():
    """Unit regression test: Ensure min_debit_per_contract filter uses correct units."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    snapshot = load_snapshot(snapshot_path)
    
    metadata = get_snapshot_metadata(snapshot)
    S0 = metadata["current_price"]
    expiry = "20260227"
    
    # Use moneyness that will produce low debit spreads
    moneyness_targets = [-0.02]  # Close to ATM
    spread_widths = [5]  # Narrow spread
    min_debit_threshold = 50.0  # $50 per contract threshold
    max_candidates = 10
    
    candidates, filtered_out = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        min_debit_per_contract=min_debit_threshold,
        max_candidates=max_candidates
    )
    
    # Check all passing candidates have debit_per_contract >= threshold
    for candidate in candidates:
        debit_per_contract = candidate.get("debit_per_contract", 0)
        assert debit_per_contract >= min_debit_threshold, \
            f"Candidate debit_per_contract ${debit_per_contract:.2f} < threshold ${min_debit_threshold:.2f}"
    
    # Check all filtered candidates have debit_per_contract < threshold (if filtered for debit)
    for item in filtered_out:
        if "debit_per_contract" in item and "Debit per contract" in item.get("reason", ""):
            debit_per_contract = item["debit_per_contract"]
            assert debit_per_contract < min_debit_threshold, \
                f"Filtered item debit_per_contract ${debit_per_contract:.2f} >= threshold ${min_debit_threshold:.2f}"
            
            # Verify the log message shows per-contract units (not per-share)
            reason = item["reason"]
            assert "$" in reason, "Filter reason should show dollar amounts"
            # Should say something like "$12.00 < $50.00", not "$0.12 < $50.00"
            assert "per contract" in reason.lower() or "per-contract" in reason.lower(), \
                "Filter reason should specify per-contract units"
    
    print(f"✓ Unit regression test passed: {len(candidates)} candidates above threshold, "
          f"{len([x for x in filtered_out if 'debit_per_contract' in x])} filtered")


def test_width_integrity():
    """Test that requested widths do not collapse into identical structures."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    snapshot = load_snapshot(snapshot_path)
    
    metadata = get_snapshot_metadata(snapshot)
    S0 = metadata["current_price"]
    expiry = "20260227"
    
    # Test with different widths
    moneyness_targets = [-0.02]
    spread_widths = [5, 10, 15]  # Different requested widths
    min_debit = 1.0
    max_candidates = 10
    
    candidates, filtered_out = generate_candidates_from_snapshot(
        snapshot=snapshot,
        expiry=expiry,
        S0=S0,
        moneyness_targets=moneyness_targets,
        spread_widths=spread_widths,
        min_debit_per_contract=min_debit,
        max_candidates=max_candidates
    )
    
    # Check that we don't have duplicate structures
    structures_set = set()
    
    for candidate in candidates:
        K_long = candidate["strikes"]["long_put"]
        K_short = candidate["strikes"]["short_put"]
        effective_width = K_long - K_short
        requested_width = candidate["width_target"]
        
        # Check width deviation is within tolerance
        width_deviation = abs(effective_width - requested_width)
        assert width_deviation <= 2.50, \
            f"Width deviation {width_deviation:.2f} exceeds $2.50 tolerance"
        
        # Check for duplicate structures
        structure_key = (K_long, K_short)
        assert structure_key not in structures_set, \
            f"Duplicate structure found: K_long={K_long}, K_short={K_short}"
        structures_set.add(structure_key)
    
    # Check filtered diagnostics include width deviation info
    for item in filtered_out:
        if "effective_width" in item:
            assert "requested_width" in item
            assert "reason" in item
            assert "deviation" in item["reason"].lower() or "width" in item["reason"].lower()
    
    print(f"✓ Width integrity test passed: {len(candidates)} unique structures, "
          f"{len(filtered_out)} filtered")


def test_max_loss_never_zero_with_positive_debit():
    """Test that real_cycle printout never shows max_loss = 0 when debit > 0."""
    snapshot_path = "examples/ibkr_snapshot_spy.json"
    config_path = "configs/test_structuring_crash_venture_v1.yaml"
    p_event = 0.30
    min_debit = 1.0
    
    result = run_crash_venture_v1_snapshot(
        config_path=config_path,
        snapshot_path=snapshot_path,
        p_event=p_event,
        min_debit_per_contract=min_debit
    )
    
    # Check every structure in top_structures
    for struct in result["top_structures"]:
        debit_per_contract = struct.get("debit_per_contract", 0)
        max_loss_per_contract = struct.get("max_loss_per_contract", 0)
        max_gain_per_contract = struct.get("max_gain_per_contract", 0)
        
        # CRITICAL ASSERTION: If debit > 0, max_loss must also be > 0
        if debit_per_contract > 0:
            assert max_loss_per_contract > 0, \
                f"Structure rank {struct['rank']}: debit={debit_per_contract:.2f} > 0 " \
                f"but max_loss={max_loss_per_contract:.2f} == 0"
            
            assert max_gain_per_contract > 0, \
                f"Structure rank {struct['rank']}: debit={debit_per_contract:.2f} > 0 " \
                f"but max_gain={max_gain_per_contract:.2f} == 0"
        
        # For put spreads, max_loss should equal debit
        if struct.get("template_name") == "put_spread":
            assert abs(max_loss_per_contract - debit_per_contract) < 0.01, \
                f"Put spread: max_loss ({max_loss_per_contract:.2f}) should equal " \
                f"debit ({debit_per_contract:.2f})"
        
        # EV per dollar calculation validation
        ev_per_dollar = struct.get("ev_per_dollar", 0)
        ev_per_contract = struct.get("ev_per_contract", 0)
        
        if debit_per_contract > 0:
            expected_ev_per_dollar = ev_per_contract / debit_per_contract
            assert abs(ev_per_dollar - expected_ev_per_dollar) < 0.001, \
                f"EV per dollar mismatch: got {ev_per_dollar:.4f}, " \
                f"expected {expected_ev_per_dollar:.4f}"
    
    print(f"✓ Max loss validation passed for {len(result['top_structures'])} structures")


# NOTE: Removed test_dict_format_string_handling and test_dict_payload_with_none_values
# These tests relied on examples/run_real_cycle.py which has been consolidated into scripts/run_daily.py
# The functionality is now tested through the integration tests above


if __name__ == "__main__":
    # Run tests
    test_load_ibkr_snapshot_spy()
    print("✓ test_load_ibkr_snapshot_spy passed")
    
    test_strikes_are_in_snapshot()
    print("✓ test_strikes_are_in_snapshot passed")
    
    test_find_nearest_strike()
    print("✓ test_find_nearest_strike passed")
    
    test_validate_put_option_pricing()
    print("✓ test_validate_put_option_pricing passed")
    
    test_compute_debit_from_put_spread()
    print("✓ test_compute_debit_from_put_spread passed")
    
    test_generate_candidates_from_snapshot()
    print("✓ test_generate_candidates_from_snapshot passed")
    
    test_run_with_ibkr_snapshot()
    print("✓ test_run_with_ibkr_snapshot passed")
    
    test_min_debit_filter()
    print("✓ test_min_debit_filter passed")
    
    test_width_integrity()
    print("✓ test_width_integrity passed")
    
    test_max_loss_never_zero_with_positive_debit()
    print("✓ test_max_loss_never_zero_with_positive_debit passed")
    
    print("\n✅ All tests passed!")
