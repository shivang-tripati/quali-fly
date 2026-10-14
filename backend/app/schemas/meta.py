"""Meta connection API schemas.

Secrets are write-only: a verification token or access token can be sent in, and the
response only reports whether one is configured. There is deliberately no field that
can echo a secret back.
"""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.meta_connection import MetaConnectionStatus, MetaProvider


class MetaConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)
    page_id: str | None = Field(default=None, max_length=64, pattern=r"^\d+$")
    # `whatsapp_business_account_id` is deliberately not here. It is a trusted provider id
    # that inbound webhook routing keys on, so it is only ever set after Meta confirms it -
    # see `POST /connections/{id}/whatsapp/business-account`. Phase 10-O.3.
    app_id: str | None = Field(default=None, max_length=64)
    status: MetaConnectionStatus = MetaConnectionStatus.ACTIVE
    # Write-only. Stored as a SHA-256 digest; never returned.
    webhook_verify_token: str | None = Field(default=None, min_length=8, max_length=256)

    @model_validator(mode="after")
    def _at_least_one_identifier(self) -> Self:
        # `instagram_business_id` is not a candidate here: an Instagram Business Account
        # is attached to a Page, so it cannot be the first identifier a connection has. It
        # is set by POST /connections/{id}/instagram once Meta has confirmed the link.
        if not self.page_id:
            raise ValueError("page_id is required")
        return self


class MetaConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)
    page_id: str | None = Field(default=None, max_length=64, pattern=r"^\d+$")
    # Absent for the same reason as on create: a verified binding only.
    app_id: str | None = Field(default=None, max_length=64)
    status: MetaConnectionStatus | None = None
    webhook_verify_token: str | None = Field(default=None, min_length=8, max_length=256)

    def changed_fields(self) -> dict[str, Any]:
        """Only the keys the caller actually sent, so a PATCH never clears by accident."""
        return self.model_dump(exclude_unset=True)


class MetaConnectionResponse(BaseModel):
    id: uuid.UUID
    provider: MetaProvider
    status: MetaConnectionStatus
    display_name: str | None
    page_id: str | None
    instagram_business_id: str | None
    whatsapp_business_account_id: str | None
    # Phase 10-O.3: the address outbound WhatsApp messages are POSTed to, and its readable
    # form. Neither is a secret; see `meta_connections.redact()`.
    whatsapp_phone_number_id: str | None
    whatsapp_display_number: str | None
    app_id: str | None
    webhook_verify_token_configured: bool
    access_token_configured: bool
    scopes: list[str] = Field(default_factory=list)
    token_expires_at: datetime | None
    connected_at: datetime | None
    last_webhook_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OAuthStartResponse(BaseModel):
    """Where to send the browser next.

    The URL is returned rather than issued as a 302 so the SPA controls the navigation
    and the test suite never has to follow a redirect to Meta. It contains the state
    value, which is single-use and short-lived - treat it as a one-shot secret.
    """

    authorize_url: str
    expires_in: int


class OAuthCallbackResponse(BaseModel):
    """The connection Meta authorized. Same safe shape as any other read."""

    connection: MetaConnectionResponse
    granted_page_count: int = Field(
        description="Pages Meta returned. 1 is the supported case; more is logged, not stored."
    )


class InstagramAccountOptionResponse(BaseModel):
    """One Instagram Business Account this connection's Page can operate."""

    instagram_business_id: str
    username: str | None = None
    name: str | None = None
    label: str = Field(description="Best available name for the account, for a select control.")


class InstagramAccountsResponse(BaseModel):
    """What the tenant may choose from, discovered from the current Meta credential.

    `connected_id` is what is already saved, so the screen can mark the current choice
    without comparing ids itself.
    """

    page_id: str
    page_name: str | None = None
    connected_id: str | None = None
    accounts: list[InstagramAccountOptionResponse] = Field(default_factory=list)


class InstagramSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instagram_business_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")


class WhatsAppNumberOptionResponse(BaseModel):
    """One phone number this connection's WhatsApp Business Account can send from."""

    phone_number_id: str
    display_number: str | None = None
    verified_name: str | None = None
    is_verified: bool = False
    label: str = Field(description="Best available name for the number, for a select control.")


class WhatsAppNumbersResponse(BaseModel):
    """What the tenant may choose from, discovered from the current Meta credential."""

    whatsapp_business_account_id: str | None = None
    connected_id: str | None = None
    numbers: list[WhatsAppNumberOptionResponse] = Field(default_factory=list)


class WhatsAppNumberSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")


class WhatsAppAccountSelectRequest(BaseModel):
    """A proposed WhatsApp Business Account id, to be confirmed against Meta.

    The field name is explicit about its status: this is a claim about which account the
    tenant runs, not an instruction to bind it. `meta_whatsapp.associate_business_account`
    asks Meta to confirm it with this connection's token before anything is written.
    """

    model_config = ConfigDict(extra="forbid")

    whatsapp_business_account_id: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")


class WebhookEventResponse(BaseModel):
    """Metadata only. The payload is deliberately not exposed yet."""

    id: uuid.UUID
    object_type: str | None
    event_type: str | None
    meta_event_id: str | None
    status: str
    received_at: datetime
    processed_at: datetime | None
    error_message: str | None


class WebhookAck(BaseModel):
    """What Meta gets back. Deliberately minimal: no ids, no tenant data."""

    status: str = "ok"
    event_id: uuid.UUID | None = None
    duplicate: bool = False
