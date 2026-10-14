"""Tenant endpoints.

`/tenants` and `/tenant/context` are deliberately user-scoped: a user must be able
to discover their own memberships before any tenant is chosen. `/tenant/members` is
tenant-scoped and role-gated, so it is the live proof that RLS and role checks work
together.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import (
    CurrentPrincipal,
    CurrentUserSession,
    SettingsDep,
    TenantCtx,
    require_minimum_role,
)
from app.core.auth import TenantContext
from app.core.errors import ApiError, ErrorCode, error_responses
from app.models.membership import Role
from app.models.meta_connection import MetaConnectionStatus
from app.repositories import meta_connections as connection_repo
from app.repositories import tenants as tenant_repo
from app.schemas.tenant import (
    TenantBrief,
    TenantContextResponse,
    TenantMemberResponse,
    TenantSummary,
)
from app.schemas.tenant_settings import (
    AIConfigResponse,
    AIConfigUpdate,
    BusinessProfileResponse,
    BusinessProfileUpdate,
    ControlPlaneState,
    FollowUpConfigResponse,
    FollowUpConfigUpdate,
    FollowUpStepConfig,
    MemberCreateRequest,
    TenantMemberCreated,
    WhatsAppSetupResponse,
)
from app.services import meta_whatsapp
from app.services.follow_up_templates import load_sequence
from app.services.tenant_settings import (
    TenantProfile,
    ai_status,
    follow_up_status,
    load_profile,
    load_stored_follow_up_config,
)

router = APIRouter(tags=["tenants"])

AdminCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.ADMIN))]


async def tenant_profile(session: CurrentUserSession, context: AdminCtx) -> TenantProfile:
    """The tenant's own control-plane row, loaded once per request.

    A dependency rather than a body of duplicated `select(Tenant)` calls, so the answer to
    "what has this workspace configured" cannot differ between two endpoints. Returns 404
    rather than an empty object if the tenant vanished between auth and here, which is the
    only honest response - a settings screen showing blanks for a workspace that does not
    exist would be worse.
    """
    profile = await load_profile(session, context.tenant_id)
    if profile is None:
        raise ApiError(404, ErrorCode.TENANT_NOT_FOUND, "This workspace no longer exists.")
    return profile


async def tenant_follow_up_config(session: CurrentUserSession, context: AdminCtx) -> dict:
    """The raw stored follow-up document, for the config endpoints to validate and render.

    Deliberately separate from `tenant_profile`: this is a mutable document whose shape the
    service validates, and folding it into the profile would mean every profile read had to
    tolerate a malformed config.
    """
    return await load_stored_follow_up_config(session, context.tenant_id)


def config_from_request(steps) -> dict:
    """Turn validated request steps into the stored JSONB shape.

    Keys are strings of the step number, matching the existing Phase 7 document exactly -
    `load_sequence` parses `int(key)`, and reusing that shape is what keeps this a write on
    an existing column rather than a format change the engine would have to learn.
    """
    if not steps:
        # An empty sequence is refused here rather than by the field constraints, because
        # "no steps" has a domain meaning - a workspace that follows nobody up - and the
        # tenant expresses that by switching follow-ups off, not by sending no steps.
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "A follow-up sequence needs at least one step. "
            "Switch follow-ups off instead if you do not want any.",
        )
    seen: set[int] = set()
    for step in steps:
        if step.step in seen:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                f"Step {step.step} appears more than once.",
            )
        seen.add(step.step)
    stored = {
        str(step.step): {
            "delay_hours": step.delay_hours,
            "template": step.template,
            "ai_enhance": step.ai_enhance,
        }
        for step in steps
    }
    # Re-validate through the same authority the engine uses, so a value that reaches
    # `scheduled_tasks` can never be one the API accepted but `load_sequence` would reject.
    #
    # The try/except is load-bearing rather than defensive: `load_sequence` is where the
    # template-marker rules live, so without it a `{{ ... }}` template became a 500
    # "internal server error" instead of a 422 naming the field. Validation errors must reach
    # the tenant as validation errors.
    try:
        load_sequence(stored)
    except ValueError as exc:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            str(exc),
        ) from None
    return stored


@router.get(
    "/tenants",
    response_model=list[TenantSummary],
    summary="Tenants the authenticated user belongs to, with the role held in each.",
    responses=error_responses(401),
)
async def my_tenants(
    session: CurrentUserSession, principal: CurrentPrincipal
) -> list[TenantSummary]:
    memberships = await tenant_repo.list_memberships_for_user(session, principal.user_id)
    return [
        TenantSummary(id=m.tenant.id, name=m.tenant.name, slug=m.tenant.slug, role=m.role)
        for m in memberships
    ]


@router.get(
    "/tenant/context",
    response_model=TenantContextResponse,
    summary="The tenant this request operates in, resolved from the X-Tenant-ID header.",
    responses=error_responses(400, 401, 403),
)
async def tenant_context(context: TenantCtx) -> TenantContextResponse:
    return TenantContextResponse(
        tenant=TenantBrief(
            id=context.tenant_id, name=context.tenant_name, slug=context.tenant_slug
        ),
        role=context.role,
    )


@router.get(
    "/tenant/members",
    response_model=list[TenantMemberResponse],
    summary="Members of the current tenant. Requires ADMIN or OWNER.",
    responses=error_responses(401, 403),
)
async def tenant_members(
    session: CurrentUserSession,
    _context: Annotated[TenantContext, Depends(require_minimum_role(Role.ADMIN))],
) -> list[TenantMemberResponse]:
    # No `.where(tenant_id == ...)` on purpose: RLS is what scopes this query.
    memberships = await tenant_repo.list_tenant_members(session)
    return [
        TenantMemberResponse(
            membership_id=m.id,
            user_id=m.user_id,
            email=m.user.email,
            name=m.user.name,
            role=m.role,
            is_active=m.user.is_active,
        )
        for m in memberships
    ]


# -- Phase 10-O.1: the tenant control plane -----------------------------------
#
# Everything below is configuration the tenant owns about itself. Before this phase it
# existed only as columns with no writer, so a new workspace could configure nothing without
# a database edit.
#
# All of it is `AdminCtx`, matching every other tenant-scoped mutation here: this is
# workspace configuration, and the audit found no case where a MEMBER should change it.


@router.get(
    "/tenant/profile",
    response_model=BusinessProfileResponse,
    summary="The business identity the AI is told about.",
    responses=error_responses(401, 403),
)
async def business_profile(
    _context: AdminCtx,
    profile: Annotated[TenantProfile, Depends(tenant_profile)],
) -> BusinessProfileResponse:
    return _profile_response(profile)


@router.put(
    "/tenant/profile",
    response_model=BusinessProfileResponse,
    summary="Update the business identity the AI is told about.",
    responses=error_responses(401, 403, 422),
)
async def update_business_profile(
    payload: BusinessProfileUpdate,
    session: CurrentUserSession,
    context: AdminCtx,
    profile: Annotated[TenantProfile, Depends(tenant_profile)],
) -> BusinessProfileResponse:
    """Partial update: an omitted field is left as it is.

    The alternative - nulling anything not supplied - would make it impossible to clear one
    field without having to resend the other, and "clear my description" is a real action.
    """
    changes = payload.model_dump(exclude_unset=True)
    await tenant_repo.update_profile(
        session,
        tenant_id=context.tenant_id,
        **changes,
    )
    updated = await load_profile(session, context.tenant_id)
    return _profile_response(updated or profile)


@router.get(
    "/tenant/ai",
    response_model=AIConfigResponse,
    summary="This tenant's AI state, and whether the deployment can honour it.",
    responses=error_responses(401, 403),
)
async def ai_config(
    settings: SettingsDep,
    profile: Annotated[TenantProfile, Depends(tenant_profile)],
) -> AIConfigResponse:
    return AIConfigResponse(**ai_status(settings, tenant_ai_enabled=profile.ai_enabled))


@router.put(
    "/tenant/ai",
    response_model=AIConfigResponse,
    summary="Turn this tenant's AI replies on or off.",
    responses=error_responses(401, 403),
)
async def update_ai_config(
    payload: AIConfigUpdate,
    session: CurrentUserSession,
    context: AdminCtx,
    settings: SettingsDep,
    profile: Annotated[TenantProfile, Depends(tenant_profile)],
) -> AIConfigResponse:
    """Record the tenant's preference.

    This never reports `active: true` on a deployment with no provider. A tenant is allowed
    to switch AI on for a deployment that is not ready - they are configuring their intent,
    and the platform flag is not theirs to change - but the response says plainly that
    nothing will happen yet, so nobody discovers it by watching leads go unanswered.
    """
    await tenant_repo.update_profile(
        session, tenant_id=context.tenant_id, ai_enabled=payload.enabled
    )
    del profile  # only needed to guarantee the tenant exists; the read above does that
    return AIConfigResponse(**ai_status(settings, tenant_ai_enabled=payload.enabled))


@router.get(
    "/tenant/follow-ups",
    response_model=FollowUpConfigResponse,
    summary="This tenant's follow-up sequence and state.",
    responses=error_responses(401, 403),
)
async def follow_up_config(
    settings: SettingsDep,
    profile: Annotated[TenantProfile, Depends(tenant_profile)],
    config: Annotated[dict, Depends(tenant_follow_up_config)],
) -> FollowUpConfigResponse:
    return _follow_up_response(profile, config, settings)


@router.put(
    "/tenant/follow-ups",
    response_model=FollowUpConfigResponse,
    summary="Replace this tenant's follow-up sequence.",
    responses=error_responses(401, 403, 422),
)
async def update_follow_up_config(
    payload: FollowUpConfigUpdate,
    session: CurrentUserSession,
    context: AdminCtx,
    settings: SettingsDep,
    profile: Annotated[TenantProfile, Depends(tenant_profile)],
) -> FollowUpConfigResponse:
    reset = payload.reset
    if reset and payload.steps is not None:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            "Send either steps or reset, not both.",
        )
    if reset:
        # `{}` is already the safe default: `load_sequence({})` yields the built-in sequence,
        # so restoring it needs no second copy of the defaults to drift out of step.
        stored: dict | None = {}
    else:
        stored = config_from_request(payload.steps) if payload.steps is not None else None
    changes: dict[str, object] = {}
    if payload.enabled is not None:
        changes["follow_up_enabled"] = payload.enabled
    if stored is not None:
        changes["follow_up_config"] = stored
    await tenant_repo.update_profile(session, tenant_id=context.tenant_id, **changes)

    fresh = await load_profile(session, context.tenant_id)
    config = await load_stored_follow_up_config(session, context.tenant_id)
    return _follow_up_response(fresh or profile, config, settings)


@router.get(
    "/tenant/whatsapp",
    response_model=WhatsAppSetupResponse,
    summary="This tenant's WhatsApp setup state.",
    responses=error_responses(401, 403),
)
async def read_whatsapp_setup(
    session: CurrentUserSession,
    context: AdminCtx,
    settings: SettingsDep,
) -> WhatsAppSetupResponse:
    """What can honestly be said about WhatsApp for this tenant, right now.

    Reported from the tenant's Meta connections rather than from a tenant column, because
    WhatsApp is a property of the Meta connection, not of the tenant - and because a
    tenant with two connections should see the union rather than a cached verdict.

    Four states, not a boolean. `configured` means the tenant finished setup; `ready`
    additionally means this deployment could actually attempt a send. The difference is the
    whole point: a tenant told "WhatsApp is on" who then watches nothing arrive has been
    lied to, and the honest version of that answer costs one enum value.

    **This endpoint never claims a message was delivered.** It reports stored,
    Meta-confirmed identifiers and whether a credential exists.
    """
    connections = await connection_repo.list_connections(session)
    # Only a connection that can actually be called is worth reporting a verdict from, so a
    # disconnected row is never chosen. Before this, a tenant who disconnected and
    # reconnected was shown the *old* connection's state - which says not_connected, because
    # the connection they just made is not the one being asked about.
    usable = [c for c in connections if c.status is MetaConnectionStatus.ACTIVE]
    # Prefer one that is actually configured, so a tenant with one stale and one live
    # connection is not shown the stale one.
    chosen = next(
        (c for c in usable if c.whatsapp_phone_number_id is not None),
        usable[0] if usable else None,
    )
    state = meta_whatsapp.setup_state(chosen, settings)
    return WhatsAppSetupResponse(
        **state,
        meta_connection_id=str(chosen.id) if chosen is not None else None,
    )


# -- team onboarding ----------------------------------------------------------


@router.post(
    "/tenant/members",
    response_model=TenantMemberCreated,
    status_code=201,
    summary="Onboard someone into this workspace. Requires ADMIN or OWNER.",
    responses=error_responses(401, 403, 409, 422),
)
async def create_tenant_member(
    payload: MemberCreateRequest,
    session: CurrentUserSession,
    context: AdminCtx,
    settings: SettingsDep,
) -> TenantMemberCreated:
    """Create a user and their membership in one transaction.

    **Why the server generates the password.** This codebase has no email delivery, so
    there is nothing to deliver an invitation with. Rather than add a mail subsystem or an
    unauthenticated token endpoint - both larger than the feature, and the second a new
    public surface - the admin gets a single-use password returned exactly once, which they
    must pass on out of band. The new member then changes it.

    The limitation is real and deliberate: an admin who loses it has to reset the member
    rather than re-invite, and there is no expiry on an unaccepted account. Both are
    recorded in the API docs rather than papered over.
    """
    try:
        created = await tenant_repo.create_member(
            session,
            settings=settings,
            tenant_id=context.tenant_id,
            email=payload.email,
            name=payload.name,
            role=Role(payload.role),
        )
    except tenant_repo.MemberAlreadyExists:
        # 409, not 422: the request was well formed, it just conflicts with something that
        # already exists. Same code the registration path uses for the same situation.
        raise ApiError(
            409,
            ErrorCode.EMAIL_TAKEN,
            "A user with that email already exists.",
        ) from None
    return TenantMemberCreated(
        user_id=created.user_id,
        email=created.email,
        name=created.name,
        role=created.role.value,
        one_time_password=created.one_time_password,
    )


def _profile_response(profile: TenantProfile) -> BusinessProfileResponse:
    description = profile.description
    return BusinessProfileResponse(
        workspace_name=profile.name,
        business_name=profile.business_name,
        business_description=profile.business_description,
        resolved_name=profile.display_name,
        has_description=description is not None,
    )


def _follow_up_response(
    profile: TenantProfile, config: dict | None, settings
) -> FollowUpConfigResponse:
    sequence = load_sequence(config)
    return FollowUpConfigResponse(
        enabled=profile.follow_up_enabled,
        state=ControlPlaneState(
            **follow_up_status(settings, tenant_follow_up_enabled=profile.follow_up_enabled)
        ),
        steps=[
            FollowUpStepConfig(
                step=number,
                delay_hours=step.delay_hours,
                template=step.template,
                ai_enhance=step.ai_enhance,
            )
            for number, step in sorted(sequence.steps.items())
        ],
        is_default=sequence.is_default,
        ghosted_after_days=settings.follow_up_ghosted_days,
    )
