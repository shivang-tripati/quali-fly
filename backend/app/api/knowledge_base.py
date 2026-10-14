"""Tenant-scoped Knowledge Base CRUD.

Exists because a knowledge base nobody can populate is not a feature. Every operation is
scoped by the authenticated `TenantContext` and enforced again by RLS.

There is no `tenant_id` parameter or request field anywhere in this module. The tenant
comes from the session, and the database enforces the rest - the same rule the leads and
conversations routers follow.

Writes use ADMIN, reads use MEMBER. Writing knowledge changes what the AI tells every
future lead on the tenant's behalf, so it is treated as an act rather than a read. No
`AGENT` role is introduced; Phase 1 has three and this does not get a fourth.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUserSession, require_minimum_role
from app.core.auth import TenantContext
from app.core.errors import ApiError, ErrorCode, error_responses
from app.models.membership import Role
from app.repositories import knowledge_base as kb_repo
from app.schemas.knowledge_base import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentResponse,
    KnowledgeDocumentSummary,
    KnowledgeDocumentUpdate,
)
from app.services.knowledge_base import (
    EmbeddingError,
    KnowledgeBaseService,
    KnowledgeValidationError,
)

router = APIRouter(prefix="/knowledge-base", tags=["knowledge-base"])

log = logging.getLogger("app.knowledge.api")

ReadCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]
WriteCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.ADMIN))]


def _service(session, context: TenantContext) -> KnowledgeBaseService:
    return KnowledgeBaseService(_factory_for(session), context.tenant_id)


def _factory_for(_session):
    """A session factory for the service.

    The service opens its own short transactions because ingestion embeds outside any
    transaction - see `services/knowledge_base.py`. The request session cannot be reused
    for that, so the application's factory is resolved here. The request's transaction
    still owns the tenant context; every session the service opens re-establishes it from
    the same tenant id, which is what RLS keys on.
    """
    from app.db.session import get_session_factory

    return get_session_factory()


async def _load_document(session, context: TenantContext, document_id: uuid.UUID):
    rows = await kb_repo.get_document(session, tenant_id=context.tenant_id, document_id=document_id)
    if rows is None:
        return None
    chunks = await kb_repo.get_document_chunks(
        session, tenant_id=context.tenant_id, document_id=document_id
    )
    return rows[0], chunks


def _summary(row, chunk_count: int) -> KnowledgeDocumentSummary:
    return KnowledgeDocumentSummary(
        document_id=row.document_id,
        title=row.title,
        source_type=row.source_type,
        is_active=bool(row.is_active),
        chunk_count=chunk_count,
        updated_at=row.updated_at,
    )


def _full(row, chunks) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        document_id=row.document_id,
        title=row.title,
        source_type=row.source_type,
        is_active=bool(row.is_active),
        chunk_count=len(chunks),
        # Reconstructed in `chunk_index` order, which the repository guarantees, so this is
        # the original text rather than a shuffled approximation of it.
        content="\n\n".join(chunk.content_chunk for chunk in chunks),
        metadata=row.extra or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post(
    "",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a knowledge document. Supplying document_id replaces that document.",
    responses=error_responses(400, 401, 403, 503),
)
async def create_document(
    payload: KnowledgeDocumentCreate,
    session: CurrentUserSession,
    context: WriteCtx,
) -> KnowledgeDocumentResponse:
    service = _service(session, context)
    try:
        result = await service.ingest(
            title=payload.title,
            content=payload.content,
            source_type=payload.source_type,
            metadata=payload.metadata,
            document_id=payload.document_id,
        )
    except KnowledgeValidationError as exc:
        raise ApiError(400, ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    except EmbeddingError as exc:
        # 503 rather than 500: the tenant's content is fine, the embedding capability is
        # not available. A retry may well succeed, and the caller should not assume the
        # document was half-written.
        log.warning("knowledge.ingest.embedding_failed", extra={"error": type(exc).__name__})
        raise ApiError(
            503, ErrorCode.INTERNAL_ERROR, "Knowledge embedding is unavailable."
        ) from exc

    loaded = await _load_document(session, context, result.document_id)
    assert loaded is not None  # just written, inside this request's transaction
    return _full(*loaded)


@router.get(
    "",
    response_model=KnowledgeDocumentListResponse,
    summary="List the tenant's knowledge documents.",
    responses=error_responses(401, 403),
)
async def list_documents(
    session: CurrentUserSession,
    context: ReadCtx,
    limit: int = 50,
) -> KnowledgeDocumentListResponse:
    bounded = max(1, min(limit, 200))
    rows = await kb_repo.list_documents(session, tenant_id=context.tenant_id, limit=bounded)
    items = [
        KnowledgeDocumentSummary(
            document_id=row["document_id"],
            title=row["title"],
            source_type=row["source_type"],
            is_active=row["is_active"],
            chunk_count=row["chunk_count"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
    return KnowledgeDocumentListResponse(items=items, count=len(items))


@router.get(
    "/{document_id}",
    response_model=KnowledgeDocumentResponse,
    summary="Fetch one document. Scoped by RLS, so another tenant's is simply absent.",
    responses=error_responses(401, 403, 404),
)
async def get_document(
    document_id: uuid.UUID,
    session: CurrentUserSession,
    context: ReadCtx,
) -> KnowledgeDocumentResponse:
    loaded = await _load_document(session, context, document_id)
    if loaded is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Knowledge document not found.")
    return _full(*loaded)


@router.put(
    "/{document_id}",
    response_model=KnowledgeDocumentResponse,
    summary="Replace a document's content, re-chunking and re-embedding it.",
    responses=error_responses(400, 401, 403, 404, 503),
)
async def update_document(
    document_id: uuid.UUID,
    payload: KnowledgeDocumentUpdate,
    session: CurrentUserSession,
    context: WriteCtx,
) -> KnowledgeDocumentResponse:
    # Existence is checked in the request's transaction first, so an update to a document
    # that does not exist is a 404 rather than a silent create. RLS decides what exists.
    existing = await _load_document(session, context, document_id)
    if existing is None:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Knowledge document not found.")

    service = _service(session, context)
    try:
        await service.ingest(
            title=payload.title,
            content=payload.content,
            source_type=payload.source_type,
            metadata=payload.metadata,
            document_id=document_id,
        )
    except KnowledgeValidationError as exc:
        raise ApiError(400, ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    except EmbeddingError as exc:
        log.warning("knowledge.update.embedding_failed", extra={"error": type(exc).__name__})
        raise ApiError(
            503, ErrorCode.INTERNAL_ERROR, "Knowledge embedding is unavailable."
        ) from exc

    loaded = await _load_document(session, context, document_id)
    assert loaded is not None
    return _full(*loaded)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and all of its chunks.",
    responses=error_responses(401, 403, 404),
)
async def delete_document(
    document_id: uuid.UUID,
    session: CurrentUserSession,
    context: WriteCtx,
) -> None:
    # Counted in the request's transaction so a cross-tenant delete is a 404 and does not
    # report success for a document it never touched.
    count = await kb_repo.document_chunk_count(
        session, tenant_id=context.tenant_id, document_id=document_id
    )
    if count == 0:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Knowledge document not found.")
    await _service(session, context).delete(document_id)
    log.info(
        "knowledge.deleted",
        extra={
            "tenant_id": str(context.tenant_id),
            "document_id": str(document_id),
            "chunk_count": count,
        },
    )


__all__ = ["router"]
