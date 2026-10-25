"""
Exhaustive mode: map-reduce over the whole document.

Sections are packed into "map units" of roughly EXHAUSTIVE_UNIT_TOKENS tokens
(at most EXHAUSTIVE_MAX_UNITS). The cheap model reads each unit and extracts
verbatim findings relevant to the question; findings become evidence and the
answer step reduces them. Each batch of MAP_CONCURRENCY units is one step, so
the same step / token / time budget applies as in deep mode.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator

from config import settings
from services import llm
from services.agent.store import DocStore
from services.agent.workspace import Budget, Workspace
from services.chunker import approx_token_count

logger = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]
MAX_UNIT_GROWTH = 4  # units may grow up to 4x the target size to fit under the unit cap

_MAP_PROMPT = """\
Question: {query}

Below is part of the document "{filename}" — section "{path}", pages {p1}-{p2}.
Extract every fact in it that helps answer the question, including partial or \
negative information. Copy a short verbatim quote (under 300 characters) for \
each. If nothing is relevant, return an empty list.

Reply with JSON only: {{"findings": [{{"fact": "...", "quote": "...", "page": <page number or null>}}]}}

---
{text}
"""


def build_map_units(
    sections: list[dict[str, Any]], unit_tokens: int, max_units: int
) -> list[dict[str, Any]]:
    """Pack section bodies into units; grow the unit size until they fit under max_units."""
    target = unit_tokens
    units = _pack(sections, target)
    while len(units) > max_units and target < unit_tokens * MAX_UNIT_GROWTH:
        target = int(target * 1.5)
        units = _pack(sections, target)
    if len(units) > max_units:
        logger.warning("Exhaustive: %d units exceed cap %d; reading the first %d", len(units), max_units, max_units)
        units = units[:max_units]
    return units


def _split_body(body: str, target: int) -> list[str]:
    if approx_token_count(body) <= target:
        return [body]
    parts, current = [], ""
    for para in re.split(r"\n\s*\n", body):
        if current and approx_token_count(current + para) > target:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


def _pack(sections: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for sec in sections:
        body = (sec.get("body") or "").strip()
        if len(body) < 20:
            continue
        for piece in _split_body(body, target):
            last = units[-1] if units else None
            if (
                last
                and last["document_id"] == sec["document_id"]
                and approx_token_count(last["text"]) + approx_token_count(piece) <= target
            ):
                last["text"] += "\n\n" + piece
                last["page_end"] = max(last["page_end"], sec["page_end"])
                if sec["path"] not in last["paths"]:
                    last["paths"].append(sec["path"])
            else:
                units.append({
                    "document_id": sec["document_id"], "filename": sec["filename"],
                    "paths": [sec["path"]], "page_start": sec["page_start"],
                    "page_end": sec["page_end"], "text": piece,
                })
    return units


async def _map_one(query: str, unit: dict[str, Any], budget: Budget) -> list[dict[str, Any]]:
    path = " | ".join(unit["paths"][:3]) + (" …" if len(unit["paths"]) > 3 else "")
    prompt = _MAP_PROMPT.format(
        query=query, filename=unit["filename"], path=path,
        p1=unit["page_start"], p2=unit["page_end"], text=unit["text"],
    )
    try:
        res = await llm.chat([{"role": "user", "content": prompt}], model=settings.fast_model, max_tokens=900)
        budget.add_tokens(res.tokens)
        data = llm.parse_json(res.content)
        findings = data.get("findings", []) if isinstance(data, dict) else []
        return [f for f in findings if isinstance(f, dict) and (f.get("quote") or f.get("fact"))]
    except Exception as exc:
        logger.warning("Map step failed for %s: %s", path, exc)
        return []


async def run_exhaustive(query: str, store: DocStore, ws: Workspace, budget: Budget) -> AsyncIterator[Event]:
    units = build_map_units(
        await store.section_bodies(), settings.exhaustive_unit_tokens, settings.exhaustive_max_units
    )
    yield "step", {
        "n": 0, "tool": "plan", "args": {"units": len(units)},
        "summary": f"reading {len(units)} parts of the document",
    }

    size = max(1, settings.map_concurrency)
    for start in range(0, len(units), size):
        reason = budget.exhausted()
        if reason:
            yield "step", {
                "n": budget.steps, "tool": "budget", "args": {},
                "summary": f"Stopped after {start}/{len(units)} parts: {reason}",
            }
            break
        batch = units[start : start + size]
        budget.steps += 1
        results = await asyncio.gather(*[_map_one(query, u, budget) for u in batch])

        kept = 0
        for unit, findings in zip(batch, results):
            for f in findings:
                page = f.get("page")
                if not isinstance(page, int) or not unit["page_start"] <= page <= unit["page_end"]:
                    page = None
                quote = str(f.get("quote") or "").strip()
                fact = str(f.get("fact") or "").strip()
                p = ws.add_passage(
                    document_id=unit["document_id"], filename=unit["filename"],
                    text=quote or fact,
                    page_start=page or unit["page_start"], page_end=page or unit["page_end"],
                    section_path=unit["paths"][0],
                )
                ws.add_evidence([p.id], fact or None)
                kept += 1
        yield "step", {
            "n": budget.steps, "tool": "map",
            "args": {"parts": f"{start + 1}-{start + len(batch)} of {len(units)}"},
            "summary": f"read parts {start + 1}-{start + len(batch)} of {len(units)}: {kept} findings",
        }
