"""Unified CLI for tab-ledger.

Usage:
    tab-ledger search "websocket auth" --project my-project
    tab-ledger stats
    tab-ledger build
    tab-ledger mcp
    tab-ledger serve
"""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="tab-ledger",
        description="Local-first analytics & knowledge base for Claude Code sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Quick start:
  tab-ledger index              Index Claude Code sessions
  tab-ledger build              Build the knowledge base
  tab-ledger search "query"     Search across all sessions
  tab-ledger stats              View token/cost analytics
  tab-ledger mcp                Run MCP server for Claude Code
  tab-ledger serve              Start web dashboard

MCP integration (add to ~/.claude/settings.json):
  { "mcpServers": { "tab-ledger": { "command": "tab-ledger", "args": ["mcp"] } } }
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # ── Indexing commands ──
    sp = sub.add_parser("index", help="Index Claude Code sessions into ledger.db")
    sp.add_argument("--force", action="store_true", help="Re-index all sessions (not just new)")

    sub.add_parser("snapshot", help="Capture browser tabs + index CC sessions")

    sp = sub.add_parser("build", help="Build the knowledge base (all stages)")
    sp.add_argument("--from", type=int, dest="from_stage", metavar="N", help="Resume from stage N")
    sp.add_argument("--only", type=int, metavar="N", help="Run only stage N")
    sp.add_argument("--drop", action="store_true", help="Drop and rebuild from scratch")
    sp.add_argument("--skip-summarize", action="store_true", help="Skip AI summarization (no API calls)")

    sub.add_parser("refresh", help="Nightly KB refresh (stages 1-3, 5-7, skip summarization)")

    # ── Query commands ──
    sp = sub.add_parser("search", help="Full-text search across sessions")
    sp.add_argument("query", nargs="+", help="Search query (FTS5 syntax supported)")
    sp.add_argument("--project", "-p", help="Filter by project")
    sp.add_argument("--limit", "-n", type=int, default=20, help="Max results (default: 20)")

    sp = sub.add_parser("semantic", help="Semantic search via embeddings")
    sp.add_argument("query", nargs="+", help="Semantic query text")
    sp.add_argument("--project", "-p", help="Filter by project")
    sp.add_argument("--limit", "-n", type=int, default=20, help="Max results")
    sp.add_argument("--provider", help="Embedding provider (hash|ollama|openai)")
    sp.add_argument("--min-score", type=float, default=0.18, help="Min similarity score")

    sp = sub.add_parser("stats", help="Token, cost, and tool usage statistics")
    sp.add_argument("--project", "-p", help="Filter by project")

    sp = sub.add_parser("timeline", help="Chronological session timeline")
    sp.add_argument("project", help="Project name")
    sp.add_argument("--limit", "-n", type=int, default=50, help="Max results")

    sp = sub.add_parser("context", help="Continuation context for resuming work on a project")
    sp.add_argument("project", help="Project name")

    sp = sub.add_parser("memory", help="Full continuity packet (context + semantic anchors)")
    sp.add_argument("project", help="Project name")

    sub.add_parser("projects", help="List all indexed projects")

    sp = sub.add_parser("session", help="Detailed view of a single session")
    sp.add_argument("uuid", help="Session UUID or prefix")

    # ── Server commands ──
    sp = sub.add_parser("serve", help="Start the web dashboard")
    sp.add_argument("--port", type=int, default=7777, help="Port (default: 7777)")
    sp.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")

    sub.add_parser("mcp", help="Run stdio MCP server for Claude Code integration")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # ── Dispatch ──
    try:
        _dispatch(args)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _dispatch(args):
    cmd = args.command

    if cmd == "index":
        from .cc_indexer import index_all
        from ._paths import ensure_data_dir
        ensure_data_dir()
        index_all(force=args.force)

    elif cmd == "snapshot":
        from .run_snapshot import main as snapshot_main
        snapshot_main()

    elif cmd == "build":
        from .kb_build import main as build_main
        from ._paths import ensure_data_dir
        ensure_data_dir()
        # Reconstruct sys.argv for kb_build's argparse
        argv = []
        if args.from_stage is not None:
            argv.extend(["--from", str(args.from_stage)])
        if args.only is not None:
            argv.extend(["--only", str(args.only)])
        if args.drop:
            argv.append("--drop")
        if args.skip_summarize:
            argv.append("--skip-summarize")
        sys.argv = ["tab-ledger build"] + argv
        build_main()

    elif cmd == "refresh":
        from .run_kb_refresh import main as refresh_main
        refresh_main()

    elif cmd == "search":
        from .kb_query import KnowledgeBase
        query = " ".join(args.query)
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.search(query, project=args.project, limit=args.limit)
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "semantic":
        from .kb_query import KnowledgeBase
        query = " ".join(args.query)
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.semantic_search(
                query, project=args.project, limit=args.limit,
                provider=args.provider, min_score=args.min_score,
            )
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "stats":
        from .kb_query import KnowledgeBase
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.get_stats(project=args.project)
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "timeline":
        from .kb_query import KnowledgeBase
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.get_timeline(args.project, limit=args.limit)
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "context":
        from .kb_query import KnowledgeBase
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.get_continuation_context(args.project)
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "memory":
        from .kb_query import KnowledgeBase
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.get_memory_packet(args.project)
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "projects":
        from .kb_query import KnowledgeBase
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.list_projects()
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "session":
        from .kb_query import KnowledgeBase
        kb = KnowledgeBase(readonly=True)
        try:
            results = kb.get_session(args.uuid)
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()

    elif cmd == "serve":
        import uvicorn
        uvicorn.run(
            "tab_ledger.server:app",
            host=args.host,
            port=args.port,
        )

    elif cmd == "mcp":
        import asyncio
        from .kb_mcp_server import main as mcp_main
        asyncio.run(mcp_main())


def _get_version():
    try:
        from . import __version__
        return __version__
    except ImportError:
        return "0.1.0"
