import json


def _seed_project_and_session(kb, project_name: str, session_uuid: str) -> None:
    kb.execute(
        "INSERT INTO kb_projects (canonical_name, display_name) VALUES (?, ?)",
        (project_name, project_name.title()),
    )
    project_id = kb.execute(
        "SELECT id FROM kb_projects WHERE canonical_name = ?",
        (project_name,),
    ).fetchone()[0]
    summary_json = json.dumps(
        {
            "summary": "Implemented oauth callback handling for auth flow",
            "decisions": ["Use signed nonce validation"],
            "next_steps": ["Add integration tests for callback edge cases"],
            "blockers": ["Need staging OAuth app credentials"],
        }
    )
    kb.execute(
        """
        INSERT INTO kb_sessions (
            session_uuid, project_id, slug, started_at, summary_text,
            summary_json, first_prompt, phase, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_uuid,
            project_id,
            "oauth-callback-fix",
            "2026-02-10T00:00:00+00:00",
            "OAuth callback failure investigation and patch.",
            summary_json,
            "Fix oauth callback loop in auth flow",
            "debug",
            "partial",
        ),
    )
    kb.commit()


def test_semantic_index_search_includes_structured_summary_fields(tmp_path, monkeypatch):
    import kb_schema
    import kb_semantic

    kb_path = tmp_path / "knowledge_base.db"
    monkeypatch.setattr(kb_schema, "KB_DB", kb_path)

    kb_schema.create_schema(drop_existing=True)
    kb = kb_schema.get_kb_db()
    _seed_project_and_session(kb, "alpha", "sess-sem-1")

    provider = kb_semantic.HashEmbeddingProvider(dim=256)
    stats = kb_semantic.build_semantic_index(kb, provider=provider)
    assert stats["documents_total"] >= 2

    results = kb_semantic.semantic_search(
        kb,
        query="signed nonce validation decision",
        provider=provider,
        project="alpha",
        limit=5,
        min_score=0.0,
    )
    kb.close()

    assert results
    assert any(r["source_type"] == "summary" for r in results)


def test_memory_packet_falls_back_when_semantic_table_missing(tmp_path, monkeypatch):
    import kb_schema
    from kb_query import KnowledgeBase

    kb_path = tmp_path / "knowledge_base.db"
    monkeypatch.setattr(kb_schema, "KB_DB", kb_path)

    kb_schema.create_schema(drop_existing=True)
    kb = kb_schema.get_kb_db()
    _seed_project_and_session(kb, "alpha", "sess-sem-legacy")
    kb.execute(
        """
        INSERT INTO kb_fts (text, session_uuid, source_type, project_name)
        VALUES (?, ?, ?, ?)
        """,
        (
            "oauth callback failure signed nonce staging credentials",
            "sess-sem-legacy",
            "summary",
            "alpha",
        ),
    )
    kb.execute("DROP TABLE kb_embeddings")
    kb.commit()
    kb.close()

    with KnowledgeBase(readonly=True) as query_kb:
        semantic_results = query_kb.semantic_search("oauth callback bug", project="alpha")
        memory = query_kb.get_memory_packet(
            "alpha",
            semantic_query="oauth callback staging credentials",
            semantic_limit=5,
        )

    assert semantic_results == []
    assert "semantic_hits" in memory
    assert memory["retrieval_mode"] in {"semantic", "fts_fallback"}
    assert isinstance(memory["semantic_hits"], list)
