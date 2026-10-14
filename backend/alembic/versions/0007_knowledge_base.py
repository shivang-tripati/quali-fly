"""knowledge base: knowledge_base

Revision ID: 0007_knowledge_base
Revises: 0006_ai_qualification
Create Date: 2026-09-28

Adds `knowledge_base`: one row per chunk of tenant-owned content, with a `vector(384)`
embedding, ENABLE + FORCE ROW LEVEL SECURITY with the same tenant policy as the rest of the
schema, and an HNSW cosine index for semantic ranking.

Embedding dimension
-------------------
`vector(384)` is written literally, and deliberately not read from the environment. Two
reasons:

  * A migration whose result depends on a runtime setting is not reproducible. Two
    environments running the same revision could end up with different column widths.
  * Changing the embedding model is a schema change with a re-indexing cost, so it has to
    be a reviewable code edit rather than a config flip. See the re-indexing note in the
    README.

384 is `HashingEmbeddingProvider.dimension` (see `app/services/embeddings.py`), and
`tests/test_knowledge_isolation.py` asserts the two agree, so the constant cannot drift
silently.

pgvector was already enabled in 0001, so this revision does not touch the extension - and
deliberately does not drop it in `downgrade`, since 0001 owns it.

Document identity
-----------------
`document_id` groups chunks into a document so update and delete can name what they
replace. It is a plain UUID column, not a foreign key to a second table: the PRD stores
chunks directly, and this is the smallest addition that makes replacement well-defined
rather than guessed.

Indexes
-------
  * HNSW on `(embedding vector_cosine_ops)` - the PRD specifies cosine, and HNSW gives
    better recall/latency than IVFFlat at this scale. IVFFlat is not added as a second
    index because there is no evidence here that it is needed, and two indexes over the
    same column means two sets of tuning to justify.
  * `(tenant_id, is_active)` - retrieval filters on both before ranking, so this serves
    the filter and prunes the candidate set the HNSW scan considers.
  * `UNIQUE (tenant_id, document_id, chunk_index)` - makes a repeated ingest of identical
    content idempotent instead of duplicating it.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0007_knowledge_base"
down_revision: str | None = "0006_ai_qualification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

TENANT_MATCH = "tenant_id = nullif(current_setting('app.current_tenant', true), '')::uuid"

#: Must match `HashingEmbeddingProvider.dimension` and `EMBEDDING_DIMENSION` in
#: `app/models/knowledge_base.py`. All three are asserted equal in the test suite.
EMBEDDING_DIMENSION = 384


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_select ON {table} FOR SELECT USING ({TENANT_MATCH})")
    op.execute(f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK ({TENANT_MATCH})")
    op.execute(
        f"CREATE POLICY {table}_update ON {table} "
        f"FOR UPDATE USING ({TENANT_MATCH}) WITH CHECK ({TENANT_MATCH})"
    )
    op.execute(f"CREATE POLICY {table}_delete ON {table} FOR DELETE USING ({TENANT_MATCH})")


def _disable_rls(table: str) -> None:
    for suffix in ("delete", "update", "insert", "select"):
        op.execute(f"DROP POLICY IF EXISTS {table}_{suffix} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.create_table(
        "knowledge_base",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum("faq", "document", "website", "manual", name="knowledge_source_type"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content_chunk", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        # `pgvector.sqlalchemy.Vector` rather than a raw string: the same class the ORM
        # model uses, so the column and the attribute cannot disagree about width or type.
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("embedded_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "chunk_index",
            name="uq_knowledge_base_document_chunk",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_knowledge_base_tenant",
            ondelete="CASCADE",
        ),
    )

    op.create_index("ix_knowledge_base_tenant_active", "knowledge_base", ["tenant_id", "is_active"])
    op.create_index("ix_knowledge_base_document", "knowledge_base", ["tenant_id", "document_id"])
    # HNSW over the cosine operator, matching the PRD's `vector_cosine_ops`. The
    # parameters are pgvector's documented starting point; they are tuned by measurement
    # against real data, not guessed further here.
    op.execute(
        "CREATE INDEX ix_knowledge_base_embedding_hnsw ON knowledge_base "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    _enable_rls("knowledge_base")

    log.info("knowledge_base created with forced RLS and an HNSW cosine index")


def downgrade() -> None:
    _disable_rls("knowledge_base")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_base_embedding_hnsw")
    op.drop_index("ix_knowledge_base_document", table_name="knowledge_base")
    op.drop_index("ix_knowledge_base_tenant_active", table_name="knowledge_base")
    op.drop_table("knowledge_base")
    # The enum type survives `drop_table`, and leaving it behind makes the next
    # `upgrade` fail with `type "knowledge_source_type" already exists`. Same trap as
    # migration 0006, so it is handled explicitly.
    sa.Enum(name="knowledge_source_type").drop(op.get_bind(), checkfirst=True)
    # `vector` extension is owned by 0001 and is deliberately left in place.
