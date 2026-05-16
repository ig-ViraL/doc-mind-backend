"""
Async pgvector CRUD operations and similarity search.

All functions accept an AsyncSession and perform fully async DB operations.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import String, bindparam, func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import Chunk, Document

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


async def save_document(
    session: AsyncSession,
    filename: str,
    file_type: str,
    page_count: int,
    file_key: str | None = None,
    file_url: str | None = None,
) -> Document:
    """
    Insert a new Document row and return the ORM object.
    """
    doc = Document(
        id=str(uuid.uuid4()),
        filename=filename,
        file_type=file_type,
        page_count=page_count,
        file_key=file_key,
        file_url=file_url,
    )
    session.add(doc)
    await session.flush()  # Populate server-side defaults (uploaded_at)
    logger.info("Saved document id=%s filename='%s'", doc.id, doc.filename)
    return doc


async def save_chunks(
    session: AsyncSession,
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    document_id: str,
) -> int:
    """
    Bulk-insert Chunk rows with their embeddings.

    Args:
        session:      Active async DB session.
        chunks:       List of chunk dicts from the chunker
                      (keys: text, chunk_index, start_char, end_char, document_id).
        embeddings:   Parallel list of embedding vectors (one per chunk).
        document_id:  Parent document UUID string.

    Returns:
        Number of chunks inserted.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"Mismatch: {len(chunks)} chunks vs {len(embeddings)} embeddings"
        )

    chunk_objects: list[Chunk] = []
    for chunk_dict, embedding in zip(chunks, embeddings):
        chunk_obj = Chunk(
            id=str(uuid.uuid4()),
            document_id=document_id,
            chunk_index=chunk_dict["chunk_index"],
            text=chunk_dict["text"],
            embedding=embedding,
            start_char=chunk_dict.get("start_char", 0),
            end_char=chunk_dict.get("end_char", 0),
        )
        chunk_objects.append(chunk_obj)

    session.add_all(chunk_objects)
    await session.flush()
    logger.info("Saved %d chunks for document_id=%s", len(chunk_objects), document_id)
    return len(chunk_objects)


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------


