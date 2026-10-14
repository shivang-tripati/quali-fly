"""Groq's OpenAI-compatible Chat Completions endpoint, over the httpx client we already have.

Request:  POST https://api.groq.com/openai/v1/chat/completions
Auth:     Authorization: Bearer $GROQ_API_KEY
Body:     {model, messages:[{role, content}], temperature, max_tokens,
           response_format:{"type":"json_object"}, stream:false}
Response: choices[0].message.content  +  usage.{prompt_tokens, completion_tokens}

The one non-obvious decision here is `response_format`. Groq offers two structured-output
modes, and they are not interchangeable:

  * `{"type":"json_object"}`  - "JSON Object Mode". Emits *a* JSON object, and rejects
    non-object JSON. Available on every Groq model, including the Llama models this
    product uses.
  * `{"type":"json_schema", ...}` - strict schema enforcement. Per Groq's model list this
    is currently limited to the GPT-OSS and Qwen families.

We are on Llama, so strict schema mode is not on the table. JSON Object Mode plus the
Pydantic validation in `app/schemas/ai.py` is the real correctness boundary. That is a
sound combination - the mode guarantees we can parse, the validator guarantees we can
trust - but the guarantee comes from the validator, not from the provider.
"""

import json
import logging
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

log = logging.getLogger("app.ai.groq")

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_JSON_OBJECT_MODE = {"type": "json_object"}

# Mirrors a couple of the classifications the Graph client already makes, but on Groq's
# own codes. Anything unexpected is treated as transient: for a read-only inference call,
# an unrecognised failure is more often a proxy blip than a permanent condition.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


class GroqProvider:
    """Thin wrapper over one HTTP endpoint. Safe to reuse for the process lifetime.

    Configuration is per-instance, never module-level, so two providers built with
    different settings cannot interfere with each other.
    """

    name = "groq"

    def __init__(
        self,
        api_key: str | None,
        *,
        timeout: float = 20.0,
        temperature: float = 0.3,
        max_tokens: int = 150,
    ) -> None:
        self._api_key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
                "User-Agent": "quali-fly/5",
            },
        )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def generate_structured(self, request: AIRequest, *, model: str) -> LLMResult:
        if not self._api_key:
            raise LLMConfigurationError("Groq is selected but GROQ_API_KEY is not set.")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            # See the module docstring: JSON Object Mode is the widest-compatible
            # structured mode on Groq, and validation is enforced downstream.
            "response_format": _JSON_OBJECT_MODE,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(_GROQ_URL, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"Groq request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(f"Groq transport error: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise _classify(response)

        latency_ms = int((time.perf_counter() - started) * 1000)
        body = _json_object(response)
        return LLMResult(
            content=_extract_content(body),
            provider=self.name,
            model=model,
            prompt_tokens=_usage_int(body, "prompt_tokens"),
            completion_tokens=_usage_int(body, "completion_tokens"),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _classify(response: httpx.Response) -> LLMError:
    # Status only, never the body: a provider error can echo the prompt, and this ends
    # up in logs.
    message = f"Groq returned HTTP {response.status_code}"
    if response.status_code in (401, 403):
        return LLMConfigurationError(f"{message}. Check GROQ_API_KEY.")
    if response.status_code == 404:
        return LLMConfigurationError(f"{message}. Check the configured model id.")
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
        raise LLMResponseError("Groq returned a 2xx response that was not JSON.") from exc
    if not isinstance(body, dict):
        raise LLMResponseError("Groq returned a JSON value that was not an object.")
    return body


def _extract_content(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("Groq response had no choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMResponseError("Groq response choice was not an object.")
    # A refusal arrives with content unset. Treat a missing or blank content string as
    # invalid rather than inventing a fallback response.
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LLMResponseError("Groq response contained no content.")
    return content


def _usage_int(body: dict[str, Any], key: str) -> int | None:
    value = (body.get("usage") or {}).get(key)
    return value if isinstance(value, int) else None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        # `Retry-After` is also allowed to be an HTTP-date; we do not parse dates for a
        # single retry, we just fall back to the configured backoff.
        return None


def build_groq_provider(
    api_key: str | None, *, timeout: float, temperature: float, max_tokens: int
) -> GroqProvider:
    """The supported way to construct a provider, so tests and prod agree on config."""
    return GroqProvider(api_key, timeout=timeout, temperature=temperature, max_tokens=max_tokens)
