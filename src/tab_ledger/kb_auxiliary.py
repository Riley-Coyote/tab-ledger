"""Knowledge Base Auxiliary Data Indexer — Indexes supporting data sources.

This module indexes all supporting data sources into the knowledge base:
- Command history (~/.claude/history.jsonl)
- Plans (~/.claude/plans/*.md)
- Todos (~/.claude/todos/*.json)
- Teams (~/.claude/teams/*/config.json)
- Claude.ai conversations (~/.claude_history_search/conversations.db)

All indexing is idempotent (INSERT OR IGNORE / INSERT OR REPLACE).
Progress is tracked in kb_progress (stage='auxiliary').
"""

import json
import sqlite3
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from .kb_schema import get_kb_db
from .kb_taxonomy import map_session


# ═══════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════

from ._paths import HISTORY_FILE, PLANS_DIR, TODOS_DIR, TEAMS_DIR, CLAUDE_AI_DB






# ═══════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════

def get_project_id_from_path(kb: sqlite3.Connection, project_path: str) -> Optional[int]:
    """Get project_id from kb_projects using taxonomy mapping."""
    if not project_path:
        return None

    try:
        proj_canonical, _ = map_session(project_path)
        row = kb.execute(
            "SELECT id FROM kb_projects WHERE canonical_name = ?",
            (proj_canonical,)
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None


def get_session_id_from_uuid(kb: sqlite3.Connection, session_uuid: str) -> Optional[int]:
    """Get session_id from kb_sessions by UUID."""
    row = kb.execute(
        "SELECT id FROM kb_sessions WHERE session_uuid = ?",
        (session_uuid,)
    ).fetchone()
    return row["id"] if row else None


def extract_markdown_title(content: str) -> Optional[str]:
    """Extract the first markdown heading (# line) from content."""
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('# '):
            return line[2:].strip()
    return None


def extract_uuid_from_filename(filename: str) -> Optional[str]:
    """Extract UUID from todo filename.

    Handles formats:
    - {uuid}.json
    - {uuid}-agent-{agent_uuid}.json
    """
    basename = Path(filename).stem
    # Remove -agent-{uuid} suffix if present
    if '-agent-' in basename:
        return basename.split('-agent-')[0]
    return basename


# ═══════════════════════════════════════════════════════
# INDEXING FUNCTIONS
# ═══════════════════════════════════════════════════════

def index_commands() -> int:
    """Index command history from ~/.claude/history.jsonl.

    Returns:
        Number of commands indexed.
    """
    if not HISTORY_FILE.exists():
        print(f"  Command history not found: {HISTORY_FILE}")
        return 0

    kb = get_kb_db()
    count = 0
    errors = 0

    try:
        with open(HISTORY_FILE, 'r') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    if not line.strip():
                        continue

                    data = json.loads(line)
                    display = data.get("display", "")
                    timestamp = data.get("timestamp")
                    project_path = data.get("project", "")
                    has_pasted = bool(data.get("pastedContents"))

                    project_id = get_project_id_from_path(kb, project_path)

                    # Convert timestamp (milliseconds) to ISO format
                    if timestamp:
                        dt = datetime.utcfromtimestamp(timestamp / 1000.0)
                        ts_iso = dt.isoformat()
                    else:
                        ts_iso = datetime.utcnow().isoformat()

                    kb.execute("""
                        INSERT OR IGNORE INTO kb_commands
                        (command_text, project_path, project_id, timestamp, has_pasted_content)
                        VALUES (?, ?, ?, ?, ?)
                    """, (display, project_path, project_id, ts_iso, has_pasted))

                    # Also insert into FTS
                    if display:
                        kb.execute("""
                            INSERT INTO kb_fts (text, session_uuid, source_type, project_name)
                            VALUES (?, ?, 'command', ?)
                        """, (display, "", project_path))

                    count += 1

                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    errors += 1
                    if errors <= 5:  # Log first 5 errors only
                        print(f"    Error on line {line_num}: {e}")

                if count % 500 == 0:
                    kb.commit()
                    print(f"    Indexed {count} commands...")

        kb.commit()
        print(f"  Indexed {count} commands ({errors} errors)")
        return count

    except Exception as e:
        print(f"  Error indexing commands: {e}")
        return count
    finally:
        kb.close()


def index_plans() -> int:
    """Index plans from ~/.claude/plans/*.md.

    Returns:
        Number of plans indexed.
    """
    if not PLANS_DIR.exists():
        print(f"  Plans directory not found: {PLANS_DIR}")
        return 0

    kb = get_kb_db()
    count = 0

    try:
        for plan_file in sorted(PLANS_DIR.glob("*.md")):
            try:
                filename = plan_file.name
                slug = plan_file.stem  # filename without .md extension

                # Read file content
                with open(plan_file, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                # Extract title from first markdown heading
                title = extract_markdown_title(content)

                # Try to find session by slug matching
                session_id = None
                session_row = kb.execute(
                    "SELECT id FROM kb_sessions WHERE slug = ?",
                    (slug,)
                ).fetchone()
                if session_row:
                    session_id = session_row["id"]

                # Get project_id from session if found
                project_id = None
                if session_id:
                    proj_row = kb.execute(
                        "SELECT project_id FROM kb_sessions WHERE id = ?",
                        (session_id,)
                    ).fetchone()
                    if proj_row:
                        project_id = proj_row["project_id"]

                file_size = plan_file.stat().st_size

                kb.execute("""
                    INSERT OR REPLACE INTO kb_plans
                    (filename, slug, session_id, project_id, content, title, created_at, file_size)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    filename, slug, session_id, project_id, content, title,
                    datetime.utcnow().isoformat(), file_size
                ))

                # Insert into FTS
                if content:
                    kb.execute("""
                        INSERT INTO kb_fts (text, session_uuid, source_type, project_name)
                        VALUES (?, ?, 'plan', ?)
                    """, (content, slug, slug))

                count += 1

            except Exception as e:
                print(f"    Error indexing plan {plan_file.name}: {e}")

        kb.commit()
        print(f"  Indexed {count} plans")
        return count

    except Exception as e:
        print(f"  Error indexing plans: {e}")
        return count
    finally:
        kb.close()


def index_todos() -> int:
    """Index todos from ~/.claude/todos/*.json.

    Returns:
        Number of todo files indexed.
    """
    if not TODOS_DIR.exists():
        print(f"  Todos directory not found: {TODOS_DIR}")
        return 0

    kb = get_kb_db()
    count = 0

    try:
        for todo_file in sorted(TODOS_DIR.glob("*.json")):
            try:
                filename = todo_file.name

                # Extract session UUID from filename
                session_uuid = extract_uuid_from_filename(filename)
                if not session_uuid:
                    continue

                # Read and parse JSON
                with open(todo_file, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        items = json.load(f)
                    except json.JSONDecodeError:
                        items = []

                # Skip if empty
                if not items:
                    continue

                # Get session_id from kb_sessions
                session_id = get_session_id_from_uuid(kb, session_uuid)

                # Get project_id if session exists
                project_id = None
                if session_id:
                    proj_row = kb.execute(
                        "SELECT project_id FROM kb_sessions WHERE id = ?",
                        (session_id,)
                    ).fetchone()
                    if proj_row:
                        project_id = proj_row["project_id"]

                # Count items by status
                total_items = len(items)
                completed_items = sum(1 for item in items if item.get("status") == "completed")
                pending_items = sum(1 for item in items if item.get("status") == "pending")

                # Store items as JSON
                items_json = json.dumps(items)

                kb.execute("""
                    INSERT OR REPLACE INTO kb_todos
                    (filename, session_uuid, session_id, project_id, items_json,
                     total_items, completed_items, pending_items)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    filename, session_uuid, session_id, project_id, items_json,
                    total_items, completed_items, pending_items
                ))

                count += 1

            except Exception as e:
                print(f"    Error indexing todo {todo_file.name}: {e}")

        kb.commit()
        print(f"  Indexed {count} todo files")
        return count

    except Exception as e:
        print(f"  Error indexing todos: {e}")
        return count
    finally:
        kb.close()


def index_teams() -> int:
    """Index teams from ~/.claude/teams/*/config.json.

    Returns:
        Number of teams indexed.
    """
    if not TEAMS_DIR.exists():
        print(f"  Teams directory not found: {TEAMS_DIR}")
        return 0

    kb = get_kb_db()
    count = 0

    # Team name to project mapping
    team_to_project = {
        "sanctuary-rebuild": "sanctuary",
        "stardew-overhaul": "sanctuary",
        "vektor-bot": "vektor",
    }

    try:
        for team_dir in TEAMS_DIR.iterdir():
            if not team_dir.is_dir():
                continue

            config_file = team_dir / "config.json"
            if not config_file.exists():
                continue

            try:
                team_name = team_dir.name

                # Read config
                with open(config_file, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        config_data = json.load(f)
                    except json.JSONDecodeError:
                        config_data = {}

                config_json = json.dumps(config_data)

                # Extract description and member_count if present
                description = config_data.get("description")
                member_count = len(config_data.get("members", []))

                # Map team to project
                project_canonical = team_to_project.get(team_name)
                project_id = None
                if project_canonical:
                    proj_row = kb.execute(
                        "SELECT id FROM kb_projects WHERE canonical_name = ?",
                        (project_canonical,)
                    ).fetchone()
                    if proj_row:
                        project_id = proj_row["id"]

                kb.execute("""
                    INSERT OR REPLACE INTO kb_teams
                    (team_name, description, member_count, config_json, project_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    team_name, description, member_count, config_json, project_id,
                    datetime.utcnow().isoformat()
                ))

                count += 1

            except Exception as e:
                print(f"    Error indexing team {team_dir.name}: {e}")

        kb.commit()
        print(f"  Indexed {count} teams")
        return count

    except Exception as e:
        print(f"  Error indexing teams: {e}")
        return count
    finally:
        kb.close()


def index_claude_ai() -> int:
    """Index Claude.ai conversations from ~/.claude_history_search/conversations.db.

    Returns:
        Number of conversations indexed.
    """
    if not CLAUDE_AI_DB.exists():
        print(f"  Claude.ai database not found: {CLAUDE_AI_DB}")
        return 0

    kb = get_kb_db()
    count = 0

    try:
        kb.execute("DELETE FROM kb_claude_ai")
        kb.commit()

        # Open Claude.ai conversations database
        claude_ai = sqlite3.connect(CLAUDE_AI_DB)
        claude_ai.row_factory = sqlite3.Row

        # Query conversations table
        try:
            conversations = claude_ai.execute(
                "SELECT id, uuid, name, summary, created_at, updated_at, message_count FROM conversations"
            ).fetchall()
        except sqlite3.OperationalError:
            # Table doesn't exist or different schema
            print("  Claude.ai conversations table not found or incompatible schema")
            claude_ai.close()
            kb.close()
            return 0

        for conv in conversations:
            try:
                cols = set(conv.keys())
                conversation_uuid = conv["uuid"] if "uuid" in cols else None
                title = conv["name"] if "name" in cols else None
                message_count = conv["message_count"] if "message_count" in cols else 0
                created_at = conv["created_at"] if "created_at" in cols else None
                updated_at = conv["updated_at"] if "updated_at" in cols else None
                summary = conv["summary"] if "summary" in cols else None

                kb.execute("""
                    INSERT OR REPLACE INTO kb_claude_ai
                    (conversation_uuid, title, message_count, created_at, updated_at, summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    conversation_uuid, title, message_count, created_at, updated_at, summary
                ))

                count += 1

            except Exception as e:
                conv_uuid = conv["uuid"] if "uuid" in set(conv.keys()) else "unknown"
                print(f"    Error indexing conversation {conv_uuid}: {e}")

        kb.commit()
        print(f"  Indexed {count} Claude.ai conversations")

        claude_ai.close()
        return count

    except Exception as e:
        print(f"  Error indexing Claude.ai conversations: {e}")
        return count
    finally:
        kb.close()


# ═══════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════

def index_all_auxiliary() -> dict:
    """Index all auxiliary data sources.

    Returns:
        Dictionary with counts for each source type.
    """
    print("\n" + "=" * 70)
    print("AUXILIARY DATA INDEXING")
    print("=" * 70)

    kb = get_kb_db()

    # Update progress
    kb.execute(
        "UPDATE kb_progress SET status='running', started_at=? WHERE stage='auxiliary'",
        (datetime.utcnow().isoformat(),)
    )
    kb.commit()
    kb.close()

    print("\nIndexing data sources:")

    # Index each source
    counts = {
        "commands": index_commands(),
        "plans": index_plans(),
        "todos": index_todos(),
        "teams": index_teams(),
        "claude_ai_conversations": index_claude_ai(),
    }

    total = sum(counts.values())

    # Update progress
    kb = get_kb_db()
    kb.execute("""
        UPDATE kb_progress SET
            status='completed', processed=?, total=?,
            completed_at=?, notes=?
        WHERE stage='auxiliary'
    """, (
        total, total,
        datetime.utcnow().isoformat(),
        f"Commands: {counts['commands']}, Plans: {counts['plans']}, "
        f"Todos: {counts['todos']}, Teams: {counts['teams']}, "
        f"Claude.ai: {counts['claude_ai_conversations']}"
    ))
    kb.commit()
    kb.close()

    # Summary
    print("\n" + "-" * 70)
    print("INDEXING SUMMARY")
    print("-" * 70)
    for source_type, count in counts.items():
        print(f"  {source_type:25s} {count:6d}")
    print("-" * 70)
    print(f"  {'TOTAL':25s} {total:6d}")
    print("=" * 70 + "\n")

    return counts


# ═══════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    counts = index_all_auxiliary()

    # Return non-zero exit if any indexing failed
    if all(c > 0 for c in counts.values()):
        sys.exit(0)
    else:
        sys.exit(1)
