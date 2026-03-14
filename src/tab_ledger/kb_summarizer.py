"""Knowledge Base Summarizer — Generates structured summaries for Claude Code sessions.

This module reads JSONL files, extracts content intelligently based on file size,
and calls the Anthropic API to generate summaries with structured JSON output.

Two-tier summarization:
  - "opus" tier: claude-opus-4-6 for major projects
  - "haiku" tier: claude-haiku-4-5-20251001 for minor/ad-hoc projects

Intelligent content extraction with size-based sampling:
  - <5 MB: ALL human messages + first/last 3 assistant responses
  - 5-50 MB: First 5 + last 3 human, every 10th in between. Assistant >500 chars.
  - 50-100 MB: First 3 + last 2 human, every 20th in between. Assistant >1000 chars.
  - 100+ MB: First 2 + last 1 human. Top 5 longest assistant text blocks.
  - Mega sessions (>100 MB): special "deep archive" analysis instead
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import subprocess

import os as _os

from .kb_schema import get_kb_db


def _claude_env():
    """Return a copy of os.environ without CLAUDECODE to avoid nested-session errors."""
    env = _os.environ.copy()
    env.pop("CLAUDECODE", None)
    return env

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

MAJOR_PROJECTS = {
    # Add your major project names here
    # e.g. "my-web-app", "my-cli-tool"
}

SUMMARIZATION_MODELS = {
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# File size thresholds (in bytes)
TINY_THRESHOLD = 5 * 1024 * 1024  # 5 MB
MEDIUM_THRESHOLD = 50 * 1024 * 1024  # 50 MB
LARGE_THRESHOLD = 100 * 1024 * 1024  # 100 MB

# Content extraction minimums for text blocks
MIN_ASSISTANT_CHARS_TINY = 0  # All messages
MIN_ASSISTANT_CHARS_SMALL = 500
MIN_ASSISTANT_CHARS_MEDIUM = 1000
MIN_ASSISTANT_CHARS_LARGE = 1000  # For mega archives, use top 5 by length

# Batch and rate limiting
BATCH_SIZE = 20
API_DELAY_SECONDS = 1
RETRY_COUNT = 3
RETRY_BACKOFF_BASE = 2

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_text_parts(content_blocks) -> List[str]:
    """Extract text from content blocks, handling both string and dict formats."""
    text_parts = []
    # content can be a plain string instead of a list of blocks
    if isinstance(content_blocks, str):
        text = content_blocks.strip()
        if text:
            text_parts.append(text)
        return text_parts
    # Standard list-of-blocks format
    for block in content_blocks:
        if isinstance(block, str):
            text = block.strip()
            if text:
                text_parts.append(text)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                text_parts.append(text)
    return text_parts


def extract_content(jsonl_path: Path, file_size: int) -> List[Dict[str, Any]]:
    """
    Extract human and assistant messages from JSONL with intelligent sampling.

    Args:
        jsonl_path: Path to the JSONL file
        file_size: Size of the file in bytes

    Returns:
        List of dicts: [{"role": "user"|"assistant", "content": str}, ...]
    """
    messages = []
    human_messages = []
    assistant_messages = []

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping invalid JSON at {jsonl_path}:{line_num}")
                    continue

                record_type = record.get("type")

                # Skip irrelevant record types
                if record_type in ("progress", "file-history-snapshot", "system"):
                    continue

                # Extract human messages
                if record_type == "user":
                    msg_data = record.get("message", {})
                    content_blocks = msg_data.get("content", [])

                    text_parts = _extract_text_parts(content_blocks)

                    if text_parts:
                        full_text = "\n".join(text_parts)
                        human_messages.append(full_text)

                # Extract assistant messages (text only, no thinking/tool_use)
                elif record_type == "assistant":
                    msg_data = record.get("message", {})
                    content_blocks = msg_data.get("content", [])

                    text_parts = _extract_text_parts(content_blocks)

                    if text_parts:
                        full_text = "\n".join(text_parts)
                        assistant_messages.append(full_text)

    except (OSError, IOError) as e:
        logger.error(f"Error reading {jsonl_path}: {e}")
        return []

    # Apply size-based sampling strategy
    sampled_human = _sample_messages(human_messages, file_size, is_human=True)
    sampled_assistant = _sample_messages(
        assistant_messages, file_size, is_human=False
    )

    # Interleave human and assistant in rough order (simplified)
    # For simplicity, we'll just concatenate with clear separation
    for msg in sampled_human:
        messages.append({"role": "user", "content": msg})

    for msg in sampled_assistant:
        messages.append({"role": "assistant", "content": msg})

    return messages


def _sample_messages(
    messages: List[str], file_size: int, is_human: bool
) -> List[str]:
    """Sample messages based on file size tier."""
    if not messages:
        return []

    if file_size < TINY_THRESHOLD:
        # <5 MB: ALL messages
        return messages

    elif file_size < MEDIUM_THRESHOLD:
        # 5-50 MB
        if is_human:
            # First 5, last 3, every 10th in between
            return _sample_with_stride(messages, first=5, last=3, stride=10)
        else:
            # Filter by minimum chars
            filtered = [m for m in messages if len(m) > MIN_ASSISTANT_CHARS_SMALL]
            return _sample_with_stride(filtered, first=5, last=3, stride=10)

    elif file_size < LARGE_THRESHOLD:
        # 50-100 MB
        if is_human:
            # First 3, last 2, every 20th in between
            return _sample_with_stride(messages, first=3, last=2, stride=20)
        else:
            # Filter by minimum chars
            filtered = [m for m in messages if len(m) > MIN_ASSISTANT_CHARS_MEDIUM]
            return _sample_with_stride(filtered, first=3, last=2, stride=20)

    else:
        # 100+ MB (mega archives)
        if is_human:
            # First 2, last 1
            result = []
            if len(messages) > 0:
                result.append(messages[0])
            if len(messages) > 1:
                result.append(messages[1])
            if len(messages) > 2:
                result.append(messages[-1])
            return result
        else:
            # Top 5 longest by length
            sorted_msgs = sorted(messages, key=len, reverse=True)
            return sorted_msgs[:5]


def _sample_with_stride(
    messages: List[str], first: int, last: int, stride: int
) -> List[str]:
    """Sample: first N + last M + every stride-th in between."""
    if len(messages) <= first + last:
        return messages

    result = []

    # Add first N
    result.extend(messages[:first])

    # Add every stride-th in the middle
    middle_start = first
    middle_end = len(messages) - last
    for i in range(middle_start, middle_end, stride):
        result.append(messages[i])

    # Add last M
    result.extend(messages[-last:])

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY GENERATION
# ═══════════════════════════════════════════════════════════════════════════════


def build_summary_prompt(metadata: Dict[str, Any], content: List[Dict[str, str]]) -> str:
    """
    Build a summarization prompt for the Anthropic API.

    Returns a prompt asking for a JSON response with specific fields.
    """
    # Format content for the prompt
    content_preview = []
    for i, msg in enumerate(content[:20]):  # Show first 20 messages as context
        role = msg.get("role", "unknown")
        text = msg.get("content", "")[:200]  # Truncate for readability
        content_preview.append(f"[{role.upper()}] {text}...")

    content_str = "\n".join(content_preview)

    # Build the prompt
    prompt = f"""Analyze this Claude Code session and generate a structured summary.

