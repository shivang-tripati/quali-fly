"""The conversation engine: the channel-agnostic core of Phase 4.

Everything surface-specific is delegated to a `ChannelAdapter`; everything else -
finding the thread, deduplicating, persisting, sending, classifying failures - happens
once, here. That is the PRD's "do not create three separate conversation engines", and it
is also just what the code would look like, because Messenger and Instagram share the
send endpoint.

Two operations:

    ingest_inbound   webhook delivery -> thread + inbound messages (durable, idempotent)
    send_outbound    text -> Meta    -> OUTBOUND message, SENT or FAILED

`send_outbound` is the capability Phase 5's AI will call. It is exposed now and used now
by a human-reply endpoint, so it is exercised against a real Graph boundary rather than
sitting untested until Phase 5.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.core.crypto import DecryptionError, decrypt
from app.db.tenant_context import set_current_tenant
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.repositories import conversations as conversation_repo
from app.repositories import meta_connections as connection_repo
from app.repositories import scheduled_tasks as task_repo
from app.services.channels import WHATSAPP_PRODUCT, UnknownRecipient, adapter_for
from app.services.meta_graph import (
    MetaApiError,
    MetaAuthError,
    MetaGraphClient,
    MetaMessageWindowClosed,
    MetaTransientError,
    MetaUnknownOutcomeError,
)
from app.services.meta_messaging import (
    MalformedMessage,
    normalize_outbound,
)

log = logging.getLogger("app.conversations")

# Meta's standard messaging window, from the time of the last inbound message.
MESSAGING_WINDOW = timedelta(hours=24)

# Short failure categories stored on the message. Never Meta's payload.
REASON_WINDOW_CLOSED = "messaging_window_closed"
REASON_RECIPIENT_UNAVAILABLE = "recipient_unavailable"
REASON_CREDENTIAL = "credential_unavailable"
REASON_UNSENDABLE = "not_sendable"
REASON_META_UNAVAILABLE = "meta_unavailable"
REASON_OUTCOME_UNKNOWN = "delivery_outcome_unknown"
REASON_REJECTED = "rejected_by_meta"


@dataclass(frozen=True)
class AIAttribution:
    """Real provider usage for the message being sent (Phase 5).

    Carries no credentials and no prompt text, so it is safe to log and to pass across
    layers. `provider` is intentionally absent: `messages` records which model spoke, and
    the full audit trail (including provider) lives on `ai_message_runs`.
    """

    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0


class ConversationNotSendable(Exception):
    """The conversation cannot receive a message right now.

    Raised for local, pre-flight problems - no credential, no page id, an empty body -
    where a round trip to Meta would be pointless. A `reason` is stored on the message so
    the API can report it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConversationEngine:
    """One tenant's conversation engine."""

    def __init__(
        self,
        session,
        tenant_id: uuid.UUID,
        *,
        graph: MetaGraphClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings or get_settings()
        self._graph = graph

    # -- inbound ----------------------------------------------------------

    async def ingest_inbound(
        self,
        *,
        meta_connection_id: uuid.UUID,
        channel: Channel,
        messages: list,
    ) -> list[Message]:
        """Store the messages of one delivery, opening the thread if it is new.

        Idempotent at both levels: the thread is found-or-created against a unique index,
        and each message is inserted against the `mid` index. A Meta retry is a success.

        **Follow-up cancellation happens here, inside the caller's transaction.** This is
        the single most important invariant in Phase 7:

            INBOUND MESSAGE INSERT
              + FOLLOW-UP CANCELLATION
              = SAME TRANSACTION

        The alternative - insert, commit, then cancel in a follow-up transaction - leaves a
        window in which the scheduler claims a due follow-up and sends it to a lead who has
        already replied. That is the failure this ordering exists to make impossible.

        The cancellation runs **only when the message row was newly created**. That is not a
        weakening of the Phase 7 invariant, which is about the *first* delivery: the insert
        and the cancellation are in one transaction, so a genuine reply always cancels
        atomically with being stored.

        Running it on duplicates was previously justified as "a duplicate still proves the
        lead replied", and the `UPDATE` is idempotent - but idempotent is not the same as
        harmless. Meta retries are not instantaneous, so this ordering is reachable:

            reply stored at T0  ->  step 1 sends, step 2 armed
            ->  Meta redelivers the T0 message at T0+1day
            ->  step 2 cancelled, for a lead who never spoke again

        The first delivery had already cancelled everything it should have. Re-running the
        cancellation can only ever reach tasks created *after* it, which are exactly the ones
        that must survive.
        """
        if not messages:
            return []

        stored: list[Message] = []
        for message in messages:
            await set_current_tenant(self._session, self._tenant_id)
            now = message.sent_at or datetime.now(UTC)
            conversation, created = await conversation_repo.find_or_create(
                self._session,
                tenant_id=self._tenant_id,
                meta_connection_id=meta_connection_id,
                channel=channel,
                participant_id=message.participant_id,
                source_id=message.source_id,
                referral_ad_id=message.referral_ad_id,
                now=now,
            )
            row, message_created = await conversation_repo.insert_inbound_if_new(
                self._session,
                tenant_id=self._tenant_id,
                conversation_id=conversation.id,
                channel=channel,
                meta_message_id=message.meta_message_id,
                message_type=message.message_type,
                text=message.text,
                attachments=message.attachments,
                sent_at=now,
            )
            if message_created:
                await conversation_repo.touch(self._session, conversation, now=now, inbound=True)

                # Same transaction as the insert above, and only for a newly-created row.
                # The scheduler reads these rows under its own connection, so it cannot
                # observe "message stored, follow-up still pending" - only the committed
                # before-state or the committed after-state. A duplicate skips this entirely;
                # see the method docstring for why running it again is not harmless.
                cancelled = await task_repo.cancel_pending_for_conversation(
                    self._session,
                    tenant_id=self._tenant_id,
                    conversation_id=conversation.id,
                    now=now,
                )
                if cancelled:
                    log.info(
                        "follow_up_cancelled",
                        extra={
                            "tenant_id": str(self._tenant_id),
                            "conversation_id": str(conversation.id),
                            "cancelled_count": cancelled,
                            "trigger": "inbound_message",
                        },
                    )
            stored.append(row)

            log.info(
                "message_persisted" if message_created else "message_duplicate",
                extra={
                    "conversation_id": str(conversation.id),
                    "channel": channel.value,
                    "meta_message_id": message.meta_message_id,
                    "conversation_created": created,
                    "has_text": message.text is not None,
                    "delivery": row.delivery.value,
                },
            )
        return stored

    # -- outbound ---------------------------------------------------------

    def messaging_window_expires_at(self, conversation: Conversation) -> datetime | None:
        """When Meta will stop accepting a `RESPONSE` send for this thread.

        Informational. The send is still attempted, and Meta is still the authority - a
        proactive refusal here would be us guessing at a policy we do not own.
        """
        if conversation.last_inbound_at is None:
            return None
        return conversation.last_inbound_at + MESSAGING_WINDOW

    def is_within_messaging_window(self, conversation: Conversation) -> bool:
        expires = self.messaging_window_expires_at(conversation)
        return expires is not None and datetime.now(UTC) < expires

    async def send_outbound(
        self,
        conversation: Conversation,
        *,
        text: str,
        ai_attribution: AIAttribution | None = None,
        messaging_type: str | None = None,
        message_tag: str | None = None,
        scheduled_task_id: uuid.UUID | None = None,
    ) -> Message:
        """Send a text message to a thread, persisting the attempt either way.

        The message row is written PENDING *before* the call, so an attempt is never
        invisible within the transaction. It ends up SENT with Meta's `message_id`, or
        FAILED with a category, and the caller commits either outcome.

        The PENDING insert, the network call and the outcome write all share one
        transaction, which is deliberate and load-bearing. It is tempting to commit the
        PENDING row first "so the attempt survives a crash", but that is exactly backwards:
        it would leave PENDING rows that no longer distinguish *about to send* from
        *already delivered, unknown*, and a recovered follow-up would read that orphan as
        safe-to-send and deliver a second copy to a real prospect. Keeping the call inside
        the transaction means a hard crash rolls the attempt back and leaves no misleading
        evidence behind. The cost is holding a pooled connection across the HTTP call.

        What this cannot cover is a process killed after Meta accepted a message but before
        the commit lands: the message really was delivered and the local record of it is
        gone. Meta's Send API takes no idempotency key, so nothing local can detect that
        case. The best available response is the one below - when a send's outcome is
        *reported* as unknown, stop rather than retry.

        `ai_attribution` is Phase 5's hook and is deliberately optional, so the existing
        human send path is unchanged. It is threaded through *this* method rather than
        applied by the caller, because the token counts have to be written onto the same
        row that records the delivery - a separate update afterwards would risk
        attributing a different send's usage to this message.

        `messaging_type` and `message_tag` are Phase 7's hook, for the same reason: a
        follow-up that falls outside the 24-hour window needs `MESSAGE_TAG` + `HUMAN_AGENT`,
        and the decision about which window applies belongs with the caller that knows the
        conversation's activity, not buried in here. Both default to `None`, so every
        pre-Phase-7 send is byte-identical.

        `scheduled_task_id` attributes the attempt to the task that asked for it. A partial
        unique index on `messages` allows one successful outbound per task, so a follow-up
        that is somehow executed twice fails here at the database rather than delivering a
        second copy of the same message to a real prospect.
        """
        adapter = adapter_for(conversation.channel)
        await set_current_tenant(self._session, self._tenant_id)

        try:
            body = normalize_outbound(text)
        except MalformedMessage:
            raise ConversationNotSendable(REASON_UNSENDABLE) from None

        connection, access_token = await self._credential_for(conversation)

        message = await conversation_repo.create_outbound_pending(
            self._session,
            tenant_id=self._tenant_id,
            conversation_id=conversation.id,
            channel=conversation.channel,
            text=body,
            sent_at=datetime.now(UTC),
            scheduled_task_id=scheduled_task_id,
        )
        if ai_attribution is not None:
            # Written on the PENDING row, before the network call, so the token counts
            # land on the same row that records the delivery. If the send later fails, the
            # attempt is kept as FAILED and still shows that an AI produced this text - the
            # alternative, a second write after the outcome, could attribute a different
            # send's usage to this message.
            conversation_repo.apply_ai_attribution(self._session, message, ai_attribution)
        log.info(
            "message_send_started",
            extra={
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "channel": conversation.channel.value,
                "permission": adapter.required_permission,
                "within_window": self.is_within_messaging_window(conversation),
            },
        )

        try:
            request = adapter.build_send(
                participant_id=conversation.meta_participant_id,
                text=body,
                messaging_type=messaging_type,
                tag=message_tag,
            )
        except UnknownRecipient:
            return await conversation_repo.mark_message_failed(
                self._session, message, reason=REASON_UNSENDABLE
            )

        if self._graph is None:
            # A misconfigured worker must fail loudly, not quietly mark messages sent.
            raise RuntimeError("ConversationEngine.send_outbound needs a MetaGraphClient")

        try:
            if request.messaging_product == WHATSAPP_PRODUCT:
                # The 24-hour customer service window is enforced *here* rather than in the
                # Graph client, because the client cannot see when the person last wrote.
                # Outside it WhatsApp requires an approved message template, and template
                # management is not part of this phase - so the honest outcome is a
                # permanent, explainable refusal rather than a send Meta will reject.
                if not self.is_within_messaging_window(conversation):
                    return await conversation_repo.mark_message_failed(
                        self._session, message, reason=REASON_WINDOW_CLOSED
                    )
                meta_message_id = await self._graph.send_whatsapp_message(
                    access_token=access_token,
                    phone_number_id=connection.whatsapp_phone_number_id or "",
                    recipient_id=request.recipient_id,
                    text=request.text,
                )
            else:
                meta_message_id = await self._graph.send_message(
                    access_token=access_token,
                    page_id=connection.page_id,
                    recipient_id=request.recipient_id,
                    text=request.text,
                    messaging_type=request.messaging_type,
                    tag=request.tag,
                )
        except MetaMessageWindowClosed:
            return await conversation_repo.mark_message_failed(
                self._session, message, reason=REASON_WINDOW_CLOSED
            )
        except MetaAuthError:
            return await conversation_repo.mark_message_failed(
                self._session, message, reason=REASON_CREDENTIAL
            )
        except MetaTransientError:
            # Meta answered and said it did not take the request (429, 5xx, a documented
            # transient code). Nothing was delivered, so a retry cannot duplicate anything.
            return await conversation_repo.mark_message_failed(
                self._session, message, reason=REASON_META_UNAVAILABLE
            )
        except MetaUnknownOutcomeError:
            # The request went out and the answer never came back. It may or may not have been
            # accepted, so it is recorded under its own reason and callers must not treat it
            # as a plain failure to retry.
            return await conversation_repo.mark_message_failed(
                self._session, message, reason=REASON_OUTCOME_UNKNOWN
            )
        except MetaApiError:
            return await conversation_repo.mark_message_failed(
                self._session, message, reason=REASON_REJECTED
            )

        sent = await conversation_repo.mark_message_sent(
            self._session, message, meta_message_id=meta_message_id, at=datetime.now(UTC)
        )
        # An outbound message moves thread recency but not the window: answering inside
        # the window does not extend it.
        await conversation_repo.touch(
            self._session, conversation, now=datetime.now(UTC), inbound=False
        )
        log.info(
            "message_sent",
            extra={
                "conversation_id": str(conversation.id),
                "message_id": str(sent.id),
                "channel": conversation.channel.value,
                "meta_message_id": meta_message_id,
                "delivery": sent.delivery.value,
            },
        )
        return sent

    async def _credential_for(self, conversation: Conversation) -> tuple[MetaConnection, str]:
        """Resolve and decrypt the token a send needs, and refuse a connection with no address.

        Every failure here is local and permanent: no connection, an inactive one, no
        credential, a key that no longer decrypts, or a connection that has no id to send
        to. None of them improve on a retry.

        **Which identifier must be present depends on the channel**, which is the one
        thing Phase 10-O.3 changed here. A Messenger or Instagram send is addressed to a
        Page; a WhatsApp send is addressed to a phone number. Both go through the same
        encrypted token on the same row, so the difference is one check rather than a second
        credential path - and getting it wrong would either refuse working Instagram
        connections or try to post WhatsApp messages to a Page endpoint.
        """
        if conversation.meta_connection_id is None:
            raise ConversationNotSendable(REASON_CREDENTIAL)
        connection = await connection_repo.get_connection(
            self._session, conversation.meta_connection_id
        )
        if connection is None or connection.status is not MetaConnectionStatus.ACTIVE:
            raise ConversationNotSendable(REASON_CREDENTIAL)
        if not connection.access_token_encrypted:
            raise ConversationNotSendable(REASON_CREDENTIAL)

        if conversation.channel is Channel.WHATSAPP:
            if not connection.whatsapp_phone_number_id:
                # The account is set but no number was chosen, so there is no endpoint.
                raise ConversationNotSendable(REASON_CREDENTIAL)
        elif not connection.page_id:
            # Instagram-only connections have no page id and therefore no send endpoint.
            raise ConversationNotSendable(REASON_CREDENTIAL)

        try:
            token = decrypt(
                connection.access_token_encrypted,
                key=self._settings.meta_token_encryption_key,
            )
        except DecryptionError:
            log.error(
                "message_token_decrypt_failed",
                extra={
                    "meta_connection_id": str(connection.id),
                    "conversation_id": str(conversation.id),
                },
            )
            raise ConversationNotSendable(REASON_CREDENTIAL) from None
        return connection, token


def is_delivered(message: Message) -> bool:
    return message.direction is MessageDirection.INBOUND or (
        message.delivery is MessageDelivery.SENT
    )


__all__ = [
    "ConversationEngine",
    "ConversationNotSendable",
    "MESSAGING_WINDOW",
    "REASON_CREDENTIAL",
    "REASON_META_UNAVAILABLE",
    "REASON_RECIPIENT_UNAVAILABLE",
    "REASON_REJECTED",
    "REASON_UNSENDABLE",
    "REASON_WINDOW_CLOSED",
    "is_delivered",
]
