"""Knowledge Base MCP Server — Exposes KB as tools for Claude Code & agents.

Stdio MCP server wrapping KnowledgeBase from kb_query.py.
Register in ~/.claude/settings.json under mcpServers.
"""

import json
import sys
from pathlib import Path

# Add tab-ledger to path so we can import kb_query

from .kb_query import KnowledgeBase
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("tab-ledger")


def _json_result(data) -> list[TextContent]:
    """Wrap data as JSON TextContent for MCP response."""
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


# ─── Tool definitions ────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="kb_search",
        description="Full-text search across all KB sessions. Returns matching snippets with session metadata. Supports FTS5 query syntax.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (FTS5 syntax supported, e.g. 'websocket OR authentication')"},
                "project": {"type": "string", "description": "Optional: filter to a specific project (e.g. 'polyphonic', 'vessel')"},
                "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="kb_semantic",
        description="Semantic search over memory embeddings. Great for finding conceptually related sessions beyond keyword matches.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic query text"},
                "project": {"type": "string", "description": "Optional project scope"},
                "source_type": {"type": "string", "description": "Optional source type filter (summary|prompt|plan|todo|message)"},
                "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                "provider": {"type": "string", "description": "Optional embedding provider override: hash|ollama|openai"},
                "model": {"type": "string", "description": "Optional embedding model override"},
                "min_score": {"type": "number", "description": "Minimum similarity score", "default": 0.18},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="kb_memory",
        description="Build a continuity packet for a project: last context, unresolved blockers, next steps, continuity threads, and semantic anchors.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Canonical project name"},
                "semantic_query": {"type": "string", "description": "Optional query to steer semantic retrieval"},
                "semantic_limit": {"type": "integer", "description": "Semantic hit limit (default 10)", "default": 10},
                "provider": {"type": "string", "description": "Optional embedding provider override: hash|ollama|openai"},
                "model": {"type": "string", "description": "Optional embedding model override"},
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="kb_context",
        description="Get continuation context for resuming work on a project. Returns last session summary, next steps, blockers, recent decisions, and related sessions. THE key tool for session handoff.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Canonical project name (e.g. 'polyphonic', 'vessel', 'sanctuary')"},
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="kb_session",
        description="Get full session detail by UUID or prefix. Returns metadata, messages, connected sessions, and parsed summary.",
        inputSchema={
            "type": "object",
            "properties": {
                "uuid_prefix": {"type": "string", "description": "Full session UUID or partial prefix (e.g. 'a1b2c3d4')"},
            },
            "required": ["uuid_prefix"],
        },
    ),
    Tool(
        name="kb_projects",
        description="List all projects in the knowledge base with session counts, costs, and date ranges.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="kb_timeline",
        description="Chronological session list for a project. Shows slug, date, model, phase, outcome, and summary for each session.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Canonical project name"},
                "limit": {"type": "integer", "description": "Max sessions (default 50)", "default": 50},
            },
            "required": ["project"],
        },
    ),
    Tool(
        name="kb_stats",
        description="Token counts, costs, tool rankings, model breakdown, and phase breakdown. Global or per-project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional: project name for per-project stats. Omit for global stats."},
            },
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    kb = KnowledgeBase(readonly=True)
    try:
        if name == "kb_search":
            results = kb.search(
                query=arguments["query"],
                project=arguments.get("project"),
                limit=arguments.get("limit", 20),
            )
            return _json_result(results)

        elif name == "kb_semantic":
            results = kb.semantic_search(
                query=arguments["query"],
                project=arguments.get("project"),
                source_type=arguments.get("source_type"),
                limit=arguments.get("limit", 20),
                provider=arguments.get("provider"),
                model=arguments.get("model"),
                min_score=arguments.get("min_score", 0.18),
            )
            return _json_result(results)

        elif name == "kb_memory":
            result = kb.get_memory_packet(
                project=arguments["project"],
                semantic_query=arguments.get("semantic_query"),
                semantic_limit=arguments.get("semantic_limit", 10),
                provider=arguments.get("provider"),
                model=arguments.get("model"),
            )
            return _json_result(result)

        elif name == "kb_context":
            result = kb.get_continuation_context(arguments["project"])
            return _json_result(result)

        elif name == "kb_session":
            result = kb.get_session(arguments["uuid_prefix"])
            if result is None:
                return _json_result({"error": f"Session '{arguments['uuid_prefix']}' not found"})
            return _json_result(result)

        elif name == "kb_projects":
            return _json_result(kb.list_projects())

        elif name == "kb_timeline":
            results = kb.get_timeline(
                project=arguments["project"],
                limit=arguments.get("limit", 50),
            )
            return _json_result(results)

        elif name == "kb_stats":
            result = kb.get_stats(project=arguments.get("project"))
            return _json_result(result)

        else:
            return _json_result({"error": f"Unknown tool: {name}"})

    finally:
        kb.close()


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
