# Knowledge Base Handoff — For Agents

You have access to a comprehensive knowledge base built from 1,316 Claude Code sessions across 11 projects. This document tells you everything you need to query it.

## What This Is

A 58 MB SQLite database at `~/.tab-ledger/knowledge_base.db` containing:

- **1,316 sessions** with full metadata (model, tokens, cost, duration, tools used)
- **50,241 messages** indexed at the message level
- **21,334 FTS entries** for full-text search (Porter stemming + Unicode)
- **1,322 cross-session connections** linking related work
- **195 AI-generated summaries** with next steps, blockers, and decisions
- **11 projects** spanning Dec 2025 – Feb 2026

Total spend indexed: $12,351.85 across Opus 4.5, Opus 4.6, Haiku 4.5, Sonnet 4.5, and Sonnet 4.6.

## The Projects

| Project | Sessions | Cost | Description |
|---------|----------|------|-------------|
| my-other-project | 411 | $3,619 | The My Other Project — AI consciousness research site |
| my-project | 373 | $4,749 | My Project — multi-model chat platform |
| exploration | 105 | $745 | Ad-hoc exploration and experiments |
| tools | 101 | $302 | Supporting tools, scripts, infrastructure |
| data-research | 68 | $673 | Data processing and research |
| my-app | 60 | $696 | My App Chat — real-time chat app |
| my-protocol | 45 | $464 | My Protocol Protocol |
| my-bot | 44 | $496 | My Bot — Discord/chat bot |
| my-tool | 42 | $426 | My Tool Terminal |
| my-dashboard | 39 | $110 | My Dashboard CLI tool |
| my-engine | 6 | $68 | My Engine project |

## How to Query It

You have three interfaces, from simplest to most flexible.

### Option 1: CLI Commands (Recommended)

Run these from any shell. All return JSON by default. Add `--human` for formatted text.

```bash
# List all projects
python3 ~/.tab-ledger/kb_query.py projects

# Get continuation context for a project (THE key command for resuming work)
# Returns: last session summary, next steps, blockers, recent decisions, related sessions
python3 ~/.tab-ledger/kb_query.py context my-project

# Full-text search across all sessions
python3 ~/.tab-ledger/kb_query.py search "websocket authentication"

# Semantic search (conceptual retrieval)
python3 ~/.tab-ledger/kb_query.py semantic "oauth callback bug in websocket flow" --project my-app

# Continuity packet (timeline + blockers + semantic anchors)
python3 ~/.tab-ledger/kb_query.py memory my-app

# Search within a specific project
python3 ~/.tab-ledger/kb_query.py search "database migration" --project my-app

# Chronological session timeline for a project
python3 ~/.tab-ledger/kb_query.py timeline my-project --limit 20

# Full session detail by UUID or prefix
python3 ~/.tab-ledger/kb_query.py session a1b2c3d4

# Get stats (global or per-project)
python3 ~/.tab-ledger/kb_query.py stats
python3 ~/.tab-ledger/kb_query.py stats --project my-app

# Recent sessions across all projects
python3 ~/.tab-ledger/kb_query.py recent 10

# Project iterations grouped by phase
python3 ~/.tab-ledger/kb_query.py iterations my-project

# Find sessions related to a specific session
python3 ~/.tab-ledger/kb_query.py related <session-uuid>
```

### Option 2: Python API

```python
import sys
sys.path.insert(0, "~/.tab-ledger")
from kb_query import KnowledgeBase

kb = KnowledgeBase(readonly=True)

# List projects
projects = kb.list_projects()

# Resume context (summary, next steps, blockers, related sessions)
context = kb.get_continuation_context("my-project")

# Full-text search (FTS5 syntax: AND, OR, NOT, quotes for phrases)
results = kb.search("websocket OR authentication", project="my-app", limit=10)

# Semantic search
semantic = kb.semantic_search("oauth callback bug in websocket flow", project="my-app", limit=8)

# Memory continuity packet
memory = kb.get_memory_packet("my-app")

# Session detail by UUID prefix
session = kb.get_session("a1b2c3d4")

# Timeline
timeline = kb.get_timeline("my-project", limit=50)

# Stats
stats = kb.get_stats(project="my-app")  # or stats = kb.get_stats() for global

# Always close when done
kb.close()
```

### Option 3: Direct SQLite

```python
import sqlite3
from pathlib import Path

db_path = Path.home() / ".tab-ledger" / "knowledge_base.db"
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
```

**IMPORTANT:** Always open read-only (`?mode=ro`). This is a shared resource.

## Database Schema

### Core Tables

**`kb_projects`** — The 11 canonical projects.
```
canonical_name TEXT UNIQUE  -- e.g. 'my-project', 'my-app'
display_name TEXT           -- e.g. 'My Project', 'My App Chat'
status TEXT                 -- 'active'
total_sessions INTEGER
total_cost_usd REAL
first_session_at TIMESTAMP
last_session_at TIMESTAMP
```

**`kb_sessions`** — Every session with full metadata.
```
session_uuid TEXT UNIQUE    -- UUID or agent-prefixed ID
project_id INTEGER          -- FK to kb_projects
slug TEXT                   -- Human-readable session name
model TEXT                  -- e.g. 'claude-opus-4-6'
started_at TIMESTAMP
ended_at TIMESTAMP
message_count INTEGER
cost_usd REAL
input_tokens INTEGER
output_tokens INTEGER
tools_used TEXT             -- Comma-separated tool names
summary_json TEXT           -- JSON with next_steps, blockers, decisions
summary_text TEXT           -- Plain-text summary
phase TEXT                  -- 'build', 'debug', 'refactor', 'explore', etc.
outcome TEXT
first_prompt TEXT           -- What the user asked at session start
```

