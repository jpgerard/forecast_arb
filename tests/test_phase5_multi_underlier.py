"""
Phase 5: SPY+QQQ Multi-Underlier Integration Test

Validates:
1. Both SPY and QQQ produce candidates
2. No cross-contamination (spot/strike ranges differ)
3. Snapshots are isolated per underlier
4. Kalshi probabilities fetched independently
5. Per-cell accounting logs are generated
6. Mapping diagnostics persisted when Kalshi fails
"""

import json
import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

# Add parent to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from forecast_arb.campaign.grid_runner import run_campaign_grid


logger = logging.getLogger(__name__)


@pytest.fixture
def mock_campaign_config():
    """Campaign config with SPY and QQQ underliers."""
    return {
        "underliers": ["SPY", "QQQ"],
        "regimes": [
            {"name": "crash", "threshold": -0.10}
        ],
        "expiry_buckets": [
            {"name": "near", "dte_min": 30, "dte_max": 45}
        ],
        "cluster_map": {
            "SPY": "EQUITY_LARGE_CAP",
            "QQQ": "EQUITY_TECH"
        },
        "governors": {
            "daily_premium_cap_usd": 1250.0,
            "cluster_cap_per_day": 1,
            "max_open_positions_by_regime": {"crash": 3},
            "premium_at_risk_caps_usd": {"crash": 2500.0, "total": 5000.0}
        },
        "selection": {
            "max_trades_per_day": 2,
            "scoring": "ev_per_dollar"
        }
    }


@pytest.fixture
def mock_structuring_config():
    """Minimal structuring config."""
    return {
        "edge_gating": {
            "event_moneyness": -0.10
        },
        "structuring": {
            "dte_range": [30, 60],
            "moneyness_targets": [-0.10],
            "spread_widths": [15, 20],
            "monte_carlo": {"paths": 10000},
            "constraints": {
                "max_candidates_evaluated": 50,
                "max_loss_usd_per_trade": 10000,
                "top_n_output": 5
            },
            "objective": "max_ev_per_dollar"
        }
    }


def create_mock_snapshot(underlier: str, spot: float):
    """Create mock snapshot with underlier-specific parameters."""
    # Set expiry to be ~35 days from snapshot (within near bucket 30-45 DTE)
    base_expiry = "20260403"  # April 3, 2026 (35 days from Feb 27)
    
    # Create strikes relative to spot (10% range)
    strikes = [spot * (1 + i * 0.01) for i in range(-10, 11)]
    
    puts = []
    for strike in strikes:
        puts.append({
            "strike": strike,
            "right": "P",
            "bid": max(0.5, strike * 0.01),
            "ask": max(0.6, strike * 0.012),
            "last": max(0.55, strike * 0.011),
            "implied_vol": 0.20 + (strike / spot - 1) * 0.5  # Vol skew
        })
    
    return {
        "metadata": {
            "underlier": underlier,
            "current_price": spot,
            "snapshot_time": "2026-02-27T10:00:00Z"
        },
        base_expiry: puts
    }


