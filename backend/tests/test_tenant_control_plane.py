"""Phase 10-O.1: the tenant control plane.

Every test here runs against real PostgreSQL as the unprivileged application role, because
the properties under test are properties of the database: a column default that decides
whether live automation survives an upgrade, RLS deciding whose tenant a row belongs to, a
transaction either committing both halves of a member or neither, and the follow-up engine
reading a document the API wrote.

The two properties that matter most and are easiest to fake:

  * `test_the_tenant_switch_is_actually_read_by_the_engine` - a follow-up API that saves to a
    column nothing reads would pass every persistence test and change nothing.
  * `test_the_business_description_reaches_the_prompt` - the audit's headline finding was a
    column-shaped hole, so the test asserts on the rendered prompt text, not on the column.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api.deps import TENANT_HEADER
from app.core.errors import ErrorCode
from app.db.tenant_context import set_current_tenant
from app.models.conversation import Conversation
from app.models.membership import Role, TenantMembership
from app.models.scheduled_task import ScheduledTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai_prompt import PromptBuilder
from app.services.follow_up_templates import ALLOWED_PLACEHOLDERS, load_sequence
from app.services.tenant_settings import (
    AI_UNAVAILABLE_DISABLED,
    AI_UNAVAILABLE_NO_PROVIDER,
    ai_status,
    load_profile,
    platform_ai_available,
)
from tests.factories import TEST_PASSWORD, add_member, create_tenant, create_user, issue_session

SESSION_COOKIE = "qf_session"


def fu_settings(**overrides):
    from app.core.config import get_settings

    base = {"follow_up_enabled": True}
    base.update(overrides)
    return get_settings().model_copy(update=base)


def ai_settings(**overrides):
    from app.core.config import get_settings

    base = {"ai_enabled": True, "groq_api_key": "gsk-test", "ai_max_backoff_seconds": 0.01}
    base.update(overrides)
    return get_settings().model_copy(update=base)


@pytest.fixture
async def world(owner):
    """A tenant with an owner and a plain member, both able to authenticate."""
    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    owner_user = await create_user(owner, email="owner@acme.test", name="Olive Owner")
    member = await create_user(owner, email="member@acme.test", name="Milo Member")
    outsider = await create_user(owner, email="outsider@other.test", name="Otto Outsider")
    other = await create_tenant(owner, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    await add_member(owner, tenant_id=tenant, user_id=owner_user, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant, user_id=member, role=Role.MEMBER)
    await add_member(owner, tenant_id=other, user_id=outsider, role=Role.OWNER)
    return {
        "tenant": tenant,
        "owner": owner_user,
        "member": member,
        "outsider": outsider,
        "other": other,
    }


async def signed_in(api, owner, user_id, tenant_id):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=user_id))
    return {TENANT_HEADER: str(tenant_id)}


# ============================================================ business profile


async def test_a_new_tenant_has_no_business_description_and_says_so(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])

    response = await api.get("/api/v1/tenant/profile")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["business_name"] is None
    assert body["business_description"] is None
    # `resolved_name` is what the AI is actually told, so a tenant can see it rather than
    # having to infer that an unset business_name falls back to the workspace name.
    assert body["resolved_name"] == "Acme"
    assert body["has_description"] is False


async def test_a_tenant_can_set_and_read_back_its_business(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])

    saved = await api.put(
        "/api/v1/tenant/profile",
        json={
            "business_name": "Acme Auto Sales",
            "business_description": "We sell used cars. Open 9-6, we answer fast.",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["resolved_name"] == "Acme Auto Sales"
    assert saved.json()["has_description"] is True

    # And it is genuinely persisted, not echoed from the request.
    async with owner() as session, session.begin():
        stored = (
            await session.execute(select(Tenant).where(Tenant.id == world["tenant"]))
        ).scalar_one()
    assert stored.business_name == "Acme Auto Sales"
    assert "used cars" in stored.business_description


async def test_a_partial_update_leaves_the_other_field_alone(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    await api.put(
        "/api/v1/tenant/profile",
        json={"business_name": "Acme Motors", "business_description": "We sell cars."},
    )

    response = await api.put("/api/v1/tenant/profile", json={"business_name": "Acme Auto"})

    assert response.status_code == 200
    assert response.json()["business_name"] == "Acme Auto"
    assert response.json()["business_description"] == "We sell cars."


async def test_a_tenant_can_clear_its_description(api, owner, world):
    """Clearing is a real action, and a blank string must not read as an answer."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    await api.put("/api/v1/tenant/profile", json={"business_description": "We sell cars."})

    response = await api.put("/api/v1/tenant/profile", json={"business_description": None})

    assert response.status_code == 200
    assert response.json()["business_description"] is None
    assert response.json()["has_description"] is False

    # Whitespace-only is the same as absent, and must not become an empty claim.
    blank = await api.put("/api/v1/tenant/profile", json={"business_description": "   "})
    assert blank.json()["has_description"] is False