**`kb_messages`** — Message-level index (50K+ rows).
```
session_id INTEGER          -- FK to kb_sessions
message_index INTEGER
message_type TEXT           -- 'human', 'assistant', 'tool_result', etc.
role TEXT
content_text TEXT           -- Full message content
content_length INTEGER
has_tool_use BOOLEAN
tool_names TEXT
model TEXT
timestamp TIMESTAMP
```

**`kb_fts`** — Full-text search virtual table (FTS5, Porter stemming).
```
text TEXT                   -- Searchable content
session_uuid TEXT           -- Links back to session
source_type TEXT            -- 'summary', 'message', 'prompt', 'plan', 'todo'
project_name TEXT           -- Canonical project name
```

Query with: `SELECT * FROM kb_fts WHERE text MATCH 'your query here'`

FTS5 supports: `AND`, `OR`, `NOT`, `"exact phrases"`, `prefix*`, `NEAR(a b, 5)`

**`kb_connections`** — Cross-session links (1,322 connections).
```
source_session_id INTEGER
target_session_id INTEGER
connection_type TEXT        -- e.g. 'continuation', 'related', 'references'
strength REAL               -- 0.0 to 1.0
reason TEXT
```

**`kb_embeddings`** — Semantic embedding index for conceptual retrieval.
```
source_key TEXT UNIQUE      -- e.g. 'summary:<session_uuid>', 'plan:<filename>'
session_uuid TEXT
source_type TEXT            -- summary|prompt|plan|todo|message
project_name TEXT
text_hash TEXT              -- Detects changes for incremental re-embedding
embedding BLOB              -- Float32 vector bytes
embedding_norm REAL
embedding_dim INTEGER
embedding_model TEXT        -- hash-768 | text-embedding-3-small | nomic-embed-text, etc.
metadata_json TEXT
```

### Auxiliary Tables

| Table | Purpose |
|-------|---------|
| `kb_sub_projects` | Sub-project breakdown within projects |
| `kb_commands` | CLI commands issued across sessions |
| `kb_plans` | Plan files created during sessions |
| `kb_todos` | Todo lists with completion tracking |
| `kb_teams` | Agent team configurations |
| `kb_deep_archives` | Analysis of very large sessions |
| `kb_progress` | Build pipeline progress tracking |

## Useful Direct Queries

```sql
-- Find all sessions for a project, most recent first
SELECT session_uuid, slug, started_at, model, summary_text
FROM kb_sessions
WHERE project_id = (SELECT id FROM kb_projects WHERE canonical_name = 'my-app')
ORDER BY started_at DESC
LIMIT 10;

-- Full-text search
SELECT * FROM kb_fts WHERE text MATCH 'websocket authentication';

-- Search within a project
SELECT * FROM kb_fts
WHERE text MATCH 'database schema'
AND project_name = 'my-project';

-- Find connected sessions
SELECT
    s.session_uuid, s.slug, s.summary_text,
    c.connection_type, c.strength
FROM kb_connections c
JOIN kb_sessions s ON c.target_session_id = s.id
WHERE c.source_session_id = (SELECT id FROM kb_sessions WHERE session_uuid = 'some-uuid')
ORDER BY c.strength DESC;

-- Cost by model
SELECT model, COUNT(*) as sessions, SUM(cost_usd) as total_cost
FROM kb_sessions
GROUP BY model
ORDER BY total_cost DESC;

-- Most active phases
SELECT phase, COUNT(*) as session_count, SUM(cost_usd) as cost
FROM kb_sessions
WHERE phase IS NOT NULL
GROUP BY phase
ORDER BY session_count DESC;

-- Recent sessions across all projects
SELECT
    s.session_uuid, s.slug, s.started_at, s.model, s.summary_text,
    p.canonical_name as project
FROM kb_sessions s
JOIN kb_projects p ON s.project_id = p.id
ORDER BY s.started_at DESC
LIMIT 20;
```

## Key Files

| File | Purpose |
|------|---------|
| `~/.tab-ledger/knowledge_base.db` | The SQLite database (58 MB) |
| `~/.tab-ledger/kb_query.py` | Python query API + CLI (KnowledgeBase class) |
| `~/.tab-ledger/kb_schema.py` | Schema definitions + get_kb_db() helper |
| `~/.tab-ledger/kb_mcp_server.py` | MCP server (used by Claude Code sessions) |

## MCP Server (If You Support MCP)

If your runtime supports stdio MCP servers, you can use the same server Claude Code uses:

```json
{
  "type": "stdio",
  "command": "/opt/homebrew/bin/python3",
  "args": ["~/.tab-ledger/kb_mcp_server.py"]
}
```

This exposes 6 tools: `kb_search`, `kb_context`, `kb_session`, `kb_projects`, `kb_timeline`, `kb_stats`.

## How to Use This as Memory

The most valuable pattern for session continuity:

1. **At session start**, run `kb_context <project>` to get the last session's summary, next steps, and blockers for whatever project you're working on.
2. **When you need history**, run `kb_search "topic"` to find what was discussed, decided, or built previously.
3. **When you need full detail**, grab a session UUID from search results and run `kb_session <uuid>` to see the complete session including messages and connections.
4. **When exploring**, run `kb_timeline <project>` to see the chronological arc of a project's development.

This database is **read-only** from your perspective. The build pipeline that populates it runs separately. Do not attempt to write to it.
