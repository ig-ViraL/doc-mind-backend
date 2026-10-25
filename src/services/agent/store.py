"""
What the agent can read. ``DocStore`` is the interface the tools use; ``PgStore``
implements it on Postgres for one request, scoped to the requested documents.
Tests use an in-memory fake with the same methods.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from services import retrieval, vector_store


class DocStore(Protocol):
    async def documents(self) -> list[dict[str, Any]]: ...
    async def outline(self, document_id: str) -> list[dict[str, Any]]: ...
    async def search(self, query: str, k: int) -> list[dict[str, Any]]: ...
    async def keyword_scan(self, terms: list[str], limit: int) -> list[dict[str, Any]]: ...
    async def read_pages(self, document_id: str, start: int, end: int) -> list[dict[str, Any]]: ...
    async def read_section(self, section_id: str) -> dict[str, Any] | None: ...
    async def section_bodies(self) -> list[dict[str, Any]]: ...


class PgStore:
    def __init__(self, session: AsyncSession, document_ids: list[str] | None) -> None:
        self.session = session
        self.document_ids = document_ids or None
        self._docs: list[dict[str, Any]] | None = None

    async def documents(self) -> list[dict[str, Any]]:
        if self._docs is None:
            self._docs = [
                d for d in await vector_store.get_documents_brief(self.session, self.document_ids)
                if d["status"] != "failed"
            ]
        return self._docs

    async def _allowed(self, document_id: str) -> bool:
        return any(d["document_id"] == document_id for d in await self.documents())

    async def outline(self, document_id: str) -> list[dict[str, Any]]:
        if not await self._allowed(document_id):
            return []
        return await vector_store.get_outline(self.session, document_id)

    async def search(self, query: str, k: int) -> list[dict[str, Any]]:
        return await retrieval.retrieve(self.session, query, self.document_ids, k)

    async def keyword_scan(self, terms: list[str], limit: int) -> list[dict[str, Any]]:
        return await vector_store.keyword_scan(self.session, terms, self.document_ids, limit=limit)

    async def read_pages(self, document_id: str, start: int, end: int) -> list[dict[str, Any]]:
        if not await self._allowed(document_id):
            return []
        return await vector_store.read_pages(self.session, document_id, start, end)

    async def read_section(self, section_id: str) -> dict[str, Any] | None:
        sec = await vector_store.read_section(self.session, section_id)
        if sec is None or not await self._allowed(sec["document_id"]):
            return None
        return sec

    async def section_bodies(self) -> list[dict[str, Any]]:
        """Every section's own body, in document order, for the exhaustive map step."""
        from sqlalchemy import select

        from db.database import Section

        out: list[dict[str, Any]] = []
        for doc in await self.documents():
            rows = (await self.session.execute(
                select(Section.path, Section.page_start, Section.page_end, Section.body)
                .where(Section.document_id == doc["document_id"])
                .order_by(Section.ordinal)
            )).fetchall()
            out += [
                {
                    "document_id": doc["document_id"], "filename": doc["filename"], "path": r.path,
                    "page_start": r.page_start, "page_end": r.page_end, "body": r.body,
                }
                for r in rows
            ]
        return out
