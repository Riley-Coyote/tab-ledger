"""Tab Ledger — FastAPI server with dashboard routes and API endpoints."""

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .snapshot import init_db, take_snapshot, LEDGER_DB
from .cc_indexer import index_all
from .categorizer import get_category_colors
from .kb_query import KnowledgeBase

app = FastAPI(title="Tab Ledger")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Ensure DB exists on startup
init_db()


def get_db():
    conn = sqlite3.connect(LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Dashboard ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "category_colors": get_category_colors(),
    })


# ─── API: Snapshots ───────────────────────────────────────

@app.post("/api/snapshot")
async def api_take_snapshot(request: Request):
    """Take a manual snapshot."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    note = body.get("note", None)
    result = await run_in_threadpool(take_snapshot, "manual", note)
    return result


@app.get("/api/snapshots")
async def api_list_snapshots(limit: int = 50):
    """List recent snapshots."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM snapshots ORDER BY taken_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/snapshot/{snapshot_id}")
async def api_get_snapshot(snapshot_id: int):
    """Get tabs for a specific snapshot."""
    conn = get_db()
    snapshot = conn.execute(
        "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if not snapshot:
        raise HTTPException(404, "Snapshot not found")

    tabs = conn.execute(
        "SELECT * FROM tabs WHERE snapshot_id = ? ORDER BY category, domain, url",
        (snapshot_id,)
    ).fetchall()
    conn.close()

    return {
        "snapshot": dict(snapshot),
        "tabs": [dict(t) for t in tabs],
    }


# ─── API: Right Now ───────────────────────────────────────

@app.get("/api/right-now")
async def api_right_now():
    """Get the most recent snapshot (or take one if none recent)."""
    conn = get_db()

    # Get latest snapshot
    latest = conn.execute(
        "SELECT * FROM snapshots ORDER BY taken_at DESC LIMIT 1"
    ).fetchone()

    if not latest:
        conn.close()
        # Take first snapshot
        await run_in_threadpool(take_snapshot, "auto")
        conn = get_db()
        latest = conn.execute(
            "SELECT * FROM snapshots ORDER BY taken_at DESC LIMIT 1"
        ).fetchone()

    tabs = conn.execute(
        "SELECT * FROM tabs WHERE snapshot_id = ? ORDER BY category, domain, url",
        (latest["id"],)
    ).fetchall()

    # Also get recent CC sessions (today)
    today = date.today().isoformat()
    cc_sessions = conn.execute(
        """SELECT * FROM cc_sessions
           WHERE date(started_at) >= ? OR date(ended_at) >= ?
           ORDER BY started_at DESC""",
        (today, today)
    ).fetchall()

    conn.close()

    # Group tabs by category
    grouped = {}
    for tab in tabs:
        cat = tab["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(dict(tab))

    stale_tabs = [dict(t) for t in tabs if t["is_stale"]]

    # Detect source from snapshot note
    snap_note = latest["note"] or ""
    tab_source = "cdp" if "[cdp]" in snap_note else "session_files"

    return {
        "snapshot": dict(latest),
        "tabs_by_category": grouped,
        "stale_tabs": stale_tabs,
        "total_tabs": len(tabs),
        "stale_count": len(stale_tabs),
        "cc_sessions": [dict(s) for s in cc_sessions],
        "category_count": len(grouped),
        "tab_source": tab_source,
    }


# ─── API: Today ───────────────────────────────────────────

@app.get("/api/today")
async def api_today():
    """Get today's activity summary."""
    conn = get_db()
    today = date.today().isoformat()

    # Today's snapshots
    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE date(taken_at) >= ? ORDER BY taken_at",
        (today,)
    ).fetchall()

    # Get all tabs from today's snapshots
    all_tabs = []
    for snap in snapshots:
        tabs = conn.execute(
            "SELECT * FROM tabs WHERE snapshot_id = ?", (snap["id"],)
        ).fetchall()
        all_tabs.extend([dict(t) for t in tabs])

    # Today's CC sessions
    cc_sessions = conn.execute(
        """SELECT * FROM cc_sessions
           WHERE date(started_at) >= ? OR date(ended_at) >= ?
           ORDER BY started_at DESC""",
        (today, today)
    ).fetchall()

    conn.close()

    # Category summary
    cat_counts = Counter(t["category"] for t in all_tabs)
    peak_tabs = max((s["tab_count"] for s in snapshots), default=0)

    # Build digest
    total_msgs = sum(s["message_count"] or 0 for s in cc_sessions)
    top_categories = cat_counts.most_common(5)

    return {
        "date": today,
        "snapshots": [dict(s) for s in snapshots],
        "snapshot_count": len(snapshots),
        "peak_tabs": peak_tabs,
        "cc_sessions": [dict(s) for s in cc_sessions],
        "session_count": len(cc_sessions),
        "total_messages": total_msgs,
        "top_categories": [{"category": c, "count": n} for c, n in top_categories],
        "unique_domains": len(set(t["domain"] for t in all_tabs)),
    }


# ─── API: Search ──────────────────────────────────────────

@app.get("/api/search")
async def api_search(q: str = "", category: str = None, date_from: str = None, date_to: str = None):
    """Search across all tabs and CC sessions. All params optional."""
    conn = get_db()
    results = {"tabs": [], "cc_sessions": []}

    # Search tabs
    conditions = []
    params = []

    if q:
        conditions.append("(t.url LIKE ? OR t.title LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if category:
        conditions.append("t.category = ?")
        params.append(category)
    if date_from:
        conditions.append("date(s.taken_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date(s.taken_at) <= ?")
        params.append(date_to)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    query = f"""
        SELECT t.*, s.taken_at FROM tabs t
        JOIN snapshots s ON t.snapshot_id = s.id
        WHERE {where_clause}
        ORDER BY s.taken_at DESC LIMIT 200
    """
    tabs = conn.execute(query, params).fetchall()

    # Deduplicate by URL, keep most recent
    seen_urls = set()
    for t in tabs:
        if t["url"] not in seen_urls:
            seen_urls.add(t["url"])
            results["tabs"].append(dict(t))

    # Search CC sessions
    cc_conditions = []
    cc_params = []

    if q:
        cc_conditions.append("(summary LIKE ? OR first_prompt LIKE ? OR project_name LIKE ?)")
        cc_params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if category:
        cc_conditions.append("category = ?")
        cc_params.append(category)
    if date_from:
        cc_conditions.append("date(started_at) >= ?")
        cc_params.append(date_from)
    if date_to:
        cc_conditions.append("date(started_at) <= ?")
        cc_params.append(date_to)

    cc_where = " AND ".join(cc_conditions) if cc_conditions else "1=1"
    cc_query = f"""
        SELECT * FROM cc_sessions
        WHERE {cc_where}
        ORDER BY started_at DESC LIMIT 100
    """
    cc_sessions = conn.execute(cc_query, cc_params).fetchall()
    results["cc_sessions"] = [dict(s) for s in cc_sessions]

    conn.close()
    return results


# ─── API: History ─────────────────────────────────────────

@app.get("/api/history")
async def api_history(days: int = 7):
    """Get snapshot history for the last N days."""
    conn = get_db()
    since = (date.today() - timedelta(days=days)).isoformat()

    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE date(taken_at) >= ? ORDER BY taken_at DESC",
        (since,)
    ).fetchall()

    # Daily summaries
    daily = {}
    for snap in snapshots:
        day = snap["taken_at"][:10]
        if day not in daily:
            daily[day] = {"snapshots": 0, "peak_tabs": 0}
        daily[day]["snapshots"] += 1
        daily[day]["peak_tabs"] = max(daily[day]["peak_tabs"], snap["tab_count"])

    conn.close()

    return {
        "since": since,
        "snapshots": [dict(s) for s in snapshots],
        "daily_summary": daily,
    }


# ─── API: Parking Lot ────────────────────────────────────

@app.post("/api/park")
async def api_park_tabs(request: Request):
    """Park a group of tabs."""
    body = await request.json()
    name = body.get("name", "Unnamed group")
    note = body.get("note", "")
    tab_ids = body.get("tab_ids", [])
    urls = body.get("urls", [])

    conn = get_db()

    cursor = conn.execute(
        "INSERT INTO parked_groups (name, note) VALUES (?, ?)",
        (name, note)
    )
    group_id = cursor.lastrowid

    # Park by tab IDs (from current snapshot)
    if tab_ids:
        for tid in tab_ids:
            tab = conn.execute("SELECT * FROM tabs WHERE id = ?", (tid,)).fetchone()
            if tab:
                conn.execute(
                    "INSERT INTO parked_tabs (group_id, url, title, domain, category) VALUES (?, ?, ?, ?, ?)",
                    (group_id, tab["url"], tab["title"], tab["domain"], tab["category"])
                )

    # Park by URLs directly
    if urls:
        for u in urls:
            url = u if isinstance(u, str) else u.get("url", "")
            title = "" if isinstance(u, str) else u.get("title", "")
            domain = "" if isinstance(u, str) else u.get("domain", "")
            category = "" if isinstance(u, str) else u.get("category", "")
            conn.execute(
                "INSERT INTO parked_tabs (group_id, url, title, domain, category) VALUES (?, ?, ?, ?, ?)",
                (group_id, url, title, domain, category)
            )

    conn.commit()
    conn.close()

    return {"group_id": group_id, "name": name, "tab_count": len(tab_ids) + len(urls)}


@app.get("/api/parked")
async def api_list_parked():
    """List all parked tab groups."""
    conn = get_db()
    groups = conn.execute(
        "SELECT * FROM parked_groups WHERE reopened_at IS NULL ORDER BY created_at DESC"
    ).fetchall()

    result = []
    for g in groups:
        tabs = conn.execute(
            "SELECT * FROM parked_tabs WHERE group_id = ?", (g["id"],)
        ).fetchall()
        result.append({
            **dict(g),
            "tabs": [dict(t) for t in tabs],
            "tab_count": len(tabs),
        })

    conn.close()
    return result


@app.post("/api/parked/{group_id}/reopen")
async def api_reopen_parked(group_id: int):
    """Mark a parked group as reopened and return its URLs."""
    conn = get_db()
    group = conn.execute("SELECT * FROM parked_groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        raise HTTPException(404, "Group not found")

    tabs = conn.execute("SELECT * FROM parked_tabs WHERE group_id = ?", (group_id,)).fetchall()
    conn.execute(
        "UPDATE parked_groups SET reopened_at = CURRENT_TIMESTAMP WHERE id = ?", (group_id,)
    )
    conn.commit()
    conn.close()

    return {
        "group": dict(group),
        "tabs": [dict(t) for t in tabs],
        "urls": [t["url"] for t in tabs],
    }


@app.delete("/api/parked/{group_id}")
async def api_delete_parked(group_id: int):
    """Delete a parked group."""
    conn = get_db()
    conn.execute("DELETE FROM parked_tabs WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM parked_groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()
    return {"deleted": group_id}


# ─── API: Digest ──────────────────────────────────────────

@app.get("/api/digest/{date_str}")
async def api_digest(date_str: str):
    """Get or generate a daily digest."""
    conn = get_db()

    # Check if digest already exists
    existing = conn.execute(
        "SELECT * FROM daily_digests WHERE date = ?", (date_str,)
    ).fetchone()
    if existing:
        conn.close()
        return dict(existing)

    # Generate digest
    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE date(taken_at) = ?", (date_str,)
    ).fetchall()

    all_tabs = []
    for snap in snapshots:
        tabs = conn.execute(
            "SELECT * FROM tabs WHERE snapshot_id = ?", (snap["id"],)
        ).fetchall()
        all_tabs.extend(tabs)

    cc_sessions = conn.execute(
        "SELECT * FROM cc_sessions WHERE date(started_at) = ? OR date(ended_at) = ?",
        (date_str, date_str)
    ).fetchall()

    if not snapshots and not cc_sessions:
        conn.close()
        return {"date": date_str, "digest_markdown": "No activity recorded for this date."}

    # Build markdown
    cat_counts = Counter(t["category"] for t in all_tabs)
    peak_tabs = max((s["tab_count"] for s in snapshots), default=0)
    total_msgs = sum(s["message_count"] or 0 for s in cc_sessions)
    stale = sum(1 for t in all_tabs if t["is_stale"])

    top_cats = cat_counts.most_common(3)
    main_focus = top_cats[0][0] if top_cats else "Unknown"
    also_active = ", ".join(c for c, _ in top_cats[1:3]) if len(top_cats) > 1 else "nothing else"

    models = Counter(s["model"] for s in cc_sessions if s["model"])
    model_str = ", ".join(f"{m}" for m, _ in models.most_common(3)) if models else "unknown"

    md = f"""## {date_str} — Daily Digest
- **Main focus:** {main_focus} ({cat_counts.get(main_focus, 0)} tabs)
- **Also active:** {also_active}
- **Claude sessions:** {len(cc_sessions)} total, {total_msgs} messages, using {model_str}
- **Tabs at peak:** {peak_tabs}
- **Stale tabs detected:** {stale}
- **Snapshots taken:** {len(snapshots)}
"""

    main_cats = json.dumps([c for c, _ in top_cats])

    conn.execute(
        "INSERT OR REPLACE INTO daily_digests (date, digest_markdown, tab_peak, session_count, main_categories) VALUES (?, ?, ?, ?, ?)",
        (date_str, md, peak_tabs, len(cc_sessions), main_cats)
    )
    conn.commit()
    conn.close()

    return {
        "date": date_str,
        "digest_markdown": md,
        "tab_peak": peak_tabs,
        "session_count": len(cc_sessions),
        "main_categories": main_cats,
    }


# ─── API: Categories ─────────────────────────────────────

@app.get("/api/categories")
async def api_categories():
    """Get category summary from latest snapshot."""
    return get_category_colors()


# ─── API: Re-index ────────────────────────────────────────

@app.post("/api/reindex")
async def api_reindex(force: bool = False):
    """Re-index Claude Code sessions."""
    result = await run_in_threadpool(index_all, force)
    return result


# ─── API: CC Stats ───────────────────────────────────────

@app.get("/api/cc/stats")
async def api_cc_stats():
    """Aggregate stats across all indexed CC sessions."""
    conn = get_db()

    # Overall totals
    totals = conn.execute("""
        SELECT
            COUNT(*) as total_sessions,
            SUM(message_count) as total_messages,
            SUM(turn_count) as total_turns,
            SUM(input_tokens) as total_input_tokens,
            SUM(output_tokens) as total_output_tokens,
            SUM(cache_creation_tokens) as total_cache_write,
            SUM(cache_read_tokens) as total_cache_read,
            ROUND(SUM(cost_usd), 2) as total_cost_usd,
            SUM(tool_call_count) as total_tool_calls,
            SUM(CASE WHEN is_sidechain THEN 1 ELSE 0 END) as sidechain_count,
            MIN(started_at) as earliest_session,
            MAX(ended_at) as latest_session,
            SUM(total_duration_ms) as total_duration_ms
        FROM cc_sessions
    """).fetchone()

    # Per-model breakdown
    models = conn.execute("""
        SELECT
            model,
            COUNT(*) as sessions,
            SUM(input_tokens) as input_tokens,
            SUM(output_tokens) as output_tokens,
            SUM(cache_creation_tokens) as cache_write,
            SUM(cache_read_tokens) as cache_read,
            ROUND(SUM(cost_usd), 2) as cost_usd,
            SUM(turn_count) as turns,
            SUM(tool_call_count) as tool_calls
        FROM cc_sessions
        WHERE model != ''
        GROUP BY model
        ORDER BY cost_usd DESC
    """).fetchall()

    # Per-project breakdown
    projects = conn.execute("""
        SELECT
            project_name,
            COUNT(*) as sessions,
            SUM(message_count) as messages,
            SUM(turn_count) as turns,
            ROUND(SUM(cost_usd), 2) as cost_usd,
            SUM(tool_call_count) as tool_calls
        FROM cc_sessions
        GROUP BY project_name
        ORDER BY sessions DESC
    """).fetchall()

    # Tool usage rankings (aggregate across all sessions)
    tool_rows = conn.execute("SELECT tools_used FROM cc_sessions WHERE tools_used != ''").fetchall()
    tool_counts = Counter()
    for row in tool_rows:
        for tool in row["tools_used"].split(","):
            if tool:
                tool_counts[tool] += 1

    # Category breakdown
    categories = conn.execute("""
        SELECT category, COUNT(*) as sessions,
               ROUND(SUM(cost_usd), 2) as cost_usd,
               SUM(tool_call_count) as tool_calls
        FROM cc_sessions
        GROUP BY category
        ORDER BY sessions DESC
    """).fetchall()

    conn.close()

    return {
        "totals": dict(totals),
        "models": [dict(m) for m in models],
        "projects": [dict(p) for p in projects],
        "tools": [{"tool": t, "session_count": c} for t, c in tool_counts.most_common(30)],
        "categories": [dict(c) for c in categories],
    }


@app.get("/api/cc/session/{session_id}")
async def api_cc_session(session_id: str):
    """Get full detail for a single CC session."""
    conn = get_db()
    session = conn.execute(
        "SELECT * FROM cc_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()

    if not session:
        raise HTTPException(404, "Session not found")

    result = dict(session)
    # Parse tools_used into a list
    result["tools_list"] = [t for t in (result.get("tools_used") or "").split(",") if t]
    return result


@app.get("/api/cc/timeline")
async def api_cc_timeline(days: int = 30):
    """Daily activity timeline with tokens and cost."""
    conn = get_db()
    since = (date.today() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT
            date(started_at) as day,
            COUNT(*) as sessions,
            SUM(message_count) as messages,
            SUM(turn_count) as turns,
            SUM(input_tokens) as input_tokens,
            SUM(output_tokens) as output_tokens,
            SUM(cache_creation_tokens) as cache_write,
            SUM(cache_read_tokens) as cache_read,
            ROUND(SUM(cost_usd), 2) as cost_usd,
            SUM(tool_call_count) as tool_calls,
            SUM(total_duration_ms) as duration_ms
        FROM cc_sessions
        WHERE date(started_at) >= ?
        GROUP BY date(started_at)
        ORDER BY day
    """, (since,)).fetchall()

    conn.close()
    return {"since": since, "days": [dict(r) for r in rows]}


@app.get("/api/cc/tools")
async def api_cc_tools():
    """Tool usage analytics across all sessions."""
    conn = get_db()

    # Get tools_used from all sessions
    rows = conn.execute("""
        SELECT tools_used, tool_call_count, model, project_name, cost_usd
        FROM cc_sessions WHERE tools_used != ''
    """).fetchall()

    # Aggregate tool → session count and co-occurrence
    tool_sessions = Counter()
    tool_by_model = {}
    tool_by_project = {}

    for row in rows:
        tools = [t for t in row["tools_used"].split(",") if t]
        model = row["model"]
        project = row["project_name"]

        for tool in tools:
            tool_sessions[tool] += 1

            if model:
                if tool not in tool_by_model:
                    tool_by_model[tool] = Counter()
                tool_by_model[tool][model] += 1

            if tool not in tool_by_project:
                tool_by_project[tool] = Counter()
            tool_by_project[tool][project] += 1

    # Build ranked tool list
    tool_list = []
    for tool, count in tool_sessions.most_common():
        entry = {"tool": tool, "session_count": count}
        if tool in tool_by_model:
            entry["top_models"] = [{"model": m, "count": c}
                                   for m, c in tool_by_model[tool].most_common(3)]
        if tool in tool_by_project:
            entry["top_projects"] = [{"project": p, "count": c}
                                     for p, c in tool_by_project[tool].most_common(3)]
        tool_list.append(entry)

    conn.close()
    return {"tools": tool_list, "total_tools_tracked": len(tool_list)}


@app.get("/api/cc/models")
async def api_cc_models():
    """Model usage breakdown with daily trends."""
    conn = get_db()

    models = conn.execute("""
        SELECT
            model,
            COUNT(*) as sessions,
            SUM(input_tokens) as input_tokens,
            SUM(output_tokens) as output_tokens,
            SUM(cache_creation_tokens) as cache_write,
            SUM(cache_read_tokens) as cache_read,
            ROUND(SUM(cost_usd), 2) as cost_usd,
            MIN(started_at) as first_used,
            MAX(ended_at) as last_used
        FROM cc_sessions
        WHERE model != ''
        GROUP BY model
        ORDER BY cost_usd DESC
    """).fetchall()

    # Daily model usage
    daily = conn.execute("""
        SELECT
            date(started_at) as day,
            model,
            COUNT(*) as sessions,
            ROUND(SUM(cost_usd), 2) as cost_usd
        FROM cc_sessions
        WHERE model != '' AND started_at IS NOT NULL
        GROUP BY date(started_at), model
        ORDER BY day
    """).fetchall()

    conn.close()
    return {
        "models": [dict(m) for m in models],
        "daily": [dict(d) for d in daily],
    }


# ─── API: Semantic Memory ───────────────────────────────

@app.get("/api/kb/semantic")
async def api_kb_semantic(
    q: str,
    project: str = None,
    source_type: str = None,
    limit: int = 20,
    provider: str = None,
    model: str = None,
    min_score: float = 0.18,
):
    """Semantic search over KB embeddings."""

    def _run():
        with KnowledgeBase(readonly=True) as kb:
            return kb.semantic_search(
                query=q,
                project=project,
                source_type=source_type,
                limit=limit,
                provider=provider,
                model=model,
                min_score=min_score,
            )

    results = await run_in_threadpool(_run)
    return {
        "query": q,
        "project": project,
        "source_type": source_type,
        "count": len(results),
        "results": results,
    }


@app.get("/api/kb/memory/{project}")
async def api_kb_memory(
    project: str,
    semantic_query: str = None,
    semantic_limit: int = 10,
    provider: str = None,
    model: str = None,
):
    """High-signal continuity packet for project resumption."""

    def _run():
        with KnowledgeBase(readonly=True) as kb:
            return kb.get_memory_packet(
                project=project,
                semantic_query=semantic_query,
                semantic_limit=semantic_limit,
                provider=provider,
                model=model,
            )

    result = await run_in_threadpool(_run)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


# ─── Startup ──────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    print("Tab Ledger is running. Dashboard at http://localhost:7777")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("tab_ledger.server:app", host="127.0.0.1", port=7777, reload=True)
