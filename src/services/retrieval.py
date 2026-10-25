"""
Retrieval: embed the query -> hybrid search (vector + BM25-style FTS, fused by RRF)
-> cross-encoder rerank -> top-k. Shared by fast mode and the agent's search tool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from services.embedder import embedder, embedding_text
from services.vector_store import hybrid_search

logger = logging.getLogger(__name__)

_cross_encoder: Any | None = None
_cross_encoder_failed = False


def warm_up_cross_encoder() -> None:
    """Pre-load the cross-encoder so the first request isn't slow."""
    _get_cross_encoder()


def _get_cross_encoder() -> Any:
    global _cross_encoder, _cross_encoder_failed
    if _cross_encoder is None and not _cross_encoder_failed:
        try:
            from sentence_transformers import CrossEncoder

            from services.devices import pick_device, use_fp16

            device = pick_device(settings.reranker_device)
            logger.info("Loading cross-encoder %s on %s", settings.cross_encoder_model, device)
            ce = CrossEncoder(settings.cross_encoder_model, device=device, max_length=settings.reranker_max_length)
            if use_fp16(device, settings.reranker_fp16):
                ce.model.half()
            _cross_encoder = ce
        except Exception as exc:
            logger.warning("Failed to load cross-encoder (%s) — reranking disabled", exc)
            _cross_encoder_failed = True
    return _cross_encoder


def rerank(query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort chunks by cross-encoder score (blocking; call via a thread). Falls back to input order."""
    cross_encoder = _get_cross_encoder()
    if cross_encoder is None or not chunks:
        return chunks
    try:
        pairs = [(query, embedding_text(c.get("header"), c["text"])) for c in chunks]
        scores = [float(s) for s in cross_encoder.predict(pairs, batch_size=settings.reranker_batch_size)]
        ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        return [{**c, "rerank_score": round(s, 4)} for s, c in ranked]
    except Exception as exc:
        logger.warning("Cross-encoder reranking failed: %s — using original order", exc)
        return chunks


async def retrieve(
    session: AsyncSession,
    query: str,
    document_ids: list[str] | None,
    k: int,
) -> list[dict[str, Any]]:
    """Top-k chunks for *query* (each dict has text, header, pages, section, filename, score)."""
    query_embedding = (await embedder.embed([query], input_type="query"))[0]
    pool = max(settings.retrieval_k, k)
    candidates = await hybrid_search(
        session=session,
        query=query,
        query_embedding=query_embedding,
        k=pool,
        document_ids=document_ids,
        rrf_k=settings.rrf_k,
    )
    # The cross-encoder is GPU/CPU-bound; run it in a worker thread so other requests keep flowing.
    ranked = await asyncio.to_thread(rerank, query, candidates)
    logger.info("Retrieved %d candidates, keeping %d", len(candidates), min(k, len(ranked)))
    return ranked[:k]