SESSION METADATA:
- Session UUID: {metadata.get('session_uuid', 'unknown')}
- Project: {metadata.get('project_name', 'unknown')}
- Model: {metadata.get('model', 'unknown')}
- Started: {metadata.get('started_at', 'unknown')}
- Duration: {metadata.get('total_duration_ms', 'unknown')} ms
- Turn count: {metadata.get('turn_count', 0)}
- Message count: {metadata.get('message_count', 0)}

CONVERSATION EXCERPT:
{content_str}

Generate a JSON response with this exact structure (no markdown, pure JSON):
{{
  "objective": "1-2 sentence description of what the user was trying to accomplish",
  "actions_taken": "3-5 bullet points describing major actions/commands",
  "outcome": "description of the result or current state",
  "files_touched": ["list", "of", "key", "files", "or", "empty"],
  "blockers": "description of problems encountered, or null if none",
  "next_steps": "what should happen next, or null if session concluded",
  "phase": "one of: research|prototype|build|deploy|debug|refactor|explore|data-processing",
  "tags": ["3-5", "keyword", "tags"]
}}

Return ONLY valid JSON, no explanations."""

    return prompt


def summarize_session(kb_conn: sqlite3.Connection, session_row: sqlite3.Row) -> bool:
    """
    Generate a summary for a single session.

    Args:
        kb_conn: Database connection
        session_row: Row from kb_sessions with summary_version=0

    Returns:
        True if successful, False if failed
    """
    session_id = session_row["id"]
    session_uuid = session_row["session_uuid"]
    project_id = session_row["project_id"]

    logger.info(f"Summarizing session {session_uuid}...")

    # Find the JSONL file
    jsonl_path = _find_jsonl_for_session(session_uuid, session_row)
    if not jsonl_path or not jsonl_path.exists():
        logger.warning(f"Could not locate JSONL for session {session_uuid}")
        return False

    # Get file size
    try:
        file_size = jsonl_path.stat().st_size
    except OSError:
        logger.error(f"Could not stat {jsonl_path}")
        return False

    # For mega-archives (>100 MB), use special analysis
    if file_size > LARGE_THRESHOLD:
        return _summarize_deep_archive(kb_conn, session_row, jsonl_path, file_size)

    # Extract content
    content = extract_content(jsonl_path, file_size)
    if not content:
        logger.warning(f"No content extracted from {session_uuid}")
        return False

    # Get project info for tier selection
    project_row = kb_conn.execute(
        "SELECT summarization_tier, canonical_name FROM kb_projects WHERE id = ?",
        (project_id,),
    ).fetchone()

    tier = "haiku"  # default
    if project_row:
        tier = project_row["summarization_tier"] or "haiku"

    model_name = SUMMARIZATION_MODELS.get(tier, SUMMARIZATION_MODELS["haiku"])

    # Build metadata dict
    metadata = {
        "session_uuid": session_uuid,
        "project_name": project_row["canonical_name"] if project_row else "unknown",
        "model": session_row["model"] if session_row["model"] else "unknown",
        "started_at": session_row["started_at"] if session_row["started_at"] else "unknown",
        "total_duration_ms": session_row["total_duration_ms"] or 0,
        "turn_count": session_row["turn_count"] or 0,
        "message_count": session_row["message_count"] or 0,
    }

    # Build prompt
    prompt = build_summary_prompt(metadata, content)

    # Call claude CLI with retry logic
    summary_json = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model_name, "--no-session-persistence", prompt],
                capture_output=True, text=True, timeout=120, env=_claude_env(),
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"claude CLI exited with code {result.returncode}: "
                    f"{result.stderr.strip()[:300]}"
                )

            summary_text = result.stdout.strip()

            # Strip markdown code fences if present
            if summary_text.startswith("```"):
                lines = summary_text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                summary_text = "\n".join(lines).strip()

            # Parse JSON response
            summary_json = json.loads(summary_text)
            break

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response for {session_uuid}: {e}")
            logger.debug(f"Raw response: {summary_text[:500]}")
            return False

        except (RuntimeError, subprocess.TimeoutExpired) as e:
            if attempt < RETRY_COUNT:
                wait_time = RETRY_BACKOFF_BASE ** (attempt - 1)
                logger.warning(
                    f"CLI error on attempt {attempt}/{RETRY_COUNT} for {session_uuid}: {e}. "
                    f"Retrying in {wait_time}s..."
                )
                time.sleep(wait_time)
            else:
                logger.error(
                    f"CLI error on final attempt for {session_uuid}: {e}"
                )
                return False

    if not summary_json:
        return False

    # Rate limiting
    time.sleep(API_DELAY_SECONDS)

    # Store in database
    summary_json_str = json.dumps(summary_json)
    summary_text_para = _build_summary_paragraph(summary_json)

    try:
        # Keep summarization idempotent when re-running.
        kb_conn.execute(
            """DELETE FROM kb_fts
               WHERE session_uuid = ? AND source_type IN ('summary', 'deep_archive')""",
            (session_uuid,),
        )
        kb_conn.execute(
            "DELETE FROM kb_deep_archives WHERE session_id = ?",
            (session_id,),
        )

        kb_conn.execute(
            """UPDATE kb_sessions
               SET summary_json = ?, summary_text = ?,
                   phase = ?, summary_version = 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                summary_json_str,
                summary_text_para,
                summary_json.get("phase"),
                session_id,
            ),
        )

        # Also insert into FTS for searchability
        kb_conn.execute(
            """INSERT INTO kb_fts (text, session_uuid, source_type, project_name)
               VALUES (?, ?, 'summary', ?)""",
            (
                summary_text_para,
                session_uuid,
                metadata["project_name"],
            ),
        )

        logger.info(f"Successfully summarized {session_uuid}")
        return True

    except sqlite3.Error as e:
        logger.error(f"Database error storing summary for {session_uuid}: {e}")
        return False


