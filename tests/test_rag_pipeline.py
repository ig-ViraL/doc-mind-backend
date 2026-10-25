import json
from types import SimpleNamespace

import pytest

from services import rag_pipeline as rp
from services import retrieval
from services.agent import answer as answer_mod
from services.agent.workspace import Passage


def _chunk(i, text="alpha", filename="a.pdf"):
    return {
        "chunk_id": f"c{i}", "chunk_index": i, "text": text, "score": 0.5, "document_id": "d1",
        "filename": filename, "page_start": 3, "page_end": 3, "section_path": "Intro", "header": None,
    }


class OneShotStore:
    def __init__(self, hits):
        self.hits = hits

    async def search(self, query, k):
        return self.hits[:k]


@pytest.mark.parametrize("q", ["hi", "Hello!", "who are you?", "what can you do", "Tell me about yourself"])
def test_meta_queries_detected(q):
    assert rp._is_meta_query(q)


@pytest.mark.parametrize("q", ["What is the refund policy?", "summarise chapter 2", "hi-tech revenue in 2023"])
def test_document_queries_not_meta(q):
    assert not rp._is_meta_query(q)


def test_prompt_numbers_evidence_with_pages_and_sections():
    p = Passage(id="P1", document_id="d", filename="doc.pdf", text="because", page_start=4, page_end=5, section_path="2 Scope")
    user = answer_mod.build_messages("why?", [p])[1]["content"]
    assert "[1] doc.pdf, pp. 4-5, § 2 Scope" in user and "because" in user and "Question: why?" in user


def test_prompt_without_evidence_forbids_answering():
    user = answer_mod.build_messages("why?", [])[1]["content"]
    assert "No relevant passages" in user


def test_rerank_orders_by_score_and_degrades_gracefully(monkeypatch):
    chunks = [_chunk(0, "low"), _chunk(1, "high")]

    class FakeCE:
        def predict(self, pairs, **kw):
            import numpy as np
            return np.array([0.1 if "low" in p[1] else 0.9 for p in pairs])

    monkeypatch.setattr(retrieval, "_cross_encoder", FakeCE())
    assert [c["text"] for c in retrieval.rerank("q", chunks)] == ["high", "low"]

    class Broken:
        def predict(self, pairs, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(retrieval, "_cross_encoder", Broken())
    assert retrieval.rerank("q", chunks) == chunks  # falls back to original order


def test_rerank_uses_section_header(monkeypatch):
    seen = []

    class FakeCE:
        def predict(self, pairs, **kw):
            seen.extend(pairs)
            return [0.0 for _ in pairs]

    monkeypatch.setattr(retrieval, "_cross_encoder", FakeCE())
    retrieval.rerank("q", [{**_chunk(0, "body"), "header": "Document: a.pdf\nSection: X"}])
    assert seen[0][1] == "Document: a.pdf\nSection: X\n\nbody"


async def _collect(gen):
    return [item async for item in gen]


async def test_fast_stream_emits_mode_sources_tokens_citations_done(monkeypatch):
    async def fake_stream():
        for t in ["Hel", "lo [1]"]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=t))])

    async def fake_create(**kw):
        assert kw["stream"] is True
        return fake_stream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(rp, "get_openrouter_client", lambda: fake_client)

    events = await _collect(rp.stream_answer(
        "what is ctx?", None, 5, session=None, mode="fast", store=OneShotStore([_chunk(0, "ctx")])
    ))
    assert [e for e, _ in events] == ["mode", "sources", "token", "token", "citations", "done"]
    src = events[1][1]["sources"][0]
    assert src["filename"] == "a.pdf" and src["page_start"] == 3 and src["marker"] == 1
    assert "".join(d["text"] for e, d in events if e == "token") == "Hello [1]"
    cites = events[4][1]["citations"]
    assert cites[0]["marker"] == 1 and cites[0]["page_start"] == 3
    for _, data in events:
        json.dumps(data)  # payloads must be JSON-serialisable


async def test_meta_query_skips_retrieval():
    events = await _collect(rp.stream_answer("who are you?", None, 5, session=None, mode="deep", store=None))
    assert [e for e, _ in events] == ["mode", "sources", "token", "citations", "done"]
    assert "DocMind" in events[2][1]["text"]
