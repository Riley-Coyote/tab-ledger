"""Knowledge Base Taxonomy — Maps sessions to canonical projects and sub-projects.

Uses verified project_path values from ledger.db to assign every session
to a canonical project and sub-project. Path matching is deterministic
(exact substring match, first match wins, most specific paths first).
"""

import sqlite3
from pathlib import Path
from datetime import datetime

from kb_schema import get_kb_db, KB_DB

LEDGER_DB = Path.home() / ".tab-ledger" / "ledger.db"

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
#    (e.g., Sanctuary sub-projects before Sanctuary root)

PROJECTS = [
    ("polyphonic", "Polyphonic", "opus", [
        ("twitter-bot/anima-website/design-docs", "Anima Website Design Docs",
         "polyphonic-twitter-bot/anima-website/design-docs"),
        ("twitter-bot/anima-website", "Anima Website",
         "polyphonic-twitter-bot/anima-website"),
        ("twitter-bot", "Twitter Bot",
         "polyphonic-twitter-bot"),
        ("staging-branch", "Staging Branch (Jan 2026)",
         "branches/staging-jan-14-2026-polyphonic"),
        ("opus-branch/polyphonic", "Opus 4.6 Branch (Polyphonic sub)",
         "branches/Opus4.6-branch/polyphonic"),
        ("opus-branch", "Opus 4.6 Branch",
         "branches/Opus4.6-branch"),
        ("gpt-bot", "GPT Bot Variant",
         "polyphonic-gpt-bot"),
        ("conversation-sharing", "Conversation Sharing",
         "claude-artifacts/polyphonic-master-plan"),
        ("memory-artifacts", "Memory Artifacts",
         "claude-artifacts/Memory"),
        ("poly-nexus-DNA/dashboard", "Poly-Nexus DNA Dashboard",
         "poly-nexus-DNA/intelligence-layer-dashboard"),
        ("poly-nexus-DNA", "Poly-Nexus DNA",
         "poly-nexus-DNA"),
        ("explainer", "Polyphonic Explainer",
         "polyphonic-explainer"),
        ("branches-root", "Branches Root",
         "Polyphonic/branches"),
        ("core", "Core",
         "Repositories/Polyphonic"),
    ]),

    ("sanctuary", "The Sanctuary", "opus", [
        ("files/sanctuary-site", "Sanctuary Site (via Files)",
         "The-Sanctuary/files/sanctuary-site"),
        ("files/sanctuary-project", "Sanctuary Project (via Files)",
         "The-Sanctuary/files/sanctuary-project"),
        ("files", "Files System",
         "The-Sanctuary/files"),
        ("pokemon-clone", "Pokémon Sapphire Clone",
         "pokemon-game/pokemon-sapphire-clone"),
        ("SIGIL-poa-site", "SIGIL Proof-of-Auth Site",
         "The-Sanctuary/SIGIL/poa-site"),
        ("SIGIL-integration", "SIGIL Integration",
         "The-Sanctuary/SIGIL"),
        ("components", "Components",
         "The-Sanctuary/components"),
        ("archive-v1", "Archive V1",
         "The-Sanctuary/archive"),
        ("sanctuary-app-legacy", "Sanctuary App (Legacy)",
         "The-Sanctuary/sanctuary-app"),
        ("sanctuary-main", "Sanctuary Main App",
         "The-Sanctuary/sanctuary"),
        ("teletype", "Teletype",
         "sanctuary-teletype"),
        ("root", "Root",
         "Repositories/The-Sanctuary"),
    ]),

    ("sigil", "SIGIL Protocol", "opus", [
        ("sigil-protocol", "Protocol Repo",
         "Repositories/sigil-protocol"),
        ("SIGIL-repo", "SIGIL Repo",
         "Repositories/SIGIL-repo"),
        ("sigil-site", "SIGIL Site",
         "clawd/sigil-site"),
    ]),

    ("nexus", "Nexus", "opus", [
        ("dashboard", "Nexus Dashboard",
         "nexus-truth-0121-26/nexus-dashboard"),
        ("research-simple", "Research Simple",
         "Nexus-research-simple"),
        ("nexus-truth", "Nexus Truth",
         "nexus-truth-0121-26"),
    ]),

    ("vessel", "Vessel Chat", "opus", [
        ("vessel-sub", "Vessel Sub-app",
         "vessel-chat/vessel"),
        ("files", "Vessel Files",
         "vessel-chat/files"),
        ("core", "Core",
         "Repositories/vessel-chat"),
    ]),

    ("clawdbot", "Clawdbot", "opus", [
        ("core", "Core",
         "Repositories/clawdbot"),
    ]),

    ("vektor", "Vektor Terminal", "opus", [
        ("core", "Core",
         "clawd/vektor-terminal"),
    ]),

    ("anima", "Anima", "opus", [
        ("dashboard", "Anima Dashboard",
         "inner_life/anima-dashboard"),
        ("inner-life", "Inner Life",
         "clawd-anima/inner_life"),
    ]),

    ("tools", "Supporting Tools", "haiku", [
        ("claudeette-dashboard", "Claudeette Dashboard",
         "claudeette-dashboard"),
        ("MLP", "Memory Ledger Protocol",
         "memory-ledger-protocol-MLP"),
        ("MLP", "Memory Ledger Protocol",
         "model-ledger-protocol-MLP"),
        ("FRAME", "FRAME",
         "Repositories/FRAME"),
        ("deep-frame", "Deep Frame",
         "CLAUDE/[deep]frame"),
        ("superclaude", "Superclaude",
         "Repositories/superclaude"),
        ("forge", "Forge",
         "Repositories/forge"),
        ("SUPERMEMORY", "Supermemory",
         "Repositories/SUPERMEMORY"),
        ("tab-ledger", "Tab Ledger",
         ".tab-ledger"),
    ]),

    ("data-research", "Data & Research", "opus", [
        ("ai-rights", "AI Rights Documents",
         "AI_RIGHTS_DOCUMENTS"),
        ("chatgpt-browse", "ChatGPT Data Browser",
         "BROWSE_ME"),
        ("chatgpt-data", "ChatGPT Data Root",
         "CHATGPT-Data/ChatGPT_data"),
        ("chat-history-import", "Chat History Import",
         "chat_convo_history/claude/data-"),
        ("chat-history-root", "Chat History Root",
         "chat_convo_history/claude"),
        ("chat-history", "Chat History",
         "chat_convo_history"),
        ("x-algorithm", "X Algorithm Suite",
         "x-algorithm-suite"),
    ]),

    ("exploration", "Exploration & Ad-hoc", "haiku", [
        ("untitled", "Untitled",
         "untitled folder"),
        ("nonsense", "Nonsense",
         "Repositories/nonsense"),
        ("canvas", "Canvas",
         "claude-canvas"),
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
            INSERT OR REPLACE INTO kb_sessions (
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
