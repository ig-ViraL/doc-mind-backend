"""
Singleton SentenceTransformer embedder — lazy-loaded on first use.
Call warm_up() at server startup to avoid first-request latency.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Embedder:
    _instance: "Embedder | None" = None
    _model: Any | None = None

    def __new__(cls) -> "Embedder":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            from config import settings

            model_name = settings.embedding_model
            logger.info("Loading SentenceTransformer model: %s", model_name)
            self._model = SentenceTransformer(model_name)
            logger.info("Embedder ready — dimension: %d", self._model.get_embedding_dimension())
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed a list of strings; returns one float vector per input."""
        if not texts:
            return []

        model = self._get_model()
        logger.debug("Embedding %d text(s)", len(texts))
        vectors = model.encode(texts, batch_size=64, convert_to_numpy=True, show_progress_bar=False)
        return vectors.tolist()


embedder = Embedder()


def warm_up() -> None:
    """Pre-load the embedding model so the first request isn't slow."""
    embedder._get_model()
