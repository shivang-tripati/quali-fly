"""Ingestion lifecycle and semantic retrieval.

The transaction discipline is inherited from Phase 5 and for the same reason: embedding
is a call to the outside world and must not happen with a transaction open. Ingestion
therefore reads and validates in one short transaction, closes it, embeds with nothing
held, then persists in a second short transaction.

The consequence is the interesting part. If the process dies between the two writes, the
document simply does not exist - no rows, no partially-active state, nothing retrievable.
That is a much better failure than persisting chunks first and embedding afterwards,
which would leave chunks marked `is_active` that either have no vector or have a stale
one. `AI_ENABLED` had exactly this shape of risk in Phase 5, and here the answer is the
same: a half-applied knowledge document is worse than an absent one, because the AI would
answer from incomplete content and sound confident about it.

Update is a delete-then-insert rather than a diff. Simpler, and it cannot leave a stale
chunk behind - the invariant that matters is "a replaced document must not leave stale
active chunks in retrieval", and a full replacement satisfies it by construction.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.db.tenant_context import set_current_tenant
from app.models.knowledge_base import KnowledgeBase, KnowledgeSourceType
from app.repositories import knowledge_base as kb_repo
from app.services.chunking import EmptyContent, TextChunk, chunk
from app.services.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    build_provider,
    validate_vector,
)

log = logging.getLogger("app.knowledge.service")

#: Batched so a 40-chunk document is a handful of calls, not 40. The provider protocol
#: takes a list precisely so callers are not tempted to loop one call per chunk.
EMBED_BATCH_SIZE = 32


@dataclass(frozen=True)
class KnowledgeContextChunk:
    """One retrieved chunk, as the AI layer sees it.

    Carries no vector, no tenant id, and no database detail. The Phase 6 boundary is the
    point at which storage representation stops leaking upward, and a typed result is how
    that is enforced rather than merely intended.
    """

    title: str
    content: str
    similarity: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class IngestResult:
    document_id: uuid.UUID
    chunk_count: int
    provider: str
    model: str
    embedding_latency_ms: int


class KnowledgeBaseService:
    """Tenant-scoped knowledge ingestion and retrieval."""

    def __init__(
        self,
        session_factory,
        tenant_id: uuid.UUID,
        *,
        provider: EmbeddingProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._settings = settings or get_settings()
        self._provider = provider or build_provider(self._settings)

    @property
    def enabled(self) -> bool:
        return self._settings.embedding_enabled

    # -- ingestion --------------------------------------------------------

    async def ingest(
        self,
        *,
        title: str,
        content: str,
        source_type: KnowledgeSourceType,
        metadata: dict[str, Any] | None = None,
        document_id: uuid.UUID | None = None,
    ) -> IngestResult:
        """Chunk, embed, and persist a document. Replaces any existing `document_id`.

        `document_id` is supplied by the caller rather than derived from the content, so a
        retried request replaces rather than duplicates. Passing a new id creates; passing
        an existing one replaces.
        """
        # -- validate and chunk, no database open -------------------------
        try:
            chunks = chunk(
                content,
                chunk_size=self._settings.knowledge_chunk_size,
                overlap=self._settings.knowledge_chunk_overlap,
            )
        except EmptyContent as exc:
            raise KnowledgeValidationError("knowledge content must not be empty") from exc
        if len(chunks) > self._settings.knowledge_max_chunks_per_document:
            raise KnowledgeValidationError(
                f"content produced {len(chunks)} chunks, over the limit of "
                f"{self._settings.knowledge_max_chunks_per_document}"
            )

        doc_id = document_id or uuid.uuid4()
        title = title.strip()

        # -- embed, no transaction open -----------------------------------
        vectors = await self._embed([c.text for c in chunks])

        # -- persist -------------------------------------------------------
        rows = [
            {
                "content_chunk": piece.text,
                "chunk_index": piece.index,
                "embedding": vector,
                "metadata": metadata or {},
            }
            for piece, vector in zip(chunks, vectors, strict=True)
        ]
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            # Delete before insert, inside one transaction, so a concurrent reader can
            # never see the document with no chunks or with both versions.
            if document_id is not None:
                await kb_repo.delete_document(
                    session, tenant_id=self._tenant_id, document_id=doc_id
                )
            await kb_repo.insert_chunks(
                session,
                tenant_id=self._tenant_id,
                document_id=doc_id,
                source_type=source_type,
                title=title,
                chunks=rows,
            )
        return IngestResult(
            document_id=doc_id,
            chunk_count=len(chunks),
            provider=self._provider.name,
            model=self._provider.model,
            embedding_latency_ms=0,
        )

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed in batches, validating every vector before it can be persisted.

        Validation happens here rather than in the repository so a bad vector never
        reaches a transaction. There is no padding and no truncation: a wrong-length vector
        is an `EmbeddingInvalidDimension`, which aborts the whole ingest.
        """
        vectors: list[list[float]] = []
        started = time.perf_counter()
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            for vector in await self._provider.embed(batch):
                vectors.append(validate_vector(vector, self._provider.dimension))
        log.info(
            "knowledge.embedded",
            extra={
                "tenant_id": str(self._tenant_id),
                "texts": len(texts),
                "batches": (len(texts) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE,
                "provider": self._provider.name,
                "model": self._provider.model,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return vectors

    async def delete(self, document_id: uuid.UUID) -> int:
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            return await kb_repo.delete_document(
                session, tenant_id=self._tenant_id, document_id=document_id
            )

    async def deactivate(self, document_id: uuid.UUID) -> int:
        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            return await kb_repo.deactivate_document(
                session, tenant_id=self._tenant_id, document_id=document_id
            )

    # -- retrieval --------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[KnowledgeContextChunk]:
        """Semantic search within the tenant. Returns at most `rag_top_k` chunks.

        An empty result is a normal answer, not a failure. A tenant with no knowledge base,
        or a query nothing resembles, yields `[]` and the AI pipeline carries on with no
        retrieved content - inventing knowledge to fill the gap is explicitly not an option
        this system takes.
        """
        if not self.enabled:
            return []
        query = (query or "").strip()
        if not query:
            return []
        # Top-5 is a Phase 6 contract: the cap is applied here, at the boundary, so no
        # caller can ask for twenty chunks and push the trimming decision up into the AI
        # layer where it does not belong.
        top_k = min(limit or self._settings.rag_top_k, self._settings.rag_top_k)
        top_k = max(1, top_k)

        started = time.perf_counter()
        # Embed the query with no transaction open, same rule as ingestion.
        vectors = await self._embed([query])
        if not vectors:
            return []

        async with self._session_factory() as session, session.begin():
            await set_current_tenant(session, self._tenant_id)
            rows = await kb_repo.search_similar(
                session,
                tenant_id=self._tenant_id,
                query_embedding=vectors[0],
                limit=top_k,
                min_similarity=self._settings.rag_min_similarity,
            )
        results = [
            KnowledgeContextChunk(
                title=row["title"],
                content=row["content"],
                similarity=row["similarity"],
                metadata=row["metadata"],
            )
            for row in rows
        ]
        log.info(
            "knowledge.retrieved",
            extra={
                "tenant_id": str(self._tenant_id),
                "query_chars": len(query),
                "returned": len(results),
                "top_k": top_k,
                "min_similarity": self._settings.rag_min_similarity,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return results

    async def retrieve_for_message(
        self, message: str, history: list[Any] | None = None
    ) -> list[KnowledgeContextChunk]:
        """Retrieve using the current message, widened by recent turns.

        A bare "what about that?" retrieves poorly, because it shares no vocabulary with
        the document it is asking about. Prepending a little conversation gives the embedder
        something to match on without letting history dominate the query. Bounded, because
        an unbounded concatenation would let a long thread drown out the actual question.
        """
        if not self.enabled:
            return []
        parts: list[str] = []
        for turn in (history or [])[-3:]:
            text = getattr(turn, "text", None)
            if text:
                parts.append(text[:500])
        query = " ".join([*parts, (message or "").strip()])
        return await self.retrieve(query[:2000])


class KnowledgeValidationError(ValueError):
    """Input the caller can fix. Distinct from an `EmbeddingError`, which it cannot."""


async def knowledge_row_count(session_factory, tenant_id: uuid.UUID) -> int:
    """Chunk count for a tenant. Used by tests and health diagnostics."""
    async with session_factory() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return int(
            await session.scalar(
                select(func.count())
                .select_from(KnowledgeBase)
                .where(KnowledgeBase.tenant_id == tenant_id)
            )
            or 0
        )


__all__ = [
    "EmbeddingError",
    "IngestResult",
    "KnowledgeBaseService",
    "KnowledgeContextChunk",
    "KnowledgeValidationError",
    "TextChunk",
    "knowledge_row_count",
]
