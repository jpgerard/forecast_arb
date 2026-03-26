"""
Weekly PM Review Generator

Generates a weekly portfolio management review from append-only ledgers.
Reads decision ledgers, trade outcomes, and DQS scores to create a
comprehensive markdown memo.

Usage:
    python scripts/weekly_pm_review.py
    python scripts/weekly_pm_review.py --since 2026-02-01 --until 2026-02-07
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.core.ledger import append_jsonl
from forecast_arb.execution.outcome_ledger import read_trade_outcomes, read_trade_events
from forecast_arb.core.dqs import read_dqs_entries, compute_dqs_summary


def parse_date(date_str: str) -> datetime:
    """Parse date string to datetime."""
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def read_regime_ledger(ledger_path: Path, since: datetime, until: datetime) -> List[Dict[str, Any]]:
    """Read regime ledger entries within date range."""
    if not ledger_path.exists():
        return []
    
    entries = []
    
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            
            import json
            entry = json.loads(line)
            
            # Parse timestamp
            ts = datetime.fromisoformat(entry["ts_utc"].replace('Z', '+00:00'))
            
            # Filter by date range
            if since <= ts <= until:
                entries.append(entry)
    
    return entries


def generate_weekly_review(
    regime_ledger_path: Path,
    trade_outcomes_path: Path,
    dqs_ledger_path: Path,
    since: datetime,
    until: datetime,
) -> str:
    """
    Generate weekly PM review markdown.
    
    Args:
        regime_ledger_path: Path to regime_ledger.jsonl
        trade_outcomes_path: Path to trade_outcomes.jsonl
        dqs_ledger_path: Path to dqs.jsonl
        since: Start date
        until: End date
        
    Returns:
        Markdown string
    """
    # Read ledgers
    regime_entries = read_regime_ledger(regime_ledger_path, since, until)
    trade_outcomes = read_trade_outcomes(trade_outcomes_path)
    dqs_entries = read_dqs_entries(dqs_ledger_path)
    all_events = read_trade_events(trade_outcomes_path)
    
    # Filter trade outcomes by date range
    trades_in_range = []
    for candidate_id, trade in trade_outcomes.items():
        entry_ts = datetime.fromisoformat(trade["entry_ts_utc"].replace('Z', '+00:00'))
        if since <= entry_ts <= until:
            trades_in_range.append(trade)
    
    # Filter DQS entries by date range
    dqs_in_range = []
    for entry in dqs_entries:
        ts = datetime.fromisoformat(entry["ts_utc"].replace('Z', '+00:00'))
        if since <= ts <= until:
            dqs_in_range.append(entry)
    
    # Build markdown
    lines = []
    
    # Header
    lines.append(f"# Weekly PM Review")
    lines.append(f"")
    lines.append(f"**Period:** {since.strftime('%Y-%m-%d')} to {until.strftime('%Y-%m-%d')}")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")
    
    # Decision Summary
    lines.append(f"## Decision Summary")
    lines.append(f"")
    
    if not regime_entries:
        lines.append(f"*No decisions recorded in this period.*")
        lines.append(f"")
    else:
        # Count by regime and decision
        decision_counts = {}
        for entry in regime_entries:
            regime = entry["regime"]
            decision = entry["decision"]
            key = f"{regime}:{decision}"
            decision_counts[key] = decision_counts.get(key, 0) + 1
        
        lines.append(f"| Regime | Decision | Count |")
        lines.append(f"|--------|----------|-------|")
        
        for key in sorted(decision_counts.keys()):
            regime, decision = key.split(":")
            count = decision_counts[key]
            lines.append(f"| {regime} | {decision} | {count} |")
        
        lines.append(f"")
        lines.append(f"**Total Decisions:** {len(regime_entries)}")
        lines.append(f"")
    
    # Trade Activity
    lines.append(f"## Trade Activity")
    lines.append(f"")
    
    if not trades_in_range:
        lines.append(f"*No trades opened in this period.*")
        lines.append(f"")
    else:
        # Separate open vs closed
        open_trades = [t for t in trades_in_range if t["status"] == "OPEN"]
        closed_trades = [t for t in trades_in_range if t["status"] == "CLOSED"]
        
        lines.append(f"**Trades Opened:** {len(trades_in_range)}")
        lines.append(f"**Currently Open:** {len(open_trades)}")
        lines.append(f"**Closed in Period:** {len(closed_trades)}")
        lines.append(f"")
        
        # Closed trades detail
        if closed_trades:
            lines.append(f"### Closed Trades")
            lines.append(f"")
            lines.append(f"| Candidate ID | Regime | Entry | Exit | P&L | Exit Reason |")
            lines.append(f"|--------------|--------|-------|------|-----|-------------|")
            
            for trade in closed_trades:
                cid = trade["candidate_id"][:30]  # Truncate for display
                regime = trade["regime"]
                entry = f"${trade['entry_price']:.2f}"
                exit = f"${trade['exit_price']:.2f}"
                pnl = f"${trade['pnl']:.2f}"
                reason = trade["exit_reason"]
                lines.append(f"| {cid} | {regime} | {entry} | {exit} | {pnl} | {reason} |")
            
            lines.append(f"")
            
            # P&L summary
            total_pnl = sum(t["pnl"] for t in closed_trades)
            lines.append(f"**Total P&L (Closed Trades):** ${total_pnl:.2f}")
            lines.append(f"")
    
    # Quote Activity
    lines.append(f"## Quote Activity")
    lines.append(f"")

    # Filter event entries to the date range using timestamp_utc
    events_in_range = []
    for ev in all_events:
        ts_raw = ev.get("timestamp_utc") or ev.get("ts_utc")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if since <= ts <= until:
                events_in_range.append(ev)
        except (ValueError, TypeError):
            continue

    if not events_in_range:
        lines.append(f"*No quote events recorded in this period.*")
        lines.append(f"")
    else:
        _event_counts: Dict[str, int] = {}
        for ev in events_in_range:
            etype = ev.get("event", "UNKNOWN")
            _event_counts[etype] = _event_counts.get(etype, 0) + 1

        lines.append(f"| Event | Count |")
        lines.append(f"|-------|-------|")
        for etype in ("QUOTE_OK", "QUOTE_BLOCKED", "STAGED_PAPER", "SUBMITTED_LIVE", "FILLED_OPEN"):
            if etype in _event_counts:
                lines.append(f"| {etype} | {_event_counts[etype]} |")
        # Any unexpected event types
        for etype, cnt in sorted(_event_counts.items()):
            if etype not in ("QUOTE_OK", "QUOTE_BLOCKED", "STAGED_PAPER", "SUBMITTED_LIVE", "FILLED_OPEN"):
                lines.append(f"| {etype} | {cnt} |")
        lines.append(f"")
        lines.append(f"**Total Quote Events:** {len(events_in_range)}")
        lines.append(f"")

    # DQS Summary
    lines.append(f"## Decision Quality Summary")
    lines.append(f"")
    
    if not dqs_in_range:
        lines.append(f"*No DQS scores recorded in this period.*")
        lines.append(f"")
    else:
        dqs_summary = compute_dqs_summary(dqs_in_range)
        
        lines.append(f"**Scores Recorded:** {dqs_summary['count']}")
        lines.append(f"**Average DQS:** {dqs_summary['avg_total']:.1f}/10")
        lines.append(f"**Min DQS:** {dqs_summary['min_total']}/10")
        lines.append(f"**Max DQS:** {dqs_summary['max_total']}/10")
        lines.append(f"")
        
        # By regime
        if dqs_summary["by_regime"]:
            lines.append(f"### By Regime")
            lines.append(f"")
            lines.append(f"| Regime | Count | Avg | Min | Max |")
            lines.append(f"|--------|-------|-----|-----|-----|")
            
            for regime, stats in dqs_summary["by_regime"].items():
                lines.append(f"| {regime} | {stats['count']} | {stats['avg']:.1f} | {stats['min']} | {stats['max']} |")
            
            lines.append(f"")
    
    # Notable Section
    lines.append(f"## Notable")
    lines.append(f"")
    
    # Best DQS
    if dqs_in_range:
        best_dqs = max(dqs_in_range, key=lambda x: x["dqs_total"])
        lines.append(f"**Best DQS Trade:**")
        lines.append(f"- Candidate: {best_dqs['candidate_id']}")
        lines.append(f"- Score: {best_dqs['dqs_total']}/10")
        lines.append(f"- Notes: {best_dqs.get('notes', 'N/A')}")
        lines.append(f"")
        
        # Worst DQS
        worst_dqs = min(dqs_in_range, key=lambda x: x["dqs_total"])
        lines.append(f"**Worst DQS Trade:**")
        lines.append(f"- Candidate: {worst_dqs['candidate_id']}")
        lines.append(f"- Score: {worst_dqs['dqs_total']}/10")
        lines.append(f"- Notes: {worst_dqs.get('notes', 'N/A')}")
        lines.append(f"")
    
    # Biggest P&L
    if trades_in_range:
        closed_with_pnl = [t for t in trades_in_range if t["status"] == "CLOSED" and t["pnl"] is not None]
        if closed_with_pnl:
            best_trade = max(closed_with_pnl, key=lambda x: x["pnl"])
            worst_trade = min(closed_with_pnl, key=lambda x: x["pnl"])
            
            lines.append(f"**Biggest Gain:**")
            lines.append(f"- Candidate: {best_trade['candidate_id']}")
            lines.append(f"- P&L: ${best_trade['pnl']:.2f}")
            lines.append(f"- Exit Reason: {best_trade['exit_reason']}")
            lines.append(f"")
            
            lines.append(f"**Biggest Loss:**")
            lines.append(f"- Candidate: {worst_trade['candidate_id']}")
            lines.append(f"- P&L: ${worst_trade['pnl']:.2f}")
            lines.append(f"- Exit Reason: {worst_trade['exit_reason']}")
            lines.append(f"")
    
    # System Health
    lines.append(f"## System Health")
    lines.append(f"")
    
    # Representability failures
    if regime_entries:
        not_representable = [e for e in regime_entries if not e.get("representable", True)]
        lines.append(f"**Representability Failures:** {len(not_representable)}")
        
        # STAND_DOWN counts
        stand_down = [e for e in regime_entries if e["decision"] == "STAND_DOWN"]
        lines.append(f"**STAND_DOWN Decisions:** {len(stand_down)}")
        
        # Missing input stand downs
        missing_input_stand_downs = [
            e for e in stand_down 
            if any("MISSING" in r or "UNAVAILABLE" in r for r in e.get("reasons", []))
        ]
        lines.append(f"**STAND_DOWN (Missing Inputs):** {len(missing_input_stand_downs)}")
        lines.append(f"")
    
    return "\n".join(lines)


def main():
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Generate weekly PM review from ledgers"
    )
    
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Start date (YYYY-MM-DD, default: 7 days ago)"
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="End date (YYYY-MM-DD, default: today)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="runs/weekly_reviews",
        help="Output directory for review markdown (default: runs/weekly_reviews)"
    )
    
    args = parser.parse_args()
    
    # Determine date range
    if args.until:
        until = parse_date(args.until)
    else:
        until = datetime.now(timezone.utc)
    
    if args.since:
        since = parse_date(args.since)
    else:
        since = until - timedelta(days=7)
    
    print(f"Generating weekly review for {since.strftime('%Y-%m-%d')} to {until.strftime('%Y-%m-%d')}")
    print()
    
    # Ledger paths
    regime_ledger = Path("runs") / "regime_ledger.jsonl"
    trade_outcomes = Path("runs") / "trade_outcomes.jsonl"
    dqs_ledger = Path("runs") / "dqs.jsonl"
    
    # Check if ledgers exist
    if not regime_ledger.exists():
        print(f"WARNING: Regime ledger not found: {regime_ledger}")
    if not trade_outcomes.exists():
        print(f"WARNING: Trade outcomes ledger not found: {trade_outcomes}")
    if not dqs_ledger.exists():
        print(f"WARNING: DQS ledger not found: {dqs_ledger}")
    print()
    
    # Generate review
    try:
        review_md = generate_weekly_review(
            regime_ledger_path=regime_ledger,
            trade_outcomes_path=trade_outcomes,
            dqs_ledger_path=dqs_ledger,
            since=since,
            until=until
        )
        
        # Write output
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f"weekly_pm_review_{since.strftime('%Y%m%d')}_{until.strftime('%Y%m%d')}.md"
        output_path = output_dir / filename
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(review_md)
        
        print(f"✓ Weekly review written: {output_path}")
        print()
        print("Review Summary:")
        print("=" * 80)
        print(review_md[:500])  # Preview first 500 chars
        if len(review_md) > 500:
            print("...")
        print("=" * 80)
        
    except Exception as e:
        print(f"ERROR: Failed to generate review: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
