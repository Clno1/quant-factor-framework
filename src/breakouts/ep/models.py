"""Versioned evidence contracts. Missing market data is never zero volume."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


ALGORITHM_VERSION = "ep-observation-v1a.1"
NEW_YORK = ZoneInfo("America/New_York")


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def ticker(value: Any) -> str:
    symbol = str(value or "").strip().upper().replace(".", "-").replace("/", "-")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,19}", symbol):
        raise ValueError("INVALID_TICKER")
    return symbol


def canonical_url(value: Any) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("INVALID_SOURCE_URL")
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"),
                       urlencode(sorted(query)), ""))


@dataclass(frozen=True)
class CatalystSnapshot:
    document_id: str
    revision_id: str
    ticker: str
    kind: str
    event_date: str
    published_at: str | None
    observed_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_evidence(kind: str, row: dict[str, Any], observed_at: datetime) -> CatalystSnapshot:
    symbol = ticker(row.get("symbol"))
    observed = timestamp(observed_at)
    if kind == "CALENDAR":
        day = date.fromisoformat(str(row.get("date"))).isoformat()
        payload = {key: row.get(key) for key in
                   ("epsActual", "epsEstimated", "revenueActual", "revenueEstimated", "lastUpdated")}
        for key in ("epsActual", "epsEstimated", "revenueActual", "revenueEstimated"):
            value = payload[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("INVALID_FINANCIAL_NUMBER")
        payload.update(date=day, symbol=symbol, eps_basis="UNVERIFIED",
                       estimate_asof="UNAVAILABLE", publication_time="NOT_PROVIDED")
        identity = ["fmp", "CALENDAR", symbol, day]
        published = None
    elif kind == "ARTICLE":
        title = str(row.get("title") or "").strip()
        if not title or len(title) > 4000:
            raise ValueError("INVALID_HEADLINE")
        url = canonical_url(row.get("url"))
        raw_time = str(row.get("publishedDate") or "")
        if "T" not in raw_time and " " not in raw_time:
            raise ValueError("PUBLICATION_TIME_MISSING")
        instant = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        assumed = instant.tzinfo is None
        if assumed:
            instant = instant.replace(tzinfo=NEW_YORK)
        published = timestamp(instant)
        if published > observed:
            raise ValueError("FUTURE_PUBLICATION")
        day = instant.astimezone(NEW_YORK).date().isoformat()
        body = str(row.get("text") or "")
        payload = {"symbol": symbol, "title": title, "url": url,
                   "text": body[:16000], "text_sha256": digest(body),
                   "text_truncated": len(body) > 16000, "text_characters": len(body),
                   "published_at": published, "timezone_assumed": assumed,
                   "publisher": str(row.get("publisher") or row.get("site") or ""),
                   "evidence_scope": "PROVIDER_SNIPPET_NOT_VERIFIED_FULLTEXT"}
        identity = ["fmp", "ARTICLE", symbol, url]
    else:
        raise ValueError("UNKNOWN_EVIDENCE_KIND")
    return CatalystSnapshot(digest(identity), digest(payload), symbol, kind, day, published, observed, payload)


@dataclass(frozen=True)
class PremarketSnapshot:
    price: float | None = None
    volume: float | None = None
    dollar_volume: float | None = None
    same_clock_rvol: float | None = None
    status: str = "NOT_COLLECTED_UNVERIFIED_CONTRACT"


@dataclass(frozen=True)
class EpSettings:
    page_size: int = 100
    max_pages: int = 3
    max_profiles: int = 20
    max_requests: int = 40
    deadline_seconds: float = 120.0
    request_timeout_seconds: float = 10.0
    max_days: int = 7
    include_etfs: bool = False

    def __post_init__(self) -> None:
        if type(self.include_etfs) is not bool:
            raise ValueError("include_etfs must be a boolean")
        for key in ("page_size", "max_pages", "max_requests", "max_days"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if type(self.max_profiles) is not int or self.max_profiles < 0:
            raise ValueError("max_profiles must be a nonnegative integer")
        if self.page_size > 250 or self.max_pages > 50 or self.max_days > 31:
            raise ValueError("EP collection scope exceeds supported bounds")
        for key in ("deadline_seconds", "request_timeout_seconds"):
            if not math.isfinite(getattr(self, key)) or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive and finite")
