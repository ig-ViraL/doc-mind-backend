"""
Section and document summaries written at upload time with the cheap model.

Summaries feed the agent's outline tool and the router, so a question like
"what does chapter 4 cover?" can be answered without reading chapter 4.
"""

from __future__ import annotations

import asyncio
import logging

from config import settings
from services import llm
from services.structure import SectionNode

logger = logging.getLogger(__name__)

SECTION_INPUT_CHARS = 8_000

_SECTION_PROMPT = (
    "Summarize this section of the document '{filename}' in 2-3 sentences. "
    "Name the key entities, numbers, definitions and conclusions it contains so a "
    "reader can tell whether it answers their question. No preamble.\n\n"
    "Section: {path} (pages {p1}-{p2})\n\n{body}"
)

_DOC_PROMPT = (
    "Write a 4-6 sentence overview of the document '{filename}' from its section "
    "summaries below. Say what kind of document it is, its main topics and how it is "
    "organised. No preamble.\n\n{sections}"
)


async def summarize(
    filename: str,
    full_text: str,
    sections: list[SectionNode],
) -> tuple[dict[int, str], str]:
    """Return ({section ordinal: summary}, document summary). Failures yield empty strings."""
    if not settings.summarize_sections or not settings.openrouter_api_key or not sections:
        return {}, ""

    # Too many sections: summarize the biggest ones plus every top-level section.
    chosen = sections
    if len(sections) > settings.max_section_summaries:
        top = [s for s in sections if s.parent is None]
        rest = sorted(
            (s for s in sections if s.parent is not None),
            key=lambda s: s.end - s.start,
            reverse=True,
        )
        chosen = (top + rest)[: settings.max_section_summaries]

    sem = asyncio.Semaphore(settings.summary_concurrency)

    async def one(node: SectionNode) -> tuple[int, str]:
        # Parent sections summarize their whole span (children included), truncated.
        body = full_text[node.start:node.end][:SECTION_INPUT_CHARS]
        if len(body.strip()) < 40:
            return node.ordinal, ""
        prompt = _SECTION_PROMPT.format(
            filename=filename, path=node.path_str, p1=node.page_start, p2=node.page_end, body=body
        )
        async with sem:
            try:
                res = await llm.chat(
                    [{"role": "user", "content": prompt}], model=settings.fast_model, max_tokens=200
                )
                return node.ordinal, res.content.strip()
            except Exception as exc:
                logger.warning("Summary failed for section '%s': %s", node.title, exc)
                return node.ordinal, ""

    results = dict(await asyncio.gather(*[one(n) for n in chosen]))

    top_lines = [
        f"- {n.path_str} (pp. {n.page_start}-{n.page_end}): {results.get(n.ordinal, '')}"
        for n in sections
        if n.level == min(s.level for s in sections) and results.get(n.ordinal)
    ][:80]
    doc_summary = ""
    if top_lines:
        try:
            res = await llm.chat(
                [{"role": "user", "content": _DOC_PROMPT.format(filename=filename, sections="\n".join(top_lines))}],
                model=settings.fast_model,
                max_tokens=350,
            )
            doc_summary = res.content.strip()
        except Exception as exc:
            logger.warning("Document summary failed for '%s': %s", filename, exc)

    logger.info("Summarized %d/%d sections of '%s'", sum(1 for v in results.values() if v), len(sections), filename)
    return results, doc_summary