async def test_business_profile_rejects_unknown_fields(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.put("/api/v1/tenant/profile", json={"ai_enabled": False})
    assert response.status_code == 422


# ==================================================== business profile -> AI prompt


async def test_the_business_description_reaches_the_prompt(api, owner, world):
    """The audit's headline finding, asserted on what the model would be told.

    Not asserted on the column: a column is not delivery. This renders the actual
    `BusinessContext` the pipeline builds and checks the fallback sentence is gone.
    """
    from app.services.ai_prompt import StaticContextProvider

    await signed_in(api, owner, world["owner"], world["tenant"])
    await api.put(
        "/api/v1/tenant/profile",
        json={"business_name": "Acme Auto Sales", "business_description": "We sell used cars."},
    )

    async with owner() as session, session.begin():
        profile = await load_profile(session, world["tenant"])

    # The RAG-off branch, which is the one production actually runs today.
    provider = StaticContextProvider(
        business_name=profile.display_name,
        business_description=profile.description,
    )
    rendered = (await provider.get_context(world["tenant"])).render()

    assert "Acme Auto Sales" in rendered
    assert "We sell used cars." in rendered
    assert "No service or product detail has been configured" not in rendered


async def test_the_prompt_fallback_returns_when_a_tenant_has_said_nothing():
    """The honest-fallback requirement: the old sentence must still exist for new tenants."""
    from app.services.ai_prompt import StaticContextProvider

    context = await StaticContextProvider().get_context(uuid.uuid4())

    assert "No service or product detail has been configured" in context.render()


async def test_the_prompt_keeps_its_workspace_name_fallback(api, owner, world):
    """A tenant that never sets `business_name` is still named, via the workspace name."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    await api.put("/api/v1/tenant/profile", json={"business_description": "We sell boats."})

    async with owner() as session, session.begin():
        profile = await load_profile(session, world["tenant"])

    assert profile.business_name is None
    assert profile.display_name == "Acme"


async def test_knowledge_and_business_description_coexist_in_one_prompt():
    """Both halves of the context, because the audit found the RAG branch dropped identity."""
    from app.services.ai_prompt import StaticContextProvider

    provider = StaticContextProvider(
        business_name="Acme Auto",
        business_description="We sell used cars.",
    )
    context = await provider.get_context(uuid.uuid4())
    request = PromptBuilder(history_limit=10, max_prompt_chars=12000).build(
        context=context, lead_summary=None, history=[], current_text="do you have any SUVs?"
    )
    rendered = f"{request.system_prompt}\n{request.user_prompt}"

    assert "<business_context>" in rendered
    assert "Acme Auto" in rendered
    assert "used cars" in rendered
    assert "No service or product detail has been configured" not in rendered


# ================================================================ AI configuration


async def test_a_new_tenant_reports_ai_enabled_by_default(api, owner, world):
    """The tenant column defaults to on; the *platform* half is asserted separately below.

    `ai_enabled=True` on the row is the deliberate backfill choice: a tenant that predates
    the column was governed entirely by the platform flag, so defaulting it false would
    silently switch off live automation on upgrade.
    """
    await signed_in(api, owner, world["owner"], world["tenant"])

    async with owner() as session, session.begin():
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == world["tenant"]))
        ).scalar_one()
    assert tenant.ai_enabled is True

    response = await api.get("/api/v1/tenant/ai")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_enabled"] is True
    # The test environment has no provider key, so the honest combined state is
    # `unavailable` even though the tenant wants AI on. That is the whole point of the
    # tri-state, and asserting it here keeps the two halves from being conflated again.
    assert body["state"] == "unavailable"
    assert body["platform_available"] is False
    assert body["active"] is False
    # The reason names a *platform* condition, never the tenant's own choice - which is the
    # distinction the whole tri-state exists to make. The test environment has neither the
    # master switch nor a provider key, so either platform reason is the honest answer.
    assert body["unavailable_reason"] in {
        AI_UNAVAILABLE_DISABLED,
        AI_UNAVAILABLE_NO_PROVIDER,
    }


async def test_the_tenant_switch_and_the_platform_switch_are_separate(api, owner, world):
    """The tri-state, exercised on all three axes.

    This is the whole point of the change: "the deployment cannot do this" and "you turned it
    off" are different sentences, and a tenant who conflates them discovers the truth by
    watching leads go unanswered.
    """
    from app.core.config import get_settings

    await signed_in(api, owner, world["owner"], world["tenant"])

    disabled_deployment = get_settings().model_copy(update={"ai_enabled": False})
    status = ai_status(disabled_deployment, tenant_ai_enabled=True)
    assert status["state"] == "unavailable"
    assert status["active"] is False
    assert status["platform_available"] is False
    assert status["unavailable_reason"] == "ai_disabled_for_this_deployment"

    no_credential = get_settings().model_copy(
        update={"ai_enabled": True, "groq_api_key": None, "gemini_enabled": False}
    )
    status = ai_status(no_credential, tenant_ai_enabled=True)
    assert status["state"] == "unavailable"
    assert status["unavailable_reason"] == "no_ai_provider_configured"

    ready = get_settings().model_copy(update={"ai_enabled": True, "groq_api_key": "gsk-x"})
    assert ai_status(ready, tenant_ai_enabled=False)["state"] == "disabled"
    assert ai_status(ready, tenant_ai_enabled=True)["state"] == "enabled"


def test_platform_ai_available_requires_an_actual_key():
    """A configured provider with no key is not a working provider."""
    from app.core.config import get_settings

    base = get_settings()
    assert platform_ai_available(base.model_copy(update={"ai_enabled": False}))[0] is False
    assert (
        platform_ai_available(
            base.model_copy(
                update={
                    "ai_enabled": True,
                    "ai_primary_provider": "groq",
                    "groq_api_key": None,
                    "gemini_enabled": False,
                    "gemini_api_key": None,
                }
            )
        )[0]
        is False
    )
    assert (
        platform_ai_available(
            base.model_copy(update={"ai_enabled": True, "groq_api_key": "gsk-live"})
        )[0]
        is True
    )
    # A gemini-primary deployment with no gemini key falls back to groq if that has one.
    assert (
        platform_ai_available(
            base.model_copy(
                update={
                    "ai_enabled": True,
                    "ai_primary_provider": "gemini",
                    "gemini_api_key": None,
                    "groq_api_key": "gsk-live",
                }
            )
        )[0]
        is True
    )


async def test_a_tenant_can_turn_its_own_ai_off_without_touching_another_tenant(api, owner, world):
    """The whole reason the column exists: one workspace opts out, nobody else is affected."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    assert (await api.put("/api/v1/tenant/ai", json={"enabled": False})).status_code == 200

    async with owner() as session, session.begin():
        mine = (
            await session.execute(select(Tenant).where(Tenant.id == world["tenant"]))
        ).scalar_one()
        theirs = (
            await session.execute(select(Tenant).where(Tenant.id == world["other"]))
        ).scalar_one()
    assert mine.ai_enabled is False
    assert theirs.ai_enabled is True


async def test_the_ai_api_exposes_no_provider_secret(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.get("/api/v1/tenant/ai")
    text = response.text.lower()
    for leak in ("api_key", "apikey", "secret", "model", "groq", "gemini", "temperature"):
        assert leak not in text, f"{leak} leaked in the AI configuration response"


async def test_an_unknown_ai_field_is_rejected(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    assert (await api.put("/api/v1/tenant/ai", json={"provider": "openai"})).status_code == 422


# ============================================================ follow-up configuration


async def test_a_new_tenant_sees_the_built_in_follow_up_sequence(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])

    response = await api.get("/api/v1/tenant/follow-ups")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_default"] is True
    assert [s["step"] for s in body["steps"]] == [1, 2, 3]
    assert [s["delay_hours"] for s in body["steps"]] == [4, 24, 72]
    assert body["enabled"] is True


async def test_a_tenant_can_save_and_read_back_its_sequence(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])

    saved = await api.put(
        "/api/v1/tenant/follow-ups",
        json={
            "steps": [
                {
                    "step": 1,
                    "delay_hours": 2,
                    "template": "Hi {first_name}, any news?",
                    "ai_enhance": False,
                },
                {"step": 2, "delay_hours": 48, "template": "Still thinking about it?"},
            ]
        },
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["is_default"] is False
    assert [(s["step"], s["delay_hours"], s["ai_enhance"]) for s in body["steps"]] == [
        (1, 2, False),
        (2, 48, True),
    ]


async def test_a_tenant_can_switch_follow_ups_off(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.put("/api/v1/tenant/follow-ups", json={"enabled": False})

    assert response.status_code == 200
    # `enabled` is the tenant's own switch and is what this call changed.
    assert response.json()["enabled"] is False
    # The deployment flag is also off in the test environment, so the honest combined state is
    # `unavailable` rather than `disabled`. Asserting `disabled` here would be asserting that
    # the platform half works, which is a different test and is covered below.
    assert response.json()["state"]["state"] in {"disabled", "unavailable"}
    assert response.json()["state"]["active"] is False


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"steps": []}, "a sequence with no steps"),
        (
            {"steps": [{"step": 1, "delay_hours": 0, "template": "x"}]},
            "a zero delay",
        ),
        (
            {"steps": [{"step": 1, "delay_hours": 99999, "template": "x"}]},
            "a delay past the maximum",
        ),
        ({"steps": [{"step": 1, "delay_hours": 4, "template": ""}]}, "an empty template"),
        (
            {"steps": [{"step": 1, "delay_hours": 4, "template": "hi {{ config }}"}]},
            "a template with executable markers",
        ),
        (
            {
                "steps": [
                    {"step": 1, "delay_hours": 4, "template": "a"},
                    {"step": 1, "delay_hours": 5, "template": "b"},
                ]
            },
            "a duplicated step number",
        ),
        (
            {"steps": [{"step": 1, "delay_hours": 4, "template": "a", "unexpected": 1}]},
            "an unknown field on a step",
        ),
    ],
)
async def test_invalid_follow_up_config_is_rejected(api, owner, world, payload, why):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.put("/api/v1/tenant/follow-ups", json=payload)
    assert response.status_code == 422, f"{why} was accepted: {response.text}"


