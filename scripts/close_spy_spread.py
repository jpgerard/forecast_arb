"""
CLI: Close SPY 20260320 590/570 Bear Put Spread via IBKR BAG Order
==================================================================
Thin wrapper around forecast_arb.ibkr.close_spread.close_bear_put_spread().

Paper-trade example (connects to TWS paper port 7497):
    python scripts/close_spy_spread.py --mode paper

Live-trade example (connects to TWS live port 7496):
    python scripts/close_spy_spread.py --mode live

Stage only (sends to TWS order book but does NOT transmit to exchange):
    python scripts/close_spy_spread.py --mode paper --stage
    python scripts/close_spy_spread.py --mode live --stage

Custom fill timeout (seconds per price level before cancel+retry):
    python scripts/close_spy_spread.py --mode paper --fill-timeout 120

Custom pricing (override defaults [0.16, 0.15, 0.14]):
    python scripts/close_spy_spread.py --mode paper --ladder 0.15 0.14

Output:
    Prints structured JSON result to stdout.
    All IBKR API events also appear on stderr via Python logging.

Exit codes:
    0  — FILLED or STAGED
    1  — WIDE_MARKET_NO_CLOSE, POSITION_NOT_FOUND, LADDER_EXHAUSTED, ERROR
"""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/close_spy_spread.py` from the repo root
# without a `pip install -e .` editable install (matches all other scripts).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def _result_to_json(result) -> str:
    """Serialise SpreadCloseResult to a JSON string."""
    try:
        d = asdict(result)
    except Exception:
        # fallback: manual dict
        d = {
            "status": result.status,
            "mode": result.mode,
            "fill_price": result.fill_price,
            "fill_qty": result.fill_qty,
            "order_id": result.order_id,
            "perm_id": result.perm_id,
            "error": result.error,
            "log": result.log,
        }
    return json.dumps(d, indent=2, default=str)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Close SPY 20260320 590/570 bear put spread via IBKR BAG order",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="paper (port 7497) or live (port 7496)  [default: paper]",
    )

    # Connection
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="IBKR TWS/Gateway host  [default: 127.0.0.1]",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override port (default: auto from --mode: paper=7497, live=7496)",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=20,
        dest="client_id",
        help="IBKR client ID  [default: 20]",
    )

    # Order behaviour
    parser.add_argument(
        "--stage",
        action="store_true",
        default=False,
        help="Stage order in TWS but do NOT transmit to exchange  [default: transmit immediately]",
    )
    parser.add_argument(
        "--ladder",
        nargs="+",
        type=float,
        default=None,
        metavar="CREDIT",
        help="Override pricing ladder, e.g. --ladder 0.15 0.14  [default: 0.16 0.15 0.14]",
    )
    parser.add_argument(
        "--min-credit",
        type=float,
        default=0.14,
        dest="min_credit",
        help="Never submit below this credit  [default: 0.14]",
    )
    parser.add_argument(
        "--max-width-pct",
        type=float,
        default=0.25,
        dest="max_width_pct",
        help="Liquidity guard: block if combo width > this fraction of mid  [default: 0.25]",
    )
    parser.add_argument(
        "--fill-timeout",
        type=int,
        default=60,
        dest="fill_timeout_sec",
        help="Seconds to wait per price level before cancel + retry  [default: 60]",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        dest="poll_interval_sec",
        help="Fill status poll interval in seconds  [default: 5]",
    )

    # Spread override (leave as defaults for the target spread)
    parser.add_argument("--symbol", default="SPY", help="[default: SPY]")
    parser.add_argument("--expiry", default="20260320", help="YYYYMMDD  [default: 20260320]")
    parser.add_argument("--right", default="P", help="[default: P]")
    parser.add_argument("--long-strike", type=float, default=590.0, dest="long_strike",
                        help="[default: 590]")
    parser.add_argument("--short-strike", type=float, default=570.0, dest="short_strike",
                        help="[default: 570]")
    parser.add_argument("--qty", type=int, default=1, help="[default: 1]")

    # Misc
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    log = logging.getLogger("close_spy_spread")

    # Safety check for live mode
    if args.mode == "live":
        log.warning("=" * 60)
        log.warning("LIVE MODE — orders will be sent to the real exchange!")
        log.warning("=" * 60)

    log.info(
        "Starting close_bear_put_spread: mode=%s  symbol=%s  expiry=%s  "
        "%s/%s%s  qty=%d  transmit=%s",
        args.mode, args.symbol, args.expiry,
        args.long_strike, args.short_strike, args.right,
        args.qty, not args.stage,
    )

    # Import here so startup errors appear after logging is configured
    try:
        from forecast_arb.ibkr.close_spread import close_bear_put_spread
    except ImportError as exc:
        log.error("Import failed: %s", exc)
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2))
        return 1

    result = close_bear_put_spread(
        mode=args.mode,
        symbol=args.symbol,
        expiry=args.expiry,
        right=args.right,
        long_strike=args.long_strike,
        short_strike=args.short_strike,
        qty=args.qty,
        price_ladder=args.ladder,
        min_credit=args.min_credit,
        max_width_pct=args.max_width_pct,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        transmit=not args.stage,
        fill_timeout_sec=args.fill_timeout_sec,
        poll_interval_sec=args.poll_interval_sec,
    )

    # Print result JSON to stdout
    print(_result_to_json(result))

    # Summary line to stderr
    if result.status == "FILLED":
        log.info(
            "✅ FILLED  qty=%d  avg_fill=$%.4f  orderId=%s",
            result.fill_qty, result.fill_price or 0, result.order_id,
        )
        return 0
    elif result.status == "STAGED":
        log.info("📋 STAGED  orderId=%s  (transmit=False)", result.order_id)
        return 0
    elif result.status == "WIDE_MARKET_NO_CLOSE":
        log.warning("🚫 WIDE_MARKET_NO_CLOSE — combo spread too wide, order not submitted")
        return 1
    elif result.status == "POSITION_NOT_FOUND":
        log.error("❌ POSITION_NOT_FOUND — target legs not in IBKR account, no order placed")
        return 1
    elif result.status == "LADDER_EXHAUSTED":
        log.warning("⚠️  LADDER_EXHAUSTED — tried all prices, no fill obtained")
        return 1
    else:
        log.error("❌ %s — %s", result.status, result.error or "see log")
        return 1


if __name__ == "__main__":
    sys.exit(main())
