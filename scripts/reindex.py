"""
Re-embed every stored chunk with the configured embedding model.

Run this after changing EMBEDDING_MODEL / EMBEDDING_DIM (e.g. moving from
MiniLM 384-d to Qwen3-Embedding 1024-d). It:

  1. retypes chunks.embedding to vector(EMBEDDING_DIM) if needed,
  2. re-embeds chunk text (with its section header when present) in batches,
  3. rebuilds the HNSW index once at the end (much faster than per-row upkeep).

Usage (from the repo root):
    python scripts/reindex.py                  # everything
    python scripts/reindex.py --only-missing   # just rows with no embedding
    python scripts/reindex.py --document-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import bindparam, text  # noqa: E402

from config import settings  # noqa: E402
from db.database import engine, init_db  # noqa: E402
from services.embedder import embedder, embedding_text  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("reindex")

HNSW_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks "
    "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
)


async def column_type(conn) -> str | None:
    row = (await conn.execute(text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = to_regclass('chunks') AND attname = 'embedding' "
        "AND attnum > 0 AND NOT attisdropped"
    ))).fetchone()
    return row[0] if row else None


async def has_header_column(conn) -> bool:
    row = (await conn.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'chunks' AND column_name = 'header'"
    ))).fetchone()
    return row is not None


async def main(args: argparse.Namespace) -> None:
    target = f"vector({settings.embedding_dim})"

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        current = await column_type(conn)
        if current and current != target:
            log.info("Retyping chunks.embedding %s -> %s (existing vectors dropped)", current, target)
            await conn.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_ivfflat"))
            await conn.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
            await conn.execute(text(
                f"ALTER TABLE chunks ALTER COLUMN embedding TYPE {target} USING NULL"
            ))
            args.only_missing = False
        elif not args.only_missing:
            # Drop the index during bulk updates; rebuilt at the end.
            await conn.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))

    # Creates any tables/columns added since this DB was made.
    await init_db()

    async with engine.connect() as conn:
        header_sql = "header" if await has_header_column(conn) else "NULL AS header"
        where = ["TRUE"]
        params: dict = {}
        if args.only_missing:
            where.append("embedding IS NULL")
        if args.document_id:
            where.append("document_id = :doc_id")
            params["doc_id"] = args.document_id
        rows = (await conn.execute(
            text(f"SELECT id, {header_sql}, text FROM chunks WHERE {' AND '.join(where)} ORDER BY document_id, chunk_index"),
            params,
        )).fetchall()

    log.info("Re-embedding %d chunks with %s", len(rows), settings.local_embedding_model)
    started = time.time()
    update = text("UPDATE chunks SET embedding = CAST(:emb AS vector) WHERE id = :id").bindparams(
        bindparam("emb"), bindparam("id")
    )
    for i in range(0, len(rows), args.batch_size):
        batch = rows[i : i + args.batch_size]
        vectors = await embedder.embed([embedding_text(r.header, r.text) for r in batch])
        async with engine.begin() as conn:
            await conn.execute(
                update,
                [{"id": r.id, "emb": "[" + ",".join(f"{v:.7f}" for v in vec) + "]"} for r, vec in zip(batch, vectors)],
            )
        done = i + len(batch)
        rate = done / max(time.time() - started, 1e-6)
        log.info("  %d/%d chunks (%.1f/s)", done, len(rows), rate)

    async with engine.begin() as conn:
        log.info("Building HNSW index …")
        await conn.execute(text(HNSW_SQL))
    await engine.dispose()
    log.info("Done in %.1fs", time.time() - started)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only-missing", action="store_true", help="only embed rows with NULL embedding")
    parser.add_argument("--document-id", help="limit to one document")
    parser.add_argument("--batch-size", type=int, default=64)
    asyncio.run(main(parser.parse_args()))
