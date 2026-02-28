"""Tab Snapshot Engine — captures current browser tabs from Comet."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from categorizer import categorize_url, check_stale, get_domain

COMET_HISTORY = Path.home() / "Library/Application Support/Comet/Default/History"
COMET_SESSIONS = Path.home() / "Library/Application Support/Comet/Default/Sessions"
LEDGER_DB = Path.home() / ".tab-ledger/ledger.db"

# Chromium epoch: Jan 1, 1601 — microseconds
CHROMIUM_EPOCH_DELTA = 11644473600 * 1_000_000

# CDP ports to try
CDP_PORTS = [9222, 9223, 9224]


def chromium_to_unix(chromium_ts: int) -> float:
    """Convert Chromium timestamp to Unix timestamp."""
    return (chromium_ts - CHROMIUM_EPOCH_DELTA) / 1_000_000


def init_db():
    """Create database tables if they don't exist."""
    conn = sqlite3.connect(LEDGER_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tab_count INTEGER,
            source TEXT DEFAULT 'auto',
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS tabs (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER REFERENCES snapshots(id),
            url TEXT NOT NULL,
            title TEXT,
            domain TEXT,
            category TEXT,
            category_color TEXT,
            is_stale BOOLEAN DEFAULT FALSE,
            stale_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS cc_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT UNIQUE,
            project_path TEXT,
            project_name TEXT,
            git_branch TEXT,
            summary TEXT,
            first_prompt TEXT,
            category TEXT,
            message_count INTEGER,
            model TEXT,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            slug TEXT,
            is_sidechain BOOLEAN DEFAULT FALSE,
            turn_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            total_duration_ms INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            tools_used TEXT DEFAULT '',
            tool_call_count INTEGER DEFAULT 0,
            claude_code_version TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS parked_groups (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reopened_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS parked_tabs (
            id INTEGER PRIMARY KEY,
            group_id INTEGER REFERENCES parked_groups(id),
            url TEXT NOT NULL,
            title TEXT,
            domain TEXT,
            category TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_digests (
            id INTEGER PRIMARY KEY,
            date DATE UNIQUE,
            digest_markdown TEXT,
            tab_peak INTEGER,
            session_count INTEGER,
            main_categories TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tabs_snapshot ON tabs(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_tabs_category ON tabs(category);
        CREATE INDEX IF NOT EXISTS idx_tabs_domain ON tabs(domain);
        CREATE INDEX IF NOT EXISTS idx_tabs_url ON tabs(url);
        CREATE INDEX IF NOT EXISTS idx_cc_sessions_project ON cc_sessions(project_name);
        CREATE INDEX IF NOT EXISTS idx_cc_sessions_category ON cc_sessions(category);
        CREATE INDEX IF NOT EXISTS idx_cc_sessions_date ON cc_sessions(started_at);
    """)
    conn.close()


def get_tabs_via_cdp() -> list[dict] | None:
    """Try to get exact tab list via Chrome DevTools Protocol."""
    # Also check if Comet exposes a debug port via process args
    ports_to_try = list(CDP_PORTS)
    try:
        result = subprocess.run(
            ["pgrep", "-a", "Comet"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "--remote-debugging-port=" in line:
                port = line.split("--remote-debugging-port=")[1].split()[0]
                if port.isdigit() and int(port) not in ports_to_try:
                    ports_to_try.insert(0, int(port))
    except Exception:
        pass

    for port in ports_to_try:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/json")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
            tabs = []
            seen = set()
            for entry in data:
                url = entry.get("url", "")
                if not url or url in seen or url.startswith("chrome://") or url.startswith("chrome-extension://"):
                    continue
                seen.add(url)
                tabs.append({
                    "url": url,
                    "title": entry.get("title", ""),
                    "cdp_id": entry.get("id", ""),
                })
            print(f"  CDP connected on port {port}: {len(tabs)} tabs (exact)")
            return tabs
        except Exception:
            continue
    return None


def _normalize_url(url: str) -> str:
    """Normalize URL for deduplication: strip tracking params, fragments."""
    try:
        parsed = urlparse(url)
        # Strip fragments
        # Strip common tracking query params
        strip_params = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                        "utm_term", "ref", "fbclid", "gclid", "srsltid", "si"}
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            filtered = {k: v for k, v in qs.items() if k not in strip_params}
            new_query = urlencode(filtered, doseq=True)
        else:
            new_query = ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
    except Exception:
        return url


def get_current_tabs_from_sessions() -> list[dict]:
    """Extract currently open tab URLs from Comet's Session/Tabs files (fallback)."""
    tabs = []
    seen_urls = set()

    if not COMET_SESSIONS.exists():
        return tabs

    # Find the most recent Tabs_ file only (not 2)
    tab_files = sorted(COMET_SESSIONS.glob("Tabs_*"), key=lambda f: f.stat().st_mtime, reverse=True)

    for tab_file in tab_files[:1]:  # Only the most recent
        try:
            result = subprocess.run(
                ["strings", str(tab_file)],
                capture_output=True, text=True, timeout=10
            )
            urls = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("http://") or line.startswith("https://"):
                    # Clean URL — remove trailing junk
                    url = line.split("\x00")[0].split("\t")[0].strip()
                    if len(url) > 10:
                        norm = _normalize_url(url)
                        if norm not in seen_urls:
                            seen_urls.add(norm)
                            urls.append(url)
            for url in urls:
                tabs.append({"url": url})
        except Exception as e:
            print(f"  Warning: Could not read {tab_file.name}: {e}")

    return tabs


def enrich_tabs_from_history(tabs: list[dict]) -> list[dict]:
    """Cross-reference tab URLs with History DB to get titles and visit info."""
    if not COMET_HISTORY.exists() or not tabs:
        return tabs

    # Copy history DB to avoid lock conflicts
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy2(COMET_HISTORY, tmp)
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row

        for tab in tabs:
            url = tab["url"]
            row = conn.execute(
                "SELECT title, visit_count, last_visit_time FROM urls WHERE url = ? LIMIT 1",
                (url,)
            ).fetchone()
            if row:
                tab["title"] = row["title"] or ""
                tab["visit_count"] = row["visit_count"]
                tab["last_visit_time"] = chromium_to_unix(row["last_visit_time"])
            else:
                # Try prefix match for URLs that might differ slightly
                row = conn.execute(
                    "SELECT title, visit_count, last_visit_time FROM urls WHERE url LIKE ? ORDER BY last_visit_time DESC LIMIT 1",
                    (url.split("?")[0] + "%",)
                ).fetchone()
                if row:
                    tab["title"] = row["title"] or ""
                    tab["visit_count"] = row["visit_count"]
                    tab["last_visit_time"] = chromium_to_unix(row["last_visit_time"])
                else:
                    tab["title"] = ""
                    tab["visit_count"] = 0
                    tab["last_visit_time"] = None

        conn.close()
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    return tabs


def categorize_tabs(tabs: list[dict]) -> list[dict]:
    """Add category and stale info to each tab."""
    for tab in tabs:
        url = tab["url"]
        tab["domain"] = get_domain(url)
        cat, color = categorize_url(url)
        tab["category"] = cat
        tab["category_color"] = color
        stale, reason = check_stale(url)
        tab["is_stale"] = stale
        tab["stale_reason"] = reason
    return tabs


def check_localhost_alive(tabs: list[dict]) -> list[dict]:
    """Mark localhost tabs as stale if the server isn't running."""
    import re
    for tab in tabs:
        if tab.get("is_stale"):
            continue
        url = tab["url"]
        match = re.search(r"localhost:(\d+)", url) or re.search(r"127\.0\.0\.1:(\d+)", url)
        if match:
            port = match.group(1)
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5
            )
            if not result.stdout.strip():
                tab["is_stale"] = True
                tab["stale_reason"] = f"No server running on port {port}"
    return tabs


def save_snapshot(tabs: list[dict], source: str = "auto", note: str = None) -> int:
    """Save a tab snapshot to the ledger database. Returns snapshot ID."""
    conn = sqlite3.connect(LEDGER_DB)

    cursor = conn.execute(
        "INSERT INTO snapshots (tab_count, source, note) VALUES (?, ?, ?)",
        (len(tabs), source, note)
    )
    snapshot_id = cursor.lastrowid

    for tab in tabs:
        conn.execute(
            """INSERT INTO tabs (snapshot_id, url, title, domain, category, category_color, is_stale, stale_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                tab["url"],
                tab.get("title", ""),
                tab.get("domain", ""),
                tab.get("category", "Uncategorized"),
                tab.get("category_color", "#9CA3AF"),
                tab.get("is_stale", False),
                tab.get("stale_reason"),
            )
        )

    conn.commit()
    conn.close()
    return snapshot_id


def take_snapshot(source: str = "auto", note: str = None) -> dict:
    """Full snapshot pipeline: capture → enrich → categorize → save."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Taking tab snapshot...")

    # 1. Try CDP first (exact tab list)
    tab_source = "session_files"
    tabs = get_tabs_via_cdp()
    if tabs is not None:
        tab_source = "cdp"
        print(f"  Got {len(tabs)} tabs via CDP (exact)")
    else:
        # 2. Fallback: session files (no history supplement)
        tabs = get_current_tabs_from_sessions()
        print(f"  Found {len(tabs)} tabs from session files (approx)")

    # 3. Enrich with titles from history (only needed for session file fallback)
    if tab_source != "cdp":
        tabs = enrich_tabs_from_history(tabs)

    # 4. Categorize
    tabs = categorize_tabs(tabs)

    # 5. Check localhost liveness
    try:
        tabs = check_localhost_alive(tabs)
    except Exception:
        pass  # Non-critical

    # 6. Save — include source method in the note
    source_note = f"[{tab_source}]"
    if note:
        source_note = f"{source_note} {note}"
    snapshot_id = save_snapshot(tabs, source=source, note=source_note)

    # Stats
    categories = {}
    stale_count = 0
    for t in tabs:
        cat = t["category"]
        categories[cat] = categories.get(cat, 0) + 1
        if t["is_stale"]:
            stale_count += 1

    print(f"  Saved snapshot #{snapshot_id}: {len(tabs)} tabs, {stale_count} stale")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {count}")

    return {
        "snapshot_id": snapshot_id,
        "tab_count": len(tabs),
        "stale_count": stale_count,
        "categories": categories,
        "tabs": tabs,
        "tab_source": tab_source,
    }


if __name__ == "__main__":
    init_db()
    result = take_snapshot(source="manual")
    print(f"\nDone. Snapshot #{result['snapshot_id']} with {result['tab_count']} tabs.")
