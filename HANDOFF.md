# Knowledge Base Builder — Claude Code Execution Protocol

## What This Is

A complete pipeline to build a Master Knowledge Base from ~1,290 Claude Code sessions (5.9 GB of JSONL files). The KB creates a structured, searchable, AI-queryable index of every conversation Riley has ever had with Claude Code, organized into 12 canonical projects with cross-session linking and AI-generated summaries.

The code was written and verified in Cowork mode. Your job is to execute it.

## Prerequisites

```bash
# Verify the code files exist
ls ~/.tab-ledger/kb_*.py

# You should see:
#   kb_schema.py       — Database schema creation
#   kb_taxonomy.py     — Project taxonomy and session import
#   kb_indexer.py      — JSONL message parser and indexer
#   kb_summarizer.py   — AI summarization pipeline
#   kb_linker.py       — Cross-session connection detector
#   kb_auxiliary.py    — Commands, plans, todos, teams indexer
#   kb_query.py        — CLI + Python query interface
#   kb_build.py        — Master orchestrator (this is what you run)

# Verify ledger.db exists (source data)
ls -la ~/.tab-ledger/ledger.db

# Verify JSONL source files exist
ls ~/.claude/projects/ | head -20

# Verify anthropic SDK is installed (needed for stage 4)
python3 -c "import anthropic; print('OK')"
# If not: pip install anthropic

# Verify ANTHROPIC_API_KEY is set (needed for stage 4 only)
echo $ANTHROPIC_API_KEY | head -c 10
```

## Execution

### Option A: Run Everything (Recommended First Time)

```bash
cd ~/.tab-ledger
python3 kb_build.py
```

This runs all 8 stages in sequence. Takes ~30-60 minutes depending on machine speed and API rate limits. Stage 4 (summarization) is the longest due to API calls.

### Option B: Skip Summarization (Faster, No API Costs)

```bash
cd ~/.tab-ledger
python3 kb_build.py --skip-summarize
```

Builds everything except summaries. The KB is still fully functional for searching, browsing, and connecting sessions. Summaries can be added later with `--from 4 --only 4`.

### Option C: Resume After Interruption

```bash
cd ~/.tab-ledger
python3 kb_build.py --from <stage_number>
```

All stages are resumable. If stage 2 crashes at session 800/1290, re-running `--from 2` will pick up where it left off.

### Option D: Drop and Rebuild

```bash
cd ~/.tab-ledger
python3 kb_build.py --drop
```

Destroys the existing knowledge_base.db and rebuilds from scratch. Use if the schema changed or data is corrupted.

## Stage Reference

| Stage | Name | What It Does | Duration | API? |
|-------|------|-------------|----------|------|
| 0 | Schema | Creates knowledge_base.db with 13 tables + 17 indexes | <1s | No |
| 1 | Taxonomy | Maps 1,290 sessions → 12 projects, imports from ledger.db | ~5s | No |
| 2 | Messages | Parses all 1,277 JSONL files, indexes messages into kb_messages | 10-30 min | No |
| 3 | FTS | Builds FTS5 full-text search index from messages + summaries | ~2 min | No |
| 4 | Summarize | Calls Anthropic API to generate structured summaries | 20-40 min | **Yes** |
| 5 | Linking | Detects parent-child, same-slug, continuation, branch connections | ~1 min | No |
| 6 | Auxiliary | Indexes commands, plans, todos, teams, claude.ai conversations | ~30s | No |
| 7 | Verify | Runs 9 integrity checks and prints summary | <1s | No |

## Stage 4 Details (Summarization)

This is the expensive stage. It calls the Anthropic API for every unsummarized session.

- **Two-tier model selection**: Opus for major projects (polyphonic, sanctuary, sigil, nexus, vessel, clawdbot, vektor, anima, data-research), Haiku for minor/ad-hoc
- **Intelligent content sampling**: Adjusts extraction strategy based on JSONL file size
- **Deep archives**: Files >100MB get special analysis stored in kb_deep_archives
- **Rate limited**: 1-second delay between calls, exponential backoff on errors
- **Resumable**: Only processes sessions with summary_version=0
- **Cost estimate**: ~$5-15 for full run depending on session sizes

To run summarization alone (after other stages are done):
```bash
python3 kb_build.py --only 4
```

To check how many sessions need summarization:
```bash
python3 kb_summarizer.py --dry-run
```

## After Building: Verify

The build finishes with a verification report. All 9 checks should pass:

