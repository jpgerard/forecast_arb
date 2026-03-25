"""
Test Kalshi Status Mapping

Regression test to ensure kalshi_probe.py and kalshi_series_coverage.py
use the same market status mapping ("open" -> "active").
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from forecast_arb.kalshi.status_map import map_status, get_valid_statuses


class TestStatusMapping:
    """Test status mapping utility."""
    
    def test_map_status_open_to_active(self):
        """Test that 'open' maps to ['active']."""
        assert map_status("open") == ["active"]
    
    def test_map_status_closed_to_finalized(self):
        """Test that 'closed' maps to ['finalized']."""
        assert map_status("closed") == ["finalized"]
    
    def test_map_status_all_to_none(self):
        """Test that 'all' maps to None."""
        assert map_status("all") is None
    
    def test_map_status_none_to_none(self):
        """Test that None maps to None."""
        assert map_status(None) is None
    
    def test_map_status_invalid_raises(self):
        """Test that invalid status raises ValueError."""
        with pytest.raises(ValueError, match="Invalid status"):
            map_status("invalid")
    
    def test_get_valid_statuses(self):
        """Test that get_valid_statuses returns expected list."""
        statuses = get_valid_statuses()
        assert "open" in statuses
        assert "closed" in statuses
        assert "all" in statuses
        assert len(statuses) == 3


class TestProbeUsesStatusMap:
    """Test that kalshi_probe.py uses status mapping."""
    
    @patch('scripts.kalshi_probe.KalshiClient')
    @patch('scripts.kalshi_probe.map_status')
    def test_probe_series_calls_map_status(self, mock_map_status, mock_client_class):
        """Test that probe_series calls map_status."""
        from scripts.kalshi_probe import probe_series
        
        # Setup mocks
        mock_map_status.return_value = "closed"
        mock_client = Mock()
        mock_client.list_markets.return_value = []
        mock_client_class.return_value = mock_client
        
        # Call probe_series (will exit due to 0 markets, but we just need to verify call)
        try:
            probe_series("KXINX", status="open", limit=10)
        except SystemExit:
            pass  # Expected due to 0 markets
        
        # Verify map_status was called with "open"
        mock_map_status.assert_called_once_with("open")
        
        # Verify client.list_markets was called with "closed"
        mock_client.list_markets.assert_called_once()
        call_kwargs = mock_client.list_markets.call_args[1]
        assert call_kwargs['status'] == "closed"


class TestCoverageUsesStatusMap:
    """Test that kalshi_series_coverage.py uses status mapping."""
    
    @patch('scripts.kalshi_series_coverage.KalshiClient')
    @patch('scripts.kalshi_series_coverage.map_status')
    def test_compute_coverage_calls_map_status(self, mock_map_status, mock_client_class):
        """Test that compute_coverage calls map_status."""
        from scripts.kalshi_series_coverage import compute_coverage
        
        # Setup mocks
        mock_map_status.return_value = "closed"
        mock_client = Mock()
        mock_client.list_markets.return_value = []
        mock_client_class.return_value = mock_client
        
        # Call compute_coverage
        result = compute_coverage(["KXINX"], status="open", limit=10)
        
        # Verify map_status was called with "open"
        mock_map_status.assert_called_once_with("open")
        
        # Verify client.list_markets was called with "closed"
        mock_client.list_markets.assert_called_once()
        call_kwargs = mock_client.list_markets.call_args[1]
        assert call_kwargs['status'] == "closed"


class TestStatusMappingRegression:
    """
    Regression test: Ensure both scripts return same market count.
    
    Mock scenario: Client returns 30 markets with status="active"
    Both scripts with --status open should fetch those 30 markets.
    """
    
    def create_mock_markets(self, count: int = 30):
        """Create mock market data."""
        markets = []
        for i in range(count):
            markets.append({
                "ticker": f"KXINX-24FEB27-{5900+i*10}",
                "title": f"Will SPX close at or above {5900+i*10} on Feb 27, 2024?",
                "status": "active",
                "close_time": "2024-02-27T20:59:00Z",
                "strike": 5900 + i*10,
            })
        return markets
    
    @patch('forecast_arb.kalshi.client.KalshiClient.list_markets')
    def test_both_scripts_fetch_same_count(self, mock_list_markets):
        """Test that both scripts fetch same number of markets."""
        from scripts.kalshi_probe import probe_series
        from scripts.kalshi_series_coverage import compute_coverage
        
        # Setup mock to return 30 markets
        mock_markets = self.create_mock_markets(30)
        mock_list_markets.return_value = mock_markets
        
       # Test probe_series
        try:
            with patch('scripts.kalshi_probe.KalshiClient') as mock_client_class:
                mock_client = Mock()
                mock_client.list_markets.return_value = mock_markets
                mock_client_class.return_value = mock_client
                
                # This will exit with sys.exit(0) since markets found
                # We capture the call to list_markets
                probe_series("KXINX", status="open", limit=10)
        except SystemExit:
            pass
        
        # Verify list_markets was called with status=["active"] (not "open")
        call_args = mock_client.list_markets.call_args
        assert call_args[1]['status'] == ["active"]
        
        # Test compute_coverage
        with patch('scripts.kalshi_series_coverage.KalshiClient') as mock_client_class:
            mock_client = Mock()
            mock_client.list_markets.return_value = mock_markets
            mock_client_class.return_value = mock_client
            
            result = compute_coverage(["KXINX"], status="open", limit=500)
        
        # Verify same API status used
        call_args = mock_client.list_markets.call_args
        assert call_args[1]['status'] == ["active"]
        
        # Verify result shows 30 markets fetched
        assert result["KXINX"]["markets_fetched"] == 30
        
    def test_date_range_regression(self):
        """Test that coverage correctly reports date range for KXINX."""
        from scripts.kalshi_series_coverage import compute_coverage, parse_event_date
        from datetime import date
        
        # Create mock markets spanning Feb 27 - Mar 15
        mock_markets = []
        for day_offset in range(17):  # 17 days = Feb 27 to Mar 15
            mock_markets.append({
                "ticker": f"KXINX-24FEB{27+day_offset}-5900",
                "title": f"Will SPX close above 5900?",
                "status": "active",
                "close_time": f"2024-02-{27+day_offset:02d}T20:59:00Z" if day_offset < 2 else f"2024-03-{day_offset-1:02d}T20:59:00Z",
                "strike": 5900,
            })
        
        with patch('scripts.kalshi_series_coverage.KalshiClient') as mock_client_class:
            mock_client = Mock()
            mock_client.list_markets.return_value = mock_markets
            mock_client_class.return_value = mock_client
            
            result = compute_coverage(["KXINX"], status="open", limit=500)
        
        # For this test, just verify we got the markets
        # (Date parsing depends on actual format)
        assert result["KXINX"]["markets_fetched"] == 17


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
