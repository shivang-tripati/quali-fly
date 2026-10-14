"""Auth request/response schemas. `password_hash` is never part of any response."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.models.membership import Role
from app.models.types import EMAIL_PATTERN
from app.schemas.tenant import TenantBrief


class LoginRequest(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    # min_length=1, not the registration floor: an account set before the floor existed
    # must still be able to sign in. See `MIN_PASSWORD_LENGTH`.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RegisterRequest(BaseModel):
    """Everything a new customer has to supply, and nothing they could choose.

    `tenant_id`, `user_id` and `role` are absent by design. The server derives all three, and
    `extra="forbid"` turns an attempt to smuggle one in into a 422 rather than a silently
    ignored field - which would look like it worked.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    workspace_name: str = Field(min_length=1, max_length=200)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    is_active: bool


class SessionResponse(BaseModel):
    user: UserResponse


class RegisterResponse(SessionResponse):
    """The workspace the customer just created, so the client does not have to guess it.

    `role` is echoed because the onboarding screen needs to know whether to offer the
    admin-only connection step. It is read back from the membership the server created,
    never from the request.
    """

    workspace: TenantBrief
    role: Role


class MembershipSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Role
