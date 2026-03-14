"""Knowledge Base Indexer — Parse JSONL session files and index messages.

This module finds all JSONL files across ~/.claude/projects/, parses them line-by-line
(handling files up to 1.8 GB), extracts user and assistant messages, and stores them
in the kb_messages table with full metadata.

Features:
- Stream-parses large JSONL files (memory-efficient line-by-line reading)
- Extracts text content from both user and assistant messages
- Tracks tool usage from assistant tool_use blocks
- Resumable — picks up from where it left off if interrupted
- Batch commits every 50 sessions for safety
- Maintains progress in kb_progress table
"""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Generator, Optional, Tuple, Dict, Any
from datetime import datetime

from kb_schema import get_kb_db
from kb_taxonomy import map_session


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def find_all_jsonl() -> Generator[Tuple[Path, str], None, None]:
    """Find all JSONL files in ~/.claude/projects/ and subdirectories.

    Yields:
        Tuple of (path: Path, session_uuid: str)
        where session_uuid is the filename without .jsonl extension.
    """
    if not CLAUDE_PROJECTS.exists():
        print(f"Warning: {CLAUDE_PROJECTS} does not exist")
        return

    for jsonl_file in CLAUDE_PROJECTS.rglob("*.jsonl"):
        # Skip non-session files
        if jsonl_file.name == "sessions-index.json":
            continue

        # Extract session UUID from filename
        session_uuid = jsonl_file.stem
        if session_uuid and len(session_uuid) > 0:
            yield jsonl_file, session_uuid


def _decode_project_hint(jsonl_path: Path) -> str:
    """Best-effort decode of encoded Claude project directory back to a path-like string."""
    try:
        rel = jsonl_path.relative_to(CLAUDE_PROJECTS)
    except ValueError:
        return str(jsonl_path.parent)

    if not rel.parts:
        return str(jsonl_path.parent)

    encoded_project = rel.parts[0]
    decoded = encoded_project.replace("-", "/")
    if decoded and not decoded.startswith("/"):
        decoded = "/" + decoded
    return decoded or str(jsonl_path.parent)


def _infer_project_ids(
    conn: sqlite3.Connection,
    jsonl_path: Path
) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[str]]:
    """Infer project/sub-project IDs from a JSONL path."""
    hints = [_decode_project_hint(jsonl_path), str(jsonl_path)]
    seen = set()

    for hint in hints:
        if hint in seen:
            continue
        seen.add(hint)

        proj_canonical, sub_canonical = map_session(hint)
        proj_row = conn.execute(
            "SELECT id, canonical_name FROM kb_projects WHERE canonical_name = ?",
            (proj_canonical,),
        ).fetchone()
        if not proj_row:
            continue

        project_id = proj_row[0]
        sub_row = conn.execute(
            """SELECT id FROM kb_sub_projects
               WHERE project_id = ? AND canonical_name = ?""",
            (project_id, sub_canonical),
        ).fetchone()

        return project_id, (sub_row[0] if sub_row else None), hint, proj_row[1]

    return None, None, None, None


