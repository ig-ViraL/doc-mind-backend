"""
Async Postgres/pgvector access: documents, pages, sections, chunks, and search.

All functions take an AsyncSession and never commit; the caller owns the transaction.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import String, bindparam, delete, func, literal_column, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import Chunk, Document, Page, Section

logger = logging.getLogger(__name__)

# Columns every search hit carries (so the agent can cite pages and sections).
_HIT_COLUMNS = (
    Chunk.id.label("chunk_id"),
    Chunk.chunk_index,
    Chunk.text,
    Chunk.header,
    Chunk.section_id,
    Chunk.section_path,
    Chunk.page_start,
    Chunk.page_end,
    Chunk.document_id,
    Document.filename,
)


def _hit(row: Any, **extra: Any) -> dict[str, Any]:
    return {
        "chunk_id": str(row.chunk_id),
        "chunk_index": row.chunk_index,
        "text": row.text,
        "header": row.header,
        "section_id": str(row.section_id) if row.section_id else None,
        "section_path": row.section_path,
        "page_start": row.page_start,
        "page_end": row.page_end,
        "document_id": str(row.document_id),
        "filename": row.filename,
        **extra,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def save_document(
    session: AsyncSession,
    filename: str,
    file_type: str,
    page_count: int,
    file_key: str | None = None,
    file_url: str | None = None,
    status: str = "ready",
    stage: str | None = None,
) -> Document:
    doc = Document(
        id=str(uuid.uuid4()),
        filename=filename,
        file_type=file_type,
        page_count=page_count,
        file_key=file_key,
        file_url=file_url,
        status=status,
        stage=stage,
    )
    session.add(doc)
    await session.flush()
    logger.info("Saved document id=%s filename='%s'", doc.id, doc.filename)
    return doc


async def update_document(session: AsyncSession, document_id: str, **values: Any) -> None:
    await session.execute(update(Document).where(Document.id == document_id).values(**values))


async def save_pages(session: AsyncSession, document_id: str, pages: list[str]) -> None:
    await session.execute(delete(Page).where(Page.document_id == document_id))
    session.add_all(
        Page(id=str(uuid.uuid4()), document_id=document_id, page_number=i, text=t)
        for i, t in enumerate(pages, start=1)
    )
    await session.flush()


async def save_sections(
    session: AsyncSession,
    document_id: str,
    sections: list[dict[str, Any]],
) -> dict[int, str]:
    """Insert sections (dicts with ordinal/parent ordinal/...) and return {ordinal: id}."""
    ids = {s["ordinal"]: str(uuid.uuid4()) for s in sections}
    # Parents always precede children in document order, so one ordered insert is FK-safe.
    for s in sorted(sections, key=lambda x: x["ordinal"]):
        session.add(Section(
            id=ids[s["ordinal"]],
            document_id=document_id,
            parent_id=ids.get(s["parent"]) if s["parent"] is not None else None,
            ordinal=s["ordinal"],
            level=s["level"],
            title=s["title"],
            path=s["path"],
            page_start=s["page_start"],
            page_end=s["page_end"],
            body=s["body"],
            summary=s.get("summary") or None,
        ))
        await session.flush()
    return ids


async def set_section_summaries(session: AsyncSession, summaries: dict[str, str]) -> None:
    for section_id, summary in summaries.items():
        if summary:
            await session.execute(update(Section).where(Section.id == section_id).values(summary=summary))


async def save_chunks(
    session: AsyncSession,
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
    document_id: str,
) -> int:
    if len(chunks) != len(embeddings):
        raise ValueError(f"Mismatch: {len(chunks)} chunks vs {len(embeddings)} embeddings")

    session.add_all(
        Chunk(
            id=str(uuid.uuid4()),
            document_id=document_id,
            chunk_index=c["chunk_index"],
            text=c["text"],
            embedding=emb,
            start_char=c.get("start_char", 0),
            end_char=c.get("end_char", 0),
            page_start=c.get("page_start"),
            page_end=c.get("page_end"),
            section_id=c.get("section_id"),
            section_path=c.get("section_path"),
            header=c.get("header"),
        )
        for c, emb in zip(chunks, embeddings)
    )
    await session.flush()
    logger.info("Saved %d chunks for document_id=%s", len(chunks), document_id)
    return len(chunks)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def similarity_search(
    session: AsyncSession,
    query_embedding: list[float],
    k: int = 5,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Top-k chunks by cosine similarity (HNSW)."""
    from config import settings

    distance = Chunk.embedding.cosine_distance(query_embedding)
    # HNSW only returns ef_search candidates; keep it above the requested k.
    ef_search = max(int(settings.hnsw_ef_search), k)
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))

    stmt = (
        select(*_HIT_COLUMNS, (1 - distance).label("similarity"))
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.embedding.isnot(None))
        .order_by(distance)
        .limit(k)
    )
    if document_ids:
        stmt = stmt.where(Chunk.document_id.in_(document_ids))

    rows = (await session.execute(stmt)).fetchall()
    return [_hit(r, similarity=float(r.similarity)) for r in rows]


