"""
Document management endpoints.

Routes (mounted at /api by main.py):
  POST   /documents/upload                    — Upload + index a document
  GET    /documents                           — List all documents with chunk counts
  GET    /documents/{document_id}/chunks      — List all chunks for a document
  DELETE /documents/{document_id}             — Delete document and its chunks
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
    DocumentResponse,
    UploadResponse,
)
from services import chunker as chunker_module
from services.embedder import embedder
from services import parser as parser_module
from services import vector_store
from services.storage import r2_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

_chunker = chunker_module.TokenChunker(
    chunk_size=settings.chunk_size,
    chunk_overlap=settings.chunk_overlap,
    token_counter=embedder.count_tokens,
)


# ---------------------------------------------------------------------------
# POST /documents/upload
# ---------------------------------------------------------------------------


@router.post(
    "/documents/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and index a document",
)
async def upload_document(
    file: UploadFile = File(..., description="PDF, DOCX, or TXT file to index"),
    session: AsyncSession = Depends(get_db),
) -> UploadResponse:
    """
    Parse the uploaded file, chunk it, embed each chunk, and store everything
    in the database. Returns the new document ID and indexing statistics.
    """
    logger.info("Upload request: filename='%s' content_type='%s'", file.filename, file.content_type)

    # 1. Read bytes upfront (needed for R2 upload; seek back so parser can read normally)
    file_bytes = await file.read()
    await file.seek(0)

    # 2. Parse
    try:
        parsed = await parser_module.parse(file)
    except ValueError as exc:
        logger.warning("Parse error for '%s': %s", file.filename, exc)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    text: str = parsed["text"]
    metadata: dict = parsed["metadata"]

    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file contains no extractable text.",
        )

    # 3. Upload original file to Cloudflare R2 (best-effort — indexing continues on failure)
    file_key: str | None = None
    file_url: str | None = None
    if r2_storage.is_enabled():
        try:
            file_key, file_url = await r2_storage.upload_file(
                content=file_bytes,
                filename=metadata["filename"],
                mime_type=file.content_type or "application/octet-stream",
            )
        except Exception as exc:
            logger.warning("R2 upload failed (non-fatal): %s", exc)

    # 4. Save document metadata
    doc = await vector_store.save_document(
        session=session,
        filename=metadata["filename"],
        file_type=metadata["file_type"],
        page_count=metadata["page_count"],
        file_key=file_key,
        file_url=file_url,
    )

    # 5. Chunk
    chunks = _chunker.chunk(text=text, document_id=doc.id)
    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Document produced zero chunks after splitting.",
        )

    # 6. Embed (batch)
    texts_to_embed = [c["text"] for c in chunks]
    embeddings = await embedder.embed(texts_to_embed)

    # 7. Store chunks + embeddings
    chunk_count = await vector_store.save_chunks(
        session=session,
        chunks=chunks,
        embeddings=embeddings,
        document_id=doc.id,
    )

    logger.info(
        "Document indexed: id=%s filename='%s' chunks=%d r2=%s",
        doc.id, doc.filename, chunk_count, file_key or "none",
    )

    return UploadResponse(
        document_id=doc.id,
        filename=doc.filename,
        chunk_count=chunk_count,
        status="indexed",
        file_key=file_key,
        file_url=file_url,
    )


# ---------------------------------------------------------------------------
# GET /documents
# ---------------------------------------------------------------------------


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List all indexed documents",
)
async def list_documents(
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    """Return all documents with their chunk counts, newest first."""
    docs = await vector_store.get_all_documents(session)
    return DocumentListResponse(
        documents=[DocumentResponse(**d) for d in docs]
    )


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/chunks
# ---------------------------------------------------------------------------


@router.get(
    "/documents/{document_id}/chunks",
    response_model=DocumentChunksResponse,
    summary="List all chunks for a document",
)
async def get_document_chunks(
    document_id: str,
    session: AsyncSession = Depends(get_db),
) -> DocumentChunksResponse:
    """Return every chunk stored for the given document, ordered by chunk_index."""
    filename, chunks = await vector_store.get_document_chunks(session, document_id)
    if filename is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found.",
        )
    return DocumentChunksResponse(
        document_id=document_id,
        filename=filename,
        chunks=[ChunkResponse(**c) for c in chunks],
    )


# ---------------------------------------------------------------------------
# DELETE /documents/{document_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and all its chunks",
)
async def delete_document(
    document_id: str,
    session: AsyncSession = Depends(get_db),
) -> None:
    """
    Permanently delete the document and all associated chunks.
    Returns 204 on success, 404 if the document does not exist.
    """
    doc = await vector_store.delete_document(session, document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found.",
        )

    # Delete original file from R2 (best-effort)
    if doc.file_key and r2_storage.is_enabled():
        try:
            await r2_storage.delete_file(doc.file_key)
        except Exception as exc:
            logger.warning("R2 delete failed for key %s: %s", doc.file_key, exc)

    logger.info("Deleted document id=%s", document_id)
