# Tab Ledger

**Nothing lost. Everything findable.**

A local-first analytics and knowledge base system that continuously indexes your browser tabs and Claude Code sessions into a searchable, AI-summarized knowledge graph. Built for developers who use AI coding tools extensively and want persistent memory across sessions.

Tab Ledger runs entirely on your machine. Two SQLite databases, a handful of Python scripts, and a pair of launchd agents that keep everything current while you work.

---

## What It Does

Tab Ledger solves a specific problem: when you run hundreds of AI coding sessions across multiple projects, the context and decisions from those sessions disappear. Tab Ledger captures everything and makes it queryable — by you, by your AI tools, and by autonomous agents.

**Two-layer architecture:**

| Layer | Database | Size | Purpose |
|-------|----------|------|---------|
| **Ledger** | `ledger.db` | ~6 MB | Live operational data — tab snapshots + session metadata |
| **Knowledge Base** | `knowledge_base.db` | ~58 MB | Enriched analytical layer — FTS search, AI summaries, cross-session connections |

**What gets captured:**

- **Browser tabs** — Point-in-time snapshots of every open tab (URL, title, domain), auto-categorized across 20 categories, with stale tab detection
- **Claude Code sessions** — Every session parsed from JSONL: token counts, costs, tools used, models, durations, first prompt, summary
- **Messages** — 50K+ individual messages indexed at the message level
- **AI summaries** — Structured summaries with next steps, blockers, decisions, and phase classification
- **Cross-session connections** — 1,300+ links between related sessions detected by temporal proximity, git branches, slugs, and parent-child relationships
- **Auxiliary data** — CLI commands, plan files, todo lists, team configs

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                         │
├──────────────────────┬──────────────────────────────────────┤
│   Comet Browser      │   Claude Code (~/.claude/projects/)  │
│   (CDP / Sessions)   │   JSONL session files                │
└──────────┬───────────┴──────────────────┬───────────────────┘
           │ every 30 min                 │ every 30 min
           ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     LEDGER (ledger.db)                       │
│  snapshots · tabs · cc_sessions · parked_groups · digests   │
└─────────────────────────┬───────────────────────────────────┘
                          │ nightly (4 AM)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│               KNOWLEDGE BASE (knowledge_base.db)            │
│                                                             │
│  kb_projects (11) ──── kb_sub_projects (61)                 │
│       │                                                     │
│  kb_sessions (1,316) ── kb_messages (49,517)                │
│       │                                                     │
│  kb_fts (21,334) ───── FTS5 full-text search                │
│       │                                                     │
│  kb_connections (1,322) cross-session links                  │
│       │                                                     │
│  kb_commands · kb_plans · kb_todos · kb_teams               │
└─────────────────────────┬───────────────────────────────────┘
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
         MCP Server   CLI (kb_query)  FastAPI Dashboard
         (Claude Code)  (agents)      (localhost:7777)
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- macOS (uses launchd for scheduling; adaptable to cron on Linux)
- A Chromium-based browser (Comet, Chrome, Arc, etc.) for tab capture
- Claude Code (for session indexing — the JSONL files it creates)
- Claude CLI (`claude` command) authenticated locally for stage 4 summarization

### Installation

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/tab-ledger.git ~/.tab-ledger
cd ~/.tab-ledger

# Install dependencies
pip install -r requirements.txt

# Initialize the database
python3 snapshot.py  # Creates ledger.db with schema + first snapshot

# Index existing Claude Code sessions
python3 cc_indexer.py
```

### Set Up Automatic Collection

Install the launchd agent to capture tabs and index sessions every 30 minutes:

```bash
# Copy the plist (edit paths inside if your Python isn't at /opt/homebrew/bin/python3)
cp com.rileycoyote.tab-ledger.plist ~/Library/LaunchAgents/

# Load it
launchctl load ~/Library/LaunchAgents/com.rileycoyote.tab-ledger.plist

# Verify it's running
launchctl list | grep tab-ledger
```

### Build the Knowledge Base

The KB is a one-time build on top of the ledger data, with an optional nightly refresh:

```bash
# Full build (runs core stages 0-7)
python3 kb_build.py

# Skip AI summarization if you don't want API costs
python3 kb_build.py --skip-summarize

