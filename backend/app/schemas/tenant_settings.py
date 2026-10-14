"""Tenant-facing control-plane schemas.

Everything here is a *view* of the tenant's own configuration. None of it exposes a
provider key, a token, a Meta app id, or any other deployment secret - the whole surface is
tenant-scoped booleans and business text, and the platform capability it is combined with is
reported as a reason string rather than as configuration.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

#: Long enough for a real description of what a business sells, short enough that the
#: rendered prompt stays inside `AI_MAX_PROMPT_CHARS` without a second truncation pass.
MAX_BUSINESS_DESCRIPTION = 4000


class BusinessProfileResponse(BaseModel):
    """What the AI knows about this business.

    `resolved_name` is what the model will actually be told, after the fallback to the
    workspace name. Exposing it separately from the raw field means the settings screen can
    show a tenant exactly what their AI is being told, rather than making them infer it.
    """

    model_config = ConfigDict(from_attributes=True)

    workspace_name: str
    business_name: str | None = None
    business_description: str | None = None
    resolved_name: str
    #: False when neither the tenant nor the workspace has supplied a description, which is
    #: the only state in which the prompt uses its "ask them" fallback.
    has_description: bool


class BusinessProfileUpdate(BaseModel):
    """A partial update. Unset fields are left alone rather than nulled.

    `extra="forbid"` so a field the API does not support is a 422 rather than a silently
    ignored typo - the same reasoning as `RegisterRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    business_name: str | None = Field(default=None, max_length=200)
    business_description: str | None = Field(default=None, max_length=MAX_BUSINESS_DESCRIPTION)


class ControlPlaneState(BaseModel):
    """The honest combined state of one capability.

    `state` is the field a UI should branch on. `platform_available` and `tenant_enabled`
    are both present so a screen can explain *which* half is the problem - "your deployment
    has no AI provider configured" and "you switched AI off" need different sentences and
    different next steps.
    """

    state: str
    active: bool
    tenant_enabled: bool
    platform_available: bool
    unavailable_reason: str | None = None


class AIConfigResponse(ControlPlaneState):
    """Tenant AI configuration. Deliberately no provider, model or key fields.

    Those are deployment properties. A tenant cannot choose them, so returning them would
    imply a control that does not exist.
    """


class AIConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class FollowUpStepConfig(BaseModel):
    """One step, as a tenant configures it.

    Mirrors `follow_up_templates.TemplateStep`, which is the authority that validates and
    renders it. This model exists so an invalid value is rejected with a field-level 422 at
    the API boundary rather than as a ValueError raised later inside the scheduler.
    """

    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=1, le=20)
    delay_hours: int = Field(ge=1, le=24 * 30)
    template: str = Field(min_length=1, max_length=1000)
    ai_enhance: bool = True


class FollowUpConfigResponse(BaseModel):
    enabled: bool
    state: ControlPlaneState
    steps: list[FollowUpStepConfig]
    #: True when the tenant has never customised anything and these are the built-in
    #: defaults. Shown so an operator can tell "these are our defaults" from "someone typed
    #: these in".
    is_default: bool
    ghosted_after_days: int


class FollowUpConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    #: An empty list is rejected by the service rather than here, because "no steps" is a
    #: domain error with a domain explanation.
    steps: list[FollowUpStepConfig] | None = None
    #: Put the built-in sequence back. Explicit rather than reading `steps: []` as "defaults",
    #: because that same empty list is refused as a save, so one value would mean two things.
    reset: bool = False


class WhatsAppSetupResponse(BaseModel):
    """This tenant's WhatsApp setup state, honestly.

    `ready` means a number is chosen, Meta has confirmed it, and this deployment has a
    credential to send with. `configured` means the first two but not the third. Nothing in
    this response asserts that a message was ever delivered.

    `meta_connection_id` is which of the tenant's connections the verdict came from, so a
    tenant with more than one can tell which Page/Account it is about.
    """

    state: str = Field(
        description="not_connected | awaiting_account | awaiting_number | configured | ready",
    )
    ready: bool
    whatsapp_business_account_id: str | None = None
    phone_number_id: str | None = None
    display_number: str | None = None
    platform_available: bool
    unavailable_reason: str | None = None
    meta_connection_id: str | None = None


class MemberCreateRequest(BaseModel):
    """Onboard someone into this workspace.

    No password field on purpose. The server generates a single-use random one and returns
    it once, so an admin cannot choose something weak or reuse their own, and no credential
    is ever stored in transit or logged. See `TenantMemberCreated`.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern=r"^(OWNER|ADMIN|MEMBER)$")


class TenantMemberCreated(BaseModel):
    """The one and only time the generated password is visible.

    `one_time_password` exists because this codebase has no email delivery, and inventing a
    mail subsystem for one flow would be a larger change than the feature. The limitation is
    real and is stated in the field's own docstring in the API module: an admin must pass
    this to the new member out of band, and the member must change it.
    """

    user_id: uuid.UUID
    email: str
    name: str
    role: str
    one_time_password: str


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


__all__ = [
    "AIConfigResponse",
    "AIConfigUpdate",
    "BusinessProfileResponse",
    "BusinessProfileUpdate",
    "ControlPlaneState",
    "FollowUpConfigResponse",
    "FollowUpConfigUpdate",
    "FollowUpStepConfig",
    "MAX_BUSINESS_DESCRIPTION",
    "MemberCreateRequest",
    "PasswordChangeRequest",
    "TenantMemberCreated",
]
