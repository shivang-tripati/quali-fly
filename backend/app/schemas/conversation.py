"""Conversation and message API schemas.

Messages contain what people actually said, so they are personal data: the tenant's own,
returned only to the tenant, and behind the same RLS as everything else. No field here
can carry a credential.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import (
    Channel,
    MessageDelivery,
    MessageDirection,
)


class ConversationResponse(BaseModel):
    id: uuid.UUID
    channel: Channel
    # The person's Meta id. Not a credential, but it is a stable external identifier for
    # a real person, so it is only ever shown to the owning tenant.
    meta_participant_id: str
    meta_source_id: str | None
    meta_referral_ad_id: str | None
    lead_id: uuid.UUID | None
    #: The lead's name, joined in. `meta_participant_id` above is what Meta calls this
    #: person; this is what the operator knows them as. Nullable because a thread can exist
    #: before a lead row is attached to it.
    lead_name: str | None = None
    meta_connection_id: uuid.UUID | None
    last_message_at: datetime | None
    # When Meta will stop accepting a reply. Informational: the send is still attempted
    # and Meta is still the authority.
    messaging_window_expires_at: datetime | None
    within_messaging_window: bool
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    count: int


class MessageResponse(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    channel: Channel
    direction: MessageDirection
    delivery: MessageDelivery
    meta_message_id: str | None
    message_type: str
    text: str | None
    # Attachment types/titles only. Meta's URLs expire and can point at personal media,
    # so they are not retained.
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    sent_at: datetime
    delivered_at: datetime | None
    failure_reason: str | None
    # Declared by the PRD, written by Phase 5.
    ai_model: str | None
    ai_prompt_tokens: int | None
    ai_completion_tokens: int | None
    ai_latency_ms: int | None
    created_at: datetime


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    count: int


class OutboundMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


class ChannelInfo(BaseModel):
    channel: Channel
    payload_objects: list[str]
    required_permission: str
