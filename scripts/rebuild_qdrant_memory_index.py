#!/usr/bin/env python3
from __future__ import annotations

import argparse

from gateway.memory.settings import env_flag
from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild Qdrant memory index from SQLite facts")
    parser.add_argument("--db-path", required=True, help="Path to SQLite database with memory_os_facts")
    parser.add_argument("--qdrant-url", default=None, help="Qdrant base URL")
    parser.add_argument("--qdrant-api-key", default=None, help="Qdrant API key")
    parser.add_argument("--collection", default=None, help="Qdrant collection name")
    parser.add_argument("--timeout", type=float, default=1.5, help="Qdrant request timeout in seconds")
    parser.add_argument("--vector-size", type=int, default=32, help="Embedding vector size")
    parser.add_argument("--user-id", type=int, default=None, help="Reindex only one Telegram user_id")
    parser.add_argument("--dry-run", action="store_true", help="Count and print candidate records without contacting Qdrant")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    embedding_adapter = EmbeddingAdapter(vector_size=args.vector_size)
    if args.dry_run:
        bridge = HealBiteMemoryBridge(
            args.db_path,
            qdrant_adapter=None,
            embedding_adapter=embedding_adapter,
            background_write=False,
        )
        try:
            total = sum(1 for _ in bridge.iter_facts(user_id=args.user_id))
        finally:
            bridge.close()
        scope = f" for user_id={args.user_id}" if args.user_id is not None else ""
        print(f"Dry run: {total} facts would be reindexed{scope}")
        return 0

    qdrant_adapter = QdrantMemoryAdapter(
        url=args.qdrant_url,
        api_key=args.qdrant_api_key,
        collection_name=args.collection,
        timeout=args.timeout,
        vector_size=args.vector_size,
        embedding_adapter=embedding_adapter,
        enabled=env_flag("MEMORY_VECTOR_ENABLED", default=False),
    )
    bridge = HealBiteMemoryBridge(
        args.db_path,
        qdrant_adapter=qdrant_adapter,
        embedding_adapter=embedding_adapter,
        background_write=False,
    )
    try:
        synced = bridge.rebuild_qdrant_index(user_id=args.user_id)
    finally:
        bridge.close()
    target = args.collection or getattr(qdrant_adapter, "collection_name", "healbite_memory_os")
    print(f"Reindexed {synced} facts into {target}")
    return 0 if synced >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
