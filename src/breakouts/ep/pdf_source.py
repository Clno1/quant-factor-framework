"""Offline, page-addressable PDF evidence; never treated as approved financial facts."""
from __future__ import annotations

from io import BytesIO

from .models import digest


PDF_PARSER_VERSION = "ep-pdf-evidence-v1"


def parse_pdf(raw: bytes) -> dict:
    """Use only in a bounded audit worker; PDF decoding can expand compressed streams."""
    if not raw.startswith(b"%PDF-") or len(raw) > 5_000_000:
        raise ValueError("Invalid PDF signature or oversized file")
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(raw), strict=True)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF is not supported")
    if not 1 <= len(reader.pages) <= 80:
        raise ValueError("PDF page limit exceeded")
    pages, paragraphs = [], []
    total = 0
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        total += len(text)
        if total > 250_000:
            raise ValueError("PDF extracted text limit exceeded")
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        pages.append({"page": number, "characters": len(text), "line_count": len(lines)})
        paragraphs.extend({"id": f"page{number:03d}-line{index:04d}", "page": number, "text": line}
                          for index, line in enumerate(lines, 1))
        if len(paragraphs) > 6000:
            raise ValueError("PDF line limit exceeded")
    result = {"parser_version": PDF_PARSER_VERSION, "status": "EXTRACTED" if total >= 300 else "BODY_NOT_ESTABLISHED",
              "page_count": len(pages), "characters": total, "pages": pages, "paragraphs": paragraphs,
              "empty_pages": [p["page"] for p in pages if not p["line_count"]],
              "document_type": "PDF_UNCLASSIFIED", "fulltext_completeness": "NOT_PROVEN",
              "table_layout_semantics": "NOT_RECONSTRUCTED", "financial_facts_verified": False,
              "historical_availability_verified": False, "eligible_for_rating": False}
    result["text_revision"] = digest(result)
    return result
