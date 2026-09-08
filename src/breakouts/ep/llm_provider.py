"""Single-shot Responses adapter; credentials never enter saved request/result objects."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Protocol

from src.data.llm_transport import ResponsesHttpClient, LlmTransportError as LlmError
from .llm_contract import Response, strict_json


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

    def __post_init__(self):
        if type(self.enabled) is not bool or self.model not in {"", *RATES}:
            raise ValueError("UNSUPPORTED_LLM_SETTINGS")
        for key, minimum, maximum in (("daily_microusd", 1, 100_000_000), ("monthly_microusd", 1, 1_000_000_000),
                                      ("total_microusd", 1, 1_000_000_000),
                                      ("max_output_tokens", 500, 16000), ("max_request_bytes", 1000, 120000)):
            if type(getattr(self, key)) is not int or not minimum <= getattr(self, key) <= maximum:
                raise ValueError("INVALID_LLM_LIMIT")

    @classmethod
    def from_env(cls):
        return cls(model=os.getenv("EP_LLM_MODEL", ""), enabled=os.getenv("EP_LLM_ENABLED", "false").lower() == "true",
                   daily_microusd=int(os.getenv("EP_LLM_DAILY_MICROUSD", "3000000")),
                   monthly_microusd=int(os.getenv("EP_LLM_MONTHLY_MICROUSD", "50000000")),
                   total_microusd=int(os.getenv("EP_LLM_TOTAL_MICROUSD", "10000000")),
                   max_output_tokens=int(os.getenv("EP_LLM_MAX_OUTPUT_TOKENS", "4000")))


# Standard USD/million token rates verified 2026-09-08. No alias substitution or automatic model upgrade.
RATES = {"gpt-5.4-mini": (0.75, 4.5), "gpt-5.4": (2.5, 15)}


def responses_payload(request: dict, settings: LlmSettings) -> dict:
    if not settings.model:
        raise ValueError("LLM_MODEL_NOT_SELECTED")
    content = {k: v for k, v in request.items() if k != "rules"}
    payload = {"model": settings.model, "instructions": request["rules"],
        "input": [{"role": "user", "content": json.dumps(content, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "ep_claims", "strict": True, "schema": Response.model_json_schema()}},
        "max_output_tokens": settings.max_output_tokens, "store": False, "tools": [], "truncation": "disabled",
        "service_tier": "default"}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > settings.max_request_bytes:
        raise ValueError("LLM_REQUEST_BUDGET_EXCEEDED")
    return payload


class OpenAIResponsesTransport(ResponsesHttpClient):
    def generate(self, payload: dict) -> dict:
        try:
            return strict_json(super().generate(payload))
        except ValueError:
            raise LlmError("LLM_INVALID_RESPONSE_JSON") from None


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
    usage = body.get("usage") or {}
    if any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens")):
        raise LlmError("LLM_USAGE_UNAVAILABLE")
    return strict_json(texts[0]), {key: usage[key] for key in ("input_tokens", "output_tokens")}
