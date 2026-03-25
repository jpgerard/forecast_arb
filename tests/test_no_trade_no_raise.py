"""
Test NO_TRADE scenarios return cleanly without raising exceptions.

This test ensures that when the engine decides NO_TRADE (no candidates,
failed filters, etc.), it returns a structured result rather than raising.
"""

import json
import pytest
from pathlib import Path
import tempfile
import shutil

from forecast_arb.engine.crash_venture_v1_snapshot import run_crash_venture_v1_snapshot


def create_minimal_snapshot_shallow_strikes(spot: float = 690.0, shallow: bool = True):
    """
    Create a minimal snapshot with shallow strike coverage.
    
    When shallow=True, only includes strikes very close to spot,
    which will cause NO_TRADE for deep OTM targets like -8% to -15%.
    """
    expiry = "20260228"
    
    if shallow:
        # Very limited strike range: only ±2% around spot
        # For spot=690, this gives roughly 676-704
        strikes_below = [spot - i for i in range(1, 15, 1)]  # 14 strikes below
        strikes_above = [spot + i for i in range(0, 15, 1)]  # 15 strikes above
        all_strikes = sorted(strikes_below + strikes_above)
    else:
        # Deep strike range for comparison
        strikes_below = [spot - i for i in range(1, 100, 5)]  # Many strikes below
        strikes_above = [spot + i for i in range(0, 50, 5)]
        all_strikes = sorted(strikes_below + strikes_above)
    
    # Create minimal put options with valid bid/ask
    puts = []
    for strike in all_strikes:
        # Simple pricing model: further OTM = cheaper
        otm_factor = max(0.01, (spot - strike) / spot) if strike < spot else 0.01
        mid_price = max(0.10, otm_factor * 10.0)
        
        puts.append({
            "strike": strike,
            "bid": mid_price * 0.95,
            "ask": mid_price * 1.05,
            "last": mid_price,
            "volume": 100,
            "open_interest": 500,
            "implied_vol": 0.15,
            "delta": -0.3 if strike < spot else -0.05
        })
    
    snapshot = {
        "snapshot_metadata": {
            "underlier": "SPY",
            "snapshot_time": "2026-01-29T12:00:00+00:00",
            "current_price": spot,
            "spot_source": "last",
            "spot_is_stale": False,
            "spot_warnings": [],
            "spot_audit": {},
            "atm_strike": spot,
            "atm_distance": 0.0,
            "dte_min": 30,
            "dte_max": 60,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0
        },
        "expiries": {
            expiry: {
                "expiry_date": expiry,
                "calls": [],
                "puts": puts
            }
        }
    }
    
    return snapshot


