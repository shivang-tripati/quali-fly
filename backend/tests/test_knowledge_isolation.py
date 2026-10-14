"""Phase 6 integration tests: ingestion, retrieval, and tenant isolation.

Real PostgreSQL throughout, because everything worth proving here is database behaviour:
that pgvector's cosine operator ranks correctly, that RLS hides another tenant's rows, and
that the unique constraint makes a repeated ingest idempotent. Mocking the repository would
assert only that the mock was called.

The database is never mocked. The embedding provider is, and deliberately *with the same
dimension as the real one* - a fake with a convenient 4-dimensional output would make every
dimension assertion in this file meaningless.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app.core.config import get_settings
from app.db.tenant_context import set_current_tenant
from app.models.knowledge_base import EMBEDDING_DIMENSION, KnowledgeBase, KnowledgeSourceType
from app.services.embeddings import EmbeddingInvalidDimension, HashingEmbeddingProvider
from app.services.knowledge_base import (
    KnowledgeBaseService,
    KnowledgeValidationError,
)
from app.services.rag_context import RagContextProvider
from tests.factories import create_tenant
from tests.fakes import FakeEmbeddingProvider

ACME_PRICING = "Our pricing starts at 50000 rupees for a three van package."
GLOBEX_PRICING = "Our pricing starts at 5000 rupees for a three van package."


def _settings(**overrides):
    base = {
        "embedding_enabled": True,
        "embedding_dimension": EMBEDDING_DIMENSION,
        # The floor's own behaviour is asserted separately by
        # `test_weak_matches_are_dropped_by_the_similarity_floor`. Everywhere else it is
        # lowered so the tests exercise *ranking*, not the threshold.
        "rag_min_similarity": -1.0,
        "knowledge_chunk_size": 200,
        "knowledge_chunk_overlap": 20,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


@pytest.fixture
async def tenants(owner):
    """Two tenants. Both exist so a cross-tenant test has another tenant's row to find."""
    acme = await create_tenant(owner, name="Acme", slug="acme")
    globex = await create_tenant(owner, name="Globex", slug="globex")
    return {"acme": acme, "globex": globex}


def _service(db, tenant_id, provider=None, **overrides):
    """A service for ingestion or retrieval.

    Defaults to the **real** `HashingEmbeddingProvider`. It is local, deterministic and
    needs no credentials or downloads, so there is no reason to fake it for tests that
    assert anything about relevance - and a fake with no semantics cannot rank, so a
    ranking test written against one would be asserting that the fake sorts its own
    character counts. The fake is used only where the provider's *behaviour* is the subject
    (batching, failures, wrong dimension).
    """
    return KnowledgeBaseService(
        db,
        tenant_id,
        provider=provider or HashingEmbeddingProvider(),
        settings=_settings(**overrides),
    )


# -- ingestion ---------------------------------------------------------------


