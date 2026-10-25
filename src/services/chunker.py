"""
Token-aware text chunker.

The text is split along natural boundaries (paragraph -> line -> sentence -> word)
until every piece fits in ``chunk_size`` tokens, then pieces are merged greedily.
Overlap is built from whole trailing pieces of the previous chunk, so a chunk
never starts or ends in the middle of a word.

Token counting is pluggable: pass the embedding model's tokenizer when it is
loaded, otherwise a cheap regex approximation is used.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, TypedDict

logger = logging.getLogger(__name__)

TokenCounter = Callable[[str], int]

_APPROX_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def approx_token_count(text: str) -> int:
    """Rough token count: words and punctuation marks, scaled for sub-word splits."""
    n = len(_APPROX_TOKEN_RE.findall(text))
    return int(n * 1.3) + 1 if n else 0


class ChunkDict(TypedDict):
    text: str
    chunk_index: int
    start_char: int
    end_char: int
    document_id: str


# Ordered from coarsest to finest. The separator stays attached to the piece before it.
_BOUNDARIES: list[re.Pattern[str]] = [
    re.compile(r"\n\s*\n"),          # paragraphs
    re.compile(r"\n"),               # lines
    re.compile(r"(?<=[.!?;:])\s+"),  # sentences / clauses
    re.compile(r"\s+"),              # words
]


class TokenChunker:
    def __init__(
        self,
        chunk_size: int = 400,
        chunk_overlap: int = 60,
        token_counter: TokenCounter | None = None,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.count = token_counter or approx_token_count

    def split_spans(self, text: str, offset: int = 0) -> list[tuple[int, int]]:
        """Return (start, end) offsets of each chunk, shifted by *offset*."""
        pieces = self._pieces(text, 0, len(text), level=0)
        out: list[tuple[int, int]] = []
        for start, end in self._merge(pieces):
            # Trim surrounding whitespace so offsets point at real content.
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start:
                out.append((start + offset, end + offset))
        return out

    def chunk(self, text: str, document_id: str) -> list[ChunkDict]:
        result: list[ChunkDict] = []
        for start, end in self.split_spans(text):
            result.append(
                {
                    "text": text[start:end],
                    "chunk_index": len(result),
                    "start_char": start,
                    "end_char": end,
                    "document_id": document_id,
                }
            )
        logger.debug("TokenChunker produced %d chunks for document_id=%s", len(result), document_id)
        return result

    # ------------------------------------------------------------------

    def _pieces(self, text: str, start: int, end: int, level: int) -> list[tuple[int, int, int]]:
        """Recursively split text[start:end] into (start, end, tokens) pieces that fit."""
        segment = text[start:end]
        if not segment.strip():
            return []
        tokens = self.count(segment)
        if tokens <= self.chunk_size or level >= len(_BOUNDARIES):
            # A single "word" longer than chunk_size is kept whole rather than cut.
            return [(start, end, tokens)]

        cuts = [m.end() for m in _BOUNDARIES[level].finditer(segment)]
        if not cuts:
            return self._pieces(text, start, end, level + 1)

        out: list[tuple[int, int, int]] = []
        prev = 0
        for cut in cuts + [len(segment)]:
            if cut > prev:
                out.extend(self._pieces(text, start + prev, start + cut, level + 1))
            prev = cut
        return out

    def _merge(self, pieces: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
        chunks: list[tuple[int, int]] = []
        current: list[tuple[int, int, int]] = []
        current_tokens = 0

        for piece in pieces:
            if current and current_tokens + piece[2] > self.chunk_size:
                chunks.append((current[0][0], current[-1][1]))
                # Seed the next chunk with whole trailing pieces up to chunk_overlap tokens.
                overlap: list[tuple[int, int, int]] = []
                overlap_tokens = 0
                for prev in reversed(current):
                    if overlap_tokens + prev[2] > self.chunk_overlap:
                        break
                    overlap.insert(0, prev)
                    overlap_tokens += prev[2]
                if overlap_tokens + piece[2] > self.chunk_size:
                    overlap, overlap_tokens = [], 0
                current, current_tokens = overlap, overlap_tokens
            current.append(piece)
            current_tokens += piece[2]

        if current:
            chunks.append((current[0][0], current[-1][1]))
        return chunks
