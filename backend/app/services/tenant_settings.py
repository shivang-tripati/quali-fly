"""Tenant control plane: the configuration a workspace owns about itself.

Phase 10-O.0 found that a brand-new tenant could not configure anything. This module is the
one place that state is read from and written to, so there is exactly one answer to "what has
this tenant turned on, and what is this business" rather than a definition per call site.

Two ideas carry most of the design:

**Deployment capability and tenant preference are different things, and are never merged.**
A tenant that has AI switched on, on a deployment with no provider credentials, does not
have AI. Reporting that as "enabled" would be a lie the tenant discovers by watching leads
go unanswered, so `ai_status` keeps the two apart and returns both. Every consumer of the AI
flag has to satisfy *both* halves.

**One read, used by many.** The profile is loaded once per request by whoever needs it and
passed down, rather than each service re-querying `tenants`. It is four scalars on the
isolation root that every tenant-scoped request already touches.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant


@dataclass(frozen=True, slots=True)
class TenantProfile:
    """The tenant-owned control-plane state, as one value.

    `business_name` is already resolved here rather than at each call site: `None` on the
    column means "the tenant never set one", and the correct answer for a prompt is the
    tenant's internal `name`. Resolving it once means the API, the prompt and the UI can
    never disagree about what the business is called.
    """

    tenant_id: uuid.UUID
    name: str
    business_name: str | None
    business_description: str | None
    ai_enabled: bool
    follow_up_enabled: bool

    @property
    def display_name(self) -> str:
        """What the business is called, preferring the tenant's own wording."""
        return (self.business_name or self.name).strip()

    @property
    def description(self) -> str | None:
        """What the business does, or `None` when the tenant genuinely has not said.

        Empty and whitespace-only collapse to `None` so the AI context takes its honest
        "not configured" branch. Storing `""` and treating it as an answer would put a blank
        claim in front of a model, which reads as "this business does nothing".
        """
        if self.business_description is None:
            return None
        cleaned = self.business_description.strip()
        return cleaned or None


EMPTY = TenantProfile(
    tenant_id=uuid.UUID(int=0),
    name="",
    business_name=None,
    business_description=None,
    ai_enabled=False,
    follow_up_enabled=False,
)


async def load_profile(session: AsyncSession, tenant_id: uuid.UUID) -> TenantProfile | None:
    """Read one tenant's control-plane state. `None` if the tenant does not exist.

    `tenants` is the isolation root and has no RLS, so this is a plain indexed lookup by
    primary key. It does not use `set_current_tenant` because that context governs
    tenant-*owned* tables, and this is the table they are keyed on.
    """
    row = (
        await session.execute(
            select(
                Tenant.id,
                Tenant.name,
                Tenant.business_name,
                Tenant.business_description,
                Tenant.ai_enabled,
                Tenant.follow_up_enabled,
            ).where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return TenantProfile(
        tenant_id=row.id,
        name=row.name,
        business_name=row.business_name,
        business_description=row.business_description,
        ai_enabled=row.ai_enabled,
        follow_up_enabled=row.follow_up_enabled,
    )


async def load_stored_follow_up_config(session: AsyncSession, tenant_id: uuid.UUID) -> dict:
    """The raw follow-up document for a tenant, or `{}` meaning "use the defaults".

    Kept beside `load_profile` rather than in the repository because it reads the same row
    and both are "what has this tenant configured". It is deliberately *not* validated here:
    the config endpoints need the raw document in order to hand it to `load_sequence`, and a
    configuration read should report what is stored rather than raising on it.
    """
    from sqlalchemy import select as _select

    from app.models.tenant import Tenant as _Tenant

    config = (
        await session.execute(_select(_Tenant.follow_up_config).where(_Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return config or {}


#: Why the deployment cannot run AI, when it cannot. These strings are returned to the
#: tenant's own settings screen, so they are written for an operator rather than a developer.
AI_UNAVAILABLE_NO_PROVIDER = "no_ai_provider_configured"
AI_UNAVAILABLE_DISABLED = "ai_disabled_for_this_deployment"


def platform_ai_available(settings) -> tuple[bool, str | None]:
    """Can this deployment run AI at all? Independent of any tenant's preference.

    Three checks, because each is a different way to discover there is no working provider
    only when a real lead is waiting:

      * the master switch is off, so nothing would run;
      * the master switch is on but no provider key is present, so every call would fail
        authentication;
      * the only key present belongs to a provider the registry will not dial.

    Written as the registry's own construction rule - a provider joins the ladder when it has
    a key, and Gemini additionally has to be enabled - rather than reasoning about
    `ai_primary_provider`. That field only *reorders* a ladder, it never filters one, so
    reading it here would invent an availability the registry cannot honour: with
    `ai_primary_provider="gemini"`, a Gemini key, and `gemini_enabled=false`, the ladder is
    empty and every call raises `LLMConfigurationError`, while a primary-aware version of
    this function would report "available". One rule, in the place that enforces it.

    Returning a reason rather than a bare `False` is what lets the settings screen say "this
    deployment has no provider configured" instead of "you have AI switched off" - the two
    invite completely different responses from the person reading them.
    """
    if not settings.ai_enabled:
        return False, AI_UNAVAILABLE_DISABLED
    if settings.groq_api_key or (settings.gemini_enabled and settings.gemini_api_key):
        return True, None
    return False, AI_UNAVAILABLE_NO_PROVIDER


def ai_status(settings, *, tenant_ai_enabled: bool) -> dict:
    """The tri-state a client needs, with both halves kept distinct.

        unavailable  the deployment cannot run AI, whatever this tenant wants
        disabled     the deployment can, and this tenant has not opted in
        enabled      both halves are true

    `active` is the single field most callers want, and it is `available and tenant_enabled`
    rather than either alone - the point of the whole function.
    """
    available, reason = platform_ai_available(settings)
    if not available:
        state = "unavailable"
    elif not tenant_ai_enabled:
        state = "disabled"
    else:
        state = "enabled"
    return {
        "state": state,
        "active": available and tenant_ai_enabled,
        "tenant_enabled": tenant_ai_enabled,
        "platform_available": available,
        "unavailable_reason": reason,
    }


def follow_up_status(settings, *, tenant_follow_up_enabled: bool) -> dict:
    """The same shape as `ai_status`, for the follow-up engine.

    Follow-ups have no provider credential to be missing, so the only deployment-level
    condition is the master switch.
    """
    available = bool(settings.follow_up_enabled)
    if not available:
        state = "unavailable"
    elif not tenant_follow_up_enabled:
        state = "disabled"
    else:
        state = "enabled"
    return {
        "state": state,
        "active": available and tenant_follow_up_enabled,
        "tenant_enabled": tenant_follow_up_enabled,
        "platform_available": available,
        "unavailable_reason": None if available else "follow_ups_disabled_for_this_deployment",
    }


__all__ = [
    "AI_UNAVAILABLE_DISABLED",
    "AI_UNAVAILABLE_NO_PROVIDER",
    "TenantProfile",
    "ai_status",
    "follow_up_status",
    "load_profile",
    "load_stored_follow_up_config",
    "platform_ai_available",
]
