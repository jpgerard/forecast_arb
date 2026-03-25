"""
forecast_arb.core - Core infrastructure and utilities

This package contains cross-cutting concerns:
- Manifest management and run tracking
- Artifact I/O (JSON, YAML, text)
- Logging configuration
- Config loading
- Shared runner logic
"""

from forecast_arb.core.manifest import ManifestWriter, compute_config_checksum
from forecast_arb.core.artifacts import (
    ensure_dir,
    write_json,
    write_yaml,
    write_text,
    read_json,
    read_yaml
)

__all__ = [
    "ManifestWriter",
    "compute_config_checksum",
    "ensure_dir",
    "write_json",
    "write_yaml",
    "write_text",
    "read_json",
    "read_yaml",
]
