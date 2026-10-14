"""Tenant isolation for handoffs and notifications.

Every test here proves the *database* refuses the cross-tenant access, using the
unprivileged app role with forced RLS. An application-level `WHERE tenant_id = ?` that was
correct would pass these too - which is the point: RLS is the backstop that holds when the
application is wrong, and these tests are what make that backstop a claim rather than a
hope.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.clock import FixedClock
from app.db.tenant_context import set_current_tenant
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.internal_notification import InternalNotification, NotificationType
from app.models.membership import Role
from app.repositories import handoffs as handoff_repo
from app.repositories import notifications as notification_repo
from app.services.handoffs import HandoffNotFound, HumanHandoffService
from tests.factories import create_tenant
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import make_conversation
from tests.handoff_fixtures import T0, ho_settings, make_handoff, make_user


async def test_rls_is_enabled_and_forced_on_the_phase_8_tables(owner):
    async with owner() as session, session.begin():
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('human_handoffs', 'internal_notifications') ORDER BY relname"
                )
            )
        ).all()

    assert [r[0] for r in rows] == ["human_handoffs", "internal_notifications"]
    assert all(r[1] and r[2] for r in rows), "RLS must be enabled AND forced"


async def test_tenant_a_cannot_read_tenant_b_handoff(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)

    async with db() as session, session.begin():
        await set_current_tenant(session, acme)
        assert await handoff_repo.get(session, tenant_id=acme, handoff_id=globex_handoff) is None
        # And it is invisible in a listing, not merely unfetchable by id.
        rows = await handoff_repo.list_handoffs(session, tenant_id=acme, limit=50)
    assert [r.id for r in rows] == []


async def test_tenant_a_cannot_resolve_tenant_b_handoff(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)
    service = HumanHandoffService(db, acme, settings=ho_settings(), clock=FixedClock(T0))

    with pytest.raises(HandoffNotFound):
        await service.resolve(globex_handoff, note="sneaky")

    async with db() as session, session.begin():
        await set_current_tenant(session, globex)
        unchanged = await handoff_repo.get(session, tenant_id=globex, handoff_id=globex_handoff)
    assert unchanged.status is HandoffStatus.OPEN, "a cross-tenant resolve must change nothing"


async def test_tenant_a_cannot_claim_tenant_b_handoff(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)
    intruder = await make_user(owner, role=Role.MEMBER, tenant_id=acme)
    service = HumanHandoffService(db, acme, settings=ho_settings(), clock=FixedClock(T0))

    with pytest.raises(HandoffNotFound):
        await service.claim(globex_handoff, intruder.id)


async def test_tenant_a_cannot_assign_tenant_b_handoff(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)
    globex_agent = await make_user(owner, role=Role.MEMBER, tenant_id=globex)
    service = HumanHandoffService(db, acme, settings=ho_settings(), clock=FixedClock(T0))

    with pytest.raises(HandoffNotFound):
        await service.assign(globex_handoff, globex_agent.id)


async def test_a_handoff_cannot_be_created_for_another_tenants_conversation(db, owner):
    """The composite FK and the tenant predicate both refuse this."""
    acme = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    globex = await create_tenant(owner, name="Globex", slug=f"globex-{uuid.uuid4().hex[:8]}")
    globex_world = await make_conversation(owner, globex)
    service = HumanHandoffService(db, acme, settings=ho_settings(), clock=FixedClock(T0))

    result = await service.create_or_get_active_handoff(
        globex_world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert result is None
    async with db() as session, session.begin():
        await set_current_tenant(session, acme)
        assert await handoff_repo.list_handoffs(session, tenant_id=acme, limit=10) == []


async def test_tenant_a_cannot_read_tenant_b_notification(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)

    async with db() as session, session.begin():
        await set_current_tenant(session, globex)
        notes = await notification_repo.list_notifications(
            session, tenant_id=globex, handoff_id=globex_handoff
        )
    assert len(notes) == 1
    notification_id = notes[0].id

    async with db() as session, session.begin():
        await set_current_tenant(session, acme)
        assert (
            await notification_repo.get(session, tenant_id=acme, notification_id=notification_id)
            is None
        )
        assert await notification_repo.list_notifications(session, tenant_id=acme, limit=50) == []


async def test_tenant_a_cannot_mark_tenant_b_notification_read(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)
    async with db() as session, session.begin():
        await set_current_tenant(session, globex)
        notes = await notification_repo.list_notifications(
            session, tenant_id=globex, handoff_id=globex_handoff
        )
    notification_id = notes[0].id

    async with db() as session, session.begin():
        await set_current_tenant(session, acme)
        changed = await notification_repo.mark_read(
            session, tenant_id=acme, notification_id=notification_id, now=T0
        )

    assert changed is False
    async with db() as session, session.begin():
        await set_current_tenant(session, globex)
        reread = await session.get(InternalNotification, notification_id)
    assert reread.status.value == "UNREAD"


async def test_a_notification_cannot_be_attached_to_another_tenants_handoff(db, owner):
    """Defence in depth behind the API: the composite FK refuses the row outright."""
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    globex_handoff = await _handoff_id(db, globex, globex_world)

    async with db() as session, session.begin():
        await set_current_tenant(session, acme)
        try:
            session.add(
                InternalNotification(
                    tenant_id=acme,
                    handoff_id=globex_handoff,
                    conversation_id=acme_world["conversation_id"],
                    lead_id=None,
                    type=NotificationType.HANDOFF_CREATED,
                    title="smuggled",
                    message="smuggled",
                    payload={},
                )
            )
            await session.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError("a notification crossed a tenant boundary")

    assert True


async def test_has_active_is_false_across_tenants(db, owner):
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    await _handoff_id(db, globex, globex_world)

    acme_service = HumanHandoffService(db, acme, settings=ho_settings(), clock=FixedClock(T0))
    assert await acme_service.has_active(globex_world["conversation_id"]) is False


# -- helpers -----------------------------------------------------------------


async def _pair(owner, db):
    acme = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    globex = await create_tenant(owner, name="Globex", slug=f"globex-{uuid.uuid4().hex[:8]}")
    return (
        acme,
        globex,
        await make_conversation(owner, acme),
        await make_conversation(owner, globex),
    )


async def _handoff_id(db, tenant_id, world):
    graph = FakeGraph()
    _, creation, _ = await make_handoff(db, tenant_id, world, graph=graph)
    return creation.handoff.id


async def test_a_globex_agent_cannot_claim_an_acme_handoff_even_with_the_right_id(db, owner):
    """Both halves of the guarantee: the id is real, the tenant is not."""
    acme, globex, acme_world, globex_world = await _pair(owner, db)
    acme_handoff = await _handoff_id(db, acme, acme_world)
    globex_agent = await make_user(owner, role=Role.MEMBER, tenant_id=globex)
    service = HumanHandoffService(db, globex, settings=ho_settings(), clock=FixedClock(T0))

    with pytest.raises(HandoffNotFound):
        await service.claim(acme_handoff, globex_agent.id)