async def similarity_search(
    session: AsyncSession,
    query_embedding: list[float],
    k: int = 5,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Find the *k* most similar chunks to *query_embedding* using cosine distance.

    Optionally filters to a subset of documents via *document_ids*.

    Returns a list of dicts with keys:
        chunk_index, text, similarity, document_id, filename
    """
    # Build the query vector literal for pgvector
    vector_literal = "[" + ",".join(str(v) for v in query_embedding) + "]"

    # cosine distance: embedding <=> query_vector  (0 = identical, 2 = opposite)
    # similarity = 1 - distance
    distance_expr = text(f"chunks.embedding <=> '{vector_literal}'::vector")
    similarity_expr = literal_column(f"1 - (chunks.embedding <=> '{vector_literal}'::vector)")

    stmt = (
        select(
            Chunk.chunk_index,
            Chunk.text,
            Chunk.document_id,
            Chunk.id.label("chunk_id"),
            Document.filename,
            similarity_expr.label("similarity"),
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.embedding.isnot(None))
        .order_by(distance_expr)
        .limit(k)
    )

    if document_ids:
        stmt = stmt.where(Chunk.document_id.in_(document_ids))

    result = await session.execute(stmt)
    rows = result.fetchall()

    hits: list[dict[str, Any]] = []
    for row in rows:
        hits.append(
            {
                "chunk_index": row.chunk_index,
                "text": row.text,
                "similarity": float(row.similarity),
                "document_id": row.document_id,
                "filename": row.filename,
            }
        )

    logger.debug("similarity_search returned %d results for k=%d", len(hits), k)
    return hits


async def get_all_documents(session: AsyncSession) -> list[dict[str, Any]]:
    """
    Return all documents with their chunk counts.
    """
    stmt = (
        select(
            Document.id,
            Document.filename,
            Document.file_type,
            Document.page_count,
            Document.uploaded_at,
            Document.file_key,
            Document.file_url,
            func.count(Chunk.id).label("chunk_count"),
        )
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        .group_by(Document.id)
        .order_by(Document.uploaded_at.desc())
    )

    result = await session.execute(stmt)
    rows = result.fetchall()

    docs: list[dict[str, Any]] = []
    for row in rows:
        docs.append(
            {
                "id": row.id,
                "filename": row.filename,
                "file_type": row.file_type,
                "page_count": row.page_count,
                "uploaded_at": row.uploaded_at,
                "file_key": row.file_key,
                "file_url": row.file_url,
                "chunk_count": row.chunk_count,
            }
        )

    logger.debug("get_all_documents returned %d documents", len(docs))
    return docs


async def get_document_chunks(
    session: AsyncSession,
    document_id: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Return (filename, chunks) for the given document, ordered by chunk_index.
    Returns (None, []) if the document does not exist.
    """
    stmt = (
        select(
            Document.filename,
            Chunk.chunk_index,
            Chunk.text,
            Chunk.start_char,
            Chunk.end_char,
        )
        .join(Chunk, Chunk.document_id == Document.id)
        .where(Document.id == document_id)
        .order_by(Chunk.chunk_index)
    )

    result = await session.execute(stmt)
    rows = result.fetchall()

    if not rows:
        # Distinguish "doc exists, zero chunks" from "doc not found"
        doc_stmt = select(Document.filename).where(Document.id == document_id)
        doc_result = await session.execute(doc_stmt)
        doc_row = doc_result.scalar_one_or_none()
        if doc_row is None:
            return None, []
        return doc_row, []

    filename = rows[0].filename
    chunks = [
        {
            "chunk_index": row.chunk_index,
            "text": row.text,
            "start_char": row.start_char,
            "end_char": row.end_char,
        }
        for row in rows
    ]
    logger.debug("get_document_chunks: doc=%s chunks=%d", document_id, len(chunks))
    return filename, chunks


async def delete_document(session: AsyncSession, document_id: str) -> Document | None:
    """
    Delete a document and all its chunks (cascaded by FK constraint).

    Returns the deleted Document so callers can access its file_key before it's gone.
    Returns None if the document did not exist.
    """
    stmt = select(Document).where(Document.id == document_id)
    result = await session.execute(stmt)
    doc = result.scalar_one_or_none()

    if doc is None:
        logger.warning("delete_document: document_id=%s not found", document_id)
        return None

    await session.delete(doc)
    await session.flush()
    logger.info("Deleted document id=%s and its chunks", document_id)
    return doc


# ---------------------------------------------------------------------------
# Lexical search (PostgreSQL full-text search)
# ---------------------------------------------------------------------------


async def lexical_search(
    session: AsyncSession,
    query: str,
    k: int = 5,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Full-text search using PostgreSQL's tsvector/tsquery machinery.

    Uses websearch_to_tsquery so the caller can pass raw user input safely
    (it tolerates unbalanced quotes, stopword-only queries, etc.).
    Ranks results with ts_rank_cd (cover-density ranking).

    Returns a list of dicts with keys:
        chunk_index, text, fts_rank, document_id, filename
    Returns an empty list when no FTS matches exist.
    """
    # 'english' is a hardcoded regconfig literal — not user input.
    lang = literal_column("'english'")
    fts_query_param = bindparam("fts_query", value=query, type_=String)

    tsvec = func.to_tsvector(lang, Chunk.text)
    tsquery = func.websearch_to_tsquery(lang, fts_query_param)

    rank_col = func.ts_rank_cd(tsvec, tsquery).label("fts_rank")
    match_cond = tsvec.op("@@")(tsquery)

    stmt = (
        select(
            Chunk.chunk_index,
            Chunk.text,
            Chunk.document_id,
            Document.filename,
            rank_col,
        )
        .join(Document, Chunk.document_id == Document.id)
        .where(match_cond)
        .order_by(rank_col.desc())
        .limit(k)
    )

    if document_ids:
        stmt = stmt.where(Chunk.document_id.in_(document_ids))

    try:
        result = await session.execute(stmt)
        rows = result.fetchall()
    except Exception as exc:
        logger.warning("lexical_search failed (%s) — returning empty result", exc)
        return []

    hits: list[dict[str, Any]] = []
    for row in rows:
        hits.append(
            {
                "chunk_index": row.chunk_index,
                "text": row.text,
                "fts_rank": float(row.fts_rank),
                "document_id": row.document_id,
                "filename": row.filename,
            }
        )

    logger.debug("lexical_search returned %d results for k=%d", len(hits), k)
    return hits


# ---------------------------------------------------------------------------
# Hybrid search — vector + lexical via Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


async def hybrid_search(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    k: int = 5,
    document_ids: list[str] | None = None,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """
    Retrieve chunks by combining vector similarity and full-text search
    using Reciprocal Rank Fusion (RRF).

    Each retrieval arm produces a ranked list; the RRF score for a chunk at
    rank r (0-indexed) is  1 / (rrf_k + r + 1).  Scores from both arms are
    summed, then the top-k results are returned.

    Args:
        query:           Raw natural-language query (used for FTS).
        query_embedding: 384-dim embedding vector (used for ANN search).
        k:               Final number of chunks to return.
        document_ids:    Optional document filter.
        rrf_k:           RRF smoothing constant (default 60).

    Returns a list of dicts (same shape as similarity_search) with
    ``similarity`` set to the fused RRF score.
    """
    fetch_k = min(k * 3, 50)  # over-fetch so fusion has enough candidates

    vector_hits = await similarity_search(
        session, query_embedding, k=fetch_k, document_ids=document_ids
    )
    lexical_hits = await lexical_search(
        session, query, k=fetch_k, document_ids=document_ids
    )

    logger.debug(
        "hybrid_search: vector=%d hits, lexical=%d hits",
        len(vector_hits),
        len(lexical_hits),
    )

    # RRF: accumulate 1/(rrf_k + rank + 1) from each arm
    scores: dict[str, float] = {}
    chunk_data: dict[str, dict[str, Any]] = {}

    for rank, chunk in enumerate(vector_hits):
        key = f"{chunk['document_id']}:{chunk['chunk_index']}"
        scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
        chunk_data[key] = chunk  # prefer vector hit data (has raw similarity)

    for rank, chunk in enumerate(lexical_hits):
        key = f"{chunk['document_id']}:{chunk['chunk_index']}"
        scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
        if key not in chunk_data:
            chunk_data[key] = chunk

    top_keys = sorted(scores, key=lambda ck: scores[ck], reverse=True)[:k]

    fused: list[dict[str, Any]] = []
    for key in top_keys:
        entry = {**chunk_data[key], "score": round(scores[key], 6)}
        fused.append(entry)

    logger.info(
        "hybrid_search returning %d fused chunks (vector=%d, lexical=%d)",
        len(fused),
        len(vector_hits),
        len(lexical_hits),
    )
    return fused
