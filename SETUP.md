# Local Setup Guide for Forecast Arb

This guide will help you set up all dependencies needed to run the forecast_arb project locally.

## Prerequisites

### 1. Python Version
- **Required:** Python 3.10 or higher
- Check your version: `python --version`
- Download from: https://www.python.org/downloads/

### 2. Python Package Dependencies

Install all required packages:

```bash
pip install -r requirements.txt
```

**Core Dependencies:**
- `requests>=2.31.0` - HTTP requests
- `beautifulsoup4>=4.12.0` - HTML parsing
- `pydantic>=2.0.0` - Data validation
- `pyyaml>=6.0` - YAML configuration
- `pandas>=2.0.0` - Data manipulation
- `numpy>=1.24.0` - Numerical computing
- `python-dateutil>=2.8.0` - Date/time utilities
- `jsonschema>=4.0.0` - JSON validation
- `python-dotenv>=1.0.0` - Environment variable loading
- `cryptography>=41.0.0` - Cryptographic signing for Kalshi API

**Options Math Libraries:**
- `py_vollib>=1.0.1` - Black-Scholes and option greeks
- `scipy>=1.11.0` - Scientific computing

**IBKR Integration (Optional):**
- `ib_insync>=0.9.86` - Interactive Brokers API client
- Only needed if fetching live option chains from IBKR

### 3. Environment Variables Setup

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` and configure:

#### Kalshi API (Required for live market data)
```env
KALSHI_API_KEY_ID=your_api_key_id_here
KALSHI_PRIVATE_KEY_PATH=/path/to/your/kalshi_private_key.pem
KALSHI_DEMO_MODE=false  # Set to 'true' for demo/testing
```

**How to get Kalshi credentials:**
1. Sign up at https://kalshi.com
2. Go to Settings → API
3. Generate API key and download private key PEM file
4. Save the PEM file securely and set the path in `.env`

#### Interactive Brokers (Optional - for live option chains)
```env
IBKR_HOST=127.0.0.1
IBKR_PORT=7496  # 7496 for live
IBKR_CLIENT_ID=1
```

**Required only if using `--snapshot` creation (live IBKR data):**
1. Install Interactive Brokers TWS or IB Gateway
2. Configure API settings in TWS/Gateway:
   - Enable API connections
   - Set socket port (7496 for live)
   - Add trusted IP: 127.0.0.1
3. Keep TWS/Gateway running when fetching snapshots

#### OpenAI API (Optional - for LLM-based reviews)
```env
OPENAI_API_KEY=your_openai_api_key_here
```

Only needed if using GPT-based decision support features.

## Installation Steps

### Step 1: Clone and Navigate
```bash
cd c:/Users/jpg02/forecast_arb
```

### Step 2: Create Virtual Environment (Recommended)
```bash
python -m venv venv
venv\Scripts\activate  # On Windows
# source venv/bin/activate  # On macOS/Linux
```

### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 4: Configure Environment
```bash
cp .env.example .env
# Edit .env with your credentials
```

### Step 5: Verify Installation
```bash
python -c "import forecast_arb; print('✓ Installation successful!')"
```

## Running the Project

### Review-Only Mode (Recommended for first run)
```bash
python scripts/run_daily.py --review-only-structuring
```

This runs structure generation without requiring IBKR connection or live trading.

### With Live Quotes (Requires IBKR running)
```bash
python scripts/run_daily.py --review-only-structuring --include-live-quotes
```

### Using Existing Snapshot (No IBKR needed)
```bash
python scripts/run_daily.py --review-only-structuring --snapshot snapshots/SPY_snapshot_20260204_140505.json
```

## Dependency Checklist

Use this checklist to verify your setup:

- [ ] Python 3.10+ installed (`python --version`)
- [ ] All pip packages installed (`pip list`)
- [ ] `.env` file created and configured
- [ ] Kalshi API credentials set (if using live data)
- [ ] IBKR TWS/Gateway running (if fetching new snapshots)
- [ ] Can import forecast_arb (`python -c "import forecast_arb"`)

## Optional Dependencies

### For Development
```bash
pip install pytest pytest-cov black ruff
```

### For Testing
```bash
pytest  # Run all tests
pytest tests/test_review_only_mode.py  # Run specific test
```

## Common Issues

### Issue: "No module named 'forecast_arb'"
**Solution:** Ensure you're in the project root and Python can find the module:
```bash
cd c:/Users/jpg02/forecast_arb
python -c "import sys; sys.path.insert(0, '.'); import forecast_arb"
```

### Issue: "Kalshi API authentication failed"
**Solution:** 
- Check API key ID is correct
- Verify private key PEM file path exists
- Ensure private key matches the API key

### Issue: "Cannot connect to IBKR"
**Solution:**
- Start TWS or IB Gateway
- Check port number (7496 for live)
- Verify API is enabled in TWS settings
- Check trusted IPs include 127.0.0.1

### Issue: "ImportError: py_vollib"
**Solution:**
```bash
pip install py_vollib scipy
```

## Minimal Setup (For Review-Only Mode)

If you just want to review structures without live data:

1. **Install only core packages:**
   ```bash
   pip install pyyaml pandas numpy python-dateutil jsonschema py_vollib scipy
   ```

2. **Skip API configuration** - Not needed for review-only mode

3. **Use existing snapshots:**
   ```bash
   python scripts/run_daily.py --review-only-structuring --snapshot snapshots/SPY_snapshot_20260204_140505.json
   ```

## Next Steps

After setup is complete:

1. **Test the installation:**
   ```bash
   python scripts/run_daily.py --review-only-structuring --snapshot snapshots/SPY_snapshot_20260204_140505.json
   ```

2. **Review the output:**
   - Check `runs/crash_venture_v1_1/` for latest run
   - Open `artifacts/review_pack.md` for human-readable review

3. **Read the documentation:**
   - `REVIEW_ONLY_MODE_README.md` - Review workflow
   - `EXECUTION_REFACTOR_README.md` - Execution flow
   - `P_EVENT_SYSTEM_README.md` - Probability sources

## Support

If you encounter issues:
1. Check this setup guide
2. Review error messages carefully
3. Verify all dependencies are installed
4. Check environment variables are set correctly

For development questions, see `cline_docs/` for system architecture.
