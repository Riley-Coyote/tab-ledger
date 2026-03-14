"""Knowledge Base Query Interface — CLI and Python API.

This is the primary user-facing module for querying the Master Knowledge Base.
It provides both a CLI interface and an importable Python API for agents.

CLI usage:
    python3 kb_query.py projects [--human]
    python3 kb_query.py project <name> [--human]
    python3 kb_query.py session <uuid> [--human]
    python3 kb_query.py search <query> [--project <name>] [--type <source_type>] [--limit N] [--human]
    python3 kb_query.py semantic <query> [--project <name>] [--type <source_type>] [--provider hash|ollama|openai]
    python3 kb_query.py timeline <project> [--sub <sub_project>] [--limit N] [--human]
    python3 kb_query.py recent [N] [--human]
    python3 kb_query.py context <project> [--human]
    python3 kb_query.py memory <project> [semantic query]
    python3 kb_query.py iterations <project> [--human]
    python3 kb_query.py related <session-uuid> [--human]
    python3 kb_query.py stats [--project <name>] [--human]

Python API:
    from kb_query import KnowledgeBase
    kb = KnowledgeBase()
    results = kb.search("websocket authentication")
    project = kb.get_project("vessel")
    context = kb.get_continuation_context("polyphonic")
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from textwrap import fill

from kb_schema import get_kb_db, KB_DB


class KnowledgeBase:
    """High-level API for querying the knowledge base.

    This class provides convenient methods for agents and tools to query
    the knowledge base without needing to write SQL directly.
    """

    def __init__(self, readonly: bool = True):
        """Initialize the knowledge base connection.

        Args:
            readonly: If True, opens connection in read-only mode for safety.
        """
        self.readonly = readonly
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        """Lazy-load database connection."""
        if self._conn is None:
            self._conn = get_kb_db(readonly=self.readonly)
        return self._conn

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ═══════════════════════════════════════════════════════════════════════════
    # PROJECT QUERIES
    # ═══════════════════════════════════════════════════════════════════════════

    def list_projects(self) -> List[Dict[str, Any]]:
        """List all canonical projects with summary statistics.

        Returns:
            List of project dicts with: canonical_name, display_name, total_sessions,
            total_cost_usd, first_session_at, last_session_at, summarization_tier.
        """
        rows = self.conn.execute("""
            SELECT
                canonical_name,
                display_name,
                description,
                status,
                total_sessions,
                total_cost_usd,
                first_session_at,
                last_session_at,
                summarization_tier
            FROM kb_projects
            ORDER BY total_sessions DESC, last_session_at DESC
        """).fetchall()

        return [dict(row) for row in rows]

    def get_project(self, canonical_name: str) -> Optional[Dict[str, Any]]:
        """Get detailed project information including sub-projects and recent sessions.

        Args:
            canonical_name: The canonical project name (e.g., 'polyphonic', 'vessel').

        Returns:
            Dict with project info, sub_projects list, recent_sessions list, or None if not found.
        """
        proj = self.conn.execute(
            "SELECT id, * FROM kb_projects WHERE canonical_name = ?",
            (canonical_name,)
        ).fetchone()

        if not proj:
            return None

        project_id = proj["id"]
        proj_dict = dict(proj)

        # Get sub-projects
        sub_projs = self.conn.execute("""
            SELECT canonical_name, display_name, description, session_count
            FROM kb_sub_projects
            WHERE project_id = ?
            ORDER BY session_count DESC
        """, (project_id,)).fetchall()
        proj_dict["sub_projects"] = [dict(row) for row in sub_projs]

        # Get recent sessions (last 10)
        sessions = self.conn.execute("""
            SELECT
                session_uuid, slug, started_at, ended_at, model,
                message_count, cost_usd, phase, outcome, summary_text
            FROM kb_sessions
            WHERE project_id = ?
            ORDER BY started_at DESC
            LIMIT 10
        """, (project_id,)).fetchall()
        proj_dict["recent_sessions"] = [dict(row) for row in sessions]

        # Get cost breakdown by model
        model_costs = self.conn.execute("""
            SELECT model, COUNT(*) as session_count, SUM(cost_usd) as total_cost
            FROM kb_sessions
            WHERE project_id = ?
            GROUP BY model
            ORDER BY total_cost DESC
        """, (project_id,)).fetchall()
        proj_dict["cost_by_model"] = [dict(row) for row in model_costs]

        # Get tool usage stats
        tool_stats = self._get_tool_usage_stats(project_id)
        proj_dict["tool_usage"] = tool_stats

        return proj_dict

    def get_session(self, session_uuid_or_prefix: str) -> Optional[Dict[str, Any]]:
        """Get full session details by UUID or prefix.

        Args:
            session_uuid_or_prefix: Full session UUID or partial prefix.

        Returns:
            Dict with all session metadata, or None if not found.
        """
        # If it looks like a prefix, find matching session
        if len(session_uuid_or_prefix) < 36:
            row = self.conn.execute(
                "SELECT id, * FROM kb_sessions WHERE session_uuid LIKE ? || '%'",
                (session_uuid_or_prefix,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id, * FROM kb_sessions WHERE session_uuid = ?",
                (session_uuid_or_prefix,)
            ).fetchone()

        if not row:
            return None

        session_dict = dict(row)
        session_id = row["id"]

        # Parse JSON fields
        if session_dict.get("summary_json"):
            try:
                session_dict["summary"] = json.loads(session_dict["summary_json"])
            except json.JSONDecodeError:
                session_dict["summary"] = None

        # Get connected sessions
        connections = self.conn.execute("""
            SELECT
                c.connection_type,
                c.strength,
                c.reason,
                s.session_uuid,
                s.slug,
                s.started_at,
                s.summary_text
            FROM kb_connections c
            JOIN kb_sessions s ON c.target_session_id = s.id
            WHERE c.source_session_id = ?
            ORDER BY c.strength DESC
        """, (session_id,)).fetchall()
        session_dict["connected_sessions"] = [dict(row) for row in connections]

        # Get messages
        messages = self.conn.execute("""
            SELECT
                message_index,
                message_type,
                role,
                content_length,
                has_thinking,
                has_tool_use,
                tool_names,
                stop_reason,
                model,
                timestamp
            FROM kb_messages
            WHERE session_id = ?
            ORDER BY message_index
        """, (session_id,)).fetchall()
        session_dict["messages"] = [dict(row) for row in messages]

        # Get tools used
        if session_dict.get("tools_used"):
            session_dict["tools_list"] = session_dict["tools_used"].split(",")

        return session_dict

    # ═══════════════════════════════════════════════════════════════════════════
    # SEARCH
    # ═══════════════════════════════════════════════════════════════════════════

    def search(
        self,
        query: str,
        project: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Full-text search across all indexed content.

        Args:
            query: Search query (FTS5 syntax supported).
            project: Optional project name filter.
            source_type: Optional source type filter (e.g., 'summary', 'code', 'plan').
            limit: Max results to return (default 20).

        Returns:
            List of search results with session UUIDs, snippets, and metadata.
        """
        sql = "SELECT * FROM kb_fts WHERE text MATCH ? "
        params = [query]

        if project:
            sql += "AND project_name = ? "
            params.append(project)

        if source_type:
            sql += "AND source_type = ? "
            params.append(source_type)

        sql += "LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()

        # Enrich with session details
        results = []
        for row in rows:
            result = dict(row)
            # Get full session info
            session = self.get_session(result["session_uuid"])
            if session:
                result["session_info"] = {
                    "slug": session.get("slug"),
                    "started_at": session.get("started_at"),
                    "model": session.get("model"),
                    "summary_text": session.get("summary_text"),
                }
            results.append(result)

        return results

    def _semantic_table_exists(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_embeddings' LIMIT 1"
            ).fetchone()
            return bool(row)
        except sqlite3.Error:
            return False

    def _list_embedding_models(self) -> List[str]:
        if not self._semantic_table_exists():
            return []
        try:
            rows = self.conn.execute(
                """
                SELECT embedding_model, COUNT(*) AS cnt
                FROM kb_embeddings
                GROUP BY embedding_model
                ORDER BY cnt DESC, embedding_model
                """
            ).fetchall()
        except sqlite3.Error:
            return []
        return [str(row["embedding_model"]) for row in rows if row["embedding_model"]]

    @staticmethod
    def _is_openai_embedding_model(model_name: str) -> bool:
        name = (model_name or "").strip().lower()
        return name.startswith("text-embedding-")

    @staticmethod
    def _is_hash_embedding_model(model_name: str) -> bool:
        return (model_name or "").strip().lower().startswith("hash-")

    @classmethod
    def _is_ollama_embedding_model(cls, model_name: str) -> bool:
        name = (model_name or "").strip().lower()
        if not name:
            return False
        if cls._is_hash_embedding_model(name) or cls._is_openai_embedding_model(name):
            return False
        return True

    def _pick_model_for_provider(self, provider_name: str, models: List[str]) -> Optional[str]:
        if not models:
            return None
        if provider_name == "hash":
            return next((m for m in models if self._is_hash_embedding_model(m)), None)
        if provider_name == "openai":
            return next((m for m in models if self._is_openai_embedding_model(m)), None)
        if provider_name == "ollama":
            return next((m for m in models if self._is_ollama_embedding_model(m)), None)
        return None

    def _resolve_semantic_provider_model(
        self,
        provider: Optional[str],
        model: Optional[str],
    ) -> Tuple[str, Optional[str]]:
        provider_name = (provider or os.getenv("KB_SEMANTIC_PROVIDER") or "").strip().lower() or None
        model_name = (model or os.getenv("KB_SEMANTIC_MODEL") or "").strip() or None
        models = self._list_embedding_models()

        if not provider_name:
            hash_model = self._pick_model_for_provider("hash", models)
            if hash_model:
                return "hash", hash_model

            openai_model = self._pick_model_for_provider("openai", models)
            if openai_model and os.getenv("OPENAI_API_KEY"):
                return "openai", openai_model

            ollama_model = self._pick_model_for_provider("ollama", models)
            if ollama_model:
                return "ollama", ollama_model

            return "hash", model_name

        if not model_name:
            model_name = self._pick_model_for_provider(provider_name, models)

        # Graceful fallback when openai model exists but no API key configured.
        if provider_name == "openai" and not os.getenv("OPENAI_API_KEY"):
            hash_model = self._pick_model_for_provider("hash", models)
            if hash_model:
                return "hash", hash_model
            return "hash", model_name

        return provider_name, model_name

    def semantic_search(
        self,
        query: str,
        project: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 20,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        min_score: float = 0.18,
    ) -> List[Dict[str, Any]]:
        """Semantic search across indexed memory artifacts."""
        from kb_semantic import create_embedding_provider, semantic_search as semantic_search_impl

        if not self._semantic_table_exists():
            return []

        provider_name, model_name = self._resolve_semantic_provider_model(provider, model)
        try:
            embedder = create_embedding_provider(provider_name, model=model_name)
        except Exception:
            # Last-resort fallback for local-only continuity when provider setup is missing.
            fallback_model = self._pick_model_for_provider("hash", self._list_embedding_models()) or model_name
            embedder = create_embedding_provider("hash", model=fallback_model)

        try:
            return semantic_search_impl(
                self.conn,
                query=query,
                provider=embedder,
                project=project,
                source_type=source_type,
                limit=limit,
                min_score=min_score,
            )
        except sqlite3.OperationalError as e:
            if "no such table: kb_embeddings" in str(e).lower():
                return []
            raise

    # ═══════════════════════════════════════════════════════════════════════════
    # TIMELINE & CHRONOLOGY
    # ═══════════════════════════════════════════════════════════════════════════

    def get_timeline(
        self,
        project: str,
        sub_project: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get chronological session list for a project.

        Args:
            project: Canonical project name.
            sub_project: Optional sub-project filter.
            limit: Max sessions to return.

        Returns:
            List of sessions in chronological order with metadata.
        """
        sql = """
            SELECT
                session_uuid,
                slug,
                started_at,
                ended_at,
                model,
                message_count,
                cost_usd,
                phase,
                outcome,
                summary_text,
                first_prompt
            FROM kb_sessions
            WHERE project_id = (SELECT id FROM kb_projects WHERE canonical_name = ?)
              AND started_at IS NOT NULL
        """
        params = [project]

        if sub_project:
            sql += " AND sub_project_id = (SELECT id FROM kb_sub_projects WHERE canonical_name = ?)"
            params.append(sub_project)

        sql += " ORDER BY started_at ASC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get the most recent sessions across all projects.

        Args:
            limit: Number of recent sessions to return.

        Returns:
            List of sessions ordered by recency.
        """
        rows = self.conn.execute("""
            SELECT
                session_uuid,
                slug,
                started_at,
                ended_at,
                model,
                message_count,
                cost_usd,
                phase,
                outcome,
                summary_text,
                project_id,
                (SELECT canonical_name FROM kb_projects WHERE id = kb_sessions.project_id) as project_name
            FROM kb_sessions
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(row) for row in rows]

    # ═══════════════════════════════════════════════════════════════════════════
    # CONTEXT FOR RESUMING WORK
    # ═══════════════════════════════════════════════════════════════════════════

    def get_continuation_context(self, project: str) -> Dict[str, Any]:
        """Get context for resuming work on a project.

        This is THE key command for agents resuming work. Returns:
        - Last session summary
        - Next steps from summary
        - Open blockers
        - Recent decisions
        - Related sessions

        Args:
            project: Canonical project name.

        Returns:
            Dict with context for continuation.
        """
        proj = self.conn.execute(
            "SELECT id FROM kb_projects WHERE canonical_name = ?",
            (project,)
        ).fetchone()

        if not proj:
            return {"error": f"Project '{project}' not found"}

        project_id = proj["id"]

        # Get last session
        last_session = self.conn.execute("""
            SELECT
                session_uuid,
                slug,
                started_at,
                ended_at,
                summary_text,
                summary_json,
                first_prompt,
                phase,
                outcome
            FROM kb_sessions
            WHERE project_id = ?
            ORDER BY started_at DESC
            LIMIT 1
        """, (project_id,)).fetchone()

        context = {
            "project": project,
            "last_session": None,
            "next_steps": [],
            "blockers": [],
            "recent_decisions": [],
            "decisions": [],
            "related_sessions": [],
        }

        if not last_session:
            return context

        last_session_dict = dict(last_session)
        context["last_session"] = last_session_dict

        # Parse summary if available
        summary_obj = None
        if last_session_dict.get("summary_json"):
            try:
                summary_obj = json.loads(last_session_dict["summary_json"])
                if isinstance(summary_obj, dict):
                    context["next_steps"] = summary_obj.get("next_steps", [])
                    context["blockers"] = summary_obj.get("blockers", [])
                    decisions = summary_obj.get("decisions", [])
                    context["recent_decisions"] = decisions
                    context["decisions"] = decisions
            except json.JSONDecodeError:
                pass

        # Get related/recent sessions
        related = self.conn.execute("""
            SELECT
                s.session_uuid,
                s.slug,
                s.started_at,
                s.phase,
                c.connection_type,
                c.strength
            FROM kb_sessions s
            LEFT JOIN kb_connections c ON (
                c.source_session_id = (
                    SELECT id FROM kb_sessions WHERE session_uuid = ?
                )
                AND c.target_session_id = s.id
            )
            WHERE s.project_id = ? AND s.session_uuid != ?
            ORDER BY c.strength DESC, s.started_at DESC
            LIMIT 5
        """, (last_session["session_uuid"], project_id, last_session["session_uuid"])).fetchall()

        context["related_sessions"] = [dict(row) for row in related]

        return context

    # ═══════════════════════════════════════════════════════════════════════════
    # ITERATIONS & PHASES
    # ═══════════════════════════════════════════════════════════════════════════

    def get_iterations(self, project: str) -> List[Dict[str, Any]]:
        """Get project phases/iterations grouped by temporal clusters and phase.

        Args:
            project: Canonical project name.

        Returns:
            List of iteration groups with sessions and summary info.
        """
        rows = self.conn.execute("""
            SELECT
                phase,
                COUNT(*) as session_count,
                MIN(started_at) as phase_start,
                MAX(started_at) as phase_end,
                SUM(cost_usd) as phase_cost,
                AVG(message_count) as avg_messages
            FROM kb_sessions
            WHERE project_id = (SELECT id FROM kb_projects WHERE canonical_name = ?)
            GROUP BY phase
            ORDER BY phase_start
        """, (project,)).fetchall()

        iterations = [dict(row) for row in rows]

        # Enrich each phase with session list
        for iteration in iterations:
            phase = iteration.get("phase")
            if phase:
                sessions = self.conn.execute("""
                    SELECT
                        session_uuid,
                        slug,
                        started_at,
                        summary_text,
                        outcome
                    FROM kb_sessions
                    WHERE project_id = (SELECT id FROM kb_projects WHERE canonical_name = ?)
                    AND phase = ?
                    ORDER BY started_at
                """, (project, phase)).fetchall()
                iteration["sessions"] = [dict(row) for row in sessions]

        return iterations

    def get_memory_packet(
        self,
        project: str,
        semantic_query: Optional[str] = None,
        semantic_limit: int = 10,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a continuity packet for natural memory persistence."""
        from kb_memory import build_memory_packet

        return build_memory_packet(
            self,
            project=project,
            semantic_query=semantic_query,
            semantic_limit=semantic_limit,
            provider=provider,
            model=model,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # CONNECTIONS & RELATIONSHIPS
    # ═══════════════════════════════════════════════════════════════════════════

    def get_related_sessions(self, session_uuid: str) -> List[Dict[str, Any]]:
        """Get all sessions connected to a given session.

        Args:
            session_uuid: The session UUID to find connections for.

        Returns:
            List of connected sessions with connection type and strength.
        """
        session = self.conn.execute(
            "SELECT id FROM kb_sessions WHERE session_uuid = ?",
            (session_uuid,)
        ).fetchone()

        if not session:
            return []

        session_id = session["id"]

        rows = self.conn.execute("""
            SELECT
                c.connection_type,
                c.strength,
                c.reason,
                s.session_uuid,
                s.slug,
                s.started_at,
                s.phase,
                s.summary_text
            FROM kb_connections c
            JOIN kb_sessions s ON c.target_session_id = s.id
            WHERE c.source_session_id = ?
            ORDER BY c.strength DESC
        """, (session_id,)).fetchall()

        return [dict(row) for row in rows]

    # ═══════════════════════════════════════════════════════════════════════════
    # STATISTICS
    # ═══════════════════════════════════════════════════════════════════════════

    def get_stats(self, project: Optional[str] = None) -> Dict[str, Any]:
        """Get global or per-project statistics.

        Args:
            project: Optional project name for per-project stats.

        Returns:
            Dict with tokens, cost, tool usage, model breakdown, etc.
        """
        if project:
            sql_where = "WHERE project_id = (SELECT id FROM kb_projects WHERE canonical_name = ?)"
            params = [project]
        else:
            sql_where = ""
            params = []

        # Basic stats
        sql = f"""
            SELECT
                COUNT(*) as total_sessions,
                SUM(message_count) as total_messages,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(cache_creation_tokens) as total_cache_creation_tokens,
                SUM(cache_read_tokens) as total_cache_read_tokens,
                SUM(cost_usd) as total_cost_usd,
                SUM(total_duration_ms) as total_duration_ms,
                COUNT(CASE WHEN is_sidechain THEN 1 END) as sidechain_sessions,
                COUNT(DISTINCT model) as unique_models
            FROM kb_sessions
            {sql_where}
        """

        row = self.conn.execute(sql, params).fetchone()
        stats = dict(row) if row else {}

        # Model breakdown
        sql = f"""
            SELECT
                model,
                COUNT(*) as session_count,
                SUM(cost_usd) as cost,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens
            FROM kb_sessions
            {sql_where}
            GROUP BY model
            ORDER BY cost DESC
        """

        model_rows = self.conn.execute(sql, params).fetchall()
        stats["model_breakdown"] = [dict(row) for row in model_rows]

        # Tool usage
        project_id_for_tools = None
        if project:
            proj_row = self.conn.execute(
                "SELECT id FROM kb_projects WHERE canonical_name = ?",
                (project,)
            ).fetchone()
            if proj_row:
                project_id_for_tools = proj_row["id"]
        tool_stats = self._get_tool_usage_stats(project_id_for_tools)
        stats["tool_usage"] = tool_stats

        # Top phases
        if sql_where:
            phase_sql = f"""
                SELECT phase, COUNT(*) as session_count, SUM(cost_usd) as cost
                FROM kb_sessions {sql_where} AND phase IS NOT NULL
                GROUP BY phase ORDER BY session_count DESC LIMIT 10
            """
        else:
            phase_sql = """
                SELECT phase, COUNT(*) as session_count, SUM(cost_usd) as cost
                FROM kb_sessions WHERE phase IS NOT NULL
                GROUP BY phase ORDER BY session_count DESC LIMIT 10
            """

        phase_rows = self.conn.execute(phase_sql, params).fetchall()
        stats["phases"] = [dict(row) for row in phase_rows]

        return stats

    # ═══════════════════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _get_tool_usage_stats(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get tool usage statistics."""
        # Parse tools_used CSV field and aggregate
        if project_id:
            sessions = self.conn.execute(
                "SELECT tools_used FROM kb_sessions WHERE project_id = ? AND tools_used != ''",
                (project_id,)
            ).fetchall()
        else:
            sessions = self.conn.execute(
                "SELECT tools_used FROM kb_sessions WHERE tools_used != ''"
            ).fetchall()

        tool_counts = {}
        for row in sessions:
            if row["tools_used"]:
                for tool in row["tools_used"].split(","):
                    tool = tool.strip()
                    tool_counts[tool] = tool_counts.get(tool, 0) + 1

        return [
            {"tool": tool, "count": count}
            for tool, count in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# CLI INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

class KBFormatter:
    """Format knowledge base results for human-readable or JSON output."""

    def __init__(self, human: bool = False, brief: bool = False):
        """Initialize formatter.

        Args:
            human: If True, use formatted text output; otherwise use JSON.
            brief: If True, use compact single-line-per-result format.
        """
        self.human = human
        self.brief = brief

    def output(self, data: Any, title: Optional[str] = None) -> str:
        """Format and return output.

        Args:
            data: Data to format (dict, list, or primitive).
            title: Optional section title for human output.

        Returns:
            Formatted string.
        """
        if not self.human:
            return json.dumps(data, indent=2, default=str)

        if title:
            return self._human_output(data, title)
        return self._human_output(data)

    def _human_output(self, data: Any, title: Optional[str] = None) -> str:
        """Format data as human-readable text."""
        lines = []

        if title:
            lines.append(f"\n{'=' * 70}")
            lines.append(f"  {title}")
            lines.append(f"{'=' * 70}\n")

        if isinstance(data, list):
            if self.brief:
                lines.extend(self._format_list_brief(data))
            else:
                lines.extend(self._format_list_full(data))
        elif isinstance(data, dict):
            lines.extend(self._format_dict(data))
        else:
            lines.append(str(data))

        return "\n".join(lines)

    def _format_list_full(self, items: List[Dict[str, Any]]) -> List[str]:
        """Format list of dicts as pretty table/sections."""
        lines = []

        if not items:
            return ["  (no results)"]

        # If items are dicts, try to format as table
        if items and isinstance(items[0], dict):
            keys = list(items[0].keys())
            # Show first few columns
            cols = [k for k in keys if k not in ["id", "description"]][:4]

            # Print header
            header = " | ".join(f"{k:20}" for k in cols)
            lines.append(header)
            lines.append("-" * len(header))

            # Print rows
            for item in items:
                values = [str(item.get(k, ""))[:20] for k in cols]
                lines.append(" | ".join(f"{v:20}" for v in values))
        else:
            for item in items:
                lines.append(f"  • {item}")

        return lines

    def _format_list_brief(self, items: List[Dict[str, Any]]) -> List[str]:
        """Format list in compact single-line format."""
        lines = []
        for item in items:
            if isinstance(item, dict):
                # Show key fields
                parts = []
                if "slug" in item:
                    parts.append(item["slug"])
                if "session_uuid" in item:
                    parts.append(f"[{item['session_uuid'][:8]}]")
                if "started_at" in item:
                    parts.append(item["started_at"][:10])
                if "summary_text" in item:
                    summary = item["summary_text"]
                    if summary:
                        summary = summary[:50] + ("..." if len(summary) > 50 else "")
                        parts.append(f'"{summary}"')

                lines.append("  " + " • ".join(filter(None, parts)))
            else:
                lines.append(f"  {item}")

        return lines if lines else ["  (no results)"]

    def _format_dict(self, data: Dict[str, Any], indent: int = 0) -> List[str]:
        """Format dict as indented key-value pairs."""
        lines = []
        prefix = "  " * (indent + 1)

        for key, value in data.items():
            if value is None or value == "":
                continue

            # Skip large nested structures in human output
            if isinstance(value, (list, dict)) and len(str(value)) > 200:
                if isinstance(value, list):
                    lines.append(f"{prefix}{key}: [{len(value)} items]")
                else:
                    lines.append(f"{prefix}{key}: [object]")
                continue

            if isinstance(value, list):
                lines.append(f"{prefix}{key}:")
                for item in value[:3]:  # Show first 3
                    if isinstance(item, dict):
                        for k, v in item.items():
                            lines.append(f"{prefix}  {k}: {v}")
                    else:
                        lines.append(f"{prefix}  • {item}")
                if len(value) > 3:
                    lines.append(f"{prefix}  ... and {len(value) - 3} more")
            elif isinstance(value, dict):
                lines.append(f"{prefix}{key}:")
                for k, v in list(value.items())[:3]:
                    lines.append(f"{prefix}  {k}: {v}")
                if len(value) > 3:
                    lines.append(f"{prefix}  ... and {len(value) - 3} more fields")
            else:
                # Truncate long strings
                v_str = str(value)
                if len(v_str) > 100:
                    v_str = v_str[:100] + "..."
                lines.append(f"{prefix}{key}: {v_str}")

        return lines if lines else [f"{prefix}(empty)"]


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Query the Master Knowledge Base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  kb_query.py projects --human
  kb_query.py project polyphonic --human
  kb_query.py session a1b2c3d4 --human
  kb_query.py search "websocket auth" --project vessel --limit 10
  kb_query.py semantic "auth handshake failure" --project vessel --limit 8
  kb_query.py memory vessel
  kb_query.py timeline polyphonic --limit 20 --human
  kb_query.py recent 15 --human
  kb_query.py context vessel --human
  kb_query.py iterations polyphonic --human
  kb_query.py related a1b2c3d4 --human
  kb_query.py stats --project polyphonic --human
        """
    )

    parser.add_argument("command", help="Command to execute")
    parser.add_argument("args", nargs="*", help="Command arguments")
    parser.add_argument("--human", action="store_true", help="Use human-readable output format")
    parser.add_argument("--brief", action="store_true", help="Use brief/compact output format")
    parser.add_argument("--project", help="Filter by project name")
    parser.add_argument("--sub", help="Filter by sub-project name")
    parser.add_argument("--type", help="Filter by source type (for search)")
    parser.add_argument("--limit", type=int, help="Limit number of results")
    parser.add_argument("--provider", help="Semantic embedding provider override (hash|ollama|openai)")
    parser.add_argument("--model", help="Semantic model override")
    parser.add_argument("--min-score", type=float, default=0.18,
                        help="Minimum semantic similarity score")

    args = parser.parse_args()

    formatter = KBFormatter(human=args.human, brief=args.brief)
    kb = KnowledgeBase(readonly=True)

    try:
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # COMMANDS
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        if args.command == "projects":
            result = kb.list_projects()
            output = formatter.output(result, "All Projects")
            print(output)

        elif args.command == "project":
            if not args.args:
                print("Error: project command requires a project name")
                sys.exit(1)
            project_name = args.args[0]
            result = kb.get_project(project_name)
            if result is None:
                print(f"Error: Project '{project_name}' not found")
                sys.exit(1)
            output = formatter.output(result, f"Project: {project_name}")
            print(output)

        elif args.command == "session":
            if not args.args:
                print("Error: session command requires a session UUID or prefix")
                sys.exit(1)
            session_id = args.args[0]
            result = kb.get_session(session_id)
            if result is None:
                print(f"Error: Session '{session_id}' not found")
                sys.exit(1)
            output = formatter.output(result, f"Session: {result.get('slug', session_id)}")
            print(output)

        elif args.command == "search":
            if not args.args:
                print("Error: search command requires a query")
                sys.exit(1)
            query = " ".join(args.args)
            limit = args.limit or 20
            result = kb.search(
                query,
                project=args.project,
                source_type=args.type,
                limit=limit
            )
            output = formatter.output(result, f"Search Results: '{query}'")
            print(output)

        elif args.command == "semantic":
            if not args.args:
                print("Error: semantic command requires a query")
                sys.exit(1)
            query = " ".join(args.args)
            limit = args.limit or 20
            result = kb.semantic_search(
                query=query,
                project=args.project,
                source_type=args.type,
                limit=limit,
                provider=args.provider,
                model=args.model,
                min_score=args.min_score,
            )
            output = formatter.output(result, f"Semantic Search: '{query}'")
            print(output)

        elif args.command == "timeline":
            if not args.args:
                print("Error: timeline command requires a project name")
                sys.exit(1)
            project_name = args.args[0]
            limit = args.limit or 50
            result = kb.get_timeline(
                project_name,
                sub_project=args.sub,
                limit=limit
            )
            if not result:
                print(f"Error: Project '{project_name}' not found or has no sessions")
                sys.exit(1)
            output = formatter.output(result, f"Timeline: {project_name}")
            print(output)

        elif args.command == "recent":
            limit = 10
            if args.args and args.args[0].isdigit():
                limit = int(args.args[0])
            result = kb.get_recent(limit=limit)
            output = formatter.output(result, f"Recent Sessions (Last {limit})")
            print(output)

        elif args.command == "context":
            if not args.args:
                print("Error: context command requires a project name")
                sys.exit(1)
            project_name = args.args[0]
            result = kb.get_continuation_context(project_name)
            if "error" in result:
                print(f"Error: {result['error']}")
                sys.exit(1)
            output = formatter.output(result, f"Continuation Context: {project_name}")
            print(output)

        elif args.command == "memory":
            if not args.args:
                print("Error: memory command requires a project name")
                sys.exit(1)
            project_name = args.args[0]
            semantic_query = " ".join(args.args[1:]).strip() if len(args.args) > 1 else None
            result = kb.get_memory_packet(
                project=project_name,
                semantic_query=semantic_query,
                semantic_limit=args.limit or 10,
                provider=args.provider,
                model=args.model,
            )
            if "error" in result:
                print(f"Error: {result['error']}")
                sys.exit(1)
            output = formatter.output(result, f"Memory Continuity Packet: {project_name}")
            print(output)

        elif args.command == "iterations":
            if not args.args:
                print("Error: iterations command requires a project name")
                sys.exit(1)
            project_name = args.args[0]
            result = kb.get_iterations(project_name)
            if not result:
                print(f"Error: Project '{project_name}' not found or has no sessions")
                sys.exit(1)
            output = formatter.output(result, f"Iterations: {project_name}")
            print(output)

        elif args.command == "related":
            if not args.args:
                print("Error: related command requires a session UUID")
                sys.exit(1)
            session_id = args.args[0]
            result = kb.get_related_sessions(session_id)
            output = formatter.output(result, f"Related Sessions: {session_id}")
            print(output)

        elif args.command == "stats":
            result = kb.get_stats(project=args.project)
            title = "Global Statistics" if not args.project else f"Statistics: {args.project}"
            output = formatter.output(result, title)
            print(output)

        else:
            print(f"Error: Unknown command '{args.command}'")
            print("\nAvailable commands:")
            print("  projects, project, session, search, semantic, timeline, recent")
            print("  context, memory, iterations, related, stats")
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        kb.close()


if __name__ == "__main__":
    main()
