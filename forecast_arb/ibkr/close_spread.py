"""
IBKR Bear Put Spread Closer — Narrow Execution Utility
=======================================================
Closes one existing SPY bear put spread via a BAG (combo) order.
Does NOT open replacement trades.  Does NOT touch other positions.

Target spread (hard-coded defaults, overridable via kwargs):
    SPY  20260320  590/570  PUT  bear-put-spread  qty=1

Closing order semantics:
    SELL 1 BAG
      leg 1: SELL 1 SPY 20260320 590P  (close the long)
      leg 2: BUY  1 SPY 20260320 570P  (close the short)

Pricing ladder (DAY limit orders only — no market orders):
    $0.16 credit  → wait fill_timeout_sec; if not filled, cancel + retry
    $0.15 credit  → wait fill_timeout_sec; if not filled, cancel + retry
    $0.14 credit  → wait fill_timeout_sec; if not filled, stop
    < $0.14       → never submitted

Liquidity guard:
    Fetches live BAG bid/ask before any order.
    If  (combo_ask − combo_bid) / combo_mid  >  max_width_pct (default 25%)
    → abort with status="WIDE_MARKET_NO_CLOSE"

Position verification:
    Confirms +1 SPY 20260320 590P and −1 SPY 20260320 570P exist in the
    live IBKR account before any order is submitted.
    Aborts with status="POSITION_NOT_FOUND" if either leg is missing.

Modes:
    paper  →  port 7497  (TWS paper trading)
    live   →  port 7496  (TWS live trading)

transmit flag:
    transmit=True  (default) → order is sent to the exchange immediately
    transmit=False           → order is staged in TWS (visible, not sent)
                               status will be "STAGED" in the result

Entrypoint:
    from forecast_arb.ibkr.close_spread import close_bear_put_spread
    result = close_bear_put_spread(mode="paper")
    result = close_bear_put_spread(mode="live")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from ib_insync import IB, Option, Contract, ComboLeg, Order
    HAS_IB_INSYNC = True
except ImportError:
    HAS_IB_INSYNC = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants — target spread
# ---------------------------------------------------------------------------
_SYMBOL: str = "SPY"
_EXPIRY: str = "20260320"
_RIGHT: str = "P"
_LONG_STRIKE: float = 590.0       # the leg we are LONG (higher strike put)
_SHORT_STRIKE: float = 570.0      # the leg we are SHORT (lower strike put)
_QTY: int = 1

_PRICE_LADDER: List[float] = [0.16, 0.15, 0.14]
_MIN_CREDIT: float = 0.14
_MAX_WIDTH_PCT: float = 0.25      # 25 % of mid = liquidity guard threshold

_PORTS: Dict[str, int] = {"paper": 7497, "live": 7496}

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class LegQuote:
    strike: float
    right: str
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    source: str  # "live" | "unavailable"


@dataclass
class SpreadQuote:
    """BAG-level (or synthetic) quote for the spread being closed."""
    bid: Optional[float]        # combo bid  (credit buyer pays → min we'd receive on SELL)
    ask: Optional[float]        # combo ask  (credit seller demands → our natural ask)
    mid: Optional[float]
    width: Optional[float]      # ask - bid
    width_pct: Optional[float]  # width / mid
    source: str                 # "bag_live" | "synthetic" | "unavailable"
    long_leg: Optional[LegQuote] = None
    short_leg: Optional[LegQuote] = None


@dataclass
class CloseAttempt:
    price_credit: float
    order_id: Optional[int]
    status: str             # "FILLED" | "NOT_FILLED" | "CANCELLED" | "ERROR"
    fill_price: Optional[float]
    fill_qty: int
    timestamp_utc: str
    error: Optional[str] = None


@dataclass
class SpreadCloseResult:
    """
    Full result record for the close attempt.

    status values:
        "FILLED"               — at least one lot filled
        "STAGED"               — order staged in TWS (transmit=False)
        "WIDE_MARKET_NO_CLOSE" — liquidity guard blocked submission
        "POSITION_NOT_FOUND"   — legs not in IBKR account
        "LADDER_EXHAUSTED"     — tried all prices, no fill
        "ERROR"                — exception / connection failure
    """
    status: str
    spread_quote: Optional[SpreadQuote]
    attempts: List[CloseAttempt] = field(default_factory=list)
    fill_price: Optional[float] = None
    fill_qty: int = 0
    order_id: Optional[int] = None
    perm_id: Optional[int] = None
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: Optional[str] = None
    log: List[str] = field(default_factory=list)
    mode: str = "paper"
    symbol: str = _SYMBOL
    expiry: str = _EXPIRY
    long_strike: float = _LONG_STRIKE
    short_strike: float = _SHORT_STRIKE
    right: str = _RIGHT
    qty: int = _QTY


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(result: SpreadCloseResult, level: str, msg: str) -> None:
    """Append to result.log AND emit to Python logger."""
    entry = f"[{_ts()}] [{level}] {msg}"
    result.log.append(entry)
    getattr(logger, level.lower(), logger.info)(msg)


def _qualify_option(
    ib: "IB",
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
) -> Optional[int]:
    """
    Qualify a single option contract and return its conId.

    Returns None if qualification fails.
    """
    contract = Option(symbol, expiry, strike, right, "SMART", currency="USD")
    try:
        qualified = ib.qualifyContracts(contract)
    except Exception as exc:
        logger.error("qualifyContracts exception for %s %s %.0f %s: %s",
                     symbol, expiry, strike, right, exc)
        return None

    if not qualified or not getattr(qualified[0], "conId", None):
        logger.error("Failed to qualify %s %s %.0f %s", symbol, expiry, strike, right)
        return None

    con_id = qualified[0].conId
    logger.info("Qualified %s %s %.0f%s → conId=%d", symbol, expiry, strike, right, con_id)
    return con_id


def _verify_position(
    ib: "IB",
    result: SpreadCloseResult,
    symbol: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
    right: str,
    qty: int,
) -> bool:
    """
    Check that the live IBKR account holds:
        +qty  symbol expiry long_strike  right
        -qty  symbol expiry short_strike right

    Returns True if both legs are present in the expected quantities.
    Logs details into result.log regardless of outcome.
    """
    _log(result, "INFO", "Requesting live positions from IBKR …")
    try:
        ib.reqPositions()
        ib.sleep(2)
        positions = ib.positions()
    except Exception as exc:
        _log(result, "ERROR", f"reqPositions() failed: {exc}")
        return False

    _log(result, "INFO", f"Received {len(positions)} position records from IBKR")

    exp_norm = expiry.replace("-", "")

    long_found = False
    short_found = False

    for pos in positions:
        c = pos.contract
        if c.secType != "OPT":
            continue
        if c.symbol.upper() != symbol.upper():
            continue
        if c.right.upper() != right.upper():
            continue
        # Normalise expiry from contract (TWS may return YYYYMMDD or YYYY-MM-DD)
        pos_expiry = str(c.lastTradeDateOrContractMonth).replace("-", "")[:8]
        if pos_expiry != exp_norm:
            continue

        signed_qty = pos.position  # float

        _log(result, "INFO",
             f"  Found OPT position: {c.symbol} {pos_expiry} "
             f"strike={c.strike} {c.right}  qty={signed_qty:+.0f}")

        if float(c.strike) == long_strike and signed_qty >= qty:
            long_found = True
            _log(result, "INFO",
                 f"  ✓ Long leg matched: +{qty} {symbol} {expiry} {long_strike:.0f}{right}")

        if float(c.strike) == short_strike and signed_qty <= -qty:
            short_found = True
            _log(result, "INFO",
                 f"  ✓ Short leg matched: -{qty} {symbol} {expiry} {short_strike:.0f}{right}")

    if not long_found:
        _log(result, "ERROR",
             f"POSITION_NOT_FOUND: missing +{qty} {symbol} {expiry} {long_strike:.0f}{right}")
    if not short_found:
        _log(result, "ERROR",
             f"POSITION_NOT_FOUND: missing -{qty} {symbol} {expiry} {short_strike:.0f}{right}")

    return long_found and short_found


def _build_bag_contract(
    symbol: str,
    expiry: str,
    long_strike: float,
    short_strike: float,
    right: str,
    long_con_id: int,
    short_con_id: int,
) -> "Contract":
    """
    Construct a BAG Contract representing the closing spread order.

    Closing legs:
        SELL  long_strike  right  (close the long leg)
        BUY   short_strike right  (close the short leg)
    """
    bag = Contract()
    bag.symbol = symbol
    bag.secType = "BAG"
    bag.currency = "USD"
    bag.exchange = "SMART"

    # Leg 1: SELL the long put (close our long)
    leg1 = ComboLeg()
    leg1.conId = long_con_id
    leg1.ratio = 1
    leg1.action = "SELL"
    leg1.exchange = "SMART"

    # Leg 2: BUY the short put (close our short)
    leg2 = ComboLeg()
    leg2.conId = short_con_id
    leg2.ratio = 1
    leg2.action = "BUY"
    leg2.exchange = "SMART"

    bag.comboLegs = [leg1, leg2]
    return bag


def _fetch_single_leg_quote(
    ib: "IB",
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
) -> LegQuote:
    """Fetch live bid/ask for a single option leg via snapshot request."""
    contract = Option(symbol, expiry, strike, right, "SMART", currency="USD")
    bid = ask = mid = None
    source = "unavailable"
    try:
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, "", snapshot=True)
        ib.sleep(2)
        raw_bid = ticker.bid if (ticker.bid and ticker.bid > 0) else None
        raw_ask = ticker.ask if (ticker.ask and ticker.ask > 0) else None
        ib.cancelMktData(contract)
        if raw_bid is not None and raw_ask is not None:
            bid, ask = raw_bid, raw_ask
            mid = (bid + ask) / 2.0
            source = "live"
        elif raw_bid is not None:
            bid = raw_bid
            source = "live_bid_only"
        elif raw_ask is not None:
            ask = raw_ask
            source = "live_ask_only"
    except Exception as exc:
        logger.warning("Leg quote fetch failed for %s %.0f%s: %s", expiry, strike, right, exc)
    return LegQuote(strike=strike, right=right, bid=bid, ask=ask, mid=mid, source=source)


def _fetch_bag_quote(
    ib: "IB",
    bag: "Contract",
    result: SpreadCloseResult,
    long_strike: float,
    short_strike: float,
    right: str,
    expiry: str,
    symbol: str,
) -> SpreadQuote:
    """
    Attempt to fetch a live BAG combo quote from IBKR.
    Falls back to synthetic (leg-level) quotes if BAG data is unavailable.

    For the close direction (SELL BAG):
        combo_bid = what the market will pay us   (minimum credit achievable)
        combo_ask = what we could demand as seller (higher, natural ask side)
    """
    bag_bid = bag_ask = bag_mid = None
    source = "unavailable"

    # ---- Attempt 1: live BAG combo quote ----
    try:
        ticker = ib.reqMktData(bag, "", snapshot=True)
        ib.sleep(3)
        raw_bid = ticker.bid if (ticker.bid and ticker.bid > 0) else None
        raw_ask = ticker.ask if (ticker.ask and ticker.ask > 0) else None
        ib.cancelMktData(bag)

        if raw_bid is not None and raw_ask is not None:
            bag_bid = raw_bid
            bag_ask = raw_ask
            bag_mid = (bag_bid + bag_ask) / 2.0
            source = "bag_live"
            _log(result, "INFO",
                 f"BAG live quote: bid={bag_bid:.4f}  ask={bag_ask:.4f}  mid={bag_mid:.4f}")
        else:
            _log(result, "INFO",
                 f"BAG live quote unavailable (bid={raw_bid} ask={raw_ask}) — "
                 f"falling back to synthetic from legs")
    except Exception as exc:
        _log(result, "WARNING", f"BAG reqMktData exception: {exc} — falling back to synthetic")

    # ---- Attempt 2: synthetic from individual legs ----
    long_leg_q = _fetch_single_leg_quote(ib, symbol, expiry, long_strike, right)
    short_leg_q = _fetch_single_leg_quote(ib, symbol, expiry, short_strike, right)

    _log(result, "INFO",
         f"Leg quotes — long({long_strike:.0f}): "
         f"bid={long_leg_q.bid} ask={long_leg_q.ask}  |  "
         f"short({short_strike:.0f}): bid={short_leg_q.bid} ask={short_leg_q.ask}")

    if source == "unavailable":
        # Compute synthetic spread quotes:
        #   When we SELL 590P and BUY 570P:
        #   synthetic_bid (min credit) = long_leg_bid - short_leg_ask
        #   synthetic_ask (max credit) = long_leg_ask - short_leg_bid
        s_bid = s_ask = s_mid = None
        ll_b, ll_a = long_leg_q.bid, long_leg_q.ask
        sl_b, sl_a = short_leg_q.bid, short_leg_q.ask

        if ll_b is not None and sl_a is not None:
            s_bid = max(0.0, ll_b - sl_a)
        if ll_a is not None and sl_b is not None:
            s_ask = max(0.0, ll_a - sl_b)
        if s_bid is not None and s_ask is not None:
            s_mid = (s_bid + s_ask) / 2.0
        elif s_bid is not None:
            s_mid = s_bid
        elif s_ask is not None:
            s_mid = s_ask

        if s_bid is not None or s_ask is not None:
            bag_bid, bag_ask, bag_mid = s_bid, s_ask, s_mid
            source = "synthetic"
            _log(result, "INFO",
                 f"Synthetic quote: bid={bag_bid} ask={bag_ask} mid={bag_mid}")
        else:
            _log(result, "WARNING", "Could not compute synthetic quote — no leg prices available")

    # Compute width / width_pct
    width = None
    width_pct = None
    if bag_bid is not None and bag_ask is not None:
        width = bag_ask - bag_bid
        if bag_mid and bag_mid > 0:
            width_pct = width / bag_mid

    return SpreadQuote(
        bid=bag_bid,
        ask=bag_ask,
        mid=bag_mid,
        width=width,
        width_pct=width_pct,
        source=source,
        long_leg=long_leg_q,
        short_leg=short_leg_q,
    )


def _check_liquidity_guard(
    quote: SpreadQuote,
    max_width_pct: float,
    result: SpreadCloseResult,
) -> bool:
    """
    Returns True (blocked) if combo width > max_width_pct of mid.
    Returns False (not blocked) if quote is unavailable or width is acceptable.
    """
    if quote.width_pct is None:
        _log(result, "WARNING",
             "Liquidity guard: could not compute width_pct — proceeding (guard not applied)")
        return False

    _log(result, "INFO",
         f"Liquidity guard: width={quote.width:.4f}  mid={quote.mid:.4f}  "
         f"width_pct={quote.width_pct:.1%}  threshold={max_width_pct:.1%}")

    if quote.width_pct > max_width_pct:
        _log(result, "ERROR",
             f"WIDE_MARKET_NO_CLOSE: spread width {quote.width_pct:.1%} > "
             f"threshold {max_width_pct:.1%} — order not submitted")
        return True  # blocked

    _log(result, "INFO",
         f"Liquidity guard passed: {quote.width_pct:.1%} ≤ {max_width_pct:.1%}")
    return False


def _poll_for_fill(
    ib: "IB",
    trade: Any,
    fill_timeout_sec: int,
    poll_interval_sec: int,
    result: SpreadCloseResult,
    price_credit: float,
) -> CloseAttempt:
    """
    Poll a live trade for fill status.

    Returns a CloseAttempt with status:
        "FILLED"     — order fully filled
        "NOT_FILLED" — timeout expired with no fill
        "ERROR"      — unexpected status / exception
    """
    order_id = trade.order.orderId
    deadline = time.monotonic() + fill_timeout_sec
    last_status = ""

    while time.monotonic() < deadline:
        ib.sleep(min(poll_interval_sec, max(1, deadline - time.monotonic())))
        status = trade.orderStatus.status  # e.g. "Filled", "Submitted", "PreSubmitted"

        if status != last_status:
            _log(result, "INFO",
                 f"Order {order_id} at ${price_credit:.2f} credit — status: {status}  "
                 f"filled={trade.orderStatus.filled}  remaining={trade.orderStatus.remaining}")
            last_status = status

        if status == "Filled":
            fill_price = trade.orderStatus.avgFillPrice
            fill_qty = int(trade.orderStatus.filled)
            _log(result, "INFO",
                 f"ORDER FILLED: orderId={order_id}  qty={fill_qty}  "
                 f"avg_fill=${fill_price:.4f}")
            return CloseAttempt(
                price_credit=price_credit,
                order_id=order_id,
                status="FILLED",
                fill_price=fill_price,
                fill_qty=fill_qty,
                timestamp_utc=_ts(),
            )

        if status in ("Cancelled", "Inactive"):
            _log(result, "WARNING",
                 f"Order {order_id} entered terminal status: {status}")
            return CloseAttempt(
                price_credit=price_credit,
                order_id=order_id,
                status="CANCELLED",
                fill_price=None,
                fill_qty=0,
                timestamp_utc=_ts(),
            )

    # Timeout expired — cancel the order
    _log(result, "INFO",
         f"Fill timeout ({fill_timeout_sec}s) for order {order_id} at "
         f"${price_credit:.2f} — cancelling")
    try:
        ib.cancelOrder(trade.order)
        ib.sleep(2)
    except Exception as exc:
        _log(result, "WARNING", f"Cancel request for {order_id} raised: {exc}")

    return CloseAttempt(
        price_credit=price_credit,
        order_id=order_id,
        status="NOT_FILLED",
        fill_price=None,
        fill_qty=0,
        timestamp_utc=_ts(),
    )


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def close_bear_put_spread(
    *,
    mode: str = "paper",
    symbol: str = _SYMBOL,
    expiry: str = _EXPIRY,
    right: str = _RIGHT,
    long_strike: float = _LONG_STRIKE,
    short_strike: float = _SHORT_STRIKE,
    qty: int = _QTY,
    price_ladder: Optional[List[float]] = None,
    min_credit: float = _MIN_CREDIT,
    max_width_pct: float = _MAX_WIDTH_PCT,
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    client_id: int = 20,
    transmit: bool = True,
    fill_timeout_sec: int = 60,
    poll_interval_sec: int = 5,
) -> SpreadCloseResult:
    """
    Close one existing SPY bear put spread via a BAG combo order.

    This is the primary entrypoint for this module.

    Args:
        mode:             "paper" | "live"
        symbol:           Underlier symbol (default "SPY")
        expiry:           Option expiry YYYYMMDD (default "20260320")
        right:            Option right (default "P")
        long_strike:      Strike of the long leg — higher strike (default 590)
        short_strike:     Strike of the short leg — lower strike  (default 570)
        qty:              Number of spreads to close (default 1)
        price_ladder:     Credit ladder in descending order (default [0.16, 0.15, 0.14])
        min_credit:       Never submit below this credit (default 0.14)
        max_width_pct:    Liquidity guard threshold (default 0.25 = 25 % of mid)
        host:             IBKR TWS/Gateway host (default 127.0.0.1)
        port:             IBKR port (None → auto: paper=7497, live=7496)
        client_id:        IBKR client ID (default 20 — avoids collision with snapshot client)
        transmit:         True → send to exchange; False → stage in TWS only
        fill_timeout_sec: Seconds to wait per price level before cancel+retry (default 60)
        poll_interval_sec:Poll frequency for fill status (default 5)

    Returns:
        SpreadCloseResult with full audit trail.
    """
    if not HAS_IB_INSYNC:
        return SpreadCloseResult(
            status="ERROR",
            spread_quote=None,
            error="ib_insync not installed. Run: pip install ib_insync",
            mode=mode,
        )

    if mode not in ("paper", "live"):
        return SpreadCloseResult(
            status="ERROR",
            spread_quote=None,
            error=f"mode must be 'paper' or 'live', got {mode!r}",
            mode=mode,
        )

    if port is None:
        port = _PORTS[mode]

    if price_ladder is None:
        price_ladder = list(_PRICE_LADDER)

    # Filter ladder below min_credit floor
    ladder = [p for p in price_ladder if p >= min_credit]
    if not ladder:
        return SpreadCloseResult(
            status="ERROR",
            spread_quote=None,
            error=f"All ladder prices are below min_credit={min_credit}",
            mode=mode,
        )

    result = SpreadCloseResult(
        status="ERROR",
        spread_quote=None,
        mode=mode,
        symbol=symbol,
        expiry=expiry,
        long_strike=long_strike,
        short_strike=short_strike,
        right=right,
        qty=qty,
    )

    _log(result, "INFO",
         f"=== close_bear_put_spread START  mode={mode}  host={host}:{port} ===")
    _log(result, "INFO",
         f"Target: {symbol} {expiry} {long_strike:.0f}/{short_strike:.0f} {right}  qty={qty}")
    _log(result, "INFO",
         f"Ladder: {ladder}  min_credit={min_credit}  max_width_pct={max_width_pct:.1%}  "
         f"transmit={transmit}")

    ib = IB()

    try:
        # ---- Connect ----
        _log(result, "INFO", f"Connecting to IBKR at {host}:{port} clientId={client_id} …")
        ib.connect(host, port, clientId=client_id, readonly=False)
        _log(result, "INFO", "Connected to IBKR")

        # ---- Step 1: Verify position ----
        _log(result, "INFO", "Step 1: Verifying live position …")
        position_ok = _verify_position(
            ib, result, symbol, expiry, long_strike, short_strike, right, qty
        )
        if not position_ok:
            result.status = "POSITION_NOT_FOUND"
            _log(result, "ERROR",
                 f"ABORT: required position not found in IBKR account — "
                 f"+{qty} {symbol} {expiry} {long_strike:.0f}{right} / "
                 f"-{qty} {symbol} {expiry} {short_strike:.0f}{right}")
            return result

        _log(result, "INFO", "Position verified ✓")

        # ---- Step 2: Qualify option contracts ----
        _log(result, "INFO", "Step 2: Qualifying option contracts …")
        long_con_id = _qualify_option(ib, symbol, expiry, long_strike, right)
        short_con_id = _qualify_option(ib, symbol, expiry, short_strike, right)

        if long_con_id is None or short_con_id is None:
            result.status = "ERROR"
            result.error = (
                f"Contract qualification failed: "
                f"long_conId={long_con_id}  short_conId={short_con_id}"
            )
            _log(result, "ERROR", result.error)
            return result

        _log(result, "INFO",
             f"Contracts qualified: "
             f"{long_strike:.0f}{right}=conId:{long_con_id}  "
             f"{short_strike:.0f}{right}=conId:{short_con_id}")

        # ---- Step 3: Build BAG contract ----
        _log(result, "INFO", "Step 3: Building BAG combo contract …")
        bag = _build_bag_contract(
            symbol, expiry, long_strike, short_strike, right,
            long_con_id, short_con_id
        )
        _log(result, "INFO",
             f"BAG legs: SELL {long_strike:.0f}{right}(conId={long_con_id})  "
             f"BUY {short_strike:.0f}{right}(conId={short_con_id})")

        # ---- Step 4: Fetch live BAG quote ----
        _log(result, "INFO", "Step 4: Fetching live BAG quote …")
        spread_quote = _fetch_bag_quote(
            ib, bag, result,
            long_strike=long_strike, short_strike=short_strike,
            right=right, expiry=expiry, symbol=symbol,
        )
        result.spread_quote = spread_quote
        _log(result, "INFO",
             f"Spread quote ({spread_quote.source}): "
             f"bid={spread_quote.bid}  ask={spread_quote.ask}  "
             f"mid={spread_quote.mid}  width={spread_quote.width}  "
             f"width_pct={spread_quote.width_pct}")

        # ---- Step 5: Liquidity guard ----
        _log(result, "INFO", "Step 5: Liquidity guard check …")
        if _check_liquidity_guard(spread_quote, max_width_pct, result):
            result.status = "WIDE_MARKET_NO_CLOSE"
            return result

        # ---- Step 6: stage_only shortcut ----
        if not transmit:
            # Place the first (highest) price as a staged order and return immediately.
            price_credit = ladder[0]
            _log(result, "INFO",
                 f"transmit=False — staging SELL BAG at ${price_credit:.2f} credit (not transmitted)")
            order = Order()
            order.action = "SELL"
            order.totalQuantity = qty
            order.orderType = "LMT"
            order.lmtPrice = price_credit
            order.tif = "DAY"
            order.transmit = False

            trade = ib.placeOrder(bag, order)
            ib.sleep(1)

            order_id = trade.order.orderId
            _log(result, "INFO",
                 f"Staged order: orderId={order_id}  BAG SELL {qty} @ ${price_credit:.2f} DAY")

            result.status = "STAGED"
            result.order_id = order_id
            result.attempts.append(CloseAttempt(
                price_credit=price_credit,
                order_id=order_id,
                status="STAGED",
                fill_price=None,
                fill_qty=0,
                timestamp_utc=_ts(),
            ))
            return result

        # ---- Step 7: Pricing ladder with fill poll ----
        _log(result, "INFO",
             f"Step 7: Pricing ladder — {len(ladder)} price(s)  "
             f"timeout={fill_timeout_sec}s per level")

        for price_credit in ladder:
            _log(result, "INFO",
                 f"--- Trying ${price_credit:.2f} credit ---")

            order = Order()
            order.action = "SELL"
            order.totalQuantity = qty
            order.orderType = "LMT"
            order.lmtPrice = price_credit
            order.tif = "DAY"
            order.transmit = True

            trade = ib.placeOrder(bag, order)
            ib.sleep(1)

            order_id = trade.order.orderId
            _log(result, "INFO",
                 f"Placed SELL {qty} BAG @ ${price_credit:.2f} DAY  orderId={order_id}")

            attempt = _poll_for_fill(
                ib, trade, fill_timeout_sec, poll_interval_sec,
                result, price_credit
            )
            result.attempts.append(attempt)

            if attempt.status == "FILLED":
                result.status = "FILLED"
                result.fill_price = attempt.fill_price
                result.fill_qty = attempt.fill_qty
                result.order_id = attempt.order_id
                # Capture permId if available
                try:
                    result.perm_id = trade.orderStatus.permId
                except Exception:
                    pass
                _log(result, "INFO",
                     f"=== CLOSE FILLED: {qty} contract(s) @ ${attempt.fill_price:.4f} credit ===")
                return result

            # Not filled at this level — try next
            _log(result, "INFO",
                 f"Not filled at ${price_credit:.2f}; moving to next ladder price (if any)")

        # Ladder exhausted
        result.status = "LADDER_EXHAUSTED"
        _log(result, "WARNING",
             f"LADDER_EXHAUSTED: tried all prices {ladder} — no fill obtained")
        return result

    except Exception as exc:
        result.status = "ERROR"
        result.error = str(exc)
        _log(result, "ERROR", f"Unhandled exception: {exc}")
        logger.exception("close_bear_put_spread unexpected error")
        return result

    finally:
        if ib.isConnected():
            ib.disconnect()
            _log(result, "INFO", "Disconnected from IBKR")
        _log(result, "INFO",
             f"=== close_bear_put_spread END  status={result.status} ===")
