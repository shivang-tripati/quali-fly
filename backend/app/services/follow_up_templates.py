"""Tenant follow-up templates: validation and rendering.

Two things this module exists to guarantee, and both are security properties rather than
conveniences.

**Rendering cannot execute anything.** It is a `str.replace` over a fixed set of keys. Not
Jinja, not `str.format`, not `eval`, not regex substitution with a callable. A tenant
administers these strings, and a templating engine would turn a `{{ config.__class__ }}`
into a credential leak. The whole attack surface is "which placeholders are in my
dictionary".

**An unknown placeholder degrades, it does not explode.** A template mentioning
`{discount_code}` that Phase 7 does not support renders without it and logs a warning.
Failing the send would drop a follow-up over a cosmetic problem; silently sending the raw
`{discount_code}` to a lead would look broken to a customer. Removing it and carrying on is
the only option that is neither.

Placeholders supported - the complete list, and it is short on purpose:

    {first_name}   the lead's first name, or "" when unknown

The PRD mentions `{value_proposition}` and `{pain_point}`. They are *not* implemented,
because nothing in the existing lead, conversation or knowledge model can determine them
deterministically - inventing a value proposition is exactly the "never invent business
facts" rule the AI prompt already enforces. An operator who wants one in a message writes
it into the template text, where a human chose it.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("app.follow_up.templates")

#: The complete set of supported placeholders. Adding a key here is a deliberate act with a
#: deterministic source, not a way to expose model internals.
ALLOWED_PLACEHOLDERS = frozenset({"first_name"})

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: The PRD's business sequence. Stored in the database, not in code, so a tenant can adjust
#: it; this is the fallback for `{}`.
DEFAULT_STEPS: dict[int, dict[str, Any]] = {
    1: {
        "delay_hours": 4,
        "ai_enhance": True,
        "template": (
            "Hi {first_name}, just checking in - did you get a chance to think about what "
            "we discussed? Happy to answer any questions."
        ),
    },
    2: {
        "delay_hours": 24,
        "ai_enhance": True,
        "template": (
            "Hi {first_name}, I wanted to follow up in case anything came up on your end. "
            "Is there anything specific I can help with?"
        ),
    },
    3: {
        "delay_hours": 72,
        "ai_enhance": True,
        "template": (
            "Hi {first_name}, I don't want to keep filling your inbox - but if this is "
            "still something you're considering, I'd be glad to help. Otherwise no "
            "worries at all."
        ),
    },
}


class TemplateStep(BaseModel):
    """One follow-up step, as a tenant configures it."""

    model_config = ConfigDict(extra="forbid")

    delay_hours: int = Field(ge=1, le=24 * 30)
    template: str = Field(min_length=1, max_length=1000)
    ai_enhance: bool = True

    @field_validator("template")
    @classmethod
    def _reject_executable_markers(cls, value: str) -> str:
        """Refuse template-engine and expression syntax outright.

        Not because this renderer would execute it - it would not - but because a template
        containing `{{ ... }}` or `${ ... }` is either a mistake or an attempt, and both
        deserve a loud rejection at configuration time rather than a confusing render later.
        """
        for marker in ("{{", "}}", "${", "%}", "<%", "{%"):
            if marker in value:
                raise ValueError(
                    f"template must not contain '{marker}': only "
                    f"{{{', '.join(sorted(ALLOWED_PLACEHOLDERS))}}} are supported"
                )
        return value


@dataclass(frozen=True)
class FollowUpSequence:
    """A validated, resolved follow-up sequence for one tenant."""

    steps: dict[int, TemplateStep] = field(default_factory=dict)
    #: True when the tenant supplied no config and the built-in defaults are in use. Worth
    #: knowing: a tenant that never configured anything is a tenant nobody has looked at.
    is_default: bool = True

    def step(self, sequence_step: int) -> TemplateStep | None:
        return self.steps.get(sequence_step)

    def has_step(self, sequence_step: int) -> bool:
        return sequence_step in self.steps

    @property
    def final_step(self) -> int:
        return max(self.steps) if self.steps else 0


def load_sequence(config: dict[str, Any] | None) -> FollowUpSequence:
    """Validate a tenant's stored config into a usable sequence.

    A tenant with no config gets the PRD defaults. A tenant with *partly* invalid config
    gets an error rather than a partially-applied sequence - a half-configured follow-up
    where step 2 is silently the default and step 3 is the tenant's is worse than either
    extreme, because the tenant cannot tell what is live.
    """
    if not config:
        return FollowUpSequence(
            steps={n: TemplateStep(**v) for n, v in DEFAULT_STEPS.items()},
            is_default=True,
        )
    steps: dict[int, TemplateStep] = {}
    for key, value in config.items():
        try:
            number = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"follow-up config key {key!r} is not a step number") from exc
        if not isinstance(value, dict):
            raise ValueError(f"follow-up step {number} must be an object")
        steps[number] = TemplateStep(**value)
    if not steps:
        raise ValueError("follow-up config defines no steps")
    return FollowUpSequence(steps=steps, is_default=False)


def render(template: str, *, first_name: str | None) -> tuple[str, list[str]]:
    """Substitute the allowed placeholders. Returns `(text, unknown_placeholder_names)`.

    A `str.replace` loop, deliberately. The set of keys is closed and known here, so there
    is no path from a tenant's string to evaluation of anything.
    """
    values = {"first_name": (first_name or "").strip()}
    unknown: list[str] = []

    def _collect(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in ALLOWED_PLACEHOLDERS:
            return values[name]
        if name not in unknown:
            unknown.append(name)
        # Removed rather than left in place: sending a lead a literal `{discount_code}`
        # reads as a broken product, whereas its absence just reads as a shorter message.
        return ""

    rendered = _PLACEHOLDER.sub(_collect, template)
    rendered = re.sub(r"\s{2,}", " ", rendered).strip()
    if unknown:
        log.warning(
            "follow_up.template.unknown_placeholder",
            extra={"placeholders": ",".join(sorted(unknown))},
        )
    return rendered, unknown


def first_name_from(full_name: str | None) -> str | None:
    """Best-effort first name. `None` in, `None` out - never a placeholder or a guess."""
    if not full_name:
        return None
    first = full_name.strip().split()[0] if full_name.strip() else ""
    return first or None


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "DEFAULT_STEPS",
    "FollowUpSequence",
    "TemplateStep",
    "first_name_from",
    "load_sequence",
    "render",
]
