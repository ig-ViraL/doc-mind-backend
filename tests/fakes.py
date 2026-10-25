"""In-memory stand-ins for the document store and the LLM, used by agent tests."""

from __future__ import annotations

import json
from typing import Any

from services.llm import LLMResult, ToolCall

DOC = "doc-1"

PAGES = {
    1: "Acme Annual Report 2024. Contents: Overview, Finance, Risks.",
    2: "1 Overview\nAcme makes anvils. Headcount grew to 420 people.",
    3: "2 Finance\nRevenue was $12.4M in 2024, up from $10.1M in 2023.",
    4: "2.1 Costs\nSteel costs rose 8%. The CFO is Dana Reyes.",
    5: "3 Risks\nCurrency risk and steel prices are the main risks. Dana Reyes leads the risk committee.",
}

SECTIONS = [
    {"section_id": "s-ov", "parent_id": None, "ordinal": 0, "level": 1, "title": "1 Overview",
     "path": "1 Overview", "page_start": 2, "page_end": 2, "summary": "Company and headcount.", "body": PAGES[2]},
    {"section_id": "s-fin", "parent_id": None, "ordinal": 1, "level": 1, "title": "2 Finance",
     "path": "2 Finance", "page_start": 3, "page_end": 4, "summary": "Revenue and costs.", "body": PAGES[3]},
    {"section_id": "s-cost", "parent_id": "s-fin", "ordinal": 2, "level": 2, "title": "2.1 Costs",
     "path": "2 Finance > 2.1 Costs", "page_start": 4, "page_end": 4, "summary": "Steel costs.", "body": PAGES[4]},
    {"section_id": "s-risk", "parent_id": None, "ordinal": 3, "level": 1, "title": "3 Risks",
     "path": "3 Risks", "page_start": 5, "page_end": 5, "summary": "Main risks.", "body": PAGES[5]},
]


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def documents(self):
        return [{"document_id": DOC, "filename": "acme.pdf", "page_count": 5,
                 "summary": "Acme's 2024 annual report.", "status": "ready", "has_real_pages": True}]

    async def outline(self, document_id):
        self.calls.append(("outline", document_id))
        return [{k: v for k, v in s.items() if k != "body"} | {"body_chars": len(s["body"])}
                for s in SECTIONS] if document_id == DOC else []

    async def search(self, query, k):
        self.calls.append(("search", query))
        words = {w.lower().strip("?.,") for w in query.split() if len(w) > 3}
        hits = []
        for s in SECTIONS:
            score = sum(w in s["body"].lower() for w in words)
            if score:
                hits.append({
                    "chunk_id": f"c-{s['section_id']}", "chunk_index": s["ordinal"], "text": s["body"],
                    "header": None, "section_id": s["section_id"], "section_path": s["path"],
                    "page_start": s["page_start"], "page_end": s["page_start"], "document_id": DOC,
                    "filename": "acme.pdf", "score": float(score),
                })
        return sorted(hits, key=lambda h: -h["score"])[:k]

    async def keyword_scan(self, terms, limit):
        self.calls.append(("keyword_scan", terms))
        hits = []
        for page, text in PAGES.items():
            for t in terms:
                if t.lower() in text.lower():
                    hits.append({"document_id": DOC, "filename": "acme.pdf", "page": page, "term": t, "snippet": text})
        return hits[:limit]

    async def read_pages(self, document_id, start, end):
        self.calls.append(("read_pages", (start, end)))
        if document_id != DOC:
            return []
        return [{"page": p, "text": PAGES[p], "filename": "acme.pdf", "document_id": DOC}
                for p in range(start, end + 1) if p in PAGES]

    async def read_section(self, section_id):
        self.calls.append(("read_section", section_id))
        for s in SECTIONS:
            if s["section_id"] == section_id:
                kids = [c["body"] for c in SECTIONS if c["parent_id"] == section_id]
                return {"section_id": section_id, "document_id": DOC, "filename": "acme.pdf", "path": s["path"],
                        "page_start": s["page_start"], "page_end": s["page_end"],
                        "text": "\n\n".join([s["body"], *kids])}
        return None

    async def section_bodies(self):
        return [{"document_id": DOC, "filename": "acme.pdf", "path": s["path"], "page_start": s["page_start"],
                 "page_end": s["page_end"], "body": s["body"]} for s in SECTIONS]


def tool_turn(*calls: tuple[str, dict], tokens: int = 100) -> LLMResult:
    """An assistant turn that calls tools."""
    tcs = [ToolCall(id=f"call_{i}_{name}", name=name, arguments=args) for i, (name, args) in enumerate(calls)]
    message = {"role": "assistant", "content": "", "tool_calls": [
        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
        for tc in tcs
    ]}
    return LLMResult(content="", tool_calls=tcs, tokens=tokens, message=message)


def text_turn(content: str, tokens: int = 50) -> LLMResult:
    return LLMResult(content=content, tokens=tokens, message={"role": "assistant", "content": content})


class ScriptedLLM:
    """Replaces services.llm.chat. Routes by model/tools so each role gets its own script."""

    def __init__(self, planner=None, answer="", check=None, mapper=None, router=None):
        self.planner = list(planner or [])
        self.answer = answer
        self.check = check if check is not None else {"unsupported": []}
        self.mapper = mapper or (lambda prompt: {"findings": []})
        self.router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, messages, *, model=None, max_tokens=1024, temperature=0.0, tools=None):
        prompt = messages[-1]["content"] if isinstance(messages[-1].get("content"), str) else ""
        self.calls.append({"model": model, "tools": bool(tools), "prompt": prompt, "n_messages": len(messages)})
        if tools:
            return self.planner.pop(0) if self.planner else text_turn("done")
        if "Reply with JSON only: {\"unsupported\"" in prompt:
            return text_turn(json.dumps(self.check))
        if "Reply with JSON only: {\"findings\"" in prompt:
            return text_turn(json.dumps(self.mapper(prompt)), tokens=200)
        if "Choose how to research" in prompt:
            return text_turn(json.dumps(self.router or {"mode": "deep", "reason": "test"}))
        return text_turn(self.answer, tokens=300)
