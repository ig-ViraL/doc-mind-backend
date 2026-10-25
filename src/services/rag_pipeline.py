"""
Query orchestration for all modes.

  fast        retrieve top-k -> stream an answer that cites [n] passages
  deep        bounded tool loop (services.agent.deep) -> answer -> check
  exhaustive  map-reduce over sections (services.agent.exhaustive) -> answer -> check
  auto        router picks one of the above

``stream_answer`` yields (event, data) pairs used directly by the SSE endpoint:

  mode       {"mode", "requested", "reason"}
  step       {"n", "tool", "args", "summary"}             (deep / exhaustive)
  sources    {"sources": [...]}                            evidence the answer may cite
  token      {"text"}                                      answer text
  citations  {"citations": [...], "removed": [...]}        markers used + sentences the checker dropped
  done       {"latency_ms", "mode", "budget"}

``answer_query`` runs the same generator and collects a QueryResponse.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, AsyncIterator

import openai
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.schemas import Citation, QueryResponse, SourceChunk, StepEvent
from services.agent import answer as answer_mod
from services.agent.deep import run_deep
from services.agent.exhaustive import run_exhaustive
from services.agent.router import route
from services.agent.store import DocStore, PgStore
from services.agent.workspace import Budget, Passage, Workspace
from services.llm_client import get_openrouter_client

logger = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# Meta-query detection — bypass retrieval for questions about the assistant
# ---------------------------------------------------------------------------

_META_QUERY_RE = re.compile(
    r"""
    ^\s*(
        (who|what)\s+are\s+you                  |
        what\s+(can|do)\s+you\s+(do|help)       |
        tell\s+me\s+about\s+yourself            |
        introduce\s+yourself                     |
        your\s+(name|purpose|role|capabilities?) |
        what\s+is\s+(your\s+name|docmind)       |
        (hi|hello|hey|howdy)\b                  |
        ^help$
    )\s*[?!.]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DOCMIND_INTRO = (
    "I'm DocMind, an intelligent document assistant. "
    "I answer questions using only the content of documents you upload — "
    "I don't draw on outside knowledge or make things up.\n\n"
    "Here's what I can do:\n"
    "- Answer quick factual questions with page-level citations\n"
    "- Research harder questions step by step (deep mode)\n"
    "- Read a whole document to list or summarise everything relevant (exhaustive mode)\n"
    "- Reason over tables, image descriptions and links in your files\n\n"
    "Supported formats: PDF, DOCX, TXT, MD.\n\n"
    "Upload a document and ask me anything about its content."
)


