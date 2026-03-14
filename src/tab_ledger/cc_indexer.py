"""Claude Code Session Indexer — comprehensive JSONL parser for all CC sessions.

Extracts: token usage, tool calls, turn durations, models, costs, sidechains.
Indexes all ~1,275+ JSONL session files across ~52 project directories.
"""

import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from .categorizer import categorize_cc_session

from ._paths import CLAUDE_PROJECTS, LEDGER_DB


# Token pricing per million tokens (as of Feb 2026)
# https://docs.anthropic.com/en/docs/about-claude/models
TOKEN_PRICES = {
    "claude-opus-4-6": {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "claude-opus-4-5-20251101": {
        "input": 15.0, "output": 75.0,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-sonnet-4-5-20250929": {
        "input": 3.0, "output": 15.0,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80, "output": 4.0,
        "cache_write": 1.0, "cache_read": 0.08,
    },
}

# Default pricing for unknown models (use sonnet rates as safe middle)
DEFAULT_PRICES = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}


def get_project_name(project_dir: str) -> str:
    """Extract a readable project name from the directory-encoded path."""
    name = os.path.basename(project_dir)
    parts = name.replace("-Users-rileycoyote-", "").split("-")
    meaningful = [p for p in parts if p and p not in ("Documents", "Repositories", "CLAUDE", "claude", "code")]
    if meaningful:
        return meaningful[-1]
    return name


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_creation_tokens: int, cache_read_tokens: int) -> float:
    """Estimate USD cost from token counts and model."""
    prices = TOKEN_PRICES.get(model, DEFAULT_PRICES)
    cost = (
        (input_tokens / 1_000_000) * prices["input"]
        + (output_tokens / 1_000_000) * prices["output"]
        + (cache_creation_tokens / 1_000_000) * prices["cache_write"]
        + (cache_read_tokens / 1_000_000) * prices["cache_read"]
    )
    return round(cost, 6)


def parse_jsonl(jsonl_path: Path, project_name: str) -> dict | None:
    """Parse a JSONL session file, extracting all available data.

    Streams line-by-line to handle large files (some are 50MB+).
    """
    session_id = jsonl_path.stem
    is_sidechain = "subagents" in str(jsonl_path)

    # Accumulators
    first_prompt = ""
    summary = ""
    model = ""
    models_seen = Counter()
    git_branch = ""
    slug = ""
    version = ""
    started_at = ""
    ended_at = ""
    cwd = ""

    user_count = 0
    assistant_count = 0
    turn_count = 0

    input_tokens = 0
    output_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0

    total_duration_ms = 0
    tool_calls = Counter()
    tool_call_count = 0

    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type", "")
                timestamp = record.get("timestamp", "")

                # Track first/last timestamps
                if timestamp:
                    if not started_at:
                        started_at = timestamp
                    ended_at = timestamp

                # Extract common fields from first record that has them
                if not git_branch:
                    git_branch = record.get("gitBranch", "")
                if not slug:
                    slug = record.get("slug", "")
                if not version:
                    version = record.get("version", "")
                if not cwd:
                    cwd = record.get("cwd", "")

                # Detect sidechain from record field
                if record.get("isSidechain"):
                    is_sidechain = True

                # ── User messages ──
                if rec_type == "user":
                    user_count += 1
                    if not first_prompt:
                        msg = record.get("message", {})
                        if isinstance(msg, dict):
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        first_prompt = part.get("text", "")[:500]
                                        break
                            elif isinstance(content, str):
                                first_prompt = content[:500]
                        elif isinstance(msg, str):
                            first_prompt = msg[:500]

                # ── Assistant messages ──
                elif rec_type == "assistant":
                    assistant_count += 1
                    turn_count += 1
                    msg = record.get("message", {})
                    if isinstance(msg, dict):
                        # Model
                        msg_model = msg.get("model", "")
                        if msg_model:
                            models_seen[msg_model] += 1
                            if not model:
                                model = msg_model

                        # Token usage
                        usage = msg.get("usage", {})
                        if usage:
                            input_tokens += usage.get("input_tokens", 0)
                            output_tokens += usage.get("output_tokens", 0)
                            cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
                            cache_read_tokens += usage.get("cache_read_input_tokens", 0)

                        # Tool calls
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    tool_calls[tool_name] += 1
                                    tool_call_count += 1

                # ── System messages ──
                elif rec_type == "system":
                    subtype = record.get("subtype", "")
                    if subtype == "turn_duration":
                        total_duration_ms += record.get("durationMs", 0)

                # ── Summary records ──
                elif rec_type == "summary":
                    s = record.get("summary", "") or record.get("message", "")
                    if isinstance(s, str) and s:
                        summary = s[:500]
                    elif isinstance(s, list):
                        for part in s:
                            if isinstance(part, dict) and part.get("type") == "text":
                                summary = part.get("text", "")[:500]
                                break

    except (IOError, PermissionError) as e:
        print(f"  Error reading {jsonl_path.name}: {e}", file=sys.stderr)
        return None

    # Skip empty sessions
    if user_count == 0 and assistant_count == 0:
        return None

    # Use most common model if we saw multiple
    if models_seen:
        model = models_seen.most_common(1)[0][0]

    # Build tools_used as comma-separated sorted list
    tools_used = ",".join(sorted(tool_calls.keys()))

    # Estimate cost
    cost_usd = estimate_cost(model, input_tokens, output_tokens,
                             cache_creation_tokens, cache_read_tokens)

    # Category
    category = categorize_cc_session(summary, first_prompt, project_name)

    # Use cwd as project_path if available
    project_path = cwd or str(jsonl_path.parent)

    return {
        "session_id": session_id,
        "project_path": project_path,
        "project_name": project_name,
        "git_branch": git_branch,
        "summary": summary,
        "first_prompt": first_prompt,
        "category": category,
        "message_count": user_count,
        "model": model,
        "started_at": started_at,
        "ended_at": ended_at,
        "slug": slug,
        "is_sidechain": is_sidechain,
        "turn_count": turn_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "total_duration_ms": total_duration_ms,
        "cost_usd": cost_usd,
        "tools_used": tools_used,
        "tool_call_count": tool_call_count,
        "claude_code_version": version,
    }


