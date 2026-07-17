"""Discord incoming-webhook delivery for momentum scan digests."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


class DiscordDeliveryError(RuntimeError):
    """A sanitized Discord delivery failure suitable for an outbox state machine.

    ``uncertain`` means Discord may already have accepted the message, so an
    automatic retry could create a duplicate. ``retryable`` only states whether
    retrying can make progress; callers should still refuse automatic retries
    whenever ``uncertain`` is true.

    The exception message deliberately never contains a webhook URL, Discord's
    response body, or the original ``requests`` exception text. Those values can
    contain the webhook token.
    """

    def __init__(
        self,
        message: str,
        *,
        uncertain: bool = False,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        reason: str = "delivery_failed",
    ) -> None:
        super().__init__(message)
        self.uncertain = bool(uncertain)
        self.retryable = bool(retryable)
        self.status_code = status_code
        self.retry_after = retry_after
        self.reason = reason


_MAX_CONTENT_LENGTH = 2_000
_MAX_EMBEDS = 10
_MAX_EMBED_CHARACTERS = 6_000
_MAX_EMBED_TITLE_LENGTH = 256
_MAX_EMBED_DESCRIPTION_LENGTH = 4_096
_MAX_EMBED_FIELDS = 25
_MAX_EMBED_FIELD_NAME_LENGTH = 256
_MAX_EMBED_FIELD_VALUE_LENGTH = 1_024
_MAX_EMBED_FOOTER_LENGTH = 2_048
_MAX_EMBED_AUTHOR_LENGTH = 256
_MAX_ALLOWED_MENTION_IDS = 100


def is_discord_snowflake(value: Any) -> bool:
    """Return true only for the ASCII decimal form used by Discord IDs."""
    return isinstance(value, str) and value.isascii() and value.isdigit()


def discord_webhook_identity(webhook_url: str) -> tuple[str, str, int | None, str]:
    """Canonical credential identity, deliberately excluding non-routing query flags."""
    raw = str(webhook_url).strip()
    try:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return ("invalid", raw.casefold(), None, "")
    if hostname in {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }:
        hostname = "discord.com"
    effective_port = port if port is not None else (443 if parsed.scheme.casefold() == "https" else None)
    return (
        parsed.scheme.casefold(),
        hostname,
        effective_port,
        parsed.path.rstrip("/"),
    )


def _validation_error(detail: str) -> DiscordDeliveryError:
    return DiscordDeliveryError(
        f"Discord payload validation failed: {detail}",
        uncertain=False,
        retryable=False,
        reason="invalid_payload",
    )


def _require_string(value: Any, location: str, maximum: int) -> int:
    if not isinstance(value, str):
        raise _validation_error(f"{location} must be a string")
    if len(value) > maximum:
        raise _validation_error(f"{location} exceeds {maximum} characters")
    return len(value)


def _validate_allowed_mentions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _validation_error("allowed_mentions must be an object")
    unknown = set(value) - {"parse", "roles", "users", "replied_user"}
    if unknown:
        raise _validation_error("allowed_mentions contains unsupported keys")

    normalized = dict(value)
    parse = normalized.setdefault("parse", [])
    if not isinstance(parse, list) or parse:
        raise _validation_error("allowed_mentions.parse must be an empty list")

    for key in ("roles", "users"):
        ids = normalized.get(key, [])
        if not isinstance(ids, list):
            raise _validation_error(f"allowed_mentions.{key} must be a list")
        if len(ids) > _MAX_ALLOWED_MENTION_IDS:
            raise _validation_error(
                f"allowed_mentions.{key} exceeds {_MAX_ALLOWED_MENTION_IDS} IDs"
            )
        if any(not is_discord_snowflake(item) for item in ids):
            raise _validation_error(
                f"allowed_mentions.{key} must contain only numeric string IDs"
            )

    if "replied_user" in normalized and not isinstance(normalized["replied_user"], bool):
        raise _validation_error("allowed_mentions.replied_user must be a boolean")
    return normalized


def _validate_embed(embed: Any, index: int) -> int:
    if not isinstance(embed, dict):
        raise _validation_error(f"embeds[{index}] must be an object")

    total = 0
    if "title" in embed:
        total += _require_string(
            embed["title"], f"embeds[{index}].title", _MAX_EMBED_TITLE_LENGTH
        )
    if "description" in embed:
        total += _require_string(
            embed["description"],
            f"embeds[{index}].description",
            _MAX_EMBED_DESCRIPTION_LENGTH,
        )

    fields = embed.get("fields", [])
    if not isinstance(fields, list):
        raise _validation_error(f"embeds[{index}].fields must be a list")
    if len(fields) > _MAX_EMBED_FIELDS:
        raise _validation_error(
            f"embeds[{index}].fields exceeds {_MAX_EMBED_FIELDS} entries"
        )
    for field_index, embed_field in enumerate(fields):
        if not isinstance(embed_field, dict):
            raise _validation_error(
                f"embeds[{index}].fields[{field_index}] must be an object"
            )
        if "name" not in embed_field or "value" not in embed_field:
            raise _validation_error(
                f"embeds[{index}].fields[{field_index}] requires name and value"
            )
        total += _require_string(
            embed_field["name"],
            f"embeds[{index}].fields[{field_index}].name",
            _MAX_EMBED_FIELD_NAME_LENGTH,
        )
        total += _require_string(
            embed_field["value"],
            f"embeds[{index}].fields[{field_index}].value",
            _MAX_EMBED_FIELD_VALUE_LENGTH,
        )

    footer = embed.get("footer")
    if footer is not None:
        if not isinstance(footer, dict):
            raise _validation_error(f"embeds[{index}].footer must be an object")
        if "text" in footer:
            total += _require_string(
                footer["text"],
                f"embeds[{index}].footer.text",
                _MAX_EMBED_FOOTER_LENGTH,
            )

    author = embed.get("author")
    if author is not None:
        if not isinstance(author, dict):
            raise _validation_error(f"embeds[{index}].author must be an object")
        if "name" in author:
            total += _require_string(
                author["name"],
                f"embeds[{index}].author.name",
                _MAX_EMBED_AUTHOR_LENGTH,
            )
    return total


def validate_discord_payload(payload: Any) -> dict[str, Any]:
    """Validate Discord text/embed limits and return a safe payload copy.

    Missing ``allowed_mentions`` is normalized to ``{"parse": []}`` for
    compatibility with older callers. An explicitly unsafe mention policy is
    rejected instead of silently rewritten.
    """

    if not isinstance(payload, dict):
        raise _validation_error("payload must be an object")

    normalized = dict(payload)
    normalized["allowed_mentions"] = _validate_allowed_mentions(
        normalized.get("allowed_mentions", {"parse": []})
    )

    content = normalized.get("content")
    if content is not None:
        _require_string(content, "content", _MAX_CONTENT_LENGTH)

    embeds = normalized.get("embeds", [])
    if not isinstance(embeds, list):
        raise _validation_error("embeds must be a list")
    if len(embeds) > _MAX_EMBEDS:
        raise _validation_error(f"embeds exceeds {_MAX_EMBEDS} entries")
    embed_characters = sum(_validate_embed(embed, index) for index, embed in enumerate(embeds))
    if embed_characters > _MAX_EMBED_CHARACTERS:
        raise _validation_error(
            f"combined embed text exceeds {_MAX_EMBED_CHARACTERS} characters"
        )

    if not content and not embeds:
        raise _validation_error("payload requires non-empty content or at least one embed")
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError):
        raise _validation_error("payload must contain valid JSON values") from None
    return normalized


def _retry_after(response: requests.Response) -> float:
    raw_value: Any = response.headers.get("Retry-After")
    if raw_value in (None, ""):
        try:
            data = response.json()
        except (TypeError, ValueError, requests.RequestException):
            data = {}
        if isinstance(data, dict):
            raw_value = data.get("retry_after")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return 1.0
    return value if math.isfinite(value) and value >= 0 else 1.0


def _wait_url(webhook_url: str) -> str:
    parsed = urlparse(webhook_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key.lower() != "wait"]
    query.append(("wait", "true"))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(number) >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    return f"${number:,.0f}"


def _pct(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:+.1f}%" if signed else f"{number:.1f}%"


def _field(row: dict[str, Any], dashboard_base_url: str = "") -> dict[str, Any]:
    ticker = str(row.get("ticker") or "")
    signal = str(row.get("signal_type") or "CANDIDATE")
    score = int(row.get("score") or 0)
    name = str(row.get("name") or "")[:80]
    title = f"{ticker} · {signal} · {score}分"
    if row.get("is_upgrade"):
        title = "NEW · " + title
    pivot = row.get("pivot")
    pivot_text = "n/a" if pivot is None else f"${float(pivot):.2f}"
    lines = [
        name or "Unnamed security",
        f"现价 ${float(row.get('close') or 0):.2f} · Pivot {pivot_text} · 距离 {_pct(row.get('pivot_distance'), signed=True)}",
        f"20D {_pct(row.get('return_20d'), signed=True)} · ADR20 {_pct(row.get('adr_20d'))}",
        f"当日额 {_money(row.get('dollar_volume'))} · 20日均额 {_money(row.get('avg_dollar_volume_20d'))}",
    ]
    if row.get("intraday_trigger"):
        lines.append(str(row["intraday_trigger"]))
    if dashboard_base_url:
        lines.append(f"[打开诊断]({dashboard_base_url}/breakouts/{ticker}?universe=US_ACTIVE)")
    return {"name": title[:256], "value": "\n".join(lines)[:1024], "inline": False}


def build_discord_payload(
    snapshot: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    *,
    role_id: str = "",
    mention: bool = False,
    dashboard_base_url: str = "",
) -> dict[str, Any]:
    selected = list(rows)
    market = snapshot.get("market_hours") or {}
    is_open = bool(market.get("isMarketOpen"))
    status = "OPEN" if is_open else "CLOSED / SHADOW"
    pending = int(snapshot.get("pending_upgrade_count") or 0)
    asset_scope = "股票 + ETF" if snapshot.get("include_etfs") else "仅股票"
    description = (
        f"市场 **{status}** · 范围 **{asset_scope}** · 宽筛 **{snapshot.get('broad_count', 0)}** · "
        f"实时硬筛 **{snapshot.get('strict_count', 0)}** · 新增/升级 **{pending}**\n"
        f"报价时间 `{snapshot.get('quote_time') or 'n/a'}`"
    )
    fields = [_field(row, dashboard_base_url) for row in selected[:20]]
    fixed_embed_characters = (
        len("动量交易 · 小时扫描")
        + len(description)
        + len("研究提醒，不构成交易建议")
    )
    while (
        fixed_embed_characters
        + sum(len(field["name"]) + len(field["value"]) for field in fields)
        > 5_500
    ):
        candidates = [field for field in fields if len(field["value"]) > 64]
        if not candidates:
            break
        field = max(candidates, key=lambda item: len(item["value"]))
        total = fixed_embed_characters + sum(
            len(item["name"]) + len(item["value"]) for item in fields
        )
        target = max(64, len(field["value"]) - max(1, total - 5_500))
        field["value"] = field["value"][: max(1, target - 1)] + "…"
    if not fields:
        fields = [{"name": "本轮没有严格候选", "value": "未发现满足当前四项硬筛的标的。", "inline": False}]
    signal_types = {str(row.get("signal_type") or "") for row in selected}
    if "OPENING_RANGE_BREAK" in signal_types or "BREAKOUT" in signal_types:
        color = 0x00C853
    elif "READY" in signal_types:
        color = 0xFFB300
    else:
        color = 0x42A5F5 if is_open else 0x6B7280
    payload: dict[str, Any] = {
        "username": "Momentum Alerts",
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "动量交易 · 小时扫描",
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": "研究提醒，不构成交易建议"},
            "timestamp": snapshot.get("generated_at"),
        }],
    }
    if mention and is_discord_snowflake(role_id):
        payload["content"] = f"<@&{role_id}> 发现新的高优先级动量信号"
        payload["allowed_mentions"] = {"parse": [], "roles": [role_id]}
    return payload


@dataclass
class DiscordNotifier:
    webhook_url: str = field(repr=False)
    timeout: float = 15.0
    max_rate_limit_retries: int = 2
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    request_guard: Callable[[float], bool] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.webhook_url)
        allowed_hosts = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts \
                or "/api/webhooks/" not in parsed.path:
            raise ValueError("DISCORD_WEBHOOK_URL 不是有效的 Discord Incoming Webhook")
        if self.timeout <= 0:
            raise ValueError("Discord webhook timeout must be positive")
        if self.max_rate_limit_retries < 0:
            raise ValueError("Discord rate-limit retry count cannot be negative")

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_discord_payload(payload)
        delivery_url = _wait_url(self.webhook_url)
        rate_limit_attempts = 0

        while True:
            if self.request_guard is not None and not self.request_guard(0.0):
                raise DiscordDeliveryError(
                    "Discord delivery deadline closed before the next request",
                    uncertain=False,
                    retryable=False,
                    reason="delivery_window_closed",
                )
            try:
                response = requests.post(
                    delivery_url,
                    json=normalized,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.ConnectTimeout:
                # Requests documents ConnectTimeout as safe to retry. The
                # transport did not establish a completed request/response.
                raise DiscordDeliveryError(
                    "Discord webhook connection timed out",
                    uncertain=False,
                    retryable=True,
                    reason="connect_timeout",
                ) from None
            except (requests.ReadTimeout, requests.Timeout):
                raise DiscordDeliveryError(
                    "Discord webhook response timed out",
                    uncertain=True,
                    retryable=False,
                    reason="response_timeout",
                ) from None
            except requests.RequestException:
                raise DiscordDeliveryError(
                    "Discord webhook transport failed without a confirmed response",
                    uncertain=True,
                    retryable=False,
                    reason="network_error",
                ) from None

            status_code = int(response.status_code)
            if status_code == 429:
                retry_after = _retry_after(response)
                if rate_limit_attempts < self.max_rate_limit_retries:
                    if self.request_guard is not None and not self.request_guard(retry_after):
                        raise DiscordDeliveryError(
                            "Discord rate-limit delay would cross the delivery deadline",
                            uncertain=False,
                            retryable=False,
                            status_code=status_code,
                            retry_after=retry_after,
                            reason="delivery_window_closed",
                        )
                    rate_limit_attempts += 1
                    self.sleep(retry_after)
                    continue
                raise DiscordDeliveryError(
                    "Discord webhook rate limit remained active after bounded retries",
                    uncertain=False,
                    retryable=True,
                    status_code=status_code,
                    retry_after=retry_after,
                    reason="rate_limited",
                )

            if not 200 <= status_code < 300:
                if status_code >= 500:
                    # A distributed upstream can create the message and still
                    # return a gateway/server failure. Preserve the project's
                    # at-most-once bias instead of risking an automatic duplicate.
                    raise DiscordDeliveryError(
                        f"Discord webhook returned HTTP {status_code} without a safe retry proof",
                        uncertain=True,
                        retryable=False,
                        status_code=status_code,
                        reason="server_error_unknown",
                    )
                retryable = status_code in {408, 425}
                raise DiscordDeliveryError(
                    f"Discord webhook rejected the request with HTTP {status_code}",
                    uncertain=False,
                    retryable=retryable,
                    status_code=status_code,
                    reason="http_error",
                )

            try:
                data = response.json()
            except (TypeError, ValueError, requests.RequestException):
                data = None
            message_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(message_id, (str, int)) or not str(message_id).strip():
                # A 2xx without the message object required by wait=true may
                # still have created a message. Do not retry automatically.
                raise DiscordDeliveryError(
                    "Discord webhook returned success without a message ID",
                    uncertain=True,
                    retryable=False,
                    status_code=status_code,
                    reason="missing_message_id",
                )
            return {"status": status_code, "message_id": str(message_id)}
