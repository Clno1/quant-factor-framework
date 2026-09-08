"""Extract article text and check document alignment, not the truth of financial claims."""
from __future__ import annotations

import json
import re
from typing import Any

from .models import digest


PARSER_VERSION = "ep-article-body-v2"


def _clean(value: str) -> str:
    return " ".join(value.split())


def parse_article(raw: bytes) -> dict[str, Any]:
    from lxml import html
    root = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    objects = []
    for node in root.xpath('//script[@type="application/ld+json"]'):
        try:
            value = json.loads(node.text or "")
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    objects.extend(item.get("@graph", []) if isinstance(item.get("@graph"), list) else [item])
        except (ValueError, TypeError):
            continue
    articles = [item for item in objects if isinstance(item, dict) and
                any(kind in {"Article", "NewsArticle", "PressRelease"} for kind in
                    (item.get("@type") if isinstance(item.get("@type"), list) else [item.get("@type")]))]
    # Some issuer IR sites wrap the entire release in an ASP.NET form.
    # Remove controls, but retain its article container for explicit extraction.
    for node in root.xpath("//script|//style|//nav|//footer|//noscript|//input|//button|//select|//textarea"):
        node.drop_tree()
    for node in root.xpath('//*[@hidden or @aria-hidden="true"]'):
        node.drop_tree()
    selectors = [
        '//*[contains(concat(" ", normalize-space(@class), " "), " release-body ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " evergreen-news-body ")]',
        '//*[@itemprop="articleBody"]',
        '//article',
    ]
    selected = []
    method = None
    for selector in selectors:
        matches = root.xpath(selector)
        if matches:
            selected, method = [max(matches, key=lambda node: len(node.text_content()))], selector
            break
    paragraphs = []
    if selected:
        elements = selected[0].xpath('.//p|.//h1|.//h2|.//h3|.//h4|.//li|.//tr')
        for node in elements:
            if any(parent.tag in {"p", "li", "tr"} for parent in node.iterancestors()
                   if parent is not selected[0]):
                continue
            text = _clean(" ".join(node.itertext()))
            if text:
                paragraphs.append(text)
    if not paragraphs:
        bodies = [item for item in articles if isinstance(item.get("articleBody"), str)]
        if len(bodies) == 1:
            paragraphs = [_clean(part) for part in re.split(r"\n+", bodies[0]["articleBody"]) if _clean(part)]
            method = "JSONLD_ARTICLE_BODY"
    titles = []
    if selected and "evergreen-news-body" in (selected[0].get("class") or "").split():
        titles = selected[0].xpath('../*[contains(concat(" ", normalize-space(@class), " "), " evergreen-news-headline ")]')
        if len(titles) != 1:
            titles = []
    if not titles:
        titles = root.xpath("//h1")
    title = _clean(titles[0].text_content()) if titles else ""
    if not title and len(articles) == 1:
        title = _clean(str(articles[0].get("headline") or ""))
    if not title:
        title = _clean(" ".join(root.xpath("//title/text()")))
    authors = []
    for item in articles:
        if _clean(str(item.get("headline") or "")).casefold() != title.casefold():
            continue
        author = item.get("author", [])
        for value in author if isinstance(author, list) else [author]:
            if isinstance(value, dict) and value.get("@type") == "Organization" and isinstance(value.get("name"), str):
                authors.append(value["name"])
    for index, text in enumerate(paragraphs):
        match = re.fullmatch(r"SOURCE:?\s+(.{1,200})", text, re.IGNORECASE)
        if match:
            authors.append(match[1])
        elif text == "SOURCE" and index + 1 < len(paragraphs):
            authors.append(paragraphs[index + 1][:200])
    characters = sum(len(text) for text in paragraphs)
    status = "EXTRACTED" if characters >= 300 and len(paragraphs) >= 2 else "BODY_NOT_ESTABLISHED"
    if characters > 250_000 or len(paragraphs) > 4000:
        status, paragraphs = "EXTRACTED_BODY_TOO_LARGE", []
    result = {"parser_version": PARSER_VERSION, "status": status, "title": title,
              "method": method, "characters": characters, "issuer_attributions": sorted(set(authors)),
              "paragraphs": [{"id": f"p{index:04d}", "text": text} for index, text in enumerate(paragraphs, 1)],
              "table_layout_semantics": "NOT_RECONSTRUCTED", "fulltext_completeness": "NOT_PROVEN"}
    result["text_revision"] = digest(result)
    return result


def _name(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return " ".join(word for word in words if word not in {"inc", "incorporated", "corp", "corporation", "ltd", "limited", "plc"})


def verify_document(event: dict[str, Any], parsed: dict[str, Any], identity: dict | None) -> dict:
    expected = set(re.findall(r"[a-z0-9]+", event["evidence"]["title"].casefold()))
    actual = set(re.findall(r"[a-z0-9]+", parsed["title"].casefold()))
    title_match = bool(expected) and len(expected & actual) / len(expected) >= .85
    body_ready = parsed["status"] == "EXTRACTED"
    attribution = "IDENTITY_UNAVAILABLE" if not identity else "ISSUER_ATTRIBUTION_UNVERIFIED"
    if identity and identity.get("name") and _name(identity["name"]) in {
        _name(author) for author in parsed["issuer_attributions"]
    }:
        attribution = "ISSUER_ATTRIBUTION_MATCH"
    return {"status": "DOCUMENT_MATCHED" if title_match and body_ready else "DOCUMENT_UNVERIFIED",
            "headline_match": title_match, "body_established": body_ready,
            "issuer_status": attribution, "financial_facts_verified": False,
            "historical_availability_verified": False,
            "limitations": ["TITLE_SIMILARITY_IS_NOT_PROOF", "NO_GAAP_OR_ESTIMATE_BASIS_VERIFICATION",
                            "NO_EVENT_MATERIALITY_OR_MARKET_CONFIRMATION"]}
