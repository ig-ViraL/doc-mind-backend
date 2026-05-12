"""
Document parser — Phase 2

Extracts from PDF / DOCX / TXT:
  • Plain text
  • Tables  → Markdown table format
  • Images  → Vision LLM description (concurrent, optional via EXTRACT_IMAGES)
  • Links   → Inline anchor + URL pairs

All content is assembled into a single enriched text string that the
downstream chunker + embedder pipeline can process without modification.
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

# Minimum image size in bytes to bother describing (skips tiny icons/bullets)
_MIN_IMAGE_BYTES = 5_000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def parse(file: UploadFile) -> dict[str, Any]:
    """
    Parse an uploaded file and return enriched text with metadata.

    Returns:
        {
            "text":     full enriched text (str),
            "metadata": {"filename": str, "page_count": int, "file_type": str}
        }

    Raises ValueError for unsupported or unparseable files.
    """
    content_type: str = file.content_type or ""
    filename: str = file.filename or "unknown"
    raw_bytes: bytes = await file.read()

    logger.info(
        "Parsing '%s' (content_type=%s, size=%d bytes)",
        filename, content_type, len(raw_bytes),
    )

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
    return _parse_unstructured(raw_bytes, filename, content_type)


# ---------------------------------------------------------------------------
# PDF parser
# ---------------------------------------------------------------------------


async def _parse_pdf(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    try:
        import pypdf
        import pdfplumber
        import fitz  # pymupdf

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        page_count = len(reader.pages)

        # -- Tables (pdfplumber gives the best table detection for PDFs) ----
        tables_by_page: dict[int, list[str]] = {}
        try:
            with pdfplumber.open(io.BytesIO(raw_bytes)) as plumber_pdf:
                for page_num, page in enumerate(plumber_pdf.pages, start=1):
                    raw_tables = page.extract_tables() or []
                    md_tables = [_table_to_markdown(t) for t in raw_tables if t]
                    if md_tables:
                        tables_by_page[page_num] = md_tables
        except Exception as exc:
            logger.warning("PDF table extraction skipped: %s", exc)

        # -- Raw image bytes (pymupdf) ----------------------------------------
        # Collect as (page_num, img_bytes) so we can map descriptions back.
        raw_images: list[tuple[int, bytes]] = []
        try:
            fitz_doc = fitz.open(stream=raw_bytes, filetype="pdf")
            for page_num_0, page in enumerate(fitz_doc):
                for img_info in page.get_images(full=True):
                    xref = img_info[0]
                    try:
                        base_image = fitz_doc.extract_image(xref)
                        img_bytes = base_image["image"]
                        if len(img_bytes) >= _MIN_IMAGE_BYTES:
                            raw_images.append((page_num_0 + 1, img_bytes))
                    except Exception:
                        pass
            fitz_doc.close()
        except Exception as exc:
            logger.warning("PDF image extraction skipped: %s", exc)

        # -- Vision LLM descriptions (concurrent) ----------------------------
        image_descriptions: list[tuple[int, str]] = []
        if settings.extract_images and raw_images:
            descs = await asyncio.gather(
                *[_describe_image(img) for _, img in raw_images],
                return_exceptions=True,
            )
            for (page_num, _), result in zip(raw_images, descs):
                if isinstance(result, str) and result:
                    image_descriptions.append((page_num, result))

        # Group descriptions by page for assembly
        images_by_page: dict[int, list[str]] = {}
        for page_num, desc in image_descriptions:
            images_by_page.setdefault(page_num, []).append(desc)

        # -- Hyperlinks (pypdf annotations) ----------------------------------
        links: list[str] = []
        try:
            for page in reader.pages:
                annots = page.get("/Annots")
                if not annots:
                    continue
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
            logger.warning("PDF link extraction skipped: %s", exc)

        # -- Assemble enriched text ------------------------------------------
        sections: list[str] = []

        for page_num_0, page in enumerate(reader.pages):
            page_num = page_num_0 + 1
            page_text = (page.extract_text() or "").strip()

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
            "PDF '%s' parsed: pages=%d tables=%d image_descs=%d links=%d chars=%d",
            filename, page_count,
            sum(len(v) for v in tables_by_page.values()),
            len(image_descriptions),
            len(links),
            len(text),
        )
        return {
            "text": text,
            "metadata": {"filename": filename, "page_count": page_count, "file_type": "pdf"},
        }

    except Exception as exc:
        logger.exception("PDF parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Failed to parse PDF '{filename}': {exc}") from exc


# ---------------------------------------------------------------------------
# DOCX parser
# ---------------------------------------------------------------------------


async def _parse_docx(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    try:
        import docx
        from docx.oxml.ns import qn

        doc = docx.Document(io.BytesIO(raw_bytes))
        sections: list[str] = []

        # -- Body text -------------------------------------------------------
        body_paras = [p.text for p in doc.paragraphs if p.text.strip()]
        if body_paras:
            sections.append("\n\n".join(body_paras))

        # -- Tables ----------------------------------------------------------
        for i, table in enumerate(doc.tables, start=1):
            rows = [
                [cell.text.strip() for cell in row.cells]
                for row in table.rows
            ]
            md = _table_to_markdown(rows)
            if md:
                sections.append(f"[TABLE {i}]\n{md}")

        # -- Images (from package relationships) ----------------------------
        raw_images: list[bytes] = []
        if settings.extract_images:
            for rel in doc.part.rels.values():
                if "image" in rel.reltype.lower():
                    try:
                        img_bytes = rel.target_part.blob
                        if len(img_bytes) >= _MIN_IMAGE_BYTES:
                            raw_images.append(img_bytes)
                    except Exception:
                        pass

        if raw_images:
            descs = await asyncio.gather(
                *[_describe_image(img) for img in raw_images],
                return_exceptions=True,
            )
            for i, result in enumerate(descs, start=1):
                if isinstance(result, str) and result:
                    sections.append(f"[IMAGE {i}]\n{result}")

        # -- Hyperlinks (XML relationships) ----------------------------------
        links: list[tuple[str, str]] = []
        try:
            for para in doc.paragraphs:
                for hl in para._element.findall(f'.//{{{qn("w")}}}hyperlink'):
                    r_id = hl.get(f'{{{qn("r")}}}id')
                    if r_id and r_id in doc.part.rels:
                        url = doc.part.rels[r_id].target_ref
                        anchor = "".join(
                            t.text
                            for t in hl.findall(f'.//{{{qn("w")}}}t')
                            if t.text
                        ).strip()
                        links.append((anchor or url, url))
        except Exception as exc:
            logger.warning("DOCX link extraction skipped: %s", exc)

        if links:
            unique = list(dict.fromkeys(links))
            link_lines = "\n".join(
                f"• {anchor} → {url}" if anchor != url else f"• {url}"
                for anchor, url in unique
            )
            sections.append(f"[HYPERLINKS]\n{link_lines}")

        text = "\n\n".join(sections)
        logger.info(
            "DOCX '%s' parsed: tables=%d image_descs=%d links=%d chars=%d",
            filename, len(doc.tables), len(raw_images), len(links), len(text),
        )
        return {
            "text": text,
            "metadata": {"filename": filename, "page_count": 0, "file_type": "docx"},
        }

    except Exception as exc:
        logger.exception("DOCX parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Failed to parse DOCX '{filename}': {exc}") from exc


# ---------------------------------------------------------------------------
# TXT parser (unchanged — no tables/images/links to extract)
# ---------------------------------------------------------------------------


def _parse_txt(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            text = raw_bytes.decode(encoding)
            logger.debug("TXT '%s' decoded with %s: %d chars", filename, encoding, len(text))
            return {
                "text": text,
                "metadata": {"filename": filename, "page_count": 0, "file_type": "txt"},
            }
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
            "metadata": {
                "filename": filename,
                "page_count": 0,
                "file_type": content_type.split("/")[-1],
            },
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
    # Pad rows that are shorter than the widest row
    padded = [row + [""] * (col_count - len(row)) for row in cleaned]

    header = padded[0]
    body = padded[1:]

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
        # Detect MIME type from magic bytes
        if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif img_bytes[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif img_bytes[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/jpeg"  # safe default

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        client = get_openrouter_client()

        response = await client.chat.completions.create(
            model=settings.vision_model,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
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
