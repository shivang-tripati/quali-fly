"""Tenant-facing schemas. Tenant ids and roles come from `TenantContext`, never
from the request payload."""

import uuid

from pydantic import BaseModel, ConfigDict

from app.models.membership import Role


class TenantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    role: Role


class TenantBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str


class TenantContextResponse(BaseModel):
    tenant: TenantBrief
    role: Role


class TenantMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    name: str
    role: Role
    is_active: bool