def get_session_id_for_uuid(
    conn: sqlite3.Connection,
    session_uuid: str,
    jsonl_path: Optional[Path] = None
) -> Optional[int]:
    """Get internal session_id from session_uuid, creating entry if needed.

    Args:
        conn: Database connection
        session_uuid: UUID from JSONL filename
        jsonl_path: Optional JSONL path used to infer project assignment.

    Returns:
        Internal session_id or None if creation failed
    """
    # Check if session exists
    cursor = conn.execute(
        "SELECT id, project_id FROM kb_sessions WHERE session_uuid = ?",
        (session_uuid,)
    )
    row = cursor.fetchone()
    if row:
        session_id = row[0]
        project_id = row[1]
        if project_id is None and jsonl_path:
            inferred_project_id, inferred_sub_id, hint_path, inferred_project = _infer_project_ids(
                conn, jsonl_path
            )
            if inferred_project_id is not None:
                conn.execute(
                    """UPDATE kb_sessions
                       SET project_id = ?, sub_project_id = ?,
                           project_path = COALESCE(project_path, ?),
                           project_name_original = COALESCE(project_name_original, ?),
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (
                        inferred_project_id,
                        inferred_sub_id,
                        hint_path,
                        inferred_project,
                        session_id,
                    ),
                )
        return session_id

    # Create new session entry
    try:
        project_id = None
        sub_project_id = None
        hint_path = None
        inferred_project = None
        if jsonl_path:
            project_id, sub_project_id, hint_path, inferred_project = _infer_project_ids(conn, jsonl_path)

        if project_id is None:
            fallback = conn.execute(
                "SELECT id FROM kb_projects WHERE canonical_name = 'exploration'"
            ).fetchone()
            project_id = fallback[0] if fallback else None
            sub_fallback = conn.execute(
                """SELECT id FROM kb_sub_projects
                   WHERE project_id = ? AND canonical_name = 'root'""",
                (project_id,),
            ).fetchone() if project_id else None
            sub_project_id = sub_fallback[0] if sub_fallback else None
            inferred_project = inferred_project or "exploration"

        cursor = conn.execute(
            """INSERT INTO kb_sessions
               (session_uuid, project_id, sub_project_id, project_path, project_name_original)
               VALUES (?, ?, ?, ?, ?)""",
            (session_uuid, project_id, sub_project_id, hint_path, inferred_project)
        )
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        # Race condition - try again
        cursor = conn.execute(
            "SELECT id FROM kb_sessions WHERE session_uuid = ?",
            (session_uuid,)
        )
        row = cursor.fetchone()
        return row[0] if row else None


def _backfill_missing_project_assignments(
    kb_conn: sqlite3.Connection,
    all_jsonls: list[Tuple[Path, str]]
) -> int:
    """Backfill project assignments for sessions that were created without taxonomy mapping."""
    uuid_to_path = {}
    for jsonl_path, session_uuid in all_jsonls:
        uuid_to_path.setdefault(session_uuid, jsonl_path)

    rows = kb_conn.execute(
        "SELECT id, session_uuid FROM kb_sessions WHERE project_id IS NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        session_id, session_uuid = row
        jsonl_path = uuid_to_path.get(session_uuid)
        if not jsonl_path:
            continue

        project_id, sub_project_id, hint_path, inferred_project = _infer_project_ids(kb_conn, jsonl_path)
        if project_id is None:
            fallback = kb_conn.execute(
                "SELECT id FROM kb_projects WHERE canonical_name = 'exploration'"
            ).fetchone()
            project_id = fallback[0] if fallback else None
            sub_fallback = kb_conn.execute(
                """SELECT id FROM kb_sub_projects
                   WHERE project_id = ? AND canonical_name = 'root'""",
                (project_id,),
            ).fetchone() if project_id else None
            sub_project_id = sub_fallback[0] if sub_fallback else None
            inferred_project = inferred_project or "exploration"

        if project_id is None:
            continue

        kb_conn.execute(
            """UPDATE kb_sessions
               SET project_id = ?,
                   sub_project_id = COALESCE(sub_project_id, ?),
                   project_path = COALESCE(project_path, ?),
                   project_name_original = COALESCE(project_name_original, ?),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (project_id, sub_project_id, hint_path, inferred_project, session_id),
        )
        updated += 1

    if updated:
        kb_conn.commit()

    return updated


def extract_text_content(content_list) -> str:
    """Extract text content from a message.content field.

    Handles both formats:
      - Plain string (older sessions)
      - List of content blocks [{type: "text", text: "..."}]

    Filters out thinking blocks, tool_use blocks, and tool_result blocks.

    Args:
        content_list: String or list of content objects from message.content

    Returns:
        Concatenated text content, or empty string if none found
    """
    if isinstance(content_list, str):
        return content_list

    if not isinstance(content_list, list):
        return ""

    text_parts = []
    for item in content_list:
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict) and item.get("type") == "text":
            if "text" in item and isinstance(item["text"], str):
                text_parts.append(item["text"])

    return "".join(text_parts)


