from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.breakouts.ep.pdf_source import parse_pdf
from src.breakouts.ep.sec_source import parse_sec_attachment
from src.data.public_articles import HttpPage, SourceAccessError
from src.data.sec_attachments import SecAttachmentClient
from test_ep_sources import client


PDF = b"%PDF-1.7\nfixture"
PDF_URL = "https://example.test/letter"
SEC_URL = "https://www.sec.gov/Archives/edgar/data/1820953/000162828026059271/affirmfq426shareholderle.htm"


def test_pdf_is_explicit_and_html_default_stays_strict():
    page = HttpPage(200, {"content-type": "application/pdf"}, PDF)
    assert client({"/letter": page}).fetch(PDF_URL)["status"] == "NON_HTML_RESPONSE"
    result = client({"/letter": page}).fetch_pdf(PDF_URL)
    assert result["status"] == "FETCHED"
    assert result["pdf"] == PDF
    assert "html" not in result
    assert len(result["trace"]) == 2


@pytest.mark.parametrize("media,body,status", [
    ("text/html", PDF, "NON_PDF_RESPONSE"),
    ("application/pdf", b"<html>challenge</html>", "INVALID_PDF_SIGNATURE"),
    ("application/octet-stream", PDF, "NON_PDF_RESPONSE"),
])
def test_pdf_requires_mime_and_signature(media, body, status):
    assert client({"/letter": HttpPage(200, {"content-type": media}, body)}).fetch_pdf(PDF_URL)["status"] == status


def test_pdf_retains_robots_refusal_budgets_and_redirect_policy():
    http = client({"/robots.txt": HttpPage(200, {}, b"User-agent: *\nDisallow: /letter")})
    assert http.fetch_pdf(PDF_URL)["status"] == "ROBOTS_DISALLOWED"
    assert http.requests == 1
    http = client({"/letter": HttpPage(429, {}, b"private")})
    assert http.fetch_pdf(PDF_URL)["status"] == "SOURCE_HTTP_429"
    count = http.requests
    assert http.fetch_pdf(PDF_URL)["status"] == "SOURCE_HTTP_429"
    assert http.requests == count
    assert client(max_requests=1).fetch_pdf(PDF_URL)["status"] == "SOURCE_REQUEST_BUDGET_EXCEEDED"
    assert client({"/letter": HttpPage(302, {"location": "https://evil.test/a"}, b"")}).fetch_pdf(PDF_URL)["status"] == "URL_POLICY_REJECTED"
    assert client({"/letter": HttpPage(200, {"content-type": "application/pdf"}, PDF * 10)}, max_bytes=30).fetch_pdf(PDF_URL)["status"] == "BODY_TOO_LARGE"


@pytest.mark.parametrize("email", [None, "", "invalid", "someone@example.com", "x@y.com\r\nX-Key: bad"])
def test_sec_requires_contact_without_echoing_it(email):
    with pytest.raises(ValueError) as exc:
        SecAttachmentClient(contact_email=email)
    if email:
        assert email not in str(exc.value)


def test_sec_restricts_hosts_paths_and_does_not_store_contact():
    # Synthetic test-only contact is never sent over the network.
    sec = SecAttachmentClient(contact_email="audit@quant.test")
    assert sec.validate_url(SEC_URL) == SEC_URL
    for url in ("https://evil.test/", "https://www.sec.gov/search", SEC_URL + "?key=abc",
                SEC_URL.replace("affirmfq426shareholderle.htm", "../secret.htm")):
        with pytest.raises(SourceAccessError):
            sec.validate_url(url)
    calls = []
    def transport(url, *args):
        calls.append(args[-1])
        return HttpPage(404 if url.endswith("robots.txt") else 200, {"content-type": "text/html"}, b"<html>filing</html>")
    fake = client()
    sec = SecAttachmentClient(contact_email="audit@quant.test", transport=transport, resolver=fake.resolver,
                              pause=lambda _: None)
    result = sec.fetch(SEC_URL)
    assert result["status"] == "FETCHED"
    assert all("audit@quant.test" in agent for agent in calls)
    assert "audit@quant.test" not in str(result)
    with pytest.raises(ValueError):
        SecAttachmentClient(contact_email="audit@quant.test", allowed_hosts={"evil.test"})


