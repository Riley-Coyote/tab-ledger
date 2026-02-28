# Claude Code Data — Complete Reference Spec

> **Purpose:** Reference document describing all accessible Claude Code data on Riley's system.
> Hand this to any agent to build tools, dashboards, analytics, or search against this data.

---

## Data Source 1: Conversation Transcripts (JSONL)

**The richest data source. Every Claude Code session is recorded as a JSONL file.**

### Location

```
~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl
```

The `<encoded-project-path>` replaces `/` with `-` in the project directory path.
Example: `/Users/rileycoyote/Documents/Repositories/Polyphonic` becomes `-Users-rileycoyote-Documents-Repositories-Polyphonic`.

### Scale

- **~1,275 JSONL files** across **52 project directories**
- **~5.9 GB total**
- Date range: December 2025 → present

### Top projects by session count

| Project Directory | Sessions |
|---|---|
| `polyphonic-twitter-bot` | 35 |
| `The-Sanctuary/files` | 27 |
| `Polyphonic/branches/Opus4-6-branch` | 12 |
| Root (`-Users-rileycoyote`) | 7+ |

### JSONL Record Types

Each line is a self-contained JSON object. The `type` field determines the schema:

| `type` | Frequency | What It Contains |
|---|---|---|
| `user` | ~25% of lines | Human messages + tool results |
| `assistant` | ~37% | Claude responses + tool calls + token usage |
| `progress` | ~35% | Tool execution streaming progress |
| `system` | ~2% | Lifecycle events, hook summaries, turn duration |
| `file-history-snapshot` | ~1% | File state snapshots for undo |
| `summary` | rare | AI-generated session summary |

### Common Fields (All Record Types)

```json
{
  "type": "user|assistant|system|progress|...",
  "uuid": "d681e437-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "parentUuid": "8c35e18e-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "sessionId": "fe6d2760-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "isSidechain": false,
  "userType": "external",
  "cwd": "/Users/rileycoyote/path/to/project",
  "version": "2.1.62",
  "gitBranch": "vessel-chat",
  "slug": "functional-brewing-hinton",
  "timestamp": "2026-02-27T09:39:10.973Z"
}
```

Key fields:
- `uuid` / `parentUuid` — form a conversation tree (linked list of turns)
- `sessionId` — groups all messages in one session (matches the filename)
- `isSidechain` — `true` for sub-agent/teammate conversations
- `cwd` — working directory at time of message
- `version` — Claude Code version
- `gitBranch` — active git branch
- `slug` — human-readable session name
- `timestamp` — ISO 8601

### User Message Schema

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {"type": "text", "text": "The actual human message text"},
      {
        "type": "tool_result",
        "tool_use_id": "toolu_01XXXX",
        "content": "stdout from the tool",
        "is_error": false
      }
    ]
  },
  "toolUseResult": {
    "stdout": "...",
    "stderr": "...",
    "interrupted": false,
    "isImage": false,
    "noOutputExpected": false
  }
}
```

- When responding to a tool call: `content` array contains `tool_result` objects
- When sending a prompt: `content` array contains a single `text` object
- `toolUseResult` has structured stdout/stderr (only present when returning tool output)

### Assistant Message Schema

```json
{
  "type": "assistant",
  "requestId": "req_011CYY5xxxx",
  "message": {
    "model": "claude-opus-4-6",
    "id": "msg_01LQQxxxx",
    "type": "message",
    "role": "assistant",
    "content": [
      {
        "type": "thinking",
        "thinking": "Internal reasoning text...",
        "signature": "base64-encoded-signature"
      },
      {
        "type": "text",
        "text": "The response shown to the user"
      },
      {
        "type": "tool_use",
        "id": "toolu_01XXXX",
        "name": "Bash",
        "input": {
          "command": "git status",
          "description": "Check repo status"
        },
        "caller": {"type": "direct"}
      }
    ],
    "stop_reason": "end_turn|tool_use",
    "usage": {
      "input_tokens": 10,
      "output_tokens": 125,
      "cache_creation_input_tokens": 43733,
      "cache_read_input_tokens": 11497,
      "cache_creation": {
        "ephemeral_1h_input_tokens": 43733,
        "ephemeral_5m_input_tokens": 0
      },
      "server_tool_use": {
        "web_search_requests": 0,
        "web_fetch_requests": 0
      },
      "service_tier": "standard",
      "inference_geo": "",
      "speed": "standard"
    }
  }
}
```

Key extractable data:
- **`message.model`** — which Claude model was used (e.g., `claude-opus-4-6`, `claude-opus-4-5-20251101`, `claude-sonnet-4-5-20250929`)
- **`message.content`** — array of thinking blocks, text blocks, and tool_use blocks
- **`message.content[].name`** (on tool_use) — tool name: `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`, `Task`, `WebSearch`, `WebFetch`, etc.
- **`message.content[].input`** — tool parameters (varies by tool)
- **`message.usage`** — complete token accounting per turn (input, output, cache creation, cache read)
- **`stop_reason`** — `"end_turn"` (finished) or `"tool_use"` (needs tool result to continue)

### System Message Subtypes

```json
// Turn duration tracking
{
  "type": "system",
  "subtype": "turn_duration",
  "durationMs": 6405,
  "isMeta": false
}

