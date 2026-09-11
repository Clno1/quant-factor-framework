"""Credential-isolated, bounded HTTP transports for allowlisted official endpoints."""
import json
import re
from time import monotonic


class LlmTransportError(Exception):
    pass


PROVIDER_ERROR_TYPES = frozenset({"engine_overloaded_error", "exceeded_current_quota_error",
    "rate_limit_reached_error", "invalid_request_error", "invalid_authentication_error",
    "incorrect_api_key_error", "permission_denied_error", "resource_not_found_error", "server_error"})


def error_diagnostics(raw):
    """Discard the provider message (which may contain account/key IDs or echoed input)."""
    try:
        body = json.loads(raw)
        error = body.get("error", {})
        kind = error.get("type")
        result = {"provider_error_type": kind} if kind in PROVIDER_ERROR_TYPES else {}
        message = error.get("message", "")
        if kind == "rate_limit_reached_error" and isinstance(message, str):
            for marker, category in (("max concurrency", "CONCURRENCY"), ("max RPM", "RPM"),
                                     ("TPM rate limit", "TPM"), ("TPD rate limit", "TPD")):
                if marker in message:
                    result["rate_limit_kind"] = category
                    break
        return result
    except (ValueError, TypeError, AttributeError):
        return {}


class ResponsesHttpClient:
    provider = "openai"
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, key: str, *, timeout=60):
        if not key or not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
            raise ValueError("EP_LLM_API_KEY_MISSING_OR_INVALID")
        self._key = key
        if type(timeout) is not int or not 10 <= timeout <= 300:
            raise ValueError("INVALID_LLM_TIMEOUT")
        self.timeout = timeout
        self.last_diagnostics = {}

    def generate(self, payload: dict) -> bytes:
        import requests
        from urllib3.exceptions import ReadTimeoutError
        started = monotonic()
        self.last_diagnostics = {"phase": "WAITING_HEADERS", "read_timeout_seconds": self.timeout,
                                 "received_bytes": 0}
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.post(self.endpoint, headers={"Authorization": "Bearer " + self._key}, json=payload,
                                  timeout=(10, self.timeout), allow_redirects=False, stream=True) as response:
                    self.last_diagnostics.update(http_status=response.status_code,
                        headers_elapsed_seconds=round(monotonic() - started, 3), phase="READING_BODY")
                    if response.status_code != 200:
                        retry_after = getattr(response, "headers", {}).get("Retry-After", "")
                        if isinstance(retry_after, str) and re.fullmatch(r"[0-9]{1,6}", retry_after):
                            self.last_diagnostics["retry_after_seconds"] = int(retry_after)
                        # Bounded error-body read; retain HTTP status even if body reading fails.
                        try:
                            error_raw = bytearray()
                            for chunk in response.iter_content(4096):
                                error_raw.extend(chunk)
                                if len(error_raw) > 16384 or monotonic() - started > self.timeout:
                                    break
                            else:
                                self.last_diagnostics.update(error_diagnostics(error_raw))
                        except (requests.RequestException, AttributeError):
                            pass
                        raise LlmTransportError("LLM_HTTP_" + str(response.status_code))
                    raw = bytearray()
                    for chunk in response.iter_content(16384):
                        raw.extend(chunk)
                        self.last_diagnostics["received_bytes"] = len(raw)
                        if len(raw) > 1_000_000:
                            raise LlmTransportError("LLM_RESPONSE_LIMIT_EXCEEDED")
                        # This is a checked elapsed limit, not a hard wall-clock deadline:
                        # a blocked socket read remains bounded by the read timeout above.
                        if monotonic() - started > self.timeout:
                            raise LlmTransportError("LLM_ELAPSED_LIMIT_EXCEEDED")
                    self.last_diagnostics["phase"] = "COMPLETE"
                    return bytes(raw)
        except requests.ConnectTimeout:
            raise LlmTransportError("LLM_CONNECT_TIMEOUT") from None
        except requests.ReadTimeout:
            raise LlmTransportError("LLM_READ_TIMEOUT") from None
        except requests.exceptions.SSLError:
            raise LlmTransportError("LLM_TLS_FAILED") from None
        except requests.ConnectionError as exc:
            # requests wraps streaming read timeouts in ConnectionError.
            reason = "LLM_READ_TIMEOUT" if any(isinstance(arg, ReadTimeoutError) for arg in exc.args) else "LLM_CONNECTION_FAILED"
            raise LlmTransportError(reason) from None
        except requests.Timeout:
            raise LlmTransportError("LLM_TIMEOUT") from None
        except requests.RequestException:
            raise LlmTransportError("LLM_TRANSPORT_FAILED") from None
        finally:
            self.last_diagnostics["elapsed_seconds"] = round(monotonic() - started, 3)


class KimiHttpClient(ResponsesHttpClient):
    """Explicit region selection; never try a credential against another provider."""

    ENDPOINTS = {
        "kimi-cn": "https://api.moonshot.cn/v1/chat/completions",
        "kimi-intl": "https://api.moonshot.ai/v1/chat/completions",
    }

    def __init__(self, key: str, *, provider: str, timeout=60):
        if provider not in self.ENDPOINTS:
            raise ValueError("UNSUPPORTED_LLM_PROVIDER")
        super().__init__(key, timeout=timeout)
        self.provider = provider
        self.endpoint = self.ENDPOINTS[provider]
