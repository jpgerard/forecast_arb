"""
DEPRECATED: This module has moved to forecast_arb.core.manifest

This is a compatibility stub that will be removed in a future version.
Please update your imports:
    from forecast_arb.core.manifest import ManifestWriter, compute_config_checksum
"""

import warnings

# Emit deprecation warning on import
warnings.warn(
    "forecast_arb.utils.manifest is deprecated and will be removed in a future version. "
    "Please use forecast_arb.core.manifest instead.",
    DeprecationWarning,
    stacklevel=2
)

# Re-export from new location
from forecast_arb.core.manifest import (
    compute_config_checksum,
    ManifestWriter
)

__all__ = ["compute_config_checksum", "ManifestWriter"]
