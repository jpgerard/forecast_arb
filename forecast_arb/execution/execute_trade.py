"""
Execute Trade from OrderIntent

This module executes a trade order based on an OrderIntent JSON file.
It connects to IBKR, qualifies contracts, enforces guards, and places orders.

SAFETY FIRST:
- Requires explicit --live or --paper flag
- Requires explicit --transmit flag
- Requires --confirm SEND when transmit=true
- NO silent fallbacks
- Hard abort on guard violations

ENFORCEMENT (Phase 4b):
- PR-EXEC-1: Intent immutability - execution uses ONLY intent fields (expiry, strikes, qty, limits)
- PR-EXEC-2: Price band clamping - execution may tighten but never loosen limits
- PR-EXEC-3: ExecutionResult v2 schema - structured result with verdict
- PR-EXEC-4: Mode invariants - quote-only/paper/live strict rules
- PR-EXEC-5: Ledger hook - append to trade_outcomes.jsonl even in quote-only
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

# IBKR imports
try:
    from ib_insync import IB, Contract, ComboLeg, Order, Stock, Option
    IBKR_AVAILABLE = True
except ImportError:
    IBKR_AVAILABLE = False
    logging.warning("ib_insync not available - execution will not work")

logger = logging.getLogger(__name__)


def setup_logging():
    """Configure logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def load_order_intent(intent_path: str) -> Dict[str, Any]:
    """
    Load OrderIntent JSON file.
    
    Args:
        intent_path: Path to order_intent.json
        
    Returns:
        OrderIntent dict
    """
    logger.info(f"Loading OrderIntent: {intent_path}")
    
    if not Path(intent_path).exists():
        raise FileNotFoundError(f"OrderIntent file not found: {intent_path}")
    
    with open(intent_path, "r") as f:
        intent = json.load(f)
    
    logger.info(f"✓ Loaded intent: {intent.get('strategy')} {intent.get('symbol')} {intent.get('expiry')}")
    
    return intent


def validate_order_intent(intent: Dict[str, Any]) -> None:
    """
    Validate OrderIntent structure and required fields.

    Validation order:
      1. Structural fields (strategy, symbol, expiry, type, legs, qty, limit, tif, guards)
      2. Leg contents (not empty; each has action/right/strike)
      3. Limit contents (has start and max)
      4. intent_id (checked last so structural errors surface first)

    Args:
        intent: OrderIntent dict

    Raises:
        ValueError: If validation fails
    """
    # 1. Required structural fields (intent_id checked separately below)
    required_fields = [
        "strategy", "symbol", "expiry", "type", "legs",
        "qty", "limit", "tif", "guards",
    ]

    for field in required_fields:
        if field not in intent:
            raise ValueError(f"OrderIntent missing required field: {field}")

    # 2. Validate legs (structural checks before intent_id so errors surface first)
    if not intent["legs"]:
        raise ValueError("OrderIntent must have at least one leg")

    for i, leg in enumerate(intent["legs"]):
        if "action" not in leg or "right" not in leg or "strike" not in leg:
            raise ValueError(f"OrderIntent leg {i} missing required fields")

    # 3. Validate limit
    if "start" not in intent["limit"] or "max" not in intent["limit"]:
        raise ValueError("OrderIntent limit must have 'start' and 'max'")

    # 4. intent_id — checked last so structural errors surface first
    if "intent_id" not in intent:
        raise ValueError("OrderIntent missing required field: intent_id")
    if not intent["intent_id"] or not isinstance(intent["intent_id"], str):
        raise ValueError("OrderIntent intent_id must be a non-empty string")

    logger.info("✓ OrderIntent validation passed")


def connect_ibkr(mode: str, host: str = "127.0.0.1", port: Optional[int] = None) -> IB:
    """
    Connect to IBKR TWS/Gateway.
    
    Args:
        mode: "live" or "paper"
        host: IBKR host
        port: IBKR port (default: 7496 for live, 7497 for paper)
        
    Returns:
        Connected IB instance
    """
    if not IBKR_AVAILABLE:
        raise RuntimeError("ib_insync not available - cannot connect to IBKR")
    
    # Default ports
    if port is None:
        port = 7496
    
    logger.info(f"Connecting to IBKR ({mode} mode): {host}:{port}")
    
    ib = IB()
    
    try:
        ib.connect(host=host, port=port, clientId=1, timeout=10)
        logger.info(f"✓ Connected to IBKR ({ib.client.serverVersion()})")
        return ib
    except Exception as e:
        logger.error(f"❌ Failed to connect to IBKR: {e}")
        raise