def test_pdf_page_locations_and_fail_closed_status():
    pytest.importorskip("pypdf")
    reader = SimpleNamespace(is_encrypted=False, pages=[
        SimpleNamespace(extract_text=lambda: "Revenue discussion with unverified values.\n" * 20),
        SimpleNamespace(extract_text=lambda: "")])
    with patch("pypdf.PdfReader", return_value=reader):
        result = parse_pdf(PDF)
    assert result["page_count"] == 2
    assert result["empty_pages"] == [2]
    assert result["paragraphs"][0]["id"] == "page001-line0001"
    assert result["paragraphs"][0]["page"] == 1
    assert result["eligible_for_rating"] is False
    assert result["financial_facts_verified"] is False
    assert result["fulltext_completeness"] == "NOT_PROVEN"


@pytest.mark.parametrize("kind", ["encrypted", "pages", "characters", "lines"])
def test_pdf_limits(kind):
    pytest.importorskip("pypdf")
    reader = SimpleNamespace(is_encrypted=kind == "encrypted", pages=[SimpleNamespace(extract_text=lambda: "")])
    if kind == "pages":
        reader.pages *= 81
    elif kind == "characters":
        reader.pages = [SimpleNamespace(extract_text=lambda: "x" * 250001)]
    elif kind == "lines":
        reader.pages = [SimpleNamespace(extract_text=lambda: "x\n" * 6001)]
    with patch("pypdf.PdfReader", return_value=reader), pytest.raises(ValueError):
        parse_pdf(PDF)


def test_actual_blank_pdf_requires_text_and_invalid_bytes_are_rejected():
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    raw = BytesIO()
    writer.write(raw)
    assert parse_pdf(raw.getvalue())["status"] == "BODY_NOT_ESTABLISHED"
    for raw in (b"not a pdf", b"%PDF-" + b"x" * 5_000_000):
        with pytest.raises(ValueError):
            parse_pdf(raw)


def exhibit(body):
    return ('<DOCUMENT><TYPE>EX-99.1\n<SEQUENCE>2\n<FILENAME>affirmfq426shareholderle.htm\n'
            '<DESCRIPTION>EX-99.1\n<TEXT><html><body>' + body + '</body></html></TEXT></DOCUMENT>').encode()


def test_sec_exhibit_text_is_separate_from_image_shell():
    body = '<div>Quarterly Results</div><div>' + 'Revenue discussion. ' * 30 + '</div>'
    body += '<table><tr><td><div>Revenue</div></td><td>100</td></tr></table>'
    parsed = parse_sec_attachment(exhibit(body), SEC_URL)
    assert parsed["status"] == "EXTRACTED"
    assert parsed["title"] == "Quarterly Results"
    assert parsed["paragraphs"][-1]["text"] == "Revenue 100"
    assert parsed["financial_facts_verified"] is False
    assert parse_sec_attachment(exhibit('<p><img src="slide1.jpg"></p>'), SEC_URL)["status"] == "IMAGE_ONLY_OCR_REQUIRED"


@pytest.mark.parametrize("raw,url", [
    (exhibit("<p>text</p>").replace(b"EX-99.1", b"8-K"), SEC_URL),
    (exhibit("<p>text</p>").replace(b"affirmfq426shareholderle.htm", b"other.htm"), SEC_URL),
    (b"<html><article>Other site</article></html>", SEC_URL),
    (exhibit("<p>text</p>"), SEC_URL.replace("www.sec.gov", "evil.test")),
])
def test_sec_exhibit_requires_supported_envelope_and_origin(raw, url):
    with pytest.raises(ValueError):
        parse_sec_attachment(raw, url)