def extract_tool_names(content_list: list) -> Tuple[bool, str]:
    """Extract tool names from tool_use blocks in message content.

    Args:
        content_list: List of content objects from message.content

    Returns:
        Tuple of (has_tool_use: bool, tool_names: str)
        where tool_names is comma-separated and sorted
    """
    if not isinstance(content_list, list):
        return False, ""

    tool_names_set = set()
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            if "name" in item and isinstance(item["name"], str):
                tool_names_set.add(item["name"])

    if tool_names_set:
        return True, ",".join(sorted(tool_names_set))
    return False, ""


def extract_thinking_flag(content_list: list) -> bool:
    """Check if content has a thinking block.

    Args:
        content_list: List of content objects from message.content

    Returns:
        True if any item has type="thinking"
    """
    if not isinstance(content_list, list):
        return False

    return any(
        isinstance(item, dict) and item.get("type") == "thinking"
        for item in content_list
    )


def stream_parse_jsonl(file_path: Path) -> Generator[dict, None, None]:
    """Stream-parse a JSONL file line by line.

    Handles very large files (up to 1.8 GB) efficiently.

    Args:
        file_path: Path to JSONL file

    Yields:
        Parsed JSON dict for each line
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"Warning: {file_path} line {line_num}: {e}")
                    continue
    except (IOError, OSError) as e:
        print(f"Error reading {file_path}: {e}")


def index_session_messages(
    kb_conn: sqlite3.Connection,
    session_id: int,
    jsonl_path: Path,
    session_uuid: str
) -> int:
    """Index all messages from a JSONL file into kb_messages.

    Args:
        kb_conn: Database connection
        session_id: Internal session ID
        jsonl_path: Path to JSONL file
        session_uuid: Session UUID

    Returns:
        Number of messages indexed
    """
    message_count = 0
    message_index = 0

    # Track file size
    try:
        file_size = jsonl_path.stat().st_size
    except (OSError, FileNotFoundError):
        file_size = 0

    # Track session metadata
    session_data = {
        'first_timestamp': None,
        'last_timestamp': None,
        'model': None,
        'total_input_tokens': 0,
        'total_output_tokens': 0,
        'total_cache_creation': 0,
        'total_cache_read': 0,
        'user_message_count': 0,
        'assistant_message_count': 0,
        'tools_used': set(),
    }

    # Collect all messages first to establish metadata
    messages_to_insert = []

    for record in stream_parse_jsonl(jsonl_path):
        record_type = record.get("type")
        timestamp_str = record.get("timestamp")

        # Parse timestamp
        try:
            if timestamp_str:
                timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                if not session_data['first_timestamp']:
                    session_data['first_timestamp'] = timestamp
                session_data['last_timestamp'] = timestamp
        except (ValueError, AttributeError):
            timestamp = None

        # Process user messages
        if record_type == "user":
            message_obj = record.get("message", {})
            if isinstance(message_obj, dict):
                content_list = message_obj.get("content", [])
                content_text = extract_text_content(content_list)

                if content_text:  # Only index if there's actual text
                    messages_to_insert.append({
                        'message_index': message_index,
                        'message_type': 'user',
                        'role': message_obj.get('role', 'user'),
                        'content_text': content_text,
                        'content_length': len(content_text),
                        'has_thinking': False,
                        'has_tool_use': False,
                        'tool_names': None,
                        'stop_reason': None,
                        'tokens_in': 0,
                        'tokens_out': 0,
                        'model': None,
                        'timestamp': timestamp,
                    })
                    message_index += 1
                    session_data['user_message_count'] += 1

        # Process assistant messages
        elif record_type == "assistant":
            message_obj = record.get("message", {})
            if isinstance(message_obj, dict):
                content_list = message_obj.get("content", [])
                content_text = extract_text_content(content_list)

                # Extract metadata
                has_thinking = extract_thinking_flag(content_list)
                has_tool_use, tool_names = extract_tool_names(content_list)

                model = message_obj.get("model")
                stop_reason = message_obj.get("stop_reason")

                if model:
                    session_data['model'] = model

                # Extract token usage
                usage = message_obj.get("usage", {})
                tokens_in = usage.get("input_tokens", 0) or 0
                tokens_out = usage.get("output_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0

                session_data['total_input_tokens'] += tokens_in
                session_data['total_output_tokens'] += tokens_out
                session_data['total_cache_creation'] += cache_creation
                session_data['total_cache_read'] += cache_read

                # Track tools
                if tool_names:
                    for tool in tool_names.split(","):
                        session_data['tools_used'].add(tool)

                # Only index if there's actual text or tool use
                if content_text or has_tool_use:
                    messages_to_insert.append({
                        'message_index': message_index,
                        'message_type': 'assistant',
                        'role': message_obj.get('role', 'assistant'),
                        'content_text': content_text if content_text else None,
                        'content_length': len(content_text) if content_text else 0,
                        'has_thinking': has_thinking,
                        'has_tool_use': has_tool_use,
                        'tool_names': tool_names,
                        'stop_reason': stop_reason,
                        'tokens_in': tokens_in,
                        'tokens_out': tokens_out,
                        'model': model,
                        'timestamp': timestamp,
                    })
                    message_index += 1
                    session_data['assistant_message_count'] += 1

    # Insert all messages
    for msg_data in messages_to_insert:
        try:
            kb_conn.execute(
                """
                INSERT INTO kb_messages (
                    session_id, message_index, message_type, role, content_text,
                    content_length, has_thinking, has_tool_use, tool_names,
                    stop_reason, tokens_in, tokens_out, model, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    msg_data['message_index'],
                    msg_data['message_type'],
                    msg_data['role'],
                    msg_data['content_text'],
                    msg_data['content_length'],
                    msg_data['has_thinking'],
                    msg_data['has_tool_use'],
                    msg_data['tool_names'],
                    msg_data['stop_reason'],
                    msg_data['tokens_in'],
                    msg_data['tokens_out'],
                    msg_data['model'],
                    msg_data['timestamp'],
                )
            )
            message_count += 1
        except sqlite3.Error as e:
            print(f"Error inserting message for {session_uuid}: {e}")

    # Update session metadata
    try:
        tools_str = ",".join(sorted(session_data['tools_used'])) if session_data['tools_used'] else ""

        kb_conn.execute(
            """
            UPDATE kb_sessions SET
                jsonl_path = ?,
                jsonl_size_bytes = ?,
                message_count = ?,
                turn_count = ?,
                model = ?,
                started_at = ?,
                ended_at = ?,
                input_tokens = ?,
                output_tokens = ?,
                cache_creation_tokens = ?,
                cache_read_tokens = ?,
                tools_used = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                str(jsonl_path.relative_to(CLAUDE_PROJECTS)) if CLAUDE_PROJECTS in jsonl_path.parents else str(jsonl_path),
                file_size,
                message_count,
                session_data['assistant_message_count'],
                session_data['model'],
                session_data['first_timestamp'],
                session_data['last_timestamp'],
                session_data['total_input_tokens'],
                session_data['total_output_tokens'],
                session_data['total_cache_creation'],
                session_data['total_cache_read'],
                tools_str,
                session_id,
            )
        )
    except sqlite3.Error as e:
        print(f"Error updating session metadata for {session_uuid}: {e}")

    return message_count


def index_all_messages(resume: bool = True) -> dict:
    """Index all messages from all JSONL files.

    Finds all JSONL files, checks which sessions need indexing, and processes them.
    Batch commits every 50 sessions for safety. Resumable if interrupted.

    Args:
        resume: If True, skip sessions that already have messages indexed.
                If False, reindex everything.

    Returns:
        Dict with statistics about the indexing run
    """
    kb_conn = get_kb_db()
    stats = {
        'total_files_found': 0,
        'sessions_processed': 0,
        'sessions_skipped': 0,
        'messages_indexed': 0,
        'errors': 0,
        'start_time': datetime.now(),
    }

    try:
        # Find all JSONL files
        all_jsonls = list(find_all_jsonl())
        stats['total_files_found'] = len(all_jsonls)

        if not resume:
            # Full rebuild mode must not duplicate existing message rows.
            kb_conn.execute("DELETE FROM kb_messages")
            kb_conn.commit()

        # Repair any legacy sessions created without project mapping.
        repaired = _backfill_missing_project_assignments(kb_conn, all_jsonls)

        # Get set of sessions that already have messages in kb_messages table
        indexed_sessions = set()
        if resume:
            cursor = kb_conn.execute(
                """SELECT s.session_uuid FROM kb_sessions s
                   WHERE s.id IN (SELECT DISTINCT session_id FROM kb_messages)"""
            )
            indexed_sessions = {row[0] for row in cursor.fetchall()}

        print(f"Found {stats['total_files_found']} JSONL files")
        if resume:
            print(f"Already indexed: {len(indexed_sessions)} sessions")
        if repaired:
            print(f"Backfilled taxonomy for {repaired} orphan sessions")

        # Process in batches for safe commits
        batch_size = 50
        for batch_idx in range(0, len(all_jsonls), batch_size):
            batch = all_jsonls[batch_idx:batch_idx + batch_size]
            batch_messages = 0

            for jsonl_path, session_uuid in batch:
                # Skip if already indexed and resuming
                if resume and session_uuid in indexed_sessions:
                    stats['sessions_skipped'] += 1
                    continue

                # Get or create session ID
                session_id = get_session_id_for_uuid(kb_conn, session_uuid, jsonl_path=jsonl_path)
                if session_id is None:
                    stats['errors'] += 1
                    print(f"Failed to get session ID for {session_uuid}")
                    continue

                # Index messages from this JSONL
                try:
                    msg_count = index_session_messages(kb_conn, session_id, jsonl_path, session_uuid)
                    batch_messages += msg_count
                    stats['messages_indexed'] += msg_count
                    stats['sessions_processed'] += 1

                    if stats['sessions_processed'] % 10 == 0:
                        print(f"  Processed {stats['sessions_processed']} sessions, "
                              f"{stats['messages_indexed']} messages")
                except Exception as e:
                    stats['errors'] += 1
                    print(f"Error processing {jsonl_path}: {e}")

            # Commit batch
            try:
                kb_conn.commit()
                print(f"Batch {batch_idx // batch_size + 1}: "
                      f"committed {stats['sessions_processed']} sessions, {batch_messages} messages")
            except sqlite3.Error as e:
                print(f"Error committing batch: {e}")
                kb_conn.rollback()
                stats['errors'] += 1

        # Update progress table
        try:
            kb_conn.execute(
                """
                UPDATE kb_progress SET
                    status = 'completed',
                    processed = ?,
                    total = ?,
                    errors = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    notes = ?
                WHERE stage = 'message_indexing'
                """,
                (
                    stats['sessions_processed'],
                    stats['total_files_found'],
                    stats['errors'],
                    f"Indexed {stats['messages_indexed']} messages from "
                    f"{stats['sessions_processed']} sessions",
                )
            )
            kb_conn.commit()
        except sqlite3.Error as e:
            print(f"Error updating progress: {e}")

        stats['end_time'] = datetime.now()
        stats['duration_sec'] = (stats['end_time'] - stats['start_time']).total_seconds()

        return stats

    finally:
        kb_conn.close()


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Index JSONL session files into the knowledge base"
    )
    parser.add_argument(
        '--no-resume',
        action='store_true',
        help='Reindex all sessions, ignoring previously indexed ones'
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppress progress output'
    )

    args = parser.parse_args()

    print("=" * 70)
    print("Knowledge Base Message Indexer")
    print("=" * 70)
    print(f"Source: {CLAUDE_PROJECTS}")
    print(f"Resume: {not args.no_resume}")
    print()

    stats = index_all_messages(resume=not args.no_resume)

    print()
    print("=" * 70)
    print("Indexing Complete")
    print("=" * 70)
    print(f"Total JSONL files found: {stats['total_files_found']}")
    print(f"Sessions processed: {stats['sessions_processed']}")
    print(f"Sessions skipped: {stats['sessions_skipped']}")
    print(f"Messages indexed: {stats['messages_indexed']}")
    print(f"Errors: {stats['errors']}")
    print(f"Duration: {stats['duration_sec']:.2f}s")
    print()


if __name__ == "__main__":
    main()
