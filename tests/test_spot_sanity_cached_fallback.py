"""
Test SpotResult and cached fallback behavior.
"""

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from forecast_arb.ibkr.types import SpotResult
from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter


def create_mock_ticker(last=None, bid=None, ask=None, close=None):
    """Create a mock ticker object."""
    ticker = MagicMock()
    ticker.last = last
    ticker.bid = bid
    ticker.ask = ask
    ticker.close = close
    ticker.marketPrice.return_value = None
    ticker.modelGreeks = None
    return ticker


def test_spot_result_success_from_last():
    """Test successful spot fetch from last price."""
    exporter = IBKRSnapshotExporter()
    
    # Mock IBKR connection
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=None), \
         patch('forecast_arb.data.ibkr_snapshot.save_cached_spot') as mock_save:
        
        # Setup qualified contract
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with last price
        mock_req.return_value = create_mock_ticker(last=450.50, bid=450.45, ask=450.55, close=449.00)
        
        # Get underlier price
        result = exporter.get_underlier_price("SPY")
        
        # Verify result
        assert result.ok is True
        assert result.spot == 450.50
        assert result.source == "last"
        assert result.is_stale is False
        assert result.reason is None
        
        # Verify cache was saved
        mock_save.assert_called_once()


def test_spot_result_fallback_to_midpoint():
    """Test fallback to midpoint when last is unavailable."""
    exporter = IBKRSnapshotExporter()
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=None), \
         patch('forecast_arb.data.ibkr_snapshot.save_cached_spot') as mock_save:
        
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with only bid/ask (no last)
        mock_req.return_value = create_mock_ticker(last=None, bid=450.45, ask=450.55, close=449.00)
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is True
        assert result.spot == 450.50  # (450.45 + 450.55) / 2
        assert result.source == "midpoint"
        assert result.is_stale is False


def test_spot_result_fallback_to_close():
    """Test fallback to close (stale) when last and bid/ask unavailable."""
    exporter = IBKRSnapshotExporter()
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=None), \
         patch('forecast_arb.data.ibkr_snapshot.save_cached_spot') as mock_save:
        
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with only close
        mock_req.return_value = create_mock_ticker(last=None, bid=None, ask=None, close=449.00)
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is True
        assert result.spot == 449.00
        assert result.source == "close"
        assert result.is_stale is True
        assert "USING_STALE_CLOSE" in result.warnings
        
        # Close should NOT be cached
        mock_save.assert_not_called()


def test_spot_result_uses_cache_when_no_price():
    """Test cached spot is used when no valid price available."""
    exporter = IBKRSnapshotExporter()
    
    cached_spot = {
        "spot": 448.00,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "last",
        "conId": 12345
    }
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=cached_spot):
        
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with NO valid prices
        mock_req.return_value = create_mock_ticker(last=None, bid=None, ask=None, close=None)
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is True
        assert result.spot == 448.00
        assert result.source == "cached"
        assert result.is_stale is True
        assert "USED_CACHED_SPOT" in result.warnings
        assert "NO_VALID_LIVE_PRICE" in result.warnings


def test_spot_result_fails_when_no_price_and_no_cache():
    """Test failure when no valid price and no cache."""
    exporter = IBKRSnapshotExporter()
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=None):
        
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with NO valid prices
        mock_req.return_value = create_mock_ticker(last=None, bid=None, ask=None, close=None)
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is False
        assert result.spot is None
        assert result.reason == "NO_VALID_PRICE"


def test_spot_result_large_deviation_uses_cache():
    """Test that large deviation from close triggers cached fallback."""
    exporter = IBKRSnapshotExporter()
    
    cached_spot = {
        "spot": 450.00,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "last",
        "conId": 12345
    }
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=cached_spot):
        
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with last price that deviates >10% from close
        # last=500, close=450 -> 11% deviation
        mock_req.return_value = create_mock_ticker(last=500.00, bid=499.00, ask=501.00, close=450.00)
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is True
        assert result.spot == 450.00  # Uses cached spot
        assert result.source == "cached"
        assert "SPOT_SANITY_FAIL_USED_CACHED" in result.warnings
        assert "LARGE_DEVIATION_FROM_CLOSE" in result.warnings


def test_spot_result_last_outside_spread_uses_cache():
    """Test that last price outside bid/ask spread triggers cached fallback."""
    exporter = IBKRSnapshotExporter()
    
    cached_spot = {
        "spot": 450.00,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "last",
        "conId": 12345
    }
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch.object(exporter.ib, 'reqMktData') as mock_req, \
         patch.object(exporter.ib, 'sleep'), \
         patch.object(exporter.ib, 'cancelMktData'), \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=cached_spot):
        
        mock_contract = MagicMock()
        mock_contract.conId = 12345
        mock_contract.symbol = "SPY"
        mock_qualify.return_value = [mock_contract]
        
        # Setup ticker with last outside bid/ask spread
        # bid=450, ask=451, last=460 (outside spread)
        mock_req.return_value = create_mock_ticker(last=460.00, bid=450.00, ask=451.00, close=449.00)
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is True
        assert result.spot == 450.00  # Uses cached spot
        assert result.source == "cached"
        assert "SPOT_SANITY_FAIL_USED_CACHED" in result.warnings
        assert "LAST_OUTSIDE_SPREAD" in result.warnings


def test_spot_result_contract_mismatch_uses_cache():
    """Test that contract mismatch uses cache if available."""
    exporter = IBKRSnapshotExporter()
    
    cached_spot = {
        "spot": 450.00,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "last",
        "conId": 12345
    }
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=cached_spot):
        
        # Contract qualification returns wrong symbol
        mock_contract = MagicMock()
        mock_contract.conId = 99999
        mock_contract.symbol = "SPYG"  # Wrong symbol!
        mock_qualify.return_value = [mock_contract]
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is True
        assert result.spot == 450.00  # Uses cached spot
        assert result.source == "cached"
        assert "USED_CACHED_SPOT" in result.warnings
        assert "CONTRACT_SYMBOL_MISMATCH" in result.warnings


def test_spot_result_contract_mismatch_fails_without_cache():
    """Test that contract mismatch fails without cache."""
    exporter = IBKRSnapshotExporter()
    
    with patch.object(exporter.ib, 'qualifyContracts') as mock_qualify, \
         patch('forecast_arb.data.ibkr_snapshot.load_cached_spot', return_value=None):
        
        # Contract qualification returns wrong symbol
        mock_contract = MagicMock()
        mock_contract.conId = 99999
        mock_contract.symbol = "SPYG"
        mock_qualify.return_value = [mock_contract]
        
        result = exporter.get_underlier_price("SPY")
        
        assert result.ok is False
        assert result.spot is None
        assert result.reason == "CONTRACT_MISMATCH"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
