"""
Pydantic v2 request / response models for the DocMind API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Document schemas
# ---------------------------------------------------------------------------


class DocumentResponse(BaseModel):
    id: str
    filename: str
    file_type: str
    page_count: int
    uploaded_at: datetime
    chunk_count: int | None = None
    file_key: str | None = None
    file_url: str | None = None
    status: str = "ready"
    stage: str | None = None
    error: str | None = None
    summary: str | None = None

    model_config = {"from_attributes": True}


class DocumentStatusResponse(BaseModel):
    document_id: str
    filename: str
    status: str            # processing | ready | failed
    stage: str | None      # queued | parsing | structuring | embedding | summarizing
    error: str | None = None
    page_count: int
    section_count: int
    chunk_count: int
    summary: str | None = None


class SectionOutline(BaseModel):
    section_id: str
    parent_id: str | None
    ordinal: int
    level: int
    title: str
    path: str
    page_start: int
    page_end: int
    summary: str | None = None
    body_chars: int = 0


class DocumentOutlineResponse(BaseModel):
    document_id: str
    filename: str
    summary: str | None = None
    sections: list[SectionOutline]


class PageResponse(BaseModel):
    document_id: str
    page: int
    text: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]


class UploadResponse(BaseModel):
    document_id: str
    filename: str
    chunk_count: int
    status: str = "processing"
    file_key: str | None = None
    file_url: str | None = None


# ---------------------------------------------------------------------------
# Chunk schemas
# ---------------------------------------------------------------------------


class ChunkResponse(BaseModel):
    chunk_index: int
    text: str
    start_char: int
    end_char: int
    page_start: int | None = None
    page_end: int | None = None
    section_path: str | None = None

    model_config = {"from_attributes": True}


class DocumentChunksResponse(BaseModel):
    document_id: str
    filename: str
    chunks: list[ChunkResponse]


# ---------------------------------------------------------------------------
# Query schemas
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The natural-language question")
    document_ids: list[str] | None = Field(
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
    chunk_index: int
    text: str
    score: float
    document_id: str
    filename: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceChunk]
    latency_ms: float
