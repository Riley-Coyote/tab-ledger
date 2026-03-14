"""
Cross-session connection detection and linking for the knowledge base.

Analyzes temporal proximity, git branches, session slugs, and parent-child
relationships to build a comprehensive connection graph in kb_connections.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import logging

from .kb_schema import get_kb_db

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from ._paths import CLAUDE_PROJECTS
PROGRESS_STAGE = "linking"


def load_jsonl_file(path: Path) -> List[Dict]:
    """Load and parse a JSONL file."""
    records = []
    if not path.exists():
        return records
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON in {path}: {line[:50]}")
    except Exception as e:
        logger.error(f"Error reading {path}: {e}")
    return records


def get_parent_child_map() -> Dict[str, str]:
    """
    Build a map of parent session UUIDs to their subagent session UUIDs.

    Structure: ~/.claude/projects/{hashed-project-path}/subagents/{agent-uuid}.jsonl
    The parent session is identified by finding which top-level JSONL files
    in the same project directory spawned these subagents.

    Since we can't directly determine the parent UUID from directory structure alone,
    we find all top-level sessions in the same project dir as each subagents/ folder,
    and link subagent UUIDs to the most likely parent (by checking is_sidechain flag
    in the database and temporal proximity).

    Returns: {agent_uuid: parent_uuid} mapping
    """
    parent_map: Dict[str, str] = {}

    if not CLAUDE_PROJECTS.exists():
        logger.info(f"Projects directory not found: {CLAUDE_PROJECTS}")
        return parent_map

    try:
        for project_dir in CLAUDE_PROJECTS.iterdir():
            if not project_dir.is_dir():
                continue

            # Modern Claude layout:
            #   <project>/<parent-session-uuid>/subagents/<child-session-uuid>.jsonl
            for jsonl_file in project_dir.glob("*/subagents/*.jsonl"):
                parent_uuid = jsonl_file.parent.parent.name
                child_uuid = jsonl_file.stem
                if parent_uuid and child_uuid:
                    parent_map[child_uuid] = parent_uuid

            # Back-compat layout:
            #   <project>/subagents/<child-session-uuid>.jsonl
            legacy_subagents = list((project_dir / "subagents").glob("*.jsonl"))
            if legacy_subagents:
                top_level = [f.stem for f in project_dir.glob("*.jsonl")]
                if len(top_level) == 1:
                    parent_uuid = top_level[0]
                    for jsonl_file in legacy_subagents:
                        parent_map[jsonl_file.stem] = parent_uuid
                elif top_level:
                    parent_uuid = top_level[0]
                    for jsonl_file in legacy_subagents:
                        parent_map.setdefault(jsonl_file.stem, parent_uuid)

    except Exception as e:
        logger.error(f"Error scanning projects directory: {e}")
        return {}

    logger.info(f"Found {len(parent_map)} potential parent-child mappings")
    return parent_map


def detect_parent_child(kb_conn: sqlite3.Connection) -> int:
    """
    Detect parent-child relationships from subagent JSONL structure.

    Subagent sessions are linked to their parent session.
    Connection type: parent_child (strength: 1.0)
    Direction: parent → child

    Args:
        kb_conn: Knowledge base database connection

    Returns:
        Number of connections created
    """
    logger.info("Detecting parent-child relationships...")

    cursor = kb_conn.cursor()
    created = 0

    # Get map of agent_id -> parent_uuid
    parent_map = get_parent_child_map()

    if not parent_map:
        logger.info("No subagent relationships found")
        return 0

    # For each subagent, find its session and parent session
    for agent_id, parent_uuid in parent_map.items():
        try:
            # Find parent session by UUID
            cursor.execute(
                "SELECT id FROM kb_sessions WHERE session_uuid = ?",
                (parent_uuid,)
            )
            parent_result = cursor.fetchone()

            if not parent_result:
                logger.debug(f"Parent session not found for UUID {parent_uuid}")
                continue

            parent_session_id = parent_result[0]

            # Find child session by session_uuid (agent_id)
            cursor.execute(
                "SELECT id FROM kb_sessions WHERE session_uuid = ?",
                (agent_id,)
            )
            child_result = cursor.fetchone()

            if not child_result:
                logger.debug(f"Child session not found for UUID {agent_id}")
                continue

            child_session_id = child_result[0]

            # Insert connection (parent → child)
            cursor.execute(
                """
                INSERT OR IGNORE INTO kb_connections
                (source_session_id, target_session_id, connection_type, strength, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    parent_session_id,
                    child_session_id,
                    'parent_child',
                    1.0,
                    f'Subagent relationship: {agent_id} is subagent of {parent_uuid}'
                )
            )
            if cursor.rowcount > 0:
                created += 1
            logger.debug(f"Created parent-child connection: {parent_session_id} → {child_session_id}")

        except Exception as e:
            logger.error(f"Error linking parent-child for {agent_id}: {e}")

    kb_conn.commit()
    logger.info(f"Parent-child connections created: {created}")
    return created


