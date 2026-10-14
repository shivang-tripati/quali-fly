"""Phase 10-O.1 §4: the human-reply authorization correction.

The audit found an internal contradiction in the product's own permission model:

    POST /handoffs/{id}/claim    ->  MEMBER   (api/handoffs.py, MemberCtx)
    POST /conversations/{id}/messages  ->  ADMIN (was SendCtx, now MEMBER)

So the role the handoff queue is designed for could take ownership of a customer and then be
refused the only endpoint that answers them. These tests pin the corrected model and, just as
importantly, prove the correction did not open a hole: everything here is about a membership
in one tenant, and a cross-tenant attempt must still fail.
"""

import uuid

import pytest
from sqlalchemy import select

from app.api.deps import TENANT_HEADER
from app.core.errors import ErrorCode
from app.models.conversation import Channel, Conversation, Message, MessageDirection
from app.models.membership import Role
from app.models.user import User
from tests.factories import add_member, create_tenant, create_user, issue_session

SESSION_COOKIE = "qf_session"


@pytest.fixture
async def world(owner):
    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    other = await create_tenant(owner, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    owner_user = await create_user(owner, email="owner@acme.test", name="Olive Owner")
    admin = await create_user(owner, email="admin@acme.test", name="Ada Admin")
    member = await create_user(owner, email="member@acme.test", name="Milo Member")
    outsider = await create_user(owner, email="outsider@other.test", name="Otto Outsider")
    await add_member(owner, tenant_id=tenant, user_id=owner_user, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant, user_id=admin, role=Role.ADMIN)
    await add_member(owner, tenant_id=tenant, user_id=member, role=Role.MEMBER)
    await add_member(owner, tenant_id=other, user_id=outsider, role=Role.OWNER)
    return {
        "tenant": tenant,
        "other": other,
        "owner": owner_user,
        "admin": admin,
        "member": member,
        "outsider": outsider,
    }


async def _connection_with_token(owner, tenant_id):
    """A connection carrying a real encrypted page token.

    Without one, `ConversationEngine._credential_for` refuses before it ever reaches the role
    the send happens under, and the test would be measuring the wrong refusal. The same
    shape as the control-plane fixture, for the same reason.
    """
    from app.core.crypto import encrypt
    from app.models.meta_connection import MetaConnection
    from tests.conftest import TEST_META_TOKEN_KEY
    from tests.fakes import PAGE_TOKEN

    async with owner() as session, session.begin():
        connection = MetaConnection(
            tenant_id=tenant_id,
            page_id=f"8{uuid.uuid4().hex[:15]}",
            access_token_encrypted=encrypt(PAGE_TOKEN, key=TEST_META_TOKEN_KEY),
            connected_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        session.add(connection)
        await session.flush()
        return connection.id


async def _conversation(owner, tenant_id, *, channel=Channel.MESSENGER):
    """A real thread with a real, token-carrying connection.

    `owner` is the *privileged* factory: it bypasses the RLS the app role is subject to,
    which is what a fixture needs in order to build rows nobody else can.
    """
    connection = await _connection_with_token(owner, tenant_id)
    async with owner() as session, session.begin():
        conversation = Conversation(
            tenant_id=tenant_id,
            meta_connection_id=connection,
            channel=channel,
            meta_participant_id=f"PSID-{uuid.uuid4().hex[:10]}",
            last_inbound_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        session.add(conversation)
        await session.flush()
        return conversation.id


async def _sign_in(api, owner, user_id, tenant_id):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=user_id))
    return {TENANT_HEADER: str(tenant_id)}


# ------------------------------------------------------------ the corrected bar


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
async def test_every_member_of_the_workspace_can_reply_to_a_customer(api, owner, world, role):
    """OWNER, ADMIN and MEMBER may all send. The gap this closes was MEMBER being refused."""
    headers = await _sign_in(api, owner, world[role], world["tenant"])
    conversation_id = await _conversation(owner, world["tenant"])

    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages", json={"text": "hello"}, headers=headers
    )

    # The send cannot actually reach Meta in this environment, so the outcome is asserted on
    # the *authorization* outcome - it must not be 401 or 403 - rather than on delivery, which
    # is a separate integration concern this phase does not claim.
    assert response.status_code not in (401, 403), (
        f"{role} was refused the human-reply endpoint: {response.text}"
    )
    assert response.status_code == 201, response.text


async def test_a_member_can_reply_over_both_supported_channels(api, owner, world):
    """Channel-independent, which is the property a WhatsApp adapter will depend on."""
    headers = await _sign_in(api, owner, world["member"], world["tenant"])

    for channel in (Channel.MESSENGER, Channel.INSTAGRAM):
        conversation_id = await _conversation(owner, world["tenant"], channel=channel)
        response = await api.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"text": f"hello via {channel.value}"},
            headers=headers,
        )
        assert response.status_code == 201, f"{channel.value}: {response.text}"