def save_sessions(sessions: list[dict]):
    """Save indexed sessions to ledger.db."""
    conn = sqlite3.connect(LEDGER_DB)

    for s in sessions:
        conn.execute("""
            INSERT OR REPLACE INTO cc_sessions
            (session_id, project_path, project_name, git_branch, summary,
             first_prompt, category, message_count, model, started_at, ended_at,
             slug, is_sidechain, turn_count,
             input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
             total_duration_ms, cost_usd, tools_used, tool_call_count, claude_code_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            s["session_id"], s["project_path"], s["project_name"],
            s["git_branch"], s["summary"], s["first_prompt"],
            s["category"], s["message_count"], s["model"],
            s["started_at"], s["ended_at"],
            s["slug"], s["is_sidechain"], s["turn_count"],
            s["input_tokens"], s["output_tokens"],
            s["cache_creation_tokens"], s["cache_read_tokens"],
            s["total_duration_ms"], s["cost_usd"],
            s["tools_used"], s["tool_call_count"], s["claude_code_version"],
        ))

    conn.commit()
    conn.close()


def find_all_jsonl_files() -> list[tuple[Path, str]]:
    """Find all JSONL files across all project directories, including subagent dirs.

    Returns list of (jsonl_path, project_name) tuples.
    """
    results = []
    if not CLAUDE_PROJECTS.exists():
        return results

    for project_dir in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project_dir.is_dir():
            continue

        project_name = get_project_name(str(project_dir))

        # Top-level JSONL files
        for jsonl_file in project_dir.glob("*.jsonl"):
            results.append((jsonl_file, project_name))

        # Subagent JSONL files (nested under session dirs)
        for jsonl_file in project_dir.glob("*/subagents/*.jsonl"):
            results.append((jsonl_file, project_name))

    return results


def index_all(force: bool = False) -> dict:
    """Index all Claude Code sessions across all projects.

    Args:
        force: If True, re-index all sessions (not just new ones).
    """
    print("Indexing Claude Code sessions...")

    all_files = find_all_jsonl_files()
    if not all_files:
        print("  No JSONL files found.")
        return {"total": 0, "new": 0, "skipped": 0, "errors": 0, "projects": 0}

    # Get already-indexed session IDs (skip unless force)
    existing = set()
    if not force:
        conn = sqlite3.connect(LEDGER_DB)
        try:
            rows = conn.execute("SELECT session_id FROM cc_sessions").fetchall()
            existing = {r[0] for r in rows}
        except sqlite3.OperationalError:
            pass
        conn.close()

    # Filter to only new files
    to_process = []
    for jsonl_path, project_name in all_files:
        sid = jsonl_path.stem
        if force or sid not in existing:
            to_process.append((jsonl_path, project_name))

    projects_seen = set()
    for _, pn in all_files:
        projects_seen.add(pn)

    print(f"  Found {len(all_files)} total JSONL files across {len(projects_seen)} projects")
    print(f"  Already indexed: {len(existing)}, to process: {len(to_process)}")

    if not to_process:
        print("  Nothing new to index.")
        return {
            "total": len(existing),
            "new": 0,
            "skipped": 0,
            "errors": 0,
            "projects": len(projects_seen),
        }

    # Process in batches of 50 for progress reporting
    batch_size = 50
    all_sessions = []
    errors = 0
    skipped = 0

    for i, (jsonl_path, project_name) in enumerate(to_process):
        if (i + 1) % batch_size == 0 or i == 0:
            print(f"  Processing {i + 1}/{len(to_process)}...", end="\r")

        session = parse_jsonl(jsonl_path, project_name)
        if session:
            all_sessions.append(session)
        elif session is None:
            # Either error or empty file
            skipped += 1

    print(f"  Processed {len(to_process)} files" + " " * 20)

    if all_sessions:
        save_sessions(all_sessions)

    total = len(existing) + len(all_sessions) if not force else len(all_sessions)
    print(f"  Indexed {len(all_sessions)} new sessions (total: {total})")

    # Stats summary
    total_tokens_in = sum(s["input_tokens"] for s in all_sessions)
    total_tokens_out = sum(s["output_tokens"] for s in all_sessions)
    total_cost = sum(s["cost_usd"] for s in all_sessions)
    total_tools = sum(s["tool_call_count"] for s in all_sessions)
    sidechain_count = sum(1 for s in all_sessions if s["is_sidechain"])

    # Model breakdown
    model_counts = Counter(s["model"] for s in all_sessions if s["model"])

    # Category breakdown
    cat_counts = Counter(s["category"] for s in all_sessions)

    # Tool usage across all sessions
    all_tool_usage = Counter()
    for s in all_sessions:
        if s["tools_used"]:
            for tool in s["tools_used"].split(","):
                all_tool_usage[tool] += 1

    print(f"\n  === New Session Stats ===")
    print(f"  Tokens: {total_tokens_in:,} in / {total_tokens_out:,} out")
    print(f"  Est. cost: ${total_cost:,.2f}")
    print(f"  Tool calls: {total_tools:,}")
    print(f"  Sidechains: {sidechain_count}")

    if model_counts:
        print(f"\n  Models:")
        for m, c in model_counts.most_common():
            print(f"    {m}: {c} sessions")

    if cat_counts:
        print(f"\n  Categories:")
        for cat, count in cat_counts.most_common(10):
            print(f"    {cat}: {count}")

    if all_tool_usage:
        print(f"\n  Top tools (by session count):")
        for tool, count in all_tool_usage.most_common(10):
            print(f"    {tool}: {count} sessions")

    return {
        "total": total,
        "new": len(all_sessions),
        "skipped": skipped,
        "errors": errors,
        "projects": len(projects_seen),
        "tokens_in": total_tokens_in,
        "tokens_out": total_tokens_out,
        "est_cost": round(total_cost, 2),
        "tool_calls": total_tools,
        "sidechains": sidechain_count,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Index Claude Code sessions")
    parser.add_argument("--force", action="store_true", help="Re-index all sessions, not just new ones")
    args = parser.parse_args()

    from .snapshot import init_db
    init_db()
    result = index_all(force=args.force)
    print(f"\nDone. {result['total']} total sessions indexed.")
