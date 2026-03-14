"""Semantic indexing and retrieval for the knowledge base.

Provides:
- Incremental embedding index builds for KB content
- Multi-provider embedding generation (local hash, Ollama, OpenAI)
- Semantic search over indexed memory artifacts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kb_schema import get_kb_db


DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"


@dataclass
class SemanticDocument:
    source_key: str
    source_type: str
    session_uuid: Optional[str]
    project_name: Optional[str]
    text: str
    metadata: Dict[str, Any]

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", errors="ignore")).hexdigest()

    @property
    def text_preview(self) -> str:
        return self.text[:280]


class BaseEmbeddingProvider:
    provider_name: str = "unknown"

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    def embed_text(self, text: str) -> List[float]:
        embeddings = self.embed_texts([text])
        if not embeddings:
            raise RuntimeError("Embedding provider returned no embedding")
        return embeddings[0]


class HashEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic local embedding fallback.

    Not as semantically rich as neural embeddings, but fully local and robust.
    Useful for local-first deployments and CI.
    """

    provider_name = "hash"

    def __init__(self, dim: int = 768):
        if dim < 64:
            raise ValueError("Hash embedding dimension must be >= 64")
        self.dim = dim

    @property
    def model_name(self) -> str:
        return f"hash-{self.dim}"

    def _tokenize(self, text: str) -> List[str]:
        cleaned = re.sub(r"\s+", " ", text.lower()).strip()
        words = re.findall(r"[a-z0-9_\-]{2,}", cleaned)
        tokens = list(words)
        # Add simple phrase-ish features for better topical grouping.
        for i in range(len(words) - 1):
            tokens.append(f"{words[i]}_{words[i + 1]}")
        return tokens

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for token in self._tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vec[idx] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 1e-12:
            return vec
        return [v / norm for v in vec]

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t or "") for t in texts]


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "openai"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self._model = model or DEFAULT_OPENAI_MODEL
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for provider=openai")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    @property
    def model_name(self) -> str:
        return self._model

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []

        payload = json.dumps({"model": self._model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OpenAI embedding HTTP {e.code}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"OpenAI embedding request failed: {e}") from e

        data = body.get("data", [])
        if len(data) != len(texts):
            raise RuntimeError("OpenAI embedding response size mismatch")
        return [item["embedding"] for item in data]


class OllamaEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "ollama"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self._model = model or os.getenv("OLLAMA_EMBED_MODEL") or DEFAULT_OLLAMA_MODEL
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")

    @property
    def model_name(self) -> str:
        return self._model

    def _embed_one(self, text: str) -> List[float]:
        payload = json.dumps({"model": self._model, "prompt": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Ollama embedding HTTP {e.code}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama embedding request failed: {e}") from e

        embedding = body.get("embedding")
        if not embedding:
            raise RuntimeError("Ollama returned no embedding")
        return embedding

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._embed_one(t) for t in texts]


def create_embedding_provider(provider: str, model: Optional[str] = None) -> BaseEmbeddingProvider:
    provider_name = (provider or "").strip().lower()
    if provider_name == "openai":
        return OpenAIEmbeddingProvider(model=model)
    if provider_name == "ollama":
        return OllamaEmbeddingProvider(model=model)
    if provider_name == "hash":
        dim = 768
        if model:
            m = str(model).strip().lower()
            if m.startswith("hash-"):
                m = m.replace("hash-", "", 1)
            if m.isdigit():
                dim = int(m)
        return HashEmbeddingProvider(dim=dim)
    raise ValueError(f"Unknown embedding provider '{provider}'. Choose: hash, ollama, openai")


def _table_exists(kb_conn: sqlite3.Connection, table_name: str) -> bool:
    row = kb_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return bool(row)


def ensure_semantic_schema(kb_conn: sqlite3.Connection) -> None:
    """Ensure semantic tables/indexes exist for legacy KB databases.

    This is intentionally scoped to semantic objects so we can migrate old DBs
    lazily without requiring a full stage-0 rebuild first.
    """
    kb_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_embeddings (
            id INTEGER PRIMARY KEY,
            source_key TEXT UNIQUE NOT NULL,
            session_uuid TEXT,
            source_type TEXT NOT NULL,
            project_name TEXT,
            text_hash TEXT NOT NULL,
            text_preview TEXT,
            embedding BLOB NOT NULL,
            embedding_norm REAL DEFAULT 0.0,
            embedding_dim INTEGER NOT NULL,
            embedding_model TEXT NOT NULL,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    kb_conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_embeddings_project ON kb_embeddings(project_name)")
    kb_conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_embeddings_session ON kb_embeddings(session_uuid)")
    kb_conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_embeddings_type ON kb_embeddings(source_type)")
    kb_conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_embeddings_model ON kb_embeddings(embedding_model)")

    if _table_exists(kb_conn, "kb_progress"):
        kb_conn.execute(
            "INSERT OR IGNORE INTO kb_progress (stage, status) VALUES ('semantic_indexing', 'pending')"
        )
    kb_conn.commit()


def _pack_embedding(vec: List[float]) -> bytes:
    arr = array("f", [float(v) for v in vec])
    return arr.tobytes()


def _unpack_embedding(blob: bytes) -> List[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def _vector_norm(vec: List[float]) -> float:
    return math.sqrt(sum(v * v for v in vec))


def _cosine_similarity(query_vec: List[float], query_norm: float, db_vec: List[float], db_norm: float) -> float:
    if not query_norm or not db_norm:
        return 0.0
    if len(query_vec) != len(db_vec):
        return 0.0
    dot = sum(a * b for a, b in zip(query_vec, db_vec))
    return dot / (query_norm * db_norm)


def _coerce_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _build_summary_semantic_text(summary_text: str, summary_json_raw: str) -> str:
    """Fuse summary text + structured summary fields into one semantic document."""
    summary = (summary_text or "").strip()
    decisions: List[str] = []
    next_steps: List[str] = []
    blockers: List[str] = []

    parsed: Dict[str, Any] = {}
    if summary_json_raw:
        try:
            decoded = json.loads(summary_json_raw)
            if isinstance(decoded, dict):
                parsed = decoded
        except json.JSONDecodeError:
            parsed = {}

    if not summary:
        summary = str(parsed.get("summary") or "").strip()

    decisions = _coerce_string_list(parsed.get("decisions"))
    next_steps = _coerce_string_list(parsed.get("next_steps"))
    blockers = _coerce_string_list(parsed.get("blockers"))

    parts: List[str] = []
    if summary:
        parts.append(summary)
    if decisions:
        parts.append("Decisions:\n" + "\n".join(f"- {item}" for item in decisions))
    if next_steps:
        parts.append("Next Steps:\n" + "\n".join(f"- {item}" for item in next_steps))
    if blockers:
        parts.append("Blockers:\n" + "\n".join(f"- {item}" for item in blockers))
    return "\n\n".join(parts).strip()


def collect_semantic_documents(
    kb_conn: sqlite3.Connection,
    include_messages: bool = False,
    min_message_chars: int = 280,
    max_message_docs: int = 4000,
) -> List[SemanticDocument]:
    docs: List[SemanticDocument] = []

    # Session summaries + first prompts are high-signal continuity anchors.
    rows = kb_conn.execute(
        """
        SELECT s.session_uuid, s.slug, s.summary_text, s.summary_json, s.first_prompt, p.canonical_name
        FROM kb_sessions s
        LEFT JOIN kb_projects p ON p.id = s.project_id
        ORDER BY s.started_at DESC
        """
    ).fetchall()

    for row in rows:
        session_uuid = row["session_uuid"]
        project_name = row["canonical_name"] if "canonical_name" in row.keys() else None
        slug = row["slug"] or ""
        summary_text = _build_summary_semantic_text(
            row["summary_text"] or "",
            row["summary_json"] or "",
        )
        first_prompt = row["first_prompt"] or ""

        if summary_text.strip():
            docs.append(
                SemanticDocument(
                    source_key=f"summary:{session_uuid}",
                    source_type="summary",
                    session_uuid=session_uuid,
                    project_name=project_name,
                    text=summary_text.strip(),
                    metadata={"slug": slug},
                )
            )

        if first_prompt.strip():
            docs.append(
                SemanticDocument(
                    source_key=f"prompt:{session_uuid}",
                    source_type="prompt",
                    session_uuid=session_uuid,
                    project_name=project_name,
                    text=first_prompt.strip(),
                    metadata={"slug": slug},
                )
            )

    # Plans preserve medium-horizon intent and strategy.
    plan_rows = kb_conn.execute(
        """
        SELECT pl.filename, pl.slug, pl.title, pl.content, p.canonical_name
        FROM kb_plans pl
        LEFT JOIN kb_projects p ON p.id = pl.project_id
        ORDER BY pl.created_at DESC
        """
    ).fetchall()
    for row in plan_rows:
        content = row["content"] or ""
        title = row["title"] or row["slug"] or row["filename"]
        if not content.strip():
            continue
        docs.append(
            SemanticDocument(
                source_key=f"plan:{row['filename']}",
                source_type="plan",
                session_uuid=row["slug"] or None,
                project_name=row["canonical_name"] if "canonical_name" in row.keys() else None,
                text=f"{title}\n\n{content}".strip(),
                metadata={"filename": row["filename"], "title": title},
            )
        )

    # Todos preserve unresolved intent and pending actions.
    todo_rows = kb_conn.execute(
        """
        SELECT td.filename, td.session_uuid, td.items_json, p.canonical_name
        FROM kb_todos td
        LEFT JOIN kb_projects p ON p.id = td.project_id
        ORDER BY td.filename
        """
    ).fetchall()
    for row in todo_rows:
        raw = row["items_json"] or "[]"
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            items = []

        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or item.get("title") or "").strip()
            status = (item.get("status") or "").strip()
            if not text:
                continue
            lines.append(f"[{status or 'unknown'}] {text}")

        if not lines:
            continue
        docs.append(
            SemanticDocument(
                source_key=f"todo:{row['filename']}",
                source_type="todo",
                session_uuid=row["session_uuid"],
                project_name=row["canonical_name"] if "canonical_name" in row.keys() else None,
                text="\n".join(lines),
                metadata={"filename": row["filename"]},
            )
        )

    # Optional deep message indexing for richer semantic recall.
    if include_messages:
        msg_rows = kb_conn.execute(
            """
            SELECT
                s.session_uuid,
                p.canonical_name as project_name,
                m.message_index,
                m.role,
                m.content_text
            FROM kb_messages m
            JOIN kb_sessions s ON s.id = m.session_id
            LEFT JOIN kb_projects p ON p.id = s.project_id
            WHERE m.content_text IS NOT NULL
              AND m.content_length >= ?
            ORDER BY m.content_length DESC
            LIMIT ?
            """,
            (min_message_chars, max_message_docs),
        ).fetchall()
        for row in msg_rows:
            content_text = row["content_text"] or ""
            if not content_text.strip():
                continue
            docs.append(
                SemanticDocument(
                    source_key=f"message:{row['session_uuid']}:{row['message_index']}",
                    source_type="message",
                    session_uuid=row["session_uuid"],
                    project_name=row["project_name"],
                    text=content_text,
                    metadata={"role": row["role"], "message_index": row["message_index"]},
                )
            )

    return docs


def build_semantic_index(
    kb_conn: sqlite3.Connection,
    provider: BaseEmbeddingProvider,
    include_messages: bool = False,
    batch_size: int = 32,
    prune_stale: bool = True,
) -> Dict[str, Any]:
    ensure_semantic_schema(kb_conn)

    if batch_size < 1:
        batch_size = 1

    if _table_exists(kb_conn, "kb_progress"):
        kb_conn.execute(
            "UPDATE kb_progress SET status='running', started_at=?, notes=? WHERE stage='semantic_indexing'",
            (
                datetime.now(timezone.utc).isoformat(),
                f"provider={provider.provider_name}, model={provider.model_name}",
            ),
        )
    kb_conn.commit()

    docs = collect_semantic_documents(kb_conn, include_messages=include_messages)
    existing = kb_conn.execute(
        "SELECT source_key, text_hash, embedding_model FROM kb_embeddings"
    ).fetchall()
    existing_map = {row["source_key"]: (row["text_hash"], row["embedding_model"]) for row in existing}

    to_embed: List[SemanticDocument] = []
    current_keys = set()

    for doc in docs:
        current_keys.add(doc.source_key)
        prev = existing_map.get(doc.source_key)
        if prev and prev[0] == doc.text_hash and prev[1] == provider.model_name:
            continue
        to_embed.append(doc)

    embedded = 0
    for start in range(0, len(to_embed), batch_size):
        batch = to_embed[start : start + batch_size]
        vectors = provider.embed_texts([d.text for d in batch])
        if len(vectors) != len(batch):
            raise RuntimeError("Embedding provider returned mismatched batch length")

        for doc, vec in zip(batch, vectors):
            if not vec:
                continue
            norm = _vector_norm(vec)
            kb_conn.execute(
                """
                INSERT INTO kb_embeddings (
                    source_key, session_uuid, source_type, project_name,
                    text_hash, text_preview, embedding, embedding_norm, embedding_dim,
                    embedding_model, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    session_uuid = excluded.session_uuid,
                    source_type = excluded.source_type,
                    project_name = excluded.project_name,
                    text_hash = excluded.text_hash,
                    text_preview = excluded.text_preview,
                    embedding = excluded.embedding,
                    embedding_norm = excluded.embedding_norm,
                    embedding_dim = excluded.embedding_dim,
                    embedding_model = excluded.embedding_model,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    doc.source_key,
                    doc.session_uuid,
                    doc.source_type,
                    doc.project_name,
                    doc.text_hash,
                    doc.text_preview,
                    _pack_embedding(vec),
                    norm,
                    len(vec),
                    provider.model_name,
                    json.dumps(doc.metadata, ensure_ascii=True),
                ),
            )
            embedded += 1

        kb_conn.commit()

    stale_deleted = 0
    if prune_stale:
        stale_keys = [key for key in existing_map if key not in current_keys]
        for i in range(0, len(stale_keys), 500):
            chunk = stale_keys[i : i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            cursor = kb_conn.execute(
                f"DELETE FROM kb_embeddings WHERE source_key IN ({placeholders})",
                chunk,
            )
            stale_deleted += cursor.rowcount if cursor.rowcount is not None else 0
        kb_conn.commit()

    total_rows = kb_conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]
    notes = (
        f"docs={len(docs)}, embedded={embedded}, stale_deleted={stale_deleted}, "
        f"provider={provider.provider_name}, model={provider.model_name}"
    )
    if _table_exists(kb_conn, "kb_progress"):
        kb_conn.execute(
            """
            UPDATE kb_progress SET
                status='completed',
                processed=?,
                total=?,
                errors=0,
                completed_at=?,
                notes=?
            WHERE stage='semantic_indexing'
            """,
            (
                embedded,
                len(docs),
                datetime.now(timezone.utc).isoformat(),
                notes,
            ),
        )
    kb_conn.commit()

    return {
        "documents_total": len(docs),
        "documents_embedded": embedded,
        "stale_deleted": stale_deleted,
        "index_size": total_rows,
        "provider": provider.provider_name,
        "model": provider.model_name,
    }


def semantic_search(
    kb_conn: sqlite3.Connection,
    query: str,
    provider: BaseEmbeddingProvider,
    limit: int = 20,
    project: Optional[str] = None,
    source_type: Optional[str] = None,
    min_score: float = 0.18,
    include_fts_boost: bool = True,
) -> List[Dict[str, Any]]:
    if not query.strip():
        return []
    if not _table_exists(kb_conn, "kb_embeddings"):
        return []

    query_vec = provider.embed_text(query)
    query_norm = _vector_norm(query_vec)
    if not query_vec or query_norm <= 1e-12:
        return []

    sql = """
        SELECT source_key, session_uuid, source_type, project_name,
               text_preview, embedding, embedding_norm, embedding_model, metadata_json
        FROM kb_embeddings
        WHERE embedding_model = ?
    """
    params: List[Any] = [provider.model_name]

    if project:
        sql += " AND project_name = ?"
        params.append(project)
    if source_type:
        sql += " AND source_type = ?"
        params.append(source_type)

    rows = kb_conn.execute(sql, params).fetchall()
    if not rows:
        return []

    lexical_score: Dict[str, float] = {}
    if include_fts_boost:
        try:
            fts_rows = kb_conn.execute(
                """
                SELECT session_uuid, source_type
                FROM kb_fts
                WHERE kb_fts MATCH ?
                LIMIT ?
                """,
                (query, max(limit * 6, 30)),
            ).fetchall()
            for rank, row in enumerate(fts_rows):
                key = f"{row['source_type']}:{row['session_uuid']}"
                lexical_score[key] = max(lexical_score.get(key, 0.0), 1.0 / (1.0 + rank))
        except sqlite3.Error:
            lexical_score = {}

    scored: List[Dict[str, Any]] = []
    for row in rows:
        db_vec = _unpack_embedding(row["embedding"])
        score = _cosine_similarity(query_vec, query_norm, db_vec, row["embedding_norm"] or 0.0)
        if score < min_score:
            continue

        source_key = row["source_key"]
        fts_key = f"{row['source_type']}:{row['session_uuid']}" if row["session_uuid"] else source_key
        hybrid = score + (0.08 * lexical_score.get(fts_key, 0.0))

        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                metadata = {}

        scored.append(
            {
                "source_key": source_key,
                "source_type": row["source_type"],
                "session_uuid": row["session_uuid"],
                "project_name": row["project_name"],
                "text_preview": row["text_preview"],
                "semantic_score": round(score, 4),
                "hybrid_score": round(hybrid, 4),
                "metadata": metadata,
            }
        )

    scored.sort(key=lambda x: x["hybrid_score"], reverse=True)
    top = scored[:limit]

    # Enrich session metadata in one pass.
    uuids = sorted({r["session_uuid"] for r in top if r.get("session_uuid")})
    session_map: Dict[str, Dict[str, Any]] = {}
    if uuids:
        placeholders = ",".join(["?"] * len(uuids))
        session_rows = kb_conn.execute(
            f"""
            SELECT session_uuid, slug, started_at, phase, outcome, summary_text
            FROM kb_sessions
            WHERE session_uuid IN ({placeholders})
            """,
            uuids,
        ).fetchall()
        session_map = {r["session_uuid"]: dict(r) for r in session_rows}

    for result in top:
        sid = result.get("session_uuid")
        if sid and sid in session_map:
            result["session_info"] = session_map[sid]

    return top


def semantic_status(kb_conn: sqlite3.Connection) -> Dict[str, Any]:
    if not _table_exists(kb_conn, "kb_embeddings"):
        return {
            "ready": False,
            "reason": "kb_embeddings table does not exist yet; run semantic indexing first",
            "total_embeddings": 0,
            "by_source_type": [],
            "by_model": [],
        }

    total = kb_conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]
    by_source = kb_conn.execute(
        """
        SELECT source_type, COUNT(*) as cnt
        FROM kb_embeddings
        GROUP BY source_type
        ORDER BY cnt DESC
        """
    ).fetchall()
    by_model = kb_conn.execute(
        """
        SELECT embedding_model, COUNT(*) as cnt
        FROM kb_embeddings
        GROUP BY embedding_model
        ORDER BY cnt DESC
        """
    ).fetchall()
    return {
        "ready": True,
        "total_embeddings": total,
        "by_source_type": [dict(r) for r in by_source],
        "by_model": [dict(r) for r in by_model],
    }


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Semantic indexing and search for tab-ledger KB")
    sub = parser.add_subparsers(dest="command", required=True)

    idx = sub.add_parser("index", help="Build/refresh semantic embedding index")
    idx.add_argument("--provider", default=os.getenv("KB_SEMANTIC_PROVIDER", "hash"))
    idx.add_argument("--model", default=os.getenv("KB_SEMANTIC_MODEL"))
    idx.add_argument("--include-messages", action="store_true")
    idx.add_argument("--batch-size", type=int, default=32)
    idx.add_argument("--no-prune", action="store_true")

    sea = sub.add_parser("search", help="Run semantic search")
    sea.add_argument("query")
    sea.add_argument("--provider", default=os.getenv("KB_SEMANTIC_PROVIDER", "hash"))
    sea.add_argument("--model", default=os.getenv("KB_SEMANTIC_MODEL"))
    sea.add_argument("--project")
    sea.add_argument("--type", dest="source_type")
    sea.add_argument("--limit", type=int, default=20)
    sea.add_argument("--min-score", type=float, default=0.18)

    sta = sub.add_parser("status", help="Show semantic index status")

    return parser


def main() -> None:
    parser = _build_cli()
    args = parser.parse_args()

    if args.command == "status":
        kb = get_kb_db(readonly=True)
        try:
            print(json.dumps(semantic_status(kb), indent=2, default=str))
        finally:
            kb.close()
        return

    provider = create_embedding_provider(
        getattr(args, "provider", os.getenv("KB_SEMANTIC_PROVIDER", "hash")),
        model=getattr(args, "model", None),
    )

    if args.command == "index":
        kb = get_kb_db()
        try:
            stats = build_semantic_index(
                kb,
                provider=provider,
                include_messages=args.include_messages,
                batch_size=args.batch_size,
                prune_stale=not args.no_prune,
            )
            print(json.dumps(stats, indent=2, default=str))
        finally:
            kb.close()
        return

    if args.command == "search":
        kb = get_kb_db(readonly=True)
        try:
            results = semantic_search(
                kb,
                query=args.query,
                provider=provider,
                limit=args.limit,
                project=args.project,
                source_type=args.source_type,
                min_score=args.min_score,
            )
            print(json.dumps(results, indent=2, default=str))
        finally:
            kb.close()
        return

    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
