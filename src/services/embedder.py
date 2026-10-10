"""
Pluggable async embedder (singleton).

Providers (``EMBEDDING_PROVIDER``):
  local  — sentence-transformers ``all-MiniLM-L6-v2`` (384 dims). Free, no API key, runs in-process.
  voyage — Voyage AI API (``voyage-3-lite``, 512 dims). Needs ``VOYAGE_API_KEY``.

``embedding_dim`` in config must match the provider's output size.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

_BATCH_SIZE = 128  # Voyage AI per-request limit


class Embedder:
    _instance: "Embedder | None" = None
    _client: Any | None = None

    def __new__(cls) -> "Embedder":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_local_model(self) -> Any:
        if self._client is None:
            from sentence_transformers import SentenceTransformer
            from config import settings

            logger.info("Loading local embedding model: %s", settings.local_embedding_model)
            self._client = SentenceTransformer(settings.local_embedding_model)
        return self._client

    def _get_client(self) -> Any:
        from config import settings

        if settings.embedding_provider == "local":
            return self._get_local_model()
        if self._client is None:
            import voyageai
            from config import settings
            if not settings.voyage_api_key:
                raise RuntimeError(
                    "VOYAGE_API_KEY is not set — add it to your .env file"
                )
            logger.info("Initializing Voyage AI client (model: %s)", settings.voyage_model)
            self._client = voyageai.AsyncClient(api_key=settings.voyage_api_key)
        return self._client

    async def embed(
        self,
        texts: list[str],
        input_type: Literal["document", "query"] = "document",
    ) -> list[list[float]]:
        """Batch-embed texts via Voyage AI API; returns one float vector per input."""
        if not texts:
            return []

        client = self._get_client()
        from config import settings

        if settings.embedding_provider == "local":
            # CPU-bound: keep it off the event loop.
            vectors = await asyncio.to_thread(
                client.encode, texts, normalize_embeddings=True, batch_size=32
            )
            return vectors.tolist()

        all_embeddings: list[list[float]] = []
        total_batches = -(-len(texts) // _BATCH_SIZE)  # ceiling division
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            logger.debug(
                "Embedding batch %d/%d (%d texts, input_type=%s)",
                i // _BATCH_SIZE + 1,
                total_batches,
                len(batch),
                input_type,
            )
            result = await client.embed(
                batch,
                model=settings.voyage_model,
                input_type=input_type,
            )
            all_embeddings.extend(result.embeddings)

        return all_embeddings


embedder = Embedder()


def warm_up() -> None:
    """Pre-initialize the embedding backend so the first request isn't slow."""
    embedder._get_client()
    logger.info("Embedder initialized")