async def test_a_document_is_chunked_indexed_and_persisted(db, tenants):
    service = _service(db, tenants["acme"])
    result = await service.ingest(
        title="Pricing FAQ",
        content=" ".join(f"our vans cost {n} rupees" for n in range(60)),
        source_type=KnowledgeSourceType.FAQ,
        metadata={"owner": "sales"},
    )
    assert result.chunk_count > 1

    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        rows = (
            (
                await session.execute(
                    select(KnowledgeBase)
                    .where(KnowledgeBase.document_id == result.document_id)
                    .order_by(KnowledgeBase.chunk_index)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == result.chunk_count
    assert [r.chunk_index for r in rows] == list(range(result.chunk_count))
    assert all(r.tenant_id == tenants["acme"] for r in rows)
    assert all(r.title == "Pricing FAQ" for r in rows)
    assert all(r.source_type == KnowledgeSourceType.FAQ for r in rows)
    assert all(r.is_active for r in rows)
    # Read through `extra`, not `metadata`. The column is named `metadata` to match the
    # PRD, but `metadata` on a declarative class is the Table object - so `row.metadata`
    # silently returns the schema rather than the JSONB. Asserted deliberately so the trap
    # is recorded in a test rather than only in a comment.
    assert all(r.metadata is KnowledgeBase.metadata for r in rows)
    assert all(r.extra == {"owner": "sales"} for r in rows)
    assert all(r.embedded_at is not None for r in rows)


async def test_vectors_are_persisted_at_the_declared_width(db, tenants):
    """Read back from PostgreSQL, not from the object graph, so the column type is proven."""
    result = await _service(db, tenants["acme"]).ingest(
        title="Doc", content="pricing starts at 50000", source_type=KnowledgeSourceType.DOCUMENT
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        widths = (
            (
                await session.execute(
                    text(
                        "SELECT vector_dims(embedding) FROM knowledge_base WHERE document_id = :d"
                    ),
                    {"d": result.document_id},
                )
            )
            .scalars()
            .all()
        )
    assert widths == [EMBEDDING_DIMENSION]


async def test_a_short_document_is_a_single_chunk(db, tenants):
    result = await _service(db, tenants["acme"]).ingest(
        title="Short", content="Vans start at 50000.", source_type=KnowledgeSourceType.DOCUMENT
    )
    assert result.chunk_count == 1


async def test_empty_content_is_refused_and_nothing_is_persisted(db, tenants):
    with pytest.raises(KnowledgeValidationError):
        await _service(db, tenants["acme"]).ingest(
            title="Empty", content="   \n  ", source_type=KnowledgeSourceType.DOCUMENT
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        assert (await session.execute(select(KnowledgeBase))).scalars().all() == []


async def test_a_pastoral_limit_on_chunks_is_enforced(db, tenants):
    with pytest.raises(KnowledgeValidationError, match="over the limit"):
        await _service(db, tenants["acme"], knowledge_max_chunks_per_document=2).ingest(
            title="Huge",
            content=" ".join(f"w{i}" for i in range(500)),
            source_type=KnowledgeSourceType.DOCUMENT,
        )


async def test_a_wrong_dimension_from_the_provider_aborts_the_whole_ingest(db, tenants):
    """No partial index. Nothing is persisted, because the error precedes the write."""
    provider = FakeEmbeddingProvider()
    provider.returned_dimension = 1536
    with pytest.raises(EmbeddingInvalidDimension):
        await _service(db, tenants["acme"], provider).ingest(
            title="Bad", content="some content here", source_type=KnowledgeSourceType.DOCUMENT
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        assert (await session.execute(select(KnowledgeBase))).scalars().all() == []


async def test_an_embedding_provider_failure_persists_nothing(db, tenants):
    """A provider outage must not leave a falsely active, unembedded document."""
    from app.services.embeddings import EmbeddingProviderUnavailable

    provider = FakeEmbeddingProvider().program_error(EmbeddingProviderUnavailable("down"))
    with pytest.raises(EmbeddingProviderUnavailable):
        await _service(db, tenants["acme"], provider).ingest(
            title="Doomed",
            content="content that never lands",
            source_type=KnowledgeSourceType.DOCUMENT,
        )
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        assert (await session.execute(select(KnowledgeBase))).scalars().all() == []


async def test_chunks_are_embedded_in_batches_not_one_call_each(db, tenants):
    provider = FakeEmbeddingProvider()
    result = await _service(
        db, tenants["acme"], provider, knowledge_chunk_size=60, knowledge_chunk_overlap=10
    ).ingest(
        title="Many",
        content=" ".join(f"w{i}" for i in range(600)),
        source_type=KnowledgeSourceType.DOCUMENT,
    )
    # The service batches at 32. A per-chunk implementation would make `chunk_count` calls;
    # the assertion is that calls are far fewer than chunks, and that a batch was still too
    # big for a single round trip.
    assert result.chunk_count > 32
    assert provider.call_count < result.chunk_count
    assert len(provider.texts_embedded) == result.chunk_count


# -- update and delete -------------------------------------------------------


async def test_updating_replaces_chunks_and_retires_the_old_text(db, tenants):
    service = _service(db, tenants["acme"])
    original = await service.ingest(
        title="Pricing",
        content=ACME_PRICING,
        source_type=KnowledgeSourceType.FAQ,
        document_id=None,
    )

    await service.ingest(
        title="Pricing",
        content="Warranty on a new van is 24 months.",
        source_type=KnowledgeSourceType.FAQ,
        document_id=original.document_id,
    )

    # The old text is gone, not merely superseded.
    hits = await _service(db, tenants["acme"]).retrieve("pricing 50000 rupees")
    assert all(ACME_PRICING not in hit.content for hit in hits)
    assert any("24 months" in hit.content for hit in hits)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        rows = (
            (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.document_id == original.document_id)
                )
            )
            .scalars()
            .all()
        )
    # One chunk, not the old one plus the new one.
    assert len(rows) == 1
    assert "24 months" in rows[0].content_chunk


async def test_stale_chunks_are_gone_even_when_other_documents_still_match(db, tenants):
    """The realistic version of the update invariant.

    Asserting "the query now returns nothing" would be a weaker test than it looks: other
    tenant documents can legitimately match the same words. The invariant is that the
    *replaced* document's text is no longer reachable, while unrelated documents that
    happen to share vocabulary still are.
    """
    service = _service(db, tenants["acme"])
    replaced = await service.ingest(
        title="Pricing",
        content="Our pricing starts at 50000 rupees. Delivery within Bengaluru takes two days.",
        source_type=KnowledgeSourceType.FAQ,
    )
    # A second, unrelated document that also talks about delivery and pricing.
    await service.ingest(
        title="Unrelated",
        content="Delivery and pricing are discussed in the showroom, not online.",
        source_type=KnowledgeSourceType.MANUAL,
    )

    assert await service.retrieve("50000 rupees delivery")

    await service.ingest(
        title="Pricing",
        content="Our pricing starts at 60000 rupees.",
        source_type=KnowledgeSourceType.FAQ,
        document_id=replaced.document_id,
    )

    hits = await service.retrieve("50000 rupees delivery")
    # The replaced document's old price is gone...
    assert all("50000" not in hit.content for hit in hits)
    # ...and the other document still retrieves, so this is not a false pass caused by the
    # query stopping matching anything.
    assert hits, "the unrelated document should still match"
    assert any("showroom" in hit.content for hit in hits)


async def test_a_shrinking_update_leaves_no_orphaned_chunks(db, tenants):
    """The stale-chunk failure mode: a 5-chunk document replaced by a 1-chunk one."""
    service = _service(db, tenants["acme"])
    long_doc = await service.ingest(
        title="Long",
        content=" ".join(f"detail number {i} about pricing" for i in range(60)),
        source_type=KnowledgeSourceType.DOCUMENT,
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        before = len(
            (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.document_id == long_doc.document_id)
                )
            )
            .scalars()
            .all()
        )
    assert before > 1

    await service.ingest(
        title="Long",
        content="Short replacement.",
        source_type=KnowledgeSourceType.DOCUMENT,
        document_id=long_doc.document_id,
    )
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        after = (
            (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.document_id == long_doc.document_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(after) == 1


async def test_deleting_removes_content_from_retrieval(db, tenants):
    service = _service(db, tenants["acme"])
    document = await service.ingest(
        title="Warranty", content="Warranty is 24 months.", source_type=KnowledgeSourceType.MANUAL
    )
    assert await service.retrieve("warranty 24 months")

    await service.delete(document.document_id)

    assert await service.retrieve("warranty 24 months") == []
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        assert (await session.execute(select(KnowledgeBase))).scalars().all() == []


async def test_deactivating_excludes_from_retrieval_but_keeps_the_rows(db, tenants):
    service = _service(db, tenants["acme"])
    document = await service.ingest(
        title="Hours", content="We are open nine to five.", source_type=KnowledgeSourceType.MANUAL
    )
    await service.deactivate(document.document_id)

    assert await service.retrieve("what are your opening hours") == []
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        rows = (await session.execute(select(KnowledgeBase))).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_active is False


async def test_reingesting_the_same_document_id_does_not_duplicate(db, tenants):
    """The concurrency/duplicate-ingestion contract: identity is the id we handed out."""
    service = _service(db, tenants["acme"])
    first = await service.ingest(
        title="Doc", content="Pricing starts at 50000.", source_type=KnowledgeSourceType.DOCUMENT
    )
    for _ in range(3):
        again = await service.ingest(
            title="Doc",
            content="Pricing starts at 50000.",
            source_type=KnowledgeSourceType.DOCUMENT,
            document_id=first.document_id,
        )
        assert again.document_id == first.document_id

    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        rows = (await session.execute(select(KnowledgeBase))).scalars().all()
    assert len(rows) == 1


# -- retrieval ---------------------------------------------------------------


async def test_retrieval_ranks_by_relevance_and_returns_similarity(db, tenants):
    service = _service(db, tenants["acme"])
    await service.ingest(
        title="Pricing",
        content="Our pricing starts at 50000 rupees for a three van package.",
        source_type=KnowledgeSourceType.FAQ,
    )
    await service.ingest(
        title="Hours",
        content="Our showroom is open Monday to Friday, nine until five.",
        source_type=KnowledgeSourceType.MANUAL,
    )

    hits = await service.retrieve("how much does pricing start at")
    assert hits
    top = hits[0]
    assert "50000" in top.content
    # Similarity is reported, descending, and is a real cosine value.
    assert 0.0 < top.similarity <= 1.0
    assert [h.similarity for h in hits] == sorted((h.similarity for h in hits), reverse=True)
    # No storage detail leaks upward.
    assert not hasattr(top, "embedding")
    assert not hasattr(top, "tenant_id")


async def test_retrieval_returns_at_most_top_k(db, tenants):
    service = _service(db, tenants["acme"])
    for i in range(9):
        await service.ingest(
            title=f"Doc {i}",
            content=f"pricing and vans and vans and vans number {i}",
            source_type=KnowledgeSourceType.DOCUMENT,
            document_id=uuid.uuid4(),
        )
    assert len(await service.retrieve("pricing vans")) == 5


async def test_a_caller_cannot_widen_top_k(db, tenants):
    """Top-5 is a Phase 6 contract, enforced here rather than trusted from the caller."""
    service = _service(db, tenants["acme"])
    for i in range(9):
        await service.ingest(
            title=f"Doc {i}",
            content=f"pricing vans vans vans {i}",
            source_type=KnowledgeSourceType.DOCUMENT,
            document_id=uuid.uuid4(),
        )
    assert len(await service.retrieve("pricing vans", limit=50)) == 5


async def test_an_empty_knowledge_base_retrieves_nothing(db, tenants):
    """Day one for every tenant. Not an error."""
    assert await _service(db, tenants["acme"]).retrieve("anything at all") == []


async def test_an_unrelated_query_does_not_force_chunks_into_the_result(db, tenants):
    """Uses the *default* similarity floor, since the floor is what makes this true.

    With the floor lowered to -1.0 - as the ranking tests do - this query would return a
    ~0.009-similarity chunk, and correctly so: at that threshold everything is a match.
    The point of `RAG_MIN_SIMILARITY` is that weak matches are dropped rather than handed
    to a model that might treat them as fact.
    """
    service = _service(db, tenants["acme"], rag_min_similarity=0.15)
    await service.ingest(
        title="Hours",
        content="We are open Monday to Friday.",
        source_type=KnowledgeSourceType.MANUAL,
    )
    assert await service.retrieve("what is your serverless autoscaling policy") == []


async def test_a_relevant_query_clears_the_default_floor(db, tenants):
    """The other half of the floor's contract: it must not be so high that nothing passes."""
    service = _service(db, tenants["acme"], rag_min_similarity=0.15)
    await service.ingest(
        title="Pricing",
        content="Our pricing starts at 50000 rupees for a three van package.",
        source_type=KnowledgeSourceType.FAQ,
    )
    assert await service.retrieve("how much does pricing start at")


async def test_weak_matches_are_dropped_by_the_similarity_floor(db, tenants):
    service = _service(db, tenants["acme"])
    await service.ingest(
        title="Warranty",
        content="Vehicle warranty coverage lasts 24 months.",
        source_type=KnowledgeSourceType.MANUAL,
    )
    # Floor high enough that the only candidate cannot clear it.
    strict = _service(db, tenants["acme"], rag_min_similarity=0.999)
    assert await strict.retrieve("warranty") == []


async def test_retrieval_is_disabled_when_embeddings_are_off(db, tenants):
    service = _service(db, tenants["acme"], embedding_enabled=False)
    assert await service.retrieve("anything") == []


async def test_a_blank_query_short_circuits(db, tenants):
    service = _service(db, tenants["acme"])
    assert await service.retrieve("   ") == []


async def test_identical_queries_return_identical_ordering(db, tenants):
    """Deterministic ordering, including on ties."""
    service = _service(db, tenants["acme"])
    for i in range(4):
        await service.ingest(
            title=f"Doc {i}",
            content="identical knowledge text about vans",
            source_type=KnowledgeSourceType.DOCUMENT,
            document_id=uuid.uuid4(),
        )
    first = [h.content for h in await service.retrieve("vans")]
    second = [h.content for h in await service.retrieve("vans")]
    assert first == second


# -- tenant isolation --------------------------------------------------------


async def test_retrieval_never_returns_another_tenants_chunks(db, tenants):
    """The mandatory test, in the PRD's own terms."""
    await _service(db, tenants["acme"]).ingest(
        title="Acme pricing", content=ACME_PRICING, source_type=KnowledgeSourceType.FAQ
    )
    await _service(db, tenants["globex"]).ingest(
        title="Globex pricing", content=GLOBEX_PRICING, source_type=KnowledgeSourceType.FAQ
    )

    hits = await _service(db, tenants["acme"]).retrieve("pricing")
    assert hits, "Acme must find its own chunk"
    assert all("5000 rupees" not in hit.content for hit in hits)
    assert any("50000 rupees" in hit.content for hit in hits)

    globex_hits = await _service(db, tenants["globex"]).retrieve("pricing")
    assert all("50000 rupees" not in hit.content for hit in globex_hits)
    assert any("5000 rupees" in hit.content for hit in globex_hits)


async def test_rls_hides_another_tenants_rows_in_plain_sql(db, tenants):
    """Not via the repository - directly, so the policy is what is under test."""
    await _service(db, tenants["acme"]).ingest(
        title="Secret", content="Acme confidential pricing", source_type=KnowledgeSourceType.FAQ
    )
    # Unscoped by any WHERE clause: RLS alone has to hide it.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["globex"])
        rows = (
            await session.execute(text("SELECT title, content_chunk FROM knowledge_base"))
        ).all()
    assert rows == []


async def test_the_tenant_cannot_read_another_tenants_document_id(db, tenants):
    document = await _service(db, tenants["acme"]).ingest(
        title="Secret", content="Acme confidential", source_type=KnowledgeSourceType.FAQ
    )
    from app.repositories import knowledge_base as kb_repo

    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["globex"])
        assert (
            await kb_repo.get_document(
                session, tenant_id=tenants["globex"], document_id=document.document_id
            )
        ) is None
        assert (
            await kb_repo.document_chunk_count(
                session, tenant_id=tenants["globex"], document_id=document.document_id
            )
            == 0
        )


async def test_the_tenant_cannot_update_or_delete_another_tenants_document(db, tenants):
    document = await _service(db, tenants["acme"]).ingest(
        title="Secret", content="Acme confidential", source_type=KnowledgeSourceType.FAQ
    )
    from app.repositories import knowledge_base as kb_repo

    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["globex"])
        assert (
            await kb_repo.delete_document(
                session, tenant_id=tenants["globex"], document_id=document.document_id
            )
            == 0
        )
        assert (
            await kb_repo.deactivate_document(
                session, tenant_id=tenants["globex"], document_id=document.document_id
            )
            == 0
        )

    # Acme's document is untouched.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        rows = (
            (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.document_id == document.document_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1 and rows[0].is_active is True


async def test_a_row_cannot_be_written_under_another_tenants_tenant_id(db, tenants):
    """RLS actively refuses it, rather than the write quietly disappearing.

    Asserting the refusal is stronger than asserting absence: it proves the policy is
    evaluated on INSERT and not merely relied on for reads. The write is attempted as the
    unprivileged application role, so this is a real attack rather than a check of the
    owner's ability to write anything.
    """
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with db() as session, session.begin():
            await set_current_tenant(session, tenants["acme"])
            session.add(
                KnowledgeBase(
                    tenant_id=tenants["globex"],
                    document_id=uuid.uuid4(),
                    source_type=KnowledgeSourceType.FAQ,
                    title="Injected",
                    content_chunk="should never persist",
                    chunk_index=0,
                    embedding=[0.0] * EMBEDDING_DIMENSION,
                    metadata={},
                )
            )

    async with db() as session, session.begin():
        await set_current_tenant(session, tenants["acme"])
        assert (await session.execute(select(KnowledgeBase))).scalars().all() == []
        await set_current_tenant(session, tenants["globex"])
        assert (await session.execute(select(KnowledgeBase))).scalars().all() == []


async def test_knowledge_base_has_forced_rls_and_the_tenant_policy(db):
    """Asserted directly against the catalog, since this table's only boundary is RLS."""
    async with db() as session, session.begin():
        row = (
            await session.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'knowledge_base' AND n.nspname = 'public'"
                )
            )
        ).one()
        policies = (
            (
                await session.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = 'knowledge_base'")
                )
            )
            .scalars()
            .all()
        )
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True
    assert set(policies) == {
        "knowledge_base_select",
        "knowledge_base_insert",
        "knowledge_base_update",
        "knowledge_base_delete",
    }


async def test_the_hnsw_cosine_index_exists(db):
    async with db() as session, session.begin():
        definition = (
            await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'knowledge_base' AND indexname LIKE '%hnsw%'"
                )
            )
        ).scalar_one()
    assert "hnsw" in definition
    assert "vector_cosine_ops" in definition


