"""Add local BGE embedding segments and indexing state.

Revision ID: 20260717_0004
Revises: 20260716_0003
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260717_0004"
down_revision: str | None = "20260716_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column(
            "embedding_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "document_chunks",
        sa.Column(
            "embedding_metadata",
            JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "document_chunks",
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_document_chunks_embedding_status_values",
        "document_chunks",
        "embedding_status IN ('pending', 'completed', 'failed', 'skipped')",
    )

    op.create_table(
        "document_chunk_embeddings",
        sa.Column("id", UUID, server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("document_chunk_id", UUID, nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("model_revision", sa.String(length=128), nullable=False),
        sa.Column("backend", sa.String(length=64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("dimensions = 384", name="ck_document_chunk_embeddings_dimensions_384"),
        sa.CheckConstraint(
            "segment_index >= 0",
            name="ck_document_chunk_embeddings_segment_index_nonnegative",
        ),
        sa.CheckConstraint(
            "token_count > 0",
            name="ck_document_chunk_embeddings_token_count_positive",
        ),
        sa.ForeignKeyConstraint(
            ["document_chunk_id"],
            ["document_chunks.id"],
            name="fk_document_chunk_embeddings_document_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunk_embeddings"),
        sa.UniqueConstraint(
            "document_chunk_id",
            "segment_index",
            "model_name",
            "model_revision",
            name="uq_chunk_embeddings_chunk_segment_model_revision",
        ),
    )
    op.create_index(
        "ix_chunk_embeddings_document_chunk_id",
        "document_chunk_embeddings",
        ["document_chunk_id"],
    )
    op.create_index(
        "ix_chunk_embeddings_embedding_hnsw",
        "document_chunk_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chunk_embeddings_embedding_hnsw",
        table_name="document_chunk_embeddings",
        postgresql_using="hnsw",
    )
    op.drop_index(
        "ix_chunk_embeddings_document_chunk_id",
        table_name="document_chunk_embeddings",
    )
    op.drop_table("document_chunk_embeddings")
    op.drop_constraint(
        "ck_document_chunks_embedding_status_values",
        "document_chunks",
        type_="check",
    )
    op.drop_column("document_chunks", "embedded_at")
    op.drop_column("document_chunks", "embedding_metadata")
    op.drop_column("document_chunks", "embedding_status")