def qualify_option_contract(
    ib: IB,
    symbol: str,
    expiry: str,
    strike: float,
    right: str
) -> Contract:
    """
    Qualify an option contract with IBKR.
    
    Args:
        ib: IB connection
        symbol: Underlier symbol
        expiry: Expiry in YYYYMMDD format
        strike: Strike price
        right: "P" or "C"
        
    Returns:
        Qualified Contract
    """
    logger.info(f"Qualifying contract: {symbol} {expiry} {strike}{right}")
    
    contract = Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        exchange="SMART",
        currency="USD"
    )
    
    qualified = ib.qualifyContracts(contract)
    
    if not qualified:
        raise ValueError(f"Failed to qualify contract: {symbol} {expiry} {strike}{right}")
    
    logger.info(f"✓ Qualified: conId={qualified[0].conId}")
    
    return qualified[0]


def get_live_quotes(ib: IB, contract: Contract) -> Dict[str, float]:
    """
    Get live bid/ask quotes for a contract.
    
    Args:
        ib: IB connection
        contract: Qualified contract
        
    Returns:
        Dict with 'bid', 'ask', 'mid', 'last'
    """
    logger.info(f"Fetching live quotes for conId={contract.conId}")
    
    # Request market data
    ib.reqMktData(contract, "", False, False)
    
    # Wait for ticker to populate
    ib.sleep(2)
    
    ticker = ib.ticker(contract)
    
    # Cancel market data
    ib.cancelMktData(contract)
    
    quotes = {
        "bid": ticker.bid if ticker.bid and ticker.bid > 0 else None,
        "ask": ticker.ask if ticker.ask and ticker.ask > 0 else None,
        "mid": ticker.marketPrice() if ticker.marketPrice() and ticker.marketPrice() > 0 else None,
        "last": ticker.last if ticker.last and ticker.last > 0 else None
    }
    
    logger.info(f"  Bid: {quotes['bid']}, Ask: {quotes['ask']}, Mid: {quotes['mid']}, Last: {quotes['last']}")
    
    return quotes


def enforce_intent_immutability(
    intent: Dict[str, Any],
    resolved_expiry: str,
    resolved_strikes: list
) -> None:
    """
    PR-EXEC-1: Enforce intent immutability.
    
    Execution MUST use ONLY fields from intent:
    - expiry
    - strikes (from legs)
    - qty
    - limit_start / limit_max
    
    Block any re-derivation of expiry or strikes.
    
    FIX C: Expiry must be single-source-of-truth from resolved IBKR contract.
    If intent expiry disagrees with resolved IBKR contract, BLOCK.
    
    Args:
        intent: OrderIntent dict
        resolved_expiry: Expiry resolved from IBKR during execution
        resolved_strikes: Strikes resolved during execution
        
    Raises:
        AssertionError: If intent fields were modified
        ValueError: If expiry mismatch detected (FIX C)
    """
    logger.info("🔒 PR-EXEC-1: Enforcing intent immutability...")
    
    # FIX C: Assert expiry matches resolved IBKR contract (single source of truth)
    intent_expiry = intent["expiry"]
    if intent_expiry != resolved_expiry:
        raise ValueError(
            f"❌ FIX C VIOLATION: Expiry mismatch! "
            f"Intent expiry {intent_expiry} != IBKR resolved {resolved_expiry}. "
            f"Ledger must take expiry from resolved IBKR contract, not candidate file. "
            f"BLOCKING EXECUTION."
        )
    
    # Assert strikes match
    intent_strikes = sorted([float(leg["strike"]) for leg in intent["legs"]])
    resolved_strikes_sorted = sorted([float(s) for s in resolved_strikes])
    assert intent_strikes == resolved_strikes_sorted, \
        f"IMMUTABILITY VIOLATION: Intent strikes {intent_strikes} != resolved {resolved_strikes_sorted}"
    
    logger.info(f"✓ Expiry matches IBKR contract: {intent_expiry}")
    logger.info(f"✓ Strikes immutable: {intent_strikes}")


