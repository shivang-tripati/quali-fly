"""Tenant-scoped Lead access.

`insert_if_new` is the deduplication boundary, exactly as `insert_if_new` is for
webhook events in `meta_webhook_events`. The unique index on
`(tenant_id, meta_leadgen_id)` is the authority; a `SELECT`-then-`INSERT` would let two
concurrent processors - a retry and the original, say - both conclude the lead is new.

Every other function relies on RLS for scoping, so none of them filters on `tenant_id`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead, LeadSource, LeadStatus
from app.services.meta_leads import NormalizedLead


async def insert_if_new(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    meta_connection_id: uuid.UUID,
    lead: NormalizedLead,
) -> tuple[Lead, bool]:
    """Insert the lead unless this tenant already has it.

    Returns `(lead, created)`. `created=False` is the normal, expected outcome for a
    Meta retry or a manual replay - not an error.
    """
    statement = (
        insert(Lead)
        .values(
            tenant_id=tenant_id,
            meta_connection_id=meta_connection_id,
            meta_leadgen_id=lead.leadgen_id,
            source=LeadSource.META_LEAD_ADS,
            status=LeadStatus.NEW,
            name=lead.name,
            email=lead.email,
            phone=lead.phone,
            form_id=lead.form_id,
            ad_id=lead.ad_id,
            provider_created_at=lead.provider_created_at,
            field_data=lead.field_data,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "meta_leadgen_id"])
        .returning(Lead.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        return await get(session, inserted_id), True

    existing = await session.execute(
        select(Lead).where(
            Lead.tenant_id == tenant_id,
            Lead.meta_leadgen_id == lead.leadgen_id,
        )
    )
    return existing.scalar_one(), False


async def get(session: AsyncSession, lead_id: uuid.UUID) -> Lead | None:
    result = await session.execute(select(Lead).where(Lead.id == lead_id))
    return result.scalar_one_or_none()


async def get_by_leadgen_id(
    session: AsyncSession, tenant_id: uuid.UUID, leadgen_id: str
) -> Lead | None:
    result = await session.execute(
        select(Lead).where(Lead.tenant_id == tenant_id, Lead.meta_leadgen_id == leadgen_id)
    )
    return result.scalar_one_or_none()


async def list_leads(
    session: AsyncSession,
    *,
    limit: int = 50,
    before: datetime | None = None,
    status: LeadStatus | None = None,
    search: str | None = None,
    offset: int = 0,
) -> list[Lead]:
    """Newest first. Scoped by RLS; `tenant_id` is not passed in or filtered on.

    `status` and `search` exist because the leads screen is a working list, not a dump: an
    operator looks for "the hot ones" or "Priya", and making them scroll to find either is
    the same failure as a dashboard that hides the work.

    `search` is an `ILIKE` across the three fields a person actually knows a lead by.
    Deliberately not a full-text search over `field_data` - a Meta form can hold anything,
    and an operator looking for a lead is looking for a name, an email or a phone number.
    Leading `%` prevents index use, which is acceptable precisely because the result is
    capped by `limit` and the table is per-tenant.
    """
    statement = select(Lead).order_by(Lead.created_at.desc(), Lead.id)
    if before is not None:
        statement = statement.where(Lead.created_at < before)
    statement = _apply_lead_filters(statement, status=status, search=search, tenant_id=None)
    if offset:
        statement = statement.offset(offset)
    result = await session.execute(statement.limit(limit))
    return list(result.scalars().all())


def _apply_lead_filters(statement, *, status, search, tenant_id):
    if tenant_id is not None:
        statement = statement.where(Lead.tenant_id == tenant_id)
    if status is not None:
        statement = statement.where(Lead.status == status)
    if search:
        # The wildcards in the *pattern* are ours; any wildcard inside the *term* must be
        # escaped or it becomes one. Binding the term as a parameter prevents SQL injection
        # but does nothing about this: searching for a literal `%` would otherwise build the
        # pattern `%%%`, which matches every row in the tenant - useless to the operator and
        # a full scan on a large table.
        term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        statement = statement.where(
            or_(
                Lead.name.ilike(pattern, escape="\\"),
                Lead.email.ilike(pattern, escape="\\"),
                Lead.phone.ilike(pattern, escape="\\"),
            )
        )
    return statement


async def count_leads_filtered(
    session: AsyncSession, *, status: LeadStatus | None = None, search: str | None = None
) -> int:
    """Total matching the same filters as `list_leads`.

    Needed for real pagination: `limit` alone tells a client how many rows it got, never how
    many exist, so without this every page looks like the last one.
    """
    statement = _apply_lead_filters(
        select(func.count()).select_from(Lead), status=status, search=search, tenant_id=None
    )
    return int((await session.execute(statement)).scalar_one())


async def count_leads(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Lead))
    return result.scalar_one()


def lead_values(lead: Lead) -> dict[str, Any]:
    """Safe representation for API responses.

    `field_data` is included because the tenant supplied the form and is entitled to
    the answers; RLS has already established that it is their lead. Credential
    material is never on a Lead, by design - it lives only on the connection.
    """
    return {
        "id": str(lead.id),
        "tenant_id": str(lead.tenant_id),
        "meta_leadgen_id": lead.meta_leadgen_id,
        "source": lead.source.value,
        "status": lead.status.value,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "form_id": lead.form_id,
        "ad_id": lead.ad_id,
        "provider_created_at": lead.provider_created_at,
        "field_data": lead.field_data,
        # The AI's own merge of the four scored dimensions, surfaced so the leads screen can
        # render them as bars instead of asking an operator to read raw JSON. It is tenant
        # data about the tenant's own lead, and it contains no model internals.
        "qualification": lead.qualification,
        "created_at": lead.created_at,
        "updated_at": lead.updated_at,
    }


__all__ = [
    "count_leads",
    "count_leads_filtered",
    "get",
    "get_by_leadgen_id",
    "insert_if_new",
    "lead_values",
    "list_leads",
]
