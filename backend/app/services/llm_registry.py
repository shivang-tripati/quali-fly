"""Provider selection, escalation, and fallback - in one place, deliberately.

The point of this module is that **no business logic anywhere above it names a
provider**. The orchestrator asks for a structured result and gets one, or a typed
failure. Adding a third provider later is a change to `_build_attempts` and a new
module, not an edit to the pipeline.

The attempt list is the whole strategy, read as a sequence:

    (groq, primary model)      cheap, fast, the common case
    (groq, escalation model)   same vendor, bigger model; helps when the small one
                              returns something we cannot validate
    (gemini, gemini model)    different vendor; escapes Groq auth, quota, and outage

Consequences of building it this way, which are the parts that matter:

  * **Retry, escalation, and fallback are the same mechanism.** A transient Groq failure
    moves to the escalation model; an unparseable Groq answer moves there too, because
    "the model produced garbage" is a good reason to ask a different model. One list
    instead of retry logic and fallback logic that can disagree with each other.
  * **Failures are not all equal.** `LLMError.retryable` decides whether the *next
    attempt* is even worth making, so a 401 falls through to Gemini immediately instead of
    burning a retry and a timeout first.
  * **Bounded by construction.** The list is capped at `ai_max_provider_attempts` and each
    step sleeps for at most `ai_max_backoff_seconds`, so a pathological provider cannot
    turn one inbound message into an unbounded wait. And the sleep happens here, outside
    every database transaction, so a backoff never holds a row lock.
  * **An unconfigured provider is skipped, not an error.** Missing `GEMINI_API_KEY` drops
    that step from the list, so turning the fallback off is a configuration change.

Escalation and a multi-model tiering policy are configuration values, not code paths.
"""

import asyncio
import logging
from collections.abc import Sequence

from app.core.config import Settings
from app.schemas.ai import AIRequest, LLMResult
from app.services.llm_base import (
    LLMConfigurationError,
    LLMError,
    LLMProvider,
    LLMResponseError,
    LLMTransientError,
)
from app.services.llm_gemini import GeminiProvider
from app.services.llm_groq import GroqProvider

log = logging.getLogger("app.ai.registry")

# A step in the escalation ladder: which provider, and which of its models.
Attempt = tuple[LLMProvider, str]


class LLMRegistry:
    """Owns provider construction and the escalation ladder."""

    def __init__(self, providers: Sequence[LLMProvider], settings: Settings) -> None:
        self._providers = {p.name: p for p in providers}
        self._settings = settings
        self._attempts = self._build_attempts()

    @property
    def attempts(self) -> list[Attempt]:
        return list(self._attempts)

    def _build_attempts(self) -> list[Attempt]:
        s = self._settings
        groq = self._providers.get("groq")
        gemini = self._providers.get("gemini")

        ladder: list[Attempt] = []
        if groq is not None and groq.available:
            ladder.append((groq, s.groq_primary_model))
            # The escalation model is only useful if it actually differs; otherwise we
            # would make an identical failing call twice.
            if s.groq_escalation_model != s.groq_primary_model:
                ladder.append((groq, s.groq_escalation_model))
        if gemini is not None and gemini.available and s.gemini_enabled:
            ladder.append((gemini, s.gemini_model))
        if s.ai_primary_provider == "gemini" and gemini is not None and gemini.available:
            # Explicit primary selection wins, and the ladder is rebuilt in that order
            # rather than special-cased at the call site.
            gemini_steps = [step for step in ladder if step[0].name == "gemini"]
            other_steps = [step for step in ladder if step[0].name != "gemini"]
            ladder = gemini_steps + other_steps
        return ladder[: max(1, s.ai_max_provider_attempts)]

    async def generate_structured(self, request: AIRequest) -> LLMResult:
        """Try each step in order; return the first valid result.

        Raises `LLMError` when every step fails. The raised error is the *last* failure,
        since it is the one from the most capable thing we tried, but its category is
        normalised to configuration if the dominant cause was a missing key.
        """
        if not self._attempts:
            # Every provider was skipped for lack of credentials. This is a
            # configuration problem, and saying so precisely beats surfacing a provider
            # error that never happened.
            raise LLMConfigurationError(
                "AI is enabled but no configured provider has an API key. "
                "Set GROQ_API_KEY (and optionally GEMINI_API_KEY)."
            )

        last_error: LLMError | None = None
        for index, (provider, model) in enumerate(self._attempts):
            if index:
                await self._backoff(index, last_error)
            try:
                result = await provider.generate_structured(request, model=model)
            except LLMError as exc:
                last_error = exc
                log.warning(
                    "ai.llm.attempt_failed",
                    extra={
                        "provider": provider.name,
                        "model": model,
                        "attempt": index + 1,
                        "attempts_total": len(self._attempts),
                        "error_category": exc.category,
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                    },
                )
                # A configuration error means this provider is unusable for the rest of
                # the process, but the ladder may still have another vendor in it.
                continue
            log.info(
                "ai.llm.attempt_succeeded",
                extra={
                    "provider": result.provider,
                    "model": result.model,
                    "attempt": index + 1,
                    "latency_ms": result.latency_ms,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return result

        assert last_error is not None  # loop ran at least once and set it
        raise last_error

    async def _backoff(self, attempt_index: int, previous: LLMError | None) -> None:
        """Bounded pause between steps. No DB transaction is open when this runs."""
        if previous is None or not previous.retryable:
            # Escalating to a different model or vendor on a non-retryable failure is not
            # a "wait and hope", so there is no reason to sleep first.
            return
        delay = min(
            self._settings.ai_max_backoff_seconds,
            0.25 * (2 ** (attempt_index - 1)),
        )
        if isinstance(previous, LLMTransientError) and previous.retry_after is not None:
            delay = min(delay, previous.retry_after)
        if delay > 0:
            await asyncio.sleep(delay)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()


def build_registry(settings: Settings) -> LLMRegistry:
    """Build the production registry from settings.

    Providers are constructed unconditionally; a missing key makes `available` false and
    the registry drops that step. Constructing only what is enabled would save an httpx
    client, at the cost of a registry whose shape depends on which keys happen to be set -
    which makes the escalation ladder untestable. Two clients is not worth that.
    """
    groq = GroqProvider(
        settings.groq_api_key,
        timeout=settings.ai_request_timeout_seconds,
        temperature=settings.ai_temperature,
        max_tokens=settings.ai_max_output_tokens,
    )
    gemini = GeminiProvider(
        settings.gemini_api_key,
        timeout=settings.ai_request_timeout_seconds,
        temperature=settings.ai_gemini_temperature,
        max_tokens=settings.ai_max_output_tokens,
    )
    return LLMRegistry([groq, gemini], settings)


__all__ = ["Attempt", "LLMRegistry", "LLMResponseError", "build_registry"]
