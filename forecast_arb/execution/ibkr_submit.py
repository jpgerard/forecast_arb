"""
IBKR Execution Adapter

Handles submission of order tickets to IBKR.
Supports dry-run mode for safety.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of order execution attempt."""
    
    success: bool
    dry_run: bool
    orders_placed: int = 0
    orders_failed: int = 0
    order_ids: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    details: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return asdict(self)


def submit_tickets(
    tickets: List[Dict[str, Any]],
    ibkr_client: Optional[Any] = None,
    dry_run: bool = True
) -> ExecutionResult:
    """
    Submit order tickets to IBKR.
    
    Args:
        tickets: List of order ticket dicts
        ibkr_client: IBKR client instance (required if not dry_run)
        dry_run: If True, simulate submission without placing orders
        
    Returns:
        ExecutionResult
    """
    if dry_run:
        return _simulate_submission(tickets)
    
    if ibkr_client is None:
        logger.error("IBKR client required for live submission")
        return ExecutionResult(
            success=False,
            dry_run=False,
            errors=["IBKR client not provided"]
        )
    
    return _submit_live(tickets, ibkr_client)


def _simulate_submission(tickets: List[Dict[str, Any]]) -> ExecutionResult:
    """
    Simulate order submission (dry-run mode).
    
    Args:
        tickets: List of order ticket dicts
        
    Returns:
        ExecutionResult with simulated data
    """
    logger.info(f"DRY RUN: Simulating submission of {len(tickets)} ticket(s)")
    
    details = []
    for i, ticket in enumerate(tickets, 1):
        symbol = ticket.get("symbol", "???")
        expiry = ticket.get("expiry", "????????")
        limit_price = ticket.get("limit_price", 0)
        quantity = ticket.get("quantity", 0)
        
        # Simulate order ID (negative for dry-run)
        simulated_order_id = -(1000 + i)
        
        detail = {
            "ticket_index": i - 1,
            "order_id": simulated_order_id,
            "symbol": symbol,
            "expiry": expiry,
            "limit_price": limit_price,
            "quantity": quantity,
            "status": "SIMULATED",
            "message": "Dry-run: order NOT submitted to IBKR"
        }
        
        details.append(detail)
        logger.info(f"  Ticket #{i}: {symbol} {expiry} @ ${limit_price:.2f} x{quantity} -> Order ID {simulated_order_id} (SIMULATED)")
    
    result = ExecutionResult(
        success=True,
        dry_run=True,
        orders_placed=len(tickets),
        orders_failed=0,
        order_ids=[d["order_id"] for d in details],
        details=details
    )
    
    logger.info(f"DRY RUN COMPLETE: {len(tickets)} order(s) simulated")
    
    return result


