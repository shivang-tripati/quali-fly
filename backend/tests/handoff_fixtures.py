"""Phase 8 fixtures: users, memberships, and the handoff service.

Kept in a module rather than in a test file because four test modules need the same
`make_handoff` / `member_user` / `service` helpers, and four copies of "create a user, add
them to the tenant, return the id" is four places for them to drift.

`make_handoff` deliberately goes through the *service* rather than inserting a row, so a
test that wants a handoff in a given state also gets the real side effects - the lead
marked, the follow-ups cancelled, the notification created. A hand-inserted handoff would
be a fixture that lies about the system it is standing in for.
"""

import uuid
from datetime import UTC, datetime

from app.core.clock import FixedClock
from app.core.config import get_settings
from app.db.tenant_context import set_current_tenant
from app.models.human_handoff import HandoffReason
from app.models.membership import Role
from app.services.handoffs import HumanHandoffService

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


def ho_settings(**overrides):
    """Settings for handoff tests.

    Mirrors `tests/follow_up_fixtures.fu_settings`: AI enhancement off, because these tests
    are about the state machine and none of them should depend on a provider being
    configured. `handoff_enabled` on, because a test that wanted it off would say so.
    """
    base = {
        "handoff_enabled": True,
        "follow_up_enabled": True,
        "follow_up_max_attempts": 3,
        "follow_up_retry_base_seconds": 60,
        "follow_up_processing_lease_seconds": 300,
        "follow_up_batch_size": 20,
        "follow_up_ghosted_days": 7,
        "follow_up_ai_enhance": False,
        "ai_enabled": True,
        "groq_api_key": "gsk-test",
        "ai_max_backoff_seconds": 0.01,
        "embedding_enabled": False,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def service(db, tenant_id, clock, **overrides) -> HumanHandoffService:
    return HumanHandoffService(db, tenant_id, settings=ho_settings(**overrides), clock=clock)


class _User:
    """`factories.create_user` returns a bare id; tests want a `.id` to pass around."""

    def __init__(self, user_id, email):
        self.id = user_id
        self.email = email


async def make_user(owner, *, role: Role = Role.MEMBER, tenant_id=None, name="Agent"):
    """A user, optionally already a member of `tenant_id`."""
    from tests.factories import add_member, create_user

    email = f"agent-{uuid.uuid4().hex[:10]}@example.test"
    user_id = await create_user(owner, email=email, name=name)
    if tenant_id is not None:
        await add_member(owner, tenant_id=tenant_id, user_id=user_id, role=role)
    return _User(user_id, email)


async def make_handoff(db, tenant_id, conversation, *, graph=None, reason=None, **overrides):
    """Create one real handoff for `conversation`, returning `(service, creation)`.

    Goes through `create_or_get_active_handoff` and then acknowledges, which is the exact
    sequence the AI pipeline runs, so tests that use this fixture exercise the real path
    including the outbound message.
    """
    graph = graph or _fake_graph()
    svc = service(db, tenant_id, clock=FixedClock(T0), **overrides)
    creation = await svc.create_or_get_active_handoff(
        conversation["conversation_id"],
        reason=reason or HandoffReason.HIGH_INTENT,
        detail="test handoff",
    )
    assert creation is not None and creation.created, "fixture could not create a handoff"
    conversation_model = await _load(db, tenant_id, conversation["conversation_id"])
    await svc.acknowledge(
        creation.handoff.id, conversation=conversation_model, graph=graph, already_delivered=False
    )
    return svc, creation, graph


def _fake_graph():
    from tests.fakes import FakeGraph

    return FakeGraph()


async def _load(db, tenant_id, conversation_id):
    from sqlalchemy import select

    from app.models.conversation import Conversation

    async with db() as session:
        await set_current_tenant(session, tenant_id)
        return await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.tenant_id == tenant_id
            )
        )


async def notifications_for(db, tenant_id, handoff_id):
    """Every notification on one handoff, for idempotency assertions."""
    from sqlalchemy import select

    from app.models.internal_notification import InternalNotification

    async with db() as session:
        await set_current_tenant(session, tenant_id)
        rows = (
            (
                await session.execute(
                    select(InternalNotification).where(
                        InternalNotification.tenant_id == tenant_id,
                        InternalNotification.handoff_id == handoff_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def active_handoff(db, tenant_id, conversation_id):
    from app.repositories import handoffs as handoff_repo

    async with db() as session:
        await set_current_tenant(session, tenant_id)
        return await handoff_repo.get_active(
            session, tenant_id=tenant_id, conversation_id=conversation_id
        )


async def count_active(db, tenant_id, conversation_id) -> int:
    from sqlalchemy import func, select

    from app.models.human_handoff import ACTIVE_HANDOFF_STATUSES, HumanHandoff

    async with db() as session:
        await set_current_tenant(session, tenant_id)
        return (
            await session.scalar(
                select(func.count())
                .select_from(HumanHandoff)
                .where(
                    HumanHandoff.tenant_id == tenant_id,
                    HumanHandoff.conversation_id == conversation_id,
                    HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
                )
            )
        ) or 0


async def handoff_count(db, tenant_id) -> int:
    from sqlalchemy import func, select

    from app.models.human_handoff import HumanHandoff

    async with db() as session:
        await set_current_tenant(session, tenant_id)
        return (
            await session.scalar(
                select(func.count())
                .select_from(HumanHandoff)
                .where(HumanHandoff.tenant_id == tenant_id)
            )
        ) or 0


__all__ = [
    "T0",
    "active_handoff",
    "count_active",
    "handoff_count",
    "ho_settings",
    "make_handoff",
    "make_user",
    "notifications_for",
    "service",
]
