import json

import pytest
from fakes import FakeStore, ScriptedLLM, text_turn, tool_turn

from config import settings
from services import llm
from services import rag_pipeline as rp
from services.agent import answer as answer_mod
from services.agent.deep import run_deep
from services.agent.exhaustive import build_map_units, run_exhaustive
from services.agent.router import heuristic_mode, route
from services.agent.workspace import Budget, Passage, Workspace


async def _collect(gen):
    return [item async for item in gen]


# ---------------------------------------------------------------------------
# Deep tool loop
# ---------------------------------------------------------------------------


async def test_deep_loop_runs_tools_keeps_evidence_and_finishes(monkeypatch):
    script = ScriptedLLM(planner=[
        tool_turn(("outline", {"document": "D1"}), ("search", {"query": "revenue 2024"})),
        tool_turn(("add_evidence", {"passages": ["P1"], "note": "revenue"}), ("finish", {})),
        tool_turn(("search", {"query": "never reached"})),
    ])
    monkeypatch.setattr(llm, "chat", script)
    ws, budget = Workspace(), Budget(max_steps=8, max_tokens=10_000, time_limit_s=60)

    events = await _collect(run_deep("What was revenue in 2024?", FakeStore(), ws, budget))

    tools = [d["tool"] for _, d in events]
    assert tools == ["outline", "search", "add_evidence"]
    assert all(e == "step" for e, _ in events)
    assert [d["n"] for _, d in events] == [1, 2, 3]
    assert ws.evidence == ["P1"] and "$12.4M" in ws.passages["P1"].text
    assert budget.steps == 3 and budget.tokens == 200
    # The planner saw tool results for every call it made (OpenAI tool-call protocol).
    assert script.calls[1]["n_messages"] == 2 + 1 + 2  # system, user, assistant, 2 tool results


async def test_deep_loop_stops_at_step_limit(monkeypatch):
    many = [tool_turn(("search", {"query": f"q{i}"}), ("search", {"query": f"r{i}"})) for i in range(10)]
    monkeypatch.setattr(llm, "chat", ScriptedLLM(planner=many))
    ws, budget = Workspace(), Budget(max_steps=3, max_tokens=10**6, time_limit_s=60)

    events = await _collect(run_deep("anything about steel", FakeStore(), ws, budget))

    tool_steps = [d for _, d in events if d["tool"] == "search"]
    assert len(tool_steps) == 3
    assert any(d["tool"] == "budget" and "step limit" in d["summary"] for _, d in events)


async def test_deep_loop_stops_at_token_budget(monkeypatch):
    many = [tool_turn(("search", {"query": "steel"}), tokens=600) for _ in range(10)]
    monkeypatch.setattr(llm, "chat", ScriptedLLM(planner=many))
    ws, budget = Workspace(), Budget(max_steps=8, max_tokens=1000, time_limit_s=60)

    events = await _collect(run_deep("steel?", FakeStore(), ws, budget))

    assert budget.steps == 2
    assert events[-2][1]["tool"] == "budget" and "token budget" in events[-2][1]["summary"]


async def test_deep_loop_auto_keeps_top_passages_when_planner_keeps_none(monkeypatch):
    monkeypatch.setattr(llm, "chat", ScriptedLLM(planner=[tool_turn(("search", {"query": "risks currency"}))]))
    ws = Workspace()
    events = await _collect(run_deep("risks?", FakeStore(), ws, Budget()))
    assert ws.evidence
    assert events[-1][1]["summary"].startswith("auto-kept")


# ---------------------------------------------------------------------------
# Exhaustive map-reduce
# ---------------------------------------------------------------------------


def test_map_units_pack_small_sections_and_respect_cap():
    sections = [
        {"document_id": "d", "filename": "f", "path": f"S{i}", "page_start": i, "page_end": i, "body": "word " * 300}
        for i in range(1, 21)
    ]
    units = build_map_units(sections, unit_tokens=1000, max_units=50)
    assert 1 < len(units) < 20  # neighbours merged
    assert units[0]["page_start"] == 1 and units[-1]["page_end"] == 20
    capped = build_map_units(sections, unit_tokens=400, max_units=3)
    assert len(capped) <= 3


