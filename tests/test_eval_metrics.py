import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("eval_script", Path(__file__).parent.parent / "scripts" / "eval.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def test_cited_pages_expands_ranges_and_falls_back_to_sources():
    assert ev.cited_pages({"citations": [{"page_start": 3, "page_end": 5}, {"page_start": 9}]}) == {3, 4, 5, 9}
    assert ev.cited_pages({"citations": [], "sources": [{"page_start": 2, "page_end": 2}]}) == {2}


def test_quote_supported_exact_and_fuzzy():
    page = "Revenue was  $12.4M in\n2024, up from $10.1M."
    assert ev.quote_supported("revenue was $12.4M in 2024", page)
    assert ev.quote_supported("Revenue was $12.4M in 2024, up strongly", page)  # 6/7 words
    assert not ev.quote_supported("The CEO resigned in March", page)
    assert not ev.quote_supported("", page)
