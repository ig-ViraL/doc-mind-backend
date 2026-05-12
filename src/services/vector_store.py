"""
Async pgvector CRUD operations and similarity search.

All functions accept an AsyncSession and perform fully async DB operations.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, literal_column, select, text
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
) -> Document:
    """
    Insert a new Document row and return the ORM object.
    """
    doc = Document(
        id=str(uuid.uuid4()),
        filename=filename,
        file_type=file_type,
        page_count=page_count,
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
                "chunk_count": row.chunk_count,
            }
        )

    logger.debug("get_all_documents returned %d documents", len(docs))
    return docs


async def delete_document(session: AsyncSession, document_id: str) -> bool:
    """
    Delete a document and all its chunks (cascaded by FK constraint).

    Returns True if the document existed and was deleted, False otherwise.
    """
    # Check existence first
    stmt = select(Document).where(Document.id == document_id)
    result = await session.execute(stmt)
    doc = result.scalar_one_or_none()

    if doc is None:
        logger.warning("delete_document: document_id=%s not found", document_id)
        return False

    await session.delete(doc)
    await session.flush()
    logger.info("Deleted document id=%s and its chunks", document_id)
    return True