def _submit_live(tickets: List[Dict[str, Any]], ibkr_client: Any) -> ExecutionResult:
    """
    Submit orders to IBKR (live mode).
    
    Args:
        tickets: List of order ticket dicts
        ibkr_client: IBKR client instance
        
    Returns:
        ExecutionResult
    """
    logger.warning("=" * 80)
    logger.warning("LIVE SUBMISSION MODE")
    logger.warning(f"Submitting {len(tickets)} order(s) to IBKR")
    logger.warning("=" * 80)
    
    details = []
    order_ids = []
    errors = []
    orders_placed = 0
    orders_failed = 0
    
    for i, ticket in enumerate(tickets, 1):
        try:
            # Extract ticket details
            symbol = ticket["symbol"]
            expiry = ticket["expiry"]
            legs = ticket["legs"]
            limit_price = ticket["limit_price"]
            quantity = ticket["quantity"]
            tif = ticket.get("tif", "DAY")
            account = ticket.get("account")
            
            logger.info(f"Submitting ticket #{i}: {symbol} {expiry} @ ${limit_price:.2f} x{quantity}")
            
            # Create combo contract
            combo_contract = _create_combo_contract(symbol, expiry, legs, ibkr_client)
            
            # Create limit order
            order = _create_limit_order(
                action="BUY",  # Buying the spread
                quantity=quantity,
                limit_price=limit_price,
                tif=tif,
                account=account
            )
            
            # Place order
            trade = ibkr_client.placeOrder(combo_contract, order)
            
            # Extract order ID
            if hasattr(trade, 'order') and hasattr(trade.order, 'orderId'):
                order_id = trade.order.orderId
            else:
                order_id = None
            
            detail = {
                "ticket_index": i - 1,
                "order_id": order_id,
                "symbol": symbol,
                "expiry": expiry,
                "limit_price": limit_price,
                "quantity": quantity,
                "status": "SUBMITTED",
                "message": f"Order submitted successfully (ID: {order_id})"
            }
            
            details.append(detail)
            if order_id:
                order_ids.append(order_id)
            
            orders_placed += 1
            logger.info(f"  ✓ Ticket #{i} submitted: Order ID {order_id}")
            
        except Exception as e:
            error_msg = f"Ticket #{i} failed: {str(e)}"
            logger.error(f"  ✗ {error_msg}", exc_info=True)
            errors.append(error_msg)
            orders_failed += 1
            
            detail = {
                "ticket_index": i - 1,
                "order_id": None,
                "symbol": ticket.get("symbol", "???"),
                "expiry": ticket.get("expiry", "????????"),
                "limit_price": ticket.get("limit_price", 0),
                "quantity": ticket.get("quantity", 0),
                "status": "FAILED",
                "message": error_msg
            }
            details.append(detail)
    
    success = (orders_failed == 0)
    
    result = ExecutionResult(
        success=success,
        dry_run=False,
        orders_placed=orders_placed,
        orders_failed=orders_failed,
        order_ids=order_ids,
        errors=errors,
        details=details
    )
    
    logger.info("=" * 80)
    logger.info(f"SUBMISSION COMPLETE: {orders_placed} placed, {orders_failed} failed")
    logger.info("=" * 80)
    
    return result


def _create_combo_contract(
    symbol: str,
    expiry: str,
    legs: List[Dict[str, Any]],
    ibkr_client: Any
) -> Any:
    """
    Create IBKR combo contract for vertical spread.
    
    Args:
        symbol: Underlier symbol
        expiry: Expiry in YYYYMMDD format
        legs: List of leg dicts
        ibkr_client: IBKR client instance
        
    Returns:
        IBKR Contract object
    """
    from ibapi.contract import Contract, ComboLeg
    
    # Create individual leg contracts and qualify them
    combo_legs = []
    
    for leg in legs:
        # Create option contract for this leg
        option_contract = Contract()
        option_contract.symbol = symbol
        option_contract.secType = "OPT"
        option_contract.exchange = leg.get("exchange", "SMART")
        option_contract.currency = "USD"
        option_contract.lastTradeDateOrContractMonth = expiry
        option_contract.strike = leg["strike"]
        option_contract.right = leg["right"]  # "P" or "C"
        option_contract.multiplier = "100"
        
        # Qualify contract to get conId
        qualified = ibkr_client.qualifyContracts(option_contract)
        if not qualified:
            raise ValueError(f"Failed to qualify contract: {symbol} {expiry} {leg['strike']}{leg['right']}")
        
        qualified_contract = qualified[0]
        
        # Create combo leg
        combo_leg = ComboLeg()
        combo_leg.conId = qualified_contract.conId
        combo_leg.ratio = leg["quantity"]
        combo_leg.action = leg["action"]  # "BUY" or "SELL"
        combo_leg.exchange = leg.get("exchange", "SMART")
        
        combo_legs.append(combo_leg)
    
    # Create combo contract
    combo_contract = Contract()
    combo_contract.symbol = symbol
    combo_contract.secType = "BAG"
    combo_contract.exchange = "SMART"
    combo_contract.currency = "USD"
    combo_contract.comboLegs = combo_legs
    
    return combo_contract


def _create_limit_order(
    action: str,
    quantity: int,
    limit_price: float,
    tif: str = "DAY",
    account: Optional[str] = None
) -> Any:
    """
    Create IBKR limit order.
    
    Args:
        action: "BUY" or "SELL"
        quantity: Order quantity
        limit_price: Limit price
        tif: Time in force
        account: Account ID (optional)
        
    Returns:
        IBKR Order object
    """
    from ibapi.order import Order
    
    order = Order()
    order.action = action
    order.totalQuantity = quantity
    order.orderType = "LMT"
    order.lmtPrice = limit_price
    order.tif = tif
    
    if account:
        order.account = account
    
    return order
