"""Exact SEC company-index endpoint, retaining identified access and robots controls."""
from urllib.parse import urlsplit

from .public_articles import PublicArticleClient
from .sec_attachments import SecDisclosureClient

INDEX_URL = "https://www.sec.gov/files/company_tickers.json"


class SecCompanyIndexClient(SecDisclosureClient):
    def validate_url(self, url):
        normalized = PublicArticleClient.validate_url(self, url)
        if normalized == INDEX_URL:
            return normalized
        return super().validate_url(normalized)
