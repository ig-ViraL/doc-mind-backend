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
import re
import time
from typing import Any

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.schemas import QueryResponse, SourceChunk
from services.embedder import embedder
from services.llm_client import get_openrouter_client
from services.vector_store import hybrid_search

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Meta-query detection — bypass retrieval for questions about the assistant
# ---------------------------------------------------------------------------

_META_QUERY_RE = re.compile(
    r"""
    ^\s*(
        (who|what)\s+are\s+you                  |
        what\s+(can|do)\s+you\s+(do|help)       |
        tell\s+me\s+about\s+yourself            |
        introduce\s+yourself                     |
        your\s+(name|purpose|role|capabilities?) |
        what\s+is\s+(your\s+name|docmind)       |
        (hi|hello|hey|howdy)\b                  |
        ^help$
    )\s*[?!.]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DOCMIND_INTRO = (
    "I'm DocMind, an intelligent document assistant. "
    "I answer questions using only the content of documents you upload — "
    "I don't draw on outside knowledge or make things up.\n\n"
    "Here's what I can do:\n"
    "- Answer questions across one or more uploaded documents\n"
    "- Reason accurately over tables and structured data\n"
    "- Understand images and charts via AI-generated descriptions\n"
    "- Recognise hyperlinks referenced in your documents\n"
    "- Synthesise information across multiple files\n\n"
    "Supported formats: PDF, DOCX, TXT.\n\n"
    "Upload a document and ask me anything about its content."
)


def _is_meta_query(query: str) -> bool:
    """Return True if the query is about the assistant itself, not a document."""
    return bool(_META_QUERY_RE.match(query.strip()))


# ---------------------------------------------------------------------------
# Cross-encoder reranker (lazy-loaded)
# ---------------------------------------------------------------------------

_cross_encoder: Any | None = None


def warm_up_cross_encoder() -> None:
    """Pre-load the cross-encoder so the first request isn't slow."""
    _get_cross_encoder()


def _get_cross_encoder() -> Any:
    global _cross_encoder
    if _cross_encoder is None:
        try:
            from sentence_transformers import CrossEncoder

            logger.info("Loading cross-encoder model: %s", settings.cross_encoder_model)
            _cross_encoder = CrossEncoder(settings.cross_encoder_model)
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

_SYSTEM_PROMPT = """\
You are DocMind, an intelligent assistant that answers questions strictly from the content of uploaded documents.

Each excerpt you receive is labelled with its source file. Excerpts may contain:
- Plain text passages
- Tables in Markdown format (rows and columns of data)
- Image descriptions prefixed with [IMAGE] (treat these as factual observations)
- Hyperlinks prefixed with [HYPERLINKS]

How to answer:
- Use only the provided excerpts. Do not use outside knowledge or make up facts.
- Synthesise across multiple excerpts and sources when the answer spans them.
- Cite the source file name in parentheses when referencing a specific fact — e.g. (resume.pdf) — not a chunk or page number.
- When an excerpt contains a table, reason accurately over its values; do not paraphrase incorrectly.
- When an excerpt describes an image or chart, treat that description as ground truth.
- Write naturally and directly. Do not start answers with "Based on the context..." or "According to the documents...".
- Do not expose internal labels like chunk numbers or page numbers unless they are meaningful to the user.

Hard scope limits — never cross these:
- Do NOT write, generate, or produce any content (code, essays, summaries, plans) that is not directly extracted or paraphrased from the uploaded documents.
- Do NOT offer to generate something "for the user" as an alternative when the documents lack the answer.
- Do NOT answer general-knowledge questions, perform calculations, or act as a general-purpose assistant.
- If the uploaded documents happen to contain code, you may explain or quote that code. You may not write new code.

When the excerpts do not fully cover the question:
- Answer what you can from what is available.
- Clearly state that the information is not present in the uploaded documents.
- If helpful, suggest a more specific question that the documents could plausibly answer — only if such a question exists.
- Never offer to generate, write, or produce anything as a substitute.

If the user asks who you are or about your capabilities, always identify yourself as DocMind — \
never as a person or entity described in the documents.\
"""


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
    # 0. Short-circuit: meta / identity queries never touch retrieval
    # ------------------------------------------------------------------
    if _is_meta_query(query):
        logger.info("Meta query detected — skipping retrieval")
        return QueryResponse(
            answer=_DOCMIND_INTRO,
            sources=[],
            latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
        )

    # ------------------------------------------------------------------
    # 1. Embed the query
    # ------------------------------------------------------------------
    query_vectors = await embedder.embed([query], input_type="query")
    query_embedding: list[float] = query_vectors[0]
    logger.debug("Query embedded (%d dims)", len(query_embedding))

    # ------------------------------------------------------------------
    # 2. Hybrid search over a larger candidate pool
    # ------------------------------------------------------------------
    # retrieval_k is always >= k so reranking has more to work with.
    retrieval_k = max(settings.retrieval_k, k)
    raw_chunks = await hybrid_search(
        session=session,
        query=query,
        query_embedding=query_embedding,
        k=retrieval_k,
        document_ids=document_ids,
        rrf_k=settings.rrf_k,
    )
    logger.info(
        "Retrieved %d candidates (retrieval_k=%d, context_k=%d)",
        len(raw_chunks), retrieval_k, k,
    )
    for c in raw_chunks:
        logger.debug("  chunk=%d rrf=%.6f file=%s", c["chunk_index"], c["score"], c["filename"])

    # ------------------------------------------------------------------
    # 3. Cross-encoder reranking over full candidate pool
    # ------------------------------------------------------------------
    reranked_chunks = _rerank(query, raw_chunks)

    # ------------------------------------------------------------------
    # 3b. Truncate to context_k — only the top-k go to the LLM
    # ------------------------------------------------------------------
    ranked_chunks = reranked_chunks[:k]
    logger.info(
        "Truncated to %d chunks for LLM context (dropped %d after reranking)",
        len(ranked_chunks), len(reranked_chunks) - len(ranked_chunks),
    )

    # ------------------------------------------------------------------
    # 4. Build context string
    # ------------------------------------------------------------------
    if ranked_chunks:
        context_parts: list[str] = []
        for chunk in ranked_chunks:
            context_parts.append(f"[Source: {chunk['filename']}]\n{chunk['text']}")
        context = "\n\n---\n\n".join(context_parts)
        user_message = f"Document excerpts:\n\n{context}\n\nQuestion: {query}"
    else:
        # No chunks at all — guide the LLM to produce a useful non-answer
        # rather than hallucinating or giving a generic refusal.
        logger.warning("No chunks retrieved — asking LLM to respond constructively")
        user_message = (
            f"Question: {query}\n\n"
            "No relevant excerpts were found in the indexed documents for this question. "
            "Politely explain that the uploaded documents do not contain information relevant "
            "to this question. Do not offer to answer it yourself, generate content, or help "
            "in any way that goes beyond the document content."
        )

    # ------------------------------------------------------------------
    # 5. Call LLM via OpenRouter
    # ------------------------------------------------------------------
    client = get_openrouter_client()
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
            score=chunk["score"],
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
