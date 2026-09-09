"""Explicit SEC archive attachments, with identified access and no auto-discovery."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .public_articles import PublicArticleClient, SourceAccessError


class SecAttachmentClient(PublicArticleClient):
    SEC_HOSTS = frozenset({"www.sec.gov"})

    def __init__(self, *, contact_email: str, **options):
        if not isinstance(contact_email, str) or not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", contact_email):
            raise ValueError("SEC requires a configured real contact email")
        if contact_email.rsplit("@", 1)[1].lower() in {"example.com", "example.org", "example.net"}:
            raise ValueError("SEC contact cannot be a documentation placeholder")
        if {"allowed_hosts", "user_agent"} & options.keys():
            raise ValueError("SEC host and agent cannot be overridden")
        super().__init__(allowed_hosts=self.SEC_HOSTS, user_agent=f"QuantEPResearch/0.3 {contact_email}", **options)

    def validate_url(self, url: str) -> str:
        url = super().validate_url(url)
        parts = urlsplit(url)
        if parts.query or (parts.path != "/robots.txt" and not re.fullmatch(
                r"/Archives/edgar/data/[0-9]+/[0-9]{18}/[A-Za-z0-9_-]+\.(?:htm|html|pdf)", parts.path)):
            raise SourceAccessError("SEC_ATTACHMENT_PATH_REJECTED")
        return url


class SecDisclosureClient(SecAttachmentClient):
    """Company submissions and filing attachments only, never arbitrary SEC URLs."""
    SEC_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})

    def validate_url(self, url: str) -> str:
        url = PublicArticleClient.validate_url(self, url)
        parts = urlsplit(url)
        if parts.hostname == "data.sec.gov":
            if parts.query or not (parts.path == "/robots.txt" or re.fullmatch(r"/submissions/CIK[0-9]{10}\.json", parts.path)):
                raise SourceAccessError("SEC_SUBMISSIONS_PATH_REJECTED")
            return url
        return super().validate_url(url)
