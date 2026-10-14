"""Claiming and completing AI runs.

The repository exists for one reason: to make the unique constraint on
`ai_message_runs.inbound_message_id` reachable from application code. `claim` is the only
place a run row is created, and it is written as a single atomic statement.

`INSERT ... ON CONFLICT DO NOTHING ... RETURNING` is the whole mechanism, and it is
chosen over a read-then-write for a specific reason: under concurrency, two workers can
both read "no run exists" and both insert. The unique index makes the second insert a
no-op, `RETURNING` yields nothing, and the worker knows it lost the race. There is no
window between the check and the write for the race to land in, and no advisory lock to
hold across a provider call.

The loser then reads the winning row. That read is not incidental: it is how a worker
reports "already processed" instead of silently re-running the whole pipeline. It is also
how a *cross-tenant* collision is told apart from a duplicate delivery - see
`CrossTenantClaim`, which is the reason that read happens under the tenant's RLS policy
rather than as an unscoped primary-key lookup.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_message_run import AIMessageRun, AIRunStatus

log = logging.getLogger("app.ai.repository")


@dataclass(frozen=True)
class ClaimedRun:
    """Plain snapshot of the run that already owns a message.

    Deliberately not an `AIMessageRun`. A row returned from inside a transaction is
    expired on commit and detached once the session closes, so handing the ORM object to a
    caller invites a `DetachedInstanceError` at exactly the moment the caller wants to log
    what happened. These are the three values a caller actually needs.
    """

    run_id: uuid.UUID
    tenant_id: uuid.UUID
    status: AIRunStatus
    lead_id: uuid.UUID | None


class RunAlreadyClaimed(Exception):
    """Raised when this tenant already has a run for the message. Carries that run."""

    def __init__(self, existing: ClaimedRun) -> None:
        super().__init__("inbound message already has an AI run for this tenant")
        self.existing = existing


class CrossTenantClaim(Exception):
    """Raised when the unique conflict belongs to a *different* tenant.

    Worth its own exception because the two cases look identical to the database but mean
    opposite things. A message already claimed by this tenant is a routine duplicate
    delivery. A message claimed by another tenant is a security event: the caller holds
    another tenant's message id, and `ON CONFLICT DO NOTHING` would otherwise hide it
    behind a benign-looking "already processed" result.

    It is raised rather than returned because there is no correct way to continue. The
    right response is to fail the request loudly, not to skip a message that a different
    tenant owns.
    """

    def __init__(self, message_id: uuid.UUID) -> None:
        super().__init__(f"inbound message {message_id} is already claimed by another tenant")
        self.message_id = message_id


async def claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    inbound_message_id: uuid.UUID,
    lead_id: uuid.UUID | None,
) -> AIMessageRun:
    """Atomically claim `inbound_message_id` for AI processing.

    Returns the claimed run, or raises `RunAlreadyClaimed` / `CrossTenantClaim`.
    """
    stmt = (
        insert(AIMessageRun)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            inbound_message_id=inbound_message_id,
            lead_id=lead_id,
            status=AIRunStatus.IN_PROGRESS,
        )
        .on_conflict_do_nothing(constraint="uq_ai_message_runs_inbound_message")
        .returning(AIMessageRun.id)
    )
    claimed_id = (await session.execute(stmt)).scalar_one_or_none()
    if claimed_id is not None:
        run = await session.get(AIMessageRun, claimed_id)
        assert run is not None  # we just inserted it in this transaction
        return run

    # The insert lost. Read the winner back, scoped to this tenant so RLS decides what is
    # visible.
    existing = await get_by_inbound(session, tenant_id=tenant_id, message_id=inbound_message_id)
    if existing is None:
        # The conflicting row is real - the unique index fired - but invisible under this
        # tenant's RLS policy, so it belongs to someone else.
        raise CrossTenantClaim(inbound_message_id)
    raise RunAlreadyClaimed(
        ClaimedRun(
            run_id=existing.id,
            tenant_id=existing.tenant_id,
            status=existing.status,
            lead_id=existing.lead_id,
        )
    )


async def get_by_inbound(
    session: AsyncSession, *, tenant_id: uuid.UUID, message_id: uuid.UUID
) -> AIMessageRun | None:
    return await session.scalar(
        select(AIMessageRun).where(
            AIMessageRun.tenant_id == tenant_id,
            AIMessageRun.inbound_message_id == message_id,
        )
    )


async def mark_processed(
    session: AsyncSession,
    run: AIMessageRun,
    *,
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    latency_ms: int,
    qualification_total: int,
    high_intent: bool,
    human_handoff_required: bool,
    handoff_reason: str | None,
    ai_response: dict,
    outbound_message_id: uuid.UUID | None,
) -> AIMessageRun:
    run.status = AIRunStatus.PROCESSED
    run.provider = provider
    run.model = model
    run.prompt_tokens = prompt_tokens
    run.completion_tokens = completion_tokens
    run.latency_ms = latency_ms
    run.qualification_total = qualification_total
    run.high_intent = high_intent
    run.human_handoff_required = human_handoff_required
    run.handoff_reason = handoff_reason
    run.ai_response = ai_response
    run.outbound_message_id = outbound_message_id
    run.error_category = None
    run.completed_at = datetime.now(UTC)
    return run


async def mark_failed(
    session: AsyncSession, run: AIMessageRun, *, error_category: str
) -> AIMessageRun:
    """Record a failure durably.

    No provider or model is set on failure: a run that produced nothing has no model to
    attribute, and recording the one that errored would misattribute an outcome.
    """
    run.status = AIRunStatus.FAILED
    run.error_category = error_category[:64]
    run.completed_at = datetime.now(UTC)
    return run


async def mark_skipped(session: AsyncSession, run: AIMessageRun, *, reason: str) -> AIMessageRun:
    run.status = AIRunStatus.SKIPPED
    run.error_category = reason[:64]
    run.completed_at = datetime.now(UTC)
    return run
