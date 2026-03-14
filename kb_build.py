#!/usr/bin/env python3
"""Knowledge Base Builder — Master orchestrator for building the complete KB.

Runs all stages in sequence:
  0. Schema creation
  1. Project taxonomy mapping + session import
  2. Message-level JSONL indexing
  3. FTS5 full-text search index
  4. Summarization (API calls)
  5. Cross-session linking
  6. Auxiliary data (commands, plans, todos, teams, claude.ai)
  7. Verification

Usage:
    python3 kb_build.py                     # Run all stages
    python3 kb_build.py --from 4            # Resume from stage 4
    python3 kb_build.py --only 1            # Run only stage 1
    python3 kb_build.py --skip-summarize    # Skip the API-calling stage
    python3 kb_build.py --drop              # Drop and rebuild from scratch
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def stage_0_schema(drop: bool = False):
    """Create the knowledge base schema."""
    print("\n" + "=" * 60)
    print("STAGE 0: SCHEMA CREATION")
    print("=" * 60)
    from kb_schema import create_schema, verify_schema
    create_schema(drop_existing=drop)
    counts = verify_schema()
    print(f"\nVerification: {len(counts)} tables created")
    for name, count in sorted(counts.items()):
        if not name.startswith("sqlite_"):
            print(f"  {name}: {count} rows")


def stage_1_taxonomy():
    """Build project taxonomy and import sessions."""
    print("\n" + "=" * 60)
    print("STAGE 1: PROJECT TAXONOMY & SESSION IMPORT")
    print("=" * 60)
    from kb_taxonomy import build_taxonomy
    build_taxonomy()


def stage_2_messages():
    """Index messages from all JSONL files."""
    print("\n" + "=" * 60)
    print("STAGE 2: MESSAGE INDEXING")
    print("=" * 60)
    from kb_indexer import index_all_messages
    result = index_all_messages(resume=True)
    print(f"\nResult: {result}")


def stage_3_fts():
    """Build FTS5 full-text search index from indexed messages."""
    print("\n" + "=" * 60)
    print("STAGE 3: FTS INDEX BUILD")
    print("=" * 60)
    from kb_schema import get_kb_db
    kb = get_kb_db()

    # Update progress
    kb.execute(
        "UPDATE kb_progress SET status='running', started_at=? WHERE stage='fts_build'",
        (datetime.now(timezone.utc).isoformat(),)
    )
    kb.commit()

    # Check how many FTS entries already exist
    existing = kb.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0]
    print(f"  Existing FTS entries: {existing}")

    if existing > 0:
        print("  FTS index already has entries. Skipping to avoid duplicates.")
        print("  (To rebuild, delete kb_fts entries first)")
        kb.execute(
            "UPDATE kb_progress SET status='completed', notes='skipped - already populated' WHERE stage='fts_build'"
        )
        kb.commit()
        kb.close()
        return

    # Index messages into FTS
    print("  Indexing messages into FTS5...")
    batch_size = 5000
    offset = 0
    total_indexed = 0

    while True:
        rows = kb.execute("""
            SELECT m.content_text, s.session_uuid, p.canonical_name
            FROM kb_messages m
            JOIN kb_sessions s ON m.session_id = s.id
            JOIN kb_projects p ON s.project_id = p.id
            WHERE m.content_text IS NOT NULL AND m.content_text != ''
            LIMIT ? OFFSET ?
        """, (batch_size, offset)).fetchall()

        if not rows:
            break

        for row in rows:
            kb.execute(
                "INSERT INTO kb_fts (text, session_uuid, source_type, project_name) VALUES (?, ?, 'message', ?)",
                (row["content_text"], row["session_uuid"], row["canonical_name"])
            )

        kb.commit()
        total_indexed += len(rows)
        offset += batch_size
        print(f"    Indexed {total_indexed} messages...", end="\r")

    print(f"  Messages indexed: {total_indexed}        ")

    # Index existing summaries
    print("  Indexing summaries into FTS5...")
    summary_count = 0
    rows = kb.execute("""
        SELECT s.summary_text, s.session_uuid, p.canonical_name
        FROM kb_sessions s
        JOIN kb_projects p ON s.project_id = p.id
        WHERE s.summary_text IS NOT NULL AND s.summary_text != ''
    """).fetchall()
    for row in rows:
        kb.execute(
            "INSERT INTO kb_fts (text, session_uuid, source_type, project_name) VALUES (?, ?, 'summary', ?)",
            (row["summary_text"], row["session_uuid"], row["canonical_name"])
        )
        summary_count += 1
    kb.commit()
    print(f"  Summaries indexed: {summary_count}")

    # Optimize FTS
    print("  Optimizing FTS index...")
    kb.execute("INSERT INTO kb_fts(kb_fts) VALUES('optimize')")
    kb.commit()

    total = total_indexed + summary_count
    kb.execute("""
        UPDATE kb_progress SET
            status='completed', processed=?, total=?,
            completed_at=?, notes=?
        WHERE stage='fts_build'
    """, (total, total, datetime.now(timezone.utc).isoformat(),
          f"Indexed {total_indexed} messages + {summary_count} summaries"))
    kb.commit()
    kb.close()

    print(f"\n  FTS build complete: {total} documents indexed")


def stage_4_summarize():
    """Generate summaries for unsummarized sessions."""
    print("\n" + "=" * 60)
    print("STAGE 4: SUMMARIZATION (API calls)")
    print("=" * 60)
    from kb_summarizer import run_summarization
    result = run_summarization(batch_size=20, resume=True)
    print(f"\nResult: {result}")


def stage_5_linking():
    """Detect and create cross-session connections."""
    print("\n" + "=" * 60)
    print("STAGE 5: CROSS-SESSION LINKING")
    print("=" * 60)
    from kb_linker import build_all_connections
    result = build_all_connections()
    print(f"\nResult: {result}")


def stage_6_auxiliary():
    """Index auxiliary data sources."""
    print("\n" + "=" * 60)
    print("STAGE 6: AUXILIARY DATA INDEXING")
    print("=" * 60)
    from kb_auxiliary import index_all_auxiliary
    result = index_all_auxiliary()
    print(f"\nResult: {result}")


def stage_7_verify():
    """Run verification checks on the complete knowledge base."""
    print("\n" + "=" * 60)
    print("STAGE 7: VERIFICATION")
    print("=" * 60)
    from kb_schema import get_kb_db

    kb = get_kb_db(readonly=True)

    checks = []

    # Check 1: All sessions have project assignments
    total_sessions = kb.execute("SELECT COUNT(*) FROM kb_sessions").fetchone()[0]
    null_projects = kb.execute(
        "SELECT COUNT(*) FROM kb_sessions WHERE project_id IS NULL"
    ).fetchone()[0]
    checks.append(("All sessions have project_id", null_projects == 0,
                    f"{total_sessions} sessions, {null_projects} without project"))

    # Check 2: Project session counts match
    project_sum = kb.execute(
        "SELECT SUM(total_sessions) FROM kb_projects"
    ).fetchone()[0] or 0
    checks.append(("Project session counts sum correctly",
                    project_sum == total_sessions,
                    f"Projects sum: {project_sum}, actual: {total_sessions}"))

    # Check 3: Messages exist
    msg_count = kb.execute("SELECT COUNT(*) FROM kb_messages").fetchone()[0]
    checks.append(("Messages indexed", msg_count > 0,
                    f"{msg_count} messages"))

    # Check 4: FTS has entries
    fts_count = kb.execute("SELECT COUNT(*) FROM kb_fts").fetchone()[0]
    checks.append(("FTS index populated", fts_count > 0,
                    f"{fts_count} FTS entries"))

    # Check 5: Summaries generated
    summary_count = kb.execute(
        "SELECT COUNT(*) FROM kb_sessions WHERE summary_version > 0"
    ).fetchone()[0]
    checks.append(("Summaries generated", summary_count > 0,
                    f"{summary_count}/{total_sessions} summarized"))

    # Check 6: Connections created
    conn_count = kb.execute("SELECT COUNT(*) FROM kb_connections").fetchone()[0]
    checks.append(("Connections created", conn_count > 0,
                    f"{conn_count} connections"))

    # Check 7: FTS search works
    try:
        test_results = kb.execute(
            "SELECT COUNT(*) FROM kb_fts WHERE kb_fts MATCH 'claude'"
        ).fetchone()[0]
        checks.append(("FTS search functional", test_results > 0,
                        f"'claude' matched {test_results} docs"))
    except Exception as e:
        checks.append(("FTS search functional", False, str(e)))

    # Check 8: Auxiliary data
    cmd_count = kb.execute("SELECT COUNT(*) FROM kb_commands").fetchone()[0]
    plan_count = kb.execute("SELECT COUNT(*) FROM kb_plans").fetchone()[0]
    todo_count = kb.execute("SELECT COUNT(*) FROM kb_todos").fetchone()[0]
    checks.append(("Auxiliary data indexed", cmd_count > 0,
                    f"Commands: {cmd_count}, Plans: {plan_count}, Todos: {todo_count}"))

    # Check 9: Database integrity
    integrity = kb.execute("PRAGMA integrity_check").fetchone()[0]
    checks.append(("Database integrity", integrity == "ok", integrity))

    # Report
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print(f"\n  Verification: {passed}/{total} checks passed\n")
    for name, ok, detail in checks:
        status = "✓" if ok else "✗"
        print(f"  {status} {name}: {detail}")

    # Summary stats
    print(f"\n  === KNOWLEDGE BASE SUMMARY ===")
    print(f"  Sessions:     {total_sessions}")
    print(f"  Messages:     {msg_count}")
    print(f"  FTS entries:  {fts_count}")
    print(f"  Summaries:    {summary_count}")
    print(f"  Connections:  {conn_count}")
    print(f"  Commands:     {cmd_count}")
    print(f"  Plans:        {plan_count}")
    print(f"  Todos:        {todo_count}")

    # DB file size
    db_path = Path.home() / ".tab-ledger" / "knowledge_base.db"
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024 * 1024)
        print(f"  DB size:      {size_mb:.1f} MB")

    # Update progress
    kb.close()
    kb_w = get_kb_db()
    kb_w.execute("""
        UPDATE kb_progress SET
            status='completed', processed=?, total=?,
            completed_at=?, notes=?
        WHERE stage='verification'
    """, (passed, total, datetime.now(timezone.utc).isoformat(),
          f"{passed}/{total} checks passed"))
    kb_w.commit()
    kb_w.close()

    if passed < total:
        print(f"\n  WARNING: {total - passed} checks failed!")
    else:
        print(f"\n  All checks passed. Knowledge base is ready.")


def stage_8_semantic(provider: str, model: str | None = None, include_messages: bool = False):
    """Build/refresh semantic embedding index."""
    print("\n" + "=" * 60)
    print("STAGE 8: SEMANTIC INDEXING")
    print("=" * 60)
    from kb_schema import get_kb_db
    from kb_semantic import create_embedding_provider, build_semantic_index

    kb = get_kb_db()
    try:
        embedder = create_embedding_provider(provider, model=model)
        result = build_semantic_index(
            kb,
            provider=embedder,
            include_messages=include_messages,
        )
    finally:
        kb.close()
    print(f"\nResult: {result}")


STAGES = [
    (0, "Schema creation", stage_0_schema),
    (1, "Taxonomy & session import", stage_1_taxonomy),
    (2, "Message indexing", stage_2_messages),
    (3, "FTS index build", stage_3_fts),
    (4, "Summarization", stage_4_summarize),
    (5, "Cross-session linking", stage_5_linking),
    (6, "Auxiliary data", stage_6_auxiliary),
    (7, "Verification", stage_7_verify),
]


def main():
    parser = argparse.ArgumentParser(
        description="Build the Master Knowledge Base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Stages:
  0  Schema creation
  1  Taxonomy & session import
  2  Message indexing (parse all JSONLs)
  3  FTS index build
  4  Summarization (Claude CLI/API calls)
  5  Cross-session linking
  6  Auxiliary data (commands, plans, todos, teams)
  7  Verification

Optional:
  --semantic-provider hash|ollama|openai  Run semantic index stage after stage 7
        """
    )
    parser.add_argument("--from", type=int, dest="from_stage", default=0,
                        help="Resume from this stage number")
    parser.add_argument("--only", type=int, default=None,
                        help="Run only this stage number")
    parser.add_argument("--skip-summarize", action="store_true",
                        help="Skip stage 4 (summarization / API calls)")
    parser.add_argument("--drop", action="store_true",
                        help="Drop and rebuild the knowledge base from scratch")
    parser.add_argument("--semantic-provider", default=None,
                        help="Optional semantic embedding provider to run after stage 7 (hash|ollama|openai)")
    parser.add_argument("--semantic-model", default=None,
                        help="Optional semantic model override")
    parser.add_argument("--semantic-include-messages", action="store_true",
                        help="Include long message bodies in semantic index (larger index)")

    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════╗")
    print("║     CLAUDE CODE MASTER KNOWLEDGE BASE — BUILDER        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  DB path: {Path.home() / '.tab-ledger' / 'knowledge_base.db'}")

    start_time = time.time()

    for stage_num, stage_name, stage_fn in STAGES:
        if args.only is not None and stage_num != args.only:
            continue
        if stage_num < args.from_stage:
            print(f"\n  [Skipping stage {stage_num}: {stage_name}]")
            continue
        if args.skip_summarize and stage_num == 4:
            print(f"\n  [Skipping stage 4: Summarization (--skip-summarize)]")
            continue

        try:
            if stage_num == 0:
                stage_fn(drop=args.drop)
            else:
                stage_fn()
        except Exception as e:
            print(f"\n  ERROR in stage {stage_num} ({stage_name}): {e}")
            import traceback
            traceback.print_exc()
            print(f"\n  To resume from this stage: python3 kb_build.py --from {stage_num}")
            sys.exit(1)

    if args.semantic_provider:
        try:
            stage_8_semantic(
                provider=args.semantic_provider,
                model=args.semantic_model,
                include_messages=args.semantic_include_messages,
            )
        except Exception as e:
            print(f"\n  ERROR in stage 8 (Semantic indexing): {e}")
            import traceback
            traceback.print_exc()
            print("\n  You can rerun semantic stage with:")
            print(
                "  python3 kb_semantic.py index --provider "
                f"{args.semantic_provider}"
            )
            sys.exit(1)

    elapsed = time.time() - start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    seconds = int(elapsed % 60)

    print(f"\n{'=' * 60}")
    print(f"BUILD COMPLETE")
    print(f"  Duration: {hours}h {minutes}m {seconds}s")
    print(f"  Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
