#!/usr/bin/env python3
"""Compatibility entrypoint for GitHub Actions.

Some workflow versions call `python scripts/hathway_usage.py`. The main tracker
now lives in `outputs/broadband_usage_tracker.py`, so this wrapper keeps both
paths working.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = ROOT / "outputs"
sys.path.insert(0, str(OUTPUTS_DIR))

from broadband_usage_tracker import main  # noqa: E402


if __name__ == "__main__":
    args = sys.argv[1:] or ["--once", "--omit-raw-response"]
    raise SystemExit(main(args))
