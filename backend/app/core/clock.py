"""Time, as an injected dependency.

Follow-up scheduling is the first part of this system whose *correctness* is a function of
the clock: "is this task due?", "is this window still open?", "has this lease expired?". If
those read `datetime.now(UTC)` directly, then the only way to test them is to wait four
hours, and the tests that would matter most - the cancellation race, the ghosted
boundary, lease recovery - are exactly the ones nobody is willing to run on a schedule.

So `Clock` is a two-member protocol, production uses the real one, and tests advance it by
hand. Every business decision in Phase 7 reads time from here rather than from the module
namespace.

Timezone note: everything is UTC, everywhere. The codebase already stores
`DateTime(timezone=True)` and Phase 4 anchored the 24-hour window on it. A follow-up
scheduled "24 hours" later means 24 hours in UTC, not in the server's local zone - a
container running with a local timezone must not shift a tenant's follow-up hours.
"""

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """The only source of time in the follow-up engine."""

    def now(self) -> datetime: ...


class SystemClock:
    """Real UTC time. The default everywhere outside tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A manually advanced clock, for tests.

    `worker_id` is included in `now()`'s resolution so that two pollers sharing one clock
    still produce distinguishable lease owners - otherwise a concurrency test would pass
    for the wrong reason, with both workers looking like the same process.
    """

    def __init__(self, start: datetime, worker_id: str = "test-worker") -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)
        self.worker_id = worker_id

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> datetime:
        """Move the clock forward. `timedelta` kwargs, e.g. `advance(hours=4)`."""
        from datetime import timedelta

        self._now = self._now + timedelta(**delta)
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
        return self._now


def worker_identity() -> str:
    """A short, stable-per-process id for `locked_by`.

    `hostname:pid` is deliberate: it is enough to tell two processes apart in a log, needs
    no coordination, and stops being meaningful only if the process is gone - which is the
    point of a lease.
    """
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"


def monotonic_seconds() -> float:
    """Monotonic time, for measuring durations.

    Never used for business decisions - a monotonic clock has no relationship to UTC and
    cannot answer "is this task due". It is only ever used to fill a `latency_ms`.
    """
    return time.monotonic()


__all__ = [
    "Clock",
    "FixedClock",
    "SystemClock",
    "monotonic_seconds",
    "worker_identity",
]
