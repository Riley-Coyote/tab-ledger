"""Knowledge Base Schema — Creates and manages the KB database.

This module creates the knowledge_base.db alongside the existing ledger.db.
It does NOT modify ledger.db — the KB is a separate, enriched view of the data.
"""

import sqlite3
from pathlib import Path

KB_DB = Path.home() / ".tab-ledger" / "knowledge_base.db"


def get_kb_db(readonly: bool = False) -> sqlite3.Connection:
    """Get a connection to the knowledge base database."""
    if readonly:
        conn = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(KB_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def create_schema(drop_existing: bool = False):
    """Create all knowledge base tables.

    Args:
        drop_existing: If True, drops all kb_ tables before recreating.
                       USE WITH CAUTION — destroys all indexed data.
    """
    conn = sqlite3.connect(KB_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if drop_existing:
        # Drop in reverse dependency order
        tables = [
            "kb_fts", "kb_connections", "kb_messages",
            "kb_commands", "kb_plans", "kb_todos", "kb_teams",
            "kb_claude_ai", "kb_deep_archives", "kb_progress",
            "kb_sessions", "kb_sub_projects", "kb_projects",
        ]
        for table in tables:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            except sqlite3.OperationalError:
                pass

    conn.executescript("""
        -- ═══════════════════════════════════════════════════════
        -- CANONICAL PROJECT REGISTRY
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_projects (
            id INTEGER PRIMARY KEY,
            canonical_name TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'active',
            first_session_at TIMESTAMP,
            last_session_at TIMESTAMP,
            total_sessions INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0.0,
            summarization_tier TEXT DEFAULT 'haiku',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS kb_sub_projects (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES kb_projects(id),
            canonical_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            path_pattern TEXT NOT NULL,
            description TEXT,
            session_count INTEGER DEFAULT 0,
            UNIQUE(project_id, canonical_name)
        );

        -- ═══════════════════════════════════════════════════════
        -- SESSION METADATA
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_sessions (
            id INTEGER PRIMARY KEY,
            session_uuid TEXT UNIQUE NOT NULL,
            project_id INTEGER REFERENCES kb_projects(id),
            sub_project_id INTEGER REFERENCES kb_sub_projects(id),

            -- Original metadata
            project_path TEXT,
            project_name_original TEXT,
            git_branch TEXT,
            slug TEXT,
            model TEXT,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            message_count INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            is_sidechain BOOLEAN DEFAULT FALSE,

            -- Token accounting
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            total_duration_ms INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,

            -- Tool usage
            tools_used TEXT DEFAULT '',
            tool_call_count INTEGER DEFAULT 0,

            -- Enhanced metadata
            summary_json TEXT,
            summary_text TEXT,
            phase TEXT,
            outcome TEXT,
            first_prompt TEXT,

            -- File tracking
            jsonl_path TEXT,
            jsonl_size_bytes INTEGER,

            -- Versioning
            cc_version TEXT,
            summary_version INTEGER DEFAULT 0,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ═══════════════════════════════════════════════════════
        -- MESSAGE-LEVEL INDEX
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_messages (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES kb_sessions(id) ON DELETE CASCADE,
            message_index INTEGER NOT NULL,
            message_type TEXT NOT NULL,
            role TEXT,
            content_text TEXT,
            content_length INTEGER DEFAULT 0,
            has_thinking BOOLEAN DEFAULT FALSE,
            has_tool_use BOOLEAN DEFAULT FALSE,
            tool_names TEXT,
            stop_reason TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            model TEXT,
            timestamp TIMESTAMP
        );

        -- ═══════════════════════════════════════════════════════
        -- FULL-TEXT SEARCH (FTS5)
        -- ═══════════════════════════════════════════════════════

        CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
            text,
            session_uuid,
            source_type,
            project_name,
            tokenize = 'porter unicode61'
        );

        -- ═══════════════════════════════════════════════════════
        -- CROSS-SESSION CONNECTIONS
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_connections (
            id INTEGER PRIMARY KEY,
            source_session_id INTEGER NOT NULL REFERENCES kb_sessions(id),
            target_session_id INTEGER NOT NULL REFERENCES kb_sessions(id),
            connection_type TEXT NOT NULL,
            strength REAL DEFAULT 1.0,
            reason TEXT,
            UNIQUE(source_session_id, target_session_id, connection_type)
        );

        -- ═══════════════════════════════════════════════════════
        -- AUXILIARY DATA
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_commands (
            id INTEGER PRIMARY KEY,
            command_text TEXT NOT NULL,
            project_path TEXT,
            project_id INTEGER REFERENCES kb_projects(id),
            timestamp TIMESTAMP NOT NULL,
            has_pasted_content BOOLEAN DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS kb_plans (
            id INTEGER PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            slug TEXT,
            session_id INTEGER REFERENCES kb_sessions(id),
            project_id INTEGER REFERENCES kb_projects(id),
            content TEXT,
            title TEXT,
            created_at TIMESTAMP,
            file_size INTEGER
        );

        CREATE TABLE IF NOT EXISTS kb_todos (
            id INTEGER PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            session_uuid TEXT,
            session_id INTEGER REFERENCES kb_sessions(id),
            project_id INTEGER REFERENCES kb_projects(id),
            items_json TEXT,
            total_items INTEGER DEFAULT 0,
            completed_items INTEGER DEFAULT 0,
            pending_items INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS kb_teams (
            id INTEGER PRIMARY KEY,
            team_name TEXT UNIQUE NOT NULL,
            description TEXT,
            member_count INTEGER,
            config_json TEXT,
            project_id INTEGER REFERENCES kb_projects(id),
            created_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS kb_claude_ai (
            id INTEGER PRIMARY KEY,
            conversation_uuid TEXT UNIQUE,
            title TEXT,
            message_count INTEGER,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            summary TEXT
        );

        -- ═══════════════════════════════════════════════════════
        -- DEEP ARCHIVES (special treatment for monster sessions)
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_deep_archives (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES kb_sessions(id),
            archive_type TEXT NOT NULL,
            analysis_json TEXT,
            content_breakdown TEXT,
            data_sources_discovered TEXT,
            processing_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- ═══════════════════════════════════════════════════════
        -- PROGRESS TRACKER
        -- ═══════════════════════════════════════════════════════

        CREATE TABLE IF NOT EXISTS kb_progress (
            id INTEGER PRIMARY KEY,
            stage TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'pending',
            processed INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            notes TEXT
        );
    """)

    # Create indexes (separate from executescript for better error handling)
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_project ON kb_sessions(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_sub_project ON kb_sessions(sub_project_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_date ON kb_sessions(started_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_uuid ON kb_sessions(session_uuid)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_slug ON kb_sessions(slug)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_sidechain ON kb_sessions(is_sidechain)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sessions_phase ON kb_sessions(phase)",
        "CREATE INDEX IF NOT EXISTS idx_kb_messages_session ON kb_messages(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_messages_type ON kb_messages(message_type)",
        "CREATE INDEX IF NOT EXISTS idx_kb_connections_source ON kb_connections(source_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_connections_target ON kb_connections(target_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_connections_type ON kb_connections(connection_type)",
        "CREATE INDEX IF NOT EXISTS idx_kb_commands_project ON kb_commands(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_commands_time ON kb_commands(timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_kb_plans_project ON kb_plans(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_plans_session ON kb_plans(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_todos_session ON kb_todos(session_id)",
    ]
    for idx_sql in indexes:
        conn.execute(idx_sql)

    # Initialize progress records
    stages = [
        "taxonomy", "session_import", "message_indexing",
        "fts_build", "summarization", "linking",
        "auxiliary", "verification",
    ]
    for stage in stages:
        conn.execute(
            "INSERT OR IGNORE INTO kb_progress (stage, status) VALUES (?, 'pending')",
            (stage,)
        )

    conn.commit()
    conn.close()
    print(f"Knowledge base schema created at {KB_DB}")
    print(f"  Tables: 13 (including FTS5)")
    print(f"  Indexes: {len(indexes)}")
    print(f"  Progress stages: {len(stages)}")


def verify_schema() -> dict:
    """Verify the schema exists and return table counts."""
    conn = get_kb_db(readonly=True)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    result = {}
    for t in tables:
        name = t["name"]
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
            result[name] = count
        except sqlite3.OperationalError:
            result[name] = -1
    conn.close()
    return result


if __name__ == "__main__":
    import sys
    drop = "--drop" in sys.argv
    if drop:
        print("WARNING: Dropping all existing KB tables!")
        confirm = input("Type 'yes' to confirm: ")
        if confirm != "yes":
            print("Aborted.")
            sys.exit(1)
    create_schema(drop_existing=drop)
    counts = verify_schema()
    print("\nTable verification:")
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count} rows")