async def lexical_search(
    session: AsyncSession,
    query: str,
    k: int = 5,
    document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Postgres full-text search (websearch_to_tsquery + ts_rank_cd)."""
    lang = literal_column("'english'")
    tsvec = func.to_tsvector(lang, Chunk.text)
    tsquery = func.websearch_to_tsquery(lang, bindparam("fts_query", value=query, type_=String))
    rank_col = func.ts_rank_cd(tsvec, tsquery).label("fts_rank")

    stmt = (
        select(*_HIT_COLUMNS, rank_col)
        .join(Document, Chunk.document_id == Document.id)
        .where(tsvec.op("@@")(tsquery))
        .order_by(rank_col.desc())
        .limit(k)
    )
    if document_ids:
        stmt = stmt.where(Chunk.document_id.in_(document_ids))

    try:
        # Savepoint: a bad tsquery must not abort the caller's transaction.
        async with session.begin_nested():
            rows = (await session.execute(stmt)).fetchall()
    except Exception as exc:
        logger.warning("lexical_search failed (%s) — returning empty result", exc)
        return []
    return [_hit(r, fts_rank=float(r.fts_rank)) for r in rows]


async def hybrid_search(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    k: int = 5,
    document_ids: list[str] | None = None,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Vector + full-text search fused with Reciprocal Rank Fusion: score = Σ 1/(rrf_k + rank + 1)."""
    fetch_k = min(k * 2, 200)
    vector_hits = await similarity_search(session, query_embedding, k=fetch_k, document_ids=document_ids)
    lexical_hits = await lexical_search(session, query, k=fetch_k, document_ids=document_ids)

    scores: dict[str, float] = {}
    data: dict[str, dict[str, Any]] = {}
    for hits in (vector_hits, lexical_hits):
        for rank, hit in enumerate(hits):
            key = hit["chunk_id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
            data.setdefault(key, hit)

    top = sorted(scores, key=scores.__getitem__, reverse=True)[:k]
    logger.info(
        "hybrid_search: %d fused (vector=%d, lexical=%d)", len(top), len(vector_hits), len(lexical_hits)
    )
    return [{**data[key], "score": round(scores[key], 6)} for key in top]


# ---------------------------------------------------------------------------
# Structure reads (used by the agent tools and the API)
# ---------------------------------------------------------------------------


async def get_documents_brief(
    session: AsyncSession, document_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    stmt = select(
        Document.id, Document.filename, Document.page_count, Document.summary,
        Document.status, Document.has_real_pages,
    ).order_by(Document.uploaded_at)
    if document_ids:
        stmt = stmt.where(Document.id.in_(document_ids))
    rows = (await session.execute(stmt)).fetchall()
    return [
        {
            "document_id": str(r.id), "filename": r.filename, "page_count": r.page_count,
            "summary": r.summary, "status": r.status, "has_real_pages": r.has_real_pages,
        }
        for r in rows
    ]


async def get_outline(session: AsyncSession, document_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(
            Section.id, Section.parent_id, Section.ordinal, Section.level, Section.title,
            Section.path, Section.page_start, Section.page_end, Section.summary,
            func.length(Section.body).label("body_chars"),
        )
        .where(Section.document_id == document_id)
        .order_by(Section.ordinal)
    )
    rows = (await session.execute(stmt)).fetchall()
    return [
        {
            "section_id": str(r.id), "parent_id": str(r.parent_id) if r.parent_id else None,
            "ordinal": r.ordinal, "level": r.level, "title": r.title, "path": r.path,
            "page_start": r.page_start, "page_end": r.page_end, "summary": r.summary,
            "body_chars": r.body_chars or 0,
        }
        for r in rows
    ]


async def read_section(session: AsyncSession, section_id: str) -> dict[str, Any] | None:
    """A section with its descendants' bodies concatenated in document order."""
    root = (await session.execute(select(Section).where(Section.id == section_id))).scalar_one_or_none()
    if root is None:
        return None
    rows = (await session.execute(
        select(Section).where(Section.document_id == root.document_id).order_by(Section.ordinal)
    )).scalars().all()
    keep = {root.id}
    bodies: list[str] = []
    page_end = root.page_end
    for s in rows:
        if s.ordinal < root.ordinal:
            continue
        if s.id != root.id and s.parent_id not in keep:
            break  # left the subtree (sections are in document order)
        keep.add(s.id)
        bodies.append(s.body)
        page_end = max(page_end, s.page_end)
    filename = (await session.execute(
        select(Document.filename).where(Document.id == root.document_id)
    )).scalar_one()
    return {
        "section_id": str(root.id), "document_id": str(root.document_id), "filename": filename,
        "path": root.path, "page_start": root.page_start, "page_end": page_end,
        "text": "\n\n".join(b for b in bodies if b.strip()),
    }


async def read_pages(
    session: AsyncSession, document_id: str, start: int, end: int
) -> list[dict[str, Any]]:
    stmt = (
        select(Page.page_number, Page.text, Document.filename)
        .join(Document, Document.id == Page.document_id)
        .where(Page.document_id == document_id, Page.page_number.between(start, end))
        .order_by(Page.page_number)
    )
    rows = (await session.execute(stmt)).fetchall()
    return [{"page": r.page_number, "text": r.text, "filename": r.filename, "document_id": document_id} for r in rows]


async def keyword_scan(
    session: AsyncSession,
    terms: list[str],
    document_ids: list[str] | None = None,
    limit: int = 20,
    window: int = 220,
) -> list[dict[str, Any]]:
    """Exact (case-insensitive) term matches across pages, with a snippet around each hit."""
    terms = [t.strip() for t in terms if t and t.strip()][:8]
    if not terms:
        return []
    def _like(t: str) -> str:
        return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    conds = [Page.text.ilike(_like(t), escape="\\") for t in terms]
    stmt = (
        select(Page.document_id, Page.page_number, Page.text, Document.filename)
        .join(Document, Document.id == Page.document_id)
        .where(or_(*conds))
        .order_by(Document.uploaded_at, Page.page_number)
        .limit(limit * 3)
    )
    if document_ids:
        stmt = stmt.where(Page.document_id.in_(document_ids))
    rows = (await session.execute(stmt)).fetchall()

    hits: list[dict[str, Any]] = []
    for r in rows:
        for t in terms:
            m = re.search(re.escape(t), r.text, re.IGNORECASE)
            if m:
                a, b = max(0, m.start() - window), min(len(r.text), m.end() + window)
                hits.append({
                    "document_id": str(r.document_id), "filename": r.filename, "page": r.page_number,
                    "term": t, "snippet": r.text[a:b].strip(),
                })
        if len(hits) >= limit:
            break
    return hits[:limit]


# ---------------------------------------------------------------------------
# Document listing / deletion
# ---------------------------------------------------------------------------


async def get_all_documents(session: AsyncSession) -> list[dict[str, Any]]:
    stmt = (
        select(
            Document.id, Document.filename, Document.file_type, Document.page_count,
            Document.uploaded_at, Document.file_key, Document.file_url, Document.status,
            Document.stage, Document.error, Document.summary,
            func.count(Chunk.id).label("chunk_count"),
        )
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        .group_by(Document.id)
        .order_by(Document.uploaded_at.desc())
    )
    rows = (await session.execute(stmt)).fetchall()
    return [dict(r._mapping) for r in rows]


async def get_document_status(session: AsyncSession, document_id: str) -> dict[str, Any] | None:
    doc = (await session.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None:
        return None
    chunk_count = (await session.execute(
        select(func.count(Chunk.id)).where(Chunk.document_id == document_id)
    )).scalar_one()
    section_count = (await session.execute(
        select(func.count(Section.id)).where(Section.document_id == document_id)
    )).scalar_one()
    return {
        "document_id": str(doc.id), "filename": doc.filename, "status": doc.status, "stage": doc.stage,
        "error": doc.error, "page_count": doc.page_count, "section_count": section_count,
        "chunk_count": chunk_count, "summary": doc.summary,
    }


async def get_document_chunks(
    session: AsyncSession,
    document_id: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    filename = (await session.execute(
        select(Document.filename).where(Document.id == document_id)
    )).scalar_one_or_none()
    if filename is None:
        return None, []
    rows = (await session.execute(
        select(
            Chunk.chunk_index, Chunk.text, Chunk.start_char, Chunk.end_char,
            Chunk.page_start, Chunk.page_end, Chunk.section_path,
        )
        .where(Chunk.document_id == document_id)
        .order_by(Chunk.chunk_index)
    )).fetchall()
    return filename, [dict(r._mapping) for r in rows]


async def delete_document(session: AsyncSession, document_id: str) -> Document | None:
    """Delete a document; pages, sections and chunks cascade. Returns the deleted row."""
    doc = (await session.execute(select(Document).where(Document.id == document_id))).scalar_one_or_none()
    if doc is None:
        return None
    await session.delete(doc)
    await session.flush()
    logger.info("Deleted document id=%s", document_id)
    return doc


async def fail_interrupted(session: AsyncSession) -> int:
    """Mark documents left in 'processing' by a restart as failed."""
    result = await session.execute(
        update(Document)
        .where(Document.status == "processing")
        .values(status="failed", error="Indexing was interrupted by a server restart. Upload again.")
    )
    return result.rowcount or 0
