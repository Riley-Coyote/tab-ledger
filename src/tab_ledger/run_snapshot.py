#!/usr/bin/env python3
"""Runner script for launchd — takes a snapshot and indexes CC sessions."""

import sys
import os

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from .snapshot import init_db, take_snapshot
from .cc_indexer import index_all

if __name__ == "__main__":
    init_db()
    take_snapshot(source="auto")
    index_all()
