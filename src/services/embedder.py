"""
Singleton SentenceTransformer embedder.

Uses all-MiniLM-L6-v2 (384 dimensions) — matches the pgvector column definition.
Model is loaded once on first use (lazy singleton).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class Embedder:
    """
    Wraps SentenceTransformer with a lazy-loaded singleton model.

    Usage:
        embedder = Embedder()
        vectors = embedder.embed(["sentence one", "sentence two"])
    """

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
            logger.info("Model loaded — output dimension: %d", self._model.get_sentence_embedding_dimension())
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Batch-embed a list of strings.

        Args:
            texts: Non-empty list of strings to embed.

        Returns:
            List of float vectors, one per input string.
            Each vector has 384 dimensions (all-MiniLM-L6-v2).
        """
        if not texts:
            return []

        model = self._get_model()
        logger.debug("Embedding %d text(s)", len(texts))
        vectors = model.encode(texts, batch_size=64, convert_to_numpy=True, show_progress_bar=False)
        return vectors.tolist()


# Module-level singleton — import and use directly
embedder = Embedder()
