"""
Voyage AI embedder — API-based, async, singleton.
Uses input_type="document" for indexing and input_type="query" for retrieval.
"""

from __future__ import annotations

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

    def _get_client(self) -> Any:
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
    """Pre-initialize the Voyage AI client (validates API key is configured)."""
    embedder._get_client()
    logger.info("Voyage AI embedder initialized")
