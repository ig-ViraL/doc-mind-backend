"""
Document parser — routes PDF / DOCX / TXT files to the correct library
and returns extracted text plus metadata.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from fastapi import UploadFile

logger = logging.getLogger(__name__)


async def parse(file: UploadFile) -> dict[str, Any]:
    """
    Parse an uploaded file and return extracted text with metadata.

    Returns a dict with keys:
        text        — full extracted text (str)
        metadata    — dict with filename, page_count (int), file_type (str)

    Raises ValueError for unsupported content types.
    """
    content_type: str = file.content_type or ""
    filename: str = file.filename or "unknown"

    raw_bytes: bytes = await file.read()

    logger.info("Parsing file '%s' (content_type=%s, size=%d bytes)", filename, content_type, len(raw_bytes))

    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return _parse_pdf(raw_bytes, filename)

    if (
        content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or filename.lower().endswith(".docx")
    ):
        return _parse_docx(raw_bytes, filename)

    if content_type in ("text/plain", "text/plain; charset=utf-8") or filename.lower().endswith(".txt"):
        return _parse_txt(raw_bytes, filename)

    # Fallback: try unstructured for anything else
    logger.warning("Unsupported content_type '%s' — attempting unstructured fallback", content_type)
    return _parse_unstructured(raw_bytes, filename, content_type)


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------


def _parse_pdf(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    """Extract text from a PDF using pypdf."""
    try:
        import pypdf  # lazy import to avoid startup cost

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        pages: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            pages.append(extracted)

        text = "\n\n".join(pages)
        page_count = len(reader.pages)
        logger.debug("PDF '%s' parsed: %d pages, %d chars", filename, page_count, len(text))
        return {
            "text": text,
            "metadata": {
                "filename": filename,
                "page_count": page_count,
                "file_type": "pdf",
            },
        }
    except Exception as exc:
        logger.exception("PDF parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Failed to parse PDF '{filename}': {exc}") from exc


def _parse_docx(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    """Extract text from a DOCX file using python-docx."""
    try:
        import docx  # python-docx

        doc = docx.Document(io.BytesIO(raw_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        text = "\n\n".join(paragraphs)
        logger.debug("DOCX '%s' parsed: %d paragraphs, %d chars", filename, len(paragraphs), len(text))
        return {
            "text": text,
            "metadata": {
                "filename": filename,
                "page_count": 0,  # DOCX page count is not reliably available
                "file_type": "docx",
            },
        }
    except Exception as exc:
        logger.exception("DOCX parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Failed to parse DOCX '{filename}': {exc}") from exc


def _parse_txt(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    """Decode a plain-text file."""
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            text = raw_bytes.decode(encoding)
            logger.debug("TXT '%s' decoded with %s: %d chars", filename, encoding, len(text))
            return {
                "text": text,
                "metadata": {
                    "filename": filename,
                    "page_count": 0,
                    "file_type": "txt",
                },
            }
        except UnicodeDecodeError:
            continue

    raise ValueError(f"Unable to decode text file '{filename}' with any supported encoding")


def _parse_unstructured(raw_bytes: bytes, filename: str, content_type: str) -> dict[str, Any]:
    """
    Generic fallback parser using the `unstructured` library.
    Handles file types not directly handled above.
    """
    try:
        from unstructured.partition.auto import partition

        elements = partition(file=io.BytesIO(raw_bytes), content_type=content_type)
        text = "\n\n".join(str(el) for el in elements if str(el).strip())
        logger.debug("Unstructured '%s' parsed: %d elements, %d chars", filename, len(elements), len(text))
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
