"""
Tests for IBKR snapshot unknown contract handling.
"""

import pytest

from forecast_arb.data.ibkr_snapshot import IBKRSnapshotExporter


def _build_stub_exporter(option_data_callback):
    exporter = IBKRSnapshotExporter.__new__(IBKRSnapshotExporter)
    exporter.get_underlier_price = lambda underlier: {
        "spot": 500.0,
        "source": "last",
        "is_stale": False,
        "raw_last": 500.0,
        "raw_bid": 499.5,
        "raw_ask": 500.5,
        "raw_close": 498.0,
        "raw_market_price": 500.0
    }
    exporter.get_option_chain_params = lambda underlier: {
        "exchange": "SMART",
        "expirations": ["20260228"],
        "strikes": [480.0, 490.0, 500.0, 510.0, 520.0, 530.0]
    }
    exporter.filter_expiries = lambda expiries, dte_min, dte_max, snapshot_time_utc: expiries
    exporter.filter_strikes = lambda strikes, spot, **kwargs: (strikes, {})
    exporter.get_option_data_batch = option_data_callback
    return exporter


def _build_option_payload(strikes, calls, puts):
    payload = {}
    for strike in strikes[:calls]:
        payload[(strike, "C")] = {
            "strike": strike,
            "bid": 1.0,
            "ask": 1.1,
            "last": 1.05,
            "volume": 0,
            "open_interest": None,
            "implied_vol": None,
            "delta": None,
            "gamma": None,
            "vega": None,
            "theta": None
        }
    for strike in strikes[:puts]:
        payload[(strike, "P")] = {
            "strike": strike,
            "bid": 1.0,
            "ask": 1.1,
            "last": 1.05,
            "volume": 0,
            "open_interest": None,
            "implied_vol": None,
            "delta": None,
            "gamma": None,
            "vega": None,
            "theta": None
        }
    return payload


def test_unknown_contracts_skipped_with_coverage(tmp_path):
    strikes = [480.0, 490.0, 500.0, 510.0, 520.0, 530.0]

    def option_data_callback(symbol, expiry, strikes_arg, return_diagnostics=False):
        option_data = _build_option_payload(strikes_arg, calls=4, puts=4)
        diagnostics = {
            "attempted_contracts": len(strikes_arg) * 2,
            "qualified_contracts": 8,
            "unknown_contracts": (len(strikes_arg) * 2) - 8,
            "skipped_contracts": (len(strikes_arg) * 2) - 8,
            "final_calls": 4,
            "final_puts": 4
        }
        if return_diagnostics:
            return option_data, diagnostics
        return option_data

    exporter = _build_stub_exporter(option_data_callback)
    snapshot = exporter.export_snapshot(
        underlier="SPY",
        snapshot_time_utc="2026-01-28T00:00:00Z",
        dte_min=20,
        dte_max=60,
        strikes_below=3,
        strikes_above=3,
        out_path=str(tmp_path / "snapshot.json")
    )

    diagnostics = snapshot["snapshot_metadata"]["option_contract_diagnostics"]["totals"]
    assert diagnostics["unknown_contracts"] > 0
    expiry_data = snapshot["expiries"]["20260228"]
    assert len(expiry_data["calls"]) == 4
    assert len(expiry_data["puts"]) == 4


def test_unknown_contracts_below_coverage_raises(tmp_path):
    def option_data_callback(symbol, expiry, strikes_arg, return_diagnostics=False):
        option_data = _build_option_payload(strikes_arg, calls=1, puts=1)
        diagnostics = {
            "attempted_contracts": len(strikes_arg) * 2,
            "qualified_contracts": 2,
            "unknown_contracts": (len(strikes_arg) * 2) - 2,
            "skipped_contracts": (len(strikes_arg) * 2) - 2,
            "final_calls": 1,
            "final_puts": 1
        }
        if return_diagnostics:
            return option_data, diagnostics
        return option_data

    exporter = _build_stub_exporter(option_data_callback)

    with pytest.raises(ValueError, match="Insufficient qualified option coverage") as excinfo:
        exporter.export_snapshot(
            underlier="SPY",
            snapshot_time_utc="2026-01-28T00:00:00Z",
            dte_min=20,
            dte_max=60,
            strikes_below=3,
            strikes_above=3,
            out_path=str(tmp_path / "snapshot.json")
        )

    message = str(excinfo.value)
    assert "attempted=" in message
    assert "qualified=" in message
    assert "unknown=" in message
    assert "skipped=" in message
    assert "final_calls=" in message
    assert "final_puts=" in message


if __name__ == "__main__":
    pytest.main([__file__, "-v"])