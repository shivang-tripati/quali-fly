"""Scheduler lifecycle, owned by the application lifespan.

The recurring job is a 30-second `AsyncIOScheduler` interval that calls
`poll_all_tenants`. Three properties are deliberate:

**It is started and stopped by the lifespan, not at import time.** An import-time scheduler
would fire during tests and during Alembic runs, and would keep firing after the event loop
that owns it has gone.

**`coalesce` and `max_instances=1`.** If a poll overruns its interval - a slow Meta call
would do it - coalescing skips the missed ticks rather than stacking them, and
`max_instances=1` makes a second run impossible. A second run would be safe anyway thanks
to `SKIP LOCKED` claiming, but not stacking is strictly better than relying on that.

**`FOLLOW_UP_ENABLED=false` means no scheduler at all.** Not a scheduler that polls and
finds nothing: no scheduler. A deployment with follow-ups off should not be paying for a
30-second wake-up forever.

If the scheduler cannot start, that is logged and the application still serves traffic.
A follow-up engine that cannot start must not take the API down with it - inbound messages
and replies are worth more than the automation, and a restart will retry.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings
from app.db.session import get_session_factory
from app.services.follow_up_scheduler import poll_all_tenants

log = logging.getLogger("app.follow_up.lifecycle")

JOB_ID = "poll_pending_follow_up_tasks"


def build_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    """An unstarted scheduler with the poll job registered.

    Uses UTC explicitly. The job compares `scheduled_at` against UTC, and a scheduler
    running on a local timezone would fire at the wrong instants.
    """
    from app.core.config import get_settings

    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def _poll() -> None:
        from app.services.meta_graph import MetaGraphClient

        factory = get_session_factory()
        # A Graph client per poll, closed afterwards. The client is stateless apart from its
        # connection pool, and building one per poll means a token rotation is picked up
        # without a restart.
        async with MetaGraphClient() as graph:
            result = await poll_all_tenants(factory, settings=settings, graph=graph)
        if result.claimed:
            log.info(
                "follow_up.scheduler_tick",
                extra={
                    "claimed": result.claimed,
                    "completed": result.completed,
                    "failed": result.failed,
                    "skipped": result.skipped,
                },
            )

    scheduler.add_job(
        _poll,
        trigger=IntervalTrigger(seconds=settings.follow_up_poll_interval_seconds),
        id=JOB_ID,
        name="Poll pending follow-up tasks",
        replace_existing=True,
        # A slow poll must not queue up behind itself.
        coalesce=True,
        max_instances=1,
        # A crashed poll must not kill the schedule permanently.
        misfire_grace_time=settings.follow_up_poll_interval_seconds,
    )
    return scheduler


async def start_scheduler() -> AsyncIOScheduler | None:
    """Start the poller, unless follow-ups are disabled. Returns it, or `None`."""
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.follow_up_enabled:
        log.info("follow_up.scheduler_disabled")
        return None
    scheduler = build_scheduler(settings)
    try:
        scheduler.start()
    except Exception:  # noqa: BLE001 - the API must not depend on this starting
        log.exception("follow_up.scheduler_start_failed")
        return None
    log.info(
        "follow_up.scheduler_started",
        extra={
            "interval_seconds": settings.follow_up_poll_interval_seconds,
            "batch_size": settings.follow_up_batch_size,
        },
    )
    return scheduler


async def stop_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    if scheduler is None:
        return
    try:
        # `wait=False` on purpose: a poll in flight holds an AI or Meta call, and the
        # process is going away regardless. Waiting would delay every shutdown by up to a
        # provider timeout for no benefit - the lease recovers the task.
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        log.exception("follow_up.scheduler_stop_failed")
    else:
        log.info("follow_up.scheduler_stopped")


__all__ = ["JOB_ID", "build_scheduler", "start_scheduler", "stop_scheduler"]