def detect_same_slug(kb_conn: sqlite3.Connection) -> int:
    """
    Detect sessions sharing the same slug (resumed sessions).

    Sessions with identical slugs are the same Claude Code session resumed.
    Connection type: same_slug (strength: 0.95)
    Direction: earlier → later (by started_at)

    Args:
        kb_conn: Knowledge base database connection

    Returns:
        Number of connections created
    """
    logger.info("Detecting same-slug relationships...")

    cursor = kb_conn.cursor()
    created = 0

    try:
        # Find slugs that appear more than once
        cursor.execute(
            """
            SELECT slug, COUNT(*) as cnt
            FROM kb_sessions
            WHERE slug IS NOT NULL AND slug != ''
            GROUP BY slug
            HAVING COUNT(*) > 1
            ORDER BY slug
            """
        )

        duplicate_slugs = cursor.fetchall()
        logger.info(f"Found {len(duplicate_slugs)} duplicate slugs")

        for slug, count in duplicate_slugs:
            # Get all sessions with this slug, ordered by started_at
            cursor.execute(
                """
                SELECT id, started_at
                FROM kb_sessions
                WHERE slug = ?
                ORDER BY started_at ASC
                """,
                (slug,)
            )

            sessions = cursor.fetchall()

            # Connect each session to the next one chronologically
            for i in range(len(sessions) - 1):
                source_id, source_time = sessions[i]
                target_id, target_time = sessions[i + 1]

                cursor.execute(
                    """
                    INSERT OR IGNORE INTO kb_connections
                    (source_session_id, target_session_id, connection_type, strength, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        target_id,
                        'same_slug',
                        0.95,
                        f'Same session resumed: slug={slug}'
                    )
                )
                if cursor.rowcount > 0:
                    created += 1
                logger.debug(f"Created same-slug connection: {source_id} → {target_id}")

    except Exception as e:
        logger.error(f"Error detecting same-slug relationships: {e}")

    kb_conn.commit()
    logger.info(f"Same-slug connections created: {created}")
    return created


def detect_continuations(kb_conn: sqlite3.Connection) -> int:
    """
    Detect continuation sessions - sequential work on same project within 4 hours.

    Connection type: continuation
    Strength: 1.0 - (gap_hours / 4.0), clamped to [0, 1]
    Direction: earlier → later
    Only connects non-sidechain sessions.

    Args:
        kb_conn: Knowledge base database connection

    Returns:
        Number of connections created
    """
    logger.info("Detecting continuation relationships...")

    cursor = kb_conn.cursor()
    created = 0

    try:
        # Find sessions grouped by project, ordered by started_at
        cursor.execute(
            """
            SELECT id, project_id, started_at, is_sidechain
            FROM kb_sessions
            WHERE project_id IS NOT NULL
            ORDER BY project_id, started_at ASC
            """
        )

        sessions = cursor.fetchall()

        # Group by project_id
        project_sessions: Dict[Optional[int], List[Tuple]] = {}
        for session in sessions:
            project_id = session[1]
            if project_id not in project_sessions:
                project_sessions[project_id] = []
            project_sessions[project_id].append(session)

        # Check consecutive sessions in each project
        for project_id, project_session_list in project_sessions.items():
            for i in range(len(project_session_list) - 1):
                source_id, _, source_time_str, source_sidechain = project_session_list[i]
                target_id, _, target_time_str, target_sidechain = project_session_list[i + 1]

                # Skip if either is a sidechain
                if source_sidechain or target_sidechain:
                    continue

                # Skip if already connected (check for existing connections)
                cursor.execute(
                    """
                    SELECT 1 FROM kb_connections
                    WHERE source_session_id = ? AND target_session_id = ?
                    """,
                    (source_id, target_id)
                )
                if cursor.fetchone():
                    continue

                # Parse timestamps
                try:
                    source_time = datetime.fromisoformat(source_time_str)
                    target_time = datetime.fromisoformat(target_time_str)
                except (ValueError, TypeError):
                    logger.debug(f"Invalid timestamp for sessions {source_id}/{target_id}")
                    continue

                # Calculate gap
                gap = target_time - source_time
                gap_hours = gap.total_seconds() / 3600.0

                # Only connect if within 4 hours
                if gap_hours > 4.0:
                    continue

                # Calculate strength
                strength = max(0.0, 1.0 - (gap_hours / 4.0))

                cursor.execute(
                    """
                    INSERT OR IGNORE INTO kb_connections
                    (source_session_id, target_session_id, connection_type, strength, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        target_id,
                        'continuation',
                        strength,
                        f'Continuation: same project, {gap_hours:.2f} hours apart'
                    )
                )
                if cursor.rowcount > 0:
                    created += 1
                logger.debug(
                    f"Created continuation connection: {source_id} → {target_id} "
                    f"(gap: {gap_hours:.2f}h, strength: {strength:.3f})"
                )

    except Exception as e:
        logger.error(f"Error detecting continuation relationships: {e}")

    kb_conn.commit()
    logger.info(f"Continuation connections created: {created}")
    return created


