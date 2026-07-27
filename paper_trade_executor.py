#!/usr/bin/env python3
"""
Thin wrapper for backwards-compatible CLI access.
Routes to core.paper_trade_executor.
"""

import sys
from pathlib import Path

# Ensure the root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.paper_trade_executor import main

if __name__ == "__main__":
    main()