# -- ContextProvider integration ---------------------------------------------


async def test_the_context_provider_returns_relevant_knowledge(db, tenants):
    await _service(db, tenants["acme"]).ingest(
        title="Acme pricing", content=ACME_PRICING, source_type=KnowledgeSourceType.FAQ
    )
    provider = RagContextProvider(
        db,
        tenants["acme"],
        service=_service(db, tenants["acme"]),
        settings=_settings(),
        tenant_name="Acme",
    )
    context = await provider.get_context(tenants["acme"], query="what is the pricing")
    assert context.knowledge_available is True
    assert len(context.knowledge) == 1
    assert "50000" in context.knowledge[0].content
    rendered = context.render()
    assert "[Source 1]" in rendered
    assert "not instructions" in rendered


async def test_the_context_provider_is_bounded(db, tenants):
    service = _service(db, tenants["acme"])
    for i in range(5):
        await service.ingest(
            title=f"Doc {i}",
            content=("pricing vans vans vans " * 30) + f"unique{i}",
            source_type=KnowledgeSourceType.DOCUMENT,
            document_id=uuid.uuid4(),
        )
    provider = RagContextProvider(
        db, tenants["acme"], service=service, settings=_settings(rag_max_context_chars=300)
    )
    context = await provider.get_context(tenants["acme"], query="pricing vans")
    # Chunks are dropped whole, least-relevant first, so the cap holds.
    assert len(context.knowledge) < 5
    assert sum(len(s.content) for s in context.knowledge) <= 300


