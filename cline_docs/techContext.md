# Memory Bank: Tech Context

## Technologies
- Python project, pytest for testing.
- IBKR API integration for live snapshots.
- Kalshi REST API client.

## Development setup
- Install via pip (requirements.txt) or editable install.
- Configure .env with Kalshi and IBKR credentials.
- Snapshot scripts: examples/run_real_cycle_snapshot.py (live) and examples/run_real_cycle.py (theoretical).

## Constraints
- Preserve determinism, seeds, config checksums.
- Minimal diffs, no refactors, no silent fallbacks.
- Fail closed unless --allow-fallback is explicitly set.
