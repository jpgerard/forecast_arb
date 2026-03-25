"""
Allow running package as module: python -m forecast_arb
"""

from .engine.run import main

if __name__ == "__main__":
    main()
