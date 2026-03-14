import sqlite3
from pathlib import Path


def _create_ledger_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE cc_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT UNIQUE,
            project_path TEXT,
            project_name TEXT,
            git_branch TEXT,
            summary TEXT,
            first_prompt TEXT,
            category TEXT,
            message_count INTEGER,
            model TEXT,
            started_at TEXT,
            ended_at TEXT,
            slug TEXT,
            is_sidechain BOOLEAN,
            turn_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_creation_tokens INTEGER,
            cache_read_tokens INTEGER,
            total_duration_ms INTEGER,
            cost_usd REAL,
            tools_used TEXT,
            tool_call_count INTEGER,
            claude_code_version TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO cc_sessions (
            session_id, project_path, project_name, git_branch, summary, first_prompt,
            category, message_count, model, started_at, ended_at, slug, is_sidechain,
            turn_count, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            total_duration_ms, cost_usd, tools_used, tool_call_count, claude_code_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sess-1",
            "/Users/example/Documents/Repositories/polyphonic-twitter-bot",
            "polyphonic-twitter-bot",
            "main",
            "initial",
            "build feature",
            "Polyphonic",
            3,
            "claude-opus-4-6",
            "2026-02-01T00:00:00+00:00",
            "2026-02-01T00:05:00+00:00",
            "slug-a",
            0,
            2,
            100,
            50,
            10,
            5,
            60000,
            1.23,
            "Read",
            2,
            "2.0.0",
        ),
    )
    conn.commit()
    conn.close()


def test_taxonomy_upsert_preserves_existing_enrichment(tmp_path, monkeypatch):
    import kb_schema
    import kb_taxonomy

    kb_path = tmp_path / "knowledge_base.db"
    ledger_path = tmp_path / "ledger.db"

    monkeypatch.setattr(kb_schema, "KB_DB", kb_path)
    monkeypatch.setattr(kb_taxonomy, "LEDGER_DB", ledger_path)

    _create_ledger_db(ledger_path)
    kb_schema.create_schema(drop_existing=True)
    kb_taxonomy.build_taxonomy()

    kb = sqlite3.connect(kb_path)
    row = kb.execute(
        "SELECT id FROM kb_sessions WHERE session_uuid = 'sess-1'"
    ).fetchone()
    assert row is not None
    original_id = row[0]
    kb.execute(
        "UPDATE kb_sessions SET summary_text = ?, summary_version = 1 WHERE session_uuid = ?",
        ("keep me", "sess-1"),
    )
    kb.commit()
    kb.close()

    ledger = sqlite3.connect(ledger_path)
    ledger.execute(
        "UPDATE cc_sessions SET tools_used = ?, tool_call_count = ? WHERE session_id = ?",
        ("Read,Write", 3, "sess-1"),
    )
    ledger.commit()
    ledger.close()

    kb_taxonomy.build_taxonomy()

    kb = sqlite3.connect(kb_path)
    row = kb.execute(
        """
        SELECT id, summary_text, summary_version, tools_used, tool_call_count
        FROM kb_sessions
        WHERE session_uuid = 'sess-1'
        """
    ).fetchone()
    kb.close()

    assert row[0] == original_id
    assert row[1] == "keep me"
    assert row[2] == 1
    assert row[3] == "Read,Write"
    assert row[4] == 3


def test_indexer_assigns_project_for_new_session(tmp_path, monkeypatch):
    import kb_schema
    import kb_indexer

    kb_path = tmp_path / "knowledge_base.db"
    monkeypatch.setattr(kb_schema, "KB_DB", kb_path)
    kb_schema.create_schema(drop_existing=True)

    kb = kb_schema.get_kb_db()
    kb.execute(
        "INSERT INTO kb_projects (canonical_name, display_name) VALUES ('polyphonic', 'Polyphonic')"
    )
    poly_id = kb.execute(
        "SELECT id FROM kb_projects WHERE canonical_name = 'polyphonic'"
    ).fetchone()[0]
    kb.execute(
        """
        INSERT INTO kb_sub_projects (project_id, canonical_name, display_name, path_pattern)
        VALUES (?, 'twitter-bot', 'Twitter Bot', 'polyphonic-twitter-bot')
        """,
        (poly_id,),
    )
    kb.execute(
        "INSERT INTO kb_projects (canonical_name, display_name) VALUES ('exploration', 'Exploration')"
    )
    exp_id = kb.execute(
        "SELECT id FROM kb_projects WHERE canonical_name = 'exploration'"
    ).fetchone()[0]
    kb.execute(
        """
        INSERT INTO kb_sub_projects (project_id, canonical_name, display_name, path_pattern)
        VALUES (?, 'root', 'Root', '(catch-all)')
        """,
        (exp_id,),
    )
    kb.commit()

    claude_projects = tmp_path / "claude-projects"
    session_dir = claude_projects / "-Users-example-Documents-Repositories-polyphonic-twitter-bot"
    session_dir.mkdir(parents=True)
    jsonl_path = session_dir / "abc123.jsonl"
    jsonl_path.write_text('{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}\n')

    monkeypatch.setattr(kb_indexer, "CLAUDE_PROJECTS", claude_projects)

    session_id = kb_indexer.get_session_id_for_uuid(kb, "abc123", jsonl_path=jsonl_path)
    assert session_id is not None

    row = kb.execute(
        "SELECT project_id, sub_project_id FROM kb_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    kb.close()

    assert row[0] is not None


def test_linker_detects_nested_subagent_layout(tmp_path, monkeypatch):
    import kb_linker

    claude_projects = tmp_path / "projects"
    project_dir = claude_projects / "encoded-project"
    nested = project_dir / "parent-uuid-1" / "subagents"
    nested.mkdir(parents=True)
    (nested / "child-uuid-1.jsonl").write_text("{}\n")

    monkeypatch.setattr(kb_linker, "CLAUDE_PROJECTS", claude_projects)
    parent_map = kb_linker.get_parent_child_map()

    assert parent_map["child-uuid-1"] == "parent-uuid-1"


def test_auxiliary_claude_ai_indexing_handles_sqlite_rows(tmp_path, monkeypatch):
    import kb_schema
    import kb_auxiliary

    kb_path = tmp_path / "knowledge_base.db"
    claude_ai_db = tmp_path / "conversations.db"

    monkeypatch.setattr(kb_schema, "KB_DB", kb_path)
    monkeypatch.setattr(kb_auxiliary, "CLAUDE_AI_DB", claude_ai_db)

    kb_schema.create_schema(drop_existing=True)

    source = sqlite3.connect(claude_ai_db)
    source.execute(
        """
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            uuid TEXT,
            name TEXT,
            summary TEXT,
            created_at TEXT,
            updated_at TEXT,
            message_count INTEGER
        )
        """
    )
    source.execute(
        """
        INSERT INTO conversations (uuid, name, summary, created_at, updated_at, message_count)
        VALUES ('conv-1', 'Test', 'Summary', '2026-02-01T00:00:00+00:00', '2026-02-01T00:01:00+00:00', 4)
        """
    )
    source.commit()
    source.close()

    count = kb_auxiliary.index_claude_ai()
    assert count == 1

    kb = sqlite3.connect(kb_path)
    row = kb.execute(
        "SELECT conversation_uuid, title, message_count FROM kb_claude_ai"
    ).fetchone()
    kb.close()

    assert row == ("conv-1", "Test", 4)
