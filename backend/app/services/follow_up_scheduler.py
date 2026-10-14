"""The poller. Deliberately thin.

    poll
      → claim a bounded batch      (one short transaction, lock released)
      → execute each task          (no transaction open; AI and Meta happen here)
      → per-task failures are contained and the batch continues

There is no business logic in this file, and that is the design rather than an omission.
The rules - when a task is eligible, what a follow-up may say, whether a send is safe -
belong to `FollowUpService`, which is testable without a scheduler. What is left here is
"wake up, find work, hand it over", and it must stay that small, because this is the code
that runs unattended in every deployment.

**APScheduler is a trigger, not the state.** The durable record of what is owed lives in
`scheduled_tasks`. Replacing this process loses the job registration and nothing else,
which is what makes a container restart a non-event. Nothing here is authoritative; if the
poller never runs, the rows are still there and still correct.

Claiming is safe with many workers because the repository uses
`FOR UPDATE SKIP LOCKED`: a second poller skips rows a first one has locked rather than
blocking behind them. Ten pollers over one due task produce one winner.
"""

import logging
import uuid
from dataclasses import dataclass, field

from app.core.clock import Clock, SystemClock, worker_identity
from app.core.config import Settings, get_settings
from app.db.tenant_context import set_current_tenant
from app.models.scheduled_task import TaskStatus
from app.repositories import scheduled_tasks as task_repo
from app.services.follow_ups import FollowUpOutcome, FollowUpService

log = logging.getLogger("app.follow_up.scheduler")


@dataclass
class PollResult:
    """What one poll achieved. Returned so tests and diagnostics can assert on it."""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    outcomes: list[FollowUpOutcome] = field(default_factory=list)


class FollowUpScheduler:
    """Claims and dispatches due tasks for one tenant."""

    def __init__(
        self,
        session_factory,
        tenant_id: uuid.UUID,
        *,
        clock: Clock | None = None,
        settings: Settings | None = None,
        registry=None,
        context_provider=None,
        graph=None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._settings = settings or get_settings()
        self._clock = clock or SystemClock()
        self._worker = worker_identity()
        self._registry = registry
        self._context = context_provider
        self._graph = graph

    def _service(self) -> FollowUpService:
        return FollowUpService(
            self._session_factory,
            self._tenant_id,
            clock=self._clock,
            registry=self._registry,
            context_provider=self._context,
            settings=self._settings,
            graph=self._graph,
            # The service must verify the lease belongs to the worker that claimed it, so it
            # needs the same identity `claim_due` wrote into `locked_by`.
            worker=self._worker,
        )

    async def poll_once(self) -> PollResult:
        """Claim a bounded batch and execute it. One poll, one pass."""
        result = PollResult()
        if not self._settings.follow_up_enabled:
            return result

        # 1. claim, in a short transaction that commits before anything external runs
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            claimed = await task_repo.claim_due(
                session,
                tenant_id=self._tenant_id,
                now=self._clock.now(),
                limit=self._settings.follow_up_batch_size,
                worker=self._worker,
                lease_seconds=self._settings.follow_up_processing_lease_seconds,
            )
        result.claimed = len(claimed)
        if not claimed:
            return result

        # 2. execute, with no transaction open
        service = self._service()
        for task_id in claimed:
            try:
                outcome = await service.execute(task_id)
            except Exception:  # noqa: BLE001 - one bad task must not stop the batch
                # The task stays PROCESSING and its lease will expire, so another poll
                # reclaims it. Deliberately not failed here: a crash is not a verdict on
                # whether the send happened, and guessing "failed" could lose a follow-up
                # that was actually sent.
                log.exception(
                    "follow_up.execute_crashed",
                    extra={
                        "tenant_id": str(self._tenant_id),
                        "task_id": str(task_id),
                    },
                )
                result.failed += 1
                continue
            result.outcomes.append(outcome)
            if outcome.status == "COMPLETED":
                result.completed += 1
            elif outcome.status == "FAILED":
                result.failed += 1
            else:
                result.skipped += 1

        log.info(
            "follow_up.poll_completed",
            extra={
                "tenant_id": str(self._tenant_id),
                "claimed": result.claimed,
                "completed": result.completed,
                "failed": result.failed,
                "skipped": result.skipped,
            },
        )
        return result


async def poll_all_tenants(
    session_factory,
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    graph=None,
) -> PollResult:
    """Poll every active tenant. This is what the recurring job calls.

    A scheduler is an application worker, not a privileged tenant bypass: it uses the same
    unprivileged role as the API, and it establishes each tenant's context before touching
    that tenant's rows. It has no way to read across tenants even in principle.
    """
    from sqlalchemy import select

    from app.models.tenant import Tenant, TenantStatus

    settings = settings or get_settings()
    total = PollResult()
    if not settings.follow_up_enabled:
        return total

    async with session_factory() as session, session.begin():
        # `tenants` is not RLS-scoped - it is the isolation root - so this read is the one
        # place the scheduler enumerates, and it takes only ids.
        tenant_ids = list(
            (await session.execute(select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE)))
            .scalars()
            .all()
        )
    for tenant_id in tenant_ids:
        scheduler = FollowUpScheduler(
            session_factory, tenant_id, clock=clock, settings=settings, graph=graph
        )
        try:
            one = await scheduler.poll_once()
        except Exception:  # noqa: BLE001 - one tenant's failure must not stop the rest
            log.exception("follow_up.tenant_poll_failed", extra={"tenant_id": str(tenant_id)})
            continue
        total.claimed += one.claimed
        total.completed += one.completed
        total.failed += one.failed
        total.skipped += one.skipped
        total.outcomes.extend(one.outcomes)
    return total


__all__ = ["FollowUpScheduler", "PollResult", "TaskStatus", "poll_all_tenants"]
