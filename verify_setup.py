#!/usr/bin/env python
"""
Verify Setup Script

Checks if all dependencies and configurations are properly set up
for running forecast_arb locally.
"""

import sys
import os
from pathlib import Path

def check_python_version():
    """Check if Python version is 3.10 or higher."""
    print("Checking Python version...")
    version = sys.version_info
    if version.major >= 3 and version.minor >= 10:
        print(f"  ✓ Python {version.major}.{version.minor}.{version.micro} (compatible)")
        return True
    else:
        print(f"  ✗ Python {version.major}.{version.minor}.{version.micro} (requires 3.10+)")
        return False

def check_required_packages():
    """Check if required Python packages are installed."""
    print("\nChecking required packages...")
    
    required = [
        'requests', 'bs4', 'pydantic', 'yaml', 'pandas', 
        'numpy', 'dateutil', 'jsonschema', 'dotenv', 
        'cryptography', 'py_vollib', 'scipy'
    ]
    
    missing = []
    for package in required:
        try:
            if package == 'bs4':
                __import__('bs4')
            elif package == 'yaml':
                __import__('yaml')
            elif package == 'dateutil':
                __import__('dateutil')
            elif package == 'dotenv':
                __import__('dotenv')
            else:
                __import__(package)
            print(f"  ✓ {package}")
        except ImportError:
            print(f"  ✗ {package} (missing)")
            missing.append(package)
    
    if missing:
        print(f"\n  Missing packages: {', '.join(missing)}")
        print("  Install with: pip install -r requirements.txt")
        return False
    return True

def check_optional_packages():
    """Check optional packages."""
    print("\nChecking optional packages...")
    
    optional = {
        'ib_insync': 'For IBKR live data (optional)',
        'pytest': 'For running tests (dev only)',
    }
    
    for package, description in optional.items():
        try:
            __import__(package)
            print(f"  ✓ {package} - {description}")
        except ImportError:
            print(f"  ○ {package} - {description} (not installed)")

def check_env_file():
    """Check if .env file exists."""
    print("\nChecking environment configuration...")
    
    env_path = Path('.env')
    env_example_path = Path('.env.example')
    
    if env_path.exists():
        print("  ✓ .env file exists")
        return True
    else:
        print("  ✗ .env file not found")
        if env_example_path.exists():
            print("  → Copy .env.example to .env and configure")
        return False

def check_env_variables():
    """Check if environment variables are configured."""
    print("\nChecking environment variables...")
    
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except:
        print("  ⚠ Could not load .env file")
        return False
    
    # Check Kalshi credentials (optional but recommended)
    kalshi_api_key = os.getenv('KALSHI_API_KEY_ID')
    kalshi_private_key = os.getenv('KALSHI_PRIVATE_KEY_PATH')
    
    if kalshi_api_key and kalshi_api_key != 'your_api_key_id_here':
        print("  ✓ KALSHI_API_KEY_ID configured")
    else:
        print("  ○ KALSHI_API_KEY_ID not configured (optional for review-only)")
    
    if kalshi_private_key and kalshi_private_key != '/path/to/your/private_key.pem':
        if Path(kalshi_private_key).exists():
            print("  ✓ KALSHI_PRIVATE_KEY_PATH configured and file exists")
        else:
            print("  ⚠ KALSHI_PRIVATE_KEY_PATH configured but file not found")
    else:
        print("  ○ KALSHI_PRIVATE_KEY_PATH not configured (optional for review-only)")
    
    # Check IBKR settings (optional)
    ibkr_port = os.getenv('IBKR_PORT')
    if ibkr_port:
        print(f"  ✓ IBKR_PORT configured ({ibkr_port})")
    else:
        print("  ○ IBKR_PORT not configured (optional, only for live snapshots)")
    
    return True

def check_module_import():
    """Check if forecast_arb module can be imported."""
    print("\nChecking forecast_arb module...")
    
    try:
        import forecast_arb
        print("  ✓ forecast_arb module can be imported")
        return True
    except ImportError as e:
        print(f"  ✗ Cannot import forecast_arb: {e}")
        print("  → Ensure you're in the project root directory")
        return False

def check_snapshots():
    """Check if sample snapshots exist."""
    print("\nChecking sample snapshots...")
    
    snapshots_dir = Path('snapshots')
    if not snapshots_dir.exists():
        print("  ⚠ snapshots/ directory not found")
        return False
    
    snapshots = list(snapshots_dir.glob('SPY_snapshot_*.json'))
    if snapshots:
        latest = sorted(snapshots)[-1]
        print(f"  ✓ Found {len(snapshots)} snapshot(s)")
        print(f"  → Latest: {latest.name}")
        return True
    else:
        print("  ⚠ No snapshots found")
        print("  → You'll need IBKR connection to create snapshots")
        return False

def check_configs():
    """Check if config files exist."""
    print("\nChecking configuration files...")
    
    configs_dir = Path('configs')
    if not configs_dir.exists():
        print("  ✗ configs/ directory not found")
        return False
    
    config_files = list(configs_dir.glob('*.yaml'))
    if config_files:
        print(f"  ✓ Found {len(config_files)} config file(s)")
        for cfg in config_files:
            print(f"    - {cfg.name}")
        return True
    else:
        print("  ✗ No config files found")
        return False

def main():
    """Run all checks."""
    print("=" * 60)
    print("FORECAST ARB - SETUP VERIFICATION")
    print("=" * 60)
    
    results = []
    
    results.append(("Python Version", check_python_version()))
    results.append(("Required Packages", check_required_packages()))
    check_optional_packages()  # Informational only
    results.append(("Environment File", check_env_file()))
    results.append(("Environment Variables", check_env_variables()))
    results.append(("Module Import", check_module_import()))
    results.append(("Sample Snapshots", check_snapshots()))
    results.append(("Config Files", check_configs()))
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status:8} - {name}")
    
    print(f"\nPassed: {passed}/{total}")
    
    if passed == total:
        print("\n🎉 All checks passed! You're ready to run forecast_arb.")
        print("\nQuick start:")
        print("  python scripts/run_daily.py --review-only-structuring --snapshot snapshots/SPY_snapshot_20260204_140505.json")
    else:
        print("\n⚠️  Some checks failed. Please review the output above.")
        print("\nFor help, see SETUP.md")
    
    print("=" * 60)
    
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