```
✓ All sessions have project_id
✓ Project session counts sum correctly
✓ Messages indexed
✓ FTS index populated
✓ Summaries generated
✓ Connections created
✓ FTS search functional
✓ Auxiliary data indexed
✓ Database integrity
```

## After Building: Query

### CLI (for humans)

```bash
cd ~/.tab-ledger

# List all projects
python3 kb_query.py projects --human

# Deep-dive into a project
python3 kb_query.py project polyphonic --human

# Full-text search
python3 kb_query.py search "websocket authentication" --human

# Get continuation context for an agent
python3 kb_query.py context vessel

# Recent sessions
python3 kb_query.py recent 15 --human

# Project timeline
python3 kb_query.py timeline sanctuary --human

# Session iterations by phase
python3 kb_query.py iterations polyphonic --human
```

### Python API (for agents)

```python
from kb_query import KnowledgeBase

kb = KnowledgeBase()

# Get context for resuming work
context = kb.get_continuation_context("polyphonic")
print(context["last_session"]["summary_text"])
print(context["next_steps"])

# Search across everything
results = kb.search("SIGIL proof of authenticity")
for r in results:
    print(r["session_uuid"], r["text"][:100])

# Get full project info
project = kb.get_project("sanctuary")
print(f"Sessions: {project['total_sessions']}, Cost: ${project['total_cost_usd']}")

# Get related sessions
related = kb.get_related_sessions("some-session-uuid")

kb.close()
```

## Output Files

After a successful build, you'll have:

```
~/.tab-ledger/knowledge_base.db    # The Master Knowledge Base (SQLite)
~/.tab-ledger/ledger.db            # Unchanged — original session catalog
```

The knowledge_base.db contains 13 tables:

| Table | Purpose |
|-------|---------|
| kb_projects | 12 canonical projects with aggregated stats |
| kb_sub_projects | ~60 sub-projects with path patterns |
| kb_sessions | 1,290 sessions with project assignments, summaries, metadata |
| kb_messages | All indexed messages from JSONL files |
| kb_fts | FTS5 full-text search index (messages + summaries + commands + plans) |
| kb_connections | Cross-session relationship graph |
| kb_commands | Command history (2,444 entries) |
| kb_plans | Plans from ~/.claude/plans/ (39 files) |
| kb_todos | Todo lists from ~/.claude/todos/ (149 non-empty) |
| kb_teams | Team configs (3 teams) |
| kb_claude_ai | Claude.ai web conversations (532) |
| kb_deep_archives | Special analysis for 100MB+ sessions |
| kb_progress | Pipeline progress tracking |

## Adding to CLAUDE.md (Optional)

To make the KB available to all Claude Code sessions, add this to `~/.claude/CLAUDE.md`:

```markdown
## Knowledge Base

A Master Knowledge Base exists at `~/.tab-ledger/knowledge_base.db` containing
indexed, summarized, and cross-linked records of all Claude Code sessions.

Query it using:
```bash
python3 ~/.tab-ledger/kb_query.py context <project>  # Get continuation context
python3 ~/.tab-ledger/kb_query.py search "<query>"    # Full-text search
python3 ~/.tab-ledger/kb_query.py projects            # List all projects
```

Or via Python:
```python
sys.path.insert(0, str(Path.home() / ".tab-ledger"))
from kb_query import KnowledgeBase
kb = KnowledgeBase()
context = kb.get_continuation_context("polyphonic")
```

Projects: polyphonic, sanctuary, sigil, nexus, vessel, clawdbot, vektor, anima,
tools, data-research, exploration
```

## Troubleshooting

**Stage 2 is slow on large files**: The three 100MB+ JSONL files (two at 1.8GB, one at 478MB) take significant time to parse. This is expected. Progress is printed every 10 sessions.

**Stage 4 API errors**: The summarizer retries 3 times with exponential backoff. If you hit rate limits, wait a few minutes and resume with `--from 4`.

**FTS index missing entries after stage 6**: Stage 3 builds FTS before auxiliary data is indexed. The auxiliary indexer (stage 6) inserts its own FTS entries for commands and plans. If you re-run stage 3 after stage 6, it will skip if FTS already has entries. To rebuild FTS from scratch:
```python
from kb_schema import get_kb_db
kb = get_kb_db()
kb.execute("DELETE FROM kb_fts")
kb.commit()
kb.close()
# Then re-run stages 3 and 6
python3 kb_build.py --only 3
python3 kb_build.py --only 6
```

**"ModuleNotFoundError: No module named 'kb_schema'"**: Make sure you're running from `~/.tab-ledger/` directory, or add it to your Python path:
```bash
cd ~/.tab-ledger && python3 kb_build.py
```