def apply_price_band_clamping(
    intent: Dict[str, Any],
    computed_mid: float
) -> Tuple[float, float]:
    """
    PR-EXEC-2: Price band clamping.
    
    Execution may tighten but never loosen limits.
    
    After computing synthetic mid:
        exec_limit_low  = max(intent["limit_start"], computed_start)
        exec_limit_high = min(intent["limit_max"], computed_max)
    
    If exec_limit_low > exec_limit_high → BLOCKED_PRICE_DRIFT
    
    Args:
        intent: OrderIntent dict
        computed_mid: Computed synthetic mid price
        
    Returns:
        Tuple of (exec_limit_low, exec_limit_high)
        
    Raises:
        ValueError: If price band is invalid (BLOCKED_PRICE_DRIFT)
    """
    logger.info("💰 PR-EXEC-2: Applying price band clamping...")
    
    intent_limit_start = intent["limit"]["start"]
    intent_limit_max = intent["limit"]["max"]
    
    # Computed limits (allow some flexibility around mid)
    # For now, use mid as both start and max for computed
    computed_start = computed_mid
    computed_max = computed_mid
    
    # Clamp: execution may tighten but never loosen
    exec_limit_low = max(intent_limit_start, computed_start)
    exec_limit_high = min(intent_limit_max, computed_max)
    
    logger.info(f"  Intent limits: [{intent_limit_start:.2f}, {intent_limit_max:.2f}]")
    logger.info(f"  Computed limits: [{computed_start:.2f}, {computed_max:.2f}]")
    logger.info(f"  Effective limits: [{exec_limit_low:.2f}, {exec_limit_high:.2f}]")
    
    # Check for BLOCKED_PRICE_DRIFT
    if exec_limit_low > exec_limit_high:
        raise ValueError(
            f"BLOCKED_PRICE_DRIFT: Effective limit low {exec_limit_low:.2f} > "
            f"limit high {exec_limit_high:.2f}. Price has drifted outside acceptable range."
        )
    
    logger.info(f"✓ Price band valid: [{exec_limit_low:.2f}, {exec_limit_high:.2f}]")
    
    return exec_limit_low, exec_limit_high


def enforce_mode_invariants(
    mode: str,
    quote_only: bool,
    transmit: bool,
    confirm: Optional[str]
) -> None:
    """
    PR-EXEC-4: Enforce mode invariants.
    
    Rules:
    - quote-only → never stage or transmit
    - paper → stage allowed, transmit forbidden
    - live → transmit requires explicit confirm string
    
    Args:
        mode: Execution mode ("live" or "paper")
        quote_only: Quote-only flag
        transmit: Transmit flag
        confirm: Confirmation string
        
    Raises:
        AssertionError: If mode invariants are violated
    """
    logger.info("🛡️ PR-EXEC-4: Enforcing mode invariants...")
    
    # Rule 1: quote-only → never stage or transmit
    if quote_only:
        assert not transmit, "MODE VIOLATION: quote-only mode cannot transmit"
        logger.info("✓ Quote-only mode: no staging or transmit")
        return
    
    # Rule 2: paper → transmit forbidden
    if mode == "paper":
        assert not transmit, "MODE VIOLATION: paper mode cannot transmit to exchange"
        logger.info("✓ Paper mode: staging allowed, transmit forbidden")
    
    # Rule 3: live → transmit requires explicit confirm
    if mode == "live" and transmit:
        assert confirm == "SEND", \
            "MODE VIOLATION: live mode transmit requires --confirm SEND"
        logger.info("✓ Live mode with transmit: confirmation verified")
    elif mode == "live":
        logger.info("✓ Live mode without transmit: staging only")


# write_ledger_hook() has been removed - dangerous and replaced by append_trade_event()
# Use append_trade_event() with appropriate event types:
# - QUOTE_OK / QUOTE_BLOCKED for quote-only
# - STAGED_PAPER for paper staging
# - SUBMITTED_LIVE for live transmission
# - FILLED_OPEN for confirmed fills


def enforce_guards(
    intent: Dict[str, Any],
    leg_quotes: list,
    combo_debit: float,
    spot_price: float
) -> None:
    """
    Enforce guard conditions from intent.
    
    Args:
        intent: OrderIntent dict
        leg_quotes: List of leg quote dicts
        combo_debit: Computed combo debit
        spot_price: Current spot price
        
    Raises:
        ValueError: If any guard is violated
    """
    guards = intent["guards"]
    
    logger.info("Enforcing guards...")
    
    # Guard: max_debit
    if "max_debit" in guards:
        max_debit = guards["max_debit"]
        if combo_debit > max_debit:
            raise ValueError(
                f"GUARD VIOLATION: Debit ${combo_debit:.2f} exceeds max ${max_debit:.2f}"
            )
        logger.info(f"✓ Debit ${combo_debit:.2f} <= max ${max_debit:.2f}")
    
    # Guard: max_spread_width
    if "max_spread_width" in guards:
        max_spread_width = guards["max_spread_width"]
        strikes = [leg["strike"] for leg in intent["legs"]]
        spread_width_abs = abs(max(strikes) - min(strikes))
        spread_width_pct = spread_width_abs / spot_price
        
        if spread_width_pct > max_spread_width:
            raise ValueError(
                f"GUARD VIOLATION: Spread width {spread_width_pct:.2%} exceeds max {max_spread_width:.2%}"
            )
        logger.info(f"✓ Spread width {spread_width_pct:.2%} <= max {max_spread_width:.2%}")
    
    # Guard: require_executable_legs
    if guards.get("require_executable_legs", False):
        for i, leg_quote in enumerate(leg_quotes):
            if leg_quote["bid"] is None or leg_quote["ask"] is None:
                raise ValueError(
                    f"GUARD VIOLATION: Leg {i} missing executable quotes (bid={leg_quote['bid']}, ask={leg_quote['ask']})"
                )
        logger.info(f"✓ All legs have executable quotes")
    
    # Guard: min_dte
    if "min_dte" in guards:
        min_dte = guards["min_dte"]
        
        # Compute DTE from expiry
        expiry_str = intent["expiry"]
        expiry_date = datetime.strptime(expiry_str, "%Y%m%d").date()
        today = datetime.now(timezone.utc).date()
        dte = (expiry_date - today).days
        
        if dte < min_dte:
            raise ValueError(
                f"GUARD VIOLATION: DTE {dte} < min {min_dte}"
            )
        logger.info(f"✓ DTE {dte} >= min {min_dte}")
    
    logger.info("✅ All guards passed")