def _summarize_deep_archive(
    kb_conn: sqlite3.Connection,
    session_row: sqlite3.Row,
    jsonl_path: Path,
    file_size: int,
) -> bool:
    """
    Special handling for mega-sessions (>100 MB).

    Instead of a standard summary, generate a structured analysis of what data
    was processed, what was discovered, and what approaches were used.
    """
    session_id = session_row["id"]
    session_uuid = session_row["session_uuid"]
    project_id = session_row["project_id"]

    logger.info(f"Processing deep archive: {session_uuid} ({file_size / 1024 / 1024:.1f} MB)")

    # Extract minimal content (just enough for analysis)
    content = extract_content(jsonl_path, file_size)
    if not content:
        logger.warning(f"No content extracted from deep archive {session_uuid}")
        return False

    # Get project info
    project_row = kb_conn.execute(
        "SELECT summarization_tier, canonical_name FROM kb_projects WHERE id = ?",
        (project_id,),
    ).fetchone()

    tier = "opus"  # Always use opus for deep archives
    model_name = SUMMARIZATION_MODELS["opus"]

    metadata = {
        "session_uuid": session_uuid,
        "project_name": project_row["canonical_name"] if project_row else "unknown",
        "file_size_mb": file_size / 1024 / 1024,
        "turn_count": session_row["turn_count"] or 0,
        "message_count": session_row["message_count"] or 0,
    }

    # Build prompt for deep archive analysis
    prompt = f"""This is a DEEP ARCHIVE session — a very large (100+ MB) Claude Code conversation
that likely involved significant data processing or analysis.

FILE SIZE: {metadata['file_size_mb']:.1f} MB
TURNS: {metadata['turn_count']}
SESSION: {session_uuid}

Sample of conversation (first 30 messages):
{json.dumps([m for m in content[:30]], indent=2)[:2000]}...

Generate a JSON analysis with this structure:
{{
  "objective": "What data or problem was being processed?",
  "data_processed": "Description of inputs/datasets",
  "discoveries": ["key findings", "results", "insights"],
  "approaches_used": ["tools/techniques employed"],
  "processing_phase": "research|analysis|transformation|enrichment|export",
  "key_findings": "Summary of important discoveries",
  "processing_notes": "Technical details or limitations"
}}

Return ONLY valid JSON."""

    # Call claude CLI for deep archive analysis
    summary_json = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", model_name, "--no-session-persistence", prompt],
                capture_output=True, text=True, timeout=180, env=_claude_env(),
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"claude CLI exited with code {result.returncode}: "
                    f"{result.stderr.strip()[:300]}"
                )

            summary_text = result.stdout.strip()

            # Strip markdown code fences if present
            if summary_text.startswith("```"):
                lines = summary_text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                summary_text = "\n".join(lines).strip()

            summary_json = json.loads(summary_text)
            break

        except (json.JSONDecodeError, RuntimeError, subprocess.TimeoutExpired) as e:
            if attempt < RETRY_COUNT:
                wait_time = RETRY_BACKOFF_BASE ** (attempt - 1)
                logger.warning(
                    f"Error on attempt {attempt}/{RETRY_COUNT} for deep archive {session_uuid}: {e}. "
                    f"Retrying in {wait_time}s..."
                )
                time.sleep(wait_time)
            else:
                logger.error(f"Failed to analyze deep archive {session_uuid}: {e}")
                return False

    if not summary_json:
        return False

    time.sleep(API_DELAY_SECONDS)

    # Store as a special "deep_archive" entry
    summary_json_str = json.dumps(summary_json)
    analysis_text = summary_json.get("key_findings", "")

    try:
        # Keep deep-archive analysis idempotent when re-running.
        kb_conn.execute(
            """DELETE FROM kb_fts
               WHERE session_uuid = ? AND source_type IN ('summary', 'deep_archive')""",
            (session_uuid,),
        )
        kb_conn.execute(
            "DELETE FROM kb_deep_archives WHERE session_id = ?",
            (session_id,),
        )

        # Store main summary
        kb_conn.execute(
            """UPDATE kb_sessions
               SET summary_json = ?, summary_text = ?,
                   phase = ?, summary_version = 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                summary_json_str,
                analysis_text,
                "data-processing",
                session_id,
            ),
        )

        # Also create deep archive record
        kb_conn.execute(
            """INSERT INTO kb_deep_archives
               (session_id, archive_type, analysis_json, processing_notes)
               VALUES (?, 'session-analysis', ?, ?)""",
            (
                session_id,
                summary_json_str,
                summary_json.get("processing_notes", ""),
            ),
        )

        # Add to FTS
        kb_conn.execute(
            """INSERT INTO kb_fts (text, session_uuid, source_type, project_name)
               VALUES (?, ?, 'deep_archive', ?)""",
            (
                analysis_text,
                session_uuid,
                metadata["project_name"],
            ),
        )

        logger.info(f"Successfully analyzed deep archive {session_uuid}")
        return True

    except sqlite3.Error as e:
        logger.error(f"Database error storing deep archive {session_uuid}: {e}")
        return False


def _build_summary_paragraph(summary_json: Dict[str, Any]) -> str:
    """Convert JSON summary to a readable paragraph."""
    objective = summary_json.get("objective", "")
    actions = summary_json.get("actions_taken", [])
    outcome = summary_json.get("outcome", "")
    blockers = summary_json.get("blockers")
    next_steps = summary_json.get("next_steps")

    parts = []
    if objective:
        parts.append(objective)
    if outcome:
        parts.append(outcome)
    if actions:
        if isinstance(actions, list):
            parts.append("Actions: " + "; ".join(actions))
        else:
            parts.append("Actions: " + str(actions))
    if blockers:
        parts.append("Blockers: " + str(blockers))
    if next_steps:
        parts.append("Next: " + str(next_steps))

    return " ".join(parts)


def _find_jsonl_for_session(
    session_uuid: str, session_row: sqlite3.Row
) -> Optional[Path]:
    """
    Locate the JSONL file for a session.

    Session's jsonl_path field may not be populated yet, so search across all
    project directories matching the session_uuid filename stem.
    """
    from ._paths import CLAUDE_PROJECTS as claude_projects

    if not claude_projects.exists():
        logger.warning(f"Claude projects directory not found: {claude_projects}")
        return None

    # Prefer stored path if available.
    stored_path = session_row["jsonl_path"] if "jsonl_path" in session_row.keys() else None
    if stored_path:
        candidate = Path(stored_path)
        if not candidate.is_absolute():
            candidate = claude_projects / candidate
        if candidate.exists():
            return candidate

    # Search for matching JSONL file (top-level and subagents/)
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue

        # Check top-level
        jsonl_file = project_dir / f"{session_uuid}.jsonl"
        if jsonl_file.exists():
            return jsonl_file

        # Modern subagent layout: <project>/<parent>/subagents/<child>.jsonl
        nested = list(project_dir.glob(f"*/subagents/{session_uuid}.jsonl"))
        if nested:
            return nested[0]

        # Legacy subagent layout: <project>/subagents/<child>.jsonl
        legacy = project_dir / "subagents" / f"{session_uuid}.jsonl"
        if legacy.exists():
            return legacy

    # Final fallback if layout changes again.
    matches = list(claude_projects.rglob(f"{session_uuid}.jsonl"))
    if matches:
        return matches[0]

    logger.debug(
        f"JSONL file not found for session {session_uuid} across all project dirs"
    )
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# BATCH PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════


def run_summarization(batch_size: int = BATCH_SIZE, resume: bool = True) -> Dict[str, Any]:
    """
    Process all sessions that need summarization.

    Args:
        batch_size: Number of sessions per batch (commit after each)
        resume: If True, skip sessions already summarized (summary_version > 0)

    Returns:
        Stats dict: {processed, succeeded, failed, skipped}
    """
    kb_conn = get_kb_db()

    stats = {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "start_time": datetime.now().isoformat(),
    }

    try:
        # Update progress
        kb_conn.execute(
            """INSERT OR REPLACE INTO kb_progress
               (stage, status, started_at) VALUES ('summarization', 'running', CURRENT_TIMESTAMP)"""
        )
        kb_conn.commit()

        # Get sessions needing summarization
        if resume:
            query = "SELECT * FROM kb_sessions WHERE summary_version = 0 ORDER BY started_at"
            sessions = kb_conn.execute(query).fetchall()
        else:
            query = "SELECT * FROM kb_sessions ORDER BY started_at"
            sessions = kb_conn.execute(query).fetchall()

        total = len(sessions)
        logger.info(f"Found {total} sessions needing summarization")

        if total == 0:
            logger.info("All sessions already summarized!")
            stats["skipped"] = 0
            kb_conn.execute(
                """INSERT OR REPLACE INTO kb_progress
                   (stage, status, processed, errors, completed_at, notes)
                   VALUES ('summarization', 'completed', 0, 0, CURRENT_TIMESTAMP, 'nothing to summarize')"""
            )
            kb_conn.commit()
            return stats

        # Process in batches
        for batch_idx in range(0, total, batch_size):
            batch = sessions[batch_idx : batch_idx + batch_size]
            logger.info(
                f"Processing batch {batch_idx // batch_size + 1}/{(total + batch_size - 1) // batch_size} "
                f"({len(batch)} sessions)"
            )

            for session_row in batch:
                try:
                    success = summarize_session(kb_conn, session_row)
                    if success:
                        stats["succeeded"] += 1
                    else:
                        stats["failed"] += 1
                    stats["processed"] += 1

                except Exception as e:
                    logger.error(
                        f"Unexpected error summarizing session {session_row['session_uuid']}: {e}"
                    )
                    stats["failed"] += 1
                    stats["processed"] += 1

            # Commit batch
            kb_conn.commit()
            logger.info(
                f"Batch committed: {stats['succeeded']} succeeded, {stats['failed']} failed"
            )

        # Final update to progress
        kb_conn.execute(
            """INSERT OR REPLACE INTO kb_progress
               (stage, status, processed, errors, completed_at)
               VALUES ('summarization', 'completed', ?, ?, CURRENT_TIMESTAMP)""",
            (stats["succeeded"], stats["failed"]),
        )
        kb_conn.commit()

        logger.info(
            f"Summarization complete: {stats['succeeded']} succeeded, {stats['failed']} failed "
            f"out of {stats['processed']} processed"
        )

    except Exception as e:
        logger.error(f"Fatal error during summarization: {e}")
        kb_conn.execute(
            """INSERT OR REPLACE INTO kb_progress
               (stage, status, errors) VALUES ('summarization', 'failed', ?)""",
            (stats["failed"] + 1,),
        )
        kb_conn.commit()

    finally:
        stats["end_time"] = datetime.now().isoformat()
        kb_conn.close()

    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Summarize Claude Code sessions using the Anthropic API"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Sessions per batch (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without making changes",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Skip already summarized sessions (default: True)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Re-summarize all sessions",
    )

    args = parser.parse_args()

    if args.dry_run:
        logger.info("DRY RUN MODE — no changes will be made")
        kb_conn = get_kb_db(readonly=True)
        if args.resume:
            count = kb_conn.execute(
                "SELECT COUNT(*) FROM kb_sessions WHERE summary_version = 0"
            ).fetchone()[0]
        else:
            count = kb_conn.execute("SELECT COUNT(*) FROM kb_sessions").fetchone()[0]
        logger.info(f"Would process {count} sessions (batch size: {args.batch_size})")
        kb_conn.close()
        return

    logger.info("Starting summarization pipeline (using claude CLI)...")
    stats = run_summarization(batch_size=args.batch_size, resume=args.resume)

    logger.info("=" * 70)
    logger.info("SUMMARIZATION RESULTS")
    logger.info("=" * 70)
    logger.info(f"Processed:  {stats['processed']}")
    logger.info(f"Succeeded:  {stats['succeeded']}")
    logger.info(f"Failed:     {stats['failed']}")
    logger.info(f"Started:    {stats['start_time']}")
    logger.info(f"Completed:  {stats['end_time']}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
