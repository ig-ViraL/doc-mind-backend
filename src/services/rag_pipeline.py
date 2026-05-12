"""
RAG pipeline orchestration.

Flow:
  1. Embed the query
  2. Similarity search (top-K chunks)
  3. Optional cross-encoder reranking
  4. Build context string
  5. Call Claude API
  6. Return QueryResponse with answer, cited sources, latency_ms
"""

from __future__ import annotations

import logging
import time
from typing import Any

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.schemas import QueryResponse, SourceChunk
from services.embedder import embedder
from services.vector_store import similarity_search

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenRouter client (module-level singleton)
# ---------------------------------------------------------------------------

_openrouter_client: openai.AsyncOpenAI | None = None


def _get_openrouter_client() -> openai.AsyncOpenAI:
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = openai.AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            default_headers={
                "HTTP-Referer": "https://github.com/docmind",
                "X-Title": "DocMind RAG",
            },
        )
    return _openrouter_client


# ---------------------------------------------------------------------------
# Cross-encoder reranker (lazy-loaded)
# ---------------------------------------------------------------------------

_cross_encoder: Any | None = None
_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def _get_cross_encoder() -> Any:
    global _cross_encoder
    if _cross_encoder is None:
        try:
            from sentence_transformers import CrossEncoder

            logger.info("Loading cross-encoder model: %s", _CROSS_ENCODER_MODEL)
            _cross_encoder = CrossEncoder(_CROSS_ENCODER_MODEL)
            logger.info("Cross-encoder model loaded")
        except Exception as exc:
            logger.warning("Failed to load cross-encoder (%s) — reranking disabled", exc)
            _cross_encoder = None
    return _cross_encoder


def _rerank(query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rerank *chunks* using a cross-encoder model.
    Returns chunks sorted by descending cross-encoder score.
    If the cross-encoder is unavailable, returns chunks unchanged.
    """
    cross_encoder = _get_cross_encoder()
    if cross_encoder is None or not chunks:
        return chunks

    try:
        pairs = [(query, c["text"]) for c in chunks]
        scores: list[float] = cross_encoder.predict(pairs).tolist()
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        reranked = [chunk for _, chunk in ranked]
        logger.debug("Reranked %d chunks", len(reranked))
        return reranked
    except Exception as exc:
        logger.warning("Cross-encoder reranking failed: %s — using original order", exc)
        return chunks


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a helpful document assistant. Answer questions using the provided context chunks.\n"
    "You may synthesize and infer from the context — do not require the answer to be stated word-for-word.\n"
    "If the context contains no relevant information at all, say \"I don't have enough information in the provided documents.\"\n"
    "Always cite the source chunk index when referencing information (e.g., [Chunk 3]).\n"
    "Be concise and factual."
)


# ---------------------------------------------------------------------------
# Main pipeline function
# ---------------------------------------------------------------------------


async def answer_query(
    query: str,
    document_ids: list[str] | None,
    k: int,
    session: AsyncSession,
) -> QueryResponse:
    """
    Run the full RAG pipeline for a user query.

    Args:
        query:        Natural-language question from the user.
        document_ids: Optional list of document UUIDs to restrict search.
        k:            Number of chunks to retrieve.
        session:      Active async DB session.

    Returns:
        QueryResponse with answer, cited source chunks, and latency_ms.
    """
    start_time = time.perf_counter()

    # ------------------------------------------------------------------
    # 1. Embed the query
    # ------------------------------------------------------------------
    query_vectors = embedder.embed([query])
    query_embedding: list[float] = query_vectors[0]
    logger.debug("Query embedded (%d dims)", len(query_embedding))

    # ------------------------------------------------------------------
    # 2. Similarity search
    # ------------------------------------------------------------------
    raw_chunks = await similarity_search(
        session=session,
        query_embedding=query_embedding,
        k=k,
        document_ids=document_ids,
    )
    logger.info("Retrieved %d chunks for query (k=%d)", len(raw_chunks), k)
    for c in raw_chunks:
        logger.debug("  chunk=%d sim=%.4f file=%s", c["chunk_index"], c["similarity"], c["filename"])

    # ------------------------------------------------------------------
    # 3. Cross-encoder reranking
    # ------------------------------------------------------------------
    ranked_chunks = _rerank(query, raw_chunks)

    # ------------------------------------------------------------------
    # 4. Build context string
    # ------------------------------------------------------------------
    if ranked_chunks:
        context_parts: list[str] = []
        for i, chunk in enumerate(ranked_chunks):
            context_parts.append(
                f"Chunk {i} (from {chunk['filename']}):\n{chunk['text']}"
            )
        context = "\n\n".join(context_parts)
        user_message = f"Context:\n{context}\n\nQuestion: {query}"
    else:
        user_message = f"Question: {query}"
        logger.warning("No chunks retrieved — asking Claude without context")

    # ------------------------------------------------------------------
    # 5. Call LLM via OpenRouter
    # ------------------------------------------------------------------
    client = _get_openrouter_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openrouter_model,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
        answer: str = response.choices[0].message.content or ""
    except openai.APIError as exc:
        logger.exception("OpenRouter API error: %s", exc)
        raise

    # ------------------------------------------------------------------
    # 6. Build response
    # ------------------------------------------------------------------
    latency_ms = (time.perf_counter() - start_time) * 1000

    sources: list[SourceChunk] = [
        SourceChunk(
            chunk_index=chunk["chunk_index"],
            text=chunk["text"],
            similarity=chunk["similarity"],
            document_id=chunk["document_id"],
            filename=chunk["filename"],
        )
        for chunk in ranked_chunks
    ]

    logger.info(
        "answer_query completed: latency=%.1f ms, sources=%d, answer_len=%d",
        latency_ms,
        len(sources),
        len(answer),
    )

    return QueryResponse(
        answer=answer,
        sources=sources,
        latency_ms=round(latency_ms, 2),
    )