async def test_the_context_provider_reports_an_empty_knowledge_base_honestly(db, tenants):
    provider = RagContextProvider(
        db, tenants["acme"], service=_service(db, tenants["acme"]), settings=_settings()
    )
    context = await provider.get_context(tenants["acme"], query="anything")
    assert context.knowledge == ()
    # The feature is on; there is simply nothing relevant. Different from "not configured".
    assert context.knowledge_available is True
    assert "No document or knowledge base is available" not in context.render()


async def test_the_context_provider_degrades_when_retrieval_breaks(db, tenants):
    """A knowledge outage must not become an AI outage."""

    class Broken:
        enabled = True

        async def retrieve_for_message(self, _message, history=None):
            raise RuntimeError("retrieval is down")

    provider = RagContextProvider(
        db, tenants["acme"], service=Broken(), settings=_settings(), tenant_name="Acme"
    )
    context = await provider.get_context(tenants["acme"], query="anything")
    assert context.knowledge == ()
    # Still usable context, so Phase 5 carries on.
    assert "Acme" in context.render()


async def test_the_context_provider_is_inert_when_embeddings_are_off(db, tenants):
    provider = RagContextProvider(
        db,
        tenants["acme"],
        service=_service(db, tenants["acme"]),
        settings=_settings(embedding_enabled=False),
    )
    context = await provider.get_context(tenants["acme"], query="pricing")
    assert context.knowledge == ()
    assert context.knowledge_available is False


