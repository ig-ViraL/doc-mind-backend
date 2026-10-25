import json
from types import SimpleNamespace

import pytest

from services import rag_pipeline as rp


def _chunk(i, text="alpha", filename="a.pdf"):
    return {"chunk_index": i, "text": text, "score": 0.5, "document_id": "d1", "filename": filename}


@pytest.mark.parametrize("q", ["hi", "Hello!", "who are you?", "what can you do", "Tell me about yourself"])
def test_meta_queries_detected(q):
    assert rp._is_meta_query(q)


@pytest.mark.parametrize("q", ["What is the refund policy?", "summarise chapter 2", "hi-tech revenue in 2023"])
def test_document_queries_not_meta(q):
    assert not rp._is_meta_query(q)


def test_user_message_includes_sources_and_question():
    msg = rp._build_user_message("why?", [_chunk(0, "because", "doc.pdf")])
    assert "[Source: doc.pdf]" in msg and "because" in msg and "Question: why?" in msg


def test_user_message_without_chunks_forbids_answering():
    msg = rp._build_user_message("why?", [])
    assert "No relevant excerpts" in msg


def test_rerank_orders_by_score_and_degrades_gracefully(monkeypatch):
    chunks = [_chunk(0, "low"), _chunk(1, "high")]

    class FakeCE:
        def predict(self, pairs, **kw):
            import numpy as np
            return np.array([0.1 if "low" in p[1] else 0.9 for p in pairs])

    monkeypatch.setattr(rp, "_cross_encoder", FakeCE())
    assert [c["text"] for c in rp._rerank("q", chunks)] == ["high", "low"]

    class Broken:
        def predict(self, pairs, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(rp, "_cross_encoder", Broken())
    assert rp._rerank("q", chunks) == chunks  # falls back to original order


async def _collect(gen):
    return [item async for item in gen]


@pytest.mark.asyncio
async def test_stream_answer_emits_sources_tokens_done(monkeypatch):
    async def fake_retrieve(*a, **kw):
        return [_chunk(0, "ctx")]

    async def fake_stream():
        for t in ["Hel", "lo"]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=t))])

    async def fake_create(**kw):
        assert kw["stream"] is True
        return fake_stream()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(rp, "_retrieve", fake_retrieve)
    monkeypatch.setattr(rp, "get_openrouter_client", lambda: fake_client)

    events = await _collect(rp.stream_answer("what is ctx?", None, 5, session=None))
    names = [e for e, _ in events]
    assert names == ["sources", "token", "token", "done"]
    assert events[0][1]["sources"][0]["filename"] == "a.pdf"
    assert "".join(d["text"] for e, d in events if e == "token") == "Hello"
    json.dumps(events[-1][1])  # payloads must be JSON-serialisable


@pytest.mark.asyncio
async def test_stream_answer_meta_query_skips_retrieval(monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("retrieval must not run for meta queries")

    monkeypatch.setattr(rp, "_retrieve", boom)
    events = await _collect(rp.stream_answer("who are you", None, 5, session=None))
    assert [e for e, _ in events] == ["sources", "token", "done"]
