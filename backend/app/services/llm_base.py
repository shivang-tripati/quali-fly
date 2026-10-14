"""The provider-neutral LLM interface.

One method, one result type, one failure taxonomy. Everything above this line is written
in terms of `LLMProvider` and never names Groq or Gemini; everything provider-specific
lives in the implementations and the registry.

The failure taxonomy is the part that matters most. "Retryable" is a *decision*, so it
cannot be a guess made by the caller from an exception message. Each provider maps its
own transport and HTTP semantics onto these classes once, and the orchestrator then
follows a bounded, deterministic policy:

    LLMError               base; not retryable by default
    ├── LLMTransientError  timeout, connection reset, 429, 5xx
    ├── LLMConfigurationError  missing/unusable key, unsupported model
    └── LLMResponseError   2xx with unusable content, or a refusal

Note on retries: a retryable failure is retried at most once per provider step, and only
after an optional provider `Retry-After`. It is never a long blocking sleep inside a
database transaction - see `llm_registry`, which sleeps outside any transaction and
falls back to the next provider instead.
"""

import logging
from typing import Protocol, runtime_checkable

from app.schemas.ai import AIRequest, LLMResult

log = logging.getLogger("app.ai.llm")


class LLMError(Exception):
    """Base for every provider failure. Carries no credential or prompt material."""

    #: Whether an identical retry could plausibly succeed.
    retryable = False
    #: Coarse category for logs and for `ai_message_runs.error_category`.
    category = "error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMTransientError(LLMError):
    """Timeout, connection failure, rate limit, or 5xx. Retrying once is reasonable."""

    retryable = True
    category = "transient"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        # Seconds, from a `Retry-After` header when the provider sent one. Capped by the
        # registry so a hostile or mistaken value cannot stall a worker.
        self.retry_after = retry_after


class LLMConfigurationError(LLMError):
    """No API key, an unusable key, or a model the provider does not offer.

    Never retryable: repeating the same call with the same credentials cannot help, and
    the escalation path is to the next provider, not to try again.
    """

    retryable = False
    category = "configuration"


class LLMResponseError(LLMError):
    """A 2xx response whose content is unusable, or a provider-side refusal."""

    retryable = False
    category = "invalid_response"


@runtime_checkable
class LLMProvider(Protocol):
    """What the orchestrator needs. Three attributes and one call.

    `name` and `available` let the registry skip a provider that has no credentials
    without importing its module or catching an exception.
    """

    name: str

    @property
    def available(self) -> bool: ...

    async def generate_structured(self, request: AIRequest, *, model: str) -> LLMResult: ...

    async def aclose(self) -> None: ...


__all__ = [
    "LLMConfigurationError",
    "LLMError",
    "LLMProvider",
    "LLMResponseError",
    "LLMTransientError",
]
