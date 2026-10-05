"""Keeping the KB current: grown sessions, search, freshness, taxonomy file."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

T1 = "2026-03-01T10:00:00.000Z"
T2 = "2026-03-01T11:00:00.000Z"


def _exchange(question: str, answer: str, ts: str) -> str:
    user = {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": question}]},
    }
    assistant = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": answer}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    return json.dumps(user) + "\n" + json.dumps(assistant) + "\n"


def _kb_with_catch_all(tmp_path, monkeypatch):
    from tab_ledger import kb_schema

    monkeypatch.setattr(kb_schema, "KB_DB", tmp_path / "knowledge_base.db")
    kb_schema.create_schema(drop_existing=True)
    kb = kb_schema.get_kb_db()
    kb.execute(
        "INSERT INTO kb_projects (canonical_name, display_name) VALUES ('exploration', 'Exploration')"
    )
    kb.execute(
        """INSERT INTO kb_sub_projects (project_id, canonical_name, display_name, path_pattern)
           VALUES (1, 'root', 'Root', '(catch-all)')"""
    )
    kb.commit()
    return kb


def test_ledger_index_picks_up_grown_transcript(tmp_path, monkeypatch):
    from tab_ledger import cc_indexer, snapshot

    ledger = tmp_path / "ledger.db"
    projects = tmp_path / "projects"
    transcript = projects / "-Users-example-Documents-Repositories-demo" / "sess-grow.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_exchange("first question", "first answer", T1))

    monkeypatch.setattr(snapshot, "LEDGER_DB", ledger)
    monkeypatch.setattr(cc_indexer, "LEDGER_DB", ledger)
    monkeypatch.setattr(cc_indexer, "CLAUDE_PROJECTS", projects)
    snapshot.init_db()
    conn = sqlite3.connect(ledger)
    conn.execute("ALTER TABLE cc_sessions DROP COLUMN jsonl_size")  # a ledger from before the column
    conn.close()

    assert cc_indexer.index_all()["new"] == 1

    with open(transcript, "a") as f:
        f.write(_exchange("second question", "second answer", T2))
    result = cc_indexer.index_all()
    assert (result["new"], result["updated"]) == (0, 1)

    conn = sqlite3.connect(ledger)
    row = conn.execute(
        "SELECT message_count, ended_at, jsonl_size FROM cc_sessions WHERE session_id = 'sess-grow'"
    ).fetchone()
    conn.close()
    assert row == (2, T2, transcript.stat().st_size)

    assert cc_indexer.index_all()["updated"] == 0


def test_grown_session_is_reindexed_and_searchable(tmp_path, monkeypatch):
    from tab_ledger import kb_build, kb_indexer
    from tab_ledger.kb_query import KnowledgeBase

    _kb_with_catch_all(tmp_path, monkeypatch).close()
    projects = tmp_path / "projects"
    transcript = projects / "-Users-example-demo" / "sess-live.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_exchange("tell me about the aardvark", "the aardvark digs", T1))
    monkeypatch.setattr(kb_indexer, "CLAUDE_PROJECTS", projects)

    kb_indexer.index_all_messages()
    kb_build.stage_3_fts()
    with KnowledgeBase() as kb:
        assert [r["session_uuid"] for r in kb.search("aardvark")] == ["sess-live"]

    with open(transcript, "a") as f:
        f.write(_exchange("and the zebra?", "the zebra runs", T2))
    assert kb_indexer.index_all_messages()["sessions_reindexed"] == 1

    with KnowledgeBase() as kb:
        assert [r["session_uuid"] for r in kb.search("zebra")] == ["sess-live"]
        messages = kb.conn.execute("SELECT COUNT(*) FROM kb_messages").fetchone()[0]
        fts_rows = kb.conn.execute(
            "SELECT COUNT(*) FROM kb_fts WHERE source_type = 'message'"
        ).fetchone()[0]
    assert (messages, fts_rows) == (4, 4)

    assert kb_indexer.index_all_messages()["sessions_skipped"] == 1


def test_search_ranks_best_match_first_one_per_session(tmp_path, monkeypatch):
    from tab_ledger.kb_query import KnowledgeBase

    kb = _kb_with_catch_all(tmp_path, monkeypatch)
    for text, session in [
        ("one moving piece among some parts of a long and winding note", "sess-b"),
        ("moving parts of the engine, all the moving parts", "sess-a"),
        ("moving parts again", "sess-a"),
        ("move part", "sess-d"),  # same stems, not the phrase: ranks after phrase matches
        ("the tab-ledger refresh", "sess-c"),
    ]:
        kb.execute(
            "INSERT INTO kb_fts (text, session_uuid, source_type, project_name) "
            "VALUES (?, ?, 'message', 'exploration')",
            (text, session),
        )
    kb.commit()
    kb.close()

    with KnowledgeBase() as kb:
        results = kb.search("moving parts")
        assert [r["session_uuid"] for r in results][0] == "sess-a"
        assert sorted(r["session_uuid"] for r in results) == ["sess-a", "sess-b", "sess-d"]
        assert "[moving parts]" in results[0]["snippet"]
        assert "text" not in results[0]
        assert [r["session_uuid"] for r in kb.search("tab-ledger")] == ["sess-c"]


def test_stale_refresh_is_reported(tmp_path, monkeypatch):
    from tab_ledger import kb_mcp_server
    from tab_ledger.kb_query import KnowledgeBase

    kb = _kb_with_catch_all(tmp_path, monkeypatch)
    with KnowledgeBase() as reader:
        assert reader.freshness()["stale"] is True  # nothing recorded yet

    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
    kb.execute(
        "UPDATE kb_progress SET completed_at = ? WHERE stage = 'refresh'",
        (three_days_ago.isoformat(),),
    )
    kb.commit()
    with KnowledgeBase() as reader:
        freshness = reader.freshness()
    assert freshness["stale"] is True
    assert "3.0 days ago" in freshness["warning"]

    content = asyncio.run(kb_mcp_server.call_tool("kb_stats", {}))
    assert content[0].text.startswith("WARNING:")
    assert json.loads(content[1].text)["freshness"]["stale"] is True

    kb.execute(
        "UPDATE kb_progress SET completed_at = ? WHERE stage = 'refresh'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    kb.commit()
    kb.close()
    with KnowledgeBase() as reader:
        freshness = reader.freshness()
    assert freshness["stale"] is False
    assert "warning" not in freshness
    assert len(asyncio.run(kb_mcp_server.call_tool("kb_stats", {}))) == 1


def test_taxonomy_file_replaces_sample(tmp_path, monkeypatch):
    from tab_ledger import kb_taxonomy

    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text(json.dumps([
        ["demo", "Demo", "opus", [["core", "Core", "Repositories/demo"]]],
        ["exploration", "Exploration", "haiku", [["root", "Root", None]]],
    ]))
    monkeypatch.setattr(kb_taxonomy, "TAXONOMY_FILE", taxonomy)

    assert kb_taxonomy.map_session("/Users/example/Repositories/demo/app") == ("demo", "core")
    assert kb_taxonomy.map_session("/tmp/elsewhere") == ("exploration", "root")


def test_cli_module_runs_as_script():
    result = subprocess.run(
        [sys.executable, "-m", "tab_ledger.cli", "--version"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("tab-ledger ")