async def test_a_rejected_config_does_not_partially_apply(api, owner, world):
    """All-or-nothing: a valid step 1 followed by an invalid step 2 must save neither."""
    await signed_in(api, owner, world["owner"], world["tenant"])

    await api.put(
        "/api/v1/tenant/follow-ups",
        json={
            "steps": [
                {"step": 1, "delay_hours": 2, "template": "first"},
                {"step": 2, "delay_hours": 0, "template": "second"},
            ]
        },
    )

    async with owner() as session, session.begin():
        stored = (
            await session.execute(select(Tenant).where(Tenant.id == world["tenant"]))
        ).scalar_one()
    assert stored.follow_up_config == {}, "a rejected config left rows behind"


async def test_a_saved_sequence_is_readable_by_the_engine_that_consumes_it(api, owner, world):
    """The test that matters: the document the API writes is the document the engine reads.

    A config API that writes a shape `load_sequence` cannot parse would pass every
    persistence assertion and silently fall back to defaults at run time.
    """
    await signed_in(api, owner, world["owner"], world["tenant"])
    await api.put(
        "/api/v1/tenant/follow-ups",
        json={
            "steps": [
                {
                    "step": 1,
                    "delay_hours": 7,
                    "template": "Custom one {first_name}",
                    "ai_enhance": False,
                },
                {"step": 2, "delay_hours": 13, "template": "Custom two"},
            ]
        },
    )

    async with owner() as session, session.begin():
        stored = (
            await session.execute(select(Tenant).where(Tenant.id == world["tenant"]))
        ).scalar_one()
        config = stored.follow_up_config

    sequence = load_sequence(config)
    assert sequence.is_default is False
    assert sequence.final_step == 2
    assert sequence.step(1).delay_hours == 7
    assert sequence.step(1).template == "Custom one {first_name}"
    assert sequence.step(1).ai_enhance is False
    assert sequence.step(2).delay_hours == 13
    # And the only placeholder the renderer supports is still the only one that works.
    assert ALLOWED_PLACEHOLDERS == frozenset({"first_name"})


