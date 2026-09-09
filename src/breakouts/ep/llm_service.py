"""Bounded extraction with durable pre-request reservations; never retries or sends alerts."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
import re

from .llm_contract import prepare_request, strict_json, validate_response
from .llm_provider import LlmError, LlmSettings, pricing, extract_response, extract_kimi_response, responses_payload
from .models import digest, timestamp


def estimate_reservation(payload, settings=None):
    settings = settings or LlmSettings(model=payload["model"], max_output_tokens=payload["max_output_tokens"])
    price = pricing(settings)
    factor = Decimal(str(price["budget_usd_per_currency_unit"]))
    input_rate, output_rate = (Decimal(str(price[k])) * factor for k in ("input_per_million", "output_per_million"))
    # Conservative planning envelope, not a provider invoice guarantee or tokenizer measurement.
    estimated_input = 2 * len(json.dumps(payload, ensure_ascii=False).encode()) + 4096
    output_limit = settings.max_output_tokens
    amount = estimated_input * input_rate + output_limit * output_rate
    return {"reserved_microusd": int(amount.to_integral_value(rounding=ROUND_CEILING)),
            "planning_input_tokens": estimated_input, "output_token_limit": output_limit,
            "pricing_revision": price["revision"], "pricing": price, "billing_guaranteed": False}


def plan_llm(store, source_id, settings, *, as_of=None):
    source = store.source_detail(source_id, as_of=as_of)
    request = prepare_request(source)
    return {"source_id": source_id, "request_id": request["request_id"], "coverage": request["coverage"],
            "model": settings.model or None, "provider": settings.provider, "enabled": settings.enabled,
            "budget_estimate": estimate_reservation(responses_payload(request, settings), settings) if settings.model else None,
            "blockers": ([] if settings.model else ["MODEL_NOT_SELECTED"]) + ([] if settings.enabled else ["LLM_DISABLED"]),
            "contains_vendor_news": False, "external_requests": 0, "eligible_for_rating": False}


def run_llm(store, source_id, settings, transport, *, clock=lambda: datetime.now(timezone.utc)):
    if not settings.enabled or not settings.model:
        raise ValueError("LLM_DISABLED_OR_MODEL_NOT_SELECTED")
    if getattr(transport, "provider", settings.provider) != settings.provider:
        raise ValueError("LLM_TRANSPORT_PROVIDER_MISMATCH")
    now = clock()
    timestamp(now)
    source = store.source_detail(source_id, as_of=now)
    request = prepare_request(source)
    payload = responses_payload(request, settings)
    reservation = estimate_reservation(payload, settings)
    provider_id = "openai-responses" if settings.provider == "openai" else settings.provider
    key = digest({"provider": provider_id, "payload": payload})
    journal_request = {"source_request": request, "model": settings.model, "provider": settings.provider, "reservation": reservation}
    claimed = store.reserve_llm_call(key, source_id, journal_request, reservation["reserved_microusd"], settings, now)
    if not claimed["reserved"]:
        return {"status": claimed["status"], "reused": claimed["result"] is not None or claimed["status"] == "RESERVED",
                "result": claimed["result"], "external_requests": 0,
                "request_key": key, "eligible_for_rating": False}
    saved_body = None
    try:
        body = transport.generate(payload)
        body = strict_json(json.dumps(body, allow_nan=False))
        saved_body = body
        raw, usage = (extract_response if settings.provider == "openai" else extract_kimi_response)(body)
        returned_model = body.get("model", "")
        if not isinstance(returned_model, str) or not re.fullmatch(re.escape(settings.model) + r"(?:-\d{4}-\d{2}-\d{2})?", returned_model):
            raise LlmError("LLM_MODEL_MISMATCH")
        validation = validate_response(request, raw)
        price = reservation["pricing"]
        rates = [Decimal(str(price[k])) * Decimal(str(price["budget_usd_per_currency_unit"]))
                 for k in ("input_per_million", "output_per_million")]
        measured = int((usage["input_tokens"] * rates[0] + usage["output_tokens"] * rates[1]).to_integral_value(rounding=ROUND_CEILING))
        result = {"status": "VALIDATED", "validation": validation, "usage": usage,
                  "returned_model": returned_model,
                  "estimated_usage_microusd": measured, "reservation_exceeded": measured > reservation["reserved_microusd"]}
        if result["reservation_exceeded"]:
            result["status"] = "BILLING_REVIEW_REQUIRED"
    except (LlmError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Never persist exception messages containing credentials, URLs or echoed provider input.
        known = {"LLM_RESPONSE_INCOMPLETE", "LLM_REFUSAL", "LLM_USAGE_UNAVAILABLE", "LLM_EXPECTED_ONE_JSON_OUTPUT",
                 "LLM_UNEXPECTED_OUTPUT", "LLM_TRANSPORT_FAILED", "LLM_RESPONSE_LIMIT_EXCEEDED", "LLM_INVALID_RESPONSE_JSON",
                 "INVALID_MODEL_ENVELOPE", "MODEL_DOCUMENT_VERSION_MISMATCH", "INVALID_MODEL_CLAIM_COUNT",
                 "DUPLICATE_JSON_KEY", "NON_FINITE_JSON", "INVALID_MODEL_JSON", "MODEL_RESPONSE_TOO_LARGE"}
        known.add("LLM_MODEL_MISMATCH")
        reason = str(exc) if str(exc) in known or re.fullmatch(r"LLM_HTTP_[0-9]{3}", str(exc)) else "MODEL_TRANSPORT_OR_VALIDATION_FAILED"
        result = {"status": "BILLING_REVIEW_REQUIRED" if reason == "LLM_MODEL_MISMATCH" else "FAILED",
                  "error": reason, "validation": None}
    result.update(request_key=key, source_id=source_id, model=settings.model, provider=settings.provider, reservation=reservation,
                  eligible_for_rating=False, delivery="DISABLED_SHADOW_ONLY")
    store.finish_llm_call(key, result, saved_body, clock())
    return {"status": result["status"], "reused": False, "result": result, "external_requests": 1,
            "request_key": key, "eligible_for_rating": False}
