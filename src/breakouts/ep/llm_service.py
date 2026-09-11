"""Bounded extraction with durable reservations; no automatic retries or alerts."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json
import re

from .llm_contract import prepare_request, strict_json, validate_response
from .llm_provider import LlmError, LlmSettings, pricing, extract_response, extract_kimi_response, responses_payload, response_usage
from .models import digest, timestamp
from src.data.llm_transport import PROVIDER_ERROR_TYPES
from .llm_batches import SCOPES
from .llm_span_selection import prepare_span_packet, validate_selection
from .llm_event import prepare_event_packet, validate_event
from .llm_event_claims import prepare_atomic_packet, validate_atomic


def _prepare(source, batch, protocol, paragraph_ids):
    if protocol == "event-claims":
        if batch is not None:
            raise ValueError("EVENT_BATCH_NOT_SUPPORTED")
        packet = prepare_atomic_packet(source, paragraph_ids)
        return packet["request"], packet
    if protocol == "event-interpretation":
        if batch is not None:
            raise ValueError("EVENT_BATCH_NOT_SUPPORTED")
        packet = prepare_event_packet(source, paragraph_ids)
        return packet["request"], packet
    if protocol == "span-selection":
        if batch is None:
            raise ValueError("SPAN_BATCH_REQUIRED")
        packet = prepare_span_packet(source, batch=batch, paragraph_ids=paragraph_ids)
        return packet["request"], packet
    if protocol != "claims" or paragraph_ids is not None:
        raise ValueError("INVALID_LLM_PROTOCOL_OPTIONS")
    return prepare_request(source, batch=batch), None


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


def plan_llm(store, source_id, settings, *, as_of=None, batch=None, protocol="claims", paragraph_ids=None):
    source = store.source_detail(source_id, as_of=as_of)
    request, packet = _prepare(source, batch, protocol, paragraph_ids)
    payload = responses_payload(request, settings) if settings.model else None
    result = {"source_id": source_id, "request_id": request["request_id"], "coverage": request["coverage"],
            "model": settings.model or None, "provider": settings.provider, "enabled": settings.enabled,
            "budget_estimate": estimate_reservation(payload, settings) if payload else None,
            "request_key": digest({"provider": "openai-responses" if settings.provider == "openai" else settings.provider,
                                   "payload": payload}) if payload else None,
            "blockers": ([] if settings.model else ["MODEL_NOT_SELECTED"]) + ([] if settings.enabled else ["LLM_DISABLED"]),
            "batch": request.get("batch"), "exhaustiveness_verified": False,
            "contains_vendor_news": False, "external_requests": 0, "eligible_for_rating": False}
    if packet is not None:
        result.update(protocol=protocol, packet_hash=packet["packet_hash"],
                      selected_paragraph_ids=[b["paragraph_id"] for b in request["untrusted_blocks"]],
                      payload_bytes=len(json.dumps(payload, ensure_ascii=False).encode()) if payload else None)
        if payload:
            result["preflight"] = store.preview_llm_call(result["request_key"], result["budget_estimate"]["reserved_microusd"],
                                                       settings, as_of or datetime.now(timezone.utc))
            if result["preflight"]["status"] in {"BILLING_REVIEW_REQUIRED", "BUDGET_EXHAUSTED", "JOURNAL_UPGRADE_REQUIRED"}:
                result["blockers"].append(result["preflight"]["status"])
    return result


def run_llm(store, source_id, settings, transport, *, clock=lambda: datetime.now(timezone.utc), retry_of=None, batch=None,
            protocol="claims", paragraph_ids=None, expected_request_key=None):
    if not settings.enabled or not settings.model:
        raise ValueError("LLM_DISABLED_OR_MODEL_NOT_SELECTED")
    if getattr(transport, "provider", settings.provider) != settings.provider:
        raise ValueError("LLM_TRANSPORT_PROVIDER_MISMATCH")
    now = clock()
    timestamp(now)
    source = store.source_detail(source_id, as_of=now)
    request, packet = _prepare(source, batch, protocol, paragraph_ids)
    payload = responses_payload(request, settings)
    reservation = estimate_reservation(payload, settings)
    provider_id = "openai-responses" if settings.provider == "openai" else settings.provider
    key = digest({"provider": provider_id, "payload": payload})
    logical_key = key
    if expected_request_key is not None and expected_request_key != logical_key:
        raise ValueError("PLANNED_REQUEST_CHANGED")
    if retry_of is not None:
        if not isinstance(retry_of, str) or not re.fullmatch(r"[a-f0-9]{64}", retry_of):
            raise ValueError("INVALID_LLM_RETRY_PARENT")
        key = digest({"logical_request_key": logical_key, "retry_of": retry_of})
    journal_request = {"source_request": request, "model": settings.model, "provider": settings.provider,
        "reservation": reservation, "logical_request_key": logical_key, "retry_of": retry_of,
        "read_timeout_seconds": settings.read_timeout_seconds}
    if packet is not None:
        journal_request.update(protocol=protocol)
        journal_request["event_packet" if protocol in {"event-interpretation", "event-claims"} else "selection_packet"] = packet
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
        validation = (validate_atomic(packet, raw) if protocol == "event-claims" else
                      validate_event(packet, raw) if protocol == "event-interpretation" else
                      validate_selection(packet, raw) if packet is not None else validate_response(request, raw))
        result = {"status": "VALIDATED", "validation": validation, "usage": usage,
                  "returned_model": returned_model}
    except (LlmError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Never persist exception messages containing credentials, URLs or echoed provider input.
        known = {"LLM_RESPONSE_INCOMPLETE", "LLM_REFUSAL", "LLM_USAGE_UNAVAILABLE", "LLM_EXPECTED_ONE_JSON_OUTPUT",
                 "LLM_UNEXPECTED_OUTPUT", "LLM_TRANSPORT_FAILED", "LLM_RESPONSE_LIMIT_EXCEEDED", "LLM_INVALID_RESPONSE_JSON",
                 "INVALID_MODEL_ENVELOPE", "MODEL_DOCUMENT_VERSION_MISMATCH", "INVALID_MODEL_CLAIM_COUNT", "INVALID_MODEL_SCOPE_STATUS",
                 "DUPLICATE_JSON_KEY", "NON_FINITE_JSON", "INVALID_MODEL_JSON", "MODEL_RESPONSE_TOO_LARGE"}
        known.add("LLM_MODEL_MISMATCH")
        known.update({"INVALID_SELECTION_RESPONSE", "SELECTION_DOCUMENT_VERSION_MISMATCH", "LOCAL_PACKET_CHANGED", "DUPLICATE_FRAGMENT_ID"})
        known.update({"INVALID_EVENT_RESPONSE", "EVENT_DOCUMENT_VERSION_MISMATCH"})
        known.update({"LLM_CONNECT_TIMEOUT", "LLM_READ_TIMEOUT", "LLM_TLS_FAILED", "LLM_CONNECTION_FAILED",
                      "LLM_TIMEOUT", "LLM_ELAPSED_LIMIT_EXCEEDED"})
        reason = str(exc) if str(exc) in known or re.fullmatch(r"LLM_HTTP_[0-9]{3}", str(exc)) else "MODEL_TRANSPORT_OR_VALIDATION_FAILED"
        result = {"status": "BILLING_REVIEW_REQUIRED" if reason == "LLM_MODEL_MISMATCH" else "FAILED",
                  "error": reason, "validation": None}
    # A truncated/invalid answer can still be billed. Preserve its usage without accepting any facts.
    if isinstance(saved_body, dict):
        returned_model = saved_body.get("model")
        if not isinstance(returned_model, str) or not re.fullmatch(re.escape(settings.model) + r"(?:-\d{4}-\d{2}-\d{2})?", returned_model):
            result["status"] = "BILLING_REVIEW_REQUIRED"
        else:
            try:
                usage = response_usage(saved_body, settings.provider)
            except LlmError:
                usage = None
            if usage is not None:
                price = reservation["pricing"]
                rates = [Decimal(str(price[k])) * Decimal(str(price["budget_usd_per_currency_unit"]))
                         for k in ("input_per_million", "output_per_million")]
                measured = int((usage["input_tokens"] * rates[0] + usage["output_tokens"] * rates[1]).to_integral_value(rounding=ROUND_CEILING))
                result.update(usage=usage, estimated_usage_microusd=measured,
                              reservation_exceeded=measured > reservation["reserved_microusd"])
                if result["reservation_exceeded"]:
                    result["status"] = "BILLING_REVIEW_REQUIRED"
        if settings.provider != "openai":
            choices = saved_body.get("choices")
            if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
                finish = choices[0].get("finish_reason")
                if finish in ("stop", "length", "content_filter", "tool_calls"):
                    result["finish_reason"] = finish
    result.update(request_key=key, source_id=source_id, model=settings.model, provider=settings.provider, reservation=reservation,
                  eligible_for_rating=False, delivery="DISABLED_SHADOW_ONLY", retry_of=retry_of,
                  source_request_id=request["request_id"], logical_request_key=logical_key, batch=request.get("batch"))
    if packet is not None:
        result.update(protocol=protocol, packet_hash=packet["packet_hash"])
    # Persist only locally produced numeric diagnostics and fixed phase names, never HTTP text/headers.
    diagnostics = getattr(transport, "last_diagnostics", {})
    result["transport_diagnostics"] = {k: v for k, v in diagnostics.items()
        if (k in {"elapsed_seconds", "headers_elapsed_seconds", "http_status", "received_bytes", "read_timeout_seconds", "retry_after_seconds"}
            and type(v) in {int, float} and 0 <= v < 1e9)
        or (k == "phase" and v in {"WAITING_HEADERS", "READING_BODY", "COMPLETE"})
        or (k == "provider_error_type" and v in PROVIDER_ERROR_TYPES)
        or (k == "rate_limit_kind" and v in {"CONCURRENCY", "RPM", "TPM", "TPD"})}
    store.finish_llm_call(key, result, saved_body, clock())
    return {"status": result["status"], "reused": False, "result": result, "external_requests": 1,
            "request_key": key, "eligible_for_rating": False}


def batch_status(store, source_id, settings):
    """Read-only progress for the current source/contract; never hide missing or failed scopes."""
    history = store.llm_history(source_id)
    rows = {}
    for name in SCOPES:
        plan = plan_llm(store, source_id, settings, batch=name)
        matches = [row for row in history if row["request_key"] == plan["request_key"]
                   or (row.get("result") or {}).get("logical_request_key") == plan["request_key"]]
        row = matches[-1] if matches else None
        result = (row["result"] or {}) if row else {}
        validation = result.get("validation") or {}
        rows[name] = {"status": row["status"] if row else "NOT_COMPLETED", "request_key": row["request_key"] if row else None,
            "error": result.get("error"), "accepted_count": len(validation.get("accepted", [])),
            "rejected_count": len(validation.get("rejected", [])), "model_scope_status": validation.get("model_scope_status"),
            "validation_status": validation.get("status"),
            "rejection_reasons": sorted({reason for item in validation.get("rejected", []) for reason in item["reasons"]}),
            "claim_limit_reached": validation.get("claim_limit_reached"), "input_coverage": plan["coverage"]}
    return {"batches": rows, "all_batches_returned_valid_envelopes": all(row["status"] == "VALIDATED" for row in rows.values()),
            "exhaustiveness_verified": False, "financial_semantics_verified": False,
            "outside_batch_scope": ["ARR", "CRPO", "BACKLOG", "RETENTION", "GMV", "BUYBACK", "CONTRACT_VALUE"],
            "eligible_for_rating": False, "delivery": "DISABLED_SHADOW_ONLY", "external_requests": 0}
