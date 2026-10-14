"""Tenant-scoped conversation reads and the outbound send.

Reads are scoped by RLS. The send is the capability Phase 5's AI will call, exposed now
so it is exercised against a real Graph boundary rather than sitting untested: a tenant
admin replying to a person is a genuine use of the same code path, not a test hook.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUserSession, require_minimum_role
from app.core.auth import TenantContext
from app.core.errors import ApiError, ErrorCode, error_responses
from app.models.conversation import Conversation
from app.models.membership import Role
from app.repositories import conversations as conversation_repo
from app.schemas.conversation import (
    ChannelInfo,
    ConversationListResponse,
    ConversationResponse,
    MessageListResponse,
    MessageResponse,
    OutboundMessageRequest,
)
from app.services import conversations as engine
from app.services.channels import describe

router = APIRouter(prefix="/conversations", tags=["conversations"])

log = logging.getLogger("app.conversations.api")

ReadCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]
# Sending a message to a real person is an act, not a read, so it needs a membership - but
# Phase 10-O.1 corrected which bar.
#
# This used to require ADMIN, on the reasoning that sending is "an act" deserving the same
# bar as other outward-facing mutations. That reasoning was applied to the wrong baseline:
# the audit found `MEMBER` is *the* role the handoff queue is built for - `api/handoffs.py`
# lets a MEMBER list, claim, start and resolve an escalation - so a MEMBER could take
# ownership of a customer conversation and then be refused the only endpoint that answers
# them. A control that cannot be used by the person the workflow assigns it to is not a
# control, it is a dead end, and the failure lands exactly where it hurts most: a lead who
# asked for a person.
#
# MEMBER is therefore the bar. That is still a real bar: it is still a verified membership in
# *this* tenant, so it grants nothing across a tenant boundary, and every tenant-scoped read
# in this module already sits at the same level. What it does not grant is anything
# platform-wide - there is no cross-tenant path here, before or after this change.
SendCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]


def _conversation_response(
    conversation: Conversation, engine_: engine.ConversationEngine, lead_name: str | None = None
) -> ConversationResponse:
    window = engine_.messaging_window_expires_at(conversation)
    return ConversationResponse(
        id=conversation.id,
        channel=conversation.channel,
        meta_participant_id=conversation.meta_participant_id,
        meta_source_id=conversation.meta_source_id,
        meta_referral_ad_id=conversation.meta_referral_ad_id,
        lead_id=conversation.lead_id,
        lead_name=lead_name,
        meta_connection_id=conversation.meta_connection_id,
        last_message_at=conversation.last_message_at,
        messaging_window_expires_at=window,
        within_messaging_window=window is not None
        and engine_.is_within_messaging_window(conversation),
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_response(message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        conversation_id=message.conversation_id,
        channel=message.channel,
        direction=message.direction,
        delivery=message.delivery,
        meta_message_id=message.meta_message_id,
        message_type=message.message_type,
        text=message.text,
        attachments=list(message.attachments or []),
        sent_at=message.sent_at,
        delivered_at=message.delivered_at,
        failure_reason=message.failure_reason,
        ai_model=message.ai_model,
        ai_prompt_tokens=message.ai_prompt_tokens,
        ai_completion_tokens=message.ai_completion_tokens,
        ai_latency_ms=message.ai_latency_ms,
        created_at=message.created_at,
    )


@router.get(
    "/channels",
    response_model=list[ChannelInfo],
    summary="Messaging channels we support and the permission each one needs.",
)
async def list_channels(_context: ReadCtx) -> list[ChannelInfo]:
    return [ChannelInfo(**info) for info in describe()]


@router.get(
    "",
    response_model=ConversationListResponse,
    summary="The current tenant's conversations, most recently active first.",
    responses=error_responses(401, 403),
)
async def list_conversations(
    session: CurrentUserSession,
    context: ReadCtx,
    limit: int = 50,
    lead_id: uuid.UUID | None = None,
) -> ConversationListResponse:
    bounded = max(1, min(limit, 200))
    rows = await conversation_repo.list_conversations(session, limit=bounded, lead_id=lead_id)
    engine_ = engine.ConversationEngine(session, context.tenant_id)
    return ConversationListResponse(
        items=[_conversation_response(row, engine_, name) for row, name in rows],
        count=len(rows),
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="Fetch one conversation. RLS makes another tenant's a 404.",
    responses=error_responses(401, 403, 404),
)
async def get_conversation(
    conversation_id: uuid.UUID,
    session: CurrentUserSession,
    context: ReadCtx,
) -> ConversationResponse:
    found = await conversation_repo.get_with_lead_name(session, conversation_id)
    if found is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Conversation not found.")
    conversation, lead_name = found
    return _conversation_response(
        conversation, engine.ConversationEngine(session, context.tenant_id), lead_name
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=MessageListResponse,
    summary="A conversation's messages, oldest first.",
    responses=error_responses(401, 403, 404),
)
async def list_messages(
    conversation_id: uuid.UUID,
    session: CurrentUserSession,
    _context: ReadCtx,
    limit: int = 200,
) -> MessageListResponse:
    conversation = await conversation_repo.get(session, conversation_id)
    if conversation is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Conversation not found.")
    bounded = max(1, min(limit, 500))
    rows = await conversation_repo.list_messages(
        session, conversation_id=conversation_id, limit=bounded
    )
    return MessageListResponse(items=[_message_response(row) for row in rows], count=len(rows))


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Send a text message to the person in this conversation.",
    responses={
        401: {"description": "Authentication required."},
        403: {"description": "Authenticated but not permitted to send."},
        404: {"description": "Conversation not found."},
        409: {"description": "The conversation cannot be sent to at all."},
    },
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: OutboundMessageRequest,
    session: CurrentUserSession,
    context: SendCtx,
) -> MessageResponse:
    """Send via Meta and record the attempt.

    The attempt is persisted before the outbound call, so a crash mid-send leaves a row
    rather than nothing. That record is also why this endpoint answers 201 even when Meta
    refused the send: `delivery` and `failure_reason` report the outcome, and raising an
    error instead would roll back the request transaction and delete the very row this
    design exists to keep. Errors are reserved for pre-flight refusals, where no
    outbound call was attempted and no row exists.
    """
    from app.core.config import get_settings
    from app.services.meta_oauth import build_graph_client

    conversation = await conversation_repo.get(session, conversation_id)
    if conversation is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Conversation not found.")

    engine_ = engine.ConversationEngine(
        session, context.tenant_id, graph=build_graph_client(get_settings())
    )
    try:
        message = await engine_.send_outbound(conversation, text=payload.text)
    except engine.ConversationNotSendable as exc:
        raise ApiError(
            409,
            ErrorCode.MESSAGE_NOT_SENDABLE,
            "This conversation cannot be sent to: " + _describe_send_failure(exc.reason),
        ) from None
    except IntegrityError:
        raise ApiError(
            409, ErrorCode.MESSAGE_NOT_SENDABLE, "The message could not be recorded."
        ) from None

    return _message_response(message)


def _describe_send_failure(reason: str) -> str:
    return {
        engine.REASON_CREDENTIAL: "the Meta connection has no usable access token.",
        engine.REASON_UNSENDABLE: "the message text was empty or too long.",
        engine.REASON_WINDOW_CLOSED: "Meta's 24-hour messaging window has closed.",
        engine.REASON_RECIPIENT_UNAVAILABLE: "the recipient is no longer reachable.",
    }.get(reason, "the conversation cannot receive messages.")
