"""
Pydantic v2 request / response models for the DocMind API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Document schemas
# ---------------------------------------------------------------------------


class DocumentResponse(BaseModel):
    """Single document representation returned to callers."""

    id: str
    filename: str
    file_type: str
    page_count: int
    uploaded_at: datetime
    chunk_count: Optional[int] = None

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    """Paginated (or full) list of documents."""

    documents: list[DocumentResponse]


class UploadResponse(BaseModel):
    """Returned after a successful document upload + indexing."""

    document_id: str
    filename: str
    chunk_count: int
    status: str = "indexed"


# ---------------------------------------------------------------------------
# Chunk schemas
# ---------------------------------------------------------------------------


class ChunkResponse(BaseModel):
    chunk_index: int
    text: str
    start_char: int
    end_char: int

    model_config = {"from_attributes": True}


class DocumentChunksResponse(BaseModel):
    document_id: str
    filename: str
    chunks: list[ChunkResponse]


# ---------------------------------------------------------------------------
# Query schemas
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    """Incoming RAG query."""

    query: str = Field(..., min_length=1, description="The natural-language question")
    document_ids: Optional[list[str]] = Field(
        default=None,
        description="Restrict search to these document IDs. None = search all.",
    )
    k: int = Field(
        default=5,
        ge=1,
        le=10,
        description=(
            "Number of top-ranked chunks sent to the LLM. "
            "The pipeline always retrieves a larger candidate pool internally "
            "and reranks before truncating to this value."
        ),
    )


class SourceChunk(BaseModel):
    """A single retrieved chunk included in the query response."""

    chunk_index: int
    text: str
    score: float
    document_id: str
    filename: str


class QueryResponse(BaseModel):
    """Full answer with cited source chunks and observability data."""

    answer: str
    sources: list[SourceChunk]
    latency_ms: float
