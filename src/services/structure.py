"""
Turn a parsed document into a section tree and section-aware chunks.

Everything here is pure (no DB, no models) so it is easy to test:

  pages + headings
      -> assemble():        one string with known page start offsets
      -> locate_headings(): heading -> character offset in that string
      -> build_sections():  tree with parent links, page ranges and own body span
      -> chunk_sections():  chunks that never cross a section boundary, each with
                            a "Document / Section path" header and a page range
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

from services.chunker import TokenChunker
from services.parser import Heading

PAGE_SEP = "\n\n"
FRONT_MATTER_MIN_CHARS = 200
FALLBACK_PAGES_PER_SECTION = 5


@dataclass
class SectionNode:
    ordinal: int
    level: int
    title: str
    parent: int | None
    start: int          # heading position (start of the section incl. children)
    end: int            # end of the section incl. children
    body_start: int     # own text: from the heading to the next heading of any level
    body_end: int
    page_start: int = 1
    page_end: int = 1
    path: list[str] = field(default_factory=list)

    @property
    def path_str(self) -> str:
        return " > ".join(self.path)


def assemble(pages: list[str]) -> tuple[str, list[int]]:
    """Join pages and return (full_text, start offset of each page)."""
    offsets: list[int] = []
    parts: list[str] = []
    pos = 0
    for page in pages:
        offsets.append(pos)
        parts.append(page)
        pos += len(page) + len(PAGE_SEP)
    return PAGE_SEP.join(parts), offsets


def page_at(offsets: list[int], pos: int) -> int:
    """1-based page number containing character *pos*."""
    return max(1, bisect.bisect_right(offsets, pos))


def _title_pattern(title: str) -> re.Pattern[str] | None:
    words = re.findall(r"\w+", title)
    if not words:
        return None
    return re.compile(r"\W*".join(re.escape(w) for w in words), re.IGNORECASE)


def locate_headings(
    full_text: str, offsets: list[int], headings: list[Heading]
) -> list[tuple[int, int, str]]:
    """Return sorted (offset, level, title). Unfound titles anchor at their page start."""
    located: list[tuple[int, int, str]] = []
    last = 0
    for h in headings:
        page_idx = min(max(h.page, 1), len(offsets)) - 1 if offsets else 0
        page_start = offsets[page_idx] if offsets else 0
        page_end = offsets[page_idx + 1] if page_idx + 1 < len(offsets) else len(full_text)
        search_from = max(page_start, last) if last <= page_end else page_start
        pos = page_start
        pattern = _title_pattern(h.title)
        if pattern:
            m = pattern.search(full_text, search_from, page_end) or pattern.search(full_text, page_start, page_end)
            if m:
                pos = m.start()
        located.append((pos, h.level, h.title))
        last = pos
    located.sort(key=lambda x: x[0])
    # Two headings at the same spot (e.g. both unfound on one page): keep the first.
    deduped: list[tuple[int, int, str]] = []
    for item in located:
        if deduped and deduped[-1][0] == item[0]:
            continue
        deduped.append(item)
    return deduped


def build_sections(
    full_text: str, offsets: list[int], located: list[tuple[int, int, str]]
) -> list[SectionNode]:
    """Build the section tree. Without headings, fall back to page-group sections."""
    if not located:
        return _page_group_sections(full_text, offsets)

    located = list(located)
    preamble = full_text[: located[0][0]]
    if len(preamble.strip()) >= FRONT_MATTER_MIN_CHARS:
        located.insert(0, (0, min(l for _, l, _ in located), "Front matter"))
    elif located[0][0] > 0:
        # Short preamble (title page etc.) folds into the first section.
        first = located[0]
        located[0] = (0, first[1], first[2])

    nodes: list[SectionNode] = []
    stack: list[int] = []
    for i, (pos, level, title) in enumerate(located):
        while stack and nodes[stack[-1]].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        body_end = located[i + 1][0] if i + 1 < len(located) else len(full_text)
        node = SectionNode(
            ordinal=i, level=level, title=title.strip()[:500], parent=parent,
            start=pos, end=len(full_text), body_start=pos, body_end=body_end,
        )
        node.path = (nodes[parent].path if parent is not None else []) + [node.title]
        nodes.append(node)
        stack.append(i)

    for i, node in enumerate(nodes):
        for later in nodes[i + 1:]:
            if later.level <= node.level:
                node.end = later.start
                break
        node.page_start = page_at(offsets, node.start)
        node.page_end = page_at(offsets, max(node.start, node.end - 1))
    return nodes


def _page_group_sections(full_text: str, offsets: list[int]) -> list[SectionNode]:
    nodes: list[SectionNode] = []
    n_pages = max(1, len(offsets))
    for i, first in enumerate(range(1, n_pages + 1, FALLBACK_PAGES_PER_SECTION)):
        last = min(first + FALLBACK_PAGES_PER_SECTION - 1, n_pages)
        start = offsets[first - 1] if offsets else 0
        end = offsets[last] if last < len(offsets) else len(full_text)
        title = f"Pages {first}–{last}" if last > first else f"Page {first}"
        nodes.append(SectionNode(
            ordinal=i, level=1, title=title, parent=None, start=start, end=end,
            body_start=start, body_end=end, page_start=first, page_end=last, path=[title],
        ))
    return nodes


def chunk_header(filename: str, section_path: str) -> str:
    return f"Document: {filename}\nSection: {section_path}" if section_path else f"Document: {filename}"


def chunk_sections(
    full_text: str,
    offsets: list[int],
    sections: list[SectionNode],
    chunker: TokenChunker,
    filename: str,
) -> list[dict[str, Any]]:
    """Chunk each section's own body; chunks carry header, section ordinal and page range."""
    chunks: list[dict[str, Any]] = []
    for node in sections:
        body = full_text[node.body_start:node.body_end]
        # Skip bodies that are just the heading line.
        if len(body.strip()) - len(node.title) < 10:
            continue
        for start, end in chunker.split_spans(body, offset=node.body_start):
            chunks.append({
                "text": full_text[start:end],
                "chunk_index": len(chunks),
                "start_char": start,
                "end_char": end,
                "page_start": page_at(offsets, start),
                "page_end": page_at(offsets, max(start, end - 1)),
                "section_ordinal": node.ordinal,
                "section_path": node.path_str,
                "header": chunk_header(filename, node.path_str),
            })
    return chunks
