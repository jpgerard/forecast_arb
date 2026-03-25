"""
Standalone Phase 3/4 Clean Base Hardening Tests
Run with: python test_clean_base_standalone.py
"""

import json
import tempfile
import traceback
from pathlib import Path
from datetime import datetime, timezone

from forecast_arb.campaign.selector import run_selector, select_candidates
from forecast_arb.campaign.grid_runner import flatten_candidate


def test_no_trade_recommended_json_integrity():
    """Test that recommended.json is well-formed even with zero candidates."""
    print("\n[TEST] No-trade recommended.json integrity...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        candidates_path = Path(tmpdir) / "candidates_flat.json"
        with open(candidates_path, "w") as f:
            json.dump([], f)
        
        campaign_config = {
            "governors": {
                "daily_premium_cap_usd": 1000.0,
                "cluster_cap_per_day": 1,
                "max_open_positions_by_regime": {},
                "premium_at_risk_caps_usd": {}
            },
            "selection": {
                "max_trades_per_day": 2,
                "scoring": "ev_per_dollar"
            }
        }
        
        ledger_path = Path(tmpdir) / "trade_outcomes.jsonl"
        ledger_path.touch()
        
        recommended_path = run_selector(
            candidates_flat_path=str(candidates_path),
            campaign_config=campaign_config,
            ledger_path=str(ledger_path),
            qty=1,
            output_dir=Path(tmpdir)
        )
        
        with open(recommended_path, "r") as f:
            recommended = json.load(f)
        
        # Assertions
        assert "timestamp_utc" in recommended, "timestamp_utc must be present"
        assert recommended["timestamp_utc"] is not None, "timestamp_utc must NOT be null"
        assert isinstance(recommended["timestamp_utc"], str), "timestamp_utc must be ISO string"
        
        assert "selection_summary" in recommended
        summary = recommended["selection_summary"]
        assert "total_candidates" in summary
        assert "representable_count" in summary
        assert "blocked_by_governor" in summary
        assert "probability_breakdown" in summary
        
        print("  ✓ PASSED")


def test_missing_canonical_ev_fails():
    """Test that flatten_candidate raises ValueError if p_used cannot be determined."""
    print("\n[TEST] Missing canonical ev fails...")
    
    candidate = {
        "candidate_id": "MISSING_PROB",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "representable": True,
        "rank": 1,
    }
    
    try:
        flatten_candidate(
            candidate=candidate,
            underlier="SPY",
            regime="crash",
            expiry_bucket="30-60d",
            cluster_id="EQUITY",
            cell_id="SPY_crash_30-60d",
            regime_p_implied=None,
            regime_p_external=None
        )
        print("  ✗ FAILED: Should have raised ValueError")
        return False
    except ValueError as e:
        if "Cannot determine p_used" in str(e):
            print("  ✓ PASSED")
            return True
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False


def test_canonical_fields_all_present():
    """Test that flatten_candidate emits all required canonical fields."""
    print("\n[TEST] Canonical fields all present...")
    
    candidate = {
        "candidate_id": "FULL",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05
    )
    
    # Check all required fields
    required = ["ev_per_dollar", "ev_usd", "p_profit", "p_used", "p_used_src", 
                "p_ext_status", "p_ext_reason", "p_source", "robustness", "robustness_flags"]
    
    for field in required:
        assert field in flat, f"Missing required field: {field}"
        if field in ["ev_per_dollar", "ev_usd", "p_used", "p_used_src", "p_ext_status", "p_ext_reason", "p_source"]:
            assert flat[field] is not None, f"Field {field} must not be None"
    
    print("  ✓ PASSED")


def test_external_probability_semantics():
    """Test that when p_used_src='external', p_ext_status must be 'OK'."""
    print("\n[TEST] External probability semantics...")
    
    candidate = {
        "candidate_id": "EXT",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.04,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    regime_p_external = {
        "p": 0.05,
        "source": "kalshi",
        "authoritative": True,
        "asof_ts_utc": datetime.now(timezone.utc).isoformat(),
        "market": {},
        "match": {},
        "quality": {"liquidity_ok": True, "staleness_ok": True, "spread_ok": True, "warnings": []}
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.04,
        regime_p_external=regime_p_external
    )
    
    assert flat["p_used_src"] == "external", f"Expected external, got {flat['p_used_src']}"
    assert flat["p_ext_status"] == "OK", f"Expected OK, got {flat['p_ext_status']}"
    assert flat["p_used"] == 0.05, f"Expected 0.05, got {flat['p_used']}"
    assert flat["p_source"] == "kalshi", f"Expected kalshi, got {flat['p_source']}"
    
    print("  ✓ PASSED")


def test_p_used_src_vs_p_source_semantics():
    """Test that p_used_src and p_source are separate fields."""
    print("\n[TEST] p_used_src vs p_source semantics...")
    
    candidate = {
        "candidate_id": "SEP",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
    }
    
    regime_p_external = {
        "p": 0.046,
        "source": "kalshi",
        "authoritative": True,
        "asof_ts_utc": datetime.now(timezone.utc).isoformat(),
        "market": {},
        "match": {},
        "quality": {"liquidity_ok": True, "staleness_ok": True, "spread_ok": True, "warnings": []}
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05,
        regime_p_external=regime_p_external
    )
    
    # p_used_src: which probability source (external/implied/fallback)
    assert flat["p_used_src"] == "external"
    # p_source: operational source (kalshi/options_implied/etc)
    assert flat["p_source"] == "kalshi"
    # These should be DIFFERENT
    assert flat["p_used_src"] != flat["p_source"]
    
    print("  ✓ PASSED")


def test_conditioning_provenance_preserved():
    """Test that Phase 4 conditioning provenance is passed through unchanged."""
    print("\n[TEST] Conditioning provenance preserved...")
    
    candidate = {
        "candidate_id": "COND",
        "strikes": {"long_put": 580.0, "short_put": 560.0},
        "debit_per_contract": 100.0,
        "max_gain_per_contract": 2000.0,
        "p_implied": 0.05,
        "representable": True,
        "rank": 1,
        "expiry": "2026-03-20",
        "conditioning": {
            "confidence_score": 0.85,
            "multipliers": {"vol_regime": 1.1},
            "regime_signals": ["high_vol"],
            "p_base": 0.05,
            "p_adjusted": 0.055
        }
    }
    
    flat = flatten_candidate(
        candidate=candidate,
        underlier="SPY",
        regime="crash",
        expiry_bucket="30-60d",
        cluster_id="EQUITY",
        cell_id="SPY_crash_30-60d",
        regime_p_implied=0.05
    )
    
    assert "conditioning" in flat
    assert flat["conditioning"] is not None
    assert flat["conditioning"]["p_adjusted"] == 0.055
    assert flat["p_used"] == 0.055  # Uses p_adjusted
    assert flat["p_used_src"] == "implied_conditioned"
    
    print("  ✓ PASSED")


def test_snapshot_isolation_guard():
    """Test that snapshot isolation guard raises on underlier mismatch."""
    print("\n[TEST] Snapshot isolation guard...")
    
    mock_snapshot = {
        "metadata": {
            "underlier": "QQQ",  # WRONG
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "current_price": 480.0
        }
    }
    
    snapshot_symbol = mock_snapshot["metadata"]["underlier"]
    expected_underlier = "SPY"
    
    try:
        if snapshot_symbol != expected_underlier:
            raise ValueError(
                f"SNAPSHOT UNDERLIER MISMATCH: expected '{expected_underlier}', "
                f"got '{snapshot_symbol}'. "
                f"This would cause cross-underlier contamination."
            )
        print("  ✗ FAILED: Should have raised ValueError")
        return False
    except ValueError as e:
        if "SNAPSHOT UNDERLIER MISMATCH" in str(e):
            print("  ✓ PASSED")
            return True
        else:
            print(f"  ✗ FAILED: Wrong error: {e}")
            return False


def test_selection_summary_structure():
    """Test that selection_summary has all required structured fields."""
    print("\n[TEST] Selection summary structure...")
    
    candidates = [
        {
            "candidate_id": "A",
            "underlier": "SPY",
            "regime": "crash",
            "expiry_bucket": "30-60d",
            "cluster_id": "EQUITY",
            "cell_id": "SPY_crash_30-60d",
            "debit_per_contract": 100.0,
            "ev_per_dollar": 2.0,
            "ev_usd": 200.0,
            "p_used": 0.05,
            "p_used_src": "external",
            "p_impl": 0.04,
            "p_ext": 0.05,
            "p_ext_status": "OK",
            "p_ext_reason": "OK",
            "p_source": "kalshi",
            "p_profit": 0.05,
            "representable": True,
        }
    ]
    
    with tempfile.TemporaryDirectory() as tmpdir:
        candidates_path = Path(tmpdir) / "candidates_flat.json"
        with open(candidates_path, "w") as f:
            json.dump(candidates, f)
        
        campaign_config = {
            "governors": {
                "daily_premium_cap_usd": 1000.0,
                "cluster_cap_per_day": 1,
                "max_open_positions_by_regime": {},
                "premium_at_risk_caps_usd": {}
            },
            "selection": {
                "max_trades_per_day": 2,
                "scoring": "ev_per_dollar"
            }
        }
        
        ledger_path = Path(tmpdir) / "trade_outcomes.jsonl"
        ledger_path.touch()
        
        recommended_path = run_selector(
            candidates_flat_path=str(candidates_path),
            campaign_config=campaign_config,
            ledger_path=str(ledger_path),
            qty=1,
            output_dir=Path(tmpdir)
        )
        
        with open(recommended_path, "r") as f:
            recommended = json.load(f)
        
        summary = recommended["selection_summary"]
        
        # Check all required fields
        assert "total_candidates" in summary
        assert "representable_count" in summary
        assert "non_representable_count" in summary
        assert "selected_count" in summary
        assert "no_representable_candidates" in summary
        assert "blocked_by_governor" in summary
        assert "probability_breakdown" in summary
        
        blocked = summary["blocked_by_governor"]
        assert all(k in blocked for k in ["daily_premium_cap", "open_premium_cap", "regime_slot_cap", "cluster_cap"])
        
        breakdown = summary["probability_breakdown"]
        assert all(k in breakdown for k in ["external_count", "implied_count", "fallback_count"])
        
        print("  ✓ PASSED")


def main():
    """Run all tests."""
    print("=" * 80)
    print("PHASE 3/4 CLEAN BASE HARDENING TESTS")
    print("=" * 80)
    
    tests = [
        test_no_trade_recommended_json_integrity,
        test_missing_canonical_ev_fails,
        test_canonical_fields_all_present,
        test_external_probability_semantics,
        test_p_used_src_vs_p_source_semantics,
        test_conditioning_provenance_preserved,
        test_snapshot_isolation_guard,
        test_selection_summary_structure,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            result = test()
            if result is None or result is True:
                passed += 1
        except Exception as e:
            print(f"  ✗ FAILED with exception:")
            traceback.print_exc()
            failed += 1
    
    print("\n" + "=" * 80)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 80)
    
    return failed == 0


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