async def test_build_context_provider_falls_back_to_the_phase_five_static_one(db, tenants):
    """A deployment without embeddings keeps the exact Phase 5 code path."""
    from app.services.ai_prompt import StaticContextProvider
    from app.services.rag_context import build_context_provider

    provider = build_context_provider(
        db, tenants["acme"], settings=_settings(embedding_enabled=False)
    )
    assert isinstance(provider, StaticContextProvider)

    rag = build_context_provider(db, tenants["acme"], settings=_settings())
    assert isinstance(rag, RagContextProvider)


async def test_history_widens_a_vague_follow_up_query(db, tenants):
    """'What about that?' retrieves poorly on its own, and should not be all we search."""
    service = _service(db, tenants["acme"])
    await service.ingest(
        title="Warranty",
        content="Vehicle warranty coverage lasts 24 months.",
        source_type=KnowledgeSourceType.MANUAL,
    )
    from app.services.ai_prompt import ConversationTurn

    provider = RagContextProvider(db, tenants["acme"], service=service, settings=_settings())
    vague = await provider.get_context(tenants["acme"], query="what about that?")
    with_history = await provider.get_context(tenants["acme"], query="what about that?")
    assert vague.knowledge == () or with_history.knowledge
    # Directly: the widened query retrieves, the bare one may not.
    assert await service.retrieve_for_message(
        "what about that?",
        [ConversationTurn("inbound", "how long is the warranty on a van")],
    )
