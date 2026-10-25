"""
Tools the deep-mode agent can call. Each returns (text for the model, one-line
summary for the UI step event). Every passage a tool shows the model is
registered in the workspace under an id like P7 so it can be kept as evidence.
"""

from __future__ import annotations

import logging
from typing import Any

from services.agent.store import DocStore
from services.agent.workspace import Workspace

logger = logging.getLogger(__name__)

MAX_PAGES_PER_READ = 5
PAGE_CHARS = 6_000
SECTION_CHARS = 14_000
SEARCH_CHARS = 1_500
OUTPUT_CHARS = 24_000


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _fn(
        "outline",
        "Show a document's section tree: section refs (S1, S2…), titles, page ranges and summaries. "
        "Use it to find where a topic lives before reading.",
        {"document": {"type": "string", "description": "Document ref, e.g. D1"}},
        ["document"],
    ),
    _fn(
        "search",
        "Semantic + keyword search over all chunks; returns the best passages with pages and sections.",
        {
            "query": {"type": "string", "description": "A focused search query"},
            "k": {"type": "integer", "description": "Passages to return (1-10, default 6)"},
        },
        ["query"],
    ),
    _fn(
        "keyword_scan",
        "Find exact occurrences of terms (names, codes, numbers, defined terms) across all pages.",
        {"terms": {"type": "array", "items": {"type": "string"}, "description": "1-8 exact terms"}},
        ["terms"],
    ),
    _fn(
        "read_pages",
        f"Read the full text of up to {MAX_PAGES_PER_READ} consecutive pages of a document.",
        {
            "document": {"type": "string", "description": "Document ref, e.g. D1"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
        },
        ["document", "start"],
    ),
    _fn(
        "read_section",
        "Read the full text of one section (including its subsections).",
        {"section": {"type": "string", "description": "Section ref from outline, e.g. S4"}},
        ["section"],
    ),
    _fn(
        "add_evidence",
        "Keep passages that support the answer. Only kept evidence can be cited in the final answer.",
        {
            "passages": {"type": "array", "items": {"type": "string"}, "description": "Passage ids, e.g. [\"P3\", \"P7\"]"},
            "note": {"type": "string", "description": "What these passages show"},
        },
        ["passages"],
    ),
    _fn("finish", "Stop: the kept evidence is enough to answer (or nothing more can be found).", {}, []),
]


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + " …[truncated]"


