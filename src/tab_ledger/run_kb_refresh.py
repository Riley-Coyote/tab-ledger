#!/usr/bin/env python3
"""Nightly KB refresh — re-imports sessions and rebuilds FTS index.

Runs stages 1 (taxonomy/import), 2 (messages), 3 (FTS), 5 (linking), 6 (auxiliary).
Skips stage 4 (summarization) to avoid API costs — run that manually when needed.
"""

import os
import sys
import time
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

def main():
    start = time.time()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] KB refresh starting...")

    # Stage 0: Ensure schema exists (no-op if already there)
    from .kb_schema import create_schema
    create_schema(drop_existing=False)

    # Stage 1: Taxonomy + session import (picks up new sessions from ledger.db)
    print("\n--- Stage 1: Taxonomy & Session Import ---")
    from .kb_taxonomy import build_taxonomy
    build_taxonomy()

    # Stage 2: Message indexing (indexes new JSONL messages)
    print("\n--- Stage 2: Message Indexing ---")
    from .kb_indexer import index_all_messages
    index_all_messages(resume=True)

    # Stage 3: FTS rebuild — clear and rebuild to include new content
    print("\n--- Stage 3: FTS Rebuild ---")
    from .kb_schema import get_kb_db
    kb = get_kb_db()
    # Delete existing FTS entries so we can rebuild with new content
    kb.execute("DELETE FROM kb_fts")
    kb.commit()
    kb.close()
    # Now run the FTS build stage
    from .kb_build import stage_3_fts
    stage_3_fts()

    # Stage 5: Cross-session linking
    print("\n--- Stage 5: Linking ---")
    from .kb_linker import build_all_connections
    build_all_connections()

    # Stage 6: Auxiliary data
    print("\n--- Stage 6: Auxiliary ---")
    from .kb_auxiliary import index_all_auxiliary
    index_all_auxiliary()

    # Optional Stage 8: Semantic embeddings
    semantic_provider = os.getenv("KB_SEMANTIC_PROVIDER", "").strip().lower()
    if semantic_provider:
        print("\n--- Stage 8: Semantic Indexing ---")
        from .kb_schema import get_kb_db
        from .kb_semantic import create_embedding_provider, build_semantic_index

        embedder = create_embedding_provider(
            semantic_provider,
            model=os.getenv("KB_SEMANTIC_MODEL"),
        )
        include_messages = os.getenv("KB_SEMANTIC_INCLUDE_MESSAGES", "0").lower() in {"1", "true", "yes"}

        kb = get_kb_db()
        try:
            stats = build_semantic_index(
                kb,
                provider=embedder,
                include_messages=include_messages,
            )
        finally:
            kb.close()
        print(f"  Semantic index refreshed: {stats}")
    else:
        print("\n--- Stage 8: Semantic Indexing (skipped) ---")
        print("  Set KB_SEMANTIC_PROVIDER=hash|ollama|openai to enable.")

    elapsed = time.time() - start
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] KB refresh complete in {int(elapsed)}s")


if __name__ == "__main__":
    main()