def test_no_trade_shallow_strikes_no_raise(tmp_path):
    """
    Test that NO_TRADE with shallow strikes returns cleanly without raising.
    
    Creates a snapshot with strikes only near ATM, which will fail to satisfy
    deep OTM targets like -8% to -15%. Should return NO_TRADE, not raise.
    """
    # Create minimal config
    config = {
        "campaign_name": "crash_venture_v1_test",
        "structuring": {
            "underlier": "SPY",
            "dte_range_days": {"min": 20, "max": 70},
            "moneyness_targets": [-0.08, -0.10, -0.12, -0.15],  # Deep OTM
            "spread_widths": [10, 20],
            "constraints": {
                "max_loss_usd_per_trade": 500,
                "max_candidates_evaluated": 30,
                "top_n_output": 3
            },
            "monte_carlo": {"paths": 10000, "seed_mode": "run_id"},
            "objective": "max_ev_per_dollar",
            "output": {
                "structures_json": True,
                "summary_md": True,
                "dry_run_tickets": True
            }
        }
    }
    
    config_path = tmp_path / "test_config.yaml"
    with open(config_path, "w") as f:
        import yaml
        yaml.dump(config, f)
    
    # Create shallow snapshot (will cause NO_TRADE)
    snapshot = create_minimal_snapshot_shallow_strikes(spot=690.0, shallow=True)
    snapshot_path = tmp_path / "test_snapshot.json"
    with open(snapshot_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    
    # Run engine - should NOT raise, should return NO_TRADE
    result = run_crash_venture_v1_snapshot(
        config_path=str(config_path),
        snapshot_path=str(snapshot_path),
        p_event=0.30,
        min_debit_per_contract=10.0
    )
    
    # Assertions: NO_TRADE result structure
    assert result["ok"] is True
    assert result["decision"] == "NO_TRADE"
    assert result["reason"] in ["NO_CANDIDATES_SURVIVED_FILTERS", "INSUFFICIENT_STRIKE_COVERAGE"]
    assert len(result["warnings"]) > 0
    assert len(result["candidates"]) == 0
    assert len(result["top_structures"]) == 0
    assert "run_id" in result
    assert "run_dir" in result
    
    # Check artifacts were written
    run_dir = Path(result["run_dir"])
    assert run_dir.exists()
    
    artifacts_dir = run_dir / "artifacts"
    assert artifacts_dir.exists()
    
    # Check final_decision.json
    decision_file = artifacts_dir / "final_decision.json"
    assert decision_file.exists()
    with open(decision_file) as f:
        decision = json.load(f)
    assert decision["decision"] == "NO_TRADE"
    assert "reason" in decision
    
    # Check tickets.json (should be empty)
    tickets_file = artifacts_dir / "tickets.json"
    assert tickets_file.exists()
    with open(tickets_file) as f:
        tickets = json.load(f)
    assert len(tickets) == 0
    
    # Check review.txt exists
    review_file = artifacts_dir / "review.txt"
    assert review_file.exists()
    with open(review_file) as f:
        review_text = f.read()
    assert "NO TRADE" in review_text
    
    # Check manifest.json
    manifest_file = run_dir / "manifest.json"
    assert manifest_file.exists()
    with open(manifest_file) as f:
        manifest = json.load(f)
    assert manifest["decision"] == "NO_TRADE"
    assert manifest["n_candidates_generated"] == 0


def test_no_trade_high_min_debit_filter(tmp_path):
    """
    Test NO_TRADE when min_debit_per_contract filter is too high.
    
    Creates a scenario where candidates exist but all are filtered
    by min debit. Should return NO_TRADE, not raise.
    """
    # Create config
    config = {
        "campaign_name": "crash_venture_v1_test",
        "structuring": {
            "underlier": "SPY",
            "dte_range_days": {"min": 20, "max": 70},
            "moneyness_targets": [-0.02, -0.03],  # Close to ATM (cheap spreads)
            "spread_widths": [5, 10],  # Small widths
            "constraints": {
                "max_loss_usd_per_trade": 500,
                "max_candidates_evaluated": 30,
                "top_n_output": 3
            },
            "monte_carlo": {"paths": 10000, "seed_mode": "run_id"},
            "objective": "max_ev_per_dollar",
            "output": {
                "structures_json": True,
                "summary_md": True,
                "dry_run_tickets": True
            }
        }
    }
    
    config_path = tmp_path / "test_config.yaml"
    with open(config_path, "w") as f:
        import yaml
        yaml.dump(config, f)
    
    # Create snapshot with decent strike coverage but low prices
    snapshot = create_minimal_snapshot_shallow_strikes(spot=690.0, shallow=False)
    snapshot_path = tmp_path / "test_snapshot.json"
    with open(snapshot_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    
    # Run with VERY HIGH min_debit_per_contract (will filter everything)
    result = run_crash_venture_v1_snapshot(
        config_path=str(config_path),
        snapshot_path=str(snapshot_path),
        p_event=0.30,
        min_debit_per_contract=5000.0  # Impossibly high
    )
    
    # Should return NO_TRADE
    assert result["ok"] is True
    assert result["decision"] == "NO_TRADE"
    assert result["reason"] == "NO_CANDIDATES_SURVIVED_FILTERS"
    assert len(result["top_structures"]) == 0
    
    # Check that filter diagnostics were written
    run_dir = Path(result["run_dir"])
    filter_diag_file = run_dir / "filter_diagnostics.json"
    assert filter_diag_file.exists()


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