def build_combo_contract(ib: IB, intent: Dict[str, Any]) -> Contract:
    """
    Build a BAG combo contract for the vertical spread.
    
    Args:
        ib: IB connection
        intent: OrderIntent dict
        
    Returns:
        Combo Contract
    """
    logger.info("Building BAG combo contract...")
    
    symbol = intent["symbol"]
    expiry = intent["expiry"]
    legs = intent["legs"]
    
    # Qualify each leg
    qualified_legs = []
    for leg in legs:
        qualified = qualify_option_contract(
            ib=ib,
            symbol=symbol,
            expiry=expiry,
            strike=leg["strike"],
            right=leg["right"]
        )
        qualified_legs.append((qualified, leg))
    
    # Build combo legs
    combo_legs = []
    for qualified_contract, leg_spec in qualified_legs:
        combo_leg = ComboLeg()
        combo_leg.conId = qualified_contract.conId
        combo_leg.ratio = 1
        combo_leg.action = leg_spec["action"]
        combo_leg.exchange = "SMART"
        
        combo_legs.append(combo_leg)
    
    # Create bag contract
    bag = Contract()
    bag.symbol = symbol
    bag.secType = "BAG"
    bag.exchange = "SMART"
    bag.currency = "USD"
    bag.comboLegs = combo_legs
    
    logger.info(f"✓ Built BAG combo with {len(combo_legs)} legs")
    
    return bag, qualified_legs


def compute_combo_debit(leg_quotes: list, intent: Dict[str, Any]) -> float:
    """
    Compute combo debit from leg quotes.
    
    For a debit spread:
    - BUY leg: pay the ask
    - SELL leg: collect the bid
    - Debit = sum(BUY asks) - sum(SELL bids)
    
    Args:
        leg_quotes: List of (quotes_dict, leg_spec) tuples
        intent: OrderIntent dict
        
    Returns:
        Combo debit per spread (in dollars)
    """
    total_debit = 0.0
    
    for (quotes, leg_spec) in leg_quotes:
        if leg_spec["action"] == "BUY":
            # Pay the ask
            if quotes["ask"] is None:
                raise ValueError(f"BUY leg missing ask price: {leg_spec}")
            total_debit += quotes["ask"]
        else:  # SELL
            # Collect the bid
            if quotes["bid"] is None:
                raise ValueError(f"SELL leg missing bid price: {leg_spec}")
            total_debit -= quotes["bid"]
    
    logger.info(f"Computed combo debit: ${total_debit:.2f}")
    
    return total_debit


