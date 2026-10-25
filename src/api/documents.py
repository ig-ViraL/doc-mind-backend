"""
Document management endpoints (mounted at /api).

  POST   /documents/upload                   — store the file, index it in the background (202)
  GET    /documents                          — list documents with status and chunk counts
  GET    /documents/{id}/status              — indexing progress (stage, counts, error)
  GET    /documents/{id}/outline             — section tree with page ranges and summaries
  GET    /documents/{id}/pages/{page}        — raw text of one page
  GET    /documents/{id}/chunks              — all chunks
  DELETE /documents/{id}                     — delete document, pages, sections and chunks
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.database import get_db
from models.schemas import (
    ChunkResponse,
    DocumentChunksResponse,
    DocumentListResponse,
    DocumentOutlineResponse,
    DocumentResponse,
    DocumentStatusResponse,
    PageResponse,
    SectionOutline,
    UploadResponse,
)
from services import indexer, vector_store
from services.storage import r2_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


def _not_found(document_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document '{document_id}' not found.")


@router.post(
    "/documents/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document and index it in the background",
)
async def upload_document(
    file: UploadFile = File(..., description="PDF, DOCX, TXT or MD file to index"),
    session: AsyncSession = Depends(get_db),
) -> UploadResponse:
    """
    Stores the file and returns immediately with ``status="processing"``.
    Poll ``GET /api/documents/{id}/status`` until it is ``ready`` (or ``failed``).
    """
    from db import database

    if database.embedding_dim_mismatch:
        current, expected = database.embedding_dim_mismatch
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Stored vectors are {current} but the model expects {expected}. "
            "Run `python scripts/reindex.py` first.",
        )

    filename = file.filename or "unknown"
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty file.")
    if len(raw_bytes) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.max_file_size_mb} MB limit.",
        )

    file_key: str | None = None
    file_url: str | None = None
    if r2_storage.is_enabled():
        try:
            file_key, file_url = await r2_storage.upload_file(
                content=raw_bytes, filename=filename,
                mime_type=file.content_type or "application/octet-stream",
            )
        except Exception as exc:
            logger.warning("R2 upload failed (non-fatal): %s", exc)

    doc = await vector_store.save_document(
        session=session,
        filename=filename,
        file_type=filename.rsplit(".", 1)[-1].lower() if "." in filename else "unknown",
        page_count=0,
        file_key=file_key,
        file_url=file_url,
        status="processing",
        stage="queued",
    )
    # Commit now so the background task (own session) sees the row.
    await session.commit()
    indexer.schedule(doc.id, raw_bytes, filename, file.content_type or "")

    return UploadResponse(
        document_id=doc.id, filename=doc.filename, chunk_count=0, status="processing",
        file_key=file_key, file_url=file_url,
    )


@router.get("/documents", response_model=DocumentListResponse, summary="List all documents")
async def list_documents(session: AsyncSession = Depends(get_db)) -> DocumentListResponse:
    docs = await vector_store.get_all_documents(session)
    return DocumentListResponse(documents=[DocumentResponse(**d) for d in docs])


@router.get(
    "/documents/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Indexing progress for a document",
)
async def document_status(document_id: str, session: AsyncSession = Depends(get_db)) -> DocumentStatusResponse:
    info = await vector_store.get_document_status(session, document_id)
    if info is None:
        raise _not_found(document_id)
    return DocumentStatusResponse(**info)


@router.get(
    "/documents/{document_id}/outline",
    response_model=DocumentOutlineResponse,
    summary="Section tree with page ranges and summaries",
)
async def document_outline(document_id: str, session: AsyncSession = Depends(get_db)) -> DocumentOutlineResponse:
    info = await vector_store.get_document_status(session, document_id)
    if info is None:
        raise _not_found(document_id)
    outline = await vector_store.get_outline(session, document_id)
    return DocumentOutlineResponse(
        document_id=document_id, filename=info["filename"], summary=info["summary"],
        sections=[SectionOutline(**s) for s in outline],
    )


@router.get(
    "/documents/{document_id}/pages/{page}",
    response_model=PageResponse,
    summary="Text of one page",
)
async def document_page(document_id: str, page: int, session: AsyncSession = Depends(get_db)) -> PageResponse:
    pages = await vector_store.read_pages(session, document_id, page, page)
    if not pages:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found.")
    return PageResponse(document_id=document_id, page=page, text=pages[0]["text"])


@router.get(
    "/documents/{document_id}/chunks",
    response_model=DocumentChunksResponse,
    summary="List all chunks for a document",
)
async def get_document_chunks(document_id: str, session: AsyncSession = Depends(get_db)) -> DocumentChunksResponse:
    filename, chunks = await vector_store.get_document_chunks(session, document_id)
    if filename is None:
        raise _not_found(document_id)
    return DocumentChunksResponse(
        document_id=document_id, filename=filename, chunks=[ChunkResponse(**c) for c in chunks]
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and everything derived from it",
)
async def delete_document(document_id: str, session: AsyncSession = Depends(get_db)) -> None:
    doc = await vector_store.delete_document(session, document_id)
    if not doc:
        raise _not_found(document_id)
    if doc.file_key and r2_storage.is_enabled():
        try:
            await r2_storage.delete_file(doc.file_key)
        except Exception as exc:
            logger.warning("R2 delete failed for key %s: %s", doc.file_key, exc)
