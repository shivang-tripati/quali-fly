"""Lead API schemas.

The Lead is the tenant's own data, so `field_data` is returned - the tenant supplied
the form and is entitled to the answers. RLS is what decides whose leads these are; no
field on a Lead is a credential.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.lead import LeadSource, LeadStatus


class LeadResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    meta_leadgen_id: str
    source: LeadSource
    status: LeadStatus
    name: str | None
    email: str | None
    phone: str | None
    form_id: str | None
    ad_id: str | None
    provider_created_at: datetime | None
    field_data: dict[str, Any] = Field(default_factory=dict)
    #: The AI's merged scores, so the lead screen can render bars rather than raw JSON.
    #: `None` until the AI has processed a message for this lead - which is itself
    #: meaningful, and the UI shows it as "not scored yet" rather than as four zeroes.
    qualification: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class LeadListResponse(BaseModel):
    items: list[LeadResponse]
    #: Rows on this page.
    count: int
    #: Rows matching the current filters across all pages. Needed for real pagination:
    #: `count` alone makes every page look like the last one.
    total: int | None = None