async def test_a_member_reply_is_recorded_against_the_conversation(api, owner, world):
    """The reply really happens: a persisted OUTBOUND row with the operator's text.

    Proves the call reached the conversation engine rather than being waved through by the
    permission change.
    """
    headers = await _sign_in(api, owner, world["member"], world["tenant"])
    conversation_id = await _conversation(owner, world["tenant"])

    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "happy to help"},
        headers=headers,
    )
    assert response.status_code == 201, response.text

    async with owner() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].text == "happy to help"
    # No AI attribution: this was a person, not the model.
    assert rows[0].ai_model is None


# ------------------------------------------------------- the hole must stay closed


async def test_a_non_member_cannot_reply(api, owner, world):
    """The correction lowered the role bar, not the membership requirement."""
    await _sign_in(api, owner, world["outsider"], world["other"])
    conversation_id = await _conversation(owner, world["tenant"])

    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "let me in"},
        headers={TENANT_HEADER: str(world["tenant"])},
    )

    # The header claims Acme; the session is an OWNER of Other. TenantContext must refuse
    # before anything else happens.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.TENANT_ACCESS_DENIED


async def test_an_unauthenticated_caller_cannot_reply(api, owner, world):
    conversation_id = await _conversation(owner, world["tenant"])

    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "hello"},
        headers={TENANT_HEADER: str(world["tenant"])},
    )

    assert response.status_code == 401


async def test_a_member_cannot_reply_into_another_tenants_thread(api, owner, world):
    """Asymmetric membership, not just a wrong header.

    A user who is OWNER of Acme and MEMBER of Other must not be able to send into Acme's
    threads by selecting Other as the tenant and passing an Acme conversation id.
    """
    await add_member(owner, tenant_id=world["other"], user_id=world["owner"], role=Role.MEMBER)
    conversation_id = await _conversation(owner, world["tenant"])

    headers = await _sign_in(api, owner, world["owner"], world["other"])
    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "cross tenant"},
        headers=headers,
    )

    assert response.status_code == 404, response.text
    async with owner() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "a message was written into another tenant's conversation"


async def test_an_inactive_user_cannot_reply(api, owner, world):
    """`is_active` is checked during authentication, before any role check."""
    headers = await _sign_in(api, owner, world["member"], world["tenant"])
    conversation_id = await _conversation(owner, world["tenant"])
    async with owner() as session, session.begin():
        user = (await session.execute(select(User).where(User.id == world["member"]))).scalar_one()
        user.is_active = False

    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "hello"},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.USER_INACTIVE


async def test_replying_to_an_unknown_conversation_is_a_404_not_a_leak(api, owner, world):
    headers = await _sign_in(api, owner, world["member"], world["tenant"])

    response = await api.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"text": "hello"},
        headers=headers,
    )

    assert response.status_code == 404


async def test_an_empty_message_is_refused_before_any_send(api, owner, world):
    """Pre-existing validation, re-asserted because the role bar moved.

    The refusal comes from the conversation engine rather than the schema - a
    whitespace-only body normalises to nothing and is refused as unsendable - so the assertion
    is on "refused, and nothing was written", not on a specific status code.
    """
    headers = await _sign_in(api, owner, world["member"], world["tenant"])
    conversation_id = await _conversation(owner, world["tenant"])

    response = await api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": "   "},
        headers=headers,
    )

    assert response.status_code in (409, 422), response.text
    async with owner() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "an empty message still produced a persisted attempt"


async def test_a_member_still_cannot_reach_admin_only_conversation_endpoints(api, owner, world):
    """Lowering the send bar must not have lowered anything else.

    If `SendCtx` had been reused for a mutating admin endpoint by accident, this would catch
    it. It is the regression guard for the fix itself.
    """
    headers = await _sign_in(api, owner, world["member"], world["tenant"])
    conversation_id = await _conversation(owner, world["tenant"])

    for method, path in (
        ("PUT", f"/api/v1/conversations/{conversation_id}"),
        ("DELETE", f"/api/v1/conversations/{conversation_id}"),
    ):
        response = await api.request(method, path, headers=headers, json={})
        assert response.status_code in (403, 404, 405), (
            f"{method} {path} answered {response.status_code} for a MEMBER"
        )


async def test_handoff_operations_remain_open_to_a_member(api, owner, world):
    """The premise of the fix: a MEMBER is expected to work the handoff queue.

    Asserted so the two halves stay consistent. If handoff claiming ever becomes admin-only,
    this fails and the send bar should be revisited with it - rather than the two drifting
    apart again.
    """
    headers = await _sign_in(api, owner, world["member"], world["tenant"])

    listing = await api.get("/api/v1/handoffs", headers=headers)
    members = await api.get("/api/v1/tenant/members", headers=headers)

    assert listing.status_code == 200, "a MEMBER cannot list handoffs"
    assert members.status_code == 403, "a MEMBER can list tenant members, which is admin work"