class ToolBox:
    def __init__(self, store: DocStore, ws: Workspace) -> None:
        self.store = store
        self.ws = ws

    async def doc_ref_list(self) -> str:
        lines = []
        for d in await self.store.documents():
            ref = self.ws.ref("D", d["document_id"])
            kind = "pages" if d.get("has_real_pages", True) else "virtual pages"
            lines.append(f"{ref}: {d['filename']} ({d['page_count']} {kind})")
            if d.get("summary"):
                lines.append(f"    {d['summary']}")
        return "\n".join(lines) or "(no documents)"

    async def run(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"Unknown tool '{name}'.", f"unknown tool {name}"
        try:
            text, summary = await handler(**args)
        except TypeError as exc:
            return f"Bad arguments for {name}: {exc}", f"{name}: bad arguments"
        except Exception as exc:  # tool failures are reported to the model, not raised
            logger.warning("Tool %s failed: %s", name, exc)
            return f"{name} failed: {exc}", f"{name} failed"
        return _clip(text, OUTPUT_CHARS), summary

    def _doc(self, ref: str) -> str | None:
        return self.ws.resolve(ref)

    # -- tools --------------------------------------------------------------

    async def _t_outline(self, document: str) -> tuple[str, str]:
        doc_id = self._doc(document)
        sections = await self.store.outline(doc_id) if doc_id else []
        if not sections:
            return f"No outline for '{document}'.", f"outline {document}: nothing"
        lines = []
        for s in sections:
            ref = self.ws.ref("S", s["section_id"])
            indent = "  " * (s["level"] - 1)
            pages = f"p.{s['page_start']}" + (f"-{s['page_end']}" if s["page_end"] != s["page_start"] else "")
            summary = f" — {s['summary']}" if s.get("summary") else ""
            lines.append(f"{indent}{ref} {s['title']} ({pages}){summary}")
        return "\n".join(lines), f"outline of {document}: {len(sections)} sections"

    async def _t_search(self, query: str, k: int = 6) -> tuple[str, str]:
        k = max(1, min(int(k or 6), 10))
        hits = await self.store.search(query, k)
        blocks = []
        for h in hits:
            p = self.ws.add_passage(
                document_id=h["document_id"], filename=h["filename"], text=h["text"],
                page_start=h.get("page_start"), page_end=h.get("page_end"),
                section_path=h.get("section_path"), chunk_id=h.get("chunk_id"),
                chunk_index=h.get("chunk_index", -1),
                score=float(h.get("rerank_score", h.get("score", 0.0)) or 0.0),
            )
            blocks.append(f"[{p.id}] {p.location()}\n{_clip(p.text, SEARCH_CHARS)}")
        text = "\n\n".join(blocks) or "No passages found."
        return text, f"search “{query}”: {len(hits)} passages"

    async def _t_keyword_scan(self, terms: list[str] | str) -> tuple[str, str]:
        if isinstance(terms, str):
            terms = [terms]
        hits = await self.store.keyword_scan(list(terms), 20)
        blocks = []
        for h in hits:
            p = self.ws.add_passage(
                document_id=h["document_id"], filename=h["filename"], text=h["snippet"],
                page_start=h["page"], page_end=h["page"],
            )
            blocks.append(f"[{p.id}] {p.location()} (term: {h['term']})\n…{h['snippet']}…")
        pages = sorted({(h["filename"], h["page"]) for h in hits})
        text = "\n\n".join(blocks) or "No exact matches."
        return text, f"keyword scan {', '.join(terms)}: {len(hits)} hits on {len(pages)} pages"

    async def _t_read_pages(self, document: str, start: int, end: int | None = None) -> tuple[str, str]:
        doc_id = self._doc(document)
        start = max(1, int(start))
        end = int(end or start)
        end = max(start, min(end, start + MAX_PAGES_PER_READ - 1))
        pages = await self.store.read_pages(doc_id, start, end) if doc_id else []
        if not pages:
            return f"No pages {start}-{end} in '{document}'.", f"read {document} p.{start}-{end}: nothing"
        blocks = []
        for pg in pages:
            p = self.ws.add_passage(
                document_id=doc_id, filename=pg["filename"], text=_clip(pg["text"], PAGE_CHARS),
                page_start=pg["page"], page_end=pg["page"],
            )
            blocks.append(f"[{p.id}] {p.location()}\n{p.text}")
        return "\n\n".join(blocks), f"read {pages[0]['filename']} p.{start}-{end}"

    async def _t_read_section(self, section: str) -> tuple[str, str]:
        sec_id = self.ws.resolve(section)
        sec = await self.store.read_section(sec_id) if sec_id else None
        if not sec:
            return f"Unknown section '{section}'. Call outline first.", f"read section {section}: not found"
        p = self.ws.add_passage(
            document_id=sec["document_id"], filename=sec["filename"], text=_clip(sec["text"], SECTION_CHARS),
            page_start=sec["page_start"], page_end=sec["page_end"], section_path=sec["path"],
        )
        return f"[{p.id}] {p.location()}\n{p.text}", f"read section “{sec['path']}”"

    async def _t_add_evidence(self, passages: list[str] | str, note: str | None = None) -> tuple[str, str]:
        if isinstance(passages, str):
            passages = [passages]
        added, unknown = self.ws.add_evidence(list(passages), note)
        msg = f"Kept {len(added)} passage(s) as evidence ({len(self.ws.evidence)} total)."
        if unknown:
            msg += f" Unknown ids ignored: {', '.join(unknown)}."
        return msg, f"kept {', '.join(added) or 'nothing'} as evidence"

    async def _t_finish(self) -> tuple[str, str]:
        return "Finishing.", "finished research"
