"""Gemini `generateContent` over REST, using the httpx client we already depend on.

Request:  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
Auth:     x-goog-api-key: $GEMINI_API_KEY   (header, not a query parameter, so the key
          does not land in access logs or proxy traces)
Body:     {contents:[{role:"user", parts:[{text}]}], systemInstruction:{parts:[{text}]},
           generationConfig:{temperature, maxOutputTokens, responseMimeType,
                             responseSchema}}
Response: candidates[0].content.parts[0].text
          +  usageMetadata.{promptTokenCount, candidatesTokenCount, totalTokenCount}

Two details are worth knowing before changing this file.

**1. We send `responseSchema` in addition to `responseMimeType`.** Gemini's structured
output is materially stronger than Groq's JSON Object Mode: with a schema, constrained
decoding enforces the shape at generation time. It still needs our Pydantic validation -
constrained decoding is a best effort, and a provider regression must not be able to
write a 900 into our database - but the two are complementary, not redundant.

**2. Temperature is configurable per provider, and the default is not the PRD's 0.3.**
Google's current guidance for the 3.x model family is that values below 1.0 can cause
repetition and looping. We therefore default Gemini to 1.0 and leave Groq at the PRD's
0.3. This is a documented, deliberate deviation driven by the API's own requirements,
which is exactly the case the PRD allows for ("unless the current provider SDK/API
requires a compatible equivalent"). Both values are configuration.
"""

import json
import logging
import re
import time
from typing import Any

import httpx

from app.schemas.ai import AIRequest, LLMResult
from app.services.llm_base import (
    LLMConfigurationError,
    LLMError,
    LLMResponseError,
    LLMTransientError,
)

log = logging.getLogger("app.ai.gemini")

_GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRYABLE_STATUS = frozenset({408, 429})

# Model ids are settings, but they still need to be safe to interpolate into a path.
_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# The OpenAPI-ish subset Gemini's responseSchema accepts. We ask only for what we need:
# the four integer dimensions and the two scalar response fields. Nested objects are
# deliberately omitted - the validator, not the schema, enforces `extra="forbid"`.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "message": {"type": "STRING"},
        "budget": {"type": "INTEGER"},
        "timeline": {"type": "INTEGER"},
        "fit": {"type": "INTEGER"},
        "authority": {"type": "INTEGER"},
        "human_handoff": {"type": "BOOLEAN"},
        "handoff_reason": {"type": "STRING"},
    },
    # Required drives constrained decoding; a model that cannot fill one of these will
    # fail loudly at validation instead of quietly defaulting.
    "required": [
        "message",
        "budget",
        "timeline",
        "fit",
        "authority",
        "human_handoff",
    ],
}


class GeminiProvider:
    """Thin wrapper over one HTTP endpoint. Per-instance configuration, never global."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None,
        *,
        timeout: float = 20.0,
        temperature: float = 1.0,
        max_tokens: int = 150,
    ) -> None:
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={"Content-Type": "application/json", "User-Agent": "quali-fly/5"},
        )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def generate_structured(self, request: AIRequest, *, model: str) -> LLMResult:
        if not self._api_key:
            raise LLMConfigurationError(
                "Gemini is selected as a fallback but GEMINI_API_KEY is not set."
            )
        if not _SAFE_MODEL_ID.match(model):
            # Configuration reaches us from the environment, but interpolating it into a
            # URL unescaped is still how path traversal gets into an outbound request.
            raise LLMConfigurationError("GEMINI_MODEL is not a valid model identifier.")
        payload = {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            # A single user turn. Gemini also takes `model` turns in `contents`, but
            # flattening keeps the injection boundaries that the prompt builder
            # established intact, and history is already summarised into the prompt.
            "contents": [{"role": "user", "parts": [{"text": request.user_prompt}]}],
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }
        url = f"{_GEMINI_ROOT}/{model}:generateContent"
        started = time.perf_counter()
        try:
            response = await self._client.post(
                url, json=payload, headers={"x-goog-api-key": self._api_key}
            )
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"Gemini request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"Gemini transport error: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise _classify(response)

        latency_ms = int((time.perf_counter() - started) * 1000)
        body = _json_object(response)
        usage = body.get("usageMetadata") or {}
        return LLMResult(
            content=_extract_text(body),
            provider=self.name,
            model=model,
            prompt_tokens=_usage_int(usage, "promptTokenCount"),
            completion_tokens=_usage_int(usage, "candidatesTokenCount"),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _classify(response: httpx.Response) -> LLMError:
    message = f"Gemini returned HTTP {response.status_code}"
    if response.status_code in (400, 404):
        # 404 is the usual "model not available to this key"; 400 covers a malformed or
        # unsupported generationConfig, e.g. a schema this model will not accept.
        return LLMConfigurationError(f"{message}. Check GEMINI_MODEL and the request shape.")
    if response.status_code in (401, 403):
        return LLMConfigurationError(f"{message}. Check GEMINI_API_KEY.")
    if response.status_code in _RETRYABLE_STATUS or response.status_code >= 500:
        return LLMTransientError(
            message,
            status_code=response.status_code,
            retry_after=_retry_after(response),
        )
    return LLMResponseError(message, status_code=response.status_code)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMResponseError("Gemini returned a 2xx response that was not JSON.") from exc
    if not isinstance(body, dict):
        raise LLMResponseError("Gemini returned a JSON value that was not an object.")
    return body


def _extract_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        # `promptFeedback.blockReason` is the usual cause - a safety block returns 200
        # with no candidates at all. We surface it as an invalid response, which routes
        # to the failure path rather than to a fabricated reply.
        raise LLMResponseError("Gemini response had no candidates.")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise LLMResponseError("Gemini response candidate had no parts.")
    # Concatenate: a non-streaming response can still split content across parts.
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        raise LLMResponseError("Gemini response contained no text.")
    return text


def _usage_int(usage: Any, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) else None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def build_gemini_provider(
    api_key: str | None, *, timeout: float, temperature: float, max_tokens: int
) -> GeminiProvider:
    """The supported way to construct a provider, so tests and prod agree on config."""
    return GeminiProvider(api_key, timeout=timeout, temperature=temperature, max_tokens=max_tokens)
