"""
Answer from evidence only, then check it.

- ``build_prompt`` numbers the evidence [1]..[n] with file, pages and section.
- The answer model must cite every factual sentence with [n].
- ``check`` asks the cheap model which sentences the evidence does not support
  and removes them (sentences, not whole paragraphs, so the rest survives).
- ``citations`` maps the [n] markers actually used to their pages and quotes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from config import settings
from services import llm
from services.agent.workspace import Passage

logger = logging.getLogger(__name__)

EVIDENCE_CHARS = 2_500
MAX_EVIDENCE = 40

SYSTEM_PROMPT = """\
You are DocMind, an assistant that answers questions strictly from uploaded documents.

You receive numbered evidence passages [1], [2], … each labelled with its file, \
pages and section. Passages may contain Markdown tables, [IMAGE] descriptions \
(treat as factual) and [HYPERLINKS].

Rules:
- Use only the evidence. No outside knowledge, no guessing.
- Put a citation like [2] or [1][4] right after every sentence that states a fact \
from the documents. Only cite passages that actually support that sentence.
- Synthesise across passages; reason carefully over table values.
- If the evidence does not fully answer the question, answer what it supports \
and say plainly what the documents do not cover.
- Write naturally and directly. Do not start with "Based on the evidence" or similar.
- Do not write code, essays or content that is not drawn from the documents. If the \
documents contain code you may quote or explain it.
- If asked who you are, you are DocMind — never a person described in the documents.\
"""

NO_EVIDENCE_NOTE = (
    "No relevant passages were found in the uploaded documents. Say briefly that the "
    "documents do not contain this information. Do not answer from general knowledge."
)

_CHECK_PROMPT = """\
You are checking an answer against its evidence. For each numbered sentence of the \
answer, decide whether the cited evidence (or any evidence below) supports it. \
Sentences that only say information is missing, or that introduce/structure the \
answer without stating facts, count as supported.

Evidence:
{evidence}

Answer sentences:
{sentences}

Reply with JSON only: {{"unsupported": [<sentence numbers>]}}"""


def select_evidence(passages: list[Passage]) -> list[Passage]:
    return passages[:MAX_EVIDENCE]


def evidence_block(passages: list[Passage]) -> str:
    return "\n\n".join(
        f"[{i}] {p.location()}\n{p.text[:EVIDENCE_CHARS]}" for i, p in enumerate(passages, start=1)
    )


def build_messages(query: str, passages: list[Passage]) -> list[dict[str, str]]:
    if passages:
        user = f"Evidence:\n\n{evidence_block(passages)}\n\nQuestion: {query}"
    else:
        user = f"Question: {query}\n\n{NO_EVIDENCE_NOTE}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


async def generate(query: str, passages: list[Passage]) -> tuple[str, int]:
    res = await llm.chat(build_messages(query, passages), model=settings.openrouter_model, max_tokens=1500)
    return res.content.strip(), res.tokens


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"(\n+|(?<=[.!?])\s+(?=[A-Z0-9\"'(*\-]))")


def split_sentences(answer: str) -> list[str]:
    """Split into alternating [text, sep, text, sep, …] so the answer can be rebuilt exactly."""
    return _SPLIT_RE.split(answer)


def _is_claim(segment: str) -> bool:
    return sum(c.isalpha() for c in segment) >= 12


def remove_sentences(answer: str, numbers: set[int]) -> tuple[str, list[str]]:
    """Drop claim sentences by 1-based number (as shown to the checker)."""
    parts = split_sentences(answer)
    out: list[str] = []
    removed: list[str] = []
    n = 0
    skip_sep = False
    for i, part in enumerate(parts):
        is_sep = i % 2 == 1
        if is_sep:
            if not skip_sep:
                out.append(part)
            skip_sep = False
            continue
        if _is_claim(part):
            n += 1
            if n in numbers:
                removed.append(part.strip())
                skip_sep = "\n" not in (parts[i + 1] if i + 1 < len(parts) else "")
                continue
        out.append(part)
    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, removed


def numbered_claims(answer: str) -> list[str]:
    parts = split_sentences(answer)
    return [p.strip() for i, p in enumerate(parts) if i % 2 == 0 and _is_claim(p)]


async def check(answer: str, passages: list[Passage]) -> tuple[str, list[str], int]:
    """Return (answer without unsupported sentences, removed sentences, tokens used)."""
    claims = numbered_claims(answer)
    if not claims or not passages:
        return answer, [], 0
    prompt = _CHECK_PROMPT.format(
        evidence=evidence_block(passages),
        sentences="\n".join(f"{i}. {c}" for i, c in enumerate(claims, start=1)),
    )
    try:
        res = await llm.chat([{"role": "user", "content": prompt}], model=settings.fast_model, max_tokens=200)
        data = llm.parse_json(res.content)
        bad = {int(x) for x in (data.get("unsupported", []) if isinstance(data, dict) else []) if str(x).isdigit()}
    except Exception as exc:
        logger.warning("Answer check failed (%s) — keeping the answer unchanged", exc)
        return answer, [], 0
    bad = {b for b in bad if 1 <= b <= len(claims)}
    if not bad:
        return answer, [], res.tokens
    cleaned, removed = remove_sentences(answer, bad)
    if not cleaned.strip():
        cleaned = "I couldn't verify an answer to this question against the documents."
    return cleaned, removed, res.tokens


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(r"\[(\d{1,3})\]")


def citations(answer: str, passages: list[Passage]) -> list[dict[str, Any]]:
    used: list[int] = []
    for m in _MARKER_RE.finditer(answer):
        n = int(m.group(1))
        if 1 <= n <= len(passages) and n not in used:
            used.append(n)
    out = []
    for n in sorted(used):
        p = passages[n - 1]
        out.append({
            "marker": n,
            "document_id": p.document_id,
            "filename": p.filename,
            "page_start": p.page_start,
            "page_end": p.page_end,
            "section_path": p.section_path,
            "quote": p.text[:300],
        })
    return out