# Resume from a specific stage if interrupted
python3 kb_build.py --from 3
```

**Build stages:**

| # | Stage | What It Does | Cost |
|---|-------|-------------|------|
| 0 | Schema | Creates all `kb_*` tables | Free |
| 1 | Taxonomy | Maps sessions to canonical projects via path matching | Free |
| 2 | Messages | Parses every JSONL at the message level | Free |
| 3 | FTS | Builds FTS5 full-text search index | Free |
| 4 | Summarization | Calls Claude CLI for structured summaries | Usage costs |
| 5 | Linking | Detects cross-session connections | Free |
| 6 | Auxiliary | Indexes commands, plans, todos, teams | Free |
| 7 | Verification | 9-point integrity check | Free |
| 8 | Semantic (optional) | Embedding index for semantic/hybrid memory retrieval | Free (hash/ollama) or API usage (openai) |

Run the optional semantic stage:

```bash
python3 kb_build.py --semantic-provider hash
# or: --semantic-provider ollama
# or: --semantic-provider openai
```

### Set Up Nightly Refresh (Optional)

Keep the KB current automatically:

```bash
# Install the nightly refresh agent (runs at 4 AM, skips summarization)
cp com.rileycoyote.tab-ledger-kb-refresh.plist ~/Library/LaunchAgents/  # Create this from the snapshot plist, pointing to run_kb_refresh.py
launchctl load ~/Library/LaunchAgents/com.rileycoyote.tab-ledger-kb-refresh.plist

# Optional: enable semantic refresh inside run_kb_refresh.py
export KB_SEMANTIC_PROVIDER=hash
# Optional:
# export KB_SEMANTIC_MODEL=hash-768
# export KB_SEMANTIC_INCLUDE_MESSAGES=1
```

---

## Querying the Knowledge Base

### CLI (Primary Interface)

All commands return JSON by default. Add `--human` for formatted text output.

```bash
# List all projects with session counts and costs
python3 kb_query.py projects

# Get continuation context for a project
# Returns: last session summary, next steps, blockers, decisions, related sessions
python3 kb_query.py context polyphonic

# Full-text search across all sessions
python3 kb_query.py search "websocket authentication"

# Semantic search across memory embeddings
python3 kb_query.py semantic "oauth callback bug in websocket flow" --project vessel --limit 8

# Search within a specific project
python3 kb_query.py search "database migration" --project vessel --limit 10

# Chronological session timeline
python3 kb_query.py timeline polyphonic --limit 20

# Full session detail by UUID or prefix
python3 kb_query.py session a1b2c3d4

# Statistics (global or per-project)
python3 kb_query.py stats
python3 kb_query.py stats --project vessel

# Recent sessions across all projects
python3 kb_query.py recent 10

# Project iterations grouped by phase
python3 kb_query.py iterations polyphonic

# Sessions connected to a specific session
python3 kb_query.py related <session-uuid>

# High-signal continuity packet (timeline + blockers + semantic anchors)
python3 kb_query.py memory polyphonic
```

### Python API

```python
import sys
sys.path.insert(0, "/path/to/.tab-ledger")
from kb_query import KnowledgeBase

kb = KnowledgeBase(readonly=True)

# Resume context — the key query for session continuity
context = kb.get_continuation_context("polyphonic")
# Returns: last_session, next_steps, blockers, decisions, related_sessions

# Full-text search (FTS5 syntax: AND, OR, NOT, "phrases", prefix*, NEAR())
results = kb.search("websocket OR authentication", project="vessel", limit=10)

# Semantic search
semantic = kb.semantic_search("oauth callback bug in websocket flow", project="vessel", limit=8)

# Continuity packet for natural project memory continuity
memory = kb.get_memory_packet("polyphonic")

# List all projects
projects = kb.list_projects()

# Session detail
session = kb.get_session("a1b2c3d4")  # prefix match supported

# Timeline
timeline = kb.get_timeline("polyphonic", limit=50)

# Stats
stats = kb.get_stats(project="vessel")

