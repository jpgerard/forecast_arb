"""
Repository Hygiene Tests

Ensures the refactored structure is maintained:
- Single canonical daily runner exists
- No duplicate runners
- Canonical package layout
- No imports from deprecated locations (in new code)
"""

import pytest
from pathlib import Path
import sys
import importlib
import warnings


def test_canonical_runner_exists():
    """scripts/run_daily.py must exist as the canonical runner."""
    runner_path = Path(__file__).parent.parent / "scripts" / "run_daily.py"
    assert runner_path.exists(), "scripts/run_daily.py must exist as canonical daily runner"


def test_no_duplicate_runners_in_examples():
    """examples/ directory should not contain runners - they should be in scripts/."""
    examples_dir = Path(__file__).parent.parent / "examples"
    
    if not examples_dir.exists():
        # If examples dir doesn't exist, that's fine (removed)
        return
    
    # Check for runner scripts
    runner_patterns = ["run_*.py"]
    found_runners = []
    
    for pattern in runner_patterns:
        found_runners.extend(examples_dir.glob(pattern))
    
    assert len(found_runners) == 0, (
        f"Found runner scripts in examples/: {[r.name for r in found_runners]}. "
        "All runners should be in scripts/ directory."
    )


def test_core_package_exists():
    """forecast_arb.core package must exist and be importable."""
    try:
        import forecast_arb.core
        assert hasattr(forecast_arb.core, 'ManifestWriter'), "ManifestWriter should be in core"
        assert hasattr(forecast_arb.core, 'ensure_dir'), "ensure_dir should be in core"
        assert hasattr(forecast_arb.core, 'write_json'), "write_json should be in core"
    except ImportError as e:
        pytest.fail(f"forecast_arb.core package not importable: {e}")


def test_ibkr_snapshot_in_correct_location():
    """IBKRSnapshotExporter should be in forecast_arb.ibkr.snapshot, not data."""
    snapshot_path = Path(__file__).parent.parent / "forecast_arb" / "ibkr" / "snapshot.py"
    assert snapshot_path.exists(), "forecast_arb/ibkr/snapshot.py must exist"
    
    # Verify it has the main class
    try:
        from forecast_arb.ibkr.snapshot import IBKRSnapshotExporter
        assert IBKRSnapshotExporter is not None
    except ImportError as e:
        pytest.fail(f"Cannot import IBKRSnapshotExporter from forecast_arb.ibkr.snapshot: {e}")


def test_manifest_in_core():
    """ManifestWriter should be in forecast_arb.core.manifest."""
    manifest_path = Path(__file__).parent.parent / "forecast_arb" / "core" / "manifest.py"
    assert manifest_path.exists(), "forecast_arb/core/manifest.py must exist"
    
    try:
        from forecast_arb.core.manifest import ManifestWriter, compute_config_checksum
        assert ManifestWriter is not None
        assert compute_config_checksum is not None
    except ImportError as e:
        pytest.fail(f"Cannot import from forecast_arb.core.manifest: {e}")


def test_compatibility_stubs_exist():
    """Compatibility stubs should exist for gradual migration."""
    # Check data.ibkr_snapshot stub
    data_stub = Path(__file__).parent.parent / "forecast_arb" / "data" / "ibkr_snapshot.py"
    assert data_stub.exists(), "forecast_arb/data/ibkr_snapshot.py compatibility stub must exist"
    
    # Check utils.manifest stub
    utils_stub = Path(__file__).parent.parent / "forecast_arb" / "utils" / "manifest.py"
    assert utils_stub.exists(), "forecast_arb/utils/manifest.py compatibility stub must exist"
    
    # Verify stubs emit deprecation warnings
    # Important: Remove from sys.modules to avoid import caching issues
    # and ensure DeprecationWarning is not filtered
    
    # Test forecast_arb.data.ibkr_snapshot
    if "forecast_arb.data.ibkr_snapshot" in sys.modules:
        del sys.modules["forecast_arb.data.ibkr_snapshot"]
    
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always", DeprecationWarning)
        importlib.import_module("forecast_arb.data.ibkr_snapshot")
        
        # Verify at least one DeprecationWarning was emitted
        deprecation_warnings = [warning for warning in w if issubclass(warning.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0, "Expected DeprecationWarning from forecast_arb.data.ibkr_snapshot"
        
        # Check the warning message mentions deprecation
        warning_messages = [str(warning.message) for warning in deprecation_warnings]
        assert any("deprecated" in msg.lower() for msg in warning_messages), \
            f"Warning should mention 'deprecated': {warning_messages}"
    
    # Test forecast_arb.utils.manifest
    if "forecast_arb.utils.manifest" in sys.modules:
        del sys.modules["forecast_arb.utils.manifest"]
    
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always", DeprecationWarning)
        importlib.import_module("forecast_arb.utils.manifest")
        
        # Verify at least one DeprecationWarning was emitted
        deprecation_warnings = [warning for warning in w if issubclass(warning.category, DeprecationWarning)]
        assert len(deprecation_warnings) > 0, "Expected DeprecationWarning from forecast_arb.utils.manifest"
        
        # Check the warning message mentions deprecation
        warning_messages = [str(warning.message) for warning in deprecation_warnings]
        assert any("deprecated" in msg.lower() for msg in warning_messages), \
            f"Warning should mention 'deprecated': {warning_messages}"


def test_scripts_directory_clean():
    """scripts/ should only contain run_daily.py and kalshi_smoke.py."""
    scripts_dir = Path(__file__).parent.parent / "scripts"
    
    if not scripts_dir.exists():
        pytest.fail("scripts/ directory must exist")
    
    allowed_scripts = {"run_daily.py", "kalshi_smoke.py", "__init__.py", "run_real_cycle.py"}
    
    python_files = list(scripts_dir.glob("*.py"))
    script_names = {f.name for f in python_files}
    
    unexpected = script_names - allowed_scripts
    
    # Allow run_real_cycle.py temporarily during transition
    if unexpected:
        # Filter out run_real_cycle.py if it's the only unexpected file
        if unexpected == {"run_real_cycle.py"}:
            # This is acceptable during transition
            pass
        else:
            pytest.fail(
                f"Unexpected scripts in scripts/: {unexpected}. "
                f"Only {allowed_scripts} are allowed."
            )


def test_core_artifacts_module_exists():
    """forecast_arb.core.artifacts must provide centralized I/O helpers."""
    try:
        from forecast_arb.core.artifacts import (
            ensure_dir,
            write_json,
            write_yaml,
            write_text,
            read_json,
            read_yaml
        )
        assert ensure_dir is not None
        assert write_json is not None
        assert write_yaml is not None
        assert write_text is not None
        assert read_json is not None
        assert read_yaml is not None
    except ImportError as e:
        pytest.fail(f"Cannot import from forecast_arb.core.artifacts: {e}")


def test_expected_package_structure():
    """Verify the canonical package layout exists."""
    base = Path(__file__).parent.parent / "forecast_arb"
    
    expected_packages = [
        "core",
        "ibkr",
        "kalshi",
        "options",
        "oracle",
        "gating",
        "structuring",
        "engine",
        "utils"
    ]
    
    for pkg in expected_packages:
        pkg_path = base / pkg
        assert pkg_path.exists() and pkg_path.is_dir(), f"Package {pkg} must exist"
        
        init_file = pkg_path / "__init__.py"
        assert init_file.exists(), f"Package {pkg} must have __init__.py"
