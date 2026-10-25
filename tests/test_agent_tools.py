from fakes import DOC, FakeStore

from services.agent.tools import TOOL_SPECS, ToolBox
from services.agent.workspace import Budget, Workspace


async def _box():
    ws = Workspace()
    box = ToolBox(FakeStore(), ws)
    listing = await box.doc_ref_list()
    return box, ws, listing


def test_tool_specs_are_valid_function_schemas():
    names = [t["function"]["name"] for t in TOOL_SPECS]
    assert names == ["outline", "search", "keyword_scan", "read_pages", "read_section", "add_evidence", "finish"]
    for t in TOOL_SPECS:
        assert t["type"] == "function"
        params = t["function"]["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])


async def test_doc_list_uses_short_refs_and_summaries():
    _, ws, listing = await _box()
    assert listing.startswith("D1: acme.pdf (5 pages)")
    assert "annual report" in listing
    assert ws.resolve("d1") == DOC


async def test_outline_assigns_section_refs_with_pages_and_summaries():
    box, ws, _ = await _box()
    text, summary = await box.run("outline", {"document": "D1"})
    assert "S1 1 Overview (p.2) — Company and headcount." in text
    assert "  S3 2.1 Costs (p.4)" in text  # nested sections are indented
    assert "4 sections" in summary
    assert ws.resolve("S2") == "s-fin"


async def test_search_registers_passages_with_locations():
    box, ws, _ = await _box()
    text, summary = await box.run("search", {"query": "revenue in 2024", "k": 3})
    assert text.startswith("[P1] acme.pdf, p. 3, § 2 Finance")
    assert "$12.4M" in text
    assert ws.passages["P1"].page_start == 3
    assert "1 passages" in summary or "passages" in summary


async def test_keyword_scan_finds_every_page_with_the_term():
    box, ws, _ = await _box()
    text, summary = await box.run("keyword_scan", {"terms": ["Dana Reyes"]})
    pages = sorted(p.page_start for p in ws.passages.values())
    assert pages == [4, 5]
    assert "2 pages" in summary


async def test_read_pages_caps_range_and_reads_text():
    box, ws, _ = await _box()
    text, summary = await box.run("read_pages", {"document": "D1", "start": 1, "end": 50})
    assert [p.page_start for p in ws.passages.values()] == [1, 2, 3, 4, 5]
    assert "p.1-5" in summary
    bad, _ = await box.run("read_pages", {"document": "D9", "start": 1})
    assert "No pages" in bad


async def test_read_section_includes_children():
    box, ws, _ = await _box()
    await box.run("outline", {"document": "D1"})
    text, summary = await box.run("read_section", {"section": "S2"})
    assert "Revenue was" in text and "Steel costs" in text
    assert "2 Finance" in summary
    missing, _ = await box.run("read_section", {"section": "S99"})
    assert "Unknown section" in missing


async def test_add_evidence_keeps_known_ids_and_reports_unknown():
    box, ws, _ = await _box()
    await box.run("search", {"query": "revenue"})
    text, _ = await box.run("add_evidence", {"passages": ["p1", "P42"], "note": "revenue figure"})
    assert ws.evidence == ["P1"]
    assert ws.passages["P1"].note == "revenue figure"
    assert "Unknown ids ignored: P42" in text


async def test_bad_arguments_and_unknown_tools_are_reported_not_raised():
    box, _, _ = await _box()
    text, _ = await box.run("search", {"nope": 1})
    assert text.startswith("Bad arguments for search")
    text, _ = await box.run("delete_everything", {})
    assert text.startswith("Unknown tool")


def test_budget_limits():
    b = Budget(max_steps=2, max_tokens=100, time_limit_s=60)
    assert b.exhausted() is None
    b.steps = 2
    assert "step limit" in b.exhausted()
    b = Budget(max_steps=8, max_tokens=100, time_limit_s=60)
    b.add_tokens(150)
    assert "token budget" in b.exhausted()
    b = Budget(max_steps=8, max_tokens=100, time_limit_s=0)
    assert "time limit" in b.exhausted()


def test_workspace_dedupes_passages():
    ws = Workspace()
    a = ws.add_passage(document_id="d", filename="f", text="same text", chunk_id="c1", page_start=1)
    b = ws.add_passage(document_id="d", filename="f", text="same text", chunk_id="c1", page_start=1)
    assert a is b and len(ws.passages) == 1
