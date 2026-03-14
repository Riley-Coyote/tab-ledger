"""Knowledge Base Taxonomy — Maps sessions to canonical projects and sub-projects.

Uses verified project_path values from ledger.db to assign every session
to a canonical project and sub-project. Path matching is deterministic
(exact substring match, first match wins, most specific paths first).
"""

import sqlite3
from pathlib import Path
from datetime import datetime

from .kb_schema import get_kb_db, KB_DB

from ._paths import LEDGER_DB

# ═══════════════════════════════════════════════════════
# PROJECT DEFINITIONS
# ═══════════════════════════════════════════════════════
#
# Format: (canonical_name, display_name, summarization_tier, sub_projects)
# sub_projects: list of (sub_name, display_name, path_substring)
#
# ORDERING RULES:
# 1. Within each project's sub_projects, more specific paths MUST come first
# 2. Projects with overlapping paths must be ordered carefully
#    (e.g., project sub-paths before project root)

# ─────────────────────────────────────────────────────
# SAMPLE TAXONOMY — Customize this for your projects.
#
# Format: (canonical_name, display_name, summarization_tier, sub_projects)
# sub_projects: list of (sub_name, display_name, path_substring)
#
# The path_substring is matched against the encoded project_path from
# ~/.claude/projects/. More specific paths must come first within each
# project's sub_projects list.
#
# Tiers: "opus" (detailed summaries), "haiku" (brief summaries)
# ─────────────────────────────────────────────────────

PROJECTS = [
    # ── Example: A web application with multiple sub-projects ──
    ("my-web-app", "My Web App", "opus", [
        ("api", "API Server",
         "my-web-app/api"),
        ("frontend", "Frontend",
         "my-web-app/frontend"),
        ("core", "Core",
         "Repositories/my-web-app"),
    ]),

    # ── Example: A CLI tool ──
    ("my-cli-tool", "My CLI Tool", "opus", [
        ("core", "Core",
         "Repositories/my-cli-tool"),
    ]),

    # ── Supporting tools (lower-priority summaries) ──
    ("tools", "Supporting Tools", "haiku", [
        ("tab-ledger", "Tab Ledger",
         ".tab-ledger"),
    ]),

    # ── Catch-all for exploration / ad-hoc sessions ──
    ("exploration", "Exploration & Ad-hoc", "haiku", [
        ("downloads", "Downloads",
         "/Downloads"),
        ("documents", "Documents",
         "/Documents"),
        ("repositories-root", "Repositories Root",
         "/Repositories"),
        ("root", "Home Root",
         None),
    ]),
]


def map_session(project_path: str) -> tuple:
    """Map a project_path to (project_canonical, sub_project_canonical).

    Returns:
        (project_canonical_name, sub_project_canonical_name)
        sub_project may be None if only project-level match.
    """
    if not project_path:
        return ("exploration", "root")

    for proj_name, _display, _tier, sub_projects in PROJECTS:
        for sub_name, _sub_display, path_pattern in sub_projects:
            if path_pattern is None:
                # Catch-all (must be last in list)
                continue
            if path_pattern in project_path:
                return (proj_name, sub_name)

    # If nothing matched, it's exploration/root
    return ("exploration", "root")


def get_summarization_tier(project_canonical: str) -> str:
    """Get the summarization tier for a project."""
    for proj_name, _display, tier, _subs in PROJECTS:
        if proj_name == project_canonical:
            return tier
    return "haiku"


