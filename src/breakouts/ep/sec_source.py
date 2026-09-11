"""Text extraction for explicitly selected SEC EX-99 SGML-wrapped attachments."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from .models import digest

SEC_PARSER_VERSION = "ep-sec-exhibit-v3"


def clean_text(value):
    return ' '.join(''.join(c for c in value if unicodedata.category(c) != 'Cf').split())


def parse_sec_attachment(raw: bytes, url: str) -> dict:
    from lxml import etree, html
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "www.sec.gov" or parts.query or parts.fragment or
            not re.fullmatch(r"/Archives/edgar/data/[0-9]+/[0-9]{18}/[A-Za-z0-9_-]+\.(htm|html)", parts.path)):
        raise ValueError("Explicit SEC archive attachment URL required")
    if len(raw) > 5_000_000:
        raise ValueError("SEC attachment too large")
    try:
        root = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    except etree.LxmlError:
        raise ValueError("SEC attachment cannot be parsed") from None
    types, names, bodies = (root.xpath(query) for query in ("//type", "//filename", "//text"))
    if not (len(types) == len(names) == len(bodies) == 1 and
            re.fullmatch(r"EX-99(?:\.[0-9]+)?", (types[0].text or "").strip()) and
            (names[0].text or "").strip() == parts.path.rsplit("/", 1)[1]):
        raise ValueError("Unsupported SEC envelope or filename mismatch")
    body = bodies[0]
    for node in body.xpath('.//script|.//style|.//nav|.//footer|.//input|.//button|.//textarea|.//*[@hidden or @aria-hidden="true"]'):
        node.drop_tree()
    images = len(body.xpath(".//img"))
    nodes = body.xpath('.//p|.//h1|.//h2|.//h3|.//tr[not(.//tr)]|.//div[not(.//p or .//tr or .//div or .//h1 or .//h2 or .//h3)]')
    selected = set(nodes)
    paragraphs = []
    for node in nodes:
        if any(parent in selected for parent in node.iterancestors()):
            continue
        value = clean_text(' '.join(node.itertext()))
        if value:
            paragraphs.append({"id": f"p{len(paragraphs) + 1:04d}", "text": value})
    total = sum(len(p["text"]) for p in paragraphs)
    if total > 250_000 or len(paragraphs) > 4000:
        raise ValueError("SEC extracted body too large")
    status = "EXTRACTED" if total >= 300 and len(paragraphs) >= 2 else (
        "IMAGE_ONLY_OCR_REQUIRED" if images else "BODY_NOT_ESTABLISHED")
    headings = [p['text'] for p in paragraphs[:12] if 5 <= len(p['text']) <= 250
                and re.search(r'[A-Za-z]', p['text']) and not re.fullmatch(
                    r'(?:Exhibit\s+|EX-)99(?:\.[0-9]+)?', p['text'], re.I)]
    heading = next((h for h in headings if re.search(
        r'\b(?:announces?|reports?|results|to acquire|acquisition|shareholder letter)\b', h, re.I)),
        headings[0] if headings else '')
    result = {"parser_version": SEC_PARSER_VERSION, "status": status,
              "title": heading if len(heading) <= 250 else "",
              "paragraphs": paragraphs, "characters": total, "image_count": images,
              "issuer_attributions": [], "document_type": (types[0].text or "").strip(),
              "envelope_filename_match": True, "fulltext_completeness": "NOT_PROVEN",
              "table_layout_semantics": "NOT_RECONSTRUCTED", "financial_facts_verified": False,
              "historical_availability_verified": False, "eligible_for_rating": False}
    result["text_revision"] = digest(result)
    return result