def detect_branch_links(kb_conn: sqlite3.Connection) -> int:
    """
    Detect sessions on the same git branch within the same project.

    Connection type: branch (strength: 0.9)
    Direction: earlier → later
    Only connects sessions on non-main branches.

    Args:
        kb_conn: Knowledge base database connection

    Returns:
        Number of connections created
    """
    logger.info("Detecting branch-based relationships...")

    cursor = kb_conn.cursor()
    created = 0

    try:
        # Find sessions grouped by project and branch
        cursor.execute(
            """
            SELECT id, project_id, git_branch, started_at
            FROM kb_sessions
            WHERE project_id IS NOT NULL
              AND git_branch IS NOT NULL
              AND git_branch != ''
              AND git_branch != 'main'
            ORDER BY project_id, git_branch, started_at ASC
            """
        )

        sessions = cursor.fetchall()

        # Group by (project_id, git_branch)
        branch_groups: Dict[Tuple[Optional[int], str], List[Tuple]] = {}
        for session in sessions:
            session_id, project_id, git_branch, started_at = session
            key = (project_id, git_branch)
            if key not in branch_groups:
                branch_groups[key] = []
            branch_groups[key].append(session)

        # Connect consecutive sessions on same branch
        for (project_id, git_branch), branch_session_list in branch_groups.items():
            if len(branch_session_list) < 2:
                continue

            for i in range(len(branch_session_list) - 1):
                source_id = branch_session_list[i][0]
                target_id = branch_session_list[i + 1][0]

                # Check if already connected
                cursor.execute(
                    """
                    SELECT 1 FROM kb_connections
                    WHERE source_session_id = ? AND target_session_id = ?
                    """,
                    (source_id, target_id)
                )
                if cursor.fetchone():
                    continue

                cursor.execute(
                    """
                    INSERT OR IGNORE INTO kb_connections
                    (source_session_id, target_session_id, connection_type, strength, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        target_id,
                        'branch',
                        0.9,
                        f'Same branch: project_id={project_id}, branch={git_branch}'
                    )
                )
                if cursor.rowcount > 0:
                    created += 1
                logger.debug(
                    f"Created branch connection: {source_id} → {target_id} "
                    f"(branch: {git_branch})"
                )

    except Exception as e:
        logger.error(f"Error detecting branch relationships: {e}")

    kb_conn.commit()
    logger.info(f"Branch connections created: {created}")
    return created


def update_progress(kb_conn: sqlite3.Connection, stage: str, message: str) -> None:
    """Update progress tracking for the linking stage."""
    try:
        kb_conn.execute(
            """
            UPDATE kb_progress SET status='running', notes=?, started_at=?
            WHERE stage=?
            """,
            (message, datetime.now().isoformat(), stage)
        )
        kb_conn.commit()
    except Exception as e:
        logger.error(f"Error updating progress: {e}")


def build_all_connections() -> Dict[str, int]:
    """
    Build all cross-session connections.

    Processes each connection type in sequence and tracks progress.

    Returns:
        Dictionary with stats: {type: count, ...}
    """
    logger.info("Starting connection linking process...")

    stats = {
        'parent_child': 0,
        'same_slug': 0,
        'continuation': 0,
        'branch': 0,
        'total': 0
    }

    try:
        kb_conn = get_kb_db()

        # Ensure kb_connections table exists
        cursor = kb_conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_connections (
                id INTEGER PRIMARY KEY,
                source_session_id INTEGER NOT NULL REFERENCES kb_sessions(id),
                target_session_id INTEGER NOT NULL REFERENCES kb_sessions(id),
                connection_type TEXT NOT NULL,
                strength REAL DEFAULT 1.0,
                reason TEXT,
                UNIQUE(source_session_id, target_session_id, connection_type)
            )
            """
        )
        kb_conn.commit()

        # Detect parent-child relationships
        update_progress(kb_conn, PROGRESS_STAGE, "Detecting parent-child relationships...")
        stats['parent_child'] = detect_parent_child(kb_conn)

        # Detect same-slug relationships
        update_progress(kb_conn, PROGRESS_STAGE, "Detecting same-slug relationships...")
        stats['same_slug'] = detect_same_slug(kb_conn)

        # Detect continuations
        update_progress(kb_conn, PROGRESS_STAGE, "Detecting continuation relationships...")
        stats['continuation'] = detect_continuations(kb_conn)

        # Detect branch links
        update_progress(kb_conn, PROGRESS_STAGE, "Detecting branch-based relationships...")
        stats['branch'] = detect_branch_links(kb_conn)

        # Calculate total and log summary
        stats['total'] = sum(
            stats[k] for k in ['parent_child', 'same_slug', 'continuation', 'branch']
        )

        summary_msg = (
            f"Linking complete. Created: "
            f"parent_child={stats['parent_child']}, "
            f"same_slug={stats['same_slug']}, "
            f"continuation={stats['continuation']}, "
            f"branch={stats['branch']}, "
            f"total={stats['total']}"
        )

        # Final progress update with completion
        kb_conn.execute("""
            UPDATE kb_progress SET
                status='completed', processed=?, total=?,
                completed_at=?, notes=?
            WHERE stage=?
        """, (stats['total'], stats['total'], datetime.now().isoformat(),
              summary_msg, PROGRESS_STAGE))
        kb_conn.commit()
        logger.info(summary_msg)

        kb_conn.close()

    except Exception as e:
        logger.error(f"Fatal error in build_all_connections: {e}")
        raise

    return stats


if __name__ == "__main__":
    """Main entry point for connection linking."""
    stats = build_all_connections()

    print("\nConnection Linking Summary:")
    print(f"  Parent-Child: {stats['parent_child']}")
    print(f"  Same Slug: {stats['same_slug']}")
    print(f"  Continuation: {stats['continuation']}")
    print(f"  Branch: {stats['branch']}")
    print(f"  Total: {stats['total']}")
