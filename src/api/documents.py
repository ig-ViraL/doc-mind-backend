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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

_chunker = chunker_module.RecursiveCharacterChunker()


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

    # 1. Parse
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

    # 2. Save document metadata
    doc = await vector_store.save_document(
        session=session,
        filename=metadata["filename"],
        file_type=metadata["file_type"],
        page_count=metadata["page_count"],
    )

    # 3. Chunk
    chunks = _chunker.chunk(text=text, document_id=doc.id)
    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Document produced zero chunks after splitting.",
        )

    # 4. Embed (batch)
    texts_to_embed = [c["text"] for c in chunks]
    embeddings = await embedder.embed(texts_to_embed)

    # 5. Store chunks + embeddings
    chunk_count = await vector_store.save_chunks(
        session=session,
        chunks=chunks,
        embeddings=embeddings,
        document_id=doc.id,
    )

    logger.info(
        "Document indexed: id=%s filename='%s' chunks=%d",
        doc.id,
        doc.filename,
        chunk_count,
    )

    return UploadResponse(
        document_id=doc.id,
        filename=doc.filename,
        chunk_count=chunk_count,
        status="indexed",
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
    deleted = await vector_store.delete_document(session, document_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found.",
        )
    logger.info("Deleted document id=%s", document_id)