async def test_exhaustive_maps_every_section_and_keeps_findings(monkeypatch):
    def mapper(prompt):
        text = prompt.split("---", 1)[1]  # the section text, not the question
        if "Dana Reyes" in text:
            page = 5 if "Risks" in text else 4
            return {"findings": [{"fact": "Dana Reyes is mentioned", "quote": "Dana Reyes", "page": page}]}
        return {"findings": []}

    monkeypatch.setattr(llm, "chat", ScriptedLLM(mapper=mapper))
    monkeypatch.setattr(settings, "exhaustive_unit_tokens", 20)  # one unit per section
    monkeypatch.setattr(settings, "map_concurrency", 2)
    ws, budget = Workspace(), Budget(max_steps=8, max_tokens=10**6, time_limit_s=60)

    events = await _collect(run_exhaustive("List every mention of Dana Reyes", FakeStore(), ws, budget))

    assert events[0][1]["tool"] == "plan"
    maps = [d for _, d in events if d["tool"] == "map"]
    assert len(maps) == budget.steps == 2  # 4 sections / 2 per batch
    pages = sorted(p.page_start for p in ws.evidence_passages())
    assert pages == [4, 5]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q,mode", [
    ("What is the CFO's name?", "fast"),
    ("List all risks mentioned in the report", "exhaustive"),
    ("Summarize the whole document", "exhaustive"),
    ("Compare revenue growth with cost growth", "deep"),
    ("Why did steel costs rise?", "deep"),
    ("What does section 2.1 say about suppliers?", "deep"),
])
def test_router_heuristics(q, mode):
    assert heuristic_mode(q)[0] == mode


async def test_router_asks_model_for_ambiguous_questions(monkeypatch):
    monkeypatch.setattr(llm, "chat", ScriptedLLM(router={"mode": "exhaustive", "reason": "needs all"}))
    q = "Give me the obligations the supplier has under this agreement and the dates they apply from"
    assert heuristic_mode(q)[0] is None
    assert await route(q) == ("exhaustive", "needs all")


# ---------------------------------------------------------------------------
# Checker and citations
# ---------------------------------------------------------------------------


def _p(i, page):
    return Passage(id=f"P{i}", document_id="d", filename="acme.pdf", text=f"text {i}", page_start=page, page_end=page)


def test_remove_sentences_keeps_structure():
    answer = "Revenue was $12.4M [1]. The CEO is Bob Smith [2].\n\n- Steel costs rose 8% [3].\n- Headcount is 900 [1]."
    claims = answer_mod.numbered_claims(answer)
    assert len(claims) == 4
    cleaned, removed = answer_mod.remove_sentences(answer, {2, 4})
    assert removed == ["The CEO is Bob Smith [2].", "- Headcount is 900 [1]."]
    assert cleaned == "Revenue was $12.4M [1].\n\n- Steel costs rose 8% [3]."


async def test_check_removes_unsupported_and_survives_bad_output(monkeypatch):
    monkeypatch.setattr(llm, "chat", ScriptedLLM(check={"unsupported": [2]}))
    text, removed, _ = await answer_mod.check("Revenue rose sharply [1]. Profit doubled overnight [1].", [_p(1, 3)])
    assert text == "Revenue rose sharply [1]." and removed == ["Profit doubled overnight [1]."]

    async def garbage(*a, **kw):
        return text_turn("not json at all")

    monkeypatch.setattr(llm, "chat", garbage)
    text, removed, _ = await answer_mod.check("Revenue rose sharply [1].", [_p(1, 3)])
    assert text == "Revenue rose sharply [1]." and removed == []


def test_citations_map_used_markers_to_pages():
    passages = [_p(1, 3), _p(2, 4), _p(3, 9)]
    cites = answer_mod.citations("A [2]. B [1][2]. C [7].", passages)
    assert [(c["marker"], c["page_start"]) for c in cites] == [(1, 3), (2, 4)]


# ---------------------------------------------------------------------------
# End to end through the pipeline (deep mode, mocked LLM)
# ---------------------------------------------------------------------------


async def test_pipeline_deep_mode_end_to_end(monkeypatch):
    script = ScriptedLLM(
        planner=[
            tool_turn(("keyword_scan", {"terms": ["Dana Reyes"]})),
            tool_turn(("add_evidence", {"passages": ["P1", "P2"]}), ("finish", {})),
        ],
        answer="Dana Reyes is the CFO [1]. She leads the risk committee [2]. She founded Acme [2].",
        check={"unsupported": [3]},
    )
    monkeypatch.setattr(llm, "chat", script)

    events = await _collect(rp.stream_answer("Who is Dana Reyes and what does she do?", None, 5,
                                             session=None, mode="deep", store=FakeStore()))
    names = [e for e, _ in events]
    assert events[0] == ("mode", {"mode": "deep", "requested": "deep", "reason": "requested"})
    assert names.count("step") == 3  # keyword_scan, add_evidence, check
    assert names[-2:] == ["citations", "done"] and "token" in names

    answer = "".join(d["text"] for e, d in events if e == "token")
    assert answer == "Dana Reyes is the CFO [1]. She leads the risk committee [2]."
    cites = next(d for e, d in events if e == "citations")
    assert [c["page_start"] for c in cites["citations"]] == [4, 5]
    assert cites["removed"] == ["She founded Acme [2]."]
    done = events[-1][1]
    assert done["mode"] == "deep" and done["budget"]["steps"] == 2
    for _, data in events:
        json.dumps(data)

    resp = await rp.answer_query("Who is Dana Reyes?", None, 5, session=None, mode="deep", store=FakeStore())
    assert resp.mode == "deep"
