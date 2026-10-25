"""
Evaluate DocMind on a long PDF: page recall and citation accuracy per mode.

You provide the PDF and a JSON file of questions with the pages that answer them:

    [
      {"question": "What was revenue in 2024?", "pages": [42]},
      {"question": "List every risk factor named in the report", "pages": [12, 13, 14, 88]}
    ]

The script uploads the PDF (or reuses --document-id), waits for indexing, asks
every question in each mode and reports:

  page recall        share of the expected pages that the answer cites
  citation accuracy  share of citations whose quote really appears on the cited pages
  latency / steps / tokens per answer

Usage (server running on :8000):
    python scripts/eval.py --pdf report.pdf --questions questions.json
    python scripts/eval.py --document-id <uuid> --questions questions.json --modes fast,deep
    python scripts/eval.py ... --out eval-report.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


class Client:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=600)
        self._pages: dict[tuple[str, int], str] = {}

    def upload(self, pdf: Path) -> str:
        with pdf.open("rb") as fh:
            r = self.http.post("/api/documents/upload", files={"file": (pdf.name, fh, "application/pdf")})
        r.raise_for_status()
        return r.json()["document_id"]

    def wait_ready(self, doc_id: str, timeout_s: float = 1800) -> dict[str, Any]:
        started, last = time.time(), None
        while time.time() - started < timeout_s:
            info = self.http.get(f"/api/documents/{doc_id}/status").json()
            if info["stage"] != last:
                print(f"  indexing: {info['status']} {info['stage'] or ''}", file=sys.stderr)
                last = info["stage"]
            if info["status"] in ("ready", "failed"):
                return info
            time.sleep(2)
        raise TimeoutError("indexing did not finish")

    def ask(self, question: str, doc_id: str, mode: str) -> dict[str, Any]:
        r = self.http.post("/api/query", json={"query": question, "document_ids": [doc_id], "mode": mode})
        r.raise_for_status()
        return r.json()

    def page_text(self, doc_id: str, page: int) -> str:
        key = (doc_id, page)
        if key not in self._pages:
            r = self.http.get(f"/api/documents/{doc_id}/pages/{page}")
            self._pages[key] = r.json()["text"] if r.status_code == 200 else ""
        return self._pages[key]


def cited_pages(resp: dict[str, Any]) -> set[int]:
    items = resp.get("citations") or resp.get("sources") or []
    pages: set[int] = set()
    for c in items:
        if c.get("page_start"):
            pages.update(range(c["page_start"], (c.get("page_end") or c["page_start"]) + 1))
    return pages


def quote_supported(quote: str, page_text: str) -> bool:
    """Exact (whitespace/case-insensitive) match, or ≥80% of the quote's words present in order-free form."""
    q = norm(quote.replace("…[truncated]", ""))[:160]
    if not q:
        return False
    if q in norm(page_text):
        return True
    qw = words(q)
    pw = set(words(page_text))
    return bool(qw) and sum(w in pw for w in qw) / len(qw) >= 0.8


def citation_accuracy(client: Client, doc_id: str, resp: dict[str, Any]) -> float | None:
    cites = resp.get("citations") or []
    if not cites:
        return None
    ok = 0
    for c in cites:
        start = c.get("page_start")
        if not start:
            continue
        end = c.get("page_end") or start
        text = "\n".join(client.page_text(doc_id, p) for p in range(start, min(end, start + 9) + 1))
        ok += quote_supported(c.get("quote", ""), text)
    return ok / len(cites)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", required=True, type=Path)
    ap.add_argument("--pdf", type=Path)
    ap.add_argument("--document-id")
    ap.add_argument("--modes", default="fast,deep,exhaustive")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    if not args.pdf and not args.document_id:
        ap.error("give --pdf or --document-id")

    questions = json.loads(args.questions.read_text())
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    client = Client(args.base_url)

    doc_id = args.document_id
    if not doc_id:
        print(f"Uploading {args.pdf} …", file=sys.stderr)
        doc_id = client.upload(args.pdf)
    info = client.wait_ready(doc_id)
    if info["status"] != "ready":
        sys.exit(f"Indexing failed: {info.get('error')}")
    print(f"Document {doc_id}: {info['page_count']} pages, {info['section_count']} sections, "
          f"{info['chunk_count']} chunks", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for q in questions:
        expected = set(q.get("pages", []))
        for mode in q.get("modes", modes):
            t0 = time.time()
            try:
                resp = client.ask(q["question"], doc_id, mode)
            except httpx.HTTPError as exc:
                print(f"  ! {mode}: {exc}", file=sys.stderr)
                continue
            pages = cited_pages(resp)
            row = {
                "question": q["question"],
                "mode": mode,
                "mode_used": resp.get("mode"),
                "expected_pages": sorted(expected),
                "cited_pages": sorted(pages),
                "page_recall": (len(expected & pages) / len(expected)) if expected else None,
                "citation_accuracy": citation_accuracy(client, doc_id, resp),
                "citations": len(resp.get("citations") or []),
                "removed_claims": len(resp.get("removed_claims") or []),
                "steps": len(resp.get("steps") or []),
                "tokens": (resp.get("budget") or {}).get("tokens"),
                "latency_s": round(time.time() - t0, 1),
                "answer": resp.get("answer", ""),
            }
            rows.append(row)
            pr = "-" if row["page_recall"] is None else f"{row['page_recall']:.2f}"
            ca = "-" if row["citation_accuracy"] is None else f"{row['citation_accuracy']:.2f}"
            print(f"  [{mode:10}] recall={pr} cite_acc={ca} steps={row['steps']} "
                  f"{row['latency_s']}s  {q['question'][:60]}", file=sys.stderr)

    print("\nmode         n   page_recall  citation_acc  avg_steps  avg_latency_s")
    summary: dict[str, Any] = {}
    for mode in modes:
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue

        def avg(key: str) -> float | None:
            vals = [r[key] for r in rs if r[key] is not None]
            return round(statistics.mean(vals), 3) if vals else None

        summary[mode] = {
            "n": len(rs), "page_recall": avg("page_recall"), "citation_accuracy": avg("citation_accuracy"),
            "avg_steps": avg("steps"), "avg_latency_s": avg("latency_s"),
        }
        s = summary[mode]
        print(f"{mode:12} {s['n']:<3} {s['page_recall']!s:<12} {s['citation_accuracy']!s:<13} "
              f"{s['avg_steps']!s:<10} {s['avg_latency_s']}")

    if args.out:
        args.out.write_text(json.dumps({"document_id": doc_id, "summary": summary, "rows": rows}, indent=2))
        print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