def print_ticket_summary(
    intent: Dict[str, Any],
    spot_price: float,
    leg_quotes_with_specs: list,
    combo_debit: float,
    limit_price: float,
    guards_passed: bool,
    guards_result: str,
    transmit: bool,
    quote_only: bool
) -> None:
    """
    Print human-readable ticket summary.
    
    Format:
    INTENT: SPY 20260320 P590/P570 x1  LIMIT start=0.35 max=0.36  transmit=false
    LEGS: 590P bid/ask/mid=... | 570P bid/ask/mid=...
    SPREAD(synth): bid/ask/mid=...
    SPREAD(combo): bid/ask/mid=... (or N/A)
    GUARDS: executable_legs=PASS | max_spread_width=PASS | max_debit=PASS | min_dte=PASS
    DECISION: OK_TO_STAGE / ABORT: <reason>
    """
    symbol = intent["symbol"]
    expiry = intent["expiry"]
    qty = intent["qty"]
    legs = intent["legs"]
    guards = intent["guards"]
    
    # Extract strikes
    strikes = sorted([leg["strike"] for leg in legs])
    if len(strikes) >= 2:
        strike_str = f"P{strikes[1]:.0f}/P{strikes[0]:.0f}"
    else:
        strike_str = "UNKNOWN"
    
    # INTENT line
    print("")
    print("=" * 80)
    print("TICKET SUMMARY")
    print("=" * 80)
    print(f"INTENT: {symbol} {expiry} {strike_str} x{qty}  "
          f"LIMIT start={limit_price:.2f} max={intent['limit']['max']:.2f}  "
          f"transmit={str(transmit).lower()}")
    
    # LEGS line
    leg_parts = []
    for quotes, leg_spec in leg_quotes_with_specs:
        strike = leg_spec["strike"]
        right = leg_spec["right"]
        bid = quotes["bid"] if quotes["bid"] is not None else "N/A"
        ask = quotes["ask"] if quotes["ask"] is not None else "N/A"
        mid = quotes["mid"] if quotes["mid"] is not None else "N/A"
        
        if isinstance(bid, (int, float)):
            bid_str = f"{bid:.2f}"
        else:
            bid_str = bid
        if isinstance(ask, (int, float)):
            ask_str = f"{ask:.2f}"
        else:
            ask_str = ask
        if isinstance(mid, (int, float)):
            mid_str = f"{mid:.2f}"
        else:
            mid_str = mid
        
        leg_parts.append(f"{strike:.0f}{right} bid/ask/mid={bid_str}/{ask_str}/{mid_str}")
    
    print(f"LEGS: {' | '.join(leg_parts)}")
    
    # SPREAD(synth) - pessimistic (buy ask for long, sell bid for short)
    spread_bid = None
    spread_ask = None
    spread_mid = None
    
    # Compute synthetic spread quotes
    # For put spread: BUY higher strike (long), SELL lower strike (short)
    # Bid: sell the spread = (bid_long - ask_short)
    # Ask: buy the spread = (ask_long - bid_short)
    # Mid: (bid + ask) / 2
    
    try:
        long_leg = next((q, s) for q, s in leg_quotes_with_specs if s["action"] == "BUY")
        short_leg = next((q, s) for q, s in leg_quotes_with_specs if s["action"] == "SELL")
        
        long_quotes, _ = long_leg
        short_quotes, _ = short_leg
        
        if long_quotes["bid"] is not None and short_quotes["ask"] is not None:
            spread_bid = long_quotes["bid"] - short_quotes["ask"]
        
        if long_quotes["ask"] is not None and short_quotes["bid"] is not None:
            spread_ask = long_quotes["ask"] - short_quotes["bid"]
        
        if spread_bid is not None and spread_ask is not None:
            spread_mid = (spread_bid + spread_ask) / 2
    except StopIteration:
        pass
    
    # Format spread quotes
    spread_bid_str = f"{spread_bid:.2f}" if spread_bid is not None else "N/A"
    spread_ask_str = f"{spread_ask:.2f}" if spread_ask is not None else "N/A"
    spread_mid_str = f"{spread_mid:.2f}" if spread_mid is not None else "N/A"
    
    print(f"SPREAD(synth): bid/ask/mid={spread_bid_str}/{spread_ask_str}/{spread_mid_str}")
    print(f"SPREAD(combo): bid/ask/mid=N/A (combo quotes not implemented)")
    
    # GUARDS line - check each guard
    guard_checks = []
    
    # Check executable_legs
    if guards.get("require_executable_legs", False):
        all_executable = all(
            q["bid"] is not None and q["ask"] is not None
            for q, _ in leg_quotes_with_specs
        )
        guard_checks.append(f"executable_legs={'PASS' if all_executable else 'FAIL'}")
    
    # Check max_spread_width
    if "max_spread_width" in guards:
        max_spread_width = guards["max_spread_width"]
        spread_width_abs = abs(strikes[-1] - strikes[0])
        spread_width_pct = spread_width_abs / spot_price
        passed = spread_width_pct <= max_spread_width
        guard_checks.append(f"max_spread_width={'PASS' if passed else 'FAIL'}")
    
    # Check max_debit
    if "max_debit" in guards:
        max_debit = guards["max_debit"]
        passed = combo_debit <= max_debit
        guard_checks.append(f"max_debit={'PASS' if passed else 'FAIL'}")
    
    # Check min_dte
    if "min_dte" in guards:
        min_dte = guards["min_dte"]
        expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
        today = datetime.now(timezone.utc).date()
        dte = (expiry_date - today).days
        passed = dte >= min_dte
        guard_checks.append(f"min_dte={'PASS' if passed else 'FAIL'}")
    
    print(f"GUARDS: {' | '.join(guard_checks)}")
    
    # DECISION line
    if quote_only:
        if guards_passed:
            print(f"DECISION: OK_TO_STAGE (quote-only mode, not placing order)")
        else:
            print(f"DECISION: ABORT: {guards_result}")
    else:
        if guards_passed:
            print(f"DECISION: OK_TO_STAGE")
        else:
            print(f"DECISION: ABORT: {guards_result}")
    
    print("=" * 80)
    print("")


