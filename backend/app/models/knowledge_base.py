"""Tenant-owned knowledge chunks.

The PRD stores chunks directly in a `knowledge_base` table, and this follows that. What it
adds is one column - `document_id` - and that is the minimum needed to make the lifecycle
correct rather than merely plausible:

Without it, `PUT /knowledge-base/{id}` has no way to know which existing rows the new
content should replace. "Replace the document" would have to mean "delete whatever the
client thinks it wrote", which is not a rule anyone can implement reliably. A plain
grouping column gives document identity, so update is `DELETE WHERE document_id = X`
followed by fresh inserts, and delete is the same delete. No second table, no version
chain, no hash-guessing business rule - the client holds the id it was given at create.

    knowledge_base
      ├── document_id = A, chunk_index = 0, is_active = true
      ├── document_id = A, chunk_index = 1, is_active = true
      └── document_id = B, chunk_index = 0, is_active = true

`embedding` is `vector(384)`, matching `HashingEmbeddingProvider`. The dimension is
written literally in the migration rather than read from the environment on purpose: a
migration that depends on a runtime setting is not reproducible, and changing the
dimension has to be a reviewable code change. See the re-indexing note in the README.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

#: Must equal `HashingEmbeddingProvider.dimension`. Duplicated deliberately rather than
#: imported: a migration and a runtime module must be able to disagree visibly, because
#: silently inheriting a changed constant is how a column ends up one width off its data.
#: `tests/test_knowledge_isolation.py` asserts they agree.
EMBEDDING_DIMENSION = 384


class KnowledgeSourceType(enum.StrEnum):
    """The PRD's vocabulary, and only the PRD's vocabulary.

    Phase 6 ingests content the tenant supplies. `pdf`, `url`, `notion` and friends are
    *connectors*, and a connector that quietly starts fetching remote URLs is a
    server-side request forgery surface nobody reviewed - so the vocabulary stays closed
    until a phase that is actually about connecting somewhere.
    """

    FAQ = "faq"
    DOCUMENT = "document"
    WEBSITE = "website"
    MANUAL = "manual"


class KnowledgeBase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One chunk of one tenant's knowledge."""

    __tablename__ = "knowledge_base"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: Groups chunks into a document. The client receives this at create time and uses it
    #: for update and delete; it is not derived from content, so no guessing is involved.
    document_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_type: Mapped[KnowledgeSourceType] = mapped_column(
        SAEnum(
            KnowledgeSourceType,
            name="knowledge_source_type",
            native_enum=True,
            # `values_callable` is load-bearing here, and is the only place in this schema
            # that needs it. The PRD's vocabulary is lowercase (`faq`, `document`), but
            # SQLAlchemy persists enum *member names* by default, which are `FAQ` and
            # `DOCUMENT` - and the database would reject every insert. Every other enum in
            # this schema has name == value, which is why they get away without it.
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_chunk: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    # The SQLAlchemy declarative class already owns a `metadata` attribute (it *is*
    # `Table.metadata`), so a column named `metadata` has to be declared under another
    # attribute name. The database column is still `metadata`, as the PRD specifies.
    extra: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: When the chunk was last embedded. Diagnostic only - `updated_at` is the column other
    #: phases use for the same question.
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The dedupe guard. Two chunks of the same document cannot share a position, which
        # is what makes a retried ingest of identical content idempotent rather than
        # duplicating it.
        UniqueConstraint(
            "tenant_id", "document_id", "chunk_index", name="uq_knowledge_base_document_chunk"
        ),
        # Plain tenant FK rather than a composite `(id, tenant_id)` one. The composite
        # pattern in Phases 2-5 stops a row pointing at another tenant's *child*; this
        # table has exactly one parent, `tenants`, whose `id` is already a primary key. A
        # composite would be extra machinery for an identical guarantee.
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_knowledge_base_tenant",
            ondelete="CASCADE",
        ),
        # Retrieval filters on tenant and active state before ranking, so the composite
        # index is the one the planner wants. The HNSW index is declared in the migration,
        # not here: it is an operational tuning choice rather than part of the model.
        Index("ix_knowledge_base_tenant_active", "tenant_id", "is_active"),
        Index("ix_knowledge_base_document", "tenant_id", "document_id"),
    )


__all__ = ["EMBEDDING_DIMENSION", "KnowledgeBase", "KnowledgeSourceType"]
