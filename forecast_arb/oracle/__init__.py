"""Oracle module for treating Kalshi probabilities as ground truth."""

from .kalshi_oracle import KalshiOracle, collect_oracle_data

__all__ = ["KalshiOracle", "collect_oracle_data"]
