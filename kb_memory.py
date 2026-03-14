"""Continuity packet builder for fluid memory persistence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _parse_summary_json(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return {}


def _fts_fallback_query(text: str, max_terms: int = 10) -> str:
    tokens = re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())
    deduped: List[str] = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
        if len(deduped) >= max_terms:
            break
    return " OR ".join(deduped)


def build_memory_packet(
    kb: Any,
    project: str,
    semantic_query: Optional[str] = None,
    semantic_limit: int = 10,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a high-signal continuity payload for resuming work naturally."""
    context = kb.get_continuation_context(project)
    timeline = kb.get_timeline(project, limit=15)

    if "error" in context:
        return {
            "project": project,
            "error": context["error"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    last_session = context.get("last_session") or {}
    project_row = kb.conn.execute(
        "SELECT id, display_name FROM kb_projects WHERE canonical_name = ?",
        (project,),
    ).fetchone()
    if not project_row:
        return {
            "project": project,
            "error": f"Project '{project}' not found",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    project_id = project_row["id"]
    display_name = project_row["display_name"]

    summary_rows = kb.conn.execute(
        """
        SELECT session_uuid, slug, started_at, summary_json
        FROM kb_sessions
        WHERE project_id = ? AND summary_json IS NOT NULL AND summary_json != ''
        ORDER BY started_at DESC
        LIMIT 25
        """,
        (project_id,),
    ).fetchall()

    unresolved_blockers: List[Dict[str, Any]] = []
    open_next_steps: List[Dict[str, Any]] = []

    for row in summary_rows:
        parsed = _parse_summary_json(row["summary_json"])
        blockers = _as_list(parsed.get("blockers"))
        next_steps = _as_list(parsed.get("next_steps"))

        for blocker in blockers:
            unresolved_blockers.append(
                {
                    "session_uuid": row["session_uuid"],
                    "slug": row["slug"],
                    "started_at": row["started_at"],
                    "text": blocker,
                }
            )

        for step in next_steps:
            open_next_steps.append(
                {
                    "session_uuid": row["session_uuid"],
                    "slug": row["slug"],
                    "started_at": row["started_at"],
                    "text": step,
                }
            )

    # Dedupe while preserving recency ordering.
    seen_blockers = set()
    deduped_blockers = []
    for item in unresolved_blockers:
        key = item["text"].lower()
        if key in seen_blockers:
            continue
        seen_blockers.add(key)
        deduped_blockers.append(item)

    seen_steps = set()
    deduped_steps = []
    for item in open_next_steps:
        key = item["text"].lower()
        if key in seen_steps:
            continue
        seen_steps.add(key)
        deduped_steps.append(item)

    thread_rows = kb.conn.execute(
        """
        SELECT
            c.connection_type,
            c.strength,
            s1.session_uuid AS source_session_uuid,
            s1.slug AS source_slug,
            s2.session_uuid AS target_session_uuid,
            s2.slug AS target_slug
        FROM kb_connections c
        JOIN kb_sessions s1 ON s1.id = c.source_session_id
        JOIN kb_sessions s2 ON s2.id = c.target_session_id
        WHERE s1.project_id = ? OR s2.project_id = ?
        ORDER BY c.strength DESC
        LIMIT 40
        """,
        (project_id, project_id),
    ).fetchall()
    continuity_threads = [dict(r) for r in thread_rows]

    query_text = (semantic_query or "").strip()
    if not query_text:
        query_text = (
            last_session.get("summary_text")
            or last_session.get("first_prompt")
            or last_session.get("slug")
            or project
        )
    if len(query_text) > 1200:
        query_text = query_text[:1200].rsplit(" ", 1)[0] or query_text[:1200]

    semantic_hits: List[Dict[str, Any]] = []
    if query_text:
        try:
            semantic_hits = kb.semantic_search(
                query_text,
                project=project,
                limit=semantic_limit,
                provider=provider,
                model=model,
                min_score=0.1,
            )
        except Exception:
            semantic_hits = []

    retrieval_mode = "semantic"
    if query_text and not semantic_hits:
        fallback_query = _fts_fallback_query(query_text)
        if fallback_query:
            try:
                lexical_hits = kb.search(
                    fallback_query,
                    project=project,
                    limit=semantic_limit,
                )
            except Exception:
                lexical_hits = []

            semantic_hits = []
            for idx, hit in enumerate(lexical_hits):
                source_type = hit.get("source_type") or "fts"
                session_uuid = hit.get("session_uuid")
                preview = (hit.get("text") or "")[:280]
                semantic_hits.append(
                    {
                        "source_key": f"{source_type}:{session_uuid or idx}",
                        "source_type": source_type,
                        "session_uuid": session_uuid,
                        "project_name": hit.get("project_name") or project,
                        "text_preview": preview,
                        "semantic_score": None,
                        "hybrid_score": round(1.0 / (1.0 + idx), 4),
                        "metadata": {
                            "retrieval": "fts_fallback",
                            "fallback_query": fallback_query,
                        },
                        "session_info": hit.get("session_info"),
                    }
                )
            if semantic_hits:
                retrieval_mode = "fts_fallback"

    semantic_index_ready = False
    semantic_table_exists = getattr(kb, "_semantic_table_exists", None)
    if callable(semantic_table_exists):
        try:
            semantic_index_ready = bool(semantic_table_exists())
        except Exception:
            semantic_index_ready = False

    return {
        "project": project,
        "project_display_name": display_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query_used": query_text,
        "retrieval_mode": retrieval_mode,
        "semantic_index_ready": semantic_index_ready,
        "last_session": last_session,
        "timeline": timeline,
        "related_sessions": context.get("related_sessions", []),
        "recent_decisions": context.get("recent_decisions") or context.get("decisions", []),
        "unresolved_blockers": deduped_blockers[:20],
        "open_next_steps": deduped_steps[:20],
        "continuity_threads": continuity_threads[:25],
        "semantic_hits": semantic_hits,
    }
