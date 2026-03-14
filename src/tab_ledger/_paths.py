"""Centralized path constants for tab-ledger.

All data directories and file paths are defined here. Override with
environment variables for non-default locations or CI testing.
"""

import os
from pathlib import Path

# ── Data directory ──
DATA_DIR = Path(os.environ.get("TAB_LEDGER_DATA_DIR", Path.home() / ".tab-ledger"))

# ── Databases ──
LEDGER_DB = DATA_DIR / "ledger.db"
KB_DB = DATA_DIR / "knowledge_base.db"

# ── Claude Code data sources ──
CLAUDE_PROJECTS = Path(
    os.environ.get("TAB_LEDGER_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects")
)
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PLANS_DIR = Path.home() / ".claude" / "plans"
TODOS_DIR = Path.home() / ".claude" / "todos"
TEAMS_DIR = Path.home() / ".claude" / "teams"

# ── Claude.ai conversation export ──
CLAUDE_AI_DB = Path.home() / ".claude_history_search" / "conversations.db"

# ── Browser (macOS only) ──
COMET_HISTORY = Path.home() / "Library/Application Support/Comet/Default/History"
COMET_SESSIONS = Path.home() / "Library/Application Support/Comet/Default/Sessions"


def ensure_data_dir() -> Path:
    """Create the data directory if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