async def test_the_tenant_switch_is_actually_read_by_the_engine(db, owner, world, clock):
    """A disabled tenant arms nothing.

    Goes through the real `FollowUpService`, not the API, because the failure this guards is
    a column that saves correctly and is never consulted. A real conversation is used rather
    than a hand-built row, because `conversations` carries a composite tenant foreign key and
    a fabricated connection id would fail the constraint instead of testing anything.
    """
    from app.services.follow_ups import FollowUpService

    conversation_id = await _seed_conversation(owner, world["tenant"])

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        tenant = await session.get(Tenant, world["tenant"])
        tenant.follow_up_enabled = False

    service = FollowUpService(db, world["tenant"], settings=fu_settings(), clock=clock)
    assert await service.on_reply_sent(conversation_id) is None, (
        "a tenant with follow-ups switched off armed a sequence anyway"
    )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        tasks = (await session.execute(select(ScheduledTask))).scalars().all()
    assert tasks == [], "a disabled tenant scheduled follow-ups"


async def test_the_follow_up_prompt_carries_the_business_description(db, owner, world):
    """The second AI path must know the business too.

    The scheduler builds `FollowUpService` without a context provider, so before this the
    enhanced follow-up fell back to a provider with no identity - an AI rewrite that could
    contradict the tenant's own description. Asserted through the real service, not the
    provider class, because the provider being correct is not what was broken.
    """
    from app.services.follow_ups import FollowUpService

    async with owner() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        tenant = await session.get(Tenant, world["tenant"])
        tenant.business_name = "Acme Boats"
        tenant.business_description = "We sell and service boats in Leeds."

    service = FollowUpService(db, world["tenant"])
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        context = await service._build_context(session)

    rendered = (await context.get_context(world["tenant"])).render()

    assert "Acme Boats" in rendered
    assert "service boats in Leeds" in rendered
    assert "No service or product detail has been configured" not in rendered


