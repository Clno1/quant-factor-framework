"""Credential-free, bounded public HTML retrieval with explicit host policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import math
import re
import socket
import ssl
import time
from typing import Callable
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser


DEFAULT_ARTICLE_HOSTS = frozenset({"www.prnewswire.com", "www.globenewswire.com", "www.businesswire.com"})


class SourceAccessError(Exception):
    """Public error codes only; never persist remote error pages or credentials."""


@dataclass(frozen=True)
class HttpPage:
    status: int
    headers: dict[str, str]
    body: bytes


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # DNS is validated once, then the connection uses that exact IP with the original TLS name.
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _request_html(url: str, address: str, timeout: float, max_bytes: int, agent: str) -> HttpPage:
    parts = urlsplit(url)
    connection = _PinnedHTTPSConnection(parts.hostname, address, timeout)
    try:
        connection.request("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")),
                           headers={"User-Agent": agent, "Accept": "text/html,text/plain;q=0.8",
                                    "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status in {401, 403, 429}:
            return HttpPage(response.status, headers, b"")
        if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
            raise SourceAccessError("UNSUPPORTED_CONTENT_ENCODING")
        if int(headers.get("content-length", "0")) > max_bytes:
            raise SourceAccessError("BODY_TOO_LARGE")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise SourceAccessError("BODY_TOO_LARGE")
        return HttpPage(response.status, headers, body)
    finally:
        connection.close()


class PublicArticleClient:
    def __init__(self, *, allowed_hosts=DEFAULT_ARTICLE_HOSTS, max_requests: int = 16,
                 deadline_seconds: float = 90, timeout_seconds: float = 10,
                 max_bytes: int = 2_000_000, user_agent: str = "QuantEPResearch/0.2",
                 transport: Callable = _request_html, resolver: Callable | None = None,
                 monotonic: Callable = time.monotonic, pause: Callable = time.sleep):
        self.allowed_hosts = frozenset(str(host).lower() for host in allowed_hosts)
        if not self.allowed_hosts or any(not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                                         for host in self.allowed_hosts):
            raise ValueError("Only exact DNS hosts are allowed; no schemes or wildcards")
        if type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise ValueError("max_requests must be between 1 and 100")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 5_000_000:
            raise ValueError("max_bytes must be between 1 and 5000000")
        if any(not math.isfinite(value) or value <= 0 for value in (deadline_seconds, timeout_seconds)):
            raise ValueError("Timeouts must be positive and finite")
        if not user_agent or len(user_agent) > 200 or any(ord(c) < 32 or ord(c) > 126 for c in user_agent):
            raise ValueError("Invalid user agent")
        self.max_requests, self.max_bytes = max_requests, max_bytes
        self.timeout, self.agent = timeout_seconds, user_agent
        self.clock, self.pause = monotonic, pause
        self.ends_at = self.clock() + deadline_seconds
        self.transport, self.resolver = transport, resolver or socket.getaddrinfo
        self.requests = 0
        self.robots: dict[str, RobotFileParser] = {}
        self.blocked: dict[str, str] = {}
        self.last_request: dict[str, float] = {}
        self.trace: list[dict] = []

    def validate_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (ValueError, TypeError):
            raise SourceAccessError("URL_POLICY_REJECTED") from None
        if (parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts or
                parsed.username or parsed.password or port not in {None, 443} or
                any(ord(c) < 32 or ord(c) > 126 for c in url)):
            raise SourceAccessError("URL_POLICY_REJECTED")
        if any(key.lower() in {"apikey", "api_key", "token", "access_token", "authorization"}
               for key, _ in parse_qsl(parsed.query)):
            raise SourceAccessError("CREDENTIAL_QUERY_REJECTED")
        return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))

    def _get(self, url: str, *, robots: bool = False) -> HttpPage:
        url = self.validate_url(url)
        host = urlsplit(url).hostname
        if host in self.blocked:
            raise SourceAccessError(self.blocked[host])
        remaining = self.ends_at - self.clock()
        if remaining <= 0:
            raise SourceAccessError("SOURCE_TIME_BUDGET_EXCEEDED")
        if self.requests >= self.max_requests:
            raise SourceAccessError("SOURCE_REQUEST_BUDGET_EXCEEDED")
        addresses = self.resolver(host, 443, type=socket.SOCK_STREAM)
        ips = sorted({item[4][0] for item in addresses})
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise SourceAccessError("NON_PUBLIC_ADDRESS_REJECTED")
        delay = max(1, (self.robots[host].crawl_delay(self.agent) or 0) if host in self.robots else 0)
        rate = self.robots[host].request_rate(self.agent) if host in self.robots else None
        if rate and rate.requests > 0:
            delay = max(delay, rate.seconds / rate.requests)
        wait = max(0, self.last_request.get(host, -float("inf")) + delay - self.clock())
        if wait >= self.ends_at - self.clock():
            raise SourceAccessError("SOURCE_TIME_BUDGET_EXCEEDED")
        if wait:
            self.pause(wait)
        remaining = self.ends_at - self.clock()
        if remaining <= 0:
            raise SourceAccessError("SOURCE_TIME_BUDGET_EXCEEDED")
        self.requests += 1
        self.last_request[host] = self.clock()
        response = self.transport(url, ips[0], min(remaining, self.timeout),
                                  min(self.max_bytes, 128_000) if robots else self.max_bytes, self.agent)
        self.trace.append({"url": url, "http_status": response.status, "robots": robots})
        if response.status in {401, 403, 429}:
            self.blocked[host] = f"SOURCE_HTTP_{response.status}"
            raise SourceAccessError(self.blocked[host])
        if len(response.body) > (min(self.max_bytes, 128_000) if robots else self.max_bytes):
            raise SourceAccessError("BODY_TOO_LARGE")
        return response

    def _check_robots(self, url: str) -> None:
        host = urlsplit(url).hostname
        if host not in self.robots:
            response = self._get(f"https://{host}/robots.txt", robots=True)
            parser = RobotFileParser()
            if response.status == 404:
                parser.parse([])
            elif response.status == 200:
                # A challenge HTML page is not an empty robots policy.
                body = response.body.decode("utf-8", errors="replace")
                if "<html" in body.casefold() or "<!doctype" in body.casefold():
                    raise SourceAccessError("ROBOTS_RESPONSE_UNVERIFIED")
                parser.parse(body.splitlines())
            else:
                raise SourceAccessError("ROBOTS_UNAVAILABLE")
            self.robots[host] = parser
        if not self.robots[host].can_fetch(self.agent, url):
            raise SourceAccessError("ROBOTS_DISALLOWED")

    def fetch(self, url: str) -> dict:
        return self._fetch(url, kind="html")

    def fetch_pdf(self, url: str) -> dict:
        """Explicit PDF retrieval; HTML callers retain their original type gate."""
        return self._fetch(url, kind="pdf")

    def fetch_json(self, url: str) -> dict:
        """Return bounded JSON bytes; callers validate its schema before using it."""
        return self._fetch(url, kind="json")

    def _fetch(self, url: str, *, kind: str) -> dict:
        trace_start = len(self.trace)
        current = None
        try:
            current = self.validate_url(url)
            visited = set()
            for _ in range(4):
                if current in visited:
                    raise SourceAccessError("REDIRECT_LOOP")
                visited.add(current)
                try:
                    self._check_robots(current)
                except (socket.timeout, TimeoutError):
                    self.blocked[urlsplit(current).hostname] = "SOURCE_TIMEOUT"
                    raise
                except SourceAccessError as exc:
                    if str(exc) not in {"ROBOTS_DISALLOWED", "SOURCE_REQUEST_BUDGET_EXCEEDED", "SOURCE_TIME_BUDGET_EXCEEDED"}:
                        self.blocked[urlsplit(current).hostname] = str(exc)
                    raise
                response = self._get(current)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceAccessError("REDIRECT_WITHOUT_LOCATION")
                    current = self.validate_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise SourceAccessError(f"SOURCE_HTTP_{response.status}")
                media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                media_types = {"html": {"text/html", "application/xhtml+xml"}, "pdf": {"application/pdf"},
                               "json": {"application/json"}}
                if media_type not in media_types[kind]:
                    raise SourceAccessError(f"NON_{kind.upper()}_RESPONSE")
                if kind == "pdf" and not response.body.startswith(b"%PDF-"):
                    raise SourceAccessError("INVALID_PDF_SIGNATURE")
                if len(response.body) > self.max_bytes:
                    raise SourceAccessError("BODY_TOO_LARGE")
                return {"status": "FETCHED", "final_url": current, kind: response.body,
                        "raw_sha256": hashlib.sha256(response.body).hexdigest(),
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "trace": self.trace[trace_start:]}
            raise SourceAccessError("REDIRECT_LIMIT")
        except SourceAccessError as exc:
            status = str(exc)
        except (socket.timeout, TimeoutError):
            status = "SOURCE_TIMEOUT"
        except Exception:
            status = "SOURCE_TRANSPORT_ERROR"
        return {"status": status, "final_url": current, "received_at": datetime.now(timezone.utc).isoformat(),
                "trace": self.trace[trace_start:]}
