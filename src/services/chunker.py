"""
Text chunkers for the DocMind RAG pipeline.

Two implementations:
  - RecursiveCharacterChunker  — deterministic, character-based (default)
  - SemanticChunker            — embedding-based sentence grouping
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chunk dict shape
# ---------------------------------------------------------------------------
# {
#   "text": str,
#   "chunk_index": int,
#   "start_char": int,
#   "end_char": int,
#   "document_id": str,
# }
ChunkDict = dict[str, Any]


# ---------------------------------------------------------------------------
# RecursiveCharacterChunker
# ---------------------------------------------------------------------------


class RecursiveCharacterChunker:
    """
    Splits text hierarchically by a list of separators, then by max chunk_size.
    Maintains chunk_overlap characters of context between consecutive chunks.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        separators: list[str] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators: list[str] = separators or ["\n\n", "\n", ". ", " "]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(self, text: str, document_id: str) -> list[ChunkDict]:
        """
        Split *text* into overlapping chunks and annotate with positions.

        Returns a list of ChunkDict objects ready for DB insertion.
        """
        raw_chunks: list[tuple[int, int]] = self._split_with_offsets(text)
        result: list[ChunkDict] = []
        for idx, (start, end) in enumerate(raw_chunks):
            chunk_text = text[start:end].strip()
            if not chunk_text:
                continue
            result.append(
                {
                    "text": chunk_text,
                    "chunk_index": idx,
                    "start_char": start,
                    "end_char": end,
                    "document_id": document_id,
                }
            )

        # Re-index after filtering empties
        for idx, chunk in enumerate(result):
            chunk["chunk_index"] = idx

        logger.debug(
            "RecursiveCharacterChunker produced %d chunks for document_id=%s",
            len(result),
            document_id,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _split_with_offsets(self, text: str) -> list[tuple[int, int]]:
        """
        Return (start, end) character offsets for each chunk.
        Uses recursive separator splitting then merges small pieces.
        """
        # First: get flat split positions using the separator hierarchy
        split_positions = self._find_split_positions(text)
        split_positions = sorted(set([0] + split_positions + [len(text)]))

        # Build segments between split points
        segments: list[tuple[int, int]] = []
        for i in range(len(split_positions) - 1):
            start = split_positions[i]
            end = split_positions[i + 1]
            if end > start:
                segments.append((start, end))

        # Merge segments into chunks respecting chunk_size, with overlap
        return self._merge_segments(text, segments)

    def _find_split_positions(self, text: str) -> list[int]:
        """
        Find character positions to split at using the separator hierarchy.
        Tries separators in order; if a separator is found it is used.
        """
        positions: list[int] = []
        for sep in self.separators:
            pos = 0
            while True:
                idx = text.find(sep, pos)
                if idx == -1:
                    break
                positions.append(idx + len(sep))
                pos = idx + len(sep)
            if positions:
                break

        # Fallback: split every chunk_size chars if no separators found
        if not positions and len(text) > self.chunk_size:
            positions = list(range(self.chunk_size, len(text), self.chunk_size))

        return positions

    def _merge_segments(
        self, text: str, segments: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Merge small segments into chunks of at most chunk_size characters."""
        if not segments:
            return []

        chunks: list[tuple[int, int]] = []
        current_start, current_end = segments[0]

        for seg_start, seg_end in segments[1:]:
            projected_len = seg_end - current_start
            if projected_len <= self.chunk_size:
                current_end = seg_end
            else:
                chunks.append((current_start, current_end))
                # Apply overlap: walk back from current_end by chunk_overlap
                overlap_start = max(current_start, current_end - self.chunk_overlap)
                current_start = overlap_start
                current_end = seg_end

        chunks.append((current_start, current_end))

        # Safety pass: hard-split any chunk still exceeding chunk_size
        final: list[tuple[int, int]] = []
        for start, end in chunks:
            while end - start > self.chunk_size:
                final.append((start, start + self.chunk_size))
                start = start + self.chunk_size - self.chunk_overlap
            final.append((start, end))

        return final


# ---------------------------------------------------------------------------
# SemanticChunker
# ---------------------------------------------------------------------------


class SemanticChunker:
    """
    Groups sentences into chunks by measuring cosine similarity between
    consecutive sentence embeddings.  A new chunk starts when the similarity
    drops below *similarity_threshold*.
    """

    def __init__(self, similarity_threshold: float = 0.85) -> None:
        self.similarity_threshold = similarity_threshold
        self._model: Any | None = None  # lazy-loaded

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(self, text: str, document_id: str) -> list[ChunkDict]:
        """
        Split *text* semantically and return a list of ChunkDict objects.
        """
        sentences = self._split_sentences(text)
        if not sentences:
            return []

        embeddings = self._embed_sentences([s for s, _ in sentences])
        groups = self._group_by_similarity(sentences, embeddings)

        result: list[ChunkDict] = []
        for idx, (group_text, start_char, end_char) in enumerate(groups):
            if group_text.strip():
                result.append(
                    {
                        "text": group_text.strip(),
                        "chunk_index": idx,
                        "start_char": start_char,
                        "end_char": end_char,
                        "document_id": document_id,
                    }
                )

        logger.debug(
            "SemanticChunker produced %d chunks for document_id=%s",
            len(result),
            document_id,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("SemanticChunker: loaded SentenceTransformer model")
        return self._model

    def _split_sentences(self, text: str) -> list[tuple[str, int]]:
        """
        Naively split text into (sentence, start_char) pairs.
        Uses a simple rule-based approach rather than pulling in NLTK.
        """
        sentences: list[tuple[str, int]] = []
        start = 0
        for i, ch in enumerate(text):
            if ch in {".", "!", "?"} and i + 1 < len(text) and text[i + 1] in {" ", "\n"}:
                sent = text[start : i + 1]
                if sent.strip():
                    sentences.append((sent, start))
                start = i + 2
        # Tail
        tail = text[start:]
        if tail.strip():
            sentences.append((tail, start))
        return sentences

    def _embed_sentences(self, sentences: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = model.encode(sentences, convert_to_numpy=True)
        return vectors.tolist()

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        import math

        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def _group_by_similarity(
        self,
        sentences: list[tuple[str, int]],
        embeddings: list[list[float]],
    ) -> list[tuple[str, int, int]]:
        """
        Group consecutive sentences into chunks.
        Returns list of (chunk_text, start_char, end_char).
        """
        if not sentences:
            return []

        groups: list[tuple[str, int, int]] = []
        current_text = sentences[0][0]
        current_start = sentences[0][1]
        current_end = current_start + len(sentences[0][0])

        for i in range(1, len(sentences)):
            sent, sent_start = sentences[i]
            sent_end = sent_start + len(sent)
            sim = self._cosine_similarity(embeddings[i - 1], embeddings[i])

            if sim >= self.similarity_threshold:
                # Same group — extend
                current_text += " " + sent
                current_end = sent_end
            else:
                # Boundary — save current group, start new
                groups.append((current_text, current_start, current_end))
                current_text = sent
                current_start = sent_start
                current_end = sent_end

        groups.append((current_text, current_start, current_end))
        return groups
