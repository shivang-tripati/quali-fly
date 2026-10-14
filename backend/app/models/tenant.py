"""The tenant is the isolation root, so it is intentionally not RLS-scoped.

Access to a tenant is always decided by `TenantMembership`, which is.
"""

import enum
from typing import Any

from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class TenantStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("slug", name="uq_tenants_slug"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    status: Mapped[TenantStatus] = mapped_column(
        SAEnum(TenantStatus, name="tenant_status", native_enum=True),
        nullable=False,
        default=TenantStatus.ACTIVE,
        index=True,
    )
    # Phase 7 follow-up templates and per-step delays, as one JSONB document rather than a
    # table. The PRD's shape is `{"follow_up_1": {"delay_hours", "template", "ai_enhance"},
    # ...}`, which is naturally a document: it is always read whole, always written whole,
    # and has no rows to reference. A table would be three more joins to answer the same
    # question. `{}` means "use the built-in defaults", so an existing tenant needs no
    # backfill to be functional.
    #
    # Validated on write by `app.services.follow_up_templates`; the column is untyped JSONB
    # because the schema differs per tenant.
    #
    # Phase 10-O.1: this column finally has a writer. It was read and rendered from day one
    # and could only be set by editing a test - see `0012_tenant_control_plane`.
    follow_up_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # -- Phase 10-O.1: the tenant control plane ----------------------------
    #
    # Everything above this line is identity and integration. Everything below is
    # configuration the tenant owns, which it could not do at all before this phase.

    #: What the business is called, as the *customer-facing staff* would write it. Null means
    #: "not set", and the AI context falls back to `tenants.name` rather than inventing one.
    #: Separate from `name` because the slug-safe internal name and the name a person says
    #: out loud to a lead are frequently not the same string.
    business_name: Mapped[str | None] = mapped_column(String(200))

    #: What the business actually does, in the tenant's own words. This is the single most
    #: valuable field in the product for AI quality and the one the audit found the model
    #: never receiving: `RagContextProvider` used to leave it unset, so every prompt in
    #: production carried the "no detail configured" fallback.
    business_description: Mapped[str | None] = mapped_column(Text)

    #: Does this tenant want AI replies? NOT the deployment gate - `AI_ENABLED` is, and
    #: stays the master switch. This is the tenant's own opt-out, so one workspace can stop
    #: automating itself without affecting anyone else on the platform.
    #:
    #: Defaults true because a tenant that predates this column was, until now, governed
    #: entirely by the platform flag. Defaulting false would silently switch off live
    #: automation for every existing tenant on upgrade.
    ai_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    #: The same shape and the same reasoning as `ai_enabled`, for the follow-up engine.
    follow_up_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
