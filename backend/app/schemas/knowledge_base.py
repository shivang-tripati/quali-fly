"""Knowledge Base request and response schemas.

Typed responses, never SQLAlchemy entities, matching the convention in
`app/schemas/conversation.py`.

The vector is deliberately absent from every response. An embedding is internal storage
detail with no use to a client, and 384 floats per chunk in a list response would be a way
to ship the index over an HTTP API. Chunk *counts* are exposed instead, which is what a
person managing documents actually wants to know.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.knowledge_base import KnowledgeSourceType


class KnowledgeDocumentCreate(BaseModel):
    """Ingest a document.

    `extra="forbid"`, and there is no `tenant_id` field to reject. Tenant identity comes
    from the authenticated request context; accepting one from the body would create a
    field whose only possible use is an escalation attempt.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_000_000)
    source_type: KnowledgeSourceType = KnowledgeSourceType.DOCUMENT
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Supplying an existing document_id replaces that document. Used by the API to make
    # `PUT` an update rather than a second create.
    document_id: uuid.UUID | None = None


class KnowledgeDocumentUpdate(BaseModel):
    """Replace a document's content.

    A full replacement rather than a patch: chunk boundaries are a function of the whole
    text, so a partial update would have to re-chunk from content the client did not send.
    That is a trap, so the API is a PUT with full content.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=1_000_000)
    source_type: KnowledgeSourceType = KnowledgeSourceType.DOCUMENT
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeDocumentSummary(BaseModel):
    """A document as it appears in a list.

    `extra="forbid"` on a *response* schema, deliberately. The default is to ignore unknown
    keys, and that is exactly how a field the router passes but the schema forgot to
    declare gets silently dropped from the API - a 201 whose body is missing `content`
    still looks like a success to the client. Forbidding extras turns that into a loud
    error at construction.
    """

    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    title: str
    source_type: KnowledgeSourceType
    is_active: bool
    chunk_count: int
    updated_at: datetime | None = None


class KnowledgeDocumentResponse(KnowledgeDocumentSummary):
    """A single document, with its chunks' text.

    `content` is the concatenation of the document's chunks, reconstructed for display.
    The stored form is chunked for retrieval; presenting it whole is what a reader expects
    and the ordering is deterministic, so the reconstruction is faithful.
    """

    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class KnowledgeDocumentListResponse(BaseModel):
    items: list[KnowledgeDocumentSummary]
    count: int


__all__ = [
    "KnowledgeDocumentCreate",
    "KnowledgeDocumentListResponse",
    "KnowledgeDocumentResponse",
    "KnowledgeDocumentSummary",
    "KnowledgeDocumentUpdate",
]
