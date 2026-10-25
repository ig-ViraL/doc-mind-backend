"""
Per-query scratch state for the agent: passages it has seen, evidence it kept,
short references for documents/sections, and the budget it is spending.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from config import settings


@dataclass
class Passage:
    id: str                      # "P3" — what the model refers to
    document_id: str
    filename: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section_path: str | None = None
    chunk_id: str | None = None
    chunk_index: int = -1
    score: float = 0.0
    note: str | None = None      # why the agent kept it (evidence only)

    def location(self) -> str:
        parts = [self.filename]
        if self.page_start:
            pages = (
                f"p. {self.page_start}"
                if not self.page_end or self.page_end == self.page_start
                else f"pp. {self.page_start}-{self.page_end}"
            )
            parts.append(pages)
        if self.section_path:
            parts.append(f"§ {self.section_path}")
        return ", ".join(parts)


@dataclass
class Budget:
    max_steps: int = field(default_factory=lambda: settings.agent_max_steps)
    max_tokens: int = field(default_factory=lambda: settings.agent_token_budget)
    time_limit_s: float = field(default_factory=lambda: settings.agent_time_limit_s)
    steps: int = 0
    tokens: int = 0
    started: float = field(default_factory=time.monotonic)

    def add_tokens(self, n: int) -> None:
        self.tokens += max(0, int(n))

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def steps_left(self) -> int:
        return max(0, self.max_steps - self.steps)

    def exhausted(self) -> str | None:
        if self.steps >= self.max_steps:
            return f"step limit ({self.max_steps}) reached"
        if self.tokens >= self.max_tokens:
            return f"token budget ({self.max_tokens}) spent"
        if self.elapsed() >= self.time_limit_s:
            return f"time limit ({self.time_limit_s:.0f}s) reached"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {"steps": self.steps, "tokens": self.tokens, "seconds": round(self.elapsed(), 1)}


class Workspace:
    def __init__(self) -> None:
        self.passages: dict[str, Passage] = {}
        self.evidence: list[str] = []
        self._dedupe: dict[tuple[Any, ...], str] = {}
        self.refs: dict[str, str] = {}       # "S4" / "D1" -> UUID
        self._ref_of: dict[str, str] = {}    # UUID -> "S4"

    # -- short references -------------------------------------------------
    def ref(self, prefix: str, uuid: str) -> str:
        if uuid in self._ref_of:
            return self._ref_of[uuid]
        n = sum(1 for r in self.refs if r.startswith(prefix)) + 1
        ref = f"{prefix}{n}"
        self.refs[ref] = uuid
        self._ref_of[uuid] = ref
        return ref

    def resolve(self, ref_or_id: str) -> str | None:
        ref_or_id = (ref_or_id or "").strip()
        return self.refs.get(ref_or_id.upper()) or (ref_or_id if ref_or_id in self._ref_of else None)

    # -- passages / evidence ---------------------------------------------
    def add_passage(self, **fields: Any) -> Passage:
        key = (fields.get("document_id"), fields.get("chunk_id"), fields.get("page_start"), fields["text"][:200])
        if key in self._dedupe:
            return self.passages[self._dedupe[key]]
        pid = f"P{len(self.passages) + 1}"
        passage = Passage(id=pid, **fields)
        self.passages[pid] = passage
        self._dedupe[key] = pid
        return passage

    def add_evidence(self, ids: list[str], note: str | None = None) -> tuple[list[str], list[str]]:
        added, unknown = [], []
        for raw in ids:
            pid = str(raw).strip().upper()
            if pid not in self.passages:
                unknown.append(str(raw))
                continue
            if pid not in self.evidence:
                self.evidence.append(pid)
                if note:
                    self.passages[pid].note = note
            added.append(pid)
        return added, unknown

    def evidence_passages(self) -> list[Passage]:
        return [self.passages[p] for p in self.evidence]
