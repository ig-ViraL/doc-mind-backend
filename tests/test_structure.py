from services import structure
from services.chunker import TokenChunker
from services.parser import Heading, _parse_txt, _text_heading_levels


def _doc():
    pages = [
        "Annual Report\nIntro text that is short.",
        "1 Overview\nThe company grew revenue by 12% in 2024. " * 3,
        "1.1 Markets\nWe sell in Europe and Asia. " * 4,
        "2 Risks\nCurrency risk is the main exposure. " * 3,
    ]
    headings = [
        Heading(1, "1 Overview", 2),
        Heading(2, "1.1 Markets", 3),
        Heading(1, "2 Risks", 4),
    ]
    return pages, headings


def _sections():
    pages, headings = _doc()
    text, offsets = structure.assemble(pages)
    located = structure.locate_headings(text, offsets, headings)
    return text, offsets, structure.build_sections(text, offsets, located)


def test_assemble_and_page_at():
    text, offsets = structure.assemble(["aaa", "bbb", "ccc"])
    assert text == "aaa\n\nbbb\n\nccc"
    assert offsets == [0, 5, 10]
    assert structure.page_at(offsets, 0) == 1
    assert structure.page_at(offsets, 6) == 2
    assert structure.page_at(offsets, 12) == 3


def test_headings_are_located_on_their_pages():
    pages, headings = _doc()
    text, offsets = structure.assemble(pages)
    located = structure.locate_headings(text, offsets, headings)
    assert [t for _, _, t in located] == ["1 Overview", "1.1 Markets", "2 Risks"]
    for (pos, _, title), h in zip(located, headings):
        assert text[pos:].startswith(title)
        assert structure.page_at(offsets, pos) == h.page


def test_section_tree_parents_paths_and_pages():
    _, _, secs = _sections()
    titles = [s.title for s in secs]
    assert titles == ["1 Overview", "1.1 Markets", "2 Risks"]
    overview, markets, risks = secs
    assert overview.parent is None and markets.parent == 0 and risks.parent is None
    assert markets.path_str == "1 Overview > 1.1 Markets"
    # Short preamble on page 1 folds into the first section.
    assert overview.start == 0 and overview.page_start == 1
    assert overview.page_end == 3  # includes its child on page 3
    assert risks.page_start == risks.page_end == 4
    # Own body of the parent stops where the child begins.
    assert overview.body_end == markets.start


def test_long_preamble_becomes_front_matter():
    pages = ["Preface " * 60, "1 Intro\nbody " * 10]
    text, offsets = structure.assemble(pages)
    located = structure.locate_headings(text, offsets, [Heading(1, "1 Intro", 2)])
    secs = structure.build_sections(text, offsets, located)
    assert secs[0].title == "Front matter"
    assert secs[1].title == "1 Intro"


def test_no_headings_falls_back_to_page_groups():
    pages = [f"page {i} text" for i in range(1, 13)]
    text, offsets = structure.assemble(pages)
    secs = structure.build_sections(text, offsets, [])
    assert [s.title for s in secs] == ["Pages 1–5", "Pages 6–10", "Pages 11–12"]
    assert secs[-1].page_end == 12


def test_chunks_stay_inside_sections_and_carry_pages():
    text, offsets, secs = _sections()
    chunks = structure.chunk_sections(text, offsets, secs, TokenChunker(30, 5), "report.pdf")
    assert chunks
    for c in chunks:
        sec = secs[c["section_ordinal"]]
        assert sec.body_start <= c["start_char"] < c["end_char"] <= sec.body_end
        assert c["header"] == f"Document: report.pdf\nSection: {sec.path_str}"
        assert sec.page_start <= c["page_start"] <= c["page_end"]
    risk_chunks = [c for c in chunks if c["section_path"] == "2 Risks"]
    assert risk_chunks and all(c["page_start"] == 4 for c in risk_chunks)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_text_heading_heuristics():
    levels = _text_heading_levels([
        "# Title", "## Sub", "2.3 Scope of work", "INTRODUCTION", "A normal sentence.", "x",
    ])
    assert levels == {0: 1, 1: 2, 2: 2, 3: 1}


def test_parse_txt_builds_headings_and_virtual_pages():
    body = "Some words here. " * 120
    raw = f"# Guide\n\n{body}\n\n## Install\n\n{body}\n\n## Usage\n\n{body}".encode()
    parsed = _parse_txt(raw, "guide.md")
    assert parsed.has_real_pages is False
    assert parsed.page_count >= 2
    assert [h.title for h in parsed.headings] == ["Guide", "Install", "Usage"]
    assert [h.level for h in parsed.headings] == [1, 2, 2]