def test_spy_qqq_multi_underlier_isolation(mock_campaign_config, mock_structuring_config, tmp_path, capfd):
    """
    Integration test: Verify SPY and QQQ produce isolated candidates.
    
    Validates:
    - Both underliers generate candidates
    - Spot prices differ (SPY ~600, QQQ ~500)
    - Strike ranges don't overlap
    - Per-cell accounting logs printed
    - Kalshi mapping diagnostics present
    """
    # Create mock snapshots with different spot prices
    spy_spot = 600.0
    qqq_spot = 500.0
    
    mock_spy_snapshot = create_mock_snapshot("SPY", spy_spot)
    mock_qqq_snapshot = create_mock_snapshot("QQQ", qqq_spot)
    
    # Write temporary configs
    campaign_config_path = tmp_path / "campaign.yaml"
    structuring_config_path = tmp_path / "structuring.yaml"
    
    import yaml
    with open(campaign_config_path, "w") as f:
        yaml.dump(mock_campaign_config, f)
    
    with open(structuring_config_path, "w") as f:
        yaml.dump(mock_structuring_config, f)
    
    # Mock IBKR snapshot export and regime runner
    with patch("forecast_arb.ibkr.snapshot.IBKRSnapshotExporter") as mock_exporter_class, \
         patch("forecast_arb.campaign.grid_runner.load_snapshot") as mock_load, \
         patch("forecast_arb.campaign.grid_runner.get_snapshot_metadata") as mock_metadata, \
         patch("scripts.run_daily_v2.run_regime") as mock_run_regime:
        
        # Setup mock exporter
        mock_exporter = MagicMock()
        mock_exporter_class.return_value = mock_exporter
        
        # Mock load_snapshot to return correct snapshot based on filename
        def load_snapshot_side_effect(path):
            if "SPY" in str(path):
                return mock_spy_snapshot
            elif "QQQ" in str(path):
                return mock_qqq_snapshot
            else:
                raise ValueError(f"Unexpected snapshot path: {path}")
        
        mock_load.side_effect = load_snapshot_side_effect
        
        # Mock get_snapshot_metadata
        def get_metadata_side_effect(snapshot):
            return snapshot["metadata"]
        
        mock_metadata.side_effect = get_metadata_side_effect
        
        # Mock run_regime to return regime result with candidates
        def run_regime_side_effect(regime, config, snapshot, snapshot_path, p_event_external, min_debit_per_contract, run_id):
            # Extract underlier from snapshot
            underlier = snapshot["metadata"]["underlier"]
            spot = snapshot["metadata"]["current_price"]
            
            # Create mock candidates with underlier-specific strikes
            candidates = []
            for i in range(3):
                long_strike = spot * 0.90  # 10% OTM
                short_strike = spot * 0.95  # 5% OTM
                
                candidate = {
                    "candidate_id": f"{underlier}_{regime}_{i}",
                    "expiry": "20260403",  # Match the snapshot expiry
                    "strikes": {
                        "long_put": long_strike,
                        "short_put": short_strike
                    },
                    "debit_per_contract": 10.0 + i,
                    "max_gain_per_contract": 50.0 + i * 5,
                    "ev_per_dollar": 0.20 + i * 0.05,
                    "prob_profit": 0.25,
                    "assumed_p_event": 0.25,
                    "p_implied": 0.22,
                    "representable": True,
                    "rank": i + 1
                }
                
                candidates.append(candidate)
            
            # Create mock regime result
            from unittest.mock import MagicMock
            regime_result = MagicMock()
            regime_result.candidates = candidates
            regime_result.p_implied = 0.22
            regime_result.p_event_external = {
                "p": None,  # Simulate NO_MARKET
                "source": "kalshi",
                "authoritative": False,
                "asof_ts_utc": "2026-02-27T10:00:00Z",
                "market": None,
                "match": None,
                "quality": {
                    "liquidity_ok": False,
                    "staleness_ok": False,
                    "spread_ok": False,
                    "warnings": ["NO_MARKET_FOUND"]
                }
            }
            
            return regime_result
        
        mock_run_regime.side_effect = run_regime_side_effect
        
        # Run campaign grid
        candidates_flat_path = run_campaign_grid(
            campaign_config_path=str(campaign_config_path),
            structuring_config_path=str(structuring_config_path),
            p_external_by_underlier=None,
            min_debit_per_contract=10.0,
            snapshot_dir=str(tmp_path / "snapshots"),
            dte_min=30,
            dte_max=60
        )
        
        # Capture printed output
        captured = capfd.readouterr()
        
        # Validate: Candidates file exists
        assert Path(candidates_flat_path).exists(), "Candidates file should exist"
        
        # Load candidates
        with open(candidates_flat_path, "r") as f:
            candidates_flat = json.load(f)
        
        # VALIDATION 1: Both underliers produce candidates
        spy_candidates = [c for c in candidates_flat if c["underlier"] == "SPY"]
        qqq_candidates = [c for c in candidates_flat if c["underlier"] == "QQQ"]
        
        assert len(spy_candidates) > 0, "SPY should produce candidates"
        assert len(qqq_candidates) > 0, "QQQ should produce candidates"
        
        logger.info(f"✓ SPY candidates: {len(spy_candidates)}")
        logger.info(f"✓ QQQ candidates: {len(qqq_candidates)}")
        
        # VALIDATION 2: Strike ranges don't overlap (no cross-contamination)
        spy_strikes = []
        for c in spy_candidates:
            spy_strikes.extend([c["long_strike"], c["short_strike"]])
        
        qqq_strikes = []
        for c in qqq_candidates:
            qqq_strikes.extend([c["long_strike"], c["short_strike"]])
        
        spy_min, spy_max = min(spy_strikes), max(spy_strikes)
        qqq_min, qqq_max = min(qqq_strikes), max(qqq_strikes)
        
        # Strikes should not overlap if spots differ significantly
        assert spy_min > qqq_max or qqq_min > spy_max, \
            f"Strike ranges overlap: SPY ({spy_min:.0f}-{spy_max:.0f}), QQQ ({qqq_min:.0f}-{qqq_max:.0f})"
        
        logger.info(f"✓ SPY strike range: {spy_min:.0f} - {spy_max:.0f}")
        logger.info(f"✓ QQQ strike range: {qqq_min:.0f} - {qqq_max:.0f}")
        
        # VALIDATION 3: Per-cell accounting logs printed
        assert "[CELL_ACCOUNTING]" in captured.out, "Per-cell accounting logs should be printed"
        
        # Count accounting log lines
        accounting_lines = [line for line in captured.out.split("\n") if "[CELL_ACCOUNTING]" in line]
        assert len(accounting_lines) >= 2, f"Should have accounting logs for both underliers, got {len(accounting_lines)}"
        
        # Verify SPY and QQQ both logged
        spy_logged = any("underlier=SPY" in line for line in accounting_lines)
        qqq_logged = any("underlier=QQQ" in line for line in accounting_lines)
        
        assert spy_logged, "SPY should have accounting log"
        assert qqq_logged, "QQQ should have accounting log"
        
        logger.info(f"✓ Per-cell accounting logs: {len(accounting_lines)} lines")
        
        # VALIDATION 4: Verify probability breakdown in accounting logs
        for line in accounting_lines:
            assert "p_used_breakdown=" in line, "Accounting log should include p_used_breakdown"
            assert "p_ext_status_breakdown=" in line, "Accounting log should include p_ext_status_breakdown"
        
        logger.info("✓ Probability breakdowns present in accounting logs")
        
        # VALIDATION 5: Kalshi mapping diagnostics present for NO_MARKET status
        for candidate in candidates_flat:
            p_ext_status = candidate.get("p_ext_status")
            
            if p_ext_status and p_ext_status != "OK":
                # Should have kalshi_mapping_debug
                assert "kalshi_mapping_debug" in candidate, \
                    f"Candidate {candidate['candidate_id']} with p_ext_status={p_ext_status} should have kalshi_mapping_debug"
                
                debug = candidate["kalshi_mapping_debug"]
                assert "max_mapping_error" in debug, "Debug should include max_mapping_error"
                assert debug["max_mapping_error"] == 0.10, "max_mapping_error should be 0.10"
                assert "status" in debug, "Debug should include status"
                assert "reason" in debug, "Debug should include reason"
        
        logger.info("✓ Kalshi mapping diagnostics present for non-OK statuses")
        
        # VALIDATION 6: Canonical fields present
        for candidate in candidates_flat:
            # Check required canonical fields
            assert "ev_per_dollar" in candidate, "Canonical ev_per_dollar required"
            assert "p_used" in candidate, "Canonical p_used required"
            assert "p_used_src" in candidate, "Canonical p_used_src required"
            assert "p_ext_status" in candidate, "Canonical p_ext_status required"
            assert "p_ext_reason" in candidate, "Canonical p_ext_reason required"
            
            # Validate no None values for required fields
            assert candidate["ev_per_dollar"] is not None, "ev_per_dollar must not be None"
            assert candidate["p_used"] is not None, "p_used must not be None"
            assert candidate["p_used_src"] is not None, "p_used_src must not be None"
            assert candidate["p_ext_status"] is not None, "p_ext_status must not be None"
            assert candidate["p_ext_reason"] is not None, "p_ext_reason must not be None"
        
        logger.info("✓ All canonical fields present and non-null")
        
        logger.info("\n" + "=" * 80)
        logger.info("✅ PHASE 5 MULTI-UNDERLIER TEST PASSED")
        logger.info("=" * 80)
        logger.info(f"  SPY candidates: {len(spy_candidates)}")
        logger.info(f"  QQQ candidates: {len(qqq_candidates)}")
        logger.info(f"  Strike isolation: VERIFIED")
        logger.info(f"  Accounting logs: {len(accounting_lines)} cells")
        logger.info(f"  Kalshi diagnostics: PRESENT")
        logger.info("=" * 80)


if __name__ == "__main__":
    # Run test standalone
    import sys
    sys.exit(pytest.main([__file__, "-v", "-s"]))