async def test_an_injected_context_provider_still_wins(db, owner, world):
    """The fix must not override a caller that supplied its own provider."""
    from app.services.ai_prompt import StaticContextProvider
    from app.services.follow_ups import FollowUpService

    injected = StaticContextProvider(business_name="Injected Name")
    service = FollowUpService(db, world["tenant"], context_provider=injected)

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        context = await service._build_context(session)

    assert context is injected


async def test_defaults_can_be_restored(api, owner, world):
    """A tenant that has customised its sequence needs a way back.

    Restoring writes `{}`, which is the document the engine already treats as "use the
    built-in sequence" - so there is no second copy of the defaults anywhere to drift.
    """
    await signed_in(api, owner, world["owner"], world["tenant"])
    assert (await api.put("/api/v1/tenant/follow-ups", json={"enabled": False})) is not None

    saved = await api.put(
        "/api/v1/tenant/follow-ups",
        json={"steps": [{"step": 1, "delay_hours": 9, "template": "Hi {first_name}!"}]},
    )
    assert saved.status_code == 200
    assert saved.json()["is_default"] is False
    assert saved.json()["steps"][0]["delay_hours"] == 9

    restored = await api.put("/api/v1/tenant/follow-ups", json={"reset": True})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["is_default"] is True
    assert body["steps"], "restoring left the tenant with no sequence at all"
    assert body["steps"][0]["delay_hours"] != 9
    # Reset touches the sequence only: an explicit `enabled: false` sent earlier survives it.
    assert body["enabled"] is False

    both = await api.put(
        "/api/v1/tenant/follow-ups",
        json={"reset": True, "steps": [{"step": 1, "delay_hours": 4, "template": "Hi"}]},
    )
    assert both.status_code == 422


