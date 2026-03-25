"""Option structuring module for converting probability forecasts into trade structures."""

from .templates import generate_put_spread, generate_call_spread, generate_strangle, compute_payoff
from .option_math import compute_iv, compute_greeks, price_option
from .calibrator import calibrate_distribution, calibrate_drift, lognormal_cdf
from .evaluator import evaluate_structure, simulate_paths, compute_statistics
from .router import choose_best_structure

__all__ = [
    "generate_put_spread",
    "generate_call_spread",
    "generate_strangle",
    "compute_payoff",
    "compute_iv",
    "compute_greeks",
    "price_option",
    "calibrate_distribution",
    "calibrate_drift",
    "lognormal_cdf",
    "evaluate_structure",
    "simulate_paths",
    "compute_statistics",
    "choose_best_structure",
]