def _is_meta_query(query: str) -> bool:
    """Return True if the query is about the assistant itself, not a document."""
    return bool(_META_QUERY_RE.match(query.strip()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sources(passages: list[Passage]) -> list[dict[str, Any]]:
    return [
        SourceChunk(
            marker=i,
            chunk_index=p.chunk_index,
            text=p.text,
            score=round(p.score, 4),
            document_id=p.document_id,
            filename=p.filename,
            page_start=p.page_start,
            page_end=p.page_end,
            section_path=p.section_path,
            note=p.note,
        ).model_dump()
        for i, p in enumerate(passages, start=1)
    ]


def _paragraphs(text: str) -> list[str]:
    """Emit a checked answer in a few token events (keeps the streaming UI contract)."""
    parts = re.split(r"(\n\n)", text)
    return [p for p in parts if p]


async def _fast_passages(store: DocStore, query: str, k: int, ws: Workspace) -> list[Passage]:
    hits = await store.search(query, k)
    passages = []
    for h in hits:
        passages.append(ws.add_passage(
            document_id=h["document_id"], filename=h["filename"], text=h["text"],
            page_start=h.get("page_start"), page_end=h.get("page_end"),
            section_path=h.get("section_path"), chunk_id=h.get("chunk_id"),
            chunk_index=h.get("chunk_index", -1),
            score=float(h.get("rerank_score", h.get("score", 0.0)) or 0.0),
        ))
    ws.add_evidence([p.id for p in passages])
    return passages


# ---------------------------------------------------------------------------
# Streaming entry point
# ---------------------------------------------------------------------------


async def stream_answer(
    query: str,
    document_ids: list[str] | None,
    k: int,
    session: AsyncSession,
    mode: str = "fast",
    store: DocStore | None = None,
) -> AsyncIterator[Event]:
    start_time = time.perf_counter()

    def latency() -> float:
        return round((time.perf_counter() - start_time) * 1000, 2)

    if _is_meta_query(query):
        yield "mode", {"mode": "fast", "requested": mode, "reason": "question about DocMind"}
        yield "sources", {"sources": []}
        yield "token", {"text": _DOCMIND_INTRO}
        yield "citations", {"citations": [], "removed": []}
        yield "done", {"latency_ms": latency(), "mode": "fast", "budget": {}}
        return

    store = store or PgStore(session, document_ids)
    requested = mode
    reason = "requested"
    if mode == "auto":
        mode, reason = await route(query)
    yield "mode", {"mode": mode, "requested": requested, "reason": reason}

    ws = Workspace()
    budget = Budget()

    # ---- fast: one retrieval pass, streamed answer -----------------------
    if mode == "fast":
        passages = await _fast_passages(store, query, k, ws)
        yield "sources", {"sources": _sources(passages)}
        stream = await get_openrouter_client().chat.completions.create(
            model=settings.openrouter_model,
            max_tokens=1024,
            stream=True,
            messages=answer_mod.build_messages(query, passages),
        )
        text = ""
        async for event in stream:
            if event.choices and event.choices[0].delta.content:
                piece = event.choices[0].delta.content
                text += piece
                yield "token", {"text": piece}
        yield "citations", {"citations": answer_mod.citations(text, passages), "removed": []}
        yield "done", {"latency_ms": latency(), "mode": mode, "budget": budget.snapshot()}
        return

    # ---- deep / exhaustive: research, then answer, then check -------------
    runner = run_exhaustive if mode == "exhaustive" else run_deep
    try:
        async for event in runner(query, store, ws, budget):
            yield event
    except openai.BadRequestError as exc:
        # e.g. the configured model doesn't support tool calls — degrade to fast retrieval.
        logger.warning("Agent mode %s failed (%s) — falling back to fast retrieval", mode, exc)
        yield "step", {"n": budget.steps, "tool": "fallback", "args": {}, "summary": "agent unavailable, using fast retrieval"}
        await _fast_passages(store, query, max(k, 8), ws)

    if not ws.evidence:
        await _fast_passages(store, query, k, ws)

    passages = answer_mod.select_evidence(ws.evidence_passages())
    yield "sources", {"sources": _sources(passages)}

    text, used = await answer_mod.generate(query, passages)
    budget.add_tokens(used)
    removed: list[str] = []
    if settings.verify_answers and passages:
        text, removed, used = await answer_mod.check(text, passages)
        budget.add_tokens(used)
        yield "step", {
            "n": budget.steps, "tool": "check", "args": {},
            "summary": f"checked answer: removed {len(removed)} unsupported sentence(s)",
        }

    for piece in _paragraphs(text):
        yield "token", {"text": piece}
    yield "citations", {"citations": answer_mod.citations(text, passages), "removed": removed}
    yield "done", {"latency_ms": latency(), "mode": mode, "budget": budget.snapshot()}


# ---------------------------------------------------------------------------
# Blocking entry point
# ---------------------------------------------------------------------------


async def answer_query(
    query: str,
    document_ids: list[str] | None,
    k: int,
    session: AsyncSession,
    mode: str = "fast",
    store: DocStore | None = None,
) -> QueryResponse:
    text = ""
    sources: list[dict[str, Any]] = []
    steps: list[StepEvent] = []
    cites: list[Citation] = []
    removed: list[str] = []
    final: dict[str, Any] = {}
    mode_info: dict[str, Any] = {}

    async for event, data in stream_answer(query, document_ids, k, session, mode=mode, store=store):
        if event == "mode":
            mode_info = data
        elif event == "step":
            steps.append(StepEvent(**data))
        elif event == "sources":
            sources = data["sources"]
        elif event == "token":
            text += data["text"]
        elif event == "citations":
            cites = [Citation(**c) for c in data["citations"]]
            removed = data["removed"]
        elif event == "done":
            final = data

    logger.info(
        "answer_query: mode=%s latency=%.0fms steps=%d sources=%d",
        final.get("mode"), final.get("latency_ms", 0), len(steps), len(sources),
    )
    return QueryResponse(
        answer=text,
        sources=[SourceChunk(**s) for s in sources],
        latency_ms=final.get("latency_ms", 0.0),
        mode=final.get("mode", mode_info.get("mode", mode)),
        mode_reason=mode_info.get("reason"),
        steps=steps,
        citations=cites,
        removed_claims=removed,
        budget=final.get("budget", {}),
    )