def build_taxonomy():
    """Create all project and sub-project records in the KB database.

    Also imports all sessions from ledger.db's cc_sessions table,
    mapping each to its canonical project and sub-project.
    """
    kb = get_kb_db()
    ledger = sqlite3.connect(LEDGER_DB)
    ledger.row_factory = sqlite3.Row

    # Update progress
    kb.execute(
        "UPDATE kb_progress SET status='running', started_at=? WHERE stage='taxonomy'",
        (datetime.utcnow().isoformat(),)
    )
    kb.commit()

    # ── Step 1: Create project records ──
    project_id_map = {}  # canonical_name → id
    for proj_name, display_name, tier, _subs in PROJECTS:
        kb.execute(
            """INSERT OR IGNORE INTO kb_projects
               (canonical_name, display_name, summarization_tier)
               VALUES (?, ?, ?)""",
            (proj_name, display_name, tier)
        )
        row = kb.execute(
            "SELECT id FROM kb_projects WHERE canonical_name = ?", (proj_name,)
        ).fetchone()
        project_id_map[proj_name] = row["id"]
    kb.commit()
    print(f"  Created {len(project_id_map)} project records")

    # ── Step 2: Create sub-project records ──
    sub_project_id_map = {}  # (proj_name, sub_name) → id
    seen_subs = set()
    for proj_name, _display, _tier, sub_projects in PROJECTS:
        pid = project_id_map[proj_name]
        for sub_name, sub_display, path_pattern in sub_projects:
            key = (proj_name, sub_name)
            if key in seen_subs:
                # Duplicate sub_name (like MLP appearing twice) — skip second
                continue
            seen_subs.add(key)
            kb.execute(
                """INSERT OR IGNORE INTO kb_sub_projects
                   (project_id, canonical_name, display_name, path_pattern)
                   VALUES (?, ?, ?, ?)""",
                (pid, sub_name, sub_display, path_pattern or "(catch-all)")
            )
            row = kb.execute(
                """SELECT id FROM kb_sub_projects
                   WHERE project_id = ? AND canonical_name = ?""",
                (pid, sub_name)
            ).fetchone()
            sub_project_id_map[key] = row["id"]
    kb.commit()
    print(f"  Created {len(sub_project_id_map)} sub-project records")

    # ── Step 3: Import sessions from ledger.db ──
    sessions = ledger.execute("SELECT * FROM cc_sessions").fetchall()
    total = len(sessions)
    print(f"  Importing {total} sessions from ledger.db...")

    imported = 0
    unmapped = 0
    for i, sess in enumerate(sessions):
        proj_canonical, sub_canonical = map_session(sess["project_path"])
        pid = project_id_map.get(proj_canonical)
        sid = sub_project_id_map.get((proj_canonical, sub_canonical))

        if pid is None:
            pid = project_id_map["exploration"]
            unmapped += 1

        # Find JSONL path relative to ~/.claude/projects/
        jsonl_path = None
        jsonl_size = None
        # We'll populate these in the indexer stage

        kb.execute("""
            INSERT INTO kb_sessions (
                session_uuid, project_id, sub_project_id,
                project_path, project_name_original, git_branch, slug, model,
                started_at, ended_at, message_count, turn_count, is_sidechain,
                input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                total_duration_ms, cost_usd, tools_used, tool_call_count,
                first_prompt, cc_version
            ) VALUES (
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?
            )
            ON CONFLICT(session_uuid) DO UPDATE SET
                project_id = excluded.project_id,
                sub_project_id = excluded.sub_project_id,
                project_path = excluded.project_path,
                project_name_original = excluded.project_name_original,
                git_branch = excluded.git_branch,
                slug = excluded.slug,
                model = excluded.model,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                message_count = excluded.message_count,
                turn_count = excluded.turn_count,
                is_sidechain = excluded.is_sidechain,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cache_creation_tokens = excluded.cache_creation_tokens,
                cache_read_tokens = excluded.cache_read_tokens,
                total_duration_ms = excluded.total_duration_ms,
                cost_usd = excluded.cost_usd,
                tools_used = excluded.tools_used,
                tool_call_count = excluded.tool_call_count,
                first_prompt = excluded.first_prompt,
                cc_version = excluded.cc_version,
                updated_at = CURRENT_TIMESTAMP
        """, (
            sess["session_id"], pid, sid,
            sess["project_path"], sess["project_name"], sess["git_branch"],
            sess["slug"], sess["model"],
            sess["started_at"], sess["ended_at"], sess["message_count"],
            sess["turn_count"], sess["is_sidechain"],
            sess["input_tokens"], sess["output_tokens"],
            sess["cache_creation_tokens"], sess["cache_read_tokens"],
            sess["total_duration_ms"], sess["cost_usd"],
            sess["tools_used"], sess["tool_call_count"],
            sess["first_prompt"], sess["claude_code_version"],
        ))
        imported += 1

        if (i + 1) % 200 == 0:
            kb.commit()
            print(f"    {i + 1}/{total}...")

    kb.commit()

    # ── Step 4: Update project aggregates ──
    for proj_name, pid in project_id_map.items():
        stats = kb.execute("""
            SELECT
                COUNT(*) as cnt,
                ROUND(SUM(cost_usd), 2) as cost,
                MIN(started_at) as first_at,
                MAX(ended_at) as last_at
            FROM kb_sessions WHERE project_id = ?
        """, (pid,)).fetchone()

        kb.execute("""
            UPDATE kb_projects SET
                total_sessions = ?,
                total_cost_usd = ?,
                first_session_at = ?,
                last_session_at = ?
            WHERE id = ?
        """, (stats["cnt"], stats["cost"], stats["first_at"], stats["last_at"], pid))

    # Update sub-project counts
    for (proj_name, sub_name), sid in sub_project_id_map.items():
        cnt = kb.execute(
            "SELECT COUNT(*) FROM kb_sessions WHERE sub_project_id = ?", (sid,)
        ).fetchone()[0]
        kb.execute(
            "UPDATE kb_sub_projects SET session_count = ? WHERE id = ?", (cnt, sid)
        )

    kb.commit()

    # ── Step 5: Update progress ──
    kb.execute("""
        UPDATE kb_progress SET
            status='completed', processed=?, total=?,
            completed_at=?, notes=?
        WHERE stage='taxonomy'
    """, (imported, total, datetime.utcnow().isoformat(),
          f"Imported {imported} sessions, {unmapped} fell to exploration catch-all"))
    kb.commit()

    # ── Report ──
    print(f"\n  Taxonomy complete:")
    print(f"    Sessions imported: {imported}")
    print(f"    Unmapped (→ exploration): {unmapped}")
    print(f"    Projects: {len(project_id_map)}")
    print(f"    Sub-projects: {len(sub_project_id_map)}")

    # Project breakdown
    print(f"\n  Project breakdown:")
    for row in kb.execute("""
        SELECT canonical_name, total_sessions, total_cost_usd, summarization_tier
        FROM kb_projects ORDER BY total_sessions DESC
    """).fetchall():
        print(f"    {row['canonical_name']:20s} {row['total_sessions']:5d} sessions"
              f"  ${row['total_cost_usd']:>8.2f}  [{row['summarization_tier']}]")

    ledger.close()
    kb.close()


if __name__ == "__main__":
    build_taxonomy()
