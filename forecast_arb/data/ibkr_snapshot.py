"""
DEPRECATED: This module has moved to forecast_arb.ibkr.snapshot

This is a compatibility stub that will be removed in a future version.
Please update your imports:
    from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
"""

import warnings

# Emit deprecation warning on import
warnings.warn(
    "forecast_arb.data.ibkr_snapshot is deprecated and will be removed in a future version. "
    "Please use forecast_arb.ibkr.snapshot instead.",
    DeprecationWarning,
    stacklevel=2
)

# Re-export everything from new location for backward compatibility
from forecast_arb.ibkr.snapshot import *

__all__ = ["IBKRSnapshotExporter"]