// Hook execution summary
{
  "type": "system",
  "subtype": "stop_hook_summary",
  "hookCount": 1,
  "hookInfos": [{"command": "...", "durationMs": 13}],
  "hookErrors": [],
  "preventedContinuation": false,
  "toolUseID": "toolu_01XXXX"
}
```

---

## Data Source 2: Session Index Files

**Fast-path metadata without parsing full JSONLs.**

### Location

```
~/.claude/projects/<encoded-project-path>/sessions-index.json
```

Present in **21 of 52** project directories.

### Schema

```json
{
  "version": 1,
  "entries": [
    {
      "sessionId": "d3dac2f4-xxxx",
      "fullPath": "/Users/rileycoyote/.claude/projects/.../d3dac2f4.jsonl",
      "fileMtime": 1770063324749,
      "firstPrompt": "hey claude, can you...",
      "messageCount": 42,
      "created": "2026-01-21T07:42:38.303Z",
      "modified": "2026-01-21T07:42:50.631Z",
      "gitBranch": "main",
      "projectPath": "/Users/rileycoyote/Documents/Repositories/The-Sanctuary",
      "isSidechain": false
    }
  ]
}
```

**What's included:** sessionId, firstPrompt, messageCount, created/modified timestamps, gitBranch, projectPath, isSidechain
**What's NOT included:** model, summary, token usage, tool counts — these require parsing the JSONL

---

## Data Source 3: Usage Analytics Cache

### Location

```
~/.claude/stats-cache.json
```

### Schema (11.8 KB)

```json
{
  "version": 2,
  "lastComputedDate": "2026-02-18",
  "totalSessions": 256,
  "totalMessages": 106350,
  "firstSessionDate": "2025-12-02",
  "longestSession": {
    "sessionId": "...",
    "duration": 123456,
    "messageCount": 7437,
    "timestamp": "..."
  },
  "dailyActivity": [
    {"date": "2026-02-18", "messageCount": 450, "sessionCount": 5, "toolCallCount": 120}
  ],
  "dailyModelTokens": {
    "2026-02-18": {
      "claude-opus-4-6": {
        "inputTokens": 5000,
        "outputTokens": 2000,
        "cacheCreationTokens": 50000,
        "cacheReadTokens": 200000
      }
    }
  },
  "modelUsage": {
    "claude-opus-4-5-20251101": {
      "inputTokens": 2300000,
      "outputTokens": 1400000,
      "cacheCreationTokens": 295000000,
      "cacheReadTokens": 3200000000
    },
    "claude-opus-4-6": { "..." : "..." }
  },
  "hourCounts": [0, 0, 0, 0, 5, 12, 30, ...],
  "totalSpeculationTimeSavedMs": 0
}
```

Aggregate stats: **256 total sessions, 106K messages, first session Dec 2, 2025.** 42 days of daily breakdowns. Per-model lifetime token totals. Hourly activity distribution.

---

## Data Source 4: Active Session Database (SQLite)

### Location

```
~/.claude/__store.db
```

**Format:** SQLite (managed by Drizzle ORM)

### Tables

| Table | Purpose | Key Fields |
|---|---|---|
| `base_messages` | Message metadata | uuid, parent_uuid, session_id, timestamp, message_type, cwd, user_type, isSidechain |
| `user_messages` | Human turns | uuid, message (JSON), tool_use_result (JSON), timestamp |
| `assistant_messages` | Claude turns | uuid, cost_usd, duration_ms, message (JSON), model, timestamp |
| `conversation_summaries` | Compressed context | leaf_uuid, summary, updated_at |

**Note:** This DB only holds the **current/most recent session**. Historical sessions are in the JSONL files. The `assistant_messages` table uniquely has `cost_usd` per turn.

---

## Data Source 5: Command Input History

### Location

```
~/.claude/history.jsonl
```

**Format:** JSONL, 2,441 entries, 1.75 MB

### Schema

```json
{
  "display": "hey claude id like for you to explore the codebase...",
  "pastedContents": {},
  "timestamp": 1759304505576,
  "project": "/Users/rileycoyote/Documents/Repositories/Visual-chat-liminal-board"
}
```

Every command/prompt typed into Claude Code, with timestamp and which project it was sent from. `pastedContents` references cached paste data (see paste-cache below).

---

## Data Source 6: Plan Mode Documents

### Location

```
~/.claude/plans/*.md
```

**Count:** 39 markdown files
**Naming:** Human-readable slugs (e.g., `functional-brewing-hinton.md`, `breezy-exploring-micali.md`)

These are the planning documents created when Claude enters "plan mode" before making changes. Full markdown with sections, tables, code blocks, verification steps.

---

## Data Source 7: Todo/Task Lists

### Location

```
~/.claude/todos/*.json
```

**Count:** 584 JSON files
**Naming:** `{session-uuid}.json` or `{session-uuid}-agent-{agent-uuid}.json`

### Schema

```json
[
  {
    "content": "Examine current nexus CLI codebase",
    "status": "completed",
    "priority": "high",
    "id": "1"
  },
  {
    "content": "Fix identified errors",
    "status": "pending",
    "priority": "medium",
    "id": "2"
  }
]
```

Status values: `pending`, `in_progress`, `completed`. Priority: `high`, `medium`, `low`.

---

## Data Source 8: Multi-Agent Team Configs

### Location

```
~/.claude/teams/*/config.json
```

**Count:** 3 teams: `sanctuary-rebuild`, `stardew-overhaul`, `vektor-bot`

### Schema

```json
{
  "name": "sanctuary-rebuild",
  "description": "Rebuild The Sanctuary website with new design",
  "createdAt": 1770353391560,
  "leadAgentId": "team-lead@sanctuary-rebuild",
  "leadSessionId": "6064c38c-xxxx",
  "members": [
    {
      "agentId": "team-lead@sanctuary-rebuild",
      "name": "team-lead",
      "agentType": "team-lead",
      "model": "claude-opus-4-6",
      "joinedAt": 1770353391560,
      "cwd": "/Users/rileycoyote/...",
      "prompt": "Full system prompt for this agent...",
      "color": "blue",
      "planModeRequired": false,
      "backendType": "in-process"
    }
  ]
}
```

Each member has: name, model, full system prompt, CWD, join time.

---

## Data Source 9: Debug Logs

### Location

```
~/.claude/debug/*.txt
```

**Count:** 308 files, named by session UUID

### Content

Timestamped internal debug traces: hook execution, plugin loading, permission checks, session lifecycle, terminal rendering.

```
2026-02-02T14:10:26.053Z [DEBUG] Aborting: tool=ExitPlanMode isAbort=undefined
2026-02-02T14:10:26.162Z [DEBUG] AutoUpdaterWrapper: Installation type: npm-global
2026-02-02T14:10:26.242Z [DEBUG] Plugin not available for MCP: ralph-wiggum@claude-plugins-official
```

---

## Data Source 10: Custom Memory System

### Location

```
~/.claude_memory/memory.json
```

**Format:** JSON object, 83 memory entries, 101 KB

### Schema

```json
{
  "mem_a7b987c3_1755654488808": {
    "id": "mem_a7b987c3_1755654488808",
    "content": "Global memory system installed at...",
    "type": "context|fact|project|code|preference|task",
    "priority": "critical|high|medium|low",
    "tags": ["installation", "system"],
    "created_at": "2025-08-19T20:48:08.808698",
    "modified_at": "2025-08-19T20:48:08.808699",
    "accessed_at": "2025-08-19T20:48:08.808699",
    "access_count": 0,
    "context": {"directory": "/Users/rileycoyote"}
  }
}
```

**CLI access:**
```bash
/opt/homebrew/bin/python3 ~/.claude_memory/claude_memory.py search "keyword"
/opt/homebrew/bin/python3 ~/.claude_memory/claude_memory.py list
/opt/homebrew/bin/python3 ~/.claude_memory/claude_memory.py stats
```

Distribution: 38 facts, 14 project, 12 context, 9 code, 7 preference, 3 task. 47 high priority, 28 critical.

---

## Data Source 11: Claude.ai Conversation History (Separate from Claude Code)

### Search Index

```
~/.claude_history_search/conversations.db
```

**Format:** SQLite with FTS5 full-text search
**Coverage:** 532 conversations, 6,783 messages (3,393 human, 3,390 assistant)
**Date range:** 2024-07-22 to 2026-01-22

### Tables

```sql
-- Conversations
CREATE TABLE conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid          TEXT UNIQUE NOT NULL,
    name          TEXT,
    summary       TEXT,
    created_at    DATETIME,
    updated_at    DATETIME,
    message_count INTEGER DEFAULT 0
);

-- Messages
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    uuid            TEXT,
    sender          TEXT NOT NULL,  -- 'human' or 'assistant'
    text            TEXT,
    created_at      DATETIME
);

-- Full-text search (porter stemmer + unicode61)
CREATE VIRTUAL TABLE messages_fts USING fts5(
    text, content='messages', content_rowid='id',
    tokenize='porter unicode61'
);
```

**CLI access:**
```bash
/opt/homebrew/bin/python3 ~/.claude_history_search/search.py "query"
/opt/homebrew/bin/python3 ~/.claude_history_search/search.py "query" --context
/opt/homebrew/bin/python3 ~/.claude_history_search/search.py "query" --sender human
/opt/homebrew/bin/python3 ~/.claude_history_search/search.py "query" --after 2025-01-01
```

### Raw Export Data

```
~/Documents/chat_convo_history/claude/data-2026-01-22-10-33-40-batch-0000/
```

Contains Anthropic data export files:
- `conversations.json` — full conversation data with all messages
- `projects.json` — Claude.ai projects (10 projects)
- `memories.json` — Anthropic-side memory

**conversations.json schema:**
```json
[
  {
    "uuid": "f0d60c48-xxxx",
    "name": "Infinite Visual Workspace App",
    "created_at": "2025-08-17T09:28:19.879012Z",
    "updated_at": "2025-08-17T09:51:57.664419Z",
    "account": {"uuid": "49f295d8-xxxx"},
    "chat_messages": [
      {
        "uuid": "6037789e-xxxx",
        "text": "build a beautiful infinite canvas...",
        "sender": "human",
        "created_at": "2025-08-17T09:28:20.349512Z",
        "attachments": [],
        "files": []
      }
    ]
  }
]
```

Multiple export batches exist:
- `data-2025-08-18-01-39-09-batch-0000/` — 51 MB, 384 conversations
- `data-2026-01-22-10-33-40-batch-0000/` — more recent
- `data-2026-01-27-01-41-44-batch-0000.zip` — compressed

---

## Data Source 12: Misc Supporting Data

### Shell Snapshots

```
~/.claude/shell-snapshots/snapshot-zsh-{timestamp}-{random}.sh
```
233 files. Captures complete shell state (functions, aliases) at session start.

### Session Environment Variables

```
~/.claude/session-env/{session-uuid}.json
```
60 files. Environment variables at session start.

### Paste Cache

```
~/.claude/paste-cache/{content-hash}.txt
```
36 files. Cached content pasted into Claude Code prompts. Referenced by `pastedContents` in `history.jsonl`.

### File History (Undo Snapshots)

```
~/.claude/file-history/{session-uuid}/
```
162 entries. Before/after snapshots of files modified during sessions. Powers the undo functionality.

### Feature Flags (Statsig)

```
~/.claude/statsig/
```
16 files. Statsig feature flag evaluations. Contains `feature_gates`, `dynamic_configs` for enabled experiments.

### Installed Plugins

```
~/.claude/plugins/installed_plugins.json
```
Currently installed: `playwright`, `canvas`, `frontend-design`, `plugin-dev`, `ralph-loop`, `playground`, `claude-delegator`, and more.

### Installed Skills

```
~/.claude/skills/
```
16 skills: `brand-guidelines`, `browser-use`, `canvas-design`, `design-ui`, `find-skills`, `frontend-design`, `gsap-fundamentals`, `scroll-storyteller`, `social-growth-engineer`, `solana`, `threejs-postprocessing`, `ui-animation`, `ui-ux-pro-max`, `web-design-expert`.

---

## Tab Ledger Indexed Database

The Tab Ledger at `~/.tab-ledger/ledger.db` indexes all CC session data with comprehensive fields.

### cc_sessions Table Schema

```sql
CREATE TABLE cc_sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT UNIQUE,          -- UUID from JSONL filename
    project_path TEXT,               -- Working directory
    project_name TEXT,               -- Human-readable project name
    git_branch TEXT,                 -- Active git branch
    summary TEXT,                    -- AI-generated summary (when available)
    first_prompt TEXT,               -- First human message (500 char max)
    category TEXT,                   -- Auto-categorized (Polyphonic, Sanctuary, etc.)
    message_count INTEGER,           -- Count of user-type records
    model TEXT,                      -- Most-used Claude model in session
    started_at TIMESTAMP,            -- First record timestamp
    ended_at TIMESTAMP,              -- Last record timestamp
    indexed_at TIMESTAMP,            -- When indexed
    slug TEXT,                       -- Human-readable session slug
    is_sidechain BOOLEAN,            -- True for sub-agent/teammate sessions
    turn_count INTEGER,              -- Count of assistant messages
    input_tokens INTEGER,            -- Total input tokens across all turns
    output_tokens INTEGER,           -- Total output tokens across all turns
    cache_creation_tokens INTEGER,   -- Total cache write tokens
    cache_read_tokens INTEGER,       -- Total cache read tokens
    total_duration_ms INTEGER,       -- Summed turn durations
    cost_usd REAL,                   -- Estimated cost from token counts
    tools_used TEXT,                 -- Comma-separated sorted tool names
    tool_call_count INTEGER,         -- Total tool invocations
    claude_code_version TEXT         -- CC version string
);
```

### Coverage

- **1,290 sessions indexed** (as of 2026-02-27)
- **42 projects** tracked
- **$12,344 total estimated cost**
- **31,068 tool calls** catalogued
- **1,058 sidechain sessions** identified

### API Endpoints (server at localhost:7777)

| Endpoint | Returns |
|---|---|
| `GET /api/cc/stats` | Aggregate totals, per-model/project/tool/category breakdowns |
| `GET /api/cc/session/{id}` | Full session detail with all fields |
| `GET /api/cc/timeline?days=N` | Daily activity with tokens, cost, tool calls |
| `GET /api/cc/tools` | Tool usage analytics with model/project co-occurrence |
| `GET /api/cc/models` | Model usage breakdown with daily trends |
| `POST /api/reindex?force=true` | Re-index all sessions from JSONL files |
| `GET /api/search?q=keyword` | Search across sessions by text |

### Re-indexing

```bash
cd ~/.tab-ledger && /opt/homebrew/bin/python3 cc_indexer.py          # index new sessions only
cd ~/.tab-ledger && /opt/homebrew/bin/python3 cc_indexer.py --force  # full re-index
```

---

## Summary: What You Can Build With This

| Data | Source | Scale |
|---|---|---|
| Every Claude Code conversation transcript | JSONL files in `~/.claude/projects/` | 1,275 sessions, 5.9 GB |
| Per-turn token usage & model | Assistant messages in JSONL | Every turn |
| Per-turn cost | `__store.db` `assistant_messages.cost_usd` (current session only) | Current session |
| Tool usage (which tools, how often) | `tool_use` blocks in assistant messages | Every tool call |
| Turn-by-turn timing | `system` records with `subtype: turn_duration` | Every turn |
| Session metadata (branch, project, timestamps) | `sessions-index.json` + JSONL headers | All sessions |
| Indexed session analytics | `~/.tab-ledger/ledger.db` `cc_sessions` table | 1,290 sessions |
| Command/prompt history | `~/.claude/history.jsonl` | 2,441 entries |
| Plan documents | `~/.claude/plans/*.md` | 39 plans |
| Task lists | `~/.claude/todos/*.json` | 584 lists |
| Multi-agent team runs | `~/.claude/teams/*/config.json` | 3 teams |
| Debug/diagnostic logs | `~/.claude/debug/*.txt` | 308 sessions |
| Aggregate usage analytics | `~/.claude/stats-cache.json` | Lifetime stats |
| Claude.ai web conversations (separate) | `~/.claude_history_search/conversations.db` | 532 convos, 6,783 msgs |
| Anthropic data exports | `~/Documents/chat_convo_history/claude/` | Multiple batches |
| Custom cross-session memory | `~/.claude_memory/memory.json` | 83 memories |
| File edit history | `~/.claude/file-history/` | 162 sessions |
