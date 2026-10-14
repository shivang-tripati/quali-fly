"""The AI contract: what we ask for, and what we will accept back.

Everything in this module treats model output as **untrusted input**. A language model
can be talked into emitting a 900, a negative score, a 40KB wall of text, or JSON with
fields we never asked for. So the shape is validated twice and neither pass trusts the
model:

    provider text
        ↓ json.loads            (syntax)
        ↓ QualificationUpdate   (types, ranges, required fields, extra="forbid")
        ↓ _bounded_response     (business rules: length, emptiness)
        ↓ AIResponse            (the only thing the pipeline will ever act on)

Pydantic is doing the real work here, not decoration. `extra="forbid"` means a model
cannot smuggle an unexpected field through; `ge=0, le=25` means a hallucinated score is
rejected rather than clamped, because a clamped score is a *lie* about what the model
said. Rejection is the honest outcome, and the caller turns it into a failed run.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The PRD's four dimensions, 0-25 each, 100 total.
DIMENSION_MAX = 25
QUALIFICATION_DIMENSIONS = ("budget", "timeline", "fit", "authority")


class QualificationUpdate(BaseModel):
    """One dimension set, each 0-25 inclusive.

    Required, not defaulted. A missing dimension is a malformed answer, not a zero: if the
    model could not assess budget we want to know that rather than silently scoring 0 and
    lowering a total.
    """

    model_config = ConfigDict(extra="forbid")

    budget: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    timeline: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    fit: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    authority: int = Field(ge=0, le=DIMENSION_MAX, strict=True)

    @property
    def total(self) -> int:
        return sum(getattr(self, name) for name in QUALIFICATION_DIMENSIONS)


class AIResponse(BaseModel):
    """The PRD's structured result, validated.

    The four scores are **flat on the wire**, not nested under a `qualification_update`
    object. That is not a style preference - it is what the two providers actually handle
    best. Gemini's `responseSchema` subset is flattest for flat scalar fields, and JSON
    Object Mode on Groq is more reliable when the model is not asked to nest. Nesting
    would have been easier to read here and weaker in practice at every layer downstream.

    `qualification` exposes the nested view for callers that want the dimensions grouped,
    and it is built through the same `QualificationUpdate` validation rather than by hand,
    so there is exactly one place where a score's range is enforced.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)
    budget: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    timeline: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    fit: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    authority: int = Field(ge=0, le=DIMENSION_MAX, strict=True)
    human_handoff: bool = Field(strict=True)
    # Omitted by many models, hence the default. Present-but-blank is normalised to None
    # so we never persist a reason that is an empty string.
    handoff_reason: str | None = Field(default=None, max_length=500)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()

    @field_validator("handoff_reason")
    @classmethod
    def _reason_only_when_handoff(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _build_qualification(self) -> "AIResponse":
        # Validated here, once, at parse time. A caller that reads `.qualification`
        # downstream can rely on the ranges having been enforced, and cannot accidentally
        # skip it by reading the flat fields directly.
        object.__setattr__(self, "_qualification", self._validated_qualification())
        return self

    def _validated_qualification(self) -> QualificationUpdate:
        return QualificationUpdate(
            budget=self.budget,
            timeline=self.timeline,
            fit=self.fit,
            authority=self.authority,
        )

    @property
    def qualification(self) -> QualificationUpdate:
        return object.__getattribute__(self, "_qualification")

    def handoff_requested(self) -> bool:
        return self.human_handoff


class AIRequest(BaseModel):
    """What we hand a provider. Bounded, so a prompt cannot grow without limit."""

    model_config = ConfigDict(extra="forbid")

    system_prompt: str = Field(min_length=1)
    # Already-flattened, injection-delimited user content. Kept as one string per
    # provider because the two APIs disagree about message arrays.
    user_prompt: str = Field(min_length=1)


class LLMResult(BaseModel):
    """A provider-neutral success.

    Usage is normalized here so the orchestrator never learns that Gemini calls it
    `candidatesTokenCount` and Groq calls it `completion_tokens`. `None` means the
    provider did not report it - never a guess, because a fabricated token count is
    indistinguishable from a real one once it is stored.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = Field(ge=0)


class AIProcessingResult(BaseModel):
    """The deterministic outcome of processing one inbound message.

    Returned to the caller (a worker, a test, an operator) and logged with structured
    fields. It is deliberately a value, not a side effect report assembled from logs.
    """

    model_config = ConfigDict(extra="forbid")

    status: str
    reason: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    qualification_total: int | None = None
    high_intent: bool = False
    human_handoff_required: bool = False
    handoff_reason: str | None = None
    response_sent: bool = False
    outbound_message_id: str | None = None
    lead_id: str | None = None


# Statuses for AIProcessingResult.status, and for the persisted run row.
PROCESSED = "PROCESSED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
IN_PROGRESS = "IN_PROGRESS"


__all__ = [
    "AIProcessingResult",
    "AIRequest",
    "AIResponse",
    "DIMENSION_MAX",
    "FAILED",
    "IN_PROGRESS",
    "LLMResult",
    "PROCESSED",
    "QUALIFICATION_DIMENSIONS",
    "QualificationUpdate",
    "SKIPPED",
]
