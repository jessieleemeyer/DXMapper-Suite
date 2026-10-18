#!/usr/bin/env python3
"""Launcher for the FM Tuner application (double-clickable on Windows)."""

import multiprocessing
from pathlib import Path

try:  # one-folder suite: hide the console, copy old settings in once
    import suite_start
    suite_start.startup(Path(__file__).resolve().parent)
except ImportError:
    pass

from fm_tuner.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
