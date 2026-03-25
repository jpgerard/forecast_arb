"""
IBKR Option Chain Snapshot Exporter

Connects to local IBKR TWS/Gateway and exports option chain data to JSON.
Read-only, no trading.

Usage:
    python -m forecast_arb.data.ibkr_snapshot SPY --dte-min 20 --dte-max 40 --out snapshot.json
"""

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict

try:
    from ib_insync import IB, Stock, Option, util
    HAS_IB_INSYNC = True
except ImportError:
    HAS_IB_INSYNC = False
    logging.warning("ib_insync not installed. Install with: pip install ib_insync")

from forecast_arb.ibkr.types import SpotResult, SnapshotResult
from forecast_arb.ibkr.spot_cache import (
    make_cache_key,
    load_cached_spot,
    save_cached_spot
)


logger = logging.getLogger(__name__)

# Default TTL: 2 trading days = 48 hours
DEFAULT_CACHE_TTL_SECONDS = 48 * 60 * 60


class IBKRSnapshotExporter:
    """
    Export option chain snapshots from IBKR.
    
    Read-only data collection - no trading functionality.
    """
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7496,  # TWS live trading port
        client_id: int = 1
    ):
        """
        Initialize IBKR connection.
        
        Args:
            host: TWS/Gateway host
            port: TWS/Gateway port (7496=live, 7497=paper)
            client_id: Unique client ID
        """
        if not HAS_IB_INSYNC:
            raise ImportError("ib_insync required. Install with: pip install ib_insync")
        
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
    
    def connect(self):
        """Connect to IBKR TWS/Gateway."""
        logger.info(f"Connecting to IBKR at {self.host}:{self.port}")
        self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True)
        logger.info("Connected to IBKR")
    
    def disconnect(self):
        """Disconnect from IBKR."""
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IBKR")
    
    def get_underlier_price(self, symbol: str, primary_exchange: str = None) -> SpotResult:
        """
        Get current underlier price with robust fallback chain and cache support.
        
        Args:
            symbol: Stock symbol (e.g., "SPY")
            primary_exchange: Primary exchange (default: "ARCA" for SPY, None for others)
            
        Returns:
            SpotResult with spot price and metadata (never raises)
        """
        warnings = []
        audit = {}
        
        # Determine primary exchange
        if primary_exchange is None:
            primary_exchange = "ARCA" if symbol == "SPY" else ""
        
        # Setup cache
        cache_key = make_cache_key(symbol, primary_exchange, "USD")
        cached = load_cached_spot(cache_key, DEFAULT_CACHE_TTL_SECONDS)
        
        # Qualify contract
        try:
            if symbol == "SPY":
                contract = Stock(symbol, "SMART", "USD", primaryExchange=primary_exchange)
            else:
                contract = Stock(symbol, "SMART", "USD")
            
            qualified = self.ib.qualifyContracts(contract)
            
            # Validate contract
            if not qualified or len(qualified) == 0:
                logger.error(f"Failed to qualify {symbol} contract")
                if cached:
                    logger.info(f"Using cached spot as fallback")
                    return SpotResult(
                        ok=True,
                        spot=cached["spot"],
                        source="cached",
                        is_stale=True,
                        warnings=["USED_CACHED_SPOT", "CONTRACT_QUALIFICATION_FAILED"],
                        reason=None,
                        audit={"cached_spot": cached["spot"], "cached_timestamp": cached["timestamp"]}
                    )
                return SpotResult(
                    ok=False,
                    spot=None,
                    source=None,
                    is_stale=False,
                    warnings=[],
                    reason="CONTRACT_MISMATCH",
                    audit={"error": f"Failed to qualify {symbol} contract"}
                )
            
            qualified_contract = qualified[0]
            con_id = qualified_contract.conId
            
            logger.info(f"Qualified {symbol} contract: conId={con_id}, "
                       f"symbol={qualified_contract.symbol}, "
                       f"primaryExchange={qualified_contract.primaryExchange}")
            
            # Verify symbol match
            if qualified_contract.symbol != symbol:
                logger.error(f"Contract mismatch: requested '{symbol}', got '{qualified_contract.symbol}'")
                if cached:
                    logger.info(f"Using cached spot as fallback")
                    return SpotResult(
                        ok=True,
                        spot=cached["spot"],
                        source="cached",
                        is_stale=True,
                        warnings=["USED_CACHED_SPOT", "CONTRACT_SYMBOL_MISMATCH"],
                        reason=None,
                        audit={"cached_spot": cached["spot"], "cached_timestamp": cached["timestamp"],
                               "requested_symbol": symbol, "got_symbol": qualified_contract.symbol}
                    )
                return SpotResult(
                    ok=False,
                    spot=None,
                    source=None,
                    is_stale=False,
                    warnings=[],
                    reason="CONTRACT_MISMATCH",
                    audit={"requested_symbol": symbol, "got_symbol": qualified_contract.symbol}
                )
            
        except Exception as e:
            logger.error(f"Exception during contract qualification: {e}")
            if cached:
                logger.info(f"Using cached spot as fallback")
                return SpotResult(
                    ok=True,
                    spot=cached["spot"],
                    source="cached",
                    is_stale=True,
                    warnings=["USED_CACHED_SPOT", "CONTRACT_EXCEPTION"],
                    reason=None,
                    audit={"cached_spot": cached["spot"], "cached_timestamp": cached["timestamp"],
                           "error": str(e)}
                )
            return SpotResult(
                ok=False,
                spot=None,
                source=None,
                is_stale=False,
                warnings=[],
                reason="CONTRACT_MISMATCH",
                audit={"error": str(e)}
            )
        
        # Request market data
        try:
            ticker = self.ib.reqMktData(contract, "", snapshot=True)
            self.ib.sleep(2)
            
            # Extract raw price fields
            last = ticker.last if ticker.last and ticker.last > 0 else None
            bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
            close = ticker.close if ticker.close and ticker.close > 0 else None
            market_price = ticker.marketPrice() if ticker.marketPrice() and ticker.marketPrice() > 0 else None
            
            logger.info(f"Price data for {symbol}:")
            logger.info(f"  last: {last if last else 'N/A'}")
            logger.info(f"  bid: {bid if bid else 'N/A'}")
            logger.info(f"  ask: {ask if ask else 'N/A'}")
            logger.info(f"  close: {close if close else 'N/A'}")
            logger.info(f"  marketPrice: {market_price if market_price else 'N/A'}")
            
            audit = {
                "raw_last": last,
                "raw_bid": bid,
                "raw_ask": ask,
                "raw_close": close,
                "raw_market_price": market_price
            }
            
            # Price fallback chain: last → midpoint → close (stale)
            spot = None
            source = None
            is_stale = False
            
            if last is not None and last > 0:
                spot = last
                source = "last"
            elif bid is not None and ask is not None and bid > 0 and ask > 0:
                spot = (bid + ask) / 2.0
                source = "midpoint"
            elif close is not None and close > 0:
                spot = close
                source = "close"
                is_stale = True
                warnings.append("USING_STALE_CLOSE")
                logger.warning(f"Using close price (stale) for {symbol}")
            
            # No valid price from live data
            if spot is None:
                self.ib.cancelMktData(contract)
                logger.error(f"No valid price data for {symbol}")
                
                # Try cache as final fallback
                if cached:
                    logger.info(f"Using cached spot as fallback")
                    audit["cached_spot"] = cached["spot"]
                    audit["cached_timestamp"] = cached["timestamp"]
                    return SpotResult(
                        ok=True,
                        spot=cached["spot"],
                        source="cached",
                        is_stale=True,
                        warnings=["USED_CACHED_SPOT", "NO_VALID_LIVE_PRICE"],
                        reason=None,
                        audit=audit
                    )
                
                # No cache available - hard failure
                return SpotResult(
                    ok=False,
                    spot=None,
                    source=None,
                    is_stale=False,
                    warnings=[],
                    reason="NO_VALID_PRICE",
                    audit=audit
                )
            
            logger.info(f"Selected spot: ${spot:.2f} (source: {source})")
            
            # Sanity checks
            has_sanity_issues = False
            
            # Check bid ≤ last ≤ ask (if all available)
            if bid is not None and ask is not None and last is not None:
                if not (bid <= last <= ask):
                    warnings.append(f"LAST_OUTSIDE_SPREAD")
                    logger.warning(f"Last ${last:.2f} outside bid-ask [${bid:.2f}, ${ask:.2f}]")
                    has_sanity_issues = True
            
            # Check |spot - close| / close < 10% (if both available and spot is not close)
            if source != "close" and close is not None and close > 0:
                deviation_pct = abs(spot - close) / close
                if deviation_pct > 0.10:
                    warnings.append(f"LARGE_DEVIATION_FROM_CLOSE")
                    logger.warning(f"Spot ${spot:.2f} deviates {deviation_pct:.1%} from close ${close:.2f}")
                    has_sanity_issues = True
            
            # If sanity checks fail and cache is available and recent, prefer cache
            if has_sanity_issues and cached:
                logger.warning(f"Sanity checks failed, preferring cached spot")
                audit["cached_spot"] = cached["spot"]
                audit["cached_timestamp"] = cached["timestamp"]
                audit["rejected_live_spot"] = spot
                audit["rejected_live_source"] = source
                self.ib.cancelMktData(contract)
                return SpotResult(
                    ok=True,
                    spot=cached["spot"],
                    source="cached",
                    is_stale=True,
                    warnings=["SPOT_SANITY_FAIL_USED_CACHED"] + warnings,
                    reason=None,
                    audit=audit
                )
            
            self.ib.cancelMktData(contract)
            
            # Success - save to cache (but not if using stale close)
            if source != "close" and not is_stale:
                save_cached_spot(cache_key, spot, con_id, source)
            
            return SpotResult(
                ok=True,
                spot=spot,
                source=source,
                is_stale=is_stale,
                warnings=warnings,
                reason=None,
                audit=audit
            )
            
        except Exception as e:
            logger.error(f"Exception fetching market data: {e}")
            # Try cache on exception
            if cached:
                logger.info(f"Using cached spot as fallback after exception")
                audit["cached_spot"] = cached["spot"]
                audit["cached_timestamp"] = cached["timestamp"]
                audit["error"] = str(e)
                return SpotResult(
                    ok=True,
                    spot=cached["spot"],
                    source="cached",
                    is_stale=True,
                    warnings=["USED_CACHED_SPOT", "MARKET_DATA_EXCEPTION"],
                    reason=None,
                    audit=audit
                )
            return SpotResult(
                ok=False,
                spot=None,
                source=None,
                is_stale=False,
                warnings=[],
                reason="NO_VALID_PRICE",
                audit={"error": str(e)}
            )
    
    def get_option_chain_params(self, symbol: str) -> List[Dict]:
        """
        Get option chain parameters (available expiries and strikes).
        
        Args:
            symbol: Stock symbol
            
        Returns:
            List of chain parameter dicts
        """
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        
        chains = self.ib.reqSecDefOptParams(
            contract.symbol,
            "",
            contract.secType,
            contract.conId
        )
        
        if not chains:
            logger.error(f"No option chains found for {symbol}")
            return []
        
        # Use first exchange (usually has most complete data)
        chain = chains[0]
        
        return {
            "exchange": chain.exchange,
            "expirations": sorted(chain.expirations),
            "strikes": sorted(chain.strikes)
        }
    
    def filter_expiries(
        self,
        expiries: List[str],
        dte_min: int,
        dte_max: int,
        snapshot_time_utc: str
    ) -> List[str]:
        """
        Filter expiries by DTE range.
        
        Args:
            expiries: List of expiry strings (YYYYMMDD)
            dte_min: Minimum days to expiry
            dte_max: Maximum days to expiry
            snapshot_time_utc: Snapshot timestamp
            
        Returns:
            Filtered expiry list
        """
        snapshot_dt = datetime.fromisoformat(snapshot_time_utc.replace("Z", "+00:00"))
        
        filtered = []
        for expiry_str in expiries:
            expiry_dt = datetime.strptime(expiry_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            dte = (expiry_dt - snapshot_dt).days
            
            if dte_min <= dte <= dte_max:
                filtered.append(expiry_str)
        
        return sorted(filtered)
    
    def filter_strikes(
        self,
        all_strikes: List[float],
        spot: float,
        strikes_below: int = None,
        strikes_above: int = None,
        tail_moneyness_floor: float = None,
        min_strike: float = None
    ) -> tuple[List[float], Dict]:
        """
        Filter strikes with support for tail strike inclusion (crash venture).
        
        Args:
            all_strikes: All available strikes
            spot: Current underlier price
            strikes_below: Number of strikes below spot (legacy mode)
            strikes_above: Number of strikes above spot (legacy mode)
            tail_moneyness_floor: Tail moneyness floor (e.g., 0.20 means include down to S0*(1-0.20))
            min_strike: Explicit minimum strike floor
            
        Returns:
            Tuple of (filtered_strikes, tail_metadata)
        """
        strikes_sorted = sorted(all_strikes)
        tail_metadata = {}
        
        # Tail strike mode (for crash venture)
        if tail_moneyness_floor is not None or min_strike is not None:
            # Compute tail floor strike
            if min_strike is not None:
                tail_floor_strike = min_strike
                tail_metadata["tail_floor_strike"] = tail_floor_strike
                tail_metadata["tail_floor_source"] = "explicit_min_strike"
            else:
                # Compute from moneyness floor: S0 * (1 - tail_moneyness_floor)
                # DEFAULT: Use 0.20 (20%) if not specified for crash venture coverage
                raw_floor = spot * (1 - tail_moneyness_floor)
                
                # Round down to nearest $5 or $10
                # Use $5 rounding for strikes < 100, $10 for strikes >= 100
                if raw_floor < 100:
                    tail_floor_strike = (raw_floor // 5) * 5
                else:
                    tail_floor_strike = (raw_floor // 10) * 10
                
                tail_metadata["tail_floor_strike"] = tail_floor_strike
                tail_metadata["tail_moneyness_floor"] = tail_moneyness_floor
                tail_metadata["tail_floor_raw"] = raw_floor
                tail_metadata["tail_floor_source"] = "computed_from_moneyness"
            
            moneyness_str = f"{tail_moneyness_floor:.1%}" if tail_moneyness_floor is not None else "N/A"
            logger.info(f"Tail strike mode: floor=${tail_floor_strike:.2f}, spot=${spot:.2f}, moneyness_floor={moneyness_str}")
            
            # Build strike list:
            # a) tail band: from tail_floor_strike up to spot (puts)
            tail_band = [s for s in strikes_sorted if tail_floor_strike <= s < spot]
            
            # b) small band above spot for completeness (default: 10 strikes to cover ATM and calls)
            above_band = [s for s in strikes_sorted if s >= spot][:10]
            
            filtered_strikes = sorted(tail_band + above_band)
            
            # Validate tail_floor_strike coverage
            if filtered_strikes:
                actual_min_strike = min(filtered_strikes)
                if actual_min_strike > tail_floor_strike:
                    logger.warning(
                        f"INCOMPLETE: Requested tail floor ${tail_floor_strike:.2f} "
                        f"but lowest available strike is ${actual_min_strike:.2f}"
                    )
                    tail_metadata["incomplete"] = True
                    tail_metadata["requested_floor"] = tail_floor_strike
                    tail_metadata["actual_floor"] = actual_min_strike
                else:
                    tail_metadata["incomplete"] = False
                    tail_metadata["actual_floor"] = actual_min_strike
            else:
                logger.error("FAIL: No strikes returned in tail band")
                tail_metadata["incomplete"] = True
                tail_metadata["requested_floor"] = tail_floor_strike
                tail_metadata["actual_floor"] = None
            
            return filtered_strikes, tail_metadata
        
        # Legacy mode: use strikes_below/strikes_above
        # IMPROVED DEFAULT: Increase default coverage for crash venture
        else:
            if strikes_below is None:
                strikes_below = 60  # Increased from typical 30 for deeper OTM coverage
            if strikes_above is None:
                strikes_above = 10
            
            below = [s for s in strikes_sorted if s < spot][-strikes_below:]
            above = [s for s in strikes_sorted if s >= spot][:strikes_above]
            
            # Add metadata for legacy mode
            legacy_metadata = {
                "mode": "legacy",
                "strikes_below_requested": strikes_below,
                "strikes_above_requested": strikes_above,
                "strikes_below_actual": len(below),
                "strikes_above_actual": len(above)
            }
            
            return sorted(below + above), legacy_metadata
    
    def get_option_data_batch(
        self,
        symbol: str,
        expiry: str,
        strikes: List[float],
        return_diagnostics: bool = False
    ) -> Dict[str, Dict]:
        """
        Get option market data for all strikes in a batch (faster).
        
        Args:
            symbol: Underlier symbol
            expiry: Expiry date (YYYYMMDD)
            strikes: List of strike prices
            return_diagnostics: If True, return (results, diagnostics) tuple
            
        Returns:
            Dict mapping (strike, right) to option data, or (dict, diagnostics) if return_diagnostics=True
        """
        # Create all contracts
        contracts = []
        for strike in strikes:
            call_contract = Option(symbol, expiry, strike, "C", "SMART")
            put_contract = Option(symbol, expiry, strike, "P", "SMART")
            contracts.append(call_contract)
            contracts.append(put_contract)

        attempted_contracts = len(contracts)

        # Qualify all contracts - this modifies contracts in-place and returns them
        # Contracts that fail to qualify will NOT have a valid conId
        qualified_result = self.ib.qualifyContracts(*contracts)
        
        # Count how many contracts successfully qualified (have valid conId)
        qualified_contracts = [c for c in contracts if getattr(c, "conId", None) and c.conId > 0]
        qualified_count = len(qualified_contracts)
        unknown_contracts = attempted_contracts - qualified_count
        skipped_contracts = unknown_contracts  # Same as unknown for now

        logger.info(
            "Option contract qualification: attempted=%s, qualified=%s, unknown=%s, skipped=%s",
            attempted_contracts,
            qualified_count,
            unknown_contracts,
            skipped_contracts
        )
        
        # Request market data ONLY for qualified contracts
        tickers = []
        for contract in qualified_contracts:
            ticker = self.ib.reqMktData(contract, "", snapshot=True)
            tickers.append((contract, ticker))
        
        # Wait for all data
        self.ib.sleep(2)
        
        # Collect results
        results = {}
        for contract, ticker in tickers:
            # Get bid/ask
            bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
            ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
            last = ticker.last if ticker.last and ticker.last > 0 else None
            
            # Get model greeks if available
            iv = ticker.modelGreeks.impliedVol if ticker.modelGreeks else None
            delta = ticker.modelGreeks.delta if ticker.modelGreeks else None
            gamma = ticker.modelGreeks.gamma if ticker.modelGreeks else None
            vega = ticker.modelGreeks.vega if ticker.modelGreeks else None
            theta = ticker.modelGreeks.theta if ticker.modelGreeks else None
            
            # Get open interest from ticker
            open_interest = ticker.openInterest if hasattr(ticker, 'openInterest') and ticker.openInterest else None
            
            key = (contract.strike, contract.right)
            results[key] = {
                "strike": contract.strike,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": ticker.volume if ticker.volume else 0,
                "open_interest": open_interest,
                "implied_vol": iv,
                "delta": delta,
                "gamma": gamma,
                "vega": vega,
                "theta": theta
            }
            
            # Cancel market data
            self.ib.cancelMktData(contract)

        final_calls = len([key for key in results.keys() if key[1] == "C"])
        final_puts = len([key for key in results.keys() if key[1] == "P"])
        diagnostics = {
            "attempted_contracts": attempted_contracts,
            "qualified_contracts": qualified_count,
            "unknown_contracts": unknown_contracts,
            "skipped_contracts": skipped_contracts,
            "final_calls": final_calls,
            "final_puts": final_puts
        }

        if return_diagnostics:
            return results, diagnostics

        return results
    
    def export_snapshot(
        self,
        underlier: str,
        snapshot_time_utc: str,
        dte_min: int,
        dte_max: int,
        strikes_below: int = None,
        strikes_above: int = None,
        tail_moneyness_floor: float = None,
        min_strike: float = None,
        out_path: str = "snapshot.json"
    ):
        """
        Export option chain snapshot to JSON.
        
        Args:
            underlier: Stock symbol (e.g., "SPY")
            snapshot_time_utc: Snapshot timestamp (ISO format)
            dte_min: Minimum days to expiry
            dte_max: Maximum days to expiry
            strikes_below: Number of strikes below current price (legacy mode)
            strikes_above: Number of strikes above current price (legacy mode)
            tail_moneyness_floor: Tail moneyness floor for crash venture (e.g., 0.18)
            min_strike: Explicit minimum strike floor
            out_path: Output JSON file path
        """
        logger.info(f"Exporting snapshot for {underlier}")
        
        # Get underlier price (now returns SpotResult)
        spot_result = self.get_underlier_price(underlier)
        
        # Check if spot fetch failed
        if not spot_result.ok:
            error_msg = f"Failed to get spot price for {underlier}: {spot_result.reason}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        spot = spot_result.spot
        source = spot_result.source
        logger.info(f"Spot price: ${spot:.2f} (source: {source})")
        
        # Log warnings if any
        if spot_result.warnings:
            for warning in spot_result.warnings:
                logger.warning(f"Spot warning: {warning}")
        
        # Get option chain parameters
        chain_params = self.get_option_chain_params(underlier)
        
        # Filter expiries
        expiries = self.filter_expiries(
            chain_params["expirations"],
            dte_min,
            dte_max,
            snapshot_time_utc
        )
        logger.info(f"Found {len(expiries)} expiries in DTE range [{dte_min}, {dte_max}]")
        
        # Filter strikes (now returns strikes and tail metadata)
        strikes, tail_metadata = self.filter_strikes(
            chain_params["strikes"],
            spot,
            strikes_below=strikes_below,
            strikes_above=strikes_above,
            tail_moneyness_floor=tail_moneyness_floor,
            min_strike=min_strike
        )
        logger.info(f"Filtering to {len(strikes)} strikes")
        
        if not strikes:
            raise ValueError("No strikes found matching criteria")
        
        # SANITY ASSERTION: ATM strike must exist within +/- $5 of spot
        closest_strike = min(strikes, key=lambda s: abs(s - spot))
        atm_distance = abs(closest_strike - spot)
        if atm_distance > 5.0:
            error_msg = (
                f"SANITY CHECK FAILED: No ATM strike within $5 of spot ${spot:.2f}. "
                f"Closest strike is ${closest_strike:.2f} (distance: ${atm_distance:.2f}). "
                f"This likely indicates incorrect contract or bad data."
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.info(f"ATM strike validation passed: ${closest_strike:.2f} within ${atm_distance:.2f} of spot")
        
        # Build snapshot with spot audit trail and tail metadata
        snapshot = {
            "snapshot_metadata": {
                "underlier": underlier,
                "snapshot_time": snapshot_time_utc,
                "current_price": spot,
                "spot_source": spot_result.source,
                "spot_is_stale": spot_result.is_stale,
                "spot_warnings": spot_result.warnings,
                "spot_audit": spot_result.audit,
                "atm_strike": closest_strike,
                "atm_distance": atm_distance,
                "dte_min": dte_min,
                "dte_max": dte_max,
                "risk_free_rate": 0.0,  # User should update
                "dividend_yield": 0.0   # User should update
            },
            "expiries": {}
        }
        
        # Add tail metadata if present (for crash venture)
        if tail_metadata:
            snapshot["snapshot_metadata"]["tail_metadata"] = tail_metadata
            # Log warning if incomplete
            if tail_metadata.get("incomplete"):
                logger.warning(
                    f"⚠️  INCOMPLETE TAIL COVERAGE: Requested ${tail_metadata.get('requested_floor'):.2f}, "
                    f"got ${tail_metadata.get('actual_floor'):.2f}"
                )
        else:
            # Legacy mode metadata
            snapshot["snapshot_metadata"]["strikes_below"] = strikes_below
            snapshot["snapshot_metadata"]["strikes_above"] = strikes_above
        
        min_per_side = max(1, min(5, len(strikes) - 2))
        min_total = max(2, min(10, (len(strikes) * 2) - 4))

        snapshot["snapshot_metadata"]["option_contract_diagnostics"] = {
            "min_calls": min_per_side,
            "min_puts": min_per_side,
            "min_total": min_total,
            "expiries": {},
            "totals": {
                "attempted_contracts": 0,
                "qualified_contracts": 0,
                "unknown_contracts": 0,
                "skipped_contracts": 0,
                "final_calls": 0,
                "final_puts": 0
            }
        }

        # Fetch options for each expiry (batch mode for speed)
        for expiry in expiries:
            logger.info(f"Fetching options for expiry {expiry} ({len(strikes)} strikes)")
            
            # Get all option data in batch
            option_data, diagnostics = self.get_option_data_batch(
                underlier,
                expiry,
                strikes,
                return_diagnostics=True
            )

            snapshot["snapshot_metadata"]["option_contract_diagnostics"]["expiries"][expiry] = diagnostics
            totals = snapshot["snapshot_metadata"]["option_contract_diagnostics"]["totals"]
            totals["attempted_contracts"] += diagnostics["attempted_contracts"]
            totals["qualified_contracts"] += diagnostics["qualified_contracts"]
            totals["unknown_contracts"] += diagnostics["unknown_contracts"]
            totals["skipped_contracts"] += diagnostics["skipped_contracts"]
            totals["final_calls"] += diagnostics["final_calls"]
            totals["final_puts"] += diagnostics["final_puts"]

            total_options = diagnostics["final_calls"] + diagnostics["final_puts"]
            coverage_ok = (
                (diagnostics["final_calls"] >= min_per_side and diagnostics["final_puts"] >= min_per_side)
                or total_options >= min_total
            )
            logger.info(
                "Option coverage for %s: calls=%s puts=%s total=%s (min_calls=%s min_puts=%s min_total=%s)",
                expiry,
                diagnostics["final_calls"],
                diagnostics["final_puts"],
                total_options,
                min_per_side,
                min_per_side,
                min_total
            )
            if not coverage_ok:
                error_msg = (
                    "Insufficient qualified option coverage: "
                    f"attempted={diagnostics['attempted_contracts']}, "
                    f"qualified={diagnostics['qualified_contracts']}, "
                    f"unknown={diagnostics['unknown_contracts']}, "
                    f"skipped={diagnostics['skipped_contracts']}, "
                    f"final_calls={diagnostics['final_calls']}, "
                    f"final_puts={diagnostics['final_puts']}, "
                    f"min_calls={min_per_side}, min_puts={min_per_side}, min_total={min_total}."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
            
            expiry_data = {
                "expiry_date": expiry,
                "calls": [],
                "puts": []
            }
            
            # Organize into calls and puts
            for strike in strikes:
                if (strike, "C") in option_data:
                    expiry_data["calls"].append(option_data[(strike, "C")])
                if (strike, "P") in option_data:
                    expiry_data["puts"].append(option_data[(strike, "P")])
            
            # Sort by strike
            expiry_data["calls"].sort(key=lambda x: x["strike"])
            expiry_data["puts"].sort(key=lambda x: x["strike"])
            
            snapshot["expiries"][expiry] = expiry_data
            logger.info(f"  Fetched {len(expiry_data['calls'])} calls and {len(expiry_data['puts'])} puts")
        
        # Write to file
        with open(out_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        
        logger.info(f"Snapshot saved to {out_path}")
        
        return snapshot


def main():
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Export IBKR option chain snapshot to JSON"
    )
    parser.add_argument(
        "underlier",
        type=str,
        help="Underlier symbol (e.g., SPY, QQQ)"
    )
    parser.add_argument(
        "--snapshot-time",
        type=str,
        default=None,
        help="Snapshot time (ISO format, default: now)"
    )
    parser.add_argument(
        "--dte-min",
        type=int,
        default=20,
        help="Minimum days to expiry (default: 20)"
    )
    parser.add_argument(
        "--dte-max",
        type=int,
        default=60,
        help="Maximum days to expiry (default: 60)"
    )
    parser.add_argument(
        "--strikes-below",
        type=int,
        default=None,
        help="Number of strikes below spot (legacy mode, default: None)"
    )
    parser.add_argument(
        "--strikes-above",
        type=int,
        default=None,
        help="Number of strikes above spot (legacy mode, default: None)"
    )
    parser.add_argument(
        "--tail-moneyness-floor",
        type=float,
        default=None,
        help="Tail moneyness floor for crash venture (e.g., 0.18 for 18%% drawdown). Default for crash venture: 0.18"
    )
    parser.add_argument(
        "--min-strike",
        type=float,
        default=None,
        help="Explicit minimum strike floor (alternative to --tail-moneyness-floor)"
    )
    parser.add_argument(
        "--out",
        type=str,
        default="snapshot.json",
        help="Output JSON file (default: snapshot.json)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="TWS/Gateway host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7496,
        help="TWS/Gateway port (default: 7496 for live)"
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=1,
        help="IBKR client ID (default: 1)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Default snapshot time
    if args.snapshot_time is None:
        snapshot_time = datetime.now(timezone.utc).isoformat()
    else:
        snapshot_time = args.snapshot_time
    
    # Create exporter
    exporter = IBKRSnapshotExporter(
        host=args.host,
        port=args.port,
        client_id=args.client_id
    )
    
    try:
        # Connect
        exporter.connect()
        
        # Validate parameters
        has_legacy = args.strikes_below is not None or args.strikes_above is not None
        has_tail = args.tail_moneyness_floor is not None or args.min_strike is not None
        
        if not has_legacy and not has_tail:
            # Default to legacy mode with 10 strikes each direction
            logger.info("Using default legacy mode: 10 strikes below, 10 strikes above")
            args.strikes_below = 10
            args.strikes_above = 10
        elif has_legacy and has_tail:
            logger.error("Cannot specify both legacy mode (--strikes-below/above) and tail mode (--tail-moneyness-floor/--min-strike)")
            print("\n❌ Error: Cannot use both legacy and tail strike modes simultaneously")
            return
        
        # Export snapshot
        exporter.export_snapshot(
            underlier=args.underlier,
            snapshot_time_utc=snapshot_time,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            strikes_below=args.strikes_below,
            strikes_above=args.strikes_above,
            tail_moneyness_floor=args.tail_moneyness_floor,
            min_strike=args.min_strike,
            out_path=args.out
        )
        
        print(f"\n✅ Snapshot exported to {args.out}")
        
    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        print(f"\n❌ Export failed: {e}")
    
    finally:
        # Always disconnect
        exporter.disconnect()


if __name__ == "__main__":
    main()
