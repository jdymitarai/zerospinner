#!/usr/bin/env python3
"""ZeroSpinner - Google Jules Watchdog CLI Entrypoint."""
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from zerospinner.jules_watchdog import *
from zerospinner.jules_watchdog import main

if __name__ == "__main__":
    main()
