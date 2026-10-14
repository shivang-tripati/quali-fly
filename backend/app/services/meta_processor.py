"""The async-processing seam for inbound Meta events.

Phase 2 deliberately stopped at the durable boundary. This class owns the state machine
(`RECEIVED -> PROCESSING -> PROCESSED|FAILED`) and the concurrency-safe claim, so the
webhook request can ACK immediately.

Phase 3 fills in `handle()` with Lead Ads ingestion. The transaction discipline is the
important part, and it is unchanged:

    short transaction: claim  -> commit
    (no database lock)
    domain work:                 decrypt token, call Meta Graph API, normalize
    short transaction: insert lead, mark processed / failed

No database lock is ever held across the external call, because that is exactly what
turns one slow Meta response into a queue of blocked workers. The window is also why
lead deduplication has to be the database's job rather than a check: between claiming
and inserting, another worker may be doing the same thing for the same event.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, SystemClock, worker_identity
from app.core.config import Settings, get_settings
from app.core.crypto import DecryptionError, decrypt
from app.db.tenant_context import set_current_tenant
from app.models.meta_connection import MetaConnectionStatus
from app.models.meta_webhook_event import MetaWebhookEvent
from app.models.tenant import Tenant, TenantStatus
from app.repositories import conversations as conversation_repo
from app.repositories import leads as lead_repo
from app.repositories import meta_connections as connection_repo
from app.repositories import meta_webhook_events as event_repo
from app.schemas.ai import AIProcessingResult
from app.services.ai_qualification import AIConversationService
from app.services.channels import channel_for_payload
from app.services.conversations import REASON_REJECTED, ConversationEngine
from app.services.meta_graph import MetaApiError, MetaAuthError, MetaGraphClient
from app.services.meta_leads import InvalidLeadPayload, extract_leadgen_events, normalize_lead
from app.services.meta_messaging import extract_inbound_messages, extract_whatsapp_statuses
from app.services.rag_context import build_context_provider
from app.services.tenant_settings import load_profile

log = logging.getLogger("app.meta.processor")

# Why an event was stored but produced no domain object. Recorded on the event so a
# surprising outcome is diagnosable from the row alone, without a log spelunk.
#
# UNROUTED      the payload belongs to a surface we do not handle (Phase 12's WhatsApp,
#               an Instagram comment, a permission change). Left alone deliberately.
# NO_MESSAGE    the event reached a messaging channel but carried no message object:
#               a postback, a delivery receipt, a read receipt or a typing indicator.
RETRYABLE = "meta_api_unavailable"
PERMANENT = "permanent"
UNROUTED = "unrouted_event"
NO_MESSAGE = "messaging_event_without_message"

#: WhatsApp Cloud API delivery statuses, mapped to what this application records. Only the
#: two it acts on are named; `sent`, `read`, `deleted` and anything Meta adds later fall
#: through to a log line, because guessing at a status's meaning is how a delivery report
#: turns into a false "delivered" on a message the customer never saw.
_DELIVERED_STATUSES = frozenset({"delivered", "read"})
_FAILED_STATUS = "failed"


class MetaWebhookEventProcessor:
    """Claim events and turn Lead Ads events into tenant-scoped Leads."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        *,
        worker: str | None = None,
        graph: MetaGraphClient | None = None,
        settings: Settings | None = None,
        session_factory: async_sessionmaker | None = None,
        clock=None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings or get_settings()
        self._graph = graph
        # The claim token every terminal write is conditional on. Defaults to something
        # unique per processor instance rather than a constant, because a constant would
        # let two processes pass each other's ownership checks.
        self._worker = worker or worker_identity()
        self._clock: Clock = clock or SystemClock()
        # Phase 5 needs a *factory*, not this session: the AI pipeline opens several
        # short transactions per message and must not hold the processor's transaction
        # across an LLM call. Optional, because every pre-Phase-5 caller (and the whole
        # existing test suite) passes a single session and has no AI stage to run.
        self._session_factory = session_factory
        # Set when a lead in the current delivery could not be stored, so `handle` knows
        # not to report overall success.
        self._any_failed = False
        # Inbound messages stored by the last `handle`, newest first, for the AI stage.
        self._last_inbound_ids: list[uuid.UUID] = []

    async def claim_next(self) -> MetaWebhookEvent | None:
        """`FOR UPDATE SKIP LOCKED` so two workers never take the same event.

        The claim is recorded against this processor's worker identity, so every later
        write can prove it still owns the event. An event whose previous worker's lease
        lapsed is reclaimable here - that is the crash-recovery path.
        """
        await set_current_tenant(self._session, self._tenant_id)
        event = await event_repo.claim_next_received(
            self._session,
            worker=self._worker,
            now=self._clock.now(),
            lease_seconds=self._settings.webhook_processing_lease_seconds,
        )
        if event is not None:
            log.info(
                "webhook_processing_started",
                extra={
                    "event_id": str(event.id),
                    "meta_connection_id": str(event.meta_connection_id),
                    "event_type": event.event_type,
                    "status": event.status.value,
                    # Identifiers only: never the payload, which is customer content.
                    "attempt": event.attempt_count,
                },
            )
        return event

    async def renew_claim(self, event: MetaWebhookEvent) -> bool:
        """Push the claim's expiry out before slow external work.

        Lead retrieval is a network call the claim does not cover, so without this a slow
        but perfectly healthy worker is reclaimed mid-flight and its own result is then
        refused as stale. Called immediately before the Graph call, the same place the
        follow-up worker renews before sending.
        """
        await set_current_tenant(self._session, self._tenant_id)
        return await event_repo.renew_lease(
            self._session, event.id, worker=self._worker, now=self._clock.now()
        )

    async def handle(self, event: MetaWebhookEvent) -> MetaWebhookEvent:
        """Turn one event into leads, messages, or account for it as uninteresting.

        Runs in the lock-free window after `claim_next`'s transaction committed, so it
        re-establishes the tenant context itself: without it every RLS-scoped read here
        would silently return nothing.

        Routing is by what the payload actually contains, not by a guess: a `leadgen`
        change is a lead, a `messaging` entry on a known surface is a message, and
        anything else is deliberately ignored.
        """
        await set_current_tenant(self._session, self._tenant_id)

        references = extract_leadgen_events(event.payload)
        if not references:
            if await self._handle_messaging(event):
                return event
            # An Instagram comment, a permission change, a postback: correctly stored,
            # nothing to do. Marking it processed is what stops the worker spinning.
            await self.mark_processed(event, note=UNROUTED)
            return event

        connection = await connection_repo.get_connection(self._session, event.meta_connection_id)
        if connection is None or connection.status is not MetaConnectionStatus.ACTIVE:
            # Only reachable if an event was claimed after its connection was
            # disconnected. Retryable is the wrong answer and so is success: a lead was
            # genuinely dropped, so say so plainly instead of looping.
            await self.mark_failed(
                event,
                "connection_inactive",
                category=PERMANENT,
            )
            return event

        if not connection.access_token_encrypted:
            await self.mark_failed(event, "credential_unavailable", category=PERMANENT)
            return event

        try:
            access_token = decrypt(
                connection.access_token_encrypted,
                key=self._settings.meta_token_encryption_key,
            )
        except DecryptionError:
            # A wrong key or a tampered row. Retrying will not fix either.
            log.error(
                "meta_token_decrypt_failed",
                extra={
                    "meta_connection_id": str(connection.id),
                    "event_id": str(event.id),
                },
            )
            await self.mark_failed(event, "credential_unavailable", category=PERMANENT)
            return event

        for reference in references:
            await self._ingest_one(event, connection, access_token, reference)

        # Only claim success if every lead in the delivery landed. `_ingest_one` has
        # already marked the event FAILED if any of them did, and overwriting that with
        # PROCESSED would silently consume a lead nobody has: the event would never be
        # retried, and nothing would tell an operator. A retry is safe because the
        # unique constraint collapses the leads that already landed.
        if self._any_failed:
            return event

        await self.mark_processed(event, note=None)
        return event

    async def _handle_messaging(self, event: MetaWebhookEvent) -> bool:
        """Ingest a messaging event. Returns whether this event was one.

        `object` decides the surface, because that is the only place it appears: a Page
        webhook carries `"page"`, an Instagram business account `"instagram"`, and a
        WhatsApp Cloud API delivery `"whatsapp_business_account"`. An unknown object type is
        left to the caller, not guessed at.

        A Cloud API delivery can carry *both* customer messages and status reports about our
        own sends, so statuses are applied first and the messages second. Both are the same
        `messages` field change; reading only one of them would silently drop half of every
        WhatsApp delivery.
        """
        adapter = channel_for_payload(event.object_type)
        if adapter is None:
            return False

        await self._apply_provider_statuses(event)

        messages = extract_inbound_messages(event.payload)
        if not messages:
            # A messaging event with no usable message: a postback, a delivery receipt, a
            # typing indicator, or a WhatsApp delivery that was purely a status report.
            # Stored, accounted for, nothing created.
            await self.mark_processed(event, note=NO_MESSAGE)
            return True

        engine = ConversationEngine(
            self._session, self._tenant_id, graph=self._graph, settings=self._settings
        )
        try:
            stored = await engine.ingest_inbound(
                meta_connection_id=event.meta_connection_id,
                channel=adapter.channel,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 - a bad payload must not lose the event
            # Categorised, not swallowed: the event stays retryable and the category says
            # an operator what class of problem it is.
            log.warning(
                "messaging_ingest_failed",
                extra={
                    "event_id": str(event.id),
                    "channel": adapter.channel.value,
                    "error_type": type(exc).__name__,
                },
            )
            await self.mark_failed(event, "messaging_ingest_failed", category=RETRYABLE)
            return True

        # Handed to `run_ai_stage`, which is called *after* this event's transaction
        # commits. Captured here because the ids are only meaningful once the rows are
        # durable, and running the LLM call inside this transaction is the thing Phase 5
        # exists to avoid.
        self._last_inbound_ids = [m.id for m in stored]

        await self.mark_processed(event, note=None)
        return True

    async def _apply_provider_statuses(self, event: MetaWebhookEvent) -> int:
        """Record what the provider said about messages we sent. Returns how many applied.

        Phase 10-O.3. Only WhatsApp Cloud API deliveries carry these today; the function is
        a no-op for every other surface because `extract_whatsapp_statuses` returns nothing
        for them, so this does not become a second status path that later diverges.

        A status naming a message we do not have is *not* an error. A message can be sent
        and then have its row lost, and Meta will keep reporting on it; failing the event
        would make an infinite retry loop over a message that will never appear. It is
        logged and the delivery is still processed - the alternative, marking it failed,
        would stop legitimate traffic behind one unmatched id.
        """
        statuses = extract_whatsapp_statuses(event.payload)
        if not statuses:
            return 0

        applied = 0
        for status in statuses:
            message = await conversation_repo.get_message_by_provider_id(
                self._session, status.meta_message_id
            )
            if message is None:
                log.info(
                    "provider_status_unmatched",
                    extra={
                        "event_id": str(event.id),
                        "status": status.status,
                        "message_ref_present": True,
                    },
                )
                continue

            if status.status in _DELIVERED_STATUSES:
                await conversation_repo.record_provider_delivery(
                    self._session,
                    message,
                    at=status.occurred_at or datetime.now(UTC),
                )
            elif status.status == _FAILED_STATUS:
                # The provider's own code is logged, never stored: `failure_reason` is a
                # short category and Meta's error bodies can carry customer text.
                log.warning(
                    "provider_reported_failure",
                    extra={
                        "event_id": str(event.id),
                        "message_id": str(message.id),
                        "provider_error_code": status.error_code,
                    },
                )
                await conversation_repo.record_provider_failure(
                    self._session, message, reason=REASON_REJECTED
                )
            else:
                # `sent`, `read`, `deleted`, or something Meta adds later. Accounted for
                # and ignored rather than guessed at.
                log.debug(
                    "provider_status_ignored",
                    extra={"event_id": str(event.id), "status": status.status},
                )
                continue
            applied += 1
        return applied

    async def run_ai_stage(self) -> list[AIProcessingResult]:
        """Run AI qualification over the inbound messages of the last `handle`.

        **Must be called after the caller's transaction has committed.** This is a
        post-commit stage by construction, not by convention: the LLM call and the
        outbound send each take seconds, and running them inside the event's transaction
        would hold a connection and a row lock for the duration while a third party
        decides what to say to a lead.

        Calling it early is not an error, but it is close to useless: the service opens
        its own sessions and therefore cannot see the inbound message until the caller's
        transaction commits, so every message reports `inbound_message_not_found` and a
        warning is logged. Treat a stage returning `SKIPPED` as a bug in the caller.

        A no-op - returning `[]` - when no session factory was supplied, so a caller that
        only wants Phase 2/4 behaviour pays nothing and needs no branching of its own.

        Failures here never propagate. The webhook event is already PROCESSED, which is
        correct: the message *is* stored. An AI failure is recorded on its own run row and
        is retried on its own schedule, so failing here would be re-ingesting a message
        that is safely stored and would mark a healthy event broken.
        """
        if self._session_factory is None or not self._settings.ai_enabled:
            return []
        if not self._last_inbound_ids:
            return []

        # Phase 10-O.1: the tenant's own business identity and its AI switch, read once here
        # and threaded down. Previously the context provider was built with no knowledge of
        # the tenant at all, so every prompt shipped the "no business detail configured"
        # fallback regardless of what the workspace had configured.
        #
        # Read in its own short transaction, before the service is constructed: `get_context`
        # runs inside the AI pipeline's own transactions, so it cannot open one itself, and a
        # profile read here keeps the service constructor synchronous as it has always been.
        profile = None
        if self._session_factory is not None:
            async with self._session_factory() as session, session.begin():
                profile = await load_profile(session, self._tenant_id)

        service = AIConversationService(
            self._session_factory,
            self._tenant_id,
            graph=self._graph,
            settings=self._settings,
            tenant_ai_enabled=profile.ai_enabled if profile is not None else True,
            context_provider=build_context_provider(
                self._session_factory,
                self._tenant_id,
                settings=self._settings,
                business_name=profile.display_name if profile is not None else None,
                business_description=(profile.description if profile is not None else None),
            ),
        )
        results: list[AIProcessingResult] = []
        for message_id in self._last_inbound_ids:
            try:
                results.append(await service.process_inbound_message(message_id))
            except Exception:  # noqa: BLE001 - one bad message must not stop the batch
                log.exception(
                    "ai_stage_message_failed",
                    extra={"inbound_message_id": str(message_id)},
                )
        self._last_inbound_ids = []
        return results

    async def _ingest_one(
        self, event: MetaWebhookEvent, connection, access_token: str, reference
    ) -> None:
        """One leadgen_id. A failure here marks the event failed, never processed."""
        if self._graph is None:
            raise RuntimeError("MetaWebhookEventProcessor.handle needs a MetaGraphClient")

        # Re-assert the claim immediately before the network call, for the same reason the
        # follow-up worker renews before sending: everything above was local and fast, and
        # the call below is the part that can outlast the lease. If the claim is gone,
        # another worker is already handling this event and must be the only one that
        # fetches from Meta and writes the lead.
        if not await self.renew_claim(event):
            log.warning(
                "webhook_claim_lost_before_lead_retrieval",
                extra={
                    "event_id": str(event.id),
                    "meta_connection_id": str(connection.id),
                    "leadgen_id": reference.leadgen_id,
                },
            )
            return

        try:
            payload = await self._graph.get_lead(
                access_token=access_token, leadgen_id=reference.leadgen_id
            )
        except MetaAuthError:
            # The token is revoked or the permission was withdrawn. Retrying the same
            # token cannot help; an operator has to reconnect.
            log.warning(
                "lead_retrieval_unauthorized",
                extra={
                    "event_id": str(event.id),
                    "meta_connection_id": str(connection.id),
                    "leadgen_id": reference.leadgen_id,
                },
            )
            await self.mark_failed(event, "credential_rejected", category=PERMANENT)
            return
        except MetaApiError as exc:
            # Transient or a bad leadgen_id. Marking FAILED (not PROCESSED) is what
            # keeps the lead retrievable: the event is not consumed.
            log.warning(
                "lead_retrieval_failed",
                extra={
                    "event_id": str(event.id),
                    "leadgen_id": reference.leadgen_id,
                    "meta_error_code": exc.code,
                    "retryable": exc.retryable,
                },
            )
            await self.mark_failed(event, "lead_retrieval_failed", category=RETRYABLE)
            return

        try:
            normalized = normalize_lead(payload, reference=reference)
        except InvalidLeadPayload:
            log.warning(
                "lead_payload_invalid",
                extra={"event_id": str(event.id), "leadgen_id": reference.leadgen_id},
            )
            await self.mark_failed(event, "lead_payload_invalid", category=PERMANENT)
            return

        await set_current_tenant(self._session, self._tenant_id)
        lead, created = await lead_repo.insert_if_new(
            self._session,
            tenant_id=self._tenant_id,
            meta_connection_id=connection.id,
            lead=normalized,
        )
        log.info(
            "lead_persisted" if created else "lead_duplicate",
            extra={
                "event_id": str(event.id),
                "lead_id": str(lead.id),
                "leadgen_id": lead.meta_leadgen_id,
                "meta_connection_id": str(connection.id),
                "status": lead.status.value,
            },
        )

    async def mark_processed(self, event: MetaWebhookEvent, *, note: str | None = None) -> bool:
        """Record success. Returns whether this worker still owned the event.

        A `False` means the lease was reclaimed while this worker was busy - another worker
        owns the outcome now. That is not an error to retry and is deliberately not raised:
        the work itself was done and is idempotent, and the row already records the truth.
        It is logged, because "we did the work and someone else wrote the result" is exactly
        what an operator chasing a duplicate would want to see.
        """
        accepted = await event_repo.mark_processed(
            self._session, event.id, worker=self._worker, note=note
        )
        log.info(
            "webhook_processing_completed" if accepted else "webhook_processing_result_discarded",
            extra={
                "event_id": str(event.id),
                "meta_connection_id": str(event.meta_connection_id),
                "note": note,
                "attempt": event.attempt_count,
                "outcome": (
                    event_repo.MetaWebhookEventStatus.PROCESSED.value if accepted else "claim_lost"
                ),
            },
        )
        return accepted

    async def mark_failed(self, event: MetaWebhookEvent, reason: str, *, category: str) -> bool:
        # Set here rather than at each call site, so no failure path can forget it and
        # end up reporting overall success. Note this is set even when the write below is
        # refused: the work *did* fail, and `handle` must not report success on the strength
        # of a claim this worker no longer holds.
        self._any_failed = True
        accepted = await event_repo.mark_failed(
            self._session, event.id, worker=self._worker, error_message=f"{category}:{reason}"
        )
        log.warning(
            "webhook_processing_failed" if accepted else "webhook_processing_failure_discarded",
            extra={
                "event_id": str(event.id),
                "meta_connection_id": str(event.meta_connection_id),
                "reason": reason,
                "category": category,
                "attempt": event.attempt_count,
                "outcome": (
                    event_repo.MetaWebhookEventStatus.FAILED.value if accepted else "claim_lost"
                ),
            },
        )
        return accepted


async def active_tenants(factory: async_sessionmaker) -> Sequence[uuid.UUID]:
    """`tenants` is the isolation root and has no RLS, so this enumeration is safe."""
    async with factory() as session, session.begin():
        result = await session.execute(
            select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE).order_by(Tenant.id)
        )
        return tuple(result.scalars().all())


def event_summary(event: MetaWebhookEvent) -> dict[str, Any]:
    """Safe metadata for logs and future admin endpoints. Never the payload."""
    return {
        "event_id": str(event.id),
        "meta_connection_id": str(event.meta_connection_id),
        "object_type": event.object_type,
        "event_type": event.event_type,
        "meta_event_id": event.meta_event_id,
        "status": event.status.value,
    }


__all__ = ["MetaWebhookEventProcessor", "active_tenants", "event_summary"]