async def test_availability_matches_the_providers_the_registry_would_actually_dial():
    """The reported capability must be the registry's ladder, not a re-derivation of it.

    The bug this guards is narrow and was real: reasoning from `ai_primary_provider` reported
    "available" for a Gemini primary that has a key but `gemini_enabled=false` - a
    configuration in which the ladder is empty and every AI call raises a configuration
    error. A tenant would have been told AI was on and found out from unanswered leads.
    """
    from app.core.config import get_settings

    base = get_settings()

    def with_(**overrides):
        return base.model_copy(
            update={
                "ai_enabled": True,
                "groq_api_key": None,
                "gemini_enabled": False,
                "gemini_api_key": None,
                **overrides,
            }
        )

    # Groq keyed: in the ladder.
    assert platform_ai_available(with_(groq_api_key="k")) == (True, None)
    # Gemini keyed and enabled: in the ladder.
    assert platform_ai_available(with_(gemini_api_key="k", gemini_enabled=True)) == (True, None)
    # Gemini keyed but disabled: the registry will not dial it, so it is not available.
    assert platform_ai_available(
        with_(gemini_api_key="k", gemini_enabled=False, ai_primary_provider="gemini")
    ) == (False, AI_UNAVAILABLE_NO_PROVIDER)
    # Nothing keyed at all.
    assert platform_ai_available(with_()) == (False, AI_UNAVAILABLE_NO_PROVIDER)
    # The master switch outranks every key.
    assert platform_ai_available(with_(groq_api_key="k", ai_enabled=False)) == (
        False,
        AI_UNAVAILABLE_DISABLED,
    )


async def test_an_enabled_tenant_still_arms_normally(db, owner, world, clock):
    """The control case, so the test above cannot pass because the engine is simply broken."""
    from app.services.follow_ups import FollowUpService

    conversation_id = await _seed_conversation(owner, world["tenant"])
    service = FollowUpService(db, world["tenant"], settings=fu_settings(), clock=clock)

    armed = await service.on_reply_sent(conversation_id)

    assert armed is not None, "an enabled tenant armed nothing, so the switch test proves nothing"