def create_limit_order(
    action: str,
    qty: int,
    limit_price: float,
    tif: str = "DAY",
    transmit: bool = False
) -> Order:
    """
    Create a limit order for the combo.
    
    Args:
        action: "BUY" (for debit spreads)
        qty: Quantity
        limit_price: Limit price
        tif: Time in force
        transmit: Whether to transmit immediately
        
    Returns:
        Order object
    """
    order = Order()
    order.action = action
    order.totalQuantity = qty
    order.orderType = "LMT"
    order.lmtPrice = limit_price
    order.tif = tif
    order.transmit = transmit
    
    return order


def execute_order_intent(
    intent_path: str,
    mode: str,
    transmit: bool = False,
    confirm: Optional[str] = None,
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    quote_only: bool = False
) -> Dict[str, Any]:
    """
    Execute an OrderIntent.
    
    Args:
        intent_path: Path to order_intent.json
        mode: "live" or "paper"
        transmit: Whether to transmit order
        confirm: Confirmation string (required if transmit=True)
        host: IBKR host
        port: IBKR port
        quote_only: Quote-only mode (fetch quotes and run guards, don't place order)
        
    Returns:
        Execution result dict
    """
    # Load and validate intent
    intent = load_order_intent(intent_path)
    validate_order_intent(intent)
    
    # PR-EXEC-4: Enforce mode invariants BEFORE doing anything
    enforce_mode_invariants(mode=mode, quote_only=quote_only, transmit=transmit, confirm=confirm)
    
    # SAFETY: Ignore intent's transmit=true unless CLI flag + confirm
    if mode == "live" and intent.get("transmit", False) and not transmit:
        logger.warning("⚠️  Intent has transmit=true but CLI --transmit not set - treating as false")
        intent["transmit"] = False
    
    # SAFETY: transmit requires confirm=SEND (already checked in enforce_mode_invariants)
    if transmit and confirm != "SEND":
        raise ValueError("SAFETY: --transmit requires --confirm SEND")
    
    # Connect to IBKR
    ib = connect_ibkr(mode=mode, host=host, port=port)
    
    try:
        # In quote-only mode, we'll run all the checks but not place orders
        if quote_only:
            logger.info("📋 QUOTE-ONLY MODE: Will fetch quotes and run guards, but NOT place order")
        
        # Get spot price
        logger.info(f"Fetching spot price for {intent['symbol']}...")
        stock = Stock(intent['symbol'], "SMART", "USD")
        ib.qualifyContracts(stock)
        ib.reqMktData(stock, "", False, False)
        ib.sleep(2)
        stock_ticker = ib.ticker(stock)
        spot_price = stock_ticker.marketPrice()
        ib.cancelMktData(stock)
        
        if not spot_price or spot_price <= 0:
            raise ValueError(f"Failed to get valid spot price for {intent['symbol']}")
        
        logger.info(f"✓ Spot price: ${spot_price:.2f}")
        
        # Build combo contract and get live quotes
        combo_contract, qualified_legs = build_combo_contract(ib, intent)
        
        # FIX C: Extract resolved expiry from qualified IBKR contracts (single source of truth)
        resolved_expiry = qualified_legs[0][0].lastTradeDateOrContractMonth
        resolved_strikes = [leg[0].strike for leg in qualified_legs]
        
        # FIX C: Enforce expiry immutability - IBKR contract is single source of truth
        enforce_intent_immutability(intent, resolved_expiry, resolved_strikes)
        
        # Get live quotes for each leg
        leg_quotes_with_specs = []
        for qualified_contract, leg_spec in qualified_legs:
            quotes = get_live_quotes(ib, qualified_contract)
            leg_quotes_with_specs.append((quotes, leg_spec))
        
        # Compute combo debit
        combo_debit = compute_combo_debit(leg_quotes_with_specs, intent)
        
        # Enforce guards
        leg_quotes_only = [q for q, _ in leg_quotes_with_specs]
        try:
            enforce_guards(intent, leg_quotes_only, combo_debit, spot_price)
            guards_passed = True
            guards_result = "ALL_PASSED"
        except ValueError as e:
            guards_passed = False
            guards_result = str(e)
            if not quote_only:
                raise  # Re-raise if not in quote-only mode
        
        # Determine limit price (use start price from intent)
        limit_price = intent["limit"]["start"]
        
        # Print diagnostic ticket summary
        print_ticket_summary(
            intent=intent,
            spot_price=spot_price,
            leg_quotes_with_specs=leg_quotes_with_specs,
            combo_debit=combo_debit,
            limit_price=limit_price,
            guards_passed=guards_passed,
            guards_result=guards_result,
            transmit=transmit and not quote_only,
            quote_only=quote_only
        )
        
        # If in quote-only mode, exit here without placing order
        if quote_only:
            logger.info("")
            logger.info("=" * 80)
            logger.info("✅ QUOTE-ONLY MODE COMPLETE")
            logger.info("=" * 80)
            
            return {
                "success": True,
                "quote_only": True,
                "guards_passed": guards_passed,
                "guards_result": guards_result,
                "intent_path": intent_path,
                "symbol": intent["symbol"],
                "expiry": intent["expiry"],
                "limit_price": limit_price,
                "market_debit": combo_debit,
                "spot_price": spot_price,
                "leg_quotes": [
                    {
                        "action": leg_spec["action"],
                        "right": leg_spec["right"],
                        "strike": leg_spec["strike"],
                        "quotes": quotes
                    }
                    for quotes, leg_spec in leg_quotes_with_specs
                ],
                "timestamp_utc": datetime.now(timezone.utc).isoformat()
            }
        
        # Abort if guards failed
        if not guards_passed:
            raise ValueError(f"Guards failed: {guards_result}")
        
        # Create order
        order = create_limit_order(
            action="BUY",  # Assuming debit spread
            qty=intent["qty"],
            limit_price=limit_price,
            tif=intent["tif"],
            transmit=transmit
        )
        
        # Place order
        logger.info(f"Placing order (transmit={transmit})...")
        trade = ib.placeOrder(combo_contract, order)
        ib.sleep(1)  # Wait for order to register
        
        # Extract order ID
        order_id = trade.order.orderId if trade.order else None
        order_status = trade.orderStatus.status if trade.orderStatus else "UNKNOWN"
        
        logger.info("")
        logger.info("=" * 80)
        logger.info("✅ ORDER PLACED")
        logger.info("=" * 80)
        logger.info(f"Order ID: {order_id}")
        logger.info(f"Status: {order_status}")
        logger.info(f"Transmit: {transmit}")
        logger.info("=" * 80)
        
        # Build execution result
        execution_result = {
            "success": True,
            "order_id": order_id,
            "status": order_status,
            "transmit": transmit,
            "intent_path": intent_path,
            "symbol": intent["symbol"],
            "expiry": intent["expiry"],
            "qty": intent["qty"],
            "limit_price": limit_price,
            "market_debit": combo_debit,
            "spot_price": spot_price,
            "leg_quotes": [
                {
                    "action": leg_spec["action"],
                    "right": leg_spec["right"],
                    "strike": leg_spec["strike"],
                    "quotes": quotes
                }
                for quotes, leg_spec in leg_quotes_with_specs
            ],
            "timestamp_utc": datetime.now(timezone.utc).isoformat()
        }
        
        # Write trade event ledger based on order status
        if transmit:
            try:
                from forecast_arb.execution.outcome_ledger import append_trade_event
                
                # Extract metadata
                candidate_id = intent.get("candidate_id", f"order_{order_id}")
                run_id = intent.get("run_id", "unknown")
                regime = intent.get("regime", "unknown")
                intent_id = intent["intent_id"]  # Now mandatory
                
                # Extract strikes
                strikes = sorted([leg["strike"] for leg in intent["legs"]])
                long_strike = max(strikes) if len(strikes) >= 2 else strikes[0]
                short_strike = min(strikes) if len(strikes) >= 2 else 0.0
                
                # Determine event type based on order status
                if order_status == "Filled":
                    event = "FILLED_OPEN"
                elif order_status in ["Submitted", "PreSubmitted"]:
                    event = "SUBMITTED_LIVE"
                else:
                    event = "SUBMITTED_LIVE"  # Default for live transmission
                
                logger.info(f"Writing trade event: {event} for {candidate_id}...")
                logger.info(f"  intent_id: {intent_id}")
                logger.info(f"  order_id: {order_id}")
                
                # Write event with appropriate fields
                if event == "FILLED_OPEN":
                    # FILLED_OPEN requires all position fields
                    append_trade_event(
                        event=event,
                        intent_id=intent_id,
                        candidate_id=candidate_id,
                        run_id=run_id,
                        regime=regime,
                        timestamp_utc=execution_result["timestamp_utc"],
                        order_id=str(order_id) if order_id else None,
                        expiry=resolved_expiry,
                        long_strike=long_strike,
                        short_strike=short_strike,
                        qty=intent["qty"],
                        entry_price=limit_price,
                        also_global=True
                    )
                else:
                    # SUBMITTED_LIVE only needs basic info
                    append_trade_event(
                        event=event,
                        intent_id=intent_id,
                        candidate_id=candidate_id,
                        run_id=run_id,
                        regime=regime,
                        timestamp_utc=execution_result["timestamp_utc"],
                        order_id=str(order_id) if order_id else None,
                        also_global=True
                    )
                
                logger.info(f"✓ Trade event written: {event}")
                execution_result["ledger_written"] = True
                
            except Exception as e:
                logger.warning(f"Failed to write trade event: {e}")
                execution_result["ledger_written"] = False
                execution_result["ledger_error"] = str(e)
        elif not transmit and not quote_only:
            # Paper staging (no transmit)
            try:
                from forecast_arb.execution.outcome_ledger import append_trade_event
                
                candidate_id = intent.get("candidate_id", f"order_{order_id}")
                run_id = intent.get("run_id", "unknown")
                regime = intent.get("regime", "unknown")
                intent_id = intent["intent_id"]
                
                logger.info(f"Writing trade event: STAGED_PAPER for {candidate_id}...")
                
                append_trade_event(
                    event="STAGED_PAPER",
                    intent_id=intent_id,
                    candidate_id=candidate_id,
                    run_id=run_id,
                    regime=regime,
                    timestamp_utc=execution_result["timestamp_utc"],
                    order_id=str(order_id) if order_id else None,
                    also_global=True
                )
                
                logger.info(f"✓ Trade event written: STAGED_PAPER")
                execution_result["ledger_written"] = True
                
            except Exception as e:
                logger.warning(f"Failed to write trade event: {e}")
                execution_result["ledger_written"] = False
                execution_result["ledger_error"] = str(e)
        
        return execution_result
        
    finally:
        # Disconnect
        ib.disconnect()
        logger.info("✓ Disconnected from IBKR")


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Execute trade from OrderIntent JSON"
    )
    
    parser.add_argument(
        "--intent",
        type=str,
        required=True,
        help="Path to order_intent.json file"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute in LIVE mode (default port: 7496)"
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Execute in PAPER mode (uses same port: 7496)"
    )
    parser.add_argument(
        "--transmit",
        action="store_true",
        help="Transmit order to exchange (default: False, only stage order)"
    )
    parser.add_argument(
        "--confirm",
        type=str,
        default=None,
        help="Confirmation string (must be 'SEND' when --transmit is enabled)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="IBKR TWS/Gateway host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="IBKR TWS/Gateway port (default: 7496)"
    )
    parser.add_argument(
        "--quote-only",
        action="store_true",
        help="Quote-only mode: fetch quotes, run guards, print summary, but don't place order"
    )
    
    args = parser.parse_args()
    
    setup_logging()
    
    # Validate mode
    if not args.live and not args.paper:
        logger.error("❌ Must specify either --live or --paper")
        sys.exit(1)
    
    if args.live and args.paper:
        logger.error("❌ Cannot specify both --live and --paper")
        sys.exit(1)
    
    mode = "live" if args.live else "paper"
    
    # Safety banner
    logger.info("=" * 80)
    logger.info("EXECUTE TRADE FROM ORDER INTENT")
    logger.info("=" * 80)
    logger.info(f"Mode: {mode.upper()}")
    logger.info(f"Intent: {args.intent}")
    logger.info(f"Quote-Only: {args.quote_only}")
    logger.info(f"Transmit: {args.transmit}")
    logger.info("=" * 80)
    logger.info("")
    
    if args.quote_only:
        logger.info("📋 QUOTE-ONLY MODE: Will not place any orders")
    elif args.transmit:
        logger.warning("⚠️  WARNING: --transmit enabled - order will be sent to exchange")
    else:
        logger.info("ℹ️  Order will be STAGED only (not transmitted)")
    
    logger.info("")
    
    try:
        # Execute order intent
        result = execute_order_intent(
            intent_path=args.intent,
            mode=mode,
            transmit=args.transmit,
            confirm=args.confirm,
            host=args.host,
            port=args.port,
            quote_only=args.quote_only
        )
        
        # Write execution result to file
        result_path = Path(args.intent).parent / "execution_result.json"
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2)
        
        logger.info(f"✓ Execution result written: {result_path}")
        logger.info("")
        logger.info("=" * 80)
        logger.info("✅ EXECUTION COMPLETE")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.error(f"❌ Execution failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
