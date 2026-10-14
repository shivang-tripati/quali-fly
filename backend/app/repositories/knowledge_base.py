"""Tenant-scoped reads and writes over `knowledge_base`.

Every statement here carries an explicit `tenant_id`. That is belt and braces rather than
redundancy: RLS is the real boundary, but a query that omits the tenant would rely on the
database to hide a mistake rather than not making it, and the retrieval query in
particular is the one that would quietly return another tenant's pricing.

The similarity query is deliberately the only place that ranks. Ranking happens in
PostgreSQL through pgvector's `<=>` cosine operator with the HNSW index; the alternative -
pulling vectors into Python and comparing them - would defeat the index, move the whole
table over the wire per query, and put a floating-point implementation in front of a
tenant boundary.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase, KnowledgeSourceType


async def insert_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    source_type: KnowledgeSourceType,
    title: str,
    chunks: list[dict[str, Any]],
) -> None:
    """Persist a document's chunks. `chunks` must already be embedded and validated."""
    if not chunks:
        return
    now = datetime.now(UTC)
    values = [
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "source_type": source_type,
            "title": title,
            "content_chunk": chunk["content_chunk"],
            "chunk_index": chunk["chunk_index"],
            "embedding": chunk["embedding"],
            "metadata": chunk["metadata"],
            "is_active": True,
            "embedded_at": now,
        }
        for chunk in chunks
    ]
    await session.execute(insert(KnowledgeBase).values(values))


async def delete_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    """Remove every chunk of a document. Returns how many rows went.

    Hard delete rather than `is_active = false`. Soft deletion would leave the vectors in
    place, still consuming an HNSW index slot, and would keep them one filter mistake away
    from being retrieved. Chunk counts are small, so the write cost is not worth an audit
    trail nothing in Phase 6 reads.
    """
    result = await session.execute(
        delete(KnowledgeBase).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.document_id == document_id,
        )
    )
    return result.rowcount or 0


async def deactivate_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    """Retire a document's chunks without deleting them."""
    result = await session.execute(
        KnowledgeBase.__table__.update()
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.document_id == document_id,
        )
        .values(is_active=False)
    )
    return result.rowcount or 0


async def get_document_chunks(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> list[KnowledgeBase]:
    return list(
        (
            await session.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.document_id == document_id,
                )
                .order_by(KnowledgeBase.chunk_index)
            )
        )
        .scalars()
        .all()
    )


async def get_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[KnowledgeBase, ...] | None:
    """A representative chunk for a document, or `None`.

    Returns a one-row tuple rather than a single row so callers can use the same
    `if not rows:` emptiness check as the plural call. The row carries the document's
    `title`, `source_type` and `metadata`, which are the fields the API exposes.
    """
    rows = (
        (
            await session.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.document_id == document_id,
                )
                .order_by(KnowledgeBase.chunk_index)
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows) or None


async def list_documents(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 50
) -> list[dict[str, Any]]:
    """List the tenant's documents with their chunk counts.

    Aggregated in SQL. The API is document-shaped but storage is chunk-shaped, so the
    grouping has to happen somewhere; doing it in one query beats loading every chunk to
    count them in Python.
    """
    rows = (
        await session.execute(
            select(
                KnowledgeBase.document_id,
                KnowledgeBase.title,
                KnowledgeBase.source_type,
                # `bool_and`, not `min`: PostgreSQL has no `min(boolean)`, and "is this
                # document active" means *all* of its chunks are. With `min` the intent
                # would also be wrong - a single deactivated chunk would read as the
                # document being partly live.
                func.bool_and(KnowledgeBase.is_active).label("is_active"),
                func.count().label("chunk_count"),
                func.max(KnowledgeBase.updated_at).label("updated_at"),
            )
            .where(KnowledgeBase.tenant_id == tenant_id)
            .group_by(
                KnowledgeBase.document_id,
                KnowledgeBase.title,
                KnowledgeBase.source_type,
            )
            .order_by(func.max(KnowledgeBase.updated_at).desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "document_id": row.document_id,
            "title": row.title,
            "source_type": row.source_type,
            "is_active": bool(row.is_active),
            "chunk_count": int(row.chunk_count),
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


async def document_chunk_count(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.document_id == document_id,
        )
    )
    return int(count or 0)


#: pgvector's cosine distance operator is `<=>`: lower is closer. Similarity is `1 - d`, so
#: a distance of 0 is a similarity of 1. It is reached through pgvector's `cosine_distance`
#: comparator rather than a raw `op(...)`, because the comparator declares the return type
#: and keeps the subtraction out of the operator - see `search_similar`.


async def search_similar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query_embedding: list[float],
    limit: int,
    min_similarity: float = 0.0,
) -> list[dict[str, Any]]:
    """Rank a tenant's active chunks against `query_embedding`.

    The distance is computed in a subquery so `<=>` is evaluated **once** against the
    HNSW index rather than repeated in the SELECT, the WHERE and the ORDER BY. The
    similarity floor is applied inside that subquery, so weakly-matching chunks never
    enter the candidate set at all - which matters to the index, not just to tidiness.

    Two things here are deliberate and were both found the hard way:

    * `cosine_distance` is used rather than a raw `op("<=>")`. pgvector's comparator
      declares `return_type=Float`, which keeps the arithmetic out of the operator.
    * The similarity is `1 - distance` computed *outside* the subquery, not inside an
      expression wrapping the operator. Wrapping a typed vector literal in arithmetic
      alongside a float literal makes SQLAlchemy mis-assign the vector's bind processor
      to the `1.0` - the statement then fails with `expected list or ndarray` from a value
      that is a perfectly good float. Computing the subtraction over an already-computed
      column sidesteps that entirely.

    Ties are broken by `(similarity DESC, document_id, chunk_index)`. Without that, two
    equally-relevant chunks could come back in either order between calls, and a
    "deterministic where possible" promise is worth nothing if the tie is the common case.
    """
    ranked = (
        select(
            KnowledgeBase.document_id.label("document_id"),
            KnowledgeBase.title.label("title"),
            KnowledgeBase.content_chunk.label("content_chunk"),
            KnowledgeBase.source_type.label("source_type"),
            KnowledgeBase.extra.label("metadata"),
            # Selected so the outer ORDER BY can use it as a tie-break. Left out, the
            # ordering expression has no column to point at.
            KnowledgeBase.chunk_index.label("chunk_index"),
            KnowledgeBase.embedding.cosine_distance(query_embedding).label("distance"),
        )
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_active.is_(True),
            # `similarity >= min` restated as a distance bound, so the index does the
            # filtering rather than the database scanning and discarding afterwards.
            KnowledgeBase.embedding.cosine_distance(query_embedding) <= (1.0 - min_similarity),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                ranked.c.document_id,
                ranked.c.title,
                ranked.c.content_chunk,
                ranked.c.source_type,
                ranked.c.metadata,
                (1.0 - ranked.c.distance).label("similarity"),
            )
            .order_by(ranked.c.distance, ranked.c.document_id, ranked.c.chunk_index)
            .limit(limit)
        )
    ).all()
    return [
        {
            "document_id": row.document_id,
            "title": row.title,
            "content": row.content_chunk,
            "source_type": row.source_type,
            "metadata": row.metadata or {},
            "similarity": float(row.similarity),
        }
        for row in rows
    ]


__all__ = [
    "deactivate_document",
    "delete_document",
    "document_chunk_count",
    "get_document",
    "get_document_chunks",
    "insert_chunks",
    "list_documents",
    "search_similar",
]