async def _seed_conversation(owner, tenant_id) -> uuid.UUID:
    """A conversation with a real connection, so the composite FK is satisfied."""
    from tests.factories import create_meta_connection

    connection = await create_meta_connection(
        owner, tenant_id=tenant_id, page_id=f"9{uuid.uuid4().hex[:15]}"
    )
    async with owner() as session, session.begin():
        conversation = Conversation(
            tenant_id=tenant_id,
            meta_connection_id=connection,
            channel="MESSENGER",
            meta_participant_id=f"PSID-{uuid.uuid4().hex[:10]}",
            last_inbound_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()
        return conversation.id


async def test_a_claimed_task_is_not_executed_after_the_tenant_switches_it_off(
    db, owner, world, clock
):
    """The switch applies to work already claimed, not just to newly-armed sequences.

    A tenant that switches follow-ups off while the scheduler is mid-batch must not have the
    messages it was in the middle of sending go out anyway.
    """
    from app.services.follow_ups import FollowUpService
    from tests.fakes import FakeGraph
    from tests.follow_up_fixtures import claim_and_execute

    conversation_id = await _seed_conversation(owner, world["tenant"])
    service = FollowUpService(db, world["tenant"], settings=fu_settings(), clock=clock)
    armed = await service.on_reply_sent(conversation_id)
    assert armed is not None

    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant"])
        tenant = await session.get(Tenant, world["tenant"])
        tenant.follow_up_enabled = False

    clock.advance(hours=5)
    graph = FakeGraph()
    result = await claim_and_execute(db, world["tenant"], clock, graph=graph)

    assert graph.sends == [], "a disabled tenant sent a claimed follow-up"
    assert result.outcomes, "the claimed task produced no outcome at all"
    assert all(o.reason == "tenant_disabled" for o in result.outcomes), [
        o.reason for o in result.outcomes
    ]


# ==================================================================== team members


async def test_an_admin_can_onboard_a_member_and_get_one_time_credentials(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])

    response = await api.post(
        "/api/v1/tenant/members",
        json={"email": "New@Person.test", "name": "Nia New", "role": "MEMBER"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == "new@person.test"
    assert body["role"] == "MEMBER"
    assert len(body["one_time_password"]) >= 12
    # The password must never be anything the admin chose, and never stored in the clear.
    assert body["one_time_password"] != body["email"]

    async with owner() as session, session.begin():
        user = (
            await session.execute(select(User).where(User.email == "new@person.test"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(TenantMembership).where(TenantMembership.user_id == user.id)
            )
        ).scalar_one()
    assert membership.tenant_id == world["tenant"]
    assert membership.role is Role.MEMBER
    assert body["one_time_password"] not in user.password_hash
    assert user.password_hash.startswith("scrypt$")


async def test_the_new_member_can_authenticate_and_use_the_app(api, owner, world):
    """End to end: onboard, sign in with the one-time password, read the workspace."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    created = (
        await api.post(
            "/api/v1/tenant/members",
            json={"email": "member2@acme.test", "name": "Mo Member", "role": "MEMBER"},
        )
    ).json()

    api.cookies.clear()
    login = await api.post(
        "/api/v1/auth/login",
        json={"email": "member2@acme.test", "password": created["one_time_password"]},
    )

    assert login.status_code == 200, login.text
    assert (await api.get("/api/v1/auth/me")).json()["email"] == "member2@acme.test"
    assert (await api.get("/api/v1/tenants")).status_code == 200


async def test_the_new_member_can_change_the_one_time_password(api, owner, world):
    """The whole reason the generated password is acceptable: it is replaceable."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    created = (
        await api.post(
            "/api/v1/tenant/members",
            json={"email": "member3@acme.test", "name": "Mem Three", "role": "MEMBER"},
        )
    ).json()

    api.cookies.clear()
    await api.post(
        "/api/v1/auth/login",
        json={"email": "member3@acme.test", "password": created["one_time_password"]},
    )
    changed = await api.post(
        "/api/v1/auth/password",
        json={
            "current_password": created["one_time_password"],
            "new_password": "a-permanent-password",
        },
    )

    assert changed.status_code == 204, changed.text
    api.cookies.clear()
    assert (
        await api.post(
            "/api/v1/auth/login",
            json={"email": "member3@acme.test", "password": "a-permanent-password"},
        )
    ).status_code == 200


async def test_changing_a_password_revokes_other_sessions(api, owner, world):
    """The old credential was shown to an admin, so it must stop working everywhere else."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    created = (
        await api.post(
            "/api/v1/tenant/members",
            json={"email": "member4@acme.test", "name": "Mem Four", "role": "MEMBER"},
        )
    ).json()
    old = created["one_time_password"]

    # A second browser, signed in with the one-time password.
    other_session = await issue_session(owner, user_id=created["user_id"])

    api.cookies.clear()
    await api.post("/api/v1/auth/login", json={"email": "member4@acme.test", "password": old})
    assert (
        await api.post(
            "/api/v1/auth/password",
            json={"current_password": old, "new_password": "a-permanent-password"},
        )
    ).status_code == 204

    api.cookies.set(SESSION_COOKIE, other_session)
    assert (await api.get("/api/v1/auth/me")).status_code == 401, (
        "a session signed in with the exposed password survived a password change"
    )


async def test_a_wrong_current_password_is_refused(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.post(
        "/api/v1/auth/password",
        json={"current_password": "not-it", "new_password": "a-permanent-password"},
    )
    assert response.status_code == 401


async def test_a_short_new_password_is_refused(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.post(
        "/api/v1/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": "short"},
    )
    assert response.status_code == 422


async def test_creating_a_duplicate_member_email_is_refused(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    payload = {"email": "dupe@acme.test", "name": "Dupe", "role": "MEMBER"}
    assert (await api.post("/api/v1/tenant/members", json=payload)).status_code == 201

    again = await api.post("/api/v1/tenant/members", json=payload)

    assert again.status_code in (409, 422), again.text


async def test_an_invalid_role_is_refused(api, owner, world):
    await signed_in(api, owner, world["owner"], world["tenant"])
    response = await api.post(
        "/api/v1/tenant/members",
        json={"email": "bad@acme.test", "name": "Bad", "role": "SUPERADMIN"},
    )
    assert response.status_code == 422


async def test_member_onboarding_is_tenant_scoped(api, owner, world):
    """An admin of tenant A cannot add anybody to tenant B."""
    await signed_in(api, owner, world["owner"], world["tenant"])
    created = (
        await api.post(
            "/api/v1/tenant/members",
            json={"email": "scoped@acme.test", "name": "Scoped", "role": "MEMBER"},
        )
    ).json()

    async with owner() as session, session.begin():
        memberships = (
            (
                await session.execute(
                    select(TenantMembership).where(TenantMembership.user_id == created["user_id"])
                )
            )
            .scalars()
            .all()
        )
    assert [m.tenant_id for m in memberships] == [world["tenant"]]


async def test_a_member_cannot_onboard_somebody(api, owner, world):
    """Configuration and team management are admin work, matching every other mutation."""
    await signed_in(api, owner, world["member"], world["tenant"])
    response = await api.post(
        "/api/v1/tenant/members",
        json={"email": "sneaky@acme.test", "name": "Sneaky", "role": "OWNER"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.ROLE_REQUIRED


async def test_another_tenants_admin_cannot_read_or_write_this_tenants_config(api, owner, world):
    await signed_in(api, owner, world["outsider"], world["other"])

    assert (await api.get("/api/v1/tenant/profile")).status_code == 200
    # They get their own tenant's profile, never Acme's.
    assert (await api.get("/api/v1/tenant/profile")).json()["workspace_name"] == "Other"

    read = await api.get("/api/v1/tenant/profile", headers={TENANT_HEADER: str(world["tenant"])})
    assert read.status_code == 403
    write = await api.put(
        "/api/v1/tenant/profile",
        json={"business_description": "hijacked"},
        headers={TENANT_HEADER: str(world["tenant"])},
    )
    assert write.status_code == 403

    async with owner() as session, session.begin():
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == world["tenant"]))
        ).scalar_one()
    assert tenant.business_description is None, "a cross-tenant write landed"


async def test_an_unauthenticated_caller_reaches_none_of_it(api, world):
    for method, path, payload in (
        ("GET", "/api/v1/tenant/profile", None),
        ("PUT", "/api/v1/tenant/profile", {"business_name": "x"}),
        ("GET", "/api/v1/tenant/ai", None),
        ("PUT", "/api/v1/tenant/ai", {"enabled": True}),
        ("GET", "/api/v1/tenant/follow-ups", None),
        ("PUT", "/api/v1/tenant/follow-ups", {"enabled": False}),
        ("POST", "/api/v1/tenant/members", {"email": "a@b.test", "name": "A", "role": "MEMBER"}),
    ):
        response = await api.request(method, path, json=payload)
        assert response.status_code == 401, f"{method} {path} was reachable unauthenticated"


async def test_a_member_cannot_change_control_plane_settings(api, owner, world):
    await signed_in(api, owner, world["member"], world["tenant"])
    for method, path, payload in (
        ("PUT", "/api/v1/tenant/profile", {"business_name": "x"}),
        ("PUT", "/api/v1/tenant/ai", {"enabled": False}),
        ("PUT", "/api/v1/tenant/follow-ups", {"enabled": False}),
    ):
        response = await api.request(method, path, json=payload)
        assert response.status_code == 403, f"{method} {path} let a MEMBER through"
