#!/usr/bin/env python3
"""Print the UTC offset (seconds) of TIMEZONE in relay-stats.conf; used to pass day boundaries to relay-scan."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fasttext  # noqa: F401  (analyze imports it anyway)
import analyze
print(analyze.TZ_OFFSET)
