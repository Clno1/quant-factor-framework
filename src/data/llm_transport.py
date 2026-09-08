"""Credential-isolated, bounded HTTP transport for the official Responses endpoint."""
import re
from time import monotonic


class LlmTransportError(Exception):
    pass


class ResponsesHttpClient:
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
