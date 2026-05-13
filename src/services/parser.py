"""
Document parser — extracts text, tables, images, and links from PDF/DOCX/TXT.

Blocking document libraries (pypdf, pdfplumber, pymupdf, python-docx) are
offloaded to threads via asyncio.to_thread; vision LLM calls run concurrently
on the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any

from fastapi import UploadFile

from config import settings

logger = logging.getLogger(__name__)


async def parse(file: UploadFile) -> dict[str, Any]:
    """
    Parse an uploaded file and return enriched text with metadata.

    Returns:
        {"text": str, "metadata": {"filename": str, "page_count": int, "file_type": str}}

    Raises ValueError for oversized, unsupported, or unparseable files.
    """
    content_type: str = file.content_type or ""
    filename: str = file.filename or "unknown"
    raw_bytes: bytes = await file.read()

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise ValueError(
            f"File '{filename}' exceeds the {settings.max_file_size_mb} MB limit "
            f"({len(raw_bytes) // (1024 * 1024)} MB)."
        )

    logger.info("Parsing '%s' (content_type=%s, size=%d bytes)", filename, content_type, len(raw_bytes))

    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return await _parse_pdf(raw_bytes, filename)

    if (
        content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or filename.lower().endswith(".docx")
    ):
        return await _parse_docx(raw_bytes, filename)

    if content_type in ("text/plain", "text/plain; charset=utf-8") or filename.lower().endswith(".txt"):
        return _parse_txt(raw_bytes, filename)

    logger.warning("Unsupported content_type '%s' — attempting unstructured fallback", content_type)
    return await asyncio.to_thread(_parse_unstructured, raw_bytes, filename, content_type)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _sync_extract_pdf(
    raw_bytes: bytes,
) -> tuple[int, dict[int, list[str]], list[tuple[int, bytes]], list[tuple[int, str]], list[str]]:
    """
    All blocking PDF extraction in a single thread-pool call.
    Returns: (page_count, tables_by_page, raw_images, pages_text, links)
    """
    import fitz  # pymupdf
    import pdfplumber
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    page_count = len(reader.pages)

    # Text + links in one pass
    pages_text: list[tuple[int, str]] = []
    links: list[str] = []
    try:
        for page_num_0, page in enumerate(reader.pages):
            pages_text.append((page_num_0 + 1, (page.extract_text() or "").strip()))
            annots = page.get("/Annots")
            if annots:
                for annot_ref in annots:
                    annot = annot_ref.get_object()
                    if annot.get("/Subtype") == "/Link":
                        action = annot.get("/A")
                        if action and "/URI" in action:
                            url = action["/URI"]
                            if isinstance(url, bytes):
                                url = url.decode("utf-8", errors="ignore")
                            links.append(url)
    except Exception as exc:
        logger.warning("PDF text/link extraction partially failed: %s", exc)

    tables_by_page: dict[int, list[str]] = {}
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                md_tables = [_table_to_markdown(t) for t in (page.extract_tables() or []) if t]
                if md_tables:
                    tables_by_page[page_num] = md_tables
    except Exception as exc:
        logger.warning("PDF table extraction skipped: %s", exc)

    raw_images: list[tuple[int, bytes]] = []
    try:
        fitz_doc = fitz.open(stream=raw_bytes, filetype="pdf")
        try:
            for page_num_0, page in enumerate(fitz_doc):
                for img_info in page.get_images(full=True):
                    try:
                        img_bytes = fitz_doc.extract_image(img_info[0])["image"]
                        if len(img_bytes) >= settings.min_image_bytes:
                            raw_images.append((page_num_0 + 1, img_bytes))
                    except Exception:
                        pass
        finally:
            fitz_doc.close()
    except Exception as exc:
        logger.warning("PDF image extraction skipped: %s", exc)

    return page_count, tables_by_page, raw_images, pages_text, links


async def _parse_pdf(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    try:
        page_count, tables_by_page, raw_images, pages_text, links = await asyncio.to_thread(
            _sync_extract_pdf, raw_bytes
        )

        images_by_page: dict[int, list[str]] = {}
        if settings.extract_images and raw_images:
            descs = await asyncio.gather(
                *[_describe_image(img) for _, img in raw_images],
                return_exceptions=True,
            )
            for (page_num, _), result in zip(raw_images, descs):
                if isinstance(result, str) and result:
                    images_by_page.setdefault(page_num, []).append(result)

        sections: list[str] = []
        for page_num, page_text in pages_text:
            if page_text:
                sections.append(f"[Page {page_num}]\n{page_text}")
            for i, md_table in enumerate(tables_by_page.get(page_num, []), start=1):
                sections.append(f"[TABLE {i} | Page {page_num}]\n{md_table}")
            for i, desc in enumerate(images_by_page.get(page_num, []), start=1):
                sections.append(f"[IMAGE {i} | Page {page_num}]\n{desc}")

        if links:
            unique_links = list(dict.fromkeys(links))
            sections.append("[HYPERLINKS]\n" + "\n".join(f"• {u}" for u in unique_links))

        text = "\n\n".join(sections)
        logger.info(
            "PDF '%s': pages=%d tables=%d images=%d links=%d chars=%d",
            filename, page_count,
            sum(len(v) for v in tables_by_page.values()),
            sum(len(v) for v in images_by_page.values()),
            len(links), len(text),
        )
        return {"text": text, "metadata": {"filename": filename, "page_count": page_count, "file_type": "pdf"}}

    except Exception as exc:
        logger.exception("PDF parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Failed to parse PDF '{filename}': {exc}") from exc


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _sync_extract_docx(
    raw_bytes: bytes,
) -> tuple[list[str], list[str], list[bytes], list[tuple[str, str]]]:
    """
    All blocking DOCX extraction in a single thread-pool call.
    Returns: (body_paragraphs, tables_md, raw_images, links)
    """
    import docx
    from docx.oxml.ns import qn

    doc = docx.Document(io.BytesIO(raw_bytes))

    body_paras = [p.text for p in doc.paragraphs if p.text.strip()]

    tables_md: list[str] = []
    for table in doc.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        md = _table_to_markdown(rows)
        if md:
            tables_md.append(md)

    raw_images: list[bytes] = []
    for rel in doc.part.rels.values():
        if "image" in rel.reltype.lower():
            try:
                img_bytes = rel.target_part.blob
                if len(img_bytes) >= settings.min_image_bytes:
                    raw_images.append(img_bytes)
            except Exception:
                pass

    # python-docx exposes hyperlinks via internal XML; no public API exists for this
    links: list[tuple[str, str]] = []
    try:
        for para in doc.paragraphs:
            for hl in para._element.findall(f'.//{{{qn("w")}}}hyperlink'):
                r_id = hl.get(f'{{{qn("r")}}}id')
                if r_id and r_id in doc.part.rels:
                    url = doc.part.rels[r_id].target_ref
                    anchor = "".join(
                        t.text for t in hl.findall(f'.//{{{qn("w")}}}t') if t.text
                    ).strip()
                    links.append((anchor or url, url))
    except Exception as exc:
        logger.warning("DOCX link extraction skipped: %s", exc)

    return body_paras, tables_md, raw_images, links


async def _parse_docx(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    try:
        body_paras, tables_md, raw_images, links = await asyncio.to_thread(
            _sync_extract_docx, raw_bytes
        )

        sections: list[str] = []

        if body_paras:
            sections.append("\n\n".join(body_paras))

        for i, md in enumerate(tables_md, start=1):
            sections.append(f"[TABLE {i}]\n{md}")

        if settings.extract_images and raw_images:
            descs = await asyncio.gather(
                *[_describe_image(img) for img in raw_images],
                return_exceptions=True,
            )
            for i, result in enumerate(descs, start=1):
                if isinstance(result, str) and result:
                    sections.append(f"[IMAGE {i}]\n{result}")

        if links:
            unique = list(dict.fromkeys(links))
            link_lines = "\n".join(
                f"• {anchor} → {url}" if anchor != url else f"• {url}"
                for anchor, url in unique
            )
            sections.append(f"[HYPERLINKS]\n{link_lines}")

        text = "\n\n".join(sections)
        logger.info(
            "DOCX '%s': tables=%d images=%d links=%d chars=%d",
            filename, len(tables_md), len(raw_images), len(links), len(text),
        )
        return {"text": text, "metadata": {"filename": filename, "page_count": 0, "file_type": "docx"}}

    except Exception as exc:
        logger.exception("DOCX parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Failed to parse DOCX '{filename}': {exc}") from exc


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------


def _parse_txt(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            text = raw_bytes.decode(encoding)
            logger.debug("TXT '%s' decoded with %s: %d chars", filename, encoding, len(text))
            return {"text": text, "metadata": {"filename": filename, "page_count": 0, "file_type": "txt"}}
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Unable to decode text file '{filename}' with any supported encoding")


# ---------------------------------------------------------------------------
# Unstructured fallback
# ---------------------------------------------------------------------------


def _parse_unstructured(raw_bytes: bytes, filename: str, content_type: str) -> dict[str, Any]:
    try:
        from unstructured.partition.auto import partition

        elements = partition(file=io.BytesIO(raw_bytes), content_type=content_type)
        text = "\n\n".join(str(el) for el in elements if str(el).strip())
        logger.debug("Unstructured '%s': %d elements, %d chars", filename, len(elements), len(text))
        return {
            "text": text,
            "metadata": {"filename": filename, "page_count": 0, "file_type": content_type.split("/")[-1]},
        }
    except Exception as exc:
        logger.exception("Unstructured parsing failed for '%s': %s", filename, exc)
        raise ValueError(
            f"Unsupported file type '{content_type}' for '{filename}'. "
            "Supported types: PDF, DOCX, TXT."
        ) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_to_markdown(rows: list[list[Any]]) -> str:
    """Convert a list-of-rows into a GitHub-flavoured Markdown table."""
    if not rows:
        return ""

    def clean(cell: Any) -> str:
        return str(cell or "").replace("|", "\\|").replace("\n", " ").strip()

    cleaned = [[clean(cell) for cell in row] for row in rows]
    col_count = max(len(row) for row in cleaned)
    padded = [row + [""] * (col_count - len(row)) for row in cleaned]

    header, body = padded[0], padded[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *("| " + " | ".join(row) + " |" for row in body),
    ]
    return "\n".join(lines)


async def _describe_image(img_bytes: bytes) -> str:
    """
    Send a single image to the vision LLM and return a textual description.
    Returns an empty string on failure so callers can safely filter.
    """
    from services.llm_client import get_openrouter_client

    try:
        if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif img_bytes[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif img_bytes[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/jpeg"

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        client = get_openrouter_client()
        response = await client.chat.completions.create(
            model=settings.vision_model,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {
                            "type": "text",
                            "text": (
                                "Describe this image concisely. "
                                "If it contains text, transcribe it exactly. "
                                "If it is a chart or graph, summarize the data and trends. "
                                "If it is a diagram or flowchart, explain the structure."
                            ),
                        },
                    ],
                }
            ],
        )
        desc = (response.choices[0].message.content or "").strip()
        logger.debug("Image described: %d chars", len(desc))
        return desc

    except Exception as exc:
        logger.warning("Image description failed: %s", exc)
        return ""
