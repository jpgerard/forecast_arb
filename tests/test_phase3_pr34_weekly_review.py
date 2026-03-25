"""
Tests for Phase 3 PR3.4 - Weekly PM Review Generator

Tests weekly review generation from ledgers.
"""

import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Import the generator functions directly
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.weekly_pm_review import generate_weekly_review
from forecast_arb.core.ledger import write_regime_ledger_entry, create_regime_ledger_entry
from forecast_arb.execution.outcome_ledger import append_trade_open, append_trade_close
from forecast_arb.core.dqs import create_dqs_entry, append_dqs_entry


def test_generate_weekly_review_empty_ledgers():
    """Test generating review with empty ledgers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        regime_ledger = Path(tmpdir) / "regime_ledger.jsonl"
        trade_outcomes = Path(tmpdir) / "trade_outcomes.jsonl"
        dqs_ledger = Path(tmpdir) / "dqs.jsonl"
        
        # Don't create files - test with non-existent ledgers
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)
        
        review_md = generate_weekly_review(
            regime_ledger_path=regime_ledger,
            trade_outcomes_path=trade_outcomes,
            dqs_ledger_path=dqs_ledger,
            since=since,
            until=now
        )
        
        # Verify markdown was generated
        assert "# Weekly PM Review" in review_md
        assert "No decisions recorded" in review_md
        assert "No trades opened" in review_md
        assert "No DQS scores recorded" in review_md


def test_generate_weekly_review_with_data():
    """Test generating review with actual ledger data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            
            # Create run directory
            run_dir = Path(tmpdir) / "runs" / "test_run"
            run_dir.mkdir(parents=True, exist_ok=True)
            
            # Prepare timestamps
            now = datetime.now(timezone.utc)
            since = now - timedelta(days=7)
            
            # Write regime ledger entries
            for i in range(3):
                entry = create_regime_ledger_entry(
                    run_id=f"run_{i}",
                    regime="crash" if i % 2 == 0 else "selloff",
                    mode="CRASH_ONLY",
                    decision="TRADE" if i < 2 else "NO_TRADE",
                    reasons=["TEST"],
                    event_hash=f"evt_{i}",
                    expiry="20260320",
                    moneyness=-0.15,
                    spot=684.98,
                    threshold=582.23,
                    p_implied=0.0651,
                    p_external=0.0800,
                    representable=True,
                    ts_utc=(now - timedelta(days=6-i)).isoformat()
                )
                write_regime_ledger_entry(run_dir, entry, also_global=True)
            
            # Write trade outcomes
            append_trade_open(
                run_dir=run_dir,
                candidate_id="cand_1",
                run_id="run_0",
                regime="crash",
                entry_ts_utc=(now - timedelta(days=5)).isoformat(),
                entry_price=0.40,
                qty=1,
                expiry="20260320",
                long_strike=580.0,
                short_strike=560.0,
                also_global=True
            )
            
            append_trade_close(
                run_dir=run_dir,
                candidate_id="cand_1",
                exit_ts_utc=(now - timedelta(days=3)).isoformat(),
                exit_price=1.10,
                exit_reason="TAKE_PROFIT",
                pnl=70.0,
                also_global=True
            )
            
            # Write DQS entry
            dqs_entry = create_dqs_entry(
                candidate_id="cand_1",
                run_id="run_0",
                regime="crash",
                dqs_total=8,
                breakdown={
                    "regime": 2,
                    "pricing": 2,
                    "structure": 2,
                    "execution": 1,
                    "governance": 1
                },
                notes="Good trade",
                ts_utc=(now - timedelta(days=2)).isoformat()
            )
            append_dqs_entry(run_dir, dqs_entry, also_global=True)
            
            # Generate review
            regime_ledger = Path(tmpdir) / "runs" / "regime_ledger.jsonl"
            trade_outcomes = Path(tmpdir) / "runs" / "trade_outcomes.jsonl"
            dqs_ledger = Path(tmpdir) / "runs" / "dqs.jsonl"
            
            review_md = generate_weekly_review(
                regime_ledger_path=regime_ledger,
                trade_outcomes_path=trade_outcomes,
                dqs_ledger_path=dqs_ledger,
                since=since,
                until=now
            )
            
            # Verify content
            assert "# Weekly PM Review" in review_md
            assert "Decision Summary" in review_md
            assert "Trade Activity" in review_md
            assert "Decision Quality Summary" in review_md
            assert "Notable" in review_md
            assert "System Health" in review_md
            
            # Verify specific data
            assert "crash" in review_md.lower()
            assert "TRADE" in review_md
            assert "NO_TRADE" in review_md
            assert "$70.00" in review_md  # P&L
            assert "8/10" in review_md  # DQS score
            assert "Good trade" in review_md  # DQS notes
            
        finally:
            os.chdir(old_cwd)