kb.close()
```

### MCP Server (For Claude Code)

Register in `~/.claude/settings.json` to make the KB available as tools in every Claude Code session:

```json
{
  "mcpServers": {
    "tab-ledger": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/.tab-ledger/kb_mcp_server.py"]
    }
  }
}
```

This exposes 8 tools to Claude Code and all subagents:

| Tool | Purpose |
|------|---------|
| `kb_search` | Full-text search across all sessions |
| `kb_semantic` | Embedding-powered semantic search across memory artifacts |
| `kb_memory` | Continuity packet for natural project resumption |
| `kb_context` | Continuation context for resuming work on a project |
| `kb_session` | Full session detail by UUID prefix |
| `kb_projects` | List all projects with metadata |
| `kb_timeline` | Chronological session list for a project |
| `kb_stats` | Token counts, costs, tool rankings, phase breakdown |

### Direct SQL

```python
import sqlite3
conn = sqlite3.connect("file:~/.tab-ledger/knowledge_base.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# FTS5 search
rows = conn.execute("SELECT * FROM kb_fts WHERE text MATCH 'websocket authentication'").fetchall()

# FTS5 supports: AND, OR, NOT, "exact phrases", prefix*, NEAR(a b, 5)
rows = conn.execute("""
    SELECT * FROM kb_fts
    WHERE text MATCH 'database AND migration'
    AND project_name = 'vessel'
""").fetchall()
```

---

## Search Capabilities

The system supports multiple search patterns at different layers:

### Full-Text Search (FTS5)

The most powerful search. Built on SQLite's FTS5 engine with Porter stemming and Unicode tokenization.

| Syntax | Example | What It Does |
|--------|---------|-------------|
| Simple | `websocket` | Matches "websocket" and stemmed variants |
| AND | `websocket AND auth` | Both terms must appear |
| OR | `redis OR memcached` | Either term |
| NOT | `database NOT migration` | Exclude results |
| Phrase | `"database migration"` | Exact phrase match |
| Prefix | `web*` | Matches websocket, webrtc, webpack... |
| Proximity | `NEAR(auth websocket, 5)` | Terms within 5 tokens of each other |
| Filter | `AND project_name = 'vessel'` | Scope to a project |
| Source | `AND source_type = 'summary'` | Filter by content type |

Source types: `summary`, `message`, `prompt`, `plan`, `todo`

### Semantic Search (Embeddings)

Semantic search is available via `kb_query.py semantic` and `/api/kb/semantic`.
It supports:

- Conceptual recall when exact keywords differ
- Project and source-type filtering
- Hybrid ranking (semantic cosine + light FTS boost)
- Provider options: `hash`, `ollama`, `openai`

`hash` works fully local and deterministic; `ollama` and `openai` provide richer embeddings.

### Memory Continuity Packet

`kb_query.py memory <project>` and `/api/kb/memory/{project}` return a high-signal
continuity payload:

- Last-session context and timeline
- Unresolved blockers and deduplicated next steps
- Connection threads across sessions
- Semantic anchors (with automatic FTS fallback when semantic index is unavailable)

### Tab Search (Ledger Layer)

LIKE-based search across tab URLs and titles, filterable by category and date range. Available through the FastAPI dashboard at `/api/search`.

### Continuation Context

Not a search per se, but the most important query for agents. Returns structured context for resuming work on any project: last session summary, next steps, blockers, recent decisions, and related sessions.

---

## Dashboard

A FastAPI web dashboard at `http://localhost:7777`:

```bash
python3 server.py
# or
uvicorn server:app --host 127.0.0.1 --port 7777 --reload
```

### API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | HTML dashboard |
| `/api/right-now` | GET | Latest snapshot with tabs grouped by category |
| `/api/today` | GET | Today's activity summary |
| `/api/search?q=...` | GET | Search tabs and sessions |
| `/api/history?days=7` | GET | Snapshot history with daily summaries |
| `/api/snapshot` | POST | Take a manual snapshot |
| `/api/cc/stats` | GET | Aggregate Claude Code stats |
| `/api/cc/timeline?days=30` | GET | Daily activity chart data |
| `/api/cc/tools` | GET | Tool usage analytics |
| `/api/cc/models` | GET | Model usage breakdown |
| `/api/cc/session/{id}` | GET | Single session detail |
| `/api/park` | POST | Park a group of tabs |
| `/api/parked` | GET | List parked tab groups |
| `/api/digest/{date}` | GET | Daily digest (auto-generated) |
| `/api/reindex` | POST | Re-index Claude Code sessions |
| `/api/categories` | GET | Category color mapping |
| `/api/kb/semantic?q=...` | GET | Semantic search across KB embeddings |
| `/api/kb/memory/{project}` | GET | Memory continuity packet for a project |

---

## Tab Capture

### How It Works

The snapshot engine (`snapshot.py`) uses two methods to capture open tabs:

1. **Chrome DevTools Protocol (CDP)** — Connects to `localhost:9222` (or other debug ports) for an exact tab list with titles. This is the preferred method.

2. **Session file parsing (fallback)** — If CDP isn't available, extracts URLs from the browser's binary session files using `strings`. Cross-references with the History database for titles. Approximate but functional.

### Auto-Categorization

Every tab is classified into one of 20 categories using regex pattern matching against URLs:

| Category | Color | Examples |
|----------|-------|---------|
| AI Studio | Purple | claude.ai, chatgpt.com, gemini.google.com |
| Infrastructure | Gray | github.com, vercel.com, supabase.com |
| Dev Docs | Cyan | mdn.mozilla.org, threejs.org |
| Crypto | Yellow | dexscreener.com, tradingview.com |
| Social | Red | youtube.com, reddit.com, instagram.com |
| Local Dev | Green | localhost:*, 127.0.0.1:* |
| ... | ... | 14 more categories |

### Stale Tab Detection

Tabs are flagged as stale when they match patterns like expired OAuth flows, completed checkout pages, or localhost tabs where the server has stopped running.

---

## Claude Code Session Indexing

### What Gets Extracted

The JSONL parser (`cc_indexer.py`) streams each session file line-by-line (some are 50MB+) and extracts:

| Field | Source |
|-------|--------|
| Session UUID | Filename |
| First user prompt | First `type: "user"` message |
| Summary | `type: "summary"` records |
| Model | Most common model across assistant messages |
| Token counts | `usage` fields (input, output, cache creation, cache read) |
| Cost estimate | Calculated from token counts × per-model pricing |
| Tools used | Every `type: "tool_use"` block, deduplicated |
| Duration | Sum of `turn_duration` system records |
| Git branch, slug | Extracted from session metadata |
| Sidechain detection | Subagent sessions identified by path or flag |

### Cost Estimation

Costs are calculated using per-model pricing:

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|-------------|------------|
| Claude Opus 4.5/4.6 | $15/M | $75/M | $18.75/M | $1.50/M |
| Claude Sonnet 4.5/4.6 | $3/M | $15/M | $3.75/M | $0.30/M |
| Claude Haiku 4.5 | $0.80/M | $4/M | $1/M | $0.08/M |

---

## Knowledge Base Build Pipeline

The KB build (`kb_build.py`) is an 8-stage core pipeline (0-7) with an optional stage 8 semantic embedding pass.

### Stage 1: Project Taxonomy

Sessions are mapped to canonical projects using path-based matching defined in `kb_taxonomy.py`. Each project can have sub-projects for finer granularity. The taxonomy is deterministic — most specific path match wins.

### Stage 4: AI Summarization

The summarizer (`kb_summarizer.py`) uses a two-tier approach:

- **Opus tier** — For major projects (higher quality, higher cost)
- **Haiku tier** — For minor/ad-hoc projects (faster, cheaper)

Content extraction is size-aware:
- **< 5 MB**: All human messages + first/last assistant responses
- **5–50 MB**: Sampled — first 5 + last 3 human, every 10th in between
- **50–100 MB**: Heavily sampled
- **100+ MB**: Special "deep archive" analysis

Each summary produces structured JSON:
```json
{
  "summary": "What happened in this session",
  "decisions": ["Key decisions made"],
  "next_steps": ["What should happen next"],
  "blockers": ["Outstanding issues"],
  "phase": "build|debug|refactor|explore|deploy|research",
  "outcome": "success|partial|blocked|abandoned"
}
```

### Stage 5: Cross-Session Linking

The linker (`kb_linker.py`) detects connections between sessions using:

- **Temporal proximity** — Sessions close in time on the same project
- **Shared git branches** — Same branch = likely related work
- **Session slugs** — Claude Code assigns readable slugs; shared slugs indicate continuation
- **Parent-child** — Subagent sessions linked to their parent session

Each connection has a type and strength score (0.0–1.0).

---

## Agent Integration

Tab Ledger is designed to be queryable by AI agents, not just humans.

### For Claude Code

Register the MCP server in `~/.claude/settings.json`. Every session and subagent automatically gets access to the `kb_*` tools. The continuation context tool (`kb_context`) is particularly valuable — it gives any agent instant awareness of what happened in the last session on a project.

### For Other Agents (OpenClaw, Clawdbot, etc.)

Add the CLI commands to the agent's tool definitions. The CLI returns JSON by default, making it easy to parse programmatically:

```bash
# Add to your agent's tool/bootstrap config:
python3 ~/.tab-ledger/kb_query.py context <project>   # At session start
python3 ~/.tab-ledger/kb_query.py search "query"       # During work
python3 ~/.tab-ledger/kb_query.py projects              # Discovery
```

A comprehensive handoff document is available at `KNOWLEDGE_BASE_HANDOFF.md` with full schema documentation, example queries, and integration patterns.

---

## File Reference

| File | Purpose |
|------|---------|
| `snapshot.py` | Tab capture engine (CDP + session file parsing) |
| `cc_indexer.py` | Claude Code JSONL parser and session indexer |
| `categorizer.py` | URL and session auto-categorization (20 categories) |
| `server.py` | FastAPI dashboard and REST API (port 7777) |
| `run_snapshot.py` | Launchd runner (snapshot + reindex, every 30 min) |
| `run_kb_refresh.py` | Nightly KB refresh (import + FTS + linking + optional semantic indexing) |
| `kb_build.py` | Core 0-7 KB build orchestrator + optional semantic stage |
| `kb_schema.py` | Database schema definitions + connection helper |
| `kb_taxonomy.py` | Project/sub-project path mapping |
| `kb_indexer.py` | Message-level JSONL indexer for KB |
| `kb_summarizer.py` | AI summarization (Opus/Haiku two-tier) |
| `kb_linker.py` | Cross-session connection detection |
| `kb_auxiliary.py` | Commands, plans, todos, teams indexer |
| `kb_semantic.py` | Semantic embedding index + semantic/hybrid retrieval |
| `kb_memory.py` | Continuity packet assembly for natural project resumption |
| `kb_query.py` | Python API + CLI for querying the KB |
| `kb_mcp_server.py` | Stdio MCP server for Claude Code integration |
| `com.rileycoyote.tab-ledger.plist` | macOS launchd agent (30-min snapshots) |
| `tests/test_kb_hardening.py` | Regression tests for hardening-critical behavior |
| `.github/workflows/ci.yml` | CI pipeline (compile + pytest) |
| `requirements-dev.txt` | Dev/test dependencies |
| `KNOWLEDGE_BASE_HANDOFF.md` | Agent integration reference |
| `DATA_SPEC.md` | Detailed data specification |

---

## Customization

### Adding Projects

Edit the `PROJECTS` list in `kb_taxonomy.py`. Each entry maps filesystem paths to canonical project names:

```python
("my-project", "My Project", "opus", [
    ("frontend", "Frontend App", "my-project/frontend"),
    ("backend", "Backend API", "my-project/backend"),
])
```

### Adding Tab Categories

Edit the `CATEGORIES` list in `categorizer.py`. Each entry is a regex pattern matched against URLs:

```python
("My Service", "#3B82F6", [
    r"myservice\.com",
    r"app\.myservice\.io",
]),
```

### Changing Snapshot Frequency

Edit the `StartInterval` in the launchd plist (value is in seconds; default 1800 = 30 minutes).

---

## Testing

Run local quality checks:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m compileall -q .
pytest -q
```

The CI workflow runs these checks on push and pull requests.

---

## Privacy & Security

- **Everything is local.** No data leaves your machine unless you explicitly run summarization, which sends sampled session context to Claude via your local authenticated CLI.
- **Databases are gitignored.** The `.gitignore` excludes `*.db`, `*.log`, and `*.pid`.
- **Read-only by default.** The query API and MCP server open the database in read-only mode.
- **No telemetry.** No analytics, no tracking, no external services beyond what you configure.

---

## License

MIT
