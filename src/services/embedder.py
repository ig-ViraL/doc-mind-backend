"""
Pluggable async embedder (singleton).

Providers (``EMBEDDING_PROVIDER``):
  local  — sentence-transformers model run in-process. Default ``Qwen/Qwen3-Embedding-0.6B``
           (1024 dims) on the Mac GPU (MPS) in fp16 when available, else CUDA/CPU.
  voyage — Voyage AI API (``voyage-3-lite``, 512 dims). Needs ``VOYAGE_API_KEY``.

``embedding_dim`` in config must match the model's output size. Changing models
requires re-embedding stored chunks: ``python scripts/reindex.py``.

Queries and documents are embedded asymmetrically: queries get the instruction
prompt from ``EMBEDDING_QUERY_PROMPT`` (what Qwen3-Embedding is trained with);
documents are embedded as-is.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from services.devices import pick_device, use_fp16

logger = logging.getLogger(__name__)

_BATCH_SIZE = 128  # Voyage AI per-request limit


def embedding_text(header: str | None, text: str) -> str:
    """The exact string a chunk is embedded (and reranked) with: section header + body."""
    return f"{header}\n\n{text}" if header else text


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

            device = pick_device(settings.embedding_device)
            logger.info(
                "Loading local embedding model %s on %s", settings.local_embedding_model, device
            )
            model = SentenceTransformer(settings.local_embedding_model, device=device)
            model.max_seq_length = settings.embedding_max_seq_length
            if use_fp16(device, settings.embedding_fp16):
                model.half()
                logger.info("Embedding model running in fp16")
            get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
            dim = get_dim()
            if dim and dim != settings.embedding_dim:
                raise RuntimeError(
                    f"{settings.local_embedding_model} outputs {dim}-dim vectors but "
                    f"EMBEDDING_DIM={settings.embedding_dim}. Fix EMBEDDING_DIM, then run "
                    "`python scripts/reindex.py`."
                )
            self._client = model
        return self._client

    def _get_client(self) -> Any:
        from config import settings

        if settings.embedding_provider == "local":
            return self._get_local_model()
        if self._client is None:
            import voyageai

            if not settings.voyage_api_key:
                raise RuntimeError("VOYAGE_API_KEY is not set — add it to your .env file")
            logger.info("Initializing Voyage AI client (model: %s)", settings.voyage_model)
            self._client = voyageai.AsyncClient(api_key=settings.voyage_api_key)
        return self._client

    async def embed(
        self,
        texts: list[str],
        input_type: Literal["document", "query"] = "document",
    ) -> list[list[float]]:
        """Batch-embed texts; returns one normalised float vector per input."""
        if not texts:
            return []

        client = self._get_client()
        from config import settings

        if settings.embedding_provider == "local":
            kwargs: dict[str, Any] = {
                "normalize_embeddings": True,
                "batch_size": settings.embedding_batch_size,
                "convert_to_numpy": True,
            }
            if input_type == "query" and settings.embedding_query_prompt:
                kwargs["prompt"] = settings.embedding_query_prompt
            # GPU/CPU-bound: keep it off the event loop.
            vectors = await asyncio.to_thread(client.encode, texts, **kwargs)
            return vectors.astype("float32").tolist()

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            result = await client.embed(batch, model=settings.voyage_model, input_type=input_type)
            all_embeddings.extend(result.embeddings)
        return all_embeddings

    def count_tokens(self, text: str) -> int:
        """Token count using the local model's tokenizer when loaded, else an approximation."""
        from services.chunker import approx_token_count

        tokenizer = getattr(self._client, "tokenizer", None)
        if tokenizer is None:
            return approx_token_count(text)
        return len(tokenizer.encode(text, add_special_tokens=False))


embedder = Embedder()


def warm_up() -> None:
    """Pre-initialize the embedding backend so the first request isn't slow."""
    embedder._get_client()
    logger.info("Embedder initialized")
