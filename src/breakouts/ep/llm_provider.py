"""Single-shot provider adapters; credentials never enter saved request/result objects."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Protocol

from src.data.llm_transport import ResponsesHttpClient, KimiHttpClient, LlmTransportError as LlmError
from .llm_contract import Response, strict_json, response_schema
from .llm_span_selection import VERSION as SPAN_VERSION, selection_schema


class ModelTransport(Protocol):
    def generate(self, payload: dict) -> dict: ...


@dataclass(frozen=True)
class LlmSettings:
    model: str = ""
    enabled: bool = False
    daily_microusd: int = 3_000_000
    monthly_microusd: int = 50_000_000
    total_microusd: int = 10_000_000
    max_output_tokens: int = 4000
    max_request_bytes: int = 120000
    provider: str = "openai"
    read_timeout_seconds: int = 60

    def __post_init__(self):
        models = {"openai": set(RATES), "kimi-cn": {"kimi-k2.6"}, "kimi-intl": {"kimi-k2.6"}}
        if self.provider not in models or type(self.enabled) is not bool or self.model not in {"", *models[self.provider]}:
            raise ValueError("UNSUPPORTED_LLM_SETTINGS")
        for key, minimum, maximum in (("daily_microusd", 1, 100_000_000), ("monthly_microusd", 1, 1_000_000_000),
                                      ("total_microusd", 1, 1_000_000_000),
                                      ("max_output_tokens", 500, 16000), ("max_request_bytes", 1000, 120000),
                                      ("read_timeout_seconds", 10, 300)):
            if type(getattr(self, key)) is not int or not minimum <= getattr(self, key) <= maximum:
                raise ValueError("INVALID_LLM_LIMIT")

    @classmethod
    def from_env(cls):
        return cls(model=os.getenv("EP_LLM_MODEL", ""), provider=os.getenv("EP_LLM_PROVIDER", "openai"),
                   enabled=os.getenv("EP_LLM_ENABLED", "false").lower() == "true",
                   daily_microusd=int(os.getenv("EP_LLM_DAILY_MICROUSD", "3000000")),
                   monthly_microusd=int(os.getenv("EP_LLM_MONTHLY_MICROUSD", "50000000")),
                   total_microusd=int(os.getenv("EP_LLM_TOTAL_MICROUSD", "10000000")),
                   max_output_tokens=int(os.getenv("EP_LLM_MAX_OUTPUT_TOKENS", "4000")),
                   read_timeout_seconds=int(os.getenv("EP_LLM_READ_TIMEOUT_SECONDS", "60")))


# Standard USD/million token rates verified 2026-09-08. No alias substitution or automatic model upgrade.
RATES = {"gpt-5.4-mini": (0.75, 4.5), "gpt-5.4": (2.5, 15)}


def pricing(settings: LlmSettings) -> dict:
    if settings.provider == "openai":
        rates, currency, factor, revision = RATES[settings.model], "USD", 1, "2026-09-08-standard"
    elif settings.provider == "kimi-intl":
        rates, currency, factor, revision = (0.95, 4.00), "USD", 1, "2026-09-09-kimi-k26"
    else:
        # Conservative budget conversion, NOT a live FX quote. Cache discounts are ignored.
        rates, currency, factor, revision = (6.50, 27.00), "CNY", 0.20, "2026-09-09-kimi-k26"
    return {"input_per_million": rates[0], "output_per_million": rates[1],
            "currency": currency, "budget_usd_per_currency_unit": factor, "revision": revision}


def inline_schema(schema: dict) -> dict:
    """The fixed, acyclic contract is expanded for K2.6's limited $ref support."""
    definitions = schema.get("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return expand(definitions[value["$ref"].removeprefix("#/$defs/")])
        branches = [expand(branch) for branch in value.get("anyOf", [])]
        if len(branches) == 2 and branches[0].get("type") in {"string", "object"} and branches[1] == {"type": "null"}:
            return {**{key: item for key, item in value.items() if key != "anyOf"},
                    **branches[0], "type": [branches[0]["type"], "null"]}
        return {key: expand(item) for key, item in value.items() if key != "$defs"}

    return expand(schema)


def responses_payload(request: dict, settings: LlmSettings) -> dict:
    if not settings.model:
        raise ValueError("LLM_MODEL_NOT_SELECTED")
    if "batch" in request and settings.max_output_tokens < 8000:
        raise ValueError("BATCH_OUTPUT_LIMIT_TOO_SMALL")
    schema = selection_schema(request["batch"]) if request.get("version") == SPAN_VERSION else response_schema(request)
    if request.get("version") == "ep-event-interpretation-v1":
        from .llm_event import EventResponse
        schema = EventResponse.model_json_schema()
    if request.get("version") == "ep-event-claims-v2":
        from .llm_event_claims import AtomicResponse
        schema = AtomicResponse.model_json_schema()
    if request.get('version') == 'ep-event-context-v1':
        from .llm_event_context import ContextResponse
        schema = ContextResponse.model_json_schema()
    content = {k: v for k, v in request.items() if k != "rules"}
    payload = {"model": settings.model, "instructions": request["rules"],
        "input": [{"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "ep_claims", "strict": True, "schema": schema}},
        "max_output_tokens": settings.max_output_tokens, "store": False, "tools": [], "truncation": "disabled",
        "service_tier": "default"}
    if settings.provider != "openai":
        payload = {"model": settings.model,
            "messages": [{"role": "system", "content": request["rules"]},
                         {"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "ep_claims", "strict": True,
                                "schema": inline_schema(schema)}},
            "max_completion_tokens": settings.max_output_tokens,
            "thinking": {"type": "disabled"}, "stream": False, "n": 1}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > settings.max_request_bytes:
        raise ValueError("LLM_REQUEST_BUDGET_EXCEEDED")
    return payload


class OpenAIResponsesTransport(ResponsesHttpClient):
    def generate(self, payload: dict) -> dict:
        try:
            return strict_json(super().generate(payload))
        except ValueError:
            raise LlmError("LLM_INVALID_RESPONSE_JSON") from None


class KimiChatTransport(KimiHttpClient):
    def generate(self, payload: dict) -> dict:
        try:
            return strict_json(super().generate(payload))
        except ValueError:
            raise LlmError("LLM_INVALID_RESPONSE_JSON") from None


def create_transport(settings: LlmSettings, key: str) -> ModelTransport:
    if settings.provider == "openai":
        return OpenAIResponsesTransport(key, timeout=settings.read_timeout_seconds)
    return KimiChatTransport(key, provider=settings.provider, timeout=settings.read_timeout_seconds)


def extract_kimi_response(body: dict) -> tuple[dict, dict]:
    if not isinstance(body, dict) or body.get("object") != "chat.completion":
        raise LlmError("LLM_UNEXPECTED_OUTPUT")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise LlmError("LLM_EXPECTED_ONE_JSON_OUTPUT")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise LlmError("LLM_UNEXPECTED_OUTPUT")
    if choice.get("finish_reason") != "stop":
        raise LlmError("LLM_RESPONSE_INCOMPLETE")
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise LlmError("LLM_REFUSAL")
    if message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call"):
        raise LlmError("LLM_UNEXPECTED_OUTPUT")
    if not isinstance(message.get("content"), str):
        raise LlmError("LLM_EXPECTED_ONE_JSON_OUTPUT")
    return strict_json(message["content"]), response_usage(body, "kimi")


def response_usage(body: dict, provider: str) -> dict:
    usage = body.get("usage") or {}
    keys = ("input_tokens", "output_tokens") if provider == "openai" else ("prompt_tokens", "completion_tokens")
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys):
        raise LlmError("LLM_USAGE_UNAVAILABLE")
    return dict(zip(("input_tokens", "output_tokens"), (usage[key] for key in keys)))


def extract_response(body: dict) -> tuple[dict, dict]:
    if not isinstance(body, dict) or body.get("status") != "completed":
        raise LlmError("LLM_RESPONSE_INCOMPLETE")
    texts = []
    for item in body.get("output", []):
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise LlmError("LLM_UNEXPECTED_OUTPUT")
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                raise LlmError("LLM_REFUSAL")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise LlmError("LLM_UNEXPECTED_OUTPUT")
            texts.append(part["text"])
    if len(texts) != 1:
        raise LlmError("LLM_EXPECTED_ONE_JSON_OUTPUT")
    return strict_json(texts[0]), response_usage(body, "openai")
