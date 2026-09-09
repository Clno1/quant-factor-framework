"""Credential-isolated, bounded HTTP transports for allowlisted official endpoints."""
import re
from time import monotonic


class LlmTransportError(Exception):
    pass


class ResponsesHttpClient:
    provider = "openai"
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, key: str, *, timeout=60):
        if not key or not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
            raise ValueError("EP_LLM_API_KEY_MISSING_OR_INVALID")
        self._key = key
        self.timeout = timeout

    def generate(self, payload: dict) -> bytes:
        import requests
        started = monotonic()
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.post(self.endpoint, headers={"Authorization": "Bearer " + self._key}, json=payload,
                                  timeout=(10, self.timeout), allow_redirects=False, stream=True) as response:
                    if response.status_code != 200:
                        raise LlmTransportError("LLM_HTTP_" + str(response.status_code))
                    raw = bytearray()
                    for chunk in response.iter_content(16384):
                        raw.extend(chunk)
                        if len(raw) > 1_000_000 or monotonic() - started > self.timeout:
                            raise LlmTransportError("LLM_RESPONSE_LIMIT_EXCEEDED")
                    return bytes(raw)
        except requests.RequestException:
            raise LlmTransportError("LLM_TRANSPORT_FAILED") from None


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