def test_generate_weekly_review_file_headers():
    """Test that generated review has required headers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        regime_ledger = Path(tmpdir) / "regime_ledger.jsonl"
        trade_outcomes = Path(tmpdir) / "trade_outcomes.jsonl"
        dqs_ledger = Path(tmpdir) / "dqs.jsonl"
        
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)
        
        review_md = generate_weekly_review(
            regime_ledger_path=regime_ledger,
            trade_outcomes_path=trade_outcomes,
            dqs_ledger_path=dqs_ledger,
            since=since,
            until=now
        )
        
        # Check for all required sections
        required_sections = [
            "# Weekly PM Review",
            "## Decision Summary",
            "## Trade Activity",
            "## Decision Quality Summary",
            "## Notable",
            "## System Health"
        ]
        
        for section in required_sections:
            assert section in review_md, f"Missing section: {section}"


def test_generate_weekly_review_date_filtering():
    """Test that review correctly filters by date range."""
    with tempfile.TemporaryDirectory() as tmpdir:
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmpdir)
            
            run_dir = Path(tmpdir) / "runs" / "test_run"
            run_dir.mkdir(parents=True, exist_ok=True)
            
            now = datetime.now(timezone.utc)
            
            # Write entry OUTSIDE date range (10 days ago)
            old_entry = create_regime_ledger_entry(
                run_id="run_old",
                regime="crash",
                mode="CRASH_ONLY",
                decision="TRADE",
                reasons=["OLD"],
                event_hash="evt_old",
                expiry="20260320",
                moneyness=-0.15,
                spot=684.98,
                threshold=582.23,
                p_implied=0.0651,
                p_external=0.0800,
                representable=True,
                ts_utc=(now - timedelta(days=10)).isoformat()
            )
            write_regime_ledger_entry(run_dir, old_entry, also_global=True)
            
            # Write entry INSIDE date range (5 days ago)
            recent_entry = create_regime_ledger_entry(
                run_id="run_recent",
                regime="crash",
                mode="CRASH_ONLY",
                decision="TRADE",
                reasons=["RECENT"],
                event_hash="evt_recent",
                expiry="20260320",
                moneyness=-0.15,
                spot=684.98,
                threshold=582.23,
                p_implied=0.0651,
                p_external=0.0800,
                representable=True,
                ts_utc=(now - timedelta(days=5)).isoformat()
            )
            write_regime_ledger_entry(run_dir, recent_entry, also_global=True)
            
            # Generate review for last 7 days
            since = now - timedelta(days=7)
            
            regime_ledger = Path(tmpdir) / "runs" / "regime_ledger.jsonl"
            trade_outcomes = Path(tmpdir) / "runs" / "trade_outcomes.jsonl"
            dqs_ledger = Path(tmpdir) / "runs" / "dqs.jsonl"
            
            review_md = generate_weekly_review(
                regime_ledger_path=regime_ledger,
                trade_outcomes_path=trade_outcomes,
                dqs_ledger_path=dqs_ledger,
                since=since,
                until=now
            )
            
            # Should include recent entry
            assert "Total Decisions:** 1" in review_md
            
        finally:
            os.chdir(old_cwd)
